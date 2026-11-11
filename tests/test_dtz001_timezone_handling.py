"""Tests for DTZ-001: Timezone-aware datetime handling and rate limit boundary accuracy across timezones."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_assistant import config, db
from ai_assistant.hh_submit_policy import evaluate
from tests.test_hh_submit_policy import _create_valid_package_and_review


@pytest.fixture
def tz_test_db(tmp_path):
    orig = config.DB_FILE
    db_path = str(tmp_path / "tz_test.db")
    config.DB_FILE = db_path
    db.init_db()
    yield {"db_file": db_path, "tmp_path": tmp_path}
    config.DB_FILE = orig


def test_hourly_rate_limit_across_timezones(tz_test_db, monkeypatch):
    """Submissions in non-UTC timezones (+07:00 Bangkok, -05:00 New York) must be correctly categorized by UTC hour."""
    now_utc = datetime.now(timezone.utc)
    bangkok_tz = timezone(timedelta(hours=7))
    ny_tz = timezone(timedelta(hours=-5))

    # 1. Submission 90 minutes ago (outside 1-hour window) stored in Bangkok time
    t_old = (now_utc - timedelta(minutes=90)).astimezone(bangkok_tz).isoformat()
    db.save_hh_application_transition({
        "application_id": "app_bgk_old",
        "state": "SUBMITTED",
        "reason": "applied",
        "created_at": t_old,
    })

    # 2. Submission 25 minutes ago (inside 1-hour window) stored in NY time
    t_recent = (now_utc - timedelta(minutes=25)).astimezone(ny_tz).isoformat()
    db.save_hh_application_transition({
        "application_id": "app_ny_recent",
        "state": "SUBMITTED",
        "reason": "applied",
        "created_at": t_recent,
    })

    # 3. Submission 5 minutes ago stored in UTC
    t_utc = (now_utc - timedelta(minutes=5)).isoformat()
    db.save_hh_application_transition({
        "application_id": "app_utc_recent",
        "state": "SUBMITTED",
        "reason": "applied",
        "created_at": t_utc,
    })

    one_hour_ago = (now_utc - timedelta(hours=1)).isoformat()
    count = db.count_submitted_transitions_since(one_hour_ago)
    assert count == 2, f"Expected 2 recent submissions within the last hour, got {count}"


def test_daily_rate_limit_boundary_across_midnight(tz_test_db):
    """Submissions right across UTC day boundaries must not be miscounted due to TZ offset string comparison."""
    now_utc = datetime.now(timezone.utc)
    tokyo_tz = timezone(timedelta(hours=9))

    # 23 hours ago (inside 24h window) in Tokyo time
    t_23h = (now_utc - timedelta(hours=23)).astimezone(tokyo_tz).isoformat()
    db.save_hh_application_transition({
        "application_id": "app_tokyo_23h",
        "state": "SUBMITTED",
        "reason": "applied",
        "created_at": t_23h,
    })

    # 26 hours ago (outside 24h window) in Tokyo time
    t_26h = (now_utc - timedelta(hours=26)).astimezone(tokyo_tz).isoformat()
    db.save_hh_application_transition({
        "application_id": "app_tokyo_26h",
        "state": "SUBMITTED",
        "reason": "applied",
        "created_at": t_26h,
    })

    one_day_ago = (now_utc - timedelta(days=1)).isoformat()
    count = db.count_submitted_transitions_since(one_day_ago)
    assert count == 1, f"Expected exactly 1 submission within 24 hours, got {count}"


def test_naive_timestamp_fallback_support(tz_test_db):
    """Legacy or naive timestamps without timezone info must be safely treated as UTC without crashing."""
    now_utc = datetime.now(timezone.utc)
    t_naive_recent = (now_utc - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    t_naive_old = (now_utc - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.%f")

    db.save_hh_application_transition({
        "application_id": "app_naive_1",
        "state": "SUBMITTED",
        "reason": "applied",
        "created_at": t_naive_recent,
    })
    db.save_hh_application_transition({
        "application_id": "app_naive_2",
        "state": "SUBMITTED",
        "reason": "applied",
        "created_at": t_naive_old,
    })

    one_hour_ago = (now_utc - timedelta(hours=1)).isoformat()
    count = db.count_submitted_transitions_since(one_hour_ago)
    assert count == 1


def test_policy_rate_limit_hourly_exceeded_with_tz(tz_test_db, monkeypatch):
    """When submissions in foreign timezone reach hourly limit, policy must reject with rate_limit_hourly."""
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    monkeypatch.setenv("HH_MAX_SUBMITS_PER_HOUR", "2")

    now_utc = datetime.now(timezone.utc)
    tz_sydney = timezone(timedelta(hours=11))

    # Add 2 submissions in the last hour in Sydney timezone (+11:00)
    for i in range(2):
        t = (now_utc - timedelta(minutes=15 * (i + 1))).astimezone(tz_sydney).isoformat()
        db.save_hh_application_transition({
            "application_id": f"app_syd_{i}",
            "state": "SUBMITTED",
            "reason": "applied",
            "created_at": t,
        })

    sid = "hh:136704137"
    _pkg, _fp, letter = _create_valid_package_and_review(sid)
    app = {
        "application_id": "app_new",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": letter,
        "state": "READY_TO_SUBMIT",
    }
    decision = evaluate(app)
    assert decision.approve is False
    assert "rate_limit_hourly" in decision.checks_failed
    assert any("hourly_limit_exceeded" in r for r in decision.reasons)
