"""Phase 5B-4 real-engine tests. Skip cleanly when the local model is absent.

Covers: device selection mapping (pure), engine usability detection, silence
and non-speech controls through the REAL faster-whisper engine (no crash,
structured errors), and the no-download invariant (local_files_only path).
Heavy model load happens once per session (module cache); each test asserts
fast, deterministic properties.
"""
from __future__ import annotations

import os

import pytest

MODEL_DIR = r"C:\Users\Misha\Documents\job-search\artifacts\models\faster-whisper-small"

needs_engine = pytest.mark.skipif(
    not os.path.isfile(os.path.join(MODEL_DIR, "model.bin")),
    reason="local faster-whisper model weights (model.bin) not present")


def test_device_selection_mapping(monkeypatch):
    from ai_assistant.interview.stt import _resolve_device

    monkeypatch.setenv("STT_DEVICE", "cpu")
    assert _resolve_device() == ("cpu", "int8")
    monkeypatch.setenv("STT_DEVICE", "cuda")
    assert _resolve_device() == ("cuda", "float16")
    monkeypatch.setenv("STT_DEVICE", "auto")
    assert _resolve_device() == ("auto", "")
    monkeypatch.delenv("STT_DEVICE", raising=False)
    assert _resolve_device() == ("auto", "")


def test_no_download_path_forced(monkeypatch):
    """The adapter must always pass local_files_only (audited constructor arg)."""
    import inspect

    from ai_assistant.interview import stt

    src = inspect.getsource(stt._FasterWhisperEngine._load)
    assert "local_files_only=True" in src


@needs_engine
def test_engine_detected_usable(monkeypatch):
    from ai_assistant.interview import stt

    monkeypatch.setenv("STT_MODEL_DIR", MODEL_DIR)
    info = stt.detect_engines()
    assert info["available"] is True
    fw = [e for e in info["engines"] if e["name"] == "faster-whisper"]
    assert fw and fw[0]["usable"] is True


@needs_engine
def test_real_silence_is_unreadable(monkeypatch):
    import wave

    from ai_assistant.interview import stt

    monkeypatch.setenv("STT_MODEL_DIR", MODEL_DIR)
    monkeypatch.setenv("STT_DEVICE", "cpu")
    with tempfile_wav(16000, b"\x00\x00" * 16000) as data:
        res = stt.transcribe_audio(data, "ru", "audio/wav")
    assert res["ok"] is False and res["error"] == "audio-unreadable"


@needs_engine
def test_real_tone_is_structured_error(monkeypatch):
    import math
    import struct

    from ai_assistant.interview import stt

    monkeypatch.setenv("STT_MODEL_DIR", MODEL_DIR)
    monkeypatch.setenv("STT_DEVICE", "cpu")
    frames = b"".join(struct.pack("<h", int(0.5 * 32767 * math.sin(2 * math.pi * 440 * i / 16000)))
                      for i in range(16000))
    with tempfile_wav(16000, frames) as data:
        res = stt.transcribe_audio(data, "en", "audio/wav")
    assert res["ok"] is False
    assert res["error"] in ("audio-unreadable", "engine-error")
    assert res["retryable"] is False


@needs_engine
def test_real_garbage_never_raises(monkeypatch):
    from ai_assistant.interview import stt

    monkeypatch.setenv("STT_MODEL_DIR", MODEL_DIR)
    monkeypatch.setenv("STT_DEVICE", "cpu")
    res = stt.transcribe_audio(bytes(range(256)) * 16, "en", "application/octet-stream")
    assert res["ok"] is False and res["retryable"] is False


class tempfile_wav:
    """Context manager yielding complete WAV bytes for raw PCM frames."""

    def __init__(self, rate, frames):
        self.rate = rate
        self.frames = frames

    def __enter__(self):
        import io
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(self.frames)
        return buf.getvalue()

    def __exit__(self, *a):
        return False
