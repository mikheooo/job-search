"""GIG-SEARCH-P4 — тесты калибровки scoring.

Три измеренных дефекта, каждый закреплён тестом:

1. валюта игнорировалась, поэтому INR-бюджет получал балл как долларовый;
2. вес совпадений награждал МЕНЬШЕЕ число совпадений;
3. ``BLOCKING_SKILLS`` не знал не-IT домены, зато широкие токены давали ложные блоки.

Плюс дефекты распознавания валюты (HKD/AUD отсутствовали, символ ``$`` побеждал ISO-код).

Всё офлайн и детерминированно: сеть не трогается.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_assistant.gig.assessment import (
    BLOCKING_SKILLS,
    assess_can_deliver,
    load_candidate_profile,
    skill_confidences,
)
from ai_assistant.gig.currency import describe_conversion, exchange_rate_of, to_usd
from ai_assistant.gig.models import BudgetType, CanDeliver, Gig
from ai_assistant.gig.normalizer import normalize_gig, parse_currency
from ai_assistant.gig.scoring import budget_quality_score, effective_rate_score

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Курс, реально опубликованный Freelancer для INR (наблюдён в currencyDetails).
INR_RATE = 0.010402
#: Курс HKD из сохранённого снимка.
HKD_RATE = 0.12745


def gig(**overrides) -> Gig:
    base = dict(
        source="freelancer", source_id="fl_test", title="Test gig",
        budget_min=1000.0, budget_max=2000.0, currency="USD",
        budget_type=BudgetType.FIXED,
    )
    base.update(overrides)
    return Gig(**base)


# ---------------------------------------------------------------- currency FX
class TestExchangeRateLookup:
    def test_usd_needs_no_conversion(self):
        assert exchange_rate_of({}, "USD") == 1.0
        assert exchange_rate_of(None, "usd") == 1.0

    def test_rate_is_taken_from_source_facts(self):
        facts = {"freelancer_currency_exchange_rate": INR_RATE}
        assert exchange_rate_of(facts, "INR") == INR_RATE

    def test_missing_rate_is_none_not_one(self):
        """Отсутствие курса не должно выглядеть как «курс 1:1»."""
        assert exchange_rate_of({}, "INR") is None
        assert exchange_rate_of(None, "INR") is None

    def test_unknown_currency_code(self):
        assert exchange_rate_of({}, "XYZ") is None

    def test_zero_rate_is_rejected(self):
        assert exchange_rate_of({"freelancer_currency_exchange_rate": 0}, "INR") is None

    def test_to_usd_converts(self):
        assert to_usd(12500, "INR", INR_RATE) == pytest.approx(130.03, abs=0.01)
        assert to_usd(37500, "INR", INR_RATE) == pytest.approx(390.07, abs=0.01)

    def test_to_usd_without_rate_refuses(self):
        assert to_usd(12500, "INR", None) is None

    def test_to_usd_passes_usd_through(self):
        assert to_usd(500, "USD", None) == 500.0

    def test_describe_conversion_names_the_source_rate(self):
        text = describe_conversion("INR", INR_RATE)
        assert "INR" in text and "0.010402" in text


class TestBudgetQualityUsesUsd:
    def test_inr_budget_is_no_longer_scored_as_usd(self):
        """Измеренный дефект: INR 12500-37500 получал 100/100, как если бы это были доллары."""
        facts = {"freelancer_currency_exchange_rate": INR_RATE}
        g = gig(budget_min=12500.0, budget_max=37500.0, currency="INR",
                source_facts=facts)
        score, note = budget_quality_score(g)
        assert score < 40.0, f"INR budget must not score like USD: {score}"
        assert "130" in note and "390" in note, note

    def test_same_numbers_in_usd_score_high(self):
        g = gig(budget_min=12500.0, budget_max=37500.0, currency="USD")
        score, _ = budget_quality_score(g)
        assert score == 100.0

    def test_inr_and_usd_are_no_longer_identical(self):
        facts = {"freelancer_currency_exchange_rate": INR_RATE}
        inr = budget_quality_score(
            gig(budget_min=12500.0, budget_max=37500.0, currency="INR", source_facts=facts))[0]
        usd = budget_quality_score(
            gig(budget_min=12500.0, budget_max=37500.0, currency="USD"))[0]
        assert usd - inr > 50.0, "the currency must change the score materially"

    def test_no_rate_means_undefined_not_zero_dollars(self):
        g = gig(budget_min=12500.0, budget_max=37500.0, currency="INR")
        score, note = budget_quality_score(g)
        assert score == 0.0
        assert "не сопоставлен" in note

    def test_hourly_rate_is_converted(self):
        facts = {"freelancer_currency_exchange_rate": INR_RATE}
        g = gig(budget_min=750.0, budget_max=1250.0, currency="INR",
                budget_type=BudgetType.HOURLY, source_facts=facts)
        score, note = budget_quality_score(g)
        assert score == pytest.approx(21.5, abs=1.0)
        assert "USD" in note


class TestEffectiveRateUsesUsd:
    """Ставка конвертируется в ``assessment.effective_hourly_rate``, а не в scoring.

    Контракт P4: поле хранится и передаётся **в USD**, поэтому ``effective_rate_score``
    конверсию не повторяет — иначе значение прошло бы её дважды.
    """

    def test_rate_field_is_converted_to_usd(self):
        """Измеренный дефект: 1000 INR/ч лежало в поле как 1000 и читалось как $1000/ч."""
        from ai_assistant.gig.assessment import effective_hourly_rate
        facts = {"freelancer_currency_exchange_rate": INR_RATE}
        g = gig(budget_min=750.0, budget_max=1250.0, currency="INR",
                budget_type=BudgetType.HOURLY, source_facts=facts)
        assert effective_hourly_rate(g, None) == pytest.approx(10.4, abs=0.1)

    def test_hkd_rate_is_converted(self):
        from ai_assistant.gig.assessment import effective_hourly_rate
        facts = {"freelancer_currency_exchange_rate": HKD_RATE}
        g = gig(budget_min=100.0, budget_max=100.0, currency="HKD",
                budget_type=BudgetType.HOURLY, source_facts=facts)
        assert effective_hourly_rate(g, None) == pytest.approx(12.75, abs=0.05)

    def test_rate_without_a_rate_is_none_not_a_foreign_number(self):
        from ai_assistant.gig.assessment import effective_hourly_rate
        g = gig(budget_min=1000.0, budget_max=1000.0, currency="INR",
                budget_type=BudgetType.HOURLY)
        assert effective_hourly_rate(g, None) is None

    def test_usd_rate_is_unchanged(self):
        from ai_assistant.gig.assessment import effective_hourly_rate
        g = gig(budget_min=30.0, budget_max=30.0, currency="USD",
                budget_type=BudgetType.HOURLY)
        assert effective_hourly_rate(g, None) == 30.0

    def test_score_consumes_usd_values(self):
        """Scoring не должен конвертировать повторно."""
        score, note = effective_rate_score(gig(), 30.0)
        assert score == pytest.approx(71.43, abs=0.1)
        assert note == "≈$30/ч"

    def test_an_unconverted_inr_number_would_be_obvious_but_is_not_our_path(self):
        """Фиксируем контракт: в scoring приходит уже USD, а 1875 как USD — это $1875."""
        facts = {"freelancer_currency_exchange_rate": INR_RATE}
        inr_gig = gig(currency="INR", source_facts=facts)
        # Два значения, которые когда-то путались: 1875 INR/ч и $20/ч.
        inr_usd, _ = effective_rate_score(inr_gig, 19.5)   # уже сконвертированное 1875 INR
        usd, _ = effective_rate_score(gig(currency="USD"), 20.0)
        assert usd > inr_usd, "a real $20/h must beat 1875 INR/h (~$19.5)"

    def test_a_missing_conversion_shows_as_a_low_score(self):
        """None — это «не сопоставлено», и это видно в оценке."""
        score, note = effective_rate_score(gig(currency="INR"), None)
        assert score == 20.0
        assert "не определена" in note


# --------------------------------------------------------- currency parsing
class TestCurrencyParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("HKD", "HKD"), ("AUD", "AUD"), ("USD", "USD"), ("INR", "INR"),
        ("GBP", "GBP"), ("EUR", "EUR"), ("SGD", "SGD"), ("NZD", "NZD"),
        ("HKD:499", "HKD"), ("AUD 500", "AUD"), ("USD 500", "USD"),
        ("INR 12500-37500", "INR"), ("£50-51 GBP", "GBP"), ("₹2000-2001 INR", "INR"),
        ("$30-250 USD", "USD"),
    ])
    def test_codes_and_symbols(self, raw, expected):
        assert parse_currency(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "not-a-currency", "paid in crypto"])
    def test_unknown_stays_none(self, raw):
        assert parse_currency(raw) is None

    def test_exact_code_beats_a_dollar_symbol(self):
        """Дефект: HKD/AUD отсутствовали, и символ $ сводил их к USD."""
        assert parse_currency("HK$499 HKD") == "HKD"
        assert parse_currency("AU$500 AUD") == "AUD"

    def test_normalized_gig_keeps_a_non_usd_currency(self):
        raw = {
            "source": "freelancer", "source_id": "fl_hkd",
            "title": "HKD project", "currency": "HKD",
            "budget": {"min": 499.0, "max": 500.0, "type": "fixed"},
            "source_facts": {"freelancer_currency_exchange_rate": HKD_RATE},
        }
        g = normalize_gig(raw)
        assert g.currency == "HKD"
        assert g.budget_min == 499.0


# ------------------------------------------------------------ skill weighting
class TestSkillWeightMonotonic:
    """Вес совпадений не должен падать при росте числа релевантных совпадений."""

    def _level(self, skills: list[str], title: str = "T") -> CanDeliver:
        confidences = skill_confidences(load_candidate_profile())
        g = gig(title=title, skills=skills, description="Build and integrate a service. " * 6)
        level, _, _ = assess_can_deliver(g, confidences)
        return level

    def test_one_core_skill_is_high(self):
        assert self._level(["python"]) is CanDeliver.HIGH

    def test_four_core_skills_are_not_worse_than_one(self):
        """Прежняя формула давала 1 навык = 1.00, 4 навыка = 0.85 — то есть хуже."""
        one = self._level(["python"])
        four = self._level(["python", "rest api", "sql", "troubleshooting"])
        assert four is CanDeliver.HIGH
        assert not (one is CanDeliver.HIGH and four is not CanDeliver.HIGH)

    def test_weak_skill_cannot_outweigh_a_strong_one(self):
        confidences = skill_confidences(load_candidate_profile())
        strong = gig(title="A", skills=["python"], description="x " * 60)
        weak = gig(title="B", skills=["troubleshooting"], description="x " * 60)

        def weight_of(g: Gig) -> float:
            from ai_assistant.gig.assessment import _matched_skills
            m = _matched_skills(g, confidences)
            ordered = sorted((w for _, w in m), reverse=True)
            bonus = sum(v * (0.5 ** i) for i, v in enumerate(ordered[1:], start=1))
            return min(1.0, ordered[0] + bonus) if ordered else 0.0

        assert weight_of(strong) > weight_of(weak)


# ---------------------------------------------------------------- blockers
class TestNonItDomainsAreBlocked:
    @pytest.fixture()
    def confidences(self):
        return skill_confidences(load_candidate_profile())

    def _gig(self, skills: list[str], description: str = "Deliverable details. " * 8) -> Gig:
        return Gig(source="freelancer", source_id="fl_b", title="B",
                   description=description, skills=skills,
                   budget_min=1000.0, budget_max=2000.0, currency="USD",
                   budget_type=BudgetType.FIXED)

    @pytest.mark.parametrize("skill", [
        "3d cad", "cad", "cad/cam", "autocad", "solidworks", "electronics", "pcb",
        "electrical", "mechanical", "sensor integration", "bill of materials",
        "pneumatic", "manufacturing", "cnc", "welding", "interior design",
    ])
    def test_domain_appears_in_the_blocker_list(self, skill):
        assert skill in BLOCKING_SKILLS

    def test_pneumatic_control_design_is_low(self, confidences):
        """Измеренный дефект: этот заказ стоял #1 в рейтинге с can_deliver=high."""
        g = self._gig(["3d cad", "automation", "sensor integration",
                       "bill of materials (bom) analysis", "electronics", "cad/cam"])
        level, _, risky = assess_can_deliver(g, confidences)
        assert level is CanDeliver.LOW
        assert risky, "the reason must be stated"

    def test_software_architecture_is_not_a_blocker(self, confidences):
        """Ложное срабатывание: 'architecture' блокировал IT-заказы с software architecture."""
        g = self._gig(["python", "software architecture", "sql"],
                      description="Design a data pipeline. " * 12)
        _, _, risky = assess_can_deliver(g, confidences)
        assert not any("архитектур" in note for note in risky), risky

    def test_building_architecture_is_a_blocker(self, confidences):
        g = self._gig(["architecture", "autocad"],
                      description="Architectural drawings for a residential building. " * 8)
        _, _, risky = assess_can_deliver(g, confidences)
        assert any("архитектур" in note for note in risky), risky

    def test_embedded_feed_is_not_a_hardware_job(self, confidences):
        """Ложное срабатывание: 'embedded' ловил 'embedded Instagram feed'.

        Проверяется именно отсутствие блокера: балл такого заказа низок по другой
        причине (в профиле нет веб-навыков), и это не то же самое.
        """
        g = self._gig(["web development", "php", "javascript"],
                      description="An embedded Instagram feed keeps the site fresh. " * 8)
        _, _, risky = assess_can_deliver(g, confidences)
        assert not any("embedded" in note.lower() or "hardware" in note.lower()
                       for note in risky), risky

    def test_embedded_firmware_is_a_hardware_job(self, confidences):
        g = self._gig(["embedded systems", "firmware development", "pcb"],
                      description="Firmware for an STM32 microcontroller board. " * 8)
        level, _, risky = assess_can_deliver(g, confidences)
        assert level is CanDeliver.LOW
        assert any("embedded" in note.lower() or "hardware" in note.lower()
                   for note in risky), risky


