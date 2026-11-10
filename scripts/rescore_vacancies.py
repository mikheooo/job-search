"""Batch re-score unindexed NEW vacancies against candidate profile."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from ai_assistant.candidate_profile import load_candidate_profile
from ai_assistant.matcher import JobMatcher
from ai_assistant.db import _row_to_vacancy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "state.db"


def run(limit: int | None = None, batch_size: int = 500, min_score_to_promote: float = 40.0):
    print("Connecting to DB...", flush=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    print("Loading candidate profile...", flush=True)
    profile = load_candidate_profile()
    matcher = JobMatcher(profile)
    print("Matcher loaded.", flush=True)

    query = """
        SELECT stable_id, source, source_job_id, title, company, description, location,
               country_restrictions, timezone_restrictions, salary_min, salary_max, salary_currency,
               employment_type, job_url, application_url, published_at, first_seen_at, last_seen_at,
               state, raw_data, match_score, match_decision, match_reasons, match_strengths, match_gaps
        FROM vacancies
        WHERE state = 'NEW'
    """
    if limit:
        query += f" LIMIT {limit}"

    c.execute(query)
    rows = c.fetchall()
    total = len(rows)
    print(f"Total NEW vacancies to score: {total}", flush=True)

    if total == 0:
        conn.close()
        return

    t0 = time.time()
    scored_count = 0
    promoted_count = 0
    high_match_count = 0
    updates = []

    for r in rows:
        v = _row_to_vacancy(r)
        if not v:
            continue

        result = matcher.match(v)
        score = result.score
        decision = result.decision
        reasons_json = json.dumps(result.reasons, ensure_ascii=False)
        strengths_json = json.dumps(result.strengths, ensure_ascii=False)
        gaps_json = json.dumps(result.gaps, ensure_ascii=False)

        new_state = "NEW"
        if decision in ("APPLY", "REVIEW") and score >= min_score_to_promote:
            new_state = "DISCOVERED"
            promoted_count += 1

        if score >= 60.0:
            high_match_count += 1

        updates.append((score, decision, reasons_json, strengths_json, gaps_json, new_state, v.stable_id()))
        scored_count += 1

        if len(updates) >= batch_size:
            c.executemany("""
                UPDATE vacancies 
                SET match_score = ?,
                    match_decision = ?,
                    match_reasons = ?,
                    match_strengths = ?,
                    match_gaps = ?,
                    state = ?
                WHERE stable_id = ?
            """, updates)
            conn.commit()
            updates.clear()
            elapsed = time.time() - t0
            print(f"Scored {scored_count}/{total} ({scored_count/elapsed:.1f} vac/sec, {promoted_count} promoted)...", flush=True)

    if updates:
        c.executemany("""
            UPDATE vacancies 
            SET match_score = ?,
                match_decision = ?,
                match_reasons = ?,
                match_strengths = ?,
                match_gaps = ?,
                state = ?
            WHERE stable_id = ?
        """, updates)
        conn.commit()
        updates.clear()

    total_time = time.time() - t0
    conn.close()

    print("\n=======================================================", flush=True)
    print("           BATCH RE-SCORING COMPLETED", flush=True)
    print("=======================================================", flush=True)
    print(f"Total vacancies scored:            {scored_count}", flush=True)
    print(f"Promoted to DISCOVERED (>= 40):    {promoted_count}", flush=True)
    print(f"High matches (>= 60):              {high_match_count}", flush=True)
    print(f"Time taken:                        {total_time:.2f} s ({scored_count/total_time:.1f} vac/sec)", flush=True)
    print("=======================================================", flush=True)


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(limit=limit)
