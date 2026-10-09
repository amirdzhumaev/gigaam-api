"""Outbound-only ASR worker. Requires no access to the API database."""

import argparse
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

import httpx

from .audio_chunks import wav_chunks
from .download import DownloadError
from .page_import import PageImportError, download_source
from .schemas import Segment, Transcript

log = logging.getLogger("gigaam.worker")
FORMATS = "wav,mp3,mov,ogg,flac,aac,matroska,webm"


@contextmanager
def scratch_storage():
    """A dedicated disk directory survives restarts; abandoned inputs do not."""
    configured = os.environ.get("ASR_SCRATCH_DIR")
    if not configured:
        yield
        return
    import fcntl

    root = Path(configured)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / ".worker.lock").open("a") as lock:
        # One worker owns this directory. Do not prune another worker's current recording.
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        for path in root.glob("gigaam-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        yield


class GigaAM:
    def __init__(self):
        self.model = None
        self.model_name = os.environ.get("ASR_MODEL", "gigaam-v3-e2e-rnnt")

    def transcribe(self, source: Path, root: Path):
        started = time.monotonic()
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                FORMATS,
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(source),
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )
        duration = float(json.loads(probe.stdout)["format"]["duration"])
        if not 0 < duration <= float(os.environ.get("MAX_AUDIO_SECONDS", 14400)):
            raise ValueError("unsupported_duration")
        log.info("Media duration_seconds=%.2f; decoding audio", duration)
        wav = root / "audio.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                FORMATS,
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(wav),
            ],
            capture_output=True,
            timeout=600,
            check=True,
        )
        log.info("Audio decoded elapsed_seconds=%.2f", time.monotonic() - started)
        if self.model is None:
            import onnx_asr

            load_started = time.monotonic()
            log.info("Loading ASR model and VAD")
            model = onnx_asr.load_model(
                self.model_name,
                quantization=os.environ.get("ASR_QUANTIZATION", "int8") or None,
                providers=["CPUExecutionProvider"],
            )
            self.model = model.with_vad(onnx_asr.load_vad("silero"), max_speech_duration_s=25, batch_size=1)
            log.info("ASR model and VAD loaded elapsed_seconds=%.2f", time.monotonic() - load_started)
        recognized_at = time.monotonic()
        log.info("Transcription started duration_seconds=%.2f", duration)
        segments = []
        # onnx-asr loads its input into NumPy before VAD. Give it at most five
        # minutes, rather than a multi-hour waveform that can exhaust Pi RAM.
        with closing(wav_chunks(wav, root / "chunk.wav")) as chunks:
            for offset, chunk_duration, chunk_path in chunks:
                for part in self.model.recognize(str(chunk_path)):
                    text = part.text.strip()
                    if text:
                        end = min(duration, offset + chunk_duration, offset + float(part.end))
                        segments.append(
                            Segment(
                                id=f"s{len(segments)}",
                                start=min(end, offset + max(0, float(part.start))),
                                end=end,
                                text=text,
                            )
                        )
                log.info("Transcription progress audio_seconds=%.2f", offset + chunk_duration)
        elapsed = time.monotonic() - recognized_at
        log.info(
            "Transcription finished elapsed_seconds=%.2f realtime_factor=%.4f segments=%d",
            elapsed,
            elapsed / duration,
            len(segments),
        )
        return Transcript(
            model=self.model_name,
            duration=duration,
            text=" ".join(s.text for s in segments),
            segments=segments,
        )


def process_one(client: httpx.Client, recognizer) -> bool:
    response = client.post("/internal/jobs/claim")
    if response.status_code == 204:
        return False
    response.raise_for_status()
    job = response.json()
    ident, token = job["id"], job["lease_token"]
    started = time.monotonic()
    log.info("Started job %s source_kind=%s", ident, job["source"])
    stop = threading.Event()
    lost = threading.Event()

    def pulse():
        while not stop.wait(25):
            try:
                r = client.post(f"/internal/jobs/{ident}/heartbeat", json={"lease_token": token})
                if r.status_code == 409:
                    lost.set()
                    return
                r.raise_for_status()
            except httpx.HTTPError:
                log.warning("Heartbeat unavailable for job %s", ident)

    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(
            prefix="gigaam-", dir=os.environ.get("ASR_SCRATCH_DIR")
        ) as directory:
            root = Path(directory)
            media = root / "source"
            downloaded_at = time.monotonic()
            log.info("Job %s download started", ident)
            if job["source"] == "url":
                download_source(job["url"], media, job["max_upload_bytes"])
            else:
                with client.stream(
                    "GET", f"/internal/jobs/{ident}/media", headers={"X-Lease-Token": token}
                ) as r:
                    r.raise_for_status()
                    size = 0
                    with media.open("wb") as target:
                        for chunk in r.iter_bytes():
                            size += len(chunk)
                            if size > job["max_upload_bytes"]:
                                raise ValueError("file_too_large")
                            target.write(chunk)
            log.info(
                "Job %s downloaded bytes=%d elapsed_seconds=%.2f",
                ident,
                media.stat().st_size,
                time.monotonic() - downloaded_at,
            )
            if lost.is_set():
                return True
            result = recognizer.transcribe(media, root)
            if not lost.is_set():
                r = client.post(
                    f"/internal/jobs/{ident}/complete",
                    json={"lease_token": token, "result": result.model_dump()},
                )
                r.raise_for_status()
                log.info("Completed job %s elapsed_seconds=%.2f", ident, time.monotonic() - started)
    except Exception as exc:
        # No transcript, URL, token or upstream response body is written to logs.
        code = (
            exc.code
            if isinstance(exc, (PageImportError, DownloadError))
            else (
                "invalid_media"
                if isinstance(exc, (ValueError, subprocess.CalledProcessError))
                else "asr_unavailable"
            )
        )
        log.warning(
            "Job %s failed (%s, code=%s) elapsed_seconds=%.2f",
            ident,
            type(exc).__name__,
            code,
            time.monotonic() - started,
        )
        if not lost.is_set():
            try:
                client.post(
                    f"/internal/jobs/{ident}/fail",
                    json={"lease_token": token, "code": code, "retryable": code == "asr_unavailable"},
                ).raise_for_status()
            except httpx.HTTPError:
                log.warning("Failure acknowledgement unavailable; lease will expire")
    finally:
        stop.set()
        thread.join(timeout=1)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ["ASR_WORKER_TOKEN"]
    with (
        scratch_storage(),
        httpx.Client(
            base_url=os.environ.get("ASR_API_URL", "http://127.0.0.1:8100"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            trust_env=False,
        ) as client,
    ):
        recognizer = GigaAM()
        while True:
            try:
                worked = process_one(client, recognizer)
            except httpx.HTTPError:
                log.warning("API unavailable; retrying")
                worked = False
            if args.once:
                break
            if not worked:
                time.sleep(3)


if __name__ == "__main__":
    main()
