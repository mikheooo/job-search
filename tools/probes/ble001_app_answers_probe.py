#!/usr/bin/env python
"""Probe: does an unreadable hh_applications.answers_json open any gate?

BLE001 triage - the address left open in docs/ble001_triage.md as
``_row_to_application`` (ai_assistant/db.py:1132). The reader does this:

    answers = {}
    if row[8]:
        try:
            answers = json.loads(row[8])
        except Exception:
            answers = {}

Unlike ``_row_to_questionnaire``, which finding #46 gave a ``questions_error`` /
``answers_error`` marker, this reader cannot tell "no answers stored" from "answers
stored but unreadable" - both come out as ``{}``. That is only a defect if some
consumer treats the two differently, so the question is not "is the marker
missing" but "does the direction close".

Two consumers read the application's answers:

    hh_submit_policy.py:309       answers = app.get("answers") or quest.answers or {}
    hh_application_runner.py:531  human_answers = quest_data.get("answers") or app.get("answers") or {}

Both put the answers in the BODY of a loop over the questionnaire's questions (the
same asymmetry as #46): an empty dict makes every required question look
unanswered, so the gate should CLOSE, not open. Note the precedence differs - the
policy prefers the application's answers, the runner prefers the questionnaire's -
so the runner only consults this column when the questionnaire stored none.

This probe measures that instead of arguing it. Four shapes of the column, with a
questionnaire that has one required question and no stored answers of its own (so
the application's column is the only source), and both the gate and the validator
are asked for a verdict. The "complete" shape is what keeps the probe honest: a
gate that refuses everything would satisfy the other three.

Nothing here touches production: DB_FILE is redirected to a temp directory
*before* ai_assistant.config is imported, and the redirect is asserted.

Usage:  ./.venv/Scripts/python.exe tools/ble001_app_answers_probe.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ble001_app_answers_")
os.environ["DB_FILE"] = os.path.join(_TMP, "probe.db")
os.environ["VACANCIES_FILE"] = os.path.join(_TMP, "vacancies.json")
os.environ["LOGS_DIR"] = os.path.join(_TMP, "logs")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_assistant import config, db
from ai_assistant.hh_questionnaire import (
    HHQuestionnaire,
    validate_human_answers,
)

QID = "probe-q-answers"
QUESTIONS = [
    {
        "question_id": "q1",
        "text": "How many years of Python experience do you have?",
        "question_type": "text",
        "options": [],
        "required": True,
    }
]

# The four shapes of hh_applications.answers_json. The first three must all close
# the gate; the last one must open it, or the probe proves nothing.
SHAPES: list[tuple[str, str | None]] = [
    ("unreadable", "{not json"),
    ("NULL", None),
    ("empty dict", "{}"),
    ("complete", json.dumps({"q1": "5 years"})),
]


def _insert_questionnaire_row(qid: str) -> None:
    """A readable questionnaire with one required question and no stored answers."""
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO hh_questionnaires (
                questionnaire_id, vacancy_stable_id, conversation_id, title, employer,
                fingerprint, questions_json, answers_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                qid,
                f"hh:{qid}",
                f"conv-{qid}",
                "Python Developer",
                "Acme",
                "fp",
                json.dumps(QUESTIONS),
                None,
                "READY_TO_SUBMIT",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_application_row(app_id: str, qid: str, answers_json: str | None) -> None:
    """An application row whose answers_json holds exactly what we chose.

    save_hh_application() serialises the column itself, so a corrupt value cannot
    be produced through it - but a row already in the database can hold anything,
    and that is the row the reader reads.
    """
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO hh_applications (
                application_id, vacancy_stable_id, title, employer, state,
                draft, questionnaire_id, answers_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                app_id,
                f"hh:{qid}",
                "Python Developer",
                "Acme",
                "READY_TO_SUBMIT",
                "",
                qid,
                answers_json,
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _gate_verdict(app: dict) -> tuple[list[str], list[str], list[str]]:
    """Run the real gate on the reader's own output, and return its gate-5 outcome."""
    from ai_assistant.hh_submit_policy import evaluate

    decision = evaluate(app)
    passed = [c for c in decision.checks_passed if c.startswith("questionnaire")]
    failed = [c for c in decision.checks_failed if c.startswith("questionnaire")]
    return passed, failed, list(decision.reasons)


def measure() -> bool:
    print(f"DB_FILE = {config.DB_FILE}")
    if "ble001_app_answers_" not in config.DB_FILE:
        raise SystemExit("refusing to run: DB_FILE is not the temp copy")

    db.init_db()
    _insert_questionnaire_row(QID)

    print()
    print("hh_applications.answers_json -> what the reader reports, and what the gates say")
    print("-" * 84)
    print(f"{'shape':<12} {'reader gives':<16} {'gate 5':<10} {'validator':<22} {'answers in the record?'}")
    print("-" * 84)

    closes: dict[str, bool] = {}
    for shape, raw in SHAPES:
        app_id = f"app-{shape.replace(' ', '-')}"
        _insert_application_row(app_id, QID, raw)

        app = db.get_hh_application(app_id)
        if app is None:
            raise SystemExit(f"the reader returned None for {app_id} - probe is broken")

        passed, _failed, reasons = _gate_verdict(app)
        quest = HHQuestionnaire(**db.get_hh_questionnaire(QID))
        res = validate_human_answers(quest, app["answers"])

        gate = "PASS" if "questionnaire_complete" in passed else "REFUSED"
        validator = f"{'ok' if res.ok else 'refused'} / {res.status}"
        # The honest limitation of this reader: there is no marker to print, so
        # "unreadable" and "empty" are indistinguishable by construction.
        marker = "none - no error marker" if app["answers"] == {} else "n/a"

        print(f"{shape:<12} {app['answers']!s:<16} {gate:<10} {validator:<22} {marker}")
        if reasons:
            print(f"{'':<12} reasons: {reasons}")

        closes[shape] = (gate == "REFUSED") and (res.ok is False)

    print()
    print("=== verdict ===")
    for shape in ("unreadable", "NULL", "empty dict"):
        print(f"  {shape:<12} closes the gate : {closes[shape]}")
    print(f"  {'complete':<12} opens the gate  : {not closes['complete']}")

    sealed = all(closes[s] for s in ("unreadable", "NULL", "empty dict"))
    honest = not closes["complete"]
    print()
    print(f"  every unreadable/empty shape is fail-closed : {sealed}")
    print(f"  a complete answers column still passes      : {honest}")
    print(
        "  NOTE: the column carries no error marker, so 'unreadable' and 'empty'"
        " are the same shape to every consumer."
    )
    return sealed and honest


def main() -> int:
    ok = measure()
    print()
    print("RESULT:", "PASS - the direction closes" if ok else "FAIL - see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
