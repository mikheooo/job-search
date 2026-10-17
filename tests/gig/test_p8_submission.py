"""P8 end-to-end gates and recovery using disposable DB and fake official API."""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from ai_assistant.gig.submission.engine import (SubmissionEngine, Limits, pack_identity,
    project_fingerprint, budget_fingerprint, ACCOUNT_FIELDS, policy_reasons)
from ai_assistant.gig.submission.freelancer import (FreelancerSubmissionAdapter, OfficialAPI,
    ProviderError, digest, redact)
from ai_assistant.gig.submission.monitor import monitor_once
from ai_assistant.gig.submission.secure_store import load, save
from ai_assistant.gig.tracker import EncryptedSubmissionTracker, SubmissionTracker, SubmissionRecord


class FakeAPI:
    offline_fixture = True
    authenticated_configured = True
    def __init__(self):
        self.current = {'id': 1, 'title': 'Python CSV report',
            'description': 'Use Python to clean CSV files. Deliver a reusable script and a CSV report.',
            'jobs': [{'id': 3, 'name': 'Python'}], 'budget': {'minimum': 100, 'maximum': 500},
            'currency': {'code': 'USD', 'exchange_rate': 1}, 'type': 'fixed',
            'owner_id': 99, 'status': 'active', 'frontend_project_status': 'open',
            'bid_stats': {'bid_count': 4}}
        self.sent = []; self.reads_fail = False; self.place_fail = False
    def self_user(self): return {'id': 7, 'jobs': [{'id': 3}]}
    def project(self, ident): return copy.deepcopy(self.current)
    def own_bids(self, ident, bidder_id):
        if self.reads_fail: raise ProviderError('UNKNOWN')
        return copy.deepcopy(self.sent)
    def place(self, payload):
        bid = {**payload, 'id': 101, 'time_submitted': datetime.now(timezone.utc).timestamp()}
        self.sent.append(bid)
        if self.place_fail: raise ProviderError('Lost response')
        return {'status': 'success', 'result': bid, 'request_id': 'req-1'}


def prepared(api):
    # Meaningful safe plan and quotes; word count fits existing P7 gates.
    text = ('Hello, I propose a Python script to clean your CSV files and produce the requested CSV report. '
            'I will first inspect a supplied sample, agree the columns and validation rules, and preserve the original data. '
            'The reusable script will report missing values and unexpected input instead of silently losing records. '
            'I will reconcile row counts and totals before delivery and include instructions for running the same workflow on future files. '
            'My proposed price is USD 200 with delivery in three days after receiving the files. '
            'Additional file formats or materially different requirements need a separate estimate. '
            'Which column identifies each record uniquely?')
    entry = {'project_id': 'fl_1', 'title': 'Python CSV report',
        'project_url': 'https://www.freelancer.com/projects/python/CSV-report',
        'proposal_pack': {'PROPOSAL': text, 'referenced_requirements': ['Use Python to clean CSV files.', 'Deliver a reusable script and a CSV report.'],
            'BID': {'recommended_bid': 200, 'currency': 'USD', 'delivery_time': '3 days',
                    'milestones': ['Day 1: USD 40', 'Day 2: USD 120', 'Day 3: USD 40']}},
        'grounded_job_analysis': {'REQUIRED_WORK': ['Clean CSV files']},
        'capability': {'fit': 'strong'}, 'client_risk': {'level': 'MEDIUM'},
        'client_risk_observed_at': datetime.now(timezone.utc).isoformat(),
        'estimated_hours': 8, 'delivery_days': 3,
        'milestones': {'milestone_percentage': 20, 'plan': []},
        'milestone_semantics_verified': True,
        'score': {'first_money_score': 75,
            'components': {'capability': 82, 'competition': 80, 'freshness': 90,
                'scope_clarity': 80, 'rate': 80, 'effort': 80,
                'quick_win': 80, 'scope_creep': 80, 'action_score': 80},
            'weights': {'capability': 24, 'competition': 18, 'freshness': 14,
                'scope_clarity': 12, 'rate': 12, 'effort': 8,
                'quick_win': 4, 'scope_creep': 2, 'action_score': 6}},
        'project_description_hash': digest(api.current['description']),
        'project_fingerprint': project_fingerprint(api.current),
        'budget_fingerprint': budget_fingerprint(api.current), 'qa_reference': 'Independent test fixture review'}
    entry['qa_pack_hash'] = pack_identity(entry)
    return entry


