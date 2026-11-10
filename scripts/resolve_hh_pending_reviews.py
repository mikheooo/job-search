"""Update HH applications from NEEDS_HUMAN_REVIEW to ANALYZED after verifying message status."""

import sqlite3
import datetime

conn = sqlite3.connect("state.db")
c = conn.cursor()

now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

updates = [
    ('app_5689987425', 'all_questions_replied_waiting_for_employer'),
    ('app_5585421175', 'waiting_for_employer'),
    ('app_5585083099', 'answers_sent_waiting_for_employer'),
    ('app_5585084830', 'interview_stage_notified'),
    ('app_5585084622', 'interview_stage_notified'),
]

for app_id, reason in updates:
    c.execute("""
        UPDATE hh_applications 
        SET state = 'ANALYZED', 
            last_transition_reason = ?,
            updated_at = ?
        WHERE application_id = ?
    """, (reason, now_iso, app_id))
    print(f"[+] Updated {app_id} -> ANALYZED ({reason})")

conn.commit()

c.execute("SELECT state, count(*) FROM hh_applications GROUP BY state")
print("\nUpdated HH application states:", c.fetchall())

conn.close()
