import hashlib
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import MetaData, select, text, update
from sqlalchemy.exc import IntegrityError

from .infra import authenticate, engine_for, keys_from_env, now, public_job, save_upload, uid
from .queue import claim, fail, finish, heartbeat, job_table, lease_condition, new_job
from .schemas import Completion, Failure, Job, Lease, Transcript, URLImport


@dataclass
class Settings:
    database_url: str
    storage: Path
    api_keys: dict[str, str]
    worker_token: str
    max_upload_bytes: int = 256 * 1024 * 1024

    @classmethod
    def from_env(cls):
        root = Path(os.environ.get("ASR_STORAGE", "./data/asr")).resolve()
        root.mkdir(parents=True, exist_ok=True)
        token = os.environ.get("ASR_WORKER_TOKEN", "")
        if len(token) < 24:
            raise RuntimeError("ASR_WORKER_TOKEN must contain at least 24 characters")
        return cls(
            os.environ.get("ASR_DATABASE_URL", f"sqlite:///{root / 'asr.db'}"),
            root,
            keys_from_env("ASR_API_KEYS"),
            token,
            int(os.environ.get("MAX_UPLOAD_BYTES", 256 * 1024 * 1024)),
        )


def create_app(settings: Settings | None = None):
    cfg = settings or Settings.from_env()
    cfg.storage.mkdir(parents=True, exist_ok=True)
    metadata = MetaData()
    jobs = job_table(metadata)
    engine = engine_for(cfg.database_url)
    if engine.dialect.name == "sqlite":
        metadata.create_all(engine)

    @asynccontextmanager
    async def lifespan(_):
        yield
        engine.dispose()

    app = FastAPI(
        title="GigaAM API",
        version="0.1.0",
        lifespan=lifespan,
        description="Асинхронная транскрибация. Русский язык, сегменты и таймкоды. Авторизация: Bearer token.",
    )
    app.state.engine, app.state.jobs, app.state.settings = engine, jobs, cfg

    def owner(authorization: Annotated[str | None, Header()] = None):
        return authenticate(authorization, cfg.api_keys)

    def worker(authorization: Annotated[str | None, Header()] = None):
        return authenticate(authorization, {cfg.worker_token: "worker"})

    def owned(ident, who):
        with engine.connect() as db:
            row = (
                db.execute(
                    select(jobs).where(jobs.c.id == ident, jobs.c.owner == who, jobs.c.state != "deleted")
                )
                .mappings()
                .first()
            )
        if not row:
            raise HTTPException(404, "Задача не найдена")
        return dict(row)

    def add_job(who, payload, key, fingerprint, path=None):
        row = new_job(who, uid(), "transcription", payload, key, fingerprint)
        try:
            with engine.begin() as db:
                db.execute(jobs.insert().values(**row))
        except IntegrityError:
            if path:
                path.unlink(missing_ok=True)
            with engine.connect() as db:
                old = (
                    db.execute(select(jobs).where(jobs.c.owner == who, jobs.c.idempotency_key == key))
                    .mappings()
                    .first()
                )
            if not old or old["fingerprint"] != fingerprint or old["state"] == "deleted":
                raise HTTPException(409, "Idempotency-Key уже использован для другого запроса")
            row = old
        except BaseException:
            if path:
                path.unlink(missing_ok=True)
            raise
        return public_job(row)

    @app.get("/healthz")
    def health():
        with engine.connect() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "service": "gigaam-api"}

    @app.post("/v1/transcriptions", status_code=202, response_model=Job)
    async def upload(
        file: Annotated[UploadFile, File()],
        who: str = Depends(owner),
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        path, name, size, sha = await save_upload(file, cfg.storage / "media", cfg.max_upload_bytes)
        return add_job(
            who,
            {"source": "file", "path": str(path), "filename": name, "size": size},
            idempotency_key,
            sha,
            path,
        )

    @app.post("/v1/imports", status_code=202, response_model=Job)
    def import_url(
        body: URLImport,
        who: str = Depends(owner),
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        from .download import validate_url

        try:
            validate_url(body.url)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return add_job(
            who,
            {"source": "url", "url": body.url},
            idempotency_key,
            hashlib.sha256(body.url.encode()).hexdigest(),
        )

    @app.get("/v1/transcriptions/{ident}", response_model=Job)
    def status(ident: str, who: str = Depends(owner)):
        return public_job(owned(ident, who))

    @app.get("/v1/transcriptions/{ident}/result", response_model=Transcript)
    def result(ident: str, who: str = Depends(owner)):
        row = owned(ident, who)
        if row["state"] != "completed":
            raise HTTPException(409, "Результат ещё не готов")
        return row["result"]

    @app.post("/v1/transcriptions/{ident}/retry", status_code=202, response_model=Job)
    def retry(ident: str, who: str = Depends(owner)):
        owned(ident, who)
        with engine.begin() as db:
            row = (
                db.execute(
                    update(jobs)
                    .where(jobs.c.id == ident, jobs.c.state == "failed")
                    .values(state="queued", attempts=0, error_code=None, available_at=now(), updated_at=now())
                    .returning(jobs)
                )
                .mappings()
                .first()
            )
        if not row:
            raise HTTPException(409, "Повтор доступен только для завершившейся ошибкой задачи")
        return public_job(row)

    def purge(who, ident=None, key=None):
        with engine.begin() as db:
            condition = jobs.c.id == ident if ident else jobs.c.idempotency_key == key
            row = (
                db.execute(select(jobs).where(condition, jobs.c.owner == who).with_for_update())
                .mappings()
                .first()
            )
            if not row and key:
                tombstone = new_job(who, uid(), "transcription", {}, key)
                tombstone["state"] = "deleted"
                try:
                    with db.begin_nested():
                        db.execute(jobs.insert().values(**tombstone))
                    row = tombstone
                except IntegrityError:
                    row = (
                        db.execute(select(jobs).where(condition, jobs.c.owner == who).with_for_update())
                        .mappings()
                        .one()
                    )
            if not row:
                raise HTTPException(404, "Задача не найдена")
            path = row["payload"].get("path") or row["payload"].get("pending_delete_path")
            ident = row["id"]
            db.execute(
                update(jobs)
                .where(jobs.c.id == ident)
                .values(
                    state="deleted",
                    result=None,
                    payload={"pending_delete_path": path} if path else {},
                    lease_token=None,
                    lease_until=None,
                    updated_at=now(),
                )
            )
        # Retain only the deletion path if the filesystem is temporarily unavailable.
        if path:
            Path(path).unlink(missing_ok=True)
            with engine.begin() as db:
                db.execute(
                    update(jobs).where(jobs.c.id == ident, jobs.c.state == "deleted").values(payload={})
                )
        return Response(status_code=204)

    @app.delete("/v1/transcriptions/{ident}", status_code=204)
    def delete(ident: str, who: str = Depends(owner)):
        return purge(who, ident=ident)

    @app.delete("/v1/requests/{key}", status_code=204)
    def cancel_request(key: str, who: str = Depends(owner)):
        if not 1 <= len(key) <= 200:
            raise HTTPException(422, "Ключ должен содержать от 1 до 200 символов")
        # Tombstoning the unique owner/key pair also blocks a late or repeated upload.
        return purge(who, key=key)

    @app.post("/internal/jobs/claim", dependencies=[Depends(worker)])
    def claim_job(response: Response):
        row = claim(engine, jobs)
        if not row:
            response.status_code = 204
            return None
        payload = row["payload"]
        return {
            "id": row["id"],
            "lease_token": row["lease_token"],
            "source": payload["source"],
            "url": payload.get("url"),
            "max_upload_bytes": cfg.max_upload_bytes,
        }

    @app.get("/internal/jobs/{ident}/media", dependencies=[Depends(worker)])
    def media(ident: str, x_lease_token: Annotated[str, Header()]):
        with engine.connect() as db:
            row = (
                db.execute(select(jobs).where(lease_condition(jobs, ident, x_lease_token))).mappings().first()
            )
        if not row or not row["payload"].get("path"):
            raise HTTPException(409, "Аренда задачи истекла или файл недоступен")
        return FileResponse(row["payload"]["path"], media_type="application/octet-stream")

    @app.post("/internal/jobs/{ident}/heartbeat", dependencies=[Depends(worker)])
    def beat(ident: str, body: Lease):
        if not heartbeat(engine, jobs, ident, body.lease_token):
            raise HTTPException(409, "Аренда задачи истекла")
        return {"ok": True}

    @app.post("/internal/jobs/{ident}/complete", dependencies=[Depends(worker)])
    def complete(ident: str, body: Completion):
        if not finish(engine, jobs, ident, body.lease_token, body.result.model_dump()):
            raise HTTPException(409, "Аренда задачи истекла")
        return {"ok": True}

    @app.post("/internal/jobs/{ident}/fail", dependencies=[Depends(worker)])
    def failed(ident: str, body: Failure):
        if not fail(engine, jobs, ident, body.lease_token, body.code, body.retryable):
            raise HTTPException(409, "Аренда задачи истекла")
        return {"ok": True}

    return app
