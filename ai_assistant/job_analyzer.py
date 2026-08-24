from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from . import config
from .candidate_profile import CandidateProfile
from .schema import Vacancy
from .matcher import MatchResult

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

ANALYZER_VERSION = "v1"


class DeepAnalysisResult(BaseModel):
    fit_score: int = Field(..., ge=0, le=100, description="0-100 fit score")
    recommendation: Literal["APPLY", "REVIEW", "SKIP"] = Field(..., description="APPLY / REVIEW / SKIP")
    why_fit: List[str] = Field(default_factory=list, description="Why candidate fits")
    gaps: List[str] = Field(default_factory=list, description="Gaps vs requirements")
    must_have_requirements: List[str] = Field(default_factory=list)
    nice_to_have_requirements: List[str] = Field(default_factory=list)
    matched_skills: List[str] = Field(default_factory=list)
    missing_skills: List[str] = Field(default_factory=list)
    seniority_assessment: str = Field(default="", description="seniority match assessment")
    remote_assessment: str = Field(default="", description="remote/location assessment")
    salary_assessment: str = Field(default="", description="salary assessment")
    resume_adaptation_needed: bool = Field(default=False)
    resume_adaptation_reasons: List[str] = Field(default_factory=list)
    application_strategy: str = Field(default="", description="short strategy")

    model_config = {"extra": "forbid"}


# Resume discovery - respects principle: truth sources are CandidateProfile + existing resume file
RESUME_CANDIDATES = [
    Path("resume.md"),
    Path("RESUME.md"),
    Path("Resume.md"),
    Path("ai_assistant/resume.md"),
    Path("ai_assistant/RESUME.md"),
    Path("../resume.md"),
]


def get_resume_text(profile: Optional[CandidateProfile] = None) -> str:
    for p in RESUME_CANDIDATES:
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except Exception:
                continue
    # fallback: synthesize from profile + minimal known experience (do NOT invent)
    if profile:
        # Use only profile truth, mark unknown as not confirmed
        parts = []
        if profile.skills:
            parts.append(f"Skills (confirmed): {', '.join(profile.skills)}")
        if profile.desired_roles:
            parts.append(f"Target roles: {', '.join(profile.desired_roles)}")
        if profile.preferred_seniority:
            parts.append(f"Seniority: {', '.join(profile.preferred_seniority)}")
        if profile.years_experience:
            parts.append(f"Years experience: {profile.years_experience}")
        if profile.languages:
            parts.append(f"Languages: {', '.join(profile.languages)}")
        if parts:
            return "\n".join(parts) + "\n\n(Only above is confirmed. Other experience: unknown / not confirmed)"
    # ultimate fallback from pipeline.py
    return "AI Automation Engineer. n8n, Python, Telegram Bots. n8n, Make, JavaScript, Claude/OpenAI APIs, webhooks, RAG pipelines. (Other experience: unknown / not confirmed)"


def _build_system_prompt() -> str:
    schema = DeepAnalysisResult.model_json_schema()
    return (
        "You are a precise job-fit analyzer. You MUST NOT invent candidate experience.\n"
        "Truth sources ONLY: CandidateProfile JSON and resume text provided. If a requirement is not confirmed by these sources, output 'unknown' or 'not confirmed' and NEVER claim it as present. Never turn unknown into 'has experience'.\n"
        "Return ONLY valid JSON strictly matching this schema, no extra text:\n"
        f"{json.dumps(schema, ensure_ascii=False)}\n"
        "Rules:\n"
        "- fit_score 0-100 integer\n"
        "- recommendation: APPLY if strong fit, REVIEW if partial, SKIP if poor or hard mismatch\n"
        "- why_fit: concrete matched points confirmed by profile/resume\n"
        "- gaps: requirements not confirmed (mark as 'not confirmed' or 'unknown')\n"
        "- must_have_requirements / nice_to_have_requirements: extracted from vacancy description\n"
        "- matched_skills / missing_skills: based ONLY on profile.skills vs vacancy, missing means not confirmed\n"
        "- seniority_assessment, remote_assessment, salary_assessment: short assessments, use unknown if not confirmed\n"
        "- resume_adaptation_needed: true if gaps require tailoring, with reasons\n"
        "- application_strategy: 1-2 sentences\n"
    )


