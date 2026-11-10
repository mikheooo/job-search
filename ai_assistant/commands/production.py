"""Production operations and autonomous agent subcommands."""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from ai_assistant.db import get_production_health, init_db

logger = logging.getLogger("commands.production")


def _metric_text(value: Any) -> str:
    """Format an optional integer metric for CLI display."""
    return "unknown (read failed)" if value is None else str(value)


def production_health_cmd(output_json: bool = False) -> int:
    """Inspect production state and evaluate overall operational health."""
    res = get_production_health()

    if output_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        status_color = res["health"]
        print(f"=== PRODUCTION PIPELINE HEALTH: {status_color} ===")
        print(f"Database Path: {res['db_path']} (Accessible: {res['db_accessible']})")
        print(f"Evaluation Timestamp: {res['evaluation_timestamp']}")
        print("-" * 60)
        m = res["metrics"]
        print(
            f"Delivery Records Total: {_metric_text(m['total_delivery_records'])} "
            f"(Batches: {_metric_text(m['digest_batch_records'])}, "
            f"Vacancies: {_metric_text(m['job_digest_records'])})"
        )
        print(
            f"Attempt States: Delivered={m['delivered_count']}, Attempting={m['attempting_count']}, "
            f"Stale={m['stale_count']}, Ambiguous={m['ambiguous_count']}, Failed={m['failed_count']}"
        )
        print(f"Duplicate Delivery Keys: {m['duplicate_delivery_keys_count']}")
        print(f"Consecutive Failures: {m['consecutive_failures']}")
        print(
            "Production Circuit: "
            f"{'OPEN' if m['production_circuit_open'] else 'CLOSED'} "
            f"(threshold: {m['production_circuit_threshold']})"
        )
        print(f"Last Attempt: {m['last_digest_attempt_at'] or 'Never'}")
        print(f"Last Successful Delivery: {m['last_successful_digest_at'] or 'Never'}")
        print(f"Pending Undigested Vacancies: {_metric_text(m['pending_undigested_vacancies_count'])}")
        print("-" * 60)
        if res["alerts"]:
            print(f"ACTIVE ALERTS ({len(res['alerts'])}):")
            for alt in res["alerts"]:
                print(f"  [{alt['severity']}] {alt['message']}")
        else:
            print("Active Alerts: None (All systems operational)")
        print("=" * 60)

    return 0 if res["health"] == "HEALTHY" else (1 if res["health"] == "DEGRADED" else 2)


def production_run_cmd(dry_run: bool = False, fetcher_script: str | None = None) -> int:
    """Run the canonical production wrapper and propagate its exact outcome."""
    from ai_assistant.runner import run_production_pipeline

    return run_production_pipeline(fetcher_script=fetcher_script, dry_run=dry_run)


def production_control_cmd(
    action: str = "status",
    output_json: bool = False,
    storage_dir: str | None = None,
) -> int:
    """Inspect or explicitly resume the persistent production circuit breaker."""
    from ai_assistant.runner import ConsecutiveFailureTracker

    tracker = ConsecutiveFailureTracker(storage_dir=storage_dir)
    if action == "resume":
        status = tracker.resume_after_operator_review()
    elif action == "status":
        status = tracker.get_status()
    else:
        print(f"Unknown production-control action: {action}", file=sys.stderr)
        return 3

    payload = {
        "action": action,
        "circuit_open": status["circuit_open"],
        "circuit_threshold": status["circuit_threshold"],
        "consecutive_failures": status["consecutive_failures"],
        "history_count": len(status["history"]),
        "last_failure_at": status["last_failure_at"],
        "last_failure_summary": status["last_failure_summary"],
    }
    if output_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        circuit_text = "OPEN (tripped)" if payload["circuit_open"] else "CLOSED (normal)"
        print(f"=== PRODUCTION CONTROL: {circuit_text} ===")
        print(f"Action: {action}")
        print(f"Consecutive Failures: {payload['consecutive_failures']}/{payload['circuit_threshold']}")
        print(f"Recorded Failures: {payload['history_count']}")
        print(f"Last Failure At: {payload['last_failure_at'] or 'None'}")
        if payload["last_failure_summary"]:
            print(f"Last Failure Summary: {payload['last_failure_summary']}")
    return 0


def autonomous_once_cmd(as_json: bool = False, limit: int = 5, mode: str = "SEARCH_ONLY") -> int:
    """Execute a single complete autonomous cycle."""
    from ai_assistant.hh_autonomous_agent import AutonomousConfig, run_autonomous_cycle

    cfg = AutonomousConfig(max_applications_per_cycle=limit, mode=mode)
    res = run_autonomous_cycle(config=cfg)

    if as_json:
        print(json.dumps(res.model_dump(), indent=2, ensure_ascii=False))
        return 0 if res.status in ("SUCCESS", "SEARCH_ONLY") else 1

    print("=" * 60)
    print("      STAGE 51 FULLY AUTONOMOUS JOB AGENT CYCLE       ")
    print("=" * 60)
    print(f"Status:             {res.status}")
    print(f"Started:            {res.started_at}")
    print(f"Completed:          {res.completed_at}")
    print("-" * 60)
    print(f"Discovered:         {res.discovered_count}")
    print(f"Matched:            {res.matched_count}")
    print(f"Applied:            {res.applied_count}")
    print(f"Verified:           {res.verified_count}")
    print(f"Messages checked:   {res.messages_checked}")
    print(f"Auto-replies sent:  {res.auto_replies_count}")
    print(f"Interviews found:   {res.interviews_detected}")
    print(f"Rejections:         {res.rejections_count}")
    print(f"Unanswered (held):  {res.unanswered_questions_count}")
    print("-" * 60)
    print(f"Summary:            {res.summary}")
    print("=" * 60)

    return 0 if res.status == "SUCCESS" else 1


def autonomous_start_cmd(interval: int = 60, mode: str = "SEARCH_ONLY") -> int:
    """Start continuous autonomous agent loop."""
    from ai_assistant.hh_autonomous_agent import AutonomousConfig, start_autonomous_daemon

    cfg = AutonomousConfig(poll_interval_seconds=interval, mode=mode)
    start_autonomous_daemon(config=cfg)
    return 0


def autonomous_status_cmd(as_json: bool = False) -> int:
    """Show autonomous agent status, recent cycles, and interview invites."""
    import ai_assistant.db as db

    init_db()
    cycles = db.list_autonomous_cycle_runs(limit=5)
    interviews = db.list_interview_events(limit=10)
    notifs = db.list_autonomous_notifications(limit=10)

    if as_json:
        out = {
            "recent_cycles": cycles,
            "interview_invites": interviews,
            "recent_notifications": notifs,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    print("=" * 60)
    print("           AUTONOMOUS AGENT STATUS DASHBOARD           ")
    print("=" * 60)
    print(f"Recent cycles: {len(cycles)}")
    for c in cycles:
        print(f"  [{c.get('started_at', '?')[:19]}] status={c.get('status')} discovered={c.get('discovered_count')} applied={c.get('applied_count')}")
    print("-" * 60)
    print(f"Interviews detected: {len(interviews)}")
    for inv in interviews:
        print(f"  [{inv.get('created_at', '?')[:19]}] company={inv.get('company_name')} type={inv.get('event_type')}")
    print("=" * 60)
    return 0
