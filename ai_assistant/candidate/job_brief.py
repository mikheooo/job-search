"""Job-to-Candidate Brief (Stage 0 / T6).

WHAT THIS IS
------------
A brief answers one question: *for this vacancy, what can the candidate actually
support, and what must not be claimed?* It is an analysis layer. It never edits
the resume, never scores for submission and never sends anything.

WHAT IT REUSES (and deliberately does not rebuild)
-------------------------------------------------
* ``candidate/kb.py`` -- the confirmed-facts knowledge base. T6 reads it; it is
  the only candidate source this module trusts. It does **not** build a second
  candidate store, and it does not read the generated master-resume drafts as
  if they were approved public text (the four T5.2 editorial edits are still
  unapplied -- see :func:`t5_2_pending_note`).
* ``job_analyzer.MUST_HAVE_MARKERS`` / ``NICE_TO_HAVE_MARKERS`` -- the one
  vocabulary for the must/nice split, shared with the existing deep analyzer.
* ``matcher.extract_seniority`` / ``matcher.extract_required_years`` -- seniority
  and years-of-experience extraction, already written and tested.
* ``remote_filter.classify_work_format`` -- work-format classification.
* ``master_resume.load_kb`` -- KB loading with the same path resolution the rest
  of the pipeline uses.

There is no second matcher here. ``matcher.JobMatcher`` scores a vacancy against
the *profile* and decides whether to pursue it; that stays the only scorer. T6
answers a different question -- which individual requirements have evidence --
and it reports, it does not decide.

WHY IT IS DETERMINISTIC
-----------------------
No LLM is called. Two reasons: the project rules forbid paid APIs without
explicit permission, and an evidence trail that a model generated is not an
evidence trail. Every claim in the output carries a ``claim_id``, a status and a
file-and-line, so it can be checked by hand. ``job_analyzer.analyze_job_deep``
remains the LLM path for scoring; T6 does not duplicate it.

WHY IT SEGMENTS TEXT ITSELF
---------------------------
The stored description of every vacancy in ``state.db`` is a **single line** --
zero newlines (measured: 5293 chars, 0 newlines for one weworkremotely row).
``_fallback_analysis`` splits requirements with ``description.splitlines()``, so
on real data it sees one giant line and falls through to "first three sentences",
which invents requirements. T6 therefore segments on sentence and bullet
boundaries. It shares the *vocabulary* with the analyzer but not that broken
line split, and it never falls back to guessing: a vacancy with no requirement
markers is reported as "не указано", not filled in.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any

from ..job_analyzer import MUST_HAVE_MARKERS, NICE_TO_HAVE_MARKERS
from .kb import CandidateKnowledgeBase, ClaimStatus, is_project_sourced

__all__ = [
    "CATEGORY_LABELS",
    "Assessment",
    "Brief",
    "Conflict",
    "Evidence",
    "EvidenceClass",
    "JobBriefError",
    "PreferenceSet",
    "ProfessionalEquivalent",
    "Requirement",
    "RequirementCategory",
    "RequirementKind",
    "Tenure",
    "VacancyFacts",
    "VacancyInput",
    "build_brief",
    "candidate_job_brief_cmd",
    "render_brief_details",
    "render_brief_json",
    "render_brief_short",
    "t5_2_pending_note",
]


class JobBriefError(RuntimeError):
    """Raised when the brief cannot be built at all (bad input, missing KB)."""


# ----------------------------------------------------------------------
# Vocabulary
# ----------------------------------------------------------------------

#: A vacancy requirement is not one thing. The distinction is the whole point of
#: the brief: "5 years of commercial Python" and "writes Python in his own time"
#: are satisfied by completely different evidence, and treating them alike is how
#: a personal project gets sold as professional experience.
class RequirementKind(StrEnum):
    TOOL_USE = "TOOL_USE"
    COMMERCIAL_EXPERIENCE = "COMMERCIAL_EXPERIENCE"
    SELF_DIRECTED = "SELF_DIRECTED"
    GENERAL = "GENERAL"


#: What *kind of ask* a requirement is, read off the vacancy alone.
#:
#: ``RequirementKind`` above answers "which evidence would settle this?", and it
#: can only be answered by looking at the knowledge base. This one answers "what
#: is being asked for?", and it must be answerable with no KB at all -- T7.2,
#: section 4: a requirement the candidate has no claim for is still a requirement,
#: and it may not be filed under "не указано" merely because the KB cannot name
#: it. The two axes are independent on purpose: "at least 2 years of IT support"
#: is a DURATION and a DUTY at once, and flattening it to one of them is how the
#: two-years threshold disappeared from the Ignition brief.
class RequirementCategory(StrEnum):
    TECHNOLOGY = "TECHNOLOGY"
    DUTY = "DUTY"
    CONDITION = "CONDITION"
    DURATION = "DURATION"
    CERTIFICATE = "CERTIFICATE"


CATEGORY_LABELS: dict[str, str] = {
    RequirementCategory.TECHNOLOGY: "технология",
    RequirementCategory.DUTY: "обязанность",
    RequirementCategory.CONDITION: "условие трудоустройства",
    RequirementCategory.DURATION: "длительность опыта",
    RequirementCategory.CERTIFICATE: "сертификат / квалификация",
}


class EvidenceClass(StrEnum):
    """The five classes of the T6 specification, section 3."""

    A_CONFIRMED_EXPERIENCE = "A"
    B_OWN_PROJECT = "B"
    C_PARTIAL = "C"
    D_NEEDS_CLARIFICATION = "D"
    E_UNCONFIRMED = "E"


EVIDENCE_LABELS: dict[str, str] = {
    EvidenceClass.A_CONFIRMED_EXPERIENCE: "A — подтверждено опытом работы",
    EvidenceClass.B_OWN_PROJECT: "B — подтверждается собственным проектом",
    EvidenceClass.C_PARTIAL: "C — частично соответствует",
    EvidenceClass.D_NEEDS_CLARIFICATION: "D — есть в резюме, требует уточнения",
    EvidenceClass.E_UNCONFIRMED: "E — нет подтверждения в источниках",
}

#: Printed beside every E-class line. The spec is explicit that an empty search
#: is not proof of absence, and this sentence is what keeps the output honest.
NO_EVIDENCE_CAVEAT = (
    "Отсутствие подтверждения в доступных источниках — не доказательство "
    "отсутствия навыка."
)

#: The approved way to state the candidate's total experience.
#:
#: The user approved *«15+ лет опыта в IT»* on 2026-09-23 (T6.1). Three other
#: figures exist and none of them may be published:
#:
#: * ``years_experience = 3`` in the profile -- the meaning of this field is not
#:   established, so it is neither a career total nor a specialism total;
#: * ``15.3`` / ``14.9`` computed from the KB chronology -- an *estimate* built
#:   from month-precision role dates, never an approved statement, and not
#:   precise enough to publish (year-only dates move it by up to a year);
#: * ``11.0`` in the profile -- a different claim again.
#:
#: The KB carries the same wording as ``experience_statement``
#: (``"15+ years of IT experience"``), but its status is still
#: ``DERIVED_FROM_SOURCE``: T6.1 is forbidden from changing fact statuses, so the
#: approval is recorded here instead. If that claim is ever promoted to
#: ``CONFIRMED_BY_USER``, this constant should be replaced by a read of it.
APPROVED_TOTAL_EXPERIENCE = "Общий опыт в IT: 15+ лет — подтверждено пользователем"

#: Claim kinds that describe a *task actually performed for an employer*. They
#: are the only ones that may be called commercial experience, because they are
#: the only ones the KB attaches to a role.
_ROLE_LINKED_KINDS = frozenset({"duty", "role", "role_title", "role_company", "role_employer"})

#: Claim kinds produced by the project catalogue (T5).
_PROJECT_KINDS = frozenset(
    {"project", "project_bullet", "project_summary", "project_stack", "project_status"}
)

#: Claim kinds that are a self-description rather than an experience record.
_SELF_DESCRIPTION_KINDS = frozenset({"skill", "skill_group", "headline", "summary"})

#: Claim kinds that must never reach an application. ``project_status`` carries
#: lines like "STAGE_1_FINAL_NO_GO" -- a candid note to the author, not a selling
#: point, and printing it in a response would be actively harmful.
_NEVER_CLAIM_KINDS = frozenset({"project_status", "conflict_notes"})

#: Claim kinds that may stand as *evidence* for a requirement.
#:
#: An allowlist rather than "everything except the forbidden", because the KB
#: holds many claims that are not evidence at all. A first draft let them
#: through and the brief cited "Bitrix24: рассматривать наравне с другими
#: вакансиями, не исключать" -- the candidate's own *preference* -- as proof of
#: Bitrix24 experience. A preference is a wish, not a qualification; so are
#: contact details, education dates, languages and chronology notes.
_EVIDENCE_KINDS: frozenset[str] = frozenset({
    # what was actually done for an employer
    "duty", "role", "role_title", "role_company", "role_employer",
    # what was built independently
    "project", "project_bullet", "project_summary", "project_stack",
    # what the candidate says about themselves
    "skill", "skill_group", "headline", "summary",
})

_MUST_RE = re.compile("|".join(re.escape(m) for m in MUST_HAVE_MARKERS), re.IGNORECASE)
_NICE_RE = re.compile("|".join(re.escape(m) for m in NICE_TO_HAVE_MARKERS), re.IGNORECASE)

_COMMERCIAL_RE = re.compile(
    r"коммерческ|commercial|\d+\s*\+?\s*(?:years?|лет|года|год)\s+(?:of\s+)?"
    r"(?:experience|опыт)|опыт\s+работы|work\s+experience|стаж|в\s+компании|"
    r"production\s+experience|paid\s+(?:work|experience)",
    re.IGNORECASE,
)
_SELF_DIRECTED_RE = re.compile(
    r"самостоятельн|собственн\w*\s+проект|личн\w*\s+проект|pet\s*-?project|"
    r"side\s+project|own\s+project|self-?taught|пет-?проект|инициатив|"
    r"personal\s+project|portfolio",
    re.IGNORECASE,
)
_SENIORITY_ONLY_RE = re.compile(r"^(?:senior|middle|mid|junior|lead|principal|стажёр|стажер)\b", re.IGNORECASE)

#: A tag list is a run of ``#tag`` tokens separated by commas -- the shape
#: habr-career postings use for their whole requirement set.
#:
#: Measured need: ``Требуемые навыки: #middle, #Битрикс24, #JavaScript, #PHP,
#: #MySQL, #CI/CD, #GitLab, #RabbitMQ, #Jira, #Docker`` arrived as a *single*
#: segment. One match (Docker, from a real infrastructure duty) marked the whole
#: blob as confirmed by commercial experience, so the brief silently claimed
#: PHP, JavaScript, MySQL and RabbitMQ along with it. Ten technologies are ten
#: requirements, and each has to answer for itself.
_TAG_RE = re.compile(r"#([\w][\w.+#/-]*)", re.UNICODE)

#: How many tags a segment needs before it is treated as a tag list rather than
#: prose that happens to mention a couple of hashtags.
_MIN_TAG_LIST = 3

#: Tags that name a grade, not a skill. Postings mix them into the same list
#: ("#middle, #Битрикс24, #PHP"); the grade is already reported as seniority, so
#: expanding it into its own requirement would only add a line of noise that no
#: candidate can act on.
_GRADE_TAGS: frozenset[str] = frozenset({
    "junior", "middle", "mid", "middle+", "senior", "lead", "principal",
    "intern", "trainee", "стажер", "стажёр", "джун", "мидл", "сеньор",
})

#: Words that mark a segment as a task/duty statement rather than a requirement.
_DUTY_RE = re.compile(
    r"\b(?:develop|build|design|maintain|administer|support|troubleshoot|manage|"
    r"implement|integrate|automate|create|provide|ensure|разработ|созда|поддерж|"
    r"обеспеч|настра|внедр|автоматизир|обслужива|участие|работа\s+с)\w*",
    re.IGNORECASE,
)

#: Section headers introduce a list whose items need not repeat the keyword.
_SECTION_MUST_RE = re.compile(
    r"(?:требования|обязательн|requirements|must[\s-]?have|required|qualifications)",
    re.IGNORECASE,
)
_SECTION_NICE_RE = re.compile(
    r"(?:желательн|будет\s+плюсом|nice[\s-]?to[\s-]?have|preferred|plus(?:es)?|bonus)",
    re.IGNORECASE,
)

#: Text that talks about the *offer* or the *process*, not about what the
#: candidate must be able to do.
#:
#: Measured need: a real We Work Remotely description carries "a matching 3%
#: company contribution", "Answering phones and chats will be the primary
#: priority", "We run a three-round interview process" and "Attach your resume
#: and cover letter to an email". Every one of those contains a word like
#: *required* or *support*, so the marker test alone promoted them to
#: requirements and the brief dutifully reported them as unconfirmed candidate
#: skills. They belong in the duties/context bucket instead.
#:
#: Two tiers, because the exceptions differ. **Hard** noise is company blurb and
#: hiring logistics -- it is never a requirement, even when it name-drops a
#: technology. **Soft** noise is compensation and schedule, which is real
#: information about the offer but still not a skill, so it is dropped only when
#: the segment names no technology.
_HARD_NOISE_RE = re.compile(
    r"\b(?:interview|interviews|references?|round\s+\d|onboarding|"
    r"how\s+to\s+apply|to\s+apply|cover\s+letter|subject\s+line|"
    r"application\s+(?:process|form)|recruit\w*|hr\s+team|"
    r"trustpilot|founded|our\s+(?:team|mission|story|values|culture|company)|"
    r"we\s+(?:are|were|have\s+become|believe|think|offer|provide|seek|look)|"
    r"customer\s+base|industry[\s-]leading|award[\s-]winning|"
    r"headquarters|about\s+us|company\s+overview|"
    # Russian postings put the company blurb in the same paragraph as the
    # requirements. Without these, "Компания «ТехноГрупп» ищет хорошего
    # специалиста на вакансию ..." was reported as a requirement.
    r"о\s+компании|мы\s+(?:ищем|предлагаем|рады)|"
    r"ищет\s+(?:хорошего\s+)?(?:специалиста|сотрудника|разработчика)|"
    r"условия\s+работы|наша\s+команда|кто\s+мы)\b"
    # The Ignition blurb, and the shapes it generalises to. Measured: with only
    # the patterns above, "Apple-first TL;DR ignitionit.com is a seasoned,
    # Apple-first MSP -- in business for almost 30 years -- and we're hiring a
    # Help Desk Technician ..." was reported as a *confirmed* requirement, because
    # once the professional equivalences exist "Help Desk" is a phrase a duty can
    # answer, and an advertisement is not an ask.
    r"|\babout\s+[\w-]+\.[a-z]{2,}\b"
    r"|\bwe['’]re\s+hiring\b"
    r"|\bin\s+business\s+for\b"
    r"|\bone\s+of\s+the\s+(?:oldest|largest|leading|few|only)\b"
    r"|\bcertified\s+\w+\s+(?:partners?|resellers?|integrators?)\b"
    r"|\bwe['’]ll\s+reach\s+out\b"
    r"|\b(?:culture\s+index|testgorilla|online|video)\s+assessment\b"
    # Employer-side promises of *future* growth. Measured: "As you grow, there's
    # a clear path into infrastructure administration, project architecture and
    # implementation ..." was the only class-A item in the Ignition brief, so the
    # brief read as though it held a confirmed match -- and what it had confirmed
    # was the employer's own career pitch. A path the candidate is offered later
    # is not an ask made now, and "infrastructure" resolving to a real KB term is
    # exactly why the technology test alone cannot be trusted to decide.
    r"|\bas\s+you\s+grow\b"
    r"|\bclear\s+path\s+(?:into|to|towards?|for)\b"
    r"|\bcareer\s+(?:path|growth|progression|development|opportunit\w*)\b"
    r"|\bgrowth\s+opportunit\w*\b"
    r"|\broom\s+to\s+grow\b",
    re.IGNORECASE,
)
_SOFT_NOISE_RE = re.compile(
    r"\b(?:benefits?|medical|dental|vision|insurance|ira|401\s*\(?k\)?|pto|"
    r"paid\s+(?:time|breaks?|vacation|holidays?)|vacation|holidays?|bonus|equity|"
    r"compensation|salary|stipend|allowance|premium|discount|"
    r"shift|shifts|schedule|rota|roster|weekend|weekends|saturday|sunday|"
    r"monday|tuesday|wednesday|thursday|friday|pacific\s+time|eastern\s+time|"
    r"time\s*zone|full[\s-]?time\s+hours|working\s+hours)\b",
    re.IGNORECASE,
)

#: Wording that makes a sentence a requirement about the candidate even when it
#: never says "must". Real postings ask "Do you have two or more years of live
#: technical support experience?" and state "experience with domains, DNS,
#: hosting" -- both are requirements, and the marker test alone would file them
#: as duties and lose them.
_REQUIREMENT_SHAPE_RE = re.compile(
    r"\bdo\s+you\s+have\b|\b(?:proficiency|proficient|familiarity|familiar)\b|"
    r"\bknowledge\s+of\b|\bability\s+to\b|\bexperience\s+(?:with|in|using|of)\b|"
    r"\b\d+\s*\+?\s*(?:years?|лет|года|год)\b|\b(?:certification|certified)\b|"
    r"\bвладение\b|\bопыт\s+(?:работы\s+)?с\b|\bтребуется\b",
    re.IGNORECASE,
)

#: Qualification language: a certificate, a credential, a degree, or a
#: demonstrated aptitude. Its own category because "security work" and "a
#: security certificate" are different asks, and T6.2 established that the first
#: must never answer the second.
_CERTIFICATE_RE = re.compile(
    r"\b(?:certificat\w*|certified|credential|licen[cs]e[ds]?|qualification\w*|"
    r"qualified|accredit\w*|degree|diploma|aptitude|"
    r"сертификат\w*|квалификац\w*|аттестац\w*|диплом\w*|образование)\b",
    re.IGNORECASE,
)

#: Employment conditions: schedule, time zone, place of residence, right to
#: work, employment type, and the home-office requirements that come with them.
#:
#: Its own bucket rather than a requirement, because these are not candidate
#: skills and must never be matched against the knowledge base as if they were --
#: T7.2 section 5: "условия зарплаты и рабочего графика не считаются навыками
#: кандидата". They are still *reported*, in their own block, because a brief that
#: silently drops "standard business hours in Eastern or Pacific time zones" is
#: worse than useless: it is the condition the whole application depends on.
#:
#: Deliberately narrow, and deliberately not about pay. Benefits and salary stay
#: where they were -- in the context bucket and in :func:`extract_facts`' own
#: salary handling -- because the lesson of the earlier draft is that a benefit
#: clause which merely contains a marker word must not become an ask.
#:
#: ``shift`` is matched as a noun and not as a verb, which is a measured fix
#: rather than a stylistic one: the bare ``shifts?`` this started as matched
#: "our priorities **shift**, it's not always documented", so Ignition's culture
#: paragraph -- a company description with no employment terms in it at all --
#: was reported as a mandatory condition of the job. The plural covers the
#: postings that name shifts in a list ("Available Shifts: Saturday"), and the
#: singular is only read as a schedule when a qualifier makes it one.
_CONDITION_RE = re.compile(
    r"\b(?:time\s*zones?|(?:eastern|pacific|central|mountain|greenwich)\s+time|"
    r"gmt|utc|business\s+hours|working\s+hours|work\s+hours|office\s+hours|"
    r"shifts|(?:night|day|morning|evening|weekend|rotating|split|late|early)"
    r"\s+shift|schedule|rota|roster|on[\s-]?call|"
    r"full[\s-]?time|part[\s-]?time|permanent|temporary|contract(?:or)?|"
    r"employment\s+type|work\s+from\s+home|on[\s-]?site|hybrid|"
    r"quiet\s+(?:home\s+)?workspace|home\s+office|broadband|"
    r"reliable\s+internet|internet\s+connection|"
    r"work\s+authoris\w+|work\s+authoriz\w+|eligible\s+for\s+employment|"
    r"employment\s+eligibility|citizenship|right\s+to\s+work|work\s+permit|"
    r"visa|based\s+in|located\s+in|"
    r"график|смен\w*|часов\w*\s+пояса|полная\s+занятость|частичная\s+занятость|"
    r"удалённ\w*|удаленн\w*|гражданств\w*|разрешение\s+на\s+работу)\b",
    re.IGNORECASE,
)

#: Technology aliases. Deliberately short and explicit: every entry here changes
#: what the brief will assert, so a guess does not belong in this table.
_ALIASES: dict[str, str] = {
    "postgres": "postgresql",
    "psql": "postgresql",
    "mssql": "ms sql",
    "golang": "go",
    "js": "javascript",
    "ts": "typescript",
    "k8s": "kubernetes",
    "ad": "active directory",
    "битрикс": "bitrix24",
    "битрикс24": "bitrix24",
    "питон": "python",
    "докер": "docker",
    "линукс": "linux",
    "телеграм": "telegram",
}

_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11,
    "декабрь": 12,
}

_PRESENT_RE = re.compile(r"present|now|настоящее|по\s+настоящее|текущ", re.IGNORECASE)


#: Pure function words: articles, prepositions, pronouns, conjunctions,
#: auxiliaries. Nothing here can ever name a skill or a work area.
_FUNCTION_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "as", "at", "by", "for", "from",
    "in", "into", "of", "on", "onto", "to", "up", "with", "within", "without",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "have", "has", "had", "will", "would", "can", "could", "should", "shall",
    "may", "might", "must", "not", "no", "yes", "so", "than", "then", "there",
    "here", "when", "where", "which", "while", "who", "whom", "why", "how",
    "what", "this", "that", "these", "those", "you", "your", "yours", "our",
    "ours", "we", "us", "their", "theirs", "they", "them", "its", "it", "his",
    "her", "hers", "him", "she", "he", "all", "any", "each", "other", "more",
    "most", "some", "such", "only", "also", "very", "just", "both", "own",
    "out", "over", "under", "about", "above", "below", "after", "before",
    # Russian
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "тогда", "кто", "этот", "того", "потому", "этого",
    "какой", "совсем", "ним", "здесь", "этом", "один", "почти", "мой", "тем",
    "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех", "никогда",
    "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над",
    "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая",
    "много", "разве", "три", "эту", "моя", "впрочем", "хорошо", "свою", "этой",
    "перед", "иногда", "лучше", "чуть", "том", "нельзя", "такой", "им", "более",
    "всегда", "конечно", "всю", "между",
})

#: Generic business prose: words that appear in every posting and describe no
#: particular skill. Dropped when comparing *technologies*, because a shared
#: "with" or "web" is a coincidence.
#:
#: Deliberately **not** dropped when harvesting work areas from duties. "support"
#: and "technical" live here, and they are exactly what an IT-support vacancy
#: asks for and exactly what the candidate's Avantis duty records -- filtering
#: them would make the brief blind to the candidate's own strongest category.
_GENERIC_BUSINESS_WORDS: frozenset[str] = frozenset({
    "job", "jobs", "role", "roles", "position", "company", "team", "teams",
    "work", "working", "works", "worker", "candidate", "applicant", "applicants",
    "experience", "experienced", "years", "year", "month", "months", "time",
    "full", "part", "remote", "office", "hybrid", "based", "level", "senior",
    "junior", "middle", "lead", "staff", "head", "skill", "skills", "skilled",
    "ability", "able", "knowledge", "understanding", "strong", "good", "great",
    "excellent", "proven", "solid", "deep", "broad", "plus", "nice", "bonus",
    "required", "require", "requires", "requirement", "requirements", "preferred",
    "responsibility", "responsibilities", "duty", "duties", "task", "tasks",
    "looking", "seeking", "hiring", "join", "help", "support", "provide",
    "providing", "ensure", "ensuring", "including", "include", "includes",
    "across", "using", "use", "used", "new", "well", "like", "want", "need",
    "needs", "love", "loves", "enjoy", "enjoys", "developer", "engineer",
    "engineers", "manager", "specialist", "analyst", "administrator", "expert",
    "professional", "business", "customer", "customers", "client", "clients",
    "product", "products", "service", "services", "system", "systems", "tool",
    "tools", "platform", "solution", "solutions", "process", "processes",
    "project", "projects", "web", "website", "online", "email", "phone", "chat",
    "call", "calls", "source", "sources", "claim", "claims", "fact", "facts",
    "check", "checks", "control", "controls", "against", "data", "content",
    "information", "report", "reports", "reporting", "review", "reviews",
    "search", "master", "listen", "repeat", "explain", "retry", "test", "tests",
    "code", "app", "apps", "application", "applications", "software", "technical",
    "technology", "technologies", "digital", "best", "top", "high", "low",
    "large", "small", "quick", "fast", "easy", "simple", "complex", "modern",
    "daily", "weekly", "annual", "day", "days", "week", "weeks", "hour", "hours",
    "am", "pm", "etc", "via", "per", "one", "two", "three", "first", "second",
    "third", "next", "last", "many", "much", "several", "multiple", "various",
    # Russian
    "опыт", "работа", "работы", "работе", "требования", "требование",
    "обязанности", "навыки", "навык", "знание", "знания", "умение", "компания",
    "компании", "проект", "проекты", "система", "системы", "поддержка",
    "разработка", "разработчик", "инженер", "специалист", "сотрудник",
    "кандидат", "вакансия", "позиция", "должность", "задачи",
})

#: Words dropped before comparing technologies (both sides).
_STOPWORDS: frozenset[str] = _FUNCTION_WORDS | _GENERIC_BUSINESS_WORDS

#: Claim kinds that actually name a technology. The prose kinds -- ``headline``,
#: ``summary``, ``project_summary``, ``project_bullet`` -- are deliberately
#: absent: a sentence is not a technology. Registering the headline once made
#: every requirement containing the word *python* resolve to the sentence
#: "ai engineer ai automation engineer python developer".
#:
#: ``role_title`` is included because it is where a real technology lives when
#: the resume never lists it as a skill: the candidate's Bitrix24 experience
#: appears only in the role "Bitrix24 Technical Support Specialist" and in
#: duties. Without it, the Bitrix24 category the task requires found nothing.
_TECHNOLOGY_KINDS: frozenset[str] = frozenset({"skill", "project_stack", "role_title"})

#: Tokens that appear inside a real technology name but are too ordinary to
#: identify one on their own. ``Make`` is a genuine automation platform and also
#: the commonest English verb; a vacancy saying "make sure to document your work"
#: must not be recorded as a match for it.
_GENERIC_TECH_TOKENS: frozenset[str] = frozenset({
    "open", "make", "rest", "bot", "compose", "ms", "source", "sources",
    "engineering", "generation", "checking", "content", "workspace", "prompt",
    # "automation" arrives from the role title "AI Automation Engineer". Left in,
    # it made "Active UiPath Certification: hold a current, active UiPath
    # certification" match an AI-automation duty and be reported as confirmed
    # experience -- the candidate has never touched UiPath. A word that appears
    # in half the postings in this field identifies nothing.
    "automation",
})


def normalize(text: str) -> str:
    """Lower-case and reduce to single-spaced alphanumerics (Cyrillic kept)."""
    lowered = text.lower().replace("ё", "е")
    cleaned = re.sub(r"[^0-9a-zа-я+.#\s]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def _tokens(text: str, stopwords: frozenset[str] = _STOPWORDS) -> list[str]:
    """Significant words of ``text``: punctuation stripped, stopwords removed.

    ``normalize`` deliberately keeps ``+ # .`` so that ``c++``, ``c#`` and
    ``.net`` survive; the edges are trimmed here so a sentence-final
    ``controls.`` cannot become a token distinct from ``controls``.

    ``stopwords`` is a parameter because two callers need different strictness:
    technology comparison drops generic business prose, while harvesting work
    areas from duties keeps it (see :data:`_GENERIC_BUSINESS_WORDS`).
    """
    out: list[str] = []
    for raw in normalize(text).split():
        token = raw.strip("+.#")
        if len(token) < 2 or token in stopwords:
            continue
        out.append(token)
    return out


def _tokens_loose(text: str) -> list[str]:
    """Tokens with only function words removed -- used to read duty claims."""
    return _tokens(text, _FUNCTION_WORDS)


# ----------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------


@dataclass
class VacancyInput:
    """A vacancy as T6 needs it, decoupled from the DB row shape."""

    title: str
    description: str = ""
    company: str = ""
    location: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    employment_type: str | None = None
    source: str = ""
    stable_id: str = ""
    job_url: str = ""
    #: Requirement texts supplied by hand, copied verbatim out of the posting,
    #: for a description the splitter cannot read reliably (T7.2 section 5).
    #: *Added to* the automatic reading rather than replacing it, so the
    #: technologies, conditions and "not stated" lists are unaffected, and every
    #: entry is checked against ``description`` -- see
    #: :func:`_validated_manual_requirements` -- so the fallback cannot introduce
    #: an ask the employer never made. Empty by default.
    manual_requirements: tuple[str, ...] = ()
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Requirement:
    text: str
    kind: RequirementKind
    mandatory: bool
    technologies: tuple[str, ...] = ()
    #: Names the requirement uses that no knowledge-base fact can answer, in the
    #: posting's own spelling. Empty for most requirements. Kept on the
    #: requirement rather than recomputed at render time because it is part of
    #: reading the vacancy: the extractor has to see the names the candidate has
    #: no claim for, or they are invisible everywhere downstream.
    unrecognised: tuple[str, ...] = ()
    #: True when the requirement offers alternatives ("Python or PowerShell")
    #: rather than a conjunction ("SQL and PostgreSQL"). One alternative answers
    #: the first; the second needs all of them.
    disjunctive: bool = False
    #: Every kind of ask the requirement makes, read off the vacancy alone
    #: (:class:`RequirementCategory`). Empty only for a ``Requirement`` built by
    #: hand in a test; the extractor always fills it.
    categories: tuple[RequirementCategory, ...] = ()

    @property
    def category_labels(self) -> tuple[str, ...]:
        return tuple(CATEGORY_LABELS[item] for item in self.categories)


@dataclass
class Evidence:
    """One KB claim offered as support for a requirement.

    ``origin`` says where the fact came from, and it is what decides whether the
    claim may be called commercial experience:

    * ``"resume"`` -- a claim of a role that was paid employment;
    * ``"own_work"`` -- a claim of a role the candidate has declared to be his
      own projects with no paid orders (T6.2: the Freelance block);
    * ``"project"`` -- a fact anchored in one of the candidate's repositories;
    * ``"interview"`` -- something the candidate confirmed out loud.

    ``role_linked`` is therefore necessary but not sufficient: it says a role
    produced the claim, and a role may be unpaid. :func:`_evidence_for` subtracts
    the roles the candidate has declared, so a personal project cannot become
    commercial experience through its ``role_id``, its role title or the heading
    it happens to sit under in ``resume.md``.
    """

    claim_id: str
    claim_kind: str
    value: str
    status: str
    origin: str  # "resume" | "own_work" | "project" | "interview"
    where: str  # employer role title / project name / "резюме"
    ref: str  # file:line, project path:line, or interview
    note: str = ""
    role_linked: bool = False

    @property
    def is_commercial(self) -> bool:
        """True only for a claim the KB ties to a *paid* employer's role."""
        return self.origin == "resume" and self.role_linked

    @property
    def is_own_work(self) -> bool:
        """True for the candidate's own unpaid work, from either source."""
        return self.origin in ("own_work", "project")


