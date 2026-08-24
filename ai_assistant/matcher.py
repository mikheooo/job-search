from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .schema import Vacancy

try:
    from .candidate_profile import CandidateProfile
except Exception:
    CandidateProfile = None  # type: ignore


class JobProfile:
    def __init__(
        self,
        desired_roles: Sequence[str] = (),
        skills: Sequence[str] = (),
        experience: Sequence[str] = (),
        seniority: Sequence[str] = (),
        salary_min: Optional[float] = None,
        salary_max: Optional[float] = None,
        salary_currency: Optional[str] = None,
        employment_types: Sequence[str] = (),
        countries: Sequence[str] = (),
        timezones: Sequence[str] = (),
        excluded_roles: Sequence[str] = (),
        excluded_countries: Sequence[str] = (),
        excluded_companies: Sequence[str] = (),
        required_countries: Sequence[str] = (),
        hard_gates: Sequence[str] = (),
        **kwargs: Any,
    ) -> None:
        self.desired_roles = [str(x).strip().lower() for x in desired_roles if str(x).strip()]
        self.skills = [str(x).strip().lower() for x in skills if str(x).strip()]
        self.experience = [str(x).strip().lower() for x in experience if str(x).strip()]
        self.seniority = [str(x).strip().lower() for x in seniority if str(x).strip()]
        self.salary_min = float(salary_min) if salary_min is not None else None
        self.salary_max = float(salary_max) if salary_max is not None else None
        self.salary_currency = str(salary_currency).strip().upper() if salary_currency else None
        self.employment_types = [str(x).strip().lower() for x in employment_types if str(x).strip()]
        self.countries = [str(x).strip().lower() for x in countries if str(x).strip()]
        self.timezones = [str(x).strip().lower() for x in timezones if str(x).strip()]
        self.excluded_roles = [str(x).strip().lower() for x in excluded_roles if str(x).strip()]
        self.excluded_countries = [str(x).strip().lower() for x in excluded_countries if str(x).strip()]
        self.excluded_companies = [str(x).strip().lower() for x in excluded_companies if str(x).strip()]
        self.required_countries = [str(x).strip().lower() for x in required_countries if str(x).strip()]
        self.hard_gates = [str(x).strip().lower() for x in hard_gates if str(x).strip()]
        self.extra = kwargs
        # support new fields passed via kwargs for forward compat
        # e.g. alternative_roles, preferred_seniority, remote_required etc.
        # store for coercion
        for k, v in kwargs.items():
            setattr(self, k, v)

    def missing_profile_fields(self) -> List[str]:
        missing = []
        if not self.desired_roles:
            missing.append("desired_roles")
        if not self.skills:
            missing.append("skills")
        if self.salary_min is None and self.salary_max is None:
            missing.append("salary_expectations")
        return missing


class MatchResult:
    def __init__(self, score: int, decision: str, reasons: List[str], strengths: List[str], gaps: List[str]) -> None:
        self.score = score
        self.decision = decision
        self.reasons = reasons
        self.strengths = strengths
        self.gaps = gaps

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "decision": self.decision,
            "reasons": self.reasons,
            "strengths": self.strengths,
            "gaps": self.gaps,
        }


# ---- Profile coercion layer ----

