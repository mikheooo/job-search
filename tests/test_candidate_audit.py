"""Tests for the candidate audit (Stage 0 / T4).

T1 pinned what the parser reads, T2 pinned what the knowledge base may do, T3
pinned that the three sources are actually compared. T4 pins that the three
reviewers look at the candidate and say something true about it.

Four properties carry the weight here:

* **the three behaviours the plan requires** -- the ATS reviewer catches missing
  contacts and a non-standard headline, the HR reviewer catches a date gap and a
  missing salary expectation, the tech reviewer catches a technology with no
  project behind it;
* **the tech reviewer's mandatory obligation**: it must flag the contradictions
  T3 found, and it must take them from T3's result rather than re-deriving them;
* **no finding changes any file**, proved by hashing the protected files around a
  full run rather than by asserting it in a comment;
* **every silent rule can still fire.** A rule that never fires satisfies "no
  false positives" trivially, so each silent rule gets a positive control with
  input that really does trip it. This is the same discipline as T3, and it is
  the only reason to trust a clean result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from ai_assistant.candidate.audit import (
    ARTIFACTS_DIR,
    SEVERITY_ORDER,
    AuditContext,
    Severity,
    candidate_audit_cmd,
    format_audit,
    report_path_for,
    reviewers,
    run_audit,
    run_rule_set,
    select_reviewers,
)
from ai_assistant.candidate.audit_ats import ATS_REVIEWER, ATS_RULES
from ai_assistant.candidate.audit_hr import GAP_THRESHOLD_MONTHS, HR_REVIEWER, HR_RULES
from ai_assistant.candidate.audit_tech import (
    CONTRADICTED_STATUS,
    TECH_REVIEWER,
    TECH_RULES,
)
from ai_assistant.candidate.kb import CandidateKnowledgeBase
from ai_assistant.candidate.kb_verify import (
    Contradiction,
    SourceRef,
    VerificationResult,
    load_profile,
)
from ai_assistant.candidate.resume_parser import parse_resume

REPO_ROOT = Path(__file__).resolve().parent.parent
RESUME_PATH = REPO_ROOT / "resume.md"
PROFILE_PATH = REPO_ROOT / "candidate_profile.json"
KB_PATH = REPO_ROOT / "candidate_kb.json"
STATE_DB_PATH = REPO_ROOT / "state.db"

# Files the acceptance criterion names. A run must leave every one of them
# byte-identical; the report the command writes is not among them because it is
# the command's declared output, and --no-write suppresses even that.
#
# ai_assistant/candidate_profile.json used to be in this tuple. T8 deleted it --
# it was a byte-identical copy of the root profile that nothing ever read -- so
# there is no second copy left to protect.
PROTECTED_FILES = (RESUME_PATH, PROFILE_PATH, KB_PATH, STATE_DB_PATH)

ALL_RULES = ATS_RULES + HR_RULES + TECH_RULES

# A resume that trips nothing an ATS or an HR screen would complain about, used
# as the base for the positive controls below.
CLEAN_RESUME = """# Test Candidate

**Python Developer**
Thailand | Remote | test@example.com | Telegram: @test

## SUMMARY
Python Developer with 5 years of IT experience.

## CORE SKILLS
- **Python & tools:** Python, Docker

## PROFESSIONAL EXPERIENCE

### Python Developer | Acme
*Thailand | January 2020 - December 2022*
- Built services in Python and Docker.

## EDUCATION & LANGUAGES
- **Languages:** English - B1
"""


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def kb_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A KB built from the real resume, so the tests do not depend on a local build.

    Parsed with the label ``resume.md`` rather than an absolute path so the
    evidence locations match what the CLI produces.
    """
    directory = tmp_path_factory.mktemp("t4")
    path = directory / "candidate_kb.json"
    kb = CandidateKnowledgeBase()
    kb.merge_claims(
        parse_resume(RESUME_PATH.read_text(encoding="utf-8"), "resume.md"),
        source_file="resume.md",
        source_sha256=sha256_of(RESUME_PATH),
    )
    kb.save(path)
    return path


