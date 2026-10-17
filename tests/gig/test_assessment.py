"""Assessment tests: can_deliver, ai_automation_potential, payment_readiness,
effort, effective_rate, client_quality, risk (§4, §5).

Key spec requirement: deliverability is NOT limited to the historical resume —
learnability, AI-agent scope, verifiability and tooling all contribute.
"""
from __future__ import annotations

from ai_assistant.gig.assessment import (
    assess_ai_automation_potential,
    assess_can_deliver,
    assess_client_quality,
    assess_payment_readiness,
    assess_risks,
    effective_hourly_rate,
    estimate_effort,
    skill_confidences,
)
from ai_assistant.gig.models import CanDeliver, PaymentReadiness, RiskLevel
from ai_assistant.gig.normalizer import normalize_gig

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gig_testutils import raw_gig  # noqa: E402

PROFILE_CONFIDENCES = skill_confidences(
    {
        "core_skills": ["python", "n8n", "automation", "rest api"],
        "secondary_skills": ["docker", "fastapi"],
    }
)


class TestPaymentReadiness:
    def test_verified_client_with_method_is_ready(self):
        gig = normalize_gig(raw_gig())
        assert assess_payment_readiness(gig) is PaymentReadiness.READY

    def test_no_client_history_is_unknown_not_guess(self):
        gig = normalize_gig(raw_gig(client=None))
        assert assess_payment_readiness(gig) is PaymentReadiness.UNKNOWN

    def test_crypto_only_is_possible_not_ready(self):
        gig = normalize_gig(raw_gig(description="Payment in USDT only."))
        assert assess_payment_readiness(gig) is PaymentReadiness.POSSIBLE

    def test_unpaid_work_is_blocked(self):
        gig = normalize_gig(raw_gig(description="This is an unpaid trial task."))
        assert assess_payment_readiness(gig) is PaymentReadiness.BLOCKED

    def test_source_reported_blocked_stays_blocked(self):
        gig = normalize_gig(raw_gig(payment_readiness="blocked"))
        assert assess_payment_readiness(gig) is PaymentReadiness.BLOCKED


class TestCanDeliver:
    def test_matching_skills_is_high(self):
        gig = normalize_gig(
            raw_gig(
                description="Build a webhook automation with n8n and the client's REST API. "
                "Acceptance criteria: working demo and documentation.",
            )
        )
        level, why_matched, _ = assess_can_deliver(gig, PROFILE_CONFIDENCES)
        assert level is CanDeliver.HIGH
        assert any("навыки" in item for item in why_matched)

    def test_blocking_skill_is_low_with_penalty(self):
        gig = normalize_gig(
            raw_gig(
                title="Firmware for embedded device",
                description="Embedded C++ hardware programming, physical device debugging.",
                skills=["embedded", "c++", "hardware"],
            )
        )
        level, _, why_risky = assess_can_deliver(gig, PROFILE_CONFIDENCES)
        assert level in (CanDeliver.LOW, CanDeliver.MEDIUM)
        assert why_risky  # скрытые требования должны быть объяснены

    def test_empty_profile_is_not_high(self):
        gig = normalize_gig(raw_gig())
        level, _, _ = assess_can_deliver(gig, {})
        assert level is not CanDeliver.HIGH


class TestAiAutomationPotential:
    def test_in_0_100_range(self):
        gig = normalize_gig(raw_gig())
        value = assess_ai_automation_potential(gig)
        assert 0 <= value <= 100

    def test_scraping_data_entry_scores_high(self):
        gig = normalize_gig(
            raw_gig(description="Scraping a lead list and data entry into CSV. Bulk batch process.")
        )
        assert assess_ai_automation_potential(gig) >= 60

    def test_consulting_scores_low(self):
        gig = normalize_gig(raw_gig(description="Strategy consulting and coaching, one-on-one interviews."))
        assert assess_ai_automation_potential(gig) <= 50


class TestEffortAndRate:
    def test_effort_positive_and_bounded(self):
        gig = normalize_gig(raw_gig())
        effort = estimate_effort(gig)
        assert effort >= 2.0

    def test_bigger_budget_means_more_effort(self):
        small = estimate_effort(normalize_gig(raw_gig(budget="USD 300")))
        big = estimate_effort(normalize_gig(raw_gig(budget="USD 5000")))
        assert big >= small

    def test_effective_rate_is_budget_over_effort(self):
        gig = normalize_gig(raw_gig())
        gig.budget_min = gig.budget_max = 1000.0
        effort = estimate_effort(gig)
        rate = effective_hourly_rate(gig, effort)
        assert rate is not None
        assert rate == round(1000.0 / effort, 2)

    def test_rate_none_without_effort(self):
        gig = normalize_gig(raw_gig())
        assert effective_hourly_rate(gig, None) is None


class TestClientQuality:
    def test_none_when_no_data(self):
        gig = normalize_gig(raw_gig(client=None))
        assert assess_client_quality(gig) is None

    def test_good_client_scores_high(self):
        gig = normalize_gig(raw_gig())
        assert (assess_client_quality(gig) or 0) >= 70

    def test_rating_over_5_is_treated_as_percent(self):
        gig = normalize_gig(raw_gig(client={"rating": 96}))
        assert (assess_client_quality(gig) or 0) >= 90


class TestRisks:
    def test_low_risk_for_clean_gig(self):
        gig = normalize_gig(raw_gig())
        gig.payment_readiness = assess_payment_readiness(gig)
        level, risks = assess_risks(gig, PROFILE_CONFIDENCES)
        assert level is RiskLevel.LOW
        assert risks == []

    def test_unlimited_revisions_flagged(self):
        gig = normalize_gig(raw_gig(description="Unlimited revisions required."))
        _, risks = assess_risks(gig, PROFILE_CONFIDENCES)
        assert any("правки" in r for r in risks)

    def test_high_competition_flagged(self):
        gig = normalize_gig(raw_gig(competition=80))
        _, risks = assess_risks(gig, PROFILE_CONFIDENCES)
        assert any("конкуренция" in r for r in risks)
