"""T8.10 — Hermes fetcher: concurrency lock and fail-closed digest export.

Two defects were measured on 2026-10-08 (PROJECT_STATE.md §11.5):

1. Two schedulers reached `job_search_fetcher.py` at ~10:00 (the Windows Task
   "JobSearch_Daily_Digest" via `ai_assistant.runner`, and the Hermes cron job
   "Job Search Monitor" directly). The runner's own `job_search.lock` covers only
   its own path, so both ran discovery/export at once and the second hit
   "database is locked".
2. When `export_digest_cmd` could not be read, the fetcher returned `("", [])`,
   printed the "no new vacancies" line and exited 0 — a silent fail-open. The
   runner recorded a success and `failure_tracker.json` recorded `last_success_at`
   while nothing was delivered.

These tests pin the fixes: an exclusive lock for the discovery/export phase, and
a non-zero exit when the export failed (distinct from a legitimate empty day).

A third defect, measured on the same run, is pinned here too: the digest used to
retry the Telegram send on *any* exception and fall back to a plain-text resend
when the sender raised ``TypeError``. A timeout after a successful server-side
write therefore delivered the digest twice. The sender now returns True/False for
confirmed outcomes, raises for ambiguous ones, and permits exactly one
plain-text retry after a confirmed Markdown rejection.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_CANONICAL_FETCHER = (
    Path(__file__).resolve().parent.parent / "integrations" / "hermes" / "job_search_fetcher.py"
)
_LOCK_NAME = "job_search_fetcher.lock"

# The fetcher's own hardcoded interpreter (its `JOB_SEARCH_ROOT` literal).
# `C:\Users\Misha\Documents\job-search` and `C:\Users\Misha\Мои документы\job-search`
# are the same directory, but not the same string. The module hands the process
# over to this interpreter whenever `sys.executable` differs from it — and that
# handover runs the script for real (as `__main__`) against the production
# state.db. Pinning `sys.executable` to this exact path makes the guard false so
# the module is only *loaded*.
_FETCHER_INTERPRETER = r"C:\Users\Misha\Documents\job-search\.venv\Scripts\python.exe"


def _load_fetcher(monkeypatch, executable=_FETCHER_INTERPRETER):
    """Load the canonical fetcher under a private module name, without running it.

    The project's other tests import `job_search_fetcher` from the deployed
    Hermes path; loading the canonical copy under its own name keeps this file
    independent of import order.

    ``executable`` pins ``sys.executable`` for the duration of the load so the
    module-level interpreter handover sees the project venv and stays dormant.
    """
    monkeypatch.setattr(sys, "executable", executable)
    spec = importlib.util.spec_from_file_location("t810_job_search_fetcher", _CANONICAL_FETCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def jsf(monkeypatch):
    return _load_fetcher(monkeypatch)


@pytest.fixture(autouse=True)
def _no_real_watcher(monkeypatch):
    """Keep the poll cycle offline: it must not attempt a real fetch here."""
    from ai_assistant.watcher import Watcher

    def _raise(*args, **kwargs):
        raise RuntimeError("offline test: poll disabled")

    monkeypatch.setattr(Watcher, "poll_once", _raise)


def _stub_scoring(monkeypatch, jsf):
    monkeypatch.setattr(jsf, "score_unscored_vacancies", lambda: 0)


# ---------------------------------------------------------------------------
# Interpreter handover guard (path aliases)
# ---------------------------------------------------------------------------

def test_path_alias_does_not_trigger_interpreter_handover(monkeypatch):
    """A path *alias* of the project interpreter must not trip the handover.

    The guard used `os.path.abspath(...).lower()`, which compares raw strings.
    `C:\\Users\\Misha\\Documents\\job-search` and
    `C:\\Users\\Misha\\Мои документы\\job-search` are the same folder, so running
    the project venv's python through the localized alias made `sys.executable`
    look different and dispatched a *real* production run on a mere import.
    Canonicalizing with realpath+normcase makes any alias of the same file equal.
    """
    import subprocess

    def _boom(*args, **kwargs):
        raise AssertionError(
            "interpreter handover fired for a path alias of the project interpreter"
        )

    monkeypatch.setattr(subprocess, "run", _boom)
    # A case/format variant of the same interpreter; normcase folds it back.
    module = _load_fetcher(monkeypatch, executable=_FETCHER_INTERPRETER.upper())
    assert module is not None


# ---------------------------------------------------------------------------
# Fail-closed export
# ---------------------------------------------------------------------------

def test_nonzero_export_code_is_a_failure(monkeypatch, jsf):
    """A non-zero exporter code must surface as None, not as an empty day."""
    import ai_assistant.cli as cli

    _stub_scoring(monkeypatch, jsf)
    monkeypatch.setattr(cli, "export_digest_cmd", lambda **kwargs: 1)

    assert jsf.run_canonical_discovery_and_export(limit=1, min_score=0.0) is None


def test_empty_export_output_is_a_failure(monkeypatch, jsf):
    """Exit code 0 with no stdout is still a failure: the digest was not read."""
    import ai_assistant.cli as cli

    _stub_scoring(monkeypatch, jsf)
    monkeypatch.setattr(cli, "export_digest_cmd", lambda **kwargs: 0)  # prints nothing

    assert jsf.run_canonical_discovery_and_export(limit=1, min_score=0.0) is None


def test_unparseable_export_output_is_a_failure(monkeypatch, jsf):
    import ai_assistant.cli as cli

    _stub_scoring(monkeypatch, jsf)

    def _garbage(**kwargs):
        print("Failed to open vacancy DB: database is locked")
        return 0

    monkeypatch.setattr(cli, "export_digest_cmd", _garbage)

    assert jsf.run_canonical_discovery_and_export(limit=1, min_score=0.0) is None


def test_valid_empty_json_is_a_legitimate_no_op(monkeypatch, jsf):
    """A real empty result is not a failure: it parses to a tuple, not None."""
    import ai_assistant.cli as cli

    _stub_scoring(monkeypatch, jsf)

    def _empty(**kwargs):
        print(json.dumps({"telegram_post": "no vacancies today", "new_vacancies_data": []}))
        return 0

    monkeypatch.setattr(cli, "export_digest_cmd", _empty)

    result = jsf.run_canonical_discovery_and_export(limit=1, min_score=0.0)
    assert result is not None
    post_text, vacancies_data = result
    assert post_text == "no vacancies today"
    assert vacancies_data == []


def test_main_exits_nonzero_when_export_fails(monkeypatch, jsf):
    """The top-level entry point must not report success for a failed export."""
    monkeypatch.setattr(jsf, "run_canonical_discovery_and_export", lambda **kwargs: None)

    with pytest.raises(SystemExit) as exc:
        jsf.main()

    assert exc.value.code == 1


def test_main_does_not_exit_on_a_legitimate_empty_day(monkeypatch, jsf):
    monkeypatch.setattr(
        jsf, "run_canonical_discovery_and_export", lambda **kwargs: ("no vacancies", [])
    )

    jsf.main()  # must return normally, not raise SystemExit


# ---------------------------------------------------------------------------
# Concurrency lock
# ---------------------------------------------------------------------------

def test_second_concurrent_run_is_a_safe_noop(monkeypatch, jsf):
    """A live lock owner makes the second arrival skip without doing work."""
    from ai_assistant import config

    os.makedirs(config.LOGS_DIR, exist_ok=True)
    lock_path = os.path.join(config.LOGS_DIR, _LOCK_NAME)
    with open(lock_path, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "timestamp": "2026-10-08T03:00:00+00:00"}, handle)

    calls = []
    monkeypatch.setattr(
        jsf,
        "run_canonical_discovery_and_export",
        lambda **kwargs: calls.append(1) or ("text", []),
    )

    jsf.main()

    assert calls == [], "the second run must not perform discovery/export"
    assert os.path.exists(lock_path), "a non-owner must not delete the owner's lock"


def test_lock_is_released_after_a_run(monkeypatch, jsf):
    """A completed run must leave the lock free for the next day."""
    from ai_assistant import config

    monkeypatch.setattr(
        jsf, "run_canonical_discovery_and_export", lambda **kwargs: ("no vacancies", [])
    )

    jsf.main()

    assert not os.path.exists(os.path.join(config.LOGS_DIR, _LOCK_NAME))


def test_lock_is_released_even_when_the_run_fails_closed(monkeypatch, jsf):
    """sys.exit(1) inside the run must still release the lock (finally)."""
    from ai_assistant import config

    monkeypatch.setattr(jsf, "run_canonical_discovery_and_export", lambda **kwargs: None)

    with pytest.raises(SystemExit):
        jsf.main()

    assert not os.path.exists(os.path.join(config.LOGS_DIR, _LOCK_NAME))


# ---------------------------------------------------------------------------
# Telegram send contract: confirmed / ambiguous, and the single format retry
# ---------------------------------------------------------------------------

class _OkResponse:
    """Minimal successful Telegram response."""

    status = 200

    def read(self):
        return json.dumps({"ok": True, "result": {"message_id": 42}}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _telegram_http_error(code, description):
    """A real HTTPError carrying a Telegram JSON error body."""
    import io
    import urllib.error

    return urllib.error.HTTPError(
        "https://api.telegram.org/bot000000000:TEST_TOKEN/sendMessage",
        code,
        "Telegram error",
        None,
        io.BytesIO(json.dumps({"ok": False, "description": description}).encode("utf-8")),
    )


def _install_send_outcomes(monkeypatch, jsf, outcomes):
    """Replay `outcomes` (responses to return, exceptions to raise) to urlopen.

    Returns the list of request payloads actually sent, so a test can prove
    how many requests left the process.
    """
    import urllib.request

    sent = []
    remaining = list(outcomes)

    def _fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        if not remaining:
            raise AssertionError("the sender issued more requests than allowed")
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return sent


@pytest.fixture
def _fake_token(monkeypatch, jsf):
    """Pin a fake token so the sender's file lookups never touch real secrets."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "000000000:TEST_TOKEN")
    # The fetcher prefers the Hermes profile .env when it exists; force the
    # environment path so these tests do not depend on the machine's real file.
    monkeypatch.setattr(jsf.Path, "exists", lambda _self: False, raising=True)


