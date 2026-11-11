"""Tests for suppression of unconfirmed HH alerts and resume visibility blocker."""

from unittest.mock import MagicMock
import pytest

from ai_assistant import hh_autopilot as ha
from ai_assistant import hh_cdp_submit as hcs


def test_unconfirmed_submission_does_not_send_telegram_alert(monkeypatch, tmp_path, submit_allowed):
    """When submission post_confirmed is False, no alert should be sent to Telegram."""
    mock_send = MagicMock()
    monkeypatch.setattr(ha, "send_telegram_alert", mock_send)
    monkeypatch.setattr(ha, "PROJECT_ROOT", tmp_path)

    import sys
    import types
    mock_hs = types.ModuleType("ai_assistant.hh_submission")
    mock_hs.submission_halt_reason = lambda: None
    monkeypatch.setitem(sys.modules, "ai_assistant.hh_submission", mock_hs)
    monkeypatch.setattr(ha, "_prepare_standing_auth_review", lambda *a, **k: None)
    monkeypatch.setattr(ha, "_persist_vacancy_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(ha, "brief_vacancy", lambda *a, **k: None)

    # Mock submit module to return clicked=1, but post_confirmed=False
    mock_t722 = MagicMock()
    mock_t722.run_submit.return_value = {
        "verdict": "CLICKED",
        "clicked": 1,
        "post_confirmed": False,
        "post_reason": "page does not show response confirmation",
    }
    monkeypatch.setattr(ha, "_get_t722_send_module", lambda: mock_t722)
    monkeypatch.setattr(ha, "_post_submit_confirmed", lambda d: (False, "page does not show confirmation"))

    # Mock DB
    mock_conn = MagicMock()
    monkeypatch.setattr(ha.db, "get_connection", lambda: mock_conn)

    outcome = {}
    res = ha.submit_vacancy(
        vid="888001",
        role="AI Engineer",
        company="Test Co",
        description="Job description",
        salary="100 000 руб.",
        resume_title="Ai engineer",
        outcome=outcome,
    )

    assert res is True
    assert outcome["clicked"] is True
    assert outcome["confirmed"] is False
    assert outcome["notified"] is False
    assert outcome["status"] == "SUBMITTED_UNVERIFIED"
    assert mock_send.call_count == 0


def test_blockers_catches_resume_visibility_warning():
    """Form with warning_shown must be caught by _blockers."""
    vacancy = {
        "applicant": {
            "responseAllowed": True,
            "responseImpossible": False,
            "alreadyApplied": False,
            "negotiations_total": 0,
            "usedResumeIds": [],
        }
    }
    form = {
        "warning_shown": True,
        "warning_text": "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме на «Видно всем работодателям, зарегистрированным на hh.ru»",
        "submit": {"disabled": False},
        "resume_title": "Ai engineer",
        "textareas": [{"visible": True, "value_len": 50}],
    }

    blockers = hcs._blockers(vacancy, form, "Ai engineer")
    assert any("resume-visibility warning is displayed" in b for b in blockers)
