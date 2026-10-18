"""Fail-closed gates and SQLite outbox for restart-safe official submissions."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

from ..application import admission_reasons, economics
from ..grounded import analyze, unsupported_claims
from ..proposal import find_forbidden_claims
from ..money import competition_band, competition_score
from ..client import client_risk_penalty
from ..provider_policy import provider_access_reasons, retention_seconds
from .freelancer import bid_payload, digest, numeric_id, utcnow, ProviderError

ACCOUNT_FIELDS = ('authenticated', 'email_verified', 'profile_complete',
                  'required_skill_present', 'bid_quota_available',
                  'balance_requirement_met', 'account_restricted')


def recent(stamp, seconds, now=None):
    try:
        checked = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        return 0 <= ((now or datetime.now(timezone.utc)) - checked).total_seconds() <= seconds
    except (TypeError, ValueError, AttributeError):
        return False


def project_fingerprint(project):
    return digest({key: project.get(key) for key in
        ('id', 'title', 'description', 'type', 'jobs', 'owner_id')})


def budget_fingerprint(project):
    return digest({'budget': project.get('budget'), 'type': project.get('type'),
                   'currency': (project.get('currency') or {}).get('code')})


def pack_identity(entry):
    return digest({k: entry.get(k) for k in ('project_id', 'proposal_pack', 'delivery_days',
        'milestones', 'project_description_hash', 'project_fingerprint', 'budget_fingerprint',
        'capability', 'estimated_hours', 'milestone_semantics_verified')})


def journal_identity(payload, entry):
    return digest({'payload': payload, 'original_pack_hash': pack_identity(entry)})


@dataclass(frozen=True)
class Limits:
    max_submissions_per_hour: int = 2
    max_submissions_per_day: int = 8
    minimum_first_money_score: float = 48
    minimum_net_rate: float = 10
    maximum_client_risk: str = 'MEDIUM'
    controlled_batch: bool = False

    def __post_init__(self):
        for count in (self.max_submissions_per_hour, self.max_submissions_per_day):
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError('Submission limits must be positive integers')
        for value in (self.minimum_first_money_score, self.minimum_net_rate):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError('Invalid numeric threshold')
        if self.maximum_client_risk not in {'LOW', 'MEDIUM'}:
            raise ValueError('HIGH risk is never allowed')


def readiness(user, project, evidence=None, now=None):
    """Unavailable official fields stay UNKNOWN; verified account evidence expires.

    Evidence supplements fields absent from documented API projections, never
    substitutes identity/skills. Each field needs an official account observation
    reference, user id, timestamp, and an actual boolean value.
    """
    result = {key: 'UNKNOWN' for key in ACCOUNT_FIELDS}
    result['authenticated'] = bool(user and user.get('id'))
    jobs = user.get('jobs') if user else None
    required = project.get('jobs')
    if isinstance(jobs, list) and isinstance(required, list) and required:
        have = {str(j.get('id')) for j in jobs if isinstance(j, dict) and j.get('id')}
        need = {str(j.get('id')) for j in required if isinstance(j, dict) and j.get('id')}
        if len(need) == len(required):
            result['required_skill_present'] = need.issubset(have)
    evidence = evidence or {}
    valid = (user and str(evidence.get('bidder_id')) == str(user.get('id'))
             and recent(evidence.get('observed_at'), 300, now))
    if valid:
        for key in ACCOUNT_FIELDS[1:]:
            if key == 'required_skill_present':
                continue
            item = evidence.get(key) or {}
            if type(item.get('value')) is bool and str(item.get('source', '')).startswith('https://'):
                from urllib.parse import urlparse
                if urlparse(item['source']).hostname in {'www.freelancer.com', 'accounts.freelancer.com', 'developers.freelancer.com'}:
                    result[key] = item['value']
    blocked = [k for k in ACCOUNT_FIELDS if result[k] is not (False if k == 'account_restricted' else True)]
    return {'fields': result, 'submission_blocked': bool(blocked),
            'reasons': ['ACCOUNT_' + k.upper() + '_' + str(result[k]).upper() for k in blocked],
            'observed_at': utcnow()}


def policy_reasons(policy, bidder_id, *, mode, approval_hash, payload_hash, now=None):
    """OAuth plus §33 written permission is required in both modes."""
    reasons = []
    if mode not in {'AUTO_APPLY', 'APPROVAL_REQUIRED'}:
        return ['INVALID_MODE']
    if (policy.get('automated_access_allowed') is not True
            or policy.get('official_bid_allowed') is not True or not policy.get('permission_reference')
            or not policy.get('express_written_permission_reference')):
        reasons.append('AUTOMATIC_SUBMISSION_BLOCKED_BY_PROVIDER: EXPRESS_WRITTEN_PERMISSION_REQUIRED')
    if policy.get('api_data_storage_compliant') is not True or not policy.get('storage_evidence_reference'):
        reasons.append('API_DATA_STORAGE_COMPLIANCE_UNVERIFIED')
    if (policy.get('api_data_retention_permitted') is not True
            or not policy.get('retention_permission_reference')
            or retention_seconds(policy) is None):
        reasons.append('API_DATA_RETENTION_PERMISSION_UNVERIFIED')
    if str(policy.get('bidder_id')) != str(bidder_id) or bidder_id is None:
        reasons.append('POLICY_ACCOUNT_BINDING_UNKNOWN')
    try:
        expires = datetime.fromisoformat(policy['expires_at'].replace('Z', '+00:00'))
        if expires <= (now or datetime.now(timezone.utc)):
            reasons.append('POLICY_EVIDENCE_EXPIRED')
    except (KeyError, ValueError, TypeError):
        reasons.append('POLICY_EVIDENCE_EXPIRY_UNKNOWN')
    if not {'basic', 'fln:project_manage', 'fln:user_information'}.issubset(set(policy.get('scopes') or [])):
        reasons.append('OAUTH_SCOPE_PERMISSION_UNKNOWN')
    if mode == 'AUTO_APPLY' and policy.get('automated_bidding_allowed') is not True:
        reasons.append('AUTOMATION_PERMISSION_UNKNOWN')
    if mode == 'APPROVAL_REQUIRED' and approval_hash != payload_hash:
        reasons.append('APPROVAL_REQUIRED')
    return reasons


class SubmissionEngine:
    def __init__(self, tracker, api, *, limits=None, policy=None, account_evidence=None,
                 mode='APPROVAL_REQUIRED'):
        self.tracker, self.api = tracker, api
        self.limits, self.policy = limits or Limits(), policy or {}
        self.account_evidence, self.mode = account_evidence or {}, mode
        if hasattr(api, 'set_access_context'):
            api.set_access_context(self.policy, tracker)
        with tracker._connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS api_bid_outbox (
                project_id TEXT PRIMARY KEY, bidder_id TEXT NOT NULL,
                state TEXT NOT NULL, payload_hash TEXT NOT NULL, reserved_at TEXT NOT NULL,
                provider_bid_id TEXT, confirmed_at TEXT,
                bids_at_submission INTEGER, provider_response_id TEXT,
                expires_at TEXT)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS api_monitor_events (
                project_id TEXT, kind TEXT, evidence_hash TEXT, observed_at TEXT,
                expires_at TEXT, UNIQUE(project_id,kind,evidence_hash))''')
            outbox_columns = {r[1] for r in conn.execute('PRAGMA table_info(api_bid_outbox)')}
            if 'expires_at' not in outbox_columns:
                conn.execute('ALTER TABLE api_bid_outbox ADD COLUMN expires_at TEXT')
            monitor_columns = {r[1] for r in conn.execute('PRAGMA table_info(api_monitor_events)')}
            if 'expires_at' not in monitor_columns:
                conn.execute('ALTER TABLE api_monitor_events ADD COLUMN expires_at TEXT')
        if hasattr(tracker, 'purge_expired'):
            tracker.purge_expired()

    def _expires_at(self):
        if hasattr(self.tracker, 'retention_expires_at'):
            return self.tracker.retention_expires_at()
        seconds = getattr(self.tracker, 'retention_seconds', None)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            return None
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()

    def _journal(self, project_id):
        with self.tracker._connect() as conn:
            row = conn.execute('SELECT * FROM api_bid_outbox WHERE project_id=?', (project_id,)).fetchone()
        return dict(row) if row else None

    def _reserve(self, ident, bidder_id, payload_hash, bids_at_submission):
        """BEGIN IMMEDIATE serializes dedup, rate checks and reservation across processes."""
        with self.tracker._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM api_bid_outbox WHERE project_id=?', (ident,)).fetchone():
                return 'SKIP_DUPLICATE'
            previous = conn.execute('SELECT submitted_at,status FROM submissions WHERE project_id=? AND source=?',
                                    (ident, 'freelancer')).fetchone()
            if not previous or previous['submitted_at'] or previous['status'] != 'READY':
                return 'TRACKER_NOT_READY_OR_DUPLICATE'
            for seconds, maximum in ((3600, self.limits.max_submissions_per_hour), (86400, self.limits.max_submissions_per_day)):
                # All attempted POSTs count, including unresolved outcomes/rejections.
                count = conn.execute("SELECT COUNT(*) FROM api_bid_outbox WHERE julianday(reserved_at) >= julianday('now')-?/86400.0", (seconds,)).fetchone()[0]
                if count >= maximum:
                    return 'LOCAL_RATE_LIMIT'
            confirmed = conn.execute("SELECT COUNT(*) FROM api_bid_outbox WHERE state='CONFIRMED'").fetchone()[0]
            attempts = conn.execute('SELECT COUNT(*) FROM api_bid_outbox').fetchone()[0]
            if not self.limits.controlled_batch and attempts:
                return 'FIRST_E2E_ONLY; controlled batch disabled'
            if self.limits.controlled_batch and not confirmed:
                return 'FIRST_E2E_NOT_CONFIRMED'
            conn.execute('''INSERT INTO api_bid_outbox
                (project_id,bidder_id,state,payload_hash,reserved_at,provider_bid_id,confirmed_at,
                 bids_at_submission,provider_response_id,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (ident, str(bidder_id), 'RESERVED', payload_hash, utcnow(), None, None,
                 bids_at_submission, None, self._expires_at()))
        return None

    @staticmethod
    def _matches(bid, payload):
        try:
            return (str(bid['project_id']) == str(payload['project_id'])
                and str(bid['bidder_id']) == str(payload['bidder_id'])
                and float(bid['amount']) == float(payload['amount'])
                and int(bid['period']) == payload['period']
                and float(bid['milestone_percentage']) == float(payload['milestone_percentage'])
                and bid['description'] == payload['description'])
        except (KeyError, TypeError, ValueError):
            return False

    def _confirm(self, entry, payload, bid, response_id=None):
        if not self._matches(bid, payload) or not bid.get('id'):
            return {'status': 'SUBMISSION_UNKNOWN', 'reasons': ['OWN_BID_NOT_MATCHED; never retry POST']}
        ident, stamp = entry['project_id'], utcnow()
        with self.tracker._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM api_bid_outbox WHERE project_id=?', (ident,)).fetchone()
            if not row or row['payload_hash'] != journal_identity(payload, entry):
                return {'status': 'SUBMISSION_UNKNOWN', 'reasons': ['JOURNAL_PAYLOAD_MISMATCH']}
            submitted = row['reserved_at']
            if isinstance(bid.get('time_submitted'), (int, float)):
                submitted = datetime.fromtimestamp(bid['time_submitted'], timezone.utc).isoformat()
            response_id = response_id or row['provider_response_id']
            expires_at = self._expires_at()
            conn.execute("UPDATE api_bid_outbox SET state='CONFIRMED', provider_bid_id=?, confirmed_at=COALESCE(confirmed_at,?), expires_at=? WHERE project_id=?",
                         (str(bid['id']), stamp, expires_at, ident))
            # Outbox and tracker commit together; retry can recover after any process crash.
            conn.execute("""UPDATE submissions SET status='SUBMITTED', submitted_at=COALESCE(submitted_at,?),
                provider_bid_id=?, actual_bid=?, actual_delivery=?, bids_at_submission=?,
                proposal_hash=?, project_description_hash=?, provider_response_id=?, updated_at=?, api_data_expires_at=?
                WHERE source='freelancer' AND project_id=?""",
                (submitted, str(bid['id']), float(bid['amount']), f"{bid['period']} days", row['bids_at_submission'],
                 digest(bid['description']), entry['project_description_hash'], response_id, stamp, expires_at, ident))
            conn.execute('INSERT OR IGNORE INTO funnel_events VALUES (?,?,?,?)', ('freelancer', ident, 'SUBMITTED', stamp))
        return {'status': 'SUBMITTED', 'submission_id': str(bid['id']), 'submitted_at': submitted,
                'actual_bid': float(bid['amount']), 'delivery_days': int(bid['period']),
                'provider_response_id': response_id}

    def execute(self, prepared, *, dry_run=True, approval_hash=None):
        try:
            return self._execute(prepared, dry_run=dry_run, approval_hash=approval_hash)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            return {'status': 'DO_NOT_SUBMIT', 'reasons': ['INVALID_OR_INCOMPLETE_PACK'], 'dry_run': dry_run}

    def _execute(self, prepared, *, dry_run=True, approval_hash=None):
        import copy
        entry = copy.deepcopy(prepared)
        ident = 'fl_' + str(numeric_id(entry['project_id']))
        entry['project_id'] = ident
        existing = self._journal(ident)
        if (existing and existing['state'] == 'CONFIRMED') or self.tracker.already_tracked(ident):
            return {'project_id': ident, 'mode': self.mode, 'dry_run': dry_run,
                    'status': 'SKIP_DUPLICATE', 'reasons': ['Already recorded; no API request or POST']}
        access_reasons = provider_access_reasons(self.policy, self.mode, tracker=self.tracker,
                                                operation='api_bid_write')
        if access_reasons and not getattr(self.api, 'offline_fixture', False):
            pack = entry['proposal_pack']; bid = pack['BID']
            payload = bid_payload(ident, bid['recommended_bid'], bid['currency'],
                entry['delivery_days'], pack['PROPOSAL'], entry['milestones'], None)
            return {'project_id': ident, 'mode': self.mode, 'dry_run': dry_run,
                    'status': 'DO_NOT_SUBMIT', 'payload': payload,
                    'payload_hash': digest(payload), 'payload_complete': False,
                    'reasons': access_reasons, 'provider_requests_made': 0}
        user = {}
        account_error = None
        if self.api.authenticated_configured:
            try:
                user = self.api.self_user()
            except ProviderError as exc:
                account_error = str(exc)
        bidder_id = user.get('id')
        pack = entry['proposal_pack']; bid = pack['BID']
        payload = bid_payload(ident, bid['recommended_bid'], bid['currency'], entry['delivery_days'],
                              pack['PROPOSAL'], entry['milestones'], bidder_id)
        payload_hash = digest(payload)
        output = {'project_id': ident, 'mode': self.mode, 'dry_run': dry_run,
                  'payload': payload, 'payload_hash': payload_hash,
                  'currency': bid['currency'], 'currency_inferred_from_project': True,
                  'limits': asdict(self.limits), 'submission_id': None}
        journal = self._journal(ident)
        if (journal and journal['state'] == 'CONFIRMED') or self.tracker.already_tracked(ident):
            return {**output, 'status': 'SKIP_DUPLICATE', 'reasons': ['Already recorded; no POST']}
        if journal:
            if dry_run:
                return {**output, 'status': 'SUBMISSION_UNKNOWN', 'reasons': ['Reserved outcome; no POST retry']}
            if journal['payload_hash'] != journal_identity(payload, entry) or str(bidder_id) != journal['bidder_id']:
                return {**output, 'status': 'SUBMISSION_UNKNOWN', 'reasons': ['Recovery identity/payload mismatch']}
            try:
                own = self.api.own_bids(ident, bidder_id)
                matching = [b for b in own if self._matches(b, payload)]
                if len(matching) == 1:
                    return {**output, **self._confirm(entry, payload, matching[0])}
            except ProviderError:
                pass
            return {**output, 'status': 'SUBMISSION_UNKNOWN', 'reasons': ['Manual reconciliation required; never repeat POST']}
        try:
            project = self.api.project(ident)
        except ProviderError as exc:
            return {**output, 'status': 'DO_NOT_SUBMIT', 'reasons': ['LIVE_VERIFY_FAILED: ' + str(exc)]}
        if str(project.get('id')) != str(numeric_id(ident)):
            return {**output, 'status': 'DO_NOT_SUBMIT', 'reasons': ['PROJECT_IDENTITY_MISMATCH']}
        if project_fingerprint(project) != entry.get('project_fingerprint') or digest(project.get('description')) != entry.get('project_description_hash'):
            return {**output, 'status': 'REGENERATE_REQUIRED', 'reasons': ['Scope/title/skills changed since generation']}
        if budget_fingerprint(project) != entry.get('budget_fingerprint'):
            return {**output, 'status': 'REGENERATE_REQUIRED', 'reasons': ['Budget/currency/type changed since generation']}
        entry['project_status'] = 'OPEN' if (project.get('status') == 'active' and project.get('frontend_project_status') == 'open'
            and not project.get('deleted') and not project.get('nonpublic')) else 'CLOSED_OR_UNKNOWN'
        entry['bids_live'] = (project.get('bid_stats') or {}).get('bid_count')
        entry['live_verified'], entry['verified_at'] = True, utcnow()
        rate = (project.get('currency') or {}).get('exchange_rate')
        entry['economics'] = economics(bid['recommended_bid'], bid['currency'], entry['estimated_hours'],
                                      exchange_rate=rate, hourly=project.get('type') == 'hourly')
        reasons = admission_reasons(entry)
        # Refresh the competition component from the just-read API bid count;
        # retain P7's calibrated components, capability cap and client multiplier.
        components = (entry.get('score') or {}).get('components') or {}
        weights = (entry.get('score') or {}).get('weights') or {}
        if set(components) != set(weights) or 'competition' not in components or not weights:
            reasons.append('FIRST_MONEY_SCORE_REFRESH_UNAVAILABLE')
        else:
            fresh_components = dict(components)
            fresh_components['competition'] = competition_score(competition_band(entry['bids_live']))
            base = sum(float(fresh_components[name]) * float(weights[name]) for name in weights) / sum(float(v) for v in weights.values())
            fresh_score = base * client_risk_penalty(entry['client_risk']['level'])
            fit = entry.get('capability', {}).get('fit', '').lower()
            if fit in {'weak', 'unknown'}:
                fresh_score = min(fresh_score, 39.0)
            elif fit == 'learnable':
                fresh_score = min(fresh_score, 55.0)
            entry['score'] = {**entry['score'], 'components': fresh_components,
                              'first_money_score': round(fresh_score, 1)}
        score_observed_at = utcnow()
        if pack_identity(prepared) != prepared.get('qa_pack_hash') or not prepared.get('qa_reference'):
            reasons.append('QA_REQUIRED_OR_PACK_CHANGED')
        if find_forbidden_claims(payload['description']) or unsupported_claims(analyze(project['description'], title=project.get('title', '')), payload['description']):
            reasons.append('UNSUPPORTED_CLAIMS')
        if any(ref not in project['description'] for ref in pack.get('referenced_requirements', [])):
            reasons.append('PROPOSAL_REFERENCES_NOT_GROUNDED')
        delivery_match = re.fullmatch(r'(\d+) days', bid.get('delivery_time', ''))
        if not delivery_match or payload['period'] != int(delivery_match[1]):
            reasons.append('DELIVERY_MISMATCH')
        if payload['amount'] != bid['recommended_bid'] or bid['currency'] != (project.get('currency') or {}).get('code'):
            reasons.append('PRICE_CURRENCY_MISMATCH')
        if entry.get('milestone_semantics_verified') is not True:
            reasons.append('MILESTONE_SEMANTICS_REQUIRES_REVIEW')
        elif project.get('type') == 'hourly' and payload['milestone_percentage'] != 0:
            reasons.append('HOURLY_INITIAL_MILESTONE_MUST_REMAIN_ZERO')
        elif project.get('type') != 'hourly':
            lines = bid.get('milestones') or []
            match = re.search(r'(?:USD|INR|AUD|EUR)\s*([\d,]+(?:\.\d+)?)', lines[0]) if lines else None
            if not match:
                reasons.append('FIXED_MILESTONE_PLAN_UNVERIFIABLE')
            else:
                initial_amount = float(match[1].replace(',', ''))
                expected_percentage = round(initial_amount / float(bid['recommended_bid']) * 100)
                if initial_amount <= 0 or expected_percentage != payload['milestone_percentage']:
                    reasons.append('FIXED_MILESTONE_PERCENTAGE_MISMATCH')
        if user and str(user.get('id')) == str(project.get('owner_id')):
            reasons.append('BIDDER_IS_PROJECT_OWNER')
        live_risk = entry.get('client_risk', {}).get('level', '').upper()
        if live_risk not in {'LOW', 'MEDIUM'} or (self.limits.maximum_client_risk == 'LOW' and live_risk != 'LOW'):
            reasons.append('RISK_LIMIT_OR_UNKNOWN')
        # Public client evidence must itself be current, not a fresh project API timestamp.
        if not recent(entry.get('client_risk_observed_at'), 3600):
            reasons.append('CLIENT_RISK_SNAPSHOT_STALE')
        score = entry.get('score', {}).get('first_money_score')
        if score is None or not math.isfinite(float(score)) or float(score) < self.limits.minimum_first_money_score:
            reasons.append('FIRST_MONEY_SCORE_LIMIT')
        net = entry['economics'].get('net_effective_rate_usd')
        if net is None or net < self.limits.minimum_net_rate:
            reasons.append('NET_RATE_LIMIT')
        if not isinstance(entry['bids_live'], int) or isinstance(entry['bids_live'], bool) or entry['bids_live'] < 0:
            reasons.append('LIVE_BID_COUNT_UNKNOWN')
        account = readiness(user, project, self.account_evidence)
        reasons.extend(account['reasons'])
        if account_error:
            reasons.append(account_error)
        reasons.extend(policy_reasons(self.policy, bidder_id, mode=self.mode,
                       approval_hash=approval_hash, payload_hash=payload_hash))
        output.update({'account_readiness': account, 'bids_at_submission': entry['bids_live'],
                       'first_money_score': score, 'score_refreshed_at': score_observed_at,
                       'economics': entry['economics'], 'verified_at': entry['verified_at'],
                       'payload_complete': bidder_id is not None})
        if dry_run:
            return {**output, 'status': 'DRY_RUN', 'submit_gate': 'DO_NOT_SUBMIT' if reasons else 'PASS', 'reasons': reasons}
        if reasons:
            return {**output, 'status': 'APPROVAL_REQUIRED' if reasons == ['APPROVAL_REQUIRED'] else 'DO_NOT_SUBMIT', 'reasons': reasons}
        try:
            own = self.api.own_bids(ident, bidder_id)
        except ProviderError as exc:
            return {**output, 'status': 'DO_NOT_SUBMIT', 'reasons': ['PRE_SUBMIT_DEDUP_READ_FAILED: ' + str(exc)]}
        if own:
            return {**output, 'status': 'SKIP_DUPLICATE', 'reasons': ['Existing provider bid; no POST']}
        if getattr(self.api, 'provider_rate_limit_remaining', None) == 0:
            return {**output, 'status': 'DO_NOT_SUBMIT', 'reasons': ['PROVIDER_RATE_LIMIT_EXHAUSTED']}
        reservation_error = self._reserve(ident, bidder_id, journal_identity(payload, entry), entry['bids_live'])
        if reservation_error:
            return {**output, 'status': 'DO_NOT_SUBMIT', 'reasons': [reservation_error]}
        try:
            if hasattr(self.api, 'authorize_submission'):
                self.api.authorize_submission(payload, approval_hash=approval_hash, mode=self.mode)
            response = self.api.place(payload)
            with self.tracker._connect() as conn:
                conn.execute('UPDATE api_bid_outbox SET provider_response_id=? WHERE project_id=?',
                             (response.get('request_id'), ident))
            result = response.get('result') or {}
            own = self.api.own_bids(ident, bidder_id)
            matching = [b for b in own if str(b.get('id')) == str(result.get('id')) and self._matches(b, payload)]
            if len(matching) == 1:
                return {**output, **self._confirm(entry, payload, matching[0], response.get('request_id'))}
        except ProviderError:
            pass
        return {**output, 'status': 'SUBMISSION_UNKNOWN', 'reasons': ['No confirmed own bid; reserved, never POST retry']}
