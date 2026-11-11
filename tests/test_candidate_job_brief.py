"""Tests for the Job-to-Candidate Brief (Stage 0 / T6).

T6 answers a question a wrong answer makes *expensive*: "does this vacancy fit
me, and what may I honestly claim about it?" Every pin here exists because the
opposite behaviour would put a false statement into a real application.

The five properties that matter, and the defect each one guards against:

* **A real description is split into readable requirements.** Every stored
  vacancy has zero newlines (measured), so a line-based splitter sees one giant
  line; an earlier extractor responded by inventing requirements from the first
  three sentences. The quoted-question case is pinned separately, because five
  example questions separated only by ``?”`` merged into one 400-character blob.
* **Only a duty is commercial experience.** The KB attaches ``role_id`` to the
  claims a role produced, and to nothing else. A first draft read the wrong
  attribute, so class A was unreachable and every answer was "no evidence".
* **An own project is never paid work.** A project claim may support a
  requirement -- as class B, labelled as such -- and may never be promoted.
* **A shared word is not a shared skill.** The first vocabulary registered every
  token of every claim, which turned "with", "web" and "sources" into
  technologies; a requirement mentioning *with* then matched a project bullet
  containing *with*, and the brief reported the candidate's own project as
  covering phone support. A single work-area word is likewise not enough, which
  is what stops "Industry recognized security certification" from being answered
  by security *work*.
* **Nothing in production moves.** ``state.db``, the profile, the KB and the
  application rules are read-only to T6; the pins hash them.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ai_assistant.candidate import job_brief as jb
from ai_assistant.candidate.job_brief import (
    EvidenceClass,
    JobBriefError,
    RequirementKind,
    VacancyInput,
    build_brief,
    classify_kind,
    render_brief_details,
    render_brief_json,
    render_brief_short,
    segment,
    t5_2_pending_note,
)
from ai_assistant.candidate.kb import (
    INTERVIEW_SOURCE,
    PROJECT_SOURCE,
    CandidateKnowledgeBase,
    ClaimRecord,
    ClaimStatus,
)
from ai_assistant.candidate.master_resume import load_kb
from ai_assistant.candidate.resume_parser import make_claim_id

REPO_ROOT = Path(__file__).resolve().parent.parent

DERIVED = ClaimStatus.DERIVED_FROM_SOURCE
CONFIRMED = ClaimStatus.CONFIRMED_BY_USER


# ----------------------------------------------------------------------
# A small KB built for these tests
# ----------------------------------------------------------------------


def build_kb(
    *,
    skills: tuple[str, ...] = (),
    duties: tuple[tuple[str, str], ...] = (),
    projects: tuple[tuple[str, tuple[str, ...], str], ...] = (),
    interview: tuple[tuple[str, str], ...] = (),
    spans: tuple[tuple[str, str, str], ...] = (),
    kb_version: int = 1,
) -> CandidateKnowledgeBase:
    """A KB with exactly the facts a test needs and nothing else.

    ``duties`` are ``(role_title, duty_text)``; each gets its own role, because
    the role link is what makes a duty commercial experience. ``projects`` are
    ``(name, bullets, stack)`` and are marked as project-sourced. ``interview``
    entries are ``(kind, value)`` and carry the interview marker. ``spans`` are
    ``(title, start, end)`` and exist so tenure can be computed.
    """
    kb = CandidateKnowledgeBase(kb_version=kb_version)

    def add(record: ClaimRecord) -> None:
        kb.claims[record.claim_id] = record

    for title, start, end in spans:
        role_id = make_claim_id("role", "", f"{title}|{start}|{end}")
        add(
            ClaimRecord(
                claim_id=role_id,
                kind="role",
                value=title,
                evidence={"source_file": "resume.md", "line": 1, "quote": title},
            )
        )
        for kind, value in (("role_title", title), ("role_start", start), ("role_end", end)):
            add(
                ClaimRecord(
                    claim_id=make_claim_id(kind, role_id, value),
                    kind=kind,
                    value=value,
                    role_id=role_id,
                    evidence={"source_file": "resume.md", "line": 1, "quote": value},
                )
            )

    for value in skills:
        add(
            ClaimRecord(
                claim_id=make_claim_id("skill", "", value),
                kind="skill",
                value=value,
                evidence={"source_file": "resume.md", "line": 1, "quote": value},
            )
        )

    for title, text in duties:
        role_id = make_claim_id("role", "", f"{title}|{text}")
        add(
            ClaimRecord(
                claim_id=role_id,
                kind="role",
                value=title,
                evidence={"source_file": "resume.md", "line": 2, "quote": title},
            )
        )
        add(
            ClaimRecord(
                claim_id=make_claim_id("role_title", role_id, title),
                kind="role_title",
                value=title,
                role_id=role_id,
                evidence={"source_file": "resume.md", "line": 2, "quote": title},
            )
        )
        add(
            ClaimRecord(
                claim_id=make_claim_id("duty", role_id, text),
                kind="duty",
                value=text,
                role_id=role_id,
                evidence={"source_file": "resume.md", "line": 3, "quote": text},
            )
        )

    for name, bullets, stack in projects:
        project_id = make_claim_id("project", "", name)
        add(
            ClaimRecord(
                claim_id=project_id,
                kind="project",
                value=name,
                evidence={
                    "source_file": PROJECT_SOURCE,
                    "project_id": project_id,
                    "line": 1,
                    "quote": name,
                },
            )
        )
        add(
            ClaimRecord(
                claim_id=make_claim_id("project_stack", project_id, stack),
                kind="project_stack",
                value=stack,
                evidence={
                    "source_file": PROJECT_SOURCE,
                    "project_id": project_id,
                    "line": 2,
                    "quote": stack,
                },
            )
        )
        for bullet in bullets:
            add(
                ClaimRecord(
                    claim_id=make_claim_id("project_bullet", project_id, bullet),
                    kind="project_bullet",
                    value=bullet,
                    evidence={
                        "source_file": PROJECT_SOURCE,
                        "project_id": project_id,
                        "line": 3,
                        "quote": bullet,
                    },
                )
            )

    for kind, value in interview:
        add(
            ClaimRecord(
                claim_id=make_claim_id(kind, "", value),
                kind=kind,
                value=value,
                status=CONFIRMED,
                evidence={
                    "source_file": INTERVIEW_SOURCE,
                    "question_id": "q1",
                    "quote": value,
                },
            )
        )

    return kb


def vacancy(description: str, title: str = "Test Vacancy") -> VacancyInput:
    return VacancyInput(
        title=title,
        description=description,
        company="Test Co",
        location="Remote",
        source="test",
        stable_id="test:1",
    )


def classes_for(brief: jb.Brief) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for assessment in brief.assessments:
        out.setdefault(str(assessment.evidence_class), []).append(
            assessment.requirement.text
        )
    return out


def find(brief: jb.Brief, needle: str) -> jb.Assessment:
    """The assessment whose requirement text contains ``needle``."""
    for assessment in brief.assessments:
        if needle.lower() in assessment.requirement.text.lower():
            return assessment
    raise AssertionError(
        f"no requirement containing {needle!r}; have "
        f"{[a.requirement.text[:60] for a in brief.assessments]}"
    )


# ----------------------------------------------------------------------
# Section 2 -- reading a real vacancy
# ----------------------------------------------------------------------


def test_a_zero_newline_description_still_segments() -> None:
    """The measured shape of every stored vacancy: 5293 chars, no newlines."""
    text = (
        "We are looking for a support engineer. "
        "You must have experience with Linux. "
        "Nice to have: Docker experience. "
        "Answering tickets is the primary duty."
    )
    assert "\n" not in text
    pieces = segment(text)
    assert len(pieces) >= 4, pieces


def test_quoted_questions_do_not_merge_into_one_blob() -> None:
    """A real Porkbun posting separates five example questions with ``?”``."""
    text = (
        "Typical issues: \u201cHow do I connect my domain?\u201d "
        "\u201cHow do I transfer my domain?\u201d \u201cMy website isn\u2019t working.\u201d"
    )
    pieces = segment(text)
    assert len(pieces) >= 3, pieces
    assert all(len(piece) < 120 for piece in pieces), [len(p) for p in pieces]


def test_benefits_and_hiring_process_are_not_requirements() -> None:
    """Words like *required* appear in benefit clauses; the marker alone lies."""
    text = (
        "A simple IRA plan with a matching 3% company contribution is available. "
        "We run a three-round interview process. "
        "You must have experience with Python and PostgreSQL."
    )
    facts = jb.extract_facts(vacancy(text), build_kb(skills=("Python", "PostgreSQL")))
    requirement_text = " ".join(
        r.text for r in facts.must_have + facts.nice_to_have
    ).lower()
    assert "ira" not in requirement_text
    assert "interview process" not in requirement_text
    assert any("python" in r.text.lower() for r in facts.must_have)


def test_mandatory_is_separated_from_nice_to_have() -> None:
    text = (
        "Requirements: You must have experience with Linux. "
        "Nice to have: Docker experience."
    )
    facts = jb.extract_facts(vacancy(text), build_kb(skills=("Linux", "Docker")))
    must = " ".join(r.text.lower() for r in facts.must_have)
    nice = " ".join(r.text.lower() for r in facts.nice_to_have)
    assert "linux" in must
    assert "linux" not in nice
    assert "docker" in nice


def test_absent_information_is_marked_unstated_not_invented() -> None:
    facts = jb.extract_facts(
        VacancyInput(title="Dev", description="You must know Linux.", source="t"),
        build_kb(skills=("Linux",)),
    )
    assert facts.salary_text == "не указано"
    assert "зарплата" in facts.unstated
    assert "язык работы" in facts.unstated


def test_salary_without_a_currency_is_not_compared_to_the_minimum() -> None:
    """A habr row stores ``salary_min == salary_max == 24`` and no currency."""
    brief = build_brief(
        VacancyInput(
            title="Dev",
            description="You must know Linux.",
            salary_min=24.0,
            salary_max=24.0,
            salary_currency=None,
        ),
        build_kb(
            skills=("Linux",),
            interview=(("preference_condition", "Minimum income 1000 USD per month."),),
        ),
        profile={"minimum_salary": 1000},
    )
    assert "валюта" in brief.facts.salary_text
    jb._salary_verdict(brief)
    assert brief.facts.salary_meets_minimum is None
    # And the rendered line must not invite the reader to compare "24" against
    # the candidate's floor when no comparison was made.
    assert "сравнить нельзя" in render_brief_short(brief)


# ----------------------------------------------------------------------
# Section 2b -- a tag list is a list of requirements
# ----------------------------------------------------------------------


def test_a_tag_list_becomes_one_requirement_per_tag() -> None:
    """The habr stub carries its whole requirement set as one ``#tag`` line.

    Measured: ``Требуемые навыки: #middle, #Битрикс24, #JavaScript, #PHP,
    #MySQL, #CI/CD, #GitLab, #RabbitMQ, #Jira, #Docker`` is 272 characters and
    contains no newline, so it arrived as a single segment. Kept whole, one
    match answered for all ten tags.
    """
    brief = build_brief(
        vacancy(
            "Требуемые навыки: #middle, #Битрикс24, #JavaScript, #PHP, #MySQL, "
            "#CI/CD, #GitLab, #RabbitMQ, #Jira, #Docker."
        ),
        build_kb(
            duties=(("AI Automation Engineer", "Administered Linux and Docker infrastructure"),),
        ),
    )
    texts = [a.requirement.text for a in brief.assessments]
    assert "#Docker" in texts
    assert "#PHP" in texts
    # The grade tag is not a skill and is already reported as seniority.
    assert not any("middle" in t for t in texts)


def test_one_matched_tag_does_not_vouch_for_the_others() -> None:
    """This is the whole point of the split, stated as a test.

    Docker is genuinely confirmed by a duty. PHP sits in the same tag list and
    is not claimed anywhere -- before the split the pair shared one verdict and
    the brief asserted PHP as commercial experience too.
    """
    brief = build_brief(
        vacancy("Требуемые навыки: #PHP, #Docker, #RabbitMQ."),
        build_kb(
            # The vocabulary is built from term-like claims, so ``Docker`` has
            # to be claimed as a skill for the tag to be recognised at all; the
            # duty is what then makes it commercial.
            skills=("Docker",),
            duties=(("AI Automation Engineer", "Administered Linux and Docker infrastructure"),),
        ),
    )
    assert find(brief, "#Docker").evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE
    assert find(brief, "#PHP").evidence_class is EvidenceClass.E_UNCONFIRMED
    assert find(brief, "#RabbitMQ").evidence_class is EvidenceClass.E_UNCONFIRMED
    # The unconfirmed tags keep the "absence is not proof" caveat, and since T7
    # they also say *which* name the facts cannot answer -- a tag is already a
    # name the KB does not know, and the brief now has somewhere to put it.
    php = find(brief, "#PHP")
    assert php.explanation.startswith(jb.NO_EVIDENCE_CAVEAT)
    assert "не подтверждено доступными фактами" in php.explanation.lower()
    assert "«PHP»" in php.explanation
    # Docker's own verdict is untouched: it is answered by a duty and carries no
    # unconfirmed name.
    assert find(brief, "#Docker").requirement.unrecognised == ()
    assert find(brief, "#Docker").explanation == (
        "Указано в обязанностях роли: AI Automation Engineer."
    )


def test_a_tag_list_inherits_the_mandatory_flag_from_its_label() -> None:
    """The tags are bare, so the label's must/nice marker has to be inherited.

    "Требуемые навыки:" makes every tag in the list mandatory even though no
    single tag repeats the word -- drop the inheritance and a required list
    silently becomes a list of wishes.
    """
    required = build_brief(
        vacancy("Требуемые навыки: #Docker, #PHP, #MySQL."),
        build_kb(duties=(("Sysadmin", "Administered Docker infrastructure"),)),
    )
    for tag in ("#Docker", "#PHP", "#MySQL"):
        assessment = find(required, tag)
        assert assessment.requirement.text == tag
        assert assessment.requirement.mandatory is True

    wished = build_brief(
        vacancy("Желательно: #Docker, #PHP, #MySQL."),
        build_kb(duties=(("Sysadmin", "Administered Docker infrastructure"),)),
    )
    for tag in ("#Docker", "#PHP", "#MySQL"):
        assert find(wished, tag).requirement.mandatory is False


def test_a_nice_to_have_tag_list_stays_nice_to_have() -> None:
    brief = build_brief(
        vacancy("Желательно: #Kubernetes, #Terraform, #Ansible."),
        build_kb(skills=("Linux",)),
    )
    for tag in ("#Kubernetes", "#Terraform", "#Ansible"):
        assert find(brief, tag).requirement.mandatory is False


def test_prose_with_a_couple_of_hashtags_is_not_split() -> None:
    """Two tags is a sentence, not a list; splitting it would lose the sense."""
    piece = "Мы ищем разработчика с опытом #PHP и #MySQL для продукта"
    assert jb._expand_tag_list(piece) == [piece]


def test_paid_evidence_leads_in_a_commercial_section() -> None:
    """The short view prints the first two evidence lines, so order is meaning.

    Measured on ``hh:136706048``: the PostgreSQL requirement opened with two
    Reels Analyzer bullets under the heading "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ",
    because a ``project_stack`` claim names more technologies than any duty and
    so won the relevance sort.
    """
    brief = build_brief(
        vacancy("Commercial experience with PostgreSQL is required."),
        build_kb(
            duties=(("AI Automation Engineer", "Built PostgreSQL reporting pipelines"),),
            projects=(("Reels Analyzer", ("Processes video queues",), "PostgreSQL, Redis"),),
            spans=(("AI Automation Engineer", "2022-01", "2024-01"),),
        ),
    )
    assessment = find(brief, "PostgreSQL")
    assert assessment.evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE
    assert assessment.evidence[0].is_commercial, (
        "the first line under a commercial heading must be paid work"
    )
    assert assessment.evidence[0].where == "AI Automation Engineer"


def test_a_project_section_still_leads_with_the_project() -> None:
    """Ranking paid evidence first must not bury a project under class B."""
    brief = build_brief(
        vacancy("Experience with Redis is required."),
        build_kb(
            skills=("Redis",),
            projects=(("Reels Analyzer", ("Queues jobs in Redis",), "Redis, ARQ"),),
        ),
    )
    assessment = find(brief, "Redis")
    assert assessment.evidence_class is EvidenceClass.B_OWN_PROJECT
    assert assessment.evidence[0].origin == "project"


def test_a_list_of_only_grade_tags_is_left_alone() -> None:
    piece = "Требуемые навыки: #junior, #middle, #senior."
    assert jb._expand_tag_list(piece) == [piece]


def test_a_trailing_full_stop_is_not_part_of_the_tag() -> None:
    """``#Docker.`` must yield ``docker``, or it matches no KB technology.

    The tag character class allows a dot inside a name (``asp.net``), so a
    sentence-final full stop is captured with the tag and has to be trimmed --
    otherwise a confirmed skill reads as unconfirmed, which is the one error a
    brief must never make.
    """
    expanded = jb._expand_tag_list("Требуемые навыки: #Docker, #PHP, #MySQL.")
    assert expanded == ["#Docker", "#PHP", "#MySQL"]


def test_a_dotted_technology_name_keeps_its_dots() -> None:
    """Trimming is on the trailing full stop only, never inside the name."""
    expanded = jb._expand_tag_list("Стек: #asp.net, #node.js, #ci/cd.")
    assert expanded == ["#asp.net", "#node.js", "#ci/cd"]


def test_the_question_about_a_tag_names_only_the_tag() -> None:
    """The label must not be pasted into every question and highlight.

    Measured before the fix: the phone view read "Есть ли опыт с «Требуемые
    навыки: #JavaScript»?", and the highlights read "Опыт работы — Требуемые
    навыки: #Битрикс24".
    """
    brief = build_brief(
        vacancy("Требуемые навыки: #PHP, #MySQL, #RabbitMQ."),
        build_kb(skills=("Linux",)),
    )
    assert any("«#PHP»" in q for q in brief.questions)
    assert not any("Требуемые навыки" in q for q in brief.questions)
    assert not any("Требуемые навыки" in h for h in brief.highlights)


def test_must_markers_win_over_nice_markers() -> None:
    """A line carrying both is a requirement, matching the analyzer's order."""
    assert jb._marker_of("Required, plus nice to have") is True
    assert jb._marker_of("Требования: обязательное и желательное") is True
    assert jb._marker_of("Желательно, будет плюсом") is False
    assert jb._marker_of("Стек технологий") is None


