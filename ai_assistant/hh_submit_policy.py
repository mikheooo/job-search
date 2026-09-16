"""Stage 48 / Phase 2: Automated Submit Policy Gate.

Authoritative automated policy evaluation for autonomous HeadHunter submissions.
Replaces human-in-the-loop requirement with strict fail-closed automated invariants:
1. Fingerprint match (Gate 3 invariant).
2. Cover letter quality (300-2500 chars, company/title mention, no template placeholders).
3. Questionnaire completeness (all required questions answered, no uncertain answers).
4. Language match (letter language matches vacancy language).
5. Rate limits (max hourly and daily submit limits).
6. Kill switch (data/STOP_SUBMITS file or database submit_paused flag).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .application_review import compute_review_fingerprint, get_application_review
from .hh_application_orchestrator import (
    HHApplication,
    HHApplicationState,
    SubmitApproval,
    TransitionResult,
    transition_application,
)
from .hh_questionnaire import HHQuestionnaire

logger = logging.getLogger(__name__)

# Default rate limits
DEFAULT_MAX_SUBMITS_PER_HOUR = 10
DEFAULT_MAX_SUBMITS_PER_DAY = 40

# Placeholders that disqualify a cover letter
FORBIDDEN_LETTER_PLACEHOLDERS = ("{{", "[", "TODO", "<")

# Uncertain answers that disqualify questionnaire
UNCERTAIN_ANSWER_VALUES = {
    "не знаю",
    "нет данных",
    "unknown",
    "n/a",
    "na",
    "хз",
    "неизвестно",
    "не уверен",
}


@dataclass
class PolicyDecision:
    approve: bool
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    approval: SubmitApproval | None = None
    fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approve": self.approve,
            "checks_passed": list(self.checks_passed),
            "checks_failed": list(self.checks_failed),
            "reasons": list(self.reasons),
            "approval": self.approval.to_dict() if self.approval else None,
            "fingerprint": self.fingerprint,
        }


def detect_text_language(text: str) -> str:
    """Detect language of text based on character frequency (ru / en / unknown).
    
    For short snippets (< 50 Latin characters with 0 Cyrillic), tech job titles like
    'Python developer' or 'DevOps' are standard in Russian vacancies, so language
    remains 'unknown' unless there is substantial text.
    """
    if not text:
        return "unknown"
    cyr = len(re.findall(r"[а-яА-ЯёЁ]", text))
    lat = len(re.findall(r"[a-zA-Z]", text))
    if cyr >= 10 and cyr > lat:
        return "ru"
    if lat >= 50 and lat > cyr * 2:
        return "en"
    return "unknown"


def evaluate(
    application: dict[str, Any] | HHApplication | str,
    stop_file_path: str | None = None,
    max_per_hour: int | None = None,
    max_per_day: int | None = None,
) -> PolicyDecision:
    """Evaluate an application against all Phase 2 automated policy gates.

    Returns PolicyDecision with approve=True and SubmitApproval if all checks pass,
    or approve=False with reasons and checks_failed if any check fails.
    """
    db.init_db()

    # Resolve application dict
    if isinstance(application, str):
        app_data = db.get_hh_application(application) or db.get_hh_application_by_vacancy(application)
        if not app_data:
            return PolicyDecision(
                approve=False,
                checks_failed=["application_exists"],
                reasons=[f"Application '{application}' not found in database"],
            )
        app = app_data
    elif isinstance(application, HHApplication):
        app = application.model_dump()
    else:
        app = dict(application)

    app_id = app.get("application_id", "")
    vac_stable_id = app.get("vacancy_stable_id", "")
    company = (app.get("employer") or "").strip()
    title = (app.get("title") or "").strip()
    qid = app.get("questionnaire_id")

    checks_passed: list[str] = []
    checks_failed: list[str] = []
    reasons: list[str] = []

    # -------------------------------------------------------------------------
    # Gate 1: Kill Switch
    # -------------------------------------------------------------------------
    # BLE001 finding #21: this was the ONLY consumer of the STOP_SUBMITS file
    # and the only place that spelled out all three stops. Every other path
    # checked two of them, so the stop file stopped the autonomous runner and
    # nothing else. It now asks the shared predicate like everybody else -
    # and keeps the caller-supplied stop_file_path, which the predicate does
    # not know about, by exporting it for the duration of the call.
    _prev_stop_file = os.environ.get("STOP_SUBMITS_FILE")
    if stop_file_path:
        os.environ["STOP_SUBMITS_FILE"] = stop_file_path
    try:
        from .hh_submission import submission_halt_reason

        halt_reason = submission_halt_reason()
    finally:
        if stop_file_path:
            if _prev_stop_file is None:
                os.environ.pop("STOP_SUBMITS_FILE", None)
            else:
                os.environ["STOP_SUBMITS_FILE"] = _prev_stop_file

    if halt_reason:
        checks_failed.append("kill_switch")
        reasons.append("paused")
    else:
        checks_passed.append("kill_switch")

    # -------------------------------------------------------------------------
    # Gate 2: Rate Limits (Hourly & Daily)
    # -------------------------------------------------------------------------
    hour_limit = max_per_hour if max_per_hour is not None else int(os.environ.get("HH_MAX_SUBMITS_PER_HOUR", DEFAULT_MAX_SUBMITS_PER_HOUR))
    day_limit = max_per_day if max_per_day is not None else int(os.environ.get("HH_MAX_SUBMITS_PER_DAY", DEFAULT_MAX_SUBMITS_PER_DAY))

    now = datetime.now(timezone.utc)
    one_hour_ago = (now - timedelta(hours=1)).isoformat()
    one_day_ago = (now - timedelta(days=1)).isoformat()

    submits_last_hour = db.count_submitted_transitions_since(one_hour_ago)
    submits_last_day = db.count_submitted_transitions_since(one_day_ago)

    if submits_last_hour >= hour_limit:
        checks_failed.append("rate_limit_hourly")
        reasons.append(f"hourly_limit_exceeded ({submits_last_hour}/{hour_limit})")
    else:
        checks_passed.append("rate_limit_hourly")

    if submits_last_day >= day_limit:
        checks_failed.append("rate_limit_daily")
        reasons.append(f"daily_limit_exceeded ({submits_last_day}/{day_limit})")
    else:
        checks_passed.append("rate_limit_daily")

    # -------------------------------------------------------------------------
    # Gate 3: Fingerprint Matching (Gate 3 Invariant)
    # -------------------------------------------------------------------------
    resolved_fp: str | None = None
    pkg_row = db.get_application_package(vac_stable_id) if vac_stable_id else None
    pkg_data = None
    if pkg_row:
        if isinstance(pkg_row, tuple):
            raw_json = pkg_row[2] if len(pkg_row) > 2 else None
            pkg_data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        elif isinstance(pkg_row, dict):
            pkg_data = pkg_row
        elif isinstance(pkg_row, str):
            pkg_data = json.loads(pkg_row)

    # Try resolving cover letter from package or app draft
    cover_letter = (pkg_data.get("cover_letter") if pkg_data else None) or app.get("draft") or ""

    review = get_application_review(vac_stable_id) if vac_stable_id else None
    expected_fp = getattr(review, "form_fingerprint", None) or getattr(review, "fingerprint", None) if review else None

    if pkg_data:
        actual_fp = compute_review_fingerprint(vac_stable_id, pkg_data)
    elif cover_letter:
        actual_fp = compute_review_fingerprint(vac_stable_id, {"cover_letter": cover_letter, "answers": app.get("answers", {})})
    else:
        actual_fp = None

    if expected_fp and actual_fp:
        if expected_fp == actual_fp:
            resolved_fp = actual_fp
            checks_passed.append("fingerprint_match")
        else:
            checks_failed.append("fingerprint_match")
            reasons.append(f"fingerprint_mismatch (expected {expected_fp[:8]}, got {actual_fp[:8]})")
    elif actual_fp:
        resolved_fp = actual_fp
        checks_passed.append("fingerprint_match")
    else:
        checks_failed.append("fingerprint_match")
        reasons.append("missing_fingerprint_data")

    # -------------------------------------------------------------------------
    # Gate 4: Cover Letter Quality Check
    # -------------------------------------------------------------------------
    letter_text = (cover_letter or "").strip()
    letter_checks_ok = True

    if not letter_text:
        checks_failed.append("letter_not_empty")
        reasons.append("cover_letter_empty")
        letter_checks_ok = False
    else:
        checks_passed.append("letter_not_empty")

    if letter_text:
        letter_len = len(letter_text)
        if letter_len < 300 or letter_len > 2500:
            checks_failed.append("letter_length")
            reasons.append(f"cover_letter_length_out_of_bounds ({letter_len} chars, expected 300-2500)")
            letter_checks_ok = False
        else:
            checks_passed.append("letter_length")

        # Check placeholder presence
        found_placeholders = [ph for ph in FORBIDDEN_LETTER_PLACEHOLDERS if ph in letter_text]
        if found_placeholders:
            checks_failed.append("letter_no_placeholders")
            reasons.append(f"cover_letter_contains_placeholders ({', '.join(found_placeholders)})")
            letter_checks_ok = False
        else:
            checks_passed.append("letter_no_placeholders")

        # Check company or title mention
        comp_clean = re.sub(r"[^\w\s]", " ", company).strip()
        comp_words = [w.lower() for w in comp_clean.split() if len(w) >= 3]
        title_clean = re.sub(r"[^\w\s]", " ", title).strip()
        title_words = [w.lower() for w in title_clean.split() if len(w) >= 3]

        letter_lower = letter_text.lower()
        has_company = any(w in letter_lower for w in comp_words) if comp_words else False
        has_title = any(w in letter_lower for w in title_words) if title_words else False

        if not (has_company or has_title):
            checks_failed.append("letter_mentions_target")
            reasons.append(f"cover_letter_missing_company_or_title_mention (company: '{company}', title: '{title}')")
            letter_checks_ok = False
        else:
            checks_passed.append("letter_mentions_target")

    # -------------------------------------------------------------------------
    # Gate 5: Questionnaire Completeness Check
    # -------------------------------------------------------------------------
    if qid:
        q_data = db.get_hh_questionnaire(qid)
        if not q_data:
            checks_failed.append("questionnaire_complete")
            reasons.append(f"questionnaire_{qid}_not_found")
        else:
            quest = HHQuestionnaire(**q_data)
            answers = app.get("answers") or quest.answers or {}
            q_errors = []
            for item in quest.questions:
                if item.required:
                    ans = answers.get(item.question_id)
                    if ans is None or str(ans).strip() == "":
                        q_errors.append(f"missing_answer_for_{item.question_id}")
                    elif str(ans).strip().lower() in UNCERTAIN_ANSWER_VALUES:
                        q_errors.append(f"uncertain_answer_for_{item.question_id}")
            if q_errors:
                checks_failed.append("questionnaire_complete")
                reasons.extend(q_errors)
            else:
                checks_passed.append("questionnaire_complete")
    else:
        # BLE001 finding #37: nothing on record says this vacancy has a
        # questionnaire, and the old line recorded "questionnaire_complete" - a
        # passed check - so an audit reading checks_passed saw "the questionnaire
        # was verified" when nothing had been looked at. Measured: 8 of the 10
        # rows in state.db carry no questionnaire_id, two of them in
        # READY_TO_SUBMIT. Whether the page holds questions is exactly what is
        # unknown here (see #34, which fixed the same assumption in gate 8 of
        # hh_submission.py); the record must say "not tracked", not "complete".
        checks_passed.append("questionnaire_not_tracked")

    # -------------------------------------------------------------------------
    # Gate 6: Language Match Check
    # -------------------------------------------------------------------------
    vac_desc = ""
    vac_title = title
    vac_row = db.get_vacancy_by_id(vac_stable_id) if vac_stable_id else None
    if vac_row:
        try:
            if isinstance(vac_row, dict):
                vac_desc = vac_row.get("description") or ""
                vac_title = vac_row.get("title") or title
            elif hasattr(vac_row, "keys"):
                vac_desc = vac_row["description"] or ""
                vac_title = vac_row["title"] or title
            elif isinstance(vac_row, (list, tuple)) and len(vac_row) > 5:
                vac_title = vac_row[3] or title
                vac_desc = vac_row[5] or ""
        except Exception:
            pass

    vac_text = f"{vac_title} {company} {vac_desc}".strip()

    lang_vac = detect_text_language(vac_text)
    lang_letter = detect_text_language(letter_text)

    if lang_vac != "unknown" and lang_letter != "unknown" and lang_vac != lang_letter:
        checks_failed.append("language_match")
        reasons.append(f"language_mismatch (vacancy: {lang_vac}, letter: {lang_letter})")
    elif lang_vac == "unknown" or lang_letter == "unknown":
        # BLE001 finding #37: the detector needs >=10 Cyrillic or >=50 Latin
        # characters before it names a language at all. When it cannot, the old
        # code recorded "language_match" - a passed check - so the audit said the
        # two languages had been compared and matched. They were never compared.
        # Measured over the 1830 vacancies in state.db: 37 (2.0%) come out
        # "unknown".
        checks_passed.append("language_match_undetermined")
    else:
        checks_passed.append("language_match")

    # -------------------------------------------------------------------------
    # Aggregate Decision
    # -------------------------------------------------------------------------
    approve = len(checks_failed) == 0
    approval_obj: SubmitApproval | None = None

    if approve:
        approval_obj = SubmitApproval(
            source="policy",
            policy_version="v2.0_autonomous",
            checks_passed=list(checks_passed),
        )

    return PolicyDecision(
        approve=approve,
        checks_passed=checks_passed,
        checks_failed=checks_failed,
        reasons=reasons,
        approval=approval_obj,
        fingerprint=resolved_fp,
    )


def route_policy_rejection(
    application_id: str,
    decision: PolicyDecision,
) -> TransitionResult:
    """Transition application to NEEDS_HUMAN_REVIEW with policy rejection evidence."""
    return transition_application(
        application_id=application_id,
        to_state=HHApplicationState.NEEDS_HUMAN_REVIEW,
        reason="policy_check_failed",
        evidence={
            "reasons": decision.reasons,
            "checks_failed": decision.checks_failed,
            "checks_passed": decision.checks_passed,
            "policy_decision": decision.to_dict(),
        },
    )