# --------------------------------------------------------------- integration
class TestScoringIntegration:
    def test_pneumatic_gig_scores_below_a_python_gig(self):
        """Главный результат калибровки: домен, который закрыть нельзя, не должен быть выше."""
        from ai_assistant.gig.assessment import (
            assess_ai_automation_potential, assess_client_quality, assess_payment_readiness,
            effective_hourly_rate, estimate_effort,
        )
        from ai_assistant.gig.scoring import score_gig

        confidences = skill_confidences(load_candidate_profile())
        facts = {"freelancer_currency_exchange_rate": INR_RATE}

        def full_score(g: Gig):
            g.payment_readiness = assess_payment_readiness(g)
            g.estimated_effort_hours = estimate_effort(g)
            g.ai_automation_potential = assess_ai_automation_potential(g)
            g.client_quality = assess_client_quality(g)
            g.can_deliver, _, _ = assess_can_deliver(g, confidences)
            g.effective_hourly_rate = effective_hourly_rate(g, g.estimated_effort_hours)
            return score_gig(g, confidences, client_quality=g.client_quality,
                             effective_rate=g.effective_hourly_rate).opportunity_score

        pneumatic = Gig(
            source="freelancer", source_id="fl_p", title="Custom Pneumatic Control Design",
            description="Pneumatic control design with sensor integration and BOM. " * 6,
            skills=["3d cad", "automation", "sensor integration", "electronics",
                    "bill of materials (bom) analysis", "cad/cam"],
            budget_min=12500.0, budget_max=37500.0, currency="INR",
            budget_type=BudgetType.FIXED, source_facts=facts,
        )
        python_job = Gig(
            source="freelancer", source_id="fl_y",
            title="Python Developer for Algo Trading System",
            description="Build an algorithmic trading pipeline with a broker REST API. " * 6,
            skills=["algorithmic trading", "python", "software architecture", "sql"],
            budget_min=1500.0, budget_max=12500.0, currency="INR",
            budget_type=BudgetType.FIXED, source_facts=facts,
        )
        assert full_score(python_job) > full_score(pneumatic), (
            "a deliverable Python job must outrank a CAD/pneumatics job"
        )

    def test_payment_readiness_is_still_never_ready(self):
        from ai_assistant.gig.assessment import assess_payment_readiness
        from ai_assistant.gig.models import PaymentReadiness
        g = gig(platform_payment_options=["platform_escrow", "paypal"])
        assert assess_payment_readiness(g) is not PaymentReadiness.READY


