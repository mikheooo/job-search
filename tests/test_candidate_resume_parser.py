"""Tests for the Candidate Intelligence resume parser (Stage 0 / T1).

These pin the properties that make the extraction trustworthy rather than merely
plausible:

* the real resume's facts survive extraction (Avantis, 2009-2018, education,
  languages, contacts);
* every claim quotes a literal substring of the line it cites, so a claim can
  never outrun its source;
* bullets stay bound to the role they were listed under;
* ids are stable and content-derived;
* nothing is invented -- in particular the resume's own "15+ years of IT
  experience" is never turned into a number, and a category the resume does not
  mark (quantified achievements) comes back empty instead of guessed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ai_assistant.candidate.resume_parser import (
    FALLBACK_RESUME_CANDIDATES,
    STATUS_DERIVED_FROM_SOURCE,
    candidate_kb_extract_cmd,
    format_claims,
    normalize_date,
    parse_resume,
    resolve_resume_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESUME_PATH = REPO_ROOT / "resume.md"

# Every kind the parser can emit. Pinned so that adding a kind is a deliberate
# act with a test behind it, not an accident.
EXPECTED_KINDS = {
    "name",
    "headline",
    "title",
    "contact_location",
    "contact_email",
    "contact_telegram",
    "contact_other",
    "work_mode",
    "summary",
    "experience_statement",
    "skill_group",
    "skill",
    "role",
    "role_title",
    "role_company",
    "role_employer",
    "role_location",
    "role_start",
    "role_end",
    "duty",
    "achievement",
    "education_institution",
    "education_program",
    "education_level",
    "education_year",
    "education_note",
    "language",
    "language_level",
}


@pytest.fixture(scope="module")
def resume_text() -> str:
    return RESUME_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def resume_lines(resume_text: str) -> list[str]:
    return resume_text.splitlines()


@pytest.fixture(scope="module")
def claims(resume_text: str) -> list:
    return parse_resume(resume_text, "resume.md")


def of_kind(claims: list, kind: str) -> list:
    return [claim for claim in claims if claim.kind == kind]


def values(claims: list, kind: str) -> list[str]:
    return [claim.value for claim in of_kind(claims, kind)]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# The four mandatory pins
# --------------------------------------------------------------------------


def test_contacts_pin(claims: list) -> None:
    emails = values(claims, "contact_email")
    assert emails == ["mikhailthaiban@gmail.com"], emails
    telegram = of_kind(claims, "contact_telegram")
    assert len(telegram) == 1
    assert telegram[0].normalized == "@brokeassh"
    assert telegram[0].value == "Telegram: @brokeassh"


def test_avantis_role_pin(claims: list) -> None:
    """The Avantis role must be found, with its own bullets bound to it."""
    roles = of_kind(claims, "role")
    avantis = [r for r in roles if "Avantis" in r.value]
    assert len(avantis) == 1, values(claims, "role")
    avantis_id = avantis[0].claim_id

    titles = [c for c in of_kind(claims, "role_title") if c.role_id == avantis_id]
    assert titles and titles[0].value == "Bitrix24 Technical Support Specialist"
    companies = [c for c in of_kind(claims, "role_company") if c.role_id == avantis_id]
    assert companies and companies[0].value == "Avantis"

    duties = [c for c in of_kind(claims, "duty") if c.role_id == avantis_id]
    assert len(duties) == 2, [d.value for d in duties]
    assert any("self-hosted Bitrix24" in d.value for d in duties)
    assert any("technical support and troubleshooting" in d.value for d in duties)


def test_earlier_block_2009_2018_pin(claims: list) -> None:
    """The 2009-2018 earlier-experience block, including its four employers."""
    starts = [c for c in of_kind(claims, "role_start") if c.value == "2009"]
    ends = [c for c in of_kind(claims, "role_end") if c.value == "2018"]
    assert len(starts) == 1, values(claims, "role_start")
    assert len(ends) == 1, values(claims, "role_end")
    assert starts[0].normalized == "2009"
    assert ends[0].normalized == "2018"
    assert starts[0].role_id == ends[0].role_id

    employers = [c for c in of_kind(claims, "role_employer") if c.role_id == starts[0].role_id]
    assert [e.value for e in employers] == [
        "Speedcast / Germsat",
        "Intravision",
        "Slavyanka",
        "Russian Post",
    ]


def test_education_pin(claims: list) -> None:
    assert values(claims, "education_institution") == ["Sakhalin State University"]
    assert values(claims, "education_program") == [
        "Technological Institute, Technology and Entrepreneurship"
    ]
    assert values(claims, "education_level") == ["Higher Education"]
    year = of_kind(claims, "education_year")
    assert len(year) == 1
    assert year[0].value == "2005"
    assert year[0].normalized == "2005"


def test_languages_pin(claims: list) -> None:
    languages = of_kind(claims, "language")
    assert [lang.value for lang in languages] == ["Russian", "English"]
    assert [lang.normalized for lang in languages] == ["ru", "en"]
    levels = [c for c in of_kind(claims, "language_level")]
    assert [level.value for level in levels] == ["Native", "B1 (Intermediate)"]
    assert [level.parent_id for level in levels] == [lang.claim_id for lang in languages]


# --------------------------------------------------------------------------
# Completeness and binding
# --------------------------------------------------------------------------


def test_every_expected_role_is_found(claims: list) -> None:
    roles = values(claims, "role")
    assert len(roles) == 4, roles
    assert "Sayama Travel" in " ".join(roles)
    assert "Avantis" in " ".join(roles)
    assert "Freelance" in " ".join(roles)


def test_role_and_date_counts_match_the_source(claims: list, resume_lines: list) -> None:
    """Counts are read off the source, not asserted from memory."""
    headings = [line for line in resume_lines if line.strip().startswith("### ")]
    assert len(of_kind(claims, "role")) == len(headings) == 4

    # Single-asterisk italic lines only: the headline is **bold** and must not count.
    italic_meta = [
        line
        for line in resume_lines
        if line.strip().startswith("*")
        and not line.strip().startswith("**")
        and line.strip().endswith("*")
        and not line.strip().endswith("**")
    ]
    assert len(of_kind(claims, "role_start")) == len(italic_meta) == 4
    assert len(of_kind(claims, "role_end")) == len(italic_meta) == 4


def test_every_duty_is_bound_to_a_real_role(claims: list) -> None:
    role_ids = {role.claim_id for role in of_kind(claims, "role")}
    bullets = of_kind(claims, "duty") + of_kind(claims, "achievement")
    assert bullets, "expected role bullets"
    for bullet in bullets:
        assert bullet.role_id in role_ids, bullet
    assert len(of_kind(claims, "duty")) == 15


def test_skill_groups_and_skills_are_linked(claims: list) -> None:
    groups = of_kind(claims, "skill_group")
    assert [g.value for g in groups] == ["AI & LLM", "Python & integrations", "Automation & delivery"]
    group_ids = {group.claim_id for group in groups}
    skills = of_kind(claims, "skill")
    assert len(skills) == 32, len(skills)
    for skill in skills:
        assert skill.parent_id in group_ids, skill


def test_observed_kind_set_is_exactly_expected(claims: list) -> None:
    observed = {claim.kind for claim in claims}
    assert observed <= EXPECTED_KINDS, observed - EXPECTED_KINDS
    # The real resume marks no quantified achievement, so the category is empty.
    assert "achievement" not in observed
    assert "contact_other" not in observed
    assert "education_note" not in observed


def test_todays_measured_totals(claims: list) -> None:
    """The measurement recorded on 2026-09-22 against resume.md.

    Deliberately a separate test: if the resume is edited, this is the one that
    should be updated, and it should be updated *from a fresh run*, not by
    relaxing the count.
    """
    assert len(claims) == 96
    assert len(of_kind(claims, "role")) == 4
    assert len(of_kind(claims, "duty")) == 15
    assert len(of_kind(claims, "achievement")) == 0
    assert len(of_kind(claims, "skill")) == 32
    assert len(of_kind(claims, "skill_group")) == 3
    assert len(of_kind(claims, "role_employer")) == 4


# --------------------------------------------------------------------------
# Evidence: the anti-fabrication invariant
# --------------------------------------------------------------------------


def test_every_quote_is_a_literal_substring_of_its_cited_line(claims: list, resume_lines: list) -> None:
    for claim in claims:
        line = resume_lines[claim.evidence.line - 1]
        assert claim.evidence.quote in line, (claim.kind, claim.evidence, line)


def test_line_numbers_are_one_based_and_in_range(claims: list, resume_lines: list) -> None:
    for claim in claims:
        assert 1 <= claim.evidence.line <= len(resume_lines), claim


def test_status_is_always_derived_from_source(claims: list) -> None:
    assert {claim.status for claim in claims} == {STATUS_DERIVED_FROM_SOURCE}


def test_evidence_names_the_source_file(claims: list) -> None:
    assert {claim.evidence.source_file for claim in claims} == {"resume.md"}


def test_known_line_numbers_are_exact(claims: list) -> None:
    """A few anchors by hand, so an off-by-one in line counting cannot hide."""
    by_kind_and_value = {(c.kind, c.value): c.evidence.line for c in claims}
    assert by_kind_and_value[("name", "Mikhail Kolesnikov")] == 1
    assert by_kind_and_value[("headline", "AI Engineer | AI Automation Engineer | Python Developer")] == 3
    assert by_kind_and_value[("skill_group", "AI & LLM")] == 10
    assert by_kind_and_value[("role", "AI Automation Engineer | Sayama Travel")] == 24
    assert by_kind_and_value[("role", "Bitrix24 Technical Support Specialist | Avantis")] == 33
    assert by_kind_and_value[("education_institution", "Sakhalin State University")] == 44
    assert by_kind_and_value[("language", "Russian")] == 45
    summary = of_kind(claims, "summary")
    assert len(summary) == 1
    assert summary[0].evidence.line == 7
    assert summary[0].evidence.quote == summary[0].value


# --------------------------------------------------------------------------
# Normalisation: only where unambiguous
# --------------------------------------------------------------------------


def test_normalize_date_maps_month_names_and_years() -> None:
    assert normalize_date("May 2026") == "2026-05"
    assert normalize_date("November 2022") == "2022-11"
    assert normalize_date("2009") == "2009"
    assert normalize_date("Present") is None
    assert normalize_date("") is None
    assert normalize_date("somewhen") is None


def test_total_experience_is_never_normalized_into_a_number(claims: list) -> None:
    statements = of_kind(claims, "experience_statement")
    assert len(statements) == 1
    assert statements[0].value == "15+ years of IT experience"
    assert statements[0].normalized is None
    # And no claim anywhere claims to know a total tenure as a number.
    assert not any(claim.kind == "total_experience" for claim in claims)
    assert not any(
        claim.normalized is not None and "year" in claim.normalized.lower() for claim in claims
    )


def test_normalized_values_stay_within_the_unambiguous_sets(claims: list) -> None:
    allowed = {"TH", "ru", "en", "mikhailthaiban@gmail.com", "@brokeassh", "2005", "2009", "2018"}
    for claim in claims:
        if claim.normalized is None:
            continue
        if claim.kind in {"role_start", "role_end"}:
            assert claim.normalized[:4].isdigit(), claim
            continue
        assert claim.normalized in allowed, claim


def test_present_end_date_has_no_invented_normalization(claims: list) -> None:
    present = [c for c in of_kind(claims, "role_end") if c.value == "Present"]
    assert len(present) == 1
    assert present[0].normalized is None


def test_city_location_is_not_promoted_to_a_country(claims: list) -> None:
    moscow = [c for c in of_kind(claims, "role_location") if c.value == "Moscow"]
    assert len(moscow) == 1
    assert moscow[0].normalized is None


def test_languages_are_not_duplicated_out_of_the_summary(claims: list) -> None:
    """The summary mentions 'English B1'; that must not become a second language claim."""
    assert len(of_kind(claims, "language")) == 2


# --------------------------------------------------------------------------
# Ids
# --------------------------------------------------------------------------


def test_ids_are_unique(claims: list) -> None:
    ids = [claim.claim_id for claim in claims]
    assert len(set(ids)) == len(ids)


def test_ids_are_stable_across_parses(resume_text: str) -> None:
    first = parse_resume(resume_text, "resume.md")
    second = parse_resume(resume_text, "resume.md")
    assert [c.claim_id for c in first] == [c.claim_id for c in second]
    assert [c.to_dict() for c in first] == [c.to_dict() for c in second]


def test_ids_do_not_depend_on_unrelated_lines(resume_text: str) -> None:
    """Ids are content-derived: shifting the document must not renumber them."""
    before = parse_resume(resume_text, "resume.md")
    shifted = parse_resume("# unrelated comment line\n" + resume_text, "resume.md")

    email_before = next(c for c in before if c.kind == "contact_email")
    email_after = next(c for c in shifted if c.kind == "contact_email")
    assert email_after.claim_id == email_before.claim_id
    assert email_after.evidence.line == email_before.evidence.line + 1


def test_duplicate_values_still_get_distinct_ids() -> None:
    text = "# N\n\n## PROFESSIONAL EXPERIENCE\n\n### T | C\n- same bullet\n- same bullet\n"
    claims = parse_resume(text, "x.md")
    duties = of_kind(claims, "duty")
    assert len(duties) == 2
    assert duties[0].claim_id != duties[1].claim_id


# --------------------------------------------------------------------------
# Achievements: marked in the source, never inferred
# --------------------------------------------------------------------------


def test_achievement_rule_fires_only_on_a_marked_bullet() -> None:
    text = (
        "# N\n\n## PROFESSIONAL EXPERIENCE\n\n### T | C\n"
        "- Reduced processing time by 40% across the pipeline.\n"
        "- Automated routine administrative tasks with Bash and PowerShell.\n"
        "- Built an internal reporting tool.\n"
    )
    claims = parse_resume(text, "x.md")
    achievements = values(claims, "achievement")
    duties = values(claims, "duty")
    assert achievements == ["Reduced processing time by 40% across the pipeline."]
    assert "Automated routine administrative tasks with Bash and PowerShell." in duties
    assert "Built an internal reporting tool." in duties


# --------------------------------------------------------------------------
# Degenerate input
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n", "\t \n  \n"])
def test_empty_input_yields_no_claims(text: str) -> None:
    assert parse_resume(text, "x.md") == []


def test_bad_markup_does_not_crash_and_stays_honest() -> None:
    text = "# \n### \n**unclosed\n| | |\n- \nplain text with no structure\n"
    claims = parse_resume(text, "x.md")
    assert isinstance(claims, list)
    for claim in claims:
        assert claim.evidence.quote in text.splitlines()[claim.evidence.line - 1]


def test_role_without_dates_yields_no_date_claims() -> None:
    text = "# N\n\n## PROFESSIONAL EXPERIENCE\n\n### Some Title | Some Co\n- Did a thing.\n"
    claims = parse_resume(text, "x.md")
    assert values(claims, "role") == ["Some Title | Some Co"]
    assert values(claims, "role_title") == ["Some Title"]
    assert values(claims, "role_company") == ["Some Co"]
    assert values(claims, "duty") == ["Did a thing."]
    assert of_kind(claims, "role_start") == []
    assert of_kind(claims, "role_end") == []
    assert of_kind(claims, "role_location") == []


def test_header_only_resume_is_not_an_error() -> None:
    claims = parse_resume("# Only A Name\n", "x.md")
    assert values(claims, "name") == ["Only A Name"]


def test_extraction_follows_the_document_not_a_memory(resume_text: str) -> None:
    """Mutate the text in memory: the claims must follow it, not a memorised answer.

    This is the cheapest available guard against a parser that "knows" the resume
    rather than reading it -- if the Avantis block is gone from the input, no
    Avantis claim may survive.
    """
    lines = resume_text.splitlines()

    cut = next(index for index, line in enumerate(lines) if "Avantis" in line)
    trimmed = parse_resume("\n".join(lines[:cut]), "resume.md")
    assert len(of_kind(trimmed, "role")) == 2
    assert len(of_kind(trimmed, "duty")) == 11
    assert not any("Avantis" in claim.value for claim in trimmed)
    # Education and languages sit below the cut, so they must be gone too.
    assert of_kind(trimmed, "education_institution") == []
    assert of_kind(trimmed, "language") == []

    start = next(index for index, line in enumerate(lines) if line.strip().startswith("## PROFESSIONAL"))
    end = next(index for index, line in enumerate(lines) if line.strip().startswith("## EDUCATION"))
    without_experience = parse_resume("\n".join(lines[:start] + lines[end:]), "resume.md")
    assert of_kind(without_experience, "role") == []
    assert of_kind(without_experience, "duty") == []
    assert len(of_kind(without_experience, "language")) == 2


def test_changing_the_stated_total_changes_the_claim(resume_text: str) -> None:
    edited = parse_resume(resume_text.replace("15+ years", "7+ years"), "resume.md")
    statements = of_kind(edited, "experience_statement")
    assert [s.value for s in statements] == ["7+ years of IT experience"]
    # Still no number computed: the parser reports what the resume says.
    assert statements[0].normalized is None


# --------------------------------------------------------------------------
# CLI surface (read-only)
# --------------------------------------------------------------------------


def test_resume_candidate_list_matches_job_analyzer() -> None:
    """The local fallback must not silently drift from the canonical list."""
    from ai_assistant.job_analyzer import RESUME_CANDIDATES

    assert tuple(RESUME_CANDIDATES) == FALLBACK_RESUME_CANDIDATES


def test_resolve_resume_path_honours_an_explicit_path(tmp_path: Path) -> None:
    custom = tmp_path / "my_resume.md"
    custom.write_text("# X\n", encoding="utf-8")
    assert resolve_resume_path(str(custom)) == custom
    assert resolve_resume_path(str(tmp_path / "missing.md")) is None


def test_extract_command_prints_claims_and_cites_lines(
    capsys: pytest.CaptureFixture, claims: list
) -> None:
    code = candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), dry_run=True)
    out = capsys.readouterr().out
    assert code == 0
    assert f"{len(claims)} claims" in out
    assert "[Identity]" in out and "[Languages]" in out
    assert "resume.md:1" in out
    assert "read-only" in out


def test_extract_command_json_is_machine_readable(
    capsys: pytest.CaptureFixture, claims: list
) -> None:
    code = candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), dry_run=True, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["claim_count"] == len(payload["claims"]) == len(claims)
    assert payload["source"] == str(RESUME_PATH)
    assert {c["status"] for c in payload["claims"]} == {STATUS_DERIVED_FROM_SOURCE}


def test_extract_command_reports_a_missing_file(capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    code = candidate_kb_extract_cmd(resume_path=str(tmp_path / "nope.md"))
    captured = capsys.readouterr()
    assert code == 2
    assert "ERROR: resume file not found" in captured.err


def test_format_claims_reports_the_source_and_count(claims: list) -> None:
    text = format_claims(claims, "resume.md")
    assert text.splitlines()[0] == f"candidate-kb extract — resume.md: {len(claims)} claims"


# --------------------------------------------------------------------------
# Read-only guarantee
# --------------------------------------------------------------------------


def test_parser_and_cli_do_not_modify_protected_files(capsys: pytest.CaptureFixture) -> None:
    """Extraction must leave the resume, the profile and the database untouched."""
    protected = [
        RESUME_PATH,
        REPO_ROOT / "candidate_profile.json",
        REPO_ROOT / "state.db",
    ]
    missing = [path for path in protected if not path.is_file()]
    assert not missing, f"expected these files to exist: {missing}"

    before = {path: (sha256_of(path), path.stat().st_size) for path in protected}

    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    parse_resume(resume_text, str(RESUME_PATH))
    candidate_kb_extract_cmd(resume_path=str(RESUME_PATH), dry_run=True)
    capsys.readouterr()

    after = {path: (sha256_of(path), path.stat().st_size) for path in protected}
    assert after == before, {p: (before[p], after[p]) for p in protected if before[p] != after[p]}
