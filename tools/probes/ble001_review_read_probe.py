"""Проба №56: что читается из строки ревью, которую модель не разбирает.

Третья сестра той же семьи, что №33 (очередь) и №55 (верификации).
Аудитор читает `application_reviews` по SQL — но только колонку
`vacancy_stable_id`, для проверки сирот (`application_integrity.py:387`).
Колонку `review_json` он не читает нигде, поэтому битая строка ревью для него
невидима. У верификаций такая проверка есть (`INVALID_VERIFICATION_JSON`,
`:288`), у очереди её добавил №33, у ревью — нет.

Вопрос пробы: что видит каждый потребитель, когда строка ревью есть, но модель
её не разбирает.

Запуск:
    .venv/Scripts/python.exe tools/ble001_review_read_probe.py

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

TMP = tempfile.mkdtemp(prefix="ble001-review-read-")
config.DB_FILE = os.path.join(TMP, "state.db")

from ai_assistant import db
from ai_assistant.application_review import (
    REVIEW_VERSION,
    get_application_review,
    is_review_created,
)
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.hh_submission import HHSubmissionGates
from ai_assistant.schema import Vacancy
from ai_assistant.submission_recovery import inspect_submission_state

SID = "hh:56"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_payload(status: str = "APPROVED") -> dict:
    return {
        "vacancy_stable_id": SID,
        "company": "TestCo",
        "title": "Старший разработчик Python",
        "source": "hh",
        "vacancy_url": "https://hh.ru/vacancy/56",
        "status": status,
        "cover_letter": "Здравствуйте! Готов обсудить.",
        "form_fingerprint": "a" * 64,
        "review_id": "rev_56",
        "created_at": _now(),
        "updated_at": _now(),
        "review_version": REVIEW_VERSION,
    }


def _verify_valid_payload() -> dict:
    """Строка, валидная для СОСЕДНЕЙ модели. Нужна, чтобы асимметрия не была
    артефактом: ревьюшный payload в таблице верификаций невалиден по своей
    природе, и тогда «проверка сработала» ничего не доказывает."""
    from ai_assistant.submission_verifier import VERIFICATION_VERSION

    return {
        "vacancy_stable_id": SID,
        "submission_id": "sub_56",
        "verification_status": "VERIFIED",
        "evidence": {"body_marker": True},
        "final_url": "https://hh.ru/applicant/negotiations",
        "page_title": "Отклик отправлен",
        "verified_at": _now(),
        "warnings": [],
        "verification_version": VERIFICATION_VERSION,
    }


def _shapes() -> list[tuple[str, str, str | None]]:
    """(id, что это за форма, что реально лежит в колонке review_json)."""
    valid = _valid_payload()
    return [
        ("S1-valid", "нормальная строка текущей схемы", json.dumps(valid)),
        (
            "S2-extra-key",
            "лишнее поле (у ApplicationReview нет extra: forbid)",
            json.dumps({**valid, "screenshot_sha256": "deadbeef"}),
        ),
        (
            "S3-unknown-status",
            "статус вне enum - новый член ReviewStatus",
            json.dumps(_valid_payload(status="EXPIRED")),
        ),
        (
            "S4-dropped-required",
            "нет обязательного поля vacancy_stable_id",
            json.dumps({k: v for k, v in valid.items() if k != "vacancy_stable_id"}),
        ),
        ("S5-empty-object", "пустой объект", "{}"),
        ("S6-null", "SQL NULL в колонке", None),
        ("S7-empty-string", "пустая строка", ""),
        ("S8-broken-json", "обрезанный JSON", '{"status": "APPROVED"'),
        (
            "S9-wrong-type",
            "match_score не приводится к числу",
            json.dumps({**valid, "match_score": "not-a-number"}),
        ),
    ]


def _setup() -> str:
    db.init_db()
    db.save_vacancy(Vacancy(
        source="hh",
        source_job_id="56",
        title="Старший разработчик Python",
        company="TestCo",
        description="Python, PostgreSQL.",
        job_url="https://hh.ru/vacancy/56",
        location="Remote",
    ))
    set_application_status(
        SID,
        ApplicationStatus.READY_TO_APPLY,
        company="TestCo",
        title="Старший разработчик Python",
        source="hh",
        vacancy_url="https://hh.ru/vacancy/56",
    )
    return SID


def _write_review(raw: str | None) -> None:
    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM application_reviews")
        conn.execute(
            """
            INSERT INTO application_reviews
                (vacancy_stable_id, review_json, status, note,
                 created_at, updated_at, review_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (SID, raw, "APPROVED", None, _now(), _now(), REVIEW_VERSION),
        )
        conn.commit()
    finally:
        conn.close()