def permission():
    return {'official_bid_allowed': True, 'permission_reference': 'Verified API consent',
        'automated_access_allowed': True, 'api_project_read_allowed': True,
        'api_account_read_allowed': True, 'api_bid_read_allowed': True,
        'express_written_permission_reference': 'Provider written permission fixture, no live rights asserted',
        'api_data_storage_compliant': True, 'storage_evidence_reference': 'Disposable fixture',
        'api_data_retention_permitted': True, 'retention_permission_reference': 'Disposable retention permission fixture',
        'api_data_retention_seconds': 3600,
        'automated_bidding_allowed': True, 'bidder_id': 7,
        'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        'scopes': ['basic', 'fln:project_manage', 'fln:user_information']}


def setup(tmp_path, limits=None, mode='AUTO_APPLY'):
    api = FakeAPI(); entry = prepared(api)
    tracker = SubmissionTracker(tmp_path / 'disposable.sqlite')
    tracker.upsert(SubmissionRecord(project_id='fl_1', title=entry['title'], proposal_text=entry['proposal_pack']['PROPOSAL']))
    evidence = {'bidder_id': 7, 'observed_at': datetime.now(timezone.utc).isoformat()}
    for field in ACCOUNT_FIELDS[1:]:
        evidence[field] = {'value': field != 'account_restricted', 'source': 'https://www.freelancer.com/account'}
    engine = SubmissionEngine(tracker, api, policy=permission(), account_evidence=evidence, limits=limits, mode=mode)
    return engine, api, entry, tracker


def call(engine, entry, **kwargs):
    bid = entry['proposal_pack']['BID']
    return FreelancerSubmissionAdapter(engine, entry).submit_bid(entry['project_id'], bid['recommended_bid'],
        bid['currency'], entry['delivery_days'], entry['proposal_pack']['PROPOSAL'], entry['milestones'], **kwargs)


def test_dry_run_never_posts_or_tracks_even_all_gates_pass(tmp_path):
    engine, api, entry, tracker = setup(tmp_path)
    out = call(engine, entry)
    assert out['status'] == 'DRY_RUN'; assert out['submit_gate'] == 'PASS', out
    assert api.sent == [] and tracker.metrics()['proposals_submitted'] == 0
    assert engine._journal('fl_1') is None


def test_success_stores_bid_and_tracker_atomically_retry_restart_skips(tmp_path):
    engine, api, entry, tracker = setup(tmp_path)
    out = call(engine, entry, dry_run=False)
    assert out['status'] == 'SUBMITTED', out
    rec = tracker.get('fl_1')
    assert rec.provider_bid_id == '101' and rec.actual_bid == 200 and rec.actual_delivery == '3 days'
    assert rec.bids_at_submission == 4 and rec.proposal_hash == digest(entry['proposal_pack']['PROPOSAL'])
    assert rec.project_description_hash == digest(api.current['description'])
    assert rec.provider_response_id == 'req-1' and rec.status == 'SUBMITTED'
    restarted = SubmissionEngine(SubmissionTracker(tracker.path), api)
    assert call(restarted, entry, dry_run=False)['status'] == 'SKIP_DUPLICATE'
    assert len(api.sent) == 1 and tracker.metrics()['proposals_submitted'] == 1


@pytest.mark.parametrize('change,reason', [
    ('closed', 'project not OPEN'), ('high', 'HIGH client risk'), ('weak', 'capability not PROVEN/STRONG'),
    ('unknown', 'capability not PROVEN/STRONG'), ('learnable', 'capability not PROVEN/STRONG'),
    ('economics', 'net economics'), ('account', 'ACCOUNT_'), ('quota', 'BID_QUOTA'),
    ('risk_unknown', 'RISK_LIMIT'), ('scope', 'Scope'), ('budget', 'Budget/'),
    ('claims', 'unsafe claims'), ('placeholder', 'unsafe claims'), ('delivery', 'DELIVERY_MISMATCH'),
    ('qa', 'QA_REQUIRED'), ('bids_unknown', 'LIVE_BID_COUNT_UNKNOWN'), ('currency', 'Budget')])
def test_rejection_gates_never_post(tmp_path, change, reason):
    engine, api, entry, tracker = setup(tmp_path)
    if change == 'closed': api.current['frontend_project_status'] = 'closed'
    if change == 'high': entry['client_risk']['level'] = 'HIGH'
    if change in {'weak', 'unknown', 'learnable'}: entry['capability']['fit'] = change
    if change == 'economics': entry['estimated_hours'] = 1000
    if change == 'account': engine.account_evidence = {}
    if change == 'quota': engine.account_evidence['bid_quota_available']['value'] = False
    if change == 'risk_unknown': entry['client_risk']['level'] = 'UNKNOWN'
    if change == 'scope': api.current['description'] += ' New mandatory scope.'
    if change == 'budget': api.current['budget']['maximum'] = 600
    if change == 'claims': entry['proposal_pack']['PROPOSAL'] += ' I have 10 years of experience.'
    if change == 'placeholder': entry['proposal_pack']['PROPOSAL'] += ' [YOUR_NAME]'
    if change == 'delivery': entry['delivery_days'] = 4
    if change == 'qa': entry['qa_pack_hash'] = 'invalid'
    if change == 'bids_unknown': api.current['bid_stats'] = {}
    if change == 'currency': api.current['currency']['code'] = 'EUR'
    out = call(engine, entry, dry_run=False)
    assert out['status'] in {'DO_NOT_SUBMIT', 'REGENERATE_REQUIRED'}, out
    assert any(reason in r for r in out['reasons']), out
    assert api.sent == [] and tracker.metrics()['proposals_submitted'] == 0


def test_ambiguous_post_reconciles_without_second_post(tmp_path):
    engine, api, entry, tracker = setup(tmp_path)
    api.place_fail = True
    assert call(engine, entry, dry_run=False)['status'] == 'SUBMISSION_UNKNOWN'
    api.place_fail = False
    recovered = call(engine, entry, dry_run=False)
    assert recovered['status'] == 'SUBMITTED' and len(api.sent) == 1
    assert tracker.get('fl_1').bids_at_submission == 4


def test_unknown_without_own_bid_never_repeats_post(tmp_path):
    engine, api, entry, _ = setup(tmp_path)
    api.place_fail = True
    call(engine, entry, dry_run=False); api.sent.clear()
    assert call(engine, entry, dry_run=False)['status'] == 'SUBMISSION_UNKNOWN'
    assert api.sent == []


def test_provider_existing_bid_skipped_even_tracker_empty(tmp_path):
    engine, api, entry, _ = setup(tmp_path)
    api.sent = [{'id': 999, 'project_id': 1, 'bidder_id': 7}]
    assert call(engine, entry, dry_run=False)['status'] == 'SKIP_DUPLICATE'
    assert len(api.sent) == 1


def test_one_approve_bound_to_exact_payload(tmp_path):
    engine, api, entry, _ = setup(tmp_path, mode='APPROVAL_REQUIRED')
    dry = call(engine, entry)
    assert dry['reasons'] == ['APPROVAL_REQUIRED'], dry
    assert call(engine, entry, dry_run=False, approval_hash='wrong')['status'] == 'APPROVAL_REQUIRED'
    assert call(engine, entry, dry_run=False, approval_hash=dry['payload_hash'])['status'] == 'SUBMITTED'


@pytest.mark.parametrize('max_hour,max_day', [(1, 8), (2, 1)])
def test_limits_are_persistent_and_atomic(tmp_path, max_hour, max_day):
    engine, api, entry, tracker = setup(tmp_path, Limits(max_submissions_per_hour=max_hour,
        max_submissions_per_day=max_day, controlled_batch=True))
    with tracker._connect() as conn:
        conn.execute('INSERT INTO api_bid_outbox VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('fl_2', '7', 'CONFIRMED', 'hash', datetime.now(timezone.utc).isoformat(), '2', datetime.now(timezone.utc).isoformat(), 4, None, None))
    out = call(engine, entry, dry_run=False)
    assert 'LOCAL_RATE_LIMIT' in out['reasons'] and api.sent == []


def test_batch_requires_confirmed_first_e2e(tmp_path):
    engine, api, entry, _ = setup(tmp_path, Limits(controlled_batch=True))
    assert 'FIRST_E2E_NOT_CONFIRMED' in call(engine, entry, dry_run=False)['reasons']
    assert api.sent == []


def test_exceptions_redact_tokens_and_disallow_redirect_or_retry():
    secret = 'disposable-secret-value'
    class Session:
        trust_env = True
        class Cookies:
            def clear(self): pass
        cookies = Cookies()
        def request(self, *args, **kwargs):
            assert kwargs['allow_redirects'] is False
            assert kwargs['headers']['Freelancer-OAuth-V1'] == secret
            raise RuntimeError(secret)
    class EncryptedJournal:
        encrypted_at_rest = True
        retention_seconds = 3600
    api = OfficialAPI(token=secret, session=Session(), access_policy=permission(), tracker=EncryptedJournal())
    with pytest.raises(ProviderError) as err: api.self_user()
    assert secret not in str(err.value)
    assert secret not in str(redact({'token': secret, 'message': secret}, [secret]))


def test_readiness_unknown_and_dry_payload_incomplete_without_token(tmp_path):
    engine, api, entry, _ = setup(tmp_path)
    api.authenticated_configured = False
    out = call(engine, entry)
    assert out['payload']['bidder_id'] is None and out['payload_complete'] is False
    assert out['submit_gate'] == 'DO_NOT_SUBMIT' and api.sent == []


def test_missing_provider_permission_blocks_all_automated_api_access(tmp_path):
    class NoAccess:
        authenticated_configured = True
        def self_user(self): raise AssertionError('must not call API before permission')
        def project(self, _): raise AssertionError('must not call API before permission')
    engine, _, entry, tracker = setup(tmp_path)
    engine.api, engine.policy = NoAccess(), {}
    result = call(engine, entry, dry_run=False)
    assert result['status'] == 'DO_NOT_SUBMIT' and result['provider_requests_made'] == 0
    assert any('WRITTEN_PERMISSION_REQUIRED' in x for x in result['reasons'])
    assert tracker.metrics()['proposals_submitted'] == 0


@pytest.mark.parametrize('retention_permission,reference', [
    (False, 'Written permission excludes retention'),
    (True, ''),
])
def test_api_data_retention_permission_blocks_all_automated_api_access(
        tmp_path, retention_permission, reference):
    class NoAccess:
        authenticated_configured = True
        def self_user(self): raise AssertionError('must not call API before retention permission')
        def project(self, _): raise AssertionError('must not call API before retention permission')
    engine, _, entry, tracker = setup(tmp_path)
    engine.api = NoAccess()
    engine.policy['api_data_retention_permitted'] = retention_permission
    engine.policy['retention_permission_reference'] = reference
    result = call(engine, entry, dry_run=False)
    assert result['status'] == 'DO_NOT_SUBMIT' and result['provider_requests_made'] == 0
    assert 'API_DATA_RETENTION_PERMISSION_UNVERIFIED' in result['reasons']
    assert 'API_DATA_RETENTION_PERMISSION_UNVERIFIED' in policy_reasons(
        engine.policy, 7, mode='APPROVAL_REQUIRED', approval_hash='same', payload_hash='same')
    assert tracker.metrics()['proposals_submitted'] == 0


def test_confirmation_requires_milestone_match(tmp_path):
    engine, api, entry, _ = setup(tmp_path)
    original = api.own_bids
    def own(*args):
        bids = original(*args)
        for bid in bids: bid['milestone_percentage'] = 50
        return bids
    api.own_bids = own
    assert call(engine, entry, dry_run=False)['status'] == 'SUBMISSION_UNKNOWN'


def test_provider_rate_limit_header_prevents_post(tmp_path):
    engine, api, entry, _ = setup(tmp_path)
    api.provider_rate_limit_remaining = 0
    result = call(engine, entry, dry_run=False)
    assert result['status'] == 'DO_NOT_SUBMIT'
    assert result['reasons'] == ['PROVIDER_RATE_LIMIT_EXHAUSTED'] and api.sent == []


def test_fixed_bid_initial_milestone_must_match_first_planned_payment(tmp_path):
    engine, api, entry, _ = setup(tmp_path)
    entry['milestones']['milestone_percentage'] = 100
    entry['qa_pack_hash'] = pack_identity(entry)
    out = call(engine, entry, dry_run=False)
    assert 'FIXED_MILESTONE_PERCENTAGE_MISMATCH' in out['reasons']
    assert api.sent == []


def test_p8_artifact_storage_is_dpapi_encrypted(tmp_path):
    path = tmp_path / 'sealed.dpapi.json'
    secret_text = 'private proposal body'
    save(path, {'proposal': secret_text})
    stored = path.read_text(encoding='utf8')
    assert secret_text not in stored and 'DPAPI_CURRENT_USER_V1' in stored
    assert load(path) == {'proposal': secret_text}


def test_expired_retained_artifact_is_deleted_on_any_load(tmp_path, monkeypatch):
    from ai_assistant.gig.submission import secure_store
    monkeypatch.setattr(secure_store, '_crypt', lambda raw, decrypt=False: bytes(b ^ 0x5A for b in raw))
    path = tmp_path / 'expired.dpapi.json'
    secure_store.save(path, {'retention_expires_at': '2000-01-01T00:00:00+00:00',
                             'value': {'provider_bid_id': 'private'}})
    with pytest.raises(ValueError, match='retention window expired'):
        secure_store.load(path)
    assert not path.exists()


def test_unbounded_legacy_pack_cannot_be_used_for_submission(tmp_path, monkeypatch):
    from ai_assistant.gig.submission import secure_store
    monkeypatch.setattr(secure_store, '_crypt', lambda raw, decrypt=False: bytes(b ^ 0x5A for b in raw))
    path = tmp_path / 'legacy.dpapi.json'
    secure_store.save(path, {'project_id': 'fl_1'})
    with pytest.raises(ValueError, match='expiry is missing or invalid'):
        secure_store.load_retained(path)
    assert path.exists()


def test_encrypted_tracker_is_opaque_reopenable_and_purges_expired_rows(tmp_path, monkeypatch):
    from ai_assistant.gig.submission import secure_store
    monkeypatch.setattr(secure_store, '_crypt', lambda raw, decrypt=False: bytes(b ^ 0x5A for b in raw))
    path = tmp_path / 'private.dpapi.sqlite'
    tracker = EncryptedSubmissionTracker(path, retention_seconds=3600)
    tracker.upsert(SubmissionRecord(project_id='fl_987', title='private project title',
        proposal_text='private proposal text', provider_bid_id='provider-bid-42'))
    encrypted = path.read_bytes()
    assert b'private project title' not in encrypted
    assert b'private proposal text' not in encrypted
    reopened = EncryptedSubmissionTracker(path, retention_seconds=3600)
    assert reopened.get('fl_987').proposal_text == 'private proposal text'
    with reopened._connect() as conn:
        conn.execute("UPDATE submissions SET api_data_expires_at='2000-01-01T00:00:00+00:00' WHERE project_id='fl_987'")
    assert reopened.get('fl_987') is None


def test_encrypted_tracker_refuses_plaintext_db_without_overwriting(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    tracker = SubmissionTracker(path)
    tracker.upsert(SubmissionRecord(project_id='fl_1', title='preserve me'))
    original = path.read_bytes()
    with pytest.raises((ValueError, KeyError)):
        EncryptedSubmissionTracker(path, retention_seconds=3600)
    assert path.read_bytes() == original


def test_direct_official_api_transport_fails_before_network_without_policy():
    class Session:
        trust_env = True
        class Cookies:
            def clear(self): pass
        cookies = Cookies()
        def request(self, *args, **kwargs):
            raise AssertionError('network must not run before provider policy')
    api = OfficialAPI(token='disposable', session=Session())
    with pytest.raises(ProviderError, match='ACCESS_POLICY_BLOCKED'):
        api.self_user()


def test_direct_official_api_bid_write_requires_engine_one_shot_permit():
    class Session:
        trust_env = True
        class Cookies:
            def clear(self): pass
        cookies = Cookies()
        def request(self, *args, **kwargs):
            raise AssertionError('raw API write must not reach the network')
    class EncryptedJournal:
        encrypted_at_rest = True
        retention_seconds = 3600
    api = OfficialAPI(token='disposable', session=Session(), access_policy=permission(), tracker=EncryptedJournal())
    with pytest.raises(ProviderError, match='ENGINE_SUBMISSION_PERMIT_REQUIRED'):
        api.place({'project_id': 1, 'bidder_id': 7, 'amount': 200})


def test_direct_p6_project_read_requires_policy_before_network():
    from ai_assistant.gig.live_project import fetch_public_project
    class Source:
        def _robots_allows(self, url):
            raise AssertionError('network guard must run before robots lookup')
    with pytest.raises(PermissionError, match='PUBLIC_API_ACCESS_BLOCKED'):
        fetch_public_project({'source_id': 'fl_123'}, source=Source())


def test_monitor_permission_failure_makes_no_api_read(tmp_path):
    tracker = SubmissionTracker(tmp_path / 'monitor.sqlite')
    class NoAccess:
        authenticated_configured = True
        def self_user(self): raise AssertionError('permission gate must precede authenticated request')
    result = monitor_once(tracker, NoAccess(), {})
    assert result[0]['status'] == 'MONITOR_BLOCKED'
    assert result[0]['provider_requests_made'] == 0