@dataclass
class Assessment:
    requirement: Requirement
    evidence_class: EvidenceClass
    evidence: list[Evidence] = field(default_factory=list)
    explanation: str = ""

    # The three buckets below are what keep a personal project out of the
    # "confirmed by work experience" heading. An earlier draft printed the first
    # two evidence lines whatever they were, so a Reels Analyzer bullet appeared
    # under a commercial heading whenever the project named more technologies
    # than the duty did.

    @property
    def commercial_evidence(self) -> list[Evidence]:
        """Claims the KB ties to a paid employer. The only kind that is paid work."""
        return [item for item in self.evidence if item.is_commercial]

    @property
    def project_evidence(self) -> list[Evidence]:
        """The candidate's own work -- never paid work.

        Both flavours count: a fact anchored in a repository, and a duty of a
        role the candidate has declared to be his own projects. They are the same
        thing to a reader -- work nobody paid for -- so the report groups them,
        and neither can be promoted into the commercial bucket.
        """
        return [item for item in self.evidence if item.is_own_work]

    @property
    def resume_evidence(self) -> list[Evidence]:
        """Claims that only assert what the resume says, with no role behind them."""
        commercial = {id(item) for item in self.commercial_evidence}
        return [
            item
            for item in self.evidence
            if id(item) not in commercial and not item.is_own_work
        ]


@dataclass
class VacancyFacts:
    title: str
    employer: str
    must_have: list[Requirement] = field(default_factory=list)
    nice_to_have: list[Requirement] = field(default_factory=list)
    #: Mandatory *employment conditions* -- schedule, time zone, residence, right
    #: to work, employment type, home office. Reported, but never matched against
    #: the knowledge base and never given an evidence class: they are terms of the
    #: job, not skills, and filing them as unconfirmed skills is what made the
    #: Ignition phone view list "Have a quiet home workspace" as a missing ability.
    conditions: list[Requirement] = field(default_factory=list)
    duties: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    commercial_years_required: int | None = None
    seniority: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    work_format: str = "не указано"
    #: The classifier's own wording, kept for the detailed view only. It reads
    #: like "Rejected non-remote pattern: 'Можно удаленно'" -- accurate, but
    #: internal jargon that has no business on a phone screen.
    work_format_reason: str = ""
    remote: bool | None = None
    location: str = "не указано"
    salary_text: str = "не указано"
    salary_meets_minimum: bool | None = None
    source: str = ""
    stable_id: str = ""
    url: str = ""
    unstated: list[str] = field(default_factory=list)


@dataclass
class PreferenceSet:
    """The five preferences the candidate confirmed in the T4.5 interview."""

    min_income_usd: int | None = None
    priority: str = ""
    bitrix24_on_par: bool = False
    own_projects_since: str = ""
    own_projects_unpaid: bool = False
    chronology_note: str = ""
    claim_ids: dict[str, str] = field(default_factory=dict)


@dataclass
class Tenure:
    """IT tenure computed from the KB chronology, never from ``years_experience``.

    The computed figures exist for *internal* gates -- class A needs to know
    that some paid work exists -- and for the diagnostic view. They are not
    publishable statements: they come from month-precision arithmetic over
    year-only dates. What may be published is
    :data:`APPROVED_TOTAL_EXPERIENCE`, and
    :meth:`publishable` is the only thing the brief prints as a fact.
    """

    earliest_year: int | None = None
    total_it_years: float = 0.0
    commercial_years: float = 0.0
    unpaid_years: float = 0.0
    intervals: list[str] = field(default_factory=list)
    profile_years: int | None = None
    profile_is_total: bool = False
    method: str = ""
    note: str = ""

    @property
    def has_paid_work(self) -> bool:
        """Whether the chronology shows any paid IT work at all."""
        return self.commercial_years > 0

    def publishable(self) -> str:
        """The approved tenure statement, and nothing else."""
        return APPROVED_TOTAL_EXPERIENCE


@dataclass
class Conflict:
    """A place where a live rule disagrees with a confirmed preference."""

    where: str
    current: str
    expected: str
    why: str
    severity: str = "высокая"


@dataclass
class Brief:
    facts: VacancyFacts
    assessments: list[Assessment] = field(default_factory=list)
    preferences: PreferenceSet = field(default_factory=PreferenceSet)
    tenure: Tenure = field(default_factory=Tenure)
    conflicts: list[Conflict] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    #: Every "must not be claimed" note in the KB -- a safety net for the resume
    #: as a whole. Shown in ``--details`` only.
    cannot_claim: list[str] = field(default_factory=list)
    #: The subset of the above that this vacancy actually touches. This is what
    #: the phone view prints, because the rest is noise about other projects.
    cannot_claim_relevant: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_class(self, evidence_class: EvidenceClass) -> list[Assessment]:
        return [a for a in self.assessments if a.evidence_class == evidence_class]


# ----------------------------------------------------------------------
# Segmentation and extraction
# ----------------------------------------------------------------------


def segment(text: str) -> list[str]:
    """Split a description into requirement-sized segments.

    Handles the two shapes this data actually arrives in: a single enormous
    line (every stored vacancy, measured) and a normally bulleted description.
    Bullets and numbered items become boundaries; so does sentence-final
    punctuation followed by a capital or a digit.

    What sentence punctuation cannot do is split a requirement list that was
    written without any (see :func:`_split_requirement_block`), and what this
    function must never do is drop source text: the 400-character cut that used
    to end this loop silently removed "Have at least 2 years of IT support
    experience on the job" from the Ignition posting. Overflow is now emitted as
    further segments instead of being thrown away.
    """
    if not text:
        return []
    working = text.replace("\r\n", "\n").replace("\r", "\n")
    # Emoji and decorative markers are section separators in real postings. The
    # braille blank is in the list because the Ignition posting uses U+2800 as a
    # paragraph break, and it is not an emoji: without it two unrelated
    # paragraphs arrive welded into one segment.
    working = re.sub(
        r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2800]", "\n", working
    )
    # Bullets and numbered list items start a new segment even mid-sentence.
    working = re.sub(r"\s*[•·▪◦‣∙]\s*", "\n", working)
    working = re.sub(r"\s+[-–—]\s+(?=[A-ZА-ЯЁ0-9])", "\n", working)
    working = re.sub(r"\s+(?=\d{1,2}[.)]\s+[A-ZА-ЯЁ])", "\n", working)
    # A quoted question ends a sentence just as firmly as a bare one; without
    # this the Porkbun posting's five example questions merged into one
    # 400-character blob that no reader could scan on a phone.
    working = re.sub(
        r"(?<=[.!?;])[\"'\u201d\u2019)\]]*\s+(?=[A-ZА-ЯЁ0-9\"'\u201c\u2018(\[])",
        "\n",
        working,
    )
    # Standing section labels, which postings use without any punctuation. The
    # multi-word phrases are listed ahead of the bare ones so the split lands
    # before the whole label: "Who we hope you are The hard requirements — we
    # won't move forward unless you:" used to break at "requirements", which
    # left "The hard" as a fragment *and* hid the header that makes the four
    # items under it mandatory.
    working = re.sub(
        r"\s+(?=(?:the\s+hard\s+requirements?|who\s+we\s+hope\s+you\s+are|"
        r"everything\s+else|summary|requirements?|qualifications?|responsibilit\w+|"
        r"benefits?|about\s+us|overview|how\s+to\s+apply|nice\s+to\s+have|"
        r"must[\s-]?have|desired\s+skills?|required\s+skills?|what\s+you.ll\s+do)\b[:\s])",
        "\n",
        working,
        flags=re.IGNORECASE,
    )

    out: list[str] = []
    for raw in working.split("\n"):
        piece = re.sub(r"\s+", " ", raw).strip(" .;:-–—")
        for part in _split_requirement_block(piece):
            if len(part) < 8:
                continue
            out.extend(_cap_segment(part))
    return out


#: The longest one segment may be. Longer text is *not* dropped: it is emitted
#: as further segments (see :func:`_cap_segment`), because a silent cut is the
#: one failure a brief cannot afford. Measured: on the Ignition posting this cap
#: is what removed the hard requirement "Have at least 2 years of IT support
#: experience on the job" -- the four hard requirements were welded into one
#: 440-character blob by the missing punctuation, and the tail fell off the end.
_MAX_SEGMENT = 400


def _cap_segment(piece: str) -> list[str]:
    """``piece`` as segments of at most :data:`_MAX_SEGMENT` characters.

    Split at a word boundary, and the remainder is returned as its own segment
    rather than discarded. Callers treat each part independently, so an
    over-long paragraph simply gets examined in two goes.
    """
    if len(piece) <= _MAX_SEGMENT:
        return [piece]
    parts: list[str] = []
    rest = piece
    while len(rest) > _MAX_SEGMENT:
        cut = rest.rfind(" ", 0, _MAX_SEGMENT + 1)
        if cut <= 0:
            cut = _MAX_SEGMENT
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return [part for part in parts if part]


#: Words that begin a *new* item in a requirement list. Declared rather than
#: inferred, and only ever applied to the body of a segment that announces a
#: list -- which is what keeps it from cutting prose in half.
#:
#: Measured need (T7.2): the stored Ignition description is one line, and its
#: four hard requirements arrive with no punctuation between them --
#: "…we won't move forward unless you: Can work standard business hours in
#: Eastern or Pacific time zones (we currently need both) Have a quiet home
#: workspace and reliable broadband Have proven Apple aptitude — … Have at least
#: 2 years of IT support experience on the job." The sentence splitter has
#: nothing to bite on, so the whole block was one segment; the schedule item's
#: own wording then filed it as compensation/schedule noise, and the two-years
#: requirement never reached the brief at all.
_REQUIREMENT_OPENER_RE = re.compile(
    r"\s+(?=(?:Can|Have|Has|Must|Should|Will|Would|Are|Is|Do|Does|Know|Knows|"
    r"Ability|Able|Experience|Experienced|Proven|Familiarity|Familiar|"
    r"Proficiency|Proficient|Willing|Comfortable|Genuinely|Loves?|Likes?|Reads?|"
    r"Stays?|Notices?|Proofreads?|Thinks?|Takes?|Cares?|Writes?|Speaks?|"
    r"Onboard|Offboard|Field|Triage|Provide|Provides|Manage|Manages|Administer|"
    r"Support|Troubleshoot|Configure|Install|Maintain|Develop|Design|Build|"
    r"Automate|Integrate|Create|Ensure|Monitor|Resolve|Handle|Respond|Provision|"
    r"Revoke|Document|Collaborate|Work|Participate|Assist|Own|Lead|Drive|"
    r"Deliver|Plan|Execute|Analyse|Analyze|Test|Deploy|Debug|Optimise|Optimize|"
    r"Уметь|Иметь|Опыт|Знание|Владение|Работа|Обеспечивать|Поддерживать|"
    r"Настраивать|Разрабатывать|Внедрять|Автоматизировать|Участвовать)\b)"
)

#: What a colon has to be attached to before its tail may be split into items.
#: Deliberately about *lists of asks*: a colon in "Our CEO's opinion: …" or
#: "Base salary: …" does not introduce one, and those stay whole.
_LIST_LABEL_RE = re.compile(
    r"\b(?:you|your|we|our|us|who|someone|here|following|include[sd]?|including|"
    r"typical|day\s+one|looking\s+for|look\s+for|requirements?|skills?|"
    r"qualifications?|responsibilities|duties|must|need(?:s|ed)?|want(?:s|ed)?|"
    r"требования|навыки|обязанности|умения)\b",
    re.IGNORECASE,
)


def _split_requirement_block(piece: str) -> list[str]:
    """A run-on requirement list as ``[header, item, item, …]``.

    The split is bounded twice, and both bounds are checked here rather than
    assumed: the segment must *declare* a list (a colon whose label talks about
    asks) **and** its tail must contain at least two declared requirement
    openers (:data:`_REQUIREMENT_OPENER_RE`). A paragraph of prose, a tag list
    ("Требуемые навыки: #middle, #Битрикс24, …") and a one-item heading all fail
    the second bound and are returned untouched -- splitting them would take the
    must/nice label away from the items it governs, which is exactly the
    regression the first draft of this function caused on the habr posting.
    """
    if ":" not in piece:
        return [piece]
    header, _, body = piece.partition(":")
    header = header.strip()
    body = body.strip()
    if not body or not header or len(header) > 120:
        return [piece]
    if not _LIST_LABEL_RE.search(header):
        return [piece]
    items = [item.strip() for item in _REQUIREMENT_OPENER_RE.split(body) if item.strip()]
    if len(items) < 2:
        items = _dash_labelled_items(body)
    if len(items) < 2:
        return [piece]
    return [f"{header}:", *items]


