"""Tests for the candidate knowledge base (Stage 0 / T2).

T1 pinned what the parser *reads*. T2 pins what the knowledge base is allowed to
*do* with it. The properties below are the ones that keep a confirmation
meaningful:

* merging never deletes and never changes a status -- so re-running ``extract``
  after editing the resume cannot quietly promote or discard anything;
* a status moves only through an explicit ``confirm`` / ``reject`` call, and the
  "no auto-promotion" invariant is measured by comparing a full status snapshot
  across a merge, with a second test proving that comparison has teeth;
* because ``claim_id`` is content-derived, editing the source yields a *new*
  unconfirmed claim instead of silently inheriting an old confirmation;
* ``confirm`` / ``reject`` are idempotent, and an unknown id is refused rather
  than silently creating a record.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_assistant.candidate.confirm import (
    candidate_kb_confirm_cmd,
    candidate_kb_reject_cmd,
    candidate_kb_show_cmd,
    render_kb_summary,
)
from ai_assistant.candidate.kb import (
    DEFAULT_KB_PATH,
    KB_PATH_ENV,
    CandidateKnowledgeBase,
    resolve_kb_path,
)
from ai_assistant.candidate.resume_parser import (
    candidate_kb_extract_cmd,
    parse_resume,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESUME_PATH = REPO_ROOT / "resume.md"

DERIVED = "DERIVED_FROM_SOURCE"
CONFIRMED = "CONFIRMED_BY_USER"
REJECTED = "REJECTED_BY_USER"


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def resume_text() -> str:
    return RESUME_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def claims(resume_text: str) -> list:
    return parse_resume(resume_text, str(RESUME_PATH))


@pytest.fixture(scope="module")
def resume_sha256() -> str:
    return hashlib.sha256(RESUME_PATH.read_bytes()).hexdigest()


def build_kb(claims: list, sha: str) -> CandidateKnowledgeBase:
    """An empty KB with exactly one merge already applied."""
    kb = CandidateKnowledgeBase()
    kb.merge_claims(claims, source_file=str(RESUME_PATH), source_sha256=sha)
    return kb


def status_map(kb: CandidateKnowledgeBase) -> dict[str, str]:
    return {claim_id: record.status for claim_id, record in kb.claims.items()}


def status_changes(before: dict[str, str], after: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Ids whose status moved between two snapshots.

    Deliberately compares *only* ids present in both maps: a merge that adds a
    claim is not a status change.
    """
    return {
        claim_id: (before[claim_id], after[claim_id])
        for claim_id in before
        if claim_id in after and before[claim_id] != after[claim_id]
    }


def a_claim_id(claims: list, kind: str) -> str:
    for claim in claims:
        if claim.kind == kind:
            return claim.claim_id
    raise AssertionError(f"no claim of kind {kind!r} in the extraction")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# Merge: adds, refreshes, never deletes
# --------------------------------------------------------------------------


def test_merge_records_every_claim_as_derived_from_source(claims: list, resume_sha256: str) -> None:
    kb = build_kb(claims, resume_sha256)

    assert len(kb.claims) == len(claims)
    assert kb.kb_version == 1
    assert kb.source_file == str(RESUME_PATH)
    assert kb.source_sha256 == resume_sha256
    assert kb.counts() == {DERIVED: len(claims), CONFIRMED: 0, REJECTED: 0}
    assert {record.status for record in kb.claims.values()} == {DERIVED}
    for record in kb.claims.values():
        assert record.in_source is True
        assert record.first_seen_version == 1
        assert record.last_seen_version == 1
        assert record.confirmed_version is None
        assert record.rejected_version is None


def test_merge_of_a_subset_keeps_the_claims_that_left_the_source(
    claims: list, resume_sha256: str
) -> None:
    """Dropping a fact from the resume must flag it, not delete it."""
    kb = build_kb(claims, resume_sha256)
    kept = claims[:10]

    report = kb.merge_claims(kept, source_file=str(RESUME_PATH), source_sha256=resume_sha256)

    assert len(kb.claims) == len(claims), "merging a subset must not drop the rest"
    assert report.added == 0
    assert report.refreshed == 10
    assert report.marked_absent == len(claims) - 10
    assert report.still_absent == 0
    assert kb.kb_version == 2

    absent = [record for record in kb.claims.values() if not record.in_source]
    assert len(absent) == len(claims) - 10
    for record in absent:
        # Everything except the flag survives.
        assert record.kind and record.value
        assert record.evidence
        assert record.status == DERIVED
        assert record.last_seen_version == 1


