"""Tests for the candidate interview (Stage 0 / T4.5).

T1 pinned what the parser reads, T2 what the knowledge base may do, T3 that the
sources are compared, T4 that three reviewers say something true. T4.5 pins the
one thing none of them could: that a *person* was asked, and that their answer
was recorded without being invented, promoted or lost.

Six properties carry the weight here.

* **The questions are grounded.** Every question cites a T3 contradiction, a T4
  finding or a claim; the three T3 ambiguities each get one; and the Bitrix24
  conflict is split into an experience question and a preference question, so
  "I did Bitrix24" cannot be read as "I want Bitrix24".
* **An answer is not a confirmation.** Extracting facts writes nothing to the
  knowledge base -- proved by hashing it around the call -- and every extracted
  fact is ``PENDING`` until one fact is confirmed by id.
* **An edit requires re-confirmation**, and revising a confirmed fact does not
  rewrite it: the original keeps its status and gains a ``superseded_by`` link,
  and only when the revision is itself confirmed.
* **Nothing is invented.** A quote that is not a verbatim substring of the answer
  is refused, as is a number or a technology the answer never mentioned. The
  deterministic extractor's ``value`` *is* its ``quote``.
* **The resume half of the knowledge base is untouched**, by hashes around a full
  run, and a resume re-extract afterwards leaves interview facts alone.
* **The LLM is never consulted by accident.** The default mode is ``off``, so no
  test can reach a paid endpoint; the model path is exercised with an injected
  fake, which is also how the validation nets are shown to have teeth.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from ai_assistant.candidate.interview import (
    DEFAULT_SESSION_PATH,
    SESSION_FORMAT_VERSION,
    InterviewEngine,
    ProposalStatus,
    SessionFormatError,
    candidate_interview_cmd,
    format_progress,
    load_session,
    load_sources,
    plan_fingerprint,
    resolve_session_path,
)
from ai_assistant.candidate.interview_extract import (
    LLM_AUTO,
    LLM_MODE_ENV,
    LLM_OFF,
    LLM_REQUIRED,
    ExtractionError,
    extract_facts,
    make_claim_id,
    numbers_in,
    resolve_llm_mode,
    validate_fact,
)
from ai_assistant.candidate.interview_questions import (
    ALLOWED_KINDS,
    KIND_CLASS,
    SECTION_ORDER,
    FactClass,
    Grounding,
    Question,
    Section,
    build_plan,
    fact_class_of,
    format_question,
    is_experience_kind,
)
from ai_assistant.candidate.kb import (
    INTERVIEW_SOURCE,
    CandidateKnowledgeBase,
    is_interview_sourced,
)
from ai_assistant.candidate.resume_parser import parse_resume

REPO_ROOT = Path(__file__).resolve().parent.parent
RESUME_PATH = REPO_ROOT / "resume.md"
PROFILE_PATH = REPO_ROOT / "candidate_profile.json"
KB_PATH = REPO_ROOT / "candidate_kb.json"
STATE_DB_PATH = REPO_ROOT / "state.db"

#: A full interview run must leave every one of these byte-identical. The session
#: file and the knowledge base it is pointed at are *not* here: those are the
#: command's declared outputs, and the tests point them into tmp_path.
#:
#: ai_assistant/candidate_profile.json used to be in this tuple. T8 deleted it:
#: it was a byte-identical copy of the root profile and nothing read it.
PROTECTED_FILES = (RESUME_PATH, PROFILE_PATH, KB_PATH, STATE_DB_PATH)

#: Facts the deterministic extractor produces are verbatim, so any sentence works
#: as an answer. This one names no technology and states no figure, so it cannot
#: trip the validation nets for an unrelated reason.
ANSWER_TEXT = "I want to build automation pipelines and keep working remotely."


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes_of(paths: tuple[Path, ...]) -> dict[str, str]:
    return {str(path): sha256_of(path) for path in paths}


@pytest.fixture(scope="module")
def built_kb(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A KB built from the real resume, so tests do not depend on a local build.

    Parsed with the label ``resume.md`` rather than an absolute path so evidence
    locations match what the CLI produces.
    """
    directory = tmp_path_factory.mktemp("t45")
    path = directory / "candidate_kb.json"
    knowledge_base = CandidateKnowledgeBase()
    knowledge_base.merge_claims(
        parse_resume(RESUME_PATH.read_text(encoding="utf-8"), "resume.md"),
        source_file="resume.md",
        source_sha256=sha256_of(RESUME_PATH),
    )
    knowledge_base.save(path)
    return path


@pytest.fixture(scope="module")
def sources(built_kb: Path):
    """The T3/T4-backed sources, loaded once: every engine start rebuilds these."""
    return load_sources(str(built_kb), str(PROFILE_PATH), str(RESUME_PATH))


@pytest.fixture(scope="module")
def plan(sources) -> tuple[Question, ...]:
    return build_plan(sources.plan_context())


@pytest.fixture
def kb_file(tmp_path: Path, built_kb: Path) -> Path:
    """A fresh, writable copy of the KB: confirm/reject must not share state."""
    target = tmp_path / "candidate_kb.json"
    target.write_bytes(built_kb.read_bytes())
    return target


@pytest.fixture
def session_path(tmp_path: Path) -> Path:
    return tmp_path / "interview_state.json"


@pytest.fixture
def engine(kb_file: Path, session_path: Path, sources) -> InterviewEngine:
    """An engine pointed at a temp KB and a temp session, with sources pre-loaded."""
    instance = InterviewEngine(
        kb_path=str(kb_file),
        session_path=str(session_path),
        profile_path=str(PROFILE_PATH),
        resume_path=str(RESUME_PATH),
    )
    instance.sources = sources
    return instance


def resume_claim_snapshot(path: Path) -> dict[str, tuple[str, bool, str]]:
    """Every non-interview claim as ``id -> (status, in_source, evidence)``."""
    knowledge_base = CandidateKnowledgeBase.load(path)
    return {
        claim_id: (record.status, record.in_source, json.dumps(record.evidence, sort_keys=True))
        for claim_id, record in knowledge_base.claims.items()
        if not is_interview_sourced(record)
    }


def interview_claims(path: Path) -> dict[str, object]:
    knowledge_base = CandidateKnowledgeBase.load(path)
    return {
        claim_id: record
        for claim_id, record in knowledge_base.claims.items()
        if is_interview_sourced(record)
    }


def walk(engine: InterviewEngine, *, answers: int = 3) -> InterviewEngine:
    """Answer and decide on the first few questions, then skip one.

    Deliberately mixes confirm, reject and an edit, so the "nothing else moved"
    assertions below run against a session that has actually done something.
    """
    engine.start()
    for index in range(answers):
        outcome = engine.answer(f"{ANSWER_TEXT} Step {index + 1}.")
        if len(outcome.facts) >= 2:
            engine.confirm(outcome.facts[0].claim_id)
            engine.reject(outcome.facts[1].claim_id)
        elif outcome.facts:
            engine.confirm(outcome.facts[0].claim_id)
        engine.advance()
    engine.skip()
    return engine


