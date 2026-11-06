"""Offline routing and safety for primary LLM and legacy fallback."""
from unittest.mock import patch
import json
import shutil
import subprocess

import pytest

from ai_assistant import db
from ai_assistant.hh_autonomous_agent import AutonomousConfig, AutonomousJobAgent, NotificationDispatcher
from ai_assistant.hh_autonomous_agent import _incoming_message_still_current as real_message_guard
from ai_assistant.hh_message_reply import _CONVERSATION_JS
from test_hh_autonomous_reply_optin import _evaluate_recorder, _recruiter_question_dialog, CONV_ID


def test_real_extractor_distinguishes_current_hh_message_classes():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed to execute the HH extraction JavaScript')
    harness = r'''
const makeRow = (classes, bubbleClasses, text) => ({
  className: classes, innerText: text,
  querySelector: selector => selector.includes('chat-bubble')
    ? {className: bubbleClasses, innerText: text} : null
});
global.location = {href:'https://hh.ru/chat/123', pathname:'/chat/123'};
global.document = {title:'chat', querySelector: () => null,
  querySelectorAll: () => [
    makeRow('message--abc message-my--def', 'magritte-chat-bubble___x magritte-outgoing___x', 'My reply'),
    makeRow('message--abc', 'magritte-chat-bubble___x magritte-bot___x', 'Employer question')
  ]};
const extract = EXTRACTOR;
console.log(extract());
'''.replace('EXTRACTOR', _CONVERSATION_JS)
    result = subprocess.run([node, '-e', harness], check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    assert [m['direction'] for m in data['messages']] == ['OUTGOING', 'INCOMING']


def test_new_question_with_same_card_prefix_is_loaded(monkeypatch):
    monkeypatch.delenv('HH_AUTONOMOUS_REPLY_ENABLED', raising=False)
    monkeypatch.setattr('ai_assistant.hh_autonomous_agent.time.sleep', lambda _: None)
    prefix = 'Could you please continue describing your experience with '
    earlier = prefix + 'the network? Please give details.'
    later = prefix + 'the network? Please give technical details.'
    monkeypatch.setattr(db, 'list_hh_message_events', lambda **kwargs: [
        {'processed': 1, 'status': 'MESSAGE_AUTO_REPLIED', 'text': earlier}])
    monkeypatch.setattr('ai_assistant.hh_message_reply.fetch_hh_conversation_readonly', lambda _: {
        'messages': [{'direction': 'INCOMING', 'text': later, 'timestamp': '15:46'}]})
    drafted = []
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft', lambda dialog:
                        (drafted.append(dialog.messages[-1].text) or
                         {'status': 'DRAFT', 'reply': 'I have relevant project experience.', 'reason': 'test'}))
    seen = []
    def evaluate(script):
        seen.append(script)
        return 'clicked'
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))
    agent._process_messages([{'conversation_id': CONV_ID, 'snippet': later}], _eval_fn=evaluate)
    assert drafted == [later]
    assert any('el.click()' in script for script in seen)
    assert not any('chat-input-textarea' in script for script in seen)


@pytest.fixture(autouse=True)
def current_message(monkeypatch):
    monkeypatch.setattr('ai_assistant.hh_autonomous_agent._incoming_message_still_current', lambda *args: True)


@pytest.mark.parametrize('messages,expected', [
    ([{'direction': 'INCOMING', 'text': 'Question?'}], True),
    ([{'direction': 'INCOMING', 'text': 'Another question?'}], False),
    ([{'direction': 'OUTGOING', 'text': 'Question?'}], False),
    ([], False),
])
def test_latest_message_guard(monkeypatch, messages, expected):
    monkeypatch.setattr('ai_assistant.hh_message_reply.fetch_hh_conversation_readonly', lambda evaluate: {'messages': messages})
    assert real_message_guard(lambda _: None, 'Question?') is expected


def test_ok_without_message_does_not_pass_current_message_guard(monkeypatch):
    monkeypatch.setattr('ai_assistant.hh_message_reply.fetch_hh_conversation_readonly', lambda evaluate: {'ok': True})
    assert real_message_guard(lambda _: None, 'Question?') is False


