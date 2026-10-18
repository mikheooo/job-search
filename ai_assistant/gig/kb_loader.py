"""Candidate Knowledge Base (candidate_kb.json) loader for freelance proposals.

Pulls only facts with status == 'CONFIRMED_BY_USER'.
Protects against hallucinated claims while providing real-world proof points
from Mikhail's verified background:
- Python, scraping, ETL pipelines (Playwright, BeautifulSoup, asyncio)
- Telegram bots (aiogram, task queues, FSM)
- n8n / automation workflows (APIs, CRM, AI agents)
- Bitrix24 / CRM (business processes, robots, webhooks, telephony)
- Linux / Docker / VPN infrastructure (systemd, WireGuard, Nginx)
- AI / LLM / RAG integrations (Whisper, Gemini, OpenAI)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("gig_kb_loader")

_DEFAULT_KB_PATH = Path(__file__).resolve().parents[2] / "candidate_kb.json"

_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "telegram": ("telegram", "aiogram", "telethon", "mtproto", "бот", "bot", "fsm"),
    "scraping": ("scraping", "scraper", "парсинг", "парсер", "выгрузка", "etl", "playwright", "beautifulsoup", "asyncio"),
    "crm": ("bitrix", "bitrix24", "битрикс", "битрикс24", "crm", "amocrm", "амо", "телефония"),
    "n8n": ("n8n", "make.com", "zapier", "автоматизация", "workflow", "webhook", "albato"),
    "devops": ("linux", "docker", "vpn", "wireguard", "openvpn", "сервер", "vps", "vds", "systemd", "nginx", "деплой"),
    "ai": ("ai", "llm", "rag", "whisper", "gemini", "openai", "chatgpt", "ии", "нейросеть"),
}

# Cached confirmed facts
_CACHED_FACTS: list[dict[str, Any]] | None = None


def load_confirmed_kb_facts(kb_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load all claims with status == 'CONFIRMED_BY_USER'."""
    global _CACHED_FACTS
    if _CACHED_FACTS is not None and kb_path is None:
        return _CACHED_FACTS

    path = Path(kb_path) if kb_path else _DEFAULT_KB_PATH
    if not path.exists():
        logger.warning(f"candidate_kb.json not found at {path}")
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"Error loading candidate_kb.json: {exc}")
        return []

    claims = data.get("claims", {})
    confirmed = []
    for cid, claim in claims.items():
        if claim.get("status") == "CONFIRMED_BY_USER":
            kind = claim.get("kind", "")
            val = str(claim.get("value") or "").strip()
            if val and kind in (
                "duty",
                "project_bullet",
                "project_summary",
                "project_stack",
                "skill",
                "summary",
                "headline",
            ):
                confirmed.append({
                    "id": cid,
                    "kind": kind,
                    "value": val,
                    "lower": val.lower(),
                })

    if kb_path is None:
        _CACHED_FACTS = confirmed
    return confirmed


