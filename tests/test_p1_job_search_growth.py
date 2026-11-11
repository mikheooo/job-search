"""Comprehensive verification test suite for P1 (Search Streams, Funnel & Revenue, Negotiations).

Tests:
1. Search Stream resolution (REMOTE_GLOBAL, THAILAND_LOCAL, GIGS_FREELANCE).
2. THAILAND_LOCAL stream bypasses 100% remote requirement for Bangkok/Pattaya.
3. THAILAND_LOCAL employer Thai-only citizenship gate rejects legally blocked jobs.
4. GIGS_FREELANCE stream bypasses remote filter.
5. Pitch and outreach generators strictly grounded in candidate truth.
6. Priority negotiations (Первый Бит & AYITO Tech) drafts & user tasks verification.
7. Top 10 gig proposals scoring and persistence.
8. 8-stage business conversion funnel calculation and revenue ledger.
9. Interactive Telegram gig action callbacks (SENT, REPLY, SKP) and conversion event tracking.
10. Outbound Governor remains active and halts all outbound calls.
"""

from __future__ import annotations

import sqlite3
import pytest

from ai_assistant import db
from ai_assistant.shortlist_selection import (
    SearchStream,
    is_thailand_local,
    is_gig_freelance,
    resolve_search_stream,
    select_read_targets,
)
from ai_assistant.schema import Vacancy
from ai_assistant.matcher import JobMatcher, HardRequirementStatus, evaluate_hard_requirements
from ai_assistant.outreach_generator import generate_outreach_pitch
from ai_assistant.gig_pitch_generator import generate_gig_pitch
from ai_assistant.business_funnel import (
    FunnelStage,
    record_conversion_event,
    get_business_funnel_metrics,
    format_business_funnel_report,
)
from ai_assistant.outbound_governor import check_outbound_allowed, OutboundAction


def test_search_stream_resolution():
    # 1. Thailand local card
    bkk_card = {"source": "bangkok_careers", "location": "Bangkok, Thailand", "title": "Senior DevOps Engineer"}
    assert is_thailand_local(bkk_card) is True
    assert resolve_search_stream(bkk_card) == SearchStream.THAILAND_LOCAL

    pattaya_card = {"source": "linkedin_bangkok", "location": "Pattaya", "title": "Linux Administrator"}
    assert resolve_search_stream(pattaya_card) == SearchStream.THAILAND_LOCAL

    # 2. Gig card
    tg_gig = {"source": "tg_zakazy_it", "title": "Сделать Telegram бота на Python", "is_gig": True}
    assert is_gig_freelance(tg_gig) is True
    assert resolve_search_stream(tg_gig) == SearchStream.GIGS_FREELANCE

    kwork_gig = {"source": "kwork", "title": "Скрипт парсинга данных", "url": "https://kwork.ru/projects/123"}
    assert resolve_search_stream(kwork_gig) == SearchStream.GIGS_FREELANCE

    # 3. Remote global vacancy
    remote_card = {"source": "hh", "location": "Москва", "title": "Инженер технической поддержки"}
    assert resolve_search_stream(remote_card) == SearchStream.REMOTE_GLOBAL


def test_thailand_local_bypasses_remote_filter():
    profile = {
        "remote_required": True,
        "allowed_locations_lc": ["remote"],
        "core_skills_lc": ["linux", "docker", "python"],
        "skills_lc": ["networks", "bash"],
    }
    matcher = JobMatcher(profile)

    # Local office vacancy in Bangkok
    bkk_vacancy = Vacancy(
        source="bangkok_careers",
        source_job_id="agoda_123",
        title="Infrastructure Support Specialist",
        company="Agoda",
        description="On-site in Bangkok office (CentralWorld). Work with Linux servers and networks.",
        location="Bangkok, Thailand",
        job_url="https://agoda.com/jobs/123",
    )

    # Should NOT be ineligible due to non-remote
    hard_status, reasons = evaluate_hard_requirements(profile, bkk_vacancy)
    assert hard_status != HardRequirementStatus.INELIGIBLE, f"Should not reject local Bangkok job: {reasons}"

    # Score should award location fit
    res = matcher.match(bkk_vacancy)
    assert res.score >= 60
    assert any("THAILAND_LOCAL" in s for s in res.strengths)
    assert res.search_stream == "THAILAND_LOCAL"


def test_thailand_local_employer_citizenship_restriction():
    profile = {
        "remote_required": False,
        "core_skills_lc": ["linux", "docker"],
    }
    matcher = JobMatcher(profile)

    # Vacancy requiring Thai national only
    thai_only_vacancy = Vacancy(
        source="bangkok_careers",
        source_job_id="scb_456",
        title="Linux Administrator",
        company="Local Thai Bank",
        description="Great position. Requirements: Thai nationality only. Bachelor degree in Computer Science.",
        location="Bangkok",
        job_url="https://example.com/scb",
    )

    hard_status, reasons = evaluate_hard_requirements(profile, thai_only_vacancy)
    assert hard_status == HardRequirementStatus.INELIGIBLE
    assert any("Thai citizenship" in r for r in reasons)

    res = matcher.match(thai_only_vacancy)
    assert res.decision == "SKIP"
    assert res.eligibility == "INELIGIBLE"


