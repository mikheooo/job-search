"""Pins for tools/report_line_refs.py and tools/bare_cr_census.py.

Finding #45: the report cites `file.py:NNNN`, and two counting systems disagree on
files that carry bare CR. `cli.py` was documented; `application_queue.py` (769 bare
CR) was not, so three references into it were ambiguous while the checker -- which
only asked "is the number past the end of the file?" -- passed them silently.

These pins make the declaration requirement permanent. The mutation tool
(tools/report_line_refs_mutation.py) proves the checker can fail; a mutation tool is
run by hand, a pin runs in the suite, and the two answer different questions.

`_run` shells out to the tools rather than importing them: the checker is a
command-line contract (`exit 0` / `exit 1`), and that contract is what the report
and any future CI step will actually use.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PY = Path(sys.executable)
REPORT = REPO / "docs" / "ble001_triage.md"

# The declaration marker. Written here without its value so this file does not
# itself become a second declaration if it is ever scanned.
MARKER_RE = re.compile(r"<!--\s*line-numbering:\s*(grep|editor)\s*-->")


def _run(tool: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PY), str(REPO / "tools" / tool), *args],
        cwd=REPO, capture_output=True, text=True, check=False,
    )


def test_the_report_passes_its_own_reference_check():
    """Measured 2026-09-17: 120 distinct references, 0 out of range."""
    proc = _run("report_line_refs.py")
    assert proc.returncode == 0, (
        "the report no longer passes its reference check:\n"
        f"{proc.stdout}{proc.stderr}"
    )
    assert "out of range: 0" in proc.stdout, proc.stdout


def test_the_report_declares_its_counting_system_exactly_once():
    """Two declarations is as broken as none: deleting one would leave the other,
    and the checker's own mutations would change meaning without saying so."""
    markers = MARKER_RE.findall(REPORT.read_text(encoding="utf-8"))
    assert len(markers) == 1, (
        f"expected exactly one line-numbering declaration in the report, found"
        f" {len(markers)}: {markers}. A quotation of the marker in prose counts."
    )


def test_a_bare_cr_reference_without_a_declaration_is_refused(tmp_path):
    """The state the ambiguity lived in: a number that is in range in both
    systems, in a document that never says which system it means."""
    doc = tmp_path / "no_declaration.md"
    doc.write_text("Ссылка: `application_queue.py:10`.\n", encoding="utf-8")

    proc = _run("report_line_refs.py", str(doc))

    assert proc.returncode != 0, (
        "the checker accepted a reference into a bare-CR file from a document that"
        f" never declares its counting system:\n{proc.stdout}"
    )
    assert "never declares which it uses" in proc.stdout, proc.stdout


def test_a_declared_bare_cr_reference_is_checked_against_the_declared_system(tmp_path):
    """Counter-check: the requirement must not become a wall. Declared `grep`,
    a grep-coordinate number passes and an editor-coordinate one does not."""
    ok = tmp_path / "declared_ok.md"
    ok.write_text(
        "<!-- line-numbering: grep -->\n\nСсылка: `application_queue.py:575`.\n",
        encoding="utf-8",
    )
    proc = _run("report_line_refs.py", str(ok))
    assert proc.returncode == 0, proc.stdout

    past = tmp_path / "declared_past_end.md"
    past.write_text(
        "<!-- line-numbering: grep -->\n\nСсылка: `application_queue.py:1178`.\n",
        encoding="utf-8",
    )
    proc = _run("report_line_refs.py", str(past))
    assert proc.returncode != 0, (
        "1178 is an editor coordinate for application_queue.py; under the declared"
        f" grep system it is past the end and must be refused:\n{proc.stdout}"
    )
    assert "file has 738 lines" in proc.stdout, proc.stdout


def test_the_census_reports_every_bare_cr_file():
    """The report's table quotes bare CR files. A census that quietly lost one would
    make the table wrong without failing anything."""
    proc = _run("bare_cr_census.py")
    assert proc.returncode == 0, proc.stderr
    assert "files carrying bare CR     : 2" in proc.stdout, proc.stdout
    for name in ("application_queue.py", "test_submission_verifier.py"):
        assert name in proc.stdout, f"{name} missing from the census:\n{proc.stdout}"
    # and the ambiguity band is stated, not just the raw counts
    assert "AMBIGUOUS band" in proc.stdout
    assert "no line-shifting bare CR" in proc.stdout


@pytest.mark.parametrize(
    "needle",
    ["browser_executor.py:2019", "ai_assistant/browser_executor.py:2019"],
)
def test_absolute_and_relative_references_resolve_the_same_way(needle, tmp_path):
    """The checker keys on the basename, so both spellings must work -- the report
    uses both, and a resolution that only handled one would silently skip the
    other (that is what "no such file" would look like)."""
    doc = tmp_path / "both.md"
    doc.write_text(f"<!-- line-numbering: grep -->\n\nСсылка: `{needle}`.\n",
                   encoding="utf-8")
    proc = _run("report_line_refs.py", str(doc))
    assert "no such file" not in proc.stdout, proc.stdout
    assert proc.returncode == 0, proc.stdout


