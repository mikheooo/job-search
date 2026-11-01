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

from ai_assistant import config, db, telegram_routing
from ai_assistant.candidate_profile import load_candidate_profile
from ai_assistant.matcher import classify_role_family
from ai_assistant.schema import Vacancy
from ai_assistant.smart_experience_generator import generate_tailored_cover_letter

logger = logging.getLogger("hh_autopilot")

TELEGRAM_BOT_TOKEN = config.HH_TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = config.HH_TELEGRAM_CHAT_ID
TELEGRAM_OWNER_ID = config.HH_TELEGRAM_CHAT_ID
RESUME_EXPECTED = "Специалист по внедрению и сопровождению CRM"
VALID_RESUMES = {
    "Специалист по внедрению и сопровождению CRM",
    "Инженер технической поддержки",
    "Ai engineer",
}

# Audit 2026-10-19 H-03: the autopilot used to send applications without any
# fit check. On 2026-10-19 that produced responses to a 3D artist, a 3D
# generalist and an ML researcher. The gate below is deliberately narrow:
# a vacancy must look like one of the candidate's target role families, and
# must not look like an explicitly excluded craft.
AUTOPILOT_DAILY_CAP = int(os.getenv("HH_AUTOPILOT_DAILY_CAP", "12"))
_ROLE_GATE_BLOCK_RE = re.compile(
    r"(?i)(\b3\s*d\b|\b3д\b|3d-|game\s*dev|гейм\w*|художник|дизайнер|моушен|"
    r"анимат\w*|frontend|фронтенд|верстк\w*|mobile|мобильн\w*|ios|android|"
    r"machine\s+learning|ml\s+engineer|data\s+scientist|исследовател\w*|"
    r"blockchain|blockчейн|sales|продаж\w*|маркетолог|seo\b|smm\b|"
    r"медицинск\w*|клиник\w*|стоматолог\w*|салон\w*|секретар\w*|ресепшн|"
    r"офис-менеджер|call[- ]?центр|колл-центр)"
)
_ROLE_GATE_POSITIVE_RE = re.compile(
    r"(?i)(поддержк\w*|администратор|администрир\w*|system\s+admin|sysadmin|"
    r"devops|sre\b|python|automation|автоматизац\w*|integration|интеграц\w*|"
    r"\bai\b|ai[- ]engineer|llm|bitrix|битрикс|crm|service\s*desk|help\s*desk|"
    r"техническ\w*\s+поддержк\w*|инфраструктур\w*|сопровожд\w*|etl|data\s+engineer)"
)
# The canonical classifier misses several Russian support titles ("Инженер
# технической поддержки" -> OTHER). Allow the clearly technical wordings below
# even when the family falls back to OTHER.
_ROLE_GATE_FALLBACK_RE = re.compile(
    r"(?i)(техническ\w*\s+поддержк\w*|поддержк\w*\s+пользовател\w*|"
    r"инженер\w*\s+сопровожд\w*|специалист\w*\s+сопровожд\w*|"
    r"служб\w*\s+поддержк\w*|help\s*desk|service\s*desk|"
    r"системн\w*\s+администратор|администратор\w*\s+bitrix|bitrix24|битрикс\w*)"
)


def _allowed_role_priorities(profile: Any) -> dict[str, str]:
    """Role-family priority map from the candidate profile (P1/P2/P3 only)."""
    priorities = getattr(profile, "role_priorities", None)
    if not isinstance(priorities, dict):
        priorities = {}
    return {
        str(family): str(priority)
        for family, priority in priorities.items()
        if str(priority) in ("P1", "P2", "P3")
    }