def test_a_claim_absent_for_two_merges_is_still_kept(claims: list, resume_sha256: str) -> None:
    kb = build_kb(claims, resume_sha256)
    kept = claims[:10]

    kb.merge_claims(kept, source_file=str(RESUME_PATH), source_sha256=resume_sha256)
    report = kb.merge_claims(kept, source_file=str(RESUME_PATH), source_sha256=resume_sha256)

    assert len(kb.claims) == len(claims)
    assert report.marked_absent == 0, "already-absent claims must not be re-marked"
    assert report.still_absent == len(claims) - 10


def test_merge_reports_a_changed_source_hash(claims: list, resume_sha256: str) -> None:
    kb = build_kb(claims, resume_sha256)
    report = kb.merge_claims(claims, source_file=str(RESUME_PATH), source_sha256="0" * 64)
    assert report.source_sha256_changed is True
    assert kb.source_sha256 == "0" * 64


# --------------------------------------------------------------------------
# The "no auto-promotion" invariant
# --------------------------------------------------------------------------


def test_merge_never_promotes_a_status(claims: list, resume_sha256: str) -> None:
    """Re-extracting the same source must leave every status exactly where it was.

    This is the acceptance-critical property: `extract` is the command a person
    re-runs casually, and it must not be able to turn "read from the file" into
    "confirmed by the candidate".
    """
    kb = build_kb(claims, resume_sha256)
    confirmed_id = a_claim_id(claims, "contact_email")
    rejected_id = a_claim_id(claims, "contact_telegram")
    kb.confirm(confirmed_id)
    kb.reject(rejected_id)

    before = status_map(kb)
    assert before[confirmed_id] == CONFIRMED
    assert before[rejected_id] == REJECTED

    kb.merge_claims(claims, source_file=str(RESUME_PATH), source_sha256=resume_sha256)

    after = status_map(kb)
    moved = status_changes(before, after)
    assert moved == {}, f"merge changed {len(moved)} status(es): {moved}"
    assert kb.kb_version == 2
    assert kb.counts() == {DERIVED: len(claims) - 2, CONFIRMED: 1, REJECTED: 1}


def test_the_no_auto_promotion_assertion_detects_a_promotion(
    claims: list, resume_sha256: str
) -> None:
    """Guard the guard.

    ``status_changes(...) == {}`` would also pass if the helper were broken.
    This test feeds it exactly what an auto-promotion would look like and
    requires it to notice.
    """
    kb = build_kb(claims, resume_sha256)
    before = status_map(kb)
    assert status_changes(before, before) == {}

    target = a_claim_id(claims, "contact_email")
    promoted = dict(before)
    promoted[target] = CONFIRMED

    assert status_changes(before, promoted) == {target: (DERIVED, CONFIRMED)}


def test_merge_does_not_promote_a_claim_that_was_rejected(claims: list, resume_sha256: str) -> None:
    """A rejection is a decision too, and a re-read must not undo it."""
    kb = build_kb(claims, resume_sha256)
    rejected_id = a_claim_id(claims, "contact_location")
    kb.reject(rejected_id)

    kb.merge_claims(claims, source_file=str(RESUME_PATH), source_sha256=resume_sha256)

    record = kb.get(rejected_id)
    assert record is not None
    assert record.status == REJECTED
    assert record.rejected_version == 1
    assert record.in_source is True


# --------------------------------------------------------------------------
# Editing the source: a new fact is a new claim
# --------------------------------------------------------------------------