def test_interview_header_with_salary_questions_goes_to_model(monkeypatch):
    from ai_assistant.hh_message_reply import HHDialog, HHMessage
    monkeypatch.delenv('HH_AUTONOMOUS_REPLY_ENABLED', raising=False)
    called = []
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft', lambda dialog:
                        (called.append(dialog.messages[-1].text) or
                         {'status': 'DRAFT', 'reply': 'I can discuss the schedule and salary.', 'reason': 'test'}))
    dialog = HHDialog(conversation_id=CONV_ID, employer='Employer', messages=[
        HHMessage(message_id='m1', sender='employer', text='Анна\nСобеседование\n\nПодскажите, какой график вам подходит и какая вилка зарплаты комфортна?')])
    evaluate, _ = _evaluate_recorder(dialog)
    with patch.object(NotificationDispatcher, 'notify_interview') as interview:
        result = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))._process_messages()
    assert called and not interview.called and result['interviews_detected'] == 0


def test_changed_message_blocks_send(monkeypatch):
    monkeypatch.setenv('HH_AUTONOMOUS_REPLY_ENABLED', 'true')
    monkeypatch.setattr('ai_assistant.hh_autonomous_agent._incoming_message_still_current', lambda *args: False)
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft', lambda dialog: {'status': 'DRAFT', 'reply': 'A complete answer about Python.', 'reason': 'test'})
    evaluate, seen = _evaluate_recorder(_recruiter_question_dialog())
    with patch.object(NotificationDispatcher, 'notify_blocking_question', return_value={}):
        result = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))._process_messages()
    assert result['auto_replies_count'] == 0
    assert not any('chat-input-textarea' in script for script in seen)
    assert db.list_hh_message_events(conversation_id=CONV_ID)[0]['status'] == 'HUMAN_REVIEW_REQUIRED'


def test_existing_draft_is_not_overwritten(monkeypatch):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed to execute the send guard JavaScript')
    monkeypatch.setenv('HH_AUTONOMOUS_REPLY_ENABLED', 'true')
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft', lambda dialog:
                        {'status': 'DRAFT', 'reply': 'A complete answer about Python.', 'reason': 'test'})
    monkeypatch.setattr('ai_assistant.hh_autonomous_agent.time.sleep', lambda _: None)
    dialog = _recruiter_question_dialog()
    verdicts = []
    def evaluate(script):
        if 'chat-input-textarea' not in script:
            return json.dumps({'conversations': [dialog.model_dump()]})
        harness = '''const input = {value:'Existing user draft'};
global.location = {href:'https://hh.ru/chat/conv_optin_1'};
global.document = {querySelector: () => input};
const result = eval(JSON.parse(process.argv[1]));
console.log(JSON.stringify({result:JSON.parse(result),value:input.value}));'''
        result = subprocess.run([node, '-e', harness, json.dumps(script)], check=True, capture_output=True, text=True)
        verdict = json.loads(result.stdout)
        verdicts.append(verdict)
        return json.dumps(verdict['result'])
    with patch.object(NotificationDispatcher, 'notify_blocking_question', return_value={}):
        AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))._process_messages()
    assert verdicts == [{'result': {'ok': False, 'reason': 'Chat already has a draft'}, 'value': 'Existing user draft'}]
    assert db.list_hh_message_events(conversation_id=CONV_ID)[0]['status'] == 'HUMAN_REVIEW_REQUIRED'


def test_delivery_verification_tolerates_hh_paragraph_spacing(monkeypatch):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed to execute the delivery verification JavaScript')
    monkeypatch.setenv('HH_AUTONOMOUS_REPLY_ENABLED', 'true')
    monkeypatch.setattr('ai_assistant.hh_autonomous_agent.time.sleep', lambda _: None)
    answer = 'First paragraph.\n\nSecond paragraph.'
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft', lambda dialog:
                        {'status': 'DRAFT', 'reply': answer, 'reason': 'test'})
    dialog = _recruiter_question_dialog()
    def evaluate(script):
        if 'chat-input-textarea' in script:
            return json.dumps({'ok': True, 'conversation_id': CONV_ID})
        if 'const rows' in script:
            harness = '''const text={innerText:'First paragraph.\\n\\n\\n\\nSecond paragraph.'};
const bubble={className:'magritte-outgoing___x',querySelector:()=>text};
const row={className:'message-my--x',querySelector:()=>bubble};
global.location={href:'https://hh.ru/chat/conv_optin_1'};
global.document={querySelectorAll:()=>[row]};
console.log(eval(JSON.parse(process.argv[1])));'''
            return subprocess.run([node, '-e', harness, json.dumps(script)], check=True, capture_output=True, text=True).stdout
        return json.dumps({'conversations': [dialog.model_dump()]})
    with patch.object(NotificationDispatcher, 'notify_reply_sent', return_value={}):
        result = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))._process_messages()
    assert result['auto_replies_count'] == 1
    assert db.list_hh_message_events(conversation_id=CONV_ID)[0]['status'] == 'MESSAGE_AUTO_REPLIED'