# --------------------------------------------------------------------------
# The plan is grounded in T3 and T4
# --------------------------------------------------------------------------


def test_every_question_carries_grounding(plan: tuple[Question, ...]) -> None:
    assert plan, "the plan must not be empty"
    for question in plan:
        assert question.grounding, f"{question.question_id} has no grounding"


def test_grounding_sources_are_known(plan: tuple[Question, ...]) -> None:
    seen = {item.source for question in plan for item in question.grounding}
    assert seen <= {"t3", "t4", "kb", "profile"}, f"unexpected grounding sources: {seen}"


def test_a_question_without_grounding_cannot_be_built() -> None:
    """The grounding requirement is structural, not a convention."""
    with pytest.raises(ValueError, match="grounding"):
        Question(
            question_id="q.x",
            section=Section.GOALS,
            prompt="?",
            why="because",
            fact_kind="goal_direction",
            scope="x",
            grounding=(),
        )


def test_a_question_with_an_unknown_fact_kind_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="unknown fact kind"):
        Question(
            question_id="q.x",
            section=Section.GOALS,
            prompt="?",
            why="because",
            fact_kind="not_a_kind",
            scope="x",
            grounding=(Grounding(source="kb", rule="r", subject="s"),),
        )


def test_the_t3_ambiguities_each_get_a_question(plan: tuple[Question, ...]) -> None:
    """Every T3 conflict still present in the profile must be asked about.

    T7.3 dropped ``bitrix_experience_vs_excluded`` from this set: the candidate
    confirmed Bitrix24 as a target direction, so the ban was removed and the
    conflict it described no longer exists. The generic
    ``test_every_firing_t3_rule_gets_a_question`` still proves that *any* rule
    which actually fires is asked about; this test pins the known live set.
    """
    rules = {
        "total_experience_vs_it_support",
        "years_experience_vs_domain_years",
    }
    grounded = {
        item.rule for question in plan for item in question.grounding if item.source == "t3"
    }
    missing = rules - grounded
    assert not missing, f"T3 contradictions with no question: {sorted(missing)}"


def test_every_firing_t3_rule_gets_a_question(plan: tuple[Question, ...], sources) -> None:
    """Not just the three named ones: whatever T3 found, the interview asks about."""
    firing = {item.rule for item in sources.contradictions}
    grounded = {
        item.rule for question in plan for item in question.grounding if item.source == "t3"
    }
    assert firing <= grounded, f"fired but never asked: {sorted(firing - grounded)}"


def test_bitrix_is_no_longer_a_conflict_in_the_live_plan(plan: tuple[Question, ...]) -> None:
    """T7.3: with the ban gone, no Bitrix24 question is asked.

    The resume still presents Bitrix24 as experience, but the profile no longer
    excludes it, so there is nothing to reconcile. If a question reappears here,
    the ban has come back.
    """
    bitrix = [
        question
        for question in plan
        if any(item.rule == "bitrix_experience_vs_excluded" for item in question.grounding)
    ]
    assert bitrix == [], [question.question_id for question in bitrix]


def test_the_bitrix_conflict_splits_into_experience_and_preference(
    built_kb: Path, tmp_path: Path
) -> None:
    """The single most important distinction in the interview.

    "I ran Bitrix24 for a living" and "I do not want another Bitrix24 job" are
    different facts. They come from one T3 contradiction and must land in
    different classes, or the pipeline will paste one into the other's slot.

    T7.3 removed the live contradiction, so this is now a **positive control**:
    the ban is restored in a throwaway profile to prove the split still works.
    Without it, the mechanism would be untested until someone re-added a ban.
    """
    data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    data["excluded_roles"] = list(data["excluded_roles"]) + ["bitrix"]
    banned_profile = tmp_path / "with_bitrix_ban.json"
    banned_profile.write_text(json.dumps(data, indent=2), encoding="utf-8")

    sources = load_sources(str(built_kb), str(banned_profile), str(RESUME_PATH))
    plan = build_plan(sources.plan_context())

    bitrix = [
        question
        for question in plan
        if any(item.rule == "bitrix_experience_vs_excluded" for item in question.grounding)
    ]
    classes = {question.fact_class for question in bitrix}
    assert FactClass.EXPERIENCE in classes
    assert FactClass.PREFERENCE in classes
    assert len(bitrix) >= 2


def test_experience_and_preference_kinds_are_disjoint() -> None:
    for kind, fact_class in KIND_CLASS.items():
        assert is_experience_kind(kind) is (fact_class is FactClass.EXPERIENCE), kind


def test_every_question_asks_for_an_allowed_kind(plan: tuple[Question, ...]) -> None:
    """A question whose kind cannot be stored would fail at extraction time, not here."""
    for question in plan:
        assert question.fact_kind in ALLOWED_KINDS, question.question_id


def test_a_preference_is_never_an_experience_kind() -> None:
    """A structural guard for later stages: a preference may not enter a resume."""
    assert not is_experience_kind("preference_role")
    assert not is_experience_kind("constraint_condition")
    assert is_experience_kind("experience_note")
    assert fact_class_of("preference_role") is FactClass.PREFERENCE


def test_questions_are_grounded_in_t4_findings(plan: tuple[Question, ...], sources) -> None:
    """The gap, the missing salary and the unsupported skills are all asked about."""
    grounded = {
        item.rule for question in plan for item in question.grounding if item.source == "t4"
    }
    for rule in (
        "hr_employment_gap",
        "hr_no_salary_expectation",
        "tech_skill_without_project_context",
        "ats_contact_channel_gap",
    ):
        if any(finding.rule == rule for finding in sources.findings):
            assert rule in grounded, f"{rule} fired but produced no question"


def test_the_gap_and_salary_questions_are_questions_not_verdicts(
    plan: tuple[Question, ...],
) -> None:
    """The brief is explicit: an absent salary and a gap are not errors."""
    gap = next(q for q in plan if q.question_id.startswith("q.chronology.gap"))
    salary = next(q for q in plan if q.question_id == "q.goals.salary")
    assert "gap is a question, not a mistake" in gap.why.lower()
    assert "not a defect" in salary.why
    assert "saying no is a complete answer" in salary.why.lower()


def test_plan_has_no_duplicate_question_ids(plan: tuple[Question, ...]) -> None:
    ids = [question.question_id for question in plan]
    assert len(ids) == len(set(ids))


def test_plan_covers_every_section(plan: tuple[Question, ...]) -> None:
    assert {question.section for question in plan} == set(SECTION_ORDER)


def test_plan_is_deterministic(sources) -> None:
    """The same sources must give the same plan, or a resumed cursor points elsewhere."""
    first = [question.to_dict() for question in build_plan(sources.plan_context())]
    second = [question.to_dict() for question in build_plan(sources.plan_context())]
    assert first == second


def test_every_question_round_trips_through_json(plan: tuple[Question, ...]) -> None:
    for question in plan:
        assert Question.from_dict(question.to_dict()) == question


def test_an_unknown_section_is_refused(sources) -> None:
    from ai_assistant.candidate.interview_questions import questions_for_section

    with pytest.raises(ValueError, match="unknown section"):
        questions_for_section(sources.plan_context(), "not_a_section")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# One question per step
