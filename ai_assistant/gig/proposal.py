"""Proposal Pack — готовый отклик, план, ставка и вопрос клиенту (P6 §9).

ГЛАВНОЕ ПРАВИЛО
---------------
**Никаких выдуманных компетенций.** Запрещено писать «8 лет Salesforce», «десятки
WhatsApp-систем», «эксперт Monday.com», если этого нет в профиле. Отклик выигрывает
пониманием задачи, планом, скоростью и ценой — не выдуманной историей.

ПОЧЕМУ ШАБЛОНЫ СТРОЯТСЯ ИЗ БРИФА
---------------------------------
Отклик обязан ссылаться минимум на **два конкретных требования** из объявления. Если
требований нет, отклик получает честную оговорку, а не общие слова: generic-текст
одинаково выглядит для любого заказа, поэтому его легко отличить и он не работает.

ЧТО ЗДЕСЬ ЕСТЬ
--------------
* ``proposal``          100–180 слов, персонализированный, 2+ требований из брифа;
* ``technical_approach`` 3–6 шагов строго по реальному ТЗ;
* ``bid``               рекомендованная ставка, срок, milestones с обоснованием;
* ``clarifying_question`` один вопрос, ответа на который в объявлении нет;
* ``risks``             что увеличит срок или стоимость;
* ``proof_idea``        что можно показать за 15–30 минут, без бесплатной работы.
"""
from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from typing import Any

from .capability import CapabilityAssessment
from .grounded import TECH_VOCABULARY, GroundedAnalysis, unsupported_claims
from .models import BudgetType, Gig

#: Фразы, которые нельзя писать без подтверждения в профиле. Используются как
#: блоклист при финальной проверке текста отклика.
FORBIDDEN_CLAIM_PATTERNS: tuple[str, ...] = (
    r"\b\d+\s*\+?\s*years?\s+(?:of\s+)?experience\b",
    r"\b\d+\s*\+?\s*years?\s+(?:working|developing|building)\b",
    r"\b(?:i\s+am|i'm)\s+an?\s+expert\b",
    r"\b(?:certified|certification)\b",
    r"\b(?:dozens|hundreds|many)\s+(?:of\s+)?(?:projects|clients|systems)\b",
    r"\b(?:i\s+have\s+built|i've\s+built)\s+(?:dozens|hundreds|\d+)\b",
    r"\b(?:top|leading|best|#1)\s+(?:developer|freelancer|expert)\b",
    r"\bsuccessfully\s+delivered\s+\d+\b",
    r"\bmy\s+background\s+is\b",
    r"\bwork\s+i\s+do\s+regularly\b",
    r"\bi\s+(?:recently\s+completed|have\s+(?:built|completed|worked))\b",
)

#: Что можно честно сказать о себе. Выбирается ТОЛЬКО то, что подтверждено проектом.
#: Измеренный дефект: фиксированная строка «API and webhook integration work» добавляла
#: webhooks в отклик даже там, где бриф о них не говорит, — это выдуманная компетенция.
#: Поэтому набор строится из фактических требований, а не из готового списка.
STRENGTH_BY_TECH: dict[str, str] = {
    "python": "Python automation and integrations",
    "django": "Python backend development",
    "fastapi": "Python API services",
    "flask": "Python API services",
    "api": "API integration work",
    "rest api": "REST API integration work",
    "webhooks": "webhook integrations",
    "web scraping": "data extraction from web pages",
    "telegram": "Telegram bots and workflow automation",
    "whatsapp": "chat automation",
    "n8n": "workflow automation",
    "make.com": "workflow automation",
    "openai api": "LLM-backed automation",
    "chatgpt/openai": "LLM-backed automation",
    "llm": "LLM-backed automation",
    "sql": "database and SQL work",
    "postgresql": "databases and SQL",
    "mysql": "databases and SQL",
    "excel": "spreadsheet processing",
    "csv": "data processing and CSV pipelines",
    "google sheets": "spreadsheet automation",
    "docker": "containerised deployments",
    "linux": "Linux server work",
    "powershell": "Windows and PowerShell automation",
}

#: Слова, по которым строится уточняющий вопрос, если в брифе не хватает данных.
QUESTION_FOR_UNKNOWN: tuple[tuple[str, str], ...] = (
    ("доступы и существующие системы не описаны",
     "Which systems or accounts should I have access to, and do you already have API credentials for them?"),
    ("сдаваемый результат не описан",
     "What would a successful first milestone look like on your side — what exactly should I hand over?"),
    ("срок выполнения не указан",
     "Is there a target date you are working towards, or is the schedule flexible?"),
    ("условия оплаты не детализированы",
     "Would you prefer a fixed price per milestone or an hourly arrangement for this scope?"),
    ("требуемая работа не сформулирована явно",
     "Could you describe the main outcome you want from this project in one or two sentences?"),
    ("технологии не названы",
     "Do you have a preferred stack for this, or should I propose one?"),
)