def test_button_render_delay_retries_only_before_click(monkeypatch):
    monkeypatch.setenv('HH_AUTONOMOUS_REPLY_ENABLED', 'true')
    monkeypatch.setattr('ai_assistant.hh_autonomous_agent.time.sleep', lambda _: None)
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft', lambda dialog:
                        {'status': 'DRAFT', 'reply': 'A complete answer about Python.', 'reason': 'test'})
    dialog = _recruiter_question_dialog()
    calls = []
    def evaluate(script):
        if 'const targetConvId' in script:
            calls.append('initial')
            return json.dumps({'ok': False, 'reason': 'Chat send button not found for conversation ' + CONV_ID})
        if 'Draft changed before retry' in script:
            calls.append('retry')
            assert 'input.value !==' in script and "u.hostname" in script
            return json.dumps({'ok': True, 'conversation_id': CONV_ID, 'verified_in_hh': True})
        return json.dumps({'conversations': [dialog.model_dump()]})
    with patch.object(NotificationDispatcher, 'notify_reply_sent', return_value={}):
        result = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))._process_messages()
    assert result['auto_replies_count'] == 1 and calls == ['initial', 'retry']


def test_button_retry_stops_if_question_changes(monkeypatch):
    monkeypatch.setenv('HH_AUTONOMOUS_REPLY_ENABLED', 'true')
    monkeypatch.setattr('ai_assistant.hh_autonomous_agent.time.sleep', lambda _: None)
    checks = iter([True, False])
    monkeypatch.setattr('ai_assistant.hh_autonomous_agent._incoming_message_still_current', lambda *args: next(checks))
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft', lambda dialog:
                        {'status': 'DRAFT', 'reply': 'A complete answer about Python.', 'reason': 'test'})
    dialog = _recruiter_question_dialog()
    calls = []
    def evaluate(script):
        if 'const targetConvId' in script:
            calls.append('initial')
            return json.dumps({'ok': False, 'reason': 'Send button disabled'})
        if 'Draft changed before retry' in script:
            pytest.fail('must not retry after a changed incoming message')
        return json.dumps({'conversations': [dialog.model_dump()]})
    with patch.object(NotificationDispatcher, 'notify_blocking_question', return_value={}):
        result = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))._process_messages()
    assert result['auto_replies_count'] == 0 and calls == ['initial']
    assert db.list_hh_message_events(conversation_id=CONV_ID)[0]['status'] == 'HUMAN_REVIEW_REQUIRED'


def test_model_answer_has_priority_and_legacy_is_not_called(monkeypatch):
    monkeypatch.delenv('HH_AUTONOMOUS_REPLY_ENABLED', raising=False)
    called = []
    def draft(dialog):
        called.append(dialog.messages[-1].text)
        return {'status': 'DRAFT', 'reply': 'Работал с Python и FastAPI в собственных проектах.', 'reason': 'LLM draft'}
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft', draft)
    def forbidden(*args, **kwargs):
        pytest.fail('legacy generator must not run after successful LLM response')
    monkeypatch.setattr('ai_assistant.hh_message_reply.classify_hh_conversation_detailed', forbidden)
    dialog = _recruiter_question_dialog()
    evaluate, seen = _evaluate_recorder(dialog)
    with patch.object(NotificationDispatcher, 'notify_reply_sent') as sent:
        result = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))._process_messages()
    assert called and 'Python' in called[0]
    assert result['auto_replies_count'] == 0 and not sent.called
    assert not any('chat-input-textarea' in script for script in seen)
    event = db.list_hh_message_events(conversation_id=CONV_ID)[0]
    assert event['status'] == 'BLOCKED_AUTONOMOUS_REPLY_DISABLED'
    assert event['reply_draft'].endswith('проектах.')


