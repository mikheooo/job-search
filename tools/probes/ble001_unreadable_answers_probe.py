"""Проба №63: нечитаемая колонка ответов анкеты затирается пустым словарём.

Сосед №46 и его продолжение. №46 научил ЧИТАТЕЛЯ не выдавать ошибку разбора за
пустоту: `db._row_to_questionnaire` (db.py:934-941) кладёт в словарь
`questions_error` / `answers_error`, а `HHQuestionnaire` носит их полями. Три
предохранителя читают `questions_error` (`hh_questionnaire.py:473`,
`hh_questionnaire_audit.py:123`, `hh_submit_policy.py:316`).

`answers_error` не читает НИКТО. Проверка:

    grep -rn "answers_error" --include=*.py . | grep -v /.venv/

даёт поле модели, комментарии, тесты и пробы - и ни одного боевого читателя.

Вопрос пробы не «есть ли пометка» (она есть), а «что с ней делает ПИСАТЕЛЬ».
Все пять боевых писателей `db.update_hh_questionnaire_answers()` передают туда
то, что им дали снаружи, а раннер берёт это из чтения той же колонки
(`hh_application_runner.py:531`):

    human_answers = quest_data.get("answers") or app.get("answers") or {}

При нечитаемой колонке это `{}` - и писатель кладёт `{}` поверх нечитаемых
байтов. `"{}"` - валидный JSON, поэтому после записи `answers_error is None`:
нечитаемая строка становится НЕОТЛИЧИМОЙ от строки, которую никогда не
заполняли, а единственная улика (сами байты колонки) исчезает. Это ровно тот
класс, что №60 («стёртое неотличимо от никогда не было»), но через другую дверь.

Состояние ДО правки. Запуск:

    .venv/Scripts/python.exe tools/ble001_unreadable_answers_probe.py

Ничего не пишет в рабочие файлы: `config.DB_FILE` подменяется на временный
каталог ДО импорта `ai_assistant.config`, и подмена проверяется.
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

TMP = tempfile.mkdtemp(prefix="ble001-unreadable-answers-")
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
    "ai_assistant/db.py",
    "ai_assistant/hh_questionnaire.py",
    "ai_assistant/hh_questionnaire_audit.py",
    "ai_assistant/hh_submit_policy.py",
    "ai_assistant/hh_application_runner.py",
]

QID = "q63"
APP_ID = "app63"
QUESTIONS = [
    {"question_id": "q_exp", "text": "Сколько лет опыта?", "question_type": "number", "required": True},
    {"question_id": "q_city", "text": "Готовы к релокации?", "question_type": "text", "required": True},
]
REAL_ANSWERS = {"q_exp": "7", "q_city": "Да"}
# Обрезанный JSON: именно такую колонку мерил №46 ("{not json").
BROKEN = '{"q_exp": "7"'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(rel: str) -> str:
    with open(os.path.join(REPO, rel), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _raw_answers() -> str | None:
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT answers_json FROM hh_questionnaires WHERE questionnaire_id=?", (QID,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _status() -> str | None:
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT status FROM hh_questionnaires WHERE questionnaire_id=?", (QID,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _set_raw(value: str | None) -> None:
    """Положить в колонку ровно то, что нужно замеру.

    Через писателей это невозможно: `json.dumps()` всегда даёт валидный JSON.
    Поэтому нечитаемая колонка ставится прямым UPDATE - так же, как её поставил
    бы внешний инструмент, обрыв записи или ручная правка.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE hh_questionnaires SET answers_json=? WHERE questionnaire_id=?", (value, QID))
    conn.commit()
    conn.close()


def _seed(answers: dict | None, status: str) -> None:
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM hh_questionnaires WHERE questionnaire_id=?", (QID,))
    conn.commit()
    conn.close()
    db.save_hh_questionnaire(
        {
            "questionnaire_id": QID,
            "vacancy_stable_id": None,
            "conversation_id": None,
            "title": None,
            "employer": None,
            "fingerprint": "fp63",
            "questions": QUESTIONS,
            "answers": answers,
            "status": status,
            "created_at": _now(),
            "updated_at": _now(),
        }
    )


