#!/usr/bin/env python
"""Probe: what a partial dict does to an hh_applications row, and to answers.

Backs findings #40, #41 and #42 in docs/ble001_triage.md. All three are about the
same family of defect: a writer whose *partial* input means something the reader
did not intend.

#40 - save_hh_application() is an upsert whose SET clause mixes
    COALESCE(excluded.x, existing.x) with a bare `x = excluded.x`. A dict that
    names only some columns therefore clobbers the bare ones. Measured below.

#41 - the answers column used to be filled by three inlined lines that were
    wrong in two ways: a dict passed as `answers_json` was serialised from
    `answers` (the other field) and stored "{}", and a dict passed as `answers`
    never reached the column at all. Both call sites now share
    db._serialise_answers_json(). Measured below - including the case a naive
    fix would have broken, where an EMPTY answers dict must keep writing NULL so
    that the upsert's COALESCE cannot erase stored answers.

Nothing here touches production: DB_FILE is redirected to a temp directory
*before* ai_assistant.config is imported, and the redirect is asserted.

Usage:  ./.venv/Scripts/python.exe tools/hh_row_upsert_probe.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="hh_row_upsert_")
os.environ["DB_FILE"] = os.path.join(_TMP, "probe.db")
os.environ["VACANCIES_FILE"] = os.path.join(_TMP, "vacancies.json")
os.environ["LOGS_DIR"] = os.path.join(_TMP, "logs")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_assistant import config, db  # noqa: E402

ANSWERS = {"q1": "A"}
QUESTIONS = [{
    "question_id": "q1", "text": "Where?", "question_type": "radio",
    "required": True, "options": ["A", "B"],
}]


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _raw_questionnaire_answers(qid: str):
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT answers_json FROM hh_questionnaires WHERE questionnaire_id = ?",
            (qid,),
        ).fetchone()
        return row[0] if row else "<no row>"
    finally:
        conn.close()


def measure_partial_upsert() -> bool:
    """#40: which columns does a two-key dict clobber?"""
    _section("#40  a partial dict against a full hh_applications row")
    print(f"DB_FILE = {config.DB_FILE}")
    if "hh_row_upsert_" not in config.DB_FILE:
        raise SystemExit("refusing to run: DB_FILE is not the temp copy")

    db.init_db()
    db.save_hh_application({
        "application_id": "app_probe_1",
        "vacancy_stable_id": "hh:777001",
        "title": "Python developer",
        "employer": "Test Employer",
        "state": "READY_TO_SUBMIT",
        "draft": "draft text",
        "questionnaire_id": "quest_old",
        "answers_json": '{"q1": "yes"}',
        "error": "previous failure",
        "last_transition_reason": "11 gates passed",
    })
    before = db.get_hh_application("app_probe_1")

    # the obvious way to attach a questionnaire
    db.save_hh_application({
        "application_id": "app_probe_1",
        "questionnaire_id": "quest_new",
    })
    after = db.get_hh_application("app_probe_1")

    coalesce = {"conversation_id", "vacancy_stable_id", "title", "employer",
                "draft", "questionnaire_id", "answers_json", "answers"}
    bare = {"state", "error", "last_transition_reason", "updated_at"}

    print(f"{'column':<24} {'before':<20} {'after':<20} clause")
    # get_hh_application() returns the parsed `answers` key, not the raw
    # answers_json column, so that is what a reader of the row actually sees.
    for col in ("state", "error", "last_transition_reason", "title",
                "questionnaire_id", "answers"):
        clause = "COALESCE" if col in coalesce else ("bare" if col in bare else "-")
        print(f"{col:<24} {str(before.get(col))[:18]:<20}"
              f" {str(after.get(col))[:18]:<20} {clause}")

    # updated_at is bare too, but it is *meant* to change on every write, so it
    # is not part of the damage - listing it would overstate the finding.
    clobbered = [c for c in sorted(bare - {"updated_at"})
                 if before.get(c) != after.get(c)]
    print()
    print(f"[KEY] columns clobbered by the partial dict: {clobbered}"
          f"  (updated_at also changes, by design)")
    print(f"[KEY] state rewound to NEW: {after['state'] == 'NEW'}")
    print(f"[KEY] error erased: {after['error'] is None}")
    print(f"[KEY] last_transition_reason erased: {after['last_transition_reason'] is None}")

    # the narrow call the collection path actually uses
    db.save_hh_application({
        "application_id": "app_probe_1",
        "state": "READY_TO_SUBMIT",
        "error": "previous failure",
        "last_transition_reason": "11 gates passed",
    })
    db.set_hh_application_questionnaire("app_probe_1", "quest_narrow")
    narrow = db.get_hh_application("app_probe_1")
    inert = (narrow["state"] == "READY_TO_SUBMIT"
             and narrow["error"] == "previous failure"
             and narrow["last_transition_reason"] == "11 gates passed")
    print(f"[KEY] set_hh_application_questionnaire() is inert apart from its two"
          f" columns: {inert}")
    print(f"[KEY] it reports whether a row was touched:"
          f" {db.set_hh_application_questionnaire('app_missing', 'q') is False}")

    return (after["state"] == "NEW" and after["error"] is None
            and after["last_transition_reason"] is None and inert)