@pytest.fixture(scope="module")
def result(kb_file: Path):
    """The full audit of the real candidate."""
    return run_audit("all", kb_path=kb_file, profile_path=str(PROFILE_PATH))


@pytest.fixture(scope="module")
def real_profile():
    return load_profile(PROFILE_PATH)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_kb(directory: Path, resume_text: str) -> tuple[Path, Path]:
    """Write a synthetic resume, parse it, and store the claims as a KB."""
    resume = directory / "resume.md"
    resume.write_text(resume_text, encoding="utf-8")
    kb_path = directory / "candidate_kb.json"
    kb = CandidateKnowledgeBase()
    kb.merge_claims(
        parse_resume(resume_text, "resume.md"),
        source_file="resume.md",
        source_sha256=sha256_of(resume),
    )
    kb.save(kb_path)
    return kb_path, resume


def audit_synthetic(tmp_path: Path, resume_text: str, mode: str = "all"):
    """Run an audit over a synthetic resume, using the real profile."""
    kb_path, resume_path = build_kb(tmp_path, resume_text)
    return run_audit(
        mode,
        kb_path=kb_path,
        profile_path=str(PROFILE_PATH),
        resume_path=str(resume_path),
    )


def rule_by_id(rules, rule_id: str):
    return next(rule for rule in rules if rule.rule_id == rule_id)


def run_one_rule(context: AuditContext, rules, rule_id: str):
    """Run exactly one rule, so "this rule found nothing" cannot hide a dead rule."""
    return run_rule_set(context, (rule_by_id(rules, rule_id),))


def context_for(resume_text: str, profile, *, contradictions=(), tmp_path: Path) -> AuditContext:
    """An AuditContext built by hand, for rules that need controlled inputs.

    ``verification`` is a real :class:`VerificationResult` with a caller-supplied
    contradiction list, so the tech reviewer's use of T3's output can be tested
    without depending on which contradictions today's data happens to contain.
    """
    kb_path, _ = build_kb(tmp_path, resume_text)
    kb = CandidateKnowledgeBase.load(kb_path)
    verification = VerificationResult(
        kb_path=kb_path,
        profile_path=None,
        baseline_label="test",
        contradictions=list(contradictions),
        rules_run=(),
        rules_silent=(),
    )
    return AuditContext(
        claims=tuple(record for record in kb.claims.values() if record.in_source),
        profile=profile,
        profile_path=None,
        profile_label="test-profile",
        resume_label="resume.md",
        verification=verification,
    )


# --------------------------------------------------------------------------
# The reviewers exist and are selectable
# --------------------------------------------------------------------------


def test_there_are_three_reviewers_one_per_mode() -> None:
    assert [reviewer.mode for reviewer in reviewers()] == ["ats", "hr", "tech"]


def test_mode_all_selects_every_reviewer() -> None:
    assert len(select_reviewers("all")) == 3


@pytest.mark.parametrize("mode", ["ats", "hr", "tech"])
def test_each_mode_selects_exactly_its_reviewer(mode: str) -> None:
    chosen = select_reviewers(mode)
    assert [reviewer.mode for reviewer in chosen] == [mode]


def test_an_unknown_mode_is_refused_not_ignored() -> None:
    """Silently running nothing would look exactly like a clean audit."""
    with pytest.raises(ValueError, match="unknown audit mode"):
        select_reviewers("nope")


def test_each_reviewer_owns_only_its_own_rules() -> None:
    """A rule filed under the wrong mode would be reported under the wrong heading."""
    for reviewer, rules in ((ATS_REVIEWER, ATS_RULES), (HR_REVIEWER, HR_RULES), (TECH_REVIEWER, TECH_RULES)):
        for rule in rules:
            assert rule.rule_id.startswith(reviewer.mode), rule.rule_id


def test_every_finding_carries_a_severity_and_a_rule(result) -> None:
    assert result.findings, "the real candidate must produce findings"
    for finding in result.findings:
        assert isinstance(finding.severity, Severity), finding.rule
        assert finding.rule and finding.subject and finding.detail