def _coerce_profile(profile: Any) -> Dict[str, Any]:
    """Normalize any profile (CandidateProfile or JobProfile) to unified dict."""
    # If it's CandidateProfile (has _desired_roles_lc etc.)
    if CandidateProfile is not None and isinstance(profile, CandidateProfile):
        return {
            "desired_roles_lc": getattr(profile, "_desired_roles_lc", []),
            "alternative_roles_lc": getattr(profile, "_alternative_roles_lc", []),
            "skills_lc": getattr(profile, "_skills_lc", []),
            "seniority_lc": getattr(profile, "_seniority_lc", []),
            "remote_required": bool(getattr(profile, "remote_required", False)),
            "allowed_locations_lc": getattr(profile, "_allowed_locations_lc", []),
            "allowed_timezones_lc": getattr(profile, "_allowed_timezones_lc", []),
            "languages_lc": getattr(profile, "_languages_lc", []),
            "employment_types_lc": getattr(profile, "_employment_types_lc", []),
            "minimum_salary": getattr(profile, "minimum_salary", None),
            "salary_currency": (getattr(profile, "salary_currency", None) or "").upper() or None,
            "excluded_roles_lc": getattr(profile, "_excluded_roles_lc", []),
            "excluded_companies_lc": getattr(profile, "_excluded_companies_lc", []),
            "excluded_countries_lc": getattr(profile, "_excluded_countries_lc", []),
            "excluded_industries_lc": getattr(profile, "_excluded_industries_lc", []),
            "years_experience": getattr(profile, "years_experience", None),
        }
    # JobProfile or dict-like
    # Handle JobProfile legacy
    desired = [str(x).lower() for x in getattr(profile, "desired_roles", [])]
    # alternative_roles may be in extra kwargs
    alt = []
    if hasattr(profile, "alternative_roles"):
        alt = [str(x).lower() for x in getattr(profile, "alternative_roles") or []]
    elif hasattr(profile, "extra") and isinstance(getattr(profile, "extra"), dict):
        alt = [str(x).lower() for x in profile.extra.get("alternative_roles", []) or profile.extra.get("alternativeRoles", [])]
    skills = [str(x).lower() for x in getattr(profile, "skills", [])]
    # seniority: preferred_seniority or seniority or experience
    seniority: List[str] = []
    if hasattr(profile, "preferred_seniority"):
        seniority = [str(x).lower() for x in getattr(profile, "preferred_seniority") or []]
    elif hasattr(profile, "seniority") and getattr(profile, "seniority"):
        seniority = [str(x).lower() for x in getattr(profile, "seniority") or []]
    elif hasattr(profile, "experience") and getattr(profile, "experience"):
        seniority = [str(x).lower() for x in getattr(profile, "experience") or []]
    # also check extra
    if not seniority and hasattr(profile, "extra"):
        seniority = [str(x).lower() for x in profile.extra.get("preferred_seniority", []) or profile.extra.get("seniority", []) or []]

    remote_required = False
    if hasattr(profile, "remote_required"):
        remote_required = bool(getattr(profile, "remote_required"))
    elif hasattr(profile, "extra"):
        remote_required = bool(profile.extra.get("remote_required") or profile.extra.get("remoteRequired"))

    # allowed locations: countries / allowed_locations
    allowed_locs: List[str] = []
    if hasattr(profile, "allowed_locations"):
        allowed_locs = [str(x).lower() for x in getattr(profile, "allowed_locations") or []]
    elif hasattr(profile, "countries"):
        allowed_locs = [str(x).lower() for x in getattr(profile, "countries") or []]
    if hasattr(profile, "extra"):
        extra_locs = profile.extra.get("allowed_locations") or profile.extra.get("allowedLocations") or []
        if extra_locs and not allowed_locs:
            allowed_locs = [str(x).lower() for x in extra_locs]

    allowed_tz: List[str] = []
    if hasattr(profile, "allowed_timezones"):
        allowed_tz = [str(x).lower() for x in getattr(profile, "allowed_timezones") or []]
    elif hasattr(profile, "timezones"):
        allowed_tz = [str(x).lower() for x in getattr(profile, "timezones") or []]
    if hasattr(profile, "extra"):
        extra_tz = profile.extra.get("allowed_timezones") or profile.extra.get("allowedTimezones") or []
        if extra_tz and not allowed_tz:
            allowed_tz = [str(x).lower() for x in extra_tz]

    langs: List[str] = []
    if hasattr(profile, "languages"):
        langs = [str(x).lower() for x in getattr(profile, "languages") or []]
    elif hasattr(profile, "extra"):
        langs = [str(x).lower() for x in profile.extra.get("languages", []) or []]

    emp: List[str] = []
    if hasattr(profile, "employment_types"):
        emp = [str(x).lower() for x in getattr(profile, "employment_types") or []]
    elif hasattr(profile, "extra"):
        emp = [str(x).lower() for x in profile.extra.get("employment_types", []) or profile.extra.get("employmentTypes", []) or []]

    # salary: minimum_salary vs salary_min
    min_sal = getattr(profile, "minimum_salary", None)
    if min_sal is None:
        min_sal = getattr(profile, "salary_min", None)
    if min_sal is None and hasattr(profile, "extra"):
        min_sal = profile.extra.get("minimum_salary") or profile.extra.get("minimumSalary") or profile.extra.get("salary_min")
    if min_sal is not None:
        try:
            min_sal = float(min_sal)
        except Exception:
            min_sal = None

    curr = getattr(profile, "salary_currency", None)
    if not curr and hasattr(profile, "extra"):
        curr = profile.extra.get("salary_currency") or profile.extra.get("salaryCurrency")
    if curr:
        curr = str(curr).strip().upper() or None
    else:
        curr = None

    excl_roles = [str(x).lower() for x in getattr(profile, "excluded_roles", []) or []]
    excl_comp = [str(x).lower() for x in getattr(profile, "excluded_companies", []) or []]
    excl_countries = [str(x).lower() for x in getattr(profile, "excluded_countries", []) or []]
    excl_industries: List[str] = []
    if hasattr(profile, "excluded_industries"):
        excl_industries = [str(x).lower() for x in getattr(profile, "excluded_industries") or []]
    elif hasattr(profile, "extra"):
        excl_industries = [str(x).lower() for x in profile.extra.get("excluded_industries", []) or profile.extra.get("excludedIndustries", []) or []]

    return {
        "desired_roles_lc": desired,
        "alternative_roles_lc": alt,
        "skills_lc": skills,
        "seniority_lc": seniority,
        "remote_required": bool(remote_required),
        "allowed_locations_lc": allowed_locs,
        "allowed_timezones_lc": allowed_tz,
        "languages_lc": langs,
        "employment_types_lc": emp,
        "minimum_salary": min_sal,
        "salary_currency": curr,
        "excluded_roles_lc": excl_roles,
        "excluded_companies_lc": excl_comp,
        "excluded_countries_lc": excl_countries,
        "excluded_industries_lc": excl_industries,
        "years_experience": getattr(profile, "years_experience", None) if hasattr(profile, "years_experience") else None,
    }