def test_the_real_bitrix24_brief_does_not_claim_php_from_docker() -> None:
    """The regression that found the bug, pinned against the real database."""
    brief = build_brief(real_vacancy("habrcareer:1000168093"), load_kb(None)[0])
    assert find(brief, "#Битрикс24").evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE
    assert find(brief, "#Docker").evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE
    for unclaimed in ("#PHP", "#JavaScript", "#MySQL", "#RabbitMQ"):
        assert find(brief, unclaimed).evidence_class is EvidenceClass.E_UNCONFIRMED
    # Ten tags, one of them a grade: nine requirements, no blob.
    assert len(brief.assessments) == 9


# ----------------------------------------------------------------------
# Section 3 -- the honesty rules
# ----------------------------------------------------------------------


def test_a_duty_is_class_a_commercial_experience() -> None:
    """A duty tied to an employer is class A -- when nothing else is asked.

    T7.2 changed what happens when the *same* requirement also names a number
    of years: the class drops to C, because the KB answers "was this work done"
    and cannot answer "was it done for that long". The threshold was incidental
    to this test, so it is gone from the vacancy text and the class-A path stays
    under test; the threshold shape has its own test directly below.
    """
    kb = build_kb(
        duties=(("Support Engineer | Acme", "Provided technical support and troubleshooting for users."),),
    )
    brief = build_brief(vacancy("You must have technical support experience."), kb)
    assessment = find(brief, "technical support")
    assert assessment.evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE
    assert assessment.evidence[0].claim_kind == "duty"
    assert assessment.evidence[0].role_linked is True
    assert assessment.evidence[0].is_commercial is True


def test_a_year_threshold_splits_presence_from_duration() -> None:
    """T7.2 section 3: two independent checks, reported apart.

    "two years of technical support experience" asks whether the work was done
    *and* whether it lasted two years. The duty answers the first. Nothing in
    the KB measures years per requirement, so the second stays open and the
    class may not remain A -- A reports the whole ask as closed while half of it
    is open. The confirmed half must still name the fact it rests on, and no
    other number may be substituted for the missing duration.
    """
    kb = build_kb(
        duties=(("Support Engineer | Acme", "Provided technical support and troubleshooting for users."),),
    )
    brief = build_brief(
        vacancy("You must have two years of technical support experience."), kb
    )
    assessment = find(brief, "technical support")
    assert assessment.requirement.mandatory
    assert assessment.evidence_class is EvidenceClass.C_PARTIAL
    # The confirmed half is stated, and it names the duty it rests on.
    assert "Подтверждено" in assessment.explanation
    assert "Support Engineer | Acme" in assessment.explanation
    # The open half is stated too, and names the duration rather than a stand-in.
    assert "Не подтверждено" in assessment.explanation
    assert "называет срок 2+ лет" in assessment.explanation
    assert "общий IT-стаж сюда не подставляется" in assessment.explanation
    assert jb.APPROVED_TOTAL_EXPERIENCE not in assessment.explanation

    # The phone view carries the same distinction: the reader acts on this
    # screen, so the reason for the demotion may not be left to the detail view.
    short = render_brief_short(brief)
    assert "срок 2+ лет по этому требованию в KB не измеряется" in short


def test_an_own_project_is_class_b_and_never_paid_work() -> None:
    kb = build_kb(
        projects=(("Reels Analyzer", ("Analyses short videos.",), "Python, PostgreSQL"),),
    )
    brief = build_brief(vacancy("You must have experience with PostgreSQL."), kb)
    assessment = find(brief, "PostgreSQL")
    assert assessment.evidence_class is EvidenceClass.B_OWN_PROJECT
    assert all(e.origin == "project" for e in assessment.evidence)
    assert all(e.is_commercial is False for e in assessment.evidence)


def test_a_project_claim_is_never_promoted_to_class_a() -> None:
    """The KB ties a role only to a duty; a project has no role to tie to."""
    kb = build_kb(
        projects=(("Only Project", ("Built with Python.",), "Python"),),
    )
    brief = build_brief(vacancy("Commercial experience with Python is required."), kb)
    assessment = find(brief, "Commercial experience")
    assert assessment.evidence_class is not EvidenceClass.A_CONFIRMED_EXPERIENCE
    assert assessment.evidence_class is EvidenceClass.C_PARTIAL
    assert "не является оплачиваемой работой" in assessment.explanation


def test_a_skill_without_a_role_is_class_d_not_class_a() -> None:
    kb = build_kb(skills=("PostgreSQL",))
    brief = build_brief(vacancy("You must have experience with PostgreSQL."), kb)
    assessment = find(brief, "PostgreSQL")
    assert assessment.evidence_class is EvidenceClass.D_NEEDS_CLARIFICATION
    assert assessment.evidence[0].role_linked is False
    assert assessment.evidence[0].is_commercial is False


def test_an_unclaimed_technology_is_class_e_with_the_caveat() -> None:
    kb = build_kb(skills=("Python",))
    brief = build_brief(vacancy("You must have experience with Kubernetes."), kb)
    assessment = find(brief, "Kubernetes")
    assert assessment.evidence_class is EvidenceClass.E_UNCONFIRMED
    assert assessment.explanation == jb.NO_EVIDENCE_CAVEAT
    assert jb.NO_EVIDENCE_CAVEAT in render_brief_short(brief)


def test_absence_of_evidence_is_not_printed_as_absence_of_skill() -> None:
    brief = build_brief(
        vacancy("You must have experience with Kubernetes."), build_kb(skills=("Python",))
    )
    text = render_brief_short(brief)
    assert "не доказательство отсутствия навыка" in text


def test_a_certification_is_not_confirmed_by_work_in_the_same_area() -> None:
    """Security work does not make a security certification."""
    kb = build_kb(
        duties=(
            ("Sysadmin | Acme", "Administered Linux servers and reviewed security settings."),
        ),
        skills=("Linux",),
    )
    brief = build_brief(
        vacancy("Industry recognized security certification, IAT Level II/III (preferred)."),
        kb,
    )
    assessment = find(brief, "certification")
    assert assessment.evidence_class is EvidenceClass.E_UNCONFIRMED


def test_one_shared_work_area_word_is_not_evidence() -> None:
    """Two work-area words are a subject; one is a topic."""
    kb = build_kb(
        duties=(("Support | Acme", "Provided technical support and troubleshooting for users."),),
    )
    brief = build_brief(vacancy("You must have strong communication skills."), kb)
    assessment = find(brief, "communication")
    assert assessment.evidence_class is EvidenceClass.E_UNCONFIRMED


def test_an_unrelated_project_bullet_is_not_dragged_in_as_evidence() -> None:
    """The measured defect: a shared common word matched a project bullet."""
    kb = build_kb(
        projects=(
            (
                "Reels Analyzer",
                ("Fact-checks claims against web sources (Exa) with QA controls.",),
                "Python",
            ),
        ),
    )
    brief = build_brief(
        vacancy("You must provide amazing live support via phone and chat with customers."),
        kb,
    )
    assessment = find(brief, "live support")
    assert assessment.evidence == []
    assert assessment.evidence_class is EvidenceClass.E_UNCONFIRMED


# ----------------------------------------------------------------------
# The vocabularies themselves
# ----------------------------------------------------------------------


def test_prose_claims_do_not_become_technologies() -> None:
    """A headline is a sentence, not a technology."""
    kb = build_kb(
        skills=("Python",),
        duties=(("Engineer | Acme", "Built things with Python."),),
    )
    kb.claims["headline-x"] = ClaimRecord(
        claim_id="headline-x",
        kind="headline",
        value="AI Engineer | AI Automation Engineer | Python Developer",
        evidence={"source_file": "resume.md", "line": 1, "quote": "x"},
    )
    vocabulary = jb._kb_vocabulary(kb)
    assert vocabulary.get("python") == "python"
    assert not any("engineer" in canonical for canonical in vocabulary.values())


def test_the_vocabulary_only_knows_what_the_kb_claims() -> None:
    kb = build_kb(skills=("Python",))
    vocabulary = jb._kb_vocabulary(kb)
    assert "python" in vocabulary
    assert "kubernetes" not in vocabulary
    assert "terraform" not in vocabulary


def test_an_ambiguous_token_is_not_mapped_to_one_technology() -> None:
    """``api`` belongs to three skills; picking one would be invention."""
    kb = build_kb(skills=("OpenAI API", "Telegram Bot API", "Google Gemini API"))
    vocabulary = jb._kb_vocabulary(kb)
    assert "api" not in vocabulary
    assert vocabulary.get("openai") == "openai api"
    assert vocabulary.get("gemini") == "google gemini api"


def test_stopwords_do_not_become_technologies() -> None:
    kb = build_kb(skills=("Python",))
    kb.claims["b-x"] = ClaimRecord(
        claim_id="b-x",
        kind="project_bullet",
        value="Fact-checks claims against web sources with QA controls.",
        evidence={"source_file": PROJECT_SOURCE, "project_id": "p", "line": 1, "quote": "x"},
    )
    vocabulary = jb._kb_vocabulary(kb)
    for word in ("with", "web", "sources", "against", "controls"):
        assert word not in vocabulary


def test_domain_terms_come_only_from_duties() -> None:
    """A project must not be able to widen the work-area vocabulary."""
    duties_only = build_kb(
        duties=(("Support | Acme", "Provided technical support and troubleshooting."),),
    )
    project_only = build_kb(
        projects=(("P", ("Provided technical support and troubleshooting.",), "Python"),),
    )
    assert "troubleshooting" in jb._kb_domains(duties_only)
    assert "troubleshooting" not in jb._kb_domains(project_only)


# ----------------------------------------------------------------------
# Section 4 -- confirmed preferences, and the filters that contradict them
# ----------------------------------------------------------------------