def test_findings_are_ordered_worst_first(result) -> None:
    ranked = {level: index for index, level in enumerate(SEVERITY_ORDER)}
    seen = [ranked[finding.severity] for finding in result.findings]
    assert seen == sorted(seen), [str(finding.severity) for finding in result.findings]


# --------------------------------------------------------------------------
# ATS reviewer -- required behaviour: missing contacts, non-standard headline
# --------------------------------------------------------------------------


def test_ats_catches_a_headline_that_lists_several_titles(result) -> None:
    """The plan's "non-standard headline", on the real resume."""
    found = result.by_rule("ats_headline_uses_separators")
    assert len(found) == 1, [item.subject for item in found]
    ref = found[0].evidence[0]
    assert ref.location == "resume.md:3"
    assert "|" in ref.value


def test_ats_catches_contact_channels_the_resume_omits(result) -> None:
    """The plan's "missing contacts", on the real resume: the profile's, not the resume's."""
    found = result.by_rule("ats_contact_channel_gap")
    subjects = " | ".join(item.subject for item in found)
    assert "phone" in subjects, subjects
    assert "linkedin" in subjects, subjects
    assert "github" in subjects, subjects
    assert "portfolio" in subjects, subjects

    phone = next(item for item in found if "phone" in item.subject)
    assert phone.severity == Severity.HIGH
    fields = {ref.location.rsplit(":", 1)[-1] for ref in phone.evidence}
    assert fields == {"phone_ru", "phone_th"}, fields


def test_ats_contact_gap_does_not_double_count_a_total_absence(tmp_path: Path) -> None:
    """When there is no channel at all, the blocker is the finding -- not five gaps.

    A resume with no contacts at all would otherwise produce one BLOCKER plus one
    gap finding per declared channel, turning one problem into five.
    """
    audit = audit_synthetic(
        tmp_path,
        CLEAN_RESUME.replace(
            "Thailand | Remote | test@example.com | Telegram: @test",
            "Thailand | Remote",
        ),
    )
    assert len(audit.by_rule("ats_no_contact_channel")) == 1
    assert audit.by_rule("ats_contact_channel_gap") == []


def test_ats_names_only_the_channel_values_that_are_really_missing(tmp_path: Path) -> None:
    """The false-positive control, at the precision the rule claims.

    The profile declares *two* phone numbers. A resume carrying one of them must be
    reported for the other one only -- naming a whole channel when half of it is
    present would be the kind of overstatement that makes an audit untrustworthy.
    """
    profile = load_profile(PROFILE_PATH)
    assert profile.phone_th and profile.phone_ru, "the fixture profile must declare both phones"
    resume = CLEAN_RESUME.replace(
        "Thailand | Remote | test@example.com | Telegram: @test",
        f"Thailand | Remote | {profile.email} | Telegram: @test | {profile.phone_th}",
    )
    kb_path, resume_path = build_kb(tmp_path, resume)
    audit = run_audit(
        "ats", kb_path=kb_path, profile_path=str(PROFILE_PATH), resume_path=str(resume_path)
    )
    found = audit.by_rule("ats_contact_channel_gap")

    phone = next(item for item in found if "phone" in item.subject)
    assert "phone_ru" in phone.subject
    assert "phone_th" not in phone.subject, phone.subject
    assert {ref.location.rsplit(":", 1)[-1] for ref in phone.evidence} == {"phone_ru"}

    # The email is in the resume, so that channel must not be reported at all.
    assert not any("email" in item.subject for item in found), [item.subject for item in found]


# --- positive controls for the ATS rules that are silent on the real data ---


def test_ats_no_contact_channel_fires_on_a_resume_without_contacts(tmp_path: Path) -> None:
    audit = audit_synthetic(
        tmp_path,
        CLEAN_RESUME.replace(
            "Thailand | Remote | test@example.com | Telegram: @test",
            "Thailand | Remote",
        ),
    )
    found = audit.by_rule("ats_no_contact_channel")
    assert len(found) == 1, [item.subject for item in found]
    assert found[0].severity == Severity.BLOCKER


