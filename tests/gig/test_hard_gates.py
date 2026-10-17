"""Hard gates tests: every reject must carry a machine-readable reason (§3)."""
from __future__ import annotations

from ai_assistant.gig.hard_gates import evaluate_hard_gates
from ai_assistant.gig.models import RejectionReason
from ai_assistant.gig.normalizer import normalize_gig


def _gate(raw, **kw):
    gig = normalize_gig(raw)
    result = evaluate_hard_gates(gig, **kw)
    enum_values = {m.value for m in RejectionReason}
    for reason in result.reasons:
        assert reason in enum_values, f"non machine-readable reason: {reason}"
    return result, gig


class TestHardGates:
    def test_clean_gig_passes(self):
        result, _ = _gate(
            {
                "source": "fixture",
                "source_id": "ok1",
                "title": "Build an n8n workflow",
                "description": "Automation via API, escrow payment.",
                "budget": "USD 800",
            }
        )
        assert not result.rejected
        assert result.reasons == []

    @staticmethod
    def _raw(text_extra: str) -> dict:
        return {
            "source": "fixture",
            "source_id": "g1",
            "title": "Project",
            "description": f"Details. {text_extra}",
            "budget": "USD 1000",
        }

    def test_scam_registration_fee(self):
        result, _ = _gate(self._raw("You must pay a small registration fee to start."))
        assert RejectionReason.SCAM_RISK.value in result.reasons

    def test_country_restriction_us_only(self):
        result, _ = _gate(self._raw("Only US citizens may apply."))
        assert RejectionReason.COUNTRY_RESTRICTION.value in result.reasons

    def test_country_restriction_blocked_client_country(self):
        result, _ = _gate({**self._raw("Regular project"), "client_country": "Russia"})
        assert RejectionReason.COUNTRY_RESTRICTION.value in result.reasons

    def test_access_invite_only(self):
        result, _ = _gate(self._raw("This project is invite-only."))
        assert RejectionReason.ACCESS_UNAVAILABLE.value in result.reasons

    def test_payment_blocked_by_pattern(self):
        result, _ = _gate(self._raw("Paid only in equity."))
        assert RejectionReason.PAYMENT_UNAVAILABLE.value in result.reasons

    def test_payment_blocked_by_source_reported_readiness(self):
        result, _ = _gate({**self._raw("Details"), "payment_readiness": "blocked"})
        assert RejectionReason.PAYMENT_UNAVAILABLE.value in result.reasons

    def test_undeliverable_onsite(self):
        result, _ = _gate(self._raw("On-site meetings required in Berlin."))
        assert RejectionReason.UNDELIVERABLE.value in result.reasons

    def test_price_missing_when_no_budget(self):
        result, _ = _gate({"source": "fixture", "source_id": "p1", "title": "T", "description": "D"})
        assert RejectionReason.PRICE_MISSING.value in result.reasons

    def test_price_missing_below_min_budget_policy(self):
        result, _ = _gate(self._raw("Small job."), min_budget=5000.0)
        assert RejectionReason.PRICE_MISSING.value in result.reasons
        assert "below policy floor" in result.details[RejectionReason.PRICE_MISSING.value][0]

    def test_hourly_without_rate_rejected(self):
        result, _ = _gate(
            {"source": "fixture", "source_id": "h1", "title": "T", "description": "Work hourly", "budget": "hourly"}
        )
        assert RejectionReason.PRICE_MISSING.value in result.reasons

    def test_duplicate_reason_passthrough(self):
        result, _ = _gate(self._raw("Details"), duplicate_reason="stored canonical URL: https://x")
        assert RejectionReason.DUPLICATE.value in result.reasons
