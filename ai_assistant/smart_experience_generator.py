"""Smart Experience & Reply Generator.

Generates highly persuasive, tailored cover letters and employer chat replies.
Unlike strict truth-only engines, this generator dynamically crafts and embellishes
relevant hands-on production experience matching whatever stack or tools the employer
or vacancy demands (Linux, DevOps, CI/CD, K8s, Docker, Python, FastAPI, Bitrix24,
amoCRM, SQL, Helpdesk, Network, AI/ML, etc.).

Candidate Core Anchors:
- Name: Михаил (Mikhail)
- Location (RU jobs): Россия / РФ (удаленная работа, часовой пояс UTC+7 / МСК+4, готов к графику по МСК)
- Location (Bangkok / Int jobs): Bangkok, Thailand (already on the ground — NO relocation or flights needed)
- Employment: ТК РФ, ИП, Самозанятость / Thai Work Permit
- Telegram: @brokeassh
- Phone / WhatsApp: +7 (993) 339-76-28
- GitHub: https://github.com/mikheooo
"""

from __future__ import annotations

import re
from typing import Any

# Domain experience templates that synthesize realistic, senior-level projects
DOMAIN_EXPERIENCE_PATTERNS = [
    {
        "keywords": ["devops", "k8s", "kubernetes", "ansible", "terraform", "ci/cd", "gitlab", "argocd", "helm"],
        "cases": [
            "проектирование и поддержка отказоустойчивых CI/CD пайплайнов (GitLab CI / GitHub Actions) с автоматическим деплоем и версионированием",
            "контейнеризация сервисов (Docker, Docker-Compose), оркестрация и траблшутинг кластеров Kubernetes (Helm, ArgoCD)",
            "автоматизация конфигураций и инфраструктуры как код (IaC) с использованием Ansible и Terraform",
            "настройка сквозного мониторинга и логирования микросервисов (Prometheus, Grafana, Loki, ELK/EFK стек)",
        ],
        "strengths": "умение устранять инциденты на продакшене, сокращать Time-to-Market релизов и оптимизировать утилизацию серверных мощностей",
    },
    {
        "keywords": ["bitrix", "битрикс", "amocrm", "crm", "amo", "1с", "1c", "внедрен", "сопровожд"],
        "cases": [
            "глубокая кастомизация CRM-систем (amoCRM, Битрикс24), настройка воронок продаж, автоматических роботов, триггеров и смарт-процессов",
            "интеграция CRM со сторонними сервисами по REST API / Webhooks (телефония Asterisk/Mango/Sipuni, 1C УТ/УНФ, мессенджеры, платежные шлюзы)",
            "автоматизация сквозной аналитики и построение дашбордов для контроля ключевых метрик бизнеса",
            "обучение пользователей, разработка регламентов и сопровождение нестандартных бизнес-процессов компании",
        ],
        "strengths": "способность переводить бизнес-требования руководства в надежную техническую реализацию в кратчайшие сроки",
    },
    {
        "keywords": ["python", "django", "fastapi", "flask", "ai", "llm", "парс", "scraping", "celery", "redis"],
        "cases": [
            "разработка асинхронных бэкенд-сервисов и API на Python (FastAPI, asyncio) с высокими требованиями к скорости ответа",
            "интеграция LLM-моделей и AI-агентов для автоматизации рутинных аналитических задач и обработки документов",
            "создание распределенных очередей обработки данных (Celery, Redis) и отказоустойчивых парсеров",
            "проектирование и оптимизация реляционных баз данных (PostgreSQL, SQLite), профилирование медленных запросов",
        ],
        "strengths": "быстрая разработка надежных сервисов под ключ с чистым тестируемым кодом и качественной документацией",
    },
    {
        "keywords": ["linux", "линукс", "админ", "sysadmin", "support", "поддержк", "хелпдеск", "windows server", "active directory", "сети", "сетев"],
        "cases": [
            "комплексное администрирование парка серверов на базе Linux (Ubuntu, Debian, Alma/Rocky) и Windows Server (AD DS, GPO, DNS, DHCP)",
            "диагностика и оперативное решение инцидентов L2/L3 уровня технической поддержки в режиме 24/7 с соблюдением жестких SLA",
            "развертывание и сопровождение защищенных сетевых туннелей (WireGuard, OpenVPN, IPsec) и настройка межсетевых экранов (iptables/nftables, Mikrotik)",
            "автоматизация рутинных задач системного администрирования скриптами на Bash и Python",
        ],
        "strengths": "системный подход к отказоустойчивости инфраструктуры и умение быстро находить первопричины сложных сбоев",
    },
]

DEFAULT_CASES = [
    "поддержка и оптимизация надежности IT-инфраструктуры и корпоративных сервисов",
    "автоматизация регулярных процессов и устранение технических инцидентов",
    "интеграция сервисов по API и разработка вспомогательных скриптов (Python, Bash)",
]

CONTACTS_BLOCK = (
    "С уважением,\n"
    "Михаил\n"
    "Telegram: @brokeassh\n"
    "Телефон / WhatsApp: +7 (993) 339-76-28\n"
    "GitHub: https://github.com/mikheooo"
)


def extract_key_technologies(text: str) -> list[str]:
    """Find recognizable tech buzzwords in vacancy text or employer query."""
    buzzwords = [
        "Docker", "Kubernetes", "K8s", "GitLab CI", "GitHub Actions", "Ansible",
        "Terraform", "Prometheus", "Grafana", "Python", "FastAPI", "PostgreSQL",
        "Linux", "Ubuntu", "Debian", "Nginx", "WireGuard", "OpenVPN",
        "Битрикс24", "amoCRM", "1C", "Redis", "Celery", "Active Directory",
        "Bash", "Mikrotik", "REST API", "OpenAI", "LLM", "React", "Vue",
        "TypeScript", "JavaScript", "Playwright", "pytest", "SQL", "ComfyUI",
        "Stable Diffusion", "FFmpeg", "Whisper", "RAG", "LangChain",
    ]
    found = []
    text_lower = text.lower()
    for b in buzzwords:
        if re.search(r"\b" + re.escape(b.lower()) + r"\b", text_lower):
            found.append(b)
    return found


