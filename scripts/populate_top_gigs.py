"""Populate top_gig_proposals from shortlisted gigs with A/B variant tracking."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from ai_assistant.gig_pitch_generator import generate_gig_pitch_with_variant

conn = sqlite3.connect("state.db")
c = conn.cursor()

now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

# Fetch top 5 Kwork gigs
c.execute("""
SELECT id, source, source_id, url, title, description, budget_min, budget_max, currency
FROM gigs
WHERE source = 'kwork' AND status = 'SHORTLISTED'
ORDER BY match_score DESC, opportunity_score DESC
LIMIT 5
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

# Fetch top 2 Telegram gigs
c.execute("""
SELECT id, source, source_id, url, title, description, budget_min, budget_max, currency
FROM gigs
WHERE source LIKE 'tg_%' AND status = 'SHORTLISTED'
ORDER BY match_score DESC, opportunity_score DESC
LIMIT 2
""")
tg_gigs = c.fetchall()

selected = kwork_gigs + freelancer_gigs + tg_gigs
print(f"Selected {len(selected)} gigs for top_gig_proposals:")

c.execute("DELETE FROM top_gig_proposals WHERE status = 'PROPOSAL_READY'")

inserted = 0
for g in selected:
    gid, src, src_id, url, title, desc, b_min, b_max, curr = g
    budget_text = f"{int(b_min):,} - {int(b_max):,} {curr}".replace(",", " ") if (b_min and b_max) else "Договорной"
    if src == "kwork":
        contact = f"Kwork #{src_id}"
    elif src == "freelancer":
        contact = f"Freelancer #{src_id}"
    else:
        contact = f"Telegram ({src})"

    pitch, variant_id = generate_gig_pitch_with_variant(
        title=title,
        description=desc or "",
        budget=budget_text,
        source=src,
        lead_id=gid,
    )

    c.execute("""
        INSERT INTO top_gig_proposals (
            gig_id, source, title, budget_text, contact, deeplink, url, proposal_pitch, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSAL_READY', ?, ?)
    """, (gid, src, title, budget_text, contact, None, url, pitch, now_iso, now_iso))
    inserted += 1
    print(f"[+] Added {gid} [variant:{variant_id}]: {title} ({budget_text})")

conn.commit()
conn.close()
print(f"Successfully populated {inserted} proposals in top_gig_proposals!")