def _write_verification(raw: str | None) -> None:
    """Аналог в соседней таблице - чтобы асимметрия была видна на одном экране."""
    from ai_assistant.submission_verifier import VERIFICATION_VERSION

    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM submission_verifications")
        conn.execute(
            """
            INSERT INTO submission_verifications
                (vacancy_stable_id, submission_id, verification_version,
                 verification_status, verification_json, verified_at,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (SID, "sub_56", VERIFICATION_VERSION, "VERIFIED", raw,
             _now(), _now(), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_reviews() -> None:
    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM application_reviews")
        conn.commit()
    finally:
        conn.close()


def _clear_verifications() -> None:
    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM submission_verifications")
        conn.commit()
    finally:
        conn.close()


def _audit_codes() -> list[str]:
    """Коды аудита по этой вакансии. Таблицы надо расчищать самому вызывающему:
    аудит не различает, из какой таблицы пришла находка."""
    from ai_assistant.application_integrity import run_integrity_audit

    return sorted(
        i.code for i in run_integrity_audit(scope="full").issues
        if i.vacancy_stable_id == SID
    )


def _clear_tracking() -> None:
    """Убрать строку tracking, чтобы проверить сиротскую ветку аудита."""
    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM application_tracking WHERE vacancy_stable_id=?", (SID,))
        conn.commit()
    finally:
        conn.close()


def _sql_review_count() -> int:
    conn = sqlite3.connect(config.DB_FILE)
    try:
        return conn.execute("SELECT COUNT(*) FROM application_reviews").fetchone()[0]
    finally:
        conn.close()


def _stored_status(raw: str | None) -> str:
    if raw is None:
        return "<NULL>"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return "<unparseable JSON>"
    if not isinstance(data, dict):
        return f"<{type(data).__name__}>"
    return str(data.get("status", "<no status key>"))


def main() -> int:
    print("=" * 96)
    print("Проба №56: строка ревью, неразбираемая моделью")
    print("=" * 96)
    _setup()
    print(f"вакансия: {SID}   tracking: READY_TO_APPLY   БД: {config.DB_FILE}")
    print()

    header = (
        f"{'форма':<18} {'в строке':<16} {'get_review':<11} {'is_created':<11} "
        f"{'Gate 2':<8} {'аудит по вакансии':<22} reason"
    )
    print(header)
    print("-" * len(header))

    rows = []
    for shape_id, _desc, raw in _shapes():
        _clear_verifications()
        _write_review(raw)
        rev = get_application_review(SID)
        created = is_review_created(SID)
        gate, _fp, _rid = HHSubmissionGates._check_review_gate(SID)
        codes = _audit_codes()

        gate_s = "n/a" if gate is None else ("pass" if gate.passed else "REFUSE")
        rev_s = "None" if rev is None else rev.status.value
        print(
            f"{shape_id:<18} {_stored_status(raw):<16} {rev_s:<11} {created!s:<11} "
            f"{gate_s:<8} {(', '.join(codes) or '<тихо>'):<22} {gate.reason if gate else ''}"
        )
        rows.append({
            "id": shape_id,
            "stored": _stored_status(raw),
            "rev": rev,
            "created": created,
            "gate_passed": None if gate is None else gate.passed,
            "gate_reason": gate.reason if gate else None,
            "audit": codes,
        })

    print()
    print("=" * 96)
    print("ЧТО ЭТО ЗНАЧИТ")
    print("=" * 96)

    approved_but_unreadable = [r for r in rows if r["stored"] == "APPROVED" and r["rev"] is None]
    print(f"строк, где записано APPROVED, а читатель вернул None: {len(approved_but_unreadable)}")
    for r in approved_but_unreadable:
        print(f"   - {r['id']}: is_review_created={r['created']} | Gate 2: {r['gate_reason']}")
    print()
    silent = [r for r in rows if r["rev"] is None and not r["audit"]]
    print(f"строк, не разобранных моделью и НЕ отмеченных аудитом: {len(silent)}")
    for r in silent:
        print(f"   - {r['id']}: аудит молчит")
    print()
    false_created = [r for r in rows if r["stored"] == "APPROVED" and r["created"] is False]
    print(f"строк, где ревью есть, а is_review_created() сказал «нет»: {len(false_created)}")

    print()
    print("=" * 96)
    print("ПОТРЕБИТЕЛИ: перечисление и сиротская ветка")
    print("=" * 96)
    from ai_assistant.application_review import list_application_reviews

    print(f"{'форма':<18} {'строк в SQL':<13} {'list_reviews()':<15} REVIEW_ORPHAN (без tracking)")
    print("-" * 96)
    for shape_id, _desc, raw in _shapes():
        _clear_verifications()
        _write_review(raw)
        in_sql = _sql_review_count()
        listed = len(list_application_reviews())
        _clear_tracking()
        orphans = _audit_codes()
        _setup()
        print(
            f"{shape_id:<18} {in_sql:<13} {listed:<15} "
            f"{(', '.join(orphans) or '<тихо>')}"
        )

    print()
    print("=" * 96)
    print("РЕКАВЕРИ: тот же вопрос, что в №55, но по колонке ревью")
    print("=" * 96)
    print("Сравниваю отчёт recovery для читаемой строки APPROVED и для битой.")
    print("Если отчёты совпадают - чтение ревью там декоративно, и №55 туда не")
    print("переезжает. Это замер, а не рассуждение: параметр review_status")
    print("объявлен в _determine_recovery_status, но в теле не читается.")
    print()
    for label, raw in (
        ("APPROVED, читается", json.dumps(_valid_payload())),
        ("APPROVED, битый JSON", '{"status": "APPROVED"'),
    ):
        _clear_verifications()
        _write_review(raw)
        res = inspect_submission_state(SID)
        print(f"  {label:<22} статус={res.recovery_status.value:<18}")
        print(f"  {'':<22} reason={res.reason}")
        print(f"  {'':<22} warnings={res.warnings}")
    _clear_reviews()

    print()
    print("=" * 96)
    print("АСИММЕТРИЯ: та же поломка в соседней таблице")
    print("=" * 96)
    print("Замеры раздельные: сначала в БД лежит ТОЛЬКО строка верификации, потом")
    print("ТОЛЬКО строка ревью. Аудит не помечает, из какой таблицы пришла находка,")
    print("поэтому писать обе сразу нельзя - колонки смешаются.")
    print()
    print("В каждой колонке - payload, валидный для СВОЕЙ модели, и один битый JSON.")
    print("Ревьюшный payload в таблицу верификаций не кладу: он там невалиден по")
    print("своей природе, и «проверка сработала» тогда ничего не доказывает.")
    print()
    broken = '{"status": "APPROVED"'
    asymmetry = [
        ("валидная своя", json.dumps(_valid_payload()), json.dumps(_verify_valid_payload())),
        ("битый JSON", broken, broken),
        ("SQL NULL", None, None),
    ]
    print(f"{'форма':<18} {'submission_verifications':<28} application_reviews")
    print("-" * 96)
    for label, review_raw, verif_raw in asymmetry:
        _clear_reviews()
        _write_verification(verif_raw)
        vcodes = _audit_codes()

        _clear_verifications()
        _write_review(review_raw)
        rcodes = _audit_codes()

        print(
            f"{label:<18} {(', '.join(vcodes) or '<тихо>'):<28} "
            f"{', '.join(rcodes) or '<тихо>'}"
        )
    _clear_reviews()
    _clear_verifications()

    print()
    print(f"временно: {TMP}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