def test_confirmed_delivery_sends_once(monkeypatch, jsf, _fake_token):
    sent = _install_send_outcomes(monkeypatch, jsf, [_OkResponse()])

    assert jsf.send_to_telegram("digest text") is True
    assert len(sent) == 1
    assert sent[0]["parse_mode"] == "Markdown"


def test_markdown_parse_error_retries_once_as_plain_text(monkeypatch, jsf, _fake_token):
    """Only a confirmed formatting rejection earns the single plain-text retry."""
    sent = _install_send_outcomes(
        monkeypatch,
        jsf,
        [
            _telegram_http_error(400, "Bad Request: can't parse entities: end of Bold entity"),
            _OkResponse(),
        ],
    )

    assert jsf.send_to_telegram("digest *text*") is True
    assert len(sent) == 2, "exactly one retry is permitted"
    assert sent[0]["parse_mode"] == "Markdown"
    assert "parse_mode" not in sent[1]


def test_a_repeated_markdown_failure_is_not_retried_twice(monkeypatch, jsf, _fake_token):
    parse_error = _telegram_http_error(400, "Bad Request: can't parse entities")
    sent = _install_send_outcomes(monkeypatch, jsf, [parse_error, parse_error])

    assert jsf.send_to_telegram("digest *text*") is False
    assert len(sent) == 2


