"""CRM marketing must not inherit the candidate's technical CRM experience."""
import contextlib
import io
import json

import pytest

from ai_assistant import db
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.matcher import JobMatcher, RoleFamily, classify_role_family
from ai_assistant.schema import Vacancy


def _profile():
    return CandidateProfile(
        target_roles=['CRM Administrator', 'CRM Support Engineer', 'Bitrix24 Administrator'],
        role_families=['APPLICATION_SUPPORT', 'TECH_SUPPORT'],
        core_skills=['Python', 'n8n', 'CRM', 'Bitrix24', 'REST API'],
        role_priorities={'APPLICATION_SUPPORT': 'P1', 'TECH_SUPPORT': 'P1'},
    )


def _vacancy(title, suffix='crm'):
    return Vacancy(source='weworkremotely', source_job_id=suffix, title=title,
                   company='Example Corp', job_url=f'https://weworkremotely.com/jobs/{suffix}',
                   location='Remote Worldwide', salary_min=3000, salary_currency='USD',
                   description='Remote worldwide. CRM administration and integrations. '
                               'Python n8n Bitrix24 REST API. Email marketing, retention, LTV.')


@pytest.mark.parametrize('title', [
    'Social Discovery Group: Senior CRM Manager',
    'CRM Marketing Manager',
    'Менеджер CRM',
    'CRM-маркетолог',
    'Менеджер по продажам Битрикс24',
])
def test_business_crm_titles_are_not_technical_even_with_matching_tools(title):
    vacancy = _vacancy(title)
    assert classify_role_family(title, vacancy.description) == RoleFamily.OTHER
    result = JobMatcher(_profile()).match(vacancy)
    assert result.decision == 'SKIP'
    assert result.decision_class == 'REJECT'
    assert result.score == 0
    assert any('Non-technical CRM' in r for r in result.reasons)


@pytest.mark.parametrize('title', [
    'CRM Administrator', 'CRM Support Engineer', 'CRM Integration Developer',
    'Bitrix24 Administrator', 'Администратор Битрикс24',
    'Менеджер по внедрению CRM', 'CRM Implementation Manager',
])
def test_technical_crm_roles_remain_eligible(title):
    vacancy = _vacancy(title)
    assert classify_role_family(title, vacancy.description) == RoleFamily.APPLICATION_SUPPORT
    result = JobMatcher(_profile()).match(vacancy)
    assert not any('Non-technical CRM' in r for r in result.reasons)
    assert result.decision in ('APPLY', 'REVIEW')


def test_digest_excludes_marketing_and_keeps_technical_crm(monkeypatch, tmp_path):
    from ai_assistant import cli, config

    test_db = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_FILE", test_db)
    db.init_db()
    db.save_vacancy(_vacancy('Senior CRM Manager', 'marketing'))
    db.save_vacancy(_vacancy('CRM Support Engineer', 'support'))
    monkeypatch.setattr(cli, 'load_candidate_profile', lambda *args: _profile())
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.export_digest_cmd(format_type='json', output_json=True, min_score=0) == 0
    rows = json.loads(output.getvalue())['new_vacancies_data']
    assert [r['id'] for r in rows] == ['weworkremotely:support']
    conn = db.get_connection()
    try:
        assert conn.execute('SELECT count(*) FROM telegram_delivery_records').fetchone()[0] == 0
    finally:
        conn.close()
