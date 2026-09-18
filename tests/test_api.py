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


def test_invalid_media_is_terminal_and_retry_is_explicit(api, worker):
    class BadRecognizer:
        def transcribe(self, *_):
            raise ValueError("invalid media")

    ident = upload(api).json()["id"]
    process_one(worker, BadRecognizer())
    failed = api.get(f"/v1/transcriptions/{ident}").json()
    assert (failed["state"], failed["error_code"]) == ("failed", "invalid_media")
    assert api.post(f"/v1/transcriptions/{ident}/retry").json()["state"] == "queued"


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