def _first_sentences_for(analysis: GroundedAnalysis, tech_names: list[str], limit: int = 2) -> list[str]:
    """Выбирает конкретные требования из брифа — те, на которые сошлётся отклик."""
    picked: list[str] = []
    wanted = {t.lower() for t in tech_names}
    for item in analysis.required_work:
        text = item["work"]
        if text not in picked:
            picked.append(text)
        if len(picked) >= limit:
            break
    return picked


def _clean_requirement(text: str, limit: int = 130) -> str:
    """Очищает цитату брифа: убирает разметку и обрезает по границе слова.

    Измеренный дефект: сырая цитата давала «- What You'll Work On * Develop….»
    и обрывки вроде «withd…» посреди слова. Такой текст нельзя отправлять клиенту.
    """
    cleaned = " ".join((text or "").split())
    # Маркеры списков, жирный шрифт markdown, звёздочки и тире в начале.
    cleaned = re.sub(r"[*`#]+", "", cleaned)
    cleaned = re.sub(r"^(?:[-•·]|\d+[.)])\s*", "", cleaned)
    cleaned = cleaned.strip(" -–—:;,.")
    if len(cleaned) <= limit:
        return cleaned
    # Обрезка по последнему целому слову, без обрубков.
    cut = cleaned[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" -–—:;,.") + "…"


def _short_requirement(text: str, limit: int = 130) -> str:
    """Сокращает цитату так, чтобы она читалась внутри предложения отклика."""
    return _clean_requirement(text, limit)


@dataclass
class BidRecommendation:
    """Рекомендация по цене и сроку с обоснованием."""

    amount: float | None
    currency: str | None
    basis: str
    hourly: bool = False
    delivery_days: int | None = None
    milestones: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommended_bid": self.amount,
            "currency": self.currency,
            "basis": self.basis,
            "hourly": self.hourly,
            "delivery_time": None if self.delivery_days is None else f"{self.delivery_days} days",
            "milestones": list(self.milestones),
            "note": self.note,
        }


@dataclass
class ProposalPack:
    """Готовый набор для подачи."""

    proposal: str
    technical_approach: list[str]
    bid: BidRecommendation
    clarifying_question: str
    risks: list[str]
    proof_idea: str
    word_count: int
    referenced_requirements: list[str] = field(default_factory=list)
    injected_claims: list[str] = field(default_factory=list)
    unsupported_tech: list[str] = field(default_factory=list)
    relevant_kb_facts: list[str] = field(default_factory=list)

    def is_safe(self) -> bool:
        """Отклик не содержит выдуманных компетенций и лишних технологий."""
        return not self.injected_claims and not self.unsupported_tech

    def to_dict(self) -> dict[str, Any]:
        return {
            "PROPOSAL": self.proposal,
            "TECHNICAL_APPROACH": self.technical_approach,
            "BID": self.bid.to_dict(),
            "CLARIFYING_QUESTION": self.clarifying_question,
            "RISKS": self.risks,
            "PROOF_IDEA": self.proof_idea,
            "word_count": self.word_count,
            "referenced_requirements": self.referenced_requirements,
            "injected_claims": self.injected_claims,
            "unsupported_tech": self.unsupported_tech,
            "relevant_kb_facts": self.relevant_kb_facts,
        }


def find_forbidden_claims(text: str) -> list[str]:
    """Находит запрещённые утверждения о себе. Пустой список — текст честный."""
    found: list[str] = []
    for pattern in FORBIDDEN_CLAIM_PATTERNS:
        match = re.search(pattern, text, re.I)
        if match:
            found.append(match.group(0))
    return found


def _delivery_days(gig: Gig) -> int | None:
    hours = gig.estimated_effort_hours
    if not hours:
        return None
    # 6 рабочих часов в день: оценка трудоёмкости — это часы работы, а не календарь.
    return max(2, math.ceil(hours / 6.0))


