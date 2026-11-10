"""Tests for cross-source verification (Stage 0 / T3).

T1 pinned what the parser reads; T2 pinned what the knowledge base is allowed to
do. T3 pins that the three sources describing the candidate are actually compared,
and that the comparison reports **the contradictions that are already known to
exist** -- rather than a clean bill of health produced by a checker that does not
look.

Two properties carry the weight here:

* the three mandatory pins (resume's "15+ years" vs ``it_support``; Bitrix24 as
  experience vs ``excluded_roles``; ``years_experience`` vs ``it_support``);
* the pins are **self-proving**: each one also checks that disabling its rule
  makes the finding disappear, so a pin cannot pass by reading a hardcoded
  expectation. A rule that was silently dead would fail here.

"Zero false positives" is tested with positive controls as well: a rule that never
fires would satisfy "no false positives" trivially, so each silent rule is shown
to be capable of firing when the facts really do disagree.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ai_assistant.candidate.kb import CandidateKnowledgeBase
from ai_assistant.candidate.kb_verify import (
    DEFAULT_RULES,
    SOURCE_BASELINE,
    SOURCE_PROFILE,
    SOURCE_RESUME,
    Contradiction,
    candidate_kb_verify_cmd,
    format_verification,
    load_baseline_profile,
    resolve_profile_path,
    verify_all,
)
from ai_assistant.candidate.resume_parser import parse_resume

REPO_ROOT = Path(__file__).resolve().parent.parent
RESUME_PATH = REPO_ROOT / "resume.md"
PROFILE_PATH = REPO_ROOT / "candidate_profile.json"
KB_PATH = REPO_ROOT / "candidate_kb.json"

MANDATORY_RULES = (
    "total_experience_vs_it_support",
    "years_experience_vs_domain_years",
)

# Rules that find nothing against today's data. They are the false-positive
# controls, and each has a positive control below proving it is not simply dead.
#
# ``bitrix_experience_vs_excluded`` joined this group in T7.3: the candidate
# confirmed Bitrix24 as a target direction, so ``bitrix`` was removed from the
# profile's ``excluded_roles`` and ``must_avoid_conditions`` and the rule that
# compared the resume's Bitrix24 experience against those lists went silent.
# The rule itself is untouched -- if a Bitrix24 ban ever returns, it fires again
# (positive control: test_bitrix_rule_fires_when_the_ban_returns).
# Order must follow DEFAULT_RULES, because ``rules_silent`` is built in that order.
SILENT_RULES = (
    "bitrix_experience_vs_excluded",
    "profile_copy_drift",
    "contact_mismatch",
    "language_mismatch",
)


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def kb_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A KB built from the real resume, so the tests do not depend on a local build.

    The resume is parsed with the label ``resume.md`` rather than an absolute path
    so the evidence locations match what the CLI produces.
    """
    directory = tmp_path_factory.mktemp("t3")
    path = directory / "candidate_kb.json"
    text = RESUME_PATH.read_text(encoding="utf-8")
    kb = CandidateKnowledgeBase()
    kb.merge_claims(
        parse_resume(text, "resume.md"),
        source_file="resume.md",
        source_sha256=hashlib.sha256(RESUME_PATH.read_bytes()).hexdigest(),
    )
    kb.save(path)
    return path


@pytest.fixture(scope="module")
def result(kb_file: Path):
    return verify_all(kb_path=kb_file, profile_path=str(PROFILE_PATH))


@pytest.fixture(scope="module")
def blind(kb_file: Path):
    """The same sources with every rule disabled -- the baseline for 'rule did it'."""
    return verify_all(kb_path=kb_file, profile_path=str(PROFILE_PATH), rules=())


def ref_of(contradiction: Contradiction, source: str):
    matches = [ref for ref in contradiction.sources if ref.source == source]
    assert matches, f"no {source!r} reference in {contradiction.rule}"
    return matches[0]


def write_profile(directory: Path, filename: str, **overrides) -> Path:
    data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    data.update(overrides)
    path = directory / filename
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# The mandatory pins
# --------------------------------------------------------------------------


def test_pin_resume_total_experience_vs_profile_it_support(result, blind) -> None:
    """The resume's "15+ years" against the profile's it_support figure."""
    found = result.by_rule("total_experience_vs_it_support")
    assert len(found) == 1, [item.subject for item in found]

    contradiction = found[0]
    resume_ref = ref_of(contradiction, SOURCE_RESUME)
    profile_ref = ref_of(contradiction, SOURCE_PROFILE)

    assert resume_ref.location == "resume.md:7"
    assert "15+ years of IT experience" in resume_ref.value
    assert profile_ref.location.endswith("domain_years.it_support")
    assert profile_ref.value == "11"

    # Self-proof: the finding comes from the rule, not from a baked-in answer.
    assert blind.by_rule("total_experience_vs_it_support") == []