class TestRescoringIsPersisted:
    """Дефект: пересчитанный балл не доходил до БД при неизменном контенте.

    Наблюдено на реальном корпусе: после калибровки в базе у заказа оставалось
    `opportunity_score=79.4, can_deliver=high`, хотя новая логика давала 34.4/low.
    Причина: ветка UNCHANGED писала только провенанс, а не оценку.

    Изоляцию БД даёт глобальный conftest — здесь она не дублируется.
    """

    @pytest.fixture()
    def store(self):
        from ai_assistant.gig import store as gig_store
        return gig_store

    def test_a_changed_score_is_written_even_when_content_is_unchanged(self, store):
        g = gig(source_id="fl_rescore", title="Rescore me")
        g.opportunity_score = 10.0
        g.can_deliver = CanDeliver.HIGH
        assert store.save_gig(g) == "INSERTED"

        # Same content, different evaluation — as a recalibration would produce.
        g.opportunity_score = 90.0
        g.can_deliver = CanDeliver.LOW
        result = store.save_gig(g)
        assert result == "UNCHANGED"

        reloaded = store.get_gig(g.id)
        assert reloaded.opportunity_score == 90.0, "the recalibrated score must persist"
        assert reloaded.can_deliver is CanDeliver.LOW

    def test_a_rejected_duplicate_does_not_erase_a_stored_score(self, store):
        """Дефект первой версии правки: дубликат (score=None) обнулял балл канонической записи."""
        g = gig(source_id="fl_dup", title="Canonical gig")
        g.opportunity_score = 55.0
        assert store.save_gig(g) == "INSERTED"

        # The same gig arrives again and is classified as a duplicate, so it carries no score.
        again = gig(source_id="fl_dup", title="Canonical gig")
        again.opportunity_score = None
        again.rejection_reasons = ["DUPLICATE"]
        store.save_gig(again)

        reloaded = store.get_gig(g.id)
        assert reloaded.opportunity_score == 55.0, (
            "a duplicate re-fetch must not wipe the stored score"
        )

    def test_a_human_status_survives_rescoring(self, store):
        from ai_assistant.gig.models import GigStatus
        g = gig(source_id="fl_status", title="Keep my status")
        g.opportunity_score = 10.0
        store.save_gig(g)
        store.set_gig_status(g.id, "SHORTLISTED")

        g.opportunity_score = 80.0
        store.save_gig(g)

        reloaded = store.get_gig(g.id)
        assert reloaded.opportunity_score == 80.0
        assert reloaded.status is GigStatus.SHORTLISTED, "rescoring must not touch user status"


class TestSnapshotFixtures:
    """Снимки с реальным курсом — основа детерминированной проверки конверсии."""

    def test_usd_snapshot_carries_rate_one(self):
        from ai_assistant.gig.sources.freelancer import FreelancerGigSource
        path = PROJECT_ROOT / "artifacts" / "freelancer_project_detail.html"
        rec = FreelancerGigSource(snapshot_path=str(path)).records_from_snapshot()[0]
        assert rec["source_facts"]["freelancer_currency_exchange_rate"] == 1.0

    def test_non_usd_snapshot_carries_a_real_rate(self):
        from ai_assistant.gig.sources.freelancer import FreelancerGigSource
        path = PROJECT_ROOT / "artifacts" / "freelancer_project_detail_hkd.html"
        rec = FreelancerGigSource(snapshot_path=str(path)).records_from_snapshot()[0]
        rate = rec["source_facts"]["freelancer_currency_exchange_rate"]
        assert rec["currency"] == "HKD"
        assert 0 < rate < 1, "a non-USD rate must be a real fraction, not 1"
        g = normalize_gig(rec)
        score, note = budget_quality_score(g)
        assert score < 20.0, f"HKD 499-500 is about $64, not a premium budget: {note}"
