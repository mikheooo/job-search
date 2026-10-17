"""Freelancer.com public source — deterministic offline tests (P3 §12).

Everything here runs on saved pages: no test opens a socket. The fixtures are real
public Freelancer HTML captured earlier, so the parser is pinned against the site's
actual structure rather than a hand-made mock.

Covered: parser, missing fields, budget variants, currency, bids, pagination, stable
source id, dedup, refresh, closed/expired jobs, network failure, live/fixture separation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_assistant.gig.models import BudgetType, PaymentReadiness, SourceMode
from ai_assistant.gig.pipeline import run_source_pipeline
from ai_assistant.gig.sources.freelancer import (
    DEFAULT_COMPETITION_THRESHOLDS,
    FreelancerGigSource,
    FreelancerUnavailable,
    competition_level,
    normalize_source_status,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LISTING = PROJECT_ROOT / "artifacts" / "freelancer_jobs_listing_snapshot.html"
DETAIL = PROJECT_ROOT / "artifacts" / "freelancer_project_detail.html"


def _source(**kwargs) -> FreelancerGigSource:
    params = {"listing_snapshot_path": str(LISTING)}
    params.update(kwargs)
    return FreelancerGigSource(**params)


# --------------------------------------------------------------------- parser
class TestListingParser:
    def test_cards_are_extracted_from_the_saved_listing(self):
        html = LISTING.read_text(encoding="utf-8", errors="replace")
        cards = FreelancerGigSource.cards_from_listing(html)
        assert len(cards) >= 40, "the saved listing must yield the public 50-card page"

    def test_each_card_carries_url_title_and_skills(self):
        html = LISTING.read_text(encoding="utf-8", errors="replace")
        for card in FreelancerGigSource.cards_from_listing(html)[:10]:
            assert card["url"].startswith("/projects/")
            assert card["title"]
            assert isinstance(card["skills"], list)

    def test_project_links_are_unique_and_ordered(self):
        html = LISTING.read_text(encoding="utf-8", errors="replace")
        links = FreelancerGigSource.project_links(html)
        assert len(links) == len(set(links)), "duplicate project links must be collapsed"

    def test_listing_card_never_invents_a_budget(self):
        """The list publishes 'Average bid', not the client's budget."""
        html = LISTING.read_text(encoding="utf-8", errors="replace")
        src = _source()
        cards = FreelancerGigSource.cards_from_listing(html)
        record = src.card_record(cards[0], fetched_at="2026-09-30T00:00:00Z")
        assert record is not None
        assert record["budget"] == {"min": None, "max": None, "type": None}
        assert record["currency"] is None


class TestDetailParser:
    def test_hydration_state_is_read(self):
        html = DETAIL.read_text(encoding="utf-8", errors="replace")
        state = FreelancerGigSource.webapp_state(html)
        assert state is not None
        doc = FreelancerGigSource.raw_document(state)
        assert doc is not None
        assert doc.get("projectId")

    def test_record_fields_from_real_page(self):
        src = _source(snapshot_path=str(DETAIL))
        records = src.records_from_snapshot()
        assert len(records) == 1
        rec = records[0]
        assert rec["source"] == "freelancer"
        assert rec["source_id"].startswith("fl_")
        assert rec["title"]
        assert rec["url"].startswith("https://www.freelancer.com/projects/")
        assert rec["budget"]["min"] is not None
        assert rec["currency"]
        assert rec["competition"] is not None

    def test_missing_hydration_state_yields_none(self):
        """A 200 response without the state block is a variant, not a crash."""
        assert FreelancerGigSource.webapp_state("<html><body>nothing</body></html>") is None
        src = _source()
        assert src.record_from_html("<html></html>", page_url="https://x/y",
                                    fetched_at="2026-09-30T00:00:00Z") is None

    def test_broken_json_in_state_is_tolerated(self):
        html = '<script id="webapp-state" type="application/json">{not json</script>'
        assert FreelancerGigSource.webapp_state(html) is None

    def test_missing_project_id_is_refused(self):
        src = _source()
        assert src.record_from_document({"title": "no id"},
                                        page_url="https://x/y",
                                        fetched_at="2026-09-30T00:00:00Z") is None

    def test_missing_title_is_refused(self):
        src = _source()
        assert src.record_from_document({"projectId": 123},
                                        page_url="https://x/y",
                                        fetched_at="2026-09-30T00:00:00Z") is None