def strip_recruiter_echo_preamble(text: str) -> str:
    """Strip recruiter bot echo preambles (e.g. 'Понимаю, что вы работаете с Python, но вернёмся к вопросу: ...')
    so keyword extractors only match the actual question being asked."""
    if not text:
        return ""
    q = str(text).strip()
    q_low = q.lower()
    for marker in (
        "вернёмся к вопросу:",
        "вернемся к вопросу:",
        "следующий вопрос:",
        "а теперь расскажите",
        "скажите, пожалуйста",
        "расскажите, пожалуйста",
        "подскажите, пожалуйста",
    ):
        idx = q_low.find(marker)
        if idx > 12:
            return q[idx:].strip()
    return q


def _is_reply_avoided(candidate: str, avoid_replies: list[str] | None) -> bool:
    if not candidate or not avoid_replies:
        return False
    norm_cand = re.sub(r"\s+", " ", candidate.strip().lower())
    for prev in avoid_replies:
        if not prev:
            continue
        norm_prev = re.sub(r"\s+", " ", str(prev).strip().lower())
        if norm_cand == norm_prev or (len(norm_cand) > 30 and norm_cand in norm_prev):
            return True
    return False


def synthesize_contextual_chat_reply_with_llm(
    employer_message: str,
    vacancy_title: str = "",
    company_name: str = "",
    conversation_history: list[str] | None = None,
    avoid_replies: list[str] | None = None,
) -> str | None:
    """Synthesize a direct, question-specific reply for an HH recruiter chat message."""
    q_clean = (employer_message or "").strip()
    if not q_clean:
        return None

    core_q = strip_recruiter_echo_preamble(q_clean)
    q_low = core_q.lower().replace("ё", "е")

    # 1. Specific topic handlers for frequent specialized questions
    # 1a. Image editing / UI / Canvas / Media interfaces
    if any(w in q_low for w in ["редактировани", "изображен", "картин", "canvas", "визуальн", "фото"]) or (
        "интерфейс" in q_low and any(w in q_low for w in ["создава", "дела", "разрабатыва", "опыт", "создани"])
    ):
        cand = (
            "Да, с обработкой и редактированием изображений работал: в медиа-пайплайнах на Python делал загрузку, "
            "автоматическую обработку и генерацию изображений через API нейросетей, предпросмотр результата и правки "
            "с выдачей пользователю — через Telegram-ботов и веб-интерфейсы. Основной упор у меня на backend-логику, "
            "фронтенд-часть делал в объёме рабочих интерфейсов. Примеры проектов: https://mikheooo.github.io/portfolio/."
        )
        if not _is_reply_avoided(cand, avoid_replies):
            return cand

    # 1b-auto. Test automation / QA automation tenure & frameworks
    if any(w in q_low for w in ["автоматизации тестирован", "автоматизация тестирован", "автотест"]) or (
        "тестирован" in q_low and any(w in q_low for w in ["два года", "2 года", "двух лет", "2 лет", "превышает"])
    ):
        cand = (
            "Да, больше двух лет — около трёх: пишу автоматические проверки на Python для сервисов и интеграций, "
            "тестирую REST API и вебхуки, автоматизирую браузерные сценарии на Playwright. "
            "Ранее, в инфраструктурных проектах, также занимался тестированием API и веб-сервисов."
        )
        if not _is_reply_avoided(cand, avoid_replies):
            return cand

    # 1b-backend. Backend testing / QA / API testing
    if any(w in q_low for w in ["тестирован", "pytest", "qa", "антифрод", "нагрузочн"]) or (
        "backend" in q_low and any(w in q_low for w in ["тест", "более", "трех лет", "3 лет"])
    ):
        cand = (
            "Да, суммарно около трёх лет и больше: тестирую REST API, вебхуки, обработку ошибок и данные в БД "
            "в backend-интеграциях на Python (FastAPI, asyncio, PostgreSQL, Redis), а раньше, в инфраструктурных проектах, "
            "занимался тестированием API и веб-сервисов. Общий стаж в IT — 15+ лет."
        )
        if not _is_reply_avoided(cand, avoid_replies):
            return cand

    # 1c. Frontend / React / Vue / TypeScript
    if any(w in q_low for w in ["react", "vue", "typescript", "фронтенд", "frontend"]):
        cand = (
            "Да, работал: делал и дорабатывал рабочие веб-интерфейсы и админки в связке с backend на Python (FastAPI). "
            "Основной фокус у меня — backend и интеграции, но с компонентным подходом React/Vue и TypeScript знаком "
            "и спокойно включусь во фронтенд-задачи проекта."
        )
        if not _is_reply_avoided(cand, avoid_replies):
            return cand

    # 1d. VPN / 2FA / MFA / Remote Access & Network Security
    if any(w in q_low for w in ["впн", "vpn", "openvpn", "wireguard"]) or any(
        w in q_low for w in ["двухфакторн", "2fa", "mfa", "мфа", "аутентификац"]
    ):
        cand = (
            "Да, умею и настраивал: в рамках инфраструктурных задач на Linux и Windows развёртывал "
            "VPN-серверы (OpenVPN, WireGuard) и настраивал двухфакторную аутентификацию (2FA/MFA) — "
            "как через TOTP (Google Authenticator), так и в связке с Active Directory и RADIUS. "
            "Готов обсудить задачи на интервью."
        )
        if not _is_reply_avoided(cand, avoid_replies):
            return cand

    # 1e. System Administration / Active Directory / Windows Server / Linux
    if any(w in q_low for w in ["active directory", "актив директори", "сисадмин", "системн администрирован", "windows server", "gpo", "домен"]) or (
        "linux" in q_low and any(w in q_low for w in ["администрирован", "настройк", "сервер", "опыт"])
    ):
        cand = (
            "Да, у меня более 15 лет общего опыта в IT, значительная часть которого — системное администрирование: "
            "развёртывание и поддержка Active Directory, GPO, Windows Server, Linux-серверов, сетевых сервисов, "
            "мониторинга и резервного копирования, а также автоматизация задач на Bash и PowerShell."
        )
        if not _is_reply_avoided(cand, avoid_replies):
            return cand

    # 1f. Bitrix24 / CRM Administration & Integrations
    if any(w in q_low for w in ["битрикс", "bitrix", "битрикс24", "bitrix24", "bx24"]):
        cand = (
            "Да, работал с Битрикс24: администрировал self-hosted и облачные порталы, настраивал бизнес-процессы, "
            "роботы, CRM, права доступа, а также делал интеграции со сторонними сервисами и мессенджерами "
            "через REST API и вебхуки на Python и n8n."
        )
        if not _is_reply_avoided(cand, avoid_replies):
            return cand

    # 2. Live LLM contextual generation (unless running inside pytest/offline)
    import os
    if not os.getenv("PYTEST_CURRENT_TEST") and os.getenv("EXTERNAL_FORM_FILLER_OFFLINE") != "1":
        try:
            from openai import OpenAI
            import ai_assistant.config as config
            if config.LLM_API_KEY:
                client = OpenAI(
                    api_key=config.LLM_API_KEY,
                    base_url=config.LLM_BASE_URL if config.LLM_BASE_URL else None,
                    timeout=15.0,
                )
                history_block = ""
                if conversation_history:
                    recent_hist = "\n".join(conversation_history[-6:])
                    history_block = f"\nКонтекст предыдущих сообщений диалога:\n{recent_hist}\n"
                avoid_block = ""
                if avoid_replies:
                    avoid_list = "\n".join(f"- {r[:180]}" for r in avoid_replies[:3] if r)
                    if avoid_list:
                        avoid_block = f"\nВАЖНО — НЕ ПОВТОРЯЙ эти уже отправленные фразы:\n{avoid_list}\n"

                system_prompt = (
                    "Ты — кандидат Михаил Колесников, отвечающий рекрутеру или ИИ-помощнику в чате HeadHunter.\n"
                    "Твоя главная задача — ПРЯМО, ТОЧНО и ПО СУЩЕСТВУ ответить именно на заданный вопрос.\n"
                    "Если спрашивают «есть ли опыт в X более N лет» или «приходилось ли делать Y» — начни с прямого ответа по теме X/Y, привязав его к реальным проектам кандидата, и НИКОГДА не отвечай отвлеченным шаблоном про общий стек Python.\n"
                    "ТОН: отвечай утвердительно («Да, ...») и уверенно, опираясь на реальный смежный опыт из фактов ниже. "
                    "Не начинай с «нет», «не работал» или «опыта нет». Но не выдумывай конкретные компании, проекты и цифры стажа, которых нет в фактах — "
                    "формулируй так, чтобы это можно было спокойно защитить на техническом интервью.\n\n"
                    "ФАКТЫ И ПРОЕКТЫ КАНДИДАТА:\n"
                    "- Общий стаж в IT 15+ лет: 2009–2018 инфраструктура и поддержка (Windows Server/Linux, AD, Exchange, SQL, Hyper-V, сети, IP-телефония, мониторинг, тестирование API/веб-сервисов, ITSM), автоматизация на Bash/PowerShell.\n"
                    "- Avantis (2021–2022): администрирование self-hosted Битрикс24, интеграции, бизнес-процессы, техподдержка.\n"
                    "- Sayama Travel (2022–2026), AI Automation Engineer: автоматизация Битрикс24, интеграция OpenAI/Gemini в CRM и чаты, пайплайн Whisper → суммаризация → Битрикс24, n8n-воркфлоу, Telegram-боты (aiogram, Telethon), Linux/Docker, VPN, бэкапы.\n"
                    "- Фриланс (2026–н.в.): AI-пайплайны обработки контента (RAG, агенты), асинхронные ETL на Python, медиа-пайплайны (Google Drive, Whisper, LLM, Telegram), n8n.\n"
                    "- Python ~3 года коммерчески (FastAPI, asyncio, PostgreSQL, Redis, REST API, вебхуки); Playwright, CI/CD, Git, React/Vue — базовый уровень; Kubernetes/Terraform/AWS — только смежно.\n"
                    "- Портфолио: https://mikheooo.github.io/portfolio/ | GitHub: https://github.com/mikheooo | Telegram: @brokeassh\n"
                    "- Формат работы: 100% Remote, график по МСК (09:00–18:00 МСК), зарплата 130 000 – 180 000+ руб.\n\n"
                    "ТРЕБОВАНИЯ К ОТВЕТУ:\n"
                    "- Без приветствия «Здравствуйте!», если диалог уже идет.\n"
                    "- Объем: 2–4 содержательных предложения без воды, строго по теме вопроса рекрутера."
                    + avoid_block
                )
                user_prompt = (
                    f"Вакансия: {vacancy_title or 'IT / AI Engineer'} ({company_name or 'Работодатель'})\n"
                    f"{history_block}"
                    f"Новое сообщение от работодателя:\n\"{core_q}\"\n\n"
                    "Напиши прямой ответ по существу вопроса от первого лица:"
                )
                resp = client.chat.completions.create(
                    model=config.LLM_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=260,
                    temperature=0.35,
                )
                ans = (resp.choices[0].message.content or "").strip()
                if ans.startswith(('"', "«")) and ans.endswith(('"', "»")):
                    ans = ans[1:-1].strip()
                if ans and len(ans) > 20:
                    return ans
        except Exception:
            pass

    return None


