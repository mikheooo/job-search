#!/usr/bin/env python
"""Probe: what does gate 6 (language match) record when it never read the vacancy?

BLE001 triage, finding #52. Gate 6 builds the text it compares like this:

    vac_desc = ""
    vac_title = title                    # the APPLICATION's own title
    vac_row = db.get_vacancy_by_id(vac_stable_id) if vac_stable_id else None
    if vac_row:
        try:
            ... three branches that set vac_desc / vac_title ...
        except Exception:
            pass                         # silently, leaving no trace at all

    vac_text = f"{vac_title} {company} {vac_desc}".strip()
    lang_vac = detect_text_language(vac_text)
    lang_letter = detect_text_language(letter_text)

    if both named and different:  checks_failed += "language_match"
    elif one unknown:             checks_passed += "language_match_undetermined"
    else:                         checks_passed += "language_match"

The triage read that handler and called the direction safe: "vac_desc is empty,
so the language comes out unknown and the record is honest
(language_match_undetermined), not 'matched'". That premise is what is wrong here,
and this probe is what shows it: vac_title is NOT empty when vac_desc is - it
falls back to the APPLICATION's own title, and an application title names a
language far more often than not. So the gate compares the letter against the
package it is about to send, and when the two agree it writes "language_match" -
a PASSED check - for a vacancy it never opened.

MEASURED 2026-09-19 over the 1996 rows in state.db (by this probe's population
pass, `--population`, which is the same arithmetic the report quotes):

    text built the way gate 6 builds it     language named
    title + company + description            1959 of 1996   (98.1%)
    title + company alone (the fallback)      837 of 1996   (41.9%)

and the second row is the whole point: two rows in five would still name a
language from the fallback, so the false "matched" is not a rare corner.

There are three doors that leave vac_desc empty, and the bare `except` is only
the first:

    1. the row read raises        -> the handler swallows it
    2. the row matches no branch  -> falls out of the if/elif with no error
    3. there is no row at all     -> an empty vacancy_stable_id skips the lookup
                                     entirely, and a stale id finds nothing

Door 3 is not hypothetical: 5 of the 10 rows in hh_applications carry an empty
vacancy_stable_id. Doors 1 and 2 need a row object that is not the 25-column
tuple db.get_vacancy_by_id returns today, so they are hardening, not repair.

MEASURED BEFORE THE FIX (2026-09-19, by the red run of the six pins in
tests/test_ble001_fail_closed.py - this table is the pre-fix evidence and is NOT
what this file still reproduces):

    shape           row read              pre-fix record      should be
    healthy         real description      language_match      language_match
    unreadable      raised -> swallowed   language_match      gate CLOSED
    unknown_shape   no branch matched     language_match      gate CLOSED
    no_row          nothing to read       language_match      marked
    empty_id        lookup never made     language_match      marked
    no_description  read, but empty       language_match      marked

      shapes that never read a description                  : 5
      of those, the gate recorded "language_match"          : 5 of 5
      of those, a read was ATTEMPTED and failed             : 2
      of those, no vacancy was on record at all             : 3

The fix splits the verdicts the way #46 split them one gate up: a read that was
ATTEMPTED and failed closes the gate, while a vacancy that was never on record
only stops claiming the comparison happened. The handler is narrowed to the four
exceptions those three readers can actually raise, so an unexpected failure now
propagates - exactly as a failure of the get_vacancy_by_id call on the line above
already did - instead of being swallowed into an empty description.

WHAT THIS PROBE DOES NOT SHOW. That doors 1 and 2 occur in production: they need
a row shape that db.get_vacancy_by_id does not currently return. Door 3 is
measured against the real database above; doors 1 and 2 are injected here.

Usage:  ./.venv/Scripts/python.exe tools/ble001_gate6_probe.py
        ./.venv/Scripts/python.exe tools/ble001_gate6_probe.py --population
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ai_assistant import config, db
from ai_assistant.hh_submit_policy import detect_text_language, evaluate

# Long enough that detect_text_language names it (>=10 Cyrillic, and > Latin).
LETTER_RU = (
    "Здравствуйте! Меня заинтересовала эта позиция, и я хотел бы откликнуться. "
    "У меня большой опыт разработки на Python, проектирования асинхронных сервисов, "
    "работы с PostgreSQL, Docker и FastAPI, а также оптимизации тяжёлых запросов "
    "в высоконагруженных системах."
)
APP_TITLE = "Разработчик бэкенда на Python, удалённая работа"
COMPANY = "Максима Технологии"
DESCRIPTION_RU = (
    "Требуется разработчик бэкенда. Вы будете проектировать асинхронные сервисы "
    "на Python, работать с PostgreSQL, Docker и FastAPI, а также оптимизировать "
    "тяжёлые запросы в высоконагруженных системах."
)


class UnindexableMapping:
    """Door 1: has .keys(), so gate 6 takes the mapping branch - and refuses."""

    def keys(self):
        return ["description", "title"]

    def __getitem__(self, key):
        raise TypeError(f"{key!r} cannot be read")


# What db.get_vacancy_by_id should hand gate 6 for each shape. None means "let
# the real lookup run" (so the shape is decided by what is in the table).
SHAPES: dict[str, object] = {
    "healthy": None,
    "unreadable": UnindexableMapping(),
    # Door 2: what a narrowed SELECT would return - five columns, no branch.
    "unknown_shape": ("hh:p52", "hh", "p52", APP_TITLE, COMPANY),
    "no_row": None,
    "empty_id": None,
    "no_description": None,
}

# The three verdicts a shape can earn, and which one is correct for it.
EXPECT = {
    "healthy": "match",
    "unreadable": "closed",
    "unknown_shape": "closed",
    "no_row": "marked",
    "empty_id": "marked",
    "no_description": "marked",
}

# The vacancy row each shape needs, if any. "" is a row with no description.
ROWS = {
    "healthy": DESCRIPTION_RU,
    "no_description": "",
}


def app_for(shape: str) -> dict:
    # An empty vacancy id is door 3's second way in: the lookup is skipped.
    sid = "" if shape == "empty_id" else "hh:p52"
    return {
        "application_id": "app_probe52",
        "vacancy_stable_id": sid,
        "title": APP_TITLE,
        "employer": COMPANY,
        "draft": LETTER_RU,
    }


def insert_vacancy(description: str) -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO vacancies
                (stable_id, source, source_job_id, title, company, description,
                 job_url, state)
            VALUES ('hh:p52', 'hh', 'p52', ?, ?, ?, 'https://hh.ru/vacancy/p52', 'NEW')
            """,
            (APP_TITLE, COMPANY, description),
        )
        conn.commit()
    finally:
        conn.close()