def test_ats_headline_absent_fires_on_a_resume_without_a_headline(tmp_path: Path) -> None:
    audit = audit_synthetic(
        tmp_path,
        CLEAN_RESUME.replace("**Python Developer**\n", ""),
    )
    found = audit.by_rule("ats_headline_absent")
    assert len(found) == 1, [item.subject for item in found]
    assert found[0].severity == Severity.HIGH


def test_ats_headline_not_a_job_title_fires_on_a_cliche(tmp_path: Path) -> None:
    audit = audit_synthetic(
        tmp_path,
        CLEAN_RESUME.replace("**Python Developer**", "**Code Ninja | Python Developer**"),
    )
    found = audit.by_rule("ats_headline_not_a_job_title")
    assert len(found) == 1, [item.subject for item in found]
    assert "Ninja" in found[0].subject


def test_ats_headline_not_a_job_title_stays_silent_on_real_titles(result) -> None:
    """The other half of the control: ordinary titles must not be flagged."""
    assert result.by_rule("ats_headline_not_a_job_title") == []


# --------------------------------------------------------------------------
# HR reviewer -- required behaviour: date gap, missing salary expectation
# --------------------------------------------------------------------------


def test_hr_catches_the_real_date_gap(result) -> None:
    """The plan's "date gap", on the real resume: 2018 -> April 2021."""
    found = result.by_rule("hr_employment_gap")
    assert len(found) == 1, [item.subject for item in found]
    assert found[0].severity == Severity.HIGH
    assert found[0].subject.startswith("27 months"), found[0].subject

    locations = {ref.location for ref in found[0].evidence}
    assert "resume.md:38" in locations, locations
    assert "resume.md:33" in locations, locations
    assert "resume.md:39" in locations, locations
    assert "resume.md:34" in locations, locations


def test_hr_catches_the_missing_salary_expectation(result) -> None:
    """The plan's "missing salary expectations", with the profile's figure as evidence."""
    found = result.by_rule("hr_no_salary_expectation")
    assert len(found) == 1, [item.subject for item in found]
    assert found[0].severity == Severity.MEDIUM
    assert found[0].evidence, "the profile's minimum_salary is what makes this checkable"
    assert found[0].evidence[0].location.endswith("minimum_salary")

    # Read the figure instead of hardcoding it: T7.3 lowered the floor from 1500
    # to the user-confirmed 1000 USD, and a literal here would have to be edited
    # on every future preference change while adding no protection.
    live_floor = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["minimum_salary"]
    assert str(float(live_floor)) in found[0].evidence[0].value


def test_hr_gap_threshold_does_not_flag_a_short_gap(tmp_path: Path) -> None:
    """Two months between jobs is ordinary. The threshold is a judgement, so it is pinned.

    Without this, a future edit that lowered ``GAP_THRESHOLD_MONTHS`` would turn
    the reviewer into noise and no test would notice.
    """
    resume = CLEAN_RESUME.replace(
        "### Python Developer | Acme\n*Thailand | January 2020 - December 2022*",
        "### Python Developer | Acme\n*Thailand | January 2020 - December 2022*\n"
        "- Built services in Python and Docker.\n\n"
        "### Senior Developer | Globex\n*Thailand | March 2023 - December 2024*",
    )
    audit = audit_synthetic(tmp_path, resume)
    assert audit.by_rule("hr_employment_gap") == [], [
        item.subject for item in audit.by_rule("hr_employment_gap")
    ]
    assert GAP_THRESHOLD_MONTHS == 3, "the threshold this test pins"


def test_hr_gap_threshold_does_flag_a_long_gap(tmp_path: Path) -> None:
    """The other side of the same threshold: a real gap must still be reported."""
    resume = CLEAN_RESUME.replace(
        "### Python Developer | Acme\n*Thailand | January 2020 - December 2022*",
        "### Python Developer | Acme\n*Thailand | January 2020 - December 2022*\n"
        "- Built services in Python and Docker.\n\n"
        "### Senior Developer | Globex\n*Thailand | June 2024 - December 2024*",
    )
    audit = audit_synthetic(tmp_path, resume)
    found = audit.by_rule("hr_employment_gap")
    assert len(found) == 1, [item.subject for item in found]
    assert found[0].subject.startswith("17 months"), found[0].subject


