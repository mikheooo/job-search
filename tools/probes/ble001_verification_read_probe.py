"""Проба №55: что читается из строки верификации, которую модель не разбирает.

Вопрос не «глотается ли исключение» (оно логируется, это класс A), а «что
сообщают вызывающему». Гипотеза: битая строка неотличима от отсутствующей,
и recovery печатает «no verification performed» про строку, в которой
VERIFIED записан.

Проба пишет строки в submission_verifications НАПРЯМУЮ через SQL — так,
как их мог бы записать другой версией кода. save_verification сюда не
годится: он сериализует модель, а модель всегда сериализуется в то, что
сама же и прочитает. Дрейф схемы приходит из прошлого, не из текущего кода.

Запуск:
    .venv/Scripts/python.exe tools/ble001_verification_read_probe.py

Ничего не пишет в рабочие файлы: config.DB_FILE подменяется на временный.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ai_assistant import config

TMP = tempfile.mkdtemp(prefix="ble001-verif-read-")
config.DB_FILE = os.path.join(TMP, "state.db")

from ai_assistant import db
from ai_assistant import submission_verifier as sv
from ai_assistant.application_tracking import (
    ApplicationStatus,
    set_application_status,
    transition_application,
)
from ai_assistant.schema import Vacancy
from ai_assistant.submission_recovery import inspect_submission_state
from ai_assistant.submission_verifier import VERIFICATION_VERSION


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_payload(status: str = "VERIFIED") -> dict:
    return {
        "vacancy_stable_id": "test:v1",
        "submission_id": "sub_55",
        "verification_status": status,
        "evidence": {"body_marker": True},
        "final_url": "https://example.com/done",
        "page_title": "Success",
        "verified_at": _now(),
        "warnings": [],
        "verification_version": VERIFICATION_VERSION,
    }


def _shapes() -> list[tuple[str, str, str | None]]:
    """(id, описание, то, что реально лежит в колонке verification_json).

    None означает SQL NULL.
    """
    valid = json.dumps(_valid_payload())
    return [
        ("S1-valid", "нормальная строка текущей схемы", valid),
        (
            "S2-extra-key",
            "строка со ЛИШНИМ полем (поле убрали из модели)",
            json.dumps({**_valid_payload(), "screenshot_sha256": "deadbeef"}),
        ),
        (
            "S3-dropped-required",
            "строка без обязательного поля (поле добавили в модель)",
            json.dumps({k: v for k, v in _valid_payload().items() if k != "verification_status"}),
        ),
        ("S4-empty-object", "пустой объект", "{}"),
        ("S5-null", "SQL NULL в колонке", None),
        ("S6-empty-string", "пустая строка", ""),
        ("S7-broken-json", "обрезанный JSON", '{"verification_status": "VERIFIED"'),
        (
            "S8-unknown-enum",
            "статус вне enum (значение из будущей версии)",
            json.dumps(_valid_payload(status="ACCEPTED")),
        ),
    ]


def _setup_vacancy_and_submission() -> str:
    db.init_db()
    vac = Vacancy(
        source="test",
        source_job_id="55",
        title="Senior AI Automation Engineer (n8n / Python)",
        company="TestCo",
        description="python n8n automation LLM API AWS remote senior",
        job_url="https://example.com/55",
        location="Remote",
        country_restrictions=[],
        timezone_restrictions=[],
        salary_min=5000,
        salary_max=5000,
        salary_currency="USD",
        employment_type="Full Time",
    )
    db.save_vacancy(vac)
    sid = vac.stable_id()
    set_application_status(
        sid,
        ApplicationStatus.DISCOVERED,
        company="TestCo",
        title="Senior AI Automation Engineer (n8n / Python)",
        source="test",
        vacancy_url="https://example.com/55",
    )
    transition_application(sid, ApplicationStatus.ANALYZED)
    transition_application(sid, ApplicationStatus.READY_TO_APPLY)
    transition_application(sid, ApplicationStatus.SUBMITTED)
    db.save_submission(
        sid,
        json.dumps({"status": "SUBMITTED", "url_after": "https://example.com/55"}),
        status="SUBMITTED",
        submission_id="sub_55",
    )
    return sid


def _write_row(sid: str, raw: str | None) -> None:
    conn = sqlite3.connect(config.DB_FILE)
    conn.execute("DELETE FROM submission_verifications")
    conn.execute(
        """
        INSERT INTO submission_verifications
            (vacancy_stable_id, submission_id, verification_version,
             verification_status, verification_json, verified_at,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            "sub_55",
            VERIFICATION_VERSION,
            "VERIFIED",
            raw,
            _now(),
            _now(),
            _now(),
        ),
    )
    conn.commit()
    conn.close()


