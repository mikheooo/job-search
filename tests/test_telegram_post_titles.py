"""Telegram resumes must never become vacancies, including old persisted rows."""
import contextlib
import io
import json

import pytest

from ai_assistant import db
from ai_assistant.adapters import telegram_channels as tg
from ai_assistant.schema import Vacancy


@pytest.mark.parametrize('text', [
    'Публикатор: user Обсуждение: @devops_jobs #резюме #удаленка #sysadmin',
    '#ищу #sre #devops Степан — SRE / DevOps Engineer',
    '#resume Remote Python developer',
    '#OpenToWork DevOps Engineer',
    'Системный администратор. Ищу удалённую работу.',
])
def test_explicit_candidate_posts_are_excluded(text):
    assert tg.extract_post_title(text) is None


@pytest.mark.parametrize('text', [
    'DevOps Engineer\nОтправьте резюме на jobs@example.com. Удалённо, Linux.',
    'Python developer\nИщем инженера, пришлите ваше CV. Remote.',
    '#вакансия\nSRE Engineer\nОтклик: резюме и контакты.',
])
def test_employer_requests_for_resumes_are_kept(text):
    assert not tg.is_candidate_post(text)
    assert tg.extract_post_title(text)


def test_markup_line_breaks_preserve_the_role_after_metadata():
    text = tg.clean_post_html(
        'Публикатор: user<br>Обсуждение: @devops_jobs<br>'
        '#вакансия #remote<br><b>Вакансия: DevOps Engineer</b><br>'
        'Linux, Docker, удалённо. Присылайте резюме.'
    )
    assert tg.extract_post_title(text) == 'DevOps Engineer'


def test_unrecoverable_metadata_does_not_invent_a_role():
    assert tg.extract_post_title('Публикатор: user Обсуждение: @channel') is None


def test_generated_contact_footer_is_never_a_role():
    text = 'Публикатор: user Обсуждение: @channel DevOps remote Linux\n\nКонтакты для отклика: @recruiter'
    assert tg.extract_post_title(text) is None
    vacancy = _vacancy('125', 'Публикатор: user', text)
    assert not tg.prepare_telegram_digest_title(vacancy)


def test_stored_contact_footer_title_is_rejected():
    vacancy = _vacancy('126', 'Контакты для отклика: @recruiter',
                       'Публикатор: user remote DevOps\nКонтакты для отклика: @recruiter')
    assert not tg.prepare_telegram_digest_title(vacancy)


def _vacancy(suffix, title, description):
    return Vacancy(source='tg_devops_jobs_feed', source_job_id=suffix,
                   title=title, company='Example', description=description,
                   location='Remote Worldwide', job_url=f'https://t.me/devops_jobs_feed/{suffix}')


@pytest.mark.parametrize('text', [
    'Публикатор: K Обсуждение: @channel #резюме #удаленка #sysadmin #devops',
    'Публикатор: /home/user Обсуждение: @channel #резюме #ищу #sre #devops',
])
def test_stored_candidate_rows_are_excluded_without_persistent_changes(text, monkeypatch, tmp_path):
    from ai_assistant import config
    from ai_assistant.cli import export_digest_cmd

    test_db = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_FILE", test_db)

    vacancy = _vacancy('123', 'Публикатор: user Обсуждение: @channel', text)
    db.init_db()
    db.save_vacancy(vacancy)
    # A candidate post must never reach scoring, even if old data gave it 70 points.
    from ai_assistant.matcher import JobMatcher
    def must_not_match(*args, **kwargs):
        pytest.fail('candidate post reached vacancy matching')
    monkeypatch.setattr(JobMatcher, 'match', must_not_match)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert export_digest_cmd(format_type='json', output_json=True) == 0
    assert json.loads(out.getvalue())['new_vacancies_data'] == []
    conn = db.get_connection()
    try:
        assert conn.execute('SELECT title FROM vacancies').fetchone()[0] == vacancy.title
        assert conn.execute('SELECT count(*) FROM telegram_delivery_records').fetchone()[0] == 0
    finally:
        conn.close()


def test_old_metadata_title_can_be_repaired_in_memory():
    vacancy = _vacancy('124', 'Публикатор: user',
                       'Публикатор: user\nОбсуждение: @channel\nDevOps Engineer\nRemote Linux')
    assert tg.prepare_telegram_digest_title(vacancy)
    assert vacancy.title == 'DevOps Engineer'


def test_fetch_preserves_role_and_rejects_resume(monkeypatch):
    page = '''<div data-post="devops_jobs_feed/1">
      <div class="js-message_text">Публикатор: user<br>#вакансия<br>
      <b>DevOps Engineer</b><br>Linux Docker, удалённо. Присылайте резюме.</div></div>
      <div data-post="devops_jobs_feed/2"><div class="js-message_text">
      Публикатор: another<br>#резюме #remote<br>DevOps Engineer Linux Docker</div></div>'''
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return page.encode('utf-8')
    monkeypatch.setattr(tg.urllib.request, 'urlopen', lambda *args, **kwargs: Response())
    rows = tg.TelegramChannelsAdapter(['devops_jobs_feed']).fetch_channel('devops_jobs_feed')
    assert len(rows) == 1
    assert rows[0].title == 'DevOps Engineer'
    assert rows[0].source_job_id == 'devops_jobs_feed/1'
