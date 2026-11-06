"""Tests for the master resume (Stage 0 / T5).

T5 is the first artifact a human outside this repository could read, so the
properties pinned here are the ones that keep it honest:

* it carries **every** role the resume stated, including Avantis and the
  2009-2018 block -- the pin against the G2 finding, which was that earlier
  experience was being dropped;
* nothing below ``DERIVED_FROM_SOURCE`` reaches it: a rejected claim, a claim
  superseded by a later answer, a claim absent from its source and a claim
  flagged as an assumption are each excluded, and each exclusion is reported
  with a reason rather than performed silently;
* the length is not capped, because a cap is how a fact gets lost;
* the Russian review variant covers every line the English one carries, so a
  half-translated document cannot pass as complete;
* a project claim's provenance names a real file, a real line and a real quote,
  and the catalogue refuses to run when that stops being true;
* a resume re-extract cannot mark a project claim absent or relabel it;
* **every** claim in the knowledge base is rendered, excluded or subsumed, and the
  three buckets do not overlap -- so a fact cannot leave the resume silently, and
  the source map cannot credit one fact to two lines.

The last group is the one that caught a real defect: the own-projects role's own
facts were being dropped, and nothing in the output said so. The other two would
otherwise fail silently and late.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ai_assistant.candidate import master_resume as mr
from ai_assistant.candidate.kb import (
    PROJECT_SOURCE,
    CandidateKnowledgeBase,
    ClaimRecord,
    ClaimStatus,
)
from ai_assistant.candidate.projects import (
    KIND_PROJECT,
    KIND_PROJECT_STATUS,
    PROJECT_CATALOGUE,
    STATUS_IMPLEMENTED,
    Anchor,
    ProjectSourceError,
    ProjectSpec,
    extract_project_claims,
    resolve_projects_root,
    validate_catalogue,
)
from ai_assistant.candidate.resume_parser import make_claim_id, parse_resume

REPO_ROOT = Path(__file__).resolve().parent.parent
RESUME_PATH = REPO_ROOT / "resume.md"

DERIVED = ClaimStatus.DERIVED_FROM_SOURCE
CONFIRMED = ClaimStatus.CONFIRMED_BY_USER
REJECTED = ClaimStatus.REJECTED_BY_USER


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def resume_claims() -> list:
    return parse_resume(RESUME_PATH.read_text(encoding="utf-8"), "resume.md")


def real_kb() -> CandidateKnowledgeBase:
    """The repository's knowledge base, as shipped."""
    return CandidateKnowledgeBase.load(REPO_ROOT / "candidate_kb.json")


def kb_with_projects() -> CandidateKnowledgeBase:
    """The real KB plus the project catalogue, in memory only."""
    kb = real_kb()
    kb.sync_project_claims(extract_project_claims())
    return kb


def build(kb: CandidateKnowledgeBase) -> mr.MasterResume:
    """The resume. Its excluded/subsumed tables come from the builder itself."""
    return mr.build_master_resume(kb)


def synthetic_kb(
    *,
    roles: list[tuple[str, str, str, list[str]]] | None = None,
    extra: list[ClaimRecord] | None = None,
    summary: str | None = None,
) -> CandidateKnowledgeBase:
    """A small KB: (title, company, start, duties) per role."""
    kb = CandidateKnowledgeBase(kb_version=1)
    version = 1

    def add(record: ClaimRecord) -> None:
        kb.claims[record.claim_id] = record

    if summary is not None:
        cid = make_claim_id("summary", "", summary)
        add(
            ClaimRecord(
                claim_id=cid,
                kind="summary",
                value=summary,
                evidence={"source_file": "resume.md", "line": 1, "quote": summary},
            )
        )

    for title, company, start, duties in roles or []:
        heading = f"{title} | {company}"
        role_id = make_claim_id("role", "", heading)
        add(
            ClaimRecord(
                claim_id=role_id,
                kind="role",
                value=heading,
                evidence={"source_file": "resume.md", "line": 2, "quote": heading},
            )
        )
        for kind, value in (
            ("role_title", title),
            ("role_company", company),
            ("role_start", start),
        ):
            add(
                ClaimRecord(
                    claim_id=make_claim_id(kind, role_id, value),
                    kind=kind,
                    value=value,
                    normalized=value if kind == "role_start" else None,
                    role_id=role_id,
                    evidence={"source_file": "resume.md", "line": 3, "quote": value},
                )
            )
        for duty in duties:
            add(
                ClaimRecord(
                    claim_id=make_claim_id("duty", role_id, duty),
                    kind="duty",
                    value=duty,
                    role_id=role_id,
                    evidence={"source_file": "resume.md", "line": 4, "quote": duty},
                )
            )

    for record in extra or []:
        add(record)

    kb.kb_version = version
    return kb


def a_project_record(project_id: str = "demo", value: str = "A demo project") -> ClaimRecord:
    return ClaimRecord(
        claim_id=make_claim_id(KIND_PROJECT, project_id, value),
        kind=KIND_PROJECT,
        value=value,
        status=DERIVED,
        normalized=project_id,
        evidence={
            "source_file": PROJECT_SOURCE,
            "project_id": project_id,
            "project_status": STATUS_IMPLEMENTED,
            "source_path": "C:/nowhere/README.md",
            "source_rel_path": "README.md",
            "line": 1,
            "quote": "# demo",
            "source_sha256": "0" * 64,
        },
        in_source=True,
    )


def a_project_member(project_id: str, parent_id: str, value: str) -> ClaimRecord:
    """A non-identity project claim, bound to its project."""
    return ClaimRecord(
        claim_id=make_claim_id("project_bullet", parent_id, value),
        kind="project_bullet",
        value=value,
        status=DERIVED,
        normalized=project_id,
        parent_id=parent_id,
        evidence={
            "source_file": PROJECT_SOURCE,
            "project_id": project_id,
            "project_status": STATUS_IMPLEMENTED,
            "source_path": "C:/nowhere/README.md",
            "source_rel_path": "README.md",
            "line": 2,
            "quote": value,
            "source_sha256": "0" * 64,
        },
        in_source=True,
    )


# --------------------------------------------------------------------------
# The master resume carries the whole history
# --------------------------------------------------------------------------


def test_master_contains_every_role_from_the_resume() -> None:
    """The pin against G2: earlier experience must not be dropped."""
    text = mr.render_markdown(build(real_kb()), lang="en")

    for expected in (
        "Sayama Travel",
        "Avantis",
        "Earlier IT Infrastructure & Support Experience",
        "Speedcast / Germsat",
        "Intravision",
        "Slavyanka",
        "Russian Post",
    ):
        assert expected in text, f"{expected} is missing from the master resume"