def _seed_app() -> None:
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM hh_applications WHERE application_id=?", (APP_ID,))
    conn.commit()
    conn.close()
    db.save_hh_application(
        {
            "application_id": APP_ID,
            "conversation_id": None,
            "vacancy_stable_id": "hh:999063",
            "title": "Вакансия пробы",
            "employer": "Работодатель",
            "state": "READY_TO_SUBMIT",
            "questionnaire_id": QID,
            "created_at": _now(),
            "updated_at": _now(),
        }
    )


def _quest() -> HHQuestionnaire:
    return HHQuestionnaire(**db.get_hh_questionnaire(QID))


def _validate_reason() -> str:
    quest = _quest()
    val = validate_human_answers(quest, quest.answers)
    return f"ok={val.ok} status={val.status} reason={val.reason!r}"


def _audit_rationale() -> str:
    audit = audit_questionnaire(QID, application_id=APP_ID)
    overall = getattr(audit.overall, "value", audit.overall)
    first = audit.items[0].rationale if audit.items else "<нет пунктов>"
    source = audit.items[0].source_of_truth if audit.items else "<нет>"
    return f"overall={overall} source={source} rationale={first!r}"


def _gate5() -> str:
    from ai_assistant.hh_submit_policy import evaluate

    decision = evaluate(
        {
            "application_id": APP_ID,
            "vacancy_stable_id": "hh:999063",
            "employer": "Работодатель",
            "title": "Вакансия пробы",
            "questionnaire_id": QID,
            "state": "READY_TO_SUBMIT",
        }
    )
    reasons = [r for r in decision.reasons if "answer" in r.lower() or "question" in r.lower()]
    if "questionnaire_complete" in decision.checks_passed:
        return f"PASSED (questionnaire_complete) {reasons[:2]}"
    if "questionnaire_complete" in decision.checks_failed:
        return f"FAILED (questionnaire_complete) {reasons[:3]}"
    return "не спрошен"


def _marker() -> str:
    quest = _quest()
    if quest.answers_error:
        return "MARKER-PRESENT"
    return "MARKER-ABSENT"


