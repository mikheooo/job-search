"""Pipeline контура Gig Search.

Путь одного заказа (ТЗ §1):

    source → normalize → deduplicate → hard gates → assessment → scoring
           → ranked gigs

Порядок намеренный: дедуп и gates ДО scoring, потому что бессмысленно считать
opportunity_score заказу, который в принципе нельзя взять (нет оплаты, нет
доступа, спам). Оценки (can_deliver/payment/effort/rate/client/risk) считаются
до scoring, так как scoring их использует.

Контур prepare-only: pipeline ничего не отправляет наружу. Единственные
побочные эффекты — записи в таблицы ``gigs``/``gig_events``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from . import hard_gates, scoring
from .dedup import find_duplicate
from .assessment import (
    assess_ai_automation_potential,
    assess_can_deliver,
    assess_client_quality,
    assess_payment_readiness,
    assess_risks,
    effective_hourly_rate,
    estimate_effort,
    load_candidate_profile,
    skill_confidences,
)
from .models import Gig, PaymentReadiness, RejectionReason, RiskLevel
from .normalizer import normalize_gig
from .sources.base import GigSource
from .store import (
    find_gig_by_source_id,
    find_gig_by_url,
    gig_stats,
    iter_existing_gigs_for_dedup,
    record_gig_event,
    save_gig,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class GigRunResult:
    """Результат прогона pipeline: статистика + обработанные гиги."""

    total: int = 0
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    errors: int = 0
    #: Сколько записей пришло из живого источника, а сколько из фикстуры (P2 §7).
    live: int = 0
    fixture: int = 0
    #: Принятые гиги, отсортированные по opportunity_score (ranкed gigs).
    ranked_gigs: list[Gig] = field(default_factory=list)
    #: Отклонённые гиги с причинами.
    rejected_gigs: list[Gig] = field(default_factory=list)
    #: Дубликаты: (id дубля, id канонического гига, причина).
    duplicate_records: list[tuple[str, str, str]] = field(default_factory=list)
    #: Ошибки нормализации: (сырой source_id, сообщение).
    error_records: list[tuple[str, str]] = field(default_factory=list)
    persisted: bool = True

    def stats(self) -> dict[str, int]:
        return {
            "total": self.total,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "errors": self.errors,
            "live": self.live,
            "fixture": self.fixture,
        }


class GigPipeline:
    """Оркестратор контура Gig Search."""

    def __init__(
        self,
        profile: dict[str, Any] | None = None,
        *,
        persist: bool = True,
        min_budget: float | None = None,
        weights: dict[str, float] | None = None,
        include_closed: bool = False,
    ) -> None:
        self.profile = profile if profile is not None else load_candidate_profile()
        self.confidences = skill_confidences(self.profile)
        self.persist = persist
        self.min_budget = min_budget
        self.weights = weights
        #: P2: live-источник может отдавать только архивные записи. По умолчанию
        #: они отклоняются гейтом SOURCE_CLOSED; ``True`` включает их в scoring
        #: для аналитического просмотра (заказ остаётся помечен как закрытый).
        self.include_closed = include_closed

    # ------------------------------------------------------------------
    def process_raw(self, raw: dict) -> Gig:
        """source → normalize → dedup → gates → assessment → scoring.

        Возвращает полностью обогащённый ``Gig`` (или с rejection_reasons).
        Не пишет в БД — запись делает ``run``.
        """
        gig = normalize_gig(raw)
        return self.process_gig(gig, existing=iter_existing_gigs_for_dedup() if self.persist else [])

    def process_gig(self, gig: Gig, *, existing: list[Gig] | None = None) -> Gig:
        """Обогащает уже нормализованный гиг. Без БД-эффектов."""
        existing_gigs = existing if existing is not None else []

        # --- deduplicate -------------------------------------------------
        duplicate, reason = find_duplicate(gig, existing_gigs)
        # Проверка по БД (source_id / URL) — на случай, если снапшот неполон.
        if duplicate is None and self.persist:
            stored = find_gig_by_source_id(gig.source, gig.source_id)
            if stored is not None and stored.id != gig.id:
                duplicate, reason = stored, f"stored source+source_id: {gig.source}:{gig.source_id}"
            elif stored is not None:
                duplicate, reason = stored, f"already stored: {gig.id}"
            else:
                by_url = find_gig_by_url(gig.url)
                if by_url is not None:
                    duplicate, reason = by_url, f"stored canonical URL: {gig.url}"

        # --- hard gates ---------------------------------------------------
        gate = hard_gates.evaluate_hard_gates(
            gig,
            duplicate_reason=reason if duplicate is not None else None,
            min_budget=self.min_budget,
            reject_closed_source=not self.include_closed,
        )
        gig.rejection_reasons = list(gate.reasons)

        # --- assessment (нужен и для принятых, и для отчёта по rejected) ----
        gig.payment_readiness = assess_payment_readiness(gig)
        # payment_readiness=blocked — отдельный hard gate (ТЗ §5).
        if gig.payment_readiness is PaymentReadiness.BLOCKED and RejectionReason.PAYMENT_UNAVAILABLE.value not in gig.rejection_reasons:
            gig.rejection_reasons.append(RejectionReason.PAYMENT_UNAVAILABLE.value)

        gig.estimated_effort_hours = estimate_effort(gig)
        gig.ai_automation_potential = assess_ai_automation_potential(gig)
        gig.client_quality = assess_client_quality(gig)
        can_deliver, why_matched, why_risky = assess_can_deliver(gig, self.confidences)
        gig.can_deliver = can_deliver
        risk_level, risks = assess_risks(gig, self.confidences)
        gig.risk_level = risk_level
        gig.risks = risks

        # P2: источник сам сообщает статус - это риск, а не наша догадка.
        if gig.source_status and str(gig.source_status).strip().lower() in (
            "expired",
            "closed",
            "cancelled",
            "canceled",
            "filled",
        ):
            note = f"источник сообщает статус {gig.source_status}: заказ больше не открыт"
            if note not in risks:
                risks.append(note)
                gig.risks = risks
            if risk_level is RiskLevel.LOW:
                gig.risk_level = RiskLevel.MEDIUM
        gig.why_matched = why_matched
        gig.why_risky = why_risky
        gig.effective_hourly_rate = effective_hourly_rate(gig, gig.estimated_effort_hours)

        if gig.rejection_reasons:
            # Отклонённые заказы scoring НЕ получают: балл вводил бы в заблуждение.
            gig.opportunity_score = None
            gig.match_score = None
            gig.score_breakdown = {"rejected_before_scoring": True, "reasons": list(gig.rejection_reasons)}
            gig.updated_at = _utcnow()
            return gig

        # --- scoring -------------------------------------------------------
        gig.match_score = scoring.match_score_of(gig, self.confidences)
        breakdown = scoring.score_gig(
            gig,
            self.confidences,
            client_quality=gig.client_quality,
            effective_rate=gig.effective_hourly_rate,
            weights=self.weights,
        )
        gig.opportunity_score = breakdown.opportunity_score
        gig.score_breakdown = breakdown.to_dict()
        gig.updated_at = _utcnow()
        return gig

    # ------------------------------------------------------------------
    def run(
        self,
        sources: Iterable[GigSource],
        limit_per_source: int | None = None,
        *,
        persist: bool | None = None,
    ) -> GigRunResult:
        """Полный прогон по источникам. Возвращает ранг; пишет в БД (если persist)."""
        do_persist = self.persist if persist is None else persist
        result = GigRunResult(persisted=do_persist)

        existing = iter_existing_gigs_for_dedup() if do_persist else []
        seen_ids: set[str] = {g.id for g in existing}

        for source in sources:
            for raw in source.fetch(limit=limit_per_source):
                result.total += 1
                try:
                    gig = normalize_gig(raw)
                    # Дубликаты внутри текущего прогона тоже ловим.
                    gig = self.process_gig(gig, existing=existing)
                except (ValueError, TypeError) as exc:
                    result.errors += 1
                    result.error_records.append((str(raw.get("source_id") or raw.get("id") or "?"), str(exc)))
                    continue

                if gig.rejection_reasons == [RejectionReason.DUPLICATE.value] or (
                    RejectionReason.DUPLICATE.value in gig.rejection_reasons
                ):
                    result.duplicates += 1
                elif gig.rejection_reasons:
                    result.rejected += 1
                else:
                    result.accepted += 1

                # P2 §7: live и fixture считаются отдельно, чтобы их нельзя было
                # случайно смешать в одном отчёте.
                if gig.source_mode.value == "live":
                    result.live += 1
                elif gig.source_mode.value == "fixture":
                    result.fixture += 1

                if do_persist:
                    try:
                        save_gig(gig)
                        if gig.rejection_reasons:
                            record_gig_event(gig.id, "REJECTED", {"reasons": list(gig.rejection_reasons)})
                        else:
                            record_gig_event(
                                gig.id,
                                "SCORED",
                                {"opportunity_score": gig.opportunity_score},
                            )
                    except Exception as exc:  # noqa: BLE001 — не роняем прогон из-за одной записи
                        result.errors += 1
                        result.error_records.append((gig.id, f"persist failed: {exc}"))

                if gig.rejection_reasons:
                    result.rejected_gigs.append(gig)
                    if RejectionReason.DUPLICATE.value in gig.rejection_reasons:
                        result.duplicate_records.append((gig.id, "", "; ".join(gig.rejection_reasons)))
                else:
                    result.ranked_gigs.append(gig)

                # Дедуп внутри одного прогона не должен зависеть от записи в БД:
                # второй экземпляр того же заказа — дубль и при --no-persist.
                # Раньше список пополнялся только при do_persist, поэтому прогон
                # в режиме анализа показывал нулевые дубли там, где они есть.
                if gig.id not in seen_ids:
                    seen_ids.add(gig.id)
                    existing.append(gig)

        result.ranked_gigs.sort(key=lambda g: (g.opportunity_score or 0.0), reverse=True)
        return result


def run_source_pipeline(
    source: Any,
    *,
    limit: int | None = None,
    persist: bool = True,
    min_budget: float | None = None,
    profile: dict[str, Any] | None = None,
    include_closed: bool = False,
) -> GigRunResult:
    """Утилита для CLI/тестов: прогон любого источника через pipeline (P2 §1).

    Это тот же путь, что в P1 для fixture: source → normalize → dedup → gates →
    assessment → scoring → persist. Core не знает, сеть за источником или файл.
    """
    pipeline = GigPipeline(
        profile=profile,
        persist=persist,
        min_budget=min_budget,
        include_closed=include_closed,
    )
    return pipeline.run([source], limit_per_source=limit, persist=persist)


def run_fixture_pipeline(
    fixture_path: str | None = None,
    *,
    limit: int | None = None,
    persist: bool = True,
    min_budget: float | None = None,
    profile: dict[str, Any] | None = None,
) -> GigRunResult:
    """Утилита для CLI/тестов: прогон встроенной fixture через pipeline."""
    from .sources.fixture import FixtureGigSource

    source = FixtureGigSource(fixture_path)
    pipeline = GigPipeline(profile=profile, persist=persist, min_budget=min_budget)
    return pipeline.run([source], limit_per_source=limit, persist=persist)


__all__ = ["GigPipeline", "GigRunResult", "gig_stats", "run_fixture_pipeline", "run_source_pipeline"]