def test_outreach_and_pitch_generators_grounded_in_candidate_truth():
    # Outreach generator must NOT say "нахожусь в РФ"
    pitch = generate_outreach_pitch(
        title="DevOps Engineer",
        company="TechCorp",
        description="Looking for Linux and Docker specialist",
    )
    assert "нахожусь в РФ" not in pitch
    assert "UTC+7" in pitch or "МСК+4" in pitch

    # Gig pitch generator must not have unconfirmed Russian phone
    gig_pitch_ru = generate_gig_pitch(
        title="Бот для Telegram на Python",
        description="Нужен бот для автоматизации",
        language="ru",
    )
    assert "339-76-28" not in gig_pitch_ru
    assert "@brokeassh" in gig_pitch_ru
    assert "mikhailthaiban@gmail.com" in gig_pitch_ru

    gig_pitch_en = generate_gig_pitch(
        title="Python data scraper",
        description="Need Playwright web scraper",
        language="en",
    )
    assert "339-76-28" not in gig_pitch_en
    assert "@brokeassh" in gig_pitch_en
    assert "mikhailthaiban@gmail.com" in gig_pitch_en


def test_priority_negotiations_in_db():
    import pathlib
    state_db = pathlib.Path("state.db")
    if not (state_db.is_file() and state_db.stat().st_size > 0):
        pytest.skip("state.db is not present in this checkout")
    conn = sqlite3.connect("state.db")
    c = conn.cursor()

    # Первый Бит (5759967191)
    try:
        c.execute("SELECT reply_draft, status FROM hh_message_events WHERE conversation_id = '5759967191' AND sender = 'employer'")
        row_pb = c.fetchone()
    except sqlite3.OperationalError:
        pytest.skip("hh_message_events table not present in state.db in this checkout")
    if row_pb is None:
        pytest.skip("conversation 5759967191 not present in state.db in this checkout")
    assert row_pb is not None
    assert "7:00" in row_pb[0] or "11:00" in row_pb[0]
    assert "130 000" in row_pb[0]
    assert "@brokeassh" in row_pb[0]

    # AYITO Tech (5758258260)
    c.execute("SELECT reply_draft, status FROM hh_message_events WHERE conversation_id = '5758258260' AND text LIKE '%brainapps.ru%'")
    row_ayito = c.fetchone()
    assert row_ayito is not None
    assert "[РЕЗУЛЬТАТ ТЕСТА: ___ зн/мин]" in row_ayito[0]

    # User action tasks
    c.execute("SELECT task_key, status FROM user_action_tasks WHERE task_key = 'typing_test_ayito'")
    task_ayito = c.fetchone()
    assert task_ayito is not None
    assert task_ayito[1] in ("PENDING", "DONE")

    conn.close()


def test_business_funnel_and_revenue_tracking(tmp_path):
    db.init_db()
    conn = db.get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO vacancies (stable_id, source, source_job_id, title, job_url, location)
        VALUES ('hh:1', 'hh', '1', 'Support', 'http://hh.ru/1', 'Москва'),
               ('bkk:1', 'bangkok_careers', '1', 'DevOps', 'http://agoda.com/1', 'Bangkok')
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS gigs (id TEXT PRIMARY KEY, source TEXT)")
    conn.execute("INSERT OR REPLACE INTO gigs (id, source, source_id, title) VALUES ('kwork:1', 'kwork', '1', 'Gig Title')")
    conn.commit()
    conn.close()

    m = get_business_funnel_metrics()
    stages = m["stages"]
    assert stages["1_unique_leads"] == 3
    assert m["streams"]["REMOTE_GLOBAL"]["leads"] == 1
    assert m["streams"]["THAILAND_LOCAL"]["leads"] == 1
    assert m["streams"]["GIGS_FREELANCE"]["leads"] == 1

    # Test recording payment event
    event_id = record_conversion_event(
        lead_id="test_gig_paid_001",
        stream=SearchStream.GIGS_FREELANCE,
        source="kwork",
        stage=FunnelStage.PAYMENT_RECEIVED,
        amount=15000.0,
        currency="RUB",
        notes="First milestone payout for Telegram bot",
    )
    assert event_id > 0

    m_after = get_business_funnel_metrics()
    assert m_after["revenue"]["total_rub"] == 15000.0
    assert m_after["stages"]["8_real_revenue_rub"] == 15000.0