#: One item of a dashed technology list: a short label, a dash, then its content.
#:
#: The Ignition posting's technology section arrived as a single 318-character
#: segment -- "Apple at the core — macOS, iOS, Apple Business Manager, and MDM
#: (Jamf / Iru / Intune / Workspace ONE / and more) Yes, PCs too — about one-third
#: of our users are driving Windows Google Workspace and Microsoft 365 admin
#: consoles Identity and access — SSO, MFA, and security keys …" -- because the
#: newlines that separated those lines upstream were lost before the row was
#: stored. Splitting on the dash alone would cut the *label* off each item and
#: hand the reader "about one-third of our users are driving Windows Google
#: Workspace and Microsoft 365 admin consoles Identity and access" as an ask.
#:
#: So the break goes *before* a short label that is itself followed by a dash: at
#: most :data:`_DASH_LABEL_WORDS` words, starting with a capital, and made of
#: word characters only -- no slash, bracket or dash of its own. The word
#: structure is what keeps the match honest: a loose "any capitalised run of up
#: to forty characters" found the dash after the *next* label and split "Workspace
#: ONE / and more)" and "Yes," into items of their own.
_DASH_LABEL_WORDS = 4
_LABEL = rf"[A-Z][\w',-]*(?:\s+[\w',-]+){{0,{_DASH_LABEL_WORDS}}}"
#: An item runs to the next ``label — content`` pair, or to a trailing label that
#: has no dash of its own. The second alternative matters because the sentence
#: splitter has usually already cut the list in two: on the Ignition posting the
#: dash before "SSO" ends the segment, so the last item of the first half is a
#: bare label ("Identity and access") and would otherwise be glued onto the item
#: before it.
_DASH_ITEM_RE = re.compile(
    rf"(?P<label>{_LABEL})\s+[-–—]\s+(?P<content>.*?)"
    rf"(?=\s+(?:{_LABEL})\s+[-–—]\s+|\s+(?:{_LABEL})$|$)",
    re.DOTALL,
)


def _dash_labelled_items(body: str) -> list[str]:
    """Split a dashed list into ``label — content`` items, or return ``[]``.

    An item runs from its label to just before the next label, so the label stays
    with the content it introduces. Splitting on the dash alone cannot do that:
    it cuts "Yes, PCs too" off the "about one-third of our users are driving
    Windows …" it belongs to, and hands the reader a fragment as an ask.
    """
    if len(re.findall(r"\s+[-–—]\s+", body)) < 2:
        return []
    items: list[str] = []
    for match in _DASH_ITEM_RE.finditer(body):
        if not items and match.start() > 0:
            prefix = body[: match.start()].strip()
            if prefix:
                items.append(prefix)
        items.append(match.group(0).strip())
    return items if len(items) >= 2 else []


#: A requirement list *item* begins with one of these. The same vocabulary as
#: :data:`_REQUIREMENT_OPENER_RE`, anchored at the start of the item.
#:
#: This is the second half of the section bound: a header ending in ":" makes the
#: items under it required, and without this check that meaning leaked down the
#: whole posting -- on the Ignition description "Everything else -- and this is
#: the longer list, on purpose" and "We work with innovative, ambitious companies
#: developing AI products" were reported as mandatory requirements because a
#: header four paragraphs earlier had said "we won't move forward unless you".
#: A sentence that does not begin like a list item is not one, whatever precedes
#: it.
_ITEM_START_RE = re.compile(
    r"(?:Can|Have|Has|Must|Should|Will|Would|Are|Is|Do|Does|Know|Knows|"
    r"Ability|Able|Experience|Experienced|Proven|Familiarity|Familiar|"
    r"Proficiency|Proficient|Willing|Comfortable|Genuinely|Loves?|Likes?|Reads?|"
    r"Stays?|Notices?|Proofreads?|Thinks?|Takes?|Cares?|Writes?|Speaks?|"
    r"Onboard|Offboard|Field|Triage|Provide|Provides|Manage|Manages|Administer|"
    r"Support|Troubleshoot|Configure|Install|Maintain|Develop|Design|Build|"
    r"Automate|Integrate|Create|Ensure|Monitor|Resolve|Handle|Respond|Provision|"
    r"Revoke|Document|Collaborate|Work|Participate|Assist|Own|Lead|Drive|"
    r"Deliver|Plan|Execute|Analyse|Analyze|Test|Deploy|Debug|Optimise|Optimize|"
    r"Уметь|Иметь|Опыт|Знание|Владение|Работа|Обеспечивать|Поддерживать|"
    r"Настраивать|Разрабатывать|Внедрять|Автоматизировать|Участвовать)\b",
    re.IGNORECASE,
)


def _looks_like_list_item(piece: str) -> bool:
    """Whether ``piece`` is shaped like one item of a requirement list."""
    return _ITEM_START_RE.match(piece) is not None


def _expand_tag_list(piece: str) -> list[str]:
    """Expand a comma-separated ``#tag`` list into one requirement per tag.

    A list of ten technologies is ten requirements, not one: kept whole, a
    single match answers for every tag in it and the brief claims skills the
    candidate never mentioned.

    Each tag comes back bare (``#Docker``), because the label that introduced
    the list is repeated in every heading, question and highlight otherwise --
    "Есть ли опыт с «Требуемые навыки: #Docker»?" is not a sentence anyone
    wants to read on a phone. The label is not lost: :func:`extract_facts`
    reads the must/nice marker off the whole line and hands it to each tag.

    A segment that is not a tag list, and a list whose tags are all grades, is
    returned unchanged -- there is nothing to gain from splitting it.
    """
    tags = [tag.rstrip(".") for tag in _TAG_RE.findall(piece)]
    # ``#Docker.`` at the end of a sentence captures a trailing dot, because the
    # tag character class allows one inside a name (``asp.net``). Left on, the
    # tag would match no KB technology and be reported as unconfirmed -- a false
    # negative in exactly the case the brief exists to get right.
    if len(tags) < _MIN_TAG_LIST:
        return [piece]
    kept = [tag for tag in tags if tag.casefold() not in _GRADE_TAGS]
    if not kept:
        return [piece]
    return [f"#{tag}" for tag in kept]


def _marker_of(text: str) -> bool | None:
    """Whether ``text`` marks its requirements as required, or as wishes.

    ``None`` means it says neither, and the caller should decide per item. The
    must markers are tested first so a line carrying both ("required, plus
    nice-to-have") is a requirement -- the same precedence the analyzer uses.
    """
    if _MUST_RE.search(text):
        return True
    if _NICE_RE.search(text):
        return False
    return None


def _is_section_header(segment_text: str) -> bool:
    """Whether a segment *is* a section header, whatever kind it introduces.

    Separate from :func:`_section_of` because "this is a header and it does not
    say whether its items are required or optional" is a third state, and it has
    to reset the section rather than leave the previous one standing. Measured:
    on the Ignition description "So we're looking for someone who:" follows "we
    won't move forward unless you:", and without the reset every character wish
    under it inherited *required* from a header three paragraphs earlier.
    """
    return segment_text.rstrip().endswith(":")


def _section_of(segment_text: str) -> bool | None:
    """Whether a segment is a section header, and which kind it introduces.

    ``None`` means "not a must section and not a nice section"; callers that need
    to tell that apart from "not a header at all" ask
    :func:`_is_section_header` first.
    """
    if not _is_section_header(segment_text):
        return None
    if _SECTION_NICE_RE.search(segment_text):
        return False
    if _SECTION_MUST_RE.search(segment_text):
        return True
    return None


def classify_kind(text: str, vocabulary: dict[str, str]) -> tuple[RequirementKind, tuple[str, ...]]:
    """Classify one requirement and list the KB technologies it names.

    Order is deliberate. A segment that asks for *commercial* experience is
    commercial even when it also names a technology, because the technology
    alone would otherwise hide the harder question underneath it.
    """
    technologies = tuple(sorted({vocabulary[t] for t in _tokens(text) if t in vocabulary}))
    if _COMMERCIAL_RE.search(text):
        return RequirementKind.COMMERCIAL_EXPERIENCE, technologies
    if _SELF_DIRECTED_RE.search(text):
        return RequirementKind.SELF_DIRECTED, technologies
    if technologies:
        return RequirementKind.TOOL_USE, technologies
    if _DUTY_RE.search(text):
        return RequirementKind.GENERAL, technologies
    return RequirementKind.GENERAL, technologies


#: The order categories are reported in. Fixed so two runs of the brief agree.
_CATEGORY_ORDER: tuple[RequirementCategory, ...] = (
    RequirementCategory.TECHNOLOGY,
    RequirementCategory.DUTY,
    RequirementCategory.DURATION,
    RequirementCategory.CONDITION,
    RequirementCategory.CERTIFICATE,
)


def classify_category(
    text: str,
    technologies: tuple[str, ...],
    unrecognised: tuple[str, ...],
) -> tuple[RequirementCategory, ...]:
    """Every kind of ask ``text`` makes, read without consulting the KB.

    All that apply are kept, because a requirement is often two things at once:
    "Have at least 2 years of IT support experience on the job" is a DURATION and
    a DUTY, and reporting only one of them is exactly how the two-year threshold
    became invisible. The list is never empty -- an ask that matches nothing else
    is still an ask about the work, which is DUTY -- so no requirement can end up
    filed as "не указано" for want of a category.

    ``technologies`` and ``unrecognised`` are passed in rather than recomputed:
    they are the two halves of "this requirement names a technology", one the KB
    can resolve and one it cannot, and both are already known to the caller.
    """
    found: set[RequirementCategory] = set()
    if technologies or unrecognised:
        found.add(RequirementCategory.TECHNOLOGY)
    if _DUTY_RE.search(text) or _equivalents_asked(text):
        found.add(RequirementCategory.DUTY)
    if _years_named(text) is not None:
        found.add(RequirementCategory.DURATION)
    if _CONDITION_RE.search(text):
        found.add(RequirementCategory.CONDITION)
    if _CERTIFICATE_RE.search(text):
        found.add(RequirementCategory.CERTIFICATE)
    if not found:
        found.add(RequirementCategory.DUTY)
    return tuple(item for item in _CATEGORY_ORDER if item in found)


def _clean_stack_item(item: str) -> list[str]:
    """Split one ``project_stack`` entry into clean technology names.

    ``"PostgreSQL (asyncpg/SQLAlchemy)"`` yields ``postgresql``, ``asyncpg`` and
    ``sqlalchemy`` -- the parenthetical names real alternatives and hiding them
    would lose evidence. ``"Python 3.13"`` yields only ``python``: a version
    number is not part of the technology's name, and ``"ARQ + Redis"`` yields
    two names rather than one compound.
    """
    out: list[str] = []
    for alternative in re.split(r"[/,+]", item):
        name = re.sub(r"\(.*?\)", " ", alternative)
        name = re.sub(r"\b\d+(?:\.\d+)*\b", " ", name)
        name = normalize(name).strip("+.#")
        if name and len(name) <= 30:
            out.append(name)
    return out


def _claim_canonicals(record: Any) -> list[str]:
    """The technology names one claim contributes to the vocabulary.

    Each kind is read the way it is actually written: a ``skill`` is already a
    name, a ``project_stack`` is a comma-separated list, and a ``role_title`` is
    a job title whose individual words have to be judged one by one --
    "Bitrix24 Technical Support Specialist" yields ``bitrix24`` and drops
    "technical", "support" and "specialist" as generic.
    """
    if record.kind == "project_stack":
        return _clean_stack_item(record.value)
    if record.kind == "role_title":
        return [
            token
            for token in _tokens(record.value)
            if len(token) >= 3 and token not in _GENERIC_TECH_TOKENS
        ]
    canonical = normalize(record.value).strip("+.#")
    if canonical and len(canonical) <= 30 and len(canonical.split()) <= 3:
        return [canonical]
    return []


def _kb_vocabulary(kb: CandidateKnowledgeBase) -> dict[str, str]:
    """Map technology tokens to canonical names, built only from the KB.

    Built from the KB itself, so the brief can only ever recognise a technology
    the candidate has actually claimed somewhere. That is what stops it from
    "matching" a requirement against the model's own idea of the market.

    Three rules keep the mapping honest:

    * **Only term-like claims feed it** (:data:`_TECHNOLOGY_KINDS`). A first
      draft accepted prose claims too, which put whole sentences in the
      vocabulary and made every requirement containing a common English word
      match some unrelated project bullet.
    * **A token must identify exactly one technology.** ``openai`` and ``gemini``
      are usable; ``api`` is not, because three different skills contain it and
      guessing which one a vacancy meant would be invention. Multi-word skills
      stay reachable through their full phrase.
    * **Tokens shorter than three characters are dropped**, as are the generic
      English words in :data:`_GENERIC_TECH_TOKENS`. ``MS SQL`` would otherwise
      make the abbreviation ``ms`` a technology, and a posting quoting a
      latency in milliseconds would match it.
    """
    canonical_terms: list[str] = []
    for record in kb.claims.values():
        if not record.in_source or record.kind not in _TECHNOLOGY_KINDS:
            continue
        canonical_terms.extend(_claim_canonicals(record))

    # token -> set of canonical terms that contain it
    owners: dict[str, set[str]] = {}
    for canonical in set(canonical_terms):
        for token in _tokens(canonical):
            if len(token) < 3 or token in _GENERIC_TECH_TOKENS:
                continue
            owners.setdefault(token, set()).add(canonical)

    vocab: dict[str, str] = {}
    for canonical in set(canonical_terms):
        # A one-word canonical that is itself a generic word is unreachable
        # anyway -- its only route in is its own token -- so it is dropped
        # rather than left as a trap for the phrase lookup.
        if " " not in canonical and canonical in _GENERIC_TECH_TOKENS:
            continue
        # The full phrase is always unambiguous, so it is always registered.
        vocab.setdefault(canonical, canonical)
    for token, canonicals in owners.items():
        if len(canonicals) == 1:
            vocab.setdefault(token, next(iter(canonicals)))
    for alias, canonical in _ALIASES.items():
        if canonical in vocab.values():
            vocab.setdefault(alias, canonical)
    return vocab


#: How many distinct duties a work-area word may appear in and still count as
#: identifying. A word spread across most duties ("work", "system") separates
#: nothing; a word in one or two does.
_MAX_DOMAIN_DUTIES = 4

#: Work-area words that are too ordinary to identify anything.
#:
#: Derived by reading the harvested list rather than guessed: the first run
#: produced "based", "transfer" and "develop", and they immediately caused two
#: false matches -- "Applicants must be based in the USA" was reported as
#: confirmed commercial experience because a duty contained "FSM-based", and a
#: sentence about transferring a domain matched a media-transfer pipeline.
#: Everything here is a verb, a generic noun, or already covered by the
#: technology vocabulary.
_DOMAIN_NOISE: frozenset[str] = frozenset({
    "across", "around", "based", "using", "followed", "including", "self",
    "deal", "hyper", "state", "records", "request", "response", "message",
    "media", "transfer", "storage", "task", "tasks", "processes", "service",
    "services", "system", "systems", "business", "build", "built", "design",
    "develop", "developed", "implement", "configured", "connecting",
    "integrated", "integrating", "maintained", "provided", "worked",
    "automated", "routine", "external", "incoming", "corporate", "channels",
    "chats", "users", "email", "testing", "management", "distribution",
    "drive", "sheets", "google", "analytics", "classification",
    "summarization", "agents", "bots", "pipeline", "pipelines",
    # Modifiers that read like skills but are not. "hosted" produced a false
    # match between "how do I connect my hosted email" and "self-hosted
    # Bitrix24"; "active" and "lead" are ambiguous without their full phrase
    # ("Active Directory", "lead generation"), and "directory" still carries
    # the first one on its own.
    "hosted", "active", "lead", "processing",
})


def _kb_domains(kb: CandidateKnowledgeBase) -> frozenset[str]:
    """Work areas the candidate has demonstrably performed *for an employer*.

    A second vocabulary is needed because technologies alone cannot read a
    support or sysadmin posting. "Do you have two or more years of live technical
    support experience?" names no technology at all, yet the candidate's Avantis
    duty reads "Provided technical support and troubleshooting for business
    users" -- a genuine match a technology-only vocabulary cannot see.

    Harvested from ``duty`` claims only. That is deliberate: a duty is the one
    kind the KB ties to a role, so anything harvested here is, by construction,
    paid work. Nothing from the project catalogue can enter, which is what keeps
    the brief from dressing a personal project up as commercial experience.
    """
    per_token: dict[str, int] = {}
    for record in kb.claims.values():
        if not record.in_source or record.kind != "duty":
            continue
        for token in set(_tokens_loose(record.value)):
            if len(token) < 4:
                continue
            if token in _GENERIC_TECH_TOKENS or token in _DOMAIN_NOISE:
                continue
            per_token[token] = per_token.get(token, 0) + 1
    return frozenset(
        token for token, seen in per_token.items() if seen <= _MAX_DOMAIN_DUTIES
    )


def _domain_terms_in(text: str, domains: frozenset[str]) -> set[str]:
    """Work-area words from :func:`_kb_domains` that ``text`` names.

    Resolves :data:`_ALIASES` as well, so a Russian posting's ``#Битрикс24``
    reaches the English ``Bitrix24`` duty.
    """
    found: set[str] = set()
    for token in _tokens_loose(text):
        if token in domains:
            found.add(token)
            continue
        alias = _ALIASES.get(token)
        if alias and alias in domains:
            found.add(alias)
    return found


#: How many distinct work-area words a duty must share with a requirement before
#: it may answer it. Two, not one.
#:
#: Measured need: with a single shared word accepted, "Industry recognized
#: security certification, IAT Level II/III" was reported as confirmed
#: commercial experience because a duty mentioned security -- but security
#: *work* does not make a security *certification*, and the brief was asserting
#: a credential the candidate does not hold. A single shared word is a topic;
#: two are a subject. Technologies do not need this rule, because a shared
#: technology name is specific by construction.
_MIN_DOMAIN_HITS = 2


#: A declared equivalence between how a vacancy words an activity and how the
#: candidate's own confirmed fact words the same activity.
#:
#: WHY THIS EXISTS
#: ---------------
#: The Ignition posting asks for "at least 2 years of IT support experience on
#: the job". The candidate's Avantis duty reads "Provided technical support and
#: troubleshooting for business users." Both say the same thing about the same
#: work, and they share exactly one ordinary word -- *support* -- so the
#: two-domain-word rule below could not see the connection and the requirement
#: came back with no evidence at all. The obvious "fix" is to lower
#: :data:`_MIN_DOMAIN_HITS` to 1, and it is the wrong fix: one shared word is a
#: topic, not a subject, and accepting one is what once let a duty that mentioned
#: security confirm a security *certification*.
#:
#: So the equivalence is declared instead, one activity at a time, with both
#: halves written down and both halves required to match.
#:
#: WHAT BOUNDS IT
#: --------------
#: * ``anchors`` names the exact KB claim the equivalence may rest on. A new duty
#:   does not silently join in: extending this table is a reviewable act, and a
#:   test pins the anchors that exist today.
#: * the claim's own text must match ``fact_patterns``, so a re-worded KB stops
#:   the equivalence rather than letting it answer something it no longer says;
#: * the requirement must match ``vacancy_patterns``, so a posting that never
#:   asks for the activity cannot reach the anchor at all;
#: * the equivalence contributes **one** hit toward :data:`_MIN_DOMAIN_HITS` and
#:   nothing else. It never confirms a technology, never raises a class, and
#:   never applies to a project claim -- only to a ``duty``, which is the one
#:   claim kind the KB ties to an employer.
@dataclass(frozen=True)
class ProfessionalEquivalent:
    """One declared "these two wordings describe the same work" pair."""

    activity: str
    vacancy_patterns: tuple[re.Pattern[str], ...]
    fact_patterns: tuple[re.Pattern[str], ...]
    anchors: tuple[str, ...]
    rationale: str

    def asked_by(self, text: str) -> bool:
        """Whether a vacancy's wording expresses this activity."""
        return any(pattern.search(text) for pattern in self.vacancy_patterns)

    def stated_by(self, claim_id: str, value: str) -> bool:
        """Whether ``claim_id`` says the candidate did this activity."""
        if claim_id not in self.anchors:
            return False
        return any(pattern.search(value) for pattern in self.fact_patterns)