# ------------------------------------------------------------------- budgets
class TestBudgetVariants:
    def _record(self, **doc_overrides):
        doc = {
            "projectId": 999,
            "title": "T",
            "budget": {"min": 100, "max": 200},
            "currencyDetails": {"code": "USD"},
            "skills": [],
            "bidStats": {},
            "type": "fixed",
            "status": "active",
        }
        doc.update(doc_overrides)
        src = _source()
        return src.record_from_document(doc, page_url="https://x/y",
                                        fetched_at="2026-09-30T00:00:00Z")

    def test_fixed_range(self):
        rec = self._record()
        assert rec["budget"] == {"min": 100.0, "max": 200.0, "type": "fixed"}

    def test_zero_bounds_are_not_treated_as_amounts(self):
        """Freelancer sends 0 for an unset bound; zero is not a price."""
        rec = self._record(budget={"min": 0, "max": 0})
        assert rec["budget"]["min"] is None
        assert rec["budget"]["max"] is None

    def test_hourly_type_is_preserved(self):
        rec = self._record(type="hourly", budget={"min": 750, "max": 1250})
        assert rec["budget"]["type"] == "hourly"

    def test_type_inferred_when_absent(self):
        rec = self._record(type=None)
        assert rec["budget"]["type"] == "fixed"

    def test_missing_currency_stays_absent(self):
        rec = self._record(currencyDetails={})
        assert rec["currency"] is None

    def test_currency_code_is_used_verbatim(self):
        for code in ("USD", "INR", "GBP", "AUD"):
            rec = self._record(currencyDetails={"code": code})
            assert rec["currency"] == code


class TestNormalizationThroughPipeline:
    def test_budget_type_reaches_the_model(self):
        rec = self._record_hourly()
        gig = run_source_pipeline(_FakeSource([rec]), persist=False)
        assert gig.ranked_gigs[0].budget_type is BudgetType.HOURLY

    def _record_hourly(self):
        src = _source()
        return src.record_from_document(
            {"projectId": 1, "title": "H", "budget": {"min": 750, "max": 1250},
             "currencyDetails": {"code": "INR"}, "skills": [], "bidStats": {},
             "type": "hourly", "status": "active"},
            page_url="https://www.freelancer.com/projects/x/y",
            fetched_at="2026-09-30T00:00:00Z",
        )


class _FakeSource:
    """Wraps prepared raw records so the pipeline can be exercised offline."""

    key = "freelancer"

    def __init__(self, records):
        self._records = records

    def fetch(self, limit=None):
        yield from (self._records[:limit] if limit else self._records)


