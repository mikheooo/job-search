"""Populate top_gig_proposals from shortlisted gigs."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from ai_assistant.gig_pitch_generator import generate_gig_pitch

conn = sqlite3.connect("state.db")
c = conn.cursor()

now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

# Fetch top 3 Kwork gigs
c.execute("""
SELECT id, source, source_id, url, title, description, budget_min, budget_max, currency
FROM gigs
WHERE source = 'kwork' AND status = 'SHORTLISTED'
ORDER BY match_score DESC, opportunity_score DESC
LIMIT 3
""")
kwork_gigs = c.fetchall()

# Fetch top 3 Freelancer gigs
c.execute("""
SELECT id, source, source_id, url, title, description, budget_min, budget_max, currency
FROM gigs
WHERE source = 'freelancer' AND status = 'SHORTLISTED'
ORDER BY opportunity_score DESC, match_score DESC
LIMIT 3
""")
freelancer_gigs = c.fetchall()

selected = kwork_gigs + freelancer_gigs
print(f"Selected {len(selected)} gigs for top_gig_proposals:")

c.execute("DELETE FROM top_gig_proposals WHERE status = 'PROPOSAL_READY'")

inserted = 0
for g in selected:
    gid, src, src_id, url, title, desc, b_min, b_max, curr = g
    budget_text = f"{int(b_min):,} - {int(b_max):,} {curr}".replace(",", " ") if (b_min and b_max) else "Договорной"
    contact = f"Kwork #{src_id}" if src == "kwork" else f"Freelancer #{src_id}"
    
    pitch = generate_gig_pitch(
        title=title,
        description=desc or "",
        budget=budget_text,
        source=src
    )
    
    c.execute("""
        INSERT INTO top_gig_proposals (
            gig_id, source, title, budget_text, contact, deeplink, url, proposal_pitch, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSAL_READY', ?, ?)
    """, (gid, src, title, budget_text, contact, None, url, pitch, now_iso, now_iso))
    inserted += 1
    print(f"[+] Added {gid}: {title} ({budget_text})")

conn.commit()
conn.close()
print(f"Successfully populated {inserted} proposals in top_gig_proposals!")
