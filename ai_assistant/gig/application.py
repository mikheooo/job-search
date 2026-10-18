"""P7 admission and net economics; reuse P6 ranking without changing its score."""
from datetime import datetime, timezone
import math
import re
from .currency import to_usd
from .proposal import find_forbidden_claims


def economics(gross_bid, currency, estimated_hours, *, exchange_rate=None, hourly=False):
    values = (gross_bid, estimated_hours)
    if any(v is None or isinstance(v, bool) or not math.isfinite(float(v)) or float(v) <= 0 for v in values):
        raise ValueError('bid and estimated hours must be positive finite numbers')
    gross_usd=to_usd(float(gross_bid),currency,exchange_rate)
    fee=None if gross_usd is None else (gross_usd*.1 if hourly else max(gross_usd*.1,5.0))
    net=None if fee is None else gross_usd-fee
    rate=None if net is None else net if hourly else net/float(estimated_hours)
    return {'gross_bid':float(gross_bid),'currency':currency,'gross_usd':gross_usd,
            'platform_fee_usd':None if fee is None else round(fee,2),
            'net_revenue_usd':None if net is None else round(net,2),
            'estimated_hours':float(estimated_hours),
            'net_effective_rate_usd':None if rate is None else round(rate,2),
            'economics_pass':rate is not None and rate>=10,
            'hourly':hourly,'strategic_first_job_exception':None}


def admission_reasons(entry, *, now=None):
    reasons=[]; cap=entry.get('capability',{}).get('fit','').lower()
    if entry.get('project_status')!='OPEN': reasons.append('project not OPEN')
    if not entry.get('live_verified'): reasons.append('not live verified')
    try:
        checked=datetime.fromisoformat(entry['verified_at'].replace('Z','+00:00'))
        if not 0 <= ((now or datetime.now(timezone.utc))-checked).total_seconds() <= 3600:
            reasons.append('verification stale or future')
    except (ValueError,TypeError,KeyError): reasons.append('invalid verification time')
    if cap not in {'proven','strong'}: reasons.append('capability not PROVEN/STRONG')
    if entry.get('client_risk',{}).get('level','').upper()=='HIGH': reasons.append('HIGH client risk')
    if entry.get('bids_live') is None: reasons.append('live bids unknown')
    if not entry.get('project_url','').startswith('https://'): reasons.append('project URL missing')
    if not entry.get('grounded_job_analysis',{}).get('REQUIRED_WORK'): reasons.append('grounded work missing')
    pack=entry.get('proposal_pack') or {}; text=pack.get('PROPOSAL',''); bid=pack.get('BID') or {}
    if not 100<=len(text.split())<=180 or len(pack.get('referenced_requirements',[]))<2:
        reasons.append('final project-specific proposal missing')
    if find_forbidden_claims(text) or pack.get('unsupported_tech') or re.search(r'\[[^\]]+\]|\{[^}]+\}|<[^>]+>',text):
        reasons.append('proposal has unsafe claims or placeholders')
    if not bid.get('recommended_bid') or not bid.get('currency'): reasons.append('bid missing')
    if not bid.get('delivery_time'): reasons.append('delivery missing')
    if not entry.get('economics',{}).get('economics_pass'): reasons.append('net economics under $10/hour or unknown')
    return reasons


def sendable_batch(entries, *, now=None):
    """Keep supplied manual submission order, cap at five and deduplicate identities."""
    result=[]; seen=set()
    for entry in entries:
        ident=entry.get('project_id')
        if not ident or ident in seen or admission_reasons(entry,now=now): continue
        result.append(entry); seen.add(ident)
        if len(result)==5: break
    return result