def test_years_experience_is_not_read_as_total_it_tenure() -> None:
    """``years_experience: 3`` is not the career length -- and not a specialism either.

    T6.1 established that the meaning of the field is *not* known: the earlier
    wording asserted it described the AI specialism, which was a guess dressed
    up as a fact. What the brief may say is narrower -- the field is not used as
    total tenure and not used as tenure in a specialisation.
    """
    kb = build_kb(
        spans=(
            ("Earlier IT Infrastructure & Support", "2009", "2018"),
            ("Bitrix24 Technical Support Specialist", "April 2021", "August 2022"),
        ),
        interview=(("chronology_note", "Speedcast/Germsat: March 2018 - March 2021"),),
    )
    tenure = jb.compute_tenure(kb, profile={"years_experience": 3})
    assert tenure.profile_years == 3
    assert tenure.total_it_years > 3
    assert tenure.profile_is_total is False
    assert tenure.note
    assert "не установлено" in tenure.note
    assert "ни как общий стаж" in tenure.note
    assert "ни как стаж в специализации" in tenure.note
    # The field must not be resolved into a specialism tenure by other wording
    # either: "3 года стажа в AI" is the guess this test exists to block.
    assert "3 года стажа" not in tenure.note


def test_the_rendered_brief_labels_the_profile_field_as_not_the_total() -> None:
    kb = build_kb(
        spans=(("Earlier IT Infrastructure & Support", "2009", "2018"),),
    )
    brief = build_brief(
        vacancy("You must know Linux."), kb, profile={"years_experience": 3}
    )
    text = render_brief_short(brief)
    assert "не установлено" in text
    assert "years_experience=3" in text
    # Whatever the field means, it is never published as the confirmed figure.
    assert "15.3" not in text
    assert "14.9" not in text


def test_profile_conflicts_are_reported_and_not_applied() -> None:
    profile = {
        "minimum_salary": 1500,
        "excluded_roles": ["bitrix"],
        "must_avoid_conditions": ["bitrix"],
        "years_experience": 3,
    }
    kb = build_kb(
        interview=(
            ("preference_condition", "Minimum income 1000 USD per month."),
            (
                "preference_role",
                "Bitrix24: рассматривать наравне с другими вакансиями, не исключать.",
            ),
        )
    )
    brief = build_brief(vacancy("You must know Linux."), kb, profile=profile)
    wheres = {c.where for c in brief.conflicts}
    assert any("minimum_salary" in w for w in wheres)
    assert any("excluded_roles" in w for w in wheres)
    # And the filter itself is untouched: T6 reports, it does not rewrite.
    assert profile["excluded_roles"] == ["bitrix"]
    assert profile["minimum_salary"] == 1500


def test_bitrix24_is_not_excluded_from_the_brief() -> None:
    kb = build_kb(
        duties=(("Bitrix24 Technical Support Specialist | Avantis", "Administered self-hosted Bitrix24."),),
        interview=(("preference_role", "Bitrix24: consider on par with other vacancies."),),
    )
    brief = build_brief(vacancy("You must have experience with Bitrix24."), kb)
    assessment = find(brief, "Bitrix24")
    assert assessment.evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE


def test_a_russian_alias_reaches_an_english_duty() -> None:
    """The alias is the point: ``#Битрикс24`` must find "self-hosted Bitrix24".

    The class moved from A to C in T7, and not because the alias stopped
    working. The line asks for two things and only one of them is answerable:
    #PHP is not claimed anywhere in the KB, so the brief no longer reports the
    pair as confirmed experience. The alias itself is still what produces the
    evidence -- and the evidence is still there to be read.
    """
    kb = build_kb(
        duties=(("Bitrix24 Support | Avantis", "Administered self-hosted Bitrix24."),),
    )
    brief = build_brief(vacancy("Требуемые навыки: #Битрикс24, #PHP"), kb)
    assessment = find(brief, "Битрикс24")
    assert assessment.commercial_evidence, "the alias must still reach the duty"
    assert assessment.requirement.unrecognised == ("PHP",)
    assert assessment.evidence_class is EvidenceClass.C_PARTIAL
    # And on its own, with nothing unanswerable beside it, the same alias is A.
    alone = build_brief(vacancy("Требуемые навыки: #Битрикс24"), kb)
    assert find(alone, "Битрикс24").evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE


# ----------------------------------------------------------------------
# Section 5 -- sources and statuses survive
# ----------------------------------------------------------------------


def test_every_evidence_keeps_its_claim_id_status_and_reference() -> None:
    kb = build_kb(
        duties=(("Support | Acme", "Provided technical support and troubleshooting."),),
    )
    brief = build_brief(vacancy("You must have technical support experience."), kb)
    assessment = find(brief, "technical support")
    assert assessment.evidence
    for item in assessment.evidence:
        assert item.claim_id
        assert item.status in {str(DERIVED), str(CONFIRMED)}
        assert item.ref and ":" in item.ref
        assert item.where


def test_the_detailed_view_carries_the_claim_identifiers() -> None:
    kb = build_kb(
        duties=(("Support | Acme", "Provided technical support and troubleshooting."),),
    )
    brief = build_brief(vacancy("You must have technical support experience."), kb)
    details = render_brief_details(brief)
    for item in find(brief, "technical support").evidence:
        assert item.claim_id in details
        assert item.ref in details


def test_the_json_view_is_valid_and_carries_the_classes() -> None:
    kb = build_kb(skills=("Python",))
    brief = build_brief(vacancy("You must have experience with Python."), kb)
    payload = json.loads(render_brief_json(brief))
    assert payload["assessments"]
    assert payload["assessments"][0]["class"] in {"A", "B", "C", "D", "E"}
    assert "claim_id" in payload["assessments"][0]["evidence"][0]


def test_a_project_status_note_never_becomes_evidence() -> None:
    """``project_status`` is a candid note to the author, not a selling point."""
    kb = build_kb(skills=("Python",))
    kb.claims["ps-x"] = ClaimRecord(
        claim_id="ps-x",
        kind="project_status",
        value="STAGE_1_FINAL_NO_GO for Python work",
        evidence={"source_file": PROJECT_SOURCE, "project_id": "p", "line": 1, "quote": "x"},
    )
    brief = build_brief(vacancy("You must have experience with Python."), kb)
    for assessment in brief.assessments:
        assert all(e.claim_kind != "project_status" for e in assessment.evidence)


def test_a_preference_claim_is_not_evidence_of_experience() -> None:
    """The measured defect: a preference was cited as proof of a skill."""
    kb = build_kb(
        interview=(
            ("preference_role", "Bitrix24: рассматривать наравне с другими вакансиями"),
        )
    )
    brief = build_brief(vacancy("You must have experience with Bitrix24."), kb)
    assessment = find(brief, "Bitrix24")
    assert assessment.evidence == []
    assert assessment.evidence_class is EvidenceClass.E_UNCONFIRMED


def test_the_t5_2_stage_is_recorded_as_finished() -> None:
    """T6.2 closed T5.2, and the note has to say so by itself.

    The note counts the markers in the two drafts at call time, so it flipped
    from "НЕ применён" to "применён" the moment the generator was changed -- no
    one edited the string. That is the property being pinned: a note that has to
    be updated by hand is a note that will one day be wrong.
    """
    note = t5_2_pending_note()
    assert "T5.2" in note
    assert "НЕ применён" not in note
    assert "применён" in note
    assert "не считается утверждённой публичной версией" not in note

    # And the scan agrees, per file and per edit.
    scan = jb._t5_2_scan()
    assert [relative for relative, _ in scan] == list(jb.T5_2_FILES)
    for relative, counts in scan:
        assert all(count for count in counts.values()), relative


def test_the_phone_view_only_announces_an_unfinished_stage_when_there_is_one() -> None:
    """T6.2 found this line hard-coded and gated on "are there any notes at all".

    The condition was not the state of T5.2 -- it was whether the KB had any
    global warning, which is a different question. So the phone view went on
    announcing an unfinished stage after the stage was finished, on the one
    screen where a stale sentence decides whether the resume text may be used.
    The condition now comes from the same file-reading note the details view
    prints, so the two cannot disagree.
    """
    note = t5_2_pending_note()
    unfinished = "НЕ применён" in note
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    brief = build_brief(real_vacancy(T61_CASES["bitrix24"]), real_kb, profile=profile)

    short = render_brief_short(brief)
    details = render_brief_details(brief)
    assert ("НЕЗАВЕРШЁННЫЙ ЭТАП" in short) is unfinished
    assert note in details, "the details view must carry the note verbatim"


# ----------------------------------------------------------------------
# Section 8 -- nothing in production moves
# ----------------------------------------------------------------------


def test_the_brief_writes_nothing_to_production_data() -> None:
    """T6 reads state.db, the KB and the profile; it must not write any of them."""
    protected = [
        REPO_ROOT / "state.db",
        REPO_ROOT / "candidate_kb.json",
        REPO_ROOT / "candidate_profile.json",
        REPO_ROOT / "resume.md",
    ]
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in protected
        if path.is_file()
    }
    kb = build_kb(skills=("Python",))
    brief = build_brief(vacancy("You must have experience with Python."), kb)
    render_brief_short(brief)
    render_brief_details(brief)
    render_brief_json(brief)
    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in protected
        if path.is_file()
    }
    assert before == after


def test_t6_reuses_the_existing_components_instead_of_growing_a_second_one() -> None:
    """The spec forbids a second matcher or a second candidate base."""
    source = (REPO_ROOT / "ai_assistant" / "candidate" / "job_brief.py").read_text(
        encoding="utf-8"
    )
    assert "from ..matcher import extract_required_years, extract_seniority" in source
    assert "from ..remote_filter import classify_work_format" in source
    assert "from ..db import get_vacancy_by_id" in source
    assert "from ..db import _row_to_vacancy" in source
    assert "from .master_resume import load_kb" in source
    # And it does not *call* the LLM path. The module docstring names it, to say
    # why T6 is deterministic instead; a mention is not a dependency.
    assert "analyze_job_deep(" not in source
    assert "import job_analyzer" not in source


def test_the_marker_vocabulary_is_shared_with_the_existing_analyzer() -> None:
    """One source of truth, so the two cannot drift apart."""
    from ai_assistant.job_analyzer import MUST_HAVE_MARKERS, NICE_TO_HAVE_MARKERS

    assert jb.MUST_HAVE_MARKERS == MUST_HAVE_MARKERS
    assert jb.NICE_TO_HAVE_MARKERS == NICE_TO_HAVE_MARKERS


def test_kind_classification_puts_commercial_first() -> None:
    """A segment asking for commercial experience is commercial even if it
    also names a technology -- otherwise the harder question hides."""
    vocabulary = {"python": "python"}
    kind, technologies = classify_kind(
        "5+ years of commercial Python experience required", vocabulary
    )
    assert kind is RequirementKind.COMMERCIAL_EXPERIENCE
    assert technologies == ("python",)


# ----------------------------------------------------------------------
# The command layer
# ----------------------------------------------------------------------


def test_the_command_reads_a_file_and_returns_zero(tmp_path: Path) -> None:
    text_file = tmp_path / "vacancy.txt"
    text_file.write_text("You must have experience with Linux.", encoding="utf-8")
    out_file = tmp_path / "brief.md"
    code = jb.candidate_job_brief_cmd(text_file=str(text_file), out_path=str(out_file))
    assert code == 0
    assert out_file.is_file()
    assert "Linux" in out_file.read_text(encoding="utf-8")


def test_the_command_reports_a_missing_vacancy_instead_of_inventing_one() -> None:
    code = jb.candidate_job_brief_cmd(vacancy_id="does-not-exist:1")
    assert code == 2


def test_the_command_requires_a_source_of_vacancy_text() -> None:
    assert jb.candidate_job_brief_cmd() == 2


def test_loading_a_missing_vacancy_raises() -> None:
    with pytest.raises(JobBriefError):
        jb.load_vacancy("no-such-vacancy:0")


# ----------------------------------------------------------------------
# Real vacancies, when the database has them
# ----------------------------------------------------------------------

REAL_DB = REPO_ROOT / "state.db"


def _has_real_db() -> bool:
    return REAL_DB.is_file() and REAL_DB.stat().st_size > 0


REAL_CASES = {
    "it-support": "weworkremotely:porkbun-live-technical-support-representative-full-time-40-000-year-benefits-sat-sun-required-location-remote",
    "sysadmin": "himalayas:https://himalayas.app/companies/dmi/jobs/senior-cyber-systems-administrator",
    "bitrix24": "habrcareer:1000168093",
    "python": "hh:136706048",
}


def real_vacancy(stable_id: str) -> jb.VacancyInput:
    """Read one vacancy straight out of the real ``state.db``, read-only.

    The suite redirects ``config.DB_FILE`` to a temporary database, so the
    application loader cannot see production rows and these pins would skip.
    Opening the file with ``mode=ro`` lets them run for real while giving the
    test no way to write to it.
    """
    import sqlite3

    if not _has_real_db():
        pytest.skip("state.db is not present in this checkout")
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM vacancies WHERE stable_id = ?", (stable_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        pytest.skip(f"{stable_id} is not in this state.db")
    return jb.vacancy_from_row(row, stable_id=stable_id)


@pytest.mark.parametrize("label", sorted(REAL_CASES))
def test_a_real_vacancy_produces_a_usable_brief(label: str) -> None:
    """The required categories, against the real database."""
    vacancy_input = real_vacancy(REAL_CASES[label])
    real_kb, _ = load_kb(None)

    brief = build_brief(vacancy_input, real_kb, profile=jb.load_profile(None))
    assert brief.assessments, f"{label}: no requirements extracted"

    short = render_brief_short(brief)
    assert short.strip()
    assert len(short.splitlines()) < 120, "the short brief must stay phone-readable"

    # Every class letter must be one of the five the spec defines.
    for assessment in brief.assessments:
        assert str(assessment.evidence_class) in {"A", "B", "C", "D", "E"}

    # Sources and statuses survive for every piece of evidence.
    for assessment in brief.assessments:
        for item in assessment.evidence:
            assert item.claim_id and item.ref and item.status

    # And a project claim never masquerades as paid work.
    for assessment in brief.assessments:
        if assessment.evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE:
            assert any(e.is_commercial for e in assessment.evidence)


def test_real_brief_finds_confirmed_experience_for_the_it_support_category() -> None:
    brief = build_brief(
        real_vacancy(REAL_CASES["it-support"]),
        load_kb(None)[0],
        profile=jb.load_profile(None),
    )
    assert brief.by_class(EvidenceClass.A_CONFIRMED_EXPERIENCE), (
        "the IT-support category must find the candidate's real support duty"
    )


