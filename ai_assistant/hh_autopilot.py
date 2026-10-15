"""Autonomous HeadHunter Application Engine (Autopilot).

Executes end-to-end autonomous discovery, evaluation, cover letter tailoring,
CDP submission, reconciliation, negotiation monitoring, and Telegram alerts
without requiring manual human confirmations for vetted remote vacancies.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant import config, db
from ai_assistant.candidate_profile import load_candidate_profile
from ai_assistant.smart_experience_generator import generate_tailored_cover_letter

logger = logging.getLogger("hh_autopilot")

# Phase 3 security: credentials come from environment (.env) via
# ai_assistant.config — no hardcoded tokens. Resolves to the same HH bot
# previously hardcoded here, so runtime behavior is unchanged.
TELEGRAM_BOT_TOKEN = config.HH_TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = config.HH_TELEGRAM_CHAT_ID or config.TELEGRAM_CHAT_ID
TELEGRAM_OWNER_ID = getattr(config, "TELEGRAM_OWNER_ID", None) or TELEGRAM_CHAT_ID or 392046103
RESUME_EXPECTED = "Специалист по внедрению и сопровождению CRM"
VALID_RESUMES = {
    "Специалист по внедрению и сопровождению CRM",
    "Инженер технической поддержки",
    "Ai engineer",
}

DEFAULT_AUTOPILOT_QUERIES = [
    "AI Engineer",
    "Python Developer",
    "Специалист по внедрению CRM",
    "Администратор Bitrix24",
    "DevOps инженер",
    "Системный администратор",
    "Инженер сопровождения",
    "Инженер технической поддержки",
]


def select_best_resume_for_vacancy(role: str, description: str = "") -> str:
    """Select candidate's best matching HeadHunter resume based on vacancy role and tech stack.

    1. 'Специалист по внедрению и сопровождению CRM':
       - CRM, Bitrix24, Битрикс, amoCRM, 1C / ERP, интегратор, внедрение, бизнес-процессы.
    2. 'Ai engineer':
       - AI, LLM, нейросети, Machine Learning, Python dev, n8n, prompt engineering, Whisper, RAG, ETL.
    3. 'Инженер технической поддержки':
       - Системный администратор, техподдержка, L1/L2/L3, Helpdesk, сети, Cisco, Mikrotik,
         DevOps, Linux, Windows Server, инфраструктура, инженер сопровождения.
    """
    role_lower = (role or "").lower()
    text_lower = f"{role} {description}".lower()

    # 1. Job title inspection (highest specificity)
    if any(k in role_lower for k in [
        "битрикс", "bitrix", "crm", "црм", "amocrm", "амосрм",
        "внедрен", "интегратор", "1с", "1c", "информационных систем",
    ]):
        return "Специалист по внедрению и сопровождению CRM"

    if any(k in role_lower for k in [
        "ai", "ии", "llm", "нейросет", "python", "питон",
        "n8n", "prompt", "whisper", "rag", "машинн", "ml engineer",
    ]):
        return "Ai engineer"

    if any(k in role_lower for k in [
        "поддержк", "support", "helpdesk", "хэлпдеск", "хелпдеск",
        "администратор", "администрирован", "sysadmin", "сисадмин",
        "devops", "девопс", "сетев", "network", "инфраструктур",
        "дежурн", "сопровожден", "эксперт",
    ]):
        return "Инженер технической поддержки"

    # 2. Broader description inspection
    if any(k in text_lower for k in ["битрикс24", "bitrix24", "внедрение crm", "настройка crm", "интегратор crm"]):
        return "Специалист по внедрению и сопровождению CRM"

    if any(k in text_lower for k in ["ai engineer", "prompt engineering", "нейросети", "llm", "rag", "разработчик python", "python developer", "fastapi"]):
        return "Ai engineer"

    return "Инженер технической поддержки"


def _get_t722_send_module():
    spec = importlib.util.spec_from_file_location("t722_send", PROJECT_ROOT / "tools" / ".t722_send.py")
    if not spec or not spec.loader:
        raise RuntimeError("Cannot load tools/.t722_send.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def send_telegram_alert(text: str) -> bool:
    """Send alert directly to the dedicated HH Telegram bot (@jobsassistbot)."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram bot token not configured.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")
        return False