def find_matching_kb_facts(
    query_text_or_techs: str | list[str] | set[str],
    limit: int = 2,
    kb_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Return top 1-2 most relevant confirmed facts from KB matching the query/techs.

    Prefers rich 'duty' or 'project_bullet' entries over simple skill labels.
    """
    facts = load_confirmed_kb_facts(kb_path)
    if not facts:
        return []

    tokens: set[str] = set()
    if isinstance(query_text_or_techs, str):
        lowered = query_text_or_techs.lower()
        for domain, kw_list in _DOMAIN_KEYWORDS.items():
            if any(k in lowered for k in kw_list):
                tokens.update(kw_list)
        # Also add individual words
        for w in lowered.split():
            clean = w.strip(".,;:!?()[]\"'")
            if len(clean) > 2:
                tokens.add(clean)
    else:
        for t in query_text_or_techs:
            t_low = str(t).lower()
            tokens.add(t_low)
            for domain, kw_list in _DOMAIN_KEYWORDS.items():
                if t_low in kw_list or any(k in t_low for k in kw_list):
                    tokens.update(kw_list)

    if not tokens:
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for fact in facts:
        text = fact["lower"]
        match_count = sum(1 for tok in tokens if tok in text)
        if match_count == 0:
            continue

        score = float(match_count)
        # Weight detailed duties and project summaries higher than single words
        if fact["kind"] == "duty":
            score += 2.0
        elif fact["kind"] in ("project_bullet", "project_summary"):
            score += 2.5
        elif fact["kind"] == "project_stack":
            score += 1.5
        elif fact["kind"] == "skill":
            score += 0.5

        scored.append((score, fact))

    scored.sort(key=lambda x: -x[0])
    return [item[1] for item in scored[:limit]]


# Domain -> (query for KB matching, Russian fallback proof, English fallback proof,
#            Russian tail, English tail).  The KB facts themselves are English
# (see candidate_kb.json); only the *framing* sentence is localized so the
# reply reads naturally in the client's language without inventing anything.
_DOMAIN_TEMPLATES: dict[str, dict[str, str]] = {
    "telegram": {
        "query": "telegram aiogram bot",
        "proof_ru": "Разработка Telegram-ботов на Python (aiogram 3) с FSM, интеграцией баз данных и очередей задач.",
        "proof_en": "Python Telegram bots (aiogram 3) with FSM, database integration and task queues.",
        "tail_ru": "Развертывание под ключ на Linux-сервере в Docker с мониторингом и инструкцией.",
        "tail_en": "Turn-key deployment on a Linux server in Docker with monitoring and documentation.",
    },
    "scraping": {
        "query": "python scraping etl",
        "proof_ru": "Построение асинхронных Python ETL-пайплайнов с обходом ограничений и нормализацией данных.",
        "proof_en": "Asynchronous Python ETL pipelines with rate-limit handling and data normalization.",
        "tail_ru": "Выгрузка в PostgreSQL/MySQL, Excel или Google Таблицы с валидацией структуры.",
        "tail_en": "Export to PostgreSQL/MySQL, Excel or Google Sheets with structural validation.",
    },
    "crm": {
        "query": "bitrix crm",
        "proof_ru": "Автоматизация бизнес-процессов в Битрикс24 (роботы, триггеры, смарт-процессы, вебхуки).",
        "proof_en": "Business-process automation in Bitrix24 (robots, triggers, smart processes, webhooks).",
        "tail_ru": "Опыт интеграции телефонии, внешних API и сервисов автоматизации (n8n).",
        "tail_en": "Experience integrating telephony, external APIs and automation services (n8n).",
    },
    "n8n": {
        "query": "n8n automation",
        "proof_ru": "Создание n8n-сценариев связки CRM, баз данных, AI-агентов и внешних API.",
        "proof_en": "n8n workflows connecting CRM, databases, AI agents and external APIs.",
        "tail_ru": "Надежная обработка ошибок, повторные попытки и логирование.",
        "tail_en": "Reliable error handling, retries and logging.",
    },
    "devops": {
        "query": "linux docker vpn",
        "proof_ru": "Администрирование Linux и Docker-инфраструктуры, настройка systemd, VPN и безопасности.",
        "proof_en": "Linux and Docker infrastructure administration: systemd, VPN and security hardening.",
        "tail_ru": "Чистая настройка SSL, Nginx-проксирования, фаервола и автоматических бэкапов.",
        "tail_en": "Clean SSL, Nginx reverse-proxy, firewall and automated backups setup.",
    },
    "ai": {
        "query": "ai llm rag",
        "proof_ru": "Проектирование и внедрение AI-пайплайнов (RAG, AI-агенты, Whisper, интеграция LLM в рабочие процессы).",
        "proof_en": "Design and delivery of AI pipelines (RAG, AI agents, Whisper, LLM integration into workflows).",
        "tail_ru": "Интеграция с корпоративными чатами и прикладными базами знаний.",
        "tail_en": "Integration with corporate chats and applied knowledge bases.",
    },
    "general": {
        "query": "",
        "proof_ru": "Более 15 лет подтвержденного enterprise-опыта в IT-инфраструктуре, прикладной автоматизации и Python.",
        "proof_en": "15+ years of confirmed enterprise experience in IT infrastructure, applied automation and Python.",
        "tail_ru": "Быстро вникаю в ТЗ, сдаю результат поэтапно с тестированием и документацией.",
        "tail_en": "I ramp up fast and deliver in stages with testing and documentation.",
    },
}

_DOMAIN_MATCHERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("telegram", ("бот", "telegram", "телеграм", "tg", "aiogram")),
    ("scraping", ("парс", "scraping", "сбор данн", "выгруз", "парсер")),
    ("crm", ("битрикс", "bitrix", "amocrm", "crm", "амо")),
    ("n8n", ("n8n", "make.com", "zapier", "albato", "автоматизац", "webhook")),
    ("devops", ("сервер", "vps", "vds", "linux", "vpn", "wireguard", "docker", "деплой", "nginx", "debian", "ubuntu")),
    ("ai", ("ai", "llm", "rag", "gpt", "whisper", "gemini", "нейросеть", "агент")),
)


def _detect_domain(combined: str) -> str:
    for domain, keywords in _DOMAIN_MATCHERS:
        if any(k in combined for k in keywords):
            return domain
    return "general"


def get_confirmed_pitch_focus(
    title: str,
    description: str = "",
    kb_path: Path | str | None = None,
    language: str = "ru",
) -> str:
    """Return tailored pitch focus built from confirmed candidate_kb.json facts.

    ``language`` selects the framing sentence only ("ru" default keeps the
    previous behaviour byte-for-byte); the proof facts are always the confirmed
    English KB values, so nothing is invented in either language.
    """
    combined = f"{title} {description}".lower()
    domain = _detect_domain(combined)
    tmpl = _DOMAIN_TEMPLATES[domain]

    proof = tmpl["proof_en" if language == "en" else "proof_ru"]
    bullets: list[str] = []
    if tmpl["query"]:
        facts = find_matching_kb_facts(tmpl["query"], limit=2, kb_path=kb_path)
        bullets = [f["value"] for f in facts if f["kind"] in ("duty", "project_bullet")]
    if bullets:
        proof = bullets[0]

    if language == "en":
        prefix = "Proven track record: " if domain != "general" else ""
        return f"{prefix}{proof} {tmpl['tail_en']}"
    prefix = "В подтвержденном опыте: " if domain != "general" else ""
    return f"{prefix}{proof} {tmpl['tail_ru']}"
