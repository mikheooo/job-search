import argparse
import copy
from datetime import datetime, timezone
import pytest
from ai_assistant.gig.application import economics,sendable_batch
from ai_assistant.gig.first_money import build_shortlist
from ai_assistant.gig.submission_cli import run_tracking_command
from ai_assistant.gig.tracker import SubmissionRecord,SubmissionTracker
from tests.gig.test_first_money import NOW,make_gig,verified,entry_for,confidences


def test_fixed_net_economics_and_minimum_fee_are_used():
    e=economics(180,'USD',8)
    assert (e['platform_fee_usd'],e['net_revenue_usd'],e['net_effective_rate_usd'])==(18,162,20.25)
    assert not economics(40,'USD',4)['economics_pass']  # 35 / 4, not gross 40 / 4
    assert economics(450,'EUR',24,exchange_rate=1.1257)['net_effective_rate_usd']==19
    assert economics(1250,'INR',12,exchange_rate=.0104,hourly=True)['net_effective_rate_usd']==11.7
    assert not economics(450,'EUR',24)['economics_pass']


@pytest.fixture
def complete(confidences):
    e=build_shortlist([entry_for(verified(make_gig()),confidences)],now=NOW).apply_first[0].to_dict()
    e['economics']=economics(180,'USD',8)
    return e


@pytest.mark.parametrize('field,value', [('project_status','CLOSED'),('live_verified',False),
    ('capability',{'fit':'learnable'}),('capability',{'fit':'weak'}),('capability',{'fit':'unknown'}),
    ('client_risk',{'level':'HIGH'}),('economics',economics(40,'USD',4))])
def test_disqualified_project_never_becomes_send_now(complete,field,value):
    complete[field]=value
    assert not sendable_batch([complete],now=NOW)


@pytest.mark.parametrize('field,value',[('PROPOSAL',''),('BID',{}),('BID',{'recommended_bid':180,'currency':'USD'})])
def test_missing_final_proposal_bid_or_delivery_blocks(complete,field,value):
    complete['proposal_pack'][field]=value
    assert not sendable_batch([complete],now=NOW)


def test_send_now_is_capped_at_five_and_deduplicated(complete):
    entries=[copy.deepcopy(complete) for _ in range(8)]
    for i,e in enumerate(entries): e['project_id']=str(i)
    assert len(sendable_batch([entries[0],entries[0],*entries],now=NOW))==5


def test_manual_cli_persists_actual_submission_and_received_payment(tmp_path,capsys,monkeypatch):
    from ai_assistant.gig.submission import secure_store
    monkeypatch.setattr(secure_store, '_crypt', lambda raw, decrypt=False: bytes(b ^ 0x5A for b in raw))
    from ai_assistant.gig.tracker import EncryptedSubmissionTracker
    path=tmp_path/'tracker.dpapi.sqlite'; tracker=EncryptedSubmissionTracker(path,retention_seconds=3600)
    tracker.upsert(SubmissionRecord(project_id='fl_1',title='Fixture',recommended_bid=180))
    ns=argparse.Namespace(tracker=str(path),source='freelancer',project_id='fl_1',gig_command='mark-submitted',
                          actual_bid=175,delivery_days=3,bids_at_submission=18)
    assert run_tracking_command(ns)==0
    row=tracker.get('fl_1')
    assert row.status=='SUBMITTED' and row.actual_bid==175 and row.actual_delivery=='3 days'
    assert row.bids_at_submission==18 and row.submitted_at
    ns.gig_command='mark-paid'; ns.revenue_usd=157.5; ns.payment_date='2026-10-05'
    assert run_tracking_command(ns)==0
    assert run_tracking_command(ns)==0  # cumulative actual amount, no double revenue
    row=tracker.get('fl_1')
    assert row.revenue_usd==157.5 and row.payment_date=='2026-10-05' and row.status=='SUBMITTED'
    assert tracker.metrics()['revenue_usd']==157.5 and tracker.metrics()['awards']==0


def test_unsubmitted_project_cannot_be_given_invented_payment(tmp_path):
    tracker=SubmissionTracker(tmp_path/'tracker.sqlite')
    tracker.upsert(SubmissionRecord(project_id='fl_1',title='Fixture'))
    assert not tracker.mark_paid('fl_1',revenue_usd=100,payment_date='2026-10-05')
    assert tracker.metrics()['revenue_usd']==0
