"""Tests for Automated Submit Policy Gate (Phase 2)."""

from __future__ import annotations

import json
import os
import pytest
from datetime import datetime, timedelta, timezone

from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.application_review import (
    ApplicationReview,
    ReviewStatus,
    compute_review_fingerprint,
    save_application_review,
)
from ai_assistant.hh_application_orchestrator import HHApplicationState
from ai_assistant.hh_submit_policy import (
    evaluate,
    route_policy_rejection,
    PolicyDecision,
)


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_policy.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file, "tmp_path": tmp_path}

    config.DB_FILE = orig_db


def _create_valid_package_and_review(
    sid: str = "hh:136704137",
    company: str = "Maxima.tech",
    title: str = "Python developer middle",
    letter_text: str = None,
):
    if letter_text is None:
        letter_text = (
            f"Здравствуйте! Меня очень заинтересовала позиция {title} в компании {company}. "
            "Я обладаю глубоким опытом разработки на Python, проектирования асинхронных сервисов, "
            "работы с PostgreSQL, Docker и FastAPI. В своих прошлых проектах я успешно реализовывал "
            "высоконагруженные распределенные архитектуры и оптимизировал запросы. "
            "Буду рад обсудить подробности на техническом интервью и внести существенный вклад в развитие вашей команды."
        )
    pkg = {
        "vacancy_stable_id": sid,
        "cover_letter": letter_text,
        "title": title,
        "employer": company,
        "validation_status": "VALID",
    }
    db.save_application_package(sid, "v1", json.dumps(pkg))
    fp = compute_review_fingerprint(sid, pkg)
    save_application_review(
        ApplicationReview(
            vacancy_stable_id=sid,
            status=ReviewStatus.APPROVED,
            form_fingerprint=fp,
            review_id=f"rev_{sid}",
        )
    )
    return pkg, fp, letter_text


