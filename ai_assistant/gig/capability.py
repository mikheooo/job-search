"""Capability gate — можем ли мы честно заявить клиенту, что выполним заказ (P6 §4).

ЗАЧЕМ ОТДЕЛЬНЫЙ ГЕЙТ
--------------------
Есть разница между «мы технически могли бы разобраться» и «мы можем честно сказать
клиенту, что способны это сделать». Для первого заработка нельзя подаваться на проект
только потому, что AI теоретически поможет разобраться: проигрыш в репутации дороже
пропущенного заказа.

ГЛАВНОЕ ОТЛИЧИЕ ОТ P5
---------------------
P5 оценивал выполнимость по **совпавшим** навыкам: одно совпадение давало высокий балл.
Здесь оценивается **покрытие требуемых** технологий: если заказ требует пять технологий,
а у нас одна — это ``weak``, сколько бы совпадений ни нашлось. Именно эта логика не давала
поймать заказы вроде «Salesforce Administrator» на профиле без Salesforce.

УРОВНИ
------
``proven``    навык подтверждён реальной работой (PROFESSIONAL/PROJECT в профиле)
``strong``    покрывается существующими навыками + AI, результат реализуем уверенно
``learnable`` технически возможно, но нужно существенное изучение незнакомой технологии
``weak``      только косвенное совпадение — заявлять нельзя
``unknown``   данных мало, чтобы судить честно

В APPLY_FIRST допускаются только ``proven`` и ``strong`` (ТЗ §4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .grounded import GroundedAnalysis

#: Веса уровней профиля. Используются реальные уровни из candidate_profile.json.
LEVEL_WEIGHTS: dict[str, float] = {
    "PROFESSIONAL": 1.0,
    "PROJECT": 0.75,
    "BASIC": 0.35,
    "TRANSFERABLE": 0.25,
    "UNKNOWN": 0.0,
}

#: Технологии, которые мы закрываем уверенно, даже если их нет в списке навыков:
#: это части той же области, что и подтверждённые навыки (например, aiogram — это
#: Telegram-боты, а профиль имеет telegram на уровне PROFESSIONAL).
ADJACENT_COVERAGE: dict[str, str] = {
    "aiogram": "telegram",
    "telethon": "telegram",
    "python-telegram-bot": "telegram",
    "asyncio": "python",
    "pandas": "python",
    "fastapi": "rest api",
    "flask": "rest api",
    "django": "python",
    "postgresql": "sql",
    "mysql": "sql",
    "sqlite": "sql",
    "docker": "docker",
    "bash": "bash",
    "powershell": "powershell",
    "linux": "linux",
    "git": "git",
    "web scraping": "python",
    "selenium": "python",
    "playwright": "python",
    "beautifulsoup": "python",
    "csv": "python",
    "excel": "python",
    "google sheets": "python",
    "openai api": "llm",
    "chatgpt/openai": "llm",
    "gpt": "llm",
    "gemini": "llm",
    "claude": "llm",
    "webhooks": "webhooks",
    "rest api": "rest api",
    "api": "rest api",
    "n8n": "n8n",
    "make.com": "make",
    "zapier": "make",
    "telegram/чат-бот": "telegram",
    "chatbot": "telegram",
    "ocr": "python",
    "ocr/tesseract": "python",
    "etl": "python",
    "asyncio ": "python",
}

#: Обобщённые ярлыки навыков, которые НЕ являются технологией. Клиент ставит их в список
#: навыков, чтобы описать работу («data extraction»), а не чтобы потребовать конкретный
#: инструмент. Измерено: без этого исключения скрапер-заказ с покрытыми Python,
#: BeautifulSoup, Playwright, CSV и web scraping получал `weak`, потому что «не покрыто:
#: data extraction, data mining, data processing» — то есть терял балл за описание работы,
#: которую он же и выполняет.
GENERIC_SKILL_LABELS: frozenset[str] = frozenset({
    "data extraction", "data mining", "data processing", "data scraping",
    "data entry", "data management", "database management", "data analysis",
    "web development", "web design", "website design", "website development",
    "software architecture", "api development", "api integration", "api testing",
    "chatbot development", "bot development", "game development",
    "frontend development", "backend development", "full stack development",
    "mobile app development", "app development", "automation", "ai automation",
    "project management", "risk management", "content creation", "content writing",
    "analytics", "reporting", "documentation", "testing", "qa", "troubleshooting",
    "integration", "migration", "optimization", "monitoring", "architecture",
    "devops", "machine learning", "artificial intelligence", "natural language processing",
})

#: Области, где профиль заведомо не имеет подтверждённых навыков. Если заказ строится
#: вокруг них, честный уровень — ``weak`` или ``learnable``, но не ``strong``.
FOREIGN_STACKS: tuple[str, ...] = (
    "salesforce", "hubspot", "monday.com", "zoho", "bitrix24", "shopify",
    "woocommerce", "wordpress", "laravel", "react native", "supabase", "firebase",
    "kubernetes", "google cloud", "stripe", "tilda", "тильда", "figma",
    "3d modeling", "3d animation", "blender",
)


def _weight(level: str | None, confidences: dict[str, float]) -> float:
    """Вес навыка по его профильному уровню. Неизвестный -> 0."""
    if not level:
        return 0.0
    return float(confidences.get(level, 0.0))


@dataclass
class CapabilityAssessment:
    """Итог capability-гейта с объяснением."""

    fit: str
    coverage: float
    required_total: int
    required_covered: int
    covered: list[tuple[str, str, float]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    foreign: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def allowed_for_apply_first(self) -> bool:
        return self.fit in ("proven", "strong")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fit": self.fit,
            "coverage": self.coverage,
            "required_total": self.required_total,
            "required_covered": self.required_covered,
            "covered": [{"tech": t, "via": v, "weight": w} for t, v, w in self.covered],
            "missing": list(self.missing),
            "foreign": list(self.foreign),
            "notes": list(self.notes),
        }


def assess_capability(
    analysis: GroundedAnalysis,
    confidences: dict[str, float],
    *,
    profile_skills: dict[str, str] | None = None,
) -> CapabilityAssessment:
    """Оценивает честное покрытие требуемых технологий.

    ``confidences`` — веса уровней (skill_confidences из профиля).
    ``profile_skills`` — карта «навык -> уровень» (skill_confidence), нужна для
    определения ``proven``: это подтверждение реальной работы, а не близости области.
    """
    levels = {k.lower(): v for k, v in (profile_skills or {}).items()}
    # Из требуемых исключаются обобщённые ярлыки: они описывают работу, которую мы
    # и так выполняем подтверждёнными инструментами, поэтому не могут считаться
    # «незнакомой технологией». Разделение нужно, чтобы оценка отражала реальное
    # незнание, а не формулировку клиента.
    all_required = [t["name"] for t in analysis.required_tech]
    required = [t for t in all_required if t.strip().lower() not in GENERIC_SKILL_LABELS]
    dropped = [t for t in all_required if t.strip().lower() in GENERIC_SKILL_LABELS]
    if not required:
        return CapabilityAssessment(
            fit="unknown", coverage=0.0, required_total=0, required_covered=0,
            notes=[
                "в объявлении не названо ни одной конкретной технологии "
                f"(только общие описания: {', '.join(dropped[:4])}) — "
                "судить о способности нечестно",
            ],
        )

    covered: list[tuple[str, str, float]] = []
    missing: list[str] = []
    foreign: list[str] = []

    for tech in required:
        key = tech.strip().lower()
        if key in FOREIGN_STACKS:
            foreign.append(tech)
            missing.append(tech)
            continue
        # 1. Прямое совпадение навыка.
        if key in confidences:
            covered.append((tech, key, confidences[key]))
            continue
        # 2. Покрытие через соседний подтверждённый навык.
        adjacent = ADJACENT_COVERAGE.get(key)
        if adjacent and adjacent in confidences:
            covered.append((tech, adjacent, confidences[adjacent] * 0.85))
            continue
        # 3. Частичное совпадение по подстроке (например "python 3" -> "python").
        import re
        partial = next((s for s in confidences if re.fullmatch(re.escape(s) + r"\s*\d+(?:\.\d+)*", key)), None)
        if partial:
            covered.append((tech, partial, confidences[partial] * 0.7))
            continue
        missing.append(tech)

    total = len(required)
    coverage = round(sum(w for _, _, w in covered) / total, 3) if total else 0.0

    # --- определение уровня -------------------------------------------------
    # proven: все требуемые технологии покрыты подтверждёнными навыками уровня
    # PROFESSIONAL/PROJECT, и чужих стеков нет.
    strong_levels = {"professional", "project"}
    proven_covered = 0
    for tech, via, _weight_value in covered:
        level = str(levels.get(via) or levels.get(tech.lower()) or "").lower()
        if level in strong_levels and via == tech.lower():
            proven_covered += 1
    proven_ratio = proven_covered / total if total else 0.0

    if foreign:
        fit = "weak"
        notes = [
            f"заказ строится на стеке вне нашего опыта: {', '.join(foreign)}",
            "заявлять клиенту способность выполнить это нельзя",
        ]
    elif missing and coverage < 0.5:
        fit = "weak"
        notes = [
            f"покрыто {len(covered)} из {total} требуемых технологий",
            f"не покрыто: {', '.join(missing[:5])}",
        ]
    elif not missing and coverage >= 0.85 and proven_ratio == 1.0:
        fit = "proven"
        notes = [
            f"покрыто {len(covered)} из {total}; основная часть подтверждена реальными навыками",
        ]
    elif not missing and coverage >= 0.7:
        fit = "strong"
        notes = [
            f"покрыто {len(covered)} из {total}; результат реализуем уверенно",
        ]
    elif coverage >= 0.45:
        fit = "learnable"
        notes = [
            f"покрыто {len(covered)} из {total}; потребуется изучение: "
            f"{', '.join(missing[:4])}",
        ]
    else:
        fit = "weak"
        notes = [
            f"покрыто только {len(covered)} из {total} требуемых технологий",
        ]

    if analysis.scope_clarity < 0.15 and fit in ("proven", "strong"):
        # Ясность ТЗ низкая — это не про способность, а про риск, поэтому отмечаем,
        # но уровень не понижаем: иначе одни и те же данные давали бы разные уровни.
        notes.append("ТЗ сформулировано расплывчато — высок риск неверной оценки объёма")

    return CapabilityAssessment(
        fit=fit,
        coverage=coverage,
        required_total=total,
        required_covered=len(covered),
        covered=covered,
        missing=missing,
        foreign=foreign,
        notes=notes,
    )


def capability_allows_apply_first(fit: str) -> bool:
    """Допускает ли уровень подачу в first-money режиме (ТЗ §4)."""
    return fit in ("proven", "strong")


def capability_decision_note(fit: str) -> str:
    """Короткое объяснение решения по уровню — для отчёта и tracker."""
    return {
        "proven": "подтверждённый опыт: можно подаваться",
        "strong": "выполнимо уверенно: можно подаваться",
        "learnable": "выполнимо с изучением: только в BACKUP, не в first money",
        "weak": "недостаточное покрытие: не подаваться",
        "unknown": "данных мало для честной оценки: не подаваться",
    }.get(fit, "неизвестный уровень")


__all__ = [
    "ADJACENT_COVERAGE",
    "FOREIGN_STACKS",
    "GENERIC_SKILL_LABELS",
    "LEVEL_WEIGHTS",
    "CapabilityAssessment",
    "assess_capability",
    "capability_allows_apply_first",
    "capability_decision_note",
]
