"""Tests for the notification priority & frequency policy (Phase 5C)."""
from __future__ import annotations

from ai_assistant.notification_policy import (
    DEFAULT_DIGEST_FLOOR,
    DEFAULT_HIGH_RELEVANCE_FLOOR,
    DIGEST,
    GIG_HIGH,
    IMMEDIATE,
    SUPPRESS,
    URGENT_KINDS,
    classify,
    classify_vacancy_row,
)


def test_urgent_employer_message_immediate():
    for kind in URGENT_KINDS:
        d = classify(kind)
        assert d.bucket == IMMEDIATE
        assert d.send_now is True


def test_high_relevance_gig_sent_separately():
    d = classify("gig", score=DEFAULT_HIGH_RELEVANCE_FLOOR)
    assert d.bucket == GIG_HIGH
    assert d.send_now is True


def test_mid_gig_goes_to_digest():
    d = classify("gig", score=DEFAULT_HIGH_RELEVANCE_FLOOR - 1)
    assert d.bucket == DIGEST
    assert d.send_now is False


def test_low_score_gig_suppressed():
    d = classify("gig", score=10.0)
    assert d.bucket == SUPPRESS


def test_duplicate_suppressed_even_if_urgent():
    d = classify("interview_invitation", is_duplicate=True)
    assert d.bucket == SUPPRESS


def test_rejected_suppressed_even_if_high_gig():
    d = classify("gig", score=99.0, is_rejected=True)
    assert d.bucket == SUPPRESS


def test_irrelevant_suppressed():
    d = classify("vacancy", score=90.0, is_irrelevant=True)
    assert d.bucket == SUPPRESS


def test_vacancy_below_digest_floor_suppressed():
    assert classify("vacancy", score=DEFAULT_DIGEST_FLOOR - 1).bucket == SUPPRESS
    assert classify("vacancy", score=DEFAULT_DIGEST_FLOOR).bucket == DIGEST


def test_vacancy_row_state_rejected_not_renotified():
    d = classify_vacancy_row({"state": "REJECTED", "score": 95.0, "is_gig": True})
    assert d.bucket == SUPPRESS


def test_vacancy_row_already_delivered_not_renotified():
    d = classify_vacancy_row({"state": "DISCOVERED", "score": 80.0}, already_delivered=True)
    assert d.bucket == SUPPRESS


def test_vacancy_row_high_gig_separate_mid_digest():
    assert classify_vacancy_row({"score": 80.0, "is_gig": True}).bucket == GIG_HIGH
    assert classify_vacancy_row({"score": 60.0, "is_gig": False}).bucket == DIGEST


def test_custom_thresholds_are_honored():
    d = classify("gig", score=60.0, high_relevance_floor=60.0, digest_floor=50.0)
    assert d.bucket == GIG_HIGH