# --------------------------------------------------------------------------


def test_one_question_per_step(engine: InterviewEngine, plan: tuple[Question, ...]) -> None:
    """A step shows one question -- not the next one, not the whole plan."""
    session = engine.start()
    assert len(session.plan) > 1
    current = engine.current_question()
    assert current is not None

    rendered = format_question(current, position=1, total=len(session.plan), explain=True)
    assert current.prompt in rendered
    assert rendered.count("[1/") == 1
    for other in plan:
        if other.question_id != current.question_id:
            assert other.prompt not in rendered


def test_the_step_counter_matches_the_plan_length(engine: InterviewEngine) -> None:
    session = engine.start()
    rendered = format_question(session.current(), position=1, total=len(session.plan))
    assert f"[1/{len(session.plan)}]" in rendered


def test_clarify_explains_without_changing_the_step(engine: InterviewEngine) -> None:
    session = engine.start()
    before = session.cursor
    rendered = format_question(session.current(), explain=True)
    assert "why this is being asked" in rendered
    assert "grounded in:" in rendered
    assert engine.session.cursor == before


# --------------------------------------------------------------------------
# An answer is not a confirmation
# --------------------------------------------------------------------------


def test_answering_writes_nothing_to_the_knowledge_base(
    engine: InterviewEngine, kb_file: Path
) -> None:
    engine.start()
    before = sha256_of(kb_file)
    outcome = engine.answer(ANSWER_TEXT)
    assert outcome.facts, "the fixture answer must produce at least one fact"
    assert sha256_of(kb_file) == before, "an answer must not touch the knowledge base"


def test_answering_does_not_confirm(engine: InterviewEngine, kb_file: Path) -> None:
    engine.start()
    engine.answer(ANSWER_TEXT)
    assert all(
        decision.status == ProposalStatus.PENDING for decision in engine.session.proposals.values()
    )
    assert interview_claims(kb_file) == {}


def test_confirm_moves_exactly_one_fact(engine: InterviewEngine, kb_file: Path) -> None:
    engine.start()
    outcome = engine.answer(f"{ANSWER_TEXT}\nAnd a second, separate statement.")
    assert len(outcome.facts) == 2

    chosen = outcome.facts[0].claim_id
    decision, changed = engine.confirm(chosen)
    assert changed is True
    assert decision.status == ProposalStatus.CONFIRMED

    stored = interview_claims(kb_file)
    assert set(stored) == {chosen}
    assert stored[chosen].status == "CONFIRMED_BY_USER"

    other = outcome.facts[1].claim_id
    assert engine.session.proposals[other].status == ProposalStatus.PENDING
    assert other not in stored


def test_confirm_is_idempotent(engine: InterviewEngine, kb_file: Path) -> None:
    engine.start()
    fact_id = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.confirm(fact_id)
    after_first = sha256_of(kb_file)
    _, changed = engine.confirm(fact_id)
    assert changed is False, "confirming an already-confirmed fact must write nothing"
    assert sha256_of(kb_file) == after_first


def test_reject_keeps_the_record(engine: InterviewEngine, kb_file: Path) -> None:
    """A rejection is a decision, not an absence: T5 must tell the two apart."""
    engine.start()
    fact_id = engine.answer(ANSWER_TEXT).facts[0].claim_id
    decision, changed = engine.reject(fact_id)
    assert changed is True
    assert decision.status == ProposalStatus.REJECTED
    stored = interview_claims(kb_file)
    assert stored[fact_id].status == "REJECTED_BY_USER"


def test_reject_is_idempotent(engine: InterviewEngine, kb_file: Path) -> None:
    engine.start()
    fact_id = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.reject(fact_id)
    after_first = sha256_of(kb_file)
    _, changed = engine.reject(fact_id)
    assert changed is False
    assert sha256_of(kb_file) == after_first


def test_an_unknown_fact_id_is_reported_not_created(engine: InterviewEngine) -> None:
    engine.start()
    with pytest.raises(SessionFormatError, match="no extracted fact"):
        engine.confirm("goal_direction-deadbeef00")


def test_confirming_one_fact_does_not_touch_another(engine: InterviewEngine, kb_file: Path) -> None:
    engine.start()
    outcome = engine.answer(f"{ANSWER_TEXT}\nAnd a second, separate statement.")
    first, second = (fact.claim_id for fact in outcome.facts)
    engine.confirm(first)
    engine.confirm(second)
    stored = interview_claims(kb_file)
    assert stored[first].status == "CONFIRMED_BY_USER"
    assert stored[second].status == "CONFIRMED_BY_USER"
    assert stored[first].value != stored[second].value


# --------------------------------------------------------------------------
# Edit, then re-confirm
# --------------------------------------------------------------------------


def test_edit_creates_a_new_pending_fact(engine: InterviewEngine, kb_file: Path) -> None:
    engine.start()
    original = engine.answer(ANSWER_TEXT).facts[0].claim_id
    revised = engine.edit(original, "I want to build automation pipelines for travel companies.")

    assert revised.claim_id != original, "ids are content-derived, so a new text is a new fact"
    assert revised.status == ProposalStatus.PENDING
    assert engine.session.proposals[original].status == ProposalStatus.SUPERSEDED
    assert engine.session.proposals[original].superseded_by == revised.claim_id


def test_an_edited_fact_needs_its_own_confirmation(engine: InterviewEngine, kb_file: Path) -> None:
    engine.start()
    original = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.confirm(original)
    revised = engine.edit(original, "I want to build automation pipelines for travel companies.")

    assert revised.claim_id not in interview_claims(kb_file), (
        "the revision must not be in the knowledge base before it is confirmed"
    )
    assert engine.session.proposals[revised.claim_id].status == ProposalStatus.PENDING


def test_revising_a_confirmed_fact_keeps_the_original_until_the_revision_is_confirmed(
    engine: InterviewEngine, kb_file: Path
) -> None:
    """Nothing is demoted and nothing is rewritten: the link appears on confirmation."""
    engine.start()
    original = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.confirm(original)

    revised = engine.edit(original, "I want to build automation pipelines for travel companies.")
    stored = interview_claims(kb_file)
    assert stored[original].status == "CONFIRMED_BY_USER", "an edit must not demote a claim"
    assert "superseded_by" not in stored[original].evidence, (
        "the revision has not been confirmed yet, so it has not taken effect"
    )

    engine.confirm(revised.claim_id)
    stored = interview_claims(kb_file)
    assert stored[original].evidence["superseded_by"] == revised.claim_id
    assert stored[original].status == "CONFIRMED_BY_USER"
    assert stored[revised.claim_id].status == "CONFIRMED_BY_USER"


def test_rejecting_a_revision_leaves_the_original_effective(
    engine: InterviewEngine, kb_file: Path
) -> None:
    engine.start()
    original = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.confirm(original)
    revised = engine.edit(original, "I want to build automation pipelines for travel companies.")
    engine.reject(revised.claim_id)

    stored = interview_claims(kb_file)
    assert "superseded_by" not in stored[original].evidence
    assert stored[original].status == "CONFIRMED_BY_USER"