def test_master_keeps_every_resume_duty() -> None:
    """No fact from resume.md may be lost -- measured claim by claim."""
    kb = real_kb()
    text = mr.render_markdown(build(kb), lang="en")

    duties = [record for record in kb.by_status() if record.kind == "duty"]
    assert duties, "the fixture resume should state duties"
    for duty in duties:
        assert duty.value in text, f"duty {duty.claim_id} was dropped"


def test_no_block_line_is_glued_to_a_bullet_above_it() -> None:
    """Markdown structure: a block line after a list item needs its own blank line.

    Without the blank line markdown reads the block as a lazy continuation of the
    list item, so the reader sees one long bullet -- which is what happened to the
    project ``**Stack:**`` line. The rendered body is walked in step with the
    resume's own line kinds, so this pins the renderer's rule rather than today's
    wording: every h2/h3/paragraph/meta/note that follows a bullet must be
    separated from it by an empty line, in both languages.
    """
    resume = build(real_kb())
    block_kinds = {"h2", "h3", "paragraph", "meta", "note"}

    for lang in ("en", "ru"):
        rendered = mr.render_markdown(resume, lang=lang).splitlines()
        non_empty = [line for line in rendered if line.strip()]
        assert len(non_empty) == len(resume.lines), (
            f"{lang}: the renderer emitted {len(non_empty)} lines for "
            f"{len(resume.lines)} resume lines"
        )

        cursor = 0
        for index, line in enumerate(resume.lines):
            while not rendered[cursor].strip():
                cursor += 1
            if (
                index
                and line.kind in block_kinds
                and resume.lines[index - 1].kind == "bullet"
            ):
                assert not rendered[cursor - 1].strip(), (
                    f"{lang}: the {line.kind} line {line.text!r} is glued to the "
                    f"bullet above it; markdown will read it as part of that bullet"
                )
            cursor += 1


def test_master_has_the_required_sections() -> None:
    text = mr.render_markdown(build(real_kb()), lang="en")
    for heading in (
        "## PROFESSIONAL SUMMARY",
        "## CORE SKILLS",
        "## EMPLOYMENT HISTORY",
        "## INDEPENDENT PROJECTS",
        "## EDUCATION",
        "## LANGUAGES",
        "## CONTACT INFORMATION",
    ):
        assert heading in text, f"{heading} is missing"


def test_the_former_freelance_heading_is_not_employment() -> None:
    """The candidate confirmed there was no paid client work in that period."""
    kb = real_kb()
    resume = build(kb)
    employment: list[str] = []
    seen = False
    for line in resume.lines:
        if line.text == f"## {mr.EMPLOYMENT_HEADING}":
            seen = True
            continue
        if seen and line.text == f"## {mr.INDEPENDENT_HEADING}":
            break
        if seen:
            employment.append(line.text)

    joined = "\n".join(employment)
    assert "Freelance" not in joined, "the Freelance heading must not read as employment"
    assert "AI Integration & Automation" not in joined

    whole = mr.render_markdown(resume, lang="en")
    for duty in (
        "Design and implement end-to-end AI automation pipelines",
        "Build asynchronous Python ETL pipelines",
    ):
        assert duty in whole, "the period's real work must survive the reclassification"


def test_employment_is_reverse_chronological() -> None:
    resume = build(real_kb())
    order = [
        line.text
        for line in resume.lines
        if line.kind == "h3" and line.text.startswith("### ")
    ]
    employment_headings = [
        text
        for text in order
        if text != f"### {mr.INDEPENDENT_HEADING}"
    ]
    sayama = next(i for i, text in enumerate(employment_headings) if "Sayama Travel" in text)
    avantis = next(i for i, text in enumerate(employment_headings) if "Avantis" in text)
    earlier = next(i for i, text in enumerate(employment_headings) if "Earlier" in text)
    assert sayama < avantis < earlier


# --------------------------------------------------------------------------
# Nothing below DERIVED_FROM_SOURCE
# --------------------------------------------------------------------------


def test_no_rendered_claim_is_below_derived_from_source() -> None:
    kb = kb_with_projects()
    resume = build(kb)
    by_id = {record.claim_id: record for record in kb.by_status()}

    assert resume.claim_ids, "the master resume should carry provenance"
    for claim_id in resume.rendered_claim_ids():
        record = by_id[claim_id]
        assert record.status in (DERIVED, CONFIRMED), (
            f"{claim_id} has status {record.status} and must not be rendered"
        )
        assert record.in_source is True


def test_a_rejected_claim_is_excluded_and_reported() -> None:
    kb = synthetic_kb(roles=[("Engineer", "Acme", "2020", ["Did a thing.", "Did another."])])
    victim = next(record for record in kb.by_status() if record.kind == "duty")
    kb.reject(victim.claim_id)

    resume = build(kb)
    text = mr.render_markdown(resume, lang="en")

    assert victim.value not in text
    reasons = {item.claim_id: item.reason for item in resume.excluded}
    assert reasons[victim.claim_id] == "REJECTED_BY_USER"


def test_a_superseded_claim_is_excluded() -> None:
    """A fact replaced by a later answer must not survive alongside it."""
    kb = synthetic_kb(roles=[("Engineer", "Acme", "2020", ["The old wording."])])
    victim = next(record for record in kb.by_status() if record.kind == "duty")
    victim.evidence = {**victim.evidence, "superseded_by": "experience_note-deadbeef00"}

    resume = build(kb)
    assert victim.value not in mr.render_markdown(resume, lang="en")
    reasons = {item.claim_id: item.reason for item in resume.excluded}
    assert "superseded by" in reasons[victim.claim_id]


def test_a_claim_absent_from_its_source_is_excluded() -> None:
    kb = synthetic_kb(roles=[("Engineer", "Acme", "2020", ["A duty that left the resume."])])
    victim = next(record for record in kb.by_status() if record.kind == "duty")
    victim.in_source = False

    resume = build(kb)
    assert victim.value not in mr.render_markdown(resume, lang="en")


def test_a_claim_flagged_as_an_assumption_is_excluded() -> None:
    kb = synthetic_kb(roles=[("Engineer", "Acme", "2020", ["A guess dressed as a duty."])])
    victim = next(record for record in kb.by_status() if record.kind == "duty")
    victim.evidence = {**victim.evidence, "fact_class": "ASSUMPTION"}

    resume = build(kb)
    assert victim.value not in mr.render_markdown(resume, lang="en")
    reasons = {item.claim_id: item.reason for item in resume.excluded}
    assert "ASSUMPTION" in reasons[victim.claim_id]


def test_salary_and_search_preferences_stay_out_of_the_public_resume() -> None:
    kb = kb_with_projects()
    resume = build(kb)
    text = mr.render_markdown(resume, lang="en")

    assert "$1000" not in text
    assert "1500" not in text
    reasons = {item.kind: item.reason for item in resume.excluded}
    assert "preference_condition" in reasons
    assert "goal_direction" in reasons


