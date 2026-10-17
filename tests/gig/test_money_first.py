"""GIG-SEARCH-P5 — тесты Money-First признаков, Action Score и очереди действий.

Покрывает список §10 задания: дедуп между направлениями, ``found_via``, competition,
freshness, отсутствие выдуманного freshness, монотонность action_score, ``low`` не в
APPLY_NOW, свежее не хуже старого, 5 ставок выше 100, неизвестная валюта не USD,
P4-конверсия продолжает работать, существующие тесты Gig Search.

Всё офлайн и детерминированно: сеть не трогается.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_assistant.gig.action import (
    ACTION_WEIGHTS,
    APPLY_NOW_MIN_SCORE,
    SKIP_CAN_DELIVER_LOW,
    SKIP_EXCESSIVE_COMPETITION,
    SKIP_NON_IT_DOMAIN,
    SKIP_RATE_TOO_LOW,
    action_score_of,
    build_queue,
)
from ai_assistant.gig.assessment import has_it_signal, is_non_it_domain
from ai_assistant.gig.models import BudgetType, CanDeliver, Gig, RiskLevel
from ai_assistant.gig.money import (
    COMPETITION_BANDS,
    age_hours,
    competition_band,
    effective_rate_usd,
    freshness_band,
    money_signals,
    rate_unreliable,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LISTING = PROJECT_ROOT / "artifacts" / "freelancer_jobs_listing_snapshot.html"
DETAIL = PROJECT_ROOT / "artifacts" / "freelancer_project_detail.html"

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def gig(**overrides) -> Gig:
    base = dict(
        source="freelancer", source_id="fl_test", title="Python API automation job",
        description="Build a Python integration with a REST API and a database. " * 4,
        skills=["python", "api", "automation"],
        budget_min=500.0, budget_max=800.0, currency="USD",
        budget_type=BudgetType.FIXED, competition=5,
        published_at=iso(2), can_deliver=CanDeliver.HIGH, match_score=60.0,
        opportunity_score=55.0, estimated_effort_hours=40.0,
        effective_hourly_rate=16.0, risk_level=RiskLevel.LOW,
        source_facts={},
    )
    base.update(overrides)
    return Gig(**base)


# ------------------------------------------------------------- competition
class TestCompetitionBands:
    """Границы взяты из распределения корпуса, поэтому проверяются точно по ним."""

    @pytest.mark.parametrize("bids,expected", [
        (0, "excellent"), (5, "excellent"), (10, "excellent"),
        (11, "good"), (25, "good"),
        (26, "medium"), (50, "medium"),
        (51, "hard"), (100, "hard"),
        (101, "very_hard"), (300, "very_hard"),
    ])
    def test_band_boundaries(self, bids, expected):
        assert competition_band(bids) == expected

    def test_unknown_is_not_excellent(self):
        """Неизвестная конкуренция не должна выглядеть выгоднее известной низкой."""
        assert competition_band(None) == "unknown"
        assert competition_band(None) != "excellent"

    def test_bands_are_ordered_by_threshold(self):
        thresholds = [c for c, _ in COMPETITION_BANDS]
        assert thresholds == sorted(thresholds)

    def test_more_bids_never_scores_higher(self):
        from ai_assistant.gig.money import competition_score
        scores = [competition_score(competition_band(n))
                  for n in (0, 10, 25, 50, 100, 200)]
        assert scores == sorted(scores, reverse=True), scores

    def test_five_bids_beats_a_hundred(self):
        from ai_assistant.gig.money import competition_score
        few = competition_score(competition_band(5))
        many = competition_score(competition_band(100))
        assert few > many


# --------------------------------------------------------------- freshness
class TestFreshness:
    @pytest.mark.parametrize("hours,expected", [
        (0.5, "under_1h"), (1.0, "under_1h"),
        (3.0, "under_6h"), (6.0, "under_6h"),
        (12.0, "under_24h"), (24.0, "under_24h"),
        (48.0, "1_3_days"), (72.0, "1_3_days"),
        (100.0, "over_3_days"),
    ])
    def test_age_bands(self, hours, expected):
        assert freshness_band(iso(hours), now=NOW) == expected

    def test_missing_time_is_unknown_not_fresh(self):
        """Выдумывать время публикации запрещено: без него — unknown, и не 'свежее'."""
        assert freshness_band(None, now=NOW) == "unknown"
        assert freshness_band("", now=NOW) == "unknown"
        assert freshness_band("not-a-date", now=NOW) == "unknown"
        assert freshness_band(None, now=NOW) != "under_1h"

    def test_age_hours_computed(self):
        assert age_hours(iso(5), now=NOW) == pytest.approx(5.0, abs=0.1)

    def test_garbage_time_yields_none(self):
        assert age_hours("2026-13-45") is None
        assert age_hours("yesterday") is None

    def test_future_time_clamps_to_zero(self):
        """Часы могут разойтись; «опубликовано в будущем» — не отрицательный возраст."""
        assert age_hours(iso(-5), now=NOW) == 0.0

    def test_unknown_freshness_scores_lower_than_fresh(self):
        from ai_assistant.gig.money import freshness_score
        assert freshness_score("unknown") < freshness_score("under_1h")
        assert freshness_score("under_1h") > freshness_score("over_3_days")

    def test_fresher_is_never_worse(self):
        from ai_assistant.gig.money import freshness_score
        order = ["under_1h", "under_6h", "under_24h", "1_3_days", "over_3_days"]
        scores = [freshness_score(b) for b in order]
        assert scores == sorted(scores, reverse=True), scores


# -------------------------------------------------------------------- rate
class TestEffectiveRate:
    def test_rate_is_taken_from_the_converted_field(self):
        g = gig(effective_hourly_rate=12.5)
        assert effective_rate_usd(g) == 12.5

    def test_absent_rate_is_none_not_zero(self):
        g = gig(effective_hourly_rate=None)
        assert effective_rate_usd(g) is None

    def test_missing_rate_is_flagged_unreliable(self):
        assert rate_unreliable(gig(effective_hourly_rate=None)) is True

    def test_tiny_rate_is_flagged_unreliable(self):
        """Меньше доллара в час — это не ставка, и уверенность должна падать."""
        assert rate_unreliable(gig(effective_hourly_rate=0.07)) is True

    def test_normal_rate_is_reliable(self):
        assert rate_unreliable(gig(effective_hourly_rate=20.0)) is False


# ------------------------------------------------------------ unknown currency
class TestUnknownCurrencyIsNotUsd:
    """§5: неизвестная валюта не должна превращаться в USD."""

    def _raw(self, currency):
        return {
            "source": "freelancer", "source_id": "fl_cur", "title": "T",
            "currency": currency, "budget": {"min": 1000.0, "max": 2000.0, "type": "fixed"},
            "source_facts": {},
        }

    def test_unknown_currency_without_rate_is_not_converted(self):
        from ai_assistant.gig.normalizer import normalize_gig
        from ai_assistant.gig.scoring import budget_quality_score
        g = normalize_gig(self._raw("XYZ"))
        score, note = budget_quality_score(g)
        assert score == 0.0, f"unknown currency must not be scored as USD: {note}"
        assert "не сопоставлен" in note

    def test_p4_conversion_still_works(self):
        """P4-конверсия обязана продолжать работать после P5."""
        from ai_assistant.gig.normalizer import normalize_gig
        from ai_assistant.gig.scoring import budget_quality_score
        raw = self._raw("INR")
        raw["source_facts"] = {"freelancer_currency_exchange_rate": 0.010402}
        g = normalize_gig(raw)
        score, note = budget_quality_score(g)
        assert 0 < score < 40, note
        assert "USD" in note

    def test_missing_rate_on_a_foreign_currency_stays_undefined(self):
        from ai_assistant.gig.normalizer import normalize_gig
        from ai_assistant.gig.assessment import effective_hourly_rate
        g = normalize_gig(self._raw("INR"))
        assert effective_hourly_rate(g, 40.0) is None


# -------------------------------------------------------------- found_via
class TestFoundVia:
    def test_found_via_is_read_from_source_facts(self):
        g = gig(source_facts={"found_via": ["python", "api"]})
        signals = money_signals(g, now=NOW)
        assert set(signals.found_via) == {"python", "api"}

    def test_absent_found_via_is_empty(self):
        assert money_signals(gig(), now=NOW).found_via == ()

    def test_found_via_does_not_change_the_score(self):
        """§2: found_via — сигнал, но не множитель. Один заказ не должен дорожать."""
        one = action_score_of(gig(source_facts={"found_via": ["python"]}), now=NOW)
        many = action_score_of(
            gig(source_facts={"found_via": ["python", "api", "automation", "telegram"]}),
            now=NOW,
        )
        assert one.action_score == many.action_score

    def test_found_via_is_reported_in_signals(self):
        a = action_score_of(gig(source_facts={"found_via": ["python"]}), now=NOW)
        assert a.signals["found_via"] == ["python"]


# ------------------------------------------------------------- action score
class TestActionScore:
    def test_breakdown_has_every_component_and_weight(self):
        a = action_score_of(gig(), now=NOW)
        assert set(a.components) == set(ACTION_WEIGHTS)
        assert set(a.weights) == set(ACTION_WEIGHTS)
        for name in ACTION_WEIGHTS:
            assert name in a.notes, f"missing explanation for {name}"

    def test_score_is_bounded(self):
        a = action_score_of(gig(), now=NOW)
        assert 0.0 <= a.action_score <= 100.0

    def test_more_bids_does_not_increase_action_score(self):
        """§10: при прочих равных 5 ставок выше 100 ставок."""
        few = action_score_of(gig(competition=5), now=NOW).action_score
        many = action_score_of(gig(competition=100), now=NOW).action_score
        assert few > many

    def test_fresher_is_not_worse(self):
        """§10: более свежий аналогичный заказ не хуже старого."""
        fresh = action_score_of(gig(published_at=iso(1)), now=NOW).action_score
        stale = action_score_of(gig(published_at=iso(90)), now=NOW).action_score
        assert fresh >= stale, (fresh, stale)

    def test_higher_rate_does_not_lower_the_score(self):
        low = action_score_of(gig(effective_hourly_rate=5.0), now=NOW).action_score
        high = action_score_of(gig(effective_hourly_rate=35.0), now=NOW).action_score
        assert high > low

    def test_monotonic_in_base_score(self):
        low = action_score_of(gig(opportunity_score=20.0), now=NOW).action_score
        high = action_score_of(gig(opportunity_score=70.0), now=NOW).action_score
        assert high > low

    def test_low_deliverability_is_capped(self):
        a = action_score_of(gig(can_deliver=CanDeliver.LOW, match_score=100.0,
                                opportunity_score=100.0), now=NOW)
        assert a.action_score <= 44.0
        assert "deliverability_cap" in a.notes

    def test_unknown_rate_caps_confidence(self):
        a = action_score_of(gig(effective_hourly_rate=None, opportunity_score=100.0), now=NOW)
        assert a.action_score <= 60.0
        assert "rate_uncertainty" in a.notes

    def test_action_differs_from_old_score(self):
        """Главное требование §9: action_score не повторяет старый score."""
        old_top = gig(opportunity_score=80.0, competition=150, published_at=iso(120),
                      effective_hourly_rate=4.0)
        new_mid = gig(opportunity_score=50.0, competition=2, published_at=iso(1),
                      effective_hourly_rate=25.0)
        assert action_score_of(new_mid, now=NOW).action_score > \
            action_score_of(old_top, now=NOW).action_score


# ------------------------------------------------------------------- queue
class TestQueue:
    def test_low_never_reaches_apply_now(self):
        """§5, жёсткое правило."""
        queue = build_queue([gig(can_deliver=CanDeliver.LOW, competition=0,
                                 published_at=iso(1), effective_hourly_rate=50.0,
                                 opportunity_score=90.0)], now=NOW)
        assert queue.apply_now == []
        assert queue.skip and SKIP_CAN_DELIVER_LOW in queue.skip[0].skip_reasons

    def test_a_strong_gig_reaches_apply_now(self):
        queue = build_queue([gig(competition=3, published_at=iso(1),
                                 effective_hourly_rate=30.0, opportunity_score=70.0)], now=NOW)
        assert len(queue.apply_now) == 1
        assert queue.apply_now[0].decision == "APPLY_NOW"

    def test_excessive_competition_is_a_skip_reason(self):
        queue = build_queue([gig(competition=200)], now=NOW)
        entry = (queue.skip or queue.maybe)[0]
        assert SKIP_EXCESSIVE_COMPETITION in entry.skip_reasons

    def test_too_low_a_rate_is_a_skip_reason(self):
        queue = build_queue([gig(effective_hourly_rate=0.5)], now=NOW)
        entry = (queue.skip or queue.maybe)[0]
        assert SKIP_RATE_TOO_LOW in entry.skip_reasons

    def test_non_it_domain_is_a_skip_reason(self):
        video = gig(title="Animated YouTube Horror Short Film Creator",
                    description="Create an animated horror short film with sound design. " * 4,
                    skills=["animation", "video editing", "sound design"],
                    competition=6, effective_hourly_rate=40.0, opportunity_score=70.0)
        queue = build_queue([video], now=NOW)
        assert queue.apply_now == []
        assert SKIP_NON_IT_DOMAIN in queue.skip[0].skip_reasons

    def test_a_video_integration_job_is_not_skipped_as_non_it(self):
        """Заказ про API видео-сервиса — это интеграция, а не монтаж."""
        api_video = gig(title="Video platform API integration",
                        description="Integrate a video hosting REST API into our backend. " * 4,
                        skills=["api", "python", "restful api"],
                        competition=3, effective_hourly_rate=25.0, opportunity_score=65.0)
        ok, _markers = is_non_it_domain(api_video)
        assert has_it_signal(api_video)
        assert not (ok and not has_it_signal(api_video))
        queue = build_queue([api_video], now=NOW)
        assert queue.apply_now, "an API integration job must be recommendable"

    def test_every_skip_has_a_reason(self):
        """§6: причина обязательна."""
        gigs = [
            gig(can_deliver=CanDeliver.LOW),
            gig(competition=300),
            gig(effective_hourly_rate=0.2),
            gig(effective_hourly_rate=None),
        ]
        queue = build_queue(gigs, now=NOW)
        assert queue.skip
        for entry in queue.skip:
            assert entry.skip_reasons, entry.gig.title

    def test_counts_and_ordering(self):
        gigs = [
            gig(source_id="a", competition=2, published_at=iso(1), effective_hourly_rate=35.0,
                opportunity_score=75.0),
            gig(source_id="b", competition=3, published_at=iso(2), effective_hourly_rate=25.0,
                opportunity_score=70.0),
            gig(source_id="c", can_deliver=CanDeliver.LOW),
        ]
        queue = build_queue(gigs, now=NOW)
        counts = queue.counts()
        assert counts["APPLY_NOW"] + counts["MAYBE"] + counts["SKIP"] == 3
        scores = [e.action.action_score for e in queue.apply_now]
        assert scores == sorted(scores, reverse=True)

    def test_queue_entries_serialise(self):
        import json
        queue = build_queue([gig()], now=NOW)
        payload = [e.to_dict() for e in queue.all_entries()]
        assert json.loads(json.dumps(payload))


# ------------------------------------------------------------ 'make' defect
class TestDedupBetweenDirections:
    """§2/§10: один проект из нескольких направлений — одна запись, найденная один раз.

    Проверка идёт через pipeline, а не только на адаптере: именно там решается,
    попадёт ли запись в БД повторно.
    """

    def test_a_repeat_within_one_run_is_a_duplicate(self):
        """Один прогон с двумя записями одного проекта: 1 принята, 1 дубль.

        Это и есть реальная форма сбора: адаптер отдаёт поток, pipeline ведёт список
        в рамках прогона. Проверять дедуп двумя отдельными вызовами ``process_raw``
        нельзя — по документации они не пишут в БД и не делят список между собой.
        """
        from ai_assistant.gig.pipeline import run_source_pipeline

        class _Src:
            key = "freelancer"

            def __init__(self, records):
                self._records = records

            def fetch(self, limit=None):
                yield from self._records

        raw_a = {
            "source": "freelancer", "source_id": "fl_dedup_1",
            "url": "https://www.freelancer.com/projects/python/dedup-test",
            "title": "Python automation with API",
            "description": "Build a Python integration. " * 6,
            "budget": {"min": 500.0, "max": 800.0, "type": "fixed"},
            "currency": "USD", "competition": 4,
            "published_at": iso(3), "source_scope": "public_jobs",
            "source_mode": "live", "source_status": "OPEN",
            "source_facts": {"found_via": ["python"],
                             "freelancer_currency_exchange_rate": 1.0},
        }
        # Тот же проект, найденный во втором направлении: другой URL, тот же source_id.
        raw_b = dict(raw_a)
        raw_b["url"] = "https://www.freelancer.com/projects/automation/dedup-test"
        raw_b["source_facts"] = {"found_via": ["automation"],
                                 "freelancer_currency_exchange_rate": 1.0}

        result = run_source_pipeline(_Src([raw_a, raw_b]), persist=False)

        assert result.total == 2
        assert result.accepted == 1, result.stats()
        assert result.duplicates == 1, result.stats()
        assert result.rejected_gigs[0].rejection_reasons == ["DUPLICATE"]

    def test_same_source_id_maps_to_the_same_gig_id(self):
        from ai_assistant.gig.pipeline import GigPipeline
        raw = {
            "source": "freelancer", "source_id": "fl_same_id",
            "url": "https://www.freelancer.com/projects/python/same-id",
            "title": "T", "description": "Build a Python integration. " * 6,
            "budget": {"min": 100.0, "max": 200.0, "type": "fixed"},
            "currency": "USD", "competition": 1,
            "published_at": iso(1), "source_scope": "public_jobs",
            "source_mode": "live", "source_status": "OPEN",
            "source_facts": {"freelancer_currency_exchange_rate": 1.0},
        }
        other_url = dict(raw, url="https://www.freelancer.com/projects/x/same-id")
        pipeline = GigPipeline(persist=False)
        first = pipeline.process_raw(raw)
        second = pipeline.process_raw(other_url)
        assert first.source_id == second.source_id
        assert first.id == second.id, "stable source_id must yield a stable gig id"

    def test_found_via_is_preserved_on_the_record(self):
        from ai_assistant.gig.pipeline import GigPipeline
        raw = {
            "source": "freelancer", "source_id": "fl_via_1",
            "url": "https://www.freelancer.com/projects/python/via-test",
            "title": "Telegram bot with API integration",
            "description": "Build a Telegram bot that calls a REST API. " * 6,
            "budget": {"min": 400.0, "max": 900.0, "type": "fixed"},
            "currency": "USD", "competition": 3,
            "published_at": iso(2), "source_scope": "public_jobs",
            "source_mode": "live", "source_status": "OPEN",
            "source_facts": {"found_via": ["telegram", "python", "api"],
                             "freelancer_currency_exchange_rate": 1.0},
        }
        gig_obj = GigPipeline(persist=False).process_raw(raw)
        assert gig_obj.source_facts.get("found_via") == ["telegram", "python", "api"]


class TestAmbiguousSkillMatching:
    """Измеренный дефект: навык профиля 'make' (Make.com) совпадал с глаголом 'make'."""

    def test_make_as_a_verb_is_not_a_skill_match(self):
        from ai_assistant.gig.assessment import _matched_skills, load_candidate_profile, skill_confidences
        confidences = skill_confidences(load_candidate_profile())
        g = Gig(source="freelancer", source_id="x", title="Whole House Interior Design",
                description="We need someone to make a nice interior design. " * 4,
                skills=["interior design"], budget_min=1.0, budget_max=1.0,
                currency="USD", budget_type=BudgetType.FIXED)
        names = [s for s, _ in _matched_skills(g, confidences)]
        assert "make" not in names, names

    def test_make_com_in_context_is_a_skill_match(self):
        from ai_assistant.gig.assessment import _matched_skills, load_candidate_profile, skill_confidences
        confidences = skill_confidences(load_candidate_profile())
        g = Gig(source="freelancer", source_id="x", title="Make.com automation rebuild",
                description="Rebuild our Make.com scenarios and connect them to the CRM API. " * 3,
                skills=["make.com", "api"], budget_min=1.0, budget_max=1.0,
                currency="USD", budget_type=BudgetType.FIXED)
        names = [s for s, _ in _matched_skills(g, confidences)]
        assert "make" in names, names

    def test_a_non_it_gig_no_longer_reaches_high_on_a_stray_match(self):
        from ai_assistant.gig.assessment import assess_can_deliver, load_candidate_profile, skill_confidences
        confidences = skill_confidences(load_candidate_profile())
        g = Gig(source="freelancer", source_id="x",
                title="Armenian Spiritual Book Proofreading",
                description="Please proofread this book and make corrections. " * 6,
                skills=["proofreading", "copy editing"], budget_min=1.0, budget_max=1.0,
                currency="USD", budget_type=BudgetType.FIXED)
        level, _why, _risky = assess_can_deliver(g, confidences)
        assert level is not CanDeliver.HIGH, "a proofreading job is not ours to deliver"