def _milestones(gig: Gig, analysis: GroundedAnalysis) -> list[str]:
    """3 этапа: каркас → основная работа → сдача. Строятся из объёма и брифа."""
    days = _delivery_days(gig) or 7
    first = max(1, days // 3)
    second = max(first + 1, (days * 2) // 3)
    work = analysis.required_work[:1]
    label = _short_requirement(work[0]["work"], 60) if work else "core implementation"
    if days <= 2:
        return ["Day 1: agree acceptance criteria and show a working example",
                f"Day 2: {label}; validate and hand over the agreed deliverables"]
    return [
        f"Day 1–{first}: working prototype and agreed inputs and access verified",
        f"Day {first + 1}–{second}: {label}",
        f"Day {second + 1}–{days}: final testing, documentation and handover",
    ]


def recommend_bid(gig: Gig, analysis: GroundedAnalysis, capability: CapabilityAssessment) -> BidRecommendation:
    """Рекомендует цену и срок.

    Логика: не минимум и не максимум. Для первого заказа важно выиграть при осмысленной
    ставке, поэтому ориентир — нижняя половина диапазона клиента, но не ниже $10/ч
    эквивалента. Если бюджет не опубликован, честно об этом говорится.
    """
    currency = gig.currency
    if gig.budget_type is BudgetType.HOURLY and gig.budget_min is not None:
        low = gig.budget_min
        high = gig.budget_max if gig.budget_max is not None else gig.budget_min
        # Берём чуть ниже середины: это выигрывает при разумной ставке.
        amount = round(low + (high - low) * 0.4, 2)
        return BidRecommendation(
            amount=amount, currency=currency, hourly=True,
            basis=(
                f"your range is {low:g}-{high:g} {currency or ''}/hr; I bid slightly below "
                "the midpoint to win the first engagement without undercutting"
            ),
            delivery_days=_delivery_days(gig),
            milestones=["Weekly milestone with a working increment and a short demo"],
            note="hourly: the schedule is agreed as we go, the rate is fixed up front",
        )

    if gig.budget_min is not None or gig.budget_max is not None:
        low = gig.budget_min or gig.budget_max
        high = gig.budget_max or gig.budget_min
        amount = round(low + (high - low) * 0.45, 2)
        return BidRecommendation(
            amount=amount, currency=currency,
            basis=(
                f"your range is {low:g}-{high:g} {currency or ''}; I propose slightly below "
                "the midpoint with a fixed price per milestone"
            ),
            delivery_days=_delivery_days(gig),
            milestones=_milestones(gig, analysis),
            note="a fixed price split by milestone lowers risk on both sides",
        )

    return BidRecommendation(
        amount=None, currency=currency,
        basis="no budget is published in the brief",
        delivery_days=_delivery_days(gig),
        milestones=_milestones(gig, analysis),
        note="the price needs to be agreed after the scope is clarified, otherwise it is a guess",
    )


def _proof_idea(gig: Gig, analysis: GroundedAnalysis) -> str:
    """Что показать за 15–30 минут, не делая бесплатно существенную часть заказа."""
    techs = {t["name"].lower() for t in analysis.required_tech}
    if "web scraping" in techs or any("scrap" in t for t in techs):
        return ("I can scrape two or three public pages and show the exact output "
                "structure (CSV/JSON) in about 20 minutes, without touching your systems.")
    if "telegram" in techs:
        return ("I can send a short message-flow diagram (command -> task -> reply) plus a "
                "sample bot output on a test token, in about 20 minutes.")
    if {"api", "rest api"} & techs:
        return ("I can show the call plan: which endpoints, in what order, and how errors "
                "and retries are handled. 15 minutes, without touching your account.")
    if {"excel", "csv", "google sheets"} & techs:
        return ("I can show a sample transformation on synthetic rows: input -> cleanup -> "
                "export. 20 minutes, your files are not needed.")
    return ("I can write a one-page solution outline and milestone plan so you can see how "
            "I understood the task before any work starts.")


def _clarifying_question(analysis: GroundedAnalysis) -> str:
    """Один вопрос, ответа на который в объявлении нет."""
    for marker, question in QUESTION_FOR_UNKNOWN:
        if marker in analysis.unknown:
            return question
    return ("What would make this project a clear success for you in the first two weeks, "
            "beyond delivering the listed requirements?")


def _risks(gig: Gig, analysis: GroundedAnalysis, capability: CapabilityAssessment) -> list[str]:
    """Риски для клиента и для нас. На английском — это часть пакета для отправки."""
    risks: list[str] = []
    if capability.fit == "learnable":
        risks.append("Part of the stack is new to me — expect extra time for ramp-up.")
    if "срок выполнения не указан" in analysis.unknown:
        risks.append("No deadline is stated: a tight date would require trimming scope.")
    if "доступы и существующие системы не описаны" in analysis.unknown:
        risks.append("Access details are not described: start may shift while credentials arrive.")
    if "сдаваемый результат не описан" in analysis.unknown:
        risks.append("The expected output is not stated precisely — expectations could diverge.")
    if (gig.estimated_effort_hours or 0) > 100:
        risks.append("Large scope: without milestones the requirements may expand.")
    if gig.competition is not None and gig.competition > 20:
        risks.append(f"This posting already has {gig.competition} proposals, "
                     "so a reply may come later than others.")
    if not risks:
        risks.append("No material risk is visible in the brief; the main one is the usual "
                     "uncertainty about details.")
    return risks


def _approach(gig: Gig, analysis: GroundedAnalysis) -> list[str]:
    """3–6 шагов строго по реальному ТЗ. Ничего, чего нет в брифе.

    На английском: это текст для отправки клиенту, и он должен быть на одном
    языке с proposal. Смешение языков в одном пакете — дефект, а не мелочь.
    """
    steps: list[str] = []
    steps.append("Confirm the exact scope against your brief and send back a short "
                 "checklist for agreement.")
    techs = [t["name"] for t in analysis.required_tech][:4]
    if techs:
        steps.append(f"Implement the core on {', '.join(techs)}, starting from a minimal "
                     "working path end to end.")
    for item in analysis.required_work[:2]:
        steps.append(f"Deliver: {_clean_requirement(item['work'], 110)}.")
    if analysis.deliverables:
        steps.append("Hand over: "
                     f"{_clean_requirement(analysis.deliverables[0]['deliverable'], 110)}.")
    steps.append("Demo the result, apply your feedback, then hand over the code and access.")
    return steps[:6]


def build_proposal(
    gig: Gig,
    analysis: GroundedAnalysis,
    capability: CapabilityAssessment,
) -> ProposalPack:
    """Собирает готовый пакет для подачи.

    Отклик в 100–180 слов, ссылающийся минимум на два требования из брифа. Все
    технологии, упомянутые в тексте, обязаны быть в брифе — это проверяется.
    """
    techs = [t["name"] for t in analysis.required_tech][:3]
    refs = _first_sentences_for(analysis, techs, limit=2)

    lines: list[str] = []
    lines.append("Hello,")
    lines.append("")

    # 1. Ссылка на конкретику брифа — это и делает отклик не-generic.
    if refs:
        cleaned = [_short_requirement(r, 120) for r in refs]
        if len(cleaned) >= 2:
            lines.append(
                f"Reading your brief, two things are central: {cleaned[0]} "
                f"and {cleaned[1]}."
            )
        else:
            lines.append(f"Reading your brief, the central requirement is: {cleaned[0]}.")
    else:
        lines.append("Your brief describes the project without listing explicit requirements, "
                     "so I will start by pinning down the exact scope with you.")

    # 2. Что я предлагаю — с опорой на названную в брифе технологию.
    if techs:
        lines.append(
            f"I would build this with {', '.join(techs)} and deliver it in small, "
            "verifiable steps so you can see progress early."
        )
    else:
        lines.append("I would deliver this in small, verifiable steps so you can see "
                     "progress early.")

    # 3. Честная база навыков — только те, что подтверждены И названы в брифе.
    # Это одновременно и честно (мы действительно это делаем), и релевантно.
    lines.append(
        "Before implementation, I would agree a small acceptance checklist based on "
        "these requirements. You would receive an early working example, then the "
        "completed deliverables with clear setup instructions and a final walkthrough. "
        "Any additional scope would be estimated separately before proceeding."
    )

    # 4. Первый шаг и вопрос — движение к сделке.
    lines.append(
        "I can start with a short working prototype and a clear plan before any "
        "larger commitment."
    )
    lines.append("")
    lines.append("Happy to answer any question before we start.")

    proposal = "\n".join(lines)
    words = len(proposal.split())

    bid = recommend_bid(gig, analysis, capability)
    kb_facts: list[str] = []
    try:
        from .kb_loader import find_matching_kb_facts
        matched_facts = find_matching_kb_facts([t["name"] for t in analysis.required_tech], limit=2)
        kb_facts = [f["value"] for f in matched_facts]
    except Exception:
        kb_facts = []

    pack = ProposalPack(
        proposal=proposal,
        technical_approach=_approach(gig, analysis),
        bid=bid,
        clarifying_question=_clarifying_question(analysis),
        risks=_risks(gig, analysis, capability),
        proof_idea=_proof_idea(gig, analysis),
        word_count=words,
        referenced_requirements=refs,
        relevant_kb_facts=kb_facts,
    )

    # --- проверки честности -------------------------------------------------
    pack.injected_claims = find_forbidden_claims(proposal)
    pack.unsupported_tech = unsupported_claims(analysis, proposal)
    return pack


__all__ = [
    "FORBIDDEN_CLAIM_PATTERNS",
    "QUESTION_FOR_UNKNOWN",
    "STRENGTH_BY_TECH",
    "BidRecommendation",
    "ProposalPack",
    "build_proposal",
    "find_forbidden_claims",
    "recommend_bid",
]