# --------------------------------------------------------------------------
# Length
# --------------------------------------------------------------------------


def test_the_length_is_not_capped() -> None:
    """A truncation is how a fact disappears without anyone noticing."""
    long_duty = "Supervised " + ("a very long and detailed responsibility " * 200)
    kb = synthetic_kb(roles=[("Engineer", "Acme", "2020", [long_duty])])
    text = mr.render_markdown(build(kb), lang="en")
    assert long_duty in text
    assert len(text) > 5000


# --------------------------------------------------------------------------
# Russian review variant
# --------------------------------------------------------------------------


def test_russian_variant_covers_every_english_line() -> None:
    """A half-translated review document reads as complete, so it must not exist."""
    resume = build(kb_with_projects())
    missing: list[str] = []
    for line in resume.lines:
        if not line.translate or line.ru_text is not None:
            continue
        if mr._translate_headings_only(line.text) != line.text:
            continue
        if mr.translation_for(line.text) is None:
            missing.append(line.text)
    assert missing == [], f"no Russian text for: {missing}"


def test_russian_variant_renders_and_keeps_the_same_claims() -> None:
    resume = build(kb_with_projects())
    ru = mr.render_markdown(resume, lang="ru")
    en = mr.render_markdown(resume, lang="en")

    assert "## ОПЫТ РАБОТЫ" in ru
    assert "## СОБСТВЕННЫЕ ПРОЕКТЫ" in ru
    for expected in ("Sayama Travel", "Avantis", "Repiton", "WHUBN"):
        assert expected in ru
    # Same number of lines: the translation is a rendering, not a subset.
    assert len([line for line in ru.splitlines() if line.strip()]) == len(
        [line for line in en.splitlines() if line.strip()]
    )


def test_a_missing_translation_is_an_error_not_an_english_fallback() -> None:
    resume = build(synthetic_kb(roles=[("Engineer", "Acme", "2020", ["Untranslated duty."])]))
    with pytest.raises(mr.MissingTranslation):
        mr.render_markdown(resume, lang="ru")


# --------------------------------------------------------------------------
# Declared presentation decisions are checked, not trusted
# --------------------------------------------------------------------------


def test_independent_role_evidence_is_confirmed_and_bound() -> None:
    """The reclassification must cite a confirmed claim bound to that role."""
    kb = real_kb()
    for role_id, note_id in mr.INDEPENDENT_ROLE_EVIDENCE.items():
        role = kb.get(role_id)
        note = kb.get(note_id)
        assert role is not None, f"{role_id} is not in the knowledge base"
        assert note is not None, f"{note_id} is not in the knowledge base"
        assert note.status == CONFIRMED, f"{note_id} is not confirmed by the candidate"
        assert note.role_id == role_id, f"{note_id} is not bound to {role_id}"
        assert role.kind == "role"


def test_chronology_evidence_is_confirmed_and_matches_the_period() -> None:
    """The confirmed note must actually name the months the resume prints."""
    en_to_ru = {
        "january": "январ",
        "february": "феврал",
        "march": "март",
        "april": "апрел",
        "may": "ма",
        "june": "июн",
        "july": "июл",
        "august": "август",
        "september": "сентябр",
        "october": "октябр",
        "november": "ноябр",
        "december": "декабр",
    }
    kb = real_kb()
    assert set(mr.CHRONOLOGY_EVIDENCE) == set(mr.CHRONOLOGY_PERIOD) == set(
        mr.CHRONOLOGY_EMPLOYER
    ), "the three chronology tables must describe the same roles"
    for role_id, note_id in mr.CHRONOLOGY_EVIDENCE.items():
        note = kb.get(note_id)
        assert note is not None
        assert note.status == CONFIRMED
        start, end = mr.CHRONOLOGY_PERIOD[role_id]
        for bound in (start, end):
            month, _, year = bound.partition(" ")
            assert month.lower() in en_to_ru, f"{month} is not a month name"
            assert en_to_ru[month.lower()] in note.value.lower(), (
                f"{note_id} does not mention {month}"
            )
            assert year in note.value, f"{note_id} does not mention {year}"


def test_the_confirmed_period_names_an_employer_of_that_role() -> None:
    """The period is one company's, so it must name that company -- and a real one.

    The role's heading names four employers in a single block. Naming the wrong
    one, or naming the block, would date three companies by a fact the candidate
    only confirmed about the fourth.
    """
    kb = real_kb()
    for role_id, employer in mr.CHRONOLOGY_EMPLOYER.items():
        employers = {
            record.value
            for record in kb.by_status()
            if record.role_id == role_id and record.kind == "role_employer"
        }
        assert employers, f"{role_id} has no role_employer claims to bind the period to"
        assert employer in employers, (
            f"{employer!r} is not one of the employers of {role_id}: {sorted(employers)}"
        )


def test_the_confirmed_period_is_attached_to_one_employer_only() -> None:
    """No line may carry the confirmed dates without naming the company they belong to."""
    kb = real_kb()
    resume = build(kb)
    role_id = next(iter(mr.CHRONOLOGY_EVIDENCE))
    start, end = mr.CHRONOLOGY_PERIOD[role_id]
    employer = mr.CHRONOLOGY_EMPLOYER[role_id]
    others = sorted(
        record.value
        for record in kb.by_status()
        if record.role_id == role_id
        and record.kind == "role_employer"
        and record.value != employer
    )
    assert others, "the block has other employers, or this test proves nothing"

    for line in resume.lines:
        if start not in line.text and end not in line.text:
            continue
        assert employer in line.text, (
            f"{line.text!r} states the confirmed period without naming {employer}"
        )
        for other in others:
            assert other not in line.text, (
                f"{line.text!r} attaches the confirmed period to {other}, whose dates "
                f"nobody confirmed"
            )


def test_the_early_block_keeps_the_sources_own_dates() -> None:
    """The block's range is the resume's. Only the confirmed company's is corrected.

    Rewriting the block's range to reach March 2021 would be this generator
    inventing a date; leaving it at the source's 2009-2018 and stating the
    confirmed company's period beside it is not a gap, because that period covers
    the years in between.
    """
    kb = real_kb()
    resume = build(kb)
    role_id = next(iter(mr.CHRONOLOGY_EVIDENCE))
    claims = {
        record.kind: record for record in kb.by_status() if record.role_id == role_id
    }
    meta = next(
        line
        for line in resume.lines
        if line.kind == "meta" and claims["role_start"].claim_id in line.claim_ids
    )
    assert claims["role_start"].value in meta.text
    assert claims["role_end"].value in meta.text
    assert claims["role_end"].value == "2018", "the source's own end date must not be rewritten"