# ---- Helpers ----

_REMOTE_KEYWORDS = ["remote", "удален", "удалён", "worldwide", "anywhere", "global", "everywhere", "distributed", "работа удаленно", "удаленная", "удалëнно"]


def _is_remote_vacancy(vacancy: Vacancy) -> bool:
    loc = (vacancy.location or "").lower()
    country_text = ",".join(vacancy.country_restrictions or []).lower()
    tz_text = ",".join(str(x) for x in (vacancy.timezone_restrictions or [])).lower()
    combined = f"{loc} {country_text} {tz_text}".lower()
    title_desc = f"{vacancy.title or ''} {vacancy.description or ''}".lower()
    # check location
    if any(k in combined for k in _REMOTE_KEYWORDS):
        return True
    if any(k in title_desc for k in ["remote", "удален"]):
        # if description explicitly says remote, treat as remote even if location empty
        # but require also location not being on-site city like "Moscow office"
        if "remote" in combined or "remote" in loc or not loc.strip():
            # if no location, assume remote-ish, but check title/desc
            return True
        if "remote" in title_desc:
            return True
    # empty location + no restrictions often means remote in our dataset (adapters set location as Remote)
    if not loc.strip() and not country_text.strip():
        # ambiguous, but consider remote if title/desc suggests remote
        return False
    if loc.strip().lower() in ["remote", "remote worldwide", "remote (worldwide)", "remote (global)", "remote anywhere", "remote, worldwide", "удаленно", "удалённо", "удаленная"]:
        return True
    return False


def _contains_any(text: str, keywords: List[str]) -> Optional[str]:
    tl = text.lower()
    for k in keywords:
        if k and k.lower() in tl:
            return k
    return None