@pytest.mark.parametrize('use_fallback', [False, True])
def test_confirmed_send_uses_selected_answer_once(monkeypatch, use_fallback):
    monkeypatch.setenv('HH_AUTONOMOUS_REPLY_ENABLED', 'true')
    selected = 'Я работал с Python и интеграциями API в своих проектах.'
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft', lambda dialog:
                        {'status': 'NEEDS_REVIEW' if use_fallback else 'DRAFT',
                         'reply': '' if use_fallback else selected, 'reason': 'test'})
    monkeypatch.setattr('ai_assistant.hh_message_reply.classify_hh_conversation_detailed',
                        lambda dialog, profile=None: {'classification': 'NEEDS_REPLY',
                                                       'prepared_reply': selected, 'reason': 'fallback'})
    dialog = _recruiter_question_dialog()
    evaluate, seen = _evaluate_recorder(dialog)
    with patch.object(NotificationDispatcher, 'notify_reply_sent', return_value={}):
        result = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))._process_messages()
    assert result['auto_replies_count'] == 1
    assert len([s for s in seen if 'chat-input-textarea' in s]) == 1
    event = db.list_hh_message_events(conversation_id=CONV_ID)[0]
    assert event['status'] == 'MESSAGE_AUTO_REPLIED'
    audit = db.list_conversation_audits(conversation_id=CONV_ID)[0]
    assert audit['sent_reply'] == selected
    assert ('Fallback generator' in audit['decision_reason']) == use_fallback


@pytest.mark.parametrize('model_result', [
    {'status': 'NEEDS_REVIEW', 'reply': '', 'reason': 'LLM timeout'},
    None,
])
def test_model_failure_uses_existing_generator(monkeypatch, model_result):
    monkeypatch.delenv('HH_AUTONOMOUS_REPLY_ENABLED', raising=False)
    def draft(dialog):
        if model_result is None:
            raise TimeoutError('simulated')
        return model_result
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft', draft)
    fallback = []
    def legacy(dialog, profile=None):
        fallback.append(dialog.conversation_id)
        return {'classification': 'NEEDS_REPLY', 'prepared_reply': 'I use Python for automation and API integration.', 'reason': 'legacy'}
    monkeypatch.setattr('ai_assistant.hh_message_reply.classify_hh_conversation_detailed', legacy)
    dialog = _recruiter_question_dialog()
    evaluate, seen = _evaluate_recorder(dialog)
    result = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))._process_messages()
    assert fallback == [CONV_ID] and result['auto_replies_count'] == 0
    assert not any('chat-input-textarea' in script for script in seen)
    assert db.list_hh_message_events(conversation_id=CONV_ID)[0]['reply_draft'].startswith('I use Python')
    assert 'Fallback generator' in db.list_conversation_audits(conversation_id=CONV_ID)[0]['decision_reason']


def test_fallback_truncated_reply_is_never_sent(monkeypatch):
    monkeypatch.setenv('HH_AUTONOMOUS_REPLY_ENABLED', 'true')
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft',
                        lambda dialog: {'status': 'NEEDS_REVIEW', 'reply': '', 'reason': 'LLM unavailable'})
    monkeypatch.setattr('ai_assistant.hh_message_reply.classify_hh_conversation_detailed',
                        lambda dialog, profile=None: {'classification': 'NEEDS_REPLY', 'prepared_reply': 'Да, у меня фундаментальный бэкграунд в'})
    dialog = _recruiter_question_dialog()
    evaluate, seen = _evaluate_recorder(dialog)
    with patch.object(NotificationDispatcher, 'notify_blocking_question', return_value={}):
        result = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))._process_messages()
    assert result['auto_replies_count'] == 0
    assert not any('chat-input-textarea' in script for script in seen)
    assert db.list_hh_message_events(conversation_id=CONV_ID)[0]['status'] == 'HUMAN_REVIEW_REQUIRED'


def test_reply_claim_is_durable_and_exclusive():
    db.init_db()
    args = ('fp1', 'conv1', 'employer', 'Question?', '2026-11-02T00:00:00Z', 'A complete answer.')
    assert db.claim_hh_message_reply(*args) is True
    assert db.claim_hh_message_reply(*args) is False
    event = db.get_hh_message_event('fp1')
    assert event['processed'] == 1 and event['status'] == 'REPLY_SENDING'
    assert db.is_hh_message_processed('fp1')


