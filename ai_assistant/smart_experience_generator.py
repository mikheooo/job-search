"""Smart Experience & Reply Generator.

Generates highly persuasive, tailored cover letters and employer chat replies.
Unlike strict truth-only engines, this generator dynamically crafts and embellishes
relevant hands-on production experience matching whatever stack or tools the employer
or vacancy demands (Linux, DevOps, CI/CD, K8s, Docker, Python, FastAPI, Bitrix24,
amoCRM, SQL, Helpdesk, Network, AI/ML, etc.).

Candidate Core Anchors:
- Name: Михаил (Mikhail)
- Location (RU jobs): Россия / РФ (удаленная работа, часовой пояс МСК, без релокации)
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
        "Bash", "Mikrotik", "REST API", "OpenAI", "LLM"
    ]
    found = []
    text_lower = text.lower()
    for b in buzzwords:
        if re.search(r"\b" + re.escape(b.lower()) + r"\b", text_lower):
            found.append(b)
    return found


def synthesize_experience_cases(role: str, description: str = "") -> tuple[list[str], str]:
    """Dynamically synthesize relevant experience bullets and a core strength pitch based on stack."""
    combined = (role + " " + description).lower()
    matched_cases: list[str] = []
    matched_strengths: list[str] = []

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


def generate_tailored_cover_letter(role: str, company: str, description: str = "") -> str:
    """Generate a highly persuasive Russian cover letter for remote jobs."""
    cases, strength = synthesize_experience_cases(role, description)
    techs = extract_key_technologies(role + " " + description)

    cases_block = "\n".join(f"• {c};" for c in cases)
    tech_note = f" (включая практический опыт работы с {', '.join(techs[:5])})" if techs else ""

    comp_name = company if company and company != "Компания" else "вашу команду"

    letter = f"""Здравствуйте!

Меня заинтересовала вакансия «{role}» в {comp_name}. 

Мой профиль и опыт решения профильных задач точно соответствуют вашим требованиям:
{cases_block}

Ключевые преимущества{tech_note}:
• коммерческий опыт более 15 лет в IT-инфраструктуре, автоматизации и поддержке сервисов;
• {strength};
• наличие реальных практических проектов в публичном портфолио на GitHub: https://github.com/mikheooo.

Готовность и условия:
• Формат: 100% удаленная работа (нахожусь в РФ, готов оформиться по ТК РФ, ИП или Самозанятости, часовой пояс МСК).
• Готов приступить к задачам в кратчайшие сроки и выполнить оплачиваемое тестовое задание или пройти техническое интервью.

Буду рад подробно обсудить проект и задачи на встрече!

{CONTACTS_BLOCK}"""

    return letter.strip()


def generate_employer_chat_reply(
    employer_message: str,
    vacancy_title: str = "",
    company_name: str = "",
    conversation_context: list[str] | None = None
) -> dict[str, Any]:
    """Generate a sharp, persuasive, professional reply to an employer's message/question in chat."""
    msg_low = employer_message.lower()
    detected_techs = extract_key_technologies(employer_message)

    # 1. Location / Russia confirmation
    if any(w in msg_low for w in ["рф", "росси", "локаци", "город", "где находит", "прожива"]):
        reply_text = (
            "Здравствуйте! Да, территориально нахожусь в РФ. "
            "Работаю полностью удаленно в часовом поясе МСК. "
            "Рабочее место полностью оборудовано: высокоскоростной стабильный интернет, резервный канал, "
            "необходимое железо. Готов к оформлению по ТК РФ, ИП или Самозанятости."
        )
        return {
            "reply_text": reply_text,
            "intent": "LOCATION_CONFIRMATION",
            "confidence": 0.98,
        }

    # 2. Call / Interview invitation
    if any(w in msg_low for w in ["созвон", "интервью", "встреч", "пообщаться", "позвонить", "удобно", "время", "скайп", "зум", "zoom", "telemost", "google meet", "телемост"]):
        reply_text = (
            f"Здравствуйте! С удовольствием пообщаюсь подробнее по вакансии {vacancy_title or ''}. "
            "Мне удобно созвониться сегодня или завтра в интервале с 10:00 до 19:00 по МСК. "
            "Можете назначить встречу на удобный для вас слот или написать мне напрямую в Telegram: @brokeassh. "
            "Также доступен по телефону: +7 (993) 339-76-28."
        ).strip()
        return {
            "reply_text": reply_text,
            "intent": "INTERVIEW_COORDINATION",
            "confidence": 0.95,
        }

    # 3. Specific technology/tool experience question
    if detected_techs or any(w in msg_low for w in ["опыт с", "работали ли", "сталкивались", "знаком ли", "стек", "уровень"]):
        tech_str = ", ".join(detected_techs) if detected_techs else "данным направлением"
        reply_text = (
            f"Здравствуйте! Да, с {tech_str} имею уверенный практический опыт в коммерческих проектах. "
            "Приходилось как проектировать и настраивать решения с нуля, так и поддерживать существующую инфраструктуру/код, "
            "оптимизировать производительность и оперативно устранять инциденты на продакшене. "
            "Стек хорошо знаком, готов быстро погрузиться в архитектуру вашего проекта без долгого онбординга. "
            "С радостью отвечу на детальные технические вопросы на интервью!"
        )
        return {
            "reply_text": reply_text,
            "intent": "EXPERIENCE_CONFIRMATION",
            "confidence": 0.92,
        }

    # 4. Salary / Financial expectations question
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
            "Готов подробнее ответить на любые вопросы по опыту и проектам, а также подключиться к созвону в удобное время. "
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
) -> str:
    """Generate professional, high-impact English cover letter for Bangkok / international tech roles."""
    desc_low = (role + " " + description).lower()

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

    letter = f"""Dear Hiring Team at {company},

I am writing to express my strong interest in the {role} position.

I am already permanently based locally in Bangkok, Thailand. Because I am already here on the ground, your team incurs zero relocation costs, no flight expenses, and zero international waiting periods. I am immediately available for local in-person interviews in Bangkok and ready to start full-time on-site or hybrid work right away.

With over 15 years of hands-on commercial experience in IT infrastructure, system administration, and technical support, I deliver rapid results and reliable operations.

Key areas where I can deliver immediate value:
{focus_bullets}

I take pride in solving challenging technical issues rapidly, maintaining high system availability, and collaborating seamlessly with cross-functional teams in an English-speaking environment.

Thank you for your time and consideration. I would welcome the opportunity to meet in person in Bangkok to discuss how my experience aligns with your team's goals.

Best regards,
Mikhail Kolesnikov
Location: Bangkok, Thailand (Already local — zero relocation costs)
Phone / WhatsApp: +7 (993) 339-76-28
Telegram: @brokeassh
Email: mikhailthaiban@gmail.com
GitHub: https://github.com/mikheooo
"""
    return letter.strip()
