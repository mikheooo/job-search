"""T7.8 — SEARCH_ONLY emits nothing, and a bad geographic verdict is not permission.

Two independent claims are pinned here, because they fail in different ways.

**1. SEARCH_ONLY cannot reach an outbound action.** The autonomous cycle has six
places that write something outward: it creates an HH application, moves its
state, writes an application package, writes an application review, answers a
recruiter in the chat, and records the auto-reply. A mode that is supposed to
"only search and read" has to be shown to reach none of them -- and to reach none
of them *while the ordinary gates are open*, i.e. with an approval already on
record and ``SUBMIT_ALLOWED=true`` in the environment. A test that only proves
"nothing happened with the gates shut" would prove nothing about the mode.

The mode predicate is also pinned as fail-closed: ``None``, ``""``, a typo and a
non-string are all *not* FULL, so they cannot authorise anything. The safe answer
to "I do not know which mode this is" is "emit nothing".

**2. A missing or wrong geographic verdict is never permission.** Only band A is
auto-sendable. The predicate is asked about ``None``, about an object with no
``geo_band`` at all, about a lower-case band, about a boolean and about band B,
and every one of them must refuse. Band A is asserted to pass, so the check is
not merely "everything is refused".

One residual is pinned deliberately rather than fixed, and is called out in
``test_a_bare_string_band_is_still_read_as_a_band``: ``GeoBand`` is a ``str``
mixin enum, so the bare string ``"A"`` compares equal to ``GeoBand.A``. That is
not a fail-open -- a string ``"A"`` carries the same verdict as the enum -- but
it does mean a hand-written or corrupted ``"A"`` is indistinguishable from a
computed one. The consequence for live data is in the T7.8 report.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from ai_assistant import autonomous_mode, config, db
from ai_assistant.eligibility import GeoBand, geo_band_permits_auto_send
from ai_assistant.hh_autonomous_agent import (
    MSG_CHECK_SKIPPED,
    AutonomousConfig,
    AutonomousJobAgent,
    run_autonomous_cycle,
)

CONV_ID = "conv_t78_search_only"
VACANCY_ID = "139900001"
VACANCY_STABLE_ID = f"hh:{VACANCY_ID}"


@pytest.fixture
def clean_db(tmp_path):
    """A private database, and the module-level path put back afterwards."""
    original = config.DB_FILE
    config.DB_FILE = str(tmp_path / "test_t78.db")
    db.init_db()
    yield config.DB_FILE
    config.DB_FILE = original


# ---------------------------------------------------------------------------
# The browser stand-in, placed at the seam the SEARCH_ONLY path really uses
# ---------------------------------------------------------------------------
# ``_search_only_cycle`` never consults ``AutonomousConfig.evaluate_fn``: it runs
# the *owned* read-only search operation (``search_operation_report``), which
# acquires the HH browser through the ownership layer. These tests used to pass
# ``evaluate_fn`` and nothing else, so the stand-in was never reached, the cycle
# dialled the real CDP port 9111 and conftest blocked the connection - which is
# why the three SEARCH_ONLY tests were red.
#
# The operation is therefore replaced at its own seam with a canned report.
# Whether the search itself works is T7.26's subject and is pinned against a fake
# browser session in tests/test_t726_search_only_headless.py; what is pinned here
# is what a SEARCH_ONLY cycle WRITES. The real-port-9111 protection in conftest
# is untouched.

#: A description the matcher accepts, so the canned vacancy really does match.
MATCHING_DESCRIPTION = (
    "Разработка AI-сервисов, multi-agent систем, LLM интеграции, MCP, asyncio, "
    "PostgreSQL, Docker. 100% удалёнка."
)


def _canned_search_report(queries) -> dict:
    """One unique, matching, band-A vacancy read in full - no browser involved."""
    return {
        "busy": False,
        "start": {"ok": True, "status": "started", "message": ""},
        "unique_count": 1,
        "read_count": 1,
        "queries": [
            {"query": q, "result_count": 1, "unique_count": 1, "status": "ok"}
            for q in queries
        ],
        "vacancies": [
            {
                "ok": True,
                "vacancy_id": VACANCY_ID,
                "card": {"url": f"https://hh.ru/vacancy/{VACANCY_ID}"},
                "read": {
                    "title": "AI Engineer (Python / FastAPI)",
                    "employer": "T78 Test Co",
                    "description": MATCHING_DESCRIPTION,
                    "employment": "full",
                    "civil_law_contracts": None,
                    "salary": None,
                    "area": "Remote",
                    "archived": False,
                    "response_allowed": True,
                },
                "geo": {"band": "A", "evidence": "canned stand-in"},
                "preflight": {"verdict": "OK", "auto_send_permitted": False},
            }
        ],
    }


@pytest.fixture
def canned_search(monkeypatch):
    """Replace the owned search operation with a canned report; return its calls."""
    from ai_assistant import hh_readonly_operation as ro

    calls: list[list[str]] = []

    def _fake(queries, **kwargs):
        calls.append(list(queries))
        return _canned_search_report(queries)

    monkeypatch.setattr(ro, "search_operation_report", _fake)
    return calls


@pytest.fixture
def cdp_tripwire(monkeypatch):
    """Record any attempt to reach a browser, and answer the chat script if one is.

    ``_process_messages`` builds its transport from ``make_cdp_evaluate`` when the
    config carries no ``evaluate_fn``. Installing this makes "SEARCH_ONLY touched
    no transport" a measured claim rather than a claim about code that never ran:
    anything that did ask for a browser lands in the returned list.
    """
    from ai_assistant import prefill_execute as pe

    seen: list[str] = []

    def _factory(cdp_url, url_substring):
        def _evaluate(script: str) -> str:
            seen.append(script)
            return _recruiter_question_script()

        return _evaluate

    monkeypatch.setattr(pe, "make_cdp_evaluate", _factory)
    return seen


def _cards_script() -> str:
    """One fresh, unresponded vacancy that the matcher will accept."""
    return json.dumps([
        {
            "vacancy_id": VACANCY_ID,
            "title": "AI Automation Engineer",
            "employer": "T78 Test Co",
            "url": f"https://hh.ru/vacancy/{VACANCY_ID}",
            "already_responded": False,
        }
    ])


def _recruiter_question_script() -> str:
    """One employer message that the classifier treats as needing a reply."""
    return json.dumps([
        {
            "conversation_id": CONV_ID,
            "vacancy_title": "AI Automation Engineer",
            "vacancy_stable_id": VACANCY_STABLE_ID,
            "employer": "T78 Test Co",
            "messages": [
                {
                    "message_id": "m1",
                    "text": "Здравствуйте! Расскажите, какой у вас стек и опыт автоматизации?",
                    "sender": "employer",
                    "sent_at": "2026-09-25T16:00:00Z",
                }
            ],
        }
    ])


# ---------------------------------------------------------------------------
# 1. The mode predicate
# ---------------------------------------------------------------------------


def test_the_mode_predicate_is_fail_closed():
    """Only an explicit FULL authorises an outbound action."""
    assert autonomous_mode.outbound_allowed(autonomous_mode.FULL) is True
    assert autonomous_mode.outbound_allowed("FULL") is True
    assert autonomous_mode.outbound_allowed("  full  ") is True

    assert autonomous_mode.outbound_allowed(autonomous_mode.SEARCH_ONLY) is False
    assert autonomous_mode.outbound_allowed("SEARCH_ONLY") is False

    # Not knowing the mode must not read as permission.
    assert autonomous_mode.outbound_allowed(None) is False
    assert autonomous_mode.outbound_allowed("") is False
    assert autonomous_mode.outbound_allowed("   ") is False
    assert autonomous_mode.outbound_allowed("full time") is False
    assert autonomous_mode.outbound_allowed(0) is False
    assert autonomous_mode.outbound_allowed(1) is False
    assert autonomous_mode.outbound_allowed(True) is False
    assert autonomous_mode.outbound_allowed(["FULL"]) is False


def test_the_operator_entry_points_default_to_the_safe_mode():
    """`autonomous once` and `autonomous start` default to SEARCH_ONLY."""
    from ai_assistant import cli

    once = inspect.signature(cli.autonomous_once_cmd).parameters["mode"]
    start = inspect.signature(cli.autonomous_start_cmd).parameters["mode"]
    assert once.default == autonomous_mode.SEARCH_ONLY
    assert start.default == autonomous_mode.SEARCH_ONLY
    assert autonomous_mode.outbound_allowed(once.default) is False


# ---------------------------------------------------------------------------
# 2. A SEARCH_ONLY cycle writes nothing outward
# ---------------------------------------------------------------------------


def test_search_only_cycle_writes_nothing_outward(clean_db, monkeypatch, canned_search, cdp_tripwire):
    """No application, package, review, message event or audit is written.

    Every outbound path is reachable and would fire if the mode were consulted
    anywhere other than where it is: the search operation returns a matching
    band-A vacancy, the gates are opened deliberately (``SUBMIT_ALLOWED=true``),
    and the CDP tripwire stands ready to serve a recruiter question. The mode is
    the only thing holding the cycle back.
    """
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    assert config.submit_allowed() is True

    res = run_autonomous_cycle(
        config=AutonomousConfig(mode=autonomous_mode.SEARCH_ONLY)
    )

    # The search really ran (otherwise "nothing was written" would be vacuous)...
    assert canned_search, "the SEARCH_ONLY cycle never reached the search operation"
    # ...and no browser transport was ever reached for.
    assert cdp_tripwire == []

    assert res.status == "SEARCH_ONLY"
    assert res.applied_count == 0
    assert res.auto_replies_count == 0
    assert res.messages_checked == 0
    # T7.29 §4: a zero here is an explicit "not in scope", not an unexplained one.
    assert res.messages_check_status == MSG_CHECK_SKIPPED
    assert "SEARCH_ONLY" in res.messages_check_reason

    # Nothing was created, moved or approved.
    assert db.list_hh_applications(limit=100) == []
    assert db.list_application_packages(limit=100) == []
    assert db.list_submissions(limit=100) == []
    assert db.list_hh_message_events(limit=100) == []
    assert db.list_conversation_audits(limit=100) == []

    # The cycle still reports what it found -- that is the point of the mode.
    assert res.discovered_count == 1
    assert res.matched_count == 1
    assert res.applications_processed
    assert res.applications_processed[0]["submitted"] is False
    assert res.applications_processed[0]["verified"] is False


def test_search_only_cycle_never_runs_the_chat_send_script(clean_db, monkeypatch, canned_search, cdp_tripwire):
    """The cycle ran, and no script that types into the chat box was executed.

    The tripwire answers every browser request with a recruiter question, so if
    any part of the cycle had reached for a transport there would be something
    here to report.
    """
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")

    res = run_autonomous_cycle(
        config=AutonomousConfig(mode=autonomous_mode.SEARCH_ONLY)
    )

    assert canned_search, "the SEARCH_ONLY cycle never reached the search operation"
    assert res.discovered_count == 1
    assert cdp_tripwire == []


def test_an_unknown_mode_is_treated_as_search_only(clean_db, monkeypatch, canned_search, cdp_tripwire):
    """A typo cannot buy the full behaviour; it buys the silent one."""
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")

    res = run_autonomous_cycle(
        config=AutonomousConfig(mode="FUL")
    )

    assert res.status == "SEARCH_ONLY"
    assert cdp_tripwire == []
    assert db.list_hh_applications(limit=100) == []
    assert db.list_hh_message_events(limit=100) == []


# ---------------------------------------------------------------------------
# 3. The chat guard, exercised on its own
# ---------------------------------------------------------------------------


def test_the_chat_guard_records_the_draft_and_sends_nothing(clean_db, monkeypatch):
    """Called directly, ``_process_messages`` drafts the reply and stops there.

    Two guards, and this is the second one. The first is the mode check at the top
    of ``_process_messages``: T7.29 §4 made it refuse to read correspondence at all
    under SEARCH_ONLY, so a literal SEARCH_ONLY never reaches this code (pinned in
    tests/test_t729_message_check_status.py). The guard here is what still holds
    when the mode is *not recognised* - a typo is not FULL, so it must not buy the
    reply either. It is exercised on its own so that it is a tested surface rather
    than an assumption, and so that the draft is shown to survive: the operator can
    see what would have been sent.
    """
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")

    send_scripts: list[str] = []

    def mock_cdp(script: str) -> str:
        if "chat-input" in script or "input.value" in script:
            send_scripts.append(script)
            return json.dumps({"ok": True})
        return _recruiter_question_script()

    agent = AutonomousJobAgent(
        config=AutonomousConfig(evaluate_fn=mock_cdp, mode="FUL")
    )
    out = agent._process_messages()

    assert out["auto_replies_count"] == 0
    assert send_scripts == []

    events = db.list_hh_message_events(conversation_id=CONV_ID)
    assert len(events) == 1
    assert events[0]["status"] == "BLOCKED_SEARCH_ONLY"
    # The refusal names the mode it was actually given, which here is the typo.
    assert "forbids answering an employer message" in (events[0]["error"] or "")
    assert "FUL" in (events[0]["error"] or "")
    # The draft is preserved, so the block is auditable rather than invisible.
    assert events[0]["reply_draft"]

    audits = db.list_conversation_audits(conversation_id=CONV_ID)
    assert len(audits) == 1
    assert audits[0]["status"] != "SENT"


def test_the_same_chat_guard_lets_full_through(clean_db, monkeypatch):
    """The control: with FULL the very same mock does reach the send script.

    Without this, ``test_the_chat_guard_records_the_draft_and_sends_nothing``
    could pass because the send path is broken for everyone.
    """
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")

    send_scripts: list[str] = []

    def mock_cdp(script: str) -> str:
        if "chat-input" in script or "input.value" in script:
            send_scripts.append(script)
            return json.dumps({"ok": True})
        return _recruiter_question_script()

    agent = AutonomousJobAgent(
        config=AutonomousConfig(evaluate_fn=mock_cdp, mode=autonomous_mode.FULL)
    )
    out = agent._process_messages()

    assert out["auto_replies_count"] == 1
    assert len(send_scripts) == 1


# ---------------------------------------------------------------------------
# 4. An existing approval is not consumed by a SEARCH_ONLY cycle
# ---------------------------------------------------------------------------


def test_an_existing_approval_survives_a_search_only_cycle(clean_db, monkeypatch, canned_search):
    """An APPROVED review on record is neither used nor altered.

    The queue's single entry carries an approval from 2026-09-04. Whatever that
    approval is worth, a SEARCH_ONLY cycle must not spend it, and must not move
    the application it belongs to.
    """
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")

    from ai_assistant.application_review import (
        ApplicationReview,
        ReviewStatus,
        get_application_review,
        save_application_review,
    )

    db.save_hh_application({
        "application_id": f"app_hh_{VACANCY_ID}",
        "vacancy_stable_id": VACANCY_STABLE_ID,
        "title": "AI Automation Engineer",
        "employer": "T78 Test Co",
        "state": "READY_TO_APPLY",
    })
    save_application_review(ApplicationReview(
        vacancy_stable_id=VACANCY_STABLE_ID,
        status=ReviewStatus.APPROVED,
        note="t78 pre-existing approval",
    ))
    before = get_application_review(VACANCY_STABLE_ID)
    assert before is not None and before.status is ReviewStatus.APPROVED

    res = run_autonomous_cycle(
        config=AutonomousConfig(mode=autonomous_mode.SEARCH_ONLY)
    )
    assert res.status == "SEARCH_ONLY"

    after = get_application_review(VACANCY_STABLE_ID)
    assert after is not None
    assert after.status is ReviewStatus.APPROVED
    assert after.note == before.note
    assert after.updated_at == before.updated_at

    # The application did not move either.
    rows = db.list_hh_applications(limit=100)
    assert len(rows) == 1
    assert rows[0]["state"] == "READY_TO_APPLY"
    assert db.list_submissions(limit=100) == []


# ---------------------------------------------------------------------------
# 5. A bad geographic verdict is not permission
# ---------------------------------------------------------------------------


def test_a_missing_or_wrong_geo_verdict_is_never_permission():
    """Only a real band A passes; every other shape refuses."""
    allowed, reason = geo_band_permits_auto_send(SimpleNamespace(geo_band=GeoBand.A))
    assert allowed is True and reason == ""

    # Absent verdict.
    assert geo_band_permits_auto_send(None)[0] is False
    # Object without the attribute at all.
    assert geo_band_permits_auto_send(SimpleNamespace())[0] is False
    # Attribute present but empty.
    assert geo_band_permits_auto_send(SimpleNamespace(geo_band=None))[0] is False
    # A truthy non-band.
    assert geo_band_permits_auto_send(SimpleNamespace(geo_band=True))[0] is False
    # A misspelt band, including the lower case.
    assert geo_band_permits_auto_send(SimpleNamespace(geo_band="a"))[0] is False
    assert geo_band_permits_auto_send(SimpleNamespace(geo_band="AA"))[0] is False

    # B and C refuse, and the refusal names the band.
    for band in (GeoBand.B, GeoBand.C):
        allowed, reason = geo_band_permits_auto_send(SimpleNamespace(geo_band=band))
        assert allowed is False
        assert f"geo band {band.value}" in reason


def test_a_bare_string_band_is_still_read_as_a_band():
    """Pinned, not fixed: ``GeoBand`` is a ``str`` mixin enum.

    ``"A" == GeoBand.A`` is True, so a verdict that arrives as the bare string
    ``"A"`` passes the fuse. That is the same verdict as the enum, so it is not a
    fail-open -- but it does mean the fuse cannot tell a computed ``"A"`` from a
    hand-written one. Tightening this would be a behaviour change outside T7.8;
    the live consequence is recorded in ``docs/t78_report.md``.
    """
    assert "A" == GeoBand.A
    assert geo_band_permits_auto_send(SimpleNamespace(geo_band="A"))[0] is True


def test_the_send_gate_refuses_a_remote_vacancy_with_no_geography(clean_db):
    """``_hard_constraints_for_send`` refuses band B, where ``_hard_constraints`` does not.

    A remote posting that says nothing about where the candidate may live is band
    B. Analysis is allowed to keep reading it; sending is not.
    """
    from ai_assistant.matcher import (
        _coerce_profile,
        _hard_constraints,
        _hard_constraints_for_send,
    )
    from ai_assistant.schema import Vacancy

    vac = Vacancy(
        source="hh",
        source_job_id=VACANCY_ID,
        title="AI Automation Engineer",
        company="T78 Test Co",
        job_url=f"https://hh.ru/vacancy/{VACANCY_ID}",
        description="A perfectly ordinary remote job description with no geography in it.",
        location="Remote",
    )
    db.save_vacancy(vac)

    profile = _coerce_profile("ai_assistant/candidate_profile.json")

    analysis_reject, _ = _hard_constraints(profile, vac)
    send_reject, send_reason = _hard_constraints_for_send(profile, vac)

    assert analysis_reject is False, "band B must stay readable for analysis"
    assert send_reject is True
    assert "geo band B" in send_reason
