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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Australian Visa-Sponsored Job Search Cycle")
    parser.add_argument("--limit", type=int, default=5, help="Max jobs to inspect / send")
    parser.add_argument("--send-telegram", action="store_true", help="Send job cards to Telegram")
    parser.add_argument("--force-refresh", action="store_true", help="Bypass Seek cache and fetch fresh listings")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logger.info("Starting Australian job search cycle (limit=%d, send_telegram=%s)...", args.limit, args.send_telegram)

    jobs = discover_australian_sponsored_jobs(limit=args.limit * 3, force_refresh=args.force_refresh)
    sent_ids = load_sent_alerts()

    new_jobs = [j for j in jobs if str(j.get("id")) not in sent_ids]
    logger.info("Found %d total jobs, %d new (unalerted).", len(jobs), len(new_jobs))

    out_dir = REPO_ROOT / "artifacts" / "australia_applications"
    out_dir.mkdir(parents=True, exist_ok=True)

    targeted = new_jobs[: args.limit]
    if not targeted and jobs:
        logger.info("No unalerted jobs found; displaying top existing jobs.")
        targeted = jobs[: args.limit]

    print("\n" + "=" * 70)
    print("🇦🇺 TOP AUSTRALIAN VISA-SPONSORED VACANCIES FOR MIKHAIL KOLESNIKOV")
    print("=" * 70)

    for idx, job in enumerate(targeted, 1):
        dossier = build_application_dossier(job)
        dossier_file = out_dir / f"application_{job['id']}.json"
        with open(dossier_file, "w", encoding="utf-8") as f:
            json.dump(dossier, f, ensure_ascii=False, indent=2)

        print(f"\n#{idx} {job['title']} | {job['company']}")
        print(f"    📍 Location: {job['location']}")
        print(f"    💰 Salary:   {job['salary']}")
        print(f"    🔗 URL:      {job['url']}")
        print(f"    📄 Dossier:  {dossier_file}")

        if args.send_telegram:
            ok = send_australian_job_telegram(job)
            status = "✅ Sent to Telegram" if ok else "❌ Telegram send failed"
            print(f"    📲 Telegram: {status}")

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