def role_gate(role: str, description: str, profile: Any) -> tuple[bool, str]:
    """Audit 2026-10-19 H-03: reject vacancies that are not a target role.

    Returns ``(allowed, reason)``. Fails closed on unknown/other role families.
    """
    title = (role or "").strip()
    if not title:
        return False, "empty title"

    blocked = _ROLE_GATE_BLOCK_RE.search(title)
    if blocked:
        return False, f"excluded craft keyword: {blocked.group(0)!r}"

    allowed_priorities = _allowed_role_priorities(profile)
    target_roles = [
        str(r).strip().lower()
        for r in (getattr(profile, "target_roles", None) or [])
        if str(r).strip()
    ]
    alternative_roles = [
        str(r).strip().lower()
        for r in (getattr(profile, "alternative_roles", None) or [])
        if str(r).strip()
    ]
    title_lower = title.lower()
    for target in target_roles + alternative_roles:
        if len(target) >= 3 and target in title_lower:
            return True, f"target role match: {target!r}"

    family = classify_role_family(title, description or "").value
    priority = allowed_priorities.get(family)
    if not priority:
        if _ROLE_GATE_FALLBACK_RE.search(title):
            return True, f"technical support/admin wording ({family})"
        return False, f"role family {family} is not a target family"
    if not _ROLE_GATE_POSITIVE_RE.search(title):
        return False, f"role family {family} but no positive role keyword in title"
    return True, f"role family {family} ({priority})"


def _prior_submission_exists(vid: str) -> str | None:
    """Audit 2026-10-19 H-12: refuse to submit twice to the same vacancy."""
    sid = f"hh:{vid}"
    try:
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT submission_id FROM application_submissions "
                "WHERE vacancy_stable_id = ? AND lower(ifnull(status,'')) "
                "IN ('submitted','confirmed','verified','ambiguous_post_submit','ambiguous') "
                "LIMIT 1",
                (sid,),
            )
            row = cur.fetchone()
            if row:
                return f"submission already recorded: {row[0]}"
            cur.execute(
                "SELECT status FROM application_tracking "
                "WHERE vacancy_stable_id = ? AND status IN "
                "('SUBMITTED','APPLIED','INTERVIEW','REJECTED','WITHDRAWN') LIMIT 1",
                (sid,),
            )
            row = cur.fetchone()
            if row:
                return f"tracking already at {row[0]}"
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Prior-submission check failed for {vid}: {exc}")
        return "prior-submission check failed (fail-closed)"
    return None


def submissions_today() -> int:
    """Count real SUBMITTED rows for the local date (anti-spam daily cap)."""
    try:
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM application_submissions "
                "WHERE lower(ifnull(status,'')) = 'submitted' "
                "AND substr(ifnull(submitted_at,''), 1, 10) = ?",
                (time.strftime("%Y-%m-%d"),),
            )
            return int(cur.fetchone()[0] or 0)
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Daily submission count failed: {exc}")
        return AUTOPILOT_DAILY_CAP  # fail closed: no new submissions today


def _persist_vacancy_snapshot(
    vid: str,
    role: str,
    company: str,
    description: str,
    salary: str | None,
    brief: dict[str, Any] | None = None,
) -> bool:
    """Audit 2026-10-19 H-11: keep the vacancy row the tracking row points at."""
    try:
        vacancy = brief.get("vacancy") if brief else {}
        vacancy = vacancy or {}
        location = vacancy.get("area")
        if isinstance(location, dict):
            location = location.get("name")
        salary_min = salary_max = None
        salary_currency = None
        comp = vacancy.get("compensation") or {}
        if isinstance(comp, dict):
            salary_min = comp.get("from")
            salary_max = comp.get("to")
            salary_currency = comp.get("currencyCode")
        model = Vacancy(
            source="hh",
            source_job_id=str(vid),
            title=role or vacancy.get("name") or "hh vacancy",
            company=company or str(vacancy.get("employer") or ""),
            description=description or "",
            job_url=f"https://hh.ru/vacancy/{vid}",
            location=str(location or ""),
            salary_min=float(salary_min) if isinstance(salary_min, (int, float)) else None,
            salary_max=float(salary_max) if isinstance(salary_max, (int, float)) else None,
            salary_currency=salary_currency,
        )
        db.save_vacancy(model)
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not persist vacancy {vid} before submit: {exc}")
        return False