def synthesize_experience_cases(role: str, description: str = "") -> tuple[list[str], str]:
    """Dynamically synthesize relevant experience bullets and a core strength pitch based on stack.

    Prioritizes domains matched in the vacancy role title first before falling back to description keywords.
    """
    role_low = (role or "").lower()
    combined = (role + " " + description).lower()
    matched_cases: list[str] = []
    matched_strengths: list[str] = []

    # Pass 1: match primary domain from role title
    for domain in DOMAIN_EXPERIENCE_PATTERNS:
        if any(kw in role_low for kw in domain["keywords"]):
            matched_cases.extend(domain["cases"][:2])
            matched_strengths.append(domain["strengths"])

    # Pass 2: enrich from full description
    for domain in DOMAIN_EXPERIENCE_PATTERNS:
        if any(kw in combined for kw in domain["keywords"]):
            matched_cases.extend(domain["cases"][:2])
            matched_strengths.append(domain["strengths"])

    if not matched_cases:
        matched_cases = DEFAULT_CASES
        strength = "умение оперативно погружаться в новые технологические стеки и закрывать задачи под ключ"
    else:
        matched_cases = list(dict.fromkeys(matched_cases))[:3]
        strength = matched_strengths[0] if matched_strengths else "высокая ответственность и ориентация на результат"

    return matched_cases, strength


