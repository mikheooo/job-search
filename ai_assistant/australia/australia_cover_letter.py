"""Australian Cover Letter Generator for Visa-Sponsored Technical Roles.

Generates concise, persuasive Australian-style cover letters specifically
tailored for technical, telecommunications, and field engineering positions
requiring employer sponsorship (Skills in Demand Subclass 482 / DAMA Subclass 494).
"""
from __future__ import annotations

import datetime
from typing import Any


def generate_australian_cover_letter(
    role_title: str,
    company_name: str,
    location: str = "Australia",
    key_skills: list[str] | None = None,
    job_reference: str = "",
) -> str:
    """Generate tailored, professional Australian cover letter."""
    today_str = datetime.datetime.now().strftime("%d %B %Y")
    ref_line = f"\nRe: Application for {role_title} (Ref: {job_reference})" if job_reference else f"\nRe: Application for {role_title}"

    skills_highlight = ""
    if key_skills:
        skills_str = ", ".join(key_skills[:4])
        skills_highlight = f"My hands-on proficiency in {skills_str} directly matches the operational demands of this role. "

    letter = f"""Mikhail Kolesnikov
Email: mikhailthaiban@gmail.com
Phone / WhatsApp: +7 914 645-15-35
Location: Offshore (Ready for immediate relocation to Australia)
LinkedIn / Portfolio: https://github.com/mikheooo

{today_str}

Hiring Manager / Recruitment Team
{company_name}
{location}
{ref_line}

Dear Hiring Team,

Please accept my application for the {role_title} position currently available with {company_name}. With over 15 years of practical field engineering experience across telecommunications infrastructure, satellite ground stations (VSAT), wireless networks, and enterprise systems, I am excited about the opportunity to contribute my technical capabilities to your operations.

Throughout my career, I have specialized in the end-to-end commissioning, maintenance, and emergency restoration of critical communications hardware in demanding, remote environments:
• Telecommunications & Field Hardware: Extensive experience assembling, pointing, and tuning parabolic VSAT antennas (Ku/C-band, 1.2m to 3.8m), calibrating transceivers, BUCs, LNBs, and configuring satellite modems (iDirect, Hughes). Proven record diagnosing signal degradation, cabling faults, and RF link issues with minimal downtime.
• Enterprise Networking & Systems: Hands-on expertise configuring Cisco, MikroTik, and Ubiquiti routers and switches, implementing secure VPN tunnels, VLAN segmentation, and automated remote monitoring via Zabbix and SNMP.
• Commitment to Work Health & Safety (WHS): Fully committed to a "Zero Harm" safety culture. Experienced working under Safe Work Method Statements (SWMS), Job Safety Analysis (JSA), working at heights protocols, and strict PPE compliance on industrial sites.

{skills_highlight}Visa Status & Relocation Readiness:
I am an offshore candidate actively seeking employer sponsorship under the Skills in Demand (Subclass 482) or Designated Area Migration Agreement (DAMA Subclass 494) pathways. I am fully prepared to relocate immediately to any location across Australia—including regional hubs, remote mining camps, or FIFO (Fly-In Fly-Out) rosters. All documentation regarding my higher engineering degree, employment verification, and international driver's license is fully organized and ready for rapid skills assessment and visa sponsorship lodgement.

Thank you for your time and consideration. I would welcome the opportunity to discuss via video call how my 15+ years of hands-on field experience can deliver immediate value to {company_name}.

Yours sincerely,

Mikhail Kolesnikov
Telecommunications & Field Systems Specialist
"""
    return letter.strip()


if __name__ == "__main__":
    cl = generate_australian_cover_letter(
        role_title="Telecommunications Field Technician",
        company_name="Downer Group",
        location="Perth, WA",
        key_skills=["VSAT satellite comms", "Cisco networking", "Fiber/coax cabling", "WHS compliance"],
    )
    print(cl)