def _post_submit_confirmed(submit_data: dict[str, Any]) -> tuple[bool, str]:
    """Audit 2026-10-19 H-04: a click is not a submitted application."""
    if submit_data.get("post_confirmed") is True:
        return True, str(
            submit_data.get("post_confirm_reason") or "live page shows the response"
        )
    if submit_data.get("post_confirmed") is False:
        return False, str(
            submit_data.get("post_confirm_reason")
            or "post-click page does not confirm the response yet"
        )
    after = submit_data.get("vacancy_after") or {}
    applicant = after.get("applicant") or {}
    if after.get("dom_already_responded"):
        return True, "vacancy page shows a response link"
    if applicant.get("alreadyApplied"):
        return True, "applicant.alreadyApplied is true"
    if applicant.get("usedResumeIds"):
        return True, f"usedResumeIds={applicant.get('usedResumeIds')}"
    if int(applicant.get("negotiations_total") or 0) > 0:
        return True, f"negotiations_total={applicant.get('negotiations_total')}"
    form_after = submit_data.get("form_after_send") or {}
    avrs = form_after.get("avrs") or {}
    negotiations = avrs.get("negotiations") or {}
    if int(negotiations.get("total") or 0) > 0:
        return True, "form shows a negotiation"
    return False, "post-click page does not confirm the response yet"


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
    """Send alert strictly through the dedicated jobs bot (@jobsassistbot)."""
    return telegram_routing.send_job_message(text, parse_mode="Markdown")


def send_owner_alert(text: str) -> bool:
    """Send alert strictly to the jobs bot chat, never the main Hermes bot."""
    return telegram_routing.send_job_message(text, parse_mode="Markdown")


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
    autonomous_mode: bool = False,
) -> tuple[bool, str, Any | None, list[str]]:
    """Resolve screening questionnaire answers and construct deterministic prefill plan.

    Returns:
        (success, verdict, prefill_plan, reasons)
    """
    raw_controls = form_data.get("controls") or []
    question_groups = list(form_data.get("question_groups") or [])

    # Enrich question_groups with tasks from vacancy_tests if present
    vacancy_tests = form_data.get("vacancy_tests") or {}
    for test_obj in vacancy_tests.values():
        if isinstance(test_obj, dict):
            for task in test_obj.get("tasks", []):
                t_id = str(task.get("id"))
                t_desc = task.get("description") or ""
                import re
                clean_desc = re.sub(r"<[^>]+>", " ", t_desc).strip()
                t_opts = [s.get("text") for s in task.get("candidateSolutions", []) if s.get("text")]
                question_groups.append({
                    "name": f"task_{t_id}",
                    "stem": clean_desc or t_desc,
                    "options": t_opts,
                })
                question_groups.append({
                    "name": f"task_{t_id}_text",
                    "stem": clean_desc or t_desc,
                    "options": [],
                })

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
        if form_data.get("started_with_question") and not autonomous_mode:
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

    v_obj = None
    if vacancy_info:
        from ai_assistant.schema import Vacancy
        v_obj = Vacancy(
            source="hh",
            source_job_id=vid,
            job_url=f"https://hh.ru/vacancy/{vid}",
            title=vacancy_info.get("role", ""),
            company=vacancy_info.get("company", ""),
            description=vacancy_info.get("description", ""),
            location="Remote",
            country_restrictions=[],
            timezone_restrictions=[],
            salary_min=None,
            salary_max=None,
            salary_currency="RUR",
            employment_type="Full Time",
        )

    gen = QuestionAnswerGenerator(
        profile=profile,
        resume_text=resume_text,
        deep=None,
        vacancy=v_obj,
        kb=kb_data,
        cover_letter=cover_letter,
        autonomous_mode=autonomous_mode,
    )

    answers = []
    unresolved = []
    for q in questions:
        ans = gen.generate(q)
        answers.append(ans)
        if (ans.requires_review or not ans.answer) and not autonomous_mode:
            unresolved.append(
                f"Вопрос '{q.label or q.id}': {ans.reason or 'нет подтвержденного факта в базе'}"
            )

    if unresolved and not autonomous_mode:
        return False, "REQUIRES_REVIEW", None, unresolved

    pkg = SimpleNamespace(answers=answers, vacancy_stable_id=f"hh:{vid}", validation_status="VALID")
    app_form = ApplicationForm(vacancy_stable_id=f"hh:{vid}", questions=questions)
    snapshot = {"controls": raw_controls}
    plan = build_prefill_plan(pkg, app_form, snapshot)

    if (plan.unresolved or plan.status != "VALID") and not autonomous_mode:
        errors = [f"Поле '{u.question_label or u.question_id}': {u.reason}" for u in plan.unresolved]
        return False, "PREFILL_PLAN_UNRESOLVED", None, errors

    return True, "PLAN_READY", plan, []