def test_real_bitrix24_brief_finds_the_bitrix24_duties() -> None:
    brief = build_brief(
        real_vacancy(REAL_CASES["bitrix24"]),
        load_kb(None)[0],
        profile=jb.load_profile(None),
    )
    hits = [
        e
        for a in brief.assessments
        for e in a.evidence
        if "bitrix24" in e.value.lower()
    ]
    assert hits, "Bitrix24 experience must be reachable from a Russian posting"


def test_the_real_brief_reports_no_bitrix24_filter_conflict() -> None:
    """T7.3 removed the Bitrix24 ban, so this conflict must be gone.

    It used to fire because the profile listed ``bitrix`` in ``excluded_roles``
    and ``must_avoid_conditions`` while the candidate confirmed Bitrix24 as a
    direction to search. With the ban removed there is nothing left to disagree.
    """
    brief = build_brief(
        real_vacancy(REAL_CASES["bitrix24"]),
        load_kb(None)[0],
        profile=jb.load_profile(None),
    )
    assert not any("excluded_roles" in c.where for c in brief.conflicts), [
        c.where for c in brief.conflicts
    ]
    assert not any("must_avoid_conditions" in c.where for c in brief.conflicts), [
        c.where for c in brief.conflicts
    ]


def test_the_real_brief_still_reports_the_unestablished_years_experience_field() -> None:
    """The one conflict T7.3 deliberately left open.

    ``years_experience`` is 3 while the confirmed total is "15+ years in IT",
    and what the field means is not established. T7.3 stopped *using* it as
    tenure but did not invent a replacement value, so the brief must keep
    flagging it rather than quietly accepting it.
    """
    brief = build_brief(
        real_vacancy(REAL_CASES["bitrix24"]),
        load_kb(None)[0],
        profile=jb.load_profile(None),
    )
    assert any("years_experience" in c.where for c in brief.conflicts), [
        c.where for c in brief.conflicts
    ]


def test_the_real_brief_never_reads_years_experience_as_the_total() -> None:
    brief = build_brief(
        real_vacancy(REAL_CASES["python"]),
        load_kb(None)[0],
        profile=jb.load_profile(None),
    )
    assert brief.tenure.profile_years == 3
    assert brief.tenure.total_it_years > 3
    assert brief.tenure.profile_is_total is False


# ----------------------------------------------------------------------
# T6.1 -- the four quality fixes, pinned against the defect each one closes
# ----------------------------------------------------------------------

#: Every class heading the phone view can print, in the order it prints them.
SHORT_HEADINGS = (
    "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ:",
    "ЗАКРЫВАЕТСЯ СВОИМ ПРОЕКТОМ (не оплачиваемая работа):",
    "ЧАСТИЧНО:",
    "ЕСТЬ В РЕЗЮМЕ, НУЖНО УТОЧНЕНИЕ:",
    "ОБЯЗАТЕЛЬНЫЕ ТРЕБОВАНИЯ БЕЗ ПОДТВЕРЖДЕНИЯ:",
    "ЧТО УТОЧНИТЬ (влияет на отклик):",
    "ЧТО ВЫДЕЛИТЬ В АДАПТИРОВАННОМ РЕЗЮМЕ:",
)

T61_CASES = {
    **REAL_CASES,
    "ai-automation": (
        "weworkremotely:toptal-automation-engineer-uipath-for-innovative-ai-project"
    ),
}


def short_section(text: str, heading: str, *, required: bool = True) -> str:
    """The lines of the phone view under ``heading``, up to the next heading.

    Headings start at column 0; requirement and evidence lines are indented, so
    "the next line that starts in column 0 and ends with a colon" is the next
    heading. Reading the view by section rather than by substring is what makes
    the separation pins meaningful: "Reels Analyzer is not under the confirmed
    heading" is a claim about *where* the name appears.

    A heading is absent whenever its class is empty -- there is no "confirmed
    experience" block on a brief that confirmed nothing -- so callers asserting
    an absence pass ``required=False`` and get "".
    """
    lines = text.splitlines()
    if heading not in lines:
        if required:
            raise AssertionError(f"{heading!r} not in the short brief:\n{text}")
        return ""
    start = lines.index(heading)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index] and not lines[index].startswith(" ") and lines[index].endswith(":"):
            end = index
            break
    return "\n".join(lines[start:end])


def project_claims(kb: CandidateKnowledgeBase) -> dict[str, str]:
    """``{project claim id: project name}`` -- what a ``parent_id`` points at."""
    return {
        record.claim_id: record.value
        for record in kb.claims.values()
        if record.kind == "project"
    }


# ---- Fix 1: commercial experience and own projects are shown apart ----------


def test_a_paid_duty_and_a_project_are_shown_as_two_different_things() -> None:
    """The defect: Reels Analyzer stood under "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ".

    The technology is in both places, and both facts are true. What must not
    happen is the second one being read as the first, so the project gets its own
    line under its own heading, explicitly labelled unpaid.
    """
    kb = build_kb(
        duties=(("Bitrix24 Technical Support Specialist", "Administered self-hosted Bitrix24"),),
        projects=(("Reels Analyzer", ("Built a Bitrix24 integration",), "Bitrix24, Docker"),),
    )
    brief = build_brief(vacancy("You must have experience with Bitrix24."), kb)
    assessment = find(brief, "Bitrix24")
    assert assessment.evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE
    # Two claims, one employer: the duty and its role title.
    assert {e.where for e in assessment.commercial_evidence} == {
        "Bitrix24 Technical Support Specialist"
    }
    assert all(e.is_commercial for e in assessment.commercial_evidence)
    # Two claims, one project: the stack line and the bullet.
    assert {e.where for e in assessment.project_evidence} == {"Reels Analyzer"}
    assert all(not e.is_commercial for e in assessment.project_evidence)

    short = render_brief_short(brief)
    confirmed = short_section(short, "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ:")
    # The paid duty leads and stands on its own.
    duty_lines = [line for line in confirmed.splitlines() if "self-hosted Bitrix24" in line]
    assert duty_lines, confirmed
    # The project is still offered -- as extra evidence, on its own line, and
    # every line that names it says outright that it is not paid work.
    project_lines = [line for line in confirmed.splitlines() if "Reels Analyzer" in line]
    assert project_lines, confirmed
    for line in project_lines:
        assert "не оплачиваемая работа" in line, line
    assert confirmed.index(duty_lines[0]) < confirmed.index(project_lines[0])


def test_a_project_is_never_printed_as_paid_work_on_a_real_brief() -> None:
    """Same invariant, on every real vacancy, by heading rather than by phrase."""
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    names = set(project_claims(real_kb).values())
    assert names, "the real KB must list the candidate's own projects"

    for label, stable_id in sorted(T61_CASES.items()):
        brief = build_brief(real_vacancy(stable_id), real_kb, profile=profile)
        confirmed = short_section(render_brief_short(brief), "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ:")
        for line in confirmed.splitlines():
            if not any(name in line for name in names):
                continue
            assert "не оплачиваемая работа" in line, f"{label}: {line}"


def test_a_project_alone_never_promotes_a_tool_requirement_to_class_a() -> None:
    """A project may answer a tool question -- as class B, never as paid work."""
    kb = build_kb(
        projects=(("Job Search AI", ("Ran it in Docker",), "Docker"),),
    )
    brief = build_brief(vacancy("You must have Docker experience."), kb)
    assessment = find(brief, "Docker")
    assert assessment.evidence_class is EvidenceClass.B_OWN_PROJECT
    assert assessment.evidence
    assert all(not e.is_commercial for e in assessment.evidence)
    assert not assessment.commercial_evidence

    short = render_brief_short(brief)
    assert "Job Search AI" not in short_section(
        short, "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ:", required=False
    )
    assert "Job Search AI" in short_section(
        short, "ЗАКРЫВАЕТСЯ СВОИМ ПРОЕКТОМ (не оплачиваемая работа):"
    )
    # And the phone view names the project, not its internal identifier.
    assert "project-" not in short


def test_a_class_a_technology_is_named_by_the_paid_claim_that_carries_it() -> None:
    """T6.2, part 1, item 3: Docker, Python and Bitrix24.

    Class A is a *conjunction* -- the candidate did the work **and** the work
    used the technology. Docker, Python and Bitrix24 are the three of the spec's
    four that a paid duty really does name, so each must stay reachable as a
    paid match; the fourth, PostgreSQL, is pinned by its own test below.

    The extra assertion is the point of T6.2: it is not enough that *some*
    commercial claim exists, the claim has to name the technology. Without it a
    requirement could be reported as confirmed paid experience on the strength
    of a duty that shares only a work-area word with it.
    """
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    vocabulary = jb._kb_vocabulary(real_kb)
    # Canonical spelling, because the Russian posting writes the requirement as
    # "#Битрикс24" and a literal "Bitrix24" search would miss it.
    wanted = ("docker", "python", "bitrix24")
    paid: dict[str, list[str]] = {name: [] for name in wanted}

    for label, stable_id in sorted(T61_CASES.items()):
        brief = build_brief(real_vacancy(stable_id), real_kb, profile=profile)
        for assessment in brief.assessments:
            if assessment.evidence_class is not EvidenceClass.A_CONFIRMED_EXPERIENCE:
                continue
            for item in assessment.evidence:
                assert not (item.origin == "project" and item.is_commercial), label
            for name in wanted:
                if name not in assessment.requirement.technologies:
                    continue
                commercial = assessment.commercial_evidence
                assert commercial, f"{label}/{name}"
                assert any(
                    name in jb._canonical_terms_in(item.value, vocabulary)
                    for item in commercial
                ), f"{label}/{name} is class A without a paid claim naming it"
                paid[name].append(label)

    missing = [name for name, labels in paid.items() if not labels]
    assert not missing, f"no paid match found for {missing} in {sorted(T61_CASES)}"


def test_postgresql_is_never_confirmed_by_a_duty_that_only_says_sql() -> None:
    """T6.2, part 1, item 1.

    The Python Developer posting asks for "SQL и PostgreSQL — уверенно". The only
    paid claim in the KB that touches either names ``sql``, and it never names
    PostgreSQL -- the T6.1 brief printed it under "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ"
    anyway, which is the substitution the spec forbids.

    The claim must still be offered: the candidate really did use SQL at work.
    What must change is the class, and the reason has to be on the line.
    """
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    vocabulary = jb._kb_vocabulary(real_kb)
    brief = build_brief(real_vacancy(T61_CASES["python"]), real_kb, profile=profile)

    assessment = next(
        item for item in brief.assessments if "postgresql" in item.requirement.technologies
    )
    assert assessment.evidence_class is not EvidenceClass.A_CONFIRMED_EXPERIENCE
    assert assessment.evidence_class is EvidenceClass.C_PARTIAL

    # Lowered *despite* paid evidence, not for the want of it: that is what
    # proves the containment rule did the work.
    commercial = assessment.commercial_evidence
    assert commercial, "the SQL duty must still be offered as partial evidence"
    assert all(
        "postgresql" not in jb._canonical_terms_in(item.value, vocabulary)
        for item in commercial
    )
    assert "«postgresql»" in assessment.explanation
    assert "«sql»" in assessment.explanation

    # And it must not reach the phone view under the commercial heading.
    assert "PostgreSQL" not in short_section(
        render_brief_short(brief), "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ:", required=False
    )


# ---- Fix 2: warnings about other projects stay out of the phone view --------


def _kb_with_blocked_project() -> CandidateKnowledgeBase:
    kb = build_kb(
        duties=(("Bitrix24 Technical Support Specialist", "Administered self-hosted Bitrix24"),),
        projects=(("Repiton", ("Blocked experiment",), "Python"),),
    )
    project_id = next(iter(project_claims(kb)))
    kb.claims["ps-blocked"] = ClaimRecord(
        claim_id="ps-blocked",
        kind="project_status",
        value="STAGE_1_FINAL_NO_GO: TRACK B is blocked",
        parent_id=project_id,
        evidence={
            "source_file": PROJECT_SOURCE,
            "project_id": project_id,
            "line": 1,
            "quote": "x",
        },
    )
    return kb


def test_a_blocked_other_project_does_not_warn_on_an_unrelated_vacancy() -> None:
    """The defect: the Repiton experiment buried a Bitrix24 brief.

    The note names no technology, so wording cannot decide relevance -- the
    ``project_status`` claim's ``parent_id`` does. The project is not used by any
    answer here, so the warning is noise on a phone.
    """
    brief = build_brief(vacancy("You must have experience with Bitrix24."), _kb_with_blocked_project())
    assert not any("STAGE_1_FINAL_NO_GO" in item for item in brief.cannot_claim_relevant)
    # Dropped from the phone view only -- the safety net is still printed whole.
    assert any("STAGE_1_FINAL_NO_GO" in item for item in brief.cannot_claim)
    short = render_brief_short(brief)
    assert "STAGE_1_FINAL_NO_GO" not in short
    details = render_brief_details(brief)
    assert "STAGE_1_FINAL_NO_GO" in details
    assert "релевантны" in details


def test_a_blocked_project_that_this_vacancy_uses_is_still_reported() -> None:
    """Filtering noise must not filter the candidate's real limitations."""
    kb = _kb_with_blocked_project()
    brief = build_brief(
        vacancy("You must have Python experience for this role."), kb
    )
    assessment = find(brief, "Python")
    assert assessment.project_evidence, "the project must actually be used here"
    assert any("STAGE_1_FINAL_NO_GO" in item for item in brief.cannot_claim_relevant)
    assert "STAGE_1_FINAL_NO_GO" in render_brief_short(brief)


def test_the_real_bitrix24_brief_carries_no_other_projects_status() -> None:
    real_kb, _ = load_kb(None)
    brief = build_brief(
        real_vacancy(REAL_CASES["bitrix24"]), real_kb, profile=jb.load_profile(None)
    )
    short = render_brief_short(brief)
    for marker in ("STAGE_1", "NO_GO", "Repiton", "WHUBN"):
        assert marker not in short, f"{marker} leaked into the phone view"
    assert brief.cannot_claim, "the full warning list must still exist in --details"


# ---- Fix 3: tenure is never measured by substitution ------------------------


@pytest.mark.parametrize(
    ("text", "years"),
    (
        ("At least five (5) years of Technical Lead experience", 5),
        ("At least ten (10) years of hands-on experience in LAN/Network", 10),
        ("Do you have two or more years of live technical support experience", 2),
        ("Required Experience: 3+ years of dedicated RPA development", 3),
        ("Нужен опыт 5+ лет разработки на Python", 5),
        ("Опыт работы не менее пяти лет", 5),
        ("Experience with Windows, Linux, Database system administration", None),
    ),
)
def test_a_year_threshold_is_read_in_digits_and_in_words(text: str, years: int | None) -> None:
    """Measured: the DMI posting spells the threshold out, and it was missed."""
    assert jb._years_named(text) == years


