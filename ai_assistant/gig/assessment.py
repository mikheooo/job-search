"""Оценка выполнимости и сопутствующих атрибутов гига.

Здесь НЕТ LLM: это прозрачные эвристики над текстом заказа и профилем
кандидата. Именно поэтому результат воспроизводим и объясним, а сами правила
можно калибровать по реальным данным (ТЗ §4, §6).

Оценки:
- ``can_deliver``               — high/medium/low/unknown;
- ``ai_automation_potential``   — 0..100;
- ``estimated_effort_hours``    — ожидаемая трудоёмкость;
- ``effective_hourly_rate``     — бюджет / трудоёмкость (учитывая конкуренцию);
- ``client_quality``            — 0..100;
- ``risk_level`` + ``risks``    — риск и его причины.

Ключевое требование ТЗ §4: выполнимость НЕ ограничивается историческим
резюме. В расчёт входят: собственные силы, AI coding agents, возможность
освоить по ходу, проверяемость результата, наличие API/tools и риск скрытых
требований.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .currency import exchange_rate_of, to_usd
from .models import BudgetType, CanDeliver, Gig, PaymentReadiness, RiskLevel

# --- Доверие к навыкам ----------------------------------------------------

CONFIDENCE_WEIGHTS: dict[str, float] = {
    "professional": 1.0,
    "project": 0.8,
    "basic": 0.6,
    "transferable": 0.4,
    "unknown": 0.0,
}

#: Насколько навык поддаётся освоению по ходу (с AI coding agents).
LEARNABILITY: dict[str, float] = {
    "python": 0.9,
    "automation": 0.9,
    "n8n": 0.9,
    "rest api": 0.9,
    "api": 0.9,
    "webhooks": 0.9,
    "telegram": 0.85,
    "llm": 0.85,
    "ai": 0.85,
    "ai agents": 0.85,
    "prompt engineering": 0.85,
    "sql": 0.85,
    "postgresql": 0.8,
    "docker": 0.8,
    "linux": 0.8,
    "bash": 0.8,
    "powershell": 0.85,
    "fastapi": 0.8,
    "flask": 0.8,
    "pandas": 0.8,
    "scraping": 0.8,
    "data entry": 0.95,
    "excel": 0.9,
    "google sheets": 0.95,
    "airtable": 0.9,
    "notion": 0.95,
    "zapier": 0.95,
    "make.com": 0.9,
    "no-code": 0.95,
    "chatbot": 0.85,
    "gpt": 0.9,
    "transcription": 0.8,
    "content writing": 0.8,
    "seo": 0.7,
    "wordpress": 0.75,
    "shopify": 0.7,
    "graphic design": 0.4,
    "video editing": 0.4,
    "mobile development": 0.3,
    "ios": 0.25,
    "android": 0.3,
    "react native": 0.3,
    "c++": 0.2,
    "rust": 0.2,
    "embedded": 0.15,
    "hardware": 0.15,
    "blockchain": 0.3,
    "smart contract": 0.3,
    "sales": 0.15,
    "cold calling": 0.05,
    "phone calls": 0.1,
    "voice acting": 0.1,
    "photography": 0.1,
    "3d modeling": 0.2,
}

#: Навыки, которые мы заведомо не закрываем (даже с AI-агентами).
BLOCKING_SKILLS: dict[str, str] = {
    "c++": "требуется C++/systems experience",
    "rust": "требуется Rust",
    "embedded": "embedded/hardware",
    "hardware": "физическое железо",
    "cold calling": "требуются холодные звонки",
    "voice acting": "требуется озвучка",
    "photography": "требуется фотография",
    "3d modeling": "3D-моделирование",
    # P4: британское написание реально встречается в выдаче Freelancer ('3d modelling'),
    # а в списке было только американское — синоним не срабатывал.
    "3d modelling": "3D-моделирование",
    "3d rendering": "3D-рендеринг — визуальное производство",
    "modelling": "3D-моделирование",
    # P4: домены, реально встречавшиеся в выдаче Freelancer, но отсутствовавшие здесь.
    # Из-за этого «Custom Pneumatic Control Design» с CAD/электроникой получал
    # can_deliver=high и первое место в рейтинге.
    "3d cad": "3D CAD — механическое проектирование",
    "cad": "CAD/чертежи — инженерное проектирование",
    "cad/cam": "CAD/CAM — производственное проектирование",
    "autocad": "AutoCAD — инженерное проектирование",
    "solidworks": "SolidWorks — механическое проектирование",
    "electronics": "проектирование электроники",
    "pcb": "разводка печатных плат",
    "electrical": "электрика/схемотехника",
    "mechanical": "механика/машиностроение",
    "sensor integration": "интеграция датчиков — физическое оборудование",
    "bill of materials": "BOM — производственная спецификация",
    "pneumatic": "пневматика — физическое оборудование",
    "hydraulic": "гидравлика — физическое оборудование",
    "manufacturing": "производство/изготовление",
    "3d printing": "3D-печать — физическое изготовление",
    "cnc": "ЧПУ — физическое изготовление",
    "welding": "сварка",
    # Возвращён после P4: токен был ошибочно удалён вместо добавления квалификатора.
    # Блокирует только при строительном контексте (см. BLOCKING_SKILL_QUALIFIERS),
    # поэтому «software architecture» в IT-заказе больше не блокируется.
    "architecture": "архитектурное проектирование",
    "interior design": "дизайн интерьера",
    "civil engineering": "строительное проектирование",
}

#: Токены, которые слишком широки для блокировки в одиночку. Они блокируют только
#: вместе с не-IT контекстом: «embedded feed» — это веб-виджет, а
#: «software architecture» — обычная инженерная роль, а не архитектурное бюро.
#: Без этого списка измеренные ложные срабатывания: architecture блокировал 5 IT-заказов
#: (Data Entry Management System, Python Developer), embedded — сайт с Instagram-лентой.
BLOCKING_SKILL_QUALIFIERS: dict[str, re.Pattern[str]] = {
    "architecture": re.compile(
        r"\b(architectural|building|construction|floor\s*plan|residential|commercial)\b",
        re.I,
    ),
    "embedded": re.compile(
        r"\b(firmware|microcontroller|mcu|rtos|pcb|arm\s*cortex|stm32|arduino|esp32)\b",
        re.I,
    ),
    "civil engineering": re.compile(r"\b(civil|structural|site)\b", re.I),
    # P4: язык в списке требований через «like/such as/including» — это перечисление
    # примеров, а не требование. Наблюдено: «Experience in OO languages like Python,
    # C++, .NET etc» блокировало Python-заказ.
    "c++": re.compile(r"(?<!\blike\s)(?<!\bsuch as\s)(?<!\bincluding\s)\bc\+\+", re.I),
    "rust": re.compile(r"(?<!\blike\s)(?<!\bsuch as\s)(?<!\bincluding\s)\brust", re.I),
}

#: Формулировки, превращающие перечисление технологий в список примеров, а не требований.
_EXAMPLE_LIST_RE = re.compile(
    r"\b(?:like|such as|including|e\.g\.|for example|among others)\b[^.;]{0,120}",
    re.I,
)

#: Маркеры высокой автоматизируемости заказа (AI-агент сделает большую часть).
AUTOMATION_STRONG = (
    "scrape",
    "scraping",
    "data entry",
    "enrich",
    "enrichment",
    "lead list",
    "transcription",
    "summariz",
    "parser",
    "etl",
    "api integration",
    "zapier",
    "make.com",
    "n8n",
    "workflow automation",
    "bulk",
    "batch process",
    "csv",
    "spreadsheet",
    "generate content",
    "webhook",
    "chatbot",
    "bot development",
)
AUTOMATION_WEAK = (
    "subscription",
    "long-term",
    "research",
    "analysis",
    "report",
    "documentation",
    "review",
    "qa",
    "data cleaning",
    "seo audit",
)
AUTOMATION_LOW = (
    "consulting",
    "strategy",
    "architecture design",
    "one-on-one",
    "interview",
    "coaching",
    "negotiat",
    "design system",
    "brand identity",
)

#: Рисковые формулировки.
RISK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bno\s+escrow\b", "нет escrow — оплата без защиты"),
    (r"\bpay(?:ment)?\s+(?:after|upon)\s+(?:full\s+)?delivery\b", "оплата только после полной сдачи"),
    (r"\bunpaid\s+(?:test|trial|sample)\b", "требуется неоплачиваемый тестовый таск"),
    (r"\bquick\s+test\s+task\b", "требуется тестовое задание"),
    (r"\b(?:immediate|asap)\s+start\b.*\b(?:today|tonight)\b", "сжатые сроки старта"),
    (r"\bdeadline\s+(?:is\s+)?(?:tomorrow|today)\b", "дедлайн в течение суток"),
    (r"\bmultiple\s+rounds?\s+of\s+(?:free\s+)?revisions?\b", "неограниченные бесплатные правки"),
    (r"\bunlimited\s+revisions?\b", "неограниченные правки"),
    (r"\bnda\s+required\b", "требуется NDA"),
    (r"\bnd\s?as?\b", "требуется NDA"),
    (r"\bshare\s+(?:your\s+)?(?:github|portfolio)\s+(?:and|plus)\s+(?:id|passport)\b", "запрос личных документов"),
    (r"\bimmediate\s+full[\s-]?time\s+commitment\b", "требуется полная занятость"),
    (r"\b\d{3,}\s+(?:proposals?|applicants?)\b", "очень высокая конкуренция"),
    (r"\bnew\s+client\b\s*(?:with\s+)?(?:no|zero)\s+(?:payment|spend)", "новый клиент без истории платежей"),
)

#: Заказы, где результат объективно проверяем.
VERIFIABLE_MARKERS = (
    "test",
    "unit test",
    "passing test",
    "deliverable",
    "acceptance criteria",
    "specification",
    "spec",
    "api",
    "endpoint",
    "csv output",
    "report",
    "documentation",
    "working demo",
    "repo",
)


def load_candidate_profile(path: str | Path | None = None) -> dict[str, Any]:
    """Читает профиль кандидата. Отсутствие файла -> пустой профиль (не падаем)."""
    if path is None:
        path = Path(__file__).resolve().parent.parent.parent / "candidate_profile.json"
    candidate = Path(path)
    if not candidate.exists():
        return {}
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def skill_confidences(profile: dict[str, Any]) -> dict[str, float]:
    """Навык -> вес 0..1 из ``skill_confidence`` профиля."""
    raw = profile.get("skill_confidence") or {}
    result: dict[str, float] = {}
    for skill, level in raw.items():
        result[str(skill).strip().lower()] = CONFIDENCE_WEIGHTS.get(str(level).strip().upper().lower(), 0.0)
    for key in ("core_skills", "secondary_skills", "transferable_skills", "skills"):
        weight = {"core_skills": 1.0, "secondary_skills": 0.8, "transferable_skills": 0.4, "skills": 0.8}.get(key, 0.5)
        for skill in profile.get(key) or []:
            result.setdefault(str(skill).strip().lower(), weight)
    return result


def _text(gig: Gig) -> str:
    return f"{gig.title} {gig.description} {' '.join(gig.skills)}".lower()


#: Навыки профиля, которые являются обычными английскими словами и без контекста
#: дают ложные совпадения. «make» — это Make.com (платформа автоматизации), но в тексте
#: заказа это чаще глагол «сделать». Измерено на корпусе: без этой оговорки 29 заказов
#: вроде «Whole House Interior Design» и «Armenian Spiritual Book Proofreading» получали
#: совпадение навыка и поднимались до can_deliver=high.
AMBIGUOUS_SKILL_CONTEXT: dict[str, re.Pattern[str]] = {
    "make": re.compile(
        r"\b(make\.com|integromat|scenario|no-?code|low-?code|интеграц\w*|автоматизац\w*)\b",
        re.I,
    ),
    "llm": re.compile(r"\b(llm|gpt|openai|языков\w* модел|large language model|prompt)\b", re.I),
    "git": re.compile(r"\b(git|github|gitlab|commit|pull request|branch|repo)\b", re.I),
    "bash": re.compile(r"\b(bash|shell|sh script|терминал|cli)\b", re.I),
}


def _matched_skills(gig: Gig, confidences: dict[str, float]) -> list[tuple[str, float]]:
    """Навыки профиля, реально встречающиеся в тексте заказа.

    P5: неоднозначные короткие слова (``make``, ``llm``, ``git``, ``bash``) требуют
    подтверждающего контекста. Без него «I need someone to make a logo» давало
    совпадение по Make.com и поднимало доку заказ в can_deliver=high — измерено
    на 29 записях корпуса.
    """
    text = _text(gig)
    matched: list[tuple[str, float]] = []
    for skill, weight in confidences.items():
        if not skill:
            continue
        pattern = r"(?<![a-z0-9+#])" + re.escape(skill) + r"(?![a-z0-9+#])"
        if not re.search(pattern, text):
            continue
        qualifier = AMBIGUOUS_SKILL_CONTEXT.get(skill)
        if qualifier is not None and not qualifier.search(text):
            # Слово найдено, но подтверждающего контекста нет — это не этот навык.
            continue
        matched.append((skill, weight))
    return sorted(matched, key=lambda item: (-item[1], item[0]))


def _unmatched_blockers(gig: Gig, confidences: dict[str, float]) -> list[tuple[str, str]]:
    """Скиллы, которые профиль не закрывает и которые блокируют заказ.

    P4: два условия против ложных блокировок, оба найдены на реальном корпусе:

    * токены из ``BLOCKING_SKILL_QUALIFIERS`` блокируют только при подтверждающем
      не-IT контексте («software architecture» и «embedded Instagram feed» не считаются
      физической работой);
    * токен, упомянутый внутри перечисления примеров («languages like Python, C++, .NET»),
      не считается требованием.
    """
    text = _text(gig)
    # Фрагменты, где технологии перечислены как примеры, а не как требование.
    example_spans = [m.span() for m in _EXAMPLE_LIST_RE.finditer(text)]

    def inside_example_list(start: int) -> bool:
        return any(lo <= start < hi for lo, hi in example_spans)

    blockers: list[tuple[str, str]] = []
    for skill, note in BLOCKING_SKILLS.items():
        if confidences.get(skill, 0.0) >= 0.8:
            continue
        pattern = r"(?<![a-z0-9+#])" + re.escape(skill) + r"(?![a-z0-9+#])"
        match = re.search(pattern, text)
        if not match:
            continue
        if inside_example_list(match.start()):
            # «like Python, C++, .NET etc» — это иллюстрация, не требование.
            continue
        qualifier = BLOCKING_SKILL_QUALIFIERS.get(skill)
        if qualifier is not None and not qualifier.search(text):
            # Токен найден, но подтверждающего не-IT контекста нет → не блокируем.
            continue
        blockers.append((skill, note))
    return blockers


def _learnability(gig: Gig) -> float:
    """Средняя осваиваемость навыков заказа (0..1). Неизвестный навык — 0.5."""
    text = _text(gig)
    scores: list[float] = []
    for token, score in LEARNABILITY.items():
        pattern = r"(?<![a-z0-9+#])" + re.escape(token) + r"(?![a-z0-9+#])"
        if re.search(pattern, text):
            scores.append(score)
    if not scores:
        return 0.5
    return sum(scores) / len(scores)


def _auto_scope(gig: Gig) -> float:
    """Доля заказа, которую реально закрывают AI coding agents (0..1)."""
    text = _text(gig)
    strong = sum(1 for marker in AUTOMATION_STRONG if marker in text)
    weak = sum(1 for marker in AUTOMATION_WEAK if marker in text)
    low = sum(1 for marker in AUTOMATION_LOW if marker in text)
    if strong == 0 and weak == 0 and low == 0:
        return 0.5
    score = 0.5 + min(strong, 4) * 0.12 + min(weak, 3) * 0.04 - min(low, 3) * 0.15
    return max(0.0, min(1.0, score))


def _verifiability(gig: Gig) -> float:
    text = _text(gig)
    hits = sum(1 for marker in VERIFIABLE_MARKERS if marker in text)
    return min(1.0, 0.4 + hits * 0.15)


def _has_api_or_tools(gig: Gig) -> bool:
    text = _text(gig)
    return bool(re.search(r"\b(?:api|sdk|webhook|documentation|docs|open\s+data|cli|library)\b", text))


def estimate_effort(gig: Gig) -> float:
    """Оценка трудоёмкости в часах.

    Базовая ёмкость — из типа бюджета (fixed проекты больше hourly задач),
    корректируется автоматизируемостью. Никогда не выдумывает "точную" цифру:
    результат — эвристика, и она помечается в отчёте как limitation.
    """
    if gig.budget_type is BudgetType.HOURLY:
        base = 40.0
    elif gig.budget_type is BudgetType.FIXED:
        base = 24.0
    else:
        base = 16.0

    # Размер по бюджету: больше денег — обычно больше объём.
    ceiling = gig.budget_max or gig.budget_min
    if ceiling:
        base = max(base, min(160.0, 12.0 + ceiling / 12.0))

    # Объём текста как прокси сложности/масштаба.
    words = len(gig.description.split())
    if words > 220:
        base *= 1.25
    elif words < 60:
        base *= 0.85

    scope = _auto_scope(gig)
    base *= 1.0 - 0.3 * scope
    return round(max(2.0, base), 1)


def assess_client_quality(gig: Gig) -> float | None:
    """Качество клиента 0..100. ``None``, если данных нет (не выдумываем)."""
    signals: list[float] = []
    if gig.client_rating is not None:
        # рейтинг 5-балльный или 0..100 — нормализуем
        rating = float(gig.client_rating)
        if rating <= 5.0:
            rating *= 20.0
        signals.append(max(0.0, min(100.0, rating)))
    if gig.client_total_spent is not None:
        spend = float(gig.client_total_spent)
        if spend <= 0:
            signals.append(15.0)
        elif spend < 1000:
            signals.append(45.0)
        elif spend < 10000:
            signals.append(70.0)
        elif spend < 100000:
            signals.append(85.0)
        else:
            signals.append(95.0)
    if gig.client_hires is not None and gig.client_jobs_posted:
        posted = max(1, int(gig.client_jobs_posted))
        hire_rate = float(gig.client_hires) / posted
        signals.append(max(0.0, min(100.0, 30.0 + hire_rate * 70.0)))
    if not signals:
        return None
    return round(sum(signals) / len(signals), 1)


def assess_payment_readiness(gig: Gig) -> PaymentReadiness:
    """Готовность оплаты — независимо от общего match score (ТЗ §5 P1, §5 P2).

    Проверяем реальные данные о канале оплаты, а не "здравый смысл".

    P2 §5: список способов выплат на уровне площадки — это capability площадки,
    а НЕ готовность нашего аккаунта получать деньги. Поэтому
    ``platform_payment_options`` никогда сам по себе не даёт ``ready``: без
    подтверждённого канала и истории клиента результат — ``possible`` в лучшем
    случае, иначе ``unknown``. Персональная верификация выплат не выполнена →
    заказ не отклоняется (hard reject даёт только ``blocked``).
    """
    if gig.payment_readiness is PaymentReadiness.BLOCKED:
        return PaymentReadiness.BLOCKED

    text = _text(gig)
    if re.search(r"\b(?:equity\s+only|paid\s+in\s+exposure|work\s+for\s+free|unpaid)\b", text):
        return PaymentReadiness.BLOCKED
    if re.search(r"\b(?:crypto|usdt|btc|bitcoi?n)\s+only\b", text):
        # крипта без escrow — канал считаем неподтверждённым, но не блокируем
        return PaymentReadiness.POSSIBLE

    has_known_method = bool(
        re.search(r"\b(?:escrow|milestone|paypal|wise|bank\s+transfer|stripe|upwork\s+escrow|deel)\b", text)
    )
    currency_known = bool(gig.currency)
    payment_verified = (gig.client_total_spent or 0) > 0 and (gig.client_hires or 0) > 0

    if payment_verified and (has_known_method or currency_known):
        return PaymentReadiness.READY
    if payment_verified or has_known_method:
        return PaymentReadiness.POSSIBLE

    # Platform-level capability without any personal/account confirmation.
    # `ready` is unreachable on this branch by construction (P2 §5).
    if gig.platform_payment_options:
        return PaymentReadiness.POSSIBLE

    if gig.client_total_spent is None and gig.client_hires is None:
        return PaymentReadiness.UNKNOWN
    return PaymentReadiness.POSSIBLE


def assess_risks(gig: Gig, confidences: dict[str, float]) -> tuple[RiskLevel, list[str]]:
    """Уровень риска + список причин на человеческом языке."""
    text = _text(gig)
    risks: list[str] = []

    for pattern, note in RISK_PATTERNS:
        if re.search(pattern, text):
            risks.append(note)

    blockers = _unmatched_blockers(gig, confidences)
    for _, note in blockers:
        risks.append(note)

    if gig.budget_type is BudgetType.UNKNOWN:
        risks.append("тип бюджета не определён")
    if gig.budget_min is None and gig.budget_max is None:
        risks.append("бюджет не указан")
    if gig.payment_readiness is PaymentReadiness.UNKNOWN:
        risks.append("канал оплаты не подтверждён")
    if gig.client_total_spent is None and gig.client_hires is None:
        risks.append("нет истории клиента")
    if gig.client_rating is not None and float(gig.client_rating) <= 3.0:
        risks.append(f"низкий рейтинг клиента: {gig.client_rating}")
    if gig.competition is not None and gig.competition >= 50:
        risks.append(f"высокая конкуренция: {gig.competition} откликов")

    # Дедупликация причин с сохранением порядка.
    seen: set[str] = set()
    unique = [r for r in risks if not (r in seen or seen.add(r))]

    hard = any(b[0] in BLOCKING_SKILLS for b in blockers)
    if hard or len(unique) >= 5:
        level = RiskLevel.HIGH
    elif len(unique) >= 2:
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW
    return level, unique


def assess_ai_automation_potential(gig: Gig) -> int:
    """0..100: насколько заказ закрывается AI coding agents."""
    scope = _auto_scope(gig)
    return int(round(scope * 100))


#: Домены, которые мы не берёмся выполнять, хотя они не «железо». Нужны для причины
#: SKIP ``non-IT domain`` (ТЗ §6): без них видео- и контент-работы проходили как
#: выполнимые, потому что совпадали по случайному слову (см. AMBIGUOUS_SKILL_CONTEXT).
NON_IT_DOMAIN_MARKERS: tuple[str, ...] = (
    "video editing", "video production", "video services", "video post-editing",
    "videography", "film production", "animation", "2d animation", "3d animation",
    "character animation", "motion graphics", "sound design", "audio production",
    "audio editing", "audio services", "voice over", "voice talent", "music production",
    "photography", "photo editing", "videography",
    "graphic design", "logo design", "illustration", "caricature", "print design",
    "content writing", "copywriting", "article writing", "creative writing",
    "seo writing", "translation", "proofreading", "copy editing",
)


def is_non_it_domain(gig: Gig) -> tuple[bool, list[str]]:
    """Заказ из домена, который мы не выполняем? Возвращает (да/нет, маркеры).

    Отличается от ``BLOCKING_SKILLS``: там физические и инженерные области, здесь —
    работы, которые не закрываются нашим стеком (видео, звук, графика, копирайтинг,
    перевод). Это не «опасно», а «не наше»: следствие — причина SKIP, а не отказ гейта.
    """
    text = _text(gig)
    found = [m for m in NON_IT_DOMAIN_MARKERS if re.search(
        r"(?<![a-z0-9])" + re.escape(m) + r"(?![a-z0-9])", text)]
    return bool(found), found


def has_it_signal(gig: Gig) -> bool:
    """Есть ли в заказе признаки нашей области? Используется для различения домена.

    Многие заказы упоминают и видео, и API. Если есть явный IT-признак, заказ остаётся
    в рассмотрении: он может быть ровно про интеграцию, а не про монтаж.
    """
    text = _text(gig)
    markers = (
        "api", "automation", "автоматизац", "python", "integration", "интеграц",
        "telegram", "bot", "бот", "scraping", "парсинг", "database", "database",
        "postgresql", "mysql", "sql", "webhook", "backend", "бекенд", "ai agent",
        "llm", "openai", "chatgpt", "n8n", "zapier", "make.com", "excel macro",
        "data entry", "csv", "etl", "restful", "rest api",
    )
    return any(re.search(r"(?<![a-z0-9])" + re.escape(m) + r"(?![a-z0-9])", text)
               for m in markers)


def assess_can_deliver(
    gig: Gig,
    confidences: dict[str, float],
) -> tuple[CanDeliver, list[str], list[str]]:
    """Выполнимость заказа + (why_matched, why_risky).

    Учитывает: собственные навыки, осваиваемость, AI-агентов, проверяемость,
    наличие API/tools, риск скрытых требований (ТЗ §4).
    """
    matched = _matched_skills(gig, confidences)
    blockers = _unmatched_blockers(gig, confidences)
    learnability = _learnability(gig)
    scope = _auto_scope(gig)
    verifiability = _verifiability(gig)
    has_tools = _has_api_or_tools(gig)

    if matched:
        # P4: агрегация насыщением, а не средним. Прежнее ``сумма/количество``
        # награждало МЕНЬШЕЕ число совпадений: одно совпадение с весом 1.0 давало
        # 1.00, а четыре (python+rest api+sql+troubleshooting) — только 0.85.
        # Теперь каждое новое релевантное совпадение повышает оценку и никогда
        # не понижает её; сильные навыки весят больше слабых.
        ordered = sorted((w for _, w in matched), reverse=True)
        bonus = 0.0
        for index, value in enumerate(ordered[1:], start=1):
            bonus += value * (0.5 ** index)
        weight = min(1.0, ordered[0] + bonus)
    else:
        weight = 0.0

    # Формула: измеренный скилл-вес + способность освоить/автоматизировать.
    raw = (
        0.35 * weight
        + 0.25 * learnability
        + 0.25 * scope
        + 0.10 * verifiability
        + 0.05 * (1.0 if has_tools else 0.0)
    )

    # Штраф за скрытые требования: неизвестные тяжёлые технологии.
    hidden_requirement_penalty = 0.25 * len(blockers)
    raw -= hidden_requirement_penalty

    why_matched: list[str] = []
    if matched:
        top = ", ".join(s for s, _ in matched[:5])
        why_matched.append(f"совпадающие навыки: {top}")
    if learnability >= 0.75:
        why_matched.append("высокая осваиваемость стека (реально закрыть по ходу)")
    if scope >= 0.65:
        why_matched.append(f"высокая автоматизируемость ({int(round(scope * 100))}/100)")
    if verifiability >= 0.7:
        why_matched.append("результат объективно проверяем")
    if has_tools:
        why_matched.append("есть API/документация для работы")

    why_risky: list[str] = []
    for _, note in blockers:
        why_risky.append(note)
    if not matched:
        why_risky.append("нет прямого совпадения по навыкам профиля")
    if not has_tools and scope < 0.5:
        why_risky.append("нет явного API/инструментов, ожидаема ручная работа")
    if not gig.description or len(gig.description.split()) < 25:
        why_risky.append("описание скудное — возможны скрытые требования")

    if blockers:
        level = CanDeliver.LOW
    elif raw >= 0.68:
        level = CanDeliver.HIGH
    elif raw >= 0.45:
        level = CanDeliver.MEDIUM
    else:
        level = CanDeliver.LOW

    return level, why_matched, why_risky


def effective_hourly_rate(gig: Gig, effort_hours: float | None) -> float | None:
    """Эффективная ставка в USD: бюджет / трудоёмкость.

    Для hourly — заявленная ставка (без деления). Для fixed — mid-бюджет,
    делённый на трудоёмкость. Если данных нет — ``None`` (не выдумываем).

    P4: результат **в USD**. Раньше возвращалось число в валюте заказа, поэтому
    ``1875 INR/ч`` лежало в одном поле с ``$30/ч`` и читалось как 1875 долларов.
    Конверсия — по курсу источника; без курса возвращается ``None``, а не сырое
    число чужой валюты.
    """
    if gig.budget_type is BudgetType.HOURLY:
        if gig.budget_min is None and gig.budget_max is None:
            return None
        values = [v for v in (gig.budget_min, gig.budget_max) if v is not None]
        raw = sum(values) / len(values)
    else:
        values = [v for v in (gig.budget_min, gig.budget_max) if v is not None]
        if not values or not effort_hours:
            return None
        raw = (sum(values) / len(values)) / float(effort_hours)

    rate = exchange_rate_of(gig.source_facts, gig.currency)
    converted = to_usd(raw, gig.currency, rate)
    return None if converted is None else round(converted, 2)
