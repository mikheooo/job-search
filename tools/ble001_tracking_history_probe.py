"""Проба №59: смена статуса трекинга, которая не оставляет записи, и слепой аудит.

`application_tracking.set_application_status()` документирован как «Direct set
without validation (for sync)». По СУЩЕСТВУЮЩЕЙ строке он меняет `status` и
`last_status_change_at`, но не пишет в `application_status_history` - таблицу,
куда пишет `transition_application()` и откуда `application_integrity.
_check_lifecycle_transitions()` узнаёт о переходах.

Вопрос пробы: что остаётся в базе после смены статуса через невалидирующего
писателя - и что об этой смене говорит аудит, который существует ровно для того,
чтобы ловить незаконные переходы.

Замеры (состояние ДО правки №59, HEAD 08a72e1):

  * S1 `set_application_status` по существующей строке: история НЕ растёт
  * S2 контроль: тот же переход через `transition_application` - отказ ValueError
  * S3 форма синхронизации отправки (`hh_submission.py:1199`): то же самое
  * S4 аудит видит незаконный переход, когда строка истории ЕСТЬ - значит слепота
       не в детекторе, а в писателе
  * S5 `sync_application_tracking` при отказе автомата пишет ОТКАЗАННЫЙ статус
       (`application_tracking.py:443-445`), пропуская ступень ANALYZED

Запуск:
    .venv/Scripts/python.exe tools/ble001_tracking_history_probe.py

Ничего не пишет в рабочие файлы: config.DB_FILE подменяется на временный.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ai_assistant import config

TMP = tempfile.mkdtemp(prefix="ble001-track-hist-")
config.DB_FILE = os.path.join(TMP, "state.db")

from ai_assistant import application_tracking as tracking
from ai_assistant import db
from ai_assistant.application_integrity import run_integrity_audit
from ai_assistant.application_tracking import (
    ApplicationStatus,
    get_application_status,
    set_application_status,
    transition_application,
)
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.schema import Vacancy

MEASURED = [
    "ai_assistant/application_tracking.py",
    "ai_assistant/application_integrity.py",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(rel: str) -> str:
    with open(os.path.join(REPO, rel), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _history(sid: str) -> list[tuple[str | None, str, str]]:
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT old_status, new_status, changed_at FROM application_status_history "
        "WHERE vacancy_stable_id=? ORDER BY changed_at ASC, id ASC",
        (sid,),
    )
    rows = cur.fetchall()
    conn.close()
    return [(r[0], r[1], r[2]) for r in rows]


def _audit_codes(sid: str) -> dict[str, int]:
    report = run_integrity_audit(scope="tracked")
    codes: dict[str, int] = {}
    for issue in report.issues:
        if issue.vacancy_stable_id == sid:
            codes[issue.code] = codes.get(issue.code, 0) + 1
    return codes


def _hist_str(hist: list[tuple[str | None, str, str]]) -> str:
    if not hist:
        return "<пусто>"
    return " | ".join(f"{o or '∅'}->{n}" for o, n, _ in hist)


def _row(sid: str) -> tuple[str, str | None]:
    rec = get_application_status(sid)
    if not rec:
        return "<строки нет>", None
    return rec.status.value, rec.last_status_change_at


def _verdict(sid: str) -> str:
    """Совпадает ли конец записанной истории с фактическим статусом строки."""
    status, _ = _row(sid)
    hist = _history(sid)
    tail = hist[-1][1] if hist else None
    if status == "<строки нет>":
        return "NO-ROW"
    return "RECORDED" if tail == status else "UNRECORDED"


def _report(sid: str, label: str) -> None:
    status, changed = _row(sid)
    hist = _history(sid)
    codes = _audit_codes(sid)
    print(f"  строка.status            = {status}")
    print(f"  строка.last_status_chg   = {changed}")
    print(f"  история ({len(hist)} строк)     = {_hist_str(hist)}")
    print(f"  конец истории            = {hist[-1][1] if hist else '<пусто>'}")
    print(f"  аудит по этому sid       = {codes or '<ничего не сказал>'}")
    print(f"  ВЕРДИКТ                  = {_verdict(sid)}   ({label})")


def _vac(sid: str, title: str, desc: str) -> Vacancy:
    return Vacancy(
        source="test",
        source_job_id=str(sid),
        title=title,
        company="Acme",
        description=desc,
        job_url=f"https://example.com/{sid}_test",
        location="Remote",
        country_restrictions=[],
        timezone_restrictions=[],
        salary_min=5000,
        salary_max=5000,
        salary_currency="USD",
        employment_type="Full Time",
    )


def _profile_for_sync() -> CandidateProfile:
    return CandidateProfile(
        desired_roles=["Test Engineer"],
        alternative_roles=[],
        skills=["python"],
        preferred_seniority=[],
        remote_required=False,
        allowed_locations=[],
        allowed_timezones=[],
        languages=[],
        employment_types=[],
        minimum_salary=None,
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )


def _write_profile() -> str:
    path = Path(TMP) / "profile.json"
    path.write_text(json.dumps(_profile_for_sync().to_dict()), encoding="utf-8")
    return str(path)


def main() -> int:
    print("=" * 78)
    print("Проба №59: невалидирующий писатель, история статусов и слепой аудит")
    print("=" * 78)
    print(f"БД пробы: {config.DB_FILE}")
    for rel in MEASURED:
        print(f"sha256 {rel} = {_sha256(rel)}")
    db.init_db()

    # ---------------------------------------------------------------- S1
    print()
    print("S1. Форма watcher.py:391/408/424/440 - REJECTED по существующей строке")
    sid = "hh:59-s1"
    set_application_status(sid, ApplicationStatus.DISCOVERED, company="Acme", title="T")
    print(f"  после создания: история = {_hist_str(_history(sid))}")
    set_application_status(sid, ApplicationStatus.REJECTED, notes="watcher: ineligible")
    print("  затем set_application_status(REJECTED)  <- БЕЗ transition_application")
    _report(sid, "ожидание: UNRECORDED")

    # ---------------------------------------------------------------- S2
    print()
    print("S2. Контроль: те же два статуса через автомат состояний")
    sid2 = "hh:59-s2"
    set_application_status(sid2, ApplicationStatus.DISCOVERED, company="Acme", title="T")
    try:
        transition_application(sid2, ApplicationStatus.REJECTED)
        print("  transition_application(DISCOVERED -> REJECTED): ПРОПУСТИЛ (не ожидалось)")
    except ValueError as exc:
        print(f"  transition_application(DISCOVERED -> REJECTED): отказ ValueError: {exc}")
    print(f"  история после отказа    = {_hist_str(_history(sid2))}")
    transition_application(sid2, ApplicationStatus.ANALYZED)
    print(f"  законный шаг ANALYZED   = {_hist_str(_history(sid2))}")
    _report(sid2, "ожидание: RECORDED - законный путь записан")

    # ---------------------------------------------------------------- S3
    print()
    print("S3. Форма hh_submission.py:1199 - SUBMITTED для синхронизации с фактом")
    sid3 = "hh:59-s3"
    set_application_status(sid3, ApplicationStatus.DISCOVERED, company="Acme", title="T")
    set_application_status(sid3, ApplicationStatus.SUBMITTED)
    print("  set_application_status(SUBMITTED) по строке DISCOVERED")
    _report(sid3, "ожидание: UNRECORDED")
    # Вердикт снимается ЗДЕСЬ: S4 допишет этому же sid строку истории, и снятый
    # после неё вердикт был бы про состояние, которого в S3 не было. Первый прогон
    # пробы на этом соврал (S3 в сводке показал RECORDED).
    verdict_s3 = _verdict(sid3)
    audit_s3 = _audit_codes(sid3)

    # ---------------------------------------------------------------- S4
    print()
    print("S4. Тот же sid3, но строка истории ДОПИСАНА руками - проверка детектора")
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO application_status_history "
        "(vacancy_stable_id, old_status, new_status, changed_at, note) VALUES (?,?,?,?,?)",
        (sid3, "DISCOVERED", "SUBMITTED", _now(), "впрыснуто пробой: делает смену видимой"),
    )
    conn.commit()
    conn.close()
    print(f"  история теперь          = {_hist_str(_history(sid3))}")
    print(f"  аудит по этому sid       = {_audit_codes(sid3) or '<ничего не сказал>'}")
    print("  ВЫВОД: детектор умеет ловить незаконный переход; он слеп не сам по себе,")
    print("         а потому что писатель не оставляет ему строки.")
    # ---------------------------------------------------------------- S5
    print()
    print("S5. sync_application_tracking при отказе автомата (application_tracking.py:443)")
    vac = _vac("sync59", "Test Engineer", "python remote")
    db.save_vacancy(vac)
    sid5 = vac.stable_id()
    db.save_application_package(sid5, "probe-v1", json.dumps({"cover_letter": "x", "answers": {}}))
    prof_path = _write_profile()

    def _refuse(*_a, **_k):
        raise ValueError("Invalid transition DISCOVERED -> READY_TO_APPLY")

    orig_transition = tracking.transition_application
    tracking.transition_application = _refuse
    try:
        res = tracking.sync_application_tracking(profile_path=prof_path)
    finally:
        tracking.transition_application = orig_transition
    print(f"  отказ автомата впрыснут; sync вернул {res}")
    print("  ветка except сделала set_application_status(target) - БЕЗ валидации")
    _report(sid5, "ожидание: UNRECORDED, ступень ANALYZED пропущена")

    # ---------------------------------------------------------------- итог
    print()
    print("=" * 78)
    verdicts = {
        "S1 watcher-форма": _verdict(sid),
        "S2 автомат (законный)": _verdict(sid2),
        "S3 синхронизация отправки": verdict_s3,
        "S5 откат синка": _verdict(sid5),
    }
    for name, verdict in verdicts.items():
        print(f"  {name:28s} {verdict}")
    print()
    print("  Проверка законности переходов (`_check_lifecycle_transitions`) в S1, S3, S5")
    print("  не сказала ничего. В S3 сработала ДРУГАЯ проверка, сравнивающая трекинг с")
    print(f"  записями отправок: {audit_s3}. То есть SUBMITTED ловится не тем сторожем,")
    print("  который для этого написан, а REJECTED (S1) не ловится вообще ничем.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