def test_timeout_is_not_retried_and_raises_for_manual_review(monkeypatch, jsf, _fake_token):
    """A timeout may have delivered already: the raise makes the caller record
    AMBIGUOUS instead of sending the digest a second time."""
    import socket

    sent = _install_send_outcomes(monkeypatch, jsf, [socket.timeout("timed out")])

    with pytest.raises(Exception) as exc:
        jsf.send_to_telegram("digest text")
    assert "timed out" in str(exc.value)
    assert len(sent) == 1, "a timeout must never trigger a resend"


def test_plain_text_retry_that_times_out_still_raises(monkeypatch, jsf, _fake_token):
    import socket

    sent = _install_send_outcomes(
        monkeypatch,
        jsf,
        [_telegram_http_error(400, "Bad Request: can't parse entities"), socket.timeout("timed out")],
    )

    with pytest.raises(Exception) as exc:
        jsf.send_to_telegram("digest *text*")
    assert "timed out" in str(exc.value)
    assert len(sent) == 2


def test_auth_failure_is_confirmed_and_never_retried(monkeypatch, jsf, _fake_token):
    sent = _install_send_outcomes(monkeypatch, jsf, [_telegram_http_error(401, "Unauthorized")])

    assert jsf.send_to_telegram("digest text") is False
    assert len(sent) == 1


