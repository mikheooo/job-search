#!/usr/bin/env python
"""Mutation check for finding #60: do the pins actually see the erased answers?

The fix is one argument at one call site:

  * ``hh_application_runner.py`` wrote ``update_hh_questionnaire_answers(qid, {},
    new_status=SUBMITTED)`` in the "submission confirmed" branch. That writer is a
    SETTER, so the line overwrote ``answers_json`` with ``"{}"`` right after
    ``submit_questionnaire_response()`` had stored the answers the run actually
    sent - the second write's only effect was to erase the first. It now writes
    ``human_answers`` back.

Mutations, one mistake each:

  * **M0** restores the empty literal - the pre-fix shape, so the pins are read
    against the code rather than against my description of it.
  * **M1** keeps the answers but records the wrong status. The erasure pins would
    not notice, which is why the "keeps the answers" pin also asserts SUBMITTED.
  * **M2** makes ``db._row_to_questionnaire`` call an empty ``{}`` unreadable. The
    blindness pin exists to hold the opposite: ``"{}"`` IS valid JSON, so the #46
    guard stays silent on it, and the fix does not pretend otherwise.
  * **M3** turns ``db.update_hh_questionnaire_answers`` into a MERGER. The setter
    pin holds that it is not one: if merging were the contract, the empty literal
    would be harmless and the defect would be somewhere else.
  * **M4** replaces the write with ``pass``. The behavioural pins survive that (the
    questionnaire module already persisted the answers), so the structural pin is
    the one that has to catch it - which is what it is for. First attempt was to
    delete the line outright; that left ``if qid:`` with an empty body, the module
    stopped importing, the junit carried no testcase and the tool answered
    "NO VERDICT". An unparseable mutation is not a mutation.

Everything is restored byte-for-byte and the hashes are printed; the tool refuses
to report a verdict it could not restore. Verdicts come from --junitxml, not from
the console: the bulk-delete guard fires during pytest's own cleanup, after the
last test has run and before the summary prints. A pin missing from the junit is
UNKNOWN, never PASS.

Two files with different line endings are mutated, so every anchor is built with
its own newline (``db.py`` is LF, ``hh_application_runner.py`` is CRLF).

Usage:
    ./.venv/Scripts/python.exe tools/ble001_questionnaire_answers_mutation.py
    ./.venv/Scripts/python.exe tools/ble001_questionnaire_answers_mutation.py M0 M1
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
TESTS = "tests/test_ble001_fail_closed.py"

RUNNER = REPO / "ai_assistant" / "hh_application_runner.py"
DB = REPO / "ai_assistant" / "db.py"

PIN_KEEPS = "test_a_confirmed_submission_keeps_the_answers_the_questionnaire_was_submitted_with"
PIN_READERS = "test_the_erased_row_stops_supporting_the_claim_that_it_was_answered"
PIN_SHAPE = "test_the_runner_does_not_pass_an_empty_answers_literal_to_the_writer"
PIN_BLIND = "test_a_questionnaire_that_was_never_answered_still_reads_as_unanswered"
PIN_SETTER = "test_the_writer_is_a_setter_not_a_merger_so_the_argument_is_the_defect"

SHORT = {
    PIN_KEEPS: "keeps_answers",
    PIN_READERS: "readers_agree",
    PIN_SHAPE: "shape_no_empty",
    PIN_BLIND: "ctrl_guard_blind",
    PIN_SETTER: "ctrl_setter",
}

ERASURE_PINS = [PIN_KEEPS, PIN_READERS, PIN_SHAPE]

LF = chr(10)
CRLF = chr(13) + chr(10)


def L(*lines: str, nl: str = LF) -> str:
    """Join lines with the file's own newline and terminate the last one."""
    return nl.join(lines) + nl


# --- anchors, verbatim. A mismatch means the fix moved, and the tool says so
# rather than reporting a clean run. ------------------------------------------

CALL_FIXED = L(
    "            db.update_hh_questionnaire_answers(",
    "                qid, human_answers, new_status=HHQuestionStatus.SUBMITTED.value",
    "            )",
    nl=CRLF,
)
CALL_EMPTY = L(
    "            db.update_hh_questionnaire_answers(qid, {}, "
    "new_status=HHQuestionStatus.SUBMITTED.value)",
    nl=CRLF,
)
CALL_WRONG_STATUS = L(
    "            db.update_hh_questionnaire_answers(",
    "                qid, human_answers, new_status=HHQuestionStatus.READY_TO_SUBMIT.value",
    "            )",
    nl=CRLF,
)

ANSWERS_READ = L(
    "        except Exception as exc:",
    "            answers = {}",
    '            answers_error = f"answers_json is not readable as JSON: {exc}"',
)
ANSWERS_EMPTY_IS_ERROR = ANSWERS_READ + L(
    "        if answers == {}:",
    '            answers_error = "answers_json is empty"',
)

SETTER_LINE = L(
    "    answers_json = json.dumps(answers, ensure_ascii=False)",
)
MERGER = L(
    "    if not answers:",
    "        _c0 = get_connection()",
    "        _r0 = _c0.execute(",
    '            "SELECT answers_json FROM hh_questionnaires WHERE questionnaire_id=?",',
    "            (str(questionnaire_id).strip(),),",
    "        ).fetchone()",
    "        _c0.close()",
    "        answers = json.loads(_r0[0]) if _r0 and _r0[0] else {}",
    "    answers_json = json.dumps(answers, ensure_ascii=False)",
)