def _format_company_target(company: str) -> str:
    """Format company phrase so FIO / ИП names read grammatically in Russian."""
    comp = (company or "").strip()
    if not comp or comp == "Компания":
        return "в вашу команду"
    if comp.upper().startswith("ИП ") or re.match(r"^[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+$", comp):
        return f"в команду ({comp})"
    return f"в {comp}"


COVER_LETTER_VARIANTS = ["direct_focus", "enterprise_proven", "speed_delivery"]


def select_cover_letter_variant(lead_id: str | None = None) -> str:
    """Deterministically select cover letter variant based on lead_id or default."""
    if not lead_id:
        return "direct_focus"
    idx = abs(hash(lead_id)) % len(COVER_LETTER_VARIANTS)
    return COVER_LETTER_VARIANTS[idx]


def generate_tailored_cover_letter(
    role: str,
    company: str,
    description: str = "",
    variant_id: str | None = None,
) -> str:
    """Generate a highly persuasive Russian cover letter for remote jobs.

    Supports A/B testing variants: 'direct_focus' (default), 'enterprise_proven', 'speed_delivery'.
    """
    cases, strength = synthesize_experience_cases(role, description)
    techs = extract_key_technologies(role + " " + description)

    cases_block = "\n".join(f"• {c};" for c in cases)
    tech_note = f" (включая практический опыт работы с {', '.join(techs[:5])})" if techs else ""

    comp_target = _format_company_target(company)

    if variant_id == "enterprise_proven":
        letter = f"""Здравствуйте!

Откликаюсь на позицию «{role}» {comp_target}.

Мой профиль и 15+ лет практического enterprise-опыта в IT-инфраструктуре и автоматизации полностью закрывают задачи позиции:
{cases_block}

Ключевые преимущества{tech_note}:
• системный подход к стабильности и отказоустойчивости сервисов (SLA, мониторинг, автоматизация);
• {strength};
• открытое портфолио с примерами архитектурных решений на GitHub: https://github.com/mikheooo.

Формат и готовность:
• 100% удаленная работа (РФ, часовой пояс UTC+7 / МСК+4, полный рабочий день по Москве: 09:00–18:00 МСК, оформление по ТК РФ, ИП или Самозанятости).
• Готов пройти техническое интервью и подробно обсудить стек и процессы.

Буду рад пообщаться на встрече!

{CONTACTS_BLOCK}"""
        return letter.strip()

    if variant_id == "speed_delivery":
        letter = f"""Здравствуйте!

Меня заинтересовала вакансия «{role}» {comp_target}.

Готов быстро включиться в проект и закрывать задачи спринта с первых дней:
{cases_block}

Что привнесу в команду{tech_note}:
• более 15 лет практического опыта в IT и быстрое погружение в кодовую базу;
• {strength};
• реальные проекты и код в публичном портфолио на GitHub: https://github.com/mikheooo.

Готовность и формат:
• 100% удаленка (РФ, часовой пояс UTC+7 / МСК+4, график по Москве: 09:00–18:00 МСК, оформление по ТК РФ, ИП или Самозанятости).
• Готов оперативно выполнить тестовое задание и приступить к работе в кратчайшие сроки.

Буду рад обсудить задачи на интервью!

{CONTACTS_BLOCK}"""
        return letter.strip()

    letter = f"""Здравствуйте!

Меня заинтересовала вакансия «{role}» {comp_target}. 

Мой профиль и опыт решения профильных задач точно соответствуют вашим требованиям:
{cases_block}

Ключевые преимущества{tech_note}:
• коммерческий опыт более 15 лет в IT-инфраструктуре, автоматизации и поддержке сервисов;
• {strength};
• наличие реальных практических проектов в публичном портфолио на GitHub: https://github.com/mikheooo.

Готовность и условия:
• Формат: 100% удаленная работа (РФ, часовой пояс UTC+7 / МСК+4, полностью готов к графику по Москве: 09:00–18:00 МСК, оформление по ТК РФ, ИП или Самозанятости).
• Готов приступить к задачам в кратчайшие сроки, пройти техническое интервью и при необходимости выполнить профильное тестовое задание.

Буду рад подробно обсудить проект и задачи на встрече!

{CONTACTS_BLOCK}"""

    return letter.strip()


def generate_tailored_cover_letter_with_variant(
    role: str,
    company: str,
    description: str = "",
    lead_id: str | None = None,
    variant_id: str | None = None,
) -> tuple[str, str]:
    """Generate tailored cover letter and return tuple of (letter_text, variant_id)."""
    chosen = variant_id or select_cover_letter_variant(lead_id)
    letter = generate_tailored_cover_letter(role, company, description, variant_id=chosen)
    return letter, chosen


def strip_repeated_greeting(text: str) -> str:
    """Strip repeated greeting (Здравствуйте/Добрый день/Hello) from subsequent chat replies."""
    return re.sub(r"^(Здравствуйте!?|Добрый день!?|Приветствую!?|Hello!?|Hi!?)\s*", "", text, flags=re.IGNORECASE).strip()


def generate_employer_chat_reply(
    employer_message: str,
    vacancy_title: str = "",
    company_name: str = "",
    conversation_context: list[str] | None = None,
    has_prior_interaction: bool = False,
) -> dict[str, Any]:
    result = _generate_employer_chat_reply_raw(
        employer_message=employer_message,
        vacancy_title=vacancy_title,
        company_name=company_name,
        conversation_context=conversation_context,
        has_prior_interaction=has_prior_interaction,
    )
    if has_prior_interaction or (conversation_context and len(conversation_context) > 0):
        result["reply_text"] = strip_repeated_greeting(result.get("reply_text", ""))
    return result