def test_the_approved_edit_is_still_present_to_be_applied() -> None:
    """A silent no-op edit is how the approved wording would go missing.

    Both edits are substitutions, not deletions: the quoted clause is replaced by
    the opening the candidate approved, and "in production" by the approved
    phrasing. So the pin checks both halves of each -- the old text gone, the
    approved text there -- and that a summary missing either span is an error
    rather than a pass. The second case matters most: the summary is re-extracted
    from ``resume.md``, and a re-extract that drops one phrase must not silently
    publish a draft that no longer matches what was approved.
    """
    first_span, first_replacement, _reason = mr.APPROVED_TEXT_EDITS[0]
    second_span, second_replacement, _reason2 = mr.APPROVED_TEXT_EDITS[1]

    kb = synthetic_kb(summary=f"An engineer{first_span}doing things {second_span}.")
    text = mr.render_markdown(build(kb), lang="en")
    assert first_span.strip() not in text
    assert first_replacement.strip() in text
    assert second_span not in text
    assert second_replacement in text

    with pytest.raises(ValueError):
        mr.build_master_resume(synthetic_kb(summary="An engineer with no such phrase."))

    with pytest.raises(ValueError):
        mr.build_master_resume(
            synthetic_kb(summary=f"An engineer{first_span}doing things elsewhere.")
        )


def test_the_summary_states_the_approved_tenure_and_no_other_figure() -> None:
    """One figure, approved, and the other two numbers stay out.

    ``years_experience = 3`` and ``domain_years.it_support = 11.0`` measure two
    other things; neither is total tenure and neither may be published. The
    resume states ``15+ years in IT`` because the candidate approved exactly that
    wording, and a different figure appearing here would be an unapproved one.
    """
    resume = build(kb_with_projects())
    summary = next(line for line in resume.lines if line.kind == "paragraph")

    assert mr.APPROVED_TENURE in summary.text
    for number in ("17 year", "11 year", "3 year", "15.3", "14.9"):
        assert number not in summary.text, f"{number} is a figure nobody approved"
    assert "in production" not in mr.render_markdown(resume, lang="en")


def test_the_summary_opens_with_the_approved_opening() -> None:
    """The quoted summary may not open by selling one identity."""
    resume = build(kb_with_projects())
    summary = next(line for line in resume.lines if line.kind == "paragraph")

    assert summary.text.startswith(mr.APPROVED_SUMMARY_OPENING)
    assert "AI Automation Engineer" not in summary.text
    assert "AI Engineer" not in summary.text
    # The rest of the candidate's own sentence is still quoted behind it.
    assert "Builds end-to-end systems combining Python" in summary.text


def test_the_headline_covers_every_direction_and_ranks_none() -> None:
    """Universal positioning: four directions, and no direction made the main one.

    Two properties, both deliberate. The set has to cover support, systems
    administration, Bitrix24/CRM and automation, because the candidate's search
    spans all of them and the master resume is the source every per-vacancy
    version is cut from. And the order must not encode a ranking: alphabetical is
    the only order that carries no priority, so no reader and no later generator
    can infer that one direction is the lead.
    """
    resume = build(real_kb())
    headline = next(line for line in resume.lines if line.kind == "headline")
    labels = [part.strip() for part in headline.text.split("|")]

    assert len(labels) >= 4, f"too few directions offered: {labels}"
    assert labels == sorted(labels), (
        f"the headline order implies a priority: {labels}"
    )
    lowered = headline.text.lower()
    for required in ("support", "systems", "bitrix24", "automation"):
        assert required in lowered, f"the headline does not offer {required}"

    assert any(
        note.startswith("headline assembled") for note in resume.notes
    ), "an assembled headline must be reported as needing approval"
    assert any(
        "needs approval" in note for note in resume.notes if note.startswith("headline")
    ), "the new positioning is a proposal and must be marked as one"


# --------------------------------------------------------------------------
# Project provenance
# --------------------------------------------------------------------------


def test_the_shipped_catalogue_validates() -> None:
    counts = validate_catalogue()
    assert set(counts) == {spec.project_id for spec in PROJECT_CATALOGUE}
    assert all(count > 1 for count in counts.values())


def test_every_project_claim_points_at_a_real_quote() -> None:
    """Except the identity claim, whose value is the project's display name."""
    root = resolve_projects_root()
    for record in extract_project_claims():
        evidence = record.evidence
        assert evidence["source_file"] == PROJECT_SOURCE
        source = Path(str(evidence["source_path"]))
        assert source.is_file(), f"{source} is gone"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == evidence["source_sha256"]
        lines = source.read_text(encoding="utf-8").splitlines()
        line = int(evidence["line"])  # type: ignore[arg-type]
        quote = str(evidence["quote"])
        span = quote.count("\n") + 1
        window = "\n".join(lines[line - 1 : line - 1 + span])
        assert quote in window, f"{record.claim_id} no longer matches {source}:{line}"
        if record.kind != KIND_PROJECT:
            # The value is the quote on one line, so the two differ only in
            # whitespace -- including the space a hyphenated break does not get.
            assert record.value.replace(" ", "") == "".join(quote.split())
        assert root.exists()


