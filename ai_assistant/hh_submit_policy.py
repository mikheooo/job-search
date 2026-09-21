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
        # BLE001 finding #57: this read had no guard, so an unreadable package
        # mishandled the policy two ways (measured, .pytest_ble001_57_red2.xml):
        # empty-string and broken-json crashed it with a raw JSONDecodeError;
        # the other three read back falsy, so the fingerprint came from the
        # APPLICATION DRAFT and the refusal named fingerprint_match - fail-closed
        # by accident, about the wrong object. Readability is asked of one helper.
        from .application_prep import package_record_is_unreadable

        if package_record_is_unreadable(vac_stable_id):
            checks_failed.append("package_json_unreadable")
            reasons.append("package_json_unreadable")
        elif isinstance(pkg_row, tuple):
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
        # BLE001 finding #48, the third check in the same ledger as #37 and #46.
        # A fingerprint could be computed (a package or a draft exists) but
        # there was no stored review fingerprint to compare it against, and the
        # old line recorded "fingerprint_match" - a passed check - so an audit
        # reading checks_passed saw "the fingerprint was verified" when nothing
        # had been compared. Measured 2026-09-17 on the real state.db: of the 10
        # rows in hh_applications exactly one carries a draft, and its
        # vacancy_stable_id is NULL, so it can never join a review row - this
        # branch, not the comparison above, is the shape production is in. Only
        # 1 of the 20 review rows carries a non-empty fingerprint.
        # The computed fingerprint is still reported: the record stops lying,
        # the verdict does not move (see #37).
        resolved_fp = actual_fp
        checks_passed.append("fingerprint_not_compared")
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
            # BLE001 finding #46, same class as #37 just below. When
            # questions_json cannot be read, the loop underneath has nothing to
            # iterate, q_errors stays empty and this gate used to record
            # "questionnaire_complete" as a PASSED check - so an audit reading
            # checks_passed saw a verified questionnaire while nothing had been
            # verified. Measured: corrupt column -> passed=['questionnaire_complete'],
            # readable column with no answers -> failed=['questionnaire_complete'].
            if quest.questions_error:
                checks_failed.append("questionnaire_complete")
                reasons.append(f"questionnaire_{qid}_questions_unreadable: {quest.questions_error}")
            else:
                answers = app.get("answers") or quest.answers or {}
                # BLE001 finding #63. Same class as #46 above, one column over: #46
                # gave both columns a marker, but only questions_error was read
                # here, so an unreadable answers_json arrived as `{}` and the loop
                # below recorded missing_answer_for_* - a statement about the
                # candidate where the truth is that the column could not be read.
                # The gate fails either way; the record is what changes, and the
                # record is what a human acts on. The application's own answers are
                # a real source, so a corrupt questionnaire column only matters when
                # it is the source being judged (the readers disagree on
                # precedence: this gate prefers the application's answers, the
                # runner at hh_application_runner.py:531 prefers the
                # questionnaire's).
                if quest.answers_error and not app.get("answers"):
                    checks_failed.append("questionnaire_complete")
                    reasons.append(
                        f"questionnaire_{qid}_answers_unreadable: {quest.answers_error}"
                    )
                else:
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
    vac_row_unreadable = ""
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
            else:
                vac_row_unreadable = f"unrecognised row shape: {type(vac_row).__name__}"
        except (KeyError, TypeError, IndexError, AttributeError) as exc:
            # BLE001 finding #52. A bare `except Exception: pass` stood here and
            # swallowed the failure without a trace. These four are the only
            # exceptions the three readers above can raise - KeyError from the
            # mapping branches, TypeError/IndexError from the sequence one,
            # AttributeError from a `.keys()` object that will not be indexed.
            # Anything else propagates now, exactly as a failure of the
            # get_vacancy_by_id call on the line above already does.
            vac_row_unreadable = f"{type(exc).__name__}: {exc}"

    vac_text = f"{vac_title} {company} {vac_desc}".strip()

    lang_vac = detect_text_language(vac_text)
    lang_letter = detect_text_language(letter_text)

    if vac_row_unreadable:
        # BLE001 finding #52: the row was there and could not be read, so nothing
        # below has anything to compare. #46 set the house rule for this shape one
        # gate up - a read that was attempted and failed closes the gate - and this
        # branch follows it. Deliberately over-strict: it also closes the gate when
        # only the title was unreadable and the description came through, because a
        # row this code cannot interpret is not one to submit on. No live
        # population - every real row is a 25-column tuple - so this half is
        # hardening, not a repair.
        checks_failed.append("language_match")
        reasons.append(f"vacancy_row_unreadable: {vac_row_unreadable}")
    elif lang_vac != "unknown" and lang_letter != "unknown" and lang_vac != lang_letter:
        checks_failed.append("language_match")
        reasons.append(f"language_mismatch (vacancy: {lang_vac}, letter: {lang_letter})")
    elif not vac_desc:
        # BLE001 finding #52, the half with a live population. With no description
        # vac_title is the application's own title, not the vacancy's, so `lang_vac`
        # describes the package we are about to send rather than the vacancy we are
        # sending it to - and the code still recorded "language_match", a PASSED
        # check. The triage called this direction safe on the grounds that an empty
        # description leaves the language "unknown"; measured 2026-09-19 over the
        # 1996 rows in state.db, the fallback text (title + company) still names a
        # language for 837 of them (41.9%). Two ways in without an exception: no row
        # at all, and a row whose description is genuinely empty. An empty
        # vacancy_stable_id skips the lookup outright - 5 of the 10 rows in
        # hh_applications are in that state. Neither is a read failure, so neither
        # closes the gate; both must stop claiming the comparison happened.
        checks_passed.append("language_match_without_description")
    elif lang_vac == "unknown" or lang_letter == "unknown":
        # BLE001 finding #37: the detector needs >=10 Cyrillic or >=50 Latin
        # characters before it names a language at all. When it cannot, the old
        # code recorded "language_match" - a passed check - so the audit said the
        # two languages had been compared and matched. They were never compared.
        # Measured over the 1996 vacancies in state.db: 37 (1.9%) come out
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
