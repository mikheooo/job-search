"""T7.3 — согласование поиска с подтверждёнными предпочтениями.

Что здесь проверяется (по пункту 8 задания):

1. зарплатный минимум 1000 USD;
2. отсутствие общего запрета на Bitrix24;
3. сохранение остальных IT-направлений (Python/AI не вытеснены);
4. `years_experience` не подставляется как общий IT-стаж;
5. географические ограничения (A/B/C);
6. неизвестная зарплата не читается ни как ниже, ни как выше порога;
7. поиск отделён от отправки откликов.

Тесты делятся на два вида:

* **конфигурационные** — читают БОЕВОЙ `candidate_profile.json` (корневой: его
  грузит `load_candidate_profile`, когда cwd — корень репозитория) и
  `DEFAULT_SEARCH_QUERIES`. Они охраняют именно то, что пользователь
  подтвердил, от случайного отката.
* **поведенческие** — синтетический профиль и вакансия, проверяют логику
  matcher'а и очереди.

Тесты НЕ отправляют отклики и не ходят в сеть: conftest блокирует сокеты и
подставляет изолированную БД.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_assistant.candidate_profile import CandidateProfile, load_candidate_profile
from ai_assistant.matcher import (
    HardRequirementStatus,
    JobMatcher,
    RoleFamily,
    _coerce_profile,
    _family_domain_years,
    evaluate_hard_requirements,
)
from ai_assistant.schema import Vacancy

SUPPORT_QUERY_WORDS = (
    "it support",
    "technical support",
    "application support",
    "help desk",
    "service desk",
    "system administrator",
    "it administrator",
    "windows server",
    "active directory",
    "bitrix24",
    "crm",
    "infrastructure",
    "automation",
    "integrations",
)


def _make_vacancy(**kwargs) -> Vacancy:
    return Vacancy(
        source=kwargs.get("source", "himalayas"),
        source_job_id=kwargs.get("source_job_id", "v1"),
        title=kwargs.get("title", "IT Support Engineer"),
        company=kwargs.get("company", "Some Co"),
        description=kwargs.get("description", "Provide IT support."),
        job_url=kwargs.get("job_url", "https://example.com/job/1"),
        application_url=kwargs.get("application_url"),
        location=kwargs.get("location", "Remote"),
        country_restrictions=kwargs.get("country_restrictions", []),
        timezone_restrictions=kwargs.get("timezone_restrictions", []),
        salary_min=kwargs.get("salary_min"),
        salary_max=kwargs.get("salary_max"),
        salary_currency=kwargs.get("salary_currency"),
        employment_type=kwargs.get("employment_type", "Full Time"),
        published_at=kwargs.get("published_at", datetime.now(timezone.utc)),
    )


def _profile(**overrides) -> CandidateProfile:
    base = {
        "domain_years": {},
        "desired_roles": ["IT Support Engineer", "System Administrator"],
        "alternative_roles": ["Bitrix24 Administrator"],
        "role_families": ["TECH_SUPPORT", "SYSTEM_ADMIN", "APPLICATION_SUPPORT"],
        "core_skills": ["Windows Server", "Active Directory", "Linux", "Bitrix24"],
        "secondary_skills": ["Docker", "PostgreSQL"],
        "transferable_skills": ["Python", "Bash"],
        "seniority_range": ["Middle", "Senior"],
        "years_of_experience": 3,
        "remote_required": True,
        "allowed_locations": ["Remote", "Thailand"],
        "allowed_timezones": ["UTC+7", "Flexible"],
        "languages": ["English", "Russian"],
        "employment_types": ["Full Time", "Contract"],
        "minimum_salary": 1000.0,
        "salary_currency": "USD",
        "must_avoid_conditions": ["1C developer"],
        "excluded_roles": ["Java Developer"],
    }
    base.update(overrides)
    return CandidateProfile(**base)


# ---------------------------------------------------------------------------
# 1. Зарплатный минимум 1000 USD
# ---------------------------------------------------------------------------


def test_live_profile_salary_floor_is_1000_usd():
    """Боевой профиль: минимум 1000 USD и отметка, что это подтверждено."""
    profile = load_candidate_profile()
    assert profile.minimum_salary == 1000, (
        f"минимальный желаемый доход должен быть 1000 USD, а стоит {profile.minimum_salary}"
    )
    assert (profile.salary_currency or "").upper() == "USD"


def test_live_profile_marks_salary_floor_as_confirmed_by_user():
    """Порог 1000 — подтверждённое предпочтение, а не дефолт."""
    import json
    from pathlib import Path

    raw = json.loads(Path("candidate_profile.json").read_text(encoding="utf-8"))
    assert raw["provenance"]["salary_floor"] == "CONFIRMED_BY_USER", (
        "порог зарплаты должен быть помечен как подтверждённый пользователем; "
        f"сейчас {raw['provenance'].get('salary_floor')!r}"
    )


def test_salary_at_the_floor_is_not_below_the_floor():
    """1000 USD — это «проходит», а не «ниже порога»."""
    from ai_assistant.application_queue import _salary_fit_score

    score, reason = _salary_fit_score(
        _make_vacancy(salary_min=1000.0, salary_max=1000.0, salary_currency="USD"),
        _profile(),
    )
    assert score == 100
    assert "below" not in reason


def test_salary_below_the_floor_scores_zero():
    from ai_assistant.application_queue import _salary_fit_score

    score, reason = _salary_fit_score(
        _make_vacancy(salary_min=800.0, salary_max=900.0, salary_currency="USD"),
        _profile(),
    )
    assert score == 0
    assert "below" in reason


# ---------------------------------------------------------------------------
# 2. Отсутствие общего запрета на Bitrix24
# ---------------------------------------------------------------------------


def test_live_profile_has_no_blanket_bitrix24_ban():
    """Bitrix24 больше не отсекается ни списком ролей, ни условиями."""
    pd = _coerce_profile(load_candidate_profile())
    assert not any("bitrix" in role for role in pd["excluded_roles_lc"]), (
        f"Bitrix24 остался в excluded_roles: {pd['excluded_roles_lc']}"
    )
    assert not any("bitrix" in cond for cond in pd["must_avoid_conditions_lc"]), (
        f"Bitrix24 остался в must_avoid_conditions: {pd['must_avoid_conditions_lc']}"
    )


def test_live_profile_keeps_the_other_exclusions():
    """Снятие запрета на Bitrix24 не сняло остальные ограничения профиля."""
    pd = _coerce_profile(load_candidate_profile())
    joined = " ".join(pd["excluded_roles_lc"])
    for expected in ("1c", "php", "ruby", "java developer", "c++", "c#"):
        assert expected in joined, f"из excluded_roles пропал {expected!r}"


def test_bitrix24_vacancy_is_not_rejected_as_an_excluded_role():
    """Вакансия с Bitrix24 в тексте проходит жёсткий фильтр ролей."""
    pd = _coerce_profile(_profile())
    vac = _make_vacancy(
        title="Bitrix24 Administrator",
        description="Администрирование Bitrix24, CRM, интеграции, поддержка пользователей.",
    )
    status, reasons = evaluate_hard_requirements(pd, vac)
    assert status is not HardRequirementStatus.INELIGIBLE, reasons


# ---------------------------------------------------------------------------
# 3. Остальные IT-направления сохранены
# ---------------------------------------------------------------------------


def test_live_profile_keeps_all_five_role_families():
    profile = load_candidate_profile()
    families = set(profile.role_families or [])
    for expected in (
        "AI_AUTOMATION",
        "APPLICATION_SUPPORT",
        "TECH_SUPPORT",
        "PYTHON_BACKEND",
        "SYSTEM_ADMIN",
    ):
        assert expected in families, f"из role_families пропало направление {expected}"


def test_live_profile_lists_support_and_sysadmin_roles_alongside_python_and_ai():
    """Python/AI остались, но больше не единственное содержимое списка."""
    profile = load_candidate_profile()
    roles = [r.lower() for r in (profile.target_roles or [])]

    for expected in (
        "it support engineer",
        "technical support specialist",
        "application support specialist",
        "help desk engineer",
        "service desk engineer",
        "system administrator",
        "it administrator",
        "windows server administrator",
        "active directory administrator",
        "bitrix24 administrator",
        "bitrix24 support engineer",
        "crm administrator",
        "crm support engineer",
        "it infrastructure engineer",
        "it automation engineer",
        "integrations engineer",
    ):
        assert expected in roles, f"в target_roles нет направления {expected!r}"

    for expected in (
        "ai automation engineer",
        "ai automation developer",
        "n8n developer",
        "automation engineer",
        "ai agent developer",
    ):
        assert expected in roles, f"из target_roles вытеснено направление {expected!r}"


def test_live_profile_keeps_russian_role_names_for_russian_boards():
    profile = load_candidate_profile()
    roles = [r.lower() for r in (profile.target_roles or [])]
    for expected in (
        "специалист технической поддержки",
        "системный администратор",
        "администратор bitrix24",
        "инженер по сопровождению",
    ):
        assert expected in roles, f"в target_roles нет русского названия {expected!r}"


def test_headline_role_is_unchanged_for_cover_letter():
    """desired_roles[0] — заголовок сопроводительного письма, его трогать нельзя."""
    profile = load_candidate_profile()
    assert profile.desired_roles[0] == "AI Automation Engineer"


# ---------------------------------------------------------------------------
# 4. years_experience не подставляется как общий IT-стаж
# ---------------------------------------------------------------------------


def test_family_domain_years_is_none_without_established_tenure():
    """Нет domain_years — нет установленного стажа, а не «3 года»."""
    assert _family_domain_years({}, RoleFamily.TECH_SUPPORT) is None
    assert _family_domain_years({}, RoleFamily.SYSTEM_ADMIN) is None
    assert _family_domain_years({}, RoleFamily.APPLICATION_SUPPORT) is None


def test_family_domain_years_ignores_years_experience():
    """years_experience не подставляется вместо стажа в домене."""
    pd = {"years_experience": 3, "domain_years": {}}
    assert _family_domain_years(pd, RoleFamily.TECH_SUPPORT) is None


def test_family_domain_years_uses_established_domain_tenure():
    pd = {"years_experience": 3, "domain_years": {"it_support": 11.0}}
    assert _family_domain_years(pd, RoleFamily.TECH_SUPPORT) == 11.0
    assert _family_domain_years(pd, RoleFamily.SYSTEM_ADMIN) == 11.0


def test_experience_gap_gate_does_not_fire_from_the_unestablished_field():
    """Вакансия «10+ лет» не становится BORDERLINE, если стаж не установлен."""
    pd = _coerce_profile(_profile())
    vac = _make_vacancy(
        title="Senior System Administrator",
        description="Требуется 10+ лет опыта администрирования Windows Server и Active Directory.",
    )
    status, _ = evaluate_hard_requirements(pd, vac, candidate_years=None)
    assert status is not HardRequirementStatus.BORDERLINE, (
        "ворота опыта сработали без установленного стажа — значит, поле "
        "years_experience всё ещё подставляется вместо стажа"
    )


def test_experience_gap_gate_would_fire_from_the_three_year_field():
    """Документирует, почему поле нельзя использовать: с «3» ворота ловят лишнее.

    Это ровно то поведение, которое T7.3 убрал из matcher'а. Тест закрепляет
    разницу между «стаж установлен» и «подставлена тройка».
    """
    pd = _coerce_profile(_profile())
    vac = _make_vacancy(
        title="Senior System Administrator",
        description="Требуется 10+ лет опыта администрирования Windows Server и Active Directory.",
    )
    status_with_field, _ = evaluate_hard_requirements(pd, vac, candidate_years=3)
    status_without, _ = evaluate_hard_requirements(pd, vac, candidate_years=None)
    assert status_with_field is HardRequirementStatus.BORDERLINE
    assert status_without is not HardRequirementStatus.BORDERLINE


def test_matcher_invents_no_experience_gap_without_established_tenure():
    """Без domain_years штраф за стаж не начисляется, даже если в профиле 3."""
    profile = _profile(domain_years={}, years_of_experience=3)
    vac = _make_vacancy(
        title="IT Support Engineer",
        description="Требуется 12 лет опыта поддержки Windows Server и Active Directory.",
    )
    res = JobMatcher(profile).match(vac)
    assert not any("Experience gap" in gap for gap in res.gaps), (
        f"штраф за стаж выдуман без установленного стажа: {res.gaps}"
    )


# ---------------------------------------------------------------------------
# 5. Географические ограничения (A / B / C)
# ---------------------------------------------------------------------------


def test_country_restricted_vacancy_is_ineligible_for_thailand():
    """C: вакансия прямо ограничена другой страной."""
    from ai_assistant.eligibility import EligibilityStatus, assess_vacancy_eligibility

    vac = _make_vacancy(
        title="Technical Support Manager",
        location="United States",
        country_restrictions=["United States"],
        description="Remote within the United States only.",
    )
    a = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert a.eligibility is EligibilityStatus.INELIGIBLE


def test_onsite_vacancy_is_ineligible_for_thailand():
    """C: требуется присутствие в офисе."""
    from ai_assistant.eligibility import EligibilityStatus, assess_vacancy_eligibility

    vac = _make_vacancy(
        title="IT Support Specialist",
        location="Warsaw, Poland",
        description="This is an onsite role, work from our office in Warsaw.",
    )
    a = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert a.eligibility is EligibilityStatus.INELIGIBLE


def test_unknown_work_format_is_unknown_not_eligible():
    """B: удалённость не подтверждена — это UNKNOWN, а не разрешение."""
    from ai_assistant.eligibility import EligibilityStatus, assess_vacancy_eligibility

    vac = _make_vacancy(
        source="habrcareer",
        title="Системный администратор",
        location=None,
        description="Компания ищет системного администратора. Полный рабочий день.",
    )
    a = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert a.eligibility is EligibilityStatus.UNKNOWN


def test_remote_board_without_location_is_only_a_platform_assumption():
    """Полоса A у удалённой площадки — допущение площадки, а не слово работодателя.

    Движок считает вакансию himalayas/weworkremotely/remoteok без указанной
    географии «worldwide». Это НЕ подтверждение, что работодатель берёт
    человека из Таиланда, и в отчёте такие вакансии идут в полосу B.

    T7.4 ужесточил это: раньше тест проверял только то, что полосы A и B
    различимы в отчёте, и допускал статус ELIGIBLE. Теперь метка площадки
    физически не может дать полосу A, поэтому правильное ожидание — UNKNOWN
    и полоса B. Статус ELIGIBLE здесь означал бы, что вакансия уходит в
    автоматическую очередь на основании ярлыка площадки, а это и есть дефект
    T7.4-1.
    """
    from ai_assistant.eligibility import EligibilityStatus, GeoBand, assess_vacancy_eligibility

    vac = _make_vacancy(
        source="himalayas",
        title="IT Support Engineer",
        location=None,
        description="Join our fully remote team.",
    )
    a = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert a.eligibility == EligibilityStatus.UNKNOWN
    assert a.geo_band == GeoBand.B
    # работодатель нигде не сказал «worldwide» и не назвал Таиланд
    blob = f"{vac.title} {vac.description}".lower()
    assert "worldwide" not in blob
    assert "thailand" not in blob


# ---------------------------------------------------------------------------
# 6. Неизвестная зарплата не читается ни как ниже, ни как выше порога
# ---------------------------------------------------------------------------


def test_unknown_salary_is_neutral_not_zero():
    from ai_assistant.application_queue import _salary_fit_score

    score, reason = _salary_fit_score(_make_vacancy(), _profile())
    assert score == 50, "неизвестная зарплата не должна читаться как «ниже порога»"
    assert "unknown" in reason
    # причина обязана прямо говорить, что сумма не истолкована ни вниз, ни вверх
    assert "not read as below" in reason, reason


def test_unknown_salary_differs_from_a_confirmed_low_salary():
    """Неизвестная зарплата и подтверждённо низкая — разные вещи."""
    from ai_assistant.application_queue import _salary_fit_score

    unknown, _ = _salary_fit_score(_make_vacancy(), _profile())
    low, _ = _salary_fit_score(
        _make_vacancy(salary_min=500.0, salary_max=600.0, salary_currency="USD"),
        _profile(),
    )
    assert unknown != low
    assert unknown > low


def test_currency_mismatch_is_not_silently_converted():
    """Разные валюты без надёжного курса — не «проходит» автоматически."""
    from ai_assistant.application_queue import _salary_fit_score

    score, reason = _salary_fit_score(
        _make_vacancy(salary_min=2000.0, salary_max=3000.0, salary_currency="EUR"),
        _profile(),
    )
    assert score == 0
    assert "currency" in reason.lower()


# ---------------------------------------------------------------------------
# 7. Поиск отделён от отправки откликов
# ---------------------------------------------------------------------------


def test_autonomous_agent_does_not_submit_by_default():
    from ai_assistant.hh_autonomous_agent import AutonomousConfig

    assert AutonomousConfig().submit_enabled is False, (
        "автономная отправка должна быть выключена по умолчанию"
    )


def test_autonomous_cycle_refuses_to_run_with_submission_enabled():
    from ai_assistant.hh_autonomous_agent import AutonomousConfig, run_autonomous_cycle

    cfg = AutonomousConfig(submit_enabled=True)
    with pytest.raises(NotImplementedError):
        run_autonomous_cycle(cfg)


def test_submit_without_explicit_confirmation_performs_no_action(capsys):
    """Без --confirm-submit отклик не уходит и браузер не поднимается."""
    from ai_assistant import cli

    rc = cli.submit_vacancy("himalayas:some-vacancy", confirm_submit=False, dry_run=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "Submit confirmation required" in out
    assert "No browser action performed" in out


def test_search_cycle_creates_no_submissions_or_applications(monkeypatch):
    """collect() кладёт вакансии в базу и не создаёт ни откликов, ни заявок."""
    import sqlite3

    from ai_assistant import cli, config
    from ai_assistant.db import init_db

    class _FakeAdapter:
        source = "himalayas"

        def fetch_vacancies(self):
            return [
                {
                    "source": "himalayas",
                    "source_job_id": "t73-1",
                    "title": "IT Support Engineer",
                    "company": "Test Co",
                    "description": "Remote IT support role.",
                    "job_url": "https://himalayas.app/companies/test-co/jobs/it-support-engineer",
                    "location": "Remote",
                }
            ]

    monkeypatch.setitem(cli.SOURCES, "himalayas", _FakeAdapter())
    init_db()
    cli.collect(["himalayas"])

    conn = sqlite3.connect(config.DB_FILE)
    try:
        submissions = conn.execute("SELECT COUNT(*) FROM application_submissions").fetchone()[0]
        vacancies = conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0]
    finally:
        conn.close()

    assert vacancies >= 1, "поисковый цикл обязан сохранить найденную вакансию"
    assert submissions == 0, "поисковый цикл не имеет права создавать отклики"


def test_collect_does_not_touch_the_submission_modules(monkeypatch):
    """collect() не зовёт ни одного модуля отправки."""
    import ai_assistant.application_queue as aq
    from ai_assistant import cli

    def _boom(*args, **kwargs):
        raise AssertionError("поисковый цикл попытался обратиться к отправке откликов")

    for name in ("enqueue_application", "prepare_application", "process_queue"):
        if hasattr(aq, name):
            monkeypatch.setattr(aq, name, _boom, raising=False)

    class _FakeAdapter:
        source = "himalayas"

        def fetch_vacancies(self):
            return []

    monkeypatch.setitem(cli.SOURCES, "himalayas", _FakeAdapter())
    assert cli.collect(["himalayas"]) == 0


# ---------------------------------------------------------------------------
# 8. Поисковые запросы покрывают направления и не вытесняются
# ---------------------------------------------------------------------------


def test_search_queries_cover_the_confirmed_directions():
    from ai_assistant.hh_autonomous_agent import DEFAULT_SEARCH_QUERIES

    joined = " ".join(DEFAULT_SEARCH_QUERIES).lower()
    for expected in SUPPORT_QUERY_WORDS:
        assert expected in joined, f"в поисковых запросах нет направления {expected!r}"


def test_search_queries_keep_python_and_ai():
    from ai_assistant.hh_autonomous_agent import DEFAULT_SEARCH_QUERIES

    joined = " ".join(DEFAULT_SEARCH_QUERIES).lower()
    for expected in ("python", "ai engineer", "automation"):
        assert expected in joined, f"из запросов вытеснено направление {expected!r}"


def test_search_queries_include_russian_names_for_russian_boards():
    from ai_assistant.hh_autonomous_agent import DEFAULT_SEARCH_QUERIES

    joined = " ".join(DEFAULT_SEARCH_QUERIES).lower()
    for expected in (
        "специалист технической поддержки",
        "системный администратор",
        "администратор bitrix24",
    ):
        assert expected in joined, f"в запросах нет русского названия {expected!r}"


def test_search_query_list_is_not_truncated_in_the_discovery_loop():
    """Список запросов не обрезается до первых трёх (иначе Python/AI вытесняют всё).

    Проверяется по исходнику: в цикле обхода запросов не должно остаться
    срезки `search_queries[:3]`.
    """
    from pathlib import Path

    src = Path("ai_assistant/hh_autonomous_agent.py").read_text(encoding="utf-8")
    assert "search_queries[:3]" not in src, (
        "обнаружена срезка списка запросов до трёх — остальные направления снова вытеснены"
    )
    assert "for query in self.config.search_queries:" in src
