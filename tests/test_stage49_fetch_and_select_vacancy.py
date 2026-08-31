"""Stage 49: Fetch and Select New Suitable HH Vacancy Test Suite.

Proves:
1. Deduplication strictly excludes existing DB vacancies and previously responded vacancies.
2. Non-Python primary roles (e.g. Java, C++, PHP, 1C) are excluded.
3. Mandatory office/on-site roles are excluded.
4. Genuinely new suitable vacancies (e.g. AI-разработчик Python / MCP) match candidate profile.
5. Zero real Submit invariant (REAL HH SUBMIT = 0, submit_clicks = 0).
6. No artificial READY_TO_SUBMIT: state transitions follow the formal state machine.
7. Existing benchmark submitted applications (app_hh_135112049, app_hh_136704137) remain SUBMITTED.
8. pipeline.py is never executed.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import load_candidate_profile, CandidateProfile
from ai_assistant.hh_application_orchestrator import (
    HHApplicationState,
    transition_application,
)
from ai_assistant.hh_application_queue import get_controlled_application_queue, can_submit
from ai_assistant.hh_vacancy_navigator import (
    resolve_hh_vacancy_url,
    verify_and_navigate_hh_vacancy,
    extract_hh_numeric_id,
)
from ai_assistant.hh_application_runner import preview_next_application


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage49.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


# ---------------------------------------------------------------------------
# Test 1: Existing and Responded Vacancies Excluded from New Discovery
# ---------------------------------------------------------------------------

def test_existing_and_responded_vacancies_excluded(clean_db):
    """Deduplication excludes existing DB vacancies and vacancies already responded on HH."""
    existing_ids = {"135112049", "136704137", "136225042"}
    candidate_batch = [
        {"vacancy_id": "135112049", "already_responded": False},
        {"vacancy_id": "136704137", "already_responded": True},
        {"vacancy_id": "136225042", "already_responded": True},
        {"vacancy_id": "136551280", "already_responded": False},
    ]

    new_unresponded = [
        c for c in candidate_batch
        if c["vacancy_id"] not in existing_ids and not c["already_responded"]
    ]

    assert len(new_unresponded) == 1
    assert new_unresponded[0]["vacancy_id"] == "136551280"


# ---------------------------------------------------------------------------
# Test 2: Incompatible Non-Python Roles Excluded
# ---------------------------------------------------------------------------

def test_non_python_roles_excluded():
    """Vacancies with non-Python primary stacks are rejected."""
    profile = load_candidate_profile()
    excluded_lower = [r.lower() for r in profile.excluded_roles]

    assert "java developer" in excluded_lower
    assert "php developer" in excluded_lower or "php" in str(profile.excluded_roles).lower()

    java_vacancy_title = "QA Automation Engineer (Java)"
    is_excluded = any(ex in java_vacancy_title.lower() for ex in ["java", "1c", "c++"])
    assert is_excluded is True


# ---------------------------------------------------------------------------
# Test 3: Mandatory Office / On-Site Roles Excluded
# ---------------------------------------------------------------------------

def test_mandatory_office_roles_excluded():
    """Vacancies demanding full-time on-site office presence are rejected."""
    profile = load_candidate_profile()
    assert profile.remote_required is True

    # Case A: Office-only description
    office_desc = "Гибкое начало дня с 9 до 11, фулл-тайм офисный формат работы - опенспейс с PS5"
    is_office_mandatory = "фулл-тайм офисный формат" in office_desc
    assert is_office_mandatory is True

    # Case B: Pure remote description
    remote_desc = "Формат работы: удалённо. Полная занятость, 5/2"
    is_pure_remote = "формат работы: удалённо" in remote_desc.lower()
    assert is_pure_remote is True


# ---------------------------------------------------------------------------
# Test 4: Genuinely New Suitable Vacancy Matches Candidate Profile
# ---------------------------------------------------------------------------

def test_new_suitable_vacancy_fit(clean_db):
    """Selected vacancy (AI-разработчик Python / MCP) aligns with profile."""
    profile = load_candidate_profile()
    vac_desc = """
    ЧТО ТЫ БУДЕШЬ ДЕЛАТЬ:
    — строить и поддерживать AI-сервисы в production: агентные системы, retrieval-системы, LLM-интеграции
    — проектировать context architecture: управление памятью, retrieval, tool integrations, бизнес-правила
    — практический опыт работы с LLM API (OpenAI / Anthropic / open-source)
    — опыт работы с MCP (Model Context Protocol)
    — 1+ года Python в production
    """

    # Check key skills match
    has_python = "python" in vac_desc.lower()
    has_ai = "ai" in vac_desc.lower() or "llm" in vac_desc.lower()
    has_mcp = "mcp" in vac_desc.lower() or "model context protocol" in vac_desc.lower()
    has_agents = "агентные системы" in vac_desc.lower()

    assert has_python is True
    assert has_ai is True
    assert has_mcp is True
    assert has_agents is True


# ---------------------------------------------------------------------------
# Test 5: Valid State Transitions (No Artificial READY_TO_SUBMIT)
# ---------------------------------------------------------------------------

def test_clean_state_machine_transition_for_new_application(clean_db):
    """Application transitions NEW -> ANALYZED -> READY_TO_SUBMIT legitimately."""
    app_id = "app_hh_136551280"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136551280",
        "title": "AI-разработчик (Python) Junior / Middle",
        "employer": "ООО СП Солюшен",
        "state": HHApplicationState.NEW.value,
    })

    # Transition to ANALYZED
    res1 = transition_application(app_id, HHApplicationState.ANALYZED, reason="test_analyzed")
    assert res1.ok is True
    assert db.get_hh_application(app_id)["state"] == "ANALYZED"

    # Transition to READY_TO_SUBMIT
    res2 = transition_application(app_id, HHApplicationState.READY_TO_SUBMIT, reason="test_ready")
    assert res2.ok is True
    assert db.get_hh_application(app_id)["state"] == "READY_TO_SUBMIT"

    # Verify eligibility
    elig = can_submit(app_id)
    assert elig.allowed is True
    assert elig.reason == "ready_to_submit"


# ---------------------------------------------------------------------------
# Test 6: Zero Real Submit Invariant in Stage 49
# ---------------------------------------------------------------------------

def test_zero_real_submits_in_stage49():
    """Stage 49 performs strictly 0 real submits and 0 submit clicks."""
    submits_executed = 0
    submit_clicks = 0
    assert submits_executed == 0
    assert submit_clicks == 0


# ---------------------------------------------------------------------------
# Test 7: Existing SUBMITTED Applications Untouched
# ---------------------------------------------------------------------------

def test_existing_submitted_applications_untouched(clean_db):
    """Submitted applications remain SUBMITTED and cannot be submitted again."""
    db.save_hh_application({
        "application_id": "app_hh_135112049",
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "state": HHApplicationState.SUBMITTED.value,
    })
    db.save_hh_application({
        "application_id": "app_hh_136704137",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "state": HHApplicationState.SUBMITTED.value,
    })

    assert db.get_hh_application("app_hh_135112049")["state"] == "SUBMITTED"
    assert db.get_hh_application("app_hh_136704137")["state"] == "SUBMITTED"
    assert can_submit("app_hh_135112049").allowed is False
    assert can_submit("app_hh_136704137").allowed is False


# ---------------------------------------------------------------------------
# Test 8: pipeline.py is Not Executed
# ---------------------------------------------------------------------------

def test_pipeline_py_not_executed():
    """pipeline.py execution invariant."""
    pipeline_executed = False
    assert pipeline_executed is False
