import sqlite3
import pytest
from ai_assistant import hh_autopilot as ha
from ai_assistant import db

def test_prior_submission_exists_by_company_and_role(monkeypatch, tmp_path):
    test_db = tmp_path / "test_state.db"
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE application_submissions (
            submission_id TEXT PRIMARY KEY,
            vacancy_stable_id TEXT,
            status TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE application_tracking (
            vacancy_stable_id TEXT PRIMARY KEY,
            status TEXT,
            company TEXT,
            title TEXT
        )
    """)
    # Insert existing submission for 'Справочная 0-81'
    cur.execute(
        "INSERT INTO application_tracking (vacancy_stable_id, status, company, title) VALUES (?, ?, ?, ?)",
        ("hh:139074093", "SUBMITTED", "Справочная 0-81", "Специалист по IT / Системный администратор")
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "get_connection", lambda: sqlite3.connect(test_db))

    # Same vacancy ID should be blocked
    prior1 = ha._prior_submission_exists("139074093", company="Справочная 0-81", role="Специалист по IT / Системный администратор")
    assert prior1 is not None
    assert "tracking already at SUBMITTED" in prior1

    # New vacancy ID, but SAME company and role should also be blocked!
    prior2 = ha._prior_submission_exists("139074094", company="Справочная 0-81", role="Специалист по IT / Системный администратор")
    assert prior2 is not None
    assert "already applied to company 'Справочная 0-81'" in prior2

    # Different company should be allowed
    prior3 = ha._prior_submission_exists("139088888", company="Новая Компания", role="Специалист по IT / Системный администратор")
    assert prior3 is None


def test_in_cycle_company_deduplication():
    from ai_assistant.vacancy_identity import normalize_company, normalize_title

    discovered_mock = [
        {"vacancy_id": "139074093", "title": "Специалист по IT / Системный администратор", "card": {"employer": "Справочная 0-81", "title": "Специалист по IT / Системный администратор"}},
        {"vacancy_id": "139074094", "title": "Специалист по IT / Системный администратор", "card": {"employer": "Справочная 0-81", "title": "Специалист по IT / Системный администратор"}},
        {"vacancy_id": "139074095", "title": "Специалист по IT / Системный администратор", "card": {"employer": "Справочная 0-81", "title": "Специалист по IT / Системный администратор"}},
        {"vacancy_id": "139099999", "title": "DevOps Engineer", "card": {"employer": "Яндекс", "title": "DevOps Engineer"}},
    ]

    submitted_companies = set()
    submitted_company_roles = set()
    accepted = []

    for entry in discovered_mock:
        company = entry["card"]["employer"]
        role = entry["card"]["title"]
        norm_c = normalize_company(company)
        norm_r = normalize_title(role)

        if norm_c and norm_c not in ("компания", "company") and len(norm_c) >= 3:
            if norm_c in submitted_companies or (norm_c, norm_r) in submitted_company_roles:
                continue

        # simulate submit
        accepted.append(entry["vacancy_id"])
        submitted_companies.add(norm_c)
        submitted_company_roles.add((norm_c, norm_r))

    assert accepted == ["139074093", "139099999"]
