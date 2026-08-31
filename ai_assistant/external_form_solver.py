"""Autonomous External Form & Questionnaire Solver.

Automatically detects, navigates to, and fills external questionnaires (Google Forms, Yandex Forms)
sent by recruiters in HeadHunter chats.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .candidate_profile import CandidateProfile, load_candidate_profile

logger = logging.getLogger(__name__)

EXTERNAL_FORM_DOMAINS = [
    "forms.gle",
    "docs.google.com/forms",
    "forms.yandex.ru",
    "yandex.ru/forms",
    "typeform.com",
    "airtable.com",
]


class ExternalFormQuestion(BaseModel):
    title: str
    field_type: str = "text"  # text, textarea, radio, checkbox, select
    options: List[str] = Field(default_factory=list)
    raw_text: str = ""


class ExternalFormSubmissionResult(BaseModel):
    form_url: str
    form_title: str = ""
    status: str = "PENDING"  # SUBMITTED, FAILED, SKIPPED
    questions_count: int = 0
    filled_fields: List[Dict[str, Any]] = Field(default_factory=list)
    confirmed: bool = False
    error: Optional[str] = None


def extract_external_form_urls(text: str) -> List[str]:
    """Extract external form / questionnaire URLs from message text."""
    urls = []
    url_pattern = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
    matches = url_pattern.findall(text)
    for m in matches:
        clean_url = m.rstrip(".,;!?)")
        if any(dom in clean_url.lower() for dom in EXTERNAL_FORM_DOMAINS):
            urls.append(clean_url)
    return urls


def solve_google_form_js(profile: Optional[CandidateProfile] = None) -> str:
    """Generate JavaScript to fill all standard fields in a Google Form based on CandidateProfile."""
    if profile is None:
        profile = load_candidate_profile()

    name = profile.name or "Михаил Кириллович"
    age = str(profile.age or 42)
    github = profile.github or "https://github.com/mikheooo"
    years = profile.years_experience or 3

    answers = {
        "fio": name,
        "age": age,
        "tg": "@mikheooo",
        "hh": "https://hh.ru/applicant/resumes",
        "city": "Таиланд / Бангкок (100% Full Remote)",
        "office_5_2": "Нет",
        "office_center": "Нет",
        "work_type": "Фулл-тайм",
        "university": "Нет",
        "why_interested": "Разработка и оркестрация ИИ-агентов, практическое внедрение LLM в бизнес-процессы, построение надёжных агентных цепочек и сквозная автоматизация на Python.",
        "ai_experience": f"Более {years} лет. В разработке активно использую Python (OpenAI/Anthropic API, LangChain), n8n, Claude Code, Cursor для автоматизации backend-сервисов, интеграций и мультиагентных систем.",
        "ai_task": "Создание автономной системы поиска и отклика на вакансии / автоматизации B2B-заявок с интеграцией CRM, валидацией данных и обработкой через LLM. На входе — неструктурированные входящие требования, на выходе — готовые выверенные решения без сбоев.",
        "portfolio": github,
        "checkboxes": ["Claude", "Claude Code", "Codex / GitHub Copilot", "ChatGPT / GPT-4", "N8N", "Cursor", "Perplexity"],
        "last_job": "Разработчик систем автоматизации и бэкенда (Python/AI). Переход на новые проекты в связи с фокусом на LLM и мультиагентные архитектуры.",
        "salary": "Готов обсудить в соответствии с рынком и задачами проекта",
        "rating": "8 из 10. Сильный практический бэкграунд в Python, FastAPI, Docker, n8n и реальный опыт разработки и внедрения агентных LLM-систем.",
        "about": "Инженер с сильной базой в Python и автоматизации. Увлекаюсь агентными системами, новыми технологиями в области AI и созданием отказоустойчивых автономных сервисов."
    }

    return f"""(() => {{
        const results = [];
        const items = Array.from(document.querySelectorAll('[role="listitem"], div[jsmodel]'));

        function setVal(el, val) {{
            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
        }}

        const answersMap = {json.dumps(answers, ensure_ascii=False)};

        for (const it of items) {{
            const titleEl = it.querySelector('[role="heading"], span[dir="auto"]');
            const title = titleEl ? titleEl.innerText.trim() : '';
            if (!title || title.length < 2) continue;

            // 1. Text / Textarea
            const textInput = it.querySelector('input[type="text"], textarea');
            if (textInput) {{
                let val = null;
                if (title.includes('ФИО')) val = answersMap.fio;
                else if (title.includes('Возраст')) val = answersMap.age;
                else if (title.includes('Telegram')) val = answersMap.tg;
                else if (title.includes('резюме') || title.includes('hh.ru')) val = answersMap.hh;
                else if (title.includes('Город')) val = answersMap.city;
                else if (title.includes('заинтересовала')) val = answersMap.why_interested;
                else if (title.includes('давно ты работаешь')) val = answersMap.ai_experience;
                else if (title.includes('Опиши задачу')) val = answersMap.ai_task;
                else if (title.includes('Портфолио') || title.includes('ссылки на твои проекты')) val = answersMap.portfolio;
                else if (title.includes('последний опыт работы')) val = answersMap.last_job;
                else if (title.includes('Желаемый заработок')) val = answersMap.salary;
                else if (title.includes('Оцени свои навыки')) val = answersMap.rating;
                else if (title.includes('Расскажи немного о себе')) val = answersMap.about;

                if (val) {{
                    setVal(textInput, val);
                    results.push({{ field: title.slice(0, 30), type: 'text', val: val, ok: textInput.value === val }});
                    continue;
                }}
            }}

            // 2. Radio buttons
            const radios = Array.from(it.querySelectorAll('[role="radio"]'));
            if (radios.length > 0) {{
                let targetOption = null;
                if (title.includes('в офисе 5/2')) targetOption = answersMap.office_5_2;
                else if (title.includes('добираться в наш офис')) targetOption = answersMap.office_center;
                else if (title.includes('фулл-тайм или совмещать')) targetOption = answersMap.work_type;
                else if (title.includes('университете')) targetOption = answersMap.university;

                if (targetOption) {{
                    for (const r of radios) {{
                        const optText = (r.innerText || r.getAttribute('data-value') || r.getAttribute('aria-label') || '').trim();
                        if (optText.toLowerCase() === targetOption.toLowerCase() || optText.toLowerCase().includes(targetOption.toLowerCase())) {{
                            r.click();
                            results.push({{ field: title.slice(0, 30), type: 'radio', chosen: targetOption, ok: true }});
                            break;
                        }}
                    }}
                    continue;
                }}
            }}

            // 3. Checkboxes
            const checkboxes = Array.from(it.querySelectorAll('[role="checkbox"]'));
            if (checkboxes.length > 0 && title.includes('ИИ-инструменты')) {{
                let checkedCount = 0;
                for (const cb of checkboxes) {{
                    const cbText = (cb.innerText || cb.getAttribute('aria-label') || '').trim();
                    for (const targetCb of answersMap.checkboxes) {{
                        if (cbText.toLowerCase().includes(targetCb.toLowerCase())) {{
                            if (cb.getAttribute('aria-checked') !== 'true') {{
                                cb.click();
                            }}
                            checkedCount++;
                            break;
                        }}
                    }}
                }}
                results.push({{ field: title.slice(0, 30), type: 'checkboxes', count: checkedCount, ok: true }});
            }}
        }}

        return {{
            title: document.title,
            url: location.href,
            results: results
        }};
    }})()"""
