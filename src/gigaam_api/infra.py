import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine, event


def uid() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


def engine_for(url: str):
    kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {}
    engine = create_engine(url, pool_pre_ping=True, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def setup_sqlite(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


def keys_from_env(name: str) -> dict[str, str]:
    keys = json.loads(os.environ.get(name, "{}"))
    if (
        not isinstance(keys, dict)
        or not keys
        or any(
            not isinstance(k, str) or len(k) < 24 or not isinstance(v, str) or not v for k, v in keys.items()
        )
    ):
        raise RuntimeError(f"Set {name} to a JSON mapping of tokens (at least 24 characters) to owner IDs")
    return keys


def authenticate(header: str | None, keys: dict[str, str]) -> str:
    if not header or not header.startswith("Bearer "):
        raise HTTPException(401, "Требуется Bearer-токен", headers={"WWW-Authenticate": "Bearer"})
    token = header[7:]
    for key, owner in keys.items():
        if hmac.compare_digest(key.encode(), token.encode()):
            return owner
    raise HTTPException(401, "Неверный токен")


MEDIA_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".mov", ".aac", ".ogg", ".opus", ".flac", ".webm", ".mkv"}


async def save_upload(file: UploadFile, root: Path, limit: int):
    name = Path((file.filename or "audio.wav").replace("\\", "/")).name[:200]
    if Path(name).suffix.lower() not in MEDIA_EXTENSIONS:
        raise HTTPException(415, "Поддерживаются аудио- и видеофайлы")
    root.mkdir(parents=True, exist_ok=True)
    path = root / uid()
    size = 0
    digest = hashlib.sha256()
    try:
        with path.open("xb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "Файл превышает допустимый размер")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if not size:
            raise HTTPException(422, "Файл пуст")
        return path, name, size, digest.hexdigest()
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


def public_job(row):
    result = {
        k: row[k]
        for k in ("id", "resource_id", "kind", "state", "attempts", "error_code", "created_at", "updated_at")
    }
    result["stage"] = row["payload"].get("stage")
    return result