def _compile_all(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


#: The declared table. Three activities, all resting on the same Avantis duty --
#: it is the candidate's only confirmed user-facing support fact, and saying so
#: three times is the honest way to express that three wordings of the same job
#: all point at it.
_PROFESSIONAL_EQUIVALENTS: tuple[ProfessionalEquivalent, ...] = (
    ProfessionalEquivalent(
        activity="it-support",
        vacancy_patterns=_compile_all(
            r"\bit[\s-]support\b",
            r"\btechnical\s+support\b",
            r"\btech\s+support\b",
            r"\bhelp\s*-?\s*desk\b",
            r"\bservice\s+desk\b",
            r"\bdesktop\s+support\b",
            r"\bsupport\s+(?:representative|technician|engineer|specialist)\b",
        ),
        fact_patterns=_compile_all(r"\btechnical\s+support\b"),
        anchors=("duty-94b646ace9",),
        rationale=(
            "«IT support» в вакансии и «technical support» в обязанности — одна "
            "деятельность: поддержка пользователей на первой линии. Вакансия "
            "называет её по области (IT), обязанность — по способу (technical)."
        ),
    ),
    ProfessionalEquivalent(
        activity="user-support",
        vacancy_patterns=_compile_all(
            r"\buser\s+support\b",
            r"\bend[\s-]?user\s+support\b",
            r"\bsupport\s+(?:for|to)\s+(?:business\s+)?users\b",
        ),
        fact_patterns=_compile_all(r"\bfor\s+business\s+users\b"),
        anchors=("duty-94b646ace9",),
        rationale=(
            "«user support» в вакансии и «support … for business users» в "
            "обязанности — одна деятельность: поддержка сотрудников компании. "
            "Обязанность называет тех же пользователей точнее."
        ),
    ),
    ProfessionalEquivalent(
        activity="troubleshooting",
        vacancy_patterns=_compile_all(
            r"\btroubleshoot\w*",
            r"\bdiagnos\w*\b[^.;]{0,40}\b(?:issues?|problems?|faults?)\b",
        ),
        fact_patterns=_compile_all(r"\btroubleshoot\w*"),
        anchors=("duty-94b646ace9",),
        rationale=(
            "«diagnosis of technical issues» в вакансии и «troubleshooting» в "
            "обязанности — одна деятельность: поиск причины неисправности. "
            "Формулировки расходятся, работа одна."
        ),
    ),
)


def _equivalents_asked(text: str) -> tuple[ProfessionalEquivalent, ...]:
    """The declared equivalences a requirement's wording expresses."""
    return tuple(item for item in _PROFESSIONAL_EQUIVALENTS if item.asked_by(text))


def _equivalents_fired(
    record: Any, asked: tuple[ProfessionalEquivalent, ...]
) -> tuple[ProfessionalEquivalent, ...]:
    """The asked equivalences this KB claim can answer."""
    return tuple(
        item for item in asked if item.stated_by(record.claim_id, record.value or "")
    )


def _equivalence_note(
    requirement: Requirement, evidence: list[Evidence]
) -> str:
    """A sentence naming the equivalence that connected ask and fact.

    Printed only when the connection actually carried the match, and it names
    both halves plus the claim id, because an equivalence the reader cannot check
    is indistinguishable from a guess.
    """
    asked = _equivalents_asked(requirement.text)
    if not asked:
        return ""
    for item in asked:
        for record in evidence:
            if item.stated_by(record.claim_id, record.value or ""):
                return (
                    f"Профессиональная эквивалентность: «{item.activity}» — "
                    f"{item.rationale} Источник: {record.claim_id} "
                    f"({record.where})."
                )
    return ""


def _extract_languages(text: str) -> list[str]:
    found: list[str] = []
    lowered = normalize(text)
    for needle, label in (
        ("english", "английский"),
        ("английск", "английский"),
        ("russian", "русский"),
        ("русск", "русский"),
        ("german", "немецкий"),
        ("thai", "тайский"),
    ):
        if needle in lowered and label not in found:
            found.append(label)
    for level in ("b1", "b2", "c1", "c2", "fluent", "native", "свободн", "средн"):
        if level in lowered:
            found.append(f"уровень: {level.upper() if level.isascii() else level}")
            break
    return found


def _validated_manual_requirements(
    description: str, supplied: tuple[str, ...]
) -> list[str]:
    """Hand-supplied requirement texts, each checked against the posting.

    The fallback exists because a description can defeat the splitter. The
    stored Ignition row is one 9,362-character line, and its four hard
    requirements arrived as a single block whose own wording then matched the
    compensation filter -- so all four were dropped, including the two-year IT
    support requirement. An operator who can read the posting can type those
    four lines in and have them assessed.

    What makes it *verifiable* rather than a free-text override is the check
    below: every supplied line has to occur in the description with whitespace
    normalised. A requirement the posting does not state cannot be introduced
    through the fallback, so the brief can never report an ask the employer never
    made -- the same "не додумывать отсутствующие сведения" rule the automatic
    extractor is held to, applied to the manual path.

    Raises :class:`JobBriefError` naming the first line that fails, rather than
    dropping it silently: a fallback that quietly ignores half its input is worse
    than no fallback, because the operator would read the resulting brief as
    complete.
    """
    flat = " ".join(description.split())
    out: list[str] = []
    for raw in supplied:
        text = " ".join(raw.split())
        if len(text) < 8:
            raise JobBriefError(
                f"ручное требование слишком короткое ({text!r}) — укажите текст целиком"
            )
        if text not in flat:
            raise JobBriefError(
                "ручное требование не найдено в описании вакансии дословно: "
                f"{text[:90]!r}. Fallback добавляет требования, но не выдумывает их."
            )
        out.append(text)
    return out


def _requirement_pieces(vacancy: VacancyInput) -> tuple[list[str], set[str]]:
    """Segments to read, plus the normalised keys of the hand-supplied ones.

    Manual requirements are *appended* to the automatic reading rather than
    replacing it. Replacing it would lose the technologies list, the conditions
    bucket and the "not stated" list for exactly the messy postings the fallback
    is for; appending rescues the items the splitter missed and leaves everything
    else as it was.
    """
    manual = _validated_manual_requirements(
        vacancy.description, vacancy.manual_requirements
    )
    return list(segment(vacancy.description)) + manual, {normalize(item) for item in manual}


def extract_facts(vacancy: VacancyInput, kb: CandidateKnowledgeBase) -> VacancyFacts:
    """Turn a vacancy into structured facts, marking anything absent as such."""
    from ..matcher import extract_required_years, extract_seniority
    from ..remote_filter import classify_work_format

    vocabulary = _kb_vocabulary(kb)
    facts = VacancyFacts(
        title=vacancy.title or "не указано",
        employer=vacancy.company or "не указано",
        source=vacancy.source,
        stable_id=vacancy.stable_id,
        url=vacancy.job_url,
    )

    section: bool | None = None
    seen: set[str] = set()
    pieces, manual_keys = _requirement_pieces(vacancy)
    for raw_piece in pieces:
        if _is_section_header(raw_piece):
            # A header of unstated kind (``_section_of`` returns ``None`` for
            # both "not a header" and "header of unknown kind") clears the
            # section. Leaving the previous one standing is how a must-header
            # four paragraphs earlier made a company blurb mandatory.
            section = _section_of(raw_piece)
            continue
        # A tag list carries one requirement per tag; each answers for itself.
        expanded = _expand_tag_list(raw_piece)
        # A hand-supplied requirement is read as a requirement. It is the one
        # piece in this loop the operator vouched for, and it exists to rescue
        # exactly the text the automatic path discards -- so the company-blurb
        # and compensation filters are not applied to it, and it is never filed
        # as a condition. It still had to occur in the description to get here
        # (see :func:`_validated_manual_requirements`).
        is_manual = normalize(raw_piece) in manual_keys
        # The label that introduced a tag list is what says whether its tags are
        # required or optional -- "Требуемые навыки:" versus "Желательно:". The
        # tags themselves are shown bare, so that meaning has to be read off the
        # whole line here and inherited by every tag; without this a required
        # list would silently become a list of wishes.
        inherited = _marker_of(raw_piece) if len(expanded) > 1 else None
        # A section's must/nice meaning belongs to its *items*, not to whatever
        # sentence happens to follow the header (see :func:`_looks_like_list_item`).
        item_of_section = section if _looks_like_list_item(raw_piece) else None

        for piece in expanded:
            key = normalize(piece)
            if key in seen:
                continue
            seen.add(key)

            kind, technologies = classify_kind(piece, vocabulary)
            # Read the names the candidate has no claim for *here*, while the
            # vacancy is being read, rather than only inside the assessment: a
            # requirement that names nothing but unknown technologies must still
            # be a requirement, and it must carry those names with it.
            unrecognised = _unrecognised_terms(piece, vocabulary)
            disjunctive = _is_disjunction(piece, vocabulary)
            categories = classify_category(piece, technologies, tuple(sorted(unrecognised)))

            # Company blurb and hiring logistics are never requirements, however
            # many technologies they name-drop. Compensation and schedule are
            # context too, but a segment that names a real technology is kept,
            # so a genuine requirement mentioning a benefit word is not lost.
            if _HARD_NOISE_RE.search(piece) and not is_manual:
                facts.duties.append(piece)
                continue
            # A mandatory *condition* is reported, but as a term of the job rather
            # than as a skill: it goes to its own bucket and never reaches the
            # evidence matcher. Kept only when the segment names no technology, so
            # "must be able to work full-time with Docker" is still a skill ask.
            if (
                RequirementCategory.CONDITION in categories
                and not technologies
                and not unrecognised
                and not is_manual
            ):
                facts.conditions.append(
                    Requirement(
                        piece,
                        RequirementKind.GENERAL,
                        True,
                        technologies,
                        tuple(sorted(unrecognised)),
                        disjunctive,
                        categories,
                    )
                )
                continue
            if _SOFT_NOISE_RE.search(piece) and not technologies and not is_manual:
                facts.duties.append(piece)
                continue

            shaped = bool(_REQUIREMENT_SHAPE_RE.search(piece))
            if inherited is not None:
                mandatory = inherited
            elif is_manual:
                # A hand-supplied requirement is one the operator asserts the
                # posting makes, so it is required unless its own wording says
                # otherwise ("nice to have", "желательно"). Re-deriving must/nice
                # from markers is what the fallback is for: the markers were part
                # of what the automatic reading got wrong.
                mandatory = not _NICE_RE.search(piece)
            elif _MUST_RE.search(piece):
                mandatory = True
            elif _NICE_RE.search(piece):
                mandatory = False
            elif item_of_section is not None:
                mandatory = item_of_section
            elif kind is RequirementKind.COMMERCIAL_EXPERIENCE or shaped:
                # A bare "3+ years of commercial Python" is a requirement even
                # without the word "required" -- the years make it one.
                mandatory = True
            else:
                mandatory = False

            requirement_like = (
                kind is not RequirementKind.GENERAL
                or bool(technologies)
                # A requirement naming only technologies the KB cannot answer is
                # still a requirement. Without this the CI/CD line -- whose
                # three names the candidate has no claim for -- would fall
                # through to the duties list and vanish from the ask entirely.
                or bool(unrecognised)
                or shaped
                or inherited is not None
                or _MUST_RE.search(piece) is not None
                or _NICE_RE.search(piece) is not None
                or item_of_section is not None
                # A hand-supplied requirement is a requirement even when nothing
                # in its wording marks it as one. Without this the fallback would
                # silently drop the plainest lines -- "Quiet home workspace" is
                # not shaped like an ask, and being filed as context is exactly
                # the failure the operator used the fallback to escape.
                or is_manual
            )
            if not requirement_like:
                facts.duties.append(piece)
            elif mandatory:
                facts.must_have.append(
                    Requirement(
                        piece,
                        kind,
                        True,
                        technologies,
                        tuple(sorted(unrecognised)),
                        disjunctive,
                        categories,
                    )
                )
            else:
                facts.nice_to_have.append(
                    Requirement(
                        piece,
                        kind,
                        False,
                        technologies,
                        tuple(sorted(unrecognised)),
                        disjunctive,
                        categories,
                    )
                )

    # Seniority and years come from the tested helpers rather than a new regex.
    facts.seniority = extract_seniority(vacancy.title, vacancy.description)
    facts.commercial_years_required = extract_required_years(vacancy.title, vacancy.description)

    technologies: list[str] = []
    for requirement in facts.must_have + facts.nice_to_have:
        for tech in requirement.technologies:
            if tech not in technologies:
                technologies.append(tech)
    facts.technologies = sorted(technologies)

    facts.languages = _extract_languages(f"{vacancy.title} {vacancy.description}")
    if not facts.languages:
        facts.unstated.append("язык работы")

    remote, reason = classify_work_format(
        vacancy.title,
        vacancy.description,
        vacancy.location,
        vacancy.source,
        vacancy.employment_type,
        vacancy.raw_data or {},
    )
    facts.remote = remote
    facts.work_format_reason = reason or ""
    if remote:
        facts.work_format = "удалённо"
    elif remote is False:
        facts.work_format = "не удалённо"
    else:
        facts.work_format = "не указано"
    facts.location = vacancy.location or "не указано"
    if facts.location == "не указано":
        facts.unstated.append("география")

    if vacancy.salary_min is None and vacancy.salary_max is None:
        facts.salary_text = "не указано"
        facts.unstated.append("зарплата")
    else:
        low = vacancy.salary_min or vacancy.salary_max
        high = vacancy.salary_max or vacancy.salary_min
        amount = f"{low:g}–{high:g}" if low != high else f"{low:g}"
        currency = vacancy.salary_currency or ""
        if currency:
            facts.salary_text = f"{amount} {currency}".strip()
        else:
            # The stored figure is sometimes a parse artefact: one habr row
            # holds ``salary_min == salary_max == 24`` with no currency, which
            # is "24 000 ₽" mangled upstream. Printing "24" bare, or comparing
            # it against a USD minimum, would be a confident nonsense.
            facts.salary_text = f"{amount} (валюта в источнике не указана)"
            facts.unstated.append("валюта зарплаты")

    if not facts.must_have:
        facts.unstated.append("обязательные требования")
    if not facts.employer or facts.employer == "не указано":
        facts.unstated.append("работодатель")
    return facts


# ----------------------------------------------------------------------
# Evidence
# ----------------------------------------------------------------------


def _role_titles(kb: CandidateKnowledgeBase) -> dict[str, str]:
    titles: dict[str, str] = {}
    for record in kb.claims.values():
        if record.kind == "role_title" and record.role_id:
            titles[record.role_id] = record.value
    return titles


def _project_names(kb: CandidateKnowledgeBase) -> dict[str, str]:
    """Project name, reachable by every id the KB uses for that project.

    Measured, and the KB is not consistent about it: a ``project_status`` claim
    points at its project through ``parent_id`` and stores the *claim id*
    (``project-3e4d5f0359``), while a project-sourced claim stores the
    *normalised slug* in ``evidence["project_id"]`` (``repiton``). Keying on the
    slug alone resolved on real data and fell through to the raw identifier
    elsewhere -- and a raw identifier is precisely what the phone view must not
    contain.
    """
    names: dict[str, str] = {}
    for record in kb.claims.values():
        if record.kind != "project":
            continue
        names[record.claim_id] = record.value
        for key in (record.normalized, normalize(record.value)):
            if key:
                names[key] = record.value
    return names


def _own_work_roles() -> frozenset[str]:
    """Role ids the candidate's own confirmed words exclude from paid work.

    The declaration already exists and is already enforced elsewhere:
    ``master_resume`` holds ``INDEPENDENT_ROLE_EVIDENCE``, mapping the role to
    the ``CONFIRMED_BY_USER`` claim that says so, and that module's
    ``test_independent_role_evidence_is_confirmed_and_bound`` checks the claim is
    really confirmed and really carries that ``role_id``. Reading it here instead
    of writing a second list is the point of the exercise: the KB says "since May
    2026 I built only my own projects, with no paid orders", and one module must
    not treat that as paid employment while another treats it as not.

    This is the defect the previous draft made in the most expensive direction.
    It read ``role_id`` as "an employer paid for this", and the Freelance block
    is role-linked, so five own-project duties were reported as commercial
    experience under the heading "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ" -- carrying the
    same role title as a real employer's block, which made the two
    indistinguishable in the output.
    """
    from .master_resume import INDEPENDENT_ROLE_EVIDENCE

    return frozenset(INDEPENDENT_ROLE_EVIDENCE)


def _evidence_for(
    record: Any,
    role_titles: dict[str, str],
    project_names: dict[str, str],
    own_work_roles: frozenset[str],
) -> Evidence:
    """Build an :class:`Evidence` from a KB record, preserving its provenance.

    ``own_work_roles`` is deliberately not optional. A default of "no role is
    independent" reads as "every role was paid", which is the expensive
    direction of the T6.2 defect -- a caller that forgets the argument would
    silently restore it.
    """
    evidence = record.evidence or {}
    source_file = str(evidence.get("source_file") or "")
    role_linked = record.role_id is not None
    if is_project_sourced(record):
        origin = "project"
        project = project_names.get(str(evidence.get("project_id") or ""), "")
        if not project:
            # Never fall back to the identifier: the phone view prints ``where``
            # verbatim, and "project-d4f5dc1c96" is not a project name.
            project = "свой проект"
        where = project
        ref = f"{evidence.get('source_rel_path') or evidence.get('source_path')}:{evidence.get('line')}"
    elif source_file == "interview":
        origin = "interview"
        where = "подтверждено на интервью"
        ref = f"интервью:{evidence.get('question_id')}"
    elif role_linked and record.role_id in own_work_roles:
        # Role-linked, but the role is the candidate's own unpaid work. The
        # label has to say so outright: the Freelance block carries the same
        # role title as a genuine employer's block, so the title alone cannot
        # distinguish them, and neither can "it sits under Professional
        # Experience in resume.md".
        origin = "own_work"
        where = "свои проекты — без оплаты"
        ref = record.line_ref()
    else:
        origin = "resume"
        where = role_titles.get(record.role_id or "", "резюме") if record.role_id else "резюме"
        ref = record.line_ref()
    return Evidence(
        claim_id=record.claim_id,
        claim_kind=record.kind,
        value=record.value,
        status=record.status,
        origin=origin,
        where=where,
        ref=ref,
        # ``role_id`` alone is not enough to call something paid work: it says a
        # role produced the claim, and a role may be unpaid. The declared
        # independent roles are subtracted, which is what stops an own project
        # from becoming commercial experience through its role_id.
        role_linked=role_linked and record.role_id not in own_work_roles,
    )


def _canonical_terms_in(text: str, vocabulary: dict[str, str]) -> set[str]:
    """Canonical technology names the text actually asks for.

    Two routes, because a vacancy writes technologies both ways: as a bare token
    ("Docker") and as a phrase ("Google Workspace APIs"). Everything is resolved
    through the KB vocabulary, so a term can only appear here if the candidate
    has claimed it somewhere.
    """
    terms: set[str] = set()
    for token in _tokens(text):
        canonical = vocabulary.get(token)
        if canonical:
            terms.add(canonical)
    normalized = normalize(text)
    for canonical in set(vocabulary.values()):
        if len(canonical) < 3 or " " not in canonical:
            continue
        if re.search(rf"(?<![0-9a-zа-я]){re.escape(canonical)}(?![0-9a-zа-я])", normalized):
            terms.add(canonical)
    return terms


#: Shapes that mark a word as a technology name. Derived by reading the five
#: vacancies rather than guessed: a first draft treated every token the knowledge
#: base did not know as a technology and produced "eligible", "focus", "managed"
#: and "none" -- ordinary English that would have turned every posting into a
#: wall of unconfirmed requirements and taught the reader to skip the section.
_TERM_SHAPES: tuple[re.Pattern[str], ...] = (
    # hh tags: #PHP, #CI/CD, #Битрикс24
    re.compile(r"#[A-Za-zА-Яа-яЁё][\w./+#-]*"),
    # an internal capital: GitLab, PostgreSQL, JavaScript, UiPath, ServiceNow
    re.compile(r"\b[A-Z][a-z]+[A-Z][\w.]*"),
    # letters and digits together: Bitrix24, n8n, S3
    re.compile(r"\b(?=[A-Za-z][\w.-]*\d)[A-Za-z][\w.-]*"),
    # A symbol joining two all-caps parts: CI/CD, CI/CD-пайплайнов. Both halves
    # have to be upper-case, because "analysis/review", "patch/upgrade" and
    # "Yes/No" wear the same shape and are prose, not technologies. Names that
    # are not all-caps on both sides ("Sentinel/Splunk") still arrive through
    # the list below, where the surrounding items vouch for them. The trailing
    # lookahead stops the match from biting the first letter off a longer word:
    # without it "LAN/Network" comes back as "LAN/N".
    re.compile(r"\b[A-Z]{2,}[/+][A-Z0-9]+(?!\w)"),
    # A capital *inside* a lowercase token: macOS, iOS, eCommerce. English does
    # not put a capital in the middle of a word, so the shape identifies the
    # Apple product family without a product list -- which is what T7.2 section 4
    # needs, because "macOS" is not in the knowledge base and never will be, and
    # a brief that cannot name it cannot report it as unconfirmed either.
    re.compile(r"\b[a-z]+[A-Z][A-Za-z]*\b"),
)

#: A parenthesised list of names: ``(Jamf / Iru / Intune / Workspace ONE)``.
#:
#: :data:`_LIST_RUN` deliberately refuses to cross a bracket, because
#: "(GitLab CI, GitHub Actions, Jenkins)" is one list and the text before the
#: bracket is another. That rule also hid this one, so brackets get their own
#: pass with a stricter self-declaration (see :func:`_named_terms`): a run of
#: three or more capitalised names inside a parenthesis is a list of products.
_BRACKET_RUN = re.compile(r"\(([^()]{0,200})\)")

#: How many items a parenthesised run needs before it vouches for itself.
_MIN_BRACKET_LIST = 3

#: Conjunctions that introduce the last item of an English or Russian list.
#: Stripped from an item before it is judged, so "…, and MDM" is read as the name
#: "MDM" rather than as the phrase "and MDM", which the prose rule would reject.
_ITEM_CONJUNCTION = re.compile(r"^(?:and|or|и|или)\s+", re.IGNORECASE)

#: A product name carrying a version number: "Microsoft 365", "Windows 10",
#: "Dynamics 365". The left half has to be a mixed-case name, so the all-caps
#: forms ("SOC 2", "CIS 1") stay out: measured on the Ignition posting, "SOC 2"
#: inside a career-growth paragraph demoted a confirmed requirement to partial
#: because a name no fact can answer is an unanswered part of the ask. A
#: compliance standard mentioned as something the team does is not an ask.
_VERSIONED_PRODUCT_RE = re.compile(r"\b[A-Z][a-z]+\s+\d{1,4}\b")

#: The ordinary nouns that wear a versioned product's shape. Declared, because
#: each one would otherwise put a phantom requirement name in front of the reader
#: ("Round 2", "Level 3"), and a name the candidate has no claim for is exactly
#: the kind of line that teaches a reader to skip the section.
_VERSIONED_PRODUCT_NOISE: frozenset[str] = frozenset({
    "round", "step", "level", "page", "part", "tier", "type", "phase", "stage",
    "chapter", "section", "item", "point", "version", "grade", "row", "line",
    "table", "figure", "appendix", "question", "year", "month", "week", "day",
})

#: Two or three capitalised words in a row: "Google Workspace", "Active
#: Directory", "Apple Business Manager", "Power Platform".
#:
#: Written as a lookahead so that overlapping phrases are all offered. A
#: consuming pattern found "Windows Google Workspace" first, rejected it (the KB
#: has no "windows" technology), and thereby ate "Google Workspace" before it
#: could be considered -- which is how the one phrase the rule exists for went
#: missing on the posting it was written for.
_PRODUCT_PHRASE_RE = re.compile(
    r"(?=(?<![\w.])"
    r"([A-Z][A-Za-z0-9.+#-]*(?:\s+(?:[A-Z][A-Za-z0-9.+#-]*|\d{1,4})){1,2})"
    r"(?![\w]))"
)


def _kb_technology_words(vocabulary: dict[str, str]) -> frozenset[str]:
    """Every word that appears in a technology the KB claims.

    The *locating* half of the vocabulary, deliberately kept apart from the
    matching half. "Google Workspace" is a real ask on the Ignition posting and
    the KB does claim "Google Workspace APIs" -- but the token "google" belongs to
    two canonicals, so :func:`_kb_vocabulary` refuses to resolve it to either and
    the requirement could not be named at all. A word that cannot identify a
    technology can still help find one; it can never confirm one, because the
    names this returns are only ever reported as unrecognised.

    Two exclusions, both measured rather than guessed. The generic technology
    words are out because "Engineering" and "Automation" open ordinary noun
    phrases -- "Engineering Seniority" and "Automation Developer Professional"
    both arrived as product names on the real postings. The noise terms are out
    for the same reason: "REST API" and "AI SaaS" are prose, not products.
    """
    return frozenset(
        word
        for value in set(vocabulary.values())
        for word in _tokens(value)
        if len(word) >= 3
        and word not in _GENERIC_TECH_TOKENS
        and word not in _TERM_NOISE
    )


def _technology_led_phrases(text: str, vocabulary: dict[str, str]) -> set[str]:
    """Product names whose first word is a technology word the KB already uses."""
    words = _kb_technology_words(vocabulary)
    found: set[str] = set()
    for match in _PRODUCT_PHRASE_RE.finditer(text):
        phrase = match.group(1)
        tokens = _tokens(phrase)
        if tokens and tokens[0] in words:
            found.add(phrase)
    return found


def _versioned_products(text: str) -> set[str]:
    """Product names of the ``<Name> <number>`` shape, noise words excluded."""
    found: set[str] = set()
    for match in _VERSIONED_PRODUCT_RE.finditer(text):
        phrase = match.group(0)
        if phrase.split()[0].casefold() in _VERSIONED_PRODUCT_NOISE:
            continue
        found.add(phrase)
    return found

#: A maximal run of two or more comma/slash-separated items. Brackets are
#: excluded so a run cannot reach across a parenthetical: "(GitLab CI, GitHub
#: Actions, Jenkins)" is one list, and the "; " before the bracket is another.
_LIST_RUN = re.compile(r"[^,;/()\[\]]+(?:[,;/][^,;/()\[\]]+)+")

#: An all-caps token of two or more letters. Only ever a *marker* that a list is
#: a list of technologies -- never a term in itself, because "USA", "US" and
#: "IAT" are all-caps too.
_ACRONYM = re.compile(r"\b[A-Z]{2,}\b")

#: Tokens that clear the shape tests but are ordinary words in these postings.
#: Small and declared, because every false positive costs a requirement a class
#: for a reason the reader cannot act on.
_TERM_NOISE: frozenset[str] = frozenset({
    "usa", "us", "ii", "iii", "iv", "vi", "etc", "eg", "ie", "api", "apis",
    "level", "levels",
})

#: Markers that make a requirement a choice rather than a conjunction. Whole
#: words with word boundaries, so "monitoring" and "order" do not match "or".
_DISJUNCTION = re.compile(r"(?:\bили\b|\bor\b|\bлибо\b|\beither\b)", re.IGNORECASE)

#: How much text may stand between two technology names for the connector in it
#: to count. Wide enough for " or ", " или " and "either … or", too narrow for a
#: clause -- which is what keeps an "или" joining two kinds of database from
#: reading as a choice between the two names on the line.
_DISJUNCTION_GAP = 24


def _term_spans(text: str, vocabulary: dict[str, str]) -> list[tuple[int, int]]:
    """Where in ``text`` the technologies it names are written.

    Only the token route is used: a canonical that is a phrase ("active
    directory") is not located here, because the caller only needs the outermost
    bounds and a span that cannot be found can only make the check below more
    conservative.
    """
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"[\w+#.]+", text, re.UNICODE):
        token = match.group(0).strip("+.#")
        if token and normalize(token) in vocabulary:
            spans.append((match.start(), match.end()))
    return spans


