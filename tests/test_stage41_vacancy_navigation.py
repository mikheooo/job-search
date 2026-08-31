"""Stage 41: HH Vacancy Navigation & Pre-Submit Verification Test Suite.

Proves:
1. Active tab = hh.ru/chat -> system safely navigates to target vacancy page.
2. Application contains vacancy URL -> system resolves and opens it correctly.
3. Wrong active tab does not block navigation.
4. Different/wrong vacancy is rejected during target verification (MISMATCH / fails closed).
5. Missing vacancy URL leads to safe BLOCKED / NOT_FOUND state.
6. Missing Submit button after opening vacancy page does not trigger retry and returns safe BLOCKED.
7. REAL HH SUBMIT remains 0 across all Stage 41 tests.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.hh_vacancy_navigator import (
    resolve_hh_vacancy_url,
    extract_hh_numeric_id,
    verify_and_navigate_hh_vacancy,
    VacancyVerificationResult,
)
from ai_assistant.hh_questionnaire import (
    HHQuestionItem,
    HHQuestionnaire,
    HHQuestionStatus,
    extract_hh_questionnaire_from_snapshot,
    submit_questionnaire_response,
)


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage41_nav.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


class MockNavigationBrowser:
    """Mock CDP browser tracking active page, navigations, and submit clicks."""
    def __init__(self, initial_url: str = "https://hh.ru/chat", target_has_ui: bool = True):
        self.current_url = initial_url
        self.current_title = "HH Chat" if "chat" in initial_url else "Senior AI Automation Engineer"
        self.target_has_ui = target_has_ui
        self.navigated_urls: List[str] = []
        self.clicked_buttons: List[str] = []
        self.submit_attempts: int = 0

    def evaluate(self, script: str) -> str:
        # 1. Navigation script
        if "window.location.href =" in script:
            import re
            m = re.search(r'window\.location\.href\s*=\s*["\']([^"\']+)["\']', script)
            if m:
                new_url = m.group(1)
                self.current_url = new_url
                self.navigated_urls.append(new_url)
                self.current_title = "Senior AI Automation Engineer"
                return json.dumps({"ok": True, "navigated_to": new_url})

        # 2. Inspection script
        if "has_submit_btn" in script or "is_vacancy_page" in script:
            is_chat = "chat" in self.current_url.lower() or "messages" in self.current_url.lower()
            is_vac = "vacancy" in self.current_url.lower()
            return json.dumps({
                "url": self.current_url,
                "title": self.current_title,
                "has_submit_btn": self.target_has_ui if is_vac else False,
                "has_apply_btn": self.target_has_ui if is_vac else False,
                "has_response_modal": self.target_has_ui if is_vac else False,
                "is_chat": is_chat,
                "is_vacancy_page": is_vac,
            })

        # 3. Submit click script (execution only)
        elif "el.click()" in script:
            self.submit_attempts += 1
            if self.target_has_ui and "vacancy" in self.current_url:
                self.clicked_buttons.append("submit")
                return json.dumps({"ok": True, "clicked_button": "Откликнуться"})
            return json.dumps({"ok": False, "reason": "Submit button not found"})

        return json.dumps({"ok": True})


# ---------------------------------------------------------------------------
# Test 1: Active tab = hh.ru/chat -> System Navigates to Vacancy Page
# ---------------------------------------------------------------------------

def test_active_tab_chat_navigates_to_target_vacancy(clean_db):
    """When active tab is hh.ru/chat, system detects mismatch, navigates to vacancy URL, and verifies UI."""
    browser = MockNavigationBrowser(initial_url="https://hh.ru/chat", target_has_ui=True)

    app_id = "app_hh_135112049"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "state": "READY_TO_SUBMIT",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    res = verify_and_navigate_hh_vacancy(
        target=app_id,
        evaluate_fn=browser.evaluate,
        navigate_if_needed=True,
    )

    assert res.ok is True
    assert res.status == "READY"
    assert res.navigated is True
    assert "https://hh.ru/vacancy/135112049" in browser.navigated_urls
    assert browser.current_url == "https://hh.ru/vacancy/135112049"
    assert res.url_matched is True
    assert res.submit_or_apply_ui_present is True
    assert len(browser.clicked_buttons) == 0  # Verification only, zero clicks!


# ---------------------------------------------------------------------------
# Test 2: Application / Questionnaire Vacancy URL Resolution
# ---------------------------------------------------------------------------

def test_vacancy_url_resolution(clean_db):
    """Canonical HH vacancy URL is resolved from numeric IDs, stable IDs, applications, and dicts."""
    # From stable ID
    assert resolve_hh_vacancy_url("hh:135112049") == "https://hh.ru/vacancy/135112049"
    # From numeric ID
    assert resolve_hh_vacancy_url("135112049") == "https://hh.ru/vacancy/135112049"
    # From direct URL
    assert resolve_hh_vacancy_url("https://hh.ru/vacancy/135112049?query=python") == "https://hh.ru/vacancy/135112049?query=python"
    # From dict
    assert resolve_hh_vacancy_url({"vacancy_stable_id": "hh:999888"}) == "https://hh.ru/vacancy/999888"


# ---------------------------------------------------------------------------
# Test 3: Wrong Active Tab Does Not Block Navigation
# ---------------------------------------------------------------------------

def test_wrong_active_tab_navigates_smoothly(clean_db):
    """If browser is on search results or random page, navigation smoothly opens the target vacancy."""
    browser = MockNavigationBrowser(initial_url="https://hh.ru/search/vacancy?text=python", target_has_ui=True)

    res = verify_and_navigate_hh_vacancy(
        target="hh:135112049",
        evaluate_fn=browser.evaluate,
        navigate_if_needed=True,
    )

    assert res.ok is True
    assert res.navigated is True
    assert browser.current_url == "https://hh.ru/vacancy/135112049"


# ---------------------------------------------------------------------------
# Test 4: Different Vacancy on Active Tab Fails Closed if Navigation Disabled
# ---------------------------------------------------------------------------

def test_different_vacancy_rejected_if_mismatched(clean_db):
    """If browser is on vacancy 999999 and target is 135112049 without navigation, it is rejected."""
    browser = MockNavigationBrowser(initial_url="https://hh.ru/vacancy/999999", target_has_ui=True)

    res = verify_and_navigate_hh_vacancy(
        target="hh:135112049",
        evaluate_fn=browser.evaluate,
        navigate_if_needed=False,  # Navigation disabled
    )

    assert res.ok is False
    assert res.status == "BLOCKED"
    assert res.url_matched is False


# ---------------------------------------------------------------------------
# Test 5: Missing Vacancy URL Leads to Safe BLOCKED / NOT_FOUND State
# ---------------------------------------------------------------------------

def test_missing_vacancy_url_fails_safely(clean_db):
    """When target vacancy URL cannot be resolved, verification safely fails closed."""
    browser = MockNavigationBrowser(initial_url="https://hh.ru/chat")

    res = verify_and_navigate_hh_vacancy(
        target="non_existent_target_unknown",
        evaluate_fn=browser.evaluate,
    )

    assert res.ok is False
    assert res.status == "NOT_FOUND"
    assert "Could not resolve" in res.reason
    assert len(browser.navigated_urls) == 0


# ---------------------------------------------------------------------------
# Test 6: Missing Submit Button After Opening Page Does Not Trigger Retry
# ---------------------------------------------------------------------------

def test_missing_submit_ui_after_navigation_blocks_without_retry(clean_db):
    """If vacancy page is opened but Submit/Apply UI is absent (e.g. archived vacancy), it safely blocks."""
    browser = MockNavigationBrowser(initial_url="https://hh.ru/chat", target_has_ui=False)

    snapshot = {
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "questions": [
            {"id": "q1", "text": "Локация:", "type": "radio", "required": True, "options": ["Удаленно"]},
        ]
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    answers = {"q1": "Удаленно"}
    db.update_hh_questionnaire_answers(quest.questionnaire_id, answers, new_status=HHQuestionStatus.READY_TO_SUBMIT.value)

    res = submit_questionnaire_response(
        questionnaire_id=quest.questionnaire_id,
        human_answers=answers,
        evaluate_fn=browser.evaluate,
        confirm_submit=True,
    )

    assert res.verdict == "BLOCKED"
    assert res.status == "BLOCKED"
    assert res.submit_count == 0
    assert browser.submit_attempts == 0  # Zero submit attempts!
    assert "Vacancy pre-submit check failed" in res.reason


# ---------------------------------------------------------------------------
# Test 7: Zero Real HH Submit Invariant
# ---------------------------------------------------------------------------

def test_zero_real_submit_invariant(clean_db):
    """In all verification operations, zero submit clicks or network submissions occur."""
    browser = MockNavigationBrowser(initial_url="https://hh.ru/chat", target_has_ui=True)

    res = verify_and_navigate_hh_vacancy(
        target="hh:135112049",
        evaluate_fn=browser.evaluate,
    )
    assert res.ok is True
    assert len(browser.clicked_buttons) == 0
    assert browser.submit_attempts == 0