def test_a_spelled_out_threshold_does_not_borrow_the_total_it_tenure() -> None:
    """The substitution the spec forbids: 15 years of IT as five of leadership.

    T7.2 changed the expected class here from A to C, and that change *is* the
    task rather than a side effect of it: "at least five (5) years of Technical
    Lead experience on Linux" is two asks, and the duty answers only the first.
    Class A claimed the whole requirement was closed; C says the activity is
    confirmed and its duration is not. The substitution rule itself is unchanged,
    and the assertions below still pin it -- the 15-year total is not offered as
    five years of technical leadership, and the short view still says so.
    """
    kb = build_kb(
        skills=("Linux",),
        duties=(("Earlier IT Infrastructure & Support", "Administered Linux servers"),),
    )
    brief = build_brief(
        vacancy(
            "You must have at least five (5) years of Technical Lead experience "
            "on Linux system management."
        ),
        kb,
    )
    assessment = find(brief, "Technical Lead")
    assert assessment.requirement.mandatory
    assert assessment.evidence_class is EvidenceClass.C_PARTIAL
    # The activity half: confirmed, and the explanation names the fact it rests on.
    assert "Подтверждено" in assessment.explanation
    assert "Earlier IT Infrastructure & Support" in assessment.explanation
    # The duration half: open, named as a duration, with no number borrowed for it.
    assert "Не подтверждено" in assessment.explanation
    assert "называет срок 5+ лет" in assessment.explanation
    assert "общий IT-стаж сюда не подставляется" in assessment.explanation
    assert jb.APPROVED_TOTAL_EXPERIENCE not in assessment.explanation

    short = render_brief_short(brief)
    assert "срок 5+ лет по этому требованию в KB не измеряется" in short


def test_a_year_threshold_with_no_evidence_at_all_says_so_too() -> None:
    kb = build_kb(skills=("Python",))
    brief = build_brief(vacancy("You must have 3+ years of commercial UiPath experience."), kb)
    assessment = find(brief, "UiPath")
    assert assessment.evidence_class is EvidenceClass.E_UNCONFIRMED
    assert "называет срок 3+ лет" in assessment.explanation


def test_the_published_tenure_is_the_approved_wording_and_nothing_else() -> None:
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    for label, stable_id in sorted(T61_CASES.items()):
        brief = build_brief(real_vacancy(stable_id), real_kb, profile=profile)
        assert brief.tenure.publishable() == jb.APPROVED_TOTAL_EXPERIENCE
        short = render_brief_short(brief)
        assert jb.APPROVED_TOTAL_EXPERIENCE in short, label
        # The computed figures are gate estimates, never confirmed tenure.
        assert "15.3" not in short, label
        assert "14.9" not in short, label
        assert "не установлено" in short, label


def test_the_estimate_is_shown_only_under_an_unapproved_label() -> None:
    brief = build_brief(
        real_vacancy(REAL_CASES["python"]),
        load_kb(None)[0],
        profile=jb.load_profile(None),
    )
    details = render_brief_details(brief)
    assert jb.APPROVED_TOTAL_EXPERIENCE in details
    assert "Оценка (не утверждена)" in details
    assert "НЕ утверждена и не публикуется" in details
    # The estimate may only appear after that disclaimer.
    assert details.index("НЕ утверждена") < details.index("Оценка (не утверждена)")


def test_the_profile_field_is_not_resolved_into_a_specialism_tenure() -> None:
    """The field's meaning is unestablished; the brief must not guess at it."""
    brief = build_brief(
        real_vacancy(REAL_CASES["python"]),
        load_kb(None)[0],
        profile=jb.load_profile(None),
    )
    note = brief.tenure.note or ""
    assert "не установлено" in note
    assert "ни как стаж в специализации" in note
    # "3 года Python" would be exactly the guess this pins against.
    assert "3 года" not in note


# ---- Fix 4: the phone view stays a phone view -------------------------------


def test_the_phone_view_carries_no_service_identifiers() -> None:
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    identifiers = set(real_kb.claims)
    for label, stable_id in sorted(T61_CASES.items()):
        brief = build_brief(real_vacancy(stable_id), real_kb, profile=profile)
        short = render_brief_short(brief)
        assert not [cid for cid in identifiers if cid in short], label
        for marker in ("DERIVED_FROM_SOURCE", "CONFIRMED_BY_USER", "source_file"):
            assert marker not in short, f"{label}: {marker}"


def test_the_phone_view_asks_at_most_three_questions() -> None:
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    for label, stable_id in sorted(T61_CASES.items()):
        brief = build_brief(real_vacancy(stable_id), real_kb, profile=profile)
        assert len(brief.questions) <= 3, f"{label}: {brief.questions}"


def test_an_unconfirmed_requirement_is_not_listed_under_confirmed_experience() -> None:
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    for label, stable_id in sorted(T61_CASES.items()):
        brief = build_brief(real_vacancy(stable_id), real_kb, profile=profile)
        short = render_brief_short(brief)
        confirmed = short_section(
            short, "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ:", required=False
        )
        for assessment in brief.by_class(EvidenceClass.E_UNCONFIRMED):
            head = assessment.requirement.text[:40]
            assert head not in confirmed, f"{label}: {head}"
        mandatory = [a for a in brief.by_class(EvidenceClass.E_UNCONFIRMED) if a.requirement.mandatory]
        if mandatory:
            unconfirmed = short_section(
                short, "ОБЯЗАТЕЛЬНЫЕ ТРЕБОВАНИЯ БЕЗ ПОДТВЕРЖДЕНИЯ:"
            )
            for assessment in mandatory:
                assert assessment.requirement.text[:40] in unconfirmed, label
        else:
            # Wishes without confirmation are folded into one line instead of a
            # section of their own, so an empty mandatory list is still reported.
            assert "Пожелания без подтверждения" in short, label


def test_absence_of_confirmation_is_still_not_printed_as_absence_of_skill() -> None:
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    for label, stable_id in sorted(T61_CASES.items()):
        brief = build_brief(real_vacancy(stable_id), real_kb, profile=profile)
        short = render_brief_short(brief)
        if not brief.by_class(EvidenceClass.E_UNCONFIRMED):
            continue
        assert "не доказательство отсутствия навыка" in short, label
        assert "не хватает опыта" not in short, label


def test_a_salary_without_a_currency_is_never_compared_on_the_phone() -> None:
    """Measured: the habr row stores 24 with no currency at all."""
    brief = build_brief(
        real_vacancy(REAL_CASES["bitrix24"]),
        load_kb(None)[0],
        profile=jb.load_profile(None),
    )
    assert brief.facts.salary_meets_minimum is None
    short = render_brief_short(brief)
    assert "сравнить нельзя" in short
    assert "НИЖЕ минимума" not in short
    assert "выше минимума" not in short


# ---- Fix 6: the actual state of T5.2 ----------------------------------------


def test_the_t5_2_note_matches_what_the_files_actually_contain() -> None:
    """Read from the drafts, so the note cannot drift from the files."""
    scan = jb._t5_2_scan()
    assert [relative for relative, _ in scan] == list(jb.T5_2_FILES)
    applied = all(count for _, counts in scan for count in counts.values())
    note = t5_2_pending_note()
    if applied:
        assert "НЕ применён" not in note
    else:
        assert "НЕ применён" in note
        for relative, counts in scan:
            assert relative in note
            assert all(count == 0 for count in counts.values()), relative


def test_the_t5_2_edits_are_scanned_in_both_language_drafts() -> None:
    labels = [label for label, _ in jb.T5_2_EDITS]
    assert len(labels) == 4
    assert jb.T5_2_FILES == ("artifacts/master_resume.md", "artifacts/master_resume.ru.md")
    assert any(
        "применяемые в работе и в собственных проектах" in markers
        for _, markers in jb.T5_2_EDITS
    )


# ----------------------------------------------------------------------
# T6.2 -- evidence integrity
#
# Part 1: a fact confirms a technology only if it names that technology.
# Part 2: the Freelance block is the candidate's own unpaid work, and no
#         heading, role title or role_id may turn it into paid experience.
# ----------------------------------------------------------------------


def _containment_kb() -> CandidateKnowledgeBase:
    """One paid role whose duties name SQL, Bash and PowerShell -- and no more.

    Deliberately the shape of the real corpus: the KB contains no duty that names
    ``python`` or ``postgresql`` as such, so a requirement about either has to be
    answered by something else, or not at all.
    """
    return build_kb(
        skills=("Python", "PostgreSQL", "SQL"),
        duties=(
            (
                "Earlier IT Infrastructure & Support Experience",
                "Worked across Windows Server and Linux infrastructure, Active Directory and MS SQL",
            ),
            (
                "Earlier IT Infrastructure & Support Experience",
                "Automated routine administrative tasks with Bash and PowerShell",
            ),
        ),
        spans=(("Earlier IT Infrastructure & Support Experience", "2009", "2018"),),
    )


def test_a_duty_that_names_bash_and_powershell_never_confirms_python() -> None:
    """T6.2, part 1, item 2.

    The KB's only scripting duty names Bash and PowerShell. A vacancy asking for
    Python may be answered by the candidate's own project -- as class B, labelled
    as such -- but never as paid Python experience, which the KB does not have.
    """
    kb = _containment_kb()
    brief = build_brief(vacancy("You must have Python scripting experience."), kb)
    assessment = find(brief, "Python")
    vocabulary = jb._kb_vocabulary(kb)
    assert assessment.evidence_class is not EvidenceClass.A_CONFIRMED_EXPERIENCE
    assert not assessment.commercial_evidence
    # The resume's own skill list does name Python; that is a class-D claim about
    # what the resume says, and it must never be dressed up as paid work.
    for item in assessment.evidence:
        if "python" in jb._canonical_terms_in(item.value, vocabulary):
            assert not item.is_commercial, item.claim_id


def test_a_technology_requirement_drops_evidence_that_names_none_of_it() -> None:
    """T6.2, part 1, items 3 and 5.

    "A work-area word is not a technology." The duty about Bash and PowerShell
    shares no technology with a PostgreSQL requirement, so it must not be offered
    as evidence at all -- not even as a partial match, and above all not as
    general IT experience standing in for a specific database.
    """
    kb = _containment_kb()
    brief = build_brief(vacancy("You must have PostgreSQL experience."), kb)
    assessment = find(brief, "PostgreSQL")
    vocabulary = jb._kb_vocabulary(kb)
    assert not assessment.commercial_evidence
    assert assessment.evidence_class is not EvidenceClass.A_CONFIRMED_EXPERIENCE
    for item in assessment.evidence:
        assert jb._canonical_terms_in(item.value, vocabulary) & {"postgresql"}


def test_a_partial_match_of_a_neighbouring_technology_never_raises_the_class() -> None:
    """T6.2, part 1, item 4, on a synthetic corpus.

    The requirement names SQL *and* PostgreSQL. The paid duty names SQL only, so
    the conjunction is half met: the class must fall, and the explanation must
    say which half is missing rather than leaving the reader to guess.
    """
    brief = build_brief(
        vacancy("You must have confident SQL and PostgreSQL experience."), _containment_kb()
    )
    assessment = find(brief, "PostgreSQL")
    assert assessment.evidence_class is EvidenceClass.C_PARTIAL
    assert assessment.commercial_evidence, "the SQL duty is still partial evidence"
    assert "«postgresql»" in assessment.explanation
    assert "«sql»" in assessment.explanation


def test_no_class_a_answer_rests_on_evidence_that_names_none_of_its_technologies() -> None:
    """The same rule, measured on all five real vacancies at once.

    A single exemption would be invisible on one posting and obvious across
    five, which is why this pin walks the whole corpus rather than one case.
    """
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    vocabulary = jb._kb_vocabulary(real_kb)

    for label, stable_id in sorted(T61_CASES.items()):
        brief = build_brief(real_vacancy(stable_id), real_kb, profile=profile)
        for assessment in brief.assessments:
            if assessment.evidence_class is not EvidenceClass.A_CONFIRMED_EXPERIENCE:
                continue
            wanted = jb._canonical_terms_in(assessment.requirement.text, vocabulary)
            if not wanted:
                continue
            for item in assessment.commercial_evidence:
                assert jb._canonical_terms_in(item.value, vocabulary) & wanted, (
                    f"{label}: {item.claim_id} carries class A for "
                    f"{sorted(wanted)} without naming any of them"
                )


# ---- Part 2: the Freelance block is not paid work ---------------------


def test_the_independent_role_declaration_is_shared_with_the_generator() -> None:
    """One declaration, read from ``master_resume`` -- never a second list.

    The KB says "since May 2026 I built only my own projects, with no paid
    orders". Two modules that each keep their own copy of that fact will
    eventually disagree, and the disagreement is invisible until a brief calls a
    personal project commercial experience.
    """
    from ai_assistant.candidate.master_resume import INDEPENDENT_ROLE_EVIDENCE

    assert jb._own_work_roles() == frozenset(INDEPENDENT_ROLE_EVIDENCE)
    assert jb._own_work_roles(), "the declaration must not be empty"


def test_the_own_work_declaration_is_a_confirmed_fact_about_that_role() -> None:
    """The declaration has to be the candidate's own confirmed words."""
    from ai_assistant.candidate.master_resume import INDEPENDENT_ROLE_EVIDENCE

    real_kb, _ = load_kb(None)
    for role_id, claim_id in INDEPENDENT_ROLE_EVIDENCE.items():
        record = real_kb.claims[claim_id]
        assert record.status == ClaimStatus.CONFIRMED_BY_USER, claim_id
        assert record.role_id == role_id, claim_id