def test_editing_with_the_same_text_changes_nothing(
    engine: InterviewEngine, kb_file: Path
) -> None:
    engine.start()
    original = engine.answer(ANSWER_TEXT).facts[0].claim_id
    before = sha256_of(kb_file)
    same = engine.edit(original, ANSWER_TEXT)
    assert same.claim_id == original
    assert engine.session.proposals[original].status == ProposalStatus.PENDING
    assert sha256_of(kb_file) == before


def test_an_empty_correction_is_refused(engine: InterviewEngine) -> None:
    engine.start()
    original = engine.answer(ANSWER_TEXT).facts[0].claim_id
    with pytest.raises(SessionFormatError, match="empty"):
        engine.edit(original, "   ")


def test_an_edit_records_no_number_the_candidate_did_not_type(
    engine: InterviewEngine, kb_file: Path
) -> None:
    """An edit is the candidate's own words, so it is verbatim by construction."""
    engine.start()
    original = engine.answer(ANSWER_TEXT).facts[0].claim_id
    revised = engine.edit(original, "I have run automation pipelines for 4 years.")
    assert revised.fact.quote == revised.fact.value
    assert "4" in revised.fact.value


# --------------------------------------------------------------------------
# Answering twice is answering once
# --------------------------------------------------------------------------


def test_answering_twice_with_the_same_text_is_idempotent(engine: InterviewEngine) -> None:
    engine.start()
    first = engine.answer(ANSWER_TEXT)
    count_after_first = len(engine.session.proposals)
    second = engine.answer(ANSWER_TEXT)

    assert [fact.claim_id for fact in first.facts] == [fact.claim_id for fact in second.facts]
    assert len(engine.session.proposals) == count_after_first


def test_re_answering_does_not_demote_a_confirmed_fact(
    engine: InterviewEngine, kb_file: Path
) -> None:
    """The trap this guards: a second pass resetting a decided fact to PENDING."""
    engine.start()
    fact_id = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.confirm(fact_id)
    after_confirm = sha256_of(kb_file)

    engine.answer(ANSWER_TEXT)
    assert engine.session.proposals[fact_id].status == ProposalStatus.CONFIRMED
    assert sha256_of(kb_file) == after_confirm


def test_answering_with_different_words_adds_facts(engine: InterviewEngine) -> None:
    engine.start()
    engine.answer(ANSWER_TEXT)
    first = set(engine.session.proposals)
    engine.answer("A completely different sentence about Docker infrastructure.")
    assert set(engine.session.proposals) > first


# --------------------------------------------------------------------------
# Skip, back and progress
# --------------------------------------------------------------------------


def test_skip_advances_and_writes_nothing(engine: InterviewEngine, kb_file: Path) -> None:
    session = engine.start()
    start_cursor = session.cursor
    question_id = engine.current_question().question_id
    before = sha256_of(kb_file)

    engine.skip()
    assert engine.session.cursor == start_cursor + 1
    assert engine.session.turns[question_id].skipped is True
    assert sha256_of(kb_file) == before, "a skip is not a decision about a fact"


def test_skip_does_not_produce_facts(engine: InterviewEngine) -> None:
    engine.start()
    engine.skip()
    assert engine.session.proposals == {}


def test_skipping_the_whole_plan_completes_it(engine: InterviewEngine) -> None:
    session = engine.start()
    for _ in range(len(session.plan)):
        engine.skip()
    assert engine.session.is_complete()
    assert engine.current_question() is None
    payload = engine.progress()
    assert payload["complete"] is True
    assert payload["questions_skipped"] == len(session.plan)


def test_back_keeps_every_decision(engine: InterviewEngine) -> None:
    engine.start()
    fact_id = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.confirm(fact_id)
    engine.advance()
    engine.go_back()
    assert engine.current_question().question_id.startswith("q.")
    assert engine.session.proposals[fact_id].status == ProposalStatus.CONFIRMED


def test_back_at_the_start_stays_at_the_start(engine: InterviewEngine) -> None:
    engine.start()
    engine.go_back()
    assert engine.session.cursor == 0


def test_advance_at_the_end_stays_at_the_end(engine: InterviewEngine) -> None:
    session = engine.start()
    for _ in range(len(session.plan) + 5):
        engine.advance()
    assert engine.session.cursor == len(session.plan)


def test_progress_reports_sections_and_decisions(engine: InterviewEngine, plan) -> None:
    engine.start()
    engine.answer(ANSWER_TEXT)
    engine.skip()
    payload = engine.progress()

    # Count against the plan instead of a literal: T7.3 removed the Bitrix24 ban,
    # which resolved the conflict that used to contribute two questions (19 -> 17).
    assert payload["questions_total"] == len(plan)
    assert payload["questions_skipped"] == 1
    assert set(payload["sections"]) == {str(section) for section in SECTION_ORDER}
    assert payload["proposal_counts"][str(ProposalStatus.PENDING)] >= 1
    rendered = format_progress(payload)
    assert "interview progress" in rendered
    assert "sections:" in rendered


def test_progress_json_is_serialisable(engine: InterviewEngine) -> None:
    engine.start()
    engine.answer(ANSWER_TEXT)
    assert json.loads(json.dumps(engine.progress(), ensure_ascii=False))


# --------------------------------------------------------------------------
# The resume half of the knowledge base is untouched
# --------------------------------------------------------------------------


def test_a_full_interview_leaves_every_resume_claim_untouched(
    engine: InterviewEngine, kb_file: Path
) -> None:
    before = resume_claim_snapshot(kb_file)
    assert len(before) == 123, "the fixture KB should hold the real resume's claims"

    walk(engine)

    assert resume_claim_snapshot(kb_file) == before, (
        "an interview must not change a single resume claim"
    )
    assert interview_claims(kb_file), "the walk should have decided on at least one fact"


def test_the_envelope_keeps_the_resume_snapshot(engine: InterviewEngine, kb_file: Path) -> None:
    """``source_file``/``source_sha256``/``updated_at`` describe the resume, not the interview.

    This is exactly why interview writes do not go through ``merge_claims``.
    """
    before = CandidateKnowledgeBase.load(kb_file)
    walk(engine)
    after = CandidateKnowledgeBase.load(kb_file)

    assert after.source_file == before.source_file
    assert after.source_sha256 == before.source_sha256
    assert after.updated_at == before.updated_at


def test_an_interview_fact_is_marked_as_such(engine: InterviewEngine, kb_file: Path) -> None:
    engine.start()
    fact_id = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.confirm(fact_id)

    record = interview_claims(kb_file)[fact_id]
    assert is_interview_sourced(record)
    assert record.evidence["source_file"] == INTERVIEW_SOURCE
    assert record.evidence["quote"] == record.value, "the quote is the candidate's own words"
    assert record.evidence["question_id"]
    assert record.evidence["session_id"] == engine.session.session_id
    assert record.evidence["answered_at"]
    assert record.in_source is True


