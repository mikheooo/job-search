"""Direct Outreach Engine for Approved Australian 482 / DAMA Sponsor Agencies.

Prepares and dispatches tailored application packages directly to leading Australian
recruitment agencies specializing in international technical placement:
1. Nucleus International Recruitment (info@nucleusrecruitment.com.au)
2. Brunel Australasia (australasia@brunel.net)
3. WorkPac Group (contact@workpac.com)
4. Ventia & Downer Group (careers@ventia.com)
"""
from __future__ import annotations

import email.message
import email.mime.application
import email.mime.multipart
import email.mime.text
import email.policy
import json
import logging
import os
import pathlib
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from ai_assistant.tailoring.analyzer import VacancyAnalysis, VacancyDomain
from ai_assistant.tailoring.resume_tailor import tailor_application_for_vacancy
from ai_assistant.telegram_routing import send_job_message

logger = logging.getLogger("agency_outreach")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO_ROOT / "artifacts" / "australia_applications" / "agency_outreach"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TARGET_AGENCIES = [
    {
        "id": "nucleus_recruitment",
        "name": "Nucleus International Recruitment",
        "email": "info@nucleusrecruitment.com.au",
        "domain": VacancyDomain.MECHANICAL_TRADES,
        "title": "Automotive Technician, Diesel Fitter & Field Equipment Specialist",
        "subject": "Overseas Candidate: Technical Trades & Equipment Diagnostics — 15+ Yrs Exp (482 Visa Sponsorship)",
        "teaser": "International recruitment specialist placing overseas mechanics, diesel fitters, and skilled trades across WA, QLD, and NSW under Subclass 482 and DAMA 494 agreements.",
        "bullet_points": [
            "Full employer visa sponsorship (Skills in Demand 482 / DAMA 494) placement",
            "15+ years hands-on electro-mechanical diagnostics, multimeter signal testing, and equipment servicing",
            "Immediate offshore readiness for relocation across Australian host employers",
        ],
    },
    {
        "id": "brunel_australasia",
        "name": "Brunel Australasia",
        "email": "australasia@brunel.net",
        "domain": VacancyDomain.TELECOM_RF,
        "title": "Telecommunications & Satellite Field Infrastructure Engineer",
        "subject": "Overseas Application: Telecommunications & Field Infrastructure Engineer (Subclass 482 Candidate)",
        "teaser": "Premier technical recruitment partner for major Australian energy, resources, and telecommunications infrastructure projects.",
        "bullet_points": [
            "Satellite ground stations (VSAT C/Ku-band), parabolic antennas, BUC, LNB, and iDirect modems",
            "Microwave radio frequency (RF) links, cabling termination, and enterprise IP routing (Cisco, MikroTik)",
            "Strict compliance with Zero Harm WHS, SWMS, and Working at Heights safety standards",
        ],
    },
    {
        "id": "workpac_group",
        "name": "WorkPac Group",
        "email": "contact@workpac.com",
        "domain": VacancyDomain.FIELD_ENGINEERING,
        "title": "Field Service Technician & Equipment Specialist (FIFO / Regional)",
        "subject": "Expression of Interest: Field Service Technician — 15+ Yrs Exp (FIFO / Regional 482 Sponsorship)",
        "teaser": "Leading Australian workforce provider for mining, regional infrastructure, and industrial operations.",
        "bullet_points": [
            "On-site hardware commissioning, sensor calibration, and preventative maintenance across remote sites",
            "Rapid root-cause diagnostics, electrical schematic tracing, and 99.8% uptime preservation",
            "Prepared for immediate regional Australia or FIFO roster deployment",
        ],
    },
    {
        "id": "ventia_downer",
        "name": "Ventia & Downer Group",
        "email": "careers@ventia.com",
        "domain": VacancyDomain.TELECOM_RF,
        "title": "Field Telecommunications & Electronics Commissioning Technician",
        "subject": "Application: Field Telecommunications & Electronics Technician (TSS 482 Sponsorship Candidate)",
        "teaser": "Australia's largest essential infrastructure and telecommunications services provider.",
        "bullet_points": [
            "15+ years in critical communications network maintenance, antenna alignment, and fiber/copper cabling",
            "Enterprise network integration (Cisco, Linux, telemetry monitoring via Zabbix/SNMP)",
            "Approved Standard Business Sponsor pathways for experienced overseas technicians",
        ],
    },
]


@dataclass
class AgencyApplicationDossier:
    agency_id: str
    agency_name: str
    recipient_email: str
    subject: str
    pdf_cv_path: pathlib.Path
    cover_letter_path: pathlib.Path
    eml_file_path: pathlib.Path
    gmail_compose_url: str
    mailto_url: str
    cheatsheet_path: pathlib.Path


