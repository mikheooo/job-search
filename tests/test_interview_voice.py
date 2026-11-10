"""Phase 5B-1 voice input tests (isolated, offline).

Covers the STT engine contract (validate/detect/transcribe with fakes —
no models downloaded, none installed here), the /interview/transcribe
endpoint (auth, validation, honest stt-unavailable, fake-engine success),
and the voice->fast/hint wiring (a confirmed question reaches the existing
coach path and session history unchanged). 5A text path untouched.
"""
from __future__ import annotations

import asyncio
import json

import pytest

VOICE_Q = "Расскажите о самом сложном баге, который вы чинили"


def _authed_post_request(monkeypatch, body: bytes, token="test-token",
                         content_type="audio/webm"):
    """Owner-authed POST Request carrying a raw audio body."""
    from starlette.requests import Request

    monkeypatch.setenv("DASHBOARD_TOKEN", token)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": "/",
         "headers": [(b"authorization", f"Bearer {token}".encode()),
                     (b"content-type", content_type.encode())]},
        receive=receive)


def _anon_post_request(body: bytes):
    from starlette.requests import Request

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/",
                    "headers": [(b"content-type", b"audio/webm")]},
                   receive=receive)


class _FakeEngine:
    name = "fake-stt"
    langs = ("ru", "en")

    def __init__(self, text="распознанный вопрос", exc=None):
        self._text = text
        self._exc = exc
        self.calls = []

    def transcribe(self, audio: bytes, lang: str) -> str:
        self.calls.append((audio, lang))
        if self._exc is not None:
            raise self._exc
        return self._text


# --- engine contract ---

def test_stt_rejects_unsupported_lang():
    from ai_assistant.interview import stt

    res = stt.transcribe_audio(b"\x00\x01", "de", engine_obj=_FakeEngine())
    assert res == {"ok": False, "error": "lang-unsupported",
                   "retryable": False, "detail": "lang must be one of ['ru', 'en']"}


def test_stt_rejects_empty_audio():
    from ai_assistant.interview import stt

    res = stt.transcribe_audio(b"", "ru", engine_obj=_FakeEngine())
    assert res["ok"] is False and res["error"] == "empty-audio"


def test_stt_rejects_oversize_audio():
    from ai_assistant.interview import stt

    res = stt.transcribe_audio(b"\x00" * (stt.MAX_AUDIO_BYTES + 1), "en",
                               engine_obj=_FakeEngine())
    assert res["ok"] is False and res["error"] == "audio-too-large"


def test_stt_detect_reports_unavailable_without_engines():
    from ai_assistant.interview import stt

    def _missing(name):
        raise ImportError(f"No module named {name!r}")

    info = stt.detect_engines(import_fn=_missing)
    assert info["available"] is False
    assert info["engines"] == []
    assert info["reason"] == "stt-unavailable"


def test_stt_detect_sees_engine_but_requires_local_model(tmp_path, monkeypatch):
    from ai_assistant.interview import stt

    # Hermetic: the developer machine may legitimately configure a model via
    # .env (loaded by config import); this test pins import-then-model logic.
    monkeypatch.delenv("STT_MODEL_DIR", raising=False)
    monkeypatch.delenv("VOSK_MODEL_DIR", raising=False)

    class _Mod:
        pass

    info = stt.detect_engines(import_fn=lambda name: _Mod())
    assert info["available"] is False, "import alone must not count without a model"
    assert any(e["name"] == "faster-whisper" and e["usable"] is False
               for e in info["engines"])

    model_dir = tmp_path / "fw-model"
    model_dir.mkdir()
    info2 = stt.detect_engines(import_fn=lambda name: _Mod(),
                               model_dir=str(model_dir))
    assert any(e["name"] == "faster-whisper" and e["usable"] is True
               for e in info2["engines"])


def test_stt_fake_engine_transcribes():
    from ai_assistant.interview import stt

    eng = _FakeEngine("какой у вас опыт с Kubernetes?")
    res = stt.transcribe_audio(b"\x01\x02\x03", "ru", "audio/webm", engine_obj=eng)
    assert res == {"ok": True, "text": "какой у вас опыт с Kubernetes?",
                   "lang": "ru", "engine": "fake-stt", "device": "unknown",
                   "audio_bytes": 3,
                   "content_type": "audio/webm"}
    assert eng.calls == [(b"\x01\x02\x03", "ru")]


def test_stt_empty_transcript_is_unreadable():
    from ai_assistant.interview import stt

    res = stt.transcribe_audio(b"\x01\x02", "en", engine_obj=_FakeEngine("   "))
    assert res["ok"] is False and res["error"] == "audio-unreadable"


def test_stt_engine_exception_maps_to_engine_error():
    from ai_assistant.interview import stt

    res = stt.transcribe_audio(b"\x01\x02", "ru",
                               engine_obj=_FakeEngine(exc=RuntimeError("boom")))
    assert res["ok"] is False and res["error"] == "engine-error"
    assert res["retryable"] is False