def test_hr_salary_rule_stays_silent_when_the_resume_states_a_figure(tmp_path: Path) -> None:
    """False-positive control: a stated expectation must not be reported as missing.

    The figure goes in the summary rather than on its own line because the parser
    only turns prose inside a known section into a claim -- a line it cannot see
    would make this control prove nothing.
    """
    audit = audit_synthetic(
        tmp_path,
        CLEAN_RESUME.replace(
            "Python Developer with 5 years of IT experience.",
            "Python Developer with 5 years of IT experience. Expected salary: 2500 USD per month.",
        ),
    )
    assert audit.by_rule("hr_no_salary_expectation") == []


# --- positive controls for the HR rules that are silent on the real data ---


def test_hr_role_without_dates_fires_on_an_undated_role(tmp_path: Path) -> None:
    audit = audit_synthetic(
        tmp_path,
        CLEAN_RESUME.replace(
            "*Thailand | January 2020 - December 2022*\n",
            "",
        ),
    )
    found = audit.by_rule("hr_role_without_dates")
    assert len(found) == 1, [item.subject for item in found]
    assert found[0].severity == Severity.HIGH


def test_hr_unparsed_period_fires_on_a_season(tmp_path: Path) -> None:
    """A season is text a reader understands and a date filter cannot."""
    audit = audit_synthetic(
        tmp_path,
        CLEAN_RESUME.replace(
            "*Thailand | January 2020 - December 2022*",
            "*Thailand | Spring 2020 - December 2022*",
        ),
    )
    found = audit.by_rule("hr_unparsed_period")
    assert len(found) == 1, [item.subject for item in found]
    assert "Spring 2020" in found[0].subject


def test_hr_overlapping_roles_fires_on_overlapping_dates(tmp_path: Path) -> None:
    resume = CLEAN_RESUME.replace(
        "### Python Developer | Acme\n*Thailand | January 2020 - December 2022*",
        "### Python Developer | Acme\n*Thailand | January 2020 - December 2022*\n"
        "- Built services in Python and Docker.\n\n"
        "### Senior Developer | Globex\n*Thailand | June 2022 - December 2024*",
    )
    audit = audit_synthetic(tmp_path, resume)
    found = audit.by_rule("hr_overlapping_roles")
    assert len(found) == 1, [item.subject for item in found]


def test_hr_does_not_flag_an_open_ended_role_as_unparsed(result) -> None:
    """'Present' has no month to normalise to, and that is intent, not a failure."""
    assert result.by_rule("hr_unparsed_period") == []
    assert result.by_rule("hr_role_without_dates") == []
    assert result.by_rule("hr_overlapping_roles") == []


# --------------------------------------------------------------------------
# Tech reviewer -- required behaviour: technology without project context
# --------------------------------------------------------------------------


def test_tech_catches_a_skill_no_role_demonstrates(result) -> None:
    """The plan's "technology without project context", on the real resume."""
    found = result.by_rule("tech_skill_without_project_context")
    assert found, "the real resume claims skills its roles never mention"
    subjects = " | ".join(item.subject for item in found)
    for expected in ("MS SQL", "PostgreSQL", "Git", "Make"):
        assert f"'{expected}'" in subjects, f"{expected} missing from: {subjects}"


def test_tech_does_not_flag_a_skill_a_role_demonstrates(result) -> None:
    """False-positive control: Python, Docker, n8n and aiogram are all shown in a role."""
    flagged = {item.subject for item in result.by_rule("tech_skill_without_project_context")}
    for shown in ("Python", "Docker", "n8n", "aiogram", "Telethon", "Linux", "Bash"):
        assert not any(f"'{shown}'" in subject for subject in flagged), f"{shown} was wrongly flagged"


def test_tech_summary_is_not_counted_as_project_context(result) -> None:
    """'Make' appears in the summary and in no role, and the finding must say so.

    This is the rule's most arguable case, so it is pinned explicitly: the
    summary lists technologies in prose, and a mention there is not a project.
    """
    found = [
        item
        for item in result.by_rule("tech_skill_without_project_context")
        if item.subject.startswith("'Make'")
    ]
    assert len(found) == 1, [item.subject for item in found]
    assert "summary is deliberately not counted" in found[0].detail