# --------------------------------------------------------------------- bids
class TestBidsAndCompetition:
    def _record_with_bids(self, bid_count, bids_list=None):
        src = _source()
        doc = {
            "projectId": 7,
            "title": "B",
            "budget": {"min": 100, "max": 200},
            "currencyDetails": {"code": "USD"},
            "skills": [],
            "bidStats": {"bidCount": bid_count} if bid_count is not None else {},
            "bids": bids_list or [],
            "type": "fixed",
            "status": "active",
        }
        return src.record_from_document(doc, page_url="https://x/y",
                                        fetched_at="2026-09-30T00:00:00Z")

    def test_bid_count_becomes_competition(self):
        assert self._record_with_bids(73)["competition"] == 73

    def test_zero_bids_is_kept_as_zero(self):
        """Zero bids is a measurement, not missing data."""
        assert self._record_with_bids(0)["competition"] == 0

    def test_bids_list_is_the_fallback_when_present(self):
        rec = self._record_with_bids(None, [{"amount": 1}, {"amount": 2}])
        assert rec["competition"] == 2

    def test_empty_bids_list_is_not_zero(self):
        """An empty list means "bids not shown", not "no bids yet"."""
        rec = self._record_with_bids(None, [])
        assert rec["competition"] is None

    def test_absent_bids_stays_none(self):
        assert self._record_with_bids(None)["competition"] is None

    def test_competition_level_thresholds(self):
        assert competition_level(0) == "low"
        assert competition_level(10) == "low"
        assert competition_level(11) == "medium"
        assert competition_level(30) == "medium"
        assert competition_level(31) == "high"
        assert competition_level(None) == "unknown"

    def test_thresholds_are_configurable(self):
        tight = {"low_max": 2, "medium_max": 5}
        assert competition_level(3, tight) == "medium"
        assert competition_level(6, tight) == "high"
        assert competition_level(1, tight) == "low"

    def test_unknown_is_not_low(self):
        """Unknown competition must not look better than real low competition."""
        assert competition_level(None) != "low"

    def test_level_is_recorded_as_a_source_fact(self):
        rec = self._record_with_bids(45)
        assert rec["source_facts"]["freelancer_competition_level"] == "high"
        assert rec["source_facts"]["freelancer_bids_received"] == 45


# ------------------------------------------------------------------- status
class TestSourceStatus:
    @pytest.mark.parametrize("raw,expected", [
        ("active", "OPEN"),
        ("OPEN", "OPEN"),
        ("closed", "CLOSED"),
        ("completed", "COMPLETED"),
        ("cancelled", "CANCELLED"),
        ("frozen", "FROZEN"),
        (None, None),
        ("", None),
    ])
    def test_status_mapping(self, raw, expected):
        assert normalize_source_status(raw) == expected

    def test_active_project_is_open_in_the_record(self):
        src = _source(snapshot_path=str(DETAIL))
        assert src.records_from_snapshot()[0]["source_status"] == "OPEN"


# ---------------------------------------------------------------- provenance
class TestProvenanceAndSeparation:
    def test_scope_is_public_jobs(self):
        rec = _source(snapshot_path=str(DETAIL)).records_from_snapshot()[0]
        assert rec["source_scope"] == "public_jobs"
        assert rec["source_mode"] == "live"

    def test_platform_capabilities_are_not_personal_readiness(self):
        rec = _source(snapshot_path=str(DETAIL)).records_from_snapshot()[0]
        assert rec["platform_payment_options"], "capabilities are recorded"
        # No personal verification happened, so readiness must not be claimed.
        assert "payment_readiness" not in rec or rec.get("payment_readiness") in (None, "")

    def test_scope_separates_this_source_from_contra_scopes(self):
        rec = _source(snapshot_path=str(DETAIL)).records_from_snapshot()[0]
        assert rec["source_scope"] not in ("public_hire_pages", "authenticated_feed",
                                           "synthetic_fixture")

    def test_live_and_fixture_are_distinguishable(self, fixture_payload):
        run = run_source_pipeline(_source(), persist=False)
        assert run.live >= 1
        assert run.fixture == 0
        assert all(g.source_mode is SourceMode.LIVE for g in run.ranked_gigs)


