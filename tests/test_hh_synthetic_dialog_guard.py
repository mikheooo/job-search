"""Synthetic HH fixtures must not reach production message processing."""
import os

from ai_assistant import db
from ai_assistant.hh_autonomous_agent import AutonomousConfig, AutonomousJobAgent
from ai_assistant.hh_message_reply import HHDialog, HHMessage


def _dialog(conversation_id, text):
    return HHDialog(conversation_id=conversation_id, employer='Fixture', messages=[
        HHMessage(message_id='m1', sender='employer', text=text),
    ])


def test_synthetic_dialog_cannot_reach_db_or_notification_outside_isolated_tests(monkeypatch):
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    monkeypatch.delenv('JOB_SEARCH_TEST_NETWORK_BLOCKED', raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('synthetic dialog reached browser or database')
    monkeypatch.setattr(db, 'save_hh_message_event', forbidden)
    monkeypatch.setattr(db, 'save_autonomous_notification', forbidden)
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=forbidden))
    result = agent._process_messages([_dialog('conv_question_999', 'Question?')])
    assert result['messages_check_status'] == 'BLOCKED'
    assert result['messages_checked'] == 0
    assert result['auto_replies_count'] == 0


def test_real_numeric_dialog_is_still_processed_when_mixed_with_fixture(monkeypatch):
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    monkeypatch.delenv('JOB_SEARCH_TEST_NETWORK_BLOCKED', raising=False)
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=lambda _: '{}'))
    result = agent._process_messages([
        _dialog('conv_question_999', 'Question?'),
        _dialog('5759946459', 'К сожалению, отказ.'),
    ])
    assert result['messages_check_status'] == 'CHECKED'
    assert result['messages_checked'] == 1 and result['rejections_count'] == 1
    assert not db.list_hh_message_events(conversation_id='conv_question_999')
    assert db.list_hh_message_events(conversation_id='5759946459')[0]['status'] == 'REJECTED'


def test_test_markers_do_not_allow_fixtures_in_production_db(monkeypatch):
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'simulated_test')
    monkeypatch.setenv('JOB_SEARCH_TEST_NETWORK_BLOCKED', '1')
    monkeypatch.setattr(db.config, 'DB_FILE', os.path.join(db.config.PROJECT_ROOT, 'state.db'))
    def forbidden(*args, **kwargs):
        raise AssertionError('synthetic dialog reached browser or production database')
    monkeypatch.setattr(db, 'save_hh_message_event', forbidden)
    monkeypatch.setattr(db, 'save_autonomous_notification', forbidden)
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=forbidden))
    result = agent._process_messages([_dialog('conv_question_999', 'Question?')])
    assert result['messages_check_status'] == 'BLOCKED'
    assert result['messages_checked'] == 0
