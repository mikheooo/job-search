"""Проба №60: писатель раннера стирает ответы анкеты, и читатель не отличает
стёртое от «никогда не было».

`hh_application_runner.py:702` внутри ветки «отправка подтверждена» делает

    db.update_hh_questionnaire_answers(qid, {}, new_status=SUBMITTED)

`update_hh_questionnaire_answers()` (db.py:1022) не сливает, а ПЕРЕЗАПИСЫВАЕТ
колонку `answers_json` тем, что ей дали. Значит эта строка кладёт в колонку
`"{}"` - поверх настоящих ответов, которые за мгновение до неё положил
`hh_questionnaire.submit_questionnaire_response()` (hh_questionnaire.py:847),
позвав тот же самый writer с настоящими `human_answers`.

Вопрос пробы: что остаётся в записи после успешной отправки анкеты, что об этой
записи говорят её читатели и видит ли их предохранитель (#46, `answers_error`)
стёртое поле.

Замеры (состояние ДО правки №60, HEAD f61aa81):

  * S1 писатель раннера: `answers_json` было настоящими ответами, стало `"{}"`
  * S2 читатели о стёртой строке: `answers_error is None`, `validate_human_answers`
       отказывает, `audit_questionnaire` даёт NEEDS_CORRECTION, гейт 5 политики
       не засчитывает `questionnaire_complete`
  * S3 контроль: та же строка с настоящими ответами - читатели довольны
  * S4 дать читателю материал: вернуть ответы - все вердикты возвращаются
       (значит дело в стёртом поле, а не в читателях)
  * S5 путь раннера: `submit_questionnaire_response` кладёт НАСТОЯЩИЕ ответы со
       статусом SUBMITTED, и следующая же строка раннера их стирает

Запуск:
    .venv/Scripts/python.exe tools/ble001_questionnaire_answers_probe.py

Ничего не пишет в рабочие файлы: config.DB_FILE подменяется на временный.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ai_assistant import config

TMP = tempfile.mkdtemp(prefix="ble001-quest-answers-")
config.DB_FILE = os.path.join(TMP, "state.db")

from ai_assistant import db
from ai_assistant.hh_questionnaire import (
    HHQuestionnaire,
    HHQuestionStatus,
    submit_questionnaire_response,
    validate_human_answers,
)
from ai_assistant.hh_questionnaire_audit import audit_questionnaire

MEASURED = [
    "ai_assistant/hh_application_runner.py",
    "ai_assistant/db.py",
    "ai_assistant/hh_questionnaire.py",
    "ai_assistant/hh_questionnaire_audit.py",
]

QID = "q60"
QUESTIONS = [
    {"question_id": "q_exp", "text": "Сколько лет опыта?", "question_type": "number", "required": True},
    {"question_id": "q_city", "text": "Готовы к релокации?", "question_type": "text", "required": True},
]
REAL_ANSWERS = {"q_exp": "7", "q_city": "Да"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(rel: str) -> str:
    with open(os.path.join(REPO, rel), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _raw_answers_column() -> str | None:
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT answers_json FROM hh_questionnaires WHERE questionnaire_id=?", (QID,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _status_column() -> str | None:
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT status FROM hh_questionnaires WHERE questionnaire_id=?", (QID,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _reset() -> None:
    """Убрать строку целиком.

    Нужно потому, что `save_hh_questionnaire` пишет `answers_json` через
    `COALESCE(excluded.answers_json, hh_questionnaires.answers_json)`, а
    `_serialise_answers_json` на пустом словаре отдаёт None. То есть `{}` через
    этого писателя НЕ записывается вовсе - колонка сохраняет прежнее значение.
    (Это отдельное наблюдение, не часть №60; здесь оно только мешало замеру S4.)
    """
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM hh_questionnaires WHERE questionnaire_id=?", (QID,))
    conn.commit()
    conn.close()


def _seed(answers: dict | None, status: str) -> None:
    _reset()
    db.save_hh_questionnaire(
        {
            "questionnaire_id": QID,
            "vacancy_stable_id": None,
            "conversation_id": None,
            "title": None,
            "employer": None,
            "fingerprint": "fp60",
            "questions": QUESTIONS,
            "answers": answers,
            "status": status,
            "created_at": _now(),
            "updated_at": _now(),
        }
    )


def _readers() -> dict[str, str]:
    """Всё, что читатели говорят об этой строке."""
    data = db.get_hh_questionnaire(QID)
    quest = HHQuestionnaire(**data)
    val = validate_human_answers(quest, quest.answers)
    audit = audit_questionnaire(QID)
    out = {
        "answers": json.dumps(quest.answers, ensure_ascii=False),
        "answers_error": str(quest.answers_error),
        "validate.ok": str(val.ok),
        "validate.status": str(val.status),
        "validate.missing_required": json.dumps(val.missing_required),
        "audit.overall": str(getattr(audit.overall, "value", audit.overall)),
        "audit.first_rationale": (audit.items[0].rationale if audit.items else "<нет пунктов>"),
        "audit.real_hh_submit": str(audit.real_hh_submit),
    }
    return out


def _gate5() -> str:
    """Гейт 5 политики отправки - единственный, кто читает ответы анкеты."""
    from ai_assistant.hh_submit_policy import evaluate

    decision = evaluate(
        {
            "application_id": "app60",
            "vacancy_stable_id": None,
            "employer": "",
            "title": "",
            "questionnaire_id": QID,
            "state": "READY_TO_SUBMIT",
        }
    )
    if "questionnaire_complete" in decision.checks_passed:
        return "PASSED (questionnaire_complete)"
    if "questionnaire_complete" in decision.checks_failed:
        reasons = [r for r in decision.reasons if "answer" in r.lower() or "question" in r.lower()]
        return f"FAILED (questionnaire_complete) {reasons[:2]}"
    return "не спрошен"


def _print_readers(label: str) -> None:
    for key, value in _readers().items():
        print(f"  {key:24} = {value}")
    print(f"  {'gate5':24} = {_gate5()}")
    print(f"  --- {label}")


def _verdict_erased() -> str:
    """Слеп ли предохранитель #46 к стёртому полю."""
    data = db.get_hh_questionnaire(QID)
    quest = HHQuestionnaire(**data)
    if quest.answers_error:
        return "GUARD-FIRES"
    return "GUARD-BLIND" if quest.answers == {} else "ANSWERS-PRESENT"