def test_a_400_that_is_not_about_markdown_is_a_confirmed_failure(monkeypatch, jsf, _fake_token):
    sent = _install_send_outcomes(
        monkeypatch, jsf, [_telegram_http_error(400, "Bad Request: chat not found")]
    )

    assert jsf.send_to_telegram("digest text") is False
    assert len(sent) == 1, "a non-format error must not be retried as plain text"


def test_a_server_error_that_mentions_parsing_is_not_a_markdown_retry(monkeypatch, jsf, _fake_token):
    sent = _install_send_outcomes(
        monkeypatch, jsf, [_telegram_http_error(500, "Internal Server Error: can't parse entities")]
    )

    assert jsf.send_to_telegram("digest text") is False
    assert len(sent) == 1


def test_ambiguous_classifier_separates_may_have_delivered_from_cannot_deliver(jsf):
    import socket
    import urllib.error

    assert jsf._is_ambiguous_transport_error(socket.timeout("timed out"))
    assert jsf._is_ambiguous_transport_error(urllib.error.URLError(socket.timeout("timed out")))
    # Refused/DNS with the request never sent: a confirmed failure, not ambiguous.
    assert not jsf._is_ambiguous_transport_error(urllib.error.URLError(ConnectionRefusedError()))
    assert not jsf._is_ambiguous_transport_error(ValueError("boom"))


def test_markdown_classifier_needs_a_400_with_parse_markers(jsf):
    assert jsf._is_markdown_format_error(_telegram_http_error(400, "Bad Request: can't parse entities"))
    assert not jsf._is_markdown_format_error(_telegram_http_error(400, "Bad Request: chat not found"))
    assert not jsf._is_markdown_format_error(_telegram_http_error(500, "can't parse entities"))
    assert not jsf._is_markdown_format_error(ValueError("can't parse entities"))


def test_auth_classifier_covers_401_403_and_text_matches(jsf):
    assert jsf._is_auth_error(_telegram_http_error(401, "Unauthorized"))
    assert jsf._is_auth_error(_telegram_http_error(403, "Forbidden: bot was blocked by the user"))
    assert not jsf._is_auth_error(_telegram_http_error(400, "Bad Request"))
    assert not jsf._is_auth_error(ValueError("boom"))


def test_ambiguous_send_is_recorded_and_never_resent(monkeypatch, jsf):
    """The caller must record AMBIGUOUS and stop: a raise from the sender means
    the message may already be in the channel."""
    import socket

    import ai_assistant.db as db
    from ai_assistant.telegram_notifier import TelegramNotifier

    monkeypatch.setattr(
        jsf,
        "run_canonical_discovery_and_export",
        lambda **kwargs: ("digest text", [{"id": "hh:1", "title": "Vac", "company": "Acme"}]),
    )

    sends = []

    def _timeout(*args, **kwargs):
        sends.append(1)
        raise socket.timeout("timed out")

    monkeypatch.setattr(jsf, "send_to_telegram", _timeout)

    recorded = {}

    def _record_ambiguous(ids, batch_key=None, chat_id=None, reason=""):
        recorded["ambiguous"] = (list(ids), batch_key, reason)
        return len(ids)

    monkeypatch.setattr(db, "record_digest_attempt", lambda ids, **kw: "digest_batch:test")
    monkeypatch.setattr(db, "record_digest_ambiguous", _record_ambiguous)
    monkeypatch.setattr(db, "mark_digest_delivered", lambda *a, **kw: recorded.setdefault("delivered", True))
    monkeypatch.setattr(db, "record_digest_failed", lambda *a, **kw: recorded.setdefault("failed", True))
    monkeypatch.setattr(TelegramNotifier, "build_digest_inline_keyboard", lambda data: {"inline_keyboard": []})

    with pytest.raises(SystemExit) as exc:
        jsf._run_discovery_and_deliver(dry_run=False)

    assert exc.value.code == 1
    assert len(sends) == 1, "an ambiguous result must not be retried"
    assert recorded.get("ambiguous"), "a raise must be recorded as AMBIGUOUS"
    assert "delivered" not in recorded, "an ambiguous result is not a delivery"
    assert "failed" not in recorded, "an ambiguous result is not a confirmed failure"
