"""Fail-closed Freelancer access and retention policy checks.

Policy values are evidence references supplied by the operator. They are not
proof by themselves; the gates only make the application require explicit,
current evidence before it sends automated requests or persists provider data.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


MAX_API_RETENTION_SECONDS = 24 * 60 * 60


def retention_seconds(policy):
    value = policy.get('api_data_retention_seconds')
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0 or value > MAX_API_RETENTION_SECONDS:
        return None
    return value


def _written_access_reasons(policy):
    reasons = []
    if (policy.get('automated_access_allowed') is not True
            or not policy.get('express_written_permission_reference')):
        reasons.append('AUTOMATED_ACCESS_EXPRESS_WRITTEN_PERMISSION_REQUIRED')
    return reasons


def website_access_reasons(policy):
    """Access gate for automated public-site collection, including scraping."""
    policy = policy or {}
    reasons = _written_access_reasons(policy)
    if policy.get('website_scraping_allowed') is not True:
        reasons.append('WEBSITE_AUTOMATION_PERMISSION_UNKNOWN')
    try:
        if datetime.fromisoformat(policy['expires_at'].replace('Z', '+00:00')) <= datetime.now(timezone.utc):
            reasons.append('POLICY_EVIDENCE_EXPIRED')
    except (KeyError, TypeError, ValueError, AttributeError):
        reasons.append('POLICY_EVIDENCE_EXPIRY_UNKNOWN')
    return reasons


def provider_access_reasons(policy, mode='APPROVAL_REQUIRED', now=None, *, tracker=None,
                            operation=None):
    """Gate API reads/writes, encrypted persistence, scopes, and evidence expiry."""
    policy = policy or {}
    reasons = _written_access_reasons(policy)
    required_permission = {
        'api_project_read': 'api_project_read_allowed',
        'api_account_read': 'api_account_read_allowed',
        'api_bid_read': 'api_bid_read_allowed',
        'api_bid_write': 'official_bid_allowed',
    }.get(operation)
    if required_permission and policy.get(required_permission) is not True:
        reasons.append(f'PROVIDER_PERMISSION_UNKNOWN:{required_permission}')
    elif operation is None and policy.get('official_bid_allowed') is not True:
        reasons.append('OFFICIAL_BID_PERMISSION_UNKNOWN')
    if policy.get('api_data_storage_compliant') is not True or not policy.get('storage_evidence_reference'):
        reasons.append('API_DATA_STORAGE_COMPLIANCE_UNVERIFIED')
    if (policy.get('api_data_retention_permitted') is not True
            or not policy.get('retention_permission_reference')
            or retention_seconds(policy) is None):
        reasons.append('API_DATA_RETENTION_PERMISSION_UNVERIFIED')
    else:
        stored_lifetime = getattr(tracker, 'retention_seconds', None)
        if (tracker is None or getattr(tracker, 'encrypted_at_rest', False) is not True
                or isinstance(stored_lifetime, bool) or not isinstance(stored_lifetime, (int, float))
                or not math.isfinite(stored_lifetime) or stored_lifetime <= 0
                or stored_lifetime > retention_seconds(policy)):
            reasons.append('ENCRYPTED_TRACKER_OR_RETENTION_ENFORCEMENT_REQUIRED')
    required_scopes = {'basic', 'fln:project_manage', 'fln:user_information'}
    if not required_scopes.issubset(set(policy.get('scopes') or [])):
        reasons.append('OAUTH_SCOPE_PERMISSION_UNKNOWN')
    try:
        expires = datetime.fromisoformat(policy['expires_at'].replace('Z', '+00:00'))
        if expires <= (now or datetime.now(timezone.utc)):
            reasons.append('POLICY_EVIDENCE_EXPIRED')
    except (KeyError, ValueError, TypeError, AttributeError):
        reasons.append('POLICY_EVIDENCE_EXPIRY_UNKNOWN')
    if mode == 'AUTO_APPLY' and policy.get('automated_bidding_allowed') is not True:
        reasons.append('AUTOMATION_PERMISSION_UNKNOWN')
    return reasons