def build_gmail_compose_url(to_email: str, subject: str, body: str) -> str:
    """Build direct 1-click Gmail web compose URL."""
    base = "https://mail.google.com/mail/?view=cm&fs=1"
    params = {
        "to": to_email,
        "su": subject,
        "body": body,
    }
    return f"{base}&{urllib.parse.urlencode(params)}"


def create_agency_eml(
    to_email: str,
    subject: str,
    body: str,
    pdf_cv_path: pathlib.Path,
    slug: str,
) -> pathlib.Path:
    """Create complete RFC-822 .eml file with attached tailored PDF CV."""
    msg = email.message.EmailMessage(policy=email.policy.SMTP)
    msg["From"] = "Mikhail Kolesnikov <mikhailthaiban@gmail.com>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    if pdf_cv_path.exists():
        pdf_bytes = pdf_cv_path.read_bytes()
        msg.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=pdf_cv_path.name,
        )

    eml_path = OUT_DIR / f"{slug}.eml"
    eml_path.write_bytes(msg.as_bytes())
    return eml_path


def prepare_agency_dossier(agency: dict[str, Any]) -> AgencyApplicationDossier:
    """Generate tailored PDF CV, Cover Letter, .eml package, and 1-click links for an agency."""
    job_spec = {
        "id": agency["id"],
        "title": agency["title"],
        "company": agency["name"],
        "location": "Australia (WA / QLD / NSW / NT)",
        "teaser": agency["teaser"],
        "bullet_points": agency["bullet_points"],
    }

    # Generate bespoke tailored CV & Cover Letter
    pkg = tailor_application_for_vacancy(job_spec)

    # Build 1-click compose URLs
    gmail_url = build_gmail_compose_url(agency["email"], agency["subject"], pkg.cover_letter_text)
    mailto_params = {"subject": agency["subject"], "body": pkg.cover_letter_text}
    mailto_url = f"mailto:{agency['email']}?{urllib.parse.urlencode(mailto_params, quote_via=urllib.parse.quote)}"

    # Build .eml package with attached PDF
    eml_path = create_agency_eml(
        to_email=agency["email"],
        subject=agency["subject"],
        body=pkg.cover_letter_text,
        pdf_cv_path=pkg.pdf_cv_path,
        slug=agency["id"],
    )

    return AgencyApplicationDossier(
        agency_id=agency["id"],
        agency_name=agency["name"],
        recipient_email=agency["email"],
        subject=agency["subject"],
        pdf_cv_path=pkg.pdf_cv_path,
        cover_letter_path=pkg.cover_letter_path,
        eml_file_path=eml_path,
        gmail_compose_url=gmail_url,
        mailto_url=mailto_url,
        cheatsheet_path=pkg.cheatsheet_path,
    )


def execute_agency_outreach(send_telegram: bool = True) -> list[AgencyApplicationDossier]:
    """Prepare all 4 agency packages, record to state, and notify Mikhail in Telegram."""
    dossiers: list[AgencyApplicationDossier] = []

    for agency in TARGET_AGENCIES:
        logger.info("Preparing outreach dossier for %s...", agency["name"])
        d = prepare_agency_dossier(agency)
        dossiers.append(d)

    if send_telegram and dossiers:
        summary_lines = []
        for d in dossiers:
            summary_lines.append(
                f"🏢 *{d.agency_name}*\n"
                f"   ✉️ `{d.recipient_email}`\n"
                f"   📄 Резюме: `{d.pdf_cv_path.name}`\n"
            )

        text = (
            f"🦘 *Австралия: Сформированы заявки в 4 главных агентства со спонсорством виз 482 / DAMA*\n\n"
            + "\n".join(summary_lines)
            + f"\n\n💡 *Что сделано:*\n"
            + f"• Под каждое агентство скомпилирован персональный PDF-CV стандарта EEO.\n"
            + f"• Сформированы официальные письма и готовые файлы `.eml` с прикрепленным резюме.\n"
            + f"• Вы можете открыть письмо в 1 клик через веб-почту или файл письма на компьютере.\n"
        )

        buttons = [
            [
                {"text": "✉️ Написать в Nucleus (Авто/Механика)", "url": dossiers[0].gmail_compose_url},
            ],
            [
                {"text": "✉️ Написать в Brunel (Телеком/Связь)", "url": dossiers[1].gmail_compose_url},
            ],
            [
                {"text": "✉️ Написать в WorkPac (Выездной техник)", "url": dossiers[2].gmail_compose_url},
            ],
            [
                {"text": "✉️ Написать в Ventia (Инфраструктура)", "url": dossiers[3].gmail_compose_url},
            ],
        ]

        try:
            send_job_message(text=text, reply_markup={"inline_keyboard": buttons}, parse_mode="Markdown")
        except Exception as exc:
            logger.warning("Failed to dispatch agency telegram card: %s", exc)

    return dossiers