def test_tech_catches_the_absence_of_any_quantified_outcome(result) -> None:
    found = result.by_rule("tech_no_quantified_achievement")
    assert len(found) == 1, [item.subject for item in found]
    assert found[0].severity == Severity.LOW


def test_tech_role_without_bullets_fires_on_a_bare_role(tmp_path: Path) -> None:
    audit = audit_synthetic(
        tmp_path,
        CLEAN_RESUME.replace(
            "- Built services in Python and Docker.\n",
            "",
        ),
    )
    found = audit.by_rule("tech_role_without_bullets")
    assert len(found) == 1, [item.subject for item in found]
    assert found[0].severity == Severity.MEDIUM


# --------------------------------------------------------------------------
# The mandatory obligation: the tech lead must flag T3's contradictions
# --------------------------------------------------------------------------


def test_pin_tech_flags_every_contradiction_t3_found(result) -> None:
    """The plan's separate pin: the tech lead must mark T3's CONTRADICTED claims.

    The count is compared against T3's own result object rather than against a
    number, so this pin cannot drift away from what ``candidate-kb verify`` shows
    the candidate.
    """
    found = result.by_rule("tech_contradicted_claim")
    assert len(found) == len(result.verification.contradictions)
    assert found, "today's sources really do contradict each other"

    for finding in found:
        assert finding.status == CONTRADICTED_STATUS, finding.subject
        assert finding.severity == Severity.HIGH
        assert finding.evidence, "a contradiction must cite the sources it came from"


def test_pin_tech_contradiction_findings_quote_t3_verbatim(result) -> None:
    """The finding must carry T3's own rule name, not a re-derived one."""
    rules_in_findings = {finding.subject for finding in result.by_rule("tech_contradicted_claim")}
    subjects_in_t3 = {item.subject for item in result.verification.contradictions}
    assert rules_in_findings == subjects_in_t3


def test_tech_contradiction_rule_is_silent_without_contradictions(real_profile, tmp_path: Path) -> None:
    """Self-proof: with no contradictions from T3, the rule must find nothing.

    This is what distinguishes "reads T3's output" from "prints a fixed list".
    """
    context = context_for(CLEAN_RESUME, real_profile, contradictions=(), tmp_path=tmp_path)
    assert run_one_rule(context, TECH_RULES, "tech_contradicted_claim") == []


def test_tech_contradiction_rule_fires_on_a_supplied_contradiction(real_profile, tmp_path: Path) -> None:
    """The other half: a contradiction handed to it must come back as a finding."""
    supplied = Contradiction(
        rule="supplied_rule",
        subject="a supplied subject",
        detail="a supplied detail",
        sources=(SourceRef(source="resume", location="resume.md:1", value="x"),),
    )
    context = context_for(CLEAN_RESUME, real_profile, contradictions=(supplied,), tmp_path=tmp_path)
    found = run_one_rule(context, TECH_RULES, "tech_contradicted_claim")
    assert len(found) == 1, [item.subject for item in found]
    assert found[0].status == CONTRADICTED_STATUS
    assert found[0].subject == "a supplied subject"


def test_the_audit_never_writes_a_contradicted_status_into_the_kb(kb_file: Path, real_profile, tmp_path: Path) -> None:
    """``CONTRADICTED`` marks a finding; it is not a KB status.

    T3 kept it out of storage because resolving a contradiction is a decision.
    T4 must not smuggle it in through the back door.
    """
    before = sha256_of(kb_file)
    run_audit("tech", kb_path=kb_file, profile_path=str(PROFILE_PATH))
    assert sha256_of(kb_file) == before

    kb = CandidateKnowledgeBase.load(kb_file)
    statuses = {record.status for record in kb.claims.values()}
    assert CONTRADICTED_STATUS not in statuses, statuses


# --------------------------------------------------------------------------
# Self-proof: no rule is wired to a constant
# --------------------------------------------------------------------------