def _is_disjunction(text: str, vocabulary: dict[str, str]) -> bool:
    """Whether the requirement offers a choice *between the names it asks for*.

    "Python or PowerShell" is answered by one of them; "SQL and PostgreSQL" needs
    both. Nothing in the technology sets distinguishes the two, so the text has
    to -- and getting it wrong is asymmetric: reading a conjunction as a choice
    over-claims, reading a choice as a conjunction only under-claims.

    Two guards keep the rule about the names and not about prose, because a
    draft that only looked for the word fired on "SQL и PostgreSQL — уверенно;
    опыт хотя бы с одной аналитической или NoSQL СУБД (… Redis …)" and let SQL
    answer a PostgreSQL requirement. The marker has to sit between two names
    that are *neighbours*, and the gap it sits in has to be a connector rather
    than a clause.
    """
    spans = _term_spans(text, vocabulary)
    if len(spans) < 2:
        return False
    spans.sort()
    for (_, left_end), (right_start, _) in pairwise(spans):
        gap = text[left_end:right_start]
        if len(gap) > _DISJUNCTION_GAP or ";" in gap or "." in gap:
            continue
        if _DISJUNCTION.search(gap):
            return True
    return False


def _clean_term(raw: str) -> str:
    """A matched span reduced to the name itself.

    The tag marker comes off the front, a separator that ended the match comes
    off the back: shape 4 stops at the Cyrillic in "CI/CD-пайплайнов" but still
    leaves the dash on. ``+`` and ``#`` are never trimmed, so "C++" and "C#"
    survive whole.
    """
    return raw.strip("#").rstrip("-. ").strip()


def _is_noise_term(term: str) -> bool:
    """Whether a name is an ordinary word wearing a technology's clothes.

    "II/III" is the case that made this a function rather than a set lookup:
    neither half is a technology, but the pair clears the shape test, and a
    requirement would then carry a "technology" the vacancy never asked about.
    """
    canonical = normalize(term).strip("+.#")
    if canonical in _TERM_NOISE:
        return True
    # Split the raw term, not the normalised one: ``normalize`` turns the
    # separator into a space, and "II/III" would come back as one part.
    parts = [normalize(part).strip("+.#") for part in re.split(r"[/+]", term)]
    parts = [part for part in parts if part]
    if not parts:
        return True
    # A part may itself be a phrase: "IAT Level II" is ordinary words all the
    # way through, so every word has to be checked, not just the whole.
    words = [word for part in parts for word in part.split()]
    return all(word in _TERM_NOISE for word in words)


def _is_part_of(term: str, other: str) -> bool:
    """Whether ``term`` is already inside ``other`` -- one name read twice.

    "GitLab" and "GitLab CI" are the same ask; so are "CD" and "CI/CD". A term
    that only repeats a longer one is dropped, and the longer spelling -- the
    one the vacancy actually wrote -- is kept.
    """
    if len(other) <= len(term):
        return False
    pattern = rf"(?<!\w){re.escape(normalize(term))}(?!\w)"
    return re.search(pattern, normalize(other)) is not None


def _list_item(item: str, vocabulary: dict[str, str]) -> bool:
    """Whether one item of a comma/slash run looks like a technology name.

    Split out because a run is only as good as its items. "Docker; опыт настройки
    CI/CD-пайплайнов (GitLab CI, GitHub Actions, Jenkins)" is one segment, and a
    naive split on the separators hands back "опыт настройки CI" and
    "CD-пайплайнов" as if they were technologies -- two names the candidate
    would have to answer for that the vacancy never asked for.

    The prose rule below applies to **multi-word** items only. It used to apply to
    every item, and that is what made ``macOS`` and ``iOS`` invisible: their first
    letter is lower-case, so "a phrase carrying a lowercase word is prose" threw
    them out, and the Ignition posting's whole Apple list came back empty. A
    single word cannot be a phrase, so it is judged by its shape instead.
    """
    item = _ITEM_CONJUNCTION.sub("", item).strip()
    if not (2 <= len(item) <= 30) or len(item.split()) > 3:
        return False
    if normalize(item) in vocabulary:
        return True
    # A multi-word item carrying a lowercase word is prose, not a name. "and
    # Azure DevOps" and "Working across UiPath" both hold a technology inside
    # them and are not technologies, so a conjunction must not be reported as a
    # name the candidate has no claim for. This runs before the shape search on
    # purpose: the shape search finds "DevOps" *inside* the phrase and would
    # vouch for it.
    if len(item.split()) > 1 and any(word[:1].islower() for word in item.split()):
        return False
    if any(pattern.search(item) for pattern in _TERM_SHAPES):
        return True
    # A plain capitalised name with no other distinguishing mark: "Jenkins",
    # "Power Platform", "Tenable SC". This also rejects "CD-пайплайнов", what a
    # naive split of "CI/CD-пайплайнов" leaves behind -- the run cannot
    # manufacture a name out of the Russian word that followed the slash.
    return all(re.fullmatch(r"[A-Z][A-Za-z0-9.+#-]*", word) for word in item.split())


def _run_items(run: str, vocabulary: dict[str, str]) -> list[str]:
    """The technology-looking items of one comma/slash run, in order.

    The leading conjunction comes off before the item is judged *and* before it
    is returned: "…, and MDM" has to be read as the name "MDM", or the term the
    brief reports is the phrase "and MDM".
    """
    items = [_ITEM_CONJUNCTION.sub("", _clean_term(item)).strip() for item in re.split(r"[,;/]", run)]
    return [item for item in items if item and _list_item(item, vocabulary)]


def _run_declares_itself(items: list[str], vocabulary: dict[str, str]) -> bool:
    """Whether a run is a list of technologies rather than prose with commas.

    One item the vocabulary knows, or one item with a technology shape, or one
    all-caps item, is enough -- which is how a plain capitalised name like
    "Jenkins" is read as a technology when it sits between "GitLab CI" and
    "GitHub Actions", and as an ordinary word when it does not.
    """
    return any(
        normalize(item) in vocabulary
        or any(pattern.search(item) for pattern in _TERM_SHAPES)
        or _ACRONYM.fullmatch(item)
        for item in items
    )


def _named_terms(text: str, vocabulary: dict[str, str]) -> set[str]:
    """Technology-shaped names in ``text``, whether or not the KB knows them.

    The whole point is that this does **not** ask the knowledge base what the
    requirement is about -- only, later, whether a name it found is one the
    candidate has claimed. The earlier version asked the vocabulary, so the
    requirement "Docker; опыт настройки CI/CD-пайплайнов (GitLab CI, GitHub
    Actions, Jenkins)" came back naming only Docker: the three names the
    candidate has no claim for were invisible, and the brief reported the whole
    requirement as confirmed by Docker.
    """
    found: set[str] = set()
    for pattern in _TERM_SHAPES:
        for match in pattern.finditer(text):
            term = _clean_term(match.group(0))
            if term:
                found.add(term)

    for run in _LIST_RUN.finditer(text):
        items = _run_items(run.group(0), vocabulary)
        if len(items) < 2 or not _run_declares_itself(items, vocabulary):
            continue
        found.update(items)

    # Parenthesised runs, which :data:`_LIST_RUN` refuses to enter. Bounded by a
    # stricter self-declaration: three or more capitalised names separated by a
    # comma or slash inside a parenthesis is a product list, not a sentence.
    for match in _BRACKET_RUN.finditer(text):
        items = _run_items(match.group(1), vocabulary)
        if len(items) < _MIN_BRACKET_LIST:
            continue
        found.update(items)

    # Product names that are neither a comma list nor a shape: "Google Workspace"
    # (the KB knows the word, not the phrase) and "Microsoft 365" (the KB knows
    # neither, but the shape is unambiguous). Both are bounded rules rather than
    # "any two capitalised words", which is what would turn "Help Desk",
    # "Technical Support" and "Business Hours" into technologies.
    found.update(_technology_led_phrases(text, vocabulary))
    found.update(_versioned_products(text))

    kept = {
        term
        for term in found
        if len(term) >= 2 and not _is_noise_term(term)
    }
    return {
        term for term in kept if not any(_is_part_of(term, other) for other in kept)
    }


def _unrecognised_terms(text: str, vocabulary: dict[str, str]) -> set[str]:
    """Names the requirement uses that no knowledge-base fact can answer.

    Reported as *not recognised by the available facts*, never as "the candidate
    lacks it": the KB holds what the candidate claimed, not what he can do, and a
    name missing from it is a limit of this brief rather than a fact about him.
    That distinction is the whole reason the wording below is about the facts and
    not about the person.
    """
    known = _canonical_terms_in(text, vocabulary)
    out: set[str] = set()
    for term in _named_terms(text, vocabulary):
        canonical = normalize(term).strip("+.#")
        if canonical in vocabulary:
            # The vocabulary knows the name, so ``_canonical_terms_in`` already
            # resolved it and it is not missing evidence.
            continue
        if any(canonical in value or value in canonical for value in known):
            continue
        out.add(term)
    return out


def _answered_by(by: set[str], wanted: set[str], disjunctive: bool) -> bool:
    """Whether the technologies in ``by`` answer the requirement's technology ask.

    A disjunction is answered by one alternative, a conjunction by all of them,
    and a requirement naming no technology is answered by anything.
    """
    if not wanted:
        return True
    if disjunctive:
        return bool(wanted & by)
    return wanted <= by


def _first_hit_position(value: str, hits: set[str]) -> int:
    """Where the first matched technology appears in a claim, or ``len(value)``.
    A tie-breaker, not a filter: it only orders claims that matched equally many
    technologies. Whole words only, because a substring search would find "go"
    inside "django" and rank on an accident.
    """
    lowered = value.lower()
    positions: list[int] = []
    for hit in hits:
        match = re.search(rf"(?<!\w){re.escape(hit)}(?!\w)", lowered)
        if match:
            positions.append(match.start())
    return min(positions) if positions else len(value)


def _match_claims(
    requirement: Requirement,
    kb: CandidateKnowledgeBase,
    vocabulary: dict[str, str],
    domains: frozenset[str],
) -> list[Any]:
    """KB records that genuinely speak to this requirement.

    Matching runs on *canonical technology names* and *work-area words*, never
    on raw word overlap, and a work-area overlap has to clear
    :data:`_MIN_DOMAIN_HITS`. That is the whole difference between evidence and
    coincidence: the previous draft compared requirement text against whole
    claim values, so a requirement mentioning "with" matched a project bullet
    containing "with", and the brief reported the candidate's own project as
    covering phone support.

    A ``duty`` claim is matched through the technologies and work areas it
    names, which is what makes class A reachable at all -- duties are the only
    claims the KB ties to an employer, and their full sentences are far too long
    to appear verbatim in a vacancy.
    """
    wanted_tech = _canonical_terms_in(requirement.text, vocabulary)
    wanted_domain = _domain_terms_in(requirement.text, domains)
    asked = _equivalents_asked(requirement.text)
    if not wanted_tech and not wanted_domain and not asked:
        return []

    matched: list[tuple[int, int, int, Any]] = []
    seen: set[str] = set()
    for record in kb.claims.values():
        if not record.in_source:
            continue
        if record.kind in _NEVER_CLAIM_KINDS or record.kind not in _EVIDENCE_KINDS:
            continue

        record_tech = _canonical_terms_in(record.value, vocabulary)
        record_domain = _domain_terms_in(record.value, domains)
        tech_hits = record_tech & wanted_tech

        if record.kind == "duty":
            # A duty answers the requirement when it names a technology the
            # vacancy asks for, when it shares enough work-area words, or when a
            # declared professional equivalence connects the two wordings.
            domain_hits = record_domain & wanted_domain
            # A declared equivalence is sufficient on its own, and that is the
            # point of declaring it: the spec forbids demanding a literal
            # two-word match when the professional activity is plainly the same,
            # and "at least 2 years of help desk experience" shares *no* work-area
            # word with "Provided technical support and troubleshooting for
            # business users" while describing exactly that job. What bounds this
            # is the table, not a word count -- the equivalence has to be written
            # down, both halves have to match, and the fact it may rest on is
            # named by id.
            equiv_hits = _equivalents_fired(record, asked)
            speaks_to = (
                bool(tech_hits)
                or bool(equiv_hits)
                or len(domain_hits) >= _MIN_DOMAIN_HITS
            )
        elif record.kind in _TECHNOLOGY_KINDS:
            speaks_to = normalize(record.value).strip("+.#") in wanted_tech or bool(tech_hits)
        else:
            # Prose claims (project bullets, summaries, skill groups) are
            # evidence only when they name a wanted technology themselves. They
            # are never matched on work-area words, so a project bullet cannot
            # be dragged in to answer a question about paid experience.
            speaks_to = bool(tech_hits)

        if speaks_to and record.claim_id not in seen:
            seen.add(record.claim_id)
            matched.append(
                (
                    len(tech_hits),
                    # A duty outranks a role_title at equal relevance: "Administered
                    # self-hosted Bitrix24" says what was done, while the role title
                    # only says which job it was -- and the job is already printed
                    # in the parenthetical next to it.
                    0 if record.kind == "duty" else 1,
                    _first_hit_position(record.value, tech_hits),
                    record,
                )
            )

    # Strongest evidence first, and among equals the claim that is *about* the
    # technology beats the one that mentions it in passing. Both facts are true;
    # only one is persuasive. Measured on the Bitrix24 duties: the one that
    # administered self-hosted Bitrix24 names it at character 25, the voice
    # pipeline names it at 108, as a place to store the result.
    matched.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in matched]


def _partial_terms(requirement: Requirement, kb: CandidateKnowledgeBase) -> list[tuple[str, str]]:
    """Adjacent-but-not-equal technologies, e.g. the vacancy says PostgreSQL and
    the KB only claims SQL. Returned as ``(requested, claimed)`` pairs."""
    vocabulary = _kb_vocabulary(kb)
    claimed = set(vocabulary.values())
    pairs: list[tuple[str, str]] = []
    for token in _tokens(requirement.text):
        if len(token) < 3 or token in vocabulary:
            continue
        for term in claimed:
            if len(term) < 3 or term == token:
                continue
            if token.startswith(term) or term.startswith(token):
                pair = (token, term)
                if pair not in pairs:
                    pairs.append(pair)
    return pairs


#: Spelled-out numbers this data actually uses. ``matcher.extract_required_years``
#: only understands the digit forms ("3+ years", "5+ лет"), and the vacancies
#: write the threshold in words just as often.
_YEAR_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "один": 1,
    "одного": 1,
    "два": 2,
    "двух": 2,
    "три": 3,
    "трёх": 3,
    "трех": 3,
    "четыре": 4,
    "четырёх": 4,
    "четырех": 4,
    "пять": 5,
    "пяти": 5,
    "шесть": 6,
    "шести": 6,
    "семь": 7,
    "семи": 7,
    "восемь": 8,
    "восьми": 8,
    "девять": 9,
    "девяти": 9,
    "десять": 10,
    "десяти": 10,
}
_YEAR_UNIT = r"(?:years?|yrs?|лет|года|год)"
_YEAR_DIGIT_RE = re.compile(
    r"\b(?P<digit>\d{1,2})\s*\+?\s*" + _YEAR_UNIT + r"\b",
    re.IGNORECASE,
)
_YEAR_SPELLED_RE = re.compile(
    r"\b(?:"
    + "|".join(sorted(_YEAR_WORDS, key=len, reverse=True))
    + r")\s*(?:\(\s*\d{1,2}\s*\)\s*)?(?:or\s+more\s+|plus\s+)?"
    + _YEAR_UNIT
    + r"\b",
    re.IGNORECASE,
)


def _years_named(text: str) -> int | None:
    """The smallest year threshold the text names, in digits or in words.

    The matcher is tried first so the digit forms keep their existing meaning;
    the local patterns only fill in what it cannot see. Measured need: on the
    real DMI vacancy the threshold arrives as "At least five (5) years of
    Technical Lead experience" and "At least ten (10) years of hands-on
    experience", and the matcher returns ``None`` for both -- so the caveat
    below silently did not fire on precisely the requirements it exists for.
    """
    from ..matcher import extract_required_years

    found = extract_required_years("", text)
    if found:
        return int(found)
    seen: list[int] = []
    for match in _YEAR_DIGIT_RE.finditer(text):
        seen.append(int(match.group("digit")))
    for match in _YEAR_SPELLED_RE.finditer(text):
        word = match.group(0).split(maxsplit=1)[0].lower()
        if word in _YEAR_WORDS:
            seen.append(_YEAR_WORDS[word])
    return min(seen) if seen else None


def _technology_tenure_caveat(requirement: Requirement) -> str:
    """A sentence for requirements that name a number of years.

    "3+ years of commercial Python" is two claims at once: that Python was used
    for money, and that it was used for three years. The KB answers the first
    and cannot answer the second -- nothing in it measures years *per
    technology*. The candidate's total IT experience is not a substitute, so the
    brief says so instead of quietly letting 15 years of IT stand in for 3 years
    of Python.
    """
    years = _years_named(requirement.text)
    if not years:
        return ""
    subject = "по этой технологии" if requirement.technologies else "по этому требованию"
    return (
        f" Вакансия называет срок {years}+ лет; стаж именно {subject} в KB "
        "не измеряется, и общий IT-стаж сюда не подставляется."
    )


