"""Grounded job analysis — извлечение требований ТОЛЬКО из текста объявления (P6 §3).

ПРОБЛЕМА, КОТОРУЮ ЭТО РЕШАЕТ
-----------------------------
В P5 секция WORK собиралась по совпавшим навыкам профиля: если заказ совпал по
``api``/``python``/``automation``, в WORK появлялись Telegram-бот, LLM-промпты и миграции
БД — даже когда клиент о них не писал. Это ложное обещание в отклике, а не косметика.

ПРАВИЛО
-------
Каждый пункт анализа должен иметь **подтверждение в тексте объявления**. Нет упоминания
Telegram — нет Telegram. Нет упоминания LLM — нет LLM. Если требований не видно, они
попадают в ``UNKNOWN``, а не додумываются.

Структура результата (ТЗ §3):

    REQUIRED_WORK          что просят сделать
    REQUIRED_TECH          технологии, названные клиентом
    DELIVERABLES           что должно быть сдано
    EXPLICIT_REQUIREMENTS  явные условия («нужно 3 года опыта», «работать в моём TZ»)
    CLIENT_CONSTRAINTS     ограничения от клиента (срок, бюджет, доступы)
    UNKNOWN                что не указано — честный список пробелов
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Технологии, которые мы распознаём в тексте. Только те, что встречаются в брифе,
#: попадут в REQUIRED_TECH: список — это словарь распознавания, а не «что мы умеем».
TECH_VOCABULARY: tuple[tuple[str, str], ...] = (
    ("python", "Python"),
    ("django", "Django"),
    ("flask", "Flask"),
    ("fastapi", "FastAPI"),
    ("node.js", "Node.js"),
    ("nodejs", "Node.js"),
    ("javascript", "JavaScript"),
    ("typescript", "TypeScript"),
    ("react", "React"),
    ("react native", "React Native"),
    ("vue", "Vue"),
    ("php", "PHP"),
    ("laravel", "Laravel"),
    ("wordpress", "WordPress"),
    ("woocommerce", "WooCommerce"),
    ("shopify", "Shopify"),
    ("supabase", "Supabase"),
    ("firebase", "Firebase"),
    ("sql", "SQL"),
    ("mysql", "MySQL"),
    ("postgresql", "PostgreSQL"),
    ("postgres", "PostgreSQL"),
    ("sqlite", "SQLite"),
    ("mongodb", "MongoDB"),
    ("redis", "Redis"),
    ("docker", "Docker"),
    ("kubernetes", "Kubernetes"),
    ("linux", "Linux"),
    ("windows server", "Windows Server"),
    ("powershell", "PowerShell"),
    ("bash", "Bash"),
    ("aws", "AWS"),
    ("azure", "Azure"),
    ("google cloud", "Google Cloud"),
    ("api", "API"),
    ("rest api", "REST API"),
    ("restful", "REST API"),
    ("graphql", "GraphQL"),
    ("webhook", "Webhooks"),
    ("web scraping", "Web scraping"),
    ("scraping", "Web scraping"),
    ("scraper", "Web scraping"),
    ("selenium", "Selenium"),
    ("playwright", "Playwright"),
    ("beautifulsoup", "BeautifulSoup"),
    ("pandas", "pandas"),
    ("excel", "Excel"),
    ("vba", "VBA"),
    ("google sheets", "Google Sheets"),
    ("csv", "CSV"),
    ("telegram", "Telegram"),
    ("bot", "Chatbot"),
    ("whatsapp", "WhatsApp"),
    ("openai", "OpenAI API"),
    ("chatgpt", "ChatGPT/OpenAI"),
    ("gpt", "GPT"),
    ("llm", "LLM"),
    ("gemini", "Gemini"),
    ("claude", "Claude"),
    ("n8n", "n8n"),
    ("zapier", "Zapier"),
    ("make.com", "Make.com"),
    ("airtable", "Airtable"),
    ("notion", "Notion"),
    ("crm", "CRM"),
    ("hubspot", "HubSpot"),
    ("salesforce", "Salesforce"),
    ("zoho", "Zoho"),
    ("bitrix", "Bitrix24"),
    ("monday.com", "Monday.com"),
    ("stripe", "Stripe"),
    ("paypal", "PayPal"),
    ("twilio", "Twilio"),
    ("sendgrid", "SendGrid"),
    ("gmail", "Gmail"),
    ("outlook", "Outlook"),
    ("smtp", "SMTP"),
    ("imap", "IMAP"),
    ("ffmpeg", "FFmpeg"),
    ("opencv", "OpenCV"),
    ("pytesseract", "OCR/Tesseract"),
    ("ocr", "OCR"),
    ("asyncio", "asyncio"),
    ("celery", "Celery"),
    ("airflow", "Airflow"),
    ("etl", "ETL"),
    ("git", "Git"),
    # Дефект, найденный тестом: без этих записей гард не мог поймать выдуманную
    # технологию — он её просто не знал. Список распознавания должен покрывать
    # стеки, которыми нас могут попытаться впечатлить.
    ("rust", "Rust"),
    ("c++", "C++"),
    ("c#", "C#"),
    ("java", "Java"),
    ("kotlin", "Kotlin"),
    ("swift", "Swift"),
    ("golang", "Go"),
    ("ruby", "Ruby"),
    ("rails", "Ruby on Rails"),
    ("scala", "Scala"),
    ("perl", "Perl"),
    ("matlab", "MATLAB"),
    ("solidity", "Solidity"),
    ("unity", "Unity"),
    ("unreal", "Unreal Engine"),
    ("flutter", "Flutter"),
    ("android", "Android"),
    ("ios", "iOS"),
    ("swiftui", "SwiftUI"),
    ("objective-c", "Objective-C"),
    ("applescript", "AppleScript"),
    ("terraform", "Terraform"),
    ("ansible", "Ansible"),
    ("jenkins", "Jenkins"),
    ("sap", "SAP"),
    ("oracle", "Oracle"),
    ("drupal", "Drupal"),
    ("magento", "Magento"),
    ("power bi", "Power BI"),
    ("tableau", "Tableau"),
    ("unity3d", "Unity"),
    ("blockchain", "Blockchain"),
    ("web3", "Web3"),
    ("gamedev", "Game development"),
)

#: Глаголы и обороты, по которым распознаётся требуемая работа.
WORK_VERBS: tuple[str, ...] = (
    "build", "create", "develop", "design", "implement", "integrate", "automate",
    "scrape", "extract", "parse", "migrate", "convert", "clean", "deduplicate",
    "set up", "setup", "configure", "deploy", "maintain", "monitor", "optimize",
    "write", "generate", "send", "sync", "connect", "link", "fetch", "upload",
    "download", "validate", "test", "review", "audit", "fix", "debug", "rewrite",
    "refactor", "analyse", "analyze", "report",
    "record", "calculate", "sort", "export", "retain", "store", "return",
)

#: Маркеры сдаваемого результата.
DELIVERABLE_MARKERS: tuple[str, ...] = (
    "deliverable", "deliverables", "you will deliver", "expected output",
    "final output", "we need", "i need", "must provide", "should provide",
    "provide a", "provide an", "output should", "result should",
    "documentation", "source code", "source files", "report", "screenshot",
)

#: Явные требования и ограничения, названные клиентом.
REQUIREMENT_MARKERS: tuple[str, ...] = (
    "must ", "required", "requirement", "need to", "needs to", "should be able",
    "experience with", "experience in", "proficient", "fluent", "native",
    "years of experience", "available", "timezone", "utc", "full-time", "part-time",
    "budget is", "fixed price", "hourly", "milestone", "nda", "confidential",
    "no agencies", "individual", "long-term", "ongoing", "urgent", "asap",
    "deadline", "within ", "days", "weeks",
)

#: Что клиент может упомянуть как ограничение.
CONSTRAINT_MARKERS: tuple[str, ...] = (
    "timezone", "utc", "budget", "deadline", "urgent", "asap", "nda",
    "confidential", "no agencies", "milestone", "weekly", "daily", "hours",
    "access", "permission", "credentials", "api key", "existing",
)

#: Стоп-слова для выбора содержательных предложений.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "we", "i", "you", "it",
    "is", "are", "was", "were", "be", "been", "to", "of", "in", "on", "for",
    "with", "as", "at", "by", "from", "that", "this", "these", "those", "will",
    "would", "can", "could", "should", "may", "might", "must", "have", "has",
    "had", "do", "does", "did", "not", "no", "yes", "so", "up", "out", "about",
    "into", "over", "after", "before", "also", "just", "very", "more", "most",
})


def _sentences(text: str) -> list[str]:
    """Разбивает текст на предложения, сохраняя их целиком."""
    import re

    parts = re.split(r"(?<=[.!?;])\s+|\n+", text or "")
    return [p.strip() for p in parts if p.strip()]


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


@dataclass
class GroundedAnalysis:
    """Требования, извлечённые из брифа, с подтверждением для каждого пункта."""

    required_work: list[dict[str, str]] = field(default_factory=list)
    required_tech: list[dict[str, str]] = field(default_factory=list)
    deliverables: list[dict[str, str]] = field(default_factory=list)
    explicit_requirements: list[dict[str, str]] = field(default_factory=list)
    client_constraints: list[dict[str, str]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    #: Насколько ясно ТЗ: доля предложений с требованием от общего числа.
    scope_clarity: float = 0.0
    #: Текст, из которого построен анализ (для QA-сверки).
    source_text_chars: int = 0

    def tech_names(self) -> list[str]:
        return [t["name"] for t in self.required_tech]

    def everything(self) -> list[dict[str, str]]:
        return (self.required_work + self.required_tech + self.deliverables
                + self.explicit_requirements + self.client_constraints)

    def to_dict(self) -> dict[str, Any]:
        return {
            "REQUIRED_WORK": self.required_work,
            "REQUIRED_TECH": self.required_tech,
            "DELIVERABLES": self.deliverables,
            "EXPLICIT_REQUIREMENTS": self.explicit_requirements,
            "CLIENT_CONSTRAINTS": self.client_constraints,
            "UNKNOWN": self.unknown,
            "scope_clarity": self.scope_clarity,
            "source_text_chars": self.source_text_chars,
        }


def _snippet(sentence: str, limit: int = 190) -> str:
    text = " ".join(sentence.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def find_tech(text: str) -> list[dict[str, str]]:
    """Технологии, названные в тексте. Каждая — со своей фразой-подтверждением."""
    import re

    lowered = _norm(text)
    found: dict[str, dict[str, str]] = {}
    for token, canonical in TECH_VOCABULARY:
        pattern = r"(?<![a-z0-9+#])" + re.escape(token) + r"(?![a-z0-9+#])"
        if canonical in found:
            continue
        # Подтверждение — предложение, в котором встретилось слово.
        evidence = ""
        for sentence in _sentences(text):
            normalized = _norm(sentence)
            match = re.search(pattern, normalized)
            if match:
                prefix = normalized[max(0, match.start() - 50):match.start()]
                if re.search(r"\b(?:no|without|not using|do not use|don't use)\s+(?:\w+\s+){0,3}$", prefix):
                    continue
                evidence = sentence
                break
        if evidence:
            found[canonical] = {"name": canonical, "evidence": evidence}
    return [found[k] for k in sorted(found)]


def analyze(description: str, title: str = "", skills: list[str] | None = None) -> GroundedAnalysis:
    """Строит анализ строго из текста объявления.

    ``skills`` retained for call compatibility, but tags do not become requirements.
    """
    text = f"{title}\n{description}".strip()
    lowered = _norm(text)
    sentences = _sentences(description)

    analysis = GroundedAnalysis(source_text_chars=len(text or ""))

    # --- REQUIRED_WORK: предложения с глаголом действия ---------------------
    for sentence in sentences:
        low = _norm(sentence)
        verb = next((v for v in WORK_VERBS if v in low), None)
        if not verb:
            continue
        if len(sentence.split()) < 4:
            continue
        analysis.required_work.append({"work": _snippet(sentence), "evidence": _snippet(sentence)})
        if len(analysis.required_work) >= 8:
            break

    # --- REQUIRED_TECH ------------------------------------------------------
    analysis.required_tech = find_tech(text)
    # Search/category tags are discovery metadata, never client requirements.

    # --- DELIVERABLES -------------------------------------------------------
    for sentence in sentences:
        low = _norm(sentence)
        if any(marker in low for marker in DELIVERABLE_MARKERS):
            analysis.deliverables.append(
                {"deliverable": _snippet(sentence), "evidence": _snippet(sentence)}
            )
        if len(analysis.deliverables) >= 5:
            break

    # --- EXPLICIT_REQUIREMENTS ---------------------------------------------
    for sentence in sentences:
        low = _norm(sentence)
        if any(marker in low for marker in REQUIREMENT_MARKERS):
            analysis.explicit_requirements.append(
                {"requirement": _snippet(sentence), "evidence": _snippet(sentence)}
            )
        if len(analysis.explicit_requirements) >= 6:
            break

    # --- CLIENT_CONSTRAINTS -------------------------------------------------
    for sentence in sentences:
        low = _norm(sentence)
        if any(marker in low for marker in CONSTRAINT_MARKERS):
            entry = {"constraint": _snippet(sentence), "evidence": _snippet(sentence)}
            if entry["constraint"] not in [c["constraint"] for c in analysis.client_constraints]:
                analysis.client_constraints.append(entry)
        if len(analysis.client_constraints) >= 5:
            break

    # --- UNKNOWN: чего в брифе нет -----------------------------------------
    if not analysis.required_work:
        analysis.unknown.append("требуемая работа не сформулирована явно")
    if not analysis.required_tech:
        analysis.unknown.append("технологии не названы")
    if not analysis.deliverables:
        analysis.unknown.append("сдаваемый результат не описан")
    if not any(s in lowered for s in ("deadline", "within", "days", "weeks", "urgent", "asap")):
        analysis.unknown.append("срок выполнения не указан")
    if not any(s in lowered for s in ("milestone", "fixed price", "hourly", "budget")):
        analysis.unknown.append("условия оплаты не детализированы")
    if not any(s in lowered for s in ("access", "api key", "credentials", "permission", "existing")):
        analysis.unknown.append("доступы и существующие системы не описаны")

    # --- scope_clarity: доля предложений, несущих требование ----------------
    if sentences:
        requiring = sum(
            1 for s in sentences
            if any(m in _norm(s) for m in REQUIREMENT_MARKERS)
            or any(v in _norm(s) for v in WORK_VERBS)
        )
        analysis.scope_clarity = round(requiring / len(sentences), 2)
    return analysis


def work_hints(analysis: GroundedAnalysis) -> list[str]:
    """Секция WORK для отчёта: только подтверждённые пункты.

    Здесь нет словаря «что мы умеем»: если клиент не написал про Telegram, строки
    про Telegram не появится, даже когда навык есть в профиле.
    """
    hints: list[str] = []
    for item in analysis.required_work[:4]:
        hints.append(item["work"])
    if not hints:
        hints.append("Требуемая работа в объявлении явно не сформулирована — нужен уточняющий вопрос")
    return hints


def unsupported_claims(analysis: GroundedAnalysis, text: str) -> list[str]:
    """Проверяет, что в тексте нет технологий, которых нет в брифе (QA §14).

    Используется для проверки предложений: любая технология, упомянутая в proposal,
    должна либо быть в REQUIRED_TECH, либо отсутствовать.
    """
    allowed: set[str] = set()
    for item in analysis.required_tech:
        if item.get("name"):
            allowed.add(item["name"].lower())
    # Deliverables и work — это фразы из брифа, не названия технологий. Их текст
    # добавляется целиком, чтобы слова из подтверждённых пунктов не считались выдумкой.
    for group in (analysis.deliverables, analysis.required_work,
                  analysis.explicit_requirements, analysis.client_constraints):
        for item in group:
            for value in item.values():
                allowed.add(str(value).lower())
    injected: list[str] = []
    for token, canonical in TECH_VOCABULARY:
        if canonical.lower() in allowed:
            continue
        import re

        if re.search(r"(?<![a-z0-9+#])" + re.escape(token) + r"(?![a-z0-9+#])", _norm(text)):
            injected.append(canonical)
    return sorted(set(injected))


__all__ = [
    "CONSTRAINT_MARKERS",
    "DELIVERABLE_MARKERS",
    "GroundedAnalysis",
    "REQUIREMENT_MARKERS",
    "TECH_VOCABULARY",
    "WORK_VERBS",
    "analyze",
    "find_tech",
    "unsupported_claims",
    "work_hints",
]