def _build_user_prompt(vacancy: Vacancy, profile: CandidateProfile, resume_text: str, match: Optional[MatchResult]) -> str:
    profile_json = json.dumps(profile.to_dict(), ensure_ascii=False, indent=2)
    vacancy_json = json.dumps(
        {
            "title": vacancy.title,
            "company": vacancy.company,
            "location": vacancy.location,
            "description": vacancy.description[:4000],
            "salary_min": vacancy.salary_min,
            "salary_max": vacancy.salary_max,
            "salary_currency": vacancy.salary_currency,
            "employment_type": vacancy.employment_type,
            "country_restrictions": vacancy.country_restrictions,
            "job_url": vacancy.job_url,
        },
        ensure_ascii=False,
        indent=2,
    )
    match_info = ""
    if match:
        match_info = f"Matcher deterministic result: score={match.score} decision={match.decision} strengths={match.strengths} gaps={match.gaps}"

    return (
        f"CandidateProfile (TRUTH SOURCE):\n{profile_json}\n\n"
        f"Resume (TRUTH SOURCE, if empty use profile only, unknown = not confirmed):\n{resume_text[:3000]}\n\n"
        f"Vacancy:\n{vacancy_json}\n\n"
        f"{match_info}\n\n"
        "Task: Analyze fit. Do NOT invent experience. If requirement not in profile/resume, list as missing/unknown."
    )


def _call_llm(system_prompt: str, user_prompt: str) -> str:
    if os.getenv("JOB_ANALYZER_OFFLINE") == "1":
        raise RuntimeError("Offline mode - skip LLM")
    if OpenAI is None:
        raise RuntimeError("OpenAI client not available")
    # short timeout to avoid hanging CLI when network unavailable
    client = OpenAI(
        api_key=config.LLM_API_KEY if config.LLM_API_KEY else "dummy-key",
        base_url=config.LLM_BASE_URL if config.LLM_BASE_URL else "https://api.x.ai/v1",
        timeout=12,
    )
    resp = client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content
    if not content:
        raise RuntimeError("Empty LLM response")
    return content