def test_stt_unknown_named_engine_is_unavailable():
    from ai_assistant.interview import stt

    res = stt.transcribe_audio(b"\x01\x02", "ru", engine="nope",
                               detect_fn=lambda: {"available": False, "engines": []})
    assert res["ok"] is False and res["error"] == "stt-unavailable"


# --- endpoint ---

def test_transcribe_requires_auth():
    from fastapi import HTTPException

    from ai_assistant.hub import api as hub

    with pytest.raises(HTTPException) as exc:
        asyncio.run(hub.hub_interview_transcribe(_anon_post_request(b"\x01"), "ru"))
    assert exc.value.status_code == 401


def test_transcribe_rejects_bad_lang(monkeypatch):
    from fastapi import HTTPException

    from ai_assistant.hub import api as hub

    req = _authed_post_request(monkeypatch, b"\x01\x02")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(hub.hub_interview_transcribe(req, "de"))
    assert exc.value.status_code == 422


def test_transcribe_rejects_empty_body(monkeypatch):
    from fastapi import HTTPException

    from ai_assistant.hub import api as hub

    req = _authed_post_request(monkeypatch, b"")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(hub.hub_interview_transcribe(req, "ru"))
    assert exc.value.status_code == 422
    assert exc.value.detail == "empty-audio"


def test_transcribe_reports_unavailable_honestly(monkeypatch):
    from ai_assistant.hub import api as hub
    from ai_assistant.interview import stt as istt

    monkeypatch.setattr(istt, "detect_engines",
                        lambda *a, **k: {"available": False, "engines": [],
                                         "reason": "stt-unavailable"})
    req = _authed_post_request(monkeypatch, b"\x01\x02\x03")
    res = asyncio.run(hub.hub_interview_transcribe(req, "ru"))
    assert res["ok"] is False
    assert res["error"] == "stt-unavailable"
    assert res["retryable"] is False
    assert res["engines"]["available"] is False


def test_transcribe_success_with_engine(monkeypatch):
    from ai_assistant.hub import api as hub
    from ai_assistant.interview import stt as istt

    def _fake_transcribe(audio, lang, content_type=None, engine=None, **kw):
        assert lang == "en"
        assert audio == b"\x01\x02\x03"
        return {"ok": True, "text": "Tell me about yourself", "lang": "en",
                "engine": "fake-stt", "audio_bytes": 3, "content_type": "audio/webm"}

    monkeypatch.setattr(istt, "transcribe_audio", _fake_transcribe)
    req = _authed_post_request(monkeypatch, b"\x01\x02\x03")
    res = asyncio.run(hub.hub_interview_transcribe(req, "en"))
    assert res["ok"] is True and res["text"] == "Tell me about yourself"


# --- voice -> existing coach path (5A untouched) ---

def _authed_get_request(monkeypatch, token="test-token"):
    from starlette.requests import Request

    monkeypatch.setenv("DASHBOARD_TOKEN", token)
    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": [(b"authorization", f"Bearer {token}".encode())]})


def test_confirmed_voice_question_reaches_fast_and_session(monkeypatch):
    """A question confirmed from voice input travels the exact 5A fast path:
    prompt contains it, a 'fast' turn is saved, history shows it."""
    from ai_assistant.hub.api import InterviewFastBody, hub_interview_fast
    from ai_assistant.interview import sessions as isess
    import ai_assistant.interview.assistant as iasst

    seen = {}

    def _fake_call_model(system_prompt, user_prompt):
        seen["user"] = user_prompt
        return {"ok": True, "data": {"short": "Коротко.", "points": ["a", "b", "c"]},
                "elapsed": 0.1}

    monkeypatch.setattr(iasst, "call_model", _fake_call_model)
    req = _authed_get_request(monkeypatch)
    res = hub_interview_fast(req, InterviewFastBody(question=VOICE_Q,
                                                    role_title="QA Engineer"))
    assert res["ok"] is True and res["state"] == "ready"
    assert VOICE_Q in seen["user"], "confirmed voice text must reach the model prompt"
    assert "QA Engineer" in seen["user"], "role context must travel with it"

    from ai_assistant.hub.api import _interview_conn

    conn = _interview_conn()
    try:
        sess = isess.get_session(conn, res["session_id"])
    finally:
        conn.close()
    assert sess is not None
    kinds = [t["kind"] for t in sess["turns"]]
    assert "fast" in kinds
    assert any(t["question"] == VOICE_Q for t in sess["turns"])


def test_plain_text_fast_path_unchanged(monkeypatch):
    """5A regression guard: typed questions behave exactly as before."""
    from ai_assistant.hub.api import InterviewFastBody, hub_interview_fast
    import ai_assistant.interview.assistant as iasst

    monkeypatch.setattr(iasst, "call_model",
                        lambda s, u: {"ok": True,
                                      "data": {"short": "s", "points": ["1", "2", "3"]},
                                      "elapsed": 0.1})
    req = _authed_get_request(monkeypatch)
    res = hub_interview_fast(req, InterviewFastBody(question="Что такое индекс?"))
    assert res["ok"] is True and res["fast"]["short"] == "s"