def test_disabled_draft_can_be_sent_after_enabling(monkeypatch):
    monkeypatch.delenv('HH_AUTONOMOUS_REPLY_ENABLED', raising=False)
    monkeypatch.setattr('ai_assistant.hh_autonomous_agent.time.sleep', lambda _: None)
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft', lambda dialog:
                        {'status': 'DRAFT', 'reply': 'I have relevant project experience.', 'reason': 'test'})
    evaluate, seen = _evaluate_recorder(_recruiter_question_dialog())
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))
    first = agent._process_messages()
    assert first['auto_replies_count'] == 0
    assert db.list_hh_message_events(conversation_id=CONV_ID)[0]['status'] == 'BLOCKED_AUTONOMOUS_REPLY_DISABLED'
    monkeypatch.setenv('HH_AUTONOMOUS_REPLY_ENABLED', 'true')
    with patch.object(NotificationDispatcher, 'notify_reply_sent', return_value={}):
        second = agent._process_messages()
        third = agent._process_messages()
    assert second['auto_replies_count'] == 1 and third['auto_replies_count'] == 0
    assert len([s for s in seen if 'chat-input-textarea' in s]) == 1
    assert db.list_hh_message_events(conversation_id=CONV_ID)[0]['status'] == 'MESSAGE_AUTO_REPLIED'


@pytest.mark.parametrize('outcome', ['unverified', 'verification_error', 'transport_error', 'wrong_id'])
def test_uncertain_send_is_ambiguous_and_never_retried(monkeypatch, outcome):
    monkeypatch.setenv('HH_AUTONOMOUS_REPLY_ENABLED', 'true')
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft',
                        lambda dialog: {'status': 'DRAFT', 'reply': 'A complete answer about Python.', 'reason': 'test'})
    monkeypatch.setattr('ai_assistant.hh_autonomous_agent.time.sleep', lambda seconds: None)
    dialog = _recruiter_question_dialog()
    seen = []
    def evaluate(script):
        seen.append(script)
        if 'chat-input-textarea' in script:
            if outcome == 'transport_error':
                raise TimeoutError('unknown whether click completed')
            if outcome == 'wrong_id':
                return json.dumps({'ok': True, 'conversation_id': 'other'})
            return json.dumps({'ok': True, 'conversation_id': CONV_ID})
        if 'const rows' in script:
            if outcome == 'verification_error':
                raise TimeoutError('verification failed')
            return json.dumps({'verified': False})
        return json.dumps({'conversations': [dialog.model_dump()]})
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))
    with patch.object(NotificationDispatcher, 'notify_reply_sent') as sent:
        first = agent._process_messages()
        second = agent._process_messages()
    assert first['auto_replies_count'] == second['auto_replies_count'] == 0
    assert not sent.called
    assert len([s for s in seen if 'chat-input-textarea' in s]) == 1
    event = db.list_hh_message_events(conversation_id=CONV_ID)[0]
    assert event['processed'] == 1 and event['status'] == 'REPLY_AMBIGUOUS'


def test_preclick_rejection_is_review_not_retry(monkeypatch):
    monkeypatch.setenv('HH_AUTONOMOUS_REPLY_ENABLED', 'true')
    monkeypatch.setattr('ai_assistant.hh_llm_reply.generate_contextual_draft',
                        lambda dialog: {'status': 'DRAFT', 'reply': 'A complete answer about Python.', 'reason': 'test'})
    dialog = _recruiter_question_dialog()
    scripts = []
    def evaluate(script):
        scripts.append(script)
        if 'chat-input-textarea' in script:
            assert "segments.includes(targetConvId)" in script
            assert "sendBtn.removeAttribute" not in script
            return json.dumps({'ok': False, 'reason': 'Open conversation does not match target'})
        return json.dumps({'conversations': [dialog.model_dump()]})
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate))
    with patch.object(NotificationDispatcher, 'notify_blocking_question', return_value={}):
        agent._process_messages()
        agent._process_messages()
    assert len([s for s in scripts if 'chat-input-textarea' in s]) == 1
    event = db.list_hh_message_events(conversation_id=CONV_ID)[0]
    assert event['processed'] == 1 and event['status'] == 'HUMAN_REVIEW_REQUIRED'