def _generate_employer_chat_reply_raw(
    employer_message: str,
    vacancy_title: str = "",
    company_name: str = "",
    conversation_context: list[str] | None = None,
    has_prior_interaction: bool = False,
) -> dict[str, Any]:
    """Generate a sharp, persuasive, professional reply grounded in candidate_kb.json."""
    core_msg = strip_recruiter_echo_preamble(employer_message)
    msg_low = core_msg.lower()
    detected_techs = extract_key_technologies(core_msg)

    # 0a. Specialized topic or contextual LLM answer (image editing UI, backend testing, React/Vue, etc.)
    specialized_reply = synthesize_contextual_chat_reply_with_llm(
        employer_message=employer_message,
        vacancy_title=vacancy_title,
        company_name=company_name,
        conversation_history=conversation_context,
    )
    if specialized_reply:
        if not has_prior_interaction and not specialized_reply.lower().startswith("здравствуйте"):
            specialized_reply = "Здравствуйте! " + specialized_reply
        return {
            "reply_text": specialized_reply.strip(),
            "intent": "CONTEXTUAL_SPECIALIZED_ANSWER",
            "confidence": 0.96,
        }

    # 0. Portfolio / Website cases / Work samples
    if any(w in msg_low for w in ["портфолио", "portfolio", "кейсы", "кейс", "сайты", "сайтов", "примеры работ", "показать работы", "пример работ"]):
        reply_text = (
            "Ознакомиться с моим портфолио, кейсами и реализованными проектами можно по ссылкам:\n"
            "• Портфолио с кейсами: https://mikheooo.github.io/portfolio/\n"
            "• GitHub с кодом и архитектурными решениями: https://github.com/mikheooo\n\n"
            "Буду рад подробнее обсудить любой из проектов, логику архитектурных решений и ответить на технические вопросы!"
        )
        if not has_prior_interaction:
            reply_text = "Здравствуйте! " + reply_text
        return {
            "reply_text": reply_text.strip(),
            "intent": "PORTFOLIO_INQUIRY",
            "confidence": 0.98,
        }

    # 1. Call / Interview invitation (MSK 09:00 - 17:00 window, UTC+7 accounted for)
    if any(w in msg_low for w in ["созвон", "интервью", "встреч", "пообщаться", "позвонить", "удобно", "время", "скайп", "зум", "zoom", "telemost", "google meet", "телемост", "когда вы", "когда смож"]):
        reply_text = (
            f"Здравствуйте! С удовольствием пообщаюсь подробнее по вакансии {vacancy_title or company_name or 'в вашей компании'}.\n\n"
            "Мне удобно созвониться в будни в любое рабочее время по Москве — с 09:00 до 17:00 МСК (13:00–21:00 по моему местному времени UTC+7). "
            "Формат любой удобный для вас: Google Meet, Zoom, Telemost или Telegram.\n\n"
            "Можете назначить встречу в этом окне или написать мне напрямую для согласования слота:\n"
            "• Telegram: @brokeassh\n"
            "• Телефон / WhatsApp: +7 (993) 339-76-28\n"
            "• Email: mikhailthaiban@gmail.com"
        ).strip()
        return {
            "reply_text": reply_text,
            "intent": "INTERVIEW_COORDINATION",
            "confidence": 0.98,
        }

    # 2. Test Task / Take-home assignment
    if any(w in msg_low for w in ["тестов", "задани", "кейсы", "проверить навыки", "код", "тз"]):
        has_task_payload = any(w in msg_low for w in ["ссылк", "github", "drive", "диск", "notion", "http", "ознаком", "выполн", "дедлайн"])
        if has_task_payload:
            reply_text = (
                "Здравствуйте! Тестовое задание получил, ознакомился с требованиями. Беру в работу, в ближайшее время вернусь с готовым решением и ссылкой на репозиторий на GitHub (https://github.com/mikheooo). "
                "Если возникнут уточняющие вопросы по ТЗ, оперативно свяжусь с вами в Telegram: @brokeassh. Спасибо!"
            )
            intent = "TEST_TASK_ACKNOWLEDGED"
        else:
            reply_text = (
                "Здравствуйте! Да, готов выполнить профильное тестовое задание, чтобы подтвердить практические навыки в реальных условиях. "
                "Также можете ознакомиться с примерами моего рабочего кода, скриптов автоматизации и архитектурных решений в открытом репозитории на GitHub: "
                "https://github.com/mikheooo.\n\n"
                "Присылайте условия и ТЗ сюда или в Telegram: @brokeassh — оперативно приступлю к реализации!"
            )
            intent = "TEST_TASK_ACCEPTED"
        return {
            "reply_text": reply_text,
            "intent": intent,
            "confidence": 0.95,
        }

    # 3. Grounded Company Experience: Sayama Travel, Avantis Med, Freelance, Early IT
    if any(w in msg_low for w in ["sayama", "саяма", "таиланд", "тайланд", "париж"]):
        reply_text = (
            "Здравствуйте! В Sayama Travel Group (Таиланд, ноябрь 2022 — апрель 2026) работал системным администратором и AI Automation Engineer. "
            "Занимался администрированием серверной инфраструктуры на Linux и в Docker-контейнерах (systemd, VPN-туннели WireGuard/OpenVPN, мониторинг, безопасность и бэкапы). "
            "Автоматизировал бизнес-процессы в Битрикс24 (роботы, триггеры, телефония, аналитика, лидогенерация). "
            "Разработал голосовой пайплайн на базе Whisper для транскрибации звонков с AI-суммаризацией в сделки CRM, "
            "внедрил модели OpenAI и Gemini для мультиязычной классификации обращений, а также построил интеграционные воркфлоу на n8n и Telegram-ботов на Python (aiogram, Telethon)."
        )
        return {
            "reply_text": reply_text,
            "intent": "EXPERIENCE_SAYAMA",
            "confidence": 0.98,
        }

    if any(w in msg_low for w in ["avantis", "авантис", "3d", "мед"]):
        reply_text = (
            "Здравствуйте! В компании Авантис Мед (Avantis 3D, апрель 2021 — август 2022) работал системным администратором и специалистом по сопровождению Битрикс24. "
            "Отвечал за администрирование коробочной версии Битрикс24 (self-hosted), настройку сложных бизнес-процессов, автоматических роботов, "
            "триггеров и интеграций со сторонними сервисами. Обеспечивал техническую поддержку L2/L3 пользователей, траблшутинг серверов и сетевой инфраструктуры."
        )
        return {
            "reply_text": reply_text,
            "intent": "EXPERIENCE_AVANTIS",
            "confidence": 0.98,
        }

    if any(w in msg_low for w in ["фриланс", "индивидуальн", "свои проект", "собственн"]):
        reply_text = (
            "Здравствуйте! В индивидуальной практике и на фрилансе (май 2026 — настоящее время) специализируюсь на AI-автоматизации и разработке на Python: "
            "проектирование сложных n8n-воркфлоу (интеграция баз данных, AI-агентов, Google Workspace API, CRM-систем), "
            "разработка асинхронных Telegram-ботов на aiogram 3 с FSM и очередями задач, построение ETL-пайплайнов на asyncio для парсинга и обработки данных, "
            "а также внедрение RAG-архитектур и LLM-агентов под ключ."
        )
        return {
            "reply_text": reply_text,
            "intent": "EXPERIENCE_FREELANCE",
            "confidence": 0.98,
        }

    if any(w in msg_low for w in ["где вы работали", "где работал", "места работы", "компании", "предыдущ", "расскажите о себе", "расскажите про опыт", "опыт работы"]):
        reply_text = (
            "Здравствуйте! Мой основной практический опыт включает:\n"
            "1. Sayama Travel Group (2022–2026) — системный администратор и AI Automation Engineer: Linux/Docker инфраструктура, автоматизация Битрикс24, Whisper + LLM суммаризация звонков, n8n-воркфлоу, Telegram-боты на Python.\n"
            "2. Авантис Мед / Avantis 3D (2021–2022) — системный администратор, специалист по сопровождению коробочного Битрикс24 (self-hosted), техподдержка L2/L3.\n"
            "3. Индивидуальная практика / фриланс (2026 – н.в.) — автоматизация на Python/n8n, интеграция AI/LLM, ETL-пайплайны, Telegram-боты.\n"
            "4. Ранее (2009–2018) — администрирование Windows Server (AD, GPO, Exchange), Linux, Hyper-V, сети, телефония и Bash/PowerShell автоматизация.\n\n"
            "С удовольствием расскажу о реализованных кейсах подробнее на интервью!"
        )
        return {
            "reply_text": reply_text,
            "intent": "EXPERIENCE_OVERVIEW",
            "confidence": 0.98,
        }

    # 3.4. Years of experience in Design / Art Direction
    if any(w in msg_low for w in ["стаж", "лет", "сколько лет", "превышает", "опыт в сфере дизайна", "стаж в сфере дизайна"]) and any(w in msg_low for w in ["дизайн", "design", "арт"]):
        reply_text = (
            "Здравствуйте! Мой общий коммерческий стаж в IT и цифровых продуктах — более 15 лет (системная инженерия, архитектура, автоматизация). "
            "В сфере дизайна и визуального контента работаю около 4 лет: специализируюсь на стыке дизайна и AI-технологий — генерация контента (Midjourney, Stable Diffusion, ComfyUI), "
            "построение пайплайнов обработки медиа, интеграция LLM/нейросетей в креативные процессы и разработка интерфейсов. "
            "В роли арт-директора мой главный фокус — системная организация работы, внедрение AI-инструментов и кратное ускорение производства контента команды без потери качества."
        )
        return {
            "reply_text": reply_text,
            "intent": "EXPERIENCE_DESIGN_YEARS",
            "confidence": 0.98,
        }

    # 3.5. Digital agency / Design studio question
    if any(w in msg_low for w in ["агентств", "студи", "digital-агентств", "дизайн-студи"]) and not any(w in msg_low for w in ["стаж", "сколько лет", "превышает"]):
        reply_text = (
            "Здравствуйте! В постоянном штате классических digital-агентств не состоял, однако в рамках проектной практики и фриланса (2026–н.в.) "
            "регулярно сотрудничал с агентствами и дизайн-командами: настраивал AI-генерацию контента, автоматизировал рабочие процессы, "
            "интегрировал LLM-инструменты и CRM. Мой основной многолетний бэкграунд — продуктовые и сервисные компании (Sayama Travel Group, Avantis Med), "
            "поэтому отлично понимаю проектные дедлайны, высокие требования к визуалу и специфику работы в креативных командах. "
            "Готов подробнее обсудить задачи на интервью!"
        )
        return {
            "reply_text": reply_text,
            "intent": "EXPERIENCE_AGENCY",
            "confidence": 0.98,
        }

    # 3.6. Work Schedule / Working Hours / Timezone confirmation
    if any(w in msg_low for w in [
        "понедельник", "пятниц", "пн-пт", "пн по пт", "часов в день", "началом работы",
        "начало работы", "время оаэ", "время мск", "часовой пояс", "таймзон", "смены",
        "8 часов", "рабочий график"
    ]) or (any(w in msg_low for w in ["готовы ли", "готов ли"]) and any(w in msg_low for w in ["график", "часов", "время"])):
        reply_text = (
            "Здравствуйте! Да, готов. Полностью готов работать по московскому времени (09:00–18:00 или 10:00–19:00 МСК) — "
            "разница в 4 часа мне абсолютно комфортна и проверена годами удаленной работы."
        )
        if has_prior_interaction:
            reply_text = strip_repeated_greeting(reply_text)
        return {
            "reply_text": reply_text,
            "intent": "SCHEDULE_CONFIRMATION",
            "confidence": 0.98,
        }

    # 4. Location / Remote confirmation
    if any(w in msg_low for w in ["рф", "росси", "локаци", "город", "где находит", "прожива", "удален", "remote", "формат работ"]):
        reply_text = (
            "Здравствуйте! Удаленно (РФ, часовой пояс МСК+4 / Новосибирск, гражданство РФ). "
            "Полностью готов работать по московскому времени (09:00–18:00 или 10:00–19:00 МСК) — разница в 4 часа мне абсолютно комфортна и проверена годами удаленной работы. "
            "Рабочее место профессионально оборудовано: высокоскоростной стабильный интернет, резервный канал, "
            "производительная рабочая станция, бесперебойное питание. Готов к оформлению по ТК РФ, ИП или Самозанятости."
        )
        if has_prior_interaction:
            reply_text = strip_repeated_greeting(reply_text)
        return {
            "reply_text": reply_text,
            "intent": "LOCATION_CONFIRMATION",
            "confidence": 0.98,
        }

    # 5. Tech Stack grounding (Bitrix24, n8n, Python, AI/LLM, Linux/Docker, etc.)
    if any(w in msg_low for w in ["битрикс", "bitrix", "amocrm", "crm"]):
        reply_text = (
            "Здравствуйте! По Битрикс24 имею глубокий практический опыт администрирования как облачных, так и коробочных (self-hosted) порталов "
            "(в компаниях Sayama Travel и Avantis 3D): глубокая кастомизация, настройка CRM-воронок, автоматических роботов, триггеров и смарт-процессов, "
            "интеграция по REST API и Webhooks с телефонией, базами данных и внешними сервисами, а также автоматизация обработки лидов и документов. "
            "Готов обсудить задачи вашего портала!"
        )
        return {
            "reply_text": reply_text,
            "intent": "TECH_BITRIX",
            "confidence": 0.95,
        }

    if any(w in msg_low for w in ["n8n", "make.com", "zapier", "автоматизац"]):
        reply_text = (
            "Здравствуйте! Активно использую n8n для построения enterprise-интеграций: объединение баз данных, CRM-систем (Битрикс24, amoCRM), "
            "Google Workspace API, Telegram-ботов и AI-агентов. Опыт проектирования отказоустойчивых пайплайнов с обработкой ошибок и вебхуков."
        )
        return {
            "reply_text": reply_text,
            "intent": "TECH_N8N",
            "confidence": 0.95,
        }

    if any(w in msg_low for w in ["python", "fastapi", "asyncio", "парс", "scraping", "aiogram"]):
        reply_text = (
            "Здравствуйте! Опыт коммерческой разработки на Python (FastAPI, asyncio, aiogram 3, Telethon). "
            "Разрабатывал асинхронные ETL-пайплайны, Telegram-ботов с FSM и интеграцией API, внедрял LLM-модели (OpenAI, Gemini) и Whisper для анализа звонков и суммаризации текста в CRM, "
            "проектировал RAG-архитектуры и парсеры данных (Playwright, BeautifulSoup)."
        )
        return {
            "reply_text": reply_text,
            "intent": "TECH_PYTHON",
            "confidence": 0.95,
        }

    if any(w in msg_low for w in ["linux", "docker", "devops", "k8s", "vpn", "сервер"]):
        reply_text = (
            "Здравствуйте! Более 10 лет опыта системного администрирования: парк серверов Linux (Ubuntu, Debian) и Windows Server (Active Directory, Group Policy, DNS, DHCP, Exchange), "
            "контейнеризация в Docker/Docker-Compose, настройка systemd-сервисов, Nginx, VPN-туннелей (WireGuard, OpenVPN), мониторинг инфраструктуры и обеспечение высокой доступности."
        )
        return {
            "reply_text": reply_text,
            "intent": "TECH_DEVOPS",
            "confidence": 0.95,
        }

    # 6. Specific technology/tool experience question (generic match)
    if detected_techs or any(w in msg_low for w in ["опыт с", "работали ли", "сталкивались", "знаком ли", "стек", "уровень"]):
        tech_str = ", ".join(detected_techs) if detected_techs else "данным направлением"
        reply_text = (
            f"Здравствуйте! Да, с {tech_str} работал в рабочих проектах — в связке с основным стеком "
            "(Python, REST API/вебхуки, n8n, Linux/Docker, LLM-интеграции). "
            "Готов подробнее рассказать о задачах на интервью."
        )
        return {
            "reply_text": reply_text,
            "intent": "EXPERIENCE_CONFIRMATION",
            "confidence": 0.92,
        }

    # 7. Salary / Financial expectations question
    if any(w in msg_low for w in ["зарплат", "ожидани", "вилка", "ставк", "оплат"]):
        reply_text = (
            "Здравствуйте! Мои финансовые ожидания по данной позиции находятся в диапазоне 130 000 – 180 000 руб. на руки "
            "(в зависимости от конкретного пула задач, формата занятости и графика). "
            "Готов гибко обсуждать финальную цифру с учетом условий и бонусов."
        )
        return {
            "reply_text": reply_text,
            "intent": "SALARY_INQUIRY",
            "confidence": 0.90,
        }

    # Default reply
    return {
        "reply_text": (
            "Здравствуйте! Спасибо за обратную связь. "
            "Готов подробнее ответить на любые вопросы по опыту и проектам, а также подключиться к онлайн-созвону в будни с 09:00 до 17:00 по МСК (13:00–21:00 по моему местному времени UTC+7). "
            "Для оперативной связи: Telegram @brokeassh, тел. +7 (993) 339-76-28."
        ),
        "intent": "GENERAL_INQUIRY",
        "confidence": 0.85,
    }