def test_every_freelance_duty_is_own_work_and_never_commercial() -> None:
    """T6.2, part 2: all five duties of the Freelance block, one by one.

    The block sits under Professional Experience in ``resume.md`` and carries the
    same role title as a real employer's block, so nothing about how it *looks*
    can decide the question. What decides it is the declared role id, and every
    duty that carries it must come out as own work.
    """
    real_kb, _ = load_kb(None)
    own_roles = jb._own_work_roles()
    duties = [
        record
        for record in real_kb.claims.values()
        if record.kind == "duty" and record.role_id in own_roles
    ]
    assert len(duties) == 5, [record.claim_id for record in duties]

    for record in duties:
        evidence = jb._evidence_for(record, {}, {}, own_roles)
        assert evidence.origin == "own_work", record.claim_id
        assert not evidence.is_commercial, record.claim_id
        assert evidence.is_own_work, record.claim_id
        assert evidence.where == "свои проекты — без оплаты", record.claim_id
        # And it must not smuggle itself in through the role link either.
        assert not evidence.role_linked, record.claim_id


def test_the_own_work_role_has_a_company_but_no_employer() -> None:
    """Why the label cannot be inferred from the record's own fields.

    The Freelance role has a ``role_company`` ("Freelance - AI Integration &
    Automation") and **no** ``role_employer``, and its ``role_title`` is
    "AI Automation Engineer" -- identical to a genuine employer's block. So "has
    a company" cannot mean "was paid", and the title cannot separate them.
    """
    real_kb, _ = load_kb(None)
    own_roles = jb._own_work_roles()
    by_role = [record for record in real_kb.claims.values() if record.role_id in own_roles]
    kinds = {record.kind for record in by_role}
    assert "role_company" in kinds
    assert "role_employer" not in kinds, "the own-work block must claim no employer"

    titles = {record.value for record in by_role if record.kind == "role_title"}
    paid_titles = {
        record.value
        for record in real_kb.claims.values()
        if record.kind == "role_title" and record.role_id not in own_roles
    }
    assert titles & paid_titles, "the two blocks are meant to look alike"


def test_class_a_uses_only_evidence_from_hired_work() -> None:
    """T6.2, part 2: the commercial bucket, checked on the real corpus.

    Every claim offered as paid experience must come from a role that was paid
    employment: role-linked, and not one of the declared independent roles.
    """
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    own_roles = jb._own_work_roles()

    for label, stable_id in sorted(T61_CASES.items()):
        brief = build_brief(real_vacancy(stable_id), real_kb, profile=profile)
        for assessment in brief.assessments:
            for item in assessment.commercial_evidence:
                assert item.origin == "resume", f"{label}/{item.claim_id}"
                assert item.role_linked, f"{label}/{item.claim_id}"
                record = real_kb.claims[item.claim_id]
                assert record.role_id not in own_roles, f"{label}/{item.claim_id}"


def test_a_freelance_duty_is_never_printed_as_paid_work() -> None:
    """The same fact as seen from the phone: the label, not the claim kind.

    The Python Developer posting is answered by a Freelance duty about asyncio.
    That duty is a real duty of a real role, so nothing in its shape says it was
    unpaid -- only the label does.
    """
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    brief = build_brief(real_vacancy(T61_CASES["python"]), real_kb, profile=profile)
    own_roles = jb._own_work_roles()
    own_duties = {
        record.claim_id
        for record in real_kb.claims.values()
        if record.kind == "duty" and record.role_id in own_roles
    }

    assessment = find(brief, "asyncio")
    assert assessment.evidence_class is EvidenceClass.B_OWN_PROJECT
    assert assessment.project_evidence
    assert all(item.claim_id not in own_duties for item in assessment.commercial_evidence)

    short = render_brief_short(brief)
    confirmed = short_section(short, "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ:", required=False)
    for claim_id in own_duties:
        assert claim_id not in confirmed
    own_section = short_section(short, "ЗАКРЫВАЕТСЯ СВОИМ ПРОЕКТОМ (не оплачиваемая работа):")
    assert "asyncio" in own_section
    assert "свои проекты — без оплаты" in own_section


def test_technology_tenure_is_never_taken_from_the_total_it_history() -> None:
    """T6.2, part 2, last clause.

    The DMI posting asks for "At least five (5) years of Technical Lead
    experience on system management". The candidate has 15+ years in IT; none of
    it is measured per technology, and the brief must not spend the total on the
    requirement. The caveat has to ride on the line whatever class it came out
    as -- it used to hang off the paid-work branch only, which is how a
    spelled-out threshold slipped past.
    """
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    brief = build_brief(real_vacancy(T61_CASES["sysadmin"]), real_kb, profile=profile)

    assessment = find(brief, "Technical Lead experience")
    assert "не измеряется" in assessment.explanation
    assert "общий IT-стаж сюда не подставляется" in assessment.explanation
    assert "15" not in assessment.explanation


# ----------------------------------------------------------------------
# T7, part 1 -- a name the facts cannot answer must not disappear
#
# The defect this closes: requirement extraction asked the KB vocabulary what a
# requirement was about, so a technology the candidate has no claim for simply
# was not there. "Docker; опыт настройки CI/CD-пайплайнов (GitLab CI, GitHub
# Actions, Jenkins)" came back asking only about Docker, Docker was confirmed,
# and the whole line was reported as confirmed experience -- a partial match
# dressed up as a full one, which is the most expensive kind of wrong.
# ----------------------------------------------------------------------


def _docker_and_cicd_kb() -> CandidateKnowledgeBase:
    """One paid role that used Docker, and nothing that touches CI/CD.

    Deliberately the shape of the real corpus: the candidate's Docker duty is
    genuine paid work, and no fact anywhere names GitLab CI, GitHub Actions,
    Jenkins or CI/CD at all -- so the brief has to say so rather than let
    Docker's confirmation answer for them.

    The skill list carries the names the case table checks, because the
    vocabulary is built from term-like claims: without a claim there is no term
    to recognise, and every requirement would look unanswerable.
    """
    return build_kb(
        skills=("Docker", "Python", "PowerShell", "SQL", "PostgreSQL", "Bitrix24"),
        duties=(
            (
                "AI Automation Engineer",
                "Administered Linux and Docker infrastructure for internal services",
            ),
        ),
        spans=(("AI Automation Engineer", "Nov 2022", "Apr 2026"),),
    )


CICD_REQUIREMENT = (
    "Docker; опыт настройки CI/CD-пайплайнов (GitLab CI, GitHub Actions, Jenkins)"
)
CICD_NAMES = {"CI/CD", "GitHub Actions", "GitLab CI", "Jenkins"}


def test_docker_does_not_confirm_the_cicd_names_standing_beside_it() -> None:
    """T7, part 1, first clause.

    The requirement is one requirement and it asks for two things. Docker is
    paid work and is genuinely confirmed; CI/CD, GitLab CI, GitHub Actions and
    Jenkins are asked for in the same breath and no fact answers them. A class
    is a statement about the requirement as a whole, so it cannot stay A.
    """
    kb = _docker_and_cicd_kb()
    brief = build_brief(vacancy(CICD_REQUIREMENT), kb)
    assessment = find(brief, "Docker")

    assert assessment.requirement.unrecognised, "the names vanished from the requirement"
    assert set(assessment.requirement.unrecognised) == CICD_NAMES
    assert assessment.evidence_class is not EvidenceClass.A_CONFIRMED_EXPERIENCE
    # Docker itself is still confirmed paid work -- the demotion is about the
    # requirement, not about the technology the candidate really used.
    assert "docker" in {
        tech for item in assessment.commercial_evidence
        for tech in jb._canonical_terms_in(item.value, jb._kb_vocabulary(kb))
    }


def test_an_unknown_name_is_never_printed_as_a_missing_skill() -> None:
    """T7, part 1, "не приписывай кандидату отсутствие навыка".

    The KB holds what the candidate has claimed, so a name missing from it is a
    limit of this brief. Wording that reads as "you do not have Jenkins" would
    be a statement about the person that the sources cannot support, and he
    would have to argue with it in an interview.
    """
    kb = _docker_and_cicd_kb()
    brief = build_brief(vacancy(CICD_REQUIREMENT), kb)
    assessment = find(brief, "Docker")

    assert "не подтверждено доступными фактами" in assessment.explanation.lower()
    assert "ограничение справки" in assessment.explanation
    for forbidden in ("отсутствует", "нет навыка", "не владеет", "не умеет"):
        assert forbidden not in assessment.explanation.lower()


def test_the_unrecognised_names_reach_every_view() -> None:
    """A fact that only exists in the explanation is invisible on a phone."""
    kb = _docker_and_cicd_kb()
    brief = build_brief(vacancy(CICD_REQUIREMENT), kb)

    short = render_brief_short(brief)
    details = render_brief_details(brief)
    payload = json.loads(render_brief_json(brief))

    for name in CICD_NAMES:
        assert name in short, name
        assert name in details, name
    entry = next(
        item for item in payload["assessments"] if "Docker" in item["requirement"]
    )
    assert set(entry["unrecognised"]) == CICD_NAMES
    assert entry["disjunctive"] is False


def test_a_requirement_naming_only_unknown_names_is_still_a_requirement() -> None:
    """The second half of the spec's example, on its own.

    It names nothing the KB can resolve, so a first draft filed it as a duty and
    it disappeared from the ask entirely. It is a requirement -- an unanswered
    one, which is exactly what the brief exists to surface.
    """
    kb = _docker_and_cicd_kb()
    half = "опыт настройки CI/CD-пайплайнов (GitLab CI, GitHub Actions, Jenkins)"
    brief = build_brief(vacancy(half), kb)
    assessment = find(brief, "CI/CD-пайплайнов")

    assert set(assessment.requirement.unrecognised) == CICD_NAMES
    assert assessment.evidence_class is EvidenceClass.E_UNCONFIRMED
    assert "не подтверждено доступными фактами" in assessment.explanation.lower()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Docker", "docker"),
        ("docker", "docker"),
        ("Python experience required", "python"),
        ("SQL", "sql"),
        ("PostgreSQL", "postgresql"),
        ("postgresql", "postgresql"),
        ("PostgreSQL (asyncpg/SQLAlchemy)", "postgresql"),
        ("Опыт работы с PostgreSQL", "postgresql"),
        ("Bitrix24", "bitrix24"),
        ("#Битрикс24", "bitrix24"),
    ],
)
def test_a_technology_is_recognised_whatever_the_case(text: str, expected: str) -> None:
    """T7, part 1, "PostgreSQL распознавался независимо от регистра букв".

    Measured before it was believed: the token path lower-cases, so the case
    requirement was already met. Pinned anyway, because the containment rule
    below it is what makes a wrong answer here expensive -- "PostgreSQL" reading
    as an unknown name would quietly stop a real match from being found.
    """
    kb = _docker_and_cicd_kb()
    vocabulary = jb._kb_vocabulary(kb)
    assert expected in jb._canonical_terms_in(text, vocabulary)
    assert jb._unrecognised_terms(text, vocabulary) == set()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (CICD_REQUIREMENT, CICD_NAMES),
        ("Опыт работы с PHP и MySQL", {"MySQL"}),
        ("Знание UiPath и RPA", {"UiPath"}),
        ("Знание UiPath, RPA и ServiceNow", {"UiPath", "ServiceNow"}),
        ("#CI/CD", {"CI/CD"}),
        # A slash list: both halves are names and the first has a shape, so the
        # run vouches for the second.
        ("Azure DevOps/Jira", {"Azure DevOps", "Jira"}),
        # The same two names in prose. Only the shaped one is claimed: "Jira" on
        # its own is a capitalised word like any other, and the brief does not
        # guess. Under-reporting is the direction this rule is allowed to err in.
        ("Experience with Azure DevOps and Jira", {"DevOps"}),
        ("Familiarity with Git, Azure DevOps, Jira and ServiceNow",
         {"DevOps", "ServiceNow"}),
        # A bare capitalised word with nothing beside it is not claimed to be a
        # name: a single capitalised word is indistinguishable from ordinary
        # prose, and guessing there would put invented requirements on the page.
        # Inside a list the neighbouring names vouch for it -- which is why
        # "Jenkins" appears in the first row and not on its own here.
        ("Jenkins", set()),
        # A slash pair that is ordinary words, not a name.
        ("Security certification at IAT Level II/III", set()),
    ],
)
def test_a_name_the_facts_cannot_answer_is_kept_as_written(
    text: str, expected: set[str]
) -> None:
    """T7, part 1, "неизвестный термин сохраняй в исходном требовании".

    Kept in the posting's own spelling, because that is the string the reader
    will search the vacancy for. The table also pins what must *not* become a
    name: bare prose words, phrases carrying a technology, and slash pairs like
    "II/III". Every one of those would be a requirement the vacancy never wrote,
    and the reader cannot act on a question nobody asked.
    """
    kb = _docker_and_cicd_kb()
    vocabulary = jb._kb_vocabulary(kb)
    assert jb._unrecognised_terms(text, vocabulary) == expected


@pytest.mark.parametrize(
    ("text", "disjunctive"),
    [
        ("Python или PowerShell", True),
        ("Python or PowerShell", True),
        ("SQL и PostgreSQL", False),
        ("Python and PowerShell", False),
        ("Опыт работы с Docker", False),
        ("monitoring and order processing", False),
        # The near miss: an "или" that joins two kinds of database, with the two
        # names it sits between far apart. A draft that only searched for the
        # word read this as a choice and let SQL answer PostgreSQL.
        (
            "SQL и PostgreSQL — уверенно; опыт хотя бы с одной аналитической или NoSQL СУБД",
            False,
        ),
        # "или" inside a word is not a connector.
        ("Опыт разработки на Python", False),
    ],
)
def test_a_choice_is_told_apart_from_a_conjunction(text: str, disjunctive: bool) -> None:
    """T7, part 1, last clause.

    "A or B" is answered by either; "A and B" needs both. Nothing in the
    technology sets tells the two apart, so the wording has to. Getting it wrong
    is asymmetric: reading a conjunction as a choice over-claims, reading a
    choice as a conjunction only under-claims.
    """
    vocabulary = jb._kb_vocabulary(_docker_and_cicd_kb())
    assert jb._is_disjunction(text, vocabulary) is disjunctive
    # The dataclass default is "conjunction", never a guess: a requirement read
    # as a choice when it is not one over-claims, and that is the expensive
    # direction.
    assert jb.Requirement(text, RequirementKind.TOOL_USE, True).disjunctive is False
    # And the rule itself: a choice is answered by one alternative, a
    # conjunction by all of them, a requirement naming no technology by
    # anything.
    assert jb._answered_by(set(), {"docker"}, False) is False
    assert jb._answered_by({"docker"}, {"docker", "python"}, False) is False
    assert jb._answered_by({"docker"}, {"docker", "python"}, True) is True
    assert jb._answered_by({"docker"}, {"docker"}, False) is True
    assert jb._answered_by({"python"}, set(), False) is True


