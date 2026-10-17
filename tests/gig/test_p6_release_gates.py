"""P6 regression probes for observed unsafe admission and tracking behavior."""
from datetime import timedelta

import pytest

from ai_assistant.gig.capability import CapabilityAssessment
from ai_assistant.gig.first_money import build_shortlist
from ai_assistant.gig.grounded import analyze
from ai_assistant.gig.jit import apply_verification, is_open_status, verify_gig
from ai_assistant.gig.tracker import SubmissionRecord, SubmissionTracker
from tests.gig.test_first_money import NOW, make_gig, verified, entry_for, confidences


@pytest.mark.parametrize("status", ["", "UNKNOWN", "pending", None, "CLOSED", "AWARDED"])
def test_only_explicit_open_status_is_eligible(status, confidences):
    gig = verified(make_gig(), status=status)
    assert not is_open_status(status)
    assert not build_shortlist([entry_for(gig, confidences)], now=NOW).apply_first


@pytest.mark.parametrize("fit", ["weak", "unknown", "learnable"])
def test_every_ineligible_capability_is_blocked_even_with_high_score(fit, confidences):
    gig, analysis, _, risk = entry_for(verified(make_gig(), bids=0), confidences)
    cap = CapabilityAssessment(fit=fit, coverage=1, required_total=1, required_covered=1)
    result = build_shortlist([(gig, analysis, cap, risk)], now=NOW)
    assert not result.apply_first
    if fit == "learnable":
        assert result.backup


@pytest.mark.parametrize("field,value", [("url", None), ("description", ""),
                                          ("currency", None), ("competition", None)])
def test_missing_required_live_field_blocks_final_queue(field, value, confidences):
    gig = verified(make_gig())
    setattr(gig, field, value)
    if field == "competition":
        gig.source_facts["jit_verification"]["bids_live"] = None
    assert not build_shortlist([entry_for(gig, confidences)], now=NOW).apply_first


def test_live_verification_expires(confidences):
    gig = verified(make_gig())
    assert not build_shortlist([entry_for(gig, confidences)], now=NOW + timedelta(hours=2)).apply_first


def test_repeated_verification_preserves_initial_discovery_bids():
    gig = verified(make_gig(competition=2), bids=28)
    observation = {"competition": 35, "source_status": "OPEN"}
    verification = verify_gig(gig, observation, now=NOW)
    assert verification.bids_at_discovery == 2
    assert verification.bids_delta == 33


def test_tags_and_discovery_do_not_supply_technology():
    a = analyze("Build a Python script that cleans rows and writes output.", "Row cleanup",
                skills=["Telegram", "Kubernetes", "Salesforce"])
    assert a.tech_names() == ["Python"]


def test_negative_requirement_is_not_a_required_technology():
    a = analyze("Build a Python script without Telegram or API integration.")
    assert "Telegram" not in a.tech_names()
    assert "API" not in a.tech_names()


def test_refresh_drops_missing_live_data_and_updates_fx():
    gig = make_gig(source_facts={"found_via": ["python"],
                                "freelancer_client_payment_verified": True})
    fresh = {"source_status": "OPEN", "description": "", "skills": [],
             "currency": "INR", "budget": {"min": 10000, "max": 10000},
             "source_facts": {"freelancer_currency_exchange_rate": .01}}
    apply_verification(gig, verify_gig(gig, fresh, now=NOW), fresh)
    assert gig.description == "" and gig.skills == [] and gig.published_at is None
    assert gig.source_facts["found_via"] == ["python"]
    assert "freelancer_client_payment_verified" not in gig.source_facts
    assert gig.effective_hourly_rate == pytest.approx(100 / 30, abs=.01)


def test_each_final_entry_has_safe_pack_and_recommendation(confidences):
    result = build_shortlist([entry_for(verified(make_gig()), confidences)], now=NOW)
    assert result.apply_first
    for entry in result.apply_first:
        pack = entry.proposal_pack
        assert pack and pack.is_safe() and 100 <= pack.word_count <= 180
        assert len(pack.referenced_requirements) >= 2
        assert pack.bid.amount and pack.bid.currency and pack.bid.delivery_days
        assert entry.gig.url.startswith("https://")
        assert "My background" not in pack.proposal and "do regularly" not in pack.proposal


def test_tracker_refresh_preserves_sent_status_and_delivery(tmp_path):
    tracker = SubmissionTracker(tmp_path / "tracker.sqlite")
    tracker.upsert(SubmissionRecord(project_id="p", title="A", action_score=61,
                                   bids_live=28, recommended_delivery="5 days"))
    tracker.mark_submitted("p", actual_bid=300, actual_delivery="6 days")
    tracker.upsert(SubmissionRecord(project_id="p", title="A updated", bids_live=35))
    record = tracker.get("p")
    assert record.status == "SUBMITTED" and record.actual_bid == 300
    assert record.bids_live == 35 and record.action_score == 61
    assert record.actual_delivery == "6 days" and record.recommended_delivery == "5 days"


def test_funnel_preserves_observed_reply_but_never_invents_interview(tmp_path):
    tracker = SubmissionTracker(tmp_path / "tracker.sqlite")
    tracker.upsert(SubmissionRecord(project_id="p", title="A"))
    tracker.mark_submitted("p")
    tracker.set_status("p", "CLIENT_REPLIED")
    tracker.set_status("p", "AWARDED")
    tracker.set_status("p", "CLOSED", revenue_usd=200)
    metrics = tracker.metrics(projects_seen=100, projects_live_verified=20, apply_first_count=3)
    assert metrics["client_replies"] == 1 and metrics["awards"] == 1
    assert metrics["interviews"] == 0 and metrics["interview_rate"] == 0
    assert metrics["revenue_usd"] == 200 and metrics["projects_seen"] == 100


@pytest.mark.parametrize("compressed", [True, False])
def test_public_transport_decodes_compressed_or_plain_html(monkeypatch, compressed):
    import gzip
    import urllib.request
    from ai_assistant.gig.sources.freelancer import FreelancerGigSource
    body = '<html>Budget: ₹10000</html>'.encode('utf-8')
    class Response:
        headers = {"Content-Encoding": "gzip"} if compressed else {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return gzip.compress(body) if compressed else body
    def get(request, timeout):
        assert request.get_header("Accept-encoding") == "gzip"
        assert timeout > 0
        return Response()
    monkeypatch.setattr(urllib.request, "urlopen", get)
    policy = {'automated_access_allowed': True,
              'express_written_permission_reference': 'offline transport fixture',
              'website_scraping_allowed': True,
              'expires_at': '2099-01-01T00:00:00+00:00'}
    assert FreelancerGigSource(access_policy=policy)._http_get("https://www.freelancer.com/projects/test") == body.decode()
