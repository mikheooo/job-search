import json
from types import SimpleNamespace

from ai_assistant import config
from ai_assistant.hh_llm_reply import generate_contextual_draft, antigravity_complete
from ai_assistant.hh_message_reply import HHDialog, HHMessage


def _dialog():
    return HHDialog(conversation_id='test', vacancy_title='Systems Engineer', employer='Example', messages=[
        HHMessage(message_id='1', sender='candidate', text='У меня опыт виртуализации.'),
        HHMessage(message_id='2', sender='employer', text='Есть ли у вас опыт VMware NSX-T?'),
    ])


def test_llm_receives_profile_and_history_and_returns_draft(tmp_path, monkeypatch):
    (tmp_path / 'candidate_kb.json').write_text(json.dumps({'claims': {'a': {'status': 'CONFIRMED_BY_USER', 'value': 'Опыт Hyper-V'}, 'b': {'status': 'REJECTED_BY_USER', 'value': 'NEVER INCLUDE'}}}), encoding='utf-8')
    monkeypatch.setattr(config, 'PROJECT_ROOT', str(tmp_path))
    seen = {}
    def complete(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Знаком с виртуализацией и сетями, готов изучить NSX-T.'), finish_reason='stop')])
    result = generate_contextual_draft(_dialog(), complete)
    assert result['status'] == 'DRAFT'
    assert 'NSX-T' in result['reply']
    assert 'Опыт Hyper-V' in seen['messages'][0]['content']
    assert 'NEVER INCLUDE' not in seen['messages'][0]['content']
    assert seen['messages'][-1]['content'].endswith('NSX-T?')
    assert seen['messages'][-2]['role'] == 'assistant'


def test_model_error_and_truncated_response_never_fall_back(tmp_path, monkeypatch):
    (tmp_path / 'candidate_kb.json').write_text(json.dumps({'claims': {'a': {'status': 'CONFIRMED_BY_USER', 'value': 'Hyper-V'}}}), encoding='utf-8')
    monkeypatch.setattr(config, 'PROJECT_ROOT', str(tmp_path))
    def failed(**kwargs): raise RuntimeError('secret details')
    error = generate_contextual_draft(_dialog(), failed)
    assert error['status'] == 'NEEDS_REVIEW' and error['reply'] == ''
    assert 'secret details' not in error['reason']
    truncated = lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Да, у меня фундаментальный бэкграунд в'), finish_reason='length')])
    assert generate_contextual_draft(_dialog(), truncated)['reply'] == ''


def test_missing_kb_stays_for_review(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'PROJECT_ROOT', str(tmp_path))
    assert generate_contextual_draft(_dialog())['status'] == 'NEEDS_REVIEW'


def test_antigravity_uses_stdin_and_refuses_tool_attempts(tmp_path, monkeypatch):
    agent = tmp_path / '.agents' / 'agents' / 'hh-response-only.md'
    agent.parent.mkdir(parents=True)
    agent.write_text('agent', encoding='utf-8')
    monkeypatch.setattr(config, 'PROJECT_ROOT', str(tmp_path))
    observed = {}
    def run(args, **kwargs):
        observed.update(args=args, **kwargs)
        records = [
            {'event': 'init', 'init': {'agent': 'hh-response-only', 'permission_mode': 'strict'}},
            {'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'A complete model response.'}},
        ]
        return SimpleNamespace(returncode=0, stdout='\n'.join(json.dumps(r) for r in records))
    monkeypatch.setattr('ai_assistant.hh_llm_reply.subprocess.run', run)
    result = antigravity_complete(messages=[{'role': 'user', 'content': 'PRIVATE MESSAGE'}])
    assert result.choices[0].message.content == 'A complete model response.'
    assert 'PRIVATE MESSAGE' in observed['input']
    assert all('PRIVATE MESSAGE' not in arg for arg in observed['args'])
    assert 'read_file(*)' in observed['args'][observed['args'].index('--settings') + 1]


def test_antigravity_denies_unexpected_tool_attempt(tmp_path, monkeypatch):
    agent = tmp_path / '.agents' / 'agents' / 'hh-response-only.md'
    agent.parent.mkdir(parents=True)
    agent.write_text('agent', encoding='utf-8')
    monkeypatch.setattr(config, 'PROJECT_ROOT', str(tmp_path))
    events = [{'event': 'init', 'init': {'agent': 'hh-response-only', 'permission_mode': 'strict'}},
              {'event': 'step_update', 'step_update': {'step_type': 'tool_call'}}]
    monkeypatch.setattr('ai_assistant.hh_llm_reply.subprocess.run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='\n'.join(json.dumps(e) for e in events)))
    import pytest
    with pytest.raises(RuntimeError, match='tool attempt'):
        antigravity_complete(messages=[{'role': 'user', 'content': 'test'}])
