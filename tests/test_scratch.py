import pytest

from gigaam_api.worker import scratch_storage


def test_restart_cleans_abandoned_recordings_and_lock_prevents_active_deletion(monkeypatch, tmp_path):
    monkeypatch.setenv("ASR_SCRATCH_DIR", str(tmp_path))
    abandoned = tmp_path / "gigaam-abandoned"
    abandoned.mkdir()
    (abandoned / "source").write_bytes(b"old recording")
    retained = tmp_path / "other-app"
    retained.mkdir()
    (retained / "file").write_bytes(b"unrelated")
    with scratch_storage():
        assert not abandoned.exists() and retained.exists()
        active = tmp_path / "gigaam-active"
        active.mkdir()
        (active / "source").write_bytes(b"active recording")
        with pytest.raises(BlockingIOError):
            with scratch_storage():
                pass
        assert active.exists()
    with scratch_storage():
        assert not active.exists() and retained.exists()
