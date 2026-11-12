"""
Tests for OP-MET-001: Weekly Operational Metrics and Telegram Digest.
1. Rejection reason classification.
2. Metric computation (outbound submissions, responses, interviews, conversions, top rejections).
3. Digest formatting.
4. Delivery with outbound governor, idempotency, and dry-run protection.
5. CLI command integration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_assistant import config, db
from ai_assistant.weekly_metrics import (
    classify_rejection_reason,
    compute_weekly_metrics,
    format_weekly_metrics_digest,
    send_weekly_metrics_digest,
)


def test_classify_rejection_reason() -> None:
    assert classify_rejection_reason("Мы уже закрыли позицию, спасибо.") == "Вакансия закрыта / на паузе"
    assert classify_rejection_reason("Поиск приостановлен по решению руководства.") == "Вакансия закрыта / на паузе"
    assert classify_rejection_reason("Решили продолжить с другими кандидатами.") == "Выбран другой кандидат"
    assert classify_rejection_reason("К сожалению, недостаточно опыта с Kubernetes.") == "Несоответствие стека / опыта"
    assert classify_rejection_reason("Мы ищем только в офис в Москве.") == "Локация / формат работы / гражданство"
    assert classify_rejection_reason("К сожалению, сейчас мы не готовы пригласить вас.") == "Стандартный вежливый отказ"
    assert classify_rejection_reason("К сожалению, отказ.") == "Стандартный вежливый отказ"
    assert classify_rejection_reason(None) == "Без текста / системный отказ"
    assert classify_rejection_reason("") == "Без текста / системный отказ"
    assert classify_rejection_reason("Спасибо за уделенное время.") == "Прочее / без детализации"


def test_compute_weekly_metrics_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_db = tmp_path / "test_state.db"
    monkeypatch.setattr(config, "DB_FILE", str(test_db))

    db.init_db()
    ref_time = datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone.utc)
    t_3d = (ref_time - timedelta(days=3)).isoformat()
    t_5d = (ref_time - timedelta(days=5)).isoformat()
    t_10d = (ref_time - timedelta(days=10)).isoformat()

    conn = db.get_connection()
    try:
        # Submissions within 7 days
        conn.execute(
            """
            INSERT INTO application_submissions (vacancy_stable_id, submission_id, executor_version, status, submitted_at, created_at, updated_at)
            VALUES ('hh:101', 'sub-101', 'v1', 'SUBMITTED', ?, ?, ?)
            """,
            (t_3d, t_3d, t_3d),
        )
        conn.execute(
            """
            INSERT INTO application_submissions (vacancy_stable_id, submission_id, executor_version, status, submitted_at, created_at, updated_at)
            VALUES ('hh:102', 'sub-102', 'v1', 'SUBMITTED', ?, ?, ?)
            """,
            (t_5d, t_5d, t_5d),
        )
        # Submissions older than 7 days
        conn.execute(
            """
            INSERT INTO application_submissions (vacancy_stable_id, submission_id, executor_version, status, submitted_at, created_at, updated_at)
            VALUES ('hh:999', 'sub-999', 'v1', 'SUBMITTED', ?, ?, ?)
            """,
            (t_10d, t_10d, t_10d),
        )
        # Another source via tracking
        conn.execute(
            """
            INSERT INTO application_tracking (vacancy_stable_id, status, source, created_at, updated_at, applied_at)
            VALUES ('habrcareer:201', 'SUBMITTED', 'habrcareer', ?, ?, ?)
            """,
            (t_3d, t_3d, t_3d),
        )

        # Employer responses within 7 days
        conn.execute(
            """
            INSERT INTO hh_message_events (message_fingerprint, conversation_id, sender, text, seen_at, classification)
            VALUES ('fp1', 'conv1', 'employer', 'К сожалению, вакансия закрыта', ?, 'REJECTION')
            """,
            (t_3d,),
        )
        conn.execute(
            """
            INSERT INTO hh_message_events (message_fingerprint, conversation_id, sender, text, seen_at, classification)
            VALUES ('fp2', 'conv2', 'employer', 'Здравствуйте! Подскажите ваши зарплатные ожидания', ?, 'NEEDS_REPLY')
            """,
            (t_5d,),
        )
        # Employer rejection older than 7 days
        conn.execute(
            """
            INSERT INTO hh_message_events (message_fingerprint, conversation_id, sender, text, seen_at, classification)
            VALUES ('fp3', 'conv3', 'employer', 'Старый отказ', ?, 'REJECTION')
            """,
            (t_10d,),
        )

        # Interview event within 7 days
        conn.execute(
            """
            INSERT INTO interview_events (id, conversation_id, vacancy_stable_id, company, vacancy_title, invitation_text, status, detected_at, notified)
            VALUES (1, 'conv4', 'hh:101', 'Test Company', 'Senior Python Engineer', 'Приглашаем на интервью', 'INVITED', ?, 1)
            """,
            (t_3d,),
        )
        conn.commit()
    finally:
        conn.close()

    metrics = compute_weekly_metrics(days=7, ref_date=ref_time)

    assert metrics["period_days"] == 7
    assert metrics["window_start"] == "2026-11-05"
    assert metrics["window_end"] == "2026-11-12"

    apps = metrics["applications_submitted"]
    assert apps["total"] == 3  # hh:101, hh:102, habrcareer:201
    assert apps["by_source"]["hh"] == 2
    assert apps["by_source"]["habrcareer"] == 1

    resp = metrics["employer_responses"]
    assert resp["total"] == 2
    assert resp["by_classification"]["REJECTION"] == 1
    assert resp["by_classification"]["NEEDS_REPLY"] == 1

    interviews = metrics["interviews_scheduled"]
    assert interviews["total"] == 1

    conv = metrics["conversion"]
    assert conv["submit_to_response_pct"] == round(2 / 3 * 100, 1)
    assert conv["response_to_interview_pct"] == round(1 / 2 * 100, 1)
    assert conv["overall_conversion_pct"] == round(1 / 3 * 100, 1)

    rej = metrics["rejections"]
    assert rej["total_in_period"] == 1
    assert rej["top_reasons"][0]["reason"] == "Вакансия закрыта / на паузе"
    assert rej["top_reasons"][0]["count"] == 1


def test_format_weekly_metrics_digest() -> None:
    sample_metrics = {
        "period_days": 7,
        "window_start": "2026-11-05",
        "window_end": "2026-11-12",
        "applications_submitted": {
            "total": 10,
            "by_source": {"hh": 8, "habrcareer": 2},
        },
        "employer_responses": {
            "total": 5,
        },
        "interviews_scheduled": {
            "total": 2,
        },
        "conversion": {
            "submit_to_response_pct": 50.0,
            "response_to_interview_pct": 40.0,
            "overall_conversion_pct": 20.0,
        },
        "rejections": {
            "total_in_period": 3,
            "top_reasons": [
                {"reason": "Стандартный вежливый отказ", "count": 2, "percent": 66.7},
                {"reason": "Вакансия закрыта / на паузе", "count": 1, "percent": 33.3},
            ],
        },
    }

    text = format_weekly_metrics_digest(sample_metrics)
    assert "📊 *Еженедельный дайджест поиска работы*" in text
    assert "2026-11-05 — 2026-11-12" in text
    assert "Всего отправлено: *10*" in text
    assert "HeadHunter (hh.ru): *8*" in text
    assert "Хабр Карьера: *2*" in text
    assert "Ответов работодателей: *5*" in text
    assert "Назначено интервью: *2*" in text
    assert "Отклик → Ответ: *50.0%*" in text
    assert "Ответ → Интервью: *40.0%*" in text
    assert "Общая конверсия (отклик → интервью): *20.0%*" in text
    assert "1. Стандартный вежливый отказ: *2* (66.7%)" in text


def test_send_weekly_metrics_digest_outbound_governor_block(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_assistant import outbound_governor

    monkeypatch.setattr(
        outbound_governor,
        "check_outbound_allowed",
        lambda action: (False, "Emergency kill switch active"),
    )

    res = send_weekly_metrics_digest(dry_run=True)
    assert res["delivered"] is False
    assert "Blocked by governor" in res["reason"]


def test_send_weekly_metrics_digest_idempotency_and_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_db = tmp_path / "test_state.db"
    monkeypatch.setattr(config, "DB_FILE", str(test_db))
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:FAKE_TOKEN")
    monkeypatch.setattr(config, "TELEGRAM_CHANNEL_ID", "-100123456789")

    db.init_db()

    dummy_metrics = {
        "period_days": 7,
        "window_start": "2026-11-05",
        "window_end": "2026-11-12",
        "applications_submitted": {"total": 5, "by_source": {"hh": 5}},
        "employer_responses": {"total": 2},
        "interviews_scheduled": {"total": 0},
        "conversion": {
            "submit_to_response_pct": 40.0,
            "response_to_interview_pct": 0.0,
            "overall_conversion_pct": 0.0,
        },
        "rejections": {"total_in_period": 0, "top_reasons": []},
    }

    # 1. Dry run
    res_dry = send_weekly_metrics_digest(dry_run=True, metrics=dummy_metrics)
    assert res_dry["delivered"] is True
    assert res_dry["dry_run"] is True

    # 2. Simulated actual send with mock
    with patch("ai_assistant.weekly_metrics.send_job_message_result") as mock_send:
        mock_res = MagicMock()
        mock_res.ok = True
        mock_res.error = ""
        mock_send.return_value = mock_res

        res_send = send_weekly_metrics_digest(dry_run=False, metrics=dummy_metrics)
        assert res_send["delivered"] is True
        mock_send.assert_called_once()

    # 3. Idempotent attempt without force: should skip
    res_repeat = send_weekly_metrics_digest(dry_run=False, force=False, metrics=dummy_metrics)
    assert res_repeat["delivered"] is False
    assert "Already delivered" in res_repeat["reason"]

    # 4. Attempt with force=True: should resend
    with patch("ai_assistant.weekly_metrics.send_job_message_result") as mock_send_force:
        mock_res_force = MagicMock()
        mock_res_force.ok = True
        mock_res_force.error = ""
        mock_send_force.return_value = mock_res_force

        res_forced = send_weekly_metrics_digest(dry_run=False, force=True, metrics=dummy_metrics)
        assert res_forced["delivered"] is True
        mock_send_force.assert_called_once()


def test_cli_metrics_week_commands() -> None:
    from ai_assistant.cli import metrics_week_cmd

    # Test local print run
    code_local = metrics_week_cmd(days=7, as_json=False)
    assert code_local == 0

    # Test JSON print run
    code_json = metrics_week_cmd(days=7, as_json=True)
    assert code_json == 0

    # Test dry run send
    code_dry = metrics_week_cmd(days=7, send=True, dry_run=True)
    assert code_dry == 0


def test_cli_metrics_day_commands() -> None:
    from ai_assistant.cli import metrics_day_cmd

    code_local = metrics_day_cmd(as_json=False)
    assert code_local == 0

    code_json = metrics_day_cmd(as_json=True)
    assert code_json == 0