def test_editing_the_source_produces_a_new_unconfirmed_claim(resume_text: str) -> None:
    """A confirmation is about a piece of text, not about a topic.

    The candidate confirmed the old address. When the address changes, the old
    confirmation must not silently cover the new value -- it is kept as history
    and the new value arrives unconfirmed.
    """
    old_text_email = "mikhailthaiban@gmail.com"
    assert old_text_email in resume_text, "fixture assumption: the resume states this address"

    original = parse_resume(resume_text, str(RESUME_PATH))
    kb = CandidateKnowledgeBase()
    kb.merge_claims(original, source_file=str(RESUME_PATH), source_sha256="a" * 64)

    old_id = a_claim_id(original, "contact_email")
    kb.confirm(old_id)

    edited_text = resume_text.replace(old_text_email, "mikhail.thaiban@example.com")
    edited = parse_resume(edited_text, str(RESUME_PATH))
    new_id = a_claim_id(edited, "contact_email")

    assert new_id != old_id, "a changed value must not reuse the old claim id"

    kb.merge_claims(edited, source_file=str(RESUME_PATH), source_sha256="b" * 64)

    new_record = kb.get(new_id)
    assert new_record is not None
    assert new_record.value == "mikhail.thaiban@example.com"
    assert new_record.status == DERIVED, "a new claim must arrive unconfirmed"

    old_record = kb.get(old_id)
    assert old_record is not None, "the old claim is history, not garbage"
    assert old_record.status == CONFIRMED, "its confirmation is preserved as history"
    assert old_record.in_source is False, "but it is flagged as no longer in the source"


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------


def test_confirm_moves_exactly_one_claim(claims: list, resume_sha256: str) -> None:
    kb = build_kb(claims, resume_sha256)
    cid = a_claim_id(claims, "contact_email")

    before = kb.counts()
    record = kb.confirm(cid)

    assert record.status == CONFIRMED
    assert record.confirmed_version == 1
    assert kb.counts() == {DERIVED: before[DERIVED] - 1, CONFIRMED: 1, REJECTED: 0}


def test_confirm_is_idempotent_and_does_not_restamp(claims: list, resume_sha256: str) -> None:
    kb = build_kb(claims, resume_sha256)
    cid = a_claim_id(claims, "contact_email")

    first = kb.confirm(cid)
    assert first.confirmed_version == 1

    # Bump the version so a re-stamp would be visible as a change from 1 to 2.
    kb.merge_claims(claims, source_file=str(RESUME_PATH), source_sha256=resume_sha256)
    assert kb.kb_version == 2

    again = kb.confirm(cid)
    assert again.status == CONFIRMED
    assert again.confirmed_version == 1, "re-confirming must not re-stamp the version"


def test_reject_changes_the_status_and_keeps_the_record(claims: list, resume_sha256: str) -> None:
    kb = build_kb(claims, resume_sha256)
    cid = a_claim_id(claims, "contact_telegram")

    original = kb.get(cid)
    assert original is not None
    snapshot = (original.kind, original.value, dict(original.evidence))

    record = kb.reject(cid)

    assert record.status == REJECTED
    assert record.rejected_version == 1
    assert kb.get(cid) is not None, "rejecting must not delete the record"
    assert (record.kind, record.value, dict(record.evidence)) == snapshot
    assert kb.counts()[REJECTED] == 1


def test_reject_is_idempotent(claims: list, resume_sha256: str) -> None:
    kb = build_kb(claims, resume_sha256)
    cid = a_claim_id(claims, "contact_telegram")
    kb.reject(cid)
    first_version = kb.get(cid).rejected_version

    kb.merge_claims(claims, source_file=str(RESUME_PATH), source_sha256=resume_sha256)
    again = kb.reject(cid)

    assert again.status == REJECTED
    assert again.rejected_version == first_version


def test_an_unknown_claim_id_is_refused_not_created(claims: list, resume_sha256: str) -> None:
    kb = build_kb(claims, resume_sha256)
    before = len(kb.claims)

    with pytest.raises(KeyError):
        kb.confirm("no-such-claim")
    with pytest.raises(KeyError):
        kb.reject("no-such-claim")

    assert len(kb.claims) == before
    assert kb.counts() == {DERIVED: before, CONFIRMED: 0, REJECTED: 0}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def test_kb_round_trips_through_json(tmp_path: Path, claims: list, resume_sha256: str) -> None:
    kb = build_kb(claims, resume_sha256)
    kb.confirm(a_claim_id(claims, "contact_email"))
    kb.reject(a_claim_id(claims, "contact_telegram"))

    path = tmp_path / "kb.json"
    kb.save(path)

    reloaded = CandidateKnowledgeBase.load(path)
    assert reloaded.to_dict() == kb.to_dict()
    assert reloaded.counts() == kb.counts()
    assert status_map(reloaded) == status_map(kb)


