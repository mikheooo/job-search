"""Phase 5B-2 live prototype tests (isolated, offline).

Detector: pure rule-based RU/EN question detection over test transcripts —
bounds, incomplete buffering, dedup, no model calls (AST-pinned).
Endpoint /interview/detect: auth, validation, stateless dedup, zero DB writes.
5B-1 MVP verification: consent gating, track release, no-audio refusal,
no audio persistence, denial messages (hub.html contract + endpoint probes).
No microphone, no network, no production writes (conftest tmp DB).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _det():
    from ai_assistant.interview import questions_detect as qd

    return qd.QuestionDetector()


# --- detector: bounds ---

def test_ru_question_mark_detected():
    d = _det()
    cands = d.add_segment("Что такое индекс?", "ru")
    assert len(cands) == 1
    c = cands[0]
    assert c["text"] == "Что такое индекс?"
    assert c["lang"] == "ru" and c["incomplete"] is False
    assert c["confidence"] >= 0.75 and "question-mark" in c["reason"]


def test_en_interrogative_detected():
    d = _det()
    cands = d.add_segment("What is your experience with Kubernetes?", "en")
    assert len(cands) == 1 and cands[0]["confidence"] >= 0.75


def test_statement_is_not_a_question():
    d = _det()
    assert d.add_segment("Мы используем Docker каждый день.", "ru") == []
    assert d.add_segment("We deploy every Friday.", "en") == []


def test_imperative_invite_is_a_question():
    d = _det()
    cands = d.add_segment("Расскажите о себе.", "ru")
    assert len(cands) == 1 and cands[0]["confidence"] >= 0.6
    d2 = _det()
    cands2 = d2.add_segment("Tell me about yourself.", "en")
    assert len(cands2) == 1 and cands2[0]["confidence"] >= 0.6


def test_too_short_is_rejected():
    d = _det()
    assert d.add_segment("Да?", "ru") == []
    assert d.flush_stale("ru", force=True) == []


def test_multi_question_segment_splits():
    d = _det()
    cands = d.add_segment("Что такое индекс? А как вы тестируете API?", "ru")
    assert [c["text"] for c in cands] == ["Что такое индекс?", "А как вы тестируете API?"]


def test_speaker_label_and_timestamp_stripped():
    d = _det()
    cands = d.add_segment("Интервьюер: Что такое индекс?", "ru")
    assert len(cands) == 1 and cands[0]["norm"] == "что такое индекс"
    d2 = _det()
    cands2 = d2.add_segment("[00:12] How do you debug production?", "en")
    assert len(cands2) == 1 and cands2[0]["norm"] == "how do you debug production"


# --- detector: incomplete phrases ---

def test_incomplete_phrase_joins_across_segments():
    d = _det()
    assert d.add_segment("Расскажите о проекте где", "ru") == []
    cands = d.add_segment("вы использовали очереди?", "ru")
    assert len(cands) == 1
    assert cands[0]["incomplete"] is False
    assert "очереди" in cands[0]["text"]


def test_fresh_fragment_not_flushed_early():
    now = [1000.0]
    from ai_assistant.interview import questions_detect as qd

    d = qd.QuestionDetector(stale_after_s=20.0, clock=lambda: now[0])
    assert d.add_segment("расскажите о вашем опыте с", "ru") == []
    assert d.flush_stale("ru") == [], "a phrase still in progress must wait"
    now[0] += 21.0
    cands = d.flush_stale("ru")
    assert len(cands) == 1 and cands[0]["incomplete"] is True


def test_content_free_fragment_dropped_on_flush():
    d = _det()
    assert d.add_segment("ну в общем так сказать", "ru") == []
    assert d.flush_stale("ru", force=True) == []


# --- detector: dedup + determinism ---

def test_repeat_question_emitted_once():
    d = _det()
    first = d.add_segment("Как вы тестируете API?", "ru")
    second = d.add_segment("Как вы тестируете API?", "ru")
    third = d.add_segment("КАК ВЫ ТЕСТИРУЕТЕ API?", "ru")
    assert len(first) == 1 and second == [] and third == []


def test_detector_is_deterministic():
    from ai_assistant.interview import questions_detect as qd

    segs = ["Добрый день! Расскажите о себе.", "Сколько лет вы работаете с Python?"]
    run = lambda: (lambda d: (d.add_segment(segs[0], "ru"), d.add_segment(segs[1], "ru")))(qd.QuestionDetector())
    assert run() == run()


def test_detector_has_no_model_or_network_imports():
    """The detector must never reach an LLM: pin the import surface."""
    from ai_assistant.interview import questions_detect as qd

    tree = ast.parse(Path(qd.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    banned = {"openai", "requests", "httpx", "urllib", "job_analyzer",
              "assistant", "socket", "sqlite3"}
    assert not (imported & banned), f"detector must stay offline: {imported & banned}"


# --- endpoint ---

def _authed_request(monkeypatch, token="test-token"):
    from starlette.requests import Request

    monkeypatch.setenv("DASHBOARD_TOKEN", token)
    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": [(b"authorization", f"Bearer {token}".encode())]})


def _anon_request():
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": []})


def test_detect_requires_auth():
    from fastapi import HTTPException

    from ai_assistant.hub.api import InterviewDetectBody, hub_interview_detect

    with pytest.raises(HTTPException) as exc:
        hub_interview_detect(_anon_request(),
                             InterviewDetectBody(segments=["Что такое индекс?"]))
    assert exc.value.status_code == 401


def test_detect_validation(monkeypatch):
    from fastapi import HTTPException

    from ai_assistant.hub.api import InterviewDetectBody, hub_interview_detect

    req = _authed_request(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        hub_interview_detect(req, InterviewDetectBody(segments=["x"], lang="de"))
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        hub_interview_detect(req, InterviewDetectBody(segments=["  ", ""]))
    assert exc.value.detail == "empty-transcript"
    with pytest.raises(HTTPException) as exc:
        hub_interview_detect(req, InterviewDetectBody(segments=["q?"] * 51))
    assert exc.value.detail == "too-many-segments"
    with pytest.raises(HTTPException) as exc:
        hub_interview_detect(req, InterviewDetectBody(segments=["x?" * 5001]))
    assert exc.value.detail == "transcript-too-large"


def test_detect_ru_en_candidates_and_fields(monkeypatch):
    from ai_assistant.hub.api import InterviewDetectBody, hub_interview_detect

    req = _authed_request(monkeypatch)
    res = hub_interview_detect(req, InterviewDetectBody(
        segments=["Добрый день! Расскажите о себе.", "Мы используем Docker."]))
    assert res["ok"] is True and res["lang"] == "ru" and res["segments"] == 2
    assert len(res["candidates"]) == 1
    cand = res["candidates"][0]
    assert cand["text"] == "Расскажите о себе."
    assert set(cand) >= {"text", "lang", "confidence", "reason", "incomplete", "norm"}

    res_en = hub_interview_detect(req, InterviewDetectBody(
        segments=["What is your experience with Kubernetes?"], lang="en"))
    assert len(res_en["candidates"]) == 1


def test_detect_stateless_dedup_via_already_sent(monkeypatch):
    from ai_assistant.hub.api import InterviewDetectBody, hub_interview_detect

    req = _authed_request(monkeypatch)
    body = InterviewDetectBody(segments=["Как вы тестируете API?"])
    first = hub_interview_detect(req, body)
    assert len(first["candidates"]) == 1
    second = hub_interview_detect(
        req, InterviewDetectBody(segments=["Как вы тестируете API?"],
                                 already_sent=[first["candidates"][0]["norm"]]))
    assert second["candidates"] == [], "shown questions must not repeat"


def test_detect_writes_nothing_to_db(monkeypatch):
    """Detection is stateless: no turns, no sessions as a side effect."""
    from ai_assistant.hub.api import InterviewDetectBody, _interview_conn, hub_interview_detect
    from ai_assistant.interview import sessions as isess

    def _counts():
        conn = _interview_conn()
        try:
            turns = conn.execute("SELECT COUNT(*) FROM interview_turns").fetchone()[0]
            sess = conn.execute("SELECT COUNT(*) FROM interview_sessions").fetchone()[0]
        finally:
            conn.close()
        return turns, sess

    before = _counts()
    hub_interview_detect(_authed_request(monkeypatch),
                         InterviewDetectBody(segments=["Что такое индекс?"]))
    assert _counts() == before


def test_confirmed_candidate_reaches_hint_with_context(monkeypatch):
    """Phase 5 wiring: a confirmed question travels the existing hint path."""
    import json as _json

    from ai_assistant.hub.api import InterviewHintBody, _interview_conn, hub_interview_hint
    from ai_assistant.interview import sessions as isess
    import ai_assistant.interview.assistant as iasst

    seen = {}

    def _fake_call_model(system_prompt, user_prompt):
        seen["user"] = user_prompt
        return {"ok": True, "data": {
            "short": "Коротко.", "points": ["a", "b", "c"], "detailed": "Подробно.",
            "example": {"label": "HYPOTHETICAL", "text": "Пример."},
            "followups": ["Дальше?"], "missing_info": ""}, "elapsed": 0.1}

    monkeypatch.setattr(iasst, "call_model", _fake_call_model)
    question = "Сколько лет вы работаете с Python?"
    res = hub_interview_hint(_authed_request(monkeypatch),
                             InterviewHintBody(question=question,
                                               role_title="Python Developer"))
    assert res["ok"] is True and res["hint"]["detailed"] == "Подробно."
    assert question in seen["user"] and "Python Developer" in seen["user"]
    conn = _interview_conn()
    try:
        sess = isess.get_session(conn, res["session_id"])
    finally:
        conn.close()
    assert any(t["kind"] == "hint" and t["question"] == question
               for t in sess["turns"])


# --- 5B-1 MVP verification (hub.html contract + endpoint probes) ---

def _hub_html():
    return Path("ai_assistant/hub/static/hub.html").read_text(encoding="utf-8")


def _voice_region():
    text = _hub_html()
    start = text.index("/* ---- voice input")
    end = text.index("loadOverview();")
    return text[start:end]


def test_voice_consent_gates_mic():
    html = _hub_html()
    assert 'id="c-voice-consent"' in html
    assert 'id="c-mic" disabled' in html, "mic starts disabled until consent"
    region = _voice_region()
    assert "$('c-mic').disabled = !on" in region
    assert "if (!voiceConsent.checked)" in region


def test_live_panel_elements_present():
    html = _hub_html()
    for el in ("c-live-pick", "c-live-stop", "c-live-src", "c-live-level",
               "c-live-msg", "c-live-seg", "c-live-add", "c-live-mock-ru",
               "c-live-mock-en", "c-live-clear", "c-live-cands",
               "c-voice-text", "c-voice-ok"):
        assert f'id="{el}"' in html, f"missing live/voice element {el}"
    assert "MOCK" in html, "prototype mock surfaces must be labeled"


def test_voice_and_live_release_tracks_on_stop():
    region = _voice_region()
    assert region.count("getTracks().forEach") >= 2, \
        "voiceStop and liveStopAll must both stop every track"
    assert "tracks[0].onended" in region, "browser-side share stop must clean up"


def test_capture_refuses_source_without_audio_track():
    region = _voice_region()
    assert "getAudioTracks()" in region
    assert "без аудиодорожки" in region


def test_no_audio_persistence_in_voice_live_code():
    region = _voice_region()
    for banned in ("localStorage", "indexedDB", "sessionStorage"):
        assert banned not in region, f"captured audio must never persist via {banned}"


def test_denial_messages_are_explicit():
    region = _voice_region()
    assert "not-allowed" in region and "Доступ к микрофону запрещён" in region
    assert "NotAllowedError" in region


def test_transcribe_endpoint_creates_no_turns(monkeypatch):
    """Server voice path stores nothing: transcription is not a coach turn."""
    from ai_assistant.hub.api import _interview_conn, hub_interview_transcribe
    from ai_assistant.interview import stt as istt
    import asyncio

    from starlette.requests import Request

    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setattr(istt, "transcribe_audio",
                        lambda *a, **k: {"ok": True, "text": "Вопрос?", "lang": "ru",
                                         "engine": "fake", "audio_bytes": 3,
                                         "content_type": "audio/webm"})

    async def receive():
        return {"type": "http.request", "body": b"\x01\x02\x03", "more_body": False}

    req = Request({"type": "http", "method": "POST", "path": "/",
                   "headers": [(b"authorization", b"Bearer test-token"),
                               (b"content-type", b"audio/webm")]},
                  receive=receive)
    conn = _interview_conn()
    try:
        before = conn.execute("SELECT COUNT(*) FROM interview_turns").fetchone()[0]
    finally:
        conn.close()
    res = asyncio.run(hub_interview_transcribe(req, "ru"))
    assert res["ok"] is True
    conn = _interview_conn()
    try:
        after = conn.execute("SELECT COUNT(*) FROM interview_turns").fetchone()[0]
    finally:
        conn.close()
    assert after == before


def _live_region():
    text = Path("ai_assistant/hub/static/hub.html").read_text(encoding="utf-8")
    start = text.index("/* ---- live interview prototype")
    end = text.index("loadOverview();")
    return text[start:end]


def test_capture_requests_video_track():
    """Chrome rejects audio-only display capture (video:false) with
    NotSupportedError('Not supported'). The request must include video."""
    region = _live_region()
    assert "getDisplayMedia({ audio: true, video: true })" in region
    assert "video: false" not in region


def test_capture_stops_video_tracks_immediately():
    """Video is only requested to satisfy Chrome; only audio is ever used."""
    region = _live_region()
    assert "getVideoTracks()" in region
    assert "getAudioTracks()" in region, "audio-track refusal must stay"