def test_pin_bitrix_experience_vs_excluded(result, blind) -> None:
    """Bitrix24 is no longer excluded, so the contradiction must be gone.

    T7.3: the candidate confirmed Bitrix24/CRM as a direction to search, so
    ``bitrix`` was removed from both ``excluded_roles`` and
    ``must_avoid_conditions``. The resume still presents Bitrix24 as
    experience -- that part did not change -- but with the ban gone there is
    nothing left to contradict. This pin used to assert the contradiction
    existed; it now asserts it is absent, and the positive control below proves
    the rule itself is still alive.
    """
    assert result.by_rule("bitrix_experience_vs_excluded") == [], (
        "Bitrix24 снова противоречит профилю — значит, запрет на bitrix вернулся"
    )
    assert blind.by_rule("bitrix_experience_vs_excluded") == []


def test_bitrix_rule_fires_when_the_ban_returns(tmp_path: Path, kb_file: Path) -> None:
    """Positive control: re-introduce a bitrix ban and the rule must fire again.

    Without this, moving the rule to SILENT_RULES could hide a rule that had
    simply stopped working.
    """
    other = write_profile(
        tmp_path,
        "with_bitrix_ban.json",
        excluded_roles=["1c", "php", "bitrix", "ruby", "java developer", "c++", "c#"],
    )
    found = verify_all(kb_path=kb_file, profile_path=str(other)).by_rule(
        "bitrix_experience_vs_excluded"
    )
    assert len(found) == 1, [item.subject for item in found]

    contradiction = found[0]
    assert {ref.source for ref in contradiction.sources} == {SOURCE_RESUME, SOURCE_PROFILE}
    resume_ref = ref_of(contradiction, SOURCE_RESUME)
    assert "Bitrix24" in resume_ref.value
    assert resume_ref.location in ("resume.md:33", "resume.md:37"), "the Avantis role is the load-bearing hit"

    profile_fields = {
        ref.location.rsplit(":", 1)[-1]
        for ref in contradiction.sources
        if ref.source == SOURCE_PROFILE
    }
    assert "excluded_roles" in profile_fields


def test_pin_years_experience_vs_it_support(result, blind) -> None:
    """The profile's headline years_experience against its own it_support."""
    found = [
        item
        for item in result.by_rule("years_experience_vs_domain_years")
        if item.subject.endswith("it_support")
    ]
    assert len(found) == 1, [item.subject for item in result.by_rule("years_experience_vs_domain_years")]

    values = {ref.location.rsplit(":", 1)[-1]: ref.value for ref in found[0].sources}
    assert values["years_experience"] == "3"
    assert values["domain_years.it_support"] == "11"

    assert blind.by_rule("years_experience_vs_domain_years") == []


# --------------------------------------------------------------------------
# "The pin must fail if verification is disabled"
# --------------------------------------------------------------------------


def test_disabling_every_rule_yields_nothing(blind) -> None:
    """The baseline the self-proofs rest on: no rules, no findings."""
    assert blind.contradictions == []
    assert blind.rules_run == ()


def test_every_rule_can_be_disabled_individually(kb_file: Path) -> None:
    """Each rule's findings must vanish when that rule alone is removed.

    This is the generic form of "the pin fails if verification is switched off":
    if a rule were wired to a constant rather than to the data, its findings would
    survive its own removal and this test would fail.
    """
    for rule in DEFAULT_RULES:
        remaining = tuple(item for item in DEFAULT_RULES if item.rule_id != rule.rule_id)
        partial = verify_all(kb_path=kb_file, profile_path=str(PROFILE_PATH), rules=remaining)
        assert partial.by_rule(rule.rule_id) == [], (
            f"{rule.rule_id} still reported findings after being disabled"
        )


def test_every_rule_is_reachable_and_named(kb_file: Path) -> None:
    """No rule may be listed but never run."""
    result = verify_all(kb_path=kb_file, profile_path=str(PROFILE_PATH))
    assert result.rules_run == tuple(rule.rule_id for rule in DEFAULT_RULES)
    assert set(result.rules_silent) <= set(result.rules_run)


# --------------------------------------------------------------------------
# Acceptance: at least three contradictions, each citing both sides
# --------------------------------------------------------------------------


def test_at_least_three_contradictions_are_reported(result) -> None:
    assert len(result.contradictions) >= 3
    assert set(MANDATORY_RULES) <= {item.rule for item in result.contradictions}