def _insert_vacancy(
    sid: str,
    title: str,
    description: str,
    company: str = "Maxima.tech",
) -> None:
    """One vacancy row, written straight into the table.

    Gate 6 reads the vacancy with db.get_vacancy_by_id and, when there is no row,
    falls back to the *application's own* title (finding #52). A test that means
    to exercise the language comparison therefore has to put a row there - before
    #52 every test in this file compared the letter against the application.
    """
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO vacancies
                (stable_id, source, source_job_id, title, company, description,
                 job_url, state)
            VALUES (?, 'hh', ?, ?, ?, ?, ?, 'NEW')
            """,
            (sid, sid, title, company, description, f"https://hh.ru/vacancy/{sid}"),
        )
        conn.commit()
    finally:
        conn.close()


def test_policy_all_checks_pass(clean_db):
    """When all gates are satisfied, policy approves and returns SubmitApproval."""
    sid = "hh:136704137"
    pkg, fp, letter = _create_valid_package_and_review(sid)
    app = {
        "application_id": "app_valid_1",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": letter,
        "state": "READY_TO_SUBMIT",
    }
    decision = evaluate(app)
    assert decision.approve is True
    assert len(decision.checks_failed) == 0
    assert decision.approval is not None
    assert decision.approval.source == "policy"
    assert decision.approval.policy_version == "v2.0_autonomous"
    assert "fingerprint_match" in decision.checks_passed
    assert "letter_length" in decision.checks_passed
    assert "kill_switch" in decision.checks_passed


def test_policy_fingerprint_mismatch_fails(clean_db):
    """When review fingerprint does not match computed package fingerprint, policy rejects."""
    sid = "hh:136704137"
    _create_valid_package_and_review(sid)
    # Save a conflicting review with different fingerprint
    save_application_review(
        ApplicationReview(
            vacancy_stable_id=sid,
            status=ReviewStatus.APPROVED,
            form_fingerprint="tampered_fingerprint_hash",
            review_id="rev_tampered",
        )
    )
    app = {
        "application_id": "app_bad_fp",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": "Sample letter",
    }
    decision = evaluate(app)
    assert decision.approve is False
    assert "fingerprint_match" in decision.checks_failed
    assert any("fingerprint_mismatch" in r for r in decision.reasons)


def test_policy_cover_letter_empty_fails(clean_db):
    """Empty cover letter is rejected by policy."""
    sid = "hh:136704137"
    _create_valid_package_and_review(sid, letter_text="")
    app = {
        "application_id": "app_empty_letter",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": "",
    }
    decision = evaluate(app)
    assert decision.approve is False
    assert "letter_not_empty" in decision.checks_failed
    assert "cover_letter_empty" in decision.reasons


def test_policy_cover_letter_too_short_fails(clean_db):
    """Cover letter shorter than 300 characters is rejected."""
    sid = "hh:136704137"
    short_letter = "Привет! Я отличный Python разработчик в Maxima.tech. Возьмите меня!"
    _create_valid_package_and_review(sid, letter_text=short_letter)
    app = {
        "application_id": "app_short_letter",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": short_letter,
    }
    decision = evaluate(app)
    assert decision.approve is False
    assert "letter_length" in decision.checks_failed
    assert any("cover_letter_length_out_of_bounds" in r for r in decision.reasons)


def test_policy_cover_letter_too_long_fails(clean_db):
    """Cover letter longer than 2500 characters is rejected."""
    sid = "hh:136704137"
    long_letter = "Python developer в компании Maxima.tech. " + ("текст " * 600)
    _create_valid_package_and_review(sid, letter_text=long_letter)
    app = {
        "application_id": "app_long_letter",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": long_letter,
    }
    decision = evaluate(app)
    assert decision.approve is False
    assert "letter_length" in decision.checks_failed


def test_policy_cover_letter_placeholders_fails(clean_db):
    """Cover letter containing template placeholders like TODO or {{ is rejected."""
    sid = "hh:136704137"
    placeholder_letter = (
        "Здравствуйте! Позиция Python developer middle в компании Maxima.tech мне очень интересна. "
        "Мой опыт в технологиях {{TECH_STACK}} составляет более 5 лет. "
        "TODO: указать детали предыдущих проектов в финансовом секторе. "
        "Я готов быстро включиться в работу и принести пользу. " + ("опыт " * 30)
    )
    _create_valid_package_and_review(sid, letter_text=placeholder_letter)
    app = {
        "application_id": "app_placeholder",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": placeholder_letter,
    }
    decision = evaluate(app)
    assert decision.approve is False
    assert "letter_no_placeholders" in decision.checks_failed
    assert any("cover_letter_contains_placeholders" in r for r in decision.reasons)


def test_policy_cover_letter_no_target_mention_fails(clean_db):
    """Cover letter mentioning neither company name nor job title is rejected."""
    sid = "hh:136704137"
    generic_letter = (
        "Здравствуйте! Меня очень заинтересовала ваша вакансия. "
        "Я обладаю глубоким практическим опытом коммерческой разработки бэкенда, "
        "проектирования баз данных, настройки CI/CD пайплайнов и тестирования сервисов. "
        "В своей работе ориентируюсь на высокое качество кода, соблюдение сроков "
        "и активное взаимодействие с кросс-функциональной командой. Буду рад обратной связи!"
    )
    _create_valid_package_and_review(sid, letter_text=generic_letter)
    app = {
        "application_id": "app_no_mention",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": generic_letter,
    }
    decision = evaluate(app)
    assert decision.approve is False
    assert "letter_mentions_target" in decision.checks_failed


def test_policy_questionnaire_missing_required_answer_fails(clean_db):
    """When questionnaire has required questions without answers, policy rejects."""
    sid = "hh:136704137"
    pkg, fp, letter = _create_valid_package_and_review(sid)
    qid = "quest_test_missing_ans"
    db.save_hh_questionnaire({
        "questionnaire_id": qid,
        "vacancy_stable_id": sid,
        "fingerprint": "qfp_123",
        "questions": [
            {"question_id": "q_salary", "text": "Ожидаемый доход", "required": True},
        ],
        "answers": {},
    })
    app = {
        "application_id": "app_q_missing",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": letter,
        "questionnaire_id": qid,
        "answers": {},
    }
    decision = evaluate(app)
    assert decision.approve is False
    assert "questionnaire_complete" in decision.checks_failed
    assert any("missing_answer_for_q_salary" in r for r in decision.reasons)


def test_policy_questionnaire_uncertain_answer_fails(clean_db):
    """When questionnaire has uncertain answers like 'не знаю', policy rejects."""
    sid = "hh:136704137"
    pkg, fp, letter = _create_valid_package_and_review(sid)
    qid = "quest_test_uncertain"
    db.save_hh_questionnaire({
        "questionnaire_id": qid,
        "vacancy_stable_id": sid,
        "fingerprint": "qfp_123",
        "questions": [
            {"question_id": "q_exp", "text": "Опыт работы с Kafka", "required": True},
        ],
        "answers": {"q_exp": "не знаю"},
    })
    app = {
        "application_id": "app_q_uncertain",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": letter,
        "questionnaire_id": qid,
        "answers": {"q_exp": "не знаю"},
    }
    decision = evaluate(app)
    assert decision.approve is False
    assert "questionnaire_complete" in decision.checks_failed
    assert any("uncertain_answer_for_q_exp" in r for r in decision.reasons)


def test_policy_language_mismatch_fails(clean_db):
    """When vacancy is Russian and cover letter is pure English, policy rejects.

    The vacancy row is inserted for real (finding #52): without one gate 6
    compares the letter against the application's own title, which made this test
    pass for a reason that had nothing to do with the vacancy.
    """
    sid = "hh:136704137"
    en_letter = (
        "Hello hiring team! I am excited to apply for the Python developer position at Maxima.tech. "
        "I have extensive experience building scalable web applications, distributed microservices, "
        "and managing relational databases with PostgreSQL and Docker. Looking forward to discussing this role!"
    )
    _create_valid_package_and_review(sid, letter_text=en_letter)
    _insert_vacancy(
        sid,
        title="Разработчик бэкенда на Python middle",
        description=(
            "Ищем разработчика бэкенда на Python. Предстоит проектировать асинхронные "
            "сервисы, работать с PostgreSQL и Docker, оптимизировать тяжёлые запросы "
            "в высоконагруженных системах."
        ),
    )
    app = {
        "application_id": "app_lang_mismatch",
        "vacancy_stable_id": sid,
        "title": "Разработчик бэкенда на Python middle",
        "employer": "Максима Технологии",
        "draft": en_letter,
    }
    decision = evaluate(app)
    assert decision.approve is False
    assert "language_match" in decision.checks_failed
    assert "language_match_without_description" not in decision.checks_passed, (
        "the vacancy was read - this is a real mismatch, not an unread vacancy"
    )
    assert any("language_mismatch" in r for r in decision.reasons)


def test_policy_without_a_questionnaire_record_does_not_claim_the_check_passed(clean_db):
    """Finding #37: with no questionnaire on record the gate has nothing to look
    at. It used to record "questionnaire_complete" - a passed check - so the
    audit trail said the questionnaire had been verified.

    Measured on the real state.db: 8 of 10 rows in hh_applications carry no
    questionnaire_id, two of them in READY_TO_SUBMIT.
    """
    sid = "hh:136704137"
    _pkg, _fp, letter = _create_valid_package_and_review(sid)
    app = {
        "application_id": "app_no_questionnaire",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": letter,
        # no questionnaire_id - the state 8 of 10 real rows are in
    }

    decision = evaluate(app)

    assert "questionnaire_complete" not in decision.checks_passed, (
        "the gate recorded a passed check for a questionnaire that does not exist"
    )
    assert "questionnaire_not_tracked" in decision.checks_passed


def test_policy_with_an_undetectable_language_does_not_claim_the_check_passed(clean_db):
    """Finding #37: detect_text_language needs >=10 Cyrillic or >=50 Latin
    characters before it names a language. Below that it answers "unknown", and
    the gate used to record "language_match" - a passed check - for a comparison
    that never happened.

    The vacancy row is inserted for real (finding #52), with a description that
    is too short for the detector, so this test is about the detector and not
    about a vacancy that was never read.

    Measured over the 1996 vacancies in state.db: 37 (1.9%) come out "unknown".
    """
    sid = "hh:136704137"
    _insert_vacancy(sid, title="Python dev", description="Коротко.")
    app = {
        "application_id": "app_lang_unknown",
        "vacancy_stable_id": sid,
        "title": "Python dev",
        "employer": "ACME",
        "draft": "Hi, I want this job.",
    }

    decision = evaluate(app)

    assert "language_match" not in decision.checks_passed, (
        "the gate recorded a passed check for a language it never determined"
    )
    assert "language_match_undetermined" in decision.checks_passed
    assert "language_match_without_description" not in decision.checks_passed, (
        "the vacancy WAS read - its description is just too short to name a language"
    )


def test_policy_still_records_a_real_language_match_as_passed(clean_db):
    """The counter-check for the two tests above: when both sides are long enough
    to name a language and they agree, the record must stay "language_match".

    The vacancy row is inserted for real (finding #52). Before the fix this test
    reached "language_match" through the fallback text - the fixture's database
    held no vacancy at all, so the gate compared the letter against the
    application's own title and called that a match with the vacancy.
    """
    sid = "hh:136704137"
    ru_letter = (
        "Здравствуйте! Меня заинтересовала эта позиция, и я хотел бы откликнуться. "
        "У меня большой опыт разработки на Python, проектирования асинхронных сервисов, "
        "работы с PostgreSQL, Docker и FastAPI, а также оптимизации тяжёлых запросов "
        "в высоконагруженных системах."
    )
    _create_valid_package_and_review(sid, letter_text=ru_letter)
    _insert_vacancy(
        sid,
        title="Разработчик бэкенда на Python, удалённая работа",
        description=(
            "Требуется разработчик бэкенда. Вы будете проектировать асинхронные "
            "сервисы на Python, работать с PostgreSQL, Docker и FastAPI, а также "
            "оптимизировать тяжёлые запросы в высоконагруженных системах."
        ),
    )
    app = {
        "application_id": "app_lang_ok",
        "vacancy_stable_id": sid,
        "title": "Разработчик бэкенда на Python, удалённая работа",
        "employer": "Максима Технологии",
        "draft": ru_letter,
    }

    decision = evaluate(app)

    assert "language_match" in decision.checks_passed
    assert "language_match_undetermined" not in decision.checks_passed
    assert "language_match_without_description" not in decision.checks_passed


def test_policy_hourly_rate_limit_fails(clean_db):
    """When hourly submit count exceeds limit, policy rejects."""
    sid = "hh:136704137"
    pkg, fp, letter = _create_valid_package_and_review(sid)
    now_iso = datetime.now(timezone.utc).isoformat()
    # Insert 3 transitions in recent hour and set max_per_hour=3
    for i in range(3):
        db.save_hh_application_transition({
            "application_id": f"app_prior_{i}",
            "state": "SUBMITTED",
            "previous_state": "READY_TO_SUBMIT",
            "reason": "submit",
            "created_at": now_iso,
        })
    app = {
        "application_id": "app_rate_test",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": letter,
    }
    decision = evaluate(app, max_per_hour=3)
    assert decision.approve is False
    assert "rate_limit_hourly" in decision.checks_failed
    assert any("hourly_limit_exceeded" in r for r in decision.reasons)


def test_policy_daily_rate_limit_fails(clean_db):
    """When daily submit count exceeds limit, policy rejects."""
    sid = "hh:136704137"
    pkg, fp, letter = _create_valid_package_and_review(sid)
    now_iso = datetime.now(timezone.utc).isoformat()
    for i in range(5):
        db.save_hh_application_transition({
            "application_id": f"app_prior_day_{i}",
            "state": "SUBMITTED",
            "previous_state": "READY_TO_SUBMIT",
            "reason": "submit",
            "created_at": now_iso,
        })
    app = {
        "application_id": "app_day_limit",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": letter,
    }
    decision = evaluate(app, max_per_day=5)
    assert decision.approve is False
    assert "rate_limit_daily" in decision.checks_failed
    assert any("daily_limit_exceeded" in r for r in decision.reasons)


def test_policy_kill_switch_file_fails(clean_db, tmp_path):
    """When STOP_SUBMITS file exists, policy immediately rejects with reason='paused'."""
    sid = "hh:136704137"
    pkg, fp, letter = _create_valid_package_and_review(sid)
    stop_file = str(tmp_path / "STOP_SUBMITS")
    with open(stop_file, "w") as f:
        f.write("STOP")

    app = {
        "application_id": "app_stopped_file",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": letter,
    }
    decision = evaluate(app, stop_file_path=stop_file)
    assert decision.approve is False
    assert "kill_switch" in decision.checks_failed
    assert "paused" in decision.reasons


def test_policy_kill_switch_db_flag_fails(clean_db):
    """When database submit_paused flag is set, policy rejects with reason='paused'."""
    sid = "hh:136704137"
    pkg, fp, letter = _create_valid_package_and_review(sid)
    db.set_submit_paused(True)

    app = {
        "application_id": "app_paused_db",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": letter,
    }
    decision = evaluate(app)
    assert decision.approve is False
    assert "kill_switch" in decision.checks_failed
    assert "paused" in decision.reasons


def test_route_policy_rejection_moves_to_needs_human_review(clean_db):
    """Calling route_policy_rejection cleanly transitions app to NEEDS_HUMAN_REVIEW with evidence."""
    app_id = "app_rejection_routing"
    db.save_hh_application({
        "application_id": app_id,
        "state": "READY_TO_SUBMIT",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer",
        "employer": "Company",
    })
    decision = PolicyDecision(
        approve=False,
        checks_failed=["letter_length", "kill_switch"],
        reasons=["cover_letter_empty", "paused"],
    )
    res = route_policy_rejection(app_id, decision)
    assert res.ok is True
    assert res.to_state == "NEEDS_HUMAN_REVIEW"

    stored = db.get_hh_application(app_id)
    assert stored["state"] == "NEEDS_HUMAN_REVIEW"
    transitions = db.list_hh_application_transitions(app_id)
    assert len(transitions) == 1
    assert transitions[0]["state"] == "NEEDS_HUMAN_REVIEW"
    assert "paused" in transitions[0]["evidence"]["reasons"]

# ---------------------------------------------------------------------------
# Finding #48 - the third check in the same ledger that named itself "passed"
# without running
# ---------------------------------------------------------------------------
# Finding #37 fixed two of the six checks in evaluate(): a questionnaire that was
# not on record and a language that could not be determined both stopped being
# written down as passed. The third one was left behind, in the same ledger, four
# lines above the letter checks.
#
# When there is no stored fingerprint to compare against, but a fingerprint can
# be computed (a draft or a package exists), the gate took this branch:
#
#     elif actual_fp:
#         resolved_fp = actual_fp
#         checks_passed.append("fingerprint_match")   # nothing was compared
#
# Measured 2026-09-17 on the real state.db: of the 10 rows in hh_applications
# exactly one carries a draft (74 chars, state READY_TO_SUBMIT), and its
# vacancy_stable_id is NULL - so it cannot be joined to any application_reviews
# row at all, and this branch, not the comparison above, is the shape production
# is actually in. Only 1 of the 20 review rows carries a non-empty fingerprint.
#
# The 74-character draft is also too short for gate 4 (300-2500), so that
# particular row is refused on length and production has not yet *reached*
# submission with the false name in the ledger. What is reachable, and what the
# pins below measure, is the configuration itself: a full-length draft with no
# review row is approved with "fingerprint_match" written down.
#
# The record is not cosmetic: checks_passed travels into SubmitApproval and, from
# there, into the evidence of the SUBMITTED transition (hh_application_runner.py
# writes submit_approval.to_dict()), so the audit trail of a real submission
# asserted a comparison that never took place.

# The draft used by the two pins below. It has to stay inside 300-2500 characters
# or gate 4 fails it on length and the verdict stops being "approved", which hides
# what these pins are actually about: the first red run of this block used a
# 258-character draft and the control failed on
# "cover_letter_length_out_of_bounds (258 chars, expected 300-2500)" - a real
# failure, but of the fixture, not of the code under test. It also has to mention
# the title or the employer ("Python" does) for letter_mentions_target.
_DRAFT_ONLY_LETTER = (
    "Здравствуйте! Меня заинтересовала эта позиция, и я хотел бы откликнуться. "
    "У меня большой опыт разработки на Python: проектирование асинхронных сервисов, "
    "работа с PostgreSQL, Docker и FastAPI, оптимизация тяжёлых запросов в "
    "высоконагруженных системах. В прошлых проектах я отвечал за архитектуру "
    "внутреннего API, настраивал CI, писал тесты и разбирал инциденты в продакшене. "
    "Буду рад обсудить задачи команды на техническом интервью и рассказать подробнее "
    "о своём опыте."
)


def test_policy_without_a_stored_fingerprint_does_not_claim_the_check_passed(clean_db):
    """A computable fingerprint with nothing to compare it against is not a match."""
    app = {
        "application_id": "app_fp_uncomparable",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer",
        "employer": "Maxima.tech",
        "draft": _DRAFT_ONLY_LETTER,
        # no package and no review row - the shape the one real row with a draft is in
    }

    decision = evaluate(app)

    assert "fingerprint_match" not in decision.checks_passed, (
        "the gate recorded a passed fingerprint check without comparing anything"
    )
    assert "fingerprint_not_compared" in decision.checks_passed


def test_policy_still_records_a_real_fingerprint_match(clean_db):
    """Counter-check: with a stored fingerprint that agrees, the name stays."""
    sid = "hh:136704137"
    _pkg, _fp, letter = _create_valid_package_and_review(sid)
    app = {
        "application_id": "app_fp_ok",
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "draft": letter,
    }

    decision = evaluate(app)

    assert "fingerprint_match" in decision.checks_passed
    assert "fingerprint_not_compared" not in decision.checks_passed


def test_the_fingerprint_record_change_does_not_change_the_verdict(clean_db):
    """Finding #37's doctrine: the record stops lying, the verdict does not move.

    Two things have to stay true, and each guards a different wrong "fix": the
    approval must not start failing, and the computed fingerprint must not be
    thrown away to make the record honest.
    """
    app = {
        "application_id": "app_fp_verdict",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer",
        "employer": "Maxima.tech",
        "draft": _DRAFT_ONLY_LETTER,
    }

    decision = evaluate(app)

    assert decision.approve is True, (
        "the honest record must not turn into a new refusal"
    )
    assert decision.fingerprint, (
        "the computed fingerprint is a real value and must still be reported"
    )
    assert decision.approval is not None
    assert "fingerprint_not_compared" in decision.approval.checks_passed, (
        "the approval record is what reaches the audit trail"
    )
