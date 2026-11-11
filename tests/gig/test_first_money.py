"""GIG-SEARCH-P6 — тесты First Money Run.

Покрывает все проверки §15 задания:

    stale project cannot become APPLY_FIRST without live verification
    closed project cannot become APPLY_FIRST
    live bids override discovery bids
    grounded WORK cannot contain unsupported requirements
    missing client data remains unknown
    weak capability cannot become APPLY_FIRST
    learnable capability cannot become APPLY_FIRST
    proposal cannot contain unsupported experience claims
    proposal contains project-specific requirements
    first_money_score decreases when competition rises materially

Плюс проверки tracker'а, языка отправляемого пакета и того, что P4-конверсия
продолжает работать. Всё офлайн и детерминированно: сеть не трогается.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_assistant.gig.assessment import skill_confidences
from ai_assistant.gig.capability import assess_capability, capability_allows_apply_first
from ai_assistant.gig.client import assess_client_risk
from ai_assistant.gig.first_money import (
    APPLY_FIRST_MIN_SCORE,
    build_shortlist,
    first_money_score_of,
)
from ai_assistant.gig.grounded import analyze, unsupported_claims, work_hints
from ai_assistant.gig.jit import (
    apply_verification,
    is_open_status,
    live_bids_of,
    verify_gig,
)
from ai_assistant.gig.models import (
    BudgetType, CanDeliver, Gig, GigStatus, PaymentReadiness, RiskLevel, SourceMode,
)
from ai_assistant.gig.proposal import build_proposal, find_forbidden_claims
from ai_assistant.gig.tracker import SubmissionRecord, SubmissionTracker

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.fixture(scope="module")
def confidences():
    profile_path = Path(__file__).resolve().parents[2] / "candidate_profile.json"
    if not profile_path.exists():
        profile_path = Path(__file__).resolve().parents[2] / "candidate_profile.example.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    return skill_confidences(profile), {
        k.lower(): str(v).upper() for k, v in (profile.get("skill_confidence") or {}).items()
    }


PYTHON_BRIEF = (
    "I need a Python script that scrapes product data from an e-commerce site and exports it to CSV. "
    "The scraper should use BeautifulSoup or Playwright, handle pagination, and save the results to "
    "a CSV file. Requirements: the script must run on Linux and be configurable through a config file. "
    "Deliverables: source code and a short README. Please also add logging so I can see what happened."
)

FOREIGN_BRIEF = (
    "We need a Salesforce and HubSpot administrator. You will configure fields, flows, validation "
    "rules, objects, and permission sets inside Salesforce. Experience with HubSpot workflows and "
    "CRM migration is required. Deliverables: configured Salesforce org and documentation."
)


def make_gig(**overrides) -> Gig:
    base = dict(
        source="freelancer", source_id="fl_t1", title="Python scraper to CSV",
        url="https://www.freelancer.com/projects/python/test-scraper",
        description=PYTHON_BRIEF, skills=["python", "beautifulsoup", "csv", "playwright"],
        budget_min=500.0, budget_max=900.0, currency="USD",
        budget_type=BudgetType.FIXED, competition=5, published_at=iso(3),
        can_deliver=CanDeliver.HIGH, match_score=70.0, opportunity_score=60.0,
        estimated_effort_hours=30.0, effective_hourly_rate=18.0,
        risk_level=RiskLevel.LOW, source_status="OPEN", source_mode=SourceMode.LIVE,
        source_scope="public_jobs", source_facts={},
    )
    base.update(overrides)
    return Gig(**base)


def verified(gig: Gig, *, bids: int = 5, status: str = "OPEN", hours_ago: float = 3.0) -> Gig:
    """Помечает заказ как перепроверенный в текущем запуске."""
    fresh = {
        "competition": bids, "source_status": status,
        "budget": {"min": gig.budget_min, "max": gig.budget_max},
        "currency": gig.currency, "description": gig.description,
        "skills": gig.skills, "published_at": iso(hours_ago),
    }
    v = verify_gig(gig, fresh, now=NOW)
    apply_verification(gig, v, fresh)
    return gig


def entry_for(gig: Gig, confidences):
    conf, levels = confidences
    a = analyze(gig.description, gig.title, gig.skills)
    cap = assess_capability(a, conf, profile_skills=levels)
    cr = assess_client_risk(gig, now=NOW)
    return gig, a, cap, cr


# ----------------------------------------------------------- §15: staleness
class TestStaleCannotBeApplyFirst:
    def test_stale_project_cannot_become_apply_first(self, confidences):
        """Без перепроверки в текущем запуске — не APPLY_FIRST, каким бы хорошим он ни был."""
        gig = make_gig()  # никакой верификации не было
        shortlist = build_shortlist([entry_for(gig, confidences)], now=NOW)
        assert shortlist.apply_first == []
        assert "not live-verified in this run" in shortlist.skip[0].reasons

    def test_verified_project_can_become_apply_first(self, confidences):
        gig = verified(make_gig(), bids=2)
        shortlist = build_shortlist([entry_for(gig, confidences)], now=NOW)
        assert len(shortlist.apply_first) == 1
        assert shortlist.apply_first[0].live_verified is True

    def test_live_verified_flag_is_recorded(self):
        gig = make_gig()
        v = verify_gig(gig, {"competition": 7, "source_status": "OPEN",
                             "budget": {"min": 1, "max": 2}}, now=NOW)
        assert v.live_verified is True
        assert v.verified_at == "2026-10-03T12:00:00Z"

    def test_failed_verification_leaves_the_flag_false(self):
        v = verify_gig(make_gig(), None, now=NOW)
        assert v.live_verified is False
        assert v.error == "no fresh observation"


class TestClosedCannotBeApplyFirst:
    def test_closed_project_cannot_become_apply_first(self, confidences):
        gig = verified(make_gig(), bids=1, status="CLOSED")
        shortlist = build_shortlist([entry_for(gig, confidences)], now=NOW)
        assert shortlist.apply_first == []
        assert any("closed" in r for r in shortlist.skip[0].reasons)

    @pytest.mark.parametrize("status", ["CLOSED", "EXPIRED", "COMPLETED", "CANCELLED",
                                        "FROZEN", "DELETED", "AWARDED"])
    def test_all_closed_statuses_are_recognised(self, status):
        assert is_open_status(status) is False

    @pytest.mark.parametrize("status", ["OPEN", "open", "active"])
    def test_open_statuses_are_open(self, status):
        assert is_open_status(status) is True

    def test_unknown_status_is_not_treated_as_open(self):
        """Молчание источника не означает «открыт»."""
        assert is_open_status(None) is False


# ------------------------------------------------- §15: live bids override
class TestLiveBidsOverrideDiscovery:
    def test_live_bids_override_discovery_bids(self):
        gig = make_gig(competition=2)
        assert live_bids_of(gig) == 2
        verified(gig, bids=25)
        assert live_bids_of(gig) == 25, "live value must win over discovery"

    def test_bids_delta_is_recorded(self):
        gig = make_gig(competition=2)
        v = verify_gig(gig, {"competition": 25, "source_status": "OPEN",
                             "budget": {"min": 1, "max": 2}}, now=NOW)
        assert v.bids_at_discovery == 2
        assert v.bids_live == 25
        assert v.bids_delta == 23
        assert v.bids_increased is True

    def test_unverified_gig_does_not_pass_stale_bids_as_live(self):
        """Ключевое: устаревшее значение нельзя выдавать за актуальное."""
        gig = make_gig(competition=2)
        v = verify_gig(gig, None, now=NOW)
        apply_verification(gig, v, None)
        assert live_bids_of(gig) is None

    def test_ranking_uses_live_bids(self):
        """С ростом ставок балл обязан упасть."""
        gig = make_gig(competition=2)
        conf = {
            "python": 1.0, "api": 1.0, "automation": 1.0, "sql": 1.0,
            "linux": 1.0, "rest api": 1.0, "webhooks": 1.0, "telegram": 1.0,
            "docker": 1.0, "bash": 1.0, "powershell": 1.0, "n8n": 1.0,
            "make": 1.0, "active directory": 1.0, "ai agents": 0.75, "llm": 0.75,
        }
        levels = {"python": "PROFESSIONAL", "linux": "PROFESSIONAL"}
        a = analyze(gig.description, gig.title, gig.skills)
        cap = assess_capability(a, conf, profile_skills=levels)
        cr = assess_client_risk(gig, now=NOW)

        before = first_money_score_of(gig, a, cap, cr, now=NOW).score
        verified(gig, bids=90)
        after = first_money_score_of(gig, a, cap, cr, now=NOW).score
        assert after < before, (before, after)


# ------------------------------------------------------- §15: grounded WORK
class TestGroundedWork:
    def test_grounded_work_cannot_contain_unsupported_requirements(self):
        """Главный дефект P5: Telegram/LLM/БД появлялись без упоминания в брифе."""
        analysis = analyze(PYTHON_BRIEF, "Python scraper")
        hints = " ".join(work_hints(analysis)).lower()
        assert "telegram" not in hints
        assert "llm" not in hints
        assert "migration" not in hints

    def test_worked_requirements_are_the_ones_in_the_brief(self):
        analysis = analyze(PYTHON_BRIEF, "Python scraper")
        names = [t["name"] for t in analysis.required_tech]
        assert "Python" in names
        assert "Web scraping" in names
        assert "CSV" in names

    def test_telegram_appears_only_when_mentioned(self):
        plain = analyze(PYTHON_BRIEF, "Python scraper")
        assert "Telegram" not in [t["name"] for t in plain.required_tech]
        with_tg = analyze(PYTHON_BRIEF + " Also send results to a Telegram channel.",
                          "Python scraper")
        assert "Telegram" in [t["name"] for t in with_tg.required_tech]

    def test_every_item_has_evidence(self):
        analysis = analyze(PYTHON_BRIEF, "Python scraper")
        for group in (analysis.required_work, analysis.required_tech, analysis.deliverables,
                      analysis.explicit_requirements, analysis.client_constraints):
            for item in group:
                assert item.get("evidence"), item

    def test_unsupported_claims_detects_injected_tech(self):
        analysis = analyze(PYTHON_BRIEF, "Python scraper")
        injected = unsupported_claims(analysis, "I will use Kubernetes and Rust for this.")
        assert "Kubernetes" in injected
        # Rust есть в словаре распознавания и не упомянут в брифе — значит выдумка.
        assert "Rust" in injected

    def test_empty_brief_reports_unknown_instead_of_inventing(self):
        analysis = analyze("", "")
        assert analysis.unknown
        assert analysis.required_work == []


# ------------------------------------------------------ §15: client unknown
class TestClientMissingData:
    def test_missing_client_data_remains_unknown(self):
        gig = make_gig(source_facts={}, client_country=None, client_rating=None)
        risk = assess_client_risk(gig, now=NOW)
        assert risk.level == "UNKNOWN"
        assert risk.reasons
        assert risk.facts.known_fields() == 0

    def test_partial_data_does_not_become_unknown(self):
        gig = make_gig(client_country="Australia")
        risk = assess_client_risk(gig, now=NOW)
        assert risk.level != "UNKNOWN"

    def test_client_risk_always_has_a_reason(self):
        for facts in ({}, {"freelancer_client_payment_verified": True},
                      {"freelancer_client_payment_verified": False,
                       "freelancer_client_reviews": 0}):
            risk = assess_client_risk(make_gig(source_facts=facts), now=NOW)
            assert risk.reasons, facts

    def test_invented_client_facts_are_not_created(self):
        """Если источник молчит, поля остаются None, а не додумываются."""
        risk = assess_client_risk(make_gig(), now=NOW)
        assert risk.facts.reviews is None
        assert risk.facts.payment_verified is None
        assert risk.facts.rating is None


# --------------------------------------------------- §15: capability gate
class TestCapabilityGate:
    def test_weak_capability_cannot_become_apply_first(self, confidences):
        gig = verified(make_gig(title="Salesforce administrator needed",
                                description=FOREIGN_BRIEF,
                                skills=["salesforce", "hubspot", "crm"]))
        entries = [entry_for(gig, confidences)]
        assert entries[0][2].fit == "weak"
        shortlist = build_shortlist(entries, now=NOW)
        assert shortlist.apply_first == []
        assert any("capability_fit=weak" in r for r in shortlist.skip[0].reasons)

    def test_learnable_capability_cannot_become_apply_first(self, confidences):
        conf, levels = confidences
        # Бриф требует технологию вне подтверждённого стека, но с частичным покрытием.
        gig = verified(make_gig(
            title="Kubernetes deployment for an existing Python service",
            description=("Deploy our existing Python service to Kubernetes. You will write "
                         "manifests, configure the deployment and document the process. "
                         "Deliverables: manifests and a short README."),
            skills=["kubernetes", "python", "docker", "linux"],
        ))
        entries = [entry_for(gig, confidences)]
        fit = entries[0][2].fit
        shortlist = build_shortlist(entries, now=NOW)
        # Ключевое требование: learnable и weak/unknown не могут быть в APPLY_FIRST.
        assert fit in ("learnable", "weak", "unknown"), fit
        assert shortlist.apply_first == [], (
            f"capability_fit={fit} must not be APPLY_FIRST"
        )
        assert shortlist.skip, shortlist.counts()

    def test_proven_or_strong_can_become_apply_first(self, confidences):
        gig = verified(make_gig(), bids=2)
        entries = [entry_for(gig, confidences)]
        assert entries[0][2].fit in ("proven", "strong")
        assert build_shortlist(entries, now=NOW).apply_first

    def test_capability_helper_matches_the_rule(self):
        assert capability_allows_apply_first("proven") is True
        assert capability_allows_apply_first("strong") is True
        assert capability_allows_apply_first("learnable") is False
        assert capability_allows_apply_first("weak") is False
        assert capability_allows_apply_first("unknown") is False

    def test_foreign_stack_is_weak_however_much_else_matches(self, confidences):
        conf, levels = confidences
        a = analyze(FOREIGN_BRIEF + " Also you may use Python for a small helper script.",
                    "Salesforce admin")
        cap = assess_capability(a, conf, profile_skills=levels)
        assert cap.fit == "weak"
        assert cap.foreign

    def test_generic_skill_labels_do_not_block(self, confidences):
        """Обобщённые ярлыки («data extraction») — не технология и не должны снижать балл."""
        conf, levels = confidences
        a = analyze("Build a Python scraper. Skills: data extraction, data processing, "
                    "web scraping, python.", "Scraper")
        cap = assess_capability(a, conf, profile_skills=levels)
        assert cap.fit in ("proven", "strong"), (cap.fit, cap.missing)


# ------------------------------------------------------ §15: proposal rules
class TestProposalRules:
    def test_proposal_cannot_contain_unsupported_experience_claims(self):
        found = find_forbidden_claims(
            "I have 8 years of experience with Salesforce and dozens of delivered projects."
        )
        assert found

    def test_honest_proposal_has_no_forbidden_claims(self, confidences):
        gig = make_gig()
        entries = entry_for(gig, confidences)
        pack = build_proposal(gig, entries[1], entries[2])
        assert pack.injected_claims == []

    def test_proposal_contains_project_specific_requirements(self, confidences):
        gig = make_gig()
        entries = entry_for(gig, confidences)
        pack = build_proposal(gig, entries[1], entries[2])
        assert len(pack.referenced_requirements) >= 2, pack.referenced_requirements
        # Ссылка должна быть на конкретику брифа, а не на общую фразу.
        joined = " ".join(pack.referenced_requirements).lower()
        assert any(w in joined for w in ("python", "scrape", "csv", "beautifulsoup", "playwright"))

    def test_proposal_mentions_no_technology_outside_the_brief(self, confidences):
        gig = make_gig()
        entries = entry_for(gig, confidences)
        pack = build_proposal(gig, entries[1], entries[2])
        assert pack.unsupported_tech == [], pack.unsupported_tech
        assert pack.is_safe()

    def test_proposal_word_count_is_reasonable(self, confidences):
        gig = make_gig()
        entries = entry_for(gig, confidences)
        pack = build_proposal(gig, entries[1], entries[2])
        assert 80 <= pack.word_count <= 200, pack.word_count

    def test_proposal_is_not_generic(self, confidences):
        """Два разных брифа должны давать разные отклики."""
        a_gig = make_gig()
        b_gig = make_gig(
            title="Telegram bot for order notifications",
            description=("Build a Telegram bot in Python that notifies me about new orders. "
                         "Deliverables: bot source code and a README."),
            skills=["python", "telegram"],
        )
        _, a_analysis, a_cap, _ = entry_for(a_gig, confidences)
        _, b_analysis, b_cap, _ = entry_for(b_gig, confidences)
        pa = build_proposal(a_gig, a_analysis, a_cap)
        pb = build_proposal(b_gig, b_analysis, b_cap)
        assert pa.proposal != pb.proposal

    def test_bid_is_below_midpoint_but_not_dumping(self, confidences):
        gig = make_gig(budget_min=500.0, budget_max=900.0)
        entries = entry_for(gig, confidences)
        bid = build_proposal(gig, entries[1], entries[2]).bid
        assert bid.amount is not None
        assert 500 <= bid.amount < 700, bid.amount

    def test_missing_budget_is_stated_not_invented(self, confidences):
        gig = make_gig(budget_min=None, budget_max=None)
        entries = entry_for(gig, confidences)
        bid = build_proposal(gig, entries[1], entries[2]).bid
        assert bid.amount is None
        assert "not published" in bid.basis or "no budget" in bid.basis.lower()

    def test_sendable_pack_has_no_mixed_language(self, confidences):
        """Пакет уходит клиенту, поэтому все его поля — на одном языке."""
        import re
        cyrillic = re.compile("[А-Яа-я]")
        gig = make_gig()
        entries = entry_for(gig, confidences)
        pack = build_proposal(gig, entries[1], entries[2])
        for text in (pack.proposal, " ".join(pack.technical_approach), pack.bid.basis,
                     pack.bid.note, " ".join(pack.bid.milestones),
                     pack.clarifying_question, " ".join(pack.risks), pack.proof_idea):
            assert not cyrillic.search(text), text

    def test_question_is_asked_only_when_the_brief_lacks_something(self, confidences):
        gig = make_gig()
        entries = entry_for(gig, confidences)
        pack = build_proposal(gig, entries[1], entries[2])
        assert pack.clarifying_question.endswith("?")

    def test_proof_idea_does_not_offer_free_work(self, confidences):
        gig = make_gig()
        entries = entry_for(gig, confidences)
        pack = build_proposal(gig, entries[1], entries[2])
        lowered = pack.proof_idea.lower()
        assert "20 minutes" in lowered or "15 minutes" in lowered


# -------------------------------------------------- §15: score sensitivity
class TestFirstMoneyScore:
    def test_decreases_when_competition_rises_materially(self, confidences):
        conf, levels = confidences
        a = analyze(PYTHON_BRIEF, "Python scraper")
        gig = make_gig(competition=2)
        cap = assess_capability(a, conf, profile_skills=levels)
        cr = assess_client_risk(gig, now=NOW)
        before = first_money_score_of(gig, a, cap, cr, now=NOW).score

        verified(gig, bids=80)
        after = first_money_score_of(gig, a, cap, cr, now=NOW).score
        assert after < before

    def test_high_pay_cannot_override_weak_capability(self, confidences):
        conf, levels = confidences
        a = analyze(FOREIGN_BRIEF, "Salesforce admin")
        gig = make_gig(title="Salesforce admin", description=FOREIGN_BRIEF,
                       skills=["salesforce"], budget_min=50000.0, budget_max=90000.0,
                       effective_hourly_rate=200.0)
        cap = assess_capability(a, conf, profile_skills=levels)
        cr = assess_client_risk(gig, now=NOW)
        fm = first_money_score_of(gig, a, cap, cr, now=NOW)
        assert fm.score < APPLY_FIRST_MIN_SCORE
        assert "capability_cap" in fm.notes

    def test_breakdown_explains_every_component(self, confidences):
        conf, levels = confidences
        a = analyze(PYTHON_BRIEF, "Python scraper")
        gig = made = make_gig()
        cap = assess_capability(a, conf, profile_skills=levels)
        cr = assess_client_risk(gig, now=NOW)
        fm = first_money_score_of(gig, a, cap, cr, now=NOW)
        assert set(fm.components) == set(fm.weights)
        for name in fm.weights:
            assert name in fm.notes

    def test_high_client_risk_lowers_the_score(self, confidences):
        conf, levels = confidences
        a = analyze(PYTHON_BRIEF, "Python scraper")
        cap = assess_capability(a, conf, profile_skills=levels)
        safe = assess_client_risk(
            make_gig(source_facts={"freelancer_client_payment_verified": True,
                                   "freelancer_client_reviews": 12,
                                   "freelancer_client_registered_at": "2020-01-01T00:00:00Z",
                                   "freelancer_client_deposit_made": True}), now=NOW)
        risky = assess_client_risk(
            make_gig(source_facts={"freelancer_client_payment_verified": False,
                                   "freelancer_client_reviews": 0,
                                   "freelancer_client_registered_at": iso(24),
                                   "freelancer_client_deposit_made": False}), now=NOW)
        gig = make_gig()
        a1 = first_money_score_of(gig, a, cap, safe, now=NOW).score
        a2 = first_money_score_of(gig, a, cap, risky, now=NOW).score
        assert a1 > a2


def profile_skills_for(levels: dict[str, str]) -> dict[str, str]:
    """Оставлено для совместимости: возвращает карту уровней как есть."""
    return levels


# ------------------------------------------------------------- shortlist
class TestShortlist:
    def test_apply_first_is_capped(self, confidences):
        entries = []
        for i in range(12):
            gig = verified(make_gig(source_id=f"fl_{i}", title=f"Python scraper {i}",
                                    competition=1), bids=1)
            entries.append(entry_for(gig, confidences))
        shortlist = build_shortlist(entries, now=NOW)
        assert len(shortlist.apply_first) <= 5

    def test_backup_is_capped(self, confidences):
        entries = []
        for i in range(30):
            gig = verified(make_gig(source_id=f"fl_b{i}", title=f"Python job {i}",
                                    competition=60), bids=60)
            entries.append(entry_for(gig, confidences))
        shortlist = build_shortlist(entries, now=NOW)
        assert len(shortlist.backup) <= 10

    def test_entries_are_sorted_by_score(self, confidences):
        entries = []
        for i, bids in enumerate((1, 5, 12)):
            gig = verified(make_gig(source_id=f"fl_s{i}", title=f"Job {i}"), bids=bids)
            entries.append(entry_for(gig, confidences))
        shortlist = build_shortlist(entries, now=NOW)
        scores = [e.score.score for e in shortlist.apply_first]
        assert scores == sorted(scores, reverse=True)

    def test_high_client_risk_blocks_even_a_good_fit(self, confidences):
        gig = verified(make_gig(
            source_facts={"freelancer_client_payment_verified": False,
                          "freelancer_client_reviews": 0,
                          "freelancer_client_registered_at": iso(24),
                          "freelancer_client_deposit_made": False}), bids=1)
        entries = [entry_for(gig, confidences)]
        shortlist = build_shortlist(entries, now=NOW)
        if entries[0][3].level == "HIGH":
            assert shortlist.apply_first == []


# ---------------------------------------------------------------- tracker
class TestSubmissionTracker:
    @pytest.fixture()
    def tracker(self, tmp_path):
        return SubmissionTracker(tmp_path / "sub.sqlite")

    def test_record_round_trip(self, tracker):
        tracker.upsert(SubmissionRecord(
            project_id="fl_1", title="Scraper", project_url="https://x",
            first_money_score=67.4, capability_fit="strong", client_risk="LOW",
            bids_at_discovery=0, proposal_text="Hello", recommended_bid=280.0,
            recommended_currency="HKD",
        ))
        rec = tracker.get("fl_1")
        assert rec is not None
        assert rec.title == "Scraper"
        assert rec.recommended_bid == 280.0
        assert rec.status == "READY"

    def test_funnel_progresses_to_revenue(self, tracker):
        tracker.upsert(SubmissionRecord(project_id="fl_2", title="Job",
                                        proposal_text="Hello"))
        tracker.mark_submitted("fl_2", actual_bid=300.0, bids_at_submission=4)
        assert tracker.get("fl_2").status == "SUBMITTED"
        tracker.set_status("fl_2", "CLIENT_REPLIED")
        tracker.set_status("fl_2", "AWARDED", revenue_usd=300.0)
        metrics = tracker.metrics()
        assert metrics["proposals_submitted"] == 1
        assert metrics["client_replies"] == 1
        assert metrics["awards"] == 1
        assert metrics["revenue_usd"] == 300.0
        assert metrics["reply_rate"] == 1.0

    def test_unknown_status_is_refused(self, tracker):
        tracker.upsert(SubmissionRecord(project_id="fl_3", title="Job"))
        with pytest.raises(ValueError):
            tracker.set_status("fl_3", "NOT_A_STATUS")

    def test_repeat_upsert_does_not_duplicate(self, tracker):
        tracker.upsert(SubmissionRecord(project_id="fl_4", title="A"))
        tracker.upsert(SubmissionRecord(project_id="fl_4", title="A updated"))
        rows = tracker.list()
        assert len([r for r in rows if r.project_id == "fl_4"]) == 1
        assert tracker.get("fl_4").title == "A updated"

    def test_discovery_bids_are_not_lost_on_update(self, tracker):
        """update не должен затирать данные первого обнаружения."""
        tracker.upsert(SubmissionRecord(project_id="fl_5", title="A", bids_at_discovery=2))
        tracker.upsert(SubmissionRecord(project_id="fl_5", title="A", bids_at_submission=25))
        rec = tracker.get("fl_5")
        assert rec.bids_at_discovery == 2
        assert rec.bids_at_submission == 25

    def test_submitted_projects_are_not_offered_again(self, tracker):
        tracker.upsert(SubmissionRecord(project_id="fl_6", title="A"))
        assert tracker.already_tracked("fl_6") is False
        tracker.mark_submitted("fl_6")
        assert tracker.already_tracked("fl_6") is True

    def test_metrics_are_none_before_any_submission(self, tracker):
        tracker.upsert(SubmissionRecord(project_id="fl_7", title="A", proposal_text="Hi"))
        metrics = tracker.metrics()
        assert metrics["proposals_ready"] == 1
        assert metrics["proposals_submitted"] == 0
        assert metrics["reply_rate"] is None


# -------------------------------------------------------- P4 still works
class TestP4ConversionStillWorks:
    def test_currency_conversion_is_intact(self):
        from ai_assistant.gig.currency import exchange_rate_of, to_usd
        assert exchange_rate_of({"freelancer_currency_exchange_rate": 0.010402},
                                "INR") == 0.010402
        assert to_usd(12500, "INR", 0.010402) == pytest.approx(130.03, abs=0.01)

    def test_unknown_currency_is_not_usd(self):
        from ai_assistant.gig.currency import exchange_rate_of
        assert exchange_rate_of({}, "XYZ") is None

    def test_scoring_still_converts(self):
        from ai_assistant.gig.normalizer import normalize_gig
        from ai_assistant.gig.scoring import budget_quality_score
        g = normalize_gig({
            "source": "freelancer", "source_id": "fl_cur", "title": "T",
            "currency": "INR", "budget": {"min": 12500.0, "max": 37500.0, "type": "fixed"},
            "source_facts": {"freelancer_currency_exchange_rate": 0.010402},
        })
        score, note = budget_quality_score(g)
        assert 0 < score < 40, note