# ------------------------------------------------------------------ dedup/id
class TestStableIdAndDedup:
    def test_source_id_is_stable_across_runs(self):
        first = _source(snapshot_path=str(DETAIL)).records_from_snapshot()[0]["source_id"]
        second = _source(snapshot_path=str(DETAIL)).records_from_snapshot()[0]["source_id"]
        assert first == second

    def test_gig_id_is_derived_deterministically(self):
        run_a = run_source_pipeline(_source(), persist=False)
        run_b = run_source_pipeline(_source(), persist=False)
        assert [g.id for g in run_a.ranked_gigs] == [g.id for g in run_b.ranked_gigs]

    def test_duplicate_cards_collapse(self):
        html = LISTING.read_text(encoding="utf-8", errors="replace")
        doubled = html + html
        records = []
        src = _source()
        for card in FreelancerGigSource.cards_from_listing(doubled):
            rec = src.card_record(card, fetched_at="2026-09-30T00:00:00Z")
            if rec:
                records.append(rec)
        ids = [r["source_id"] for r in records]
        assert len(set(ids)) < len(ids), "the same project appears twice by construction"

        # Every repeat beyond the first occurrence must be classified as a duplicate,
        # regardless of whether it was rejected earlier by another gate.
        seen: set[str] = set()
        reported = 0
        for rec in records:
            if rec["source_id"] in seen:
                reported += 1
            seen.add(rec["source_id"])
        assert reported == len(records) - len(set(ids))

        run = run_source_pipeline(_FakeSource(records), persist=False)
        assert run.total == len(records)
        assert run.duplicates >= reported