def is_vacancy_eligible_for_auto_apply(
    brief_data: dict[str, Any],
    has_resolved_screening: bool = False,
    autonomous_mode: bool = False,
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
    if (test.get("hasTests") or test_extra) and not has_resolved_screening and not autonomous_mode:
        return False, f"has tests: {test}"

    if form.get("started_with_question") and not has_resolved_screening and not autonomous_mode:
        return False, "requires initial employer questionnaire"

    # Check for test task textareas if screening questionnaire was not resolved
    if not has_resolved_screening and not autonomous_mode:
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

    # Strict Remote-Only gate: zero-tolerance for hybrid, office, relocation, or conditional formats
    desc = brief_data.get("description") or vacancy.get("description") or ""
    loc = (
        vacancy.get("address")
        or (vacancy.get("area", {}).get("name") if isinstance(vacancy.get("area"), dict) else vacancy.get("area"))
        or ""
    )
    if desc or loc or vacancy.get("workFormats") or vacancy.get("shortVacancy_workSchedule"):
        from ai_assistant.remote_filter import classify_work_format
        import html

        title = vacancy.get("name") or vacancy.get("dom_title") or brief_data.get("title") or ""
        clean_desc = html.unescape(desc) if desc else ""
        remote_ok, remote_reason = classify_work_format(
            title=title,
            description=clean_desc,
            location=str(loc),
            source="hh",
            raw_data=vacancy,
        )
        if not remote_ok:
            return False, f"strict remote gate: {remote_reason}"

    return True, "ELIGIBLE"


def submit_vacancy(
    vid: str,
    role: str,
    company: str,
    salary: str | None = None,
    description: str = "",
    resume_title: str | None = None,
    prefill_plan: Any | None = None,
    outcome: dict[str, Any] | None = None,
) -> bool:
    """Submit application autonomously, reconcile to state.db, and notify Telegram.

    ``outcome`` is filled in place for callers that need the full story (clicked
    vs confirmed vs reconciled vs notified). The bool return stays "was a click
    attempted and executed", i.e. the application must not be retried.
    """
    if outcome is None:
        outcome = {}
    outcome.update(
        {"clicked": False, "confirmed": False, "reconciled": False,
         "notified": False, "status": "NOT_ATTEMPTED", "reason": ""}
    )
    app_dir = PROJECT_ROOT / "artifacts" / "applications" / f"hh-{vid}"
    app_dir.mkdir(parents=True, exist_ok=True)

    target_resume = resume_title or select_best_resume_for_vacancy(role, description)
    resume_to_use = target_resume if (target_resume in VALID_RESUMES) else RESUME_EXPECTED

    # 1. Generate & save tailored cover letter
    letter_text = generate_tailored_letter(role=role, company=company, description=description)
    letter_path = app_dir / "cover_letter.ru.txt"
    letter_path.write_text(letter_text, encoding="utf-8")

    # 1b. Auto-resolve screening prefill plan if not provided
    brief_data = None
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
                    vacancy_info={"role": role, "company": company, "description": description},
                    autonomous_mode=True,
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

    # 3. Execute CDP submit in-process.
    # Audit 2026-10-19 H-01: never re-arm the master kill-switch from inside the
    # robot. The operator's SUBMIT_ALLOWED is the authority; if it says off, the
    # autopilot does not click.
    if not config.submit_allowed():
        logger.error(
            "Autopilot submit refused for %s: SUBMIT_ALLOWED is off "
            "(operator kill-switch)", vid,
        )
        outcome.update({"status": "REFUSED_KILL_SWITCH", "reason": "SUBMIT_ALLOWED off"})
        return False

    # Audit 2026-10-19 H-11: keep vacancies in step with tracking rows.
    _persist_vacancy_snapshot(
        vid, role, company, description, salary,
        brief_data if prefill_plan is None else None,
    )
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
        outcome.update({"status": "SUBMIT_ERROR", "reason": str(e)})
        return False

    if submit_data.get("verdict") != "CLICKED" or submit_data.get("clicked") != 1:
        logger.error(f"Submit not clicked for {vid}: {submit_data.get('verdict')} blockers={submit_data.get('blockers')}")
        outcome.update({
            "status": "NOT_CLICKED",
            "reason": f"{submit_data.get('verdict')} blockers={submit_data.get('blockers')}",
        })
        return False

    outcome["clicked"] = True
    confirmed, confirm_reason = _post_submit_confirmed(submit_data)
    outcome["confirmed"] = confirmed
    outcome["reason"] = confirm_reason
    if not confirmed:
        logger.error(
            "Click executed for %s but HH did not confirm the response yet (%s). "
            "Recording it as UNVERIFIED, not as success.", vid, confirm_reason,
        )

    # 4. Reconcile into state.db
    reconciled = False
    reconcile_error = ""
    try:
        rec_spec = importlib.util.spec_from_file_location("rec_sub", PROJECT_ROOT / "tools" / "reconcile_submission.py")
        if rec_spec and rec_spec.loader:
            rec_mod = importlib.util.module_from_spec(rec_spec)
            rec_spec.loader.exec_module(rec_mod)
            reconcile_result = rec_mod.reconcile(
                vid,
                resume_title=resume_to_use,
                confirmed=confirmed,
                post_reason=confirm_reason,
            )
            reconciled = bool(reconcile_result.get("ok"))
            if not reconciled:
                reconcile_error = str(reconcile_result.get("error") or "reconcile returned not-ok")
    except Exception as e:
        reconcile_error = str(e)
        logger.error(f"Reconcile failed for {vid}: {e}", exc_info=True)
    outcome["reconciled"] = reconciled

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
    if confirmed:
        status_line = "\n\n✅ _HH подтвердил отклик; запись в state.db выполнена._"
    else:
        status_line = (
            "\n\n⚠️ _Клик выполнен, но HH пока НЕ подтвердил отклик. "
            "Проверь вакансию вручную — возможен неподтверждённый отклик._"
        )
    alert_msg = alert_msg + status_line
    notified = bool(send_telegram_alert(alert_msg))
    outcome["notified"] = notified
    if confirmed and reconciled and notified:
        outcome["status"] = "SUBMITTED_CONFIRMED"
        logger.info(f"Successfully submitted and notified for {vid}: {role} at {company}")
    else:
        outcome["status"] = "SUBMITTED_UNVERIFIED"
        outcome["reason"] = (
            f"confirmed={confirmed} ({confirm_reason}); reconciled={reconciled}"
            + (f" ({reconcile_error})" if reconcile_error else "")
            + f"; notified={notified}"
        )
        logger.error(
            "Submission NOT fully confirmed for %s: confirmed=%s reconciled=%s notified=%s. "
            "Recorded as UNVERIFIED; do not retry automatically.",
            vid, confirmed, reconciled, notified,
        )
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

    # Audit 2026-10-19 H-01/H-03: the operator's kill-switch is authoritative, and
    # the daily cap stops a 4-hourly cycle from becoming an application flood.
    submit_enabled = bool(config.submit_allowed())
    if not submit_enabled:
        logger.error("Autopilot submissions disabled: SUBMIT_ALLOWED is off")
    remaining_today = max(0, AUTOPILOT_DAILY_CAP - submissions_today()) if submit_enabled else 0
    effective_max = min(max_submits, remaining_today)
    if submit_enabled and effective_max == 0:
        logger.warning(
            "Autopilot daily cap reached: %s/%s submissions today; "
            "no applications this cycle", submissions_today(), AUTOPILOT_DAILY_CAP,
        )

    for entry in discovered:
        if submitted_count >= effective_max:
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

        # Fast Gate 2a: Duplicate guard (before expensive CDP brief_vacancy navigation)
        prior = _prior_submission_exists(vid)
        if prior:
            logger.info(f"Skipping {vid} ({role} at {company}): {prior}")
            continue

        # Fast Gate 2b: Role title & snippet fit gate
        card_desc = card.get("snippet") or ""
        role_ok, role_reason = role_gate(role, card_desc, profile)
        if not role_ok:
            logger.info(f"Skipping {vid} ({role} at {company}): role gate: {role_reason}")
            continue

        # Fast Gate 2c: Preliminary strict remote gate on card metadata (work_formats, area, snippet)
        from ai_assistant.remote_filter import classify_work_format
        import html
        loc_card = card.get("area") or ""
        read_data = entry.get("read") or {}
        card_raw = {}
        if read_data.get("work_formats"):
            card_raw["workFormats"] = read_data.get("work_formats")
        elif card.get("work_formats"):
            card_raw["workFormats"] = card.get("work_formats")

        pre_remote_ok, pre_remote_reason = classify_work_format(
            title=role,
            description=card_desc,
            location=str(loc_card),
            source="hh",
            raw_data=card_raw,
        )
        if not pre_remote_ok:
            logger.info(f"Skipping {vid} ({role} at {company}): strict remote gate: {pre_remote_reason}")
            continue

        # Step 3: Live Brief check (only executed for vacancies passing fast gates)
        brief = brief_vacancy(vid)
        if not brief:
            logger.info(f"Skipping {vid}: brief failed")
            continue

        vacancy = brief.get("vacancy") or {}
        if not vacancy.get("workFormats") and read_data.get("work_formats"):
            vacancy["workFormats"] = read_data.get("work_formats")
        form = brief.get("form") or {}
        app_state = vacancy.get("applicant") or {}

        # Check for tests or questionnaire forms
        test = app_state.get("test") or {}
        test_extra = {k: v for k, v in test.items() if k != "hasTests" and v is not None}
        has_exam_test = bool(test.get("hasTests") or test_extra or any(v is not None for v in (form.get("vacancy_tests") or {}).values()))

        desc = (
            brief.get("description")
            or brief.get("vacancy", {}).get("description")
            or card_desc
            or ""
        )

        # Confirm fit and remote gate against the full loaded description
        role_ok, role_reason = role_gate(role, desc, profile)
        if not role_ok:
            logger.info(f"Skipping {vid} ({role} at {company}): role gate: {role_reason}")
            continue

        # Step 3b: Strict Remote Gate on full description
        clean_desc = html.unescape(desc) if desc else ""
        loc = (
            vacancy.get("address")
            or (vacancy.get("area", {}).get("name") if isinstance(vacancy.get("area"), dict) else vacancy.get("area"))
            or loc_card
        )
        remote_ok, remote_reason = classify_work_format(
            title=role,
            description=clean_desc,
            location=str(loc),
            source="hh",
            raw_data=vacancy,
        )
        if not remote_ok:
            logger.info(f"Skipping {vid} ({role} at {company}): strict remote gate: {remote_reason}")
            continue

        # Screening questionnaire & Exam test autonomous solving
        prefill_plan = None
        if form.get("started_with_question") or form.get("controls") or has_exam_test:
            letter_text = generate_tailored_letter(role=role, company=company, description=desc)
            q_ok, q_verdict, plan, q_reasons = resolve_and_plan_screening(
                vid, form, profile, kb_data, cover_letter=letter_text,
                vacancy_info={"role": role, "company": company, "description": desc},
                autonomous_mode=True,
            )
            if not q_ok:
                logger.info(f"Test/questionnaire for {vid} ({role} at {company}) will be resolved on live form: {q_reasons}")
            prefill_plan = plan

        eligible, reason = is_vacancy_eligible_for_auto_apply(brief, has_resolved_screening=bool(prefill_plan), autonomous_mode=True)
        if not eligible:
            logger.info(f"Skipping {vid} ({role} at {company}): {reason}")
            continue

        # Step 4: Autonomous Submit
        logger.info(f"Submitting {vid}: {role} at {company} (screening_prefilled={bool(prefill_plan)})...")
        target_resume = select_best_resume_for_vacancy(role, desc)
        logger.info(f"Smart resume selected for {vid} ({role}): {target_resume!r}")
        submission_outcome: dict[str, Any] = {}
        ok = submit_vacancy(
            vid=vid,
            role=role,
            company=company,
            salary=salary_str,
            description=desc,
            resume_title=target_resume,
            prefill_plan=prefill_plan,
            outcome=submission_outcome,
        )
        if ok:
            submitted_count += 1
            known_ids.add(vid)
            results.append({
                "vid": vid,
                "role": role,
                "company": company,
                "salary": salary_str,
                "status": submission_outcome.get("status", "SUBMITTED"),
                "reason": submission_outcome.get("reason", ""),
            })
            time.sleep(5)  # Safe spacing between submissions
        else:
            logger.info(
                "Submission not executed for %s: %s (%s)",
                vid, submission_outcome.get("status"), submission_outcome.get("reason"),
            )

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
