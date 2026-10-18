"""Manual event recording only. None of these commands sends a proposal."""
import json
from .tracker import EncryptedSubmissionTracker

COMMANDS={'mark-replied':'CLIENT_REPLIED','mark-interview':'INTERVIEW',
          'mark-awarded':'AWARDED','mark-rejected':'REJECTED','mark-closed':'CLOSED'}


def register_tracking_commands(subparsers):
    for name in ['mark-submitted',*COMMANDS,'mark-paid','funnel']:
        parser=subparsers.add_parser(name,help='Record a real manual action locally; never send')
        parser.add_argument('--tracker',help='Optional separate DPAPI-encrypted tracker path')
        parser.add_argument('--retention-seconds',type=int,default=86400,
            help='Bounded local retention for platform-derived fields (maximum 86400)')
        if name=='funnel': continue
        parser.add_argument('project_id')
        parser.add_argument('--source',default='freelancer')
        if name=='mark-submitted':
            parser.add_argument('--actual-bid',type=float,required=True)
            parser.add_argument('--delivery-days',type=int,required=True)
            parser.add_argument('--bids-at-submission',type=int,required=True)
        elif name=='mark-paid':
            parser.add_argument('--revenue-usd',type=float,required=True,help='Total USD actually received, not budget/award')
            parser.add_argument('--payment-date',required=True,help='Actual payment ISO date or timestamp')
        else: parser.add_argument('--note')


def run_tracking_command(args):
    tracker=EncryptedSubmissionTracker(getattr(args,'tracker',None),
        retention_seconds=getattr(args,'retention_seconds',86400)); name=args.gig_command
    if name=='funnel':
        print(json.dumps(tracker.metrics(),ensure_ascii=False,indent=2)); return 0
    source=getattr(args,'source','freelancer'); record=tracker.get(args.project_id,source)
    if record is None:
        print('Project is not tracked; no event recorded'); return 1
    try:
        if name=='mark-submitted':
            import math
            if not math.isfinite(args.actual_bid) or args.actual_bid<=0 or args.delivery_days<=0 or args.bids_at_submission<0:
                raise ValueError('Actual bid/delivery must be positive; bid count must be nonnegative')
            if record.status!='READY' or record.submitted_at:
                raise ValueError('Already advanced; refusing to reset submission history')
            ok=tracker.mark_submitted(args.project_id,actual_bid=args.actual_bid,
                bids_at_submission=args.bids_at_submission,actual_delivery=f'{args.delivery_days} days',source=source)
        elif name=='mark-paid':
            ok=tracker.mark_paid(args.project_id,revenue_usd=args.revenue_usd,payment_date=args.payment_date,source=source)
        else:
            if name!='mark-closed' and not record.submitted_at:
                raise ValueError('Record an actual submission first')
            ok=tracker.set_status(args.project_id,COMMANDS[name],note=getattr(args,'note',None),source=source)
        if not ok:
            print('No event recorded; actual submission required'); return 1
    except ValueError as exc:
        print(str(exc)); return 2
    print(json.dumps(tracker.get(args.project_id,source).to_dict(),ensure_ascii=False,indent=2)); return 0
