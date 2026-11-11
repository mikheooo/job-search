"""Phase 5B-3 pipeline tests (isolated, offline).

A. createPipe controller tested for real in node (extracted from hub.html):
   sequential single-flight, backpressure drop-oldest, epoch discard on
   stop, error recovery, cancel-while-queued. Needs node, else skipped.
B. Server chain with mock STT: chunk -> /transcribe -> /detect -> fast/hint,
   STT timeout mapping, failed fast creates no turns, dangling session 404,
   epoch/auto/toggle contracts pinned in hub.html.
No microphone, no network, no production writes (conftest tmp DB).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"

HARNESS_JS = r"""
const { createPipe } = require('./pipe.js');
const tick = async (n = 8) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };
function deferred() { let res, rej; const p = new Promise((a, b) => { res = a; rej = b; }); return { p, res, rej }; }
const report = {};
async function main() {
  // S1: one in flight, order preserved.
  {
    let conc = 0, maxConc = 0;
    const gates = [], events = [];
    const pipe = createPipe({ maxPending: 3,
      postChunk: (bytes, seq) => { conc++; maxConc = Math.max(maxConc, conc);
        const g = deferred(); gates.push(g);
        return g.p.then(t => { conc--; return t; }); },
      onEvent: e => events.push(e.type + (e.text ? ':' + e.text : '')) });
    pipe.start();
    const push = [pipe.push('a'), pipe.push('b'), pipe.push('c')];
    gates[0].res({ text: 'ta' }); await tick();
    gates[1].res({ text: 'tb' }); await tick();
    gates[2].res({ text: 'tc' }); await tick();
    report.s1 = { push, maxConc,
      transcripts: events.filter(e => e.startsWith('transcript')) };
  }
  // S2: full queue drops oldest, counts drops.
  {
    const gates = [], events = [];
    const pipe = createPipe({ maxPending: 2,
      postChunk: (bytes, seq) => { const g = deferred(); gates.push(g); return g.p; },
      onEvent: e => events.push(e) });
    pipe.start();
    ['p1', 'p2', 'p3', 'p4', 'p5'].forEach(b => pipe.push(b));
    const st = pipe.stats();
    gates[0].res({ text: 't1' }); await tick();
    gates[1].res({ text: 't4' }); await tick();
    gates[2].res({ text: 't5' }); await tick();
    report.s2 = { queuedAfterPush: st.queued, dropped: pipe.stats().dropped,
      transcripts: events.filter(e => e.type === 'transcript').map(e => e.text) };
  }
  // S3: stop before resolve -> discarded, never rendered.
  {
    const gates = [], events = [];
    const pipe = createPipe({ maxPending: 3,
      postChunk: () => { const g = deferred(); gates.push(g); return g.p; },
      onEvent: e => events.push(e.type) });
    pipe.start();
    pipe.push('a');
    pipe.stop();
    gates[0].res({ text: 'ta' }); await tick();
    report.s3 = { events };
  }
  // S4: rejection surfaces as error, pipe stays usable.
  {
    const events = [];
    let n = 0;
    const pipe = createPipe({ maxPending: 3,
      postChunk: async () => { n++; if (n === 1) throw new Error('stt-unavailable'); return { text: 'ok' }; },
      onEvent: e => events.push(e.type + (e.message ? ':' + e.message : '') + (e.text ? ':' + e.text : '')) });
    pipe.start();
    pipe.push('a'); await tick(); await tick();
    pipe.push('b'); await tick(); await tick();
    report.s4 = { events, stats: pipe.stats() };
  }
  // S5: cancel while queued clears; later pushes refused.
  {
    const gates = [], events = [];
    const pipe = createPipe({ maxPending: 3,
      postChunk: () => { const g = deferred(); gates.push(g); return g.p; },
      onEvent: e => events.push(e.type) });
    pipe.start();
    pipe.push('a'); pipe.push('b');
    pipe.stop();
    const after = pipe.push('c');
    const st = pipe.stats();
    gates[0].res({ text: 'ta' }); await tick();
    report.s5 = { after, active: st.active, queued: st.queued, events };
  }
  // S6: restart resets counters and epoch advances.
  {
    const pipe = createPipe({ maxPending: 3,
      postChunk: async () => ({ text: 't' }), onEvent: () => {} });
    pipe.start(); pipe.push('a'); await tick();
    const e1 = pipe.stats().epoch;
    pipe.start();
    const st = pipe.stats();
    report.s6 = { epochAdvanced: st.epoch === e1 + 1, dropped: st.dropped,
                  sent: st.sent, done: st.done };
  }
}
main().then(() => console.log(JSON.stringify(report)));
"""


def _pipe_report(tmp_path):
    html = Path("ai_assistant/hub/static/hub.html").read_text(encoding="utf-8")
    block = html.split("/*PIPE-START*/")[1].split("/*PIPE-END*/")[0]
    (tmp_path / "pipe.js").write_text(block + "\nmodule.exports = { createPipe };\n",
                                      encoding="utf-8")
    (tmp_path / "run.js").write_text(HARNESS_JS, encoding="utf-8")
    proc = subprocess.run([NODE, "run.js"], check=False, cwd=str(tmp_path), capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture()
def pipe_report(tmp_path):
    if not Path(NODE).exists():
        pytest.skip("node is required for the pipeline controller harness")
    return _pipe_report(tmp_path)


def test_pipe_sequential_single_flight(pipe_report):
    s1 = pipe_report["s1"]
    assert s1["push"] == ["queued", "queued", "queued"]
    assert s1["maxConc"] == 1, "never two simultaneous STT requests"
    assert s1["transcripts"] == ["transcript:ta", "transcript:tb", "transcript:tc"]


def test_pipe_backpressure_drops_oldest(pipe_report):
    s2 = pipe_report["s2"]
    assert s2["queuedAfterPush"] == 2
    assert s2["dropped"] == 2
    assert s2["transcripts"] == ["t1", "t4", "t5"], "oldest queued chunks dropped first"


def test_pipe_stop_discards_late_result(pipe_report):
    assert pipe_report["s3"]["events"] == ["state", "state", "discarded"]


def test_pipe_error_recovers(pipe_report):
    s4 = pipe_report["s4"]
    assert "error:stt-unavailable" in s4["events"]
    assert "transcript:ok" in s4["events"]
    assert s4["stats"]["active"] is True


def test_pipe_cancel_clears_queue(pipe_report):
    s5 = pipe_report["s5"]
    assert s5["after"] == "inactive"
    assert s5["active"] is False and s5["queued"] == 0
    assert "discarded" in s5["events"] and "transcript" not in "".join(s5["events"])


def test_pipe_restart_resets(pipe_report):
    s6 = pipe_report["s6"]
    assert s6 == {"epochAdvanced": True, "dropped": 0, "sent": 0, "done": 0}


# --- server chain with mock STT ---

def _authed_request(monkeypatch, token="test-token"):
    from starlette.requests import Request

    monkeypatch.setenv("DASHBOARD_TOKEN", token)
    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": [(b"authorization", f"Bearer {token}".encode())]})


def _anon_request():
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": []})


def _turn_count():
    from ai_assistant.hub.api import _interview_conn

    conn = _interview_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM interview_turns").fetchone()[0]
    finally:
        conn.close()


def test_full_chain_mock_stt_to_saved_turn(monkeypatch):
    """Chunk text (as a mock STT result) -> detect -> fast -> saved turn."""
    from ai_assistant.hub.api import (InterviewDetectBody, InterviewFastBody,
                                      hub_interview_detect, hub_interview_fast)
    from ai_assistant.interview import sessions as isess
    import ai_assistant.interview.assistant as iasst
    from ai_assistant.hub.api import _interview_conn

    # 1. mock STT transcript, as the pipe would deliver it
    transcripts = ["Сколько лет вы работаете с Python?"]
    # 2. detect (no LLM involved)
    det = hub_interview_detect(_authed_request(monkeypatch),
                               InterviewDetectBody(segments=transcripts))
    assert det["ok"] and len(det["candidates"]) == 1
    question = det["candidates"][0]["text"]
    # 3. user-confirmed question -> existing fast (mock LLM)
    monkeypatch.setattr(iasst, "call_model",
                        lambda s, u: {"ok": True, "data": {
                            "short": "Коротко.", "points": ["a", "b", "c"]}, "elapsed": 0.1})
    before = _turn_count()
    res = hub_interview_fast(_authed_request(monkeypatch),
                             InterviewFastBody(question=question,
                                               role_title="Python Developer"))
    assert res["ok"] is True
    assert _turn_count() == before + 1
    conn = _interview_conn()
    try:
        sess = isess.get_session(conn, res["session_id"])
    finally:
        conn.close()
    assert any(t["kind"] == "fast" and t["question"] == question
               for t in sess["turns"])


def test_stt_timeout_maps_retryable(monkeypatch):
    from ai_assistant.interview import stt as istt

    class _TimeoutEngine:
        name = "fake-stt"
        langs = ("ru", "en")

        def transcribe(self, audio, lang):
            raise TimeoutError("timed out after 20s")

    res = istt.transcribe_audio(b"\x01\x02", "ru", engine_obj=_TimeoutEngine())
    assert res == {"ok": False, "error": "engine-error", "retryable": True,
                   "detail": "TimeoutError"}


def test_failed_fast_creates_no_turn(monkeypatch):
    """STT/detector refusals and model failures must not write history rows."""
    from ai_assistant.hub.api import InterviewFastBody, hub_interview_fast
    import ai_assistant.interview.assistant as iasst

    monkeypatch.setattr(iasst, "call_model",
                        lambda s, u: {"ok": False, "error": "connection-error",
                                      "retryable": True, "elapsed": 1.0})
    before = _turn_count()
    res = hub_interview_fast(_authed_request(monkeypatch),
                             InterviewFastBody(question="Что такое индекс?"))
    assert res["ok"] is False
    assert _turn_count() == before


def test_dangling_session_id_is_404_without_side_effects(monkeypatch):
    """A cancelled/stale session must not launch or record anything."""
    from fastapi import HTTPException

    from ai_assistant.hub.api import InterviewFastBody, hub_interview_fast
    import ai_assistant.interview.assistant as iasst

    calls = []
    monkeypatch.setattr(iasst, "call_model",
                        lambda s, u: calls.append(1) or {"ok": True, "data": {}})
    before = _turn_count()
    with pytest.raises(HTTPException) as exc:
        hub_interview_fast(_authed_request(monkeypatch),
                           InterviewFastBody(question="Что такое индекс?",
                                             session_id=999999937))
    assert exc.value.status_code == 404
    assert calls == [], "no model call for a stale session"
    assert _turn_count() == before


# --- pipeline UI contracts (hub.html) ---

def _html():
    return Path("ai_assistant/hub/static/hub.html").read_text(encoding="utf-8")


def test_stt_toggle_defaults_off_and_requires_capture():
    html = _html()
    assert 'id="c-live-stt-on"' in html
    assert 'id="c-live-stt-on" checked' not in html, "STT upload defaults OFF"
    assert "Сначала захватите вкладку" in html


def test_auto_mode_defaults_off_with_high_confidence_gate():
    html = _html()
    assert 'id="c-live-auto"' in html
    assert 'id="c-live-auto" checked' not in html, "Auto defaults OFF (separate enable)"
    assert "c.confidence >= 0.85" in html
    assert "!c.incomplete" in html
    assert "coachRunFast(ep)" in html, "auto reuses the existing fast path"


def test_epoch_guard_present():
    html = _html()
    assert "let liveEpoch = 0" in html
    assert "ep !== undefined && ep !== liveEpoch" in html
    assert html.count("liveEpoch++") >= 2, "stop and clear must invalidate pending renders"


def test_pipeline_states_rendered():
    html = _html()
    for el in ("c-live-stt", "c-live-last", "c-live-detail"):
        assert f'id="{el}"' in html
    assert "async function coachRunFast(ep)" in html
    assert "$('c-fast').onclick = () => coachRunFast();" in html, \
        "manual clicks keep 5A behaviour (no epoch)"


def test_warmup_button_and_states_present():
    html = _html()
    assert 'id="c-live-warm"' in html and 'id="c-live-warm-state"' in html
    assert "STT: загрузка модели" in html
    assert "STT готов:" in html
    assert "api/hub/interview/warmup" in html
    region = html.split("$('c-live-warm').onclick")[1].split("$('c-gen')")[0]
    assert "getDisplayMedia" not in region and "MediaRecorder" not in region, \
        "warmup must not touch mic or recording"


def test_warmup_endpoint_auth_and_passthrough(monkeypatch):
    from ai_assistant.hub import api as hub
    from ai_assistant.interview import stt as istt
    from fastapi import HTTPException

    req = _authed_request(monkeypatch)
    monkeypatch.setattr(istt, "warmup",
                        lambda *a, **k: {"ok": True, "engine": "fake",
                                         "device": "cpu", "load_s": 0.1,
                                         "warmup_s": 0.2})
    res = hub.hub_interview_warmup(req)
    assert res["ok"] is True and res["device"] == "cpu"

    monkeypatch.setattr(istt, "warmup",
                        lambda *a, **k: {"ok": False, "error": "stt-unavailable",
                                         "retryable": False, "detail": "x",
                                         "load_s": 0.0})
    res = hub.hub_interview_warmup(req)
    assert res["ok"] is False

    with pytest.raises(HTTPException) as exc:
        hub.hub_interview_warmup(_anon_request())
    assert exc.value.status_code == 401


def _needs_local_audio():
    import os as _os

    model = r"C:\Users\Misha\Documents\job-search\artifacts\models\faster-whisper-small"
    audio = r"C:\Users\Misha\AppData\Local\Temp\opencode\stt_audio\en-q.wav"
    if not (_os.path.isdir(model) and _os.path.isfile(audio)):
        pytest.skip("local model/audio fixtures not present")


def test_route_level_pipeline_real_stt_mock_llm(monkeypatch):
    """Full route composition on real audio: transcribe -> detect -> fast.
    Times are informational (no latency assertions — loaded machines vary);
    the test pins stage success and question flow, mock LLM only."""
    import asyncio as _asyncio
    import time as _time

    from starlette.requests import Request

    from ai_assistant.hub import api as hub
    import ai_assistant.interview.assistant as iasst

    _needs_local_audio()
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setenv("STT_MODEL_DIR",
                       r"C:\Users\Misha\Documents\job-search\artifacts\models\faster-whisper-small")
    monkeypatch.setenv("STT_DEVICE", "cpu")
    monkeypatch.setattr(iasst, "call_model",
                        lambda s, u: {"ok": True, "data": {
                            "short": "MOCK.", "points": ["a", "b", "c"]}, "elapsed": 0.0})

    with open(r"C:\Users\Misha\AppData\Local\Temp\opencode\stt_audio\en-q.wav",
              "rb") as f:
        audio = f.read()

    async def receive():
        return {"type": "http.request", "body": audio, "more_body": False}

    def _req():
        return Request(
            {"type": "http", "method": "POST", "path": "/",
             "headers": [(b"authorization", b"Bearer test-token"),
                         (b"content-type", b"audio/wav")]},
            receive=receive)

    t0 = _time.monotonic()
    # engine=None: FastAPI would resolve Query(None); direct calls pass it plain.
    tr = _asyncio.run(hub.hub_interview_transcribe(_req(), "en", None))
    t_stt = _time.monotonic() - t0
    assert tr["ok"] is True and tr["text"], f"STT failed: {tr}"
    assert tr["device"] in ("cpu", "cuda")

    det = hub.hub_interview_detect(
        _authed_request(monkeypatch),
        hub.InterviewDetectBody(segments=[tr["text"]], lang="en"))
    assert det["ok"] and det["candidates"], "detector missed verified speech"
    question = det["candidates"][0]["text"]

    t1 = _time.monotonic()
    fast = hub.hub_interview_fast(
        _authed_request(monkeypatch),
        hub.InterviewFastBody(question=question, role_title="QA Engineer"))
    t_fast = _time.monotonic() - t1
    assert fast["ok"] is True and fast["fast"]["short"] == "MOCK."
    total = _time.monotonic() - t0
    assert fast["session_id"], "confirmed question must open a session turn"
    print(f"\n[pipeline] stt={t_stt:.1f}s detect+fast={t_fast:.2f}s total={total:.1f}s "
          f"q={question[:60]!r}")