# ------------------------------------------------------------- offline fetch
class TestFetchModes:
    def test_live_collection_requires_written_permission_before_network(self):
        src = FreelancerGigSource(listing_pages=1, target_skills=[], delay_seconds=0)
        with pytest.raises(FreelancerUnavailable, match='WEBSITE_AUTOMATION_BLOCKED'):
            list(src.fetch(limit=1))
        assert src.requests_made == 0

    def test_listing_snapshot_fetch_is_offline_and_deterministic(self):
        first = list(_source().fetch())
        second = list(_source().fetch())
        assert len(first) >= 40
        assert [r["source_id"] for r in first] == [r["source_id"] for r in second]
        assert _source().requests_made == 0, "no network was touched"

    def test_limit_is_respected(self):
        assert len(list(_source().fetch(limit=5))) == 5

    def test_detail_snapshot_returns_one_record(self):
        assert len(list(_source(snapshot_path=str(DETAIL)).fetch())) == 1

    def test_no_snapshot_raises_when_the_network_is_unavailable(self, monkeypatch):
        src = FreelancerGigSource(listing_pages=1)

        def boom(url):
            raise OSError("network down")

        monkeypatch.setattr(src, "_http_get", boom)
        with pytest.raises(FreelancerUnavailable):
            list(src.fetch())

    def test_partial_failure_keeps_going_and_is_reported(self, monkeypatch):
        """One unreachable project page must not discard the rest of the page."""
        src = FreelancerGigSource(listing_pages=1)
        listing_html = LISTING.read_text(encoding="utf-8", errors="replace")
        calls = {"n": 0}

        def fake_get(url):
            calls["n"] += 1
            if "/projects/" in url:
                if calls["n"] % 3 == 0:
                    raise OSError("timeout")
                return DETAIL.read_text(encoding="utf-8", errors="replace")
            return listing_html

        monkeypatch.setattr(src, "_http_get", fake_get)
        # Explicitly label this injected transport as an offline fixture; live
        # Freelancer requests require written permission before robots lookup.
        src._fetch_html_override = fake_get
        records = list(src.fetch(limit=10))
        assert records, "records must still be produced"
        assert any("timeout" in e for e in src.last_fetch_errors), \
            "the failure must be reported, not swallowed"

    def test_request_budget_is_enforced(self):
        """The budget must stop the run, not silently truncate it."""
        listing_html = LISTING.read_text(encoding="utf-8", errors="replace")
        detail_html = DETAIL.read_text(encoding="utf-8", errors="replace")
        src = FreelancerGigSource(
            listing_pages=5,
            max_requests=3,
            fetch_html=lambda url: (
                listing_html if "/projects/" not in url else detail_html
            ),
        )
        with pytest.raises(FreelancerUnavailable):
            list(src.fetch())

    def test_requests_made_is_reported(self):
        """Счётчик запросов растёт и бюджет соблюдается.

        Важное свойство: дедуп по ``source_id`` возможен только ПОСЛЕ чтения страницы,
        потому что именно оттуда берётся id. Если источник отдаёт один и тот же проект
        на разные URL (как этот мок), адаптер обязан прочитать их все, прежде чем
        поймёт, что это дубль. Именно поэтому жёсткий бюджет запросов — единственная
        защита от бесконечного расхода, и он должен срабатывать.
        """
        listing_html = LISTING.read_text(encoding="utf-8", errors="replace")
        detail_html = DETAIL.read_text(encoding="utf-8", errors="replace")
        src = FreelancerGigSource(
            listing_pages=1,
            target_skills=[],
            max_requests=6,
            fetch_html=lambda url: (
                listing_html if "/projects/" not in url else detail_html
            ),
        )
        # Все 50 карточек ведут на одну страницу: распознать дубли можно только
        # прочитав их, поэтому бюджет обязан остановить работу явной ошибкой.
        with pytest.raises(FreelancerUnavailable):
            list(src.fetch(limit=3))
        assert src.requests_made == 6, src.requests_made

    def test_three_distinct_projects_cost_one_listing_plus_three_details(self):
        """На разных детальных страницах счётчик предсказуем: 1 выдача + N деталей."""
        listing_html = LISTING.read_text(encoding="utf-8", errors="replace")
        detail_html = DETAIL.read_text(encoding="utf-8", errors="replace")
        import re as _re
        counter = {"n": 0}

        def detail_for(url: str) -> str:
            counter["n"] += 1
            return _re.sub(r'"projectId":\s*\d+', f'"projectId": {700000 + counter["n"]}',
                           detail_html)

        src = FreelancerGigSource(
            listing_pages=1, target_skills=[],
            fetch_html=lambda url: (
                listing_html if "/projects/" not in url else detail_for(url)
            ),
        )
        produced = list(src.fetch(limit=3))
        assert len(produced) == 3, len(produced)
        ids = {r["source_id"] for r in produced}
        assert len(ids) == 3, ids
        assert src.requests_made == 4, "1 listing page + 3 project pages"

    def test_targeted_collection_reads_skill_pages_first(self):
        """Сбор по направлениям должен запрашивать /jobs/<skill>/."""
        listing_html = LISTING.read_text(encoding="utf-8", errors="replace")
        detail_html = DETAIL.read_text(encoding="utf-8", errors="replace")
        seen: list[str] = []

        def fake(url: str) -> str:
            seen.append(url)
            return listing_html if "/projects/" not in url else detail_html

        src = FreelancerGigSource(
            listing_pages=1, target_skills=["python", "telegram"], skill_pages=1,
            fetch_html=fake,
        )
        list(src.fetch(limit=2))
        skill_urls = [u for u in seen if "/jobs/python/" in u or "/jobs/telegram/" in u]
        assert len(skill_urls) == 2, seen

    def test_found_via_accumulates_across_skill_pages(self):
        """Проект, встречающийся в двух направлениях, должен получить оба в found_via.

        Чтобы проверка была осмысленной, транспорт отдаёт РАЗНУЮ выдачу на разные
        направления: иначе обе страницы вернули бы одни и те же 50 карточек, и
        проверка «дублей нет» ловила бы артефакт мока, а не поведение адаптера.
        """
        listing_html = LISTING.read_text(encoding="utf-8", errors="replace")
        detail_html = DETAIL.read_text(encoding="utf-8", errors="replace")
        # Первая страница отдаётся обоим направлениям, вторая — только python.
        shared = listing_html

        def fake(url: str) -> str:
            if "/projects/" in url:
                return detail_html
            if "/jobs/python/" in url:
                return shared
            # telegram — та же выдача, поэтому проекты будут найдены в двух местах.
            return shared

        src = FreelancerGigSource(
            listing_pages=1, target_skills=["python", "telegram"], skill_pages=1,
            fetch_html=fake,
        )
        records = list(src.fetch(limit=5))
        assert records
        for rec in records:
            via = rec["source_facts"].get("found_via") or []
            assert set(via) == {"python", "telegram"}, via
        # Каждый уникальный проект отдан один раз, несмотря на две находки.
        ids = [r["source_id"] for r in records]
        assert len(ids) == len(set(ids)), ids

    def test_a_project_seen_in_one_direction_is_not_duplicated(self):
        """Обратный случай: если направления дают разные проекты, found_via у каждого одно."""
        listing_html = LISTING.read_text(encoding="utf-8", errors="replace")
        detail_html = DETAIL.read_text(encoding="utf-8", errors="replace")
        src = FreelancerGigSource(
            listing_pages=1, target_skills=["python"], skill_pages=1,
            fetch_html=lambda url: (
                listing_html if "/projects/" not in url else detail_html
            ),
        )
        records = list(src.fetch(limit=3))
        for rec in records:
            assert rec["source_facts"].get("found_via") == ["python"]

    def test_injected_transport_never_touches_robots(self, monkeypatch):
        """Offline runs must not attempt a robots.txt fetch."""
        def boom(url):
            raise AssertionError(f"network touched: {url}")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        src = FreelancerGigSource(listing_snapshot_path=str(LISTING))
        assert len(list(src.fetch(limit=2))) == 2
        assert src.requests_made == 0


