"""Анализ: что реально требуют вакансии, на которые подавались, и чего нет в профиле.

Read-only. Ничего не пишет, ничего не меняет.
Строит честный список «спрос vs профиль» — вход для инвентаризации опыта.
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import sys
from collections import Counter

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

DB = PROJECT_ROOT / "state.db"

VOCAB = [
    # automation / ai
    "python", "n8n", "make.com", "zapier", "automation", "ai agent", "llm", "openai",
    "gemini", "langchain", "rag", "prompt",
    # backend
    "fastapi", "django", "flask", "rest api", "grpc", "webhook", "sql", "postgresql",
    "mysql", "sqlite", "redis", "mongodb", "clickhouse",
    # devops
    "docker", "kubernetes", "k8s", "terraform", "ansible", "jenkins", "gitlab",
    "github actions", "ci/cd", "git", "linux", "bash", "powershell",
    # windows / sysadmin
    "windows server", "active directory", "exchange", "microsoft 365", "office 365",
    "outlook", "ms office", "gpo", "hyper-v", "vmware", "proxmox",
    # monitoring / network
    "zabbix", "grafana", "prometheus", "monitoring", "networking", "tcp/ip", "vpn",
    "firewall",
    # support
    "helpdesk", "help desk", "technical support", "troubleshooting", "incident",
    "itsm", "jira", "confluence", "servicenow", "sla", "itil",
    # crm / russian stack
    "1c", "bitrix", "bitrix24", "amocrm",
    # telegram / data
    "telegram", "telethon", "aiogram", "pandas", "asyncio", "etl", "airflow",
    "power bi", "tableau", "excel", "vba",
    # other langs
    "php", "ruby", "java", "c++", "c#", "golang", "rust", "swift", "kotlin",
    # misc infra
    "selenium", "playwright", "nginx", "api", "microsoft azure", "aws", "gcp",
    "backup", "veeam", "nas", "raid", "antivirus", "mdm", "intune", "sccm",
    "patching", "onboarding", "offboarding",
]


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Population 1: vacancies actually linked to an application.
    cur.execute(
        """SELECT v.stable_id, v.title, v.description
           FROM vacancies v JOIN hh_applications a
             ON a.vacancy_stable_id = v.stable_id"""
    )
    linked = cur.fetchall()
    # Population 2 (wider): everything the matcher itself judged worth applying to.
    # Only 10 rows survive the join above, because hh_applications.vacancy_stable_id
    # is NULL for most rows — so the linked set alone is too small to be a signal.
    cur.execute(
        """SELECT stable_id, title, description FROM vacancies
           WHERE match_decision IN ('APPLY','REVIEW') AND description IS NOT NULL"""
    )
    eligible = cur.fetchall()

    print(f"Вакансий, реально слинкованных с откликом : {len(linked)}")
    print(f"Вакансий с решением APPLY/REVIEW (шире)   : {len(eligible)}")
    rows = eligible

    blob = " ".join((r["description"] or "") for r in rows).lower()
    print(f"Суммарный текст описаний: {len(blob)} символов\n")

    prof = json.loads((PROJECT_ROOT / "candidate_profile.json").read_text(encoding="utf-8"))
    pblob = json.dumps(prof, ensure_ascii=False).lower()

    print(f"{'навык':<20}{'в вакансиях':>13}   в профиле?")
    print("-" * 58)
    missing: list[tuple[int, str]] = []
    for v in VOCAB:
        # Word boundaries, not substring: a bare scan counted "rust" inside
        # "trust", "rag" inside "storage", "sla" inside "translate" and
        # "excel" inside "excellent" — which inflated the top of the list with
        # pure noise and would have made the whole report untrustworthy.
        pat = rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])"
        n = len(re.findall(pat, blob))
        if n < 3:
            continue
        in_profile = v in pblob
        if not in_profile:
            missing.append((n, v))
        print(f"{v:<20}{n:>13}   {'да' if in_profile else 'НЕТ'}")

    print("\n=== Требуют >= 5 раз, а в профиле НЕТ ===")
    for n, v in sorted(missing, reverse=True):
        if n >= 5:
            print(f"  {n:>5}  {v}")

    print("\n=== Топ-25 вакансий по частоте повторов в откликах ===")
    cur.execute(
        """SELECT v.title, v.company, COUNT(*) n
           FROM vacancies v JOIN hh_applications a
             ON a.vacancy_stable_id = v.stable_id
           GROUP BY v.title, v.company ORDER BY n DESC LIMIT 25"""
    )
    for r in cur.fetchall():
        print(f"  {r['n']:>3}  {r['title'][:60]:<60} {r['company'][:28]}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