def test_saved_kb_is_valid_json_with_a_format_version(
    tmp_path: Path, claims: list, resume_sha256: str
) -> None:
    kb = build_kb(claims, resume_sha256)
    path = tmp_path / "kb.json"
    kb.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["kb_format_version"] == 1
    assert payload["kb_version"] == 1
    assert len(payload["claims"]) == len(claims)
    sample = next(iter(payload["claims"].values()))
    assert set(sample) >= {"claim_id", "kind", "value", "status", "evidence", "in_source"}


def test_save_is_atomic_and_leaves_no_temp_file(
    tmp_path: Path, claims: list, resume_sha256: str
) -> None:
    kb = build_kb(claims, resume_sha256)
    path = tmp_path / "kb.json"
    kb.save(path)
    kb.save(path)

    assert path.is_file()
    assert not (tmp_path / "kb.json.tmp").exists()
    leftovers = sorted(entry.name for entry in tmp_path.iterdir() if entry.name.endswith(".tmp"))
    assert leftovers == [], f"the atomic write left a temp file behind: {leftovers}"


def test_loading_a_missing_file_gives_an_empty_kb(tmp_path: Path) -> None:
    kb = CandidateKnowledgeBase.load(tmp_path / "absent.json")
    assert kb.claims == {}
    assert kb.kb_version == 0
    assert kb.counts() == {DERIVED: 0, CONFIRMED: 0, REJECTED: 0}


def test_by_status_filters_and_sorts(claims: list, resume_sha256: str) -> None:
    kb = build_kb(claims, resume_sha256)
    kb.confirm(a_claim_id(claims, "contact_email"))

    derived = kb.by_status(DERIVED)
    confirmed = kb.by_status(CONFIRMED)

    assert len(derived) == len(claims) - 1
    assert len(confirmed) == 1
    assert len(kb.by_status()) == len(claims)
    keys = [(record.kind, record.value, record.claim_id) for record in derived]
    assert keys == sorted(keys)


def test_line_ref_falls_back_when_there_is_no_line() -> None:
    from ai_assistant.candidate.kb import ClaimRecord

    assert ClaimRecord("x", "kind", "value", evidence={"source_file": "r.md", "line": 7}).line_ref() == "r.md:7"
    assert ClaimRecord("x", "kind", "value", evidence={}).line_ref() == "?"


# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------


def test_resolve_kb_path_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KB_PATH_ENV, raising=False)
    assert DEFAULT_KB_PATH == REPO_ROOT / "candidate_kb.json"
    assert resolve_kb_path() == DEFAULT_KB_PATH

    monkeypatch.setenv(KB_PATH_ENV, str(tmp_path / "from_env.json"))
    assert resolve_kb_path() == tmp_path / "from_env.json"

    explicit = tmp_path / "explicit.json"
    assert resolve_kb_path(str(explicit)) == explicit, "an explicit path beats the environment"