def measure_answers_channel() -> bool:
    """#41: which shapes of 'answers' reach the column, and does empty wipe?"""
    _section("#41  the answers channel")

    def case(label: str, payload: dict, qid: str) -> bool:
        db.save_hh_questionnaire(payload)
        got = db.get_hh_questionnaire(qid)["answers"]
        ok = got == ANSWERS
        print(f"  {'OK  ' if ok else 'LOST'} {label:<46} -> {got}")
        return ok

    shapes = [
        case("answers=<dict>, no answers_json",
             {"questionnaire_id": "quest_a", "questions": QUESTIONS,
              "answers": ANSWERS}, "quest_a"),
        case("answers_json=<dict>",
             {"questionnaire_id": "quest_b", "questions": QUESTIONS,
              "answers_json": ANSWERS}, "quest_b"),
        case("answers_json=<json string>",
             {"questionnaire_id": "quest_c", "questions": QUESTIONS,
              "answers_json": json.dumps(ANSWERS)}, "quest_c"),
    ]

    from ai_assistant.hh_questionnaire import HHQuestionnaire
    dumped = HHQuestionnaire(questionnaire_id="quest_e",
                             questions=QUESTIONS, answers=ANSWERS).model_dump()
    shapes.append(case("HHQuestionnaire.model_dump()", dumped, "quest_e"))
    print(f"  [KEY] 'answers_json' in model_dump(): {'answers_json' in dumped}"
          "  (absent, so the column is filled from `answers`)")

    print()
    print("  raw answers_json column:")
    for qid in ("quest_a", "quest_b", "quest_c", "quest_e"):
        print(f"    {qid:<10} {_raw_questionnaire_answers(qid)!r}")

    # The case a naive fix breaks: an EMPTY answers dict must keep writing NULL.
    # "{}" is not NULL, so it would win COALESCE on an update and erase answers.
    db.save_hh_questionnaire({"questionnaire_id": "quest_d", "questions": QUESTIONS,
                              "answers_json": json.dumps(ANSWERS)})
    db.save_hh_questionnaire({"questionnaire_id": "quest_d", "questions": QUESTIONS})
    kept = db.get_hh_questionnaire("quest_d")["answers"] == ANSWERS
    print()
    print(f"  [KEY] a questions-only re-save leaves stored answers alone: {kept}")
    print("        (COALESCE saves it: excluded.answers_json is NULL, not '{}')")

    db.save_hh_questionnaire({"questionnaire_id": "quest_g", "questions": QUESTIONS,
                              "answers": {}})
    empty_is_null = _raw_questionnaire_answers("quest_g") is None
    print(f"  [KEY] an empty answers dict stores NULL, not '{{}}': {empty_is_null}")

    # per-question answers ride inside questions_json - a different channel
    db.save_hh_questionnaire(HHQuestionnaire(
        questionnaire_id="quest_f",
        questions=[{**QUESTIONS[0], "answer": "A"}],
    ).model_dump())
    per_q = db.get_hh_questionnaire("quest_f")["questions"][0].get("answer") == "A"
    print(f"  [KEY] HHQuestionItem.answer still survives (a different channel): {per_q}")

    # the same three inlined lines lived in save_hh_application as well
    db.save_hh_application({
        "application_id": "app_probe_ans",
        "vacancy_stable_id": "hh:777002",
        "answers": ANSWERS,
    })
    app_ans = db.get_hh_application("app_probe_ans")["answers"] == ANSWERS
    print(f"  [KEY] the same channel in save_hh_application: {app_ans}")

    print()
    print(f"  [KEY] every documented shape reaches the column: {all(shapes)}")
    return all(shapes) and kept and empty_is_null and per_q and app_ans


def main() -> int:
    ok40 = measure_partial_upsert()
    ok41 = measure_answers_channel()

    print()
    print("RESULT")
    print(f"  [{'OK' if ok40 else 'FAIL'}] #40 the partial dict rewinds state and"
          f" erases error + last_transition_reason")
    print(f"  [{'OK' if ok41 else 'FAIL'}] #41 every shape of `answers` reaches the"
          f" column, and an empty one still writes NULL")

    shutil.rmtree(_TMP, ignore_errors=True)
    gone = not os.path.exists(_TMP)
    if not gone:
        # ignore_errors=True hides a locked file: say so instead of pretending
        print(f"  [WARN] temp dir survived cleanup: {_TMP}")
    else:
        print("  [OK] temp db removed")
    return 0 if (ok40 and ok41 and gone) else 1


if __name__ == "__main__":
    raise SystemExit(main())
