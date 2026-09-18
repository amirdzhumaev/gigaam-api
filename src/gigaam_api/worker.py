"""Outbound-only ASR worker. Requires no access to the API database."""

import argparse
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx

from .download import download_public
from .schemas import Segment, Transcript

log = logging.getLogger("gigaam.worker")
FORMATS = "wav,mp3,mov,ogg,flac,aac,matroska,webm"


class GigaAM:
    def __init__(self):
        self.model = None
        self.model_name = os.environ.get("ASR_MODEL", "gigaam-v3-e2e-rnnt")

    def transcribe(self, source: Path, root: Path):
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
        if self.model is None:
            import onnx_asr

            model = onnx_asr.load_model(
                self.model_name,
                quantization=os.environ.get("ASR_QUANTIZATION", "int8") or None,
                providers=["CPUExecutionProvider"],
            )
            self.model = model.with_vad(onnx_asr.load_vad("silero"), max_speech_duration_s=25, batch_size=1)
        segments = []
        for index, part in enumerate(self.model.recognize(str(wav))):
            text = part.text.strip()
            if text:
                segments.append(
                    Segment(
                        id=f"s{index}", start=float(part.start), end=min(duration, float(part.end)), text=text
                    )
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
        with tempfile.TemporaryDirectory(prefix="gigaam-") as directory:
            root = Path(directory)
            media = root / "source"
            if job["source"] == "url":
                download_public(job["url"], media, job["max_upload_bytes"])
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
            if lost.is_set():
                return True
            result = recognizer.transcribe(media, root)
            if not lost.is_set():
                r = client.post(
                    f"/internal/jobs/{ident}/complete",
                    json={"lease_token": token, "result": result.model_dump()},
                )
                r.raise_for_status()
                log.info("Completed job %s", ident)
    except Exception as exc:
        # No transcript, URL, token or upstream response body is written to logs.
        code = (
            "invalid_media"
            if isinstance(exc, (ValueError, subprocess.CalledProcessError))
            else "asr_unavailable"
        )
        log.warning("Job %s failed (%s)", ident, type(exc).__name__)
        if not lost.is_set():
            try:
                client.post(
                    f"/internal/jobs/{ident}/fail",
                    json={"lease_token": token, "code": code, "retryable": code != "invalid_media"},
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
    logging.basicConfig(level=logging.INFO)
    token = os.environ["ASR_WORKER_TOKEN"]
    with httpx.Client(
        base_url=os.environ.get("ASR_API_URL", "http://127.0.0.1:8100"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
        trust_env=False,
    ) as client:
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