def _hard_constraints(profile_dict: Dict[str, Any], vacancy: Vacancy) -> Tuple[bool, str]:
    text = f"{vacancy.title or ''} {vacancy.description or ''}".lower()
    company = (vacancy.company or "").lower()
    loc = (vacancy.location or "").lower()
    country_text = ", ".join(vacancy.country_restrictions or []).lower()

    for role in profile_dict["excluded_roles_lc"]:
        if role and role in text:
            return True, f"Excluded role found: {role}"
    for comp in profile_dict["excluded_companies_lc"]:
        if comp and comp in company:
            return True, f"Excluded company: {comp}"
    for country in profile_dict["excluded_countries_lc"]:
        if country and (country in country_text or country in loc):
            return True, f"Excluded country: {country}"
    for ind in profile_dict["excluded_industries_lc"]:
        if ind and (ind in text or ind in company):
            return True, f"Excluded industry: {ind}"
    # remote hard constraint
    if profile_dict["remote_required"]:
        if not _is_remote_vacancy(vacancy):
            return True, "Remote required but vacancy is not remote"
    return False, ""


class JobMatcher:
    def __init__(self, profile: Any) -> None:
        self.profile = profile
        self._p = _coerce_profile(profile)

    def match(self, vacancy: Vacancy) -> MatchResult:
        reasons: List[str] = []
        strengths: List[str] = []
        gaps: List[str] = []

        hard_reject, hard_reason = _hard_constraints(self._p, vacancy)
        if hard_reject:
            reasons.append(hard_reason)
            return MatchResult(score=0, decision="SKIP", reasons=reasons, strengths=strengths, gaps=[hard_reason])

        # --- scoring components ---
        total = 0
        breakdown: List[str] = []

        # 1) Role match 25
        role_score = 0
        title_lc = (vacancy.title or "").lower()
        desc_lc = (vacancy.description or "").lower()
        desired = self._p["desired_roles_lc"]
        alternative = self._p["alternative_roles_lc"]
        if desired:
            matched_desired = next((r for r in desired if r in title_lc), None)
            if matched_desired:
                role_score = 25
                strengths.append(f"Exact role match: {matched_desired}")
                breakdown.append(f"role:25/25 exact")
            else:
                # check alternative
                matched_alt = next((r for r in alternative if r in title_lc), None) if alternative else None
                if matched_alt:
                    role_score = 15
                    strengths.append(f"Alternative role match: {matched_alt}")
                    breakdown.append("role:15/25 alternative")
                    gaps.append("No exact desired role, alternative matched")
                else:
                    # also check desired in description partial?
                    matched_desc = next((r for r in desired if r in desc_lc), None) if desired else None
                    if matched_desc:
                        role_score = 10
                        strengths.append(f"Role mentioned in description: {matched_desc}")
                        gaps.append("Role not in title but in description")
                        breakdown.append("role:10/25 desc")
                    else:
                        role_score = 0
                        gaps.append("Role does not match desired/alternative roles")
                        breakdown.append("role:0/25")
        else:
            # no desired roles set -> neutral full
            role_score = 25
            breakdown.append("role:25/25 neutral")
        total += role_score

        # 2) Skills 25
        skills = self._p["skills_lc"]
        skills_score = 0
        if not skills:
            skills_score = 25
            breakdown.append("skills:25/25 neutral")
        else:
            text = f"{title_lc} {desc_lc}"
            matched = [s for s in skills if s in text]
            ratio = len(matched) / len(skills) if skills else 0
            skills_score = int(round(25 * ratio))
            if matched:
                strengths.append(f"Skills match {len(matched)}/{len(skills)}: {', '.join(matched[:5])}")
            missing = [s for s in skills if s not in text]
            if missing:
                gaps.append(f"Missing skills: {', '.join(missing[:5])}")
            if ratio >= 1:
                breakdown.append(f"skills:25/25 {len(matched)}/{len(skills)}")
            elif ratio == 0:
                breakdown.append("skills:0/25 none")
            else:
                breakdown.append(f"skills:{skills_score}/25 {len(matched)}/{len(skills)}")
        total += skills_score

        # 3) Seniority 15
        seniority = self._p["seniority_lc"]
        seniority_score = 0
        if not seniority:
            seniority_score = 15
            breakdown.append("seniority:15/15 neutral")
        else:
            text = f"{title_lc} {desc_lc}"
            hit = next((s for s in seniority if s in text), None)
            if hit:
                seniority_score = 15
                strengths.append(f"Seniority match: {hit}")
                breakdown.append(f"seniority:15/15 {hit}")
            else:
                seniority_score = 0
                gaps.append(f"Seniority mismatch: expected {', '.join(seniority)}")
                breakdown.append("seniority:0/15")
        total += seniority_score

        # 4) Remote/location 15
        loc_score = 0
        allowed_locs = self._p["allowed_locations_lc"]
        allowed_tzs = self._p["allowed_timezones_lc"]
        remote_req = self._p["remote_required"]
        loc_text = f"{(vacancy.location or '').lower()} {', '.join(vacancy.country_restrictions or []).lower()}".strip()
        tz_text = ", ".join(vacancy.timezone_restrictions or []).lower()
        is_remote = _is_remote_vacancy(vacancy)
        if not remote_req and not allowed_locs and not allowed_tzs:
            loc_score = 15
            breakdown.append("location:15/15 neutral")
        else:
            ok = True
            loc_ok = True
            tz_ok = True
            if remote_req:
                if is_remote:
                    strengths.append("Remote location matches requirement")
                else:
                    loc_ok = False
                    gaps.append("Remote required but vacancy not remote")
            if allowed_locs:
                # if is_remote and "remote" in allowed_locs => pass
                if any(loc in loc_text for loc in allowed_locs):
                    strengths.append(f"Location allowed: {loc_text[:40]}")
                elif is_remote and "remote" in allowed_locs:
                    strengths.append("Remote location allowed")
                elif not remote_req and not loc_text:
                    # no location info but we have allowed locs -> neutral? give partial
                    gaps.append(f"Location not in allowed: {', '.join(allowed_locs[:3])}")
                    loc_ok = False
                else:
                    # check if vacancy has no restrictions and is remote -> treat as allowed
                    if is_remote:
                        # remote vacancies often have empty country_restrictions -> consider allowed
                        strengths.append("Remote vacancy considered location-flexible")
                    else:
                        loc_ok = False
                        gaps.append(f"Location not in allowed: {', '.join(allowed_locs[:3])}")
            if allowed_tzs:
                if any(tz in tz_text for tz in allowed_tzs):
                    strengths.append(f"Timezone allowed: {tz_text[:20]}")
                else:
                    # if no tz restrictions, consider flexible?
                    if not tz_text.strip():
                        # no restriction -> pass
                        pass
                    else:
                        tz_ok = False
                        gaps.append(f"Timezone not in allowed: {', '.join(allowed_tzs[:3])}")
            if loc_ok and tz_ok:
                loc_score = 15
                if remote_req or allowed_locs or allowed_tzs:
                    breakdown.append("location:15/15")
            else:
                loc_score = 0
                breakdown.append("location:0/15")
        total += loc_score

        # 5) Salary 10
        salary_score = 0
        min_sal = self._p["minimum_salary"]
        prof_curr = self._p["salary_currency"]
        if min_sal is None:
            salary_score = 10
            breakdown.append("salary:10/10 neutral")
        else:
            vac_min = vacancy.salary_min
            vac_max = vacancy.salary_max
            vac_curr = (vacancy.salary_currency or "").upper() if vacancy.salary_currency else None
            effective = vac_max if vac_max is not None else vac_min
            effective_min = vac_min if vac_min is not None else vac_max
            if effective is None and effective_min is None:
                salary_score = 5
                gaps.append("Salary not specified")
                breakdown.append("salary:5/10 unspecified")
            else:
                # currency check
                if prof_curr and vac_curr and prof_curr != vac_curr:
                    salary_score = 0
                    gaps.append(f"Currency mismatch: {vac_curr} vs {prof_curr}")
                    breakdown.append("salary:0/10 currency")
                else:
                    # check if salary meets minimum
                    # use max for generosity: if max >= min => pass
                    top = effective if effective is not None else effective_min
                    if top is not None and top >= min_sal:
                        salary_score = 10
                        strengths.append(f"Salary meets minimum: {top} {vac_curr or prof_curr or ''}")
                        breakdown.append(f"salary:10/10 {top}>={min_sal}")
                    else:
                        salary_score = 0
                        gaps.append(f"Salary below minimum: {top} < {min_sal}")
                        breakdown.append(f"salary:0/10 {top}<{min_sal}")
        total += salary_score

        # 6) Employment 5
        emp_score = 0
        emp_types = self._p["employment_types_lc"]
        if not emp_types:
            emp_score = 5
            breakdown.append("employment:5/5 neutral")
        else:
            vac_emp = (vacancy.employment_type or "").lower().strip()
            if vac_emp and vac_emp in emp_types:
                emp_score = 5
                strengths.append(f"Employment type matches: {vac_emp}")
                breakdown.append("employment:5/5")
            elif not vac_emp:
                # unspecified -> half? but we give 2?
                emp_score = 2
                gaps.append("Employment type not specified")
                breakdown.append("employment:2/5 unspecified")
            else:
                emp_score = 0
                gaps.append(f"Employment type mismatch: {vac_emp or 'n/a'} not in {', '.join(emp_types[:3])}")
                breakdown.append("employment:0/5")
        total += emp_score

        # 7) Language 5
        lang_score = 0
        langs = self._p["languages_lc"]
        if not langs:
            lang_score = 5
            breakdown.append("language:5/5 neutral")
        else:
            text = f"{title_lc} {desc_lc}"
            # use word boundary for short codes
            found = None
            for lang in langs:
                # normalize en->english, ru->russian etc for matching
                candidates = [lang]
                if lang == "en":
                    candidates = ["en", "english", "английский"]
                elif lang == "ru":
                    candidates = ["ru", "russian", "русский"]
                for cand in candidates:
                    # use regex word boundary for short
                    if len(cand) <= 2:
                        if re.search(rf"\b{re.escape(cand)}\b", text):
                            found = lang
                            break
                    else:
                        if cand in text:
                            found = lang
                            break
                if found:
                    break
            # Also heuristic: if description is non-empty and langs includes en and text is ascii => consider match?
            # but we already handle explicit
            if found:
                lang_score = 5
                strengths.append(f"Language match: {found}")
                breakdown.append("language:5/5")
            else:
                # if vacancy description has no language mention but languages includes en, we still give low?
                # For cases where language not mentioned, treat as gap but not necessarily hard
                # To avoid penalizing many vacancies without explicit language, we give 5 neutral if text has no lang mention?
                # However spec expects language to be weighted, so we give 0 if expected languages not found.
                lang_score = 0
                gaps.append(f"Language mismatch: expected {', '.join(langs[:3])}")
                breakdown.append("language:0/5")
        total += lang_score

        score = max(0, min(100, int(total)))

        # Decision thresholds: 80-100 APPLY, 65-79 REVIEW, 0-64 SKIP
        # Hard constraints already handled
        if score >= 80:
            decision = "APPLY"
        elif score >= 65:
            decision = "REVIEW"
        else:
            decision = "SKIP"

        # Reasons: summarize breakdown + top gaps/strengths
        if decision == "APPLY":
            reasons.append(f"Strong match {score}/100: " + ", ".join(breakdown))
            if strengths:
                reasons.append("Strengths: " + "; ".join(strengths[:3]))
        elif decision == "REVIEW":
            reasons.append(f"Moderate match {score}/100: " + ", ".join(breakdown))
            if gaps:
                reasons.append(f"Needs review: {'; '.join(gaps[:2])}")
            if strengths:
                reasons.append(f"Strengths: {'; '.join(strengths[:2])}")
        else:
            reasons.append(f"Low match {score}/100: " + ", ".join(breakdown))
            if gaps:
                reasons.append(f"Gaps: {'; '.join(gaps[:3])}")

        return MatchResult(score=score, decision=decision, reasons=reasons, strengths=strengths, gaps=gaps)

    def _hard_constraints(self, vacancy: Vacancy) -> Tuple[bool, str]:
        # keep for backward compat direct call
        return _hard_constraints(self._p, vacancy)
