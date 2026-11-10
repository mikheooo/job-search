"""Prepare Bangkok & Thailand outreach packages and email drafts."""

from __future__ import annotations

import json
import sqlite3
import datetime
from pathlib import Path

from ai_assistant.smart_experience_generator import generate_english_cover_letter
from ai_assistant.email_outreach import dispatch_email_outreach, get_default_cv_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = PROJECT_ROOT / "data" / "thailand_applications.json"
EMAILS_DIR = PROJECT_ROOT / "artifacts" / "emails"
EMAILS_DIR.mkdir(parents=True, exist_ok=True)

BANGKOK_LEADS = [
    {
        "job_id": "4457956542",
        "stable_id": "linkedin_bangkok:4457956542",
        "company": "Cognizant",
        "position": "Deskside Support Engineer",
        "application_url": "https://th.linkedin.com/jobs/view/deskside-support-engineer-at-cognizant-4457956542",
        "expected_salary_thb": 95000,
        "description": "Deskside Support Engineer at Cognizant Bangkok. End-user computing, Windows Server, Active Directory, hardware/software troubleshooting, VIP support, SLA management in enterprise hybrid environment.",
    },
    {
        "job_id": "4468704731",
        "stable_id": "linkedin_bangkok:4468704731",
        "company": "HCLTech",
        "position": "L1 Deskside Support",
        "application_url": "https://th.linkedin.com/jobs/view/l1-deskside-support-at-hcltech-4468704731",
        "expected_salary_thb": 85000,
        "description": "L1 Deskside Support at HCLTech Bangkok. Desktop, laptop provisioning, Windows/Linux OS support, Office 365, network diagnostics, user access management, incident response.",
    },
    {
        "job_id": "4473232592",
        "stable_id": "linkedin_bangkok:4473232592",
        "company": "Microchip Technology Inc.",
        "position": "Technician IS Desktop Support",
        "application_url": "https://th.linkedin.com/jobs/view/technician-is-desktop-support-at-microchip-technology-inc-4473232592",
        "expected_salary_thb": 90000,
        "description": "Technician IS Desktop Support at Microchip Technology Bangkok. Enterprise IT support, PC imaging, Active Directory, network troubleshooting, hardware maintenance, ticket resolution.",
    },
    {
        "job_id": "4483547924",
        "stable_id": "linkedin_bangkok:4483547924",
        "company": "Nipa Digital Marketing Agency",
        "position": "Technical Support Lead (Linux / Cloud Infrastructure)",
        "application_url": "https://th.linkedin.com/jobs/view/technical-support-lead-linux-cloud-infrastructure-at-nipa-digital-marketing-agency-4483547924",
        "expected_salary_thb": 110000,
        "description": "Technical Support Lead (Linux / Cloud Infrastructure) at Nipa Digital Bangkok. Linux server administration, cloud infrastructure, containerized environments, monitoring, high availability, L2/L3 support escalation.",
    },
    {
        "job_id": "4486122814",
        "stable_id": "linkedin_bangkok:4486122814",
        "company": "eTeam",
        "position": "System Engineer / Linux Administrator",
        "application_url": "https://th.linkedin.com/jobs/view/system-engineer-linux-administrator-at-eteam-4486122814",
        "expected_salary_thb": 105000,
        "description": "System Engineer / Linux Administrator at eTeam Bangkok. Linux infrastructure, Docker, shell scripting, automation, server provisioning, security hardening, incident resolution.",
    },
]


def run():
    existing_data = []
    if DATA_FILE.exists():
        try:
            existing_data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing_data = []

    existing_ids = {str(item.get("job_id")) for item in existing_data}

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    conn = sqlite3.connect("state.db")
    c = conn.cursor()

    added_count = 0
    for lead in BANGKOK_LEADS:
        job_id = lead["job_id"]
        company = lead["company"]
        pos = lead["position"]
        app_url = lead["application_url"]
        exp_salary = lead["expected_salary_thb"]
        desc = lead["description"]
        stable_id = lead["stable_id"]

        cl = generate_english_cover_letter(role=pos, company=company, description=desc)

        entry = {
            "job_id": job_id,
            "company": company,
            "position": pos,
            "application_url": app_url,
            "candidate": {
                "first_name": "Mikhail",
                "last_name": "Kolesnikov",
                "email": "mikhailthaiban@gmail.com",
                "phone": "+66815036090",
                "location": "Bangkok, Thailand",
                "linkedin": "https://www.linkedin.com/in/mikheooo",
                "portfolio": "https://mikheooo.github.io/portfolio/",
                "start_date": "Immediate",
                "expected_salary_thb": exp_salary,
                "hear_about": "LinkedIn Jobs / Company Career Portal",
            },
            "cover_letter": cl,
            "status": "APPLICATION_READY",
            "created_at": now_iso,
        }

        if job_id not in existing_ids:
            existing_data.append(entry)
            existing_ids.add(job_id)
            added_count += 1

        # Also generate .eml email draft package
        try:
            dispatch_email_outreach(
                to_email="careers@" + company.lower().replace(" ", "").replace(".", "") + ".com",
                role=pos,
                company=company,
                description=desc,
                is_bangkok=True,
                attempt_smtp=False,
            )
        except Exception as e:
            print(f"Warning creating eml for {company}: {e}")

        # Update state in DB
        c.execute("""
            UPDATE vacancies 
            SET state = 'OUTREACH_READY', 
                match_score = MAX(match_score, 65.0),
                match_decision = 'MATCH'
            WHERE stable_id = ?
        """, (stable_id,))

    conn.commit()
    conn.close()

    DATA_FILE.write_text(json.dumps(existing_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Successfully processed Bangkok leads! Added {added_count} new entries to {DATA_FILE}. Total: {len(existing_data)}")


if __name__ == "__main__":
    run()
