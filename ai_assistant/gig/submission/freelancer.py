"""Freelancer official SDK-compatible bid transport, never browser/cookie based.

The SDK documents POST projects/0.1/bids and GET bids with projects[]/bids[].
Currency is implicit in the project. Only milestone_percentage is a bid field;
milestone payments are deliberately outside this adapter.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

BASE = 'https://www.freelancer.com/api/'


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def numeric_id(value):
    value = str(value).removeprefix('fl_')
    if not value.isdigit() or int(value) <= 0:
        raise ValueError('Invalid project identity')
    return int(value)


def redact(value, secrets=()):
    """Do not persist arbitrary provider responses; this is a second defense."""
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.search(r'token|cookie|secret|authorization|password', k, re.I)
                else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x, secrets) for x in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
        value = re.sub(r'(?i)(Bearer\s+)[^\s,;]+', r'\1[REDACTED]', value)
    return value


class ProviderError(RuntimeError):
    pass


class OfficialAPI:
    """No redirects, automatic POST retries, cookie jar, or configurable host."""
    def __init__(self, token=None, session=None, *, access_policy=None, tracker=None):
        self._token = token if token is not None else os.getenv('FLN_OAUTH_TOKEN', '')
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._session.cookies.clear()
        self.provider_rate_limit_remaining = None
        self.access_policy = access_policy or {}
        self.tracker = tracker
        self._approved_payload_hash = None

    def set_access_context(self, access_policy, tracker):
        self.access_policy, self.tracker = access_policy or {}, tracker

    def authorize_submission(self, payload, *, approval_hash, mode):
        """Issue a single-use in-process permit only after engine gates pass."""
        expected = digest(payload)
        if mode == 'APPROVAL_REQUIRED' and approval_hash != expected:
            raise ProviderError('EXACT_PAYLOAD_APPROVAL_REQUIRED')
        if mode == 'AUTO_APPLY' and self.access_policy.get('automated_bidding_allowed') is not True:
            raise ProviderError('AUTOMATION_PERMISSION_UNKNOWN')
        self._approved_payload_hash = expected

    @property
    def authenticated_configured(self):
        return bool(self._token)

    def request(self, method, endpoint, *, params=None, payload=None, authenticated=True):
        allowed = (endpoint == 'projects/0.1/bids/' or endpoint == 'users/0.1/self/'
                   or re.fullmatch(r'projects/0\.1/projects/\d+/', endpoint))
        if not allowed or method not in {'GET', 'POST'} or (method == 'POST' and endpoint != 'projects/0.1/bids/'):
            raise ProviderError('Unsupported official operation')
        from ..provider_policy import provider_access_reasons
        if method == 'POST':
            operation = 'api_bid_write'
            if not isinstance(payload, dict) or self._approved_payload_hash != digest(payload):
                raise ProviderError('ENGINE_SUBMISSION_PERMIT_REQUIRED')
            # One-use permit: a timeout or lost response can never authorize a retry.
            self._approved_payload_hash = None
        elif endpoint == 'projects/0.1/bids/':
            operation = 'api_bid_read'
        elif endpoint == 'users/0.1/self/':
            operation = 'api_account_read'
        else:
            operation = 'api_project_read'
        access_reasons = provider_access_reasons(self.access_policy, 'APPROVAL_REQUIRED',
            tracker=self.tracker, operation=operation)
        if access_reasons:
            raise ProviderError('ACCESS_POLICY_BLOCKED: ' + ','.join(access_reasons))
        if authenticated and not self._token:
            raise ProviderError('MISSING_OAUTH_ACCESS')
        headers = {'Accept': 'application/json', 'User-Agent': 'GigSearch official API adapter'}
        if authenticated:
            headers['Freelancer-OAuth-V1'] = self._token
        try:
            self._session.cookies.clear()
            response = self._session.request(method, BASE + endpoint, params=params,
                json=payload, headers=headers, timeout=(5, 15), allow_redirects=False)
            remaining = response.headers.get('RateLimit-Remaining')
            try:
                self.provider_rate_limit_remaining = int(remaining) if remaining is not None else None
            except (TypeError, ValueError):
                self.provider_rate_limit_remaining = None
            self._session.cookies.clear()
            if response.status_code == 429:
                raise ProviderError('PROVIDER_RATE_LIMIT; no automatic retry')
            if response.status_code != 200:
                raise ProviderError(f'PROVIDER_HTTP_{int(response.status_code)}; response body suppressed')
            data = response.json()
            if data.get('status') != 'success':
                raise ProviderError('PROVIDER_REJECTED; response body suppressed')
            return data
        except ProviderError:
            raise
        except Exception:
            # Never leak exception text, URLs, response bodies or request headers.
            raise ProviderError('PROVIDER_IO_ERROR; outcome may be unknown') from None

    def project(self, project_id):
        return self.request('GET', f'projects/0.1/projects/{numeric_id(project_id)}/',
            params={'full_description': 'true', 'job_details': 'true'},
            authenticated=False)['result']

    def self_user(self):
        return self.request('GET', 'users/0.1/self/', params={'jobs': 'true'})['result']

    def own_bids(self, project_id, bidder_id):
        """Paginate completely; no inferred bidders[] filter/private active endpoint."""
        own = []
        for offset in range(0, 10000, 100):
            result = self.request('GET', 'projects/0.1/bids/',
                params={'projects[]': numeric_id(project_id), 'bidders[]': bidder_id,
                        'limit': 100, 'offset': offset})['result']
            bids = result.get('bids')
            if not isinstance(bids, list):
                raise ProviderError('BID_LIST_SCHEMA_UNKNOWN')
            own.extend(b for b in bids if str(b.get('bidder_id')) == str(bidder_id))
            if len(bids) < 100:
                return own
        raise ProviderError('BID_LIST_INCOMPLETE; refusing unsafe submit')

    def place(self, payload):
        return self.request('POST', 'projects/0.1/bids/', payload=payload)


def bid_payload(project_id, amount, currency, delivery_days, proposal, milestones, bidder_id):
    if isinstance(amount, bool) or not math.isfinite(float(amount)) or float(amount) <= 0:
        raise ValueError('Invalid bid amount')
    if isinstance(delivery_days, bool) or not isinstance(delivery_days, int) or delivery_days <= 0:
        raise ValueError('Invalid delivery period')
    if not isinstance(proposal, str) or not proposal.strip() or not re.fullmatch('[A-Z]{3}', currency):
        raise ValueError('Invalid proposal or currency')
    if not isinstance(milestones, dict) or set(milestones) - {'milestone_percentage', 'plan'}:
        raise ValueError('Only SDK milestone_percentage is supported; plan stays local')
    percentage = milestones.get('milestone_percentage')
    if isinstance(percentage, bool) or not isinstance(percentage, (int, float)) or not 0 <= percentage <= 100:
        raise ValueError('Invalid milestone percentage')
    return {'project_id': numeric_id(project_id), 'bidder_id': bidder_id,
            'amount': float(amount), 'period': delivery_days, 'description': proposal,
            'milestone_percentage': percentage}


class FreelancerSubmissionAdapter:
    """Public interface routes every call through the gate and persistent journal."""
    def __init__(self, engine, prepared):
        self.engine, self.prepared = engine, prepared

    def submit_bid(self, project_id, amount, currency, delivery_days, proposal, milestones,
                   *, dry_run=True, approval_hash=None):
        pack = self.prepared['proposal_pack']
        expected = pack['BID']
        if (numeric_id(project_id) != numeric_id(self.prepared['project_id'])
                or amount != expected['recommended_bid'] or currency != expected['currency']
                or delivery_days != self.prepared['delivery_days'] or proposal != pack['PROPOSAL']
                or milestones != self.prepared['milestones']):
            return {'status': 'DO_NOT_SUBMIT', 'reasons': ['PACK_PAYLOAD_MISMATCH']}
        return self.engine.execute(self.prepared, dry_run=dry_run, approval_hash=approval_hash)