def test_a_resume_merge_leaves_interview_facts_alone(
    engine: InterviewEngine, kb_file: Path
) -> None:
    """The guard in ``merge_claims``: re-extracting the resume must not eat answers."""
    engine.start()
    fact_id = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.confirm(fact_id)

    knowledge_base = CandidateKnowledgeBase.load(kb_file)
    report = knowledge_base.merge_claims(
        parse_resume(RESUME_PATH.read_text(encoding="utf-8"), "resume.md"),
        source_file="resume.md",
        source_sha256=sha256_of(RESUME_PATH),
        now="2026-09-22T00:00:00+00:00",
    )

    record = knowledge_base.get(fact_id)
    assert record is not None
    assert record.status == "CONFIRMED_BY_USER"
    assert record.in_source is True, "a resume merge must not mark an answer absent"
    assert is_interview_sourced(record), "a resume merge must not relabel its provenance"
    assert report.skipped_foreign == 1


def test_a_resume_merge_still_refreshes_every_resume_claim(
    engine: InterviewEngine, kb_file: Path
) -> None:
    """The guard must not have changed any T2 behaviour."""
    engine.start()
    fact_id = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.confirm(fact_id)

    knowledge_base = CandidateKnowledgeBase.load(kb_file)
    report = knowledge_base.merge_claims(
        parse_resume(RESUME_PATH.read_text(encoding="utf-8"), "resume.md"),
        source_file="resume.md",
        source_sha256=sha256_of(RESUME_PATH),
        now="2026-09-22T00:00:00+00:00",
    )
    assert report.refreshed == 123
    assert report.marked_absent == 0
    assert report.added == 0


def test_the_merge_summary_is_unchanged_when_there_is_no_interview_data(
    built_kb: Path,
) -> None:
    """T2's printed summary must stay byte-identical for a resume-only merge."""
    knowledge_base = CandidateKnowledgeBase.load(built_kb)
    report = knowledge_base.merge_claims(
        parse_resume(RESUME_PATH.read_text(encoding="utf-8"), "resume.md"),
        source_file="resume.md",
        source_sha256=sha256_of(RESUME_PATH),
        now="2026-09-22T00:00:00+00:00",
    )
    assert "interview" not in report.summary()
    assert "left alone" not in report.summary()
    assert report.skipped_foreign == 0


def test_the_merge_summary_mentions_foreign_claims_when_there_are_any(
    engine: InterviewEngine, kb_file: Path
) -> None:
    engine.start()
    fact_id = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.confirm(fact_id)
    knowledge_base = CandidateKnowledgeBase.load(kb_file)
    report = knowledge_base.merge_claims(
        parse_resume(RESUME_PATH.read_text(encoding="utf-8"), "resume.md"),
        source_file="resume.md",
        source_sha256=sha256_of(RESUME_PATH),
        now="2026-09-22T00:00:00+00:00",
    )
    assert "left alone 1 interview fact(s)" in report.summary()


# --------------------------------------------------------------------------
# Nothing is invented
# --------------------------------------------------------------------------


def test_a_quote_that_is_not_in_the_answer_is_refused() -> None:
    reason = validate_fact(
        kind="experience_note",
        value="Ran the Bitrix24 migration",
        quote="this text is not in the answer",
        answer="I ran n8n workflows.",
    )
    assert reason is not None
    assert "verbatim" in reason


def test_a_number_the_answer_never_gave_is_refused() -> None:
    reason = validate_fact(
        kind="experience_tenure",
        value="11 years of IT support",
        quote="I have worked in IT support",
        answer="I have worked in IT support",
    )
    assert reason is not None
    assert "number" in reason
    assert "11" in reason


def test_a_number_the_question_supplied_is_allowed() -> None:
    """The question may legitimately name a figure the answer refers to."""
    reason = validate_fact(
        kind="experience_tenure",
        value="it is 11 years",
        quote="it is 11 years",
        answer="it is 11 years",
        allowed_numbers=numbers_in("your profile records 11 years"),
    )
    assert reason is None


def test_a_technology_the_answer_never_mentioned_is_refused() -> None:
    """This is the net that stops 'I automated reports' becoming 'knows Python'."""
    reason = validate_fact(
        kind="experience_skill_use",
        value="used Python for automation",
        quote="I automated some reports",
        answer="I automated some reports",
        known_terms=frozenset({"python", "docker"}),
    )
    assert reason is not None
    assert "python" in reason


def test_an_empty_fact_is_refused() -> None:
    assert validate_fact(kind="experience_note", value="  ", quote="x", answer="x") is not None


def test_a_fact_with_no_quote_is_refused() -> None:
    reason = validate_fact(kind="experience_note", value="x", quote="", answer="x")
    assert reason is not None
    assert "quote" in reason


def test_an_unknown_kind_is_refused() -> None:
    reason = validate_fact(kind="invented_kind", value="x", quote="x", answer="x")
    assert reason is not None
    assert "not one of the allowed" in reason


def test_deterministic_facts_are_verbatim(engine: InterviewEngine) -> None:
    engine.start()
    outcome = engine.answer(f"{ANSWER_TEXT}\nAnd a second, separate statement.")
    assert outcome.facts
    for fact in outcome.facts:
        assert fact.value == fact.quote, "in this mode the value IS the quote"
        assert fact.quote in f"{ANSWER_TEXT}\nAnd a second, separate statement."
        assert fact.source == "deterministic"


def test_the_deterministic_extractor_strips_list_markers(engine: InterviewEngine) -> None:
    engine.start()
    outcome = engine.answer("- remote only\n- no night shifts\n1. no on-call")
    assert [fact.value for fact in outcome.facts] == ["remote only", "no night shifts", "no on-call"]


def test_a_repeated_sentence_is_one_fact(engine: InterviewEngine) -> None:
    engine.start()
    outcome = engine.answer(f"{ANSWER_TEXT}\n{ANSWER_TEXT}")
    assert len(outcome.facts) == 1


def test_an_empty_answer_produces_nothing(engine: InterviewEngine, kb_file: Path) -> None:
    engine.start()
    before = sha256_of(kb_file)
    outcome = engine.answer("   ")
    assert outcome.facts == ()
    assert engine.session.proposals == {}
    assert sha256_of(kb_file) == before


def test_a_refusal_is_recorded_as_a_refusal_not_an_achievement(
    engine: InterviewEngine,
) -> None:
    """No fact may be manufactured out of 'I don't know'."""
    engine.start()
    outcome = engine.answer("I don't know.")
    assert len(outcome.facts) == 1
    assert outcome.facts[0].value == "I don't know."
    assert outcome.facts[0].kind == engine.current_question().fact_kind


def test_make_claim_id_agrees_with_the_parser() -> None:
    """Two implementations of one id scheme would drift; this is the cross-check."""
    claims = parse_resume(
        "# Test\n\n## CORE SKILLS\n- **Tools:** Python\n", "synthetic.md"
    )
    assert claims
    for claim in claims:
        scope = claim.role_id or claim.parent_id or ""
        assert make_claim_id(claim.kind, scope, claim.value) == claim.claim_id


def test_claim_ids_are_content_derived() -> None:
    first = make_claim_id("experience_note", "s", "same text")
    assert first == make_claim_id("experience_note", "s", "same text")
    assert first != make_claim_id("experience_note", "s", "other text")
    assert first != make_claim_id("experience_note", "other", "same text")
    assert first.startswith("experience_note-")