def test_every_contradiction_cites_two_distinct_locations(result) -> None:
    for item in result.contradictions:
        assert len(item.sources) >= 2, f"{item.rule}/{item.subject} cites one side only"
        locations = [ref.location for ref in item.sources]
        assert len(set(locations)) == len(locations), f"{item.rule} cites the same place twice"
        for ref in item.sources:
            assert ref.value != "", f"{item.rule} cites {ref.location} with no value"


def test_resume_facing_pins_cite_two_distinct_sources(result) -> None:
    """The resume-vs-profile pins cite both files.

    ``years_experience_vs_domain_years`` is deliberately excluded: it is an
    *intra-profile* inconsistency (two fields of the same file), so requiring two
    distinct files there would be false. It still cites two distinct locations,
    which the test above checks.

    ``bitrix_experience_vs_excluded`` is currently silent (T7.3 removed the ban),
    so this loop guards it only if the ban ever returns; the positive control
    ``test_bitrix_rule_fires_when_the_ban_returns`` covers the live case.
    """
    for rule_id in ("total_experience_vs_it_support", "bitrix_experience_vs_excluded"):
        for item in result.by_rule(rule_id):
            assert {ref.source for ref in item.sources} == {SOURCE_RESUME, SOURCE_PROFILE}


def test_contradictions_name_the_rule_that_produced_them(result) -> None:
    known = {rule.rule_id for rule in DEFAULT_RULES}
    for item in result.contradictions:
        assert item.rule in known
        assert item.subject and item.detail


# --------------------------------------------------------------------------
# Zero false positives on facts that agree
# --------------------------------------------------------------------------


def test_matching_contact_details_are_not_reported(result) -> None:
    """The resume and the profile state the same name and email."""
    assert result.by_rule("contact_mismatch") == []


def test_matching_languages_are_not_reported(result) -> None:
    """Resume says Russian/English; the profile lists ru, en, russian, english."""
    assert result.by_rule("language_mismatch") == []


def test_identical_profile_copies_are_not_reported(result) -> None:
    assert result.by_rule("profile_copy_drift") == []


def test_the_baseline_does_not_report_the_documented_anonymity(result) -> None:
    """The fallback has no name or email on purpose (BLE001 #44), not by accident."""
    subjects = {item.subject for item in result.by_rule("baseline_vs_profile_drift")}
    assert not any(subject.endswith((" name", " email", " phone_ru", " phone_th")) for subject in subjects)


# --- positive controls: each silent rule must be capable of firing ---------


def test_contact_rule_fires_on_a_real_mismatch(tmp_path: Path, kb_file: Path) -> None:
    other = write_profile(tmp_path, "other.json", email="someone.else@example.com")
    found = verify_all(kb_path=kb_file, profile_path=str(other)).by_rule("contact_mismatch")
    assert len(found) == 1
    assert found[0].subject == "email"
    assert {ref.source for ref in found[0].sources} == {SOURCE_RESUME, SOURCE_PROFILE}


def test_language_rule_fires_when_a_language_is_missing(tmp_path: Path, kb_file: Path) -> None:
    other = write_profile(tmp_path, "no_ru.json", languages=["en", "english"])
    found = verify_all(kb_path=kb_file, profile_path=str(other)).by_rule("language_mismatch")
    assert len(found) == 1
    assert "ru" in found[0].detail


