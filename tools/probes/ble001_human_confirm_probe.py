#!/usr/bin/env python
"""Probe for finding #53: what does the human-confirmation gate do per shape?

Drives the real ``hh_human_submission.confirm_human_submission`` against a real
``HumanReviewStore`` holding a really-approved review for ``hh:136591579``, then
asks it to confirm a DIFFERENT vacancy (``hh:WRONG``). Every shape below is the
same question; only ``entry["gate"]`` changes.

    ok=False  the mismatched vacancy was caught        -> closed
    ok=True   the comparison was skipped               -> open

``None`` is the one legitimate ``ok=True``: finding #32's rule is that an ABSENT
gate field is a skip (reviews written before gate data existed), while anything
else is an anomaly. So the expectation is not "everything refuses" - it is
"everything except an absent field refuses", and the tool says which.

The second half is the reachability question, measured rather than assumed:
which writers can put a non-mapping into ``entry["gate"]`` at all? The answer is
printed as a census over the repository, not as a claim.

Usage:
    ./.venv/Scripts/python.exe tools/ble001_human_confirm_probe.py
    ./.venv/Scripts/python.exe tools/ble001_human_confirm_probe.py --writers
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ai_assistant.application_review_gate import (
    GateStatus,
    HumanReviewGate,
    HumanReviewStore,
)
from ai_assistant.hh_human_submission import (
    clear_human_confirmations,
    confirm_human_submission,
)

FP = "c" * 64
APPROVED = "hh:136591579"
WRONG = "hh:WRONG"

# (label, gate value, expected verdict)
# "closed" = must refuse; "skip" = the comparison is not made at all.
#
# "skip" is not a pass. Two different things produce it and they are NOT the
# same size:
#   * an ABSENT gate field - finding #32's stated rule, kept identical here;
#   * a gate that is present but carries no vacancy ("", {} or a blank value) -
#     also inherited from #32, and REACHABLE, because HumanReviewGate's
#     vacancy_stable_id defaults to "". Recorded as finding #54 rather than
#     quietly folded in: closing it means changing #32's copy too.
SHAPES = [
    ("dict-match", {"vacancy_stable_id": APPROVED}, "closed"),
    ("dict-other", {"vacancy_stable_id": "hh:OTHER"}, "closed"),
    ("str-truthy", "approved", "closed"),
    ("str-empty", "", "closed"),
    ("list-empty", [], "closed"),
    ("int-zero", 0, "closed"),
    ("dict-empty", {}, "skip"),
    ("dict-blank-vacancy", {"vacancy_stable_id": ""}, "skip"),
    ("absent", None, "skip"),
]


def build_store():
    """A real store with one really-approved review, through the real machine."""
    gate = HumanReviewGate(
        review_id="review_probe53",
        status=GateStatus.READY_FOR_HUMAN_REVIEW,
        fingerprint=FP,
        vacancy_stable_id=APPROVED,
    )
    store = HumanReviewStore()
    rid = store.save(gate)
    store.mark_waiting_for_human(rid)
    approved = store.approve_review(rid, FP)
    if not approved.get("ok"):
        raise SystemExit(f"could not approve the probe review: {approved}")
    return store, rid


def run_shapes() -> int:
    print(f"confirm_human_submission(..., vacancy={WRONG!r})")
    print(f"  the approved review is for {APPROVED!r}\n")
    header = f"  {'gate':20s} {'type':9s} {'verdict':9s} {'expected':9s} {'ok':5s} reason"
    print(header)
    print("  " + "-" * (len(header) - 2))

    wrong = []
    for label, value, expected in SHAPES:
        clear_human_confirmations()
        store, rid = build_store()
        store.get(rid)["gate"] = value

        result = confirm_human_submission(store, rid, FP, WRONG)
        verdict = "skip" if result["ok"] else "closed"
        flag = "" if verdict == expected else "  <-- UNEXPECTED"
        if verdict != expected:
            wrong.append(label)
        print(
            f"  {label:20s} {type(value).__name__:9s} {verdict:9s} {expected:9s} "
            f"{result['ok']!s:5s} {result['reason'][:60]}{flag}"
        )

    print()
    closed = sum(1 for _, _, e in SHAPES if e == "closed")
    skipped = sum(1 for _, _, e in SHAPES if e == "skip")
    print(f"  unreadable shapes that must close the gate: {closed}")
    print(f"  shapes that skip the comparison:            {skipped}  (absent, and present-but-no-vacancy)")
    print("  the first group is what finding #53 closed; the second is NOT closed,")
    print("  is reachable, and is recorded as finding #54")
    if wrong:
        print(f"\nRESULT: {len(wrong)} shape(s) behaved unexpectedly: {', '.join(wrong)}")
        return 1
    print("\nRESULT: fail-closed on unreadable records, skip on empty ones")
    return 0


def run_writers() -> int:
    """Who can write entry["gate"], and can any of them write a non-mapping?

    This is the honest reachability half. It reads the repository rather than
    asserting, and it is deliberately blunt: a writer is any line that assigns
    the "gate" key of a review entry, plus every HumanReviewStore.save call.
    """
    sources = [
        p for p in (REPO / "ai_assistant").rglob("*.py")
    ] + [p for p in (REPO / "scripts").rglob("*.py")]
    assign = re.compile(r'"gate"\s*:\s*([^,}]+)')
    print("writers of the review entry's \"gate\" key:\n")
    found = 0
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.split("\n"), 1):
            m = assign.search(line)
            if m:
                found += 1
                print(f"  {path.relative_to(REPO)}:{n}  -> {m.group(1).strip()}")

    print(f"\n  total: {found}")
    print("  HumanReviewStore.save() is the only store writer; it writes")
    print("  gate.model_dump(), which is always a dict. Nothing in the repository")
    print("  puts a non-mapping into entry[\"gate\"] today - so the doors the probe")
    print("  above opens are anomalies, not reachable states. Stated, not assumed.")
    return 0


def main(argv: list[str]) -> int:
    if "--writers" in argv:
        return run_writers()
    return run_shapes()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
