"""Dry-run default. An exact payload hash is the one-approve API action."""
import argparse
import json
from pathlib import Path

from ..tracker import EncryptedSubmissionTracker
from .engine import SubmissionEngine, Limits
from .freelancer import OfficialAPI, FreelancerSubmissionAdapter
from .secure_store import load_retained, purge_expired_artifacts, save_retained


def main(argv=None):
    parser = argparse.ArgumentParser(description='Official Freelancer API only; dry-run by default')
    parser.add_argument('command', choices=['apply', 'monitor'])
    parser.add_argument('--pack', help='DPAPI-encrypted prepared pack')
    parser.add_argument('--policy', help='Non-secret verified permission/readiness JSON')
    parser.add_argument('--tracker')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--submit', action='store_true', help='Explicit live action; all gates still required')
    parser.add_argument('--approve', help='Approve the exact payload SHA256 from dry-run')
    parser.add_argument('--mode', choices=['APPROVAL_REQUIRED', 'AUTO_APPLY'], default='APPROVAL_REQUIRED')
    parser.add_argument('--output', help='Encrypted output path')
    parser.add_argument('--watch', action='store_true', help='Foreground read-only periodic monitor')
    parser.add_argument('--interval', type=int, default=300)
    args = parser.parse_args(argv)
    if args.submit and args.dry_run:
        parser.error('Choose either --submit or --dry-run')
    if args.output and not str(args.output).lower().endswith('.dpapi.json'):
        parser.error('--output must use the .dpapi.json suffix so retention sweeps can find it')
    managed_artifacts = Path(__file__).resolve().parents[3] / 'artifacts' / 'p8'
    purge_expired_artifacts(managed_artifacts)
    if args.output:
        purge_expired_artifacts(Path(args.output).parent)
    config = {}
    if args.policy:
        config = json.loads(Path(args.policy).read_text(encoding='utf8'))
    permission = config.get('permission') or {}
    tracker = EncryptedSubmissionTracker(args.tracker,
        retention_seconds=permission.get('api_data_retention_seconds'))
    api = OfficialAPI(access_policy=permission, tracker=tracker)
    engine = SubmissionEngine(tracker, api, policy=permission,
        account_evidence=config.get('account_evidence'), limits=Limits(**config.get('limits', {})), mode=args.mode)
    if args.command == 'monitor':
        from .monitor import monitor_once, monitor_periodically
        if args.watch:
            monitor_periodically(engine, args.interval, on_change=lambda x: print(json.dumps(x)))
            return 0
        result = monitor_once(tracker, engine.api, engine.policy)
    else:
        if not args.pack:
            parser.error('--pack required for apply')
        prepared = load_retained(args.pack)
        bid = prepared['proposal_pack']['BID']
        result = FreelancerSubmissionAdapter(engine, prepared).submit_bid(prepared['project_id'],
            bid['recommended_bid'], bid['currency'], prepared['delivery_days'],
            prepared['proposal_pack']['PROPOSAL'], prepared['milestones'],
            dry_run=not args.submit, approval_hash=args.approve)
    if args.output:
        expiry = tracker.retention_expires_at()
        if not expiry:
            raise ValueError('Cannot persist P8 output without an authorized bounded retention period')
        save_retained(args.output, result, expires_at=expiry)
    # Exact payload is displayed to its owner; auth headers/token/response bodies are absent.
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not args.submit or result.get('status') in {'SUBMITTED', 'SKIP_DUPLICATE'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