# --------------------------------------------------------------------------
# The model path: injected, validated, and never reached by accident
# --------------------------------------------------------------------------


def fake_llm(payload: dict[str, object]):
    """A stand-in for the model that returns a fixed JSON payload."""

    def _call(system_prompt: str, user_prompt: str) -> str:
        return json.dumps(payload)

    return _call


def test_an_injected_model_is_used_even_in_off_mode(engine: InterviewEngine) -> None:
    engine.start()
    question = engine.current_question()
    outcome = extract_facts(
        question,
        "I ran the Bitrix24 migration myself.",
        llm=fake_llm(
            {
                "facts": [
                    {
                        "kind": "experience_note",
                        "value": "ran the Bitrix24 migration",
                        "quote": "I ran the Bitrix24 migration",
                    }
                ]
            }
        ),
        mode=LLM_OFF,
    )
    assert outcome.source == "llm"
    assert len(outcome.facts) == 1
    assert outcome.facts[0].value == "ran the Bitrix24 migration"


def test_a_model_fact_with_an_invented_number_is_refused(engine: InterviewEngine) -> None:
    engine.start()
    outcome = extract_facts(
        engine.current_question(),
        "I worked in IT support for a long time.",
        llm=fake_llm(
            {
                "facts": [
                    {
                        "kind": "experience_tenure",
                        "value": "11 years of IT support",
                        "quote": "I worked in IT support",
                    }
                ]
            }
        ),
    )
    assert outcome.facts == ()
    assert len(outcome.rejected) == 1
    assert "number" in outcome.rejected[0].reason


def test_a_model_fact_with_a_fabricated_quote_is_refused(engine: InterviewEngine) -> None:
    engine.start()
    outcome = extract_facts(
        engine.current_question(),
        "I automated some reports.",
        llm=fake_llm(
            {
                "facts": [
                    {
                        "kind": "experience_note",
                        "value": "automated reports",
                        "quote": "I automated all the reporting",
                    }
                ]
            }
        ),
    )
    assert outcome.facts == ()
    assert "verbatim" in outcome.rejected[0].reason


def test_a_model_fact_naming_an_unmentioned_technology_is_refused(
    engine: InterviewEngine,
) -> None:
    engine.start()
    outcome = extract_facts(
        engine.current_question(),
        "I automated some reports.",
        llm=fake_llm(
            {
                "facts": [
                    {
                        "kind": "experience_skill_use",
                        "value": "used Python for automation",
                        "quote": "I automated some reports",
                    }
                ]
            }
        ),
        known_terms=frozenset({"python"}),
    )
    assert outcome.facts == ()
    assert "python" in outcome.rejected[0].reason


def test_a_malformed_model_payload_is_reported(engine: InterviewEngine) -> None:
    engine.start()
    with pytest.raises(ExtractionError, match="JSON"):
        extract_facts(engine.current_question(), "text", llm=lambda s, u: "not json at all")


def test_a_model_payload_without_facts_is_reported(engine: InterviewEngine) -> None:
    engine.start()
    with pytest.raises(ExtractionError, match="facts"):
        extract_facts(engine.current_question(), "text", llm=lambda s, u: '{"other": 1}')


def test_a_model_fact_that_is_not_an_object_is_reported(engine: InterviewEngine) -> None:
    engine.start()
    with pytest.raises(ExtractionError, match="not an object"):
        extract_facts(engine.current_question(), "text", llm=lambda s, u: '{"facts": ["nope"]}')


def test_a_model_fact_with_an_empty_fact_list_yields_nothing(engine: InterviewEngine) -> None:
    engine.start()
    outcome = extract_facts(engine.current_question(), "I would rather not say.", llm=fake_llm({"facts": []}))
    assert outcome.facts == ()
    assert outcome.rejected == ()


