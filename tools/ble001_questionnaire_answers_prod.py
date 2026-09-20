"""How many questionnaire rows in the production DB are in the erased shape?

Copies state.db to a temp dir and points config.DB_FILE there BEFORE importing
anything that opens a connection. The production database is never opened for
writing and never left in a different state.
"""
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ai_assistant import config

PROD = os.path.join(REPO, "state.db")
TMP = tempfile.mkdtemp(prefix="ble001-q60-prod-")
COPY = os.path.join(TMP, "state.db")
shutil.copy2(PROD, COPY)
config.DB_FILE = COPY

from ai_assistant import db

db.init_db()
conn = db.get_connection()
cur = conn.cursor()
cur.execute(
    "SELECT questionnaire_id, status, answers_json, "
    "length(COALESCE(questions_json,'')) FROM hh_questionnaires"
)
rows = cur.fetchall()
conn.close()

by_status: dict[str, int] = {}
erased_by_status: dict[str, int] = {}
for _qid, status, answers_raw, qlen in rows:
    by_status[status] = by_status.get(status, 0) + 1
    empty = answers_raw is None or answers_raw.strip() in ("", "{}")
    if empty:
        erased_by_status[status] = erased_by_status.get(status, 0) + 1

print(f"копия боевой базы: {COPY}")
print(f"строк в hh_questionnaires: {len(rows)}")
print("по статусам:", json.dumps(by_status, ensure_ascii=False, sort_keys=True))
print("из них с пустыми/отсутствующими ответами:",
      json.dumps(erased_by_status, ensure_ascii=False, sort_keys=True))
submitted = by_status.get("SUBMITTED", 0)
submitted_erased = erased_by_status.get("SUBMITTED", 0)
print(f"SUBMITTED всего: {submitted}, из них без ответов: {submitted_erased}")
print("ВЕРДИКТ:", "ERASED-ROWS-PRESENT" if submitted_erased else "NO-ERASED-ROWS")
