import contextlib
import json
import shutil
import types
import wave

import pytest

from gigaam_api import audio_chunks, worker


def write_wav(path, data, *, channels=1, rate=16000):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(data)


def test_pcm_windows_cover_every_sample_and_never_read_the_whole_file(tmp_path, monkeypatch):
    source, target = tmp_path / "source.wav", tmp_path / "part.wav"
    data = b"\x10\x27" * (16000 * 5 + 123)
    write_wav(source, data)
    open_wav = wave.open
    requested, actual, offsets = [], bytearray(), []

    def open_audio(path, mode):
        audio = open_wav(path, mode)
        if mode == "rb":
            read = audio.readframes

            def readframes(count):
                requested.append(count)
                return read(count)

            audio.readframes = readframes
        return audio

    monkeypatch.setattr(audio_chunks.wave, "open", open_audio)
    for start, duration, path in audio_chunks.wav_chunks(source, target, seconds=2):
        offsets.append((start, duration))
        with open_wav(str(path), "rb") as audio:
            assert audio.getnframes() <= 32000
            actual.extend(audio.readframes(audio.getnframes()))
    assert bytes(actual) == data
    assert [start for start, _ in offsets] == [0, 2, 4]
    assert sum(duration for _, duration in offsets) == pytest.approx(5 + 123 / 16000)
    assert max(requested) == 32000
    assert not target.exists()


def test_quiet_boundary_preserves_samples_and_avoids_cutting_speech(tmp_path):
    source, target = tmp_path / "source.wav", tmp_path / "part.wav"
    speech = b"\x10\x27" * (16000 * 4)
    silence = b"\0\0" * 8000
    data = speech + silence + speech
    write_wav(source, data)
    recovered, boundaries = bytearray(), []
    for start, duration, path in audio_chunks.wav_chunks(source, target, seconds=5):
        boundaries.append(start + duration)
        with wave.open(str(path), "rb") as audio:
            recovered.extend(audio.readframes(audio.getnframes()))
    assert 4 < boundaries[0] < 4.5
    assert bytes(recovered) == data


@pytest.mark.parametrize("channels,rate", [(2, 16000), (1, 8000)])
def test_only_normalized_pcm_is_accepted(tmp_path, channels, rate):
    source, target = tmp_path / "source.wav", tmp_path / "part.wav"
    write_wav(source, b"\0" * 1000, channels=channels, rate=rate)
    with pytest.raises(ValueError, match="Expected decoded"):
        list(audio_chunks.wav_chunks(source, target))


def test_closing_windows_removes_current_chunk(tmp_path):
    source, target = tmp_path / "source.wav", tmp_path / "part.wav"
    write_wav(source, b"\x10\x27" * 48000)
    with contextlib.closing(audio_chunks.wav_chunks(source, target, seconds=1)) as chunks:
        next(chunks)
        assert target.exists()
    assert not target.exists()


def test_recognition_restores_absolute_times_across_windows_and_clips_end(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    write_wav(source, b"\x10\x27" * 80000)

    def decode(command, **kwargs):
        if command[0] == "ffprobe":
            return types.SimpleNamespace(stdout=json.dumps({"format": {"duration": "5"}}))
        assert "-vn" in command
        shutil.copyfile(source, command[-1])

    calls = []

    class Model:
        def recognize(self, path):
            with wave.open(path, "rb") as audio:
                seconds = audio.getnframes() / 16000
                assert seconds <= 2
                calls.append(seconds)
            return iter(
                [
                    types.SimpleNamespace(start=0, end=0.1, text=" "),
                    types.SimpleNamespace(start=0.2, end=seconds + 0.1, text="тест"),
                ]
            )

    monkeypatch.setattr(worker.subprocess, "run", decode)
    monkeypatch.setattr(worker, "wav_chunks", lambda a, b: audio_chunks.wav_chunks(a, b, seconds=2))
    recognizer = worker.GigaAM()
    recognizer.model = Model()
    result = recognizer.transcribe(source, tmp_path)
    assert calls == [2, 2, 1]
    assert [s.id for s in result.segments] == ["s0", "s1", "s2"]
    assert [s.start for s in result.segments] == [0.2, 2.2, 4.2]
    assert [s.end for s in result.segments] == [2, 4, 5]
    assert result.text == "тест тест тест" and result.duration == 5
    assert not (tmp_path / "chunk.wav").exists()