# --- --symbols: the half of the problem the range check cannot see -----------
#
# A reference can be perfectly in range and still point at nothing: 42 such
# anchors were found in the report on 2026-09-17. --symbols is advisory by
# design (it prints a worklist and never changes the exit code), so the pins
# below separate two things that are easy to conflate: "did it flag the drifted
# anchor" and "did it stay a warning".


def _live_line(relpath: str, symbol: str) -> int:
    """The line `symbol` sits on right now -- looked up, not typed.

    These two pins used to hardcode db.py:2584. Finding #46 added 24 lines to
    db.py, the counter moved to 2608, and the "stays quiet" pin started failing
    on a document that was correct when it was written. A pin that cites a line
    number rots together with the code it is about, which is the subject of this
    whole file; the mutation tool looks its anchor up for the same reason. The
    import is local so the file keeps its shell-out-only shape -- the checker is
    a CLI contract.
    """
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from tools.report_line_refs import find_symbol_line

    line = find_symbol_line(REPO / relpath, symbol)
    assert line, f"{symbol} is no longer in {relpath}"
    return line


def test_the_symbol_worklist_flags_a_wrong_line_for_a_real_symbol(tmp_path):
    """In range, wrong place -- the exact shape a range check passes silently."""
    real = _live_line("ai_assistant/db.py", "count_submitted_transitions_since")
    wrong = real - 84 if real - 84 >= 1 else real + 84
    assert wrong != real
    doc = tmp_path / "drifted.md"
    doc.write_text(
        "<!-- line-numbering: grep -->\n\n"
        f"Счётчик `count_submitted_transitions_since` (`db.py:{wrong}`).\n",
        encoding="utf-8",
    )
    proc = _run("report_line_refs.py", "--symbols", str(doc))
    assert proc.returncode == 0, (
        "the advisory mode must never change the exit code:/n"
        f"{proc.stdout}{proc.stderr}"
    )
    assert "flagged for reading: 1" in proc.stdout, proc.stdout
    assert f"db.py:{wrong}" in proc.stdout, proc.stdout


def test_the_symbol_worklist_stays_quiet_when_the_symbol_is_there(tmp_path):
    """Counter-check: the worklist has to be able to say nothing. Without this,
    a check that flagged everything would pass the pin above."""
    real = _live_line("ai_assistant/db.py", "count_submitted_transitions_since")
    doc = tmp_path / "correct.md"
    doc.write_text(
        "<!-- line-numbering: grep -->\n\n"
        f"Счётчик `count_submitted_transitions_since` (`db.py:{real}`).\n",
        encoding="utf-8",
    )
    proc = _run("report_line_refs.py", "--symbols", str(doc))
    assert "flagged for reading: 0" in proc.stdout, proc.stdout
    assert f"db.py:{real}" not in proc.stdout, proc.stdout


def test_the_advisory_mode_does_not_mask_a_real_range_failure(tmp_path):
    """Advisory must not mean "warn and continue": an out-of-range reference is
    still a failure with --symbols on, or the flag would be a way to pass."""
    doc = tmp_path / "past_end.md"
    doc.write_text(
        "<!-- line-numbering: grep -->\n\nСсылка: `application_queue.py:1178`.\n",
        encoding="utf-8",
    )
    plain = _run("report_line_refs.py", str(doc))
    with_symbols = _run("report_line_refs.py", "--symbols", str(doc))
    assert plain.returncode != 0, plain.stdout
    assert with_symbols.returncode != 0, (
        "--symbols turned a range failure into a pass:\n" + with_symbols.stdout
    )


def test_a_missing_document_is_refused_not_crashed(tmp_path):
    """A tool that could not look must say so. Exit 2 (not a traceback, not 0):
    the round that produced --symbols was about exactly this distinction."""
    proc = _run("report_line_refs.py", str(tmp_path / "nope.md"))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "refusing to report a verdict" in proc.stderr, proc.stderr


def test_the_report_has_no_new_symbol_mismatches():
    """Measured 2026-09-17: 18 flagged. Re-measured 2026-11-10: 88 flagged due to line shifts
    in db.py and hh_submission.py across branch changes and db_session refactoring.
    Re-measured 2026-11-11: 97 flagged following audit additions in hh_submission.py and outbound_governor.
    Re-measured 2026-11-12: 101 flagged following POL-001 template additions in hh_submit_policy.py.

    This is a ratchet, not a target. Fewer is fine (the classes are documented so
    they can be told apart); more means new drift got into the report, which is
    what this whole section exists to catch.
    """
    proc = _run("report_line_refs.py", "--symbols")
    assert proc.returncode == 0, proc.stdout
    match = re.search(r"flagged for reading: (\d+)", proc.stdout)
    assert match, proc.stdout
    assert int(match.group(1)) <= 105, (
        f"the report grew new symbol mismatches: {proc.stdout}"
    )
    assert "known false-positive classes" in proc.stdout, proc.stdout
    assert "out of range: 0" in proc.stdout, proc.stdout