def _fallback_analysis(vacancy: Vacancy, profile: CandidateProfile, match: Optional[MatchResult]) -> DeepAnalysisResult:
    # Deterministic fallback that respects "do not invent" principle
    desc_lower = (vacancy.description or "").lower()
    title_lower = (vacancy.title or "").lower()
    text = f"{title_lower} {desc_lower}"

    # must_have / nice_to_have naive extraction: split vacancy description into lines or sentences containing "must", "required", "nice"
    must = []
    nice = []
    # very simple heuristic: extract bullet-like lines
    for line in (vacancy.description or "").splitlines():
        ll = line.strip().lower()
        if any(k in ll for k in ["must", "required", "треб", "обязат"]):
            must.append(line.strip()[:120])
        elif any(k in ll for k in ["nice", "plus", "желательно", "плюс"]):
            nice.append(line.strip()[:120])
    if not must:
        # fallback: first 3 short sentences as must
        import re
        sents = re.split(r"[.\n]+", vacancy.description or "")
        must = [s.strip()[:120] for s in sents if s.strip()][:3]
        must = [m for m in must if m]

    # matched / missing based strictly on profile.skills
    matched = []
    missing = []
    for skill in profile.skills:
        if skill.lower() in text:
            matched.append(skill)
        else:
            missing.append(skill)

    # why_fit from match strengths
    why_fit = []
    if match and match.strengths:
        why_fit = match.strengths[:4]
    if matched:
        why_fit.append(f"Matched skills: {', '.join(matched[:4])}")
    if not why_fit:
        why_fit = ["Profile role/skills partially match"] if matched else ["No confirmed direct matches (unknown)"]

    gaps = []
    if match and match.gaps:
        gaps = match.gaps[:4]
    if missing:
        gaps.append(f"Missing/not confirmed: {', '.join(missing[:4])}")
    # ensure gaps use unknown phrasing if not confirmed
    gaps = [g if "not confirmed" in g.lower() or "unknown" in g.lower() else g + " (not confirmed)" if missing and g.startswith("Missing") else g for g in gaps]
    if not gaps:
        gaps = ["No major gaps confirmed, some requirements unknown / not confirmed"]

    # assessments
    seniority_assessment = "unknown / not confirmed"
    if profile.preferred_seniority:
        hit = next((s for s in profile.preferred_seniority if s.lower() in text), None)
        if hit:
            seniority_assessment = f"Match: {hit} confirmed"
        else:
            seniority_assessment = f"Required seniority not confirmed in profile (expected {', '.join(profile.preferred_seniority)}) - unknown"

    remote_assessment = "unknown / not confirmed"
    loc = (vacancy.location or "").lower()
    if "remote" in loc or "удален" in text:
        if profile.remote_required:
            remote_assessment = "Remote - matches requirement (confirmed)"
        else:
            remote_assessment = "Remote vacancy, profile allows remote"
    else:
        remote_assessment = "On-site / not confirmed remote"

    salary_assessment = "unknown / not confirmed"
    if profile.minimum_salary is not None:
        if vacancy.salary_min is not None or vacancy.salary_max is not None:
            top = vacancy.salary_max if vacancy.salary_max is not None else vacancy.salary_min
            if top is not None and top >= profile.minimum_salary:
                salary_assessment = f"Salary {top} {vacancy.salary_currency or profile.salary_currency or ''} meets minimum {profile.minimum_salary} (confirmed)"
            else:
                salary_assessment = f"Salary {top} below minimum {profile.minimum_salary} or currency not confirmed"
        else:
            salary_assessment = "Salary not specified in vacancy - unknown / not confirmed"
    else:
        salary_assessment = "No salary requirement in profile"

    # fit_score derived from matcher or heuristic
    base = match.score if match else 50
    # adjust: if many missing skills, reduce
    if missing:
        base = max(0, base - len(missing) * 2)
    fit_score = max(0, min(100, base))
    if fit_score >= 80:
        rec: Literal["APPLY", "REVIEW", "SKIP"] = "APPLY"
    elif fit_score >= 65:
        rec = "REVIEW"
    else:
        rec = "SKIP"

    resume_needed = len(missing) > 0 or len(gaps) > 2
    resume_reasons = []
    if resume_needed:
        if missing:
            resume_reasons.append(f"Highlight/clarify missing skills not confirmed: {', '.join(missing[:3])}")
        if seniority_assessment.startswith("Required"):
            resume_reasons.append("Clarify seniority not confirmed")
        if not resume_reasons:
            resume_reasons.append("Tailor resume to vacancy keywords (unknown gaps)")

    strategy = "Apply directly with tailored resume highlighting n8n/Python/automation if APPLY, otherwise network/referral." if rec == "APPLY" else "Review gaps before applying; consider referral or skill gap closing." if rec == "REVIEW" else "Skip - hard mismatch or many unknowns."

    return DeepAnalysisResult(
        fit_score=fit_score,
        recommendation=rec,
        why_fit=why_fit[:5],
        gaps=gaps[:5],
        must_have_requirements=must[:5] if must else ["Not explicitly listed - unknown"],
        nice_to_have_requirements=nice[:5],
        matched_skills=matched,
        missing_skills=missing,
        seniority_assessment=seniority_assessment,
        remote_assessment=remote_assessment,
        salary_assessment=salary_assessment,
        resume_adaptation_needed=resume_needed,
        resume_adaptation_reasons=resume_reasons[:3],
        application_strategy=strategy,
    )


def analyze_job_deep(
    vacancy: Vacancy,
    profile: CandidateProfile,
    match_result: Optional[MatchResult] = None,
    resume_text: Optional[str] = None,
) -> DeepAnalysisResult:
    if resume_text is None:
        resume_text = get_resume_text(profile)

    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(vacancy, profile, resume_text, match_result)

    try:
        raw_json = _call_llm(system_prompt, user_prompt)
        # Validate via Pydantic
        data = json.loads(raw_json)
        result = DeepAnalysisResult.model_validate(data)
        # Enforce principle: ensure missing vs matched consistency - if LLM invented, we keep but log warning
        # Ensure no invented experience by checking that matched_skills are subset of profile.skills (or resume)
        # If LLM returns matched skill not in profile, move to missing with unknown
        profile_skills_lc = {s.lower() for s in profile.skills}
        resume_lc = resume_text.lower()
        filtered_matched = []
        filtered_missing = list(result.missing_skills)
        for ms in result.matched_skills:
            if ms.lower() in profile_skills_lc or ms.lower() in resume_lc:
                filtered_matched.append(ms)
            else:
                # invented -> move to missing as not confirmed
                if ms not in filtered_missing:
                    filtered_missing.append(ms + " (not confirmed - not in profile)")
                logging.warning(f"LLM invented skill {ms} not in profile, correcting to not confirmed")
        result.matched_skills = filtered_matched
        result.missing_skills = filtered_missing
        return result
    except Exception as e:
        logging.warning(f"LLM analysis failed, using fallback: {e}")
        return _fallback_analysis(vacancy, profile, match_result)


def should_analyze(match_result: MatchResult) -> bool:
    return match_result.decision in ("APPLY", "REVIEW")
