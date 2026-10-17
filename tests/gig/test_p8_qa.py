"""Independent P8 safety checks: isolated tracker, no provider network calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from ai_assistant.gig.submission.engine import SubmissionEngine, policy_reasons, readiness
from ai_assistant.gig.submission.freelancer import digest
from ai_assistant.gig.submission.monitor import monitor_once
from ai_assistant.gig.tracker import SubmissionRecord, SubmissionTracker


def test_reservation_is_atomic_across_two_process_equivalent_connections(tmp_path):
    path = tmp_path / 'race.sqlite'
    tracker = SubmissionTracker(path)
    tracker.upsert(SubmissionRecord(project_id='fl_1', title='Disposable bid'))
    first = SubmissionEngine(tracker, None)
    second = SubmissionEngine(SubmissionTracker(path), None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda engine: engine._reserve('fl_1', 7, digest({'bid': 1}), 19), [first, second]))
    assert results.count(None) == 1
    assert results.count('SKIP_DUPLICATE') == 1
    assert tracker.metrics()['proposals_submitted'] == 0


def test_approve_cannot_override_provider_written_permission(tmp_path):
    policy = {'official_bid_allowed': True, 'permission_reference': 'OAuth consent',
              'automated_access_allowed': True,
              'api_data_storage_compliant': True, 'storage_evidence_reference': 'DPAPI',
              'api_data_retention_permitted': True, 'retention_permission_reference': 'Written',
              'api_data_retention_seconds': 3600,
              'bidder_id': 7, 'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
              'scopes': ['basic', 'fln:project_manage', 'fln:user_information']}
    reasons = policy_reasons(policy, 7, mode='APPROVAL_REQUIRED', approval_hash='same', payload_hash='same')
    assert any('EXPRESS_WRITTEN_PERMISSION_REQUIRED' in reason for reason in reasons)
    assert 'APPROVAL_REQUIRED' not in reasons


def test_missing_account_fields_and_partial_skills_never_pass():
    result = readiness({'id': 7, 'jobs': [{'id': 1}]}, {'jobs': [{'id': 1}, {'id': 2}]})
    assert result['fields']['required_skill_present'] is False
    assert result['fields']['account_restricted'] == 'UNKNOWN'
    assert result['fields']['balance_requirement_met'] == 'UNKNOWN'
    assert result['submission_blocked'] is True


def test_monitor_uses_award_status_and_does_not_invent_client_reply(tmp_path):
    tracker = SubmissionTracker(tmp_path / 'award.sqlite')
    tracker.upsert(SubmissionRecord(project_id='fl_1', title='Disposable award'))
    tracker.mark_submitted('fl_1', actual_bid=100, actual_delivery='3 days', bids_at_submission=1)
    with tracker._connect() as conn:
        conn.execute("UPDATE submissions SET provider_bid_id='99' WHERE project_id='fl_1'")
    class API:
        offline_fixture = True
        authenticated_configured = True
        def self_user(self): return {'id': 7}
        def own_bids(self, project_id, bidder_id):
            return [{'id': 99, 'bidder_id': 7, 'project_id': 1,
                     'frontend_bid_status': 'in_progress', 'award_status': 'awarded'}]
        def project(self, project_id): return {'id': 1, 'frontend_project_status': 'awarded'}
    SubmissionEngine(tracker, API())
    from tests.gig.test_p8_submission import permission
    events = monitor_once(tracker, API(), permission())
    assert events[0]['awarded_to_us'] is True
    assert events[0]['client_reply'] == 'UNKNOWN'
    assert tracker.get('fl_1').status == 'AWARDED'


def test_uncertain_submission_recovery_cannot_relabel_original_description(tmp_path):
    from tests.gig.test_p8_submission import setup, call
    engine, api, entry, tracker = setup(tmp_path)
    api.place_fail = True
    assert call(engine, entry, dry_run=False)['status'] == 'SUBMISSION_UNKNOWN'
    original_hash = entry['project_description_hash']
    entry['project_description_hash'] = digest('An entirely different client brief')
    recovered = call(engine, entry, dry_run=False)
    row = tracker.get('fl_1')
    assert recovered['status'] == 'SUBMISSION_UNKNOWN' or row.project_description_hash == original_hash
    assert row.project_description_hash != entry['project_description_hash']
    assert len(api.sent) == 1