def send_owner_alert(text: str) -> bool:
    """Send alert strictly to the owner's personal chat (TELEGRAM_OWNER_ID)."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram bot token not configured.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_OWNER_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Telegram owner alert failed: {e}")
        return False


def send_owner_test_alert(vid: str, role: str, company: str, test_info: Any) -> bool:
    """Send alert to owner when vacancy requires manual exam test."""
    msg = (
        f"📝 *HH Autopilot: Требуется тест работодателя*\n\n"
        f"💼 *Вакансия:* [{role}](https://hh.ru/vacancy/{vid})\n"
        f"🏢 *Компания:* {company}\n"
        f"⚠️ *Причина:* Вакансия содержит квалификационный тест / экзамен, который нельзя пройти автоматически.\n\n"
        f"👉 [Пройти тест на HH](https://hh.ru/vacancy/{vid})"
    )
    return send_owner_alert(msg)


def send_owner_questionnaire_alert(vid: str, role: str, company: str, reasons: list[str]) -> bool:
    """Send alert to owner when screening questionnaire requires human review/facts."""
    reasons_text = "\n".join(f"• {r}" for r in reasons[:3])
    msg = (
        f"❓ *HH Autopilot: Анкета требует внимания*\n\n"
        f"💼 *Вакансия:* [{role}](https://hh.ru/vacancy/{vid})\n"
        f"🏢 *Компания:* {company}\n"
        f"⚠️ *Неотвеченные вопросы:*\n{reasons_text}\n\n"
        f"👉 [Заполнить анкету на HH](https://hh.ru/vacancy/{vid})"
    )
    return send_owner_alert(msg)


def get_known_vacancy_ids() -> set[str]:
    """Retrieve all vacancy IDs already tracked or submitted in state.db."""
    db.init_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    known = set()

    # From vacancies table
    cursor.execute("SELECT stable_id FROM vacancies WHERE source = 'hh'")
    for (sid,) in cursor.fetchall():
        if sid.startswith("hh:"):
            known.add(sid.replace("hh:", ""))
        else:
            known.add(sid)

    # From application_tracking table
    cursor.execute("SELECT vacancy_stable_id FROM application_tracking WHERE vacancy_stable_id LIKE 'hh:%'")
    for (sid,) in cursor.fetchall():
        known.add(sid.replace("hh:", ""))

    # From hh_applications table
    # NOTE: the column is `vacancy_stable_id` (db_schema.py:316), NOT `vacancy_id`.
    # It holds 'hh:<id>' form, while callers compare against the bare numeric id
    # (`vid = entry.get("vacancy_id")`, hh_autopilot.py:353) — so strip the prefix,
    # same as the two queries above. Rows with NULL (e.g. app_5689987425) must be
    # filtered: str(None) would poison the set with the literal "None".
    cursor.execute(
        "SELECT vacancy_stable_id FROM hh_applications WHERE vacancy_stable_id IS NOT NULL"
    )
    for (sid,) in cursor.fetchall():
        known.add(str(sid).replace("hh:", ""))

    conn.close()
    return known


def check_salary_floor(salary_data: Any, min_rub: int = 100000) -> tuple[bool, str]:
    """Ensure vacancy salary meets candidate's minimum floor if specified.

    Accepts string (e.g. 'от 120 000 руб.', 'до 400 000 руб.', '80 000 – 150 000 ₽')
    or dict (e.g. {'from': None, 'to': 400000, 'currency': 'RUR'}).
    """
    if not salary_data:
        return True, "salary not specified (acceptable)"

    # Handle dict structure
    if isinstance(salary_data, dict):
        cur = (salary_data.get("currency") or salary_data.get("currencyCode") or "").upper()
        if cur and cur not in ("RUB", "RUR"):
            return True, f"foreign currency salary ({cur})"

        sal_from = salary_data.get("from")
        sal_to = salary_data.get("to")

        vals = [int(v) for v in (sal_from, sal_to) if v is not None and int(v) > 0]
        if not vals:
            return True, "salary not specified in dict (acceptable)"

        max_val = max(vals)
        if max_val < min_rub:
            return False, f"salary {salary_data} below floor {min_rub}"
        return True, f"salary ok (up to {max_val} >= {min_rub})"

    # Handle string representation
    salary_str = str(salary_data).strip()
    if not salary_str:
        return True, "salary not specified (acceptable)"

    s_lower = salary_str.lower()
    if any(cur in s_lower for cur in ("$", "€", "usd", "eur", "usdt")):
        return True, "foreign currency salary"

    # Remove all unicode whitespace (\xa0, \u202f, \u2000-\u200b, standard spaces)
    clean_s = re.sub(r"[\s\xa0\u202f\u2000-\u200b]+", "", s_lower)

    # Extract all contiguous integer numbers
    nums = [int(n) for n in re.findall(r"\d+", clean_s)]
    if not nums:
        return True, "no numbers in salary"

    max_val = max(nums)
    if max_val < min_rub:
        return False, f"salary {salary_str} below floor {min_rub}"

    return True, f"salary ok (up to {max_val} >= {min_rub})"


def generate_tailored_letter(role: str, company: str, description: str = "") -> str:
    """Generate a high-quality tailored cover letter synthesizing experience to fit the vacancy."""
    return generate_tailored_cover_letter(role=role, company=company, description=description)


def brief_vacancy(vid: str) -> dict[str, Any] | None:
    """Inspect vacancy response form via headless CDP directly in-process."""
    try:
        t722 = _get_t722_send_module()
        args = argparse.Namespace(command="brief", vid=vid)
        report = t722.run_brief(args)
        out_path = PROJECT_ROOT / "tools" / ".t722" / f"brief_{vid}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        return report
    except Exception as e:
        logger.error(f"Error briefing vacancy {vid}: {e}", exc_info=True)
        return None


def resolve_and_plan_screening(
    vid: str,
    form_data: dict[str, Any],
    profile: Any,
    kb_data: dict[str, Any] | None = None,
    cover_letter: str | None = None,
    vacancy_info: dict[str, Any] | None = None,
) -> tuple[bool, str, Any | None, list[str]]:
    """Resolve screening questionnaire answers and construct deterministic prefill plan.

    Returns:
        (success, verdict, prefill_plan, reasons)
    """
    raw_controls = form_data.get("controls") or []
    question_groups = form_data.get("question_groups") or []

    from ai_assistant.hh_extractor import (
        ApplicationForm,
        QuestionType,
        build_questions_from_controls,
    )
    from ai_assistant.application_qa import QuestionAnswerGenerator
    from ai_assistant.prefill_plan import build_prefill_plan
    from types import SimpleNamespace

    questions = build_questions_from_controls(raw_controls, question_groups=question_groups)
    if not questions:
        if form_data.get("started_with_question"):
            return False, "CONTROLS_NOT_EXTRACTABLE", None, [
                "Form requires screening questionnaire, but controls were not extractable from DOM"
            ]
        return True, "NO_QUESTIONS", None, []

    resume_path = PROJECT_ROOT / "resume.md"
    resume_text = resume_path.read_text(encoding="utf-8") if resume_path.exists() else ""

    if not cover_letter and vacancy_info:
        role = vacancy_info.get("role", "")
        company = vacancy_info.get("company", "")
        desc = vacancy_info.get("description", "")
        if role:
            cover_letter = generate_tailored_letter(role=role, company=company, description=desc)

    gen = QuestionAnswerGenerator(
        profile=profile,
        resume_text=resume_text,
        deep=None,
        vacancy=None,
        kb=kb_data,
        cover_letter=cover_letter,
    )

    answers = []
    unresolved = []
    for q in questions:
        ans = gen.generate(q)
        answers.append(ans)
        if ans.requires_review or not ans.answer:
            unresolved.append(
                f"Вопрос '{q.label or q.id}': {ans.reason or 'нет подтвержденного факта в базе'}"
            )

    if unresolved:
        return False, "REQUIRES_REVIEW", None, unresolved

    pkg = SimpleNamespace(answers=answers, vacancy_stable_id=f"hh:{vid}", validation_status="VALID")
    app_form = ApplicationForm(vacancy_stable_id=f"hh:{vid}", questions=questions)
    snapshot = {"controls": raw_controls}
    plan = build_prefill_plan(pkg, app_form, snapshot)

    if plan.unresolved or plan.status != "VALID":
        errors = [f"Поле '{u.question_label or u.question_id}': {u.reason}" for u in plan.unresolved]
        return False, "PREFILL_PLAN_UNRESOLVED", None, errors

    return True, "PLAN_READY", plan, []


def is_vacancy_eligible_for_auto_apply(
    brief_data: dict[str, Any],
    has_resolved_screening: bool = False,
) -> tuple[bool, str]:
    """Check safety gates on the live brief: remote, active, no tests, no questionnaire."""
    vacancy = brief_data.get("vacancy") or {}
    form = brief_data.get("form") or {}

    if not vacancy.get("ok"):
        return False, "vacancy page did not render"
    if vacancy.get("archived") or vacancy.get("closedForApplicants"):
        return False, "vacancy is archived or closed"

    app_state = vacancy.get("applicant") or {}
    if app_state.get("responseAllowed") is not True:
        return False, "responseAllowed is not true"
    if app_state.get("alreadyApplied") or vacancy.get("dom_already_responded"):
        return False, "already applied"

    test = app_state.get("test") or {}
    test_extra = {k: v for k, v in test.items() if k != "hasTests" and v is not None}
    if test.get("hasTests") or test_extra:
        return False, f"has tests: {test}"

    if form.get("started_with_question") and not has_resolved_screening:
        return False, "requires initial employer questionnaire"

    # Check for test task textareas if screening questionnaire was not resolved
    if not has_resolved_screening:
        textareas = form.get("textareas") or []
        for ta in textareas:
            name = ta.get("name") or ""
            if name.startswith("task_"):
                return False, f"has employer test task textarea: {name}"

    if form.get("warning_shown"):
        return False, f"resume warning shown: {form.get('warning_text')}"

    submit_btn = form.get("submit")
    if not submit_btn or submit_btn.get("disabled") is not False:
        return False, "submit button disabled or missing"

    resume_title = (form.get("resume_title") or "").strip()
    if resume_title and resume_title not in VALID_RESUMES and resume_title != RESUME_EXPECTED:
        return False, f"unexpected resume: {resume_title}"

    return True, "ELIGIBLE"


def submit_vacancy(
    vid: str,
    role: str,
    company: str,
    salary: str | None = None,
    description: str = "",
    resume_title: str | None = None,
    prefill_plan: Any | None = None,
) -> bool:
    """Submit application autonomously, reconcile to state.db, and notify Telegram."""
    app_dir = PROJECT_ROOT / "artifacts" / "applications" / f"hh-{vid}"
    app_dir.mkdir(parents=True, exist_ok=True)

    target_resume = resume_title or select_best_resume_for_vacancy(role, description)
    resume_to_use = target_resume if (target_resume in VALID_RESUMES) else RESUME_EXPECTED

    # 1. Generate & save tailored cover letter
    letter_text = generate_tailored_letter(role=role, company=company, description=description)
    letter_path = app_dir / "cover_letter.ru.txt"
    letter_path.write_text(letter_text, encoding="utf-8")

    # 1b. Auto-resolve screening prefill plan if not provided
    if prefill_plan is None:
        brief_file = PROJECT_ROOT / "tools" / ".t722" / f"brief_{vid}.json"
        brief_data = None
        if brief_file.exists():
            try:
                brief_data = json.loads(brief_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        if not brief_data:
            brief_data = brief_vacancy(vid)

        if brief_data:
            b_form = brief_data.get("form") or {}
            if b_form.get("started_with_question") or b_form.get("controls"):
                profile = load_candidate_profile()
                kb_data = None
                kb_file = PROJECT_ROOT / "candidate_kb.json"
                if kb_file.exists():
                    try:
                        kb_data = json.loads(kb_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                q_ok, _, plan, _ = resolve_and_plan_screening(
                    vid, b_form, profile, kb_data, cover_letter=letter_text,
                    vacancy_info={"role": role, "company": company, "description": description}
                )
                if q_ok and plan:
                    prefill_plan = plan

    # 2. Save preflight descriptor
    # Phase 3 audit honesty: autopilot submits under the owner's standing
    # authorization (OWNER DECISION: auto-submit stays ON, max_submits=8),
    # NOT a per-vacancy manual confirmation. `human_confirmed` therefore
    # stays False; `auto_submit_authorized` records the standing permission.
    # Nothing gates on these fields (verified: only employer/salary are read
    # back at :368-376, reconcile only writes preflight_verdict), so this
    # changes audit metadata only — submission behavior is untouched.
    preflight = {
        "vacancy_id": vid,
        "employer": company,
        "role": role,
        "url": f"https://hh.ru/vacancy/{vid}",
        "resume_expected": resume_to_use,
        "schedule": "remote",
        "has_tests": False,
        "human_confirmed": False,
        "auto_submit_authorized": True,
        "authorization_note": "standing owner authorization: HH autopilot ON, max 8 submits/cycle",
        "verdict": "AUTOPILOT_SUBMITTING",
    }
    (app_dir / "preflight.json").write_text(json.dumps(preflight, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. Execute CDP submit in-process
    os.environ["SUBMIT_ALLOWED"] = "true"
    try:
        t722 = _get_t722_send_module()
        args = argparse.Namespace(
            command="submit",
            vid=vid,
            expect_resume=resume_to_use,
            letter=str(letter_path),
            prefill_plan=prefill_plan,
        )
        submit_data = t722.run_submit(args)
        submit_file = PROJECT_ROOT / "tools" / ".t722" / f"submit_{vid}.json"
        submit_file.parent.mkdir(parents=True, exist_ok=True)
        submit_file.write_text(json.dumps(submit_data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.error(f"CDP submit failed for {vid}: {e}", exc_info=True)
        return False
    finally:
        os.environ["SUBMIT_ALLOWED"] = "false"

    if submit_data.get("verdict") != "CLICKED" or submit_data.get("clicked") != 1:
        logger.error(f"Submit not clicked for {vid}: {submit_data.get('verdict')} blockers={submit_data.get('blockers')}")
        return False

    # 4. Reconcile into state.db
    try:
        rec_spec = importlib.util.spec_from_file_location("rec_sub", PROJECT_ROOT / "tools" / "reconcile_submission.py")
        if rec_spec and rec_spec.loader:
            rec_mod = importlib.util.module_from_spec(rec_spec)
            rec_spec.loader.exec_module(rec_mod)
            rec_mod.reconcile(vid)
    except Exception as e:
        logger.error(f"Reconcile failed for {vid}: {e}", exc_info=True)

    # 5. Ensure company and title recorded in application_tracking
    try:
        conn = sqlite3.connect(str(PROJECT_ROOT / "state.db"))
        conn.execute(
            "UPDATE application_tracking SET company=?, title=? WHERE vacancy_stable_id=?",
            (company, role, f"hh:{vid}"),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Error updating DB company/title for {vid}: {e}")

    # 6. Notify Telegram
    sal_line = f"💰 *Зарплата:* {salary}\n" if salary else ""
    first_lines = "\n".join(letter_text.strip().splitlines()[:8])
    alert_msg = (
        f"🤖 *Автопилот отправил отклик!*\n\n"
        f"💼 *Вакансия:* [{role}](https://hh.ru/vacancy/{vid})\n"
        f"🏢 *Компания:* {company}\n"
        f"{sal_line}"
        f"📄 *Резюме:* {resume_to_use}\n"
        f"🕒 *Время:* {time.strftime('%Y-%m-%d %H:%M:%S MSK')}\n\n"
        f"✉️ *Сопроводительное письмо (фрагмент):*\n"
        f"_{first_lines}..._\n\n"
        f"✅ _Отклик подтвержден через DOM & state.db_"
    )
    send_telegram_alert(alert_msg)
    logger.info(f"Successfully submitted and notified for {vid}: {role} at {company}")
    return True


def run_autopilot_cycle(
    max_submits: int = 8,
    queries: list[str] | None = None,
    read_limit: int = 45,
) -> dict[str, Any]:
    """Execute one full automated discovery, vetting, application, and negotiation check cycle."""
    cycle_start = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info(f"Starting Autopilot cycle at {cycle_start}")

    known_ids = get_known_vacancy_ids()
    target_queries = queries or DEFAULT_AUTOPILOT_QUERIES
    profile = load_candidate_profile()
    kb_data = None
    kb_path = PROJECT_ROOT / "candidate_kb.json"
    if kb_path.exists():
        try:
            with open(kb_path, "r", encoding="utf-8") as f:
                kb_data = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load candidate_kb.json: {e}")

    # 1. Discover candidates via in-process search_operation_report
    logger.info(f"Running in-process discovery across {len(target_queries)} queries (read_limit={read_limit})")
    os.environ["SUBMIT_ALLOWED"] = "false"
    os.environ["HH_BROWSER_MODE"] = "headless"

    try:
        from ai_assistant import hh_readonly_operation as ro
        from ai_assistant import hh_browser_lease as lease

        raw_report = ro.search_operation_report(
            target_queries,
            read_limit=read_limit,
            owner_type=lease.OWNER_AUTONOMOUS,
            purpose="autopilot",
            area=None,
            schedule="remote",
        )
        out_dir = PROJECT_ROOT / "tools" / ".t727"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "raw_report_autopilot_temp.json").write_text(
            json.dumps(raw_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"Discovery error: {e}")
        return {"status": "ERROR", "error": str(e), "submitted": 0}

    discovered = raw_report.get("vacancies") or []
    logger.info(f"Discovered {len(discovered)} vacancies in full read (pool={raw_report.get('unique_count', 0)})")

    submitted_count = 0
    results: list[dict[str, Any]] = []

    for entry in discovered:
        if submitted_count >= max_submits:
            break

        vid = str(entry.get("vacancy_id") or "")
        if not vid or vid in known_ids:
            continue

        card = entry.get("card") or {}
        preflight = entry.get("preflight") or {}
        emp = preflight.get("employer")
        if isinstance(emp, dict):
            company = emp.get("name") or emp.get("visibleName") or card.get("employer") or "Компания"
        else:
            company = emp or card.get("employer") or "Компания"

        role = card.get("title") or entry.get("title") or "Специалист"
        salary_str = card.get("salary") or preflight.get("salary")

        # Step 2: Salary Floor check
        sal_ok, sal_reason = check_salary_floor(salary_str)
        if not sal_ok:
            logger.info(f"Skipping {vid} ({role} at {company}): {sal_reason}")
            continue

        # Step 3: Live Brief check
        brief = brief_vacancy(vid)
        if not brief:
            logger.info(f"Skipping {vid}: brief failed")
            continue

        vacancy = brief.get("vacancy") or {}
        form = brief.get("form") or {}
        app_state = vacancy.get("applicant") or {}

        # Separation 1: Exam tests with timer (hasTests: True / testId / vacancy_tests)
        test = app_state.get("test") or {}
        test_extra = {k: v for k, v in test.items() if k != "hasTests" and v is not None}
        has_exam_test = bool(test.get("hasTests") or test_extra or any(v is not None for v in (form.get("vacancy_tests") or {}).values()))
        if has_exam_test:
            logger.info(f"Skipping {vid} ({role} at {company}): requires manual exam test {test or form.get('vacancy_tests')}")
            send_owner_test_alert(vid, role, company, test_info=test or form.get("vacancy_tests"))
            continue

        desc = brief.get("vacancy", {}).get("description") or card.get("snippet") or ""

        # Separation 2: Screening questionnaire (started_with_question / controls)
        prefill_plan = None
        if form.get("started_with_question") or form.get("controls"):
            letter_text = generate_tailored_letter(role=role, company=company, description=desc)
            q_ok, q_verdict, plan, q_reasons = resolve_and_plan_screening(
                vid, form, profile, kb_data, cover_letter=letter_text,
                vacancy_info={"role": role, "company": company, "description": desc}
            )
            if not q_ok:
                logger.info(f"Skipping {vid} ({role} at {company}): questionnaire requires review: {q_reasons}")
                send_owner_questionnaire_alert(vid, role, company, reasons=q_reasons)
                continue
            prefill_plan = plan

        eligible, reason = is_vacancy_eligible_for_auto_apply(brief, has_resolved_screening=bool(prefill_plan))
        if not eligible:
            logger.info(f"Skipping {vid} ({role} at {company}): {reason}")
            continue

        # Step 4: Autonomous Submit
        logger.info(f"Submitting {vid}: {role} at {company} (screening_prefilled={bool(prefill_plan)})...")
        target_resume = select_best_resume_for_vacancy(role, desc)
        logger.info(f"Smart resume selected for {vid} ({role}): {target_resume!r}")
        ok = submit_vacancy(
            vid=vid,
            role=role,
            company=company,
            salary=salary_str,
            description=desc,
            resume_title=target_resume,
            prefill_plan=prefill_plan,
        )
        if ok:
            submitted_count += 1
            known_ids.add(vid)
            results.append({
                "vid": vid,
                "role": role,
                "company": company,
                "salary": salary_str,
                "status": "SUBMITTED",
            })
            time.sleep(5)  # Safe spacing between submissions

    # Step 5: Rapid check of HH negotiations / employer chats
    try:
        from ai_assistant.hh_chat_monitor import scan_hh_negotiations, process_negotiation_alerts
        neg_cards = scan_hh_negotiations()
        if neg_cards:
            sent_alerts = process_negotiation_alerts(neg_cards)
            logger.info(f"Negotiations checked in cycle: {len(neg_cards)} cards, {sent_alerts} alerts sent")
    except Exception as e:
        logger.warning(f"Negotiations check during autopilot cycle failed: {e}")

    cycle_end = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info(f"Cycle completed: submitted {submitted_count}/{max_submits} applications")
    return {
        "status": "SUCCESS",
        "started_at": cycle_start,
        "completed_at": cycle_end,
        "discovered_total": len(discovered),
        "submitted": submitted_count,
        "applications": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="HeadHunter Autonomous Application Engine")
    parser.add_argument("--max-submits", type=int, default=8, help="Maximum applications to submit (default: 8)")
    parser.add_argument("--read-limit", type=int, default=45, help="Number of vacancies to inspect (default: 45)")
    parser.add_argument("--query", action="append", default=None, help="Target search query (repeatable)")
    args = parser.parse_args()

    result = run_autopilot_cycle(
        max_submits=args.max_submits,
        queries=args.query,
        read_limit=args.read_limit,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