def _duration_split(
    requirement: Requirement,
    evidence_class: EvidenceClass,
    evidence: list[Evidence],
) -> tuple[EvidenceClass, str] | None:
    """Demote a year-threshold requirement whose duration the KB cannot measure.

    "Have at least 2 years of IT support experience on the job" is two
    independent checks, not one: whether the work was done, and whether it was
    done for that long. The KB answers the first and cannot answer the second --
    nothing in it measures years *per requirement* -- so a class A here would
    report the whole ask as closed while half of it is open. The class drops to C
    and the two halves are stated apart, which is what T7.2 section 3 asks for.

    Returns ``(new_class, explanation)`` when the split applies, ``None``
    otherwise. Only A and B are demoted: C, D and E already say the requirement is
    not fully answered, and overwriting their explanation would lose the more
    specific reason they carry.

    The caveat sentence from :func:`_technology_tenure_caveat` is reused verbatim
    rather than rewritten, so the phone view, the detailed view and the tests all
    keep reading the same words about the unmeasured duration.
    """
    caveat = _technology_tenure_caveat(requirement)
    if not caveat:
        return None
    if evidence_class not in (
        EvidenceClass.A_CONFIRMED_EXPERIENCE,
        EvidenceClass.B_OWN_PROJECT,
    ):
        return None
    where = ", ".join(sorted({item.where for item in evidence})[:3])
    if evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE:
        presence = "сама деятельность подтверждена оплачиваемой работой"
        origin = ""
    else:
        presence = "сама деятельность подтверждена собственным проектом"
        origin = " (не оплачиваемая работа)"
    if where:
        presence = f"{presence}: {where}{origin}"
    return EvidenceClass.C_PARTIAL, f"Подтверждено: {presence}. Не подтверждено:{caveat}"


def _quoted_terms(terms: set[str]) -> str:
    """Render canonical technology names the way the report quotes them."""
    return ", ".join(f"«{term}»" for term in sorted(terms))


def _coverage_gap_note(
    wanted: set[str],
    paid_covered: set[str],
    own_covered: set[str],
    resume_covered: set[str],
    *,
    commercial_expected: bool,
) -> str:
    """Explain why a requirement that names a technology is not class A or B.

    Class A is a *conjunction*: the candidate did the work **and** the work used
    the technology the vacancy asked for. ``paid_covered != wanted`` means only
    the first half holds, and the note has to name the missing half. A class that
    drops with no reason on the line is a class the next reader raises back, and
    the substitution it undoes -- "SQL" answering "PostgreSQL", "Bash and
    PowerShell" answering "Python" -- is exactly the over-claim the candidate
    would have to defend in an interview.

    ``commercial_expected`` picks the yardstick: class A is paid work, class B is
    the candidate's own projects. The rule only ever lowers a class, so a wrong
    guess here costs a sentence in a cover letter, not a false claim on a resume.
    """
    asked = _quoted_terms(wanted)
    named: list[str] = []
    if paid_covered:
        named.append(f"оплачиваемые обязанности — {_quoted_terms(paid_covered)}")
    if own_covered:
        named.append(f"собственные проекты — {_quoted_terms(own_covered)}")
    if resume_covered:
        named.append(f"резюме — {_quoted_terms(resume_covered)}")
    evidence_names = "; ".join(named)

    if commercial_expected:
        if paid_covered:
            return (
                f"Требование называет {asked}, а оплачиваемые обязанности — только "
                f"{_quoted_terms(paid_covered)}. Совпадение частичное: класс A требует "
                "оплачиваемой работы именно с этой технологией, а не с соседней."
            )
        if evidence_names:
            return (
                f"Требование называет {asked}, но ни одна оплачиваемая обязанность эту "
                f"технологию не называет — она есть только вне работы ({evidence_names}). "
                "Личный проект и раздел навыков не заменяют коммерческого опыта."
            )
        return (
            f"Требование называет {asked}, и ни одно доказательство в KB эту технологию не "
            "называет. Класс A не ставится."
        )

    if own_covered:
        return (
            f"Требование называет {asked}, а собственные проекты — только "
            f"{_quoted_terms(own_covered)}. Класс B ставится, только когда проект называет "
            "саму технологию."
        )
    if evidence_names:
        return (
            f"Требование называет {asked}, но собственные проекты эту технологию не "
            f"называют ({evidence_names}). Класс B не ставится."
        )
    return (
        f"Требование называет {asked}, и ни одно доказательство в KB эту технологию не "
        "называет. Класс понижен."
    )


def _uncovered_terms_note(wanted: set[str], covered: set[str]) -> str:
    """Name the technologies a requirement asks for that no evidence mentions.

    Used when the class was already lowered for another reason: the branch above
    has said *why* it is partial, and this adds *which* technology is still
    missing, so the two sentences answer different questions.
    """
    missing = wanted - covered
    if not missing:
        return ""
    rest = (
        f" доказательства называют {_quoted_terms(covered)}"
        if covered
        else " доказательства не называют ни одной"
    )
    return f"Не покрыто доказательствами: {_quoted_terms(missing)};{rest}."


def _unrecognised_terms_note(terms: tuple[str, ...]) -> str:
    """Say that a requirement names things the available facts cannot answer.

    The wording carries the whole point of the check. The knowledge base holds
    what the candidate has *claimed*, so a name missing from it says nothing
    about what he can do -- it says this brief cannot answer. Writing "у
    кандидата этого нет" would turn a limit of the tool into a statement about
    the person, and he would then have to argue with it in an interview.
    """
    if not terms:
        return ""
    quoted = ", ".join(f"«{term}»" for term in terms)
    return (
        f"Не подтверждено доступными фактами: {quoted}. "
        "Этих названий нет в KB, поэтому справка не может ни подтвердить, ни опровергнуть "
        "их — это ограничение справки, а не отсутствие навыка у кандидата."
    )


def _evidence_rank(item: Evidence, evidence_class: EvidenceClass) -> int:
    """Sort key putting the kind of evidence the class promises first.

    Class A is a claim about *paid* work, so it leads with the role-linked
    duty. Class B is a claim about the candidate's own project, so it leads
    with the project. Everything else is a claim about what the resume says, so
    resume claims lead. Within a rank the caller's relevance order survives,
    because the sort is stable.
    """
    if evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE:
        return 0 if item.is_commercial else 1
    if evidence_class is EvidenceClass.B_OWN_PROJECT:
        return 0 if item.origin == "project" else 1
    return 0 if item.origin == "resume" else 1


def assess_requirements(
    facts: VacancyFacts,
    kb: CandidateKnowledgeBase,
    tenure: Tenure,
) -> list[Assessment]:
    """Classify every requirement into A-E, with the evidence that decided it."""
    vocabulary = _kb_vocabulary(kb)
    domains = _kb_domains(kb)
    role_titles = _role_titles(kb)
    project_names = _project_names(kb)
    own_work_roles = _own_work_roles()
    assessments: list[Assessment] = []

    for requirement in facts.must_have + facts.nice_to_have:
        records = _match_claims(requirement, kb, vocabulary, domains)
        evidence = [
            _evidence_for(r, role_titles, project_names, own_work_roles) for r in records
        ]

        # A requirement that names technologies is answered by technologies.
        # ``wanted`` is exactly the set ``_match_claims`` selected on, so the two
        # cannot disagree about what the requirement is about.
        wanted = _canonical_terms_in(requirement.text, vocabulary)
        named = {id(item): _canonical_terms_in(item.value, vocabulary) for item in evidence}
        if wanted:
            # Drop records that name none of them. They got in on shared
            # work-area words, and a work-area word is not a technology: on the
            # Python Developer posting a duty about Bash and PowerShell was
            # listed as evidence for a requirement whose subject was PostgreSQL.
            evidence = [item for item in evidence if named[id(item)] & wanted]
        covered = {id(item): named[id(item)] & wanted for item in evidence}
        paid_covered: set[str] = set()
        own_covered: set[str] = set()
        resume_covered: set[str] = set()
        for item in evidence:
            if item.is_commercial:
                paid_covered |= covered[id(item)]
            elif item.is_own_work:
                own_covered |= covered[id(item)]
            else:
                resume_covered |= covered[id(item)]

        commercial = [
            e for e in evidence if e.is_commercial and e.claim_kind == "duty"
        ]
        projects = [e for e in evidence if e.is_own_work]
        self_described = [e for e in evidence if e.origin == "resume" and e.claim_kind in _SELF_DESCRIPTION_KINDS]
        other_resume = [e for e in evidence if e.origin == "resume" and e not in commercial and e not in self_described]

        evidence_class = EvidenceClass.E_UNCONFIRMED
        explanation = NO_EVIDENCE_CAVEAT

        if requirement.kind is RequirementKind.COMMERCIAL_EXPERIENCE:
            if commercial and tenure.has_paid_work:
                evidence_class = EvidenceClass.A_CONFIRMED_EXPERIENCE
                explanation = (
                    f"Оплачиваемая работа: {', '.join(sorted({e.where for e in commercial})[:3])}."
                )
            elif projects or self_described:
                evidence_class = EvidenceClass.C_PARTIAL
                if projects:
                    explanation = (
                        "Технология подтверждается только собственным проектом "
                        f"({', '.join(sorted({e.where for e in projects})[:2])}), а требование — "
                        "о коммерческом опыте. Личный проект не является оплачиваемой работой."
                    )
                else:
                    explanation = (
                        "Технология заявлена в разделе навыков резюме, но ни одна обязанность "
                        "не привязана к ней в рамках роли. Требуется уточнение."
                    )
            elif tenure.has_paid_work:
                evidence_class = EvidenceClass.E_UNCONFIRMED
                explanation = (
                    "Опыт работы в KB есть, но именно это требование в нём не подтверждено. "
                    + NO_EVIDENCE_CAVEAT
                )

        elif requirement.kind is RequirementKind.SELF_DIRECTED:
            if projects:
                evidence_class = EvidenceClass.B_OWN_PROJECT
                explanation = (
                    "Собственная разработка подтверждена каталогом проектов: "
                    f"{', '.join(sorted({e.where for e in projects})[:3])}."
                )
            elif commercial:
                evidence_class = EvidenceClass.C_PARTIAL
                explanation = (
                    "Есть только коммерческая работа, отдельной самостоятельной разработки "
                    "в источниках нет."
                )

        else:  # TOOL_USE / GENERAL
            if commercial:
                evidence_class = EvidenceClass.A_CONFIRMED_EXPERIENCE
                explanation = (
                    f"Указано в обязанностях роли: {', '.join(sorted({e.where for e in commercial})[:3])}."
                )
                if projects and not self_described:
                    explanation += (
                        " Дополнительно подтверждено собственным проектом — "
                        "в отклике это стоит разделять."
                    )
            elif projects:
                evidence_class = EvidenceClass.B_OWN_PROJECT
                explanation = (
                    "Подтверждается только собственным проектом "
                    f"({', '.join(sorted({e.where for e in projects})[:3])}), "
                    "коммерческого применения в KB нет."
                )
            elif self_described:
                evidence_class = EvidenceClass.D_NEEDS_CLARIFICATION
                explanation = (
                    "Есть в резюме в списке навыков, но без привязки к роли или проекту — "
                    "нужно уточнить, где именно применялось."
                )
            elif other_resume:
                evidence_class = EvidenceClass.D_NEEDS_CLARIFICATION
                explanation = "Найдено в резюме, но не как опыт работы. Требуется уточнение."

        if evidence_class is EvidenceClass.E_UNCONFIRMED:
            partial = _partial_terms(requirement, kb)
            if partial:
                evidence_class = EvidenceClass.C_PARTIAL
                pairs = ", ".join(f"«{a}» → в KB только «{b}»" for a, b in partial[:3])
                explanation = f"Точного совпадения нет, есть близкая технология: {pairs}."

        # -- presence of the experience vs its duration ----------------------
        # "Have at least 2 years of IT support experience on the job" is two
        # independent checks, not one: whether the work was done, and whether it
        # was done for that long. The KB answers the first and cannot answer the
        # second -- nothing in it measures years *per requirement* -- so leaving
        # the class at A would report the whole ask as closed while half of it is
        # open. The class drops to C and the two halves are stated apart.
        #
        # What must *not* happen is a substitution. The approved 15+ years of
        # total IT, ``years_experience = 3`` in the profile, another role's
        # duration and a technology's tenure are all the wrong number for this
        # question, and none of them is used -- the brief says the duration is
        # not established instead.
        duration_split = _duration_split(requirement, evidence_class, evidence)
        if duration_split:
            evidence_class, explanation = duration_split

        # A requirement that names a number of years is two claims at once: that
        # the skill was used at all, and that it was used for that long. The KB
        # answers the first and never the second, so the caveat rides along
        # whatever the class came from. It used to hang off the paid-work branch
        # only, which let "At least five (5) years of Technical Lead experience"
        # pass as a plain class-A match -- a 15-year IT history read as five
        # years of technical leadership, which is the substitution the spec
        # forbids. Kept in a variable because the unrecognised-name branch below
        # rebuilds the line and has to carry this across.
        #
        # Skipped when the split above already said it: one sentence about the
        # unmeasured duration is a fact, two are noise.
        tenure_caveat = "" if duration_split else _technology_tenure_caveat(requirement)
        explanation += tenure_caveat

        # -- technology containment ------------------------------------------
        # A fact confirms a technology only if it names that technology. Class A
        # is a claim about a *conjunction*: that the candidate did this work and
        # that the work used the technology the vacancy asked for. Partial
        # coverage satisfies the first and not the second, and reporting it as A
        # is how "SQL" became "PostgreSQL" and "Bash/PowerShell" became "Python"
        # in the T6.1 output.
        #
        # The rule is deliberately one-directional: it can only ever lower a
        # class, never raise one. Under-claiming costs a sentence in a cover
        # letter; over-claiming costs the candidate the interview.
        #
        # Whether the technology set is a conjunction or a choice is read off
        # the requirement's own wording (see :func:`_answered_by`). "SQL and
        # PostgreSQL" needs both; "Python or PowerShell" is answered by either,
        # and demanding both there would demote a requirement the candidate
        # genuinely meets.
        missing = wanted - (paid_covered | own_covered | resume_covered)
        if wanted and evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE:
            if not _answered_by(paid_covered, wanted, requirement.disjunctive):
                evidence_class = EvidenceClass.C_PARTIAL
                explanation = _coverage_gap_note(
                    wanted, paid_covered, own_covered, resume_covered, commercial_expected=True
                )
        elif wanted and evidence_class is EvidenceClass.B_OWN_PROJECT:
            if not _answered_by(own_covered, wanted, requirement.disjunctive):
                evidence_class = EvidenceClass.C_PARTIAL
                explanation = _coverage_gap_note(
                    wanted, paid_covered, own_covered, resume_covered, commercial_expected=False
                )
        elif wanted and missing and evidence_class is EvidenceClass.C_PARTIAL:
            # Keep the reason the class branch already gave ("a personal project
            # is not paid work") and add which technology is missing: the two
            # answer different questions.
            note = _uncovered_terms_note(wanted, paid_covered | own_covered | resume_covered)
            if note:
                explanation += " " + note

        # Names the requirement uses that no KB fact can answer. Appended last
        # and phrased about the facts rather than about the candidate: the KB
        # holds what he claimed, so a name missing from it is a limit of this
        # brief, not a missing skill. Without this the CI/CD line was reported
        # as answered by Docker, because the three names it also asks for had
        # nowhere to appear.
        unrecognised_note = _unrecognised_terms_note(requirement.unrecognised)
        if unrecognised_note:
            if not wanted and evidence_class in (
                EvidenceClass.A_CONFIRMED_EXPERIENCE,
                EvidenceClass.B_OWN_PROJECT,
            ):
                # Every name in the requirement is one no fact can answer, so
                # nothing here can be *confirmed*. The work-area evidence that
                # matched would be answering a question about something else --
                # the substitution T6.2 forbids -- so it comes off the line too.
                # Only A and B are overridden: C, D and E already say "not
                # confirmed" and carry a more specific reason than a blanket
                # "no evidence", which the note then sits beside.
                evidence_class = EvidenceClass.E_UNCONFIRMED
                evidence = []
                # Rebuilt rather than patched, so the tenure caveat is carried
                # over by hand: it belongs to the requirement's *shape* (it
                # names a number of years), not to the class it landed in.
                explanation = f"{NO_EVIDENCE_CAVEAT}{tenure_caveat} {unrecognised_note}"
            else:
                if evidence_class in (
                    EvidenceClass.A_CONFIRMED_EXPERIENCE,
                    EvidenceClass.B_OWN_PROJECT,
                ):
                    # A class says the requirement *as a whole* is answered. A
                    # name no fact can answer is an unanswered part of it, so
                    # the class drops: this is what stops "Docker; опыт настройки
                    # CI/CD-пайплайнов (GitLab CI, GitHub Actions, Jenkins)" from
                    # being reported as confirmed because Docker alone is. Only
                    # ever a demotion -- an unrecognised name cannot raise one.
                    evidence_class = EvidenceClass.C_PARTIAL
                    explanation += (
                        " Требование подтверждено только в той части, которую называют "
                        "факты; остальное справка не покрывает."
                    )
                explanation += " " + unrecognised_note

        # A declared professional equivalence, printed only when it is what
        # connected the ask to the fact, and naming both halves plus the claim id
        # so the reader can check the connection by hand. Appended last so it
        # survives every branch above -- including the unrecognised-name branch,
        # which rebuilds the explanation from scratch.
        equivalence_note = _equivalence_note(requirement, evidence)
        if equivalence_note and evidence:
            explanation += " " + equivalence_note

        # The heading promises a kind of evidence, so that kind has to lead.
        # The short view prints only the first two lines of each requirement,
        # and relevance alone put the wrong kind first: on ``hh:136706048`` the
        # PostgreSQL requirement opened with two Reels Analyzer bullets under
        # the heading "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ", because a ``project_stack``
        # claim names more technologies than any duty and won the match sort.
        # Sorting here rather than in :func:`_match_claims` keeps the rule where
        # the class is actually known, and the sort is stable, so within a kind
        # the most relevant claim still comes first.
        evidence.sort(key=lambda e: _evidence_rank(e, evidence_class))

        assessments.append(Assessment(requirement, evidence_class, evidence, explanation))

    return assessments


# ----------------------------------------------------------------------
# Preferences, tenure and conflicts
# ----------------------------------------------------------------------

_MIN_INCOME_RE = re.compile(r"\$\s*(\d{3,5})|(\d{3,5})\s*(?:usd|долл)", re.IGNORECASE)
_SINCE_RE = re.compile(r"с\s+([а-я]+\s+\d{4})", re.IGNORECASE)


def read_preferences(kb: CandidateKnowledgeBase) -> PreferenceSet:
    """Read the five confirmed preferences out of the KB.

    Looked up by claim *kind* among the confirmed records, not by claim_id: an
    id is content-derived, so it changes the moment the wording does, and the
    brief must survive a re-worded confirmation.
    """
    prefs = PreferenceSet()
    for record in kb.by_status(ClaimStatus.CONFIRMED_BY_USER):
        prefs.claim_ids[record.kind] = record.claim_id
        if record.kind == "preference_condition":
            match = _MIN_INCOME_RE.search(record.value)
            if match:
                prefs.min_income_usd = int(match.group(1) or match.group(2))
        elif record.kind == "goal_direction":
            prefs.priority = record.value
        elif record.kind == "preference_role":
            prefs.bitrix24_on_par = "bitrix" in record.value.lower() and "не исключ" in record.value.lower()
        elif record.kind == "experience_note":
            since = _SINCE_RE.search(record.value)
            prefs.own_projects_since = since.group(1) if since else ""
            prefs.own_projects_unpaid = "без оплачиваемых заказов" in record.value.lower()
        elif record.kind == "chronology_note":
            prefs.chronology_note = record.value
    return prefs


def _parse_point(text: str, *, end: bool) -> tuple[int, int] | None:
    """Parse ``2018`` / ``April 2021`` / ``Present`` into ``(year, month)``."""
    cleaned = str(text).strip()
    if not cleaned:
        return None
    if _PRESENT_RE.search(cleaned):
        today = _today()
        return (today[0], today[1])
    year_match = re.search(r"(19|20)\d{2}", cleaned)
    if not year_match:
        return None
    year = int(year_match.group(0))
    month = 12 if end else 1
    lowered = cleaned.lower()
    for name, number in _MONTHS.items():
        if name in lowered:
            month = number
            break
    return (year, month)


def _today() -> tuple[int, int]:
    from datetime import date

    # A local calendar date is the right input here: the question is which
    # month the tenure runs to, and DTZ011's concern is a naive *timestamp*
    # crossing a timezone. Pinned rather than silenced.
    now = date.today()  # noqa: DTZ011 - a calendar date, not an instant
    return (now.year, now.month)


def _months_between(start: tuple[int, int], end: tuple[int, int]) -> int:
    return max(0, (end[0] - start[0]) * 12 + (end[1] - start[1]) + 1)