def main() -> int:
    db.init_db()
    print("=" * 78)
    print("№63. Нечитаемая колонка ответов: что говорит читатель и что делает писатель")
    print("=" * 78)
    for rel in MEASURED:
        print(f"  sha256 {rel:42} {_sha256(rel)}")
    assert config.DB_FILE.startswith(TMP), "проба обязана работать во временной базе"
    print(f"  DB_FILE = {config.DB_FILE}")
    print()

    # ------------------------------------------------------------------ S1
    print("S1. Нечитаемая колонка глазами читателей (вопросы читаемы)")
    _seed(REAL_ANSWERS, HHQuestionStatus.READY_TO_SUBMIT.value)
    _set_raw(BROKEN)
    print(f"  answers_json                = {_raw_answers()!r}")
    print(f"  пометка #46                 = {_marker()}")
    validate_reason = _validate_reason()
    audit_rationale = _audit_rationale()
    gate5 = _gate5()
    print(f"  validate_human_answers      = {validate_reason}")
    print(f"  audit_questionnaire         = {audit_rationale}")
    print(f"  гейт 5                      = {gate5}")
    named = (
        "answers_json" in validate_reason
        or "answers_json" in audit_rationale
        or "answers_unreadable" in gate5
    )
    print(f"  ВЕРДИКТ S1 = {'COLUMN-NAMED' if named else 'SPEAKS-ABOUT-THE-HUMAN'}")
    print("               (до правки: MARKER-PRESENT, но запись говорила о ЧЕЛОВЕКЕ)")
    print()

    # ------------------------------------------------------------------ S2
    print("S2. Писатель, получив прочитанное обратно (форма всех пяти вызовов)")
    quest = _quest()
    print(f"  что вернул читатель         = {json.dumps(quest.answers)}")
    db.update_hh_questionnaire_answers(
        QID, quest.answers, new_status=HHQuestionStatus.BLOCKED.value
    )
    after = _raw_answers()
    print(f"  answers_json после писателя = {after!r}")
    print(f"  пометка #46 после писателя  = {_marker()}")
    print(f"  ВЕРДИКТ S2 = {'EVIDENCE-ERASED' if after == '{}' else 'EVIDENCE-KEPT'}")
    print()

    # ------------------------------------------------------------------ S3
    print("S3. Контроль: колонка читаема, тот же писатель ничего не теряет")
    _seed(REAL_ANSWERS, HHQuestionStatus.READY_TO_SUBMIT.value)
    before = _raw_answers()
    quest = _quest()
    db.update_hh_questionnaire_answers(
        QID, quest.answers, new_status=HHQuestionStatus.BLOCKED.value
    )
    after = _raw_answers()
    print(f"  answers_json до              = {before!r}")
    print(f"  answers_json после           = {after!r}")
    print(f"  ВЕРДИКТ S3 = {'NOTHING-LOST' if after == before else 'LOST'}")
    print()

    # ------------------------------------------------------------------ S4
    print("S4. Контроль предохранителя: настоящие ответы поверх нечитаемой колонки")
    _seed(REAL_ANSWERS, HHQuestionStatus.READY_TO_SUBMIT.value)
    _set_raw(BROKEN)
    db.update_hh_questionnaire_answers(
        QID, REAL_ANSWERS, new_status=HHQuestionStatus.READY_TO_SUBMIT.value
    )
    after = _raw_answers()
    print(f"  answers_json после           = {after!r}")
    print(
        "  ВЕРДИКТ S4 = "
        + ("REPLACED-WITH-REAL" if after == json.dumps(REAL_ANSWERS, ensure_ascii=False) else "NOT-REPLACED")
    )
    print("  (предохранитель не имеет права отказывать всегда: живая правка обязана проходить)")
    print()

    # ------------------------------------------------------------------ S5
    print("S5. Путь раннера: его собственное выражение на нечитаемой колонке")
    _seed(REAL_ANSWERS, HHQuestionStatus.READY_TO_SUBMIT.value)
    _set_raw(BROKEN)
    _seed_app()
    quest_data = db.get_hh_questionnaire(QID) or {}
    app = db.get_hh_application(APP_ID) or {}
    human_answers = quest_data.get("answers") or app.get("answers") or {}
    print(f"  ключи строки заявки         = {sorted(app.keys())[:8]}...")
    print(f"  app.get('answers')          = {app.get('answers')!r}")
    print(f"  human_answers раннера       = {json.dumps(human_answers)}")
    if human_answers != {}:
        print("  ВЕРДИКТ S5 = НЕ ВОСПРОИЗВЕДЕНО (раннер получил непустые ответы)")
        print()
        return 1
    q_res = submit_questionnaire_response(
        questionnaire_id=QID,
        human_answers=human_answers,
        confirm_submit=True,
        evaluate_fn=lambda script: json.dumps({"ok": True, "clicked_button": "Откликнуться"}),
    )
    print(f"  submit_questionnaire_response: verdict={q_res.verdict} status={q_res.status}")
    after = _raw_answers()
    print(f"  answers_json после раннера  = {after!r}")
    print(f"  пометка #46 после раннера   = {_marker()}")
    print(f"  status после раннера        = {_status()}")
    print(
        "  ВЕРДИКТ S5 = "
        + ("RUNNER-PATH-ERASES-EVIDENCE" if after == "{}" else "EVIDENCE-KEPT")
    )
    print()

    print("=" * 78)
    print("Сводка: S1 читатели обязаны назвать колонку; S2 писатель обязан не стирать")
    print("        улику и не обнулять пометку; S3/S4 контроли «ничего не теряется» и")
    print("        «настоящие ответы проходят»; S5 путь раннера не стирает.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
