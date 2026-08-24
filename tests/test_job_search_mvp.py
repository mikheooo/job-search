from __future__ import annotations

import sqlite3
from datetime import datetime

import tempfile
import shutil

from ai_assistant.schema import Vacancy
from ai_assistant.normalizer import normalize_description, normalize_employment_type, normalize_vacancy, normalize_salary_text
from ai_assistant.matcher import JobMatcher, JobProfile
from ai_assistant import db
import ai_assistant.config as config


def test_normalize_description_strips_html():
    raw = "<p>Hello&nbsp;world</p>  "
    assert normalize_description(raw) == "Hello world"


def test_normalize_employment_type_maps_variants():
    assert normalize_employment_type("Full-Time") == "Full Time"
    assert normalize_employment_type("contract") == "Contract"


def test_normalize_salary_text_parses_currency_and_numbers():
    result = normalize_salary_text("$1,000 - $2,500 USD/month")
    assert result["salary_currency"] == "USD"
    assert result["salary_min"] == 1000
    assert result["salary_max"] == 2500


def test_normalize_vacancy_preserves_source_fields():
    item = {
        "source": "weworkremotely",
        "source_job_id": "abc",
        "title": "Python Dev",
        "company": "Acme",
        "description": "<p>Write python code.</p>",
        "job_url": "https://example.com/x",
        "country_restrictions": ["US"],
        "timezone_restrictions": ["-5"],
        "salary_min": 1000,
        "salary_max": 2000,
        "salary_currency": "USD",
        "employment_type": "Full-Time",
        "published_at": "2026-01-01T00:00:00",
    }
    vacancy = normalize_vacancy(item)
    assert vacancy.source == "weworkremotely"
    assert vacancy.title == "Python Dev"
    assert vacancy.salary_min == 1000
    assert vacancy.employment_type == "Full Time"


def test_matcher_skips_excluded_company():
    profile = JobProfile(desired_roles=["python"], excluded_companies=["acme"])
    vacancy = Vacancy(
        source="x",
        source_job_id="1",
        title="Python Dev",
        company="Acme",
        description="python",
        job_url="https://example.com/1",
    )
    result = JobMatcher(profile).match(vacancy)
    assert result.decision == "SKIP"
    assert result.score == 0


def test_matcher_skips_excluded_country():
    profile = JobProfile(desired_roles=["python"], excluded_countries=["china"])
    vacancy = Vacancy(
        source="x",
        source_job_id="1",
        title="Python Dev",
        company="Acme",
        description="python",
        job_url="https://example.com/1",
        country_restrictions=["China"],
    )
    result = JobMatcher(profile).match(vacancy)
    assert result.decision == "SKIP"


def test_matcher_prefers_strong_match():
    profile = JobProfile(
        desired_roles=["python"],
        skills=["python", "automation"],
        salary_min=50,
        salary_max=200,
        salary_currency="USD",
        employment_types=["full time"],
    )
    vacancy = Vacancy(
        source="x",
        source_job_id="1",
        title="Python Automation Engineer",
        company="Acme",
        description="python automation job",
        job_url="https://example.com/1",
        salary_min=80,
        salary_max=120,
        salary_currency="USD",
        employment_type="Full Time",
    )
    result = JobMatcher(profile).match(vacancy)
    assert result.decision == "APPLY"
    assert result.score >= 75


def test_db_save_and_dedup():
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = str(tmp_dir + "/state.db")
        orig_db = config.DB_FILE
        config.DB_FILE = db_file
        db.init_db()
        vacancy = Vacancy(
            source="x",
            source_job_id="1",
            title="Python Dev",
            company="Acme",
            description="python",
            job_url="https://example.com/1",
        )
        db.save_vacancy(vacancy)
        db.save_vacancy(vacancy)
        rows = db.list_vacancies(limit=10)
        assert len(rows) == 1
    finally:
        config.DB_FILE = orig_db
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_cli_collect_is_idempotent():
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = str(tmp_dir + "/state.db")
        config.DB_FILE = db_file
        from ai_assistant import cli
        new_first = cli.collect(["himalayas"])
        new_second = cli.collect(["himalayas"])
        assert new_first >= 0
        assert new_second == 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