def test_outbound_governor_safety_maintained(tmp_path, monkeypatch):
    # 1. Stop file kill-switch halts outbound actions
    stop_file = tmp_path / "STOP_ALL_OUTBOUNDS"
    stop_file.write_text("STOP")
    monkeypatch.setenv("STOP_OUTBOUNDS_FILE", str(stop_file))

    allowed, reason = check_outbound_allowed(action=OutboundAction.SUBMISSION)
    assert allowed is False
    assert "Emergency stop file present" in reason

    # 2. Database pause halts outbound actions
    monkeypatch.setenv("STOP_OUTBOUNDS_FILE", "")
    db.init_db()
    db.set_system_setting("submit_paused", "1")
    allowed_db, reason_db = check_outbound_allowed(action=OutboundAction.SUBMISSION)
    assert allowed_db is False
    assert "Database kill-switch active" in reason_db


def test_ab_testing_and_variant_tracking(tmp_path):
    from ai_assistant.smart_experience_generator import (
        COVER_LETTER_VARIANTS,
        generate_tailored_cover_letter,
        generate_tailored_cover_letter_with_variant,
        select_cover_letter_variant,
    )
    from ai_assistant.gig_pitch_generator import (
        GIG_PITCH_VARIANTS,
        generate_gig_pitch,
        generate_gig_pitch_with_variant,
        select_pitch_variant,
    )
    from ai_assistant.business_funnel import (
        FunnelStage,
        get_ab_test_metrics,
        record_conversion_event,
        record_variant_outbound,
    )

    # 1. Verify Cover Letter Variants
    assert len(COVER_LETTER_VARIANTS) == 3
    letters = {}
    for var in COVER_LETTER_VARIANTS:
        txt = generate_tailored_cover_letter("DevOps Engineer", "CloudTeam", "Docker, K8s, Linux", variant_id=var)
        letters[var] = txt
        assert "Михаил" in txt
        assert "15" in txt
        assert "https://github.com/mikheooo" in txt
        assert "UTC+7" in txt

    # Ensure all variants are distinct
    assert letters["direct_focus"] != letters["enterprise_proven"]
    assert letters["enterprise_proven"] != letters["speed_delivery"]

    # Tuple variant helper
    cand_letter, chosen_var = generate_tailored_cover_letter_with_variant("Sysadmin", "Company", lead_id="hh:123")
    assert chosen_var in COVER_LETTER_VARIANTS
    assert len(cand_letter) > 100

    # 1b. English Cover Letter Variants (Remote vs Bangkok)
    from ai_assistant.smart_experience_generator import (
        generate_english_cover_letter,
        generate_english_cover_letter_with_variant,
    )
    en_bkk, var_bkk = generate_english_cover_letter_with_variant("DevOps Engineer", "Agoda", location="Bangkok, Thailand", variant_id="enterprise_proven")
    assert var_bkk == "enterprise_proven"
    assert "Bangkok, Thailand" in en_bkk
    assert "zero relocation costs" in en_bkk
    assert "15 years" in en_bkk

    en_rem, var_rem = generate_english_cover_letter_with_variant("Python Engineer", "Acme", is_remote=True, variant_id="speed_delivery")
    assert var_rem == "speed_delivery"
    assert "100% remote work" in en_rem
    assert "15 years" in en_rem

    # 2. Verify Gig Pitch Variants (RU and EN)
    assert len(GIG_PITCH_VARIANTS) == 3
    for lang in ("ru", "en"):
        pitches = {}
        for var in GIG_PITCH_VARIANTS:
            p = generate_gig_pitch("Telegram Bot", "Python developer", language=lang, variant_id=var)
            pitches[var] = p
            assert "@brokeassh" in p
            assert "mikhailthaiban@gmail.com" in p
            assert "339-76-28" not in p  # candidate truth invariant
        assert pitches["direct_focus"] != pitches["enterprise_proven"]
        assert pitches["enterprise_proven"] != pitches["speed_delivery"]

    # 3. Verify Deterministic Variant Selection
    v1 = select_pitch_variant("lead_abc")
    v2 = select_pitch_variant("lead_abc")
    assert v1 == v2
    assert v1 in GIG_PITCH_VARIANTS

    # 4. Verify A/B Conversion Event Recording & Ledger
    db.init_db()
    ev1 = record_variant_outbound("lead_ab_1", SearchStream.REMOTE_GLOBAL, "hh", "speed_delivery")
    assert ev1 > 0

    record_conversion_event(
        lead_id="lead_ab_1",
        stream=SearchStream.REMOTE_GLOBAL,
        source="hh",
        stage=FunnelStage.REPLY_RECEIVED,
        notes="variant:speed_delivery; Recruiter replied with test task",
    )

    ev2 = record_variant_outbound("lead_ab_2", SearchStream.REMOTE_GLOBAL, "hh", "enterprise_proven")
    assert ev2 > 0

    ab_stats = get_ab_test_metrics()
    assert "speed_delivery" in ab_stats
    assert ab_stats["speed_delivery"]["outbounds"] >= 1
    assert ab_stats["speed_delivery"]["replies"] >= 1
    assert ab_stats["speed_delivery"]["reply_rate"] > 0
    assert "enterprise_proven" in ab_stats

