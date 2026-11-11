"""Australian Visa-Sponsored Jobs Discovery & Outreach Cycle.

Usage:
    python scripts/run_australia_search_cycle.py [--limit 5] [--send-telegram] [--force-refresh]
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ai_assistant.australia.australia_outreach import (
    VETTED_AUSTRALIAN_SPONSOR_AGENCIES,
    build_application_dossier,
    format_australian_job_card,
    load_sent_alerts,
    send_australian_job_telegram,
)
from ai_assistant.australia.seek_discovery import discover_australian_sponsored_jobs

logger = logging.getLogger("run_australia_cycle")


def run_australian_outreach_cycle(
    limit: int = 3,
    send_telegram: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Execute one Australian discovery, tailoring, and outreach cycle."""
    logger.info("Executing Australian outreach cycle (limit=%d, send_telegram=%s)...", limit, send_telegram)
    jobs = discover_australian_sponsored_jobs(limit=limit * 3, force_refresh=force_refresh)
    sent_ids = load_sent_alerts()

    new_jobs = [j for j in jobs if str(j.get("id")) not in sent_ids]
    logger.info("Found %d total jobs, %d unalerted.", len(jobs), len(new_jobs))

    out_dir = REPO_ROOT / "artifacts" / "australia_applications"
    out_dir.mkdir(parents=True, exist_ok=True)

    targeted = new_jobs[:limit]
    processed_dossiers: list[dict[str, Any]] = []

    for job in targeted:
        dossier = build_application_dossier(job)
        dossier_file = out_dir / f"application_{job['id']}.json"
        with open(dossier_file, "w", encoding="utf-8") as f:
            json.dump(dossier, f, ensure_ascii=False, indent=2)

        sent = False
        if send_telegram:
            sent = send_australian_job_telegram(job)

        processed_dossiers.append({
            "job_id": job.get("id"),
            "title": job.get("title"),
            "company": job.get("company"),
            "telegram_sent": sent,
            "dossier_file": str(dossier_file),
        })

    return {
        "status": "OK",
        "total_discovered": len(jobs),
        "unalerted": len(new_jobs),
        "processed_count": len(processed_dossiers),
        "dossiers": processed_dossiers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Australian Visa-Sponsored Job Search Cycle")
    parser.add_argument("--limit", type=int, default=5, help="Max jobs to inspect / send")
    parser.add_argument("--send-telegram", action="store_true", help="Send job cards to Telegram")
    parser.add_argument("--force-refresh", action="store_true", help="Bypass Seek cache and fetch fresh listings")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    res = run_australian_outreach_cycle(
        limit=args.limit,
        send_telegram=args.send_telegram,
        force_refresh=args.force_refresh,
    )

    print("\n" + "=" * 70)
    print("🇦🇺 AUSTRALIAN VISA-SPONSORED VACANCIES CYCLE RESULT")
    print("=" * 70)
    print(f"Total discovered: {res['total_discovered']}")
    print(f"Unalerted:        {res['unalerted']}")
    print(f"Processed:        {res['processed_count']}")
    for d in res["dossiers"]:
        status = "✅ Sent to Telegram" if d.get("telegram_sent") else "📁 Dossier created"
        print(f"• {d['title']} | {d['company']} -> {status}")

    print("\n" + "=" * 70)
    print("🏢 VETTED AUSTRALIAN 482 / DAMA SPONSOR RECRUITMENT AGENCIES")
    print("=" * 70)
    for agency in VETTED_AUSTRALIAN_SPONSOR_AGENCIES:
        print(f"\n• {agency['name']} ({agency['website']})")
        print(f"  Specialty: {agency['specialty']}")
        print(f"  Focus:     {agency['focus']}")
        print(f"  Contact:   {agency['email']}")

    print("\n" + "=" * 70)
    print("✅ Australian search cycle completed.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