def test_every_rule_can_be_disabled_individually(real_profile, tmp_path: Path, result) -> None:
    """Removing a rule must remove its findings.

    If a rule were wired to a constant rather than to the data, its findings would
    survive its own removal and this test would fail. The generic form of "the pin
    fails if the check is switched off".
    """
    context = context_for(RESUME_PATH.read_text(encoding="utf-8"), real_profile, tmp_path=tmp_path)
    context = replace(context, verification=result.verification)

    for rule in ALL_RULES:
        without = tuple(item for item in ALL_RULES if item.rule_id != rule.rule_id)
        still_there = [item for item in run_rule_set(context, without) if item.rule == rule.rule_id]
        assert still_there == [], f"{rule.rule_id} survived its own removal"


def test_running_no_rules_yields_nothing(real_profile, tmp_path: Path) -> None:
    """The baseline the self-proof above rests on."""
    context = context_for(CLEAN_RESUME, real_profile, tmp_path=tmp_path)
    assert run_rule_set(context, ()) == []


# --------------------------------------------------------------------------
# The acceptance criterion: no finding changes any file
# --------------------------------------------------------------------------


def test_a_full_audit_changes_no_protected_file(tmp_path: Path) -> None:
    """Hashes before and after a real run, including the report write."""
    before = {path: sha256_of(path) for path in PROTECTED_FILES}
    code = candidate_audit_cmd(
        mode="all",
        kb_path=str(KB_PATH),
        profile_path=str(PROFILE_PATH),
        out_path=str(tmp_path / "report.md"),
    )
    assert code == 0
    after = {path: sha256_of(path) for path in PROTECTED_FILES}
    assert after == before, [path for path in before if before[path] != after[path]]


def test_no_write_creates_no_file_at_all(tmp_path: Path) -> None:
    """``--no-write`` is the strictest form of the same promise.

    Asserted on an output *directory* rather than on ``tmp_path`` itself, because
    the shared conftest fixture already puts ``global_runtime/`` in every
    ``tmp_path`` -- an empty-directory assertion there would fail for a reason
    that has nothing to do with this command.
    """
    target = tmp_path / "out"
    code = candidate_audit_cmd(
        mode="all",
        kb_path=str(KB_PATH),
        profile_path=str(PROFILE_PATH),
        out_path=str(target / "report.md"),
        write=False,
    )
    assert code == 0
    assert not target.exists(), list(target.iterdir()) if target.exists() else []


def test_the_only_file_the_command_writes_is_its_report(tmp_path: Path) -> None:
    directory = tmp_path / "out"
    code = candidate_audit_cmd(
        mode="ats",
        kb_path=str(KB_PATH),
        profile_path=str(PROFILE_PATH),
        out_path=str(directory / "report.md"),
    )
    assert code == 0
    assert [path.name for path in directory.iterdir()] == ["report.md"]


def test_the_report_contains_every_finding_and_its_severity(result) -> None:
    """The plan's acceptance: findings with severity, in the report."""
    text = format_audit(result)
    for level in SEVERITY_ORDER:
        assert f"{level}: " in text
    for finding in result.findings:
        assert finding.rule in text
        assert finding.subject[:40] in text


# --------------------------------------------------------------------------
# CLI behaviour
# --------------------------------------------------------------------------


def test_the_default_report_path_is_dated_and_under_artifacts() -> None:
    path = report_path_for(date(2026, 1, 2))
    assert path == ARTIFACTS_DIR / "candidate_audit_2026-01-02.md"
    assert path.parent.name == "artifacts"


def test_a_missing_knowledge_base_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = candidate_audit_cmd(
        mode="all",
        kb_path=str(tmp_path / "absent.json"),
        profile_path=str(PROFILE_PATH),
        out_path=str(tmp_path / "report.md"),
    )
    assert code == 2
    assert "no knowledge base" in capsys.readouterr().err


