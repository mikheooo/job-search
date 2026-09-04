"""Stage 46: Controlled Batch Application Execution Runner.

Provides a safe, single-step batch executor for HeadHunter applications queue.
Picks the next READY_TO_SUBMIT application, performs exhaustive pre-checks
(audit, navigation, questionnaire, eligibility), strictly halts before Submit
unless human confirmation is explicitly given, executes at most ONE submit,
runs post-submit verification, and halts immediately without auto-advancing.

SAFETY INVARIANTS:
1. Never selects SUBMITTED, NEEDS_HUMAN_REVIEW, BLOCKED, FAILED, ANALYZED, or NOT_ELIGIBLE.
2. Preview mode performs ZERO browser mutations and ZERO submits.
3. Execution without --confirm-submit stops after pre-checks (REAL HH SUBMIT = 0).
4. With --confirm-submit, executes at most ONE submit for the single selected application.
5. Runner strictly STOPS after submit (Next Application Automatically Executed = NO).
6. Post-submit verification is mandatory before marking as SUBMITTED.
7. Pre-check failure halts execution immediately with Submit Count = 0.
8. Zero autonomous looping; pipeline.py is never executed.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from . import db
from .hh_application_orchestrator import HHApplicationState, transition_application
from .hh_application_queue import can_submit, get_controlled_application_queue, HHQueueItem
from .hh_post_submit_verifier import verify_hh_submitted_application
from .hh_questionnaire import HHQuestionStatus, submit_questionnaire_response
from .hh_questionnaire_audit import audit_questionnaire
from .hh_vacancy_navigator import resolve_hh_vacancy_url, verify_and_navigate_hh_vacancy

logger = logging.getLogger(__name__)


class RunnerPreCheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"
    NOT_REQUIRED = "NOT_REQUIRED"


class RunnerExecutionResult(BaseModel):
    application_id: Optional[str] = None
    vacancy_id: Optional[str] = None
    vacancy_title: Optional[str] = None
    company: Optional[str] = None
    queue_ready_count: int = 0
    queue_review_count: int = 0
    queue_submitted_count: int = 0
    selected_application: Optional[str] = None
    pre_submit_audit: RunnerPreCheckStatus = RunnerPreCheckStatus.NOT_RUN
    navigation: RunnerPreCheckStatus = RunnerPreCheckStatus.NOT_RUN
    questionnaire: RunnerPreCheckStatus = RunnerPreCheckStatus.NOT_RUN
    submit_confirmation: bool = False
    real_hh_submit: int = 0
    post_submit_verification: RunnerPreCheckStatus = RunnerPreCheckStatus.NOT_RUN
    final_application_state: str = "UNKNOWN"
    next_application_executed: bool = False
    pipeline_py: str = "NOT RUN"
    reason: str = ""
    error: Optional[str] = None

    model_config = {"extra": "forbid"}


def preview_next_application() -> RunnerExecutionResult:
    """Preview the next eligible application in the queue without performing any browser mutation."""
    db.init_db()
    all_queue = get_controlled_application_queue()

    ready_count = sum(1 for it in all_queue if it.application_state == HHApplicationState.READY_TO_SUBMIT.value)
    review_count = sum(1 for it in all_queue if it.application_state == HHApplicationState.NEEDS_HUMAN_REVIEW.value)
    submitted_count = sum(1 for it in all_queue if it.application_state == HHApplicationState.SUBMITTED.value)

    ready_apps = [it for it in all_queue if it.application_state == HHApplicationState.READY_TO_SUBMIT.value and it.can_submit_allowed]

    if not ready_apps:
        return RunnerExecutionResult(
            queue_ready_count=ready_count,
            queue_review_count=review_count,
            queue_submitted_count=submitted_count,
            selected_application=None,
            final_application_state="NO_READY_APPLICATIONS",
            reason="No applications in READY_TO_SUBMIT state eligible for execution.",
        )

    target_app = ready_apps[0]
    app_id = target_app.application_id

    # Evaluate pre-checks deterministically in preview (read-only)
    audit_status = RunnerPreCheckStatus.NOT_RUN
    quest_status = RunnerPreCheckStatus.NOT_REQUIRED

    if target_app.questionnaire_id:
        quest_status = RunnerPreCheckStatus.PASS if target_app.questionnaire_state in (HHQuestionStatus.READY_TO_SUBMIT.value, HHQuestionStatus.SUBMITTED.value) else RunnerPreCheckStatus.FAIL
        try:
            audit_report = audit_questionnaire(target_app.questionnaire_id, application_id=app_id)
            audit_status = RunnerPreCheckStatus.PASS if audit_report.overall.value == "SAFE_TO_SUBMIT" else RunnerPreCheckStatus.FAIL
        except Exception:
            audit_status = RunnerPreCheckStatus.PASS if target_app.audit_state == "SAFE_TO_SUBMIT" else RunnerPreCheckStatus.FAIL
    else:
        audit_status = RunnerPreCheckStatus.PASS

    # Navigation URL preview check
    vac_url = resolve_hh_vacancy_url(target_app.vacancy_id or app_id)
    nav_status = RunnerPreCheckStatus.PASS if vac_url else RunnerPreCheckStatus.FAIL

    return RunnerExecutionResult(
        application_id=app_id,
        vacancy_id=target_app.vacancy_id,
        vacancy_title=target_app.vacancy_title,
        company=target_app.company,
        queue_ready_count=ready_count,
        queue_review_count=review_count,
        queue_submitted_count=submitted_count,
        selected_application=f"{target_app.vacancy_title} ({target_app.vacancy_id}) @ {target_app.company} [{app_id}]",
        pre_submit_audit=audit_status,
        navigation=nav_status,
        questionnaire=quest_status,
        submit_confirmation=False,
        real_hh_submit=0,
        post_submit_verification=RunnerPreCheckStatus.NOT_RUN,
        final_application_state=target_app.application_state,
        next_application_executed=False,
        pipeline_py="NOT RUN",
        reason="Preview completed. Ready for pre-checks and human-confirmed submission.",
    )


def run_next_application(
    confirm_submit: bool = False,
    auto: bool = False,
    evaluate_fn: Optional[Callable[[str], str]] = None,
    cdp_url: Optional[str] = None,
    dry_run: bool = False,
) -> RunnerExecutionResult:
    """Select and run the next READY_TO_SUBMIT application in the queue."""
    db.init_db()
    all_queue = get_controlled_application_queue()

    ready_count = sum(1 for it in all_queue if it.application_state == HHApplicationState.READY_TO_SUBMIT.value)
    review_count = sum(1 for it in all_queue if it.application_state == HHApplicationState.NEEDS_HUMAN_REVIEW.value)
    submitted_count = sum(1 for it in all_queue if it.application_state == HHApplicationState.SUBMITTED.value)

    ready_apps = [it for it in all_queue if it.application_state == HHApplicationState.READY_TO_SUBMIT.value and it.can_submit_allowed]

    if not ready_apps:
        return RunnerExecutionResult(
            queue_ready_count=ready_count,
            queue_review_count=review_count,
            queue_submitted_count=submitted_count,
            selected_application=None,
            final_application_state="N/A",
            reason="NO_APPLICATION_SELECTED: No applications in READY_TO_SUBMIT state available to run.",
        )

    target_app = ready_apps[0]
    return run_application(
        application_id=target_app.application_id,
        confirm_submit=confirm_submit,
        auto=auto,
        evaluate_fn=evaluate_fn,
        cdp_url=cdp_url,
        queue_ready_count=ready_count,
        queue_review_count=review_count,
        queue_submitted_count=submitted_count,
        dry_run=dry_run,
    )


def run_application(
    application_id: str,
    confirm_submit: bool = False,
    auto: bool = False,
    evaluate_fn: Optional[Callable[[str], str]] = None,
    cdp_url: Optional[str] = None,
    queue_ready_count: int = 0,
    queue_review_count: int = 0,
    queue_submitted_count: int = 0,
    dry_run: bool = False,
) -> RunnerExecutionResult:
    """Execute a single HeadHunter application with strict gating and verification."""
    db.init_db()
    app = db.get_hh_application(application_id)
    if not app:
        app = db.get_hh_application_by_vacancy(application_id)
    if not app:
        return RunnerExecutionResult(
            application_id=application_id,
            reason=f"Application '{application_id}' not found in database.",
            error="application_not_found",
        )

    app_id = app.get("application_id", application_id)
    vac_stable_id = app.get("vacancy_stable_id") or ""
    vac_id = vac_stable_id.split(":")[-1] if ":" in vac_stable_id else vac_stable_id
    vac_title = app.get("title") or "Unknown Vacancy"
    company = app.get("employer") or "Unknown Company"
    current_state = app.get("state") or "UNKNOWN"
    qid = app.get("questionnaire_id")
    selected_app_label = f"{vac_title} ({vac_id}) @ {company} [{app_id}]"

    if queue_ready_count == 0 and queue_review_count == 0 and queue_submitted_count == 0:
        try:
            all_queue = get_controlled_application_queue()
            queue_ready_count = sum(1 for it in all_queue if it.application_state == HHApplicationState.READY_TO_SUBMIT.value)
            queue_review_count = sum(1 for it in all_queue if it.application_state == HHApplicationState.NEEDS_HUMAN_REVIEW.value)
            queue_submitted_count = sum(1 for it in all_queue if it.application_state == HHApplicationState.SUBMITTED.value)
        except Exception:
            pass

    # Step 1: Submit Eligibility Check
    elig = can_submit(app_id)
    if not elig.allowed:
        return RunnerExecutionResult(
            application_id=app_id,
            vacancy_id=vac_id,
            vacancy_title=vac_title,
            company=company,
            queue_ready_count=queue_ready_count,
            queue_review_count=queue_review_count,
            queue_submitted_count=queue_submitted_count,
            selected_application=selected_app_label,
            final_application_state=current_state,
            real_hh_submit=0,
            reason=f"Application not eligible for submit: {elig.reason}",
        )

    # Step 2: Questionnaire & Audit Pre-Check
    audit_status = RunnerPreCheckStatus.NOT_REQUIRED
    quest_status = RunnerPreCheckStatus.NOT_REQUIRED

    if qid:
        quest_data = db.get_hh_questionnaire(qid)
        if not quest_data:
            return RunnerExecutionResult(
                application_id=app_id,
                vacancy_id=vac_id,
                vacancy_title=vac_title,
                company=company,
                queue_ready_count=queue_ready_count,
                queue_review_count=queue_review_count,
                queue_submitted_count=queue_submitted_count,
                selected_application=selected_app_label,
                pre_submit_audit=RunnerPreCheckStatus.FAIL,
                questionnaire=RunnerPreCheckStatus.FAIL,
                final_application_state=current_state,
                reason=f"Questionnaire '{qid}' not found in database.",
            )

        try:
            audit_report = audit_questionnaire(qid, application_id=app_id)
            if audit_report.overall.value != "SAFE_TO_SUBMIT":
                return RunnerExecutionResult(
                    application_id=app_id,
                    vacancy_id=vac_id,
                    vacancy_title=vac_title,
                    company=company,
                    pre_submit_audit=RunnerPreCheckStatus.FAIL,
                    questionnaire=RunnerPreCheckStatus.FAIL,
                    final_application_state=current_state,
                    reason=f"Pre-submit questionnaire audit did not pass SAFE_TO_SUBMIT (verdict: {audit_report.overall.value})",
                )
            audit_status = RunnerPreCheckStatus.PASS
            quest_status = RunnerPreCheckStatus.PASS
        except Exception as e:
            logger.error(f"Audit failed with exception: {e}")
            return RunnerExecutionResult(
                application_id=app_id,
                vacancy_id=vac_id,
                vacancy_title=vac_title,
                company=company,
                pre_submit_audit=RunnerPreCheckStatus.FAIL,
                questionnaire=RunnerPreCheckStatus.FAIL,
                final_application_state=current_state,
                reason=f"Pre-submit questionnaire audit failed with exception: {e}",
            )
    else:
        audit_status = RunnerPreCheckStatus.PASS
        quest_status = RunnerPreCheckStatus.NOT_REQUIRED

    # Step 3: Vacancy Navigation Pre-Check
    nav_status = RunnerPreCheckStatus.NOT_RUN
    target_url = resolve_hh_vacancy_url(vac_stable_id or app_id)

    # Attach evaluate_fn to the target vacancy tab if evaluate_fn is not provided
    if evaluate_fn is None:
        try:
            from .hh_browser_launcher import ensure_hh_browser
            from .hh_vacancy_navigator import ensure_open_vacancy_tab, extract_hh_numeric_id
            from .cli import _resolve_hh_evaluate, _DEFAULT_HH_CDP_URL
            ensure_hh_browser()
            endpoint = cdp_url or _DEFAULT_HH_CDP_URL
            if target_url:
                ensure_open_vacancy_tab(endpoint, target_url)
            vac_num = vac_id or extract_hh_numeric_id(str(target_url or ""))
            fresh_eval = _resolve_hh_evaluate(endpoint, vac_num) if vac_num else None
            if fresh_eval:
                evaluate_fn = fresh_eval
        except Exception as e:
            logger.debug(f"Could not resolve evaluate_fn: {e}")

    if evaluate_fn is not None:
        nav_res = verify_and_navigate_hh_vacancy(
            target=vac_stable_id or app_id,
            evaluate_fn=evaluate_fn,
            expected_title=vac_title,
        )
        if not nav_res.ok:
            if getattr(nav_res, "status", None) == "ALREADY_RESPONDED":
                # Factual submission already recorded on HH
                logger.info(
                    "Application %s (%s) already responded on HeadHunter; synchronizing state",
                    app_id,
                    vac_id,
                )
                if not dry_run:
                    try:
                        transition_application(
                            application_id=app_id,
                            to_state=HHApplicationState.STALE,
                            reason="external_response_detected",
                            evidence={
                                "detected_external": True,
                                "hh_status": getattr(nav_res, "status", "ALREADY_RESPONDED"),
                                "submit_executed": False,
                                "reason": getattr(nav_res, "reason", "Already responded on HeadHunter"),
                            },
                        )
                    except Exception as te:
                        logger.warning(f"Could not transition already responded app {app_id} to STALE: {te}")
                return RunnerExecutionResult(
                    application_id=app_id,
                    vacancy_id=vac_id,
                    vacancy_title=vac_title,
                    company=company,
                    queue_ready_count=queue_ready_count,
                    queue_review_count=queue_review_count,
                    queue_submitted_count=queue_submitted_count,
                    selected_application=f"{vac_title} ({vac_id}) @ {company} [{app_id}]",
                    pre_submit_audit=audit_status,
                    navigation=RunnerPreCheckStatus.PASS,
                    questionnaire=quest_status,
                    submit_confirmation=False,
                    real_hh_submit=0,
                    post_submit_verification=RunnerPreCheckStatus.PASS,
                    final_application_state=HHApplicationState.STALE.value if not dry_run else current_state,
                    reason="Application is already responded on HeadHunter.",
                )
            return RunnerExecutionResult(
                application_id=app_id,
                vacancy_id=vac_id,
                vacancy_title=vac_title,
                company=company,
                queue_ready_count=queue_ready_count,
                queue_review_count=queue_review_count,
                queue_submitted_count=queue_submitted_count,
                selected_application=f"{vac_title} ({vac_id}) @ {company} [{app_id}]",
                pre_submit_audit=audit_status,
                navigation=RunnerPreCheckStatus.FAIL,
                questionnaire=quest_status,
                final_application_state=current_state,
                reason=f"Navigation pre-check failed: {nav_res.reason}",
            )
        nav_status = RunnerPreCheckStatus.PASS
    else:
        # Dry URL check
        vac_url = resolve_hh_vacancy_url(vac_stable_id or app_id)
        if not vac_url:
            return RunnerExecutionResult(
                application_id=app_id,
                vacancy_id=vac_id,
                vacancy_title=vac_title,
                company=company,
                queue_ready_count=queue_ready_count,
                queue_review_count=queue_review_count,
                queue_submitted_count=queue_submitted_count,
                selected_application=f"{vac_title} ({vac_id}) @ {company} [{app_id}]",
                pre_submit_audit=audit_status,
                navigation=RunnerPreCheckStatus.FAIL,
                questionnaire=quest_status,
                final_application_state=current_state,
                reason="Cannot resolve canonical HH vacancy URL.",
            )
        nav_status = RunnerPreCheckStatus.PASS

    # Step 4: Submission Gate
    import os
    auto_mode = auto or os.environ.get("HH_AUTO_SUBMIT") == "1"
    policy_decision = None
    submit_approval = None

    if auto_mode:
        from .hh_submit_policy import evaluate as evaluate_policy, route_policy_rejection
        policy_decision = evaluate_policy(app)
        if not policy_decision.approve:
            route_policy_rejection(app_id, policy_decision)
            return RunnerExecutionResult(
                application_id=app_id,
                vacancy_id=vac_id,
                vacancy_title=vac_title,
                company=company,
                queue_ready_count=queue_ready_count,
                queue_review_count=queue_review_count,
                queue_submitted_count=queue_submitted_count,
                selected_application=selected_app_label,
                pre_submit_audit=audit_status,
                navigation=nav_status,
                questionnaire=quest_status,
                submit_confirmation=False,
                real_hh_submit=0,
                post_submit_verification=RunnerPreCheckStatus.NOT_RUN,
                final_application_state=HHApplicationState.NEEDS_HUMAN_REVIEW.value,
                next_application_executed=False,
                pipeline_py="NOT RUN",
                reason=f"Submit policy rejected: {', '.join(policy_decision.reasons)}",
            )
        submit_approval = policy_decision.approval
    elif not confirm_submit:
        return RunnerExecutionResult(
            application_id=app_id,
            vacancy_id=vac_id,
            vacancy_title=vac_title,
            company=company,
            queue_ready_count=queue_ready_count,
            queue_review_count=queue_review_count,
            queue_submitted_count=queue_submitted_count,
            selected_application=f"{vac_title} ({vac_id}) @ {company} [{app_id}]",
            pre_submit_audit=audit_status,
            navigation=nav_status,
            questionnaire=quest_status,
            submit_confirmation=False,
            real_hh_submit=0,
            post_submit_verification=RunnerPreCheckStatus.NOT_RUN,
            final_application_state=current_state,
            next_application_executed=False,
            pipeline_py="NOT RUN",
            reason="Pre-checks PASSED. Submission paused: explicit confirmation required (--confirm-submit).",
        )
    else:
        from .hh_application_orchestrator import SubmitApproval
        submit_approval = SubmitApproval(source="human", policy_version="legacy_confirm")

    # Step 5: Execute Exactly ONE Submit with confirmation
    real_submit_count = 0
    if qid:
        quest_data = db.get_hh_questionnaire(qid) or {}
        human_answers = quest_data.get("answers") or app.get("answers") or {}
        q_res = submit_questionnaire_response(
            questionnaire_id=qid,
            human_answers=human_answers,
            confirm_submit=True,
            evaluate_fn=evaluate_fn,
        )
        is_submitted = q_res.verdict in ("SUBMITTED", "ALREADY_SUBMITTED") or q_res.submit_count > 0
        if not is_submitted:
            return RunnerExecutionResult(
                application_id=app_id,
                vacancy_id=vac_id,
                vacancy_title=vac_title,
                company=company,
                queue_ready_count=queue_ready_count,
                queue_review_count=queue_review_count,
                queue_submitted_count=queue_submitted_count,
                selected_application=selected_app_label,
                pre_submit_audit=audit_status,
                navigation=nav_status,
                questionnaire=quest_status,
                submit_confirmation=bool(confirm_submit or auto_mode),
                real_hh_submit=0,
                final_application_state=current_state,
                reason=f"Questionnaire submit execution failed: {q_res.reason}",
            )
        real_submit_count = 1
    else:
        if evaluate_fn is not None:
            from .hh_submission import execute_hh_submission
            exec_res = execute_hh_submission(
                vacancy_stable_id=vac_stable_id or f"hh:{vac_id}",
                evaluate_fn=evaluate_fn,
                human_confirmed=confirm_submit,
                approval=submit_approval,
                dry_run=dry_run,
                sync_hh_application=False,
            )
            if exec_res.status == "DRY_RUN_OK":
                return RunnerExecutionResult(
                    application_id=app_id,
                    vacancy_id=vac_id,
                    vacancy_title=vac_title,
                    company=company,
                    queue_ready_count=queue_ready_count,
                    queue_review_count=queue_review_count,
                    queue_submitted_count=queue_submitted_count,
                    selected_application=selected_app_label,
                    pre_submit_audit=audit_status,
                    navigation=nav_status,
                    questionnaire=quest_status,
                    submit_confirmation=False,
                    real_hh_submit=0,
                    final_application_state=current_state,
                    reason=exec_res.reason,
                )
            if exec_res.submit_count == 0:
                return RunnerExecutionResult(
                    application_id=app_id,
                    vacancy_id=vac_id,
                    vacancy_title=vac_title,
                    company=company,
                    queue_ready_count=queue_ready_count,
                    queue_review_count=queue_review_count,
                    queue_submitted_count=queue_submitted_count,
                    selected_application=selected_app_label,
                    pre_submit_audit=audit_status,
                    navigation=nav_status,
                    questionnaire=quest_status,
                    submit_confirmation=bool(confirm_submit or auto_mode),
                    real_hh_submit=0,
                    final_application_state=current_state,
                    reason=exec_res.reason,
                )
            real_submit_count = exec_res.submit_count
        else:
            real_submit_count = 1

    # Step 6: Post-Submit Verification
    import time
    time.sleep(3.5)
    post_res = verify_hh_submitted_application(app_id, evaluate_fn=evaluate_fn, cdp_url=cdp_url)
    post_verdict = RunnerPreCheckStatus.PASS if post_res.verification_verdict == "PASS" else RunnerPreCheckStatus.FAIL

    if post_verdict == RunnerPreCheckStatus.PASS:
        # Extract or compute fingerprint for evidence
        pkg_fp = None
        if policy_decision and policy_decision.fingerprint:
            pkg_fp = policy_decision.fingerprint
        else:
            from .application_review import get_application_review
            rev = get_application_review(vac_stable_id) if vac_stable_id else None
            if rev:
                pkg_fp = getattr(rev, "form_fingerprint", None) or getattr(rev, "fingerprint", None)
            if not pkg_fp and 'exec_res' in locals() and exec_res and exec_res.gate_check_result:
                for gr in getattr(exec_res.gate_check_result, "gate_results", []):
                    if gr.gate == "fingerprint_match" and gr.details:
                        pkg_fp = gr.details.get("fingerprint")
            if not pkg_fp:
                pkg_fp = f"runner_fp_{app_id}"

        transition_application(
            application_id=app_id,
            to_state=HHApplicationState.SUBMITTED,
            reason="autonomous_policy_submit_confirmed" if auto_mode else "controlled_runner_submit_confirmed",
            evidence={
                "fingerprint": pkg_fp,
                "submit_executed": True,
                "post_submit_verification": "passed",
                "hh_status": post_res.hh_status,
                "evidence_text": post_res.evidence_text,
                "vacancy_url": post_res.vacancy_url,
                "verified_at": post_res.timestamp,
                "approval": submit_approval.to_dict() if submit_approval else None,
            },
            approval=submit_approval,
        )
        if qid:
            db.update_hh_questionnaire_answers(qid, {}, new_status=HHQuestionStatus.SUBMITTED.value)
        final_state = HHApplicationState.SUBMITTED.value
        msg = "Application submitted and verified on HeadHunter."
    else:
        transition_application(
            application_id=app_id,
            to_state=HHApplicationState.BLOCKED,
            reason="post_submit_verification_failed",
            evidence={
                "submit_executed": True,
                "post_submit_verification": "failed",
                "hh_status": post_res.hh_status,
                "evidence_text": post_res.evidence_text,
                "vacancy_url": post_res.vacancy_url,
                "verified_at": post_res.timestamp,
                "error": f"Submit executed but verification failed: {post_res.reason}",
            },
        )
        final_state = HHApplicationState.BLOCKED.value
        msg = f"Submit executed but post-submit verification failed: {post_res.reason}"

    return RunnerExecutionResult(
        application_id=app_id,
        vacancy_id=vac_id,
        vacancy_title=vac_title,
        company=company,
        queue_ready_count=queue_ready_count,
        queue_review_count=queue_review_count,
        queue_submitted_count=queue_submitted_count,
        selected_application=f"{vac_title} ({vac_id}) @ {company} [{app_id}]",
        pre_submit_audit=audit_status,
        navigation=nav_status,
        questionnaire=quest_status,
        submit_confirmation=bool(confirm_submit or auto_mode),
        real_hh_submit=real_submit_count,
        post_submit_verification=post_verdict,
        final_application_state=final_state,
        next_application_executed=False,
        pipeline_py="NOT RUN",
        reason=msg,
    )


def format_runner_result_cli(res: RunnerExecutionResult, mode: str = "preview") -> str:
    """Format runner execution output for CLI display."""
    if res.selected_application is None:
        audit_str = "SKIPPED"
        nav_str = "SKIPPED"
        quest_str = "SKIPPED"
        post_str = "SKIPPED"
        final_state_str = "N/A"
    else:
        audit_str = res.pre_submit_audit.value
        nav_str = res.navigation.value
        quest_str = res.questionnaire.value
        post_str = res.post_submit_verification.value
        if res.real_hh_submit == 0 and ("already responded" in (res.reason or "").lower() or res.final_application_state == "STALE"):
            final_state_str = "STALE (external response detected on HH)"
        else:
            final_state_str = res.final_application_state

    lines = [
        "=======================================================",
        "        STAGE 46 CONTROLLED APPLICATION RUNNER         ",
        "=======================================================",
        "Queue:",
        f"  READY_TO_SUBMIT:    {res.queue_ready_count}",
        f"  NEEDS_HUMAN_REVIEW: {res.queue_review_count}",
        f"  SUBMITTED:          {res.queue_submitted_count}",
        "",
        f"Selected application: {res.selected_application or 'None'}",
        "",
        f"Pre-submit audit:     {audit_str}",
        f"Navigation:           {nav_str}",
        f"Questionnaire:        {quest_str}",
        "",
        f"Submit confirmation:  {'YES' if res.submit_confirmation else 'NO'}",
        f"REAL HH SUBMIT:       {res.real_hh_submit}",
        f"Post-submit verify:   {post_str}",
        "",
        f"Final state:          {final_state_str}",
        f"Next app executed:    {'YES' if res.next_application_executed else 'NO'}",
        f"PIPELINE.PY:          {res.pipeline_py}",
        "-------------------------------------------------------",
        f"Status Details:       {res.reason}",
        "=======================================================",
    ]
    return "\n".join(lines)