def _stored_status(raw: str | None) -> str:
    """Что строка утверждает сама о себе — до всякого чтения моделью.

    The except is narrowed for the same reason the fix's helper narrows its own:
    "this text is not JSON" is a ValueError, and anything else is a bug in this
    probe that should be loud instead of being filed as an unreadable row.
    """
    if raw is None:
        return "<NULL>"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return "<unparseable JSON>"
    if not isinstance(data, dict):
        return f"<{type(data).__name__}>"
    return str(data.get("verification_status", "<no status key>"))


def integrity_section(sid: str) -> None:
    """Асимметрия: аудит поломку видит, recovery-отчёт — нет.

    _check_verification_json_records читает колонку по SQL, независимо от
    модельного геттера, и на неразбираемой строке поднимает
    INVALID_VERIFICATION_JSON. Смотрим, что на ТОЙ ЖЕ строке говорит
    inspect_submission_state.
    """
    from ai_assistant.application_integrity import run_integrity_audit

    print()
    print("=" * 78)
    print("АСИММЕТРИЯ: аудит против recovery-отчёта на одной и той же строке")
    print("=" * 78)
    print(f"{'форма':<18} {'аудит по этой вакансии':<46} recovery reason")
    print("-" * 118)
    for shape_id, _desc, raw in _shapes():
        _write_row(sid, raw)
        report = run_integrity_audit(scope="full")
        codes = sorted(
            i.code for i in report.issues if i.vacancy_stable_id == sid
        )
        rec = inspect_submission_state(sid)
        print(
            f"{shape_id:<18} {(', '.join(codes) or '<тихо>'):<46} {rec.reason}"
        )


def main() -> int:
    print("=" * 78)
    print("Проба №55: строка верификации, неразбираемая моделью")
    print("=" * 78)
    sid = _setup_vacancy_and_submission()
    print(f"вакансия: {sid}   submission_id: sub_55   tracking: SUBMITTED")
    print(f"БД: {config.DB_FILE}")
    print()

    header = (
        f"{'форма':<18} {'в строке лежит':<18} "
        f"{'db.get':<8} {'sv.get':<8} {'db.is_verified':<15} "
        f"{'recovery_status':<20} reason"
    )
    print(header)
    print("-" * len(header))

    rows = []
    for shape_id, _desc, raw in _shapes():
        _write_row(sid, raw)

        live = db.get_verification(sid, "sub_55")
        dead = sv.get_verification(sid, "sub_55")
        is_ver = db.is_verified(sid, "sub_55")
        rec = inspect_submission_state(sid)

        live_s = "None" if live is None else live.verification_status.value
        dead_s = "None" if dead is None else dead.verification_status.value
        print(
            f"{shape_id:<18} {_stored_status(raw):<18} "
            f"{live_s:<8} {dead_s:<8} {is_ver!s:<15} "
            f"{rec.recovery_status.value:<20} {rec.reason}"
        )
        rows.append(
            {
                "id": shape_id,
                "stored": _stored_status(raw),
                "live": live,
                "dead": dead,
                "is_verified": is_ver,
                "recovery_status": rec.recovery_status.value,
                "reason": rec.reason,
                "recommended_action": rec.recommended_action,
                "warnings": list(rec.warnings),
                "last_verification": rec.last_verification,
            }
        )

    print()
    print("=" * 78)
    print("ЧТО ЭТО ЗНАЧИТ")
    print("=" * 78)

    # Направление отказа: ложное «нет записи» vs ложное «всё хорошо».
    false_negative = [r for r in rows if r["stored"] == "VERIFIED" and r["live"] is None]
    false_positive = [
        r for r in rows if r["stored"] not in ("VERIFIED",) and r["live"] is not None
    ]
    print(f"строк, где записано VERIFIED, но читатель вернул None: {len(false_negative)}")
    for r in false_negative:
        print(f"   - {r['id']}: recovery={r['recovery_status']} | {r['reason']}")
        print(f"     recommended_action: {r['recommended_action']}")
        for w in r["warnings"]:
            print(f"     warning: {w}")
    print()
    print(f"строк, где записано НЕ VERIFIED, но читатель вернул объект: {len(false_positive)}")
    for r in false_positive:
        print(f"   - {r['id']}: читатель вернул {r['live'].verification_status.value}")

    print()
    print("Дверь в reconcile (единственное место, где запись двигает статус):")
    for r in rows:
        acts = (
            r["recovery_status"] == "NO_ACTION" and r["last_verification"] is not None
        )
        print(
            f"   {r['id']:<18} last_verification={'есть' if r['last_verification'] else 'нет':<4} "
            f"-> reconcile {'СРАБОТАЕТ' if acts else 'пропустит'}"
        )

    print()
    print(f"временно: {TMP}")

    integrity_section(sid)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
