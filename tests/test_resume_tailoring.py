"""Tests for Dynamic Resume & Experience Tailoring Engine."""
from __future__ import annotations

import pathlib
import pytest

from ai_assistant.australia.australia_outreach import build_application_dossier
from ai_assistant.tailoring.analyzer import VacancyDomain, analyze_vacancy
from ai_assistant.tailoring.resume_tailor import (
    generate_interview_cheatsheet,
    generate_tailored_cover_letter,
    generate_tailored_resume_html,
    tailor_application_for_vacancy,
)


def test_analyzer_domain_classification():
    """Verify accurate domain classification across diverse job types."""
    # 1. Telecom
    t_job = {
        "title": "VSAT Satellite Communications Technician",
        "company": "Apex Sat",
        "teaser": "C-band and Ku-band dish alignment, BUC and LNB tuning",
    }
    assert analyze_vacancy(t_job).domain == VacancyDomain.TELECOM_RF

    # 2. Fire & Building Automation
    f_job = {
        "title": "Fire Alarm & Dry Fire Programmer",
        "company": "Safety First",
        "teaser": "Loop diagnostics and signaling panel programming",
    }
    assert analyze_vacancy(f_job).domain == VacancyDomain.FIRE_AUTOMATION

    # 3. Networks & Sysadmin
    n_job = {
        "title": "Senior Network Engineer",
        "company": "Enterprise Tech",
        "teaser": "Cisco routing, MikroTik, VLAN, WireGuard VPN and Linux servers",
    }
    assert analyze_vacancy(n_job).domain == VacancyDomain.NETWORKS_SYSADMIN

    # 4. Mechanical & Equipment Trades
    m_job = {
        "title": "Automotive Technician & Diesel Fitter",
        "company": "Fleet Operations",
        "teaser": "Machinery overhaul, multimeter electrical diagnostics, preventative maintenance",
    }
    assert analyze_vacancy(m_job).domain == VacancyDomain.MECHANICAL_TRADES

    # 5. Software Automation
    s_job = {
        "title": "Python Automation Engineer",
        "company": "Cloud Data",
        "teaser": "API integration, database pipelines and automated telemetry",
    }
    assert analyze_vacancy(s_job).domain == VacancyDomain.SOFTWARE_AUTOMATION


def test_tailored_resume_html_eeo_compliance():
    """Tailored HTML must strictly follow EEO standards and contain target domain highlights."""
    job = {
        "title": "Field Service Technician - Visa Sponsorship Available",
        "company": "Downer Infrastructure",
        "location": "Perth WA",
        "teaser": "Commissioning field hardware and structured cabling under WHS protocols",
        "bullet_points": ["Field commissioning", "Safe work method statements (SWMS)"],
    }
    analysis = analyze_vacancy(job)
    html = generate_tailored_resume_html(analysis)
    lowered = html.lower()

    assert "photo" not in lowered
    assert "date of birth" not in lowered
    assert "marital status" not in lowered
    assert "gender" not in lowered

    assert "field service technician" in lowered
    assert "whs" in lowered or "swms" in lowered
    assert "mikhail kolesnikov" in lowered


def test_tailor_application_end_to_end():
    """End-to-end test verifying PDF, HTML, cover letter and cheatsheet generation."""
    job = {
        "id": "test_7788",
        "title": "Telecommunications Field Specialist",
        "company": "Test Australian Telco",
        "location": "Brisbane QLD",
        "teaser": "Antenna pointing, Cisco routers, microwave links",
        "bullet_points": ["Subclass 482 visa sponsorship available"],
    }
    pkg = tailor_application_for_vacancy(job)
    assert pkg.job_id == "test_7788"
    assert pkg.domain == VacancyDomain.TELECOM_RF
    assert pkg.html_cv_path.exists()
    assert pkg.pdf_cv_path.exists()
    assert pkg.pdf_cv_path.stat().st_size > 10000
    assert pkg.cover_letter_path.exists()
    assert pkg.cheatsheet_path.exists()

    assert "Test Australian Telco" in pkg.cover_letter_text
    assert "Subclass 482" in pkg.cover_letter_text
    assert "Q1:" in pkg.cheatsheet_text
    assert "Q2:" in pkg.cheatsheet_text
    assert "Q3:" in pkg.cheatsheet_text


def test_outreach_dossier_integration():
    """Verify that build_application_dossier produces tailored package with cheatsheet."""
    job = {
        "id": "test_dossier_99",
        "title": "Low Voltage Technician",
        "company": "Secure Building Systems",
        "location": "Melbourne VIC",
        "teaser": "CCTV, access control and fire signaling installation",
    }
    dossier = build_application_dossier(job)
    assert dossier["job_id"] == "test_dossier_99"
    assert "cheatsheet" in dossier
    assert pathlib.Path(dossier["cv_pdf_path"]).exists()
    assert pathlib.Path(dossier["cheatsheet_path"]).exists()
