"""First-Money Score и финальный shortlist (P6 §6–7).

ЗАЧЕМ ОТДЕЛЬНО ОТ action_score
-------------------------------
``action_score`` (P5) отвечает «разумно ли податься сейчас». Для первого заработка нужен
более узкий вопрос: **с какой вероятностью именно этот заказ даст первые деньги**. Разница
существенная: заказ с высокой оплатой и неясным ТЗ хуже дешёвого, но понятного и
выполнимого — потому что первый заказ нужен как репутация и отзыв, а не как максимум денег.

ЧТО УЧИТЫВАЕТСЯ (§7)
--------------------
live-конкуренция, свежесть, ``capability_fit``, ясность ТЗ, эффективная ставка, объём
работы, возможность быстрого результата, риск scope creep, качество клиента.

ЧЕГО НЕ ПРОИСХОДИТ
------------------
Высокая оплата **не перекрывает** слабую capability: вес capability выше веса ставки, а
``weak``/``unknown`` вообще не допускаются в APPLY_FIRST — это решается гейтом, не баллом.

ПОРОГИ ОЧЕРЕДЕЙ (§6)
--------------------
``APPLY_FIRST`` 3–5, ``BACKUP`` 5–10, остальное SKIP. Списки ограничены сверху: 15 заказов
для ручной подачи — это не shortlist, а второй список задач.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .capability import CapabilityAssessment
from .client import ClientRisk, client_risk_penalty
from .grounded import GroundedAnalysis
from .jit import JIT_FACT_KEY, live_bids_of, is_open_status
from .action import action_score_of
from .proposal import ProposalPack, build_proposal
from .currency import exchange_rate_of, to_usd
from .action import MIN_VIABLE_RATE_USD
from datetime import datetime, timezone
from .models import CanDeliver, Gig, RiskLevel
from .money import competition_band, freshness_band, freshness_score, competition_score

#: Веса вкладов. Нормируются автоматически.
FIRST_MONEY_WEIGHTS: dict[str, float] = {
    # capability выше денег: на первый заказ нельзя идти с неподтверждённой способностью
    "capability": 24.0,
    "competition": 18.0,
    "freshness": 14.0,
    "scope_clarity": 12.0,
    "rate": 12.0,
    "effort": 8.0,
    "quick_win": 4.0,
    "scope_creep": 2.0,
    "action_score": 6.0,
}

#: Размеры APPLY_FIRST / BACKUP.
APPLY_FIRST_MAX = 5
APPLY_FIRST_MIN = 3
BACKUP_MAX = 10

#: Минимальный балл для попадания в APPLY_FIRST.
#:
#: Heuristic shortlist floor, inherited from the partial P6 implementation.
#: It is not a measured probability of winning or a reason to relax the gates.
APPLY_FIRST_MIN_SCORE = 48.0
#: Минимальный балл для BACKUP.
BACKUP_MIN_SCORE = 38.0

#: Объём работы в часах, выше которого «быстрый результат» недостижим.
QUICK_WIN_MAX_HOURS = 40.0
#: Объём, выше которого растёт риск scope creep.
SCOPE_CREEP_HOURS = 120.0

CAPABILITY_SCORES: dict[str, float] = {
    "proven": 100.0,
    "strong": 82.0,
    "learnable": 45.0,
    "weak": 12.0,
    "unknown": 30.0,
}


@dataclass
class FirstMoneyScore:
    """First-Money Score с полным breakdown."""

    score: float
    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_money_score": self.score,
            "components": dict(self.components),
            "weights": dict(self.weights),
            "notes": dict(self.notes),
        }


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _rate_component(gig: Gig) -> tuple[float, str]:
    """Ставка USD/ч. Для первого заказа важен не максимум, а отсутствие бессмысленности."""
    rate = gig.effective_hourly_rate
    if rate is None:
        return 35.0, "ставка не определена — уверенность ниже"
    if rate < 2.0:
        return 5.0, f"${rate:g}/ч — ниже осмысленного порога"
    # 5..40 -> 0..100, выше 40 — максимум.
    return _clamp((rate - 5.0) / 35.0 * 100.0), f"${rate:g}/ч"


def _effort_component(gig: Gig) -> tuple[float, str]:
    """Объём работы: средний объём лучше огромного для первого заказа."""
    hours = gig.estimated_effort_hours
    if hours is None:
        return 45.0, "объём не оценён"
    if hours <= 20:
        return 95.0, f"{hours:g} ч — короткий заказ"
    if hours <= 60:
        return 80.0, f"{hours:g} ч — умеренный объём"
    if hours <= 120:
        return 55.0, f"{hours:g} ч — крупный объём"
    return 25.0, f"{hours:g} ч — очень крупный объём для первого заказа"


def _quick_win_component(gig: Gig, analysis: GroundedAnalysis) -> tuple[float, str]:
    """Можно ли показать результат быстро — это ускоряет первый отзыв."""
    hours = gig.estimated_effort_hours
    deliverables = len(analysis.deliverables)
    if hours is None:
        return 40.0, "объём неизвестен"
    if hours <= QUICK_WIN_MAX_HOURS:
        return 90.0, f"результат достижим за {hours:g} ч"
    if deliverables >= 3:
        return 45.0, f"несколько сдаваемых результатов ({deliverables}) при {hours:g} ч"
    return 30.0, f"долгий цикл: {hours:g} ч"


def _scope_creep_component(gig: Gig, analysis: GroundedAnalysis) -> tuple[float, str]:
    """Чем больше неизвестного в ТЗ, тем выше риск расползания объёма."""
    unknowns = len(analysis.unknown)
    hours = gig.estimated_effort_hours or 0.0
    score = 100.0 - unknowns * 12.0
    if hours > SCOPE_CREEP_HOURS:
        score -= 20.0
    if analysis.scope_clarity < 0.2:
        score -= 15.0
    return _clamp(score), f"неизвестных пунктов: {unknowns}; ясность ТЗ {analysis.scope_clarity}"


def first_money_score_of(
    gig: Gig,
    analysis: GroundedAnalysis,
    capability: CapabilityAssessment,
    client_risk: ClientRisk,
    *,
    now: Any = None,
) -> FirstMoneyScore:
    """Считает First-Money Score.

    Использует **live** число ставок, если перепроверка была: устаревшее значение
    описывает момент сбора, а не момент подачи.
    """
    bids = live_bids_of(gig)
    band = competition_band(bids)
    fresh = freshness_band(gig.published_at, now=now)

    cap_score = CAPABILITY_SCORES.get(capability.fit, 30.0)
    rate_score, rate_note = _rate_component(gig)
    effort_score, effort_note = _effort_component(gig)
    quick_score, quick_note = _quick_win_component(gig, analysis)
    creep_score, creep_note = _scope_creep_component(gig, analysis)

    components: dict[str, float] = {
        "capability": cap_score,
        "competition": competition_score(band),
        "freshness": freshness_score(fresh),
        "scope_clarity": _clamp(analysis.scope_clarity * 100.0),
        "rate": rate_score,
        "effort": effort_score,
        "quick_win": quick_score,
        "scope_creep": creep_score,
        "action_score": (gig.source_facts or {}).get("p6_action_score_at_refresh", action_score_of(gig, now=now).action_score),
    }
    weights = dict(FIRST_MONEY_WEIGHTS)
    notes: dict[str, str] = {
        "capability": f"capability_fit={capability.fit} (покрытие {capability.coverage})",
        "competition": f"{band}" + (f" ({bids} live ставок)" if bids is not None
                                    else " (нет live-данных)"),
        "freshness": f"{fresh}",
        "scope_clarity": f"ясность ТЗ {analysis.scope_clarity} "
                         f"({len(analysis.required_work)} пунктов работы)",
        "rate": rate_note,
        "effort": effort_note,
        "quick_win": quick_note,
        "scope_creep": creep_note,
        "action_score": "P5 action_score, unchanged formula",
        "client_risk": f"{client_risk.level}: {'; '.join(client_risk.reasons)}",
        "project_status": str(gig.source_status or "UNKNOWN"),
    }

    total_weight = sum(weights.values()) or 1.0
    weighted = sum(components[n] * weights[n] for n in weights)
    raw = _clamp(weighted / total_weight)

    # Штраф клиента применяется множителем, потому что это не «качество заказа»,
    # а риск, который сдвигает всю оценку целиком.
    penalty = client_risk_penalty(client_risk.level)
    score = _clamp(raw * penalty)
    if penalty < 1.0:
        notes["client_risk"] = f"{client_risk.level} (множитель {penalty:g})"

    # Жёсткие правила не дают деньгам перевесить слабую способность (ТЗ §7).
    if capability.fit in ("weak", "unknown"):
        cap_limit = 39.0
        if score > cap_limit:
            notes["capability_cap"] = (
                f"ограничено {cap_limit:g}: capability_fit={capability.fit} "
                "не допускается в first-money список"
            )
            score = cap_limit
    elif capability.fit == "learnable":
        cap_limit = 55.0
        if score > cap_limit:
            notes["capability_cap"] = (
                f"ограничено {cap_limit:g}: learnable идёт в BACKUP, не в APPLY_FIRST"
            )
            score = cap_limit

    # Закрытый проект не может иметь осмысленный балл: подаваться некуда.
    if not is_open_status(gig.source_status):
        notes["closed_source"] = "источник сообщает, что заказ закрыт"
        score = 0.0

    return FirstMoneyScore(
        score=round(score, 1),
        components=components,
        weights=weights,
        notes=notes,
    )


@dataclass
class ShortlistEntry:
    """Запись финального shortlist."""

    gig: Gig
    decision: str
    score: FirstMoneyScore
    analysis: GroundedAnalysis
    capability: CapabilityAssessment
    client_risk: ClientRisk
    reasons: list[str] = field(default_factory=list)
    live_verified: bool = False
    bids_at_discovery: int | None = None
    bids_live: int | None = None
    bids_delta: int | None = None
    verified_at: str | None = None
    proposal_pack: ProposalPack | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gig_id": self.gig.id,
            "title": self.gig.title,
            "project_id": self.gig.source_id,
            "project_url": self.gig.url,
            "project_status": self.gig.source_status,
            "budget": self.gig.budget_text,
            "currency": self.gig.currency,
            "published_at": self.gig.published_at,
            "estimated_effort_hours": self.gig.estimated_effort_hours,
            "effective_rate_usd": self.gig.effective_hourly_rate,
            "action_score": (self.gig.source_facts or {}).get("p6_action_score_at_refresh", action_score_of(self.gig).action_score),
            "effort_review": (self.gig.source_facts or {}).get("p6_effort_review"),
            "grounded_job_analysis": self.analysis.to_dict(),
            "decision": self.decision,
            "score": self.score.to_dict(),
            "capability": self.capability.to_dict(),
            "client_risk": self.client_risk.to_dict(),
            "reasons": list(self.reasons),
            "live_verified": self.live_verified,
            "bids_at_discovery": self.bids_at_discovery,
            "bids_live": self.bids_live,
            "bids_delta": self.bids_delta,
            "verified_at": self.verified_at,
            "proposal_pack": self.proposal_pack.to_dict() if self.proposal_pack else None,
        }


@dataclass
class Shortlist:
    """Три списка финального shortlist."""

    apply_first: list[ShortlistEntry] = field(default_factory=list)
    backup: list[ShortlistEntry] = field(default_factory=list)
    skip: list[ShortlistEntry] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "APPLY_FIRST": len(self.apply_first),
            "BACKUP": len(self.backup),
            "SKIP": len(self.skip),
        }


def _verification_fields(gig: Gig) -> dict[str, Any]:
    jit = (gig.source_facts or {}).get(JIT_FACT_KEY)
    if not isinstance(jit, dict):
        return {"live_verified": False}
    return {
        "live_verified": bool(jit.get("live_verified")),
        "bids_at_discovery": jit.get("bids_at_discovery"),
        "bids_live": jit.get("bids_live"),
        "bids_delta": jit.get("bids_delta"),
        "verified_at": jit.get("verified_at"),
    }


def build_shortlist(
    entries: list[tuple[Gig, GroundedAnalysis, CapabilityAssessment, ClientRisk]],
    *,
    now: Any = None,
) -> Shortlist:
    """Строит APPLY_FIRST / BACKUP / SKIP из подготовленных записей.

    Правила допуска (все обязательны для APPLY_FIRST):

    * ``live_verified`` — заказ перепроверен в текущем запуске (ТЗ §1);
    * проект открыт (ТЗ §2);
    * ``capability_fit`` — ``proven`` или ``strong`` (ТЗ §4);
    * ``client_risk`` не ``HIGH``;
    * балл выше порога.

    ``learnable`` уходит в BACKUP, ``weak``/``unknown`` — в SKIP (ТЗ §4).
    """
    shortlist = Shortlist()
    scored: list[ShortlistEntry] = []

    for gig, analysis, capability, client_risk in entries:
        fm = first_money_score_of(gig, analysis, capability, client_risk, now=now)
        ver = _verification_fields(gig)
        reasons: list[str] = []

        is_open = is_open_status(gig.source_status)
        if not ver["live_verified"]:
            reasons.append("not live-verified in this run")
        if not is_open:
            reasons.append("project no longer open (closed/awarded)" if
                           str(gig.source_status or "").upper() in {"CLOSED", "AWARDED"}
                           else "project status unknown or not open")
        if capability.fit in ("weak", "unknown"):
            reasons.append(f"capability_fit={capability.fit}")
        elif capability.fit == "learnable":
            reasons.append("capability_fit=learnable — backup only")
        if str(client_risk.level).upper() == "HIGH":
            reasons.append(f"client_risk={client_risk.level}")

        pack = build_proposal(gig, analysis, capability)
        readiness = []
        if not gig.url or not gig.url.startswith("https://"):
            readiness.append("missing live project URL")
        if not gig.description.strip() or not analysis.required_work:
            readiness.append("missing live description or grounded requirements")
        if (gig.source_facts or {}).get("description_live") != gig.description:
            readiness.append("description does not match the live observation")
        grounded_text = " ".join(f"{gig.title} {gig.description}".lower().split())
        for group in (analysis.required_work, analysis.required_tech, analysis.deliverables,
                      analysis.explicit_requirements, analysis.client_constraints):
            for item in group:
                evidence = " ".join(str(item.get("evidence") or "").rstrip("…").lower().split())
                if not evidence or evidence not in grounded_text:
                    readiness.append("grounded analysis contains unsupported evidence")
                    break
        if ver.get("bids_live") is None:
            readiness.append("live bids unknown")
        try:
            checked = datetime.fromisoformat(str(ver.get("verified_at")).replace("Z", "+00:00"))
            current = now or datetime.now(timezone.utc)
            if isinstance(current, str):
                current = datetime.fromisoformat(current.replace("Z", "+00:00"))
            if checked.tzinfo is None or current.tzinfo is None or not 0 <= (current - checked).total_seconds() <= 3600:
                readiness.append("live verification stale (over one hour) or future-dated")
        except (ValueError, TypeError):
            readiness.append("invalid verification timestamp")
        if not pack.is_safe() or len(pack.referenced_requirements) < 2 or not 100 <= pack.word_count <= 180:
            readiness.append("proposal not ready or unsafe")
        if pack.bid.amount is None or not pack.bid.currency or not pack.bid.delivery_days:
            readiness.append("bid or delivery recommendation missing")
        bid_usd = to_usd(pack.bid.amount, pack.bid.currency,
                         exchange_rate_of(gig.source_facts, pack.bid.currency))
        net_rate = None
        if bid_usd is not None:
            net_rate = bid_usd * .9 if pack.bid.hourly else ((bid_usd - max(bid_usd * .1, 5)) / gig.estimated_effort_hours if gig.estimated_effort_hours else None)
        if net_rate is None or net_rate < MIN_VIABLE_RATE_USD:
            readiness.append("recommended bid is not economically viable after platform fee")
        reasons.extend(readiness)

        entry = ShortlistEntry(
            gig=gig, decision="SKIP", score=fm, analysis=analysis,
            capability=capability, client_risk=client_risk, reasons=reasons, **ver,
            proposal_pack=pack,
        )

        # --- решение ---------------------------------------------------------
        blocking = (
            not ver["live_verified"]
            or not is_open
            or capability.fit in ("weak", "unknown")
            or str(client_risk.level).upper() == "HIGH"
        )
        if not blocking and not readiness and capability.allowed_for_apply_first and fm.score >= APPLY_FIRST_MIN_SCORE:
            entry.decision = "APPLY_FIRST"
        elif (capability.fit in ("proven", "strong", "learnable")
              and ver["live_verified"] and is_open
              and str(client_risk.level).upper() != "HIGH"
              and fm.score >= BACKUP_MIN_SCORE):
            entry.decision = "BACKUP"
        else:
            entry.decision = "SKIP"
            if not entry.reasons:
                entry.reasons.append("below first-money score threshold")
        scored.append(entry)

    for entry in scored:
        if entry.decision == "APPLY_FIRST":
            shortlist.apply_first.append(entry)
        elif entry.decision == "BACKUP":
            shortlist.backup.append(entry)
        else:
            shortlist.skip.append(entry)

    for bucket in (shortlist.apply_first, shortlist.backup, shortlist.skip):
        bucket.sort(key=lambda e: -e.score.score)

    # Списки ограничены: 15 заказов для ручной подачи — не shortlist (ТЗ §6).
    overflow = shortlist.apply_first[APPLY_FIRST_MAX:]
    shortlist.apply_first = shortlist.apply_first[:APPLY_FIRST_MAX]
    for entry in overflow:
        entry.decision = "BACKUP"
        entry.reasons.append("apply-first list is capped")
        shortlist.backup.append(entry)
    shortlist.backup.sort(key=lambda e: -e.score.score)
    remaining = shortlist.backup[BACKUP_MAX:]
    shortlist.backup = shortlist.backup[:BACKUP_MAX]
    for entry in remaining:
        entry.decision = "SKIP"
        entry.reasons.append("backup list is capped")
        shortlist.skip.append(entry)
    return shortlist


__all__ = [
    "APPLY_FIRST_MAX",
    "APPLY_FIRST_MIN",
    "APPLY_FIRST_MIN_SCORE",
    "BACKUP_MAX",
    "BACKUP_MIN_SCORE",
    "CAPABILITY_SCORES",
    "FIRST_MONEY_WEIGHTS",
    "FirstMoneyScore",
    "Shortlist",
    "ShortlistEntry",
    "build_shortlist",
    "first_money_score_of",
]