def test_the_default_mode_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test, and no stray run, may reach a paid endpoint by accident."""
    monkeypatch.delenv(LLM_MODE_ENV, raising=False)
    monkeypatch.delenv("JOB_ANALYZER_OFFLINE", raising=False)
    assert resolve_llm_mode() == LLM_OFF


def test_job_analyzer_offline_forces_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_ANALYZER_OFFLINE", "1")
    monkeypatch.setenv(LLM_MODE_ENV, LLM_AUTO)
    assert resolve_llm_mode() == LLM_OFF


def test_a_garbage_mode_falls_back_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_MODE_ENV, "yolo")
    assert resolve_llm_mode() == LLM_OFF


def test_an_unknown_explicit_mode_is_refused() -> None:
    with pytest.raises(ExtractionError, match="unknown LLM mode"):
        resolve_llm_mode("sometimes")


def test_required_mode_raises_when_the_model_is_unavailable(
    engine: InterviewEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.start()
    monkeypatch.setenv(LLM_MODE_ENV, LLM_REQUIRED)

    def broken(system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("no route to host")

    import ai_assistant.candidate.interview_extract as extract_module

    monkeypatch.setattr(extract_module, "_default_llm", broken)
    with pytest.raises(ExtractionError, match="required") as raised:
        extract_facts(engine.current_question(), "Some answer.", mode=LLM_REQUIRED)
    # The failure must be the model being unavailable, not a bug in the prompt.
    assert "RuntimeError" in str(raised.value)


def test_auto_mode_falls_back_to_deterministic_with_a_note(
    engine: InterviewEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.start()
    monkeypatch.setenv(LLM_MODE_ENV, LLM_AUTO)

    def broken(system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("no route to host")

    import ai_assistant.candidate.interview_extract as extract_module

    monkeypatch.setattr(extract_module, "_default_llm", broken)
    outcome = extract_facts(engine.current_question(), ANSWER_TEXT, mode=LLM_AUTO)
    assert outcome.source == "deterministic"
    assert outcome.note is not None
    assert "unavailable" in outcome.note
    # Name the cause: a crash inside prompt building would also "fall back", and
    # that is how a broken prompt once looked like an unavailable model.
    assert "RuntimeError" in outcome.note
    assert outcome.facts


def test_off_mode_never_calls_the_model(engine: InterviewEngine) -> None:
    """With no model injected and the mode off, the dull extractor is the only one used."""
    engine.start()
    outcome = extract_facts(engine.current_question(), ANSWER_TEXT, llm=None, mode=LLM_OFF)
    assert outcome.source == "deterministic"
    assert outcome.note is None
    assert outcome.facts


# --------------------------------------------------------------------------
# Session persistence and migration
# --------------------------------------------------------------------------


def test_a_session_survives_a_reload(kb_file: Path, session_path: Path, sources) -> None:
    first = InterviewEngine(
        kb_path=str(kb_file),
        session_path=str(session_path),
        profile_path=str(PROFILE_PATH),
        resume_path=str(RESUME_PATH),
    )
    first.sources = sources
    session = first.start()
    fact_id = first.answer(ANSWER_TEXT).facts[0].claim_id
    first.confirm(fact_id)
    first.skip()

    second = InterviewEngine(
        kb_path=str(kb_file),
        session_path=str(session_path),
        profile_path=str(PROFILE_PATH),
        resume_path=str(RESUME_PATH),
    )
    second.sources = sources
    resumed = second.start()

    assert resumed.session_id == session.session_id
    assert resumed.cursor == first.session.cursor
    assert second.session.proposals[fact_id].status == ProposalStatus.CONFIRMED
    assert resumed.turns


def test_start_is_idempotent(kb_file: Path, session_path: Path, sources) -> None:
    first = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    first.sources = sources
    created = first.start()
    second = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    second.sources = sources
    assert second.start().session_id == created.session_id


def test_reset_starts_a_new_session(kb_file: Path, session_path: Path, sources) -> None:
    first = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    first.sources = sources
    created = first.start()
    second = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    second.sources = sources
    fresh = second.start(reset=True)
    assert fresh.session_id != created.session_id
    assert fresh.turns == {}
    assert fresh.proposals == {}


def test_rebuild_refreshes_the_plan_but_keeps_the_decisions(
    kb_file: Path, session_path: Path, sources
) -> None:
    first = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    first.sources = sources
    first.start()
    fact_id = first.answer(ANSWER_TEXT).facts[0].claim_id
    first.confirm(fact_id)

    second = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    second.sources = sources
    second.start(rebuild=True)
    assert second.session.proposals[fact_id].status == ProposalStatus.CONFIRMED


def test_the_plan_fingerprint_detects_drift(kb_file: Path, session_path: Path, sources) -> None:
    first = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    first.sources = sources
    first.start()
    stored = json.loads(session_path.read_text(encoding="utf-8"))
    stored["plan_fingerprint"] = "stale-fingerprint"
    session_path.write_text(json.dumps(stored), encoding="utf-8")

    second = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    second.sources = sources
    second.start()
    assert second.plan_drift is not None
    assert "changed since this session began" in second.plan_drift


def test_a_session_without_a_version_field_is_read_as_version_one(
    kb_file: Path, session_path: Path, sources
) -> None:
    """The migration path: an older file is read, not refused."""
    first = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    first.sources = sources
    first.start()
    stored = json.loads(session_path.read_text(encoding="utf-8"))
    del stored["session_format_version"]
    session_path.write_text(json.dumps(stored), encoding="utf-8")

    reloaded = load_session(session_path)
    assert reloaded is not None
    assert reloaded.session_id == first.session.session_id


def test_a_future_session_format_is_refused(kb_file: Path, session_path: Path, sources) -> None:
    """Guessing at unknown fields is how a confirmation gets silently dropped."""
    first = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    first.sources = sources
    first.start()
    stored = json.loads(session_path.read_text(encoding="utf-8"))
    stored["session_format_version"] = SESSION_FORMAT_VERSION + 1
    session_path.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(SessionFormatError, match="format version"):
        load_session(session_path)


def test_unknown_session_keys_are_ignored(kb_file: Path, session_path: Path, sources) -> None:
    first = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    first.sources = sources
    first.start()
    stored = json.loads(session_path.read_text(encoding="utf-8"))
    stored["a_field_from_the_future"] = {"whatever": True}
    session_path.write_text(json.dumps(stored), encoding="utf-8")

    reloaded = load_session(session_path)
    assert reloaded is not None
    assert reloaded.session_id == first.session.session_id


def test_a_broken_session_file_is_reported(session_path: Path) -> None:
    session_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SessionFormatError, match="cannot read session file"):
        load_session(session_path)


def test_a_session_that_is_not_an_object_is_reported(session_path: Path) -> None:
    session_path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SessionFormatError, match="JSON object"):
        load_session(session_path)


def test_no_session_file_means_no_session(session_path: Path) -> None:
    assert load_session(session_path) is None


def test_a_session_round_trips(kb_file: Path, session_path: Path, sources) -> None:
    first = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    first.sources = sources
    session = first.start()
    first.answer(ANSWER_TEXT)
    reloaded = load_session(session_path)
    assert reloaded is not None
    assert reloaded.to_dict() == session.to_dict()


def test_saving_is_atomic_and_leaves_no_temp_file(
    kb_file: Path, session_path: Path, sources
) -> None:
    engine = InterviewEngine(kb_path=str(kb_file), session_path=str(session_path))
    engine.sources = sources
    engine.start()
    engine.answer(ANSWER_TEXT)
    leftovers = list(session_path.parent.glob("*.tmp"))
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_the_fingerprint_depends_on_the_plan_and_the_source(plan: tuple[Question, ...]) -> None:
    assert plan_fingerprint(list(plan), "aaa") != plan_fingerprint(list(plan), "bbb")
    assert plan_fingerprint(list(plan), "aaa") == plan_fingerprint(list(plan), "aaa")


def test_resolve_session_path_prefers_explicit_then_env_then_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CANDIDATE_INTERVIEW_STATE", raising=False)
    assert resolve_session_path("explicit.json") == Path("explicit.json")
    assert resolve_session_path() == DEFAULT_SESSION_PATH
    monkeypatch.setenv("CANDIDATE_INTERVIEW_STATE", "from-env.json")
    assert resolve_session_path() == Path("from-env.json")
    assert resolve_session_path("explicit.json") == Path("explicit.json")


# --------------------------------------------------------------------------
# Isolation: only the two declared files are written
# --------------------------------------------------------------------------


def test_a_full_interview_leaves_every_protected_file_untouched(
    engine: InterviewEngine, kb_file: Path
) -> None:
    """The engine must not reach for the repo KB just because it is the default."""
    before = hashes_of(PROTECTED_FILES)
    walk(engine)
    after = hashes_of(PROTECTED_FILES)
    assert after == before, "a protected file changed"


def test_the_default_session_file_is_not_created_by_a_test(
    engine: InterviewEngine,
) -> None:
    existed = DEFAULT_SESSION_PATH.exists()
    walk(engine)
    if not existed:
        assert not DEFAULT_SESSION_PATH.exists(), (
            "the engine wrote to the default session path instead of the one it was given"
        )


def test_the_engine_writes_only_the_paths_it_was_given(
    engine: InterviewEngine, kb_file: Path, session_path: Path
) -> None:
    walk(engine)
    assert session_path.is_file(), "the session must be persisted"
    assert interview_claims(kb_file), "the confirmed fact must be in the given KB"


def test_the_repo_knowledge_base_is_not_written(
    engine: InterviewEngine, kb_file: Path
) -> None:
    before = sha256_of(KB_PATH)
    walk(engine)
    assert sha256_of(KB_PATH) == before


# --------------------------------------------------------------------------
# The command layer
# --------------------------------------------------------------------------


def test_the_command_starts_and_reports_progress(
    kb_file: Path, session_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = {"kb_path": str(kb_file), "session_path": str(session_path)}
    assert candidate_interview_cmd("start", **common) == 0
    assert "question(s) in" in capsys.readouterr().out

    assert candidate_interview_cmd("progress", **common) == 0
    assert "interview progress" in capsys.readouterr().out


def test_progress_before_start_says_so(
    kb_file: Path, session_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert candidate_interview_cmd("progress", kb_path=str(kb_file), session_path=str(session_path)) == 0
    assert "no interview session" in capsys.readouterr().out


def test_the_command_answers_confirms_and_lists_facts(
    kb_file: Path, session_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = {"kb_path": str(kb_file), "session_path": str(session_path)}
    assert candidate_interview_cmd("start", **common) == 0
    capsys.readouterr()

    assert candidate_interview_cmd("answer", ANSWER_TEXT, **common) == 0
    assert "Nothing has been written to the knowledge base" in capsys.readouterr().out

    assert candidate_interview_cmd("facts", as_json=True, **common) == 0
    payload = json.loads(capsys.readouterr().out)
    fact_id = payload["facts"][0]["claim_id"]
    assert payload["facts"][0]["status"] == "PENDING"

    assert candidate_interview_cmd("confirm", fact_id=fact_id, **common) == 0
    assert "CONFIRMED" in capsys.readouterr().out

    assert candidate_interview_cmd("facts", status="CONFIRMED", **common) == 0
    assert fact_id in capsys.readouterr().out


def test_the_command_edits_and_requires_reconfirmation(
    kb_file: Path, session_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = {"kb_path": str(kb_file), "session_path": str(session_path)}
    candidate_interview_cmd("start", **common)
    candidate_interview_cmd("answer", ANSWER_TEXT, **common)
    capsys.readouterr()
    candidate_interview_cmd("facts", as_json=True, **common)
    fact_id = json.loads(capsys.readouterr().out)["facts"][0]["claim_id"]

    assert candidate_interview_cmd("edit", "A corrected statement.", fact_id=fact_id, **common) == 0
    out = capsys.readouterr().out
    assert "needs its own confirmation" in out
    assert "PENDING" in out


def test_the_command_skips_and_moves(
    kb_file: Path, session_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = {"kb_path": str(kb_file), "session_path": str(session_path)}
    candidate_interview_cmd("start", **common)
    capsys.readouterr()
    assert candidate_interview_cmd("skip", **common) == 0
    assert "skipped" in capsys.readouterr().out
    assert candidate_interview_cmd("back", **common) == 0
    assert candidate_interview_cmd("next", **common) == 0
    assert candidate_interview_cmd("clarify", **common) == 0
    assert "why this is being asked" in capsys.readouterr().out


def test_an_unknown_action_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert candidate_interview_cmd("sing") == 2
    assert "unknown action" in capsys.readouterr().err


def test_an_unknown_fact_id_is_reported(
    kb_file: Path, session_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = {"kb_path": str(kb_file), "session_path": str(session_path)}
    candidate_interview_cmd("start", **common)
    capsys.readouterr()
    assert candidate_interview_cmd("confirm", fact_id="nope-0000000000", **common) == 2
    assert "no extracted fact" in capsys.readouterr().err


def test_confirm_without_a_fact_id_is_a_usage_error(
    kb_file: Path, session_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = {"kb_path": str(kb_file), "session_path": str(session_path)}
    candidate_interview_cmd("start", **common)
    capsys.readouterr()
    assert candidate_interview_cmd("confirm", **common) == 2
    assert "needs --fact" in capsys.readouterr().err


def test_answering_without_a_session_is_reported(
    kb_file: Path, session_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = candidate_interview_cmd(
        "answer", ANSWER_TEXT, kb_path=str(kb_file), session_path=str(session_path)
    )
    assert code == 2
    assert "no interview session" in capsys.readouterr().err


def test_a_bad_llm_mode_is_a_usage_error(
    kb_file: Path, session_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = candidate_interview_cmd(
        "start", kb_path=str(kb_file), session_path=str(session_path), llm_mode="whenever"
    )
    assert code == 2
    assert "unknown LLM mode" in capsys.readouterr().err


def test_an_unknown_status_filter_is_refused(
    kb_file: Path, session_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = {"kb_path": str(kb_file), "session_path": str(session_path)}
    candidate_interview_cmd("start", **common)
    capsys.readouterr()
    assert candidate_interview_cmd("facts", status="MAYBE", **common) == 2
    assert "unknown status" in capsys.readouterr().err


def test_a_superseded_fact_cannot_be_decided(engine: InterviewEngine) -> None:
    """Deciding a fact the candidate already revised would act on a stale position."""
    engine.start()
    original = engine.answer(ANSWER_TEXT).facts[0].claim_id
    engine.edit(original, "A corrected statement.")
    with pytest.raises(SessionFormatError, match="superseded by"):
        engine.confirm(original)
    with pytest.raises(SessionFormatError, match="superseded by"):
        engine.reject(original)
    with pytest.raises(SessionFormatError, match="superseded by"):
        engine.edit(original, "A third statement.")


def test_every_action_is_reachable(
    kb_file: Path, session_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = {"kb_path": str(kb_file), "session_path": str(session_path)}
    assert candidate_interview_cmd("start", **common) == 0
    assert candidate_interview_cmd("answer", f"{ANSWER_TEXT}\nA second, separate statement.", **common) == 0
    capsys.readouterr()
    candidate_interview_cmd("facts", as_json=True, **common)
    ids = [item["claim_id"] for item in json.loads(capsys.readouterr().out)["facts"]]
    assert len(ids) == 2

    for action, kwargs in (
        ("clarify", {}),
        ("next", {}),
        ("back", {}),
        ("skip", {}),
        ("progress", {}),
        ("facts", {}),
        ("confirm", {"fact_id": ids[0]}),
        ("reject", {"fact_id": ids[1]}),
        ("edit", {"value": "A different statement.", "fact_id": ids[0]}),
    ):
        assert candidate_interview_cmd(action, **common, **kwargs) == 0, action
    capsys.readouterr()


# --------------------------------------------------------------------------
# The CLI wiring itself (argparse + dispatch, inserted by byte surgery)
# --------------------------------------------------------------------------


def test_the_cli_parses_the_interview_command(
    kb_file: Path, session_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_assistant import cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cli.py",
            "candidate-interview",
            "progress",
            "--kb",
            str(kb_file),
            "--session",
            str(session_path),
        ],
    )
    assert cli.main() == 0
    assert "no interview session" in capsys.readouterr().out


def test_the_cli_accepts_dash_prefixed_text_through_the_text_option(
    kb_file: Path, session_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The positional cannot carry text starting with a dash; --text can."""
    from ai_assistant import cli

    base = ["cli.py", "candidate-interview"]
    common = ["--kb", str(kb_file), "--session", str(session_path)]
    monkeypatch.setattr(sys, "argv", [*base, "start", *common])
    assert cli.main() == 0
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", [*base, "answer", "--text", "- remote only", *common])
    assert cli.main() == 0
    assert "remote only" in capsys.readouterr().out


def test_the_cli_dispatches_the_interview_command(
    kb_file: Path, session_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dispatch branch reaches the real handler, not the help fallback."""
    from ai_assistant import cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cli.py",
            "candidate-interview",
            "start",
            "--kb",
            str(kb_file),
            "--session",
            str(session_path),
        ],
    )
    assert cli.main() == 0
    assert "Professional goals and constraints" in capsys.readouterr().out
