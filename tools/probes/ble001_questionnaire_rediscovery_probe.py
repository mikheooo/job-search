"""Проба №64: повторное обнаружение анкеты откатывает её статус.

Сосед №41, №60 и №63 — и та же асимметрия, что у них, но в третьей колонке.
`db.save_hh_questionnaire()` (db.py:865) — upsert с детерминированным ключом: id
анкеты считается как sha256 от (вакансия, беседа, фингерпринт вопросов), поэтому
обнаружение той же формы второй раз попадает в ту же строку (так и задумано,
см. докстроку `discover_hh_questionnaire_from_snapshot`).

Колонки в этом upsert защищены по-разному:

    vacancy_stable_id = COALESCE(excluded.vacancy_stable_id, ...)   защищена
    conversation_id   = COALESCE(excluded.conversation_id, ...)     защищена
    title             = COALESCE(excluded.title, ...)               защищена
    employer          = COALESCE(excluded.employer, ...)            защищена
    fingerprint       = excluded.fingerprint                        перезапись
    questions_json    = excluded.questions_json                     перезапись
    answers_json      = COALESCE(excluded.answers_json, ...)        защищена (#41)
    status            = excluded.status                             перезапись
    updated_at        = excluded.updated_at                         перезапись

`answers_json` защищена осознанно: №41 завёл `_serialise_answers_json()`
(db.py:25), который для пустого словаря возвращает NULL, и докстрока прямо
говорит, зачем - «a questions-only re-discovery must not write "{}" here - that
would erase the answers instead of leaving them alone».

`status` такой защиты не получил, а полезная нагрузка всегда несёт статус:
`extract_hh_questionnaire_from_snapshot()` собирает модель с жёстко
проставленным `status=HHQuestionStatus.NEEDS_HUMAN_REVIEW.value` (hh_questionnaire.py:272).
Значит любое повторное обнаружение пишет NEEDS_HUMAN_REVIEW поверх того, что
лежит в строке.

Дверь живая и не экзотическая: `record_questionnaire_for_vacancy()` зовётся из
CLI-команды подготовки (`cli.py:448`) и срабатывает на КАЖДОМ прогоне по той же
вакансии, у которой форма нашлась.

Почему это запись, а не дыра: `submit_questionnaire_response()`
(hh_questionnaire.py:771) отказывает повторной отправке РОВНО по статусу -

    if quest.status == HHQuestionStatus.SUBMITTED.value:
        report.reason = "Questionnaire already submitted - one-shot invariant ..."

После отката эта проверка перестаёт срабатывать, и строка говорит «человек ещё
не ответил» о анкете, которая уже отправлена.

Прогон:

    .venv/Scripts/python.exe tools/ble001_questionnaire_rediscovery_probe.py

Ничего не пишет в рабочие файлы: `config.DB_FILE` подменяется на временный
каталог ДО импорта `ai_assistant.config`, и подмена проверяется.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ai_assistant import config

TMP = tempfile.mkdtemp(prefix="ble001-questionnaire-rediscovery-")
config.DB_FILE = os.path.join(TMP, "state.db")

from ai_assistant import db
from ai_assistant.hh_questionnaire import (
    HHQuestionStatus,
    discover_hh_questionnaire_from_snapshot,
    record_questionnaire_for_vacancy,
    submit_questionnaire_response,
)

MEASURED = [
    "ai_assistant/db.py",
    "ai_assistant/hh_questionnaire.py",
    "ai_assistant/cli.py",
]

SUBMITTED = HHQuestionStatus.SUBMITTED.value
NEEDS_REVIEW = HHQuestionStatus.NEEDS_HUMAN_REVIEW.value

ANSWERS = {"q1": "7"}


def _snapshot(sid: str, conversation: str, question_text: str = "Опыт работы?") -> dict:
    return {
        "vacancy_stable_id": sid,
        "conversation_id": conversation,
        "title": "Python Engineer",
        "questions": [
            {"id": "q1", "text": question_text, "type": "number", "required": True},
        ],
    }


def _sha256(rel: str) -> str:
    with open(os.path.join(REPO, rel), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _status(qid: str) -> str | None:
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT status FROM hh_questionnaires WHERE questionnaire_id=?", (qid,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _raw_answers(qid: str) -> str | None:
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT answers_json FROM hh_questionnaires WHERE questionnaire_id=?", (qid,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _verdict(ok: bool, bad: str, good: str) -> str:
    return good if ok else bad


def main() -> int:
    if config.DB_FILE != os.path.join(TMP, "state.db"):
        print("REFUSING: config.DB_FILE was not redirected to the temp directory")
        return 2
    print(f"db file (redirected): {config.DB_FILE}")
    for rel in MEASURED:
        print(f"  {rel:<38} sha256 {_sha256(rel)[:16]}")
    print()

    db.init_db()

    # --- S1: живая дверь. Обнаружили, человек ответил, статус SUBMITTED, потом
    #     тот же прогон подготовки повторился и обнаружил ту же форму снова.
    snap = _snapshot("hh:640001", "conv64a")
    quest = discover_hh_questionnaire_from_snapshot(snap, vacancy_stable_id="hh:640001")
    assert quest is not None
    qid = quest.questionnaire_id
    db.update_hh_questionnaire_answers(qid, ANSWERS, new_status=SUBMITTED)
    before = _status(qid)
    discover_hh_questionnaire_from_snapshot(snap, vacancy_stable_id="hh:640001")
    after = _status(qid)
    print(f"S1 rediscovery after submit: {before} -> {after}  "
          f"{_verdict(after == SUBMITTED, 'STATUS-REWOUND', 'STATUS-KEPT')}")

    # --- S2: контроль. Анкета, которой раньше не было, обязана появиться с
    #     NEEDS_HUMAN_REVIEW: предохранитель, запрещающий записывать статус
    #     вообще, сломал бы обнаружение.
    fresh = discover_hh_questionnaire_from_snapshot(
        _snapshot("hh:640002", "conv64b", question_text="Другой вопрос?"),
        vacancy_stable_id="hh:640002",
    )
    assert fresh is not None
    fresh_status = _status(fresh.questionnaire_id)
    print(f"S2 control, a genuinely new questionnaire: {fresh_status}  "
          f"{_verdict(fresh_status == NEEDS_REVIEW, 'CREATION-BROKEN', 'CREATED-AS-NEEDS-REVIEW')}")

    # --- S3: контроль. Ответы переживают повторное обнаружение: это уже чинил
    #     №41, и здесь проверяется, что №64 его не сломал.
    raw = _raw_answers(qid)
    try:
        kept = json.loads(raw) == ANSWERS
    except (TypeError, ValueError):
        kept = False
    print(f"S3 control, answers survive the rediscovery: {raw}  "
          f"{_verdict(kept, 'ANSWERS-LOST', 'ANSWERS-KEPT')}")

    # --- S4: наблюдаемое следствие. One-shot инвариант держится на статусе,
    #     значит после отката он не срабатывает.
    res = submit_questionnaire_response(qid, ANSWERS, confirm_submit=False)
    held = "already submitted" in (res.reason or "")
    print(f"S4 one-shot invariant after the rediscovery: verdict={res.verdict} "
          f"reason={(res.reason or '')[:72]}  "
          f"{_verdict(held, 'ONE-SHOT-DISARMED', 'ONE-SHOT-HELD')}")

    # --- S5: тот же откат через ЖИВОГО вызывающего — дверь CLI.
    sid = "hh:640003"
    snap3 = _snapshot(sid, "conv64c")
    qid3 = record_questionnaire_for_vacancy(snap3, sid, title="Python Engineer")
    assert qid3
    db.update_hh_questionnaire_answers(qid3, ANSWERS, new_status=SUBMITTED)
    before3 = _status(qid3)
    record_questionnaire_for_vacancy(snap3, sid, title="Python Engineer")
    after3 = _status(qid3)
    print(f"S5 live CLI caller (record_questionnaire_for_vacancy): "
          f"{before3} -> {after3}  "
          f"{_verdict(after3 == SUBMITTED, 'CLI-PATH-REWINDS', 'CLI-PATH-KEEPS')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