def test_the_kb_file_is_gitignored() -> None:
    """The KB holds the candidate's own facts, so it must not be committable."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.json" in gitignore

    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    result = subprocess.run(
        ["git", "check-ignore", "-q", "candidate_kb.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, "candidate_kb.json must be ignored by git"


# --------------------------------------------------------------------------
# CLI: extract
# --------------------------------------------------------------------------


def test_extract_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    target = tmp_path / "kb.json"

    code = candidate_kb_extract_cmd(
        resume_path=str(RESUME_PATH), dry_run=True, kb_path=str(target)
    )
    out = capsys.readouterr().out

    assert code == 0
    assert not target.exists(), "--dry-run must not create the knowledge base"
    assert "--dry-run was given, so nothing was written" in out


def test_extract_creates_the_kb_with_every_claim_derived(
    tmp_path: Path, capsys: pytest.CaptureFixture, claims: list, resume_sha256: str
) -> None:
    target = tmp_path / "kb.json"

    code = candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    out = capsys.readouterr().out

    assert code == 0
    assert target.is_file(), "extract must create the knowledge base"

    kb = CandidateKnowledgeBase.load(target)
    assert len(kb.claims) == len(claims)
    assert kb.kb_version == 1
    assert kb.source_sha256 == resume_sha256
    assert {record.status for record in kb.claims.values()} == {DERIVED}
    assert f"kb_version 0 -> 1, added {len(claims)}" in out


def test_extract_twice_merges_rather_than_duplicates(
    tmp_path: Path, capsys: pytest.CaptureFixture, claims: list
) -> None:
    target = tmp_path / "kb.json"

    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    out = capsys.readouterr().out

    kb = CandidateKnowledgeBase.load(target)
    assert len(kb.claims) == len(claims), "a second extract must not duplicate claims"
    assert kb.kb_version == 2
    assert f"added 0, refreshed {len(claims)}" in out


def test_extract_json_carries_the_merge_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture, claims: list
) -> None:
    target = tmp_path / "kb.json"

    code = candidate_kb_extract_cmd(
        resume_path=str(RESUME_PATH), kb_path=str(target), as_json=True
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["claim_count"] == len(claims)
    assert payload["knowledge_base"]["kb_version"] == 1
    assert payload["knowledge_base"]["added"] == len(claims)
    assert payload["knowledge_base"]["path"] == str(target)


# --------------------------------------------------------------------------
# CLI: show
# --------------------------------------------------------------------------


def test_show_reports_a_missing_kb(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = candidate_kb_show_cmd(kb_path=str(tmp_path / "absent.json"))
    captured = capsys.readouterr()

    assert code == 2
    assert "ERROR: no knowledge base at" in captured.err
    assert "candidate-kb extract" in captured.err


def test_show_lists_claims_with_their_line_refs(
    tmp_path: Path, capsys: pytest.CaptureFixture, claims: list
) -> None:
    target = tmp_path / "kb.json"
    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    capsys.readouterr()

    code = candidate_kb_show_cmd(kb_path=str(target))
    out = capsys.readouterr().out

    assert code == 0
    assert f"[{DERIVED}] {len(claims)}" in out
    assert f"[{CONFIRMED}] 0" in out
    assert "resume.md:" in out


def test_show_rejects_an_unknown_status(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    target = tmp_path / "kb.json"
    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    capsys.readouterr()

    code = candidate_kb_show_cmd(status="PROBABLY_TRUE", kb_path=str(target))
    captured = capsys.readouterr()

    assert code == 2
    assert "ERROR: unknown status" in captured.err
    assert "PROBABLY_TRUE" in captured.err


def test_show_can_filter_to_one_status(
    tmp_path: Path, capsys: pytest.CaptureFixture, claims: list
) -> None:
    target = tmp_path / "kb.json"
    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    capsys.readouterr()

    candidate_kb_confirm_cmd(a_claim_id(claims, "contact_email"), kb_path=str(target))
    capsys.readouterr()

    code = candidate_kb_show_cmd(status=CONFIRMED, kb_path=str(target))
    out = capsys.readouterr().out

    assert code == 0
    assert f"[{CONFIRMED}] 1" in out
    assert f"[{DERIVED}]" not in out, "a filtered listing must not print the other groups"


def test_show_json_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture, claims: list
) -> None:
    target = tmp_path / "kb.json"
    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    capsys.readouterr()

    code = candidate_kb_show_cmd(as_json=True, kb_path=str(target))
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["kb_version"] == 1
    assert payload["counts"] == {DERIVED: len(claims), CONFIRMED: 0, REJECTED: 0}
    assert len(payload["claims"]) == len(claims)


# --------------------------------------------------------------------------
# CLI: confirm / reject
# --------------------------------------------------------------------------


def test_confirm_cli_moves_exactly_one_claim_and_keeps_the_version(
    tmp_path: Path, capsys: pytest.CaptureFixture, claims: list
) -> None:
    target = tmp_path / "kb.json"
    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    capsys.readouterr()

    cid = a_claim_id(claims, "contact_email")
    code = candidate_kb_confirm_cmd(cid, kb_path=str(target))
    out = capsys.readouterr().out

    assert code == 0
    assert f"confirm: {cid}" in out
    assert f"{DERIVED} -> {CONFIRMED}" in out

    kb = CandidateKnowledgeBase.load(target)
    assert kb.counts() == {DERIVED: len(claims) - 1, CONFIRMED: 1, REJECTED: 0}
    assert kb.kb_version == 1, "a confirmation must not bump the merge version"
    assert kb.get(cid).confirmed_version == 1


def test_confirm_cli_is_idempotent_and_writes_nothing_the_second_time(
    tmp_path: Path, capsys: pytest.CaptureFixture, claims: list
) -> None:
    target = tmp_path / "kb.json"
    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    cid = a_claim_id(claims, "contact_email")
    candidate_kb_confirm_cmd(cid, kb_path=str(target))
    capsys.readouterr()

    before = target.read_bytes()
    code = candidate_kb_confirm_cmd(cid, kb_path=str(target))
    out = capsys.readouterr().out

    assert code == 0
    assert "no change, nothing written" in out
    assert target.read_bytes() == before, "an idempotent confirm must not rewrite the file"


def test_reject_cli_keeps_the_record_and_flips_the_status(
    tmp_path: Path, capsys: pytest.CaptureFixture, claims: list
) -> None:
    target = tmp_path / "kb.json"
    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    capsys.readouterr()

    cid = a_claim_id(claims, "contact_telegram")
    code = candidate_kb_reject_cmd(cid, kb_path=str(target))
    out = capsys.readouterr().out

    assert code == 0
    assert f"reject: {cid}" in out
    assert f"{DERIVED} -> {REJECTED}" in out

    kb = CandidateKnowledgeBase.load(target)
    assert kb.counts() == {DERIVED: len(claims) - 1, CONFIRMED: 0, REJECTED: 1}
    assert kb.get(cid) is not None


def test_confirm_cli_refuses_an_unknown_id(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    target = tmp_path / "kb.json"
    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    capsys.readouterr()

    before = target.read_bytes()
    code = candidate_kb_confirm_cmd("no-such-claim", kb_path=str(target))
    captured = capsys.readouterr()

    assert code == 2
    assert "ERROR: no claim with id" in captured.err
    assert target.read_bytes() == before


def test_confirm_cli_reports_a_missing_kb(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = candidate_kb_confirm_cmd("anything", kb_path=str(tmp_path / "absent.json"))
    captured = capsys.readouterr()

    assert code == 2
    assert "ERROR: no knowledge base at" in captured.err


def test_render_kb_summary_counts_absent_records(
    tmp_path: Path, claims: list, resume_sha256: str
) -> None:
    kb = build_kb(claims, resume_sha256)
    kb.merge_claims(claims[:5], source_file=str(RESUME_PATH), source_sha256=resume_sha256)

    text = render_kb_summary(kb, tmp_path / "kb.json")

    assert "kb_version     : 2" in text
    assert f"absent records : {len(claims) - 5}" in text


# --------------------------------------------------------------------------
# The KB commands stay away from the candidate's real files
# --------------------------------------------------------------------------


def test_kb_commands_do_not_modify_protected_files(
    tmp_path: Path, capsys: pytest.CaptureFixture, claims: list
) -> None:
    """Storage must not reach outside its own file."""
    # ai_assistant/candidate_profile.json was a byte-identical copy of the root
    # profile and is gone as of T8. The root file is still protected below.
    protected = [
        RESUME_PATH,
        REPO_ROOT / "candidate_profile.json",
        REPO_ROOT / "state.db",
    ]
    missing = [path for path in protected if not path.is_file()]
    assert not missing, f"expected these files to exist: {missing}"
    before = {path: (sha256_of(path), path.stat().st_size) for path in protected}

    target = tmp_path / "kb.json"
    cid = a_claim_id(claims, "contact_email")
    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), kb_path=str(target))
    candidate_kb_confirm_cmd(cid, kb_path=str(target))
    candidate_kb_reject_cmd(cid, kb_path=str(target))
    candidate_kb_show_cmd(kb_path=str(target))
    capsys.readouterr()

    after = {path: (sha256_of(path), path.stat().st_size) for path in protected}
    assert after == before, {p: (before[p], after[p]) for p in protected if before[p] != after[p]}