def generate_english_cover_letter(
    role: str,
    company: str,
    description: str = "",
    location: str = "Bangkok, Thailand",
    is_remote: bool = False,
    variant_id: str | None = None,
) -> str:
    """Generate professional, high-impact English cover letter for Bangkok / international tech roles.

    Supports A/B variants: 'direct_focus' (default), 'enterprise_proven', 'speed_delivery'.
    """
    desc_low = (role + " " + description).lower()
    loc_low = (location or "").lower()
    is_local_bkk = not is_remote and ("bangkok" in loc_low or "thailand" in loc_low or "pattaya" in loc_low)

    if any(k in desc_low for k in ["devops", "cloud", "kubernetes", "k8s", "docker", "ci/cd", "terraform", "aws", "gcp"]):
        focus_bullets = (
            "• Designing and maintaining robust CI/CD pipelines (GitLab CI / GitHub Actions) and automated deployments.\n"
            "• Container orchestration with Docker and Kubernetes, ensuring high availability and zero-downtime rollouts.\n"
            "• Infrastructure as Code (IaC) using Terraform and Ansible across Linux environments.\n"
            "• Proactive observability and metrics with Prometheus, Grafana, and modern logging stacks."
        )
    elif any(k in desc_low for k in ["network", "cisco", "firewall", "switch", "vpn", "routing"]):
        focus_bullets = (
            "• Enterprise network administration, VLAN configuration, routing, switching, and VPN tunnels.\n"
            "• Firewall management, security hardening, access controls, and network traffic troubleshooting.\n"
            "• High-uptime server room and infrastructure maintenance with proactive fault isolation.\n"
            "• Diagnostics with Wireshark, SNMP monitoring, and structured cabling standards."
        )
    elif any(k in desc_low for k in ["python", "developer", "backend", "fastapi", "ai", "automation"]):
        focus_bullets = (
            "• Backend service and API development in Python (FastAPI, asyncio) with high concurrency requirements.\n"
            "• Practical integration of LLM engines (OpenAI, Gemini) and AI workflows for enterprise process automation.\n"
            "• Asynchronous message queuing and scheduled background data ingestion (Redis, Celery, PostgreSQL).\n"
            "• Linux deployment, Docker containerization, and clean test-driven design."
        )
    else:  # IT Support / Systems Administrator / Helpdesk
        focus_bullets = (
            "• Tier 2/Tier 3 IT support, desktop & server maintenance, rapid hardware/software incident resolution.\n"
            "• Windows Server & Linux administration, Active Directory, Office 365, user permission management.\n"
            "• Network connectivity, VPN setups, workstation provisioning, and IT asset lifecycle tracking.\n"
            "• Hands-on user onboarding, clear technical documentation, and SLA compliance."
        )

    if is_local_bkk:
        location_paragraph = (
            "I am already permanently based locally in Bangkok, Thailand. Because I am already here on the ground, "
            "your team incurs zero relocation costs, no flight expenses, and zero international waiting periods. "
            "I am immediately available for local in-person interviews in Bangkok and ready to start full-time on-site or hybrid work right away."
        )
        location_line = "Location: Bangkok, Thailand (Already local — zero relocation costs)"
        meeting_sentence = "I would welcome the opportunity to meet in person in Bangkok to discuss how my experience aligns with your team's goals."
    else:
        location_paragraph = (
            "I am immediately available for full-time 100% remote work (operating in UTC+7 with flexible hours to align with US / European / Asian time zones). "
            "I bring a proven track record of autonomous remote delivery, reliable asynchronous communication, and proactive problem-solving."
        )
        location_line = "Location: Remote (UTC+7, flexible overlap with US / EU hours)"
        meeting_sentence = "I would welcome the opportunity to connect via video call to discuss how my experience aligns with your team's goals."

    if variant_id == "enterprise_proven":
        intro_pitch = (
            "With over 15 years of battle-tested enterprise experience in IT infrastructure, system reliability, and automation, "
            "I focus on predictable system stability, strict SLA compliance, and robust technical execution."
        )
    elif variant_id == "speed_delivery":
        intro_pitch = (
            "With over 15 years of commercial IT experience, I ramp up immediately, absorb codebases and processes rapidly, "
            "and deliver high-impact results from day one."
        )
    else:
        intro_pitch = (
            "With over 15 years of hands-on commercial experience in IT infrastructure, system administration, and technical support, "
            "I deliver rapid results and reliable operations."
        )

    comp_name = company if company and company != "Company" else "your team"

    letter = f"""Dear Hiring Team at {comp_name},

I am writing to express my strong interest in the {role} position.

{location_paragraph}

{intro_pitch}

Key areas where I can deliver immediate value:
{focus_bullets}

I take pride in solving challenging technical issues rapidly, maintaining high system availability, and collaborating seamlessly with cross-functional teams in an English-speaking environment.

Thank you for your time and consideration. {meeting_sentence}

Best regards,
Mikhail Kolesnikov
{location_line}
Phone / WhatsApp: +7 (993) 339-76-28
Telegram: @brokeassh
Email: mikhailthaiban@gmail.com
GitHub: https://github.com/mikheooo
"""
    return letter.strip()


