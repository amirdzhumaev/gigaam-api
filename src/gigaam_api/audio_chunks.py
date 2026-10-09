"""Read decoded PCM from disk in bounded windows, preferring quiet boundaries."""

import sys
import wave
from array import array

SAMPLE_RATE = 16000
WINDOW_SECONDS = 300


def quiet_cut(data, rate):
    frames = len(data) // 2
    search = min(30 * rate, frames // 2)
    samples = array("h", data[(frames - search) * 2 :])
    if sys.byteorder != "little":
        samples.byteswap()
    step = rate // 50
    quiet_frames, quiet_end = 0, 0
    # A 300 ms quiet interval near the end is safer than cutting mid-word.
    for start in range(len(samples) - step, -1, -step):
        window = samples[start : start + step]
        if sum(value * value for value in window) <= len(window) * 512**2:
            if not quiet_frames:
                quiet_end = start + step
            quiet_frames += step
            if quiet_frames >= rate * 0.3:
                return frames - search + (start + quiet_end) // 2
        else:
            quiet_frames = 0
    return frames


def wav_chunks(source, destination, *, seconds=WINDOW_SECONDS):
    """Yield (start seconds, duration, WAV path); retain only one PCM window."""
    try:
        with wave.open(str(source), "rb") as audio:
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getframerate() != SAMPLE_RATE
                or audio.getcomptype() != "NONE"
            ):
                raise ValueError("Expected decoded mono 16 kHz PCM16")
            total = audio.getnframes()
            while audio.tell() < total:
                start = audio.tell()
                data = audio.readframes(seconds * SAMPLE_RATE)
                if not data:
                    raise ValueError("Truncated decoded audio")
                frames = len(data) // 2
                if start + frames < total:
                    frames = quiet_cut(data, SAMPLE_RATE)
                audio.setpos(start + frames)
                with wave.open(str(destination), "wb") as chunk:
                    chunk.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
                    chunk.writeframes(data[: frames * 2])
                del data
                yield start / SAMPLE_RATE, frames / SAMPLE_RATE, destination
    finally:
        destination.unlink(missing_ok=True)