def main() -> int:
    db.init_db()
    print("=" * 78)
    print("№60. Ответы анкеты, стёртые писателем раннера")
    print("=" * 78)
    for rel in MEASURED:
        print(f"  sha256 {rel:42} {_sha256(rel)}")
    print()

    # ------------------------------------------------------------------ S1
    print("S1. Писатель раннера (hh_application_runner.py:702) по строке с ответами")
    _seed(REAL_ANSWERS, HHQuestionStatus.READY_TO_SUBMIT.value)
    print(f"  answers_json до писателя    = {_raw_answers_column()}")
    print(f"  status до писателя          = {_status_column()}")
    db.update_hh_questionnaire_answers(QID, {}, new_status=HHQuestionStatus.SUBMITTED.value)
    after = _raw_answers_column()
    print(f"  answers_json после писателя = {after}")
    print(f"  status после писателя       = {_status_column()}")
    print(f"  ВЕРДИКТ S1 = {'ERASED' if after == '{}' else 'KEPT'}")
    print()

    # ------------------------------------------------------------------ S2
    print("S2. Что читатели говорят о стёртой строке")
    _print_readers("стёртая строка")
    print(f"  предохранитель #46          = {_verdict_erased()}")
    print()

    # ------------------------------------------------------------------ S3
    print("S3. Контроль: та же строка, ответы на месте")
    _seed(REAL_ANSWERS, HHQuestionStatus.SUBMITTED.value)
    print(f"  answers_json                = {_raw_answers_column()}")
    _print_readers("ответы на месте")
    print("  NB: audit.overall = NEEDS_CORRECTION и здесь, и на стёртой строке -")
    print("      этот вердикт НЕ различает два состояния (ожидание опровергнуто).")
    print("      Различают validate_human_answers и гейт 5, а причину видно в")
    print("      audit.first_rationale: «No answer provided» против «Unverified».")
    print()

    # ------------------------------------------------------------------ S4
    print("S4. Дать читателю материал: вернуть стёртые ответы")
    _seed({}, HHQuestionStatus.SUBMITTED.value)
    print(f"  answers_json (стёрто)       = {_raw_answers_column()}")
    _print_readers("стёрто")
    db.update_hh_questionnaire_answers(QID, REAL_ANSWERS, new_status=HHQuestionStatus.SUBMITTED.value)
    print(f"  answers_json (возвращено)   = {_raw_answers_column()}")
    _print_readers("возвращено")
    print()

    # ------------------------------------------------------------------ S5
    print("S5. Путь раннера: анкета кладёт настоящие ответы, раннер их стирает")
    _seed(REAL_ANSWERS, HHQuestionStatus.READY_TO_SUBMIT.value)
    clicked: list[str] = []

    def _fake_evaluate(script: str) -> str:
        clicked.append(script)
        return json.dumps({"ok": True, "clicked_button": "Откликнуться"})

    q_res = submit_questionnaire_response(
        questionnaire_id=QID,
        human_answers=REAL_ANSWERS,
        evaluate_fn=_fake_evaluate,
        confirm_submit=True,
    )
    print(f"  submit_questionnaire_response: verdict={q_res.verdict} submit_count={q_res.submit_count}")
    print(f"  answers_json после анкеты   = {_raw_answers_column()}")
    print(f"  status после анкеты         = {_status_column()}")
    stored = _raw_answers_column()
    # строка раннера дословно
    db.update_hh_questionnaire_answers(QID, {}, new_status=HHQuestionStatus.SUBMITTED.value)
    print(f"  answers_json после раннера  = {_raw_answers_column()}")
    print(
        "  ВЕРДИКТ S5 = "
        + ("RUNNER-ERASES-STORED" if stored == json.dumps(REAL_ANSWERS, ensure_ascii=False) and _raw_answers_column() == "{}" else "NOT-REPRODUCED")
    )
    print()

    print("=" * 78)
    print("Сводка: S1 ERASED, S2 GUARD-BLIND + отказы читателей, S3 читатели довольны,")
    print("        S4 возврат ответов возвращает вердикты, S5 раннер стирает уже")
    print("        записанные анкетой ответы.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
