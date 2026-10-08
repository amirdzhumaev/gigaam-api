from concurrent.futures import ThreadPoolExecutor

from conftest import B
from sqlalchemy import update

from gigaam_api.queue import claim
from gigaam_api.schemas import Segment, Transcript
from gigaam_api.worker import process_one


def upload(api, value=b"audio", key="one"):
    return api.post(
        "/v1/transcriptions", files={"file": ("voice.m4a", value)}, headers={"Idempotency-Key": key}
    )


def test_auth_and_owner_isolation(api, worker):
    assert api.get("/healthz", headers={"Authorization": ""}).status_code == 200
    assert upload(api).status_code == 202
    ident = upload(api).json()["id"]
    assert api.get(f"/v1/transcriptions/{ident}", headers={"Authorization": f"Bearer {B}"}).status_code == 404
    assert api.get(f"/v1/transcriptions/{ident}", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert api.post("/internal/jobs/claim").status_code == 401
    assert worker.get(f"/v1/transcriptions/{ident}").status_code == 401


def test_idempotency_and_upload_limits(api):
    first = upload(api).json()
    assert upload(api).json()["id"] == first["id"]
    assert upload(api, b"different").status_code == 409
    assert len(list((api.app.state.settings.storage / "media").iterdir())) == 1
    assert upload(api, b"x" * 1025, "large").status_code == 413
    assert upload(api, b"", "empty").status_code == 422
    assert api.post("/v1/transcriptions", files={"file": ("page.html", b"html")}).status_code == 415
    assert len(list((api.app.state.settings.storage / "media").iterdir())) == 1


def test_worker_roundtrip_and_restart(api, worker):
    class Recognizer:
        def transcribe(self, source, root):
            assert source.read_bytes() == b"audio"
            return Transcript(
                model="test",
                duration=2,
                text="Обсудили релиз.",
                segments=[Segment(id="s0", start=0, end=2, text="Обсудили релиз.")],
            )

    ident = upload(api).json()["id"]
    assert api.get(f"/v1/transcriptions/{ident}/result").status_code == 409
    api.app.state.engine.dispose()  # Result is recovered from storage, not process memory.
    assert process_one(worker, Recognizer())
    assert not process_one(worker, Recognizer())
    assert api.get(f"/v1/transcriptions/{ident}").json()["state"] == "completed"
    result = api.get(f"/v1/transcriptions/{ident}/result").json()
    assert result["segments"][0]["start"] == 0
    assert result["text"] == "Обсудили релиз."
    assert not list((api.app.state.settings.storage / "media").iterdir())


def test_stale_lease_and_delete_reject_late_completion(api, worker):
    ident = upload(api).json()["id"]
    old = worker.post("/internal/jobs/claim").json()
    engine, jobs = api.app.state.engine, api.app.state.jobs
    with engine.begin() as db:
        db.execute(update(jobs).where(jobs.c.id == ident).values(lease_until=0))
    current = worker.post("/internal/jobs/claim").json()
    assert old["lease_token"] != current["lease_token"]
    assert (
        worker.post(f"/internal/jobs/{ident}/heartbeat", json={"lease_token": old["lease_token"]}).status_code
        == 409
    )
    assert api.delete(f"/v1/transcriptions/{ident}").status_code == 204
    body = {
        "lease_token": current["lease_token"],
        "result": {"model": "test", "duration": 0, "text": "", "segments": []},
    }
    assert worker.post(f"/internal/jobs/{ident}/complete", json=body).status_code == 409
    assert api.get(f"/v1/transcriptions/{ident}").status_code == 404
    assert not list((api.app.state.settings.storage / "media").iterdir())


def test_concurrent_workers_claim_each_job_once(api):
    for index in range(12):
        assert upload(api, key=str(index)).status_code == 202
    engine, jobs = api.app.state.engine, api.app.state.jobs
    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = list(pool.map(lambda _: claim(engine, jobs), range(20)))
    ids = [job["id"] for job in claimed if job]
    assert len(ids) == len(set(ids)) == 12


def test_invalid_media_is_removed_and_requires_new_upload(api, worker):
    class BadRecognizer:
        def transcribe(self, *_):
            raise ValueError("invalid media")

    ident = upload(api).json()["id"]
    process_one(worker, BadRecognizer())
    failed = api.get(f"/v1/transcriptions/{ident}").json()
    assert (failed["state"], failed["error_code"]) == ("failed", "invalid_media")
    assert api.post(f"/v1/transcriptions/{ident}/retry").status_code == 409
    assert not list((api.app.state.settings.storage / "media").iterdir())


def test_invalid_timestamps_rejected(api, worker):
    ident = upload(api).json()["id"]
    lease = worker.post("/internal/jobs/claim").json()["lease_token"]
    body = {
        "lease_token": lease,
        "result": {
            "model": "test",
            "duration": 1,
            "text": "текст",
            "segments": [{"id": "s0", "start": 0, "end": 2, "text": "текст"}],
        },
    }
    assert worker.post(f"/internal/jobs/{ident}/complete", json=body).status_code == 422


def test_cancellation_blocks_late_upload_without_known_job_id(api):
    assert api.delete("/v1/requests/late-upload").status_code == 204
    assert upload(api, key="late-upload").status_code == 409
    assert not list((api.app.state.settings.storage / "media").iterdir())
    assert api.delete("/v1/requests/late-upload").status_code == 204


def test_cancellation_recovers_after_lost_submission_response(api):
    ident = upload(api, key="lost-response").json()["id"]
    assert api.delete("/v1/requests/lost-response").status_code == 204
    assert api.get(f"/v1/transcriptions/{ident}").status_code == 404
    assert not list((api.app.state.settings.storage / "media").iterdir())
    assert api.delete(f"/v1/transcriptions/{ident}").status_code == 204


def test_download_failure_is_not_mislabeled_as_corrupt_audio(api, worker, monkeypatch):
    from gigaam_api import worker as worker_module
    from gigaam_api.download import DownloadError

    class UnusedRecognizer:
        def transcribe(self, *args):
            raise AssertionError("Incomplete downloads must never be transcribed")

    for code in ("download_timeout", "download_too_large", "download_failed"):

        def failed_download(*args):
            raise DownloadError(code)

        monkeypatch.setattr(worker_module, "download_source", failed_download)
        job = api.post("/v1/imports", json={"url": "https://public.example/audio.mp3"}).json()
        assert process_one(worker, UnusedRecognizer())
        status = api.get(f"/v1/transcriptions/{job['id']}").json()
        assert status["state"] == "failed" and status["error_code"] == code


def test_retryable_failure_retains_input_until_attempts_are_exhausted(api, worker):
    ident = upload(api).json()["id"]
    media = api.app.state.settings.storage / "media"
    for attempt in range(3):
        with api.app.state.engine.begin() as db:
            db.execute(
                update(api.app.state.jobs).where(api.app.state.jobs.c.id == ident).values(available_at=0)
            )
        job = worker.post("/internal/jobs/claim").json()
        assert (
            worker.post(
                f"/internal/jobs/{ident}/fail",
                json={
                    "lease_token": job["lease_token"],
                    "code": "asr_unavailable",
                    "retryable": True,
                },
            ).status_code
            == 200
        )
        assert bool(list(media.iterdir())) == (attempt < 2)
    assert api.get(f"/v1/transcriptions/{ident}").json()["state"] == "failed"


def test_expired_last_lease_removes_terminal_input(api, worker):
    ident = upload(api).json()["id"]
    worker.post("/internal/jobs/claim")
    with api.app.state.engine.begin() as db:
        db.execute(
            update(api.app.state.jobs)
            .where(api.app.state.jobs.c.id == ident)
            .values(attempts=3, lease_until=0)
        )
    assert worker.post("/internal/jobs/claim").status_code == 204
    assert not list((api.app.state.settings.storage / "media").iterdir())


def test_failed_unlink_is_durable_and_retried_without_losing_transcript(api, worker, monkeypatch):
    from pathlib import Path

    from sqlalchemy import select

    from gigaam_api.media_cleanup import cleanup_terminal_media

    ident = upload(api).json()["id"]
    job = worker.post("/internal/jobs/claim").json()
    unlink = Path.unlink

    def unavailable(self, *args, **kwargs):
        if self.parent == api.app.state.settings.storage / "media":
            raise OSError("filesystem unavailable")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unavailable)
    assert (
        worker.post(
            f"/internal/jobs/{ident}/complete",
            json={
                "lease_token": job["lease_token"],
                "result": {"model": "test", "duration": 0, "text": "", "segments": []},
            },
        ).status_code
        == 200
    )
    assert api.get(f"/v1/transcriptions/{ident}/result").status_code == 200
    with api.app.state.engine.connect() as db:
        payload = db.execute(
            select(api.app.state.jobs.c.payload).where(api.app.state.jobs.c.id == ident)
        ).scalar()
    assert "path" not in payload and payload["pending_delete_path"]
    monkeypatch.setattr(Path, "unlink", unlink)
    cleanup_terminal_media(api.app.state.engine, api.app.state.jobs)
    assert not list((api.app.state.settings.storage / "media").iterdir())
