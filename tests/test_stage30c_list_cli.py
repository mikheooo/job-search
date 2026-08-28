"""Stage 30C-1: regression tests for the fixed `list` CLI subcommand.

Historical bug: `python -m ai_assistant.cli list` raised
`NameError: name 'list_cmd' is not defined` — the subcommand was registered in
main()'s argparse dispatch but its handler was never wired. This test locks in
the fix and verifies the command's *actual* expected result (rendered stored
vacancies), not merely the absence of an exception.

Pure read: DB access and rendering are faked; no fetch, no write, no send.
"""

from __future__ import annotations

import sys

import pytest

from ai_assistant import cli
from ai_assistant.schema import Vacancy


class _FakeRow:
    """Minimal stand-in for a sqlite row from list_vacancies; _row_to_vacancy
    is monkeypatched, so its true schema is irrelevant here."""


def _vac(delta: int, title: str) -> Vacancy:
    return Vacancy(
        source="test",
        source_job_id=f"j{delta}",
        title=title,
        company="Acme",
        description="desc",
        location="Remote",
        job_url=f"https://x.example/{delta}",
    )


def _patch_db(monkeypatch, rows, row_to_vac):
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "list_vacancies",
                        lambda limit=20, state=None: rows)
    monkeypatch.setattr(cli, "_row_to_vacancy", row_to_vac)


def test_cli_list_dispatch_no_nameerror_and_prints_vacancies(monkeypatch, capsys):
    """The `list` subcommand reaches a handler and renders stored vacancies
    (the command's expected result), not just silently succeeding."""
    row = _FakeRow()
    _patch_db(monkeypatch, [row, _FakeRow()], lambda r: _vac(1, "AI Platform Engineer"))

    monkeypatch.setattr(sys, "argv", ["job-search-cli", "list"])
    rc = cli.main()

    out = capsys.readouterr().out
    assert rc == 0                      # correct exit code for success
    assert "AI Platform Engineer" in out   # actual vacancy content is printed
    assert "Acme" in out
    assert "vacancy(ies) listed" in out


def test_cli_list_passes_limit_and_state_to_query(monkeypatch, capsys):
    """Ensure the requested --limit/--state reach the existing list_vacancies
    query (that is the handler's real behavioural contract)."""
    seen = {}

    def fake_list(limit=20, state=None):
        seen["limit"] = limit
        seen["state"] = state
        return []

    def fake_to_vac(r):
        raise AssertionError("should not convert rows when none returned")

    _patch_db(monkeypatch, [], fake_to_vac)
    monkeypatch.setattr(cli, "list_vacancies", fake_list)
    monkeypatch.setattr(sys, "argv",
                        ["job-search-cli", "list", "--state", "new", "--limit", "5"])
    rc = cli.main()

    assert rc == 0
    assert seen == {"limit": 5, "state": "new"}


def test_cli_list_empty_db_is_success(monkeypatch, capsys):
    """Empty result set must still exit 0 (the command ran successfully)."""
    _patch_db(monkeypatch, [], lambda r: _vac(1, "x"))
    monkeypatch.setattr(sys, "argv", ["job-search-cli", "list"])
    rc = cli.main()
    assert rc == 0
    assert "No stored vacancies found." in capsys.readouterr().err


def test_cli_list_db_error_returns_nonzero(monkeypatch):
    """If the DB cannot be opened, the command must fail (nothing mutated)."""
    def boom_init():
        raise RuntimeError("db down")
    monkeypatch.setattr(cli, "init_db", boom_init)
    monkeypatch.setattr(sys, "argv", ["job-search-cli", "list"])
    rc = cli.main()
    assert rc == 1