def _merge_intervals(intervals: list[tuple[tuple[int, int], tuple[int, int]]]) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda pair: pair[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        # Overlapping or touching (a one-month gap is not a career break).
        if _months_between(last_end, start) <= 2:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def compute_tenure(kb: CandidateKnowledgeBase, profile: dict[str, Any] | None = None) -> Tenure:
    """Compute IT tenure from the chronology, never from ``years_experience``.

    Two things this function must *not* do, both of which an earlier draft did:

    * read ``years_experience`` as a career total. It is 3, and what the field
      means is not established -- it may be a specialism, a filter setting, or
      stale. It is reported as "not established", not as a specialism total.
    * publish ``15.3`` / ``14.9`` as confirmed figures. They are an estimate from
      month-precision arithmetic over year-only dates, so the brief prints the
      approved wording (:data:`APPROVED_TOTAL_EXPERIENCE`) and keeps these for
      the diagnostic view only.

    Year-only dates are read as January for a start and December for an end --
    the standard reading of "2009 – 2018", and stated here because it moves the
    number by up to a year.
    """
    tenure = Tenure(method="объединение периодов ролей из KB; год без месяца — январь/декабрь")

    starts: dict[str, tuple[int, int] | None] = {}
    ends: dict[str, tuple[int, int] | None] = {}
    for record in kb.claims.values():
        if not record.role_id:
            continue
        if record.kind == "role_start":
            starts[record.role_id] = _parse_point(record.value, end=False)
        elif record.kind == "role_end":
            ends[record.role_id] = _parse_point(record.value, end=True)

    intervals: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for role_id, start in starts.items():
        if start is None:
            continue
        end = ends.get(role_id) or _today()
        end = max(end, start)
        intervals.append((start, end))
        tenure.intervals.append(f"{start[0]}-{start[1]:02d} → {end[0]}-{end[1]:02d}")

    merged = _merge_intervals(intervals)
    if merged:
        tenure.earliest_year = merged[0][0][0]
        total_months = sum(_months_between(start, end) for start, end in merged)
        tenure.total_it_years = round(total_months / 12, 1)
    else:
        tenure.total_it_years = 0.0

    # The unpaid stretch: the candidate confirmed that the current role is own
    # projects with no paid orders. It counts toward the IT timeline and must
    # not count as commercial experience.
    unpaid_months = 0
    for role_id, start in starts.items():
        if start is None:
            continue
        role_titles = {r.value for r in kb.claims.values() if r.kind == "role_title" and r.role_id == role_id}
        if any("freelance" in t.lower() for t in role_titles) or any(
            "собственн" in r.value.lower() and r.role_id == role_id for r in kb.claims.values()
        ):
            end = ends.get(role_id) or _today()
            unpaid_months += _months_between(start, end)
    tenure.unpaid_years = round(unpaid_months / 12, 1)
    tenure.commercial_years = round(max(0.0, tenure.total_it_years - tenure.unpaid_years), 1)

    if profile:
        raw = profile.get("years_experience")
        try:
            tenure.profile_years = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            tenure.profile_years = None

    if tenure.profile_years is not None:
        tenure.profile_is_total = bool(
            tenure.total_it_years and abs(tenure.profile_years - tenure.total_it_years) <= 1.0
        )
        if not tenure.profile_is_total:
            # Deliberately silent about *what* the field means. Saying it "описывает
            # специализацию" was an interpretation nobody approved: the value is 3
            # and its meaning is simply not established. The approved wording is
            # printed on the line above, so it is not repeated here.
            tenure.note = (
                f"В профиле years_experience={tenure.profile_years}; что именно означает это поле, "
                f"не установлено, поэтому оно не используется ни как общий стаж, ни как стаж "
                f"в специализации."
            )
    return tenure


def detect_conflicts(
    preferences: PreferenceSet,
    profile: dict[str, Any] | None,
    tenure: Tenure,
) -> list[Conflict]:
    """List where a live rule disagrees with a confirmed preference.

    T6 does not change any of these. It reports them, because the spec forbids
    touching the search filters inside this task.
    """
    conflicts: list[Conflict] = []
    if not profile:
        return conflicts

    if preferences.min_income_usd is not None:
        current = profile.get("minimum_salary")
        if current is not None and int(current) != preferences.min_income_usd:
            direction = "выше" if int(current) > preferences.min_income_usd else "ниже"
            conflicts.append(
                Conflict(
                    where="candidate_profile.json → minimum_salary",
                    current=f"{current} {profile.get('salary_currency') or ''}".strip(),
                    expected=f"{preferences.min_income_usd} USD (подтверждено на интервью)",
                    why=(
                        f"Порог в профиле {direction} подтверждённого. "
                        f"Слишком высокий порог отсекает вакансии, которые кандидат готов рассматривать."
                        if int(current) > preferences.min_income_usd
                        else "Порог ниже подтверждённого — фильтр пропустит то, что кандидат не хочет."
                    ),
                )
            )

    if preferences.bitrix24_on_par:
        hits: list[str] = []
        for key in ("excluded_roles", "must_avoid_conditions"):
            values = profile.get(key) or []
            if any("bitrix" in str(v).lower() for v in values):
                hits.append(key)
        if hits:
            conflicts.append(
                Conflict(
                    where="candidate_profile.json → " + ", ".join(hits),
                    current="Bitrix24 в списке исключений",
                    expected="рассматривать наравне с другими вакансиями (подтверждено на интервью)",
                    why=(
                        "Профиль активно исключает Bitrix24, хотя кандидат подтвердил, что такие "
                        "вакансии нужно рассматривать. Это прямое противоречие."
                    ),
                )
            )

    if tenure.profile_years is not None and not tenure.profile_is_total:
        conflicts.append(
            Conflict(
                where="candidate_profile.json → years_experience / years_of_experience",
                current=str(tenure.profile_years),
                expected="не подставлять; утверждено только «15+ лет в IT»",
                why=(
                    "Что именно означает это поле, не установлено, поэтому подставлять туда "
                    "число нельзя. Вакансии с требованием «N+ лет опыта» нельзя оценивать "
                    "по этому полю."
                ),
            )
        )

    try:
        from ..hh_autonomous_agent import DEFAULT_SEARCH_QUERIES

        queries = list(DEFAULT_SEARCH_QUERIES)
    except Exception as exc:  # noqa: BLE001 - an import guard must not crash the brief
        # Not silent: if the query list cannot be read, the search-filter check
        # did not run, and saying nothing would read as "the filters are fine".
        queries = []
        conflicts.append(
            Conflict(
                where="hh_autonomous_agent.DEFAULT_SEARCH_QUERIES",
                current="не удалось прочитать список запросов",
                expected="проверить вручную",
                why=(
                    "Проверка поисковых запросов не выполнена, поэтому отсутствие "
                    f"конфликта по ним ничего не подтверждает. Причина: {exc}"
                ),
            )
        )
    if queries:
        joined = " ".join(queries).lower()
        missing = [
            label
            for label, needles in (
                ("IT Support", ("support", "поддержк")),
                ("System Administration", ("sysadmin", "system admin", "системный администратор")),
                ("Bitrix24/CRM", ("bitrix", "crm")),
            )
            if not any(needle in joined for needle in needles)
        ]
        if missing:
            conflicts.append(
                Conflict(
                    where="hh_autonomous_agent.DEFAULT_SEARCH_QUERIES",
                    current=", ".join(queries),
                    expected="запросы также по: " + ", ".join(missing),
                    why=(
                        "Подтверждённый приоритет — любая подходящая IT-работа. Поисковые запросы "
                        "сейчас только Python/AI, поэтому поддержка, системное администрирование "
                        "и Bitrix24 в выборку не попадают вообще."
                    ),
                )
            )

    return conflicts


# ----------------------------------------------------------------------
# Assembly and rendering
# ----------------------------------------------------------------------


#: The four T5.2 editorial decisions, each with the wording it would introduce.
#:
#: Markers are *any-of*: the drafts are an English/Russian pair, so a decision
#: can be satisfied in either language. Group names stay English in both files
#: (measured: the Russian skills block reads "AI & LLM", "Automation & delivery"),
#: which is why "IT Support & Systems" is a valid marker for both.
T5_2_EDITS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("вступление сводки", ("IT specialist with", "IT-специалист с")),
    ("число стажа «15+»", ("15+",)),
    (
        "«in production» → «used in my work and in my own projects»",
        ("used in my work and in my own projects", "применяемые в работе и в собственных проектах"),
    ),
    ("группа «IT Support & Systems Administration»", ("IT Support & Systems",)),
)

#: Both drafts. The earlier check looked only at the English one.
T5_2_FILES: tuple[str, ...] = ("artifacts/master_resume.md", "artifacts/master_resume.ru.md")


def _t5_2_scan() -> list[tuple[str, dict[str, int]]]:
    """Per-file counts for each approved T5.2 edit, read from the real drafts."""
    scan: list[tuple[str, dict[str, int]]] = []
    for relative in T5_2_FILES:
        counts: dict[str, int] = {}
        try:
            text = (Path(__file__).resolve().parents[2] / relative).read_text(encoding="utf-8")
        except OSError:
            text = ""
        for label, markers in T5_2_EDITS:
            counts[label] = sum(text.count(marker) for marker in markers)
        scan.append((relative, counts))
    return scan


def t5_2_pending_note() -> str:
    """Whether the four approved T5.2 edits are in the drafts.

    The counts are read from the files at call time rather than hard-coded, so
    the note cannot go stale the way a written-in number would: the moment the
    edits land, the wording changes by itself. That is how the note went from
    "не применён" to "применён" without anyone editing it.
    """
    scan = _t5_2_scan()
    applied = all(count for _, counts in scan for count in counts.values())
    if applied:
        return (
            "T5.2 применён: все четыре утверждённые редакционные правки найдены в обоих "
            "черновиках (artifacts/master_resume.md и artifacts/master_resume.ru.md)."
        )
    detail = "; ".join(
        f"{relative}: " + ", ".join(f"«{label}» — {count}" for label, count in counts.items())
        for relative, counts in scan
    )
    return (
        "T5.2 НЕ применён: четыре утверждённые редакционные правки (вступление сводки, "
        "число стажа, «in production» → «used in my work and in my own projects», "
        f"группа «IT Support & Systems Administration») в черновиках отсутствуют — "
        f"проверено поиском: {detail}. "
        "Текущий текст master-резюме не считается утверждённой публичной версией."
    )


def _never_claim(kb: CandidateKnowledgeBase) -> list[str]:
    """Every global "must not be claimed" note in the KB, with its provenance."""
    out: list[str] = []
    for record in kb.claims.values():
        if record.kind in _NEVER_CLAIM_KINDS:
            out.append(f"{record.value[:160]} [{record.claim_id}, {record.line_ref()}]")
        elif not record.in_source:
            out.append(f"Больше нет в источнике: {record.value[:120]} [{record.claim_id}]")
    return out


def _relevant_warnings(
    kb: CandidateKnowledgeBase,
    facts: VacancyFacts,
    assessments: list[Assessment],
) -> list[str]:
    """The never-claim notes this vacancy actually touches.

    A global warning is noise on a phone when it has nothing to do with the job:
    the blocked pronunciation-scorer experiment (project *Repiton*) has no
    business appearing under a Bitrix24 vacancy, where it buried the lines that
    did matter.

    Relevance is decided by *linkage*, not by wording. A ``project_status`` claim
    carries ``parent_id`` -> its project, so the question is simply whether the
    brief proposes to use that project in a response at all. A warning with no
    project behind it is kept when it names a technology the vacancy asks for.

    Nothing is dropped for being unwelcome -- only for being unrelated to this
    vacancy. Everything dropped here is still printed in full by
    :func:`render_brief_details`.
    """
    vocabulary = _kb_vocabulary(kb)
    wanted = set(facts.technologies)
    used_projects: set[str] = set()
    for assessment in assessments:
        if assessment.evidence_class is EvidenceClass.E_UNCONFIRMED:
            continue
        wanted.update(assessment.requirement.technologies)
        used_projects.update(item.where for item in assessment.project_evidence)

    project_names = _project_names(kb)

    kept: list[str] = []
    for record in kb.claims.values():
        if record.kind not in _NEVER_CLAIM_KINDS and record.in_source:
            continue
        project = project_names.get(record.parent_id or "", "")
        if project:
            if project not in used_projects:
                continue
            prefix = f"{project}: "
        elif not (_canonical_terms_in(record.value, vocabulary) & wanted):
            continue
        else:
            prefix = ""
        if record.kind in _NEVER_CLAIM_KINDS:
            kept.append(prefix + _plain_text(record.value))
        else:
            kept.append(f"Больше нет в источнике: {_plain_text(record.value)}")
    return kept


def _build_highlights(facts: VacancyFacts, assessments: list[Assessment]) -> list[str]:
    """What an adapted resume would lead with, named by the right kind of work.

    A class-A line must name the *employer*, never the candidate's own project:
    "Опыт работы — Docker (Reels Analyzer)" claims paid work that does not exist.
    """
    highlights: list[str] = []
    for evidence_class, prefix in (
        (EvidenceClass.A_CONFIRMED_EXPERIENCE, "Опыт работы"),
        (EvidenceClass.B_OWN_PROJECT, "Свой проект"),
    ):
        for assessment in assessments:
            if assessment.evidence_class is not evidence_class:
                continue
            if evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE:
                source = assessment.commercial_evidence
            else:
                source = assessment.project_evidence
            names = sorted({item.where for item in source})[:2]
            if not names:
                continue
            highlights.append(
                f"{prefix} — {_clip_words(assessment.requirement.text, 90)} ({', '.join(names)})"
            )
    return highlights[:8]


def _question_subject(requirement: Requirement) -> tuple[str, bool]:
    """What a clarifying question is about, and whether that is a *name*.

    Returns ``(subject, is_name)``. Most questions are about a technology, and
    quoting it reads naturally: "Есть ли опыт с «PostgreSQL»?". A requirement
    that names nothing the vocabulary knows has no such term, and the fallback
    used to be the first sixty characters of the sentence, quoted as though it
    were a skill. Measured on the Ignition posting that produced "Есть ли опыт с
    «Have proven Apple aptitude — you actually use and like the A»?" -- a
    sentence cut mid-word and asked about as if it were a product name.

    So the caller is told which of the two it has, and the sentence is trimmed
    on a word boundary by :func:`_short_quote`.
    """
    technologies = requirement.technologies
    if technologies:
        return technologies[0], True
    return _short_quote(requirement.text), False


def _build_questions(assessments: list[Assessment], kb: CandidateKnowledgeBase) -> list[str]:
    """Ask only what the KB cannot answer and what changes the decision."""
    questions: list[str] = []
    for assessment in assessments:
        if not assessment.requirement.mandatory:
            continue
        subject, is_name = _question_subject(assessment.requirement)
        if assessment.evidence_class is EvidenceClass.E_UNCONFIRMED:
            if is_name:
                questions.append(f"Есть ли опыт с «{subject}»? В KB подтверждения нет.")
            else:
                questions.append(f"Есть ли подтверждение по пункту «{subject}»? В KB подтверждения нет.")
        elif assessment.evidence_class is EvidenceClass.C_PARTIAL and any(
            e.origin == "project" for e in assessment.evidence
        ):
            if is_name:
                questions.append(
                    f"Применялось ли «{subject}» в оплачиваемой работе, а не только в своём проекте?"
                )
            else:
                questions.append(
                    f"Подтверждён ли оплачиваемой работой пункт «{subject}»? "
                    "Сейчас — только своим проектом."
                )
        elif assessment.evidence_class is EvidenceClass.D_NEEDS_CLARIFICATION:
            if is_name:
                questions.append(f"Где именно применялось «{subject}» — роль или проект?")
            else:
                questions.append(f"К какой роли или проекту относится пункт «{subject}»?")
    # De-duplicate, keep order, cap. The KB already answers salary, format and
    # availability, so those are deliberately not asked. The cap is three: a
    # phone screen has room for the questions that decide whether an application
    # is possible, not for a questionnaire.
    seen: set[str] = set()
    unique: list[str] = []
    for question in questions:
        if question not in seen:
            seen.add(question)
            unique.append(question)
    return unique[:3]


def build_brief(
    vacancy: VacancyInput,
    kb: CandidateKnowledgeBase,
    *,
    profile: dict[str, Any] | None = None,
) -> Brief:
    """Assemble the brief: facts, evidence classes, preferences, conflicts."""
    facts = extract_facts(vacancy, kb)
    preferences = read_preferences(kb)
    tenure = compute_tenure(kb, profile)
    assessments = assess_requirements(facts, kb, tenure)
    return Brief(
        facts=facts,
        assessments=assessments,
        preferences=preferences,
        tenure=tenure,
        conflicts=detect_conflicts(preferences, profile, tenure),
        highlights=_build_highlights(facts, assessments),
        cannot_claim=_never_claim(kb),
        cannot_claim_relevant=_relevant_warnings(kb, facts, assessments),
        questions=_build_questions(assessments, kb),
        notes=[t5_2_pending_note()],
    )


#: How much of a claim's own wording the phone view shows. The full text is in
#: ``--details``; here it only has to be enough to recognise the fact.
_SHORT_QUOTE_CHARS = 64


def _plain_text(value: str) -> str:
    """Strip the markdown a claim value may carry before it reaches a phone.

    Emphasis markers only. Underscores are deliberately *not* stripped: they are
    also part of identifiers, and stripping them turned
    ``BLOCKED BY STAGE_1_FINAL_NO_GO`` into ``STAGE1FINALNOGO``.
    """
    text = " ".join(value.split())
    text = re.sub(r"\*{1,2}|`+", "", text)
    return text.lstrip("-–—• ").strip()


def _short_quote(value: str) -> str:
    """Trim a claim to something readable on a phone, cut on a word boundary.

    A cut that lands inside brackets drops the opening bracket too, so the line
    never ends on an unbalanced "(systemd services".
    """
    text = _plain_text(value)
    if len(text) <= _SHORT_QUOTE_CHARS:
        return text
    cut = text[:_SHORT_QUOTE_CHARS]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    for opening, closing in (("(", ")"), ("[", "]")):
        if cut.count(opening) > cut.count(closing):
            head = cut[: cut.rfind(opening)]
            if head.strip():
                cut = head
    return cut.rstrip(" ,;:-") + "…"