def clear_vacancies() -> None:
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM vacancies")
        conn.commit()
    finally:
        conn.close()


def run_shape(shape: str) -> tuple[str, str, str, str]:
    """Drive the real evaluate() and report which language record it produced."""
    stub = SHAPES[shape]
    looked_up: list[str] = []
    original = db.get_vacancy_by_id

    def fake(sid):
        looked_up.append(sid)
        return stub if stub is not None else original(sid)

    db.get_vacancy_by_id = fake
    try:
        decision = evaluate(app_for(shape))
    finally:
        db.get_vacancy_by_id = original

    if "language_match" in decision.checks_failed:
        verdict = "closed"
    elif "language_match" in decision.checks_passed:
        verdict = "match"
    elif "language_match_without_description" in decision.checks_passed:
        verdict = "marked"
    else:
        verdict = "none"

    read_ok = "yes" if shape == "healthy" else ("read, empty" if shape == "no_description" else "no")
    if shape == "empty_id":
        read_ok = f"no (lookup skipped: {len(looked_up)} calls)"
    elif shape in {"unreadable", "unknown_shape"}:
        read_ok = "attempted, failed"
    elif shape == "no_row":
        read_ok = "looked up, absent"

    reason = next((r for r in decision.reasons if "language" in r or "vacancy_row" in r), "")
    return verdict, read_ok, reason, str(len(looked_up))


def population() -> int:
    """The 41.9% the report quotes, recomputed against the real state.db."""
    path = REPO / "state.db"
    if not path.is_file():
        print(f"no {path} - skipping the population pass")
        return 0
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT title, company, description FROM vacancies").fetchall()
    finally:
        conn.close()
    total = len(rows)
    full = sum(
        1
        for t, c, d in rows
        if detect_text_language(f"{t or ''} {c or ''} {d or ''}") != "unknown"
    )
    fallback = sum(
        1 for t, c, _d in rows if detect_text_language(f"{t or ''} {c or ''} ") != "unknown"
    )
    print(f"population pass over {path.name}: {total} rows")
    print(f"  title + company + description names a language : {full:5} ({full / total * 100:.1f}%)")
    print(f"  title + company alone (the fallback)           : {fallback:5} ({fallback / total * 100:.1f}%)")
    print()
    return 0


def main() -> int:
    if "--population" in sys.argv:
        return population()

    tmp = Path(tempfile.mkdtemp(prefix="ble001_gate6_"))
    config.DB_FILE = str(tmp / "probe52.db")
    db.init_db()

    print(f"db: {config.DB_FILE}")
    print("gate 6 driven through the real evaluate(); one shape per vacancy state\n")
    header = f"{'shape':14s} {'row read':19s} {'verdict':8s} {'expected':9s} {'lookup':7s} reason"
    print(header)
    print("-" * len(header))

    rows = []
    for shape in SHAPES:
        clear_vacancies()
        if shape in ROWS:
            insert_vacancy(ROWS[shape])
        verdict, read_ok, reason, calls = run_shape(shape)
        rows.append((shape, verdict, EXPECT[shape], reason, calls))
        print(
            f"{shape:14s} {read_ok:19s} {verdict:8s} {EXPECT[shape]:9s} {calls:7s} {reason}"
        )

    print()
    never_read = [r for r in rows if r[0] != "healthy"]
    unearned = [r for r in never_read if r[1] == "match"]
    wrong = [r for r in rows if r[1] != r[2]]

    print("reading:")
    print(f"  shapes that never read a description             : {len(never_read)}")
    print(f"  of those, the gate recorded 'language_match'     : {len(unearned)} of {len(never_read)}")
    print(f"  read failures that closed the gate               : "
          f"{sum(1 for r in rows if r[0] in {'unreadable', 'unknown_shape'} and r[1] == 'closed')} of 2")
    print(f"  never-read vacancies that closed the gate        : "
          f"{sum(1 for r in rows if r[0] in {'no_row', 'empty_id', 'no_description'} and r[1] == 'closed')} of 3")
    print(f"  healthy rows still recording 'language_match'    : "
          f"{sum(1 for r in rows if r[0] == 'healthy' and r[1] == 'match')} of 1")
    print()

    if wrong:
        print("RESULT: a shape landed on the wrong verdict:")
        for shape, verdict, expected, reason, _c in wrong:
            print(f"        {shape}: got {verdict}, expected {expected} ({reason})")
        return 1
    if unearned:
        print("RESULT: the gate still records 'language_match' for a vacancy it never read.")
        return 1
    print("RESULT: fail-closed confirmed - a failed read closes the gate, a vacancy")
    print("        that was never on record is marked, and a real comparison still")
    print("        records 'language_match'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
