"""Read-only periodic polling of tracked bids. Never sends client replies."""
from threading import Event

from .freelancer import ProviderError, digest, utcnow


def monitor_once(tracker, api, policy=None):
    from .engine import policy_reasons, provider_access_reasons
    policy = policy or {}
    if hasattr(api, 'set_access_context'):
        api.set_access_context(policy, tracker)
    reasons = provider_access_reasons(policy, 'APPROVAL_REQUIRED', tracker=tracker,
                                      operation='api_bid_read')
    if reasons and not getattr(api, 'offline_fixture', False):
        return [{'status': 'MONITOR_BLOCKED', 'reasons': reasons, 'provider_requests_made': 0}]
    if not api.authenticated_configured:
        return [{'status': 'MONITOR_BLOCKED', 'reason': 'MISSING_OAUTH_ACCESS'}]
    try:
        bidder_id = api.self_user()['id']
    except (ProviderError, KeyError):
        return [{'status': 'MONITOR_BLOCKED', 'reason': 'AUTHENTICATION_UNVERIFIED'}]
    # Monitoring is also automated API access; a bidding approval is not required
    # for read-only polling, but provider permission/storage/scope evidence is.
    reasons = policy_reasons(policy, bidder_id, mode='APPROVAL_REQUIRED',
                             approval_hash='read-only', payload_hash='read-only')
    if reasons:
        return [{'status': 'MONITOR_BLOCKED', 'reasons': reasons}]
    updates = []
    for record in tracker.list(limit=10000):
        if not record.provider_bid_id or not record.submitted_at:
            continue
        try:
            bids = api.own_bids(record.project_id, bidder_id)
            bid = next((b for b in bids if str(b.get('id')) == record.provider_bid_id), None)
            project = api.project(record.project_id)
            event = {'bid_active': 'UNKNOWN', 'awarded_to_us': 'UNKNOWN',
                     'project_closed': False, 'client_reply': 'UNKNOWN'}
            if bid:
                # Status values are documented API enums, not inferred from project award.
                event['bid_active'] = bid.get('frontend_bid_status') == 'active' if bid.get('frontend_bid_status') else 'UNKNOWN'
                event['awarded_to_us'] = bid.get('award_status') == 'awarded' if bid.get('award_status') else 'UNKNOWN'
            event['project_closed'] = project.get('frontend_project_status') in {'closed', 'complete', 'completed', 'cancelled'}
            if event['awarded_to_us'] is True and record.status not in {'AWARDED', 'CLOSED'}:
                tracker.set_status(record.project_id, 'AWARDED', note='Official own-bid status observed')
            elif event['project_closed'] and record.status not in {'AWARDED', 'CLOSED'}:
                tracker.set_status(record.project_id, 'CLOSED', note='Official project closure observed')
            with tracker._connect() as conn:
                seconds = getattr(tracker, 'retention_seconds', None)
                from datetime import datetime, timedelta, timezone
                expiry = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat() \
                    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0 else None
                conn.execute('''INSERT OR IGNORE INTO api_monitor_events
                    (project_id,kind,evidence_hash,observed_at,expires_at) VALUES (?,?,?,?,?)''',
                    (record.project_id, 'BID_STATUS', digest(event), utcnow(), expiry))
            updates.append({'project_id': record.project_id, 'status': 'OBSERVED', **event})
        except ProviderError:
            updates.append({'project_id': record.project_id, 'status': 'MONITOR_UNKNOWN'})
    return updates


def monitor_periodically(engine, interval_seconds=300, stop=None, on_change=None):
    if interval_seconds < 60:
        raise ValueError('Minimum monitor interval is 60 seconds')
    stop = stop or Event()
    previous = None
    while not stop.is_set():
        result = monitor_once(engine.tracker, engine.api, engine.policy)
        current = digest(result)
        if current != previous and on_change:
            on_change(result)
        previous = current
        # The caller runs this foreground worker; no background production service is installed.
        stop.wait(interval_seconds)