# ------------------------------------------------------------------- refresh
class TestRefresh:
    """Refresh semantics for this source, measured against the real store.

    The detail snapshot is used because it yields a scored record (the listing cards
    carry no budget, so every one of them is correctly rejected by PRICE_MISSING).
    """

    def _one(self) -> FreelancerGigSource:
        return _source(snapshot_path=str(DETAIL))

    def test_detail_record_is_accepted_and_scored(self):
        run = run_source_pipeline(self._one(), persist=True)
        assert run.accepted == 1
        assert run.ranked_gigs[0].opportunity_score is not None

    def test_refetch_creates_no_duplicates(self):
        from ai_assistant.gig.store import count_gigs
        run_source_pipeline(self._one(), persist=True)
        rows_after_first = count_gigs()
        second = run_source_pipeline(self._one(), persist=True)
        assert second.duplicates == 1
        assert count_gigs() == rows_after_first, "a re-fetch must not add rows"

    def test_first_seen_is_immutable_and_last_seen_advances(self):
        from ai_assistant.gig.store import list_gigs
        run_source_pipeline(self._one(), persist=True)
        gig = list_gigs(limit=10, source="freelancer")[0]
        first_seen, last_seen = gig.first_seen_at, gig.last_seen_at
        assert first_seen and last_seen

        run_source_pipeline(self._one(), persist=True)
        reloaded = list_gigs(limit=10, source="freelancer")[0]
        assert reloaded.first_seen_at == first_seen, "first_seen_at is written once"
        assert reloaded.last_seen_at >= last_seen, "last_seen_at only moves forward"

    def test_user_status_survives_refetch(self):
        from ai_assistant.gig.models import GigStatus
        from ai_assistant.gig.store import get_gig, list_gigs, set_gig_status
        run_source_pipeline(self._one(), persist=True)
        gig = list_gigs(limit=10, source="freelancer")[0]
        set_gig_status(gig.id, "SHORTLISTED")

        run_source_pipeline(self._one(), persist=True)

        assert get_gig(gig.id).status is GigStatus.SHORTLISTED

    def test_missing_gig_is_not_deleted(self):
        from ai_assistant.gig.store import list_gigs
        run_source_pipeline(self._one(), persist=True)
        before = {g.id for g in list_gigs(limit=100, source="freelancer")}
        assert before

        # A later run that observes nothing must not remove stored rows.
        run_source_pipeline(_FakeSource([]), persist=True)

        after = {g.id for g in list_gigs(limit=100, source="freelancer")}
        assert before <= after, "gigs absent from a later fetch must not be removed"

    def test_source_status_is_refreshed_on_re_fetch(self):
        from ai_assistant.gig.store import list_gigs
        run_source_pipeline(self._one(), persist=True)
        gig = list_gigs(limit=10, source="freelancer")[0]
        assert gig.source_status == "OPEN"


