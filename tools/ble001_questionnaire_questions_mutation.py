"""Paired mutations for BLE001 finding #46.

Finding #46: ``db._row_to_questionnaire`` turned an unreadable ``questions_json``
into an empty question list, and three gates that decide "may we submit?" all
iterate that list - so all three answered "fine" without looking. The pins in
``tests/test_ble001_fail_closed.py`` assert the refusals; this script proves the
pins can fail, because a pin that cannot fail is a comment.

Every mutation is applied to a copy of the real source file, the named pins are
run against it, and the file is restored byte-for-byte in a ``finally`` block.
The script refuses to report success if any file changed, and it prints the
before/after hash of every file it touches.

Pairs matter more than single mutations here. Three of the mutations break the
*guard* (M1, M3, M5, M6) and three break the *positive direction* (M2, M4, M7).
Without the second kind, "the tests are green" would also be satisfied by an
audit that refuses everything and a reader that flags every row.

Usage:
    python tools/ble001_questionnaire_questions_mutation.py

Exit code is 0 only when every mutation was caught by the pins it names.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
TEST_FILE = "tests/test_ble001_fail_closed.py"

DB = "ai_assistant/db.py"
QUEST = "ai_assistant/hh_questionnaire.py"
AUDIT = "ai_assistant/hh_questionnaire_audit.py"
POLICY = "ai_assistant/hh_submit_policy.py"


def pin(name: str) -> str:
    return f"{TEST_FILE}::{name}"


PINS = {
    "reader": pin("test_the_reader_reports_an_unreadable_questions_column"),
    "empty": pin("test_a_genuinely_empty_question_list_is_not_flagged"),
    "audit_bad": pin("test_unreadable_questions_never_audit_as_safe_to_submit"),
    "audit_ctl": pin("test_readable_questions_without_answers_still_need_correction"),
    "audit_ok": pin("test_a_fully_answered_questionnaire_still_audits_as_safe"),
    "validate_bad": pin("test_unreadable_questions_block_answer_validation"),
    "validate_ctl": pin("test_readable_questions_without_answers_still_block_validation"),
    "policy_bad": pin("test_unreadable_questions_fail_the_submit_policy_gate"),
    "policy_ctl": pin("test_readable_questions_without_answers_fail_the_submit_policy_gate"),
}

# (name, file, old, new, pins that must go red)
MUTATIONS: list[tuple[str, str, str, str, list[str]]] = [
    (
        "M1 reader stops reporting the decode failure",
        DB,
        """        except Exception as exc:
            questions = []
            questions_error = f"questions_json is not readable as JSON: {exc}"
""",
        """        except Exception:
            questions = []
            questions_error = None
""",
        [PINS["reader"], PINS["audit_bad"], PINS["validate_bad"], PINS["policy_bad"]],
    ),
    (
        "M2 reader treats an honestly empty list as unreadable",
        DB,
        """            questions = json.loads(row[6])
        except Exception as exc:
""",
        """            questions = json.loads(row[6])
            if not questions:
                raise ValueError("empty question list")
        except Exception as exc:
""",
        [PINS["empty"]],
    ),
    (
        "M3 audit stops refusing an unreadable questionnaire",
        AUDIT,
        "    if quest.questions_error:\n",
        "    if quest.questions_error and False:\n",
        [PINS["audit_bad"]],
    ),
    (
        "M4 audit refuses everything",
        AUDIT,
        "    overall = OverallAuditStatus.SAFE_TO_SUBMIT if all_pass else OverallAuditStatus.NEEDS_CORRECTION\n",
        "    overall = OverallAuditStatus.NEEDS_CORRECTION\n",
        [PINS["audit_ok"]],
    ),
    (
        "M5 answer validation stops refusing an unreadable questionnaire",
        QUEST,
        "    if questionnaire.questions_error:\n",
        "    if questionnaire.questions_error and False:\n",
        [PINS["validate_bad"]],
    ),
    (
        "M6 submit-policy gate 5 stops refusing an unreadable questionnaire",
        POLICY,
        "            if quest.questions_error:\n",
        "            if quest.questions_error and False:\n",
        [PINS["policy_bad"]],
    ),
    (
        "M7 submit-policy gate 5 refuses every questionnaire",
        POLICY,
        "            if quest.questions_error:\n",
        "            if True:\n",
        [PINS["policy_ctl"]],
    ),
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def run_pins(node_ids: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider", *node_ids],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    tail = (proc.stdout or proc.stderr).strip().splitlines()
    return proc.returncode, tail[-1] if tail else "(no output)"


def main() -> int:
    originals = {rel: (REPO / rel).read_bytes() for rel in (DB, QUEST, AUDIT, POLICY)}
    before = {rel: digest(REPO / rel) for rel in originals}

    print("=== baselines (pins must be green before mutating) ===")
    rc, summary = run_pins(sorted(set(PINS.values())))
    print(f"    all pins: rc={rc}  {summary}")
    if rc != 0:
        print("    REFUSING TO CONTINUE: the pins are not green on the unmutated tree.")
        return 1

    caught = 0
    for name, rel, old, new, expected in MUTATIONS:
        path = REPO / rel
        text = originals[rel].decode("utf-8")
        if text.count(old) != 1:
            print(f"--- {name}")
            print(f"    SKIPPED: anchor occurs {text.count(old)} times in {rel}")
            continue
        try:
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            rc, summary = run_pins(expected)
        finally:
            path.write_bytes(originals[rel])

        ok = rc != 0
        caught += 1 if ok else 0
        print(f"--- {name}")
        print(f"    expected red: {len(expected)} pin(s) in {rel}")
        print(f"    {'CAUGHT' if ok else 'SURVIVED'}: rc={rc}  {summary}")

    print()
    print("=== integrity ===")
    clean = True
    for rel, original in originals.items():
        now = (REPO / rel).read_bytes()
        same = now == original
        clean &= same
        print(f"    {rel}: {'restored' if same else 'CHANGED'} ({before[rel]} -> {digest(REPO / rel)})")

    print()
    print("=== summary ===")
    print(f"    mutations caught : {caught} of {len(MUTATIONS)}")
    print(f"    tree restored    : {'yes' if clean else 'NO - inspect before committing'}")
    return 0 if caught == len(MUTATIONS) and clean else 1


if __name__ == "__main__":
    sys.exit(main())