# (name, description, path, old, new, pins)
MUTATIONS = [
    (
        "M0",
        "restore the empty answers literal - the pre-fix shape",
        RUNNER,
        CALL_FIXED,
        CALL_EMPTY,
        ERASURE_PINS,
    ),
    (
        "M1",
        "write the answers back but record the wrong status",
        RUNNER,
        CALL_FIXED,
        CALL_WRONG_STATUS,
        [PIN_KEEPS],
    ),
    (
        "M2",
        "an empty {} answers column counts as unreadable",
        DB,
        ANSWERS_READ,
        ANSWERS_EMPTY_IS_ERROR,
        [PIN_BLIND],
    ),
    (
        "M3",
        "make the writer a MERGER instead of a setter",
        DB,
        SETTER_LINE,
        MERGER,
        [PIN_SETTER],
    ),
    (
        "M4",
        "replace the write with pass",
        RUNNER,
        CALL_FIXED,
        L("            pass", nl=CRLF),
        [PIN_SHAPE],
    ),
]

FILES = [RUNNER, DB]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin_tag(node_ids: list[str]) -> str:
    """A stable, filesystem-safe tag for a pin set - one junit file per set.

    Deliberately no unlink(): every deletion counts against the repo's
    bulk-delete budget, and that budget is what killed a mutation run in the
    middle of M1 on 2026-09-20. Overwriting a junit is free; deleting one is not.
    """
    return hashlib.sha1("|".join(sorted(node_ids)).encode()).hexdigest()[:8]


def run_pins(node_ids: list[str]) -> tuple[dict[str, str], dict[str, tuple[int, int]], bool]:
    """Return ({pin: PASS/FAIL/UNKNOWN}, {pin: (failed_cases, total)}, measured)."""
    expr = " or ".join(node_ids)
    xml = REPO / f".pytest_q60_{pin_tag(node_ids)}.xml"
    proc = subprocess.run(
        [str(VENV_PY), "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider",
         "-k", expr, "--no-header", "-rf", f"--junitxml={xml}"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    verdicts = dict.fromkeys(node_ids, "UNKNOWN")
    counts: dict[str, tuple[int, int]] = {p: (0, 0) for p in node_ids}
    if xml.exists():
        root = ElementTree.parse(xml).getroot()
        tally = {p: [0, 0] for p in node_ids}  # [failed, total]
        for case in root.iter("testcase"):
            name = case.get("name", "")
            broken = any(child.tag in ("failure", "error") for child in case)
            for pin in node_ids:
                if name == pin or name.startswith(f"{pin}["):
                    tally[pin][1] += 1
                    if broken:
                        tally[pin][0] += 1
        for pin in node_ids:
            failed, total = tally[pin]
            counts[pin] = (failed, total)
            if failed:
                verdicts[pin] = "FAIL"
            elif total:
                verdicts[pin] = "PASS"
    else:
        out = proc.stdout + proc.stderr
        print(f"     !! no junit written (returncode {proc.returncode}); "
              f"output tail: {(out[-300:] or '<empty>')!r}")
    return verdicts, counts, all(v != "UNKNOWN" for v in verdicts.values())


def main() -> int:
    # Accept mutation names as arguments: `... M0 M1` runs just those. Several
    # pytest runs inside one shell invocation exhaust the bulk-delete budget, so
    # running them a few at a time is a supported way to get an honest verdict.
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    mutations = [m for m in MUTATIONS if not only or m[0] in only]

    originals = {p: p.read_bytes() for p in FILES}
    before = {p.name: sha(p) for p in FILES}

    for name, digest in before.items():
        print(f"{name} sha256 before: {digest}")
    print(f"{len(mutations)} of {len(MUTATIONS)} mutations"
          f"{' (' + ', '.join(only) + ')' if only else ''}\n")
    header = f"{'mut':4s} {'verdict':9s} what changed"
    print(header)
    print("-" * len(header))

    failures = []
    try:
        for name, description, path, old, new, pins in mutations:
            source = originals[path]
            count = source.count(old.encode())
            if count != 1:
                print(
                    f"{name:4s} !! anchor appears {count} times in {path.name}, "
                    f"expected 1: {old.splitlines()[0]!r}"
                )
                failures.append(name)
                continue
            mutated = source.replace(old.encode(), new.encode(), 1)
            if mutated == source:
                print(f"{name:4s} !! mutation changed nothing")
                failures.append(name)
                continue

            path.write_bytes(mutated)
            try:
                verdicts, counts, reported = run_pins(pins)
            finally:
                path.write_bytes(originals[path])

            if not reported:
                print(f"{name:4s} !! NO VERDICT - mutation left unresolved, not counted")
                failures.append(name)
                continue
            # Caught means SOME listed pin failed, not all of them: the question
            # is whether the pin set is blind to this mistake, and one failing
            # pin answers it.
            caught = any(verdicts[p] == "FAIL" for p in pins)
            status = "caught" if caught else "SURVIVED"
            print(f"{name:4s} {status:9s} {description}")
            evidence = []
            for p in pins:
                failed, total = counts[p]
                suffix = f"({failed}/{total})" if total > 1 else ""
                evidence.append(f"{SHORT[p]}={verdicts[p]}{suffix}")
            print(f"     evidence: {', '.join(evidence)}")
            if not caught:
                failures.append(name)
    finally:
        for path, blob in originals.items():
            path.write_bytes(blob)

    after = {p.name: sha(p) for p in FILES}
    print()
    for name, digest in after.items():
        print(f"{name} sha256 after : {digest}")
    restored = after == before
    print(f"restored byte-for-byte: {restored}")
    if not restored:
        return 2

    print()
    if failures:
        print(f"RESULT: {len(failures)} mutation(s) survived: {', '.join(failures)}")
        return 1
    print(f"RESULT: all {len(mutations)} mutations caught, tree restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
