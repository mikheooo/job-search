"""Phase 5B-4b switching tests: CPU/GPU selection, CUDA fallback, residency.

Fake-engine/sys.modules-based (no model needed) except two skip-guarded
real-audio checks that run only where the local fixtures exist. Never pins
RU transcript text (acoustic accuracy is measurement-reported, not asserted).
"""
from __future__ import annotations

import os
import sys
import types

import pytest

MODEL_DIR = r"C:\Users\Misha\Documents\job-search\artifacts\models\faster-whisper-small"
AUDIO_DIR = r"C:\Users\Misha\AppData\Local\Temp\opencode\stt_audio"

needs_model = pytest.mark.skipif(
    not os.path.isdir(MODEL_DIR), reason="local faster-whisper model not present")


def _audio(name):
    path = os.path.join(AUDIO_DIR, name)
    if not os.path.isfile(path):
        pytest.skip(f"fixture {name} not present")
    with open(path, "rb") as f:
        return f.read()


class _Seg:
    def __init__(self, text):
        self.text = text


class _FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel via sys.modules injection."""

    instances = []

    def __init__(self, model_dir, device="cpu", compute_type="int8", **kw):
        _FakeWhisperModel.instances.append((model_dir, device, compute_type))
        if device == "cuda":
            raise RuntimeError("no CUDA in this test")
        self.device = device

    def transcribe(self, path, **kw):
        return [_Seg("hello world")], object()


@pytest.fixture()
def fake_whisper(monkeypatch):
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    _FakeWhisperModel.instances.clear()
    from ai_assistant.interview import stt

    monkeypatch.delenv("STT_MODEL_DIR", raising=False)
    before = set(stt._MODEL_CACHE)
    yield mod
    for key in set(stt._MODEL_CACHE) - before:
        stt._MODEL_CACHE.pop(key, None)


@pytest.fixture()
def fake_model_dir(tmp_path):
    """An existing (empty) dir: passes the is_dir gate, never a real model."""
    return str(tmp_path)


def test_cpu_fallback_when_cuda_load_fails(monkeypatch, fake_whisper, fake_model_dir):
    """STT_DEVICE=auto must fall back to CPU, not fail the request."""
    from ai_assistant.interview import stt

    monkeypatch.setenv("STT_DEVICE", "auto")
    eng = stt._FasterWhisperEngine(fake_model_dir)
    assert eng.transcribe(b"\x01\x02", "en") == "hello world"
    assert eng.last_device == "cpu"
    assert _FakeWhisperModel.instances[0][1] == "cuda", "tried CUDA first"
    assert _FakeWhisperModel.instances[-1][1] == "cpu", "then fell back"


def test_explicit_cpu_never_touches_cuda(monkeypatch, fake_whisper, fake_model_dir):
    from ai_assistant.interview import stt

    monkeypatch.setenv("STT_DEVICE", "cpu")
    eng = stt._FasterWhisperEngine(fake_model_dir)
    assert eng.transcribe(b"\x01\x02", "en") == "hello world"
    assert [i[1] for i in _FakeWhisperModel.instances] == ["cpu"]


def test_model_resident_across_calls(monkeypatch, fake_whisper, fake_model_dir):
    """One construction serves repeated transcribes (no reload per chunk)."""
    from ai_assistant.interview import stt

    monkeypatch.setenv("STT_DEVICE", "cpu")
    eng = stt._FasterWhisperEngine(fake_model_dir)
    eng.transcribe(b"\x01", "en")
    eng.transcribe(b"\x02", "ru")
    assert len(_FakeWhisperModel.instances) == 1


def test_device_reported_in_result():
    from ai_assistant.interview import stt

    class _GpuFake:
        name = "fake-stt"
        langs = ("ru", "en")
        last_device = "cuda"

        def transcribe(self, audio, lang):
            return "ok"

    res = stt.transcribe_audio(b"\x01\x02", "en", engine_obj=_GpuFake())
    assert res["ok"] is True and res["device"] == "cuda"


def test_decode_defaults_are_vad_beam5_noctx(monkeypatch, fake_whisper, fake_model_dir):
    """Production decode settings pinned: VAD on, beam 5, no cross-chunk ctx."""
    from ai_assistant.interview import stt

    seen = {}

    class _KwFake(_FakeWhisperModel):
        def transcribe(self, path, **kw):
            seen.update(kw)
            return [_Seg("hi")], object()

    monkeypatch.setattr(fake_whisper, "WhisperModel", _KwFake)
    monkeypatch.setenv("STT_DEVICE", "cpu")
    eng = stt._FasterWhisperEngine(fake_model_dir)
    assert eng.transcribe(b"RIFF" + b"\x00" * 100, "en") == "hi"
    assert seen.get("vad_filter") is True
    assert seen.get("beam_size") == 5
    assert seen.get("condition_on_previous_text") is False


def test_transcribe_unlinks_temp_file(monkeypatch, fake_whisper, fake_model_dir):
    """Windows holds PyAV handles: a locked chunk file must be retried, then gone."""
    import os as _os
    import tempfile as _tempfile

    from ai_assistant.interview import stt

    created = []
    real_ntf = _tempfile.NamedTemporaryFile

    def _tracking(*a, **k):
        f = real_ntf(*a, **k)
        created.append(f.name)
        return f

    monkeypatch.setattr(_tempfile, "NamedTemporaryFile", _tracking)
    real_unlink = _os.unlink
    attempts = []

    def _flaky(path):
        if path not in created:
            return real_unlink(path)
        attempts.append(path)
        if len(attempts) == 1:
            raise OSError("file locked by decoder")
        return real_unlink(path)

    monkeypatch.setattr(_os, "unlink", _flaky)
    monkeypatch.setenv("STT_DEVICE", "cpu")
    eng = stt._FasterWhisperEngine(fake_model_dir)
    assert eng.transcribe(b"RIFF" + b"\x00" * 100, "en") == "hello world"
    assert created, "expected a temp chunk file"
    assert len(attempts) >= 2, "locked file must be retried, not swallowed"
    assert all(not _os.path.exists(p) for p in created), created


@needs_model
def test_en_forced_matches_auto(monkeypatch):
    """Greedy decode is deterministic: forced lang == autodetect on EN."""
    from ai_assistant.interview import stt

    monkeypatch.setenv("STT_MODEL_DIR", MODEL_DIR)
    monkeypatch.setenv("STT_DEVICE", "cpu")
    data = _audio("en-q.wav")

    from ai_assistant.interview.stt import _FasterWhisperEngine
    import tempfile

    eng = _FasterWhisperEngine()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        import faster_whisper  # noqa: F401  (real package here, not the fake)

        model = eng._load()
        a = " ".join(s.text.strip() for s in
                     model.transcribe(path, language="en", beam_size=5,
                                      condition_on_previous_text=False)[0]).strip()
        b = " ".join(s.text.strip() for s in
                     model.transcribe(path, language=None, beam_size=5,
                                      condition_on_previous_text=False)[0]).strip()
    finally:
        os.unlink(path)
    assert a and a == b, f"forced/auto diverged: {a!r} vs {b!r}"


@needs_model
def test_ru_result_is_wellformed_schema(monkeypatch):
    """RU acoustic accuracy is measurement-reported; the suite pins only the
    contract: a dict with valid schema, never a raise, never a crash."""
    from ai_assistant.interview import stt

    monkeypatch.setenv("STT_MODEL_DIR", MODEL_DIR)
    monkeypatch.setenv("STT_DEVICE", "cpu")
    res = stt.transcribe_audio(_audio("ru-5s.wav"), "ru", "audio/wav")
    assert set(res) >= {"ok"}
    if res["ok"]:
        assert isinstance(res["text"], str) and res["lang"] == "ru"
        assert res["engine"] == "faster-whisper" and res["device"] in ("cpu", "cuda")
    else:
        assert res["error"] in ("audio-unreadable", "engine-error", "stt-unavailable")
        assert res["retryable"] is False


def test_suffix_routing():
    from ai_assistant.interview.stt import _suffix_for_audio

    assert _suffix_for_audio(b"RIFF....") == ".wav"
    assert _suffix_for_audio(bytes.fromhex("1a45dfa3") + b"\x00" * 8) == ".webm"
    assert _suffix_for_audio(b"\x00\x00\x00\x20ftypM4A ") == ".m4a"
    assert _suffix_for_audio(b"\xff\xfb\x90\x00" + b"\x00" * 8) == ".webm"
    assert _suffix_for_audio(b"") == ".webm"


def test_warmup_success_with_fake_engine(monkeypatch, fake_whisper, fake_model_dir):
    """Warmup exercises load + one inference, reports device and timings."""
    from ai_assistant.interview import stt

    monkeypatch.setenv("STT_DEVICE", "cpu")
    res = stt.warmup(fake_model_dir)
    assert res["ok"] is True
    assert res["engine"] == "faster-whisper" and res["device"] == "cpu"
    assert set(res) >= {"load_s", "warmup_s"}
    assert res["warmup_s"] >= res["load_s"] >= 0


def test_warmup_without_model_is_unavailable(monkeypatch, tmp_path):
    from ai_assistant.interview import stt

    monkeypatch.delenv("STT_MODEL_DIR", raising=False)
    res = stt.warmup(str(tmp_path / "nope"))
    assert res["ok"] is False and res["error"] == "stt-unavailable"
    assert res["retryable"] is False


def test_warmup_inference_failure_is_engine_error(monkeypatch, fake_whisper,
                                                 fake_model_dir):
    from ai_assistant.interview import stt

    class _Boom:
        name = "boom"
        langs = ("ru", "en")
        last_device = "cpu"

        def transcribe(self, audio, lang, vad=True):
            raise RuntimeError("decode blew up")

    monkeypatch.setattr(stt._FasterWhisperEngine, "transcribe", _Boom().transcribe)
    monkeypatch.setenv("STT_DEVICE", "cpu")
    res = stt.warmup(fake_model_dir)
    assert res["ok"] is False and res["error"] == "engine-error"
