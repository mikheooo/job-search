"""Stage 17C: HH Application Q&A resolution + validation.

Resolution is TRUTH ONLY:
- PROFILE questions: answered from confirmed CandidateProfile/resume data only.
- SCREENING questions: answered from confirmed facts or (optionally) an LLM
  that is restricted to truth sources (CandidateProfile + resume.md +
  DeepAnalysisResult + vacancy). If no confirmed fact exists -> answer=None,
  requires_review=True.
- UNKNOWN questions: never guessed; type/options/required preserved as
  UNKNOWN/unknown, requires_review=True, with an explicit reason explaining
  HH did not expose the field without an authenticated session.

Answers with options:
- If options are absent (auth gate), we NEVER fabricate a free-text value for
  SELECT/RADIO; requires_review=True.
- If options are present, we resolve only to one of the existing options and
  never create new ones.

Validator is read-only: it never mutates anything, never writes to DB, never
calls LLM, never submits.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .candidate_profile import CandidateProfile
from .schema import Vacancy
from .job_analyzer import DeepAnalysisResult
from .hh_extractor import (
    ApplicationAnswer,
    ApplicationForm,
    ApplicationQuestion,
    QuestionSource,
    QuestionType,
)


class QuestionAnswerGenerator:
    """Produce a safe ApplicationAnswer for one question from truth sources."""

    def __init__(
        self,
        profile: CandidateProfile,
        resume_text: str,
        deep: Optional[DeepAnalysisResult],
        vacancy: Optional[Vacancy],
        llm: Optional[Any] = None,
    ):
        self.profile = profile
        self.resume_text = resume_text or ""
        self.deep = deep
        self.vacancy = vacancy
        self.llm = llm  # optional callable(system, user) -> str; only truth-source-grounded

    # --- confirmed profile/resume lookups (truth-only) ---

    def _confirmed(self, question: ApplicationQuestion) -> Optional[str]:
        label = (question.label or "").lower()
        if "имя" in label or "name" in label or "фио" in label or "фамилия" in label:
            m = re.search(r"(?:name|candidate|фио):\s*([A-Za-zА-Яа-яЁё]+(?:\s+[A-Za-zА-Яа-яЁё]+)+)", self.resume_text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
            return None
        if "email" in label or "почта" in label or "e-mail" in label:
            m = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", self.resume_text)
            if m:
                return m.group(0)
            return None
        if "телефон" in label or "phone" in label:
            m = re.search(r"\+?\d[0-9 \-\(\)]{7,}", self.resume_text)
            if m:
                return m.group(0).strip()
            return None
        if "город" in label or "location" in label or "где вы находитесь" in label or "место работы" in label:
            if self.profile.allowed_locations:
                return self.profile.allowed_locations[0]
            if self.profile.remote_required:
                return "Remote"
            return None
        if "лет опыта" in label or "опыт" in label and "год" in label or "years" in label:
            if self.profile.years_experience is not None:
                return str(self.profile.years_experience)
            m = re.search(r"(\d+)\s+years", self.resume_text, re.IGNORECASE)
            if m:
                return m.group(1)
            return None
        if "график" in label or "занятость" in label or "employment" in label:
            if self.profile.employment_types:
                return self.profile.employment_types[0]
            return None
        return None

    # --- option resolution (never fabricates) ---

    def _resolve_option(self, question: ApplicationQuestion, candidate: Optional[str]) -> Optional[str]:
        if not question.options:
            return None
        if not candidate:
            return None
        cand_l = candidate.lower().strip()
        for opt in question.options:
            opt_l = opt.lower().strip()
            if opt_l == "свой вариант":
                continue  # custom option needs human text - never auto-selected
            if opt_l == cand_l or cand_l in opt_l:
                return opt
        return None

    def generate(self, question: ApplicationQuestion) -> ApplicationAnswer:
        # UNKNOWN: never guess type/options/required.
        if question.normalized_type == QuestionType.UNKNOWN:
            return ApplicationAnswer(
                question_id=question.id,
                answer=None,
                answer_type=QuestionType.UNKNOWN,
                confidence=0.0,
                requires_review=True,
                reason="HH did not expose this field without an authenticated session; type/options/required are UNKNOWN",
            )

        # PROFILE: truth-only confirmed profile/resume value.
        if question.source == QuestionSource.PROFILE:
            val = self._confirmed(question)
            if val is None:
                return ApplicationAnswer(
                    question_id=question.id,
                    answer=None,
                    answer_type=question.normalized_type,
                    confidence=0.0,
                    requires_review=True,
                    reason="No confirmed candidate fact found for profile question",
                )
            return ApplicationAnswer(
                question_id=question.id,
                answer=val,
                answer_type=question.normalized_type,
                confidence=1.0,
                requires_review=False,
                reason="Confirmed from profile/resume",
            )

        # SELECT/RADIO without options (auth gate): never fabricate.
        if question.normalized_type in (QuestionType.SELECT, QuestionType.RADIO) and not question.options:
            return ApplicationAnswer(
                question_id=question.id,
                answer=None,
                answer_type=question.normalized_type,
                confidence=0.0,
                requires_review=True,
                reason="SELECT/RADIO options not exposed (auth gate); refusing to fabricate a value",
            )

        # SCREENING / SYSTEM: try confirmed fact, then optional truth-grounded LLM.
        val = self._confirmed(question)

        # Option-constrained questions: only accept a real option.
        if question.normalized_type in (QuestionType.SELECT, QuestionType.RADIO):
            resolved = self._resolve_option(question, val)
            if resolved is None:
                return ApplicationAnswer(
                    question_id=question.id,
                    answer=None,
                    answer_type=question.normalized_type,
                    confidence=0.0,
                    requires_review=True,
                    reason="No matching option among the real options; refusing to create a new variant",
                )
            return ApplicationAnswer(
                question_id=question.id,
                answer=resolved,
                answer_type=question.normalized_type,
                confidence=1.0,
                requires_review=False,
                reason=f"Resolved to existing option '{resolved}'",
            )

        # CHECKBOX (Stage 20C): multi-select - resolve EVERY confirmed fact
        # against the real options; never invent an option; "Свой вариант"
        # requires human text, which we never generate.
        if question.normalized_type == QuestionType.CHECKBOX:
            if not question.options:
                return ApplicationAnswer(
                    question_id=question.id,
                    answer=None,
                    answer_type=QuestionType.CHECKBOX,
                    confidence=0.0,
                    requires_review=True,
                    reason="CHECKBOX options not exposed - cannot resolve safely",
                )
            matched: List[str] = []
            if val:
                val_l = val.lower()
                for opt in question.options:
                    opt_l = opt.lower().strip()
                    if opt_l == "свой вариант":
                        continue  # custom option needs human text - never auto-selected
                    if opt_l in val_l or val_l in opt_l:
                        if opt not in matched:
                            matched.append(opt)
            # fallback: direct resume hit for CHECKBOX (label may be generic
            # e.g. "Какие агенты?" while the fact lives in resume)
            if not matched and self.resume_text:
                resume_l = self.resume_text.lower()
                for opt in question.options:
                    if opt.strip().lower() == "свой вариант":
                        continue
                    if opt.lower().strip() in resume_l and opt not in matched:
                        matched.append(opt)
            if matched:
                return ApplicationAnswer(
                    question_id=question.id,
                    answer="; ".join(matched),
                    answer_type=QuestionType.CHECKBOX,
                    confidence=1.0,
                    requires_review=False,
                    reason="Resolved to existing options",
                )
            return ApplicationAnswer(
                question_id=question.id,
                answer=None,
                answer_type=QuestionType.CHECKBOX,
                confidence=0.0,
                requires_review=True,
                reason="No confirmed fact matches the real options; "
                       + ("'Свой вариант' requires human text" if any(
                           o.strip().lower() == "свой вариант" for o in question.options)
                          else "answer left empty (truth-only)"),
            )

        # Free text (TEXT/TEXTAREA/COVER_LETTER/NUMBER): use confirmed fact or LLM.
        if val:
            return ApplicationAnswer(
                question_id=question.id,
                answer=val,
                answer_type=question.normalized_type,
                confidence=1.0,
                requires_review=False,
                reason="Confirmed from profile/resume",
            )

        if self.llm is not None:
            try:
                text = self.llm(question, self)
                if text:
                    return ApplicationAnswer(
                        question_id=question.id,
                        answer=text,
                        answer_type=question.normalized_type,
                        confidence=0.6,
                        requires_review=True,
                        reason="Generated from truth sources; human review recommended",
                    )
            except Exception:
                pass

        return ApplicationAnswer(
            question_id=question.id,
            answer=None,
            answer_type=question.normalized_type,
            confidence=0.0,
            requires_review=True,
            reason="No confirmed candidate fact; answer left empty (truth-only)",
        )


class ApplicationPackageValidator:
    """Validate a package's Q&A state. Read-only, never mutates, never writes."""

    def validate(self, pkg: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        form: Optional[ApplicationForm] = getattr(pkg, "form", None)
        questions: List[ApplicationQuestion] = list(getattr(pkg, "questions", []) or [])
        answers: List[ApplicationAnswer] = list(getattr(pkg, "answers", []) or [])

        # Fall back to questions embedded in form.
        if not questions and form is not None:
            questions = list(form.questions or [])

        answer_by_qid = {a.question_id: a for a in answers}
        all_valid = True

        for q in questions:
            a = answer_by_qid.get(q.id)
            # UNKNOWN question -> needs review.
            if q.normalized_type == QuestionType.UNKNOWN:
                all_valid = False
                reasons.append(f"Question '{q.label or q.id}' is UNKNOWN - HH did not expose it (needs review)")
                continue
            # Unknown required flag -> needs review.
            if q.required is None:
                all_valid = False
                reasons.append(f"Required status of '{q.label or q.id}' unknown")
                continue
            # Unknown options for constrained types -> needs review.
            if q.normalized_type in (QuestionType.SELECT, QuestionType.RADIO) and not q.options:
                all_valid = False
                reasons.append(f"Options for '{q.label or q.id}' unknown (auth gate) - cannot resolve safely")
                continue
            # Required question without a usable answer.
            if q.required and (a is None or not a.answer or a.requires_review):
                all_valid = False
                reasons.append(f"Required question '{q.label or q.id}' has no confirmed answer")
                continue
            # Option membership check (SELECT / RADIO / CHECKBOX).
            if a and a.answer and q.options and q.normalized_type in (
                    QuestionType.SELECT, QuestionType.RADIO, QuestionType.CHECKBOX):
                if q.normalized_type == QuestionType.CHECKBOX:
                    parts = [p.strip() for p in a.answer.split(";") if p.strip()]
                    invalid = [p for p in parts if p not in q.options]
                    if invalid:
                        all_valid = False
                        reasons.append(f"Answer for '{q.label or q.id}' contains non-option values: {invalid}")
                        continue
                elif a.answer not in q.options:
                    all_valid = False
                    reasons.append(f"Answer for '{q.label or q.id}' is not a real option")
                    continue
            # "Свой вариант" linked textarea: human text is required, which the
            # pipeline never generates -> stays NEEDS_REVIEW by design.
            if q.custom_option_text_id and a and a.answer and any(
                    p.strip().lower() == "свой вариант" for p in a.answer.split(";")):
                all_valid = False
                reasons.append(f"Custom variant selected for '{q.label or q.id}' - human text required")
                continue
            # Any answer flagged requires_review makes the package need review.
            if a and a.requires_review:
                all_valid = False
                reasons.append(f"Answer for '{q.label or q.id}' requires review")

        # Required resume / cover letter.
        if any(q.normalized_type == QuestionType.FILE and q.required for q in questions):
            has_resume = any(a and a.answer for a in answers if a.question_id and getattr(a, "answer_type", None) == QuestionType.FILE)
            if not has_resume:
                all_valid = False
                reasons.append("Required resume missing")

        cover_letter_required = any(q.normalized_type == QuestionType.COVER_LETTER and q.required for q in questions)
        if cover_letter_required and not getattr(pkg, "cover_letter", None):
            all_valid = False
            reasons.append("Required cover letter missing")

        if not questions:
            # No questions at all: nothing to block on (empty/simple form).
            pass

        status = "VALID" if all_valid else "NEEDS_REVIEW"
        return {"status": status, "reasons": reasons}


def resolve_answers(
    questions: List[ApplicationQuestion],
    profile: CandidateProfile,
    resume_text: str,
    deep: Optional[DeepAnalysisResult],
    vacancy: Optional[Vacancy],
    llm: Optional[Any] = None,
) -> List[ApplicationAnswer]:
    """Convenience: resolve all questions in a form to answers."""
    gen = QuestionAnswerGenerator(profile, resume_text, deep, vacancy, llm=llm)
    return [gen.generate(q) for q in questions]


def enrich_package_with_form(
    pkg: Any,
    form: ApplicationForm,
    profile: CandidateProfile,
    resume_text: str,
    deep: Optional[DeepAnalysisResult] = None,
    vacancy: Optional[Vacancy] = None,
    llm: Optional[Any] = None,
) -> Any:
    """Populate a package's Q&A fields from an extracted form.

    Read-only with respect to the DB; never submits, never clicks, never
    fills, never uploads. LLM is allowed only inside answer generation and
    only grounded on truth sources.
    """
    answers = resolve_answers(form.questions, profile, resume_text, deep, vacancy, llm=llm)
    pkg.form = form
    pkg.application_type = form.application_type
    pkg.answers = answers
    result = ApplicationPackageValidator().validate(pkg)
    pkg.validation_status = result["status"]
    pkg.review_reasons = result["reasons"]
    return pkg


def prepare_package_with_form(
    pkg: Any,
    vacancy_stable_id: str,
    url: str,
    profile: CandidateProfile,
    resume_text: str,
    deep: Optional[DeepAnalysisResult] = None,
    vacancy: Optional[Vacancy] = None,
    adapter: Optional[Any] = None,
    canonical_id: Optional[str] = None,
    llm: Optional[Any] = None,
) -> Any:
    """Stage 17D integration: extraction -> resolution -> validation.

    Uses the existing browser-layer extraction (extract_form_for_vacancy)
    and the Stage 17C enrichment. Failure-safe: any extraction error leaves
    the package NEEDS_REVIEW with an explicit reason (never READY_FOR_REVIEW).

    Read-only with respect to the DB. Never submits/clicks/fills/uploads.
    LLM only inside answer generation (truth sources only).
    """
    from .browser_executor import extract_form_for_vacancy

    try:
        form = extract_form_for_vacancy(
            vacancy_stable_id, url, adapter=adapter, canonical_id=canonical_id
        )
    except Exception as e:
        pkg.validation_status = "NEEDS_REVIEW"
        reasons = list(getattr(pkg, "review_reasons", []) or [])
        reasons.append(f"Form extraction failed: {e}")
        pkg.review_reasons = reasons
        return pkg

    if form is None:
        pkg.validation_status = "NEEDS_REVIEW"
        reasons = list(getattr(pkg, "review_reasons", []) or [])
        reasons.append("Form extraction returned no form")
        pkg.review_reasons = reasons
        return pkg

    meta = form.extraction_meta or {}
    gate_reasons: List[str] = []
    if meta.get("captcha"):
        gate_reasons.append("CAPTCHA detected during extraction - manual required")
    if meta.get("cloudflare"):
        gate_reasons.append("Cloudflare challenge detected during extraction - manual required")
    if meta.get("auth_form"):
        gate_reasons.append("HH auth gate: answer fields hidden without login - fields remain UNKNOWN")

    pkg = enrich_package_with_form(pkg, form, profile, resume_text, deep, vacancy, llm=llm)

    # Merge gate reasons with validator reasons (gate reasons first, dedup).
    merged: List[str] = []
    for r in list(gate_reasons) + list(getattr(pkg, "review_reasons", []) or []):
        if r not in merged:
            merged.append(r)
    pkg.review_reasons = merged
    return pkg