def test_copy_drift_rule_fires_when_the_copies_disagree(
    tmp_path: Path, kb_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_assistant import candidate_profile as profile_module

    first = write_profile(tmp_path, "copy_a.json", name="Copy A")
    second = write_profile(tmp_path, "copy_b.json", name="Copy B")
    monkeypatch.setattr(profile_module, "DEFAULT_PROFILE_PATHS", [first, second])

    found = verify_all(kb_path=kb_file, profile_path=str(PROFILE_PATH)).by_rule("profile_copy_drift")
    assert len(found) == 1
    assert found[0].subject.endswith("name")


def test_baseline_drift_rule_fires_when_the_fallback_disagrees(tmp_path: Path, kb_file: Path) -> None:
    """Today it fires on the role lists; that must be data-driven, not assumed."""
    found = verify_all(kb_path=kb_file, profile_path=str(PROFILE_PATH)).by_rule(
        "baseline_vs_profile_drift"
    )
    subjects = {item.subject.rsplit(" ", 1)[-1] for item in found}
    assert subjects, "the fallback differs from the live profile on the role lists"

    for item in found:
        assert {ref.source for ref in item.sources} == {SOURCE_PROFILE, SOURCE_BASELINE}


# --------------------------------------------------------------------------
# The baseline loader
# --------------------------------------------------------------------------


def test_baseline_loader_returns_the_anonymous_fallback() -> None:
    baseline = load_baseline_profile()
    assert baseline.name is None
    assert baseline.email is None
    assert baseline.target_roles, "the fallback still targets roles"
    assert baseline.domain_years.get("it_support") == 11.0


def test_baseline_loader_restores_module_state() -> None:
    """It mutates module globals to reach the fallback; that must be undone."""
    from ai_assistant import candidate_profile as profile_module

    before = profile_module.DEFAULT_PROFILE_PATHS
    load_baseline_profile()
    assert profile_module.DEFAULT_PROFILE_PATHS is before
    assert profile_module.logger.disabled is False


def test_baseline_loader_does_not_leave_the_env_poisoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    monkeypatch.setenv("CANDIDATE_PROFILE", "some/where.json")
    load_baseline_profile()
    assert os.environ.get("CANDIDATE_PROFILE") == "some/where.json"


# --------------------------------------------------------------------------
# Profile resolution
# --------------------------------------------------------------------------


def test_resolve_profile_path_prefers_an_explicit_file(tmp_path: Path) -> None:
    explicit = write_profile(tmp_path, "explicit.json")
    assert resolve_profile_path(str(explicit)) == explicit


def test_resolve_profile_path_returns_none_for_a_missing_explicit_file(tmp_path: Path) -> None:
    assert resolve_profile_path(str(tmp_path / "absent.json")) is None


def test_resolve_profile_path_finds_the_live_profile() -> None:
    """The first match is the cwd-relative entry, exactly as the pipeline sees it."""
    resolved = resolve_profile_path()
    assert resolved is not None
    assert resolved.is_file()
    assert resolved.resolve() == PROFILE_PATH.resolve()


def test_resolve_profile_path_works_from_another_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Away from the repo the cwd-relative entries miss; the root fallback must hold."""
    monkeypatch.chdir(tmp_path)
    resolved = resolve_profile_path()
    assert resolved is not None
    assert resolved.resolve() == PROFILE_PATH.resolve()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_verify_command_prints_contradictions_with_both_sides(
    capsys: pytest.CaptureFixture, kb_file: Path
) -> None:
    code = candidate_kb_verify_cmd(kb_path=str(kb_file), profile_path=str(PROFILE_PATH))
    out = capsys.readouterr().out

    assert code == 0
    assert "cross-source verification" in out
    assert "contradictions :" in out
    for rule_id in MANDATORY_RULES:
        assert f"[{rule_id}]" in out


def test_verify_command_json_is_machine_readable(
    capsys: pytest.CaptureFixture, kb_file: Path
) -> None:
    code = candidate_kb_verify_cmd(
        kb_path=str(kb_file), profile_path=str(PROFILE_PATH), as_json=True
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["contradiction_count"] == len(payload["contradictions"]) >= 3
    assert payload["rules_silent"] == list(SILENT_RULES)
    for item in payload["contradictions"]:
        assert len(item["sources"]) >= 2


def test_verify_command_reports_a_missing_kb(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    code = candidate_kb_verify_cmd(kb_path=str(tmp_path / "absent.json"))
    captured = capsys.readouterr()

    assert code == 2
    assert "ERROR: no knowledge base at" in captured.err


def test_format_verification_groups_by_rule(result) -> None:
    text = format_verification(result)
    assert text.startswith("cross-source verification")
    assert f"contradictions : {len(result.contradictions)}" in text
    for rule_id in MANDATORY_RULES:
        assert f"[{rule_id}]" in text
    assert "rules that found nothing" in text


# --------------------------------------------------------------------------
# Verification is read-only
# --------------------------------------------------------------------------


def test_verification_modifies_nothing(capsys: pytest.CaptureFixture, kb_file: Path) -> None:
    # ai_assistant/candidate_profile.json used to be listed here. T8 deleted it:
    # it duplicated the root profile field for field and nothing read it, so
    # there is nothing left to protect.
    protected = [
        RESUME_PATH,
        PROFILE_PATH,
        REPO_ROOT / "state.db",
        kb_file,
    ]
    missing = [path for path in protected if not path.is_file()]
    assert not missing, f"expected these files to exist: {missing}"
    before = {path: sha256_of(path) for path in protected}

    candidate_kb_verify_cmd(kb_path=str(kb_file), profile_path=str(PROFILE_PATH))
    candidate_kb_verify_cmd(kb_path=str(kb_file), profile_path=str(PROFILE_PATH), as_json=True)
    capsys.readouterr()

    after = {path: sha256_of(path) for path in protected}
    assert after == before, {p: "changed" for p in protected if before[p] != after[p]}


def test_verification_does_not_touch_the_real_kb() -> None:
    """The repo-root KB is not redirected by conftest, so check it explicitly."""
    if not KB_PATH.is_file():
        pytest.skip("no repo-root knowledge base built yet")
    before = sha256_of(KB_PATH)
    verify_all()
    assert sha256_of(KB_PATH) == before
