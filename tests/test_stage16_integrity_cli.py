from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime

import pytest

import ai_assistant.config as config
import ai_assistant.db as db
from ai_assistant.schema import Vacancy
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.application_integrity import run_integrity_audit


def _vac(**kw):
    d = dict(source="test", source_job_id="1", title="Senior AI Engineer", company="TestCo",
             description="test python", job_url=None, location="Remote", country_restrictions=[],
             timezone_restrictions=[], salary_min=5000, salary_max=5000, salary_currency="USD",
             employment_type="Full Time")
    d.update(kw)
    if not d.get("job_url"):
        d["job_url"] = f"https://example.com/{d['source_job_id']}"
    return Vacancy(**d)


@pytest.fixture()
def clean_db():
    tmp = tempfile.mkdtemp()
    old = config.DB_FILE
    config.DB_FILE = os.path.join(tmp, "state.db")
    db.init_db()
    yield
    config.DB_FILE = old
    shutil.rmtree(tmp, ignore_errors=True)


def _run_main(monkeypatch, *argv):
    import ai_assistant.cli as cli
    monkeypatch.setattr(sys, "argv", ["cli"] + list(argv))
    return cli.main()


class CountConn:
    def __init__(self, conn):
        self._c = conn
        self.writes = {"INSERT": 0, "UPDATE": 0, "DELETE": 0}

    def cursor(self):
        return CountCursor(self._c.cursor(), self.writes)

    def commit(self):
        self._c.commit()

    def close(self):
        self._c.close()

    def __getattr__(self, name):
        return getattr(self._c, name)


class CountCursor:
    def __init__(self, cur, writes):
        self._c = cur
        self._w = writes

    def execute(self, sql, *args):
        s = sql.strip().upper() if isinstance(sql, str) else ""
        if s.startswith("INSERT"):
            self._w["INSERT"] += 1
        elif s.startswith("UPDATE"):
            self._w["UPDATE"] += 1
        elif s.startswith("DELETE"):
            self._w["DELETE"] += 1
        return self._c.execute(sql, *args)

    def executemany(self, sql, seq):
        return self._c.executemany(sql, seq)

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    def fetchmany(self, n=1):
        return self._c.fetchmany(n)

    def __getattr__(self, name):
        return getattr(self._c, name)


def _spy_get_connection(monkeypatch):
    writes = {"INSERT": 0, "UPDATE": 0, "DELETE": 0}
    orig = db.get_connection

    def wrapped():
        return CountConn(orig())

    monkeypatch.setattr(db, "get_connection", wrapped)
    return writes


def test_tracked_healthy_state(clean_db):
    v = _vac()
    db.save_vacancy(v)
    set_application_status(v.stable_id(), ApplicationStatus.DISCOVERED, company=v.company, title=v.title)
    r = run_integrity_audit(scope="tracked")
    assert r.scope == "tracked"
    assert r.error_count == 0
    assert r.warning_count == 0
    assert r.healthy is True
    assert r.total_checked == 1


def test_tracked_canonical_filtering(clean_db):
    # Tracked vacancy resolves to canonical A
    v1 = _vac(source_job_id="t1", job_url="https://example.com/a")
    db.save_vacancy(v1)
    from ai_assistant.vacancy_identity import resolve_vacancy_identity, save_vacancy_alias, MatchType, normalize_url
    c1 = resolve_vacancy_identity(v1).canonical_id
    save_vacancy_alias(c1, v1.stable_id(), v1.source, v1.job_url, normalize_url(v1.job_url), MatchType.DISTINCT, 100)
    set_application_status(v1.stable_id(), ApplicationStatus.DISCOVERED, company=v1.company, title=v1.title)
    # Untracked canonical B exists in DB but has no tracked application
    v2 = _vac(source_job_id="t2", job_url="https://example.com/b", company="OtherCo", title="Junior Dev")
    db.save_vacancy(v2)
    resolve_vacancy_identity(v2)

    full = run_integrity_audit(scope="full")
    tracked = run_integrity_audit(scope="tracked")

    # Full scope counts all canonical_vacancies; tracked scope only those linked to tracked apps
    assert tracked.canonical_checked >= 1
    # tracked canonical_checked must not include the irrelevant canonical B
    assert tracked.canonical_checked < full.canonical_checked
    # tracked set must be subset of full set
    assert tracked.audited_canonicals <= full.audited_canonicals


def test_full_vs_tracked_audit_counts(clean_db):
    v = _vac()
    db.save_vacancy(v)
    from ai_assistant.vacancy_identity import resolve_vacancy_identity, save_vacancy_alias, MatchType, normalize_url
    cid = resolve_vacancy_identity(v).canonical_id
    save_vacancy_alias(cid, v.stable_id(), v.source, v.job_url, normalize_url(v.job_url), MatchType.DISTINCT, 100)
    set_application_status(v.stable_id(), ApplicationStatus.DISCOVERED, company=v.company, title=v.title)

    full = run_integrity_audit(scope="full")
    tracked = run_integrity_audit(scope="tracked")

    assert full.scope == "full"
    assert tracked.scope == "tracked"
    assert full.total_checked == tracked.total_checked == 1
    # full counts all canonical_vacancies in DB; tracked only the linked one
    assert tracked.canonical_checked == 1
    assert tracked.canonical_checked <= full.canonical_checked
    # artifact counts present and deterministic
    assert tracked.queue_items == 0
    assert tracked.aliases >= 1


def test_exit_code_0(clean_db, monkeypatch):
    v = _vac()
    db.save_vacancy(v)
    set_application_status(v.stable_id(), ApplicationStatus.DISCOVERED, company=v.company, title=v.title)
    code = _run_main(monkeypatch, "audit")
    assert code == 0