def test_the_catalogue_refuses_to_run_when_an_anchor_moves(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n\nA description.\n", encoding="utf-8")

    catalogue = (
        ProjectSpec(
            project_id="demo",
            name="Demo",
            rel_repo="demo",
            status=STATUS_IMPLEMENTED,
            anchors=(
                Anchor("README.md", 1, "# demo", KIND_PROJECT),
                Anchor("README.md", 3, "A description.", "project_summary"),
            ),
        ),
    )
    assert validate_catalogue(tmp_path, catalogue) == {"demo": 2}

    (repo / "README.md").write_text("# demo\n\nReworded.\n", encoding="utf-8")
    with pytest.raises(ProjectSourceError):
        validate_catalogue(tmp_path, catalogue)
    with pytest.raises(ProjectSourceError):
        extract_project_claims(tmp_path, catalogue)


def test_repository_facts_hold_one_value_each() -> None:
    """``git log`` prints one line per commit; the table needs exactly one.

    Without this, ``first_commit_date`` came back as every commit date at once
    and tore the source map's table apart.
    """
    from ai_assistant.candidate.projects import repository_facts

    facts = repository_facts()
    assert facts, "the catalogue should have repositories"
    for fact in facts:
        for value in (fact.remote, fact.first_commit_date, fact.last_commit_date):
            assert value is None or "\n" not in value, f"{fact.path}: {value!r} is multi-line"


def test_the_source_map_has_one_row_per_project() -> None:
    resume = build(kb_with_projects())
    text = mr.render_source_map(resume, real_kb(), project_evidence=mr.project_evidence())
    table = text.split("## Project repositories")[1]
    rows = [
        line
        for line in table.splitlines()
        if line.startswith("| ") and not line.startswith("| project") and "---" not in line
    ]
    assert len(rows) == len(PROJECT_CATALOGUE)


def test_project_claims_reach_the_resume() -> None:
    kb = kb_with_projects()
    text = mr.render_markdown(build(kb), lang="en")
    for name in ("Job Search AI (Job Search Hub)", "Reels Analyzer", "Repiton", "WHUBN"):
        assert name in text
    assert "NO-GO" in text, "Repiton's measured negative result must be shown, not hidden"
    assert "Prototype" in text


def test_a_project_sync_never_confirms_anything() -> None:
    kb = real_kb()
    kb.sync_project_claims(extract_project_claims())
    for record in kb.by_status():
        if str(record.evidence.get("source_file")) == PROJECT_SOURCE:
            assert record.status == DERIVED


def test_a_project_sync_ignores_an_incoming_status() -> None:
    """The real guard: a catalogue claim is not confirmed on arrival.

    The shipped extractor always hands over ``DERIVED_FROM_SOURCE`` records, so
    asserting on those proves nothing -- the assertion has to hand the sync a
    record that claims to be confirmed and check that the sync declines to
    believe it.
    """
    kb = CandidateKnowledgeBase()
    incoming = a_project_record("demo", "A demo project")
    incoming.status = CONFIRMED

    kb.sync_project_claims([incoming])

    stored = kb.get(incoming.claim_id)
    assert stored is not None
    assert stored.status == DERIVED, "a catalogue claim must not arrive confirmed"


def test_a_project_sync_does_not_restamp_a_confirmed_project_claim() -> None:
    """A refresh may rewrite evidence; it may not touch the candidate's decision."""
    kb = CandidateKnowledgeBase()
    record = a_project_record("demo", "A demo project")
    kb.sync_project_claims([record])
    kb.confirm(record.claim_id)

    changed = a_project_record("demo", "A demo project")
    changed.evidence = {**changed.evidence, "line": 9}
    report = kb.sync_project_claims([changed])

    stored = kb.get(record.claim_id)
    assert stored is not None
    assert report.refreshed == 1
    assert stored.evidence["line"] == 9
    assert stored.status == CONFIRMED, "a refresh must not un-confirm a fact"


def test_a_project_sync_is_idempotent() -> None:
    """Runs against an empty KB so it holds whatever the live KB already has."""
    kb = CandidateKnowledgeBase()
    records = extract_project_claims()
    first = kb.sync_project_claims(records)
    version_after_first = kb.kb_version

    second = kb.sync_project_claims(extract_project_claims())

    assert first.added == len(records)
    assert second.added == 0
    assert second.refreshed == 0
    assert second.unchanged == len(records)
    assert second.marked_absent == 0
    assert kb.kb_version == version_after_first


def test_a_resume_merge_leaves_project_claims_alone() -> None:
    """The guard: re-extracting the resume must not eat the project section."""
    kb = real_kb()
    kb.sync_project_claims(extract_project_claims())
    project_ids = {
        record.claim_id
        for record in kb.by_status()
        if str(record.evidence.get("source_file")) == PROJECT_SOURCE
    }
    assert project_ids

    report = kb.merge_claims(
        resume_claims(),
        source_file=str(RESUME_PATH),
        source_sha256=hashlib.sha256(RESUME_PATH.read_bytes()).hexdigest(),
    )

    assert report.skipped_other_origin == len(project_ids)
    for claim_id in project_ids:
        record = kb.get(claim_id)
        assert record is not None
        assert record.in_source is True, "a resume merge must not mark a project absent"
        assert record.evidence.get("source_file") == PROJECT_SOURCE, (
            "a resume merge must not relabel a project claim's provenance"
        )


def test_a_project_sync_marks_a_vanished_claim_absent_without_deleting_it() -> None:
    """A reworded quote must not leave two versions of one fact in the resume.

    Without this, editing one line of a README would add a new claim while the
    old one stayed bound to the same project -- and the resume would print the
    same fact twice, in two different wordings.
    """
    kb = CandidateKnowledgeBase()
    identity = a_project_record("demo", "Demo")
    old = a_project_member("demo", identity.claim_id, "The first wording.")
    kb.sync_project_claims([identity, old])

    new = a_project_member("demo", identity.claim_id, "The second wording.")
    report = kb.sync_project_claims([identity, new])

    assert report.added == 1
    assert report.marked_absent == 1

    stored_old = kb.get(old.claim_id)
    assert stored_old is not None, "the old claim is history, not garbage"
    assert stored_old.in_source is False
    assert stored_old.status == DERIVED, "marking absent is not a rejection"
    assert kb.get(new.claim_id).in_source is True

    text = mr.render_markdown(build(kb), lang="en")
    assert "The second wording." in text
    assert "The first wording." not in text


def test_a_project_sync_does_not_touch_resume_or_interview_claims() -> None:
    kb = real_kb()
    before = {
        claim_id: (record.status, record.in_source, json.dumps(record.evidence, sort_keys=True))
        for claim_id, record in kb.claims.items()
    }
    kb.sync_project_claims(extract_project_claims())

    for claim_id, snapshot in before.items():
        record = kb.get(claim_id)
        assert record is not None, f"{claim_id} disappeared"
        after = (record.status, record.in_source, json.dumps(record.evidence, sort_keys=True))
        assert after == snapshot, f"{claim_id} changed during a project sync"


def test_project_claims_never_use_the_resume_or_interview_marker() -> None:
    for record in extract_project_claims():
        assert record.evidence["source_file"] not in {"interview", "resume.md"}
        assert record.kind.startswith("project")


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def test_build_command_writes_the_three_artifacts(tmp_path: Path) -> None:
    from ai_assistant.candidate.master_resume import candidate_master_build_cmd
    from ai_assistant.candidate.projects import candidate_kb_projects_cmd

    kb_path = tmp_path / "candidate_kb.json"
    kb = real_kb()
    kb.save(kb_path)

    assert candidate_kb_projects_cmd(kb_path=str(kb_path)) == 0
    assert candidate_master_build_cmd(
        kb_path=str(kb_path), out_path=str(tmp_path / "artifacts"), projects_root=str(tmp_path)
    ) == 0

    en = tmp_path / "artifacts" / "master_resume.md"
    ru = tmp_path / "artifacts" / "master_resume.ru.md"
    sources = tmp_path / "artifacts" / "master_resume_sources.md"
    for path in (en, ru, sources):
        assert path.is_file(), f"{path} was not written"
    assert "## INDEPENDENT PROJECTS" in en.read_text(encoding="utf-8")
    assert "## СОБСТВЕННЫЕ ПРОЕКТЫ" in ru.read_text(encoding="utf-8")
    assert "Rendered claims" in sources.read_text(encoding="utf-8")


def test_build_command_fails_loudly_without_a_knowledge_base(tmp_path: Path) -> None:
    from ai_assistant.candidate.master_resume import candidate_master_build_cmd

    assert candidate_master_build_cmd(kb_path=str(tmp_path / "missing.json")) == 2


def test_the_projects_command_writes_nothing_in_dry_run(tmp_path: Path) -> None:
    from ai_assistant.candidate.projects import candidate_kb_projects_cmd

    kb_path = tmp_path / "candidate_kb.json"
    real_kb().save(kb_path)
    before = kb_path.read_bytes()

    assert candidate_kb_projects_cmd(kb_path=str(kb_path), dry_run=True) == 0
    assert kb_path.read_bytes() == before


# --------------------------------------------------------------------------
# The three-way partition: every claim is rendered, excluded or subsumed
# --------------------------------------------------------------------------
#
# Without this, a fact can leave the resume with nothing in the output to show
# it did. The accounting is what turns "the draft looks fine" into a check.


def test_every_claim_is_rendered_excluded_or_subsumed() -> None:
    """The whole KB is accounted for. This is the pin against silent loss."""
    kb = kb_with_projects()
    resume = build(kb)

    missing = mr.unaccounted_claims(kb, resume)

    assert missing == [], (
        "these claims are neither on a line, nor declared excluded, nor declared "
        "subsumed, so nothing would show they went missing: "
        + ", ".join(f"{record.claim_id} ({record.kind})" for record in missing)
    )
    total = len(kb.by_status())
    accounted = (
        len(resume.rendered_claim_ids()) + len(resume.excluded) + len(resume.subsumed)
    )
    assert accounted == total


def test_no_claim_is_counted_twice() -> None:
    """The three buckets must partition, not overlap.

    An overlap means the source map credits one fact to two lines, which reads
    as two facts and inflates the account until it stops meaning anything.
    """
    kb = kb_with_projects()
    resume = build(kb)

    rendered = resume.rendered_claim_ids()
    excluded = {item.claim_id for item in resume.excluded}
    subsumed_ids = {item.claim_id for item in resume.subsumed}

    assert not (rendered & excluded)
    assert not (rendered & subsumed_ids)
    assert not (excluded & subsumed_ids)


def test_the_source_map_reports_the_partition() -> None:
    """The reader of the map can see the account balance without recomputing it."""
    kb = kb_with_projects()
    resume = build(kb)
    text = mr.render_source_map(resume, kb)

    total = len(kb.by_status())
    assert f"Claims in the knowledge base: {total}" in text
    assert f"Claims accounted for: {total} of {total}" in text
    assert "## Subsumed claims" in text


def test_the_build_command_warns_about_an_unaccounted_claim(tmp_path: Path) -> None:
    """A kind nobody has decided about must be loud, not quietly absent."""
    from ai_assistant.candidate.master_resume import candidate_master_build_cmd

    kb = synthetic_kb(roles=[("Engineer", "Acme", "2020", ["Did a thing."])])
    stray = "Something nobody has decided how to render."
    kb.claims["note-0123456789"] = ClaimRecord(
        claim_id="note-0123456789",
        kind="some_future_kind",
        value=stray,
        status=DERIVED,
        evidence={"source_file": "resume.md", "line": 9, "quote": stray},
    )
    kb_path = tmp_path / "candidate_kb.json"
    kb.save(kb_path)

    exit_code = candidate_master_build_cmd(
        kb_path=str(kb_path),
        out_path=str(tmp_path / "artifacts"),
        projects_root=str(tmp_path),
        lang="en",
    )

    assert exit_code == 1, "an unaccounted claim must not pass as a clean build"


def test_the_build_command_reports_a_missing_translation_as_an_error(tmp_path: Path) -> None:
    """An untranslated line must be a message, not a traceback out of the CLI."""
    from ai_assistant.candidate.master_resume import candidate_master_build_cmd

    kb = synthetic_kb(roles=[("Engineer", "Acme", "2020", ["A duty with no translation."])])
    kb_path = tmp_path / "candidate_kb.json"
    kb.save(kb_path)

    exit_code = candidate_master_build_cmd(
        kb_path=str(kb_path), out_path=str(tmp_path / "artifacts"), projects_root=str(tmp_path)
    )

    assert exit_code == 2
    assert not (tmp_path / "artifacts").exists() or not list(
        (tmp_path / "artifacts").glob("*.md")
    ), "a failed render must not leave a half-written artifact set"


# --------------------------------------------------------------------------
# Role headings and meta lines must not credit the same claim twice
# --------------------------------------------------------------------------


def test_a_role_heading_and_its_meta_line_share_no_claim() -> None:
    kb = synthetic_kb(roles=[("Engineer", "Acme", "2020", ["Did a thing."])])
    resume = build(kb)

    headings = [line for line in resume.lines if line.kind == "h3"]
    metas = [line for line in resume.lines if line.kind == "meta"]
    assert headings and metas

    for heading in headings:
        for meta in metas:
            assert not (set(heading.claim_ids) & set(meta.claim_ids)), (
                f"{heading.text!r} and {meta.text!r} both claim "
                f"{set(heading.claim_ids) & set(meta.claim_ids)}"
            )


def test_the_meta_line_carries_only_location_and_dates() -> None:
    kb = synthetic_kb(roles=[("Engineer", "Acme", "2020", ["Did a thing."])])
    resume = build(kb)
    by_id = {record.claim_id: record for record in kb.by_status()}

    meta = next(line for line in resume.lines if line.kind == "meta" and "2020" in line.text)
    kinds = {by_id[claim_id].kind for claim_id in meta.claim_ids}
    assert kinds <= {"role_location", "role_start", "role_end"}
    assert "role_start" in kinds, "the date on the line must be traceable"


# --------------------------------------------------------------------------
# The independent-projects period carries its own facts
# --------------------------------------------------------------------------


def test_the_independent_role_renders_its_own_facts() -> None:
    """The role's title, place and dates are real facts and must not be dropped."""
    kb = kb_with_projects()
    resume = build(kb)

    role_id = next(iter(mr.INDEPENDENT_ROLE_EVIDENCE))
    children = [record for record in kb.by_status() if record.role_id == role_id]
    rendered = resume.rendered_claim_ids()

    assert role_id in rendered, "the role claim itself must be traceable"
    for record in children:
        if record.kind == "role_company":
            continue  # deliberately excluded, asserted separately below
        assert record.claim_id in rendered, (
            f"{record.claim_id} ({record.kind}) is a fact of the own-projects "
            f"period and is not rendered anywhere"
        )

    text = mr.render_markdown(resume, lang="en")
    independent = text.split(f"## {mr.INDEPENDENT_HEADING}", 1)[1]
    assert "May 2026" in independent
    assert "Thailand" in independent


def test_the_former_employer_name_is_excluded_with_a_reason() -> None:
    """'Freelance' would claim paid work the candidate denied, so it is dropped."""
    kb = kb_with_projects()
    resume = build(kb)
    reasons = {item.claim_id: item.reason for item in resume.excluded}

    assert "role_company-fcd10805e4" in reasons
    assert "paid client work" in reasons["role_company-fcd10805e4"]
    assert "Freelance" not in mr.render_markdown(resume, lang="en")


def test_the_no_paid_work_note_is_translated() -> None:
    """A review document with an English sentence in it is not reviewable."""
    kb = kb_with_projects()
    resume = build(kb)
    ru = mr.render_markdown(resume, lang="ru")

    assert mr.INDEPENDENT_PROJECTS_NOTE not in ru
    assert "оплачиваемых заказов не было" in ru


# --------------------------------------------------------------------------
# Subsumed facts survive in the KB, and say why they are not quoted
# --------------------------------------------------------------------------


def test_the_subsumed_titles_are_kept_for_later_versions() -> None:
    """A title held back from the master draft is raw material, not garbage."""
    kb = kb_with_projects()
    resume = build(kb)
    by_id = {record.claim_id: record for record in kb.by_status()}

    assert resume.subsumed, "the candidate's own titles are subsumed, not dropped"
    for item in resume.subsumed:
        assert item.claim_id in by_id, "a subsumed claim must still be in the KB"
        assert "per-vacancy" in item.reason or "headline" in item.reason
    kinds = {item.kind for item in resume.subsumed}
    assert "title" in kinds


def test_the_experience_statement_is_excluded_by_kind() -> None:
    """'15+ years of IT experience' must not reach the draft by any route."""
    kb = kb_with_projects()
    resume = build(kb)
    reasons = {item.kind: item.reason for item in resume.excluded}

    assert "experience_statement" in reasons
    assert "domain" in reasons["experience_statement"]
    text = mr.render_markdown(resume, lang="en")
    assert "15+ years of IT experience" not in text


# --------------------------------------------------------------------------
# Proposals are offered, never published
# --------------------------------------------------------------------------
#
# The new positioning, the tenure wording and the project phrasing are new prose
# about the candidate. Only the candidate can approve prose about himself, so it
# lives in the source map and must not leak into either draft.


def test_no_proposal_reaches_either_draft() -> None:
    """A proposal that quietly appears in the resume is no longer a proposal."""
    resume = build(kb_with_projects())
    en = mr.render_markdown(resume, lang="en")
    ru = mr.render_markdown(resume, lang="ru")

    for label, proposed, why in mr.PROPOSALS:
        assert proposed not in en, f"the proposal {label!r} leaked into the English draft"
        assert proposed not in ru, f"the proposal {label!r} leaked into the Russian draft"
        assert why.strip(), f"the proposal {label!r} does not say why it needs a decision"


def test_an_approved_decision_is_not_still_listed_as_a_proposal() -> None:
    """T5.2: the four approved edits moved out of the waiting room.

    Two failure modes, opposite and both silent. An approved decision left in
    ``PROPOSALS`` makes the resume look unapproved while the wording is in fact
    published; an approved decision in neither table makes published prose about
    the candidate unattributable. So each approved text has to be in exactly one
    of the two tables, and in the drafts.
    """
    resume = build(kb_with_projects())
    en = mr.render_markdown(resume, lang="en")
    ru = mr.render_markdown(resume, lang="ru")

    approved = {text for _label, text, _why in mr.APPROVED_EDITORIAL_DECISIONS}
    assert mr.APPROVED_TENURE in approved
    assert mr.APPROVED_SUMMARY_OPENING in approved
    assert mr.APPROVED_PROJECTS_PHRASE in approved
    assert mr.DUTY_SOURCED_GROUP_HEADING in approved

    pending = {text for _label, text, _why in mr.PROPOSALS}
    assert not (approved & pending), "a decision cannot await approval and be approved"

    # Three of the four are published in the English draft; the fourth is a group
    # heading, which is published as a skills bullet.
    for text in (mr.APPROVED_SUMMARY_OPENING, mr.APPROVED_PROJECTS_PHRASE):
        assert text in en
    assert f"**{mr.DUTY_SOURCED_GROUP_HEADING}:**" in en
    # And the Russian draft carries the approved wording in Russian, not English.
    assert "15+ лет опыта в IT" in ru
    assert "применяемые в работе и в собственных проектах" in ru
    assert "в продакшене" not in ru
    assert "в продакшене" not in en


def test_the_tenure_decision_shows_its_arithmetic() -> None:
    """The '15+' figure must be traceable to the sources, not asserted."""
    decisions = {label: (text, why) for label, text, why in mr.APPROVED_EDITORIAL_DECISIONS}
    tenure = next(
        (text, why) for label, (text, why) in decisions.items() if label.startswith("Tenure")
    )
    text, why = tenure

    assert text == mr.APPROVED_TENURE
    assert "15+" in text
    # The arithmetic: the block starts in 2009, the last employed role ends in 2026.
    assert "2009" in why and "2026" in why
    # And it must say why the other two numbers are not a contradiction.
    assert "domain_years.it_support" in why
    assert "years_experience = 3" in why


def test_every_duty_sourced_skill_names_its_source() -> None:
    """T5.2 edit 4: each skill in the new group is carried by a real duty.

    The group exists because the KB holds no ``skill`` claim for any of these
    eight technologies, yet the candidate's professional history names every one.
    The check is deliberately literal -- the cited duty's own text must contain
    the phrase -- because "has a source" is otherwise a promise, and a label that
    drifted from its duty would keep rendering without anything noticing.
    """
    kb = kb_with_projects()
    claims = {record.claim_id: record for record in kb.by_status()}
    seen: list[str] = []

    for heading, entries in mr.DUTY_SOURCED_SKILL_GROUPS:
        assert heading == mr.DUTY_SOURCED_GROUP_HEADING
        for label, claim_id, phrase in entries:
            record = claims[claim_id]
            assert record.kind == "duty", f"{label}: {claim_id} is a {record.kind}"
            assert record.role_id, f"{label}: {claim_id} has no role, so it is not history"
            assert phrase.lower() in record.value.lower(), (
                f"{label} is sourced from {claim_id}, which does not contain {phrase!r}"
            )
            seen.append(label)

    assert seen == [
        "Windows Server",
        "Active Directory",
        "Exchange",
        "Hyper-V",
        "Networking",
        "IP Telephony",
        "IT Support",
        "Bitrix24",
    ]


def test_the_duty_sourced_group_reaches_both_drafts() -> None:
    """The group is published, and its duties are credited to it in the map."""
    kb = kb_with_projects()
    resume = build(kb)
    en = mr.render_markdown(resume, lang="en")
    ru = mr.render_markdown(resume, lang="ru")
    sources = mr.render_source_map(resume, kb)

    line = f"- **{mr.DUTY_SOURCED_GROUP_HEADING}:** " + ", ".join(
        label for _heading, entries in mr.DUTY_SOURCED_SKILL_GROUPS for label, _c, _p in entries
    )
    assert line in en
    assert line in ru, "the skills bullet is language-neutral and must appear verbatim"

    assert "## Core-skills groups sourced from duties" in sources
    for _heading, entries in mr.DUTY_SOURCED_SKILL_GROUPS:
        for label, claim_id, phrase in entries:
            assert f"| {label} | `{claim_id}` | {phrase} |" in sources

    # And the group's duties are rendered claims, so the partition still closes.
    assert mr.unaccounted_claims(kb, resume) == []


def test_the_source_map_carries_the_brief_map_and_the_decisions() -> None:
    kb = kb_with_projects()
    resume = build(kb)
    text = mr.render_source_map(resume, kb)

    assert "## Brief map" in text
    assert "## Approved editorial decisions" in text
    assert "## Proposals awaiting approval" not in text, "nothing is awaiting approval"
    total = len(kb.by_status())
    assert f"| **total** | **{total}** |" in text
    for label, proposed, _why in mr.APPROVED_EDITORIAL_DECISIONS:
        assert label in text
        assert proposed in text


def test_every_origin_marker_has_a_label() -> None:
    """A new origin appearing must not reach the brief map unlabelled."""
    kb = kb_with_projects()
    for record in kb.by_status():
        marker = str(record.evidence.get("source_file") or "unknown")
        assert marker in mr.SOURCE_LABELS, f"{marker!r} has no label in SOURCE_LABELS"


# --------------------------------------------------------------------------
# The shipped drafts
#
# Everything above tests the builder. These read the files on disk, because a
# pin that only rebuilds in memory passes happily while the shipped draft is
# stale -- and the four T5.2 decisions were approved once and have to survive
# the next generation, not just the next import.
# --------------------------------------------------------------------------

#: The four approved T5.2 edits as they must appear in the files on disk:
#: ``(what the edit is, marker in the English draft, marker in the Russian one)``.
T5_2_SHIPPED_MARKERS: tuple[tuple[str, str, str], ...] = (
    (
        "the approved summary opening",
        "IT specialist with 15+ years in IT",
        "IT-специалист с 15+ лет опыта в IT",
    ),
    (
        "the approved tenure figure",
        "15+ years in IT",
        "15+ лет опыта в IT",
    ),
    (
        "the approved replacement for 'in production'",
        "used in my work and in my own projects",
        "применяемые в работе и в собственных проектах",
    ),
    (
        "the support & systems skills group",
        "**IT Support & Systems Administration:**",
        "**IT Support & Systems Administration:**",
    ),
)


def shipped_draft(name: str) -> str:
    """One generated draft, read from disk."""
    path = REPO_ROOT / "artifacts" / name
    if not path.is_file():
        pytest.skip(f"{name} has not been generated in this checkout")
    return path.read_text(encoding="utf-8")


def test_the_four_approved_edits_are_in_the_files_on_disk() -> None:
    """T5.2, all four decisions, checked against the published drafts.

    This is the pin that outlives the session: the decisions were approved in
    conversation, the generator was changed to apply them, and from here on a
    regression that drops one has to fail a test rather than be noticed by a
    reader of the resume. The replaced wording is checked too -- an edit that
    stops applying would otherwise leave both versions on the page.
    """
    en = shipped_draft("master_resume.md")
    ru = shipped_draft("master_resume.ru.md")

    for what, en_marker, ru_marker in T5_2_SHIPPED_MARKERS:
        assert en_marker in en, f"{what} is missing from master_resume.md"
        assert ru_marker in ru, f"{what} is missing from master_resume.ru.md"

    assert "in production" not in en
    assert "в продакшене" not in ru
    assert "AI Automation Engineer with 15+ years of IT experience:" not in en


def test_the_files_on_disk_are_what_the_generator_produces_now() -> None:
    """A stale draft is a draft nobody regenerated.

    Read the two files and rebuild in memory from the same sources. If they
    differ, one of the two is wrong, and either way the file a reader would open
    is not the file this code claims to produce.
    """
    kb = kb_with_projects()
    resume = build(kb)
    assert shipped_draft("master_resume.md") == mr.render_markdown(resume, lang="en")
    assert shipped_draft("master_resume.ru.md") == mr.render_markdown(resume, lang="ru")


def test_the_shipped_source_map_attributes_the_approved_wording() -> None:
    """The map that ships must credit the group and the decisions."""
    text = shipped_draft("master_resume_sources.md")

    assert "## Approved editorial decisions" in text
    assert "## Core-skills groups sourced from duties" in text
    assert "## Proposals awaiting approval" not in text
    for label, _text, _why in mr.APPROVED_EDITORIAL_DECISIONS:
        assert label in text
    for _heading, entries in mr.DUTY_SOURCED_SKILL_GROUPS:
        for label, claim_id, phrase in entries:
            assert f"| {label} | `{claim_id}` | {phrase} |" in text


def test_every_project_status_is_either_anchored_or_declared() -> None:
    """A status label the repository never states must be declared as a judgement.

    The label printed after each project's name comes from the catalogue, not from
    a sentence the project wrote. Where the repository does state its status, an
    anchored ``project_status`` claim carries it verbatim and the project must not
    also be listed as unanchored. Every project has to be in exactly one of the two
    sets, so a project added without either cannot pass.
    """
    anchored = {
        spec.project_id
        for spec in PROJECT_CATALOGUE
        if spec.anchors_of(KIND_PROJECT_STATUS)
    }
    declared = set(mr.STATUS_WITHOUT_ANCHOR)
    all_projects = {spec.project_id for spec in PROJECT_CATALOGUE}

    assert anchored | declared == all_projects, (
        "a project's status label is neither backed by a quoted line nor declared "
        f"as a judgement: {sorted(all_projects - anchored - declared)}"
    )
    assert not (anchored & declared), (
        "these projects state their status in the repository, so listing them as "
        f"unanchored overstates what is a judgement: {sorted(anchored & declared)}"
    )
    for project_id, reason in mr.STATUS_WITHOUT_ANCHOR.items():
        assert reason.strip(), f"{project_id} is declared unanchored with no reason"

