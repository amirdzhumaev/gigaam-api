import shutil
import subprocess
import sys
import types
import wave

import pytest

from gigaam_api.worker import GigaAM


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required for decoder smoke test")
def test_real_ffmpeg_decoder_with_injected_recognizer(tmp_path, monkeypatch):
    source = tmp_path / "recording.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((2, 2, 44100, 44100, "NONE", "not compressed"))
        audio.writeframes(b"\0" * 44100 * 4)

    class Model:
        def with_vad(self, vad, **kwargs):
            assert kwargs["max_speech_duration_s"] == 25
            return self

        def recognize(self, path):
            with wave.open(path) as audio:
                assert (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) == (1, 16000, 2)
            return iter([types.SimpleNamespace(start=0, end=1, text="Тестовая запись.")])

    monkeypatch.setitem(
        sys.modules,
        "onnx_asr",
        types.SimpleNamespace(load_model=lambda *a, **k: Model(), load_vad=lambda *_: object()),
    )
    result = GigaAM().transcribe(source, tmp_path)
    assert result.duration == 1
    assert result.text == "Тестовая запись."


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="FFprobe required")
def test_playlist_cannot_read_arbitrary_local_paths(tmp_path):
    playlist = tmp_path / "audio.m4a"
    playlist.write_text("ffconcat version 1.0\nfile '/etc/passwd'\n")
    with pytest.raises(subprocess.CalledProcessError):
        GigaAM().transcribe(playlist, tmp_path)