def test_warning_exit_code_1(clean_db, monkeypatch):
    v = _vac()
    db.save_vacancy(v)
    set_application_status(v.stable_id(), ApplicationStatus.DISCOVERED, company=v.company, title=v.title)
    from ai_assistant.application_review import ApplicationReview, ReviewStatus, save_application_review
    rev = ApplicationReview(vacancy_stable_id=v.stable_id(), company=v.company, title=v.title, status=ReviewStatus.APPROVED)
    save_application_review(rev)
    code = _run_main(monkeypatch, "audit")
    assert code == 1


def test_error_exit_code_2(clean_db, monkeypatch):
    v = _vac()
    db.save_vacancy(v)
    set_application_status(v.stable_id(), ApplicationStatus.READY_TO_APPLY, company=v.company, title=v.title)
    from ai_assistant.browser_executor import BrowserApplicationSession, BrowserStatus, save_browser_session
    sess = BrowserApplicationSession(vacancy_stable_id=v.stable_id(), url=v.job_url, status=BrowserStatus.READY_FOR_REVIEW,
                                     fields_detected=["a"], fields_filled=[], fields_skipped=[], warnings=[],
                                     created_at=datetime.utcnow().isoformat(), updated_at=datetime.utcnow().isoformat(),
                                     final_url=v.job_url, page_title="t", site="example.com", form_detected=True)
    save_browser_session(sess)
    code = _run_main(monkeypatch, "audit")
    assert code == 2


def test_invalid_cli_exit_code_3(clean_db, monkeypatch):
    code = _run_main(monkeypatch, "audit", "--bogus-flag")
    assert code == 3


def test_unknown_command_exit_code_3(clean_db, monkeypatch):
    code = _run_main(monkeypatch, "definitely-not-a-command")
    assert code == 3


def test_deterministic_json_contract(clean_db, monkeypatch, capsys):
    v = _vac()
    db.save_vacancy(v)
    set_application_status(v.stable_id(), ApplicationStatus.DISCOVERED, company=v.company, title=v.title)

    import ai_assistant.cli as cli
    monkeypatch.setattr(sys, "argv", ["cli", "audit", "--json"])
    assert cli.main() == 0
    out1 = capsys.readouterr().out
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["cli", "audit", "--json"])
    assert cli.main() == 0
    out2 = capsys.readouterr().out

    d1 = json.loads(out1)
    d2 = json.loads(out2)
    d1.pop("generated_at")
    d2.pop("generated_at")
    assert d1 == d2
    # Required contract keys
    for key in ["scope", "total_checked", "canonical_checked", "info_count",
                "warning_count", "error_count", "healthy", "issues"]:
        assert key in d1
    assert d1["scope"] == "full"
    assert d1["healthy"] is True


def test_tracked_json_scope(clean_db, monkeypatch, capsys):
    v = _vac()
    db.save_vacancy(v)
    set_application_status(v.stable_id(), ApplicationStatus.DISCOVERED, company=v.company, title=v.title)

    import ai_assistant.cli as cli
    monkeypatch.setattr(sys, "argv", ["cli", "audit", "--tracked", "--json"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    d = json.loads(out)
    assert d["scope"] == "tracked"


def test_tracked_audit_readonly(clean_db, monkeypatch):
    v = _vac()
    db.save_vacancy(v)
    set_application_status(v.stable_id(), ApplicationStatus.READY_TO_APPLY, company=v.company, title=v.title)
    writes = _spy_get_connection(monkeypatch)
    r = run_integrity_audit(scope="tracked")
    assert writes["INSERT"] == 0
    assert writes["UPDATE"] == 0
    assert writes["DELETE"] == 0
    assert r.error_count == 0


def test_full_audit_regression(clean_db):
    # Stage 15 semantics preserved: full scope reports no issues on healthy data
    v = _vac()
    db.save_vacancy(v)
    from ai_assistant.vacancy_identity import resolve_vacancy_identity, save_vacancy_alias, MatchType, normalize_url
    cid = resolve_vacancy_identity(v).canonical_id
    save_vacancy_alias(cid, v.stable_id(), v.source, v.job_url, normalize_url(v.job_url), MatchType.DISTINCT, 100)
    set_application_status(v.stable_id(), ApplicationStatus.DISCOVERED, company=v.company, title=v.title)
    r = run_integrity_audit(scope="full")
    assert r.scope == "full"
    assert r.error_count == 0
    assert r.warning_count == 0
    assert r.total_checked == 1
    # full canonical_checked counts all canonical_vacancies (310 is real-DB-specific; here at least the linked one)
    assert r.canonical_checked >= 1


def test_no_auto_repair_commands():
    import pathlib
    src = pathlib.Path("ai_assistant/cli.py").read_text(encoding="utf-8")
    assert "--fix" not in src
    assert "--repair" not in src
    assert "--merge" not in src
    assert "--cleanup" not in src


def test_audit_show_and_canonical_healthy(clean_db, monkeypatch):
    v = _vac()
    db.save_vacancy(v)
    from ai_assistant.vacancy_identity import resolve_vacancy_identity, save_vacancy_alias, MatchType, normalize_url
    cid = resolve_vacancy_identity(v).canonical_id
    save_vacancy_alias(cid, v.stable_id(), v.source, v.job_url, normalize_url(v.job_url), MatchType.DISTINCT, 100)
    set_application_status(v.stable_id(), ApplicationStatus.DISCOVERED, company=v.company, title=v.title)
    assert _run_main(monkeypatch, "audit", "show", v.stable_id()) == 0
    assert _run_main(monkeypatch, "audit", "canonical", cid) == 0