"""Проба №65: что читается из строки `application_submissions`, у которой не читается payload.

Семья та же, что №33 (очередь), №55 (верификации), №56 (ревью): строка есть, а
читатель отвечает так, будто её нет. Здесь дверь другая — **колонки строки**.

`get_submission()` возвращает кортеж
`(0 vacancy_stable_id, 1 submission_id, 2 executor_version, 3 submission_json,
4 status, 5 submitted_at, 6 created_at, 7 updated_at)`.

В живой `state.db` **девять строк из пятнадцати** лежат со сдвигом: колонка
`submission_json` содержит submission_id, `submitted_at` — сам payload, а
`created_at` — настоящий вердикт (`SUBMITTED`/`BLOCKED`/`SUBMISSION_UNKNOWN`).
Их записал прежний вызов `save_submission()` с другим представлением о подписи:
`save_submission(sid, sub_id, EXECUTOR_VERSION, json, status)`, а в девятом
случае `save_submission(sid, sub_id, "SUBMITTED", json)`. Сегодня все шесть мест
вызова передают `status=`/`submission_id=` по имени, поэтому писатель такой
строки больше не делает — но строки живые.

Ключевая деталь, которую видно только на живой базе: `submission_id` в колонке 1
у этих строк **сгенерирован самим `save_submission`** (`hh:<id>_<YYYYmmdd_HHMMSS>`),
потому что прежний вызов не передавал его. А верификация подписана тем самым
`sub_<id>_<epoch>`, который уехал в колонку payload. То есть колонка 1 у них —
не тот ключ, которым подписана верификация, и «прочитать правильный ключ» из
такой строки нельзя без догадки.

Вопрос пробы: что видит каждый потребитель такой строки.

Запуск:
    .venv/Scripts/python.exe tools/ble001_submission_payload_probe.py

Ничего не пишет в рабочие файлы: `config.DB_FILE` подменяется на временный.
Перепись по живой базе — только чтение (`mode=ro`).
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ai_assistant import config

TMP = tempfile.mkdtemp(prefix="ble001-submission-payload-")
config.DB_FILE = os.path.join(TMP, "state.db")
assert config.DB_FILE.startswith(TMP), config.DB_FILE

from ai_assistant import db
from ai_assistant.application_tracking import (
    ApplicationStatus,
    set_application_status,
    transition_application,
)
from ai_assistant.cli import submissions_show, submissions_verify
from ai_assistant.schema import Vacancy
from ai_assistant.submission_recovery import inspect_submission_state
from ai_assistant.submission_verifier import VERIFICATION_VERSION

SID = "hh:65"
# Ключ, которым подписана верификация в живой базе: он лежит в колонке payload.
VERIFIED_SUB_ID = "sub_65_1787999999"
# Ключ, который сгенерировал бы save_submission, если его не передали.
GENERATED_SUB_ID = "hh:65_20260920_000000"

LIVE_DB = os.path.join(REPO, "state.db")

FILES = (
    os.path.join("ai_assistant", "submission_recovery.py"),
    os.path.join("ai_assistant", "cli.py"),
    os.path.join("ai_assistant", "db.py"),
)


def sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _verified_payload() -> dict:
    return {
        "vacancy_stable_id": SID,
        "submission_id": VERIFIED_SUB_ID,
        "verification_status": "VERIFIED",
        "evidence": {"body_marker": True},
        "final_url": "https://hh.ru/applicant/negotiations",
        "page_title": "Отклик отправлен",
        "verified_at": "2026-09-20T00:00:00",
        "warnings": [],
        "verification_version": VERIFICATION_VERSION,
    }


def _write_verification(raw: str | None, submission_id: str = VERIFIED_SUB_ID) -> None:
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
            (
                SID, submission_id, VERIFICATION_VERSION, "VERIFIED", raw,
                "2026-09-20T00:00:00", "2026-09-20T00:00:00", "2026-09-20T00:00:00",
            ),
        )
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


def _write_submission(payload_column: str | None, submission_id: str = GENERATED_SUB_ID) -> None:
    """Положить в `submission_json` ровно `payload_column` — SQL, не save_submission().

    Пишем SQL-ом намеренно: сегодняшний писатель всегда кладёт туда json.dumps,
    и воспроизвести живую строку им нельзя.
    """
    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM application_submissions")
        conn.execute(
            """
            INSERT INTO application_submissions
                (vacancy_stable_id, submission_id, executor_version,
                 submission_json, status, submitted_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                SID, submission_id, "v1", payload_column, "SUBMITTED",
                "2026-09-20T00:00:00", "2026-09-20T00:00:00", "2026-09-20T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _fixture() -> None:
    """Вакансия в SUBMITTED: ровно то состояние, в котором отчёт идёт за верификацией."""
    db.init_db()
    db.save_vacancy(Vacancy(
        source="hh", source_job_id="65", title="Старший разработчик Python",
        company="TechCo", description="Python, PostgreSQL, FastAPI.",
        job_url="https://hh.ru/vacancy/65", location="Remote",
    ))
    set_application_status(
        SID, ApplicationStatus.DISCOVERED, company="TechCo",
        title="Старший разработчик Python", source="hh",
        vacancy_url="https://hh.ru/vacancy/65",
    )
    transition_application(SID, ApplicationStatus.ANALYZED)
    transition_application(SID, ApplicationStatus.READY_TO_APPLY)
    transition_application(SID, ApplicationStatus.SUBMITTED)


def _report_line(result) -> str:
    return (
        f"status={getattr(result.recovery_status, 'value', result.recovery_status)} | "
        f"submission_id={result.submission_id!r} | "
        f"reason={result.reason!r} | "
        f"warnings={result.warnings} | "
        f"last_verification="
        f"{None if result.last_verification is None else result.last_verification.get('verification_status')!r}"
    )


def _says_no_verification(result) -> bool:
    haystack = " | ".join([result.reason, *result.warnings]).lower()
    return "no verification performed" in haystack or "no verification was performed" in haystack


def _says_verified(result) -> bool:
    return (
        result.last_verification is not None
        and result.last_verification.get("verification_status") == "VERIFIED"
    )


def section_1_live_shape() -> str:
    banner("S1. живая форма: колонка 1 сгенерирована, верификация подписана ключом из payload")
    _write_submission(VERIFIED_SUB_ID, submission_id=GENERATED_SUB_ID)
    _write_verification(json.dumps(_verified_payload()))

    result = inspect_submission_state(SID)
    print("  отчёт:", _report_line(result))
    print("  действие:", repr(result.recommended_action))
    false_sentence = _says_no_verification(result)
    print("  ложное «no verification performed»:", false_sentence)
    print("  отчёт говорит VERIFIED:", _says_verified(result))
    if false_sentence:
        return "REPORT-CLAIMS-NO-VERIFICATION"
    if _says_verified(result):
        return "VERIFICATION-FOUND"
    return "REPORT-SAYS-UNREADABLE"


def section_2_control_readable() -> str:
    banner("S2. контроль: payload читается, верификация подписана ключом из колонки 1")
    _write_submission(json.dumps({"status": "SUBMITTED"}), submission_id=VERIFIED_SUB_ID)
    _write_verification(json.dumps(_verified_payload()))

    result = inspect_submission_state(SID)
    print("  отчёт:", _report_line(result))
    ok = _says_verified(result) and not result.warnings
    return "VERIFICATION-FOUND" if ok else "CONTROL-BROKEN"


def section_3_control_true_missing() -> str:
    banner("S3. контроль: payload читается, верификации нет вовсе — честное «не было»")
    _write_submission(json.dumps({"status": "SUBMITTED"}), submission_id=VERIFIED_SUB_ID)
    _clear_verifications()

    result = inspect_submission_state(SID)
    print("  отчёт:", _report_line(result))
    honest = _says_no_verification(result)
    return "STILL-REPORTS-MISSING" if honest else "HONEST-SENTENCE-LOST"


def section_4_broken_payload_right_key() -> str:
    banner("S4. payload сломан, но колонка 1 — тот самый ключ верификации")
    _write_submission('{"status": "SUBMITTED"', submission_id=VERIFIED_SUB_ID)
    _write_verification(json.dumps(_verified_payload()))

    result = inspect_submission_state(SID)
    print("  отчёт:", _report_line(result))
    if _says_verified(result):
        return "VERIFICATION-FOUND"
    if _says_no_verification(result):
        return "REPORT-CLAIMS-NO-VERIFICATION"
    return "REPORT-SAYS-UNREADABLE"


def section_5_control_55() -> str:
    banner("S5. контроль №55: payload читается, запись верификации не разбирается")
    _write_submission(json.dumps({"status": "SUBMITTED"}), submission_id=VERIFIED_SUB_ID)
    _write_verification('{"verification_status": "VERIFIED"')

    result = inspect_submission_state(SID)
    print("  отчёт:", _report_line(result))
    said = "cannot be read" in result.reason.lower()
    return "UNREADABLE-STILL-NAMED" if said else "REGRESSION-55"


def section_6_unreadable_payload_no_verification() -> str:
    banner("S6. контроль: payload не разбирается и верификации нет — отказ обязан остаться")
    _write_submission(VERIFIED_SUB_ID, submission_id=GENERATED_SUB_ID)
    _clear_verifications()

    result = inspect_submission_state(SID)
    print("  отчёт:", _report_line(result))
    status = getattr(result.recovery_status, "value", result.recovery_status)
    refused = status in ("NEEDS_VERIFICATION", "NEEDS_REVIEW")
    print("  всё ещё отказывает:", refused, "| не объявляет VERIFIED:", not _says_verified(result))
    return "STILL-REFUSES" if refused and not _says_verified(result) else "REFUSAL-LOST"


def section_7_cli_show() -> str:
    banner("S7. `submissions show` на строке с неразбираемым payload")
    _write_submission(VERIFIED_SUB_ID, submission_id=GENERATED_SUB_ID)
    _write_verification(json.dumps(_verified_payload()))

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = submissions_show(SID)
    # Вопрос пробы ровно в том, падает команда или нет, и чем именно: сузить
    # здесь значит перестать отвечать на заданный вопрос.
    except Exception as exc:  # noqa: BLE001 - the shape IS the measurement
        print(f"  ИСКЛЮЧЕНИЕ: {type(exc).__name__}: {exc}")
        print("  напечатано до падения:", repr(buf.getvalue()[:80]))
        return "CRASHES"
    out = buf.getvalue()
    print(f"  код возврата: {rc}")
    print("  напечатано:", repr(out[:200]))
    says_none = "No verification performed yet." in out
    print("  говорит «No verification performed yet.»:", says_none)
    return "SHOW-CLAIMS-NO-VERIFICATION" if says_none else "SHOW-REPORTS-RECORD"


def section_8_cli_verify_columns() -> str:
    banner("S8. `submissions verify` — из какой колонки он берёт submission_id")
    _write_submission(VERIFIED_SUB_ID, submission_id=GENERATED_SUB_ID)

    row = db.get_submission(SID)
    print(f"  get_submission → index1={row[1]!r} index3={row[3]!r}")
    for idx in (1, 3):
        try:
            json.loads(row[idx])
            parses = "разбирается"
        except (ValueError, TypeError) as exc:
            parses = f"НЕ разбирается ({type(exc).__name__})"
        print(f"  json.loads(row[{idx}]): {parses}")

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = submissions_verify(SID)
    except Exception as exc:  # noqa: BLE001 - the shape IS the measurement
        print(f"  вызов команды: ИСКЛЮЧЕНИЕ {type(exc).__name__}: {exc}")
        return "VERIFY-CRASHES"
    print(f"  вызов команды: код {rc}")
    return "VERIFY-RUNS"


def section_9_live_census() -> str:
    banner("S9. перепись по живой state.db (только чтение)")
    if not os.path.exists(LIVE_DB):
        print("  state.db не найден — раздел пропущен")
        return "NO-LIVE-DB"

    con = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT vacancy_stable_id, submission_id, submission_json, status FROM application_submissions"
        ).fetchall()
        payload_unreadable = set()
        id_unreadable = 0
        status_not_a_status = set()
        for sid, sub_id, payload, status in rows:
            try:
                json.loads(sub_id)
            except (ValueError, TypeError):
                id_unreadable += 1
            if payload:
                try:
                    json.loads(payload)
                except (ValueError, TypeError):
                    payload_unreadable.add(sid)
            if status == "v1":
                status_not_a_status.add(sid)
        print(f"  всего строк: {len(rows)}")
        print(f"  колонка 1 (submission_id) не разбирается как JSON: {id_unreadable} из {len(rows)}")
        print(f"  колонка 3 (payload) не разбирается: {len(payload_unreadable)} строк — {sorted(payload_unreadable)}")
        print(f"  колонка status содержит 'v1': {len(status_not_a_status)} строк — {sorted(status_not_a_status)}")

        verified_without_lookup = []
        for sid in sorted(payload_unreadable):
            n = con.execute(
                "SELECT COUNT(*) FROM submission_verifications "
                "WHERE vacancy_stable_id = ? AND verification_status = 'VERIFIED'",
                (sid,),
            ).fetchone()[0]
            if n:
                verified_without_lookup.append((sid, n))
        print(f"  из них имеют VERIFIED-верификацию: {verified_without_lookup}")
        return f"{len(payload_unreadable)}-UNREADABLE-PAYLOADS"
    finally:
        con.close()


def main() -> int:
    print("Проба №65 — строка application_submissions с неразбираемым payload")
    print(f"временная база: {config.DB_FILE}")
    for rel in FILES:
        print(f"  sha256 {rel}: {sha256(os.path.join(REPO, rel))}")

    _fixture()

    verdicts = {
        "S1 живая форма": section_1_live_shape(),
        "S2 контроль: payload читается": section_2_control_readable(),
        "S3 контроль: верификации нет": section_3_control_true_missing(),
        "S4 сломанный payload, верный ключ": section_4_broken_payload_right_key(),
        "S5 контроль №55": section_5_control_55(),
        "S6 контроль: отказ держится": section_6_unreadable_payload_no_verification(),
        "S7 submissions show": section_7_cli_show(),
        "S8 submissions verify": section_8_cli_verify_columns(),
        "S9 живая база": section_9_live_census(),
    }

    banner("ИТОГ")
    for key, value in verdicts.items():
        print(f"  {key:36s} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