def test_a_choice_is_answered_by_one_alternative_end_to_end() -> None:
    """The rule reaches the class, not just the helper.

    The requirement "Python or PowerShell" is answered by the candidate's paid
    Bash/PowerShell duty: one alternative is enough. Demanding both would demote
    a requirement he genuinely meets, which is the failure mode on the other
    side of the one the spec is closing. The skill list carries both names
    because the vocabulary is built from term-like claims -- without a claim
    there is no term for the requirement to be a choice about.
    """
    kb = build_kb(
        skills=("Python", "PowerShell"),
        duties=(
            (
                "Earlier IT Infrastructure & Support Experience",
                "Automated routine administrative tasks with Bash and PowerShell",
            ),
        ),
        spans=(("Earlier IT Infrastructure & Support Experience", "2009", "2018"),),
    )
    brief = build_brief(vacancy("Practical Python or PowerShell scripting required."), kb)
    assessment = find(brief, "scripting")
    assert assessment.requirement.disjunctive is True
    assert assessment.evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE


def test_a_conjunction_still_needs_every_alternative() -> None:
    """The same duty, the same technologies, one word changed."""
    kb = build_kb(
        skills=("Python", "PowerShell"),
        duties=(
            (
                "Earlier IT Infrastructure & Support Experience",
                "Automated routine administrative tasks with Bash and PowerShell",
            ),
        ),
        spans=(("Earlier IT Infrastructure & Support Experience", "2009", "2018"),),
    )
    brief = build_brief(vacancy("Practical Python and PowerShell scripting required."), kb)
    assessment = find(brief, "scripting")
    assert assessment.requirement.disjunctive is False
    assert assessment.evidence_class is not EvidenceClass.A_CONFIRMED_EXPERIENCE


def test_a_disjunction_elsewhere_in_the_line_does_not_make_the_names_a_choice() -> None:
    """The near miss that the first draft of this rule fell into.

    The Python Developer posting asks for "SQL и PostgreSQL — уверенно; опыт хотя
    бы с одной аналитической или NoSQL СУБД (ClickHouse, Elasticsearch,
    MongoDB)". The "или" joins two *kinds of database*, not the two names the
    candidate is being measured on. Reading it as a choice let SQL answer a
    PostgreSQL requirement -- the exact over-claim this whole rule exists to
    prevent, reintroduced by the fix for the opposite one.
    """
    real_kb, _ = load_kb(None)
    brief = build_brief(
        real_vacancy(T61_CASES["python"]), real_kb, profile=jb.load_profile(None)
    )
    assessment = find(brief, "PostgreSQL")
    assert "или" in assessment.requirement.text
    assert assessment.requirement.disjunctive is False
    assert assessment.evidence_class is EvidenceClass.C_PARTIAL


def test_the_real_python_posting_no_longer_reads_docker_as_the_whole_line() -> None:
    """The defect on the vacancy it was found on, not on a fixture.

    Before this change the line was class A: Docker is paid work, so the brief
    answered the whole requirement with it and the three names it also asks for
    were nowhere on the page.
    """
    real_kb, _ = load_kb(None)
    brief = build_brief(
        real_vacancy(T61_CASES["python"]), real_kb, profile=jb.load_profile(None)
    )
    assessment = find(brief, "CI/CD-пайплайнов")

    assert set(assessment.requirement.unrecognised) == CICD_NAMES
    assert assessment.evidence_class is not EvidenceClass.A_CONFIRMED_EXPERIENCE
    assert "Docker" in assessment.requirement.text  # the original line is intact
    assert "не подтверждено доступными фактами" in assessment.explanation.lower()
    # And the three names are on the phone screen, not only in --details.
    assert "Jenkins" in render_brief_short(brief)


# ----------------------------------------------------------------------
# T7.2 -- IT support evidence, long-text extraction, and the fallback
# ----------------------------------------------------------------------

#: The vacancy T7.2 was written for. Measured shape: 9,362 characters and
#: **zero** newlines, with the braille blank U+2800 standing where the posting
#: had its paragraph breaks. Every defect below was measured on this row.
IGNITION = "weworkremotely:ignition-inc-mac-msp-help-desk-guru-work-from-home"

T72_CASES = {**T61_CASES, "ignition": IGNITION}

#: The posting's four hard requirements, quoted verbatim. Two are candidate asks
#: and two are terms of the job; all four have to be on the page either way.
IGNITION_HARD_REQUIREMENTS = (
    "Can work standard business hours in Eastern or Pacific time zones (we currently need both)",
    "Have a quiet home workspace and reliable broadband",
    "Have proven Apple aptitude — you actually use and like the Apple ecosystem",
    "Have at least 2 years of IT support experience on the job",
)

#: Company advertising. The pre-T7.2 brief reported three of these four as
#: *requirements* -- two of them mandatory -- and lost all four hard ones.
IGNITION_ADVERTISEMENT = (
    "Apple-first TL;DR ignitionit.com is a seasoned",
    "About ignitionit.com We're one of the oldest Apple Technical Partners",
    "As you grow, there's a clear path into infrastructure administration",
    "We move fast, priorities shift",
)

#: The Apple/MSP stack the posting names. The KB has a claim for none of it, and
#: T7.2 section 4 requires that to be *reported* rather than silently absent.
IGNITION_UNRECOGNISED = {
    "Apple Business Manager",
    "Entra ID",
    "Google Workspace",
    "Intune",
    "Iru",
    "Jamf",
    "MDM",
    "MFA",
    "Microsoft 365",
    "Okta",
    "SSO",
    "Workspace ONE",
    "Yubikey",
    "iOS",
    "macOS",
}


def ignition_brief() -> jb.Brief:
    return build_brief(
        real_vacancy(IGNITION), load_kb(None)[0], profile=jb.load_profile(None)
    )


def ignition_vacancy(*requirements: str) -> VacancyInput:
    """The Ignition row with hand-supplied requirement lines attached."""
    return replace(real_vacancy(IGNITION), manual_requirements=requirements)


def everything_read(brief: jb.Brief) -> list[str]:
    """Every piece of text the brief read out of the posting."""
    return [a.requirement.text for a in brief.assessments] + [
        condition.text for condition in brief.facts.conditions
    ]


# ---- 1. The long one-line description is read at all ------------------------


def test_the_ignition_hard_requirements_all_survive_the_long_description() -> None:
    """T7.2 sections 2 and 5, on the vacancy both were reported against.

    Measured before the fix: the four hard requirements arrived as one
    440-character segment -- the posting separates them with nothing at all --
    and the segment's own mention of "Pacific time" matched the compensation
    filter, so the whole block was filed as context. The brief then reported
    five requirements, four of them the company's own advertising, and the
    two-year IT support requirement was nowhere on the page.
    """
    brief = ignition_brief()
    read = everything_read(brief)
    for requirement in IGNITION_HARD_REQUIREMENTS:
        assert any(requirement in text for text in read), requirement


def test_the_ignition_advertisement_is_not_reported_as_a_requirement() -> None:
    """T7.2 section 5: the company description must not become an ask.

    Each of these was a requirement in the baseline brief, and two of them were
    *mandatory* -- so the brief presented the employer's own marketing as things
    the candidate had to satisfy.
    """
    brief = ignition_brief()
    read = everything_read(brief)
    for blurb in IGNITION_ADVERTISEMENT:
        assert not any(blurb in text for text in read), blurb


def test_a_career_path_pitch_is_neither_a_requirement_nor_a_condition() -> None:
    """The specific false positive the growth-promise rule closes.

    "As you grow, there's a clear path into infrastructure administration ..."
    named a real KB term ("infrastructure"), so it survived the technology test
    and became the only class-A item in the whole Ignition brief. The brief then
    read as though it held one confirmed match, and what it had confirmed was
    the employer's promise about the candidate's future.
    """
    brief = ignition_brief()
    assert not [
        a for a in brief.assessments if "clear path into" in a.requirement.text
    ]
    assert not [
        c for c in brief.facts.conditions if "clear path into" in c.text
    ]


def test_a_culture_paragraph_is_not_an_employment_condition() -> None:
    """The ``shifts?`` false positive, pinned where it was measured.

    The bare pattern matched the *verb* in "our priorities shift", so Ignition's
    culture paragraph -- which states no employment term at all -- was reported
    as a mandatory condition of the job.
    """
    brief = ignition_brief()
    assert not [
        c for c in brief.facts.conditions if "priorities shift" in c.text
    ]
    assert not [a for a in brief.assessments if "priorities shift" in a.requirement.text]
    # The pattern still catches shifts named as a schedule.
    assert any("Available Shifts" in c.text for c in _porkbun_brief().facts.conditions)


def _porkbun_brief() -> jb.Brief:
    return build_brief(
        real_vacancy(REAL_CASES["it-support"]),
        load_kb(None)[0],
        profile=jb.load_profile(None),
    )


def test_the_ignition_schedule_and_home_office_are_conditions_not_skills() -> None:
    """T7.2 section 5: pay and schedule are not candidate skills.

    They are still *reported* -- a brief that dropped "standard business hours in
    Eastern or Pacific time zones" would hide the condition the whole
    application depends on -- but in their own bucket, where no evidence class is
    claimed for them.
    """
    brief = ignition_brief()
    conditions = [c.text for c in brief.facts.conditions]
    assert any("Eastern or Pacific time zones" in text for text in conditions)
    assert any("quiet home workspace" in text for text in conditions)
    # And the same texts are not also assessed as skills.
    assessed = [a.requirement.text for a in brief.assessments]
    assert not any("quiet home workspace" in text for text in assessed)


def test_the_ignition_salary_line_is_a_condition_and_never_a_skill() -> None:
    brief = ignition_brief()
    assert any("Base salary" in c.text for c in brief.facts.conditions)
    assert not [a for a in brief.assessments if "Base salary" in a.requirement.text]


def test_the_ignition_apple_stack_is_reported_even_though_no_fact_answers_it() -> None:
    """T7.2 section 4: an unknown name is not filed as "not stated".

    The KB holds no claim for any of these, and the honest report is "the brief
    cannot answer this", not silence -- silence reads as "the vacancy does not
    ask", which is the opposite of the truth.
    """
    brief = ignition_brief()
    named = {
        name for assessment in brief.assessments for name in assessment.requirement.unrecognised
    }
    assert IGNITION_UNRECOGNISED <= named
    # The full view names every one of them against the requirement that uses it,
    # so the reader can see *which* ask the brief cannot answer.
    details = render_brief_details(brief)
    for name in ("Apple Business Manager", "Jamf", "Intune", "Workspace ONE", "macOS"):
        assert f"«{name}»" in details, name
    # The phone view has one screen for this, and the point there is that the
    # names are a limit of the brief rather than a skill the candidate lacks.
    short = render_brief_short(brief)
    assert "macOS" in short
    assert "не подтверждено доступными фактами" in short


def test_the_ignition_brief_stays_phone_readable() -> None:
    short = render_brief_short(ignition_brief())
    assert len(short.splitlines()) < 120
    # The reason the one class-C mandatory line is only partly answered is on
    # this screen, not only in --details.
    assert "срок 2+ лет по этому требованию в KB не измеряется" in short


# ---- 2. Professional equivalences ------------------------------------------


def test_the_two_year_it_support_ask_is_answered_by_the_real_support_duty() -> None:
    """T7.2 section 2, measured on the requirement it was reported against.

    "Have at least 2 years of IT support experience on the job" shares no literal
    two-word match with "Provided technical support and troubleshooting for
    business users." and, because it names no technology at all, shared no
    work-area word either -- so the matcher returned nothing and the brief
    reported a confirmed duty as absent.
    """
    brief = ignition_brief()
    assessment = find(brief, "IT support experience")
    assert assessment.requirement.mandatory
    assert [item.claim_id for item in assessment.evidence] == ["duty-94b646ace9"]
    assert assessment.evidence[0].is_commercial is True


def test_the_equivalence_that_carried_the_match_is_named_with_its_source() -> None:
    """An equivalence the reader cannot check is indistinguishable from a guess."""
    assessment = find(ignition_brief(), "IT support experience")
    assert "Профессиональная эквивалентность" in assessment.explanation
    assert "«it-support»" in assessment.explanation
    assert "duty-94b646ace9" in assessment.explanation
    assert "technical support" in assessment.explanation


def test_every_declared_equivalence_carries_a_reason_and_a_real_anchor() -> None:
    """T7.2 section 2: each equivalent needs a test, an explanation, a source fact.

    This is that sentence as an assertion. An entry with no rationale, no pattern
    or no anchor fails here, and so does an anchor that is not a duty -- a
    project bullet cannot be the basis for calling two wordings the same job.
    """
    real_kb, _ = load_kb(None)
    assert jb._PROFESSIONAL_EQUIVALENTS
    for equivalent in jb._PROFESSIONAL_EQUIVALENTS:
        assert equivalent.activity
        assert equivalent.rationale.strip(), equivalent.activity
        assert equivalent.vacancy_patterns, equivalent.activity
        assert equivalent.fact_patterns, equivalent.activity
        assert equivalent.anchors, equivalent.activity
        for anchor in equivalent.anchors:
            record = real_kb.claims[anchor]
            assert record.kind == "duty", anchor
            assert record.in_source, anchor
            assert any(p.search(record.value) for p in equivalent.fact_patterns), (
                equivalent.activity,
                record.value,
            )


def test_each_declared_equivalence_fires_on_the_wording_it_was_declared_for() -> None:
    """Both halves of every entry, in the direction the matcher uses them."""
    real_kb, _ = load_kb(None)
    anchor = real_kb.claims["duty-94b646ace9"]
    for equivalent in jb._PROFESSIONAL_EQUIVALENTS:
        for pattern in equivalent.fact_patterns:
            assert pattern.search(anchor.value), equivalent.activity
    for text, activity in (
        ("Have at least 2 years of IT support experience on the job", "it-support"),
        ("Experience with user support in a corporate environment", "user-support"),
        ("Diagnosis of technical issues across Windows and macOS", "troubleshooting"),
    ):
        asked = {item.activity for item in jb._equivalents_asked(text)}
        assert activity in asked, (text, activity)