def test_an_unknown_mode_exits_two_without_writing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Refused before anything is written, so a typo cannot leave a stray report."""
    target = tmp_path / "out"
    code = candidate_audit_cmd(
        mode="nope",
        kb_path=str(KB_PATH),
        profile_path=str(PROFILE_PATH),
        out_path=str(target / "report.md"),
    )
    assert code == 2
    assert "unknown audit mode" in capsys.readouterr().err
    assert not target.exists()


def test_findings_do_not_change_the_exit_code(tmp_path: Path) -> None:
    """Findings are the product of the audit, not a failure of it.

    A non-zero code would make the command unusable from a script, which could
    not then tell "found 27 things" from "could not run".
    """
    code = candidate_audit_cmd(
        mode="all",
        kb_path=str(KB_PATH),
        profile_path=str(PROFILE_PATH),
        out_path=str(tmp_path / "report.md"),
        write=False,
    )
    assert code == 0


def test_json_output_is_pure_json_and_reports_the_report_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = tmp_path / "report.md"
    code = candidate_audit_cmd(
        mode="ats",
        kb_path=str(KB_PATH),
        profile_path=str(PROFILE_PATH),
        out_path=str(report),
        as_json=True,
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "ats"
    assert payload["finding_count"] == len(payload["findings"])
    assert payload["report_path"] == str(report)
    assert set(payload["counts_by_severity"]) == {str(level) for level in SEVERITY_ORDER}


def _kb_with_source_sha(tmp_path: Path, source_sha256: str | None) -> Path:
    """A KB built from CLEAN_RESUME, with a chosen recorded source hash."""
    kb_path = tmp_path / "candidate_kb.json"
    kb = CandidateKnowledgeBase()
    kb.merge_claims(
        parse_resume(CLEAN_RESUME, "resume.md"),
        source_file="resume.md",
        source_sha256=source_sha256,
    )
    kb.save(kb_path)
    return kb_path


def test_a_resume_edited_since_the_last_extract_is_reported(tmp_path: Path) -> None:
    """Auditing a snapshot while the resume has moved must say so.

    The audit reads the KB, so a resume edited since the last extract makes every
    finding describe text that no longer exists.
    """
    resume = tmp_path / "resume.md"
    resume.write_text(CLEAN_RESUME, encoding="utf-8")
    kb_path = _kb_with_source_sha(tmp_path, "0" * 64)

    audit = run_audit("ats", kb_path=kb_path, profile_path=str(PROFILE_PATH), resume_path=str(resume))
    assert audit.stale_note is not None
    assert "has changed since the knowledge base was built" in audit.stale_note
    assert "re-run" in audit.stale_note


def test_a_missing_resume_is_reported_as_unchecked_not_as_fresh(tmp_path: Path) -> None:
    """Not being able to look must never read as "all clear"."""
    kb_path = _kb_with_source_sha(tmp_path, "0" * 64)
    audit = run_audit(
        "ats",
        kb_path=kb_path,
        profile_path=str(PROFILE_PATH),
        resume_path=str(tmp_path / "absent.md"),
    )
    assert audit.stale_note is not None
    assert "was not found" in audit.stale_note


def test_a_kb_without_a_recorded_hash_is_reported_as_unknown(tmp_path: Path) -> None:
    resume = tmp_path / "resume.md"
    resume.write_text(CLEAN_RESUME, encoding="utf-8")
    kb_path = _kb_with_source_sha(tmp_path, None)

    audit = run_audit("ats", kb_path=kb_path, profile_path=str(PROFILE_PATH), resume_path=str(resume))
    assert audit.stale_note is not None
    assert "records no source hash" in audit.stale_note


def test_a_current_knowledge_base_carries_no_staleness_warning(result) -> None:
    assert result.stale_note is None


def test_the_real_audit_finds_something_in_every_mode(result) -> None:
    """A reviewer that silently finds nothing is the failure mode this guards."""
    assert result.reviewers_run == ("ats", "hr", "tech")
    for mode in ("ats", "hr", "tech"):
        assert any(finding.rule.startswith(mode) for finding in result.findings), mode


def test_the_summary_counts_match_the_findings(result) -> None:
    counts = result.counts_by_severity()
    assert sum(counts.values()) == len(result.findings)
    assert counts[str(result.worst())] > 0