def _clip_words(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, never inside a word.

    Every rendered view trims requirement text -- forty characters in the wishes
    line, 110 in the phone view, 160 in the detail view -- and a slice at a fixed
    offset lands inside a word often enough to read as corruption rather than as
    a cut. Measured on the Ignition posting, the wishes line rendered "Apple at
    the core — macOS, iOS, Apple Bu".

    Trailing punctuation and dashes go with the cut word, so the line does not
    end on "iOS," or on a dangling em dash either.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.-–—")


def _short_evidence_lines(assessment: Assessment) -> list[str]:
    """Evidence lines for the phone view, keeping paid and project work apart.

    Under a commercial heading only employment evidence is printed. If the same
    skill also appears in one of the candidate's own projects it gets its own
    line, explicitly labelled as unpaid, so the two never merge into a single
    claim the way they did when the project's longer technology list won the
    sort and led the section.
    """
    indent = "      "

    def render(item: Evidence) -> str:
        # A role_title claim's value *is* the role, so "X (X)" is not a line
        # anyone should have to read.
        text = _short_quote(item.value)
        if _plain_text(item.value) == item.where:
            return f"{indent}— {text}"
        return f"{indent}— {text} ({item.where})"

    lines: list[str] = []
    if assessment.evidence_class is EvidenceClass.A_CONFIRMED_EXPERIENCE:
        lines.extend(render(item) for item in assessment.commercial_evidence[:2])
        projects = sorted({item.where for item in assessment.project_evidence})
        if projects:
            lines.append(
                f"{indent}— дополнительно, свой проект (не оплачиваемая работа): "
                f"{', '.join(projects[:2])}"
            )
    elif assessment.evidence_class is EvidenceClass.B_OWN_PROJECT:
        lines.extend(render(item) for item in assessment.project_evidence[:2])
    else:
        source = assessment.resume_evidence or assessment.evidence
        lines.extend(render(item) for item in source[:2])
    # A requirement may name technologies no KB fact can answer. Printed under
    # whatever class the requirement landed in, because the reader has to see
    # the *whole* ask before calling the match complete -- and phrased as a
    # limit of the brief, never as a skill the candidate lacks.
    if assessment.requirement.unrecognised:
        names = ", ".join(f"«{term}»" for term in assessment.requirement.unrecognised)
        lines.append(
            f"{indent}— не подтверждено доступными фактами: {names} "
            "(названий нет в KB; это ограничение справки, а не отсутствие навыка)"
        )
    # A match on a requirement that also names a number of years is only half a
    # match, and the phone view is where the half gets mistaken for the whole.
    # Fired on any class that actually has evidence, not just A and B: since
    # T7.2 a year-threshold requirement is demoted to C precisely because its
    # duration is unmeasured, and hiding that line on the demoted line would take
    # the reason for the demotion off the one screen the reader acts on.
    years = _years_named(assessment.requirement.text)
    if years and assessment.evidence:
        lines.append(
            f"{indent}— срок {years}+ лет по этому требованию в KB не измеряется: "
            "общий IT-стаж сюда не подставляется"
        )
    return lines


def render_brief_short(brief: Brief) -> str:
    """The one-to-two-minute Russian version, readable from a phone.

    Reading order is the one the T6.1 spec asks for: the vacancy and its terms,
    then what matches, then what is only partial, then the mandatory
    requirements nothing confirms, and the few questions that actually decide
    whether an application is possible. Everything technical -- claim ids,
    statuses, full claim text -- belongs to :func:`render_brief_details`.
    """
    facts = brief.facts
    lines: list[str] = []
    lines.append(f"ВАКАНСИЯ: {facts.title}")
    lines.append(f"Работодатель: {facts.employer}")
    salary_line = f"Зарплата: {facts.salary_text}"
    if brief.preferences.min_income_usd is not None:
        if facts.salary_meets_minimum is True:
            salary_line += f" — выше минимума ${brief.preferences.min_income_usd}"
        elif facts.salary_meets_minimum is False:
            salary_line += f" — НИЖЕ минимума ${brief.preferences.min_income_usd}"
        else:
            # No comparison was possible -- the figure is absent, or it carries
            # no currency (see :func:`extract_facts`). Saying "минимум кандидата
            # $1000" alone invited the reader to compare "24" against it, so the
            # line now states plainly that nothing was compared.
            salary_line += (
                f" — сравнить нельзя, минимум кандидата ${brief.preferences.min_income_usd}"
            )
    lines.append(salary_line)
    lines.append(f"Формат: {facts.work_format} | География: {facts.location}")
    if facts.seniority:
        lines.append(f"Уровень: {', '.join(facts.seniority)}")
    lines.append("")

    groups = (
        (EvidenceClass.A_CONFIRMED_EXPERIENCE, "ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ"),
        (EvidenceClass.B_OWN_PROJECT, "ЗАКРЫВАЕТСЯ СВОИМ ПРОЕКТОМ (не оплачиваемая работа)"),
        (EvidenceClass.C_PARTIAL, "ЧАСТИЧНО"),
        (EvidenceClass.D_NEEDS_CLARIFICATION, "ЕСТЬ В РЕЗЮМЕ, НУЖНО УТОЧНЕНИЕ"),
    )
    for evidence_class, heading in groups:
        items = brief.by_class(evidence_class)
        if not items:
            continue
        lines.append(f"{heading}:")
        for assessment in items:
            mark = "обязательно" if assessment.requirement.mandatory else "пожелание"
            lines.append(f"  • [{mark}] {_clip_words(assessment.requirement.text, 110)}")
            lines.extend(_short_evidence_lines(assessment))
        lines.append("")

    # What blocks an application is the *mandatory* requirement nothing
    # confirms, so those are listed in full and the wishes are folded into one
    # line. On the sysadmin vacancy this is the difference between nine lines a
    # reader can act on and twelve that bury them.
    unconfirmed = brief.by_class(EvidenceClass.E_UNCONFIRMED)
    required_unconfirmed = [item for item in unconfirmed if item.requirement.mandatory]
    wished_unconfirmed = [item for item in unconfirmed if not item.requirement.mandatory]
    if required_unconfirmed:
        lines.append("ОБЯЗАТЕЛЬНЫЕ ТРЕБОВАНИЯ БЕЗ ПОДТВЕРЖДЕНИЯ:")
        for assessment in required_unconfirmed:
            lines.append(f"  • {_clip_words(assessment.requirement.text, 110)}")
        lines.append("")
    if wished_unconfirmed:
        wishes = ", ".join(_clip_words(item.requirement.text, 40) for item in wished_unconfirmed)
        lines.append(f"Пожелания без подтверждения: {wishes}")
        lines.append("")

    if unconfirmed:
        lines.append(NO_EVIDENCE_CAVEAT)
        lines.append("")

    if brief.questions:
        lines.append("ЧТО УТОЧНИТЬ (влияет на отклик):")
        for item in brief.questions:
            lines.append(f"  • {item}")
        lines.append("")

    if brief.highlights:
        lines.append("ЧТО ВЫДЕЛИТЬ В АДАПТИРОВАННОМ РЕЗЮМЕ:")
        for item in brief.highlights:
            lines.append(f"  • {item}")
        lines.append("")

    # Only the warnings this vacancy touches. The global list is a safety net
    # for the resume as a whole, not a note about this job, and printing it here
    # put a blocked pronunciation-scorer experiment under a Bitrix24 vacancy.
    if brief.cannot_claim_relevant:
        lines.append("НЕЛЬЗЯ ПИСАТЬ В ОТКЛИК БЕЗ ПОДТВЕРЖДЕНИЯ:")
        for item in brief.cannot_claim_relevant[:3]:
            lines.append(f"  • {item}")
        lines.append("")

    if brief.conflicts:
        lines.append("ФИЛЬТРЫ ПОИСКА ПРОТИВОРЕЧАТ ПОДТВЕРЖДЁННЫМ ПРЕДПОЧТЕНИЯМ (T6 их не меняет):")
        for conflict in brief.conflicts:
            lines.append(f"  • {conflict.where}: сейчас {conflict.current} → нужно {conflict.expected}")
        lines.append("")

    # Mandatory employment conditions -- schedule, time zone, residence, right to
    # work, employment type, home office. Their own block, because they are terms
    # of the job and not candidate skills: they are never matched against the KB,
    # and the phone view is where "Have a quiet home workspace" read as a missing
    # ability. Given their own block rather than folded into the "wishes" line,
    # because they are the conditions an application depends on and a reader who
    # mistakes them for skills would take "Eastern or Pacific time zones" for
    # something the candidate is missing. Trimmed to a readable length, never
    # mid-word.
    if facts.conditions:
        lines.append("ОБЯЗАТЕЛЬНЫЕ УСЛОВИЯ РАБОТЫ (не навыки кандидата):")
        for condition in facts.conditions:
            lines.append(f"  • {_clip_words(condition.text, 140)}")
        lines.append("")

    if facts.unstated:
        lines.append(f"В вакансии не указано: {', '.join(facts.unstated)}.")
    lines.append(brief.tenure.publishable())
    if brief.tenure.note:
        lines.append(brief.tenure.note)
    # The unfinished stage belongs in the short view too: it decides whether the
    # master resume text may be used as-is, which is a decision the reader makes
    # from this screen. The condition comes from :func:`t5_2_pending_note`, which
    # counts the approved edits in the two drafts at call time. The sentence that
    # used to stand here was hard-coded and gated on "are there any notes at all",
    # so it went on announcing an unfinished stage after T6.2 finished it -- the
    # one screen where a stale statement costs the most.
    if any("НЕ применён" in note for note in brief.notes):
        lines.append("")
        lines.append(
            "НЕЗАВЕРШЁННЫЙ ЭТАП: T5.2 не применён — четыре утверждённые правки "
            "мастер-резюме отсутствуют в черновиках, поэтому его текст не считается "
            "утверждённой публичной версией (детали: --details)."
        )
    return "\n".join(lines)


def render_brief_details(brief: Brief) -> str:
    """The technical version: every claim id, status and source."""
    lines: list[str] = ["# Job-to-Candidate Brief — подробная версия", ""]
    facts = brief.facts
    lines += [
        f"- Вакансия: {facts.title}",
        f"- Работодатель: {facts.employer}",
        f"- Источник: {facts.source} {facts.stable_id}".rstrip(),
        f"- Ссылка: {facts.url or 'не указано'}",
        f"- Формат: {facts.work_format}"
        + (f" (классификатор: {facts.work_format_reason})" if facts.work_format_reason else ""),
        f"- География: {facts.location}",
        f"- Зарплата: {facts.salary_text}",
        (
            f"- Технологии из требований, распознанные по KB: "
            f"{', '.join(facts.technologies) or 'не распознано'}"
        ),
        (
            f"- Обязательных требований: {len(facts.must_have)}; "
            f"пожеланий: {len(facts.nice_to_have)}; "
            f"обязательных условий работы: {len(facts.conditions)}"
        ),
        "",
        "## Требования и доказательства",
        "",
        "| № | Класс | Обязательно | Категории | Требование | Доказательство |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for index, assessment in enumerate(brief.assessments, 1):
        evidence = "; ".join(
            f"{e.claim_id} ({e.origin}, {e.status}, {e.ref})" for e in assessment.evidence[:4]
        ) or "—"
        lines.append(
            f"| {index} | {assessment.evidence_class} | "
            f"{'да' if assessment.requirement.mandatory else 'нет'} | "
            f"{', '.join(assessment.requirement.category_labels) or '—'} | "
            f"{_clip_words(assessment.requirement.text, 110)} | {evidence} |"
        )
    if facts.conditions:
        lines += [
            "",
            "## Обязательные условия работы (не навыки кандидата)",
            "",
            (
                "Эти пункты — условия найма, а не навыки: они не сопоставляются с KB и "
                "класса доказательства не получают. Они перечислены потому, что от них "
                "зависит сама возможность отклика."
            ),
            "",
            "| № | Категории | Условие |",
            "| --- | --- | --- |",
        ]
        for index, condition in enumerate(facts.conditions, 1):
            lines.append(
                f"| {index} | {', '.join(condition.category_labels) or '—'} | "
                f"{_clip_words(condition.text, 140)} |"
            )
    lines += ["", "## Пояснения по каждому требованию", ""]
    for index, assessment in enumerate(brief.assessments, 1):
        lines.append(
            f"{index}. **{assessment.evidence_class}** "
            f"{_clip_words(assessment.requirement.text, 160)}"
        )
        lines.append(f"   - Вид требования: {assessment.requirement.kind}")
        lines.append(
            "   - Категории требования (читаются по тексту вакансии, без KB): "
            + (", ".join(assessment.requirement.category_labels) or "—")
        )
        form = (
            "выбор («или» — достаточно одной альтернативы)"
            if assessment.requirement.disjunctive
            else "конъюнкция (нужны все названные технологии)"
        )
        lines.append(f"   - Форма требования: {form}")
        if assessment.requirement.unrecognised:
            lines.append(
                "   - Не подтверждено доступными фактами: "
                + ", ".join(f"«{term}»" for term in assessment.requirement.unrecognised)
                + " (этих названий нет в KB; справка не может их ни подтвердить, "
                "ни опровергнуть)"
            )
        lines.append(f"   - Объяснение: {assessment.explanation}")
        for item in assessment.evidence:
            lines.append(
                f"   - Факт: {item.value[:120]} — {item.claim_id}, {item.claim_kind}, "
                f"{item.status}, {item.where}, {item.ref}"
            )
        lines.append("")

    lines += ["## Хронология, из которой посчитан стаж", ""]
    lines.append(f"Утверждённая формулировка: {brief.tenure.publishable()}")
    lines.append("")
    lines.append("Ниже — внутренняя оценка по хронологии KB. Она НЕ утверждена и не публикуется:")
    lines.append("")
    for interval in brief.tenure.intervals:
        lines.append(f"- {interval}")
    lines += [
        "",
        f"Метод: {brief.tenure.method}",
        (
            f"Оценка (не утверждена): IT ≈ {brief.tenure.total_it_years:.1f} г.; оплачиваемая "
            f"работа ≈ {brief.tenure.commercial_years:.1f} г.; неоплачиваемые свои проекты ≈ "
            f"{brief.tenure.unpaid_years:.1f} г."
        ),
        (
            "Почему не публикуется: это арифметика по датам ролей, где часть дат указана "
            "только годом (год без месяца читается как январь/декабрь), поэтому оценка "
            "сдвигается на срок до года. Утверждённая формулировка общего опыта — "
            f"«{APPROVED_TOTAL_EXPERIENCE}»."
        ),
    ]
    if brief.tenure.note:
        lines.append(f"Оговорка: {brief.tenure.note}")
    lines += ["", "## Подтверждённые предпочтения (из KB)", ""]
    preferences = brief.preferences
    lines += [
        (
            f"- Минимальный доход: {preferences.min_income_usd} USD "
            f"[{preferences.claim_ids.get('preference_condition', '—')}]"
        ),
        f"- Приоритет: {preferences.priority} [{preferences.claim_ids.get('goal_direction', '—')}]",
        (
            f"- Bitrix24 наравне: {'да' if preferences.bitrix24_on_par else 'нет'} "
            f"[{preferences.claim_ids.get('preference_role', '—')}]"
        ),
        (
            f"- Свои проекты с {preferences.own_projects_since or '—'}, "
            f"{'без оплаты' if preferences.own_projects_unpaid else 'оплата не уточнена'} "
            f"[{preferences.claim_ids.get('experience_note', '—')}]"
        ),
        (
            f"- Хронология: {preferences.chronology_note} "
            f"[{preferences.claim_ids.get('chronology_note', '—')}]"
        ),
        "",
        "## Ограничения этого анализа",
        "",
    ]
    for note in brief.notes:
        lines.append(f"- {note}")
    # The full warning list lives here, not on the phone: it is a safety net for
    # the resume as a whole and most of it is unrelated to any one vacancy.
    lines += ["", "## Что нельзя утверждать без подтверждения (весь KB)", ""]
    if brief.cannot_claim:
        for item in brief.cannot_claim:
            lines.append(f"- {item}")
    else:
        lines.append("- нет")
    lines += [
        "",
        (
            "Для этой вакансии релевантны: "
            f"{len(brief.cannot_claim_relevant)} из {len(brief.cannot_claim)}."
        ),
    ]
    return "\n".join(lines)


def render_brief_json(brief: Brief) -> str:
    payload = {
        "vacancy": {
            "title": brief.facts.title,
            "employer": brief.facts.employer,
            "stable_id": brief.facts.stable_id,
            "source": brief.facts.source,
            "work_format": brief.facts.work_format,
            "location": brief.facts.location,
            "salary": brief.facts.salary_text,
            "technologies": brief.facts.technologies,
            "unstated": brief.facts.unstated,
        },
        # Mandatory employment conditions, kept out of ``assessments`` because
        # they carry no evidence class: they are terms of the job, not skills, and
        # a consumer that treats every assessment as a skill question would
        # otherwise read "standard business hours in Eastern or Pacific time
        # zones" as something the candidate is missing.
        "conditions": [
            {
                "text": c.text,
                "categories": [str(item) for item in c.categories],
                "mandatory": c.mandatory,
            }
            for c in brief.facts.conditions
        ],
        "assessments": [
            {
                "requirement": a.requirement.text,
                "kind": str(a.requirement.kind),
                "mandatory": a.requirement.mandatory,
                # Every kind of ask the requirement makes, read off the vacancy
                # alone (T7.2 section 4). Independent of ``class``: the category
                # says what is being asked for, the class says what the facts can
                # support.
                "categories": [str(item) for item in a.requirement.categories],
                # Names the requirement uses that no KB fact can answer, in the
                # posting's own spelling. Separate from ``class`` on purpose:
                # "the brief cannot answer this" is not "the candidate lacks
                # it", and a consumer reading only this field must not confuse
                # the two.
                "unrecognised": list(a.requirement.unrecognised),
                "disjunctive": a.requirement.disjunctive,
                "class": str(a.evidence_class),
                "explanation": a.explanation,
                "evidence": [
                    {
                        "claim_id": e.claim_id,
                        "kind": e.claim_kind,
                        "value": e.value,
                        "status": e.status,
                        "origin": e.origin,
                        "where": e.where,
                        "ref": e.ref,
                    }
                    for e in a.evidence
                ],
            }
            for a in brief.assessments
        ],
        "tenure": {
            # ``approved`` is the only publishable statement; the three numbers
            # are the unapproved internal estimate and are named as such.
            "approved": brief.tenure.publishable(),
            "estimate_not_approved": {
                "total_it_years": brief.tenure.total_it_years,
                "commercial_years": brief.tenure.commercial_years,
                "unpaid_years": brief.tenure.unpaid_years,
            },
            "profile_years": brief.tenure.profile_years,
            "profile_is_total": brief.tenure.profile_is_total,
            "note": brief.tenure.note,
        },
        "preferences": {
            "min_income_usd": brief.preferences.min_income_usd,
            "priority": brief.preferences.priority,
            "bitrix24_on_par": brief.preferences.bitrix24_on_par,
            "claim_ids": brief.preferences.claim_ids,
        },
        "conflicts": [
            {"where": c.where, "current": c.current, "expected": c.expected, "why": c.why}
            for c in brief.conflicts
        ],
        "highlights": brief.highlights,
        "cannot_claim": brief.cannot_claim,
        "questions": brief.questions,
        "notes": brief.notes,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# Input loading and the command layer
# ----------------------------------------------------------------------


def load_profile(path: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    """Read ``candidate_profile.json`` for the conflict check. Read-only."""
    from .kb import PROJECT_ROOT

    candidate = Path(path) if path else PROJECT_ROOT / "candidate_profile.json"
    if not candidate.is_file():
        return None
    return json.loads(candidate.read_text(encoding="utf-8"))


def _json_dict(raw: Any) -> dict[str, Any]:
    """Decode a ``raw_data`` column into a dict, tolerating both shapes."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def vacancy_from_row(row: Any, stable_id: str | None = None) -> VacancyInput:
    """Convert a ``state.db`` row into the shape T6 works with.

    Two row shapes reach this function and both are real:

    * **Positional** -- what :func:`db.get_vacancy_by_id` actually returns.
      ``db.get_connection`` sets no ``row_factory``, so the row is a plain
      tuple. Every other caller in the project pairs it with
      :func:`db._row_to_vacancy`, and so do we, rather than growing a second
      and subtly different conversion.
    * **Mapping** -- a ``sqlite3.Row`` or dict, which tests hand over directly.

    What is *not* tolerated is a row we cannot read at all. A first draft read
    the tuple with ``row["title"]``, caught the resulting ``TypeError`` and
    returned every field empty; the brief then rendered confidently from
    nothing. Silent blank is the worst possible failure here, so the positional
    path lets :func:`db._row_to_vacancy` raise instead of papering over it.
    """
    if hasattr(row, "keys"):
        def get(key: str, default: Any = None) -> Any:
            try:
                return row[key]
            except (IndexError, KeyError, TypeError):
                return default

        return VacancyInput(
            title=get("title") or "",
            description=get("description") or "",
            company=get("company") or "",
            location=get("location") or "",
            salary_min=get("salary_min"),
            salary_max=get("salary_max"),
            salary_currency=get("salary_currency"),
            employment_type=get("employment_type"),
            source=get("source") or "",
            stable_id=get("stable_id") or stable_id or "",
            job_url=get("job_url") or "",
            raw_data=_json_dict(get("raw_data")),
        )

    from ..db import _row_to_vacancy

    vac = _row_to_vacancy(row)
    return VacancyInput(
        title=vac.title or "",
        description=vac.description or "",
        company=vac.company or "",
        location=vac.location or "",
        salary_min=vac.salary_min,
        salary_max=vac.salary_max,
        salary_currency=vac.salary_currency,
        employment_type=vac.employment_type,
        source=vac.source or "",
        stable_id=stable_id or vac.stable_id(),
        job_url=vac.job_url or "",
        raw_data=_json_dict(vac.raw_data),
    )


def load_vacancy(stable_id: str) -> VacancyInput:
    """Fetch one vacancy from ``state.db`` by stable id (read-only).

    A missing row and a missing table are both reported as
    :class:`JobBriefError`: the command layer turns that into one clear line,
    whereas an escaping ``sqlite3.OperationalError`` would print a traceback
    over a typo in the id.
    """
    import sqlite3

    from ..db import get_vacancy_by_id

    try:
        row = get_vacancy_by_id(stable_id)
    except sqlite3.Error as exc:
        raise JobBriefError(
            f"не удалось прочитать вакансию из state.db: {stable_id} ({exc})"
        ) from exc
    if row is None:
        raise JobBriefError(f"вакансия не найдена в state.db: {stable_id}")
    return vacancy_from_row(row, stable_id=stable_id)


def _salary_verdict(brief: Brief) -> None:
    """Set ``salary_meets_minimum`` once the confirmed minimum is known."""
    facts = brief.facts
    minimum = brief.preferences.min_income_usd
    # A figure whose currency the source never stated cannot be compared with a
    # USD minimum; the honest answer is "unknown", not a comparison.
    if minimum is None or facts.salary_text == "не указано" or "валюта" in facts.salary_text:
        facts.salary_meets_minimum = None
        return
    top = None
    for value in (facts.salary_text.replace(",", "").split("–")):
        match = re.search(r"(\d+(?:\.\d+)?)", value)
        if match:
            top = float(match.group(1))
    facts.salary_meets_minimum = None if top is None else top >= minimum


def candidate_job_brief_cmd(
    *,
    vacancy_id: str | None = None,
    text: str | None = None,
    text_file: str | None = None,
    requirements: tuple[str, ...] = (),
    kb_path: str | None = None,
    profile_path: str | None = None,
    as_json: bool = False,
    details: bool = False,
    out_path: str | None = None,
) -> int:
    """CLI entry point: ``candidate-job-brief``.

    Exactly one source of vacancy text is required: a ``stable_id`` already in
    the system, inline ``--text``, or ``--file``. Nothing is written to the
    database and no application is prepared.

    ``requirements`` is the manual fallback of T7.2 section 5: verbatim
    requirement lines for a description the splitter cannot read. They are added
    to the automatic reading, never substituted for it, and each has to be found
    in the description or the command fails rather than reporting a requirement
    the posting does not contain.
    """
    from .master_resume import load_kb

    try:
        kb, kb_file = load_kb(kb_path)
    except Exception as exc:  # noqa: BLE001 - the CLI must not traceback at the user
        print(f"ОШИБКА: не удалось прочитать KB: {exc}")
        return 2

    try:
        if vacancy_id:
            vacancy = load_vacancy(vacancy_id)
        elif text_file:
            path = Path(text_file)
            if not path.is_file():
                print(f"ОШИБКА: файл не найден: {path}")
                return 2
            vacancy = VacancyInput(title=path.stem, description=path.read_text(encoding="utf-8"))
        elif text:
            first_line = text.strip().splitlines()[0][:120] if text.strip() else "Вакансия"
            vacancy = VacancyInput(title=first_line, description=text)
        else:
            print("ОШИБКА: укажите --vacancy <stable_id>, --text или --file")
            return 2
    except JobBriefError as exc:
        print(f"ОШИБКА: {exc}")
        return 2

    if requirements:
        vacancy = replace(vacancy, manual_requirements=tuple(requirements))

    profile = load_profile(profile_path)
    try:
        brief = build_brief(vacancy, kb, profile=profile)
    except JobBriefError as exc:
        # The manual fallback fails here, not above: its entries are checked
        # against the description inside the extractor, so an entry the posting
        # does not state has to come back as a message rather than a traceback.
        print(f"ОШИБКА: {exc}")
        return 2
    _salary_verdict(brief)

    if as_json:
        rendered = render_brief_json(brief)
    elif details:
        rendered = render_brief_details(brief)
    else:
        rendered = render_brief_short(brief)

    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
        print(f"Отчёт записан: {target}")
    else:
        print(rendered)

    print(
        f"\nKB: {kb_file} (версия {kb.kb_version}); "
        f"требований разобрано: {len(brief.assessments)}; "
        f"факты KB не изменялись.",
    )
    return 0