def test_the_equivalence_mechanism_did_not_lower_the_domain_threshold() -> None:
    """The shortcut T7.2 section 2 forbids, pinned as a number.

    Lowering the threshold to one shared work-area word is how security *work*
    became a security *certificate* in T6.2. The equivalence reaches the same
    fact by declaring the connection, not by loosening the test.
    """
    assert jb._MIN_DOMAIN_HITS == 2
    kb = build_kb(
        duties=(("Support | Acme", "Provided technical support and troubleshooting for users."),),
    )
    assessment = find(build_brief(vacancy("You must have strong communication skills."), kb), "communication")
    assert assessment.evidence_class is EvidenceClass.E_UNCONFIRMED


def test_an_equivalence_never_confirms_a_technology_the_fact_does_not_name() -> None:
    """A declared equivalence connects two wordings of one *activity*.

    It must not become a second, quieter route around the containment rule: the
    support duty does not mention PostgreSQL, so a PostgreSQL requirement that
    also reads like IT support is still not answered by it.
    """
    real_kb, _ = load_kb(None)
    brief = build_brief(
        ignition_vacancy("Have at least 2 years of IT support experience on the job"),
        real_kb,
        profile=jb.load_profile(None),
    )
    assessment = find(brief, "IT support experience")
    vocabulary = jb._kb_vocabulary(real_kb)
    for item in assessment.evidence:
        assert "postgresql" not in jb._canonical_terms_in(item.value, vocabulary)


# ---- 3. Presence of the experience vs its duration -------------------------


def test_the_two_year_requirement_is_split_and_not_reported_as_closed() -> None:
    """T7.2 section 3, on the Ignition requirement.

    Two independent checks live in that one line: whether the work was done, and
    whether it lasted two years. The duty answers the first. Nothing in the KB
    measures years per requirement, so class A would report the whole ask as
    closed while half of it is open.
    """
    assessment = find(ignition_brief(), "IT support experience")
    assert assessment.evidence_class is EvidenceClass.C_PARTIAL
    assert "Подтверждено" in assessment.explanation
    assert "Bitrix24 Technical Support Specialist" in assessment.explanation
    assert "Не подтверждено" in assessment.explanation
    assert "называет срок 2+ лет" in assessment.explanation


def test_no_year_threshold_is_ever_answered_as_fully_confirmed() -> None:
    """The substitution T7.2 section 3 forbids, pinned across all six vacancies.

    Every requirement whose text names a number of years must land outside A and
    B: those two classes mean "this ask is met", and no ask that includes a
    duration is met on evidence that cannot measure duration.
    """
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    for label, stable_id in sorted(T72_CASES.items()):
        brief = build_brief(real_vacancy(stable_id), real_kb, profile=profile)
        for assessment in brief.assessments:
            years = jb._years_named(assessment.requirement.text)
            if not years:
                continue
            assert assessment.evidence_class not in (
                EvidenceClass.A_CONFIRMED_EXPERIENCE,
                EvidenceClass.B_OWN_PROJECT,
            ), f"{label}: {years} years reported as met :: {assessment.requirement.text}"


def test_the_unmeasured_duration_is_stated_and_never_borrowed() -> None:
    """The four numbers the spec names are all the wrong number for this question.

    Not the approved 15+ years of total IT, not ``years_experience = 3`` from the
    profile, not another role's duration and not a technology's tenure. The brief
    says the duration is not established instead.
    """
    brief = ignition_brief()
    assessment = find(brief, "IT support experience")
    assert jb.APPROVED_TOTAL_EXPERIENCE not in assessment.explanation
    assert "years_experience" not in assessment.explanation
    assert "3" not in assessment.explanation
    assert jb.APPROVED_TOTAL_EXPERIENCE in render_brief_short(brief)


def test_a_project_match_with_a_year_threshold_drops_out_of_class_b() -> None:
    """B means the same thing as A, one origin down: this ask is met.

    A duration the KB cannot measure disqualifies it the same way, and the
    explanation says which half is which.
    """
    kb = build_kb(
        projects=(("Reels Analyzer", ("Analyses short videos.",), "PostgreSQL"),),
    )
    assessment = find(
        build_brief(vacancy("You must have 3+ years of commercial PostgreSQL experience."), kb),
        "PostgreSQL",
    )
    assert assessment.evidence_class is EvidenceClass.C_PARTIAL
    assert "собственным проектом" in assessment.explanation
    assert "называет срок 3+ лет" in assessment.explanation


# ---- 4. Requirement categories ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "technologies", "expected"),
    (
        (
            "Have at least 2 years of IT support experience on the job",
            (),
            {jb.RequirementCategory.DURATION, jb.RequirementCategory.DUTY},
        ),
        (
            "Can work standard business hours in Eastern or Pacific time zones",
            (),
            {jb.RequirementCategory.CONDITION},
        ),
        (
            "Industry recognized security certification, IAT Level II/III",
            (),
            {jb.RequirementCategory.CERTIFICATE},
        ),
        (
            "Experience with Windows, Linux, Database, and Web Apps system administration",
            ("windows", "linux"),
            {jb.RequirementCategory.TECHNOLOGY},
        ),
    ),
)
def test_categories_separate_what_kind_of_ask_it_is(
    text: str,
    technologies: tuple[str, ...],
    expected: set[jb.RequirementCategory],
) -> None:
    """T7.2 section 4: technology, duty, condition, duration, certificate.

    Read off the vacancy alone, so this needs no KB and no candidate facts: the
    category says *what is being asked for*, which is a different question from
    whether the candidate can answer it. ``technologies`` is passed in because
    the caller has already resolved the names -- the classifier does not go
    looking for them a second time.
    """
    categories = set(jb.classify_category(text, technologies, ()))
    assert expected <= categories


def test_no_requirement_is_filed_without_a_category() -> None:
    """The category list is never empty -- "не указано" is not a category.

    A requirement that matches nothing else is still an ask about the work, so
    the classifier falls back to DUTY. Without that a new kind of ask would
    silently become invisible in every grouped view.
    """
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    for label, stable_id in sorted(T72_CASES.items()):
        brief = build_brief(real_vacancy(stable_id), real_kb, profile=profile)
        for assessment in brief.assessments:
            assert assessment.requirement.categories, (
                label,
                assessment.requirement.text,
            )
        for condition in brief.facts.conditions:
            assert jb.RequirementCategory.CONDITION in condition.categories


def test_categories_and_evidence_class_answer_different_questions() -> None:
    """A category is read from the vacancy; a class is read from the KB.

    The Ignition two-year requirement is DUTY + DURATION whatever the facts say,
    and it is C because of what the facts cannot say. A consumer that conflates
    the two would read "DUTY" as "confirmed duty".
    """
    assessment = find(ignition_brief(), "IT support experience")
    assert set(assessment.requirement.categories) == {
        jb.RequirementCategory.DUTY,
        jb.RequirementCategory.DURATION,
    }
    assert assessment.evidence_class is EvidenceClass.C_PARTIAL


# ---- 5. The manual fallback ------------------------------------------------


def test_a_manual_requirement_is_added_to_the_reading_and_assessed() -> None:
    """T7.2 section 5: verbatim requirement lines as a checked fallback.

    The automatic reading is not replaced -- the technologies, conditions and
    "not stated" lists stay as they were -- and the supplied line is read as a
    requirement, not as context, which is the whole point of supplying it.
    """
    manual = "Can work standard business hours in Eastern or Pacific time zones (we currently need both)"
    brief = build_brief(
        ignition_vacancy(manual),
        load_kb(None)[0],
        profile=jb.load_profile(None),
    )
    assessment = find(brief, manual[:40])
    assert assessment.requirement.mandatory
    assert assessment.evidence_class is EvidenceClass.E_UNCONFIRMED
    # Not filed as a condition any more: the operator asserted it as an ask.
    assert not [c for c in brief.facts.conditions if manual in c.text]


def test_the_fallback_cannot_invent_a_requirement_the_posting_does_not_state() -> None:
    """The check that makes it verifiable rather than a free-text override."""
    with pytest.raises(JobBriefError) as caught:
        build_brief(
            ignition_vacancy("Have 10 years of Kubernetes experience"),
            load_kb(None)[0],
            profile=jb.load_profile(None),
        )
    assert "не найдено в описании вакансии дословно" in str(caught.value)
    assert "Kubernetes" in str(caught.value)


def test_the_fallback_refuses_a_fragment_too_short_to_be_a_requirement() -> None:
    with pytest.raises(JobBriefError) as caught:
        build_brief(
            ignition_vacancy("macOS"),
            load_kb(None)[0],
            profile=jb.load_profile(None),
        )
    assert "слишком короткое" in str(caught.value)


def test_the_fallback_does_not_disturb_the_rest_of_the_reading() -> None:
    """Added, never substituted: everything else is byte-identical."""
    manual = "Have a quiet home workspace and reliable broadband"
    real_kb, _ = load_kb(None)
    profile = jb.load_profile(None)
    plain = build_brief(real_vacancy(IGNITION), real_kb, profile=profile)
    supplied = build_brief(ignition_vacancy(manual), real_kb, profile=profile)
    assert supplied.facts.technologies == plain.facts.technologies
    assert supplied.facts.unstated == plain.facts.unstated
    assert {c.text for c in supplied.facts.conditions} == (
        {c.text for c in plain.facts.conditions} - {manual}
    )


# ---- 6. The negative cases T7.2 section 6 lists ----------------------------


def test_windows_server_never_confirms_macos_support() -> None:
    """T7.2 section 2: a Windows estate is not an Apple one.

    The duty names Windows Server, Active Directory and SQL. It shares work-area
    words with an Apple requirement -- "support", "server", "infrastructure" --
    and none of that is macOS.
    """
    kb = build_kb(
        duties=(
            (
                "IT Infrastructure & Support | Acme",
                (
                    "Worked across Windows Server and Linux infrastructure, "
                    "Active Directory, Exchange, SQL, Hyper-V, networking and monitoring."
                ),
            ),
        ),
    )
    assessment = find(
        build_brief(
            vacancy(
                "You must have experience supporting macOS in an enterprise environment."
            ),
            kb,
        ),
        "macOS",
    )
    assert assessment.evidence_class is not EvidenceClass.A_CONFIRMED_EXPERIENCE
    assert assessment.evidence_class is not EvidenceClass.B_OWN_PROJECT


def test_an_ios_app_never_confirms_mac_support_for_a_managed_provider() -> None:
    """T7.2 section 2: shipping an iOS app is not running a Mac fleet.

    Swift and MapKit are Apple-ecosystem names, which is exactly why the
    temptation exists. They are not Jamf, not MDM and not help-desk work, and the
    requirement says so by naming what it wants.
    """
    kb = build_kb(
        projects=(
            (
                "Map Explorer",
                ("Shipped an iOS navigation app built with Swift and MapKit.",),
                "Swift, MapKit, iOS",
            ),
        ),
    )
    brief = build_brief(
        vacancy(
            "You must have at least 2 years of Mac support experience for managed "
            "service provider clients (Apple Business Manager, Jamf, MDM)."
        ),
        kb,
    )
    assessment = find(brief, "Mac support")
    assert assessment.evidence_class is not EvidenceClass.A_CONFIRMED_EXPERIENCE
    assert assessment.evidence_class is not EvidenceClass.B_OWN_PROJECT
    assert set(assessment.requirement.unrecognised) >= {"Apple Business Manager", "Jamf", "MDM"}


def test_the_total_it_tenure_never_answers_a_product_specific_threshold() -> None:
    """T7.2 section 3: 15 years of IT is not three years of one product.

    The duty is real paid work and it names SQL -- so it is offered, as partial
    evidence -- and it says nothing about three years of PostgreSQL. Both halves
    of the answer have to be on the line: the SQL that was used, and the
    PostgreSQL that was not.
    """
    kb = build_kb(
        duties=(
            (
                "IT Infrastructure & Support | Acme",
                "Worked across Windows Server and Linux infrastructure, SQL and monitoring.",
            ),
        ),
        skills=("SQL",),
    )
    assessment = find(
        build_brief(vacancy("You must have 3+ years of commercial SQL and PostgreSQL experience."), kb),
        "PostgreSQL",
    )
    assert assessment.evidence_class is EvidenceClass.C_PARTIAL
    assert assessment.commercial_evidence, "the SQL duty must still be offered"
    # The duty really is the SQL one and it really does not name PostgreSQL.
    vocabulary = jb._kb_vocabulary(kb)
    assert all(
        "postgresql" not in jb._canonical_terms_in(item.value, vocabulary)
        for item in assessment.commercial_evidence
    )
    # And the three years are not filled in from anywhere.
    assert jb.APPROVED_TOTAL_EXPERIENCE not in assessment.explanation
    assert "называет срок 3+ лет" in assessment.explanation
    assert "не измеряется" in assessment.explanation


def test_the_cli_flag_carries_a_manual_requirement_into_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The flag and its dispatch line, exercised end to end.

    Both were added to ``cli.py`` by byte surgery, so a pin that only called
    :func:`candidate_job_brief_cmd` would not notice either of them being dropped
    on the way in. The description here is the shape the fallback exists for: the
    requirement is glued to a compensation clause, so the automatic pass files
    the whole segment as context and the ask never reaches the brief.
    """
    from ai_assistant import cli

    manual = "Have at least 2 years of IT support experience on the job"
    text_file = tmp_path / "vacancy.txt"
    # One sentence on purpose: the requirement is glued to a compensation clause,
    # so the automatic pass files the whole segment as context -- which is the
    # shape this fallback exists for.
    text_file.write_text(f"Base salary is $50,000 per year, and {manual}", encoding="utf-8")
    base = ["cli.py", "candidate-job-brief", "--file", str(text_file)]

    monkeypatch.setattr(sys, "argv", list(base))
    assert cli.main() == 0
    assert manual not in capsys.readouterr().out, "the automatic pass swallows it"

    monkeypatch.setattr(sys, "argv", [*base, "--requirement", manual])
    assert cli.main() == 0
    assert manual in capsys.readouterr().out


def test_the_cli_reports_a_manual_requirement_the_posting_does_not_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_assistant import cli

    text_file = tmp_path / "vacancy.txt"
    text_file.write_text("Base salary: $50,000 per year. Have a car.", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cli.py",
            "candidate-job-brief",
            "--file",
            str(text_file),
            "--requirement",
            "Have 10 years of Kubernetes experience",
        ],
    )
    assert cli.main() == 2
    assert "не найдено в описании вакансии дословно" in capsys.readouterr().out