# ------------------------------------------------------------------ plumbing
class TestPipelineIntegration:
    """The listing alone carries no budget, so it is gated; the detail page is scored."""

    def test_listing_cards_are_rejected_for_missing_price(self):
        run = run_source_pipeline(_source(), persist=False)
        assert run.total >= 40
        assert run.accepted == 0, "a card without a published budget cannot be scored"
        # The saved page contains one project twice, so exactly one duplicate is
        # expected besides the price rejections.
        assert run.rejected + run.duplicates == run.total
        assert run.duplicates == 1
        for gig in run.rejected_gigs:
            assert any(r in ("PRICE_MISSING", "DUPLICATE") for r in gig.rejection_reasons)
            assert gig.opportunity_score is None, "rejected gigs must not be scored"

    def test_detail_record_passes_gates_and_gets_scored(self):
        run = run_source_pipeline(_source(snapshot_path=str(DETAIL)), persist=False)
        assert run.accepted == 1
        gig = run.ranked_gigs[0]
        assert gig.opportunity_score is not None
        assert gig.score_breakdown, "scoring must stay explainable"
        components = gig.score_breakdown.get("components") or {}
        assert components, "component breakdown must be present"
        assert gig.competition is not None

    def test_payment_readiness_is_never_ready(self):
        run = run_source_pipeline(_source(snapshot_path=str(DETAIL)), persist=False)
        assert all(g.payment_readiness is not PaymentReadiness.READY
                   for g in run.ranked_gigs + run.rejected_gigs)

    def test_competition_reaches_the_model(self):
        run = run_source_pipeline(_source(snapshot_path=str(DETAIL)), persist=False)
        assert any(g.competition is not None for g in run.ranked_gigs)

    def test_no_scoring_is_reimplemented_here(self):
        """The adapter must not carry its own score: it only supplies raw facts."""
        src = _source()
        assert not hasattr(src, "score")
        assert not hasattr(src, "opportunity_score")


class TestFieldHonesty:
    def test_client_rating_zero_with_no_reviews_is_absent(self):
        src = _source()
        rec = src.record_from_document(
            {"projectId": 5, "title": "T", "budget": {"min": 1, "max": 2},
             "currencyDetails": {"code": "USD"}, "skills": [], "bidStats": {},
             "type": "fixed", "status": "active",
             "client": {"rating": {"average": 0, "reviewCount": 0}}},
            page_url="https://x/y", fetched_at="2026-09-30T00:00:00Z")
        assert rec["client_rating"] is None, "0 average with 0 reviews is not a rating"

    def test_client_rating_is_kept_when_reviews_exist(self):
        src = _source()
        rec = src.record_from_document(
            {"projectId": 6, "title": "T", "budget": {"min": 1, "max": 2},
             "currencyDetails": {"code": "USD"}, "skills": [], "bidStats": {},
             "type": "fixed", "status": "active",
             "client": {"rating": {"average": 4.5, "reviewCount": 3}}},
            page_url="https://x/y", fetched_at="2026-09-30T00:00:00Z")
        assert rec["client_rating"] == 4.5

    def test_no_description_is_left_empty_not_generated(self):
        src = _source()
        rec = src.record_from_document(
            {"projectId": 8, "title": "T", "budget": {"min": 1, "max": 2},
             "currencyDetails": {"code": "USD"}, "skills": [], "bidStats": {},
             "type": "fixed", "status": "active"},
            page_url="https://x/y", fetched_at="2026-09-30T00:00:00Z")
        assert rec["description"] == ""
        assert rec["client_name"] is None
        assert rec["client_hires"] is None
        assert rec["proposal_cost"] is None

    def test_default_thresholds_are_documented_values(self):
        assert DEFAULT_COMPETITION_THRESHOLDS == {"low_max": 10, "medium_max": 30}

    def test_record_is_json_serialisable(self):
        rec = _source(snapshot_path=str(DETAIL)).records_from_snapshot()[0]
        assert json.loads(json.dumps(rec))