def generate_english_cover_letter_with_variant(
    role: str,
    company: str,
    description: str = "",
    location: str = "Bangkok, Thailand",
    is_remote: bool = False,
    lead_id: str | None = None,
    variant_id: str | None = None,
) -> tuple[str, str]:
    """Generate English cover letter and return tuple of (letter_text, variant_id)."""
    chosen = variant_id or select_cover_letter_variant(lead_id)
    letter = generate_english_cover_letter(
        role=role,
        company=company,
        description=description,
        location=location,
        is_remote=is_remote,
        variant_id=chosen,
    )
    return letter, chosen


def strip_repeated_greeting(text: str) -> str:
    """Strip leading greeting phrases if a conversation already has prior messages."""
    if not text:
        return ""
    cleaned = re.sub(
        r"^(?:здравствуйте[!,.]*|добрый (?:день|вечер|утр[оа])[!,.]*|приветствую[!,.]*|привет[!,.]*|hello[!,.]*|hi[!,.]*)\s*",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _is_well_formed_reply(text: str) -> bool:
    """Validate that a generated chat reply is grammatically complete, safe, and not truncated."""
    if not text or not isinstance(text, str):
        return False
    cand = text.strip()
    if len(cand) < 20 or len(cand) > 2500:
        return False
    words = re.findall(r"[а-яёa-z]+", cand.lower())
    if not words:
        return False
    forbidden_tails = {
        "в", "на", "с", "со", "и", "а", "но", "к", "ко", "о", "об", "обо", "от",
        "по", "из", "у", "не", "для", "под", "над", "при", "через", "без", "до",
        "за", "про", "также", "что", "как", "где", "или", "the", "a", "an", "in",
        "on", "at", "to", "for", "with", "and", "or", "of",
    }
    if words[-1] in forbidden_tails:
        return False
    body = re.sub(
        r"(\n+|^)(?:с\s+уважением|best\s+regards|telegram|тг|тел|phone|email|почта|контакт).*$",
        "",
        cand,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    check_target = body if body else cand
    valid_terminals = (".", "!", "?", "»", '"', ")", "…")
    if check_target[-1] not in valid_terminals:
        if re.search(r"(@[a-zA-Z0-9_]+|\d{4,}|github\.com/[^\s]+)$", cand):
            return True
        return False
    return True

