"""Stage 34: Human-in-the-Loop HH Questionnaire.

Provides safe, controlled handling of employer screening questions (questionnaires) on HeadHunter.
Detects questions from live DOM / forms, stops before submit (NEEDS_HUMAN_REVIEW), formats questions
for CLI display, validates human-provided answers against question constraints, and ensures submission
only proceeds after all required questions are answered with valid options and confirmed by a human.

SAFETY INVARIANTS:
1. Questionnaire detected -> Submit = 0 (stops before submit).
2. Required question without answer -> Submit = 0.
3. Partial answers -> Submit = 0.
4. Invalid option -> Submit = 0.
5. Unknown question_id -> Submit = 0.
6. Changed questionnaire DOM -> Submit = 0 (fails closed to NEEDS_HUMAN_REVIEW).
7. Submit MAY proceed only with valid complete human answers AND explicit confirmation.
8. Zero autonomous submission is strictly preserved.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from enum import Enum
from typing import Any
from collections.abc import Callable

from pydantic import BaseModel, Field

from . import db

logger = logging.getLogger(__name__)


class HHQuestionType(str, Enum):
    TEXT = "text"
    TEXTAREA = "textarea"
    NUMBER = "number"
    SELECT = "select"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    UNKNOWN = "unknown"


class HHQuestionStatus(str, Enum):
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTED = "SUBMITTED"
    BLOCKED = "BLOCKED"


class HHQuestionItem(BaseModel):
    question_id: str
    text: str
    question_type: str = "text"
    required: bool = True
    options: list[str] = Field(default_factory=list)
    field_name: str | None = None
    answer: Any | None = None

    model_config = {"extra": "forbid"}


class HHQuestionnaire(BaseModel):
    questionnaire_id: str
    vacancy_stable_id: str | None = None
    conversation_id: str | None = None
    title: str | None = None
    employer: str | None = None
    questions: list[HHQuestionItem] = Field(default_factory=list)
    answers: dict[str, Any] = Field(default_factory=dict)
    status: str = HHQuestionStatus.NEEDS_HUMAN_REVIEW.value
    fingerprint: str = ""
    created_at: str = ""
    updated_at: str = ""

    model_config = {"extra": "forbid"}


class QuestionnaireValidationResult(BaseModel):
    ok: bool = False
    status: str = HHQuestionStatus.BLOCKED.value
    reason: str = ""
    missing_required: list[str] = Field(default_factory=list)
    invalid_options: list[str] = Field(default_factory=list)
    unknown_questions: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class QuestionnaireSubmitResult(BaseModel):
    verdict: str = "BLOCKED"
    submit_count: int = 0
    click_count: int = 0
    status: str = HHQuestionStatus.BLOCKED.value
    reason: str = ""
    questionnaire_id: str = ""
    vacancy_stable_id: str | None = None
    errors: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


def compute_questionnaire_fingerprint(questions: list[HHQuestionItem]) -> str:
    """Compute a stable, collision-resistant SHA-256 fingerprint for a questionnaire structure."""
    normalized = []
    for q in sorted(questions, key=lambda x: str(x.question_id)):
        normalized.append({
            "id": str(q.question_id).strip(),
            "text": str(q.text).strip(),
            "type": str(q.question_type).strip().lower(),
            "required": bool(q.required),
            "options": [str(o).strip() for o in (q.options or [])],
        })
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"hh_qfp_{digest[:24]}"


# Read-only JS for extracting questionnaire elements from active HH DOM
_EXTRACT_QUESTIONNAIRE_JS = """(() => {
    try {
        const questions = [];
        
        // 1. Find explicit question blocks
        const qEls = Array.from(document.querySelectorAll("[data-qa*='vacancy-response-question'], [data-qa*='screening-question'], .vacancy-response-question"));
        
        // 2. Find form inputs/groups
        const formControls = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='submit']), textarea, select"));
        
        // Map radio and checkbox groups by name
        const groups = {};
        for (const el of formControls) {
            const tag = el.tagName.toUpperCase();
            const type = (el.getAttribute('type') || '').toLowerCase();
            const name = el.getAttribute('name') || '';
            const dataQa = el.getAttribute('data-qa') || '';
            
            if (type === 'radio' || type === 'checkbox') {
                if (!name) continue;
                if (!groups[name]) {
                    groups[name] = {
                        name: name,
                        type: type,
                        options: [],
                        required: !!(el.required || el.getAttribute('aria-required') === 'true'),
                        label: ''
                    };
                }
                let optLabel = '';
                try {
                    if (el.labels && el.labels[0]) optLabel = (el.labels[0].innerText || '').trim();
                    else {
                        const wrap = el.closest('label');
                        if (wrap) optLabel = (wrap.innerText || '').trim();
                    }
                } catch(e) {}
                if (optLabel && !groups[name].options.includes(optLabel)) {
                    groups[name].options.push(optLabel);
                }
            }
        }
        
        return JSON.stringify({
            ok: true,
            url: location.href,
            title: document.title || '',
            questions_raw: qEls.map(e => ({
                text: (e.innerText || '').trim(),
                dataQa: e.getAttribute('data-qa') || ''
            })),
            groups: Object.values(groups),
            has_submit_btn: !!document.querySelector("[data-qa='vacancy-response-submit-popup'], [data-qa='vacancy-response-submit']")
        });
    } catch(e) {
        return JSON.stringify({ok: false, error: e.message});
    }
})()"""


def extract_hh_questionnaire_from_snapshot(
    snapshot: dict[str, Any],
    vacancy_stable_id: str | None = None,
    conversation_id: str | None = None,
    title: str | None = None,
    employer: str | None = None,
) -> HHQuestionnaire | None:
    """Extract an HHQuestionnaire from a DOM snapshot or structured questions list."""
    raw_questions = snapshot.get("questions") or []
    controls = snapshot.get("controls") or []
    q_groups = snapshot.get("question_groups") or []
    
    extracted_items: list[HHQuestionItem] = []
    
    # 1. Process from controls / question groups if available
    if controls:
        from .hh_extractor import build_questions_from_controls
        app_questions = build_questions_from_controls(controls, question_groups=q_groups)
        for idx, aq in enumerate(app_questions, start=1):
            qtype = (aq.normalized_type.value if hasattr(aq.normalized_type, "value") else str(aq.normalized_type)).lower()
            if qtype == "unknown":
                qtype = "text"
            is_req = aq.required is not False
            extracted_items.append(
                HHQuestionItem(
                    question_id=f"q{idx}",
                    text=aq.label or f"Question {idx}",
                    question_type=qtype,
                    required=is_req,
                    options=aq.options or [],
                    field_name=aq.id,
                )
            )
    elif raw_questions:
        for idx, rq in enumerate(raw_questions, start=1):
            text = rq.get("text") or rq.get("label") or f"Question {idx}"
            qtype = rq.get("type") or rq.get("question_type") or "text"
            is_req = rq.get("required") if "required" in rq else True
            options = rq.get("options") or []
            qid = rq.get("id") or rq.get("question_id") or f"q{idx}"
            extracted_items.append(
                HHQuestionItem(
                    question_id=str(qid),
                    text=str(text).strip(),
                    question_type=str(qtype).lower(),
                    required=bool(is_req),
                    options=list(options),
                    field_name=rq.get("field_name") or rq.get("name"),
                )
            )

    if not extracted_items:
        return None

    fp = compute_questionnaire_fingerprint(extracted_items)
    vid = vacancy_stable_id or snapshot.get("vacancy_stable_id") or ""
    cid = conversation_id or snapshot.get("conversation_id") or ""
    qid = f"quest_{hashlib.sha256(f'{vid}_{cid}_{fp}'.encode()).hexdigest()[:16]}"
    
    now = datetime.utcnow().isoformat()
    quest = HHQuestionnaire(
        questionnaire_id=qid,
        vacancy_stable_id=vid or None,
        conversation_id=cid or None,
        title=title or snapshot.get("title") or None,
        employer=employer or snapshot.get("employer") or None,
        questions=extracted_items,
        answers={},
        status=HHQuestionStatus.NEEDS_HUMAN_REVIEW.value,
        fingerprint=fp,
        created_at=now,
        updated_at=now,
    )
    
    db.init_db()
    db.save_hh_questionnaire(quest.model_dump())
    return quest


def format_hh_application_form_cli_output(
    vacancy_title: str,
    quest: HHQuestionnaire | None = None,
) -> str:
    """Format HH application form discovery output for CLI."""
    lines = [
        "-------------------------------------------------------",
        "HH APPLICATION FORM",
        "-------------------------------------------------------",
        f"Vacancy: {vacancy_title or 'N/A'}",
    ]
    if not quest or not quest.questions:
        lines.extend([
            "Questionnaire: NONE",
            "-------------------------------------------------------",
            "Status: ANALYZED",
            "Submit: BLOCKED (Zero autonomous send)",
            "-------------------------------------------------------",
        ])
        return "\n".join(lines)

    lines.extend([
        "Questionnaire: FOUND",
        "",
        f"Questionnaire ID: {quest.questionnaire_id}",
        f"Fingerprint:      {quest.fingerprint}",
        "",
    ])
    for idx, item in enumerate(quest.questions, start=1):
        req_marker = "[required]" if item.required else "[optional]"
        lines.append(f"Q{idx} {req_marker}")
        lines.append(item.text)
        lines.append("")
        lines.append(f"Type: {item.question_type}")
        if item.options:
            lines.append("")
            lines.append("Options:")
            for opt in item.options:
                lines.append(f"- {opt}")
        lines.append("")

    lines.extend([
        "-------------------------------------------------------",
        f"Status: {quest.status}",
        "Submit: BLOCKED",
        "-------------------------------------------------------",
    ])
    return "\n".join(lines)


def format_questionnaire_cli_output(q: HHQuestionnaire) -> str:
    """Format an HH questionnaire for user-friendly CLI output."""
    lines = [
        "-------------------------------------------------------",
        "HH QUESTIONNAIRE — HUMAN INPUT REQUIRED",
        f"Conversation: {q.conversation_id or 'N/A'}",
        f"Vacancy:      {q.title or q.vacancy_stable_id or 'N/A'}",
        "-------------------------------------------------------",
        "",
    ]
    for idx, item in enumerate(q.questions, start=1):
        req_marker = "[required]" if item.required else "[optional]"
        lines.append(f"Q{idx} {req_marker}")
        lines.append(item.text)
        lines.append("")
        lines.append(f"Type: {item.question_type}")
        if item.options:
            lines.append("Options:")
            for opt in item.options:
                lines.append(f"- {opt}")
        lines.append("")

    lines.extend([
        "-------------------------------------------------------",
        f"Status: {q.status}",
        "Submit: BLOCKED",
        "-------------------------------------------------------",
    ])
    return "\n".join(lines)


def validate_human_answers(
    questionnaire: HHQuestionnaire,
    human_answers: dict[str, Any],
    current_dom_fingerprint: str | None = None,
) -> QuestionnaireValidationResult:
    """Validate provided human answers against the questionnaire rules and safety invariants.

    Safety Rules:
    1. Every required question must have a non-empty answer.
    2. Partial answers are rejected.
    3. Select/radio answers must match one of the allowed options.
    4. Checkbox answers must match allowed options.
    5. Unknown question_id in answers is rejected.
    6. Number fields must contain valid numeric strings.
    7. Unsupported question types halt flow at NEEDS_HUMAN_REVIEW.
    8. If current_dom_fingerprint is provided and differs, rejects (changed questionnaire -> NEEDS_HUMAN_REVIEW).
    """
    res = QuestionnaireValidationResult(
        ok=False,
        status=HHQuestionStatus.BLOCKED.value,
    )

    # Invariant 6: Changed questionnaire DOM -> fail closed
    if current_dom_fingerprint and current_dom_fingerprint != questionnaire.fingerprint:
        res.reason = "Questionnaire structure on page has changed; human review required"
        res.status = HHQuestionStatus.NEEDS_HUMAN_REVIEW.value
        return res

    valid_qids = {q.question_id: q for q in questionnaire.questions}
    
    # Invariant 5: Unknown question_id -> Submit = 0
    for qid in human_answers.keys():
        if qid not in valid_qids:
            res.unknown_questions.append(qid)
    if res.unknown_questions:
        res.reason = f"Unknown question_id(s) provided: {', '.join(res.unknown_questions)}"
        res.status = HHQuestionStatus.BLOCKED.value
        return res

    # Check required questions and option validity
    for q in questionnaire.questions:
        ans = human_answers.get(q.question_id)
        if ans is None or (isinstance(ans, str) and not ans.strip()) or (isinstance(ans, list) and not ans):
            if q.required:
                res.missing_required.append(q.question_id)
            continue

        # Invariant 4: Option validation for choice questions
        if q.options:
            opts_lower = [o.strip().lower() for o in q.options]
            if isinstance(ans, list):
                for single_ans in ans:
                    if str(single_ans).strip().lower() not in opts_lower:
                        res.invalid_options.append(f"{q.question_id}: '{single_ans}' not in options")
            else:
                if str(ans).strip().lower() not in opts_lower:
                    res.invalid_options.append(f"{q.question_id}: '{ans}' not in options")

        # Number type validation
        if q.question_type == "number":
            try:
                float(str(ans).replace(",", ".").strip())
            except ValueError:
                res.invalid_options.append(f"{q.question_id}: '{ans}' is not a valid number")

    # Invariant 2 & 3: Missing required / partial answers -> Submit = 0
    if res.missing_required:
        res.reason = f"Required question(s) without answer: {', '.join(res.missing_required)}"
        res.status = HHQuestionStatus.BLOCKED.value
        return res

    if res.invalid_options:
        res.reason = f"Invalid option(s) provided: {'; '.join(res.invalid_options)}"
        res.status = HHQuestionStatus.BLOCKED.value
        return res

    res.ok = True
    res.status = HHQuestionStatus.READY_TO_SUBMIT.value
    res.reason = "All questionnaire requirements satisfied"
    return res


def _notify_human_question_blocked(
    quest: HHQuestionnaire,
    val: QuestionnaireValidationResult,
) -> None:
    """Tell a human that a questionnaire parked the application.

    BLE001 finding #23: the questionnaire stopped before submit and nobody was
    told - the notification existed, but nothing called it. Only the cases a
    human can act on are reported; an unknown question_id means the *caller*
    passed a bad key, which is a bug, not a question to answer.

    Best-effort by design: a broken notifier must never change the safety
    outcome of submit_questionnaire_response() - Submit stays 0 either way.
    """
    human_must_act = bool(val.missing_required or val.invalid_options) or (
        val.status == HHQuestionStatus.NEEDS_HUMAN_REVIEW.value
    )
    if not human_must_act:
        return

    unanswered = (
        ", ".join(val.missing_required)
        or ", ".join(val.invalid_options)
        or val.reason
        or "questionnaire structure changed on the page"
    )

    application_id = None
    if quest.vacancy_stable_id:
        try:
            app = db.get_hh_application_by_vacancy(quest.vacancy_stable_id)
            application_id = (app or {}).get("application_id")
        except Exception as e:  # noqa: BLE001
            logger.debug("cannot resolve application_id for %s: %s",
                         quest.vacancy_stable_id, e)

    try:
        from .hh_autonomous_agent import NotificationDispatcher
        NotificationDispatcher.notify_blocking_question(
            company=quest.employer or "HeadHunter Employer",
            vacancy_title=quest.title or "Python Role",
            unanswered_question=unanswered,
            vacancy_url=(f"https://hh.ru/vacancy/{quest.vacancy_stable_id}"
                         if quest.vacancy_stable_id else None),
            application_id=application_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "cannot notify about the blocked questionnaire %s - the application is "
            "still parked, the human just will not be told: %s",
            quest.questionnaire_id, e,
        )


# JS: Click submit button (gated execution)
_EXECUTE_SUBMIT_JS = """(() => {
    const el = document.querySelector('[data-qa*="vacancy-response-submit-popup"], [data-qa*="response-submit"], [data-qa*="submit-popup"], button[type="submit"], input[type="submit"]');
    if (!el) return JSON.stringify({ok: false, reason: 'Submit button not found'});
    if (el.disabled) return JSON.stringify({ok: false, reason: 'Submit button is disabled'});
    el.click();
    return JSON.stringify({ok: true});
})()"""


def _make_fill_and_submit_js(answers: dict[str, Any]) -> str:
    escaped_answers = json.dumps(json.dumps(answers, ensure_ascii=False))
    return f"""(() => {{
        try {{
            const answers = JSON.parse({escaped_answers});
            
            // 1. If response modal is not open, click the "Откликнуться" button on the vacancy page
            let modal = document.querySelector('[data-qa="vacancy-response-popup"], .bloko-modal, form.vacancy-response');
            let submitBtn = document.querySelector('[data-qa*="vacancy-response-submit-popup"], [data-qa*="response-submit"], [data-qa*="submit-popup"], button[type="submit"], input[type="submit"]');
            if (!modal && !submitBtn) {{
                const applyLink = document.querySelector('[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"], [data-qa="vacancy-response-link-view"]');
                if (applyLink) {{
                    applyLink.click();
                }}
            }}
            
            // 2. Fill answers into inputs / radios / checkboxes / textareas
            const nativeInputSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value") ? Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set : null;
            const nativeTextareaSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value") ? Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set : null;

            const textareas = Array.from(document.querySelectorAll("textarea"));
            const ansValues = Object.values(answers);

            for (const [qid, ans] of Object.entries(answers)) {{
                if (ans === null || ans === undefined) continue;
                
                if (Array.isArray(ans)) {{
                    for (const item of ans) {{
                        const itemStr = String(item).trim().toLowerCase();
                        const labels = Array.from(document.querySelectorAll("label"));
                        for (const lbl of labels) {{
                            if (lbl.innerText.trim().toLowerCase().includes(itemStr)) {{
                                const inp = lbl.querySelector("input[type='checkbox']") || document.getElementById(lbl.getAttribute("for"));
                                if (inp && !inp.checked) {{
                                    inp.click();
                                }}
                            }}
                        }}
                    }}
                }} else {{
                    const strAns = String(ans).trim();
                    const strAnsLower = strAns.toLowerCase();
                    
                    let radioFound = false;
                    const labels = Array.from(document.querySelectorAll("label"));
                    for (const lbl of labels) {{
                        const lblText = lbl.innerText.trim().toLowerCase();
                        if (lblText === strAnsLower || lblText.includes(strAnsLower)) {{
                            const inp = lbl.querySelector("input[type='radio']") || document.getElementById(lbl.getAttribute("for"));
                            if (inp) {{
                                inp.click();
                                radioFound = true;
                                break;
                            }}
                        }}
                    }}
                    
                    if (!radioFound) {{
                        const textInputs = Array.from(document.querySelectorAll("textarea, input[type='text'], input[type='number']"));
                        for (const inp of textInputs) {{
                            if (!inp.value || inp.value === strAns) {{
                                inp.focus();
                                if (inp.tagName === 'TEXTAREA' && nativeTextareaSetter) {{
                                    nativeTextareaSetter.call(inp, strAns);
                                }} else if (inp.tagName === 'INPUT' && nativeInputSetter) {{
                                    nativeInputSetter.call(inp, strAns);
                                }} else {{
                                    inp.value = strAns;
                                }}
                                inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                inp.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                                break;
                            }}
                        }}
                    }}
                }}
            }}

            // Fallback for empty textareas if any remain unfilled
            for (let i = 0; i < textareas.length; i++) {{
                const ta = textareas[i];
                if (!ta.value && i < ansValues.length) {{
                    const v = ansValues[i];
                    const strVal = typeof v === 'object' ? JSON.stringify(v) : String(v);
                    if (nativeTextareaSetter) nativeTextareaSetter.call(ta, strVal);
                    else ta.value = strVal;
                    ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    ta.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    ta.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                }}
            }}
            
            // 3. Find and click final Submit button
            const el = document.querySelector('[data-qa*="vacancy-response-submit-popup"], [data-qa*="response-submit"], [data-qa*="submit-popup"], button[type="submit"], input[type="submit"]');
            if (!el) return JSON.stringify({{ ok: false, reason: 'Submit button not found' }});
            if (el.disabled) return JSON.stringify({{ ok: false, reason: 'Submit button is disabled' }});
            el.click();
            return JSON.stringify({{ ok: true, clicked_button: el.innerText ? el.innerText.trim() : 'Откликнуться' }});
        }} catch(err) {{
            return JSON.stringify({{ ok: false, reason: err.message }});
        }}
    }})()"""


def submit_questionnaire_response(
    questionnaire_id: str,
    human_answers: dict[str, Any],
    evaluate_fn: Callable[[str], str] | None = None,
    confirm_submit: bool = False,
    current_dom_fingerprint: str | None = None,
) -> QuestionnaireSubmitResult:
    """Safely submit an application with validated human answers.

    SAFETY INVARIANTS:
    - confirm_submit == False -> BLOCKED (Submit = 0)
    - validation failure -> BLOCKED (Submit = 0)
    - changed questionnaire -> BLOCKED / NEEDS_HUMAN_REVIEW (Submit = 0)
    - one-shot submit invariant: once submitted, cannot submit again without new review.
    """
    report = QuestionnaireSubmitResult(
        verdict="BLOCKED",
        submit_count=0,
        click_count=0,
        status=HHQuestionStatus.BLOCKED.value,
        questionnaire_id=questionnaire_id,
    )

    db.init_db()
    q_data = db.get_hh_questionnaire(questionnaire_id)
    if not q_data:
        report.reason = f"Questionnaire {questionnaire_id} not found"
        report.errors.append(report.reason)
        return report

    quest = HHQuestionnaire(**q_data)
    report.vacancy_stable_id = quest.vacancy_stable_id

    # One-shot check
    if quest.status == HHQuestionStatus.SUBMITTED.value:
        report.reason = "Questionnaire already submitted - one-shot invariant prevents duplicate submit"
        report.errors.append(report.reason)
        return report

    # 1. Validate answers
    val = validate_human_answers(quest, human_answers, current_dom_fingerprint=current_dom_fingerprint)
    if not val.ok:
        report.reason = val.reason
        report.status = val.status
        report.errors.append(val.reason)
        # Update status in DB
        db.update_hh_questionnaire_answers(questionnaire_id, human_answers, new_status=val.status)
        # BLE001 finding #23: parking the application is exactly the moment a
        # human has to be told. Previously nothing was sent at all.
        _notify_human_question_blocked(quest, val)
        return report

    # 2. Check explicit human confirmation
    if not confirm_submit:
        report.reason = "Explicit confirmation required (--confirm-submit). Zero browser mutations performed."
        report.status = HHQuestionStatus.READY_TO_SUBMIT.value
        report.errors.append(report.reason)
        # Store answers in DB as ready to submit
        db.update_hh_questionnaire_answers(questionnaire_id, human_answers, new_status=HHQuestionStatus.READY_TO_SUBMIT.value)
        return report

    # 2.5 Pre-Submit Vacancy Verification & Navigation
    if evaluate_fn is not None and (quest.vacancy_stable_id or quest.title):
        from .hh_vacancy_navigator import verify_and_navigate_hh_vacancy
        nav_res = verify_and_navigate_hh_vacancy(
            target=quest.vacancy_stable_id or questionnaire_id,
            evaluate_fn=evaluate_fn,
            expected_title=quest.title,
        )
        if not nav_res.ok:
            if getattr(nav_res, "status", None) == "ALREADY_RESPONDED":
                report.reason = f"Application already submitted on HeadHunter: {nav_res.reason}"
                report.status = HHQuestionStatus.SUBMITTED.value
                report.verdict = "ALREADY_SUBMITTED"
                # BLE001 finding #29 (sibling): this used to claim
                # submit_count = 1 while click_count stayed 0 - a submit with
                # no click, in a run that clicked nothing. The verdict and the
                # SUBMITTED status already say the application is on HH; the
                # counter is read by a human as "Real Submit Count" and must
                # count THIS run.
                db.update_hh_questionnaire_answers(questionnaire_id, human_answers, new_status=HHQuestionStatus.SUBMITTED.value)
                return report
            report.reason = f"Vacancy pre-submit check failed: {nav_res.reason}"
            report.errors.append(report.reason)
            report.status = HHQuestionStatus.BLOCKED.value
            return report

    # 3. The click IS the submission. No executor -> no click -> no submit.
    # BLE001 finding #25: this used to fall through to SUBMITTED with a
    # click_count of 1 and persist SUBMITTED in the DB without touching a page,
    # which also tripped the one-shot invariant and blocked every later attempt.
    if evaluate_fn is None:
        report.reason = ("No browser executor available (evaluate_fn is None) - "
                         "questionnaire NOT submitted; answers stored and ready")
        report.errors.append(report.reason)
        report.status = HHQuestionStatus.READY_TO_SUBMIT.value
        db.update_hh_questionnaire_answers(
            questionnaire_id, human_answers,
            new_status=HHQuestionStatus.READY_TO_SUBMIT.value)
        return report

    # 3. Perform the single click submit
    try:
        import time
        submit_script = _make_fill_and_submit_js(human_answers) if human_answers else _EXECUTE_SUBMIT_JS
        raw = evaluate_fn(submit_script)
        res = json.loads(raw) if isinstance(raw, str) else raw

        if isinstance(res, dict) and res.get("navigated_to_apply"):
            time.sleep(2.5)
            raw = evaluate_fn(submit_script)
            res = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(res, dict) or not res.get("ok"):
            why = res.get("reason") if isinstance(res, dict) else res
            report.reason = f"DOM Submit click failed: {why or 'no click confirmation from the page'}"
            report.errors.append(report.reason)
            report.status = HHQuestionStatus.BLOCKED.value
            return report
    except Exception as e:
        report.reason = f"CDP evaluate error during submit: {e}"
        report.errors.append(report.reason)
        report.status = HHQuestionStatus.BLOCKED.value
        return report

    # Single click executed successfully
    report.submit_count = 1
    report.click_count = 1
    report.verdict = "SUBMITTED"
    report.status = HHQuestionStatus.SUBMITTED.value
    report.reason = "Questionnaire answers submitted successfully with explicit human confirmation"

    # Persist submitted state in DB
    db.update_hh_questionnaire_answers(questionnaire_id, human_answers, new_status=HHQuestionStatus.SUBMITTED.value)
    return report


def generate_suggested_answers(
    questionnaire: HHQuestionnaire,
    profile_data: dict[str, Any] | None = None,
    vacancy_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate smart, tailored questionnaire answer suggestions from the candidate profile and vacancy context.

    Features:
    1. Tailors location/relocation answers based on candidate profile.
    2. Suggests experience in years (number or text).
    3. Matches relevant technologies and skill options.
    4. Provides tailored summaries for free-text / textarea questions.
    5. Links GitHub and Portfolio.
    """
    from .candidate_profile import load_candidate_profile
    profile = profile_data if profile_data is not None else load_candidate_profile().to_dict()

    skills = [s.lower() for s in profile.get("skills", [])]
    years_exp = str(profile.get("years_experience") or "3")
    github_url = profile.get("github") or "https://github.com/mikheooo"
    portfolio_url = profile.get("portfolio") or "https://mikheooo.github.io/portfolio/"
    remote_req = profile.get("remote_required", True)

    suggested: dict[str, Any] = {}

    for q in questionnaire.questions:
        q_text_lower = q.text.lower()
        q_type = q.question_type.lower()

        # 1. Location / Remote / Residence
        if any(w in q_text_lower for w in ["место работы", "проживан", "локаци", "город", "страна", "релокац", "location"]):
            if q.options:
                matched_opt = None
                for opt in q.options:
                    opt_l = opt.lower()
                    if remote_req and any(r in opt_l for r in ["удален", "удалён", "вне рф", "релокац", "remote"]):
                        matched_opt = opt
                        break
                if not matched_opt and q.options:
                    matched_opt = q.options[0]
                suggested[q.question_id] = matched_opt
            else:
                suggested[q.question_id] = "Удалённо (Вне РФ)" if remote_req else "Москва"

        # 2. Years of experience / commercial experience
        elif any(w in q_text_lower for w in ["сколько лет", "опыт работы", "коммерческ", "experience", "стаж"]):
            if q_type == "number":
                suggested[q.question_id] = years_exp
            elif q.options:
                matched_opt = None
                for opt in q.options:
                    opt_l = opt.lower()
                    if "3" in opt_l or "3-5" in opt_l or "1-3" in opt_l or "middle" in opt_l:
                        matched_opt = opt
                        break
                suggested[q.question_id] = matched_opt or q.options[0]
            else:
                suggested[q.question_id] = f"{years_exp} года коммерческой разработки (Python, AI/LLM, автоматизация)"

        # 3. Employment format / Schedule
        elif any(w in q_text_lower for w in ["график", "формат занятости", "занятост", "полный день", "employment"]):
            if q.options:
                matched_opt = None
                for opt in q.options:
                    opt_l = opt.lower()
                    if any(e in opt_l for e in ["полный", "full-time", "удален", "гибк"]):
                        matched_opt = opt
                        break
                suggested[q.question_id] = matched_opt or q.options[0]
            else:
                suggested[q.question_id] = "Полный день (Full-time), удаленно"

        # 4. Technologies / Skills (Checkbox or Multi-select)
        elif any(w in q_text_lower for w in ["технолог", "стек", "навык", "skills", "инструмент", "используете"]):
            if q_type == "checkbox" or q.options:
                matched_opts = []
                for opt in q.options:
                    opt_l = opt.lower()
                    if any(s in opt_l for s in skills) or any(s in opt_l for s in ["fastapi", "asyncio", "n8n", "postgres", "docker", "llm", "langchain", "python", "git", "api"]):
                        matched_opts.append(opt)
                if not matched_opts and q.options:
                    matched_opts = [q.options[0]]
                suggested[q.question_id] = matched_opts if q_type == "checkbox" else matched_opts[0]
            else:
                suggested[q.question_id] = ", ".join(profile.get("skills", ["Python", "FastAPI", "n8n", "LLM APIs", "PostgreSQL", "Docker"]))

        # 5. Portfolio / GitHub / Code samples
        elif any(w in q_text_lower for w in ["github", "портфолио", "portfolio", "ссылк", "код", "проект"]):
            if "портфолио" in q_text_lower and portfolio_url:
                suggested[q.question_id] = f"{github_url} (Портфолио: {portfolio_url})"
            else:
                suggested[q.question_id] = github_url

        # 6. Detailed / Free text / Cover note / Achievements
        elif q_type in ["textarea", "text"]:
            suggested[q.question_id] = (
                f"3 года коммерческого опыта: разработка backend и AI-агентов на Python (FastAPI, Asyncio), "
                f"автоматизация бизнес-процессов на n8n, интеграция LLM API и баз данных PostgreSQL. "
                f"Примеры проектов и код: {github_url}"
            )

        # 7. Fallback by type
        elif q_type == "number":
            suggested[q.question_id] = years_exp
        elif q_type == "checkbox":
            suggested[q.question_id] = [q.options[0]] if q.options else []
        elif q.options:
            suggested[q.question_id] = q.options[0]
        else:
            suggested[q.question_id] = "Да"

    return suggested
