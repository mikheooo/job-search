"""Tests for Australian Job Discovery & Visa Sponsorship Outreach Engine."""
from __future__ import annotations

import json
import pathlib
import pytest

from ai_assistant.australia.australia_cover_letter import generate_australian_cover_letter
from ai_assistant.australia.australia_outreach import (
    build_application_dossier,
    format_australian_job_card,
    load_sent_alerts,
    record_sent_alert,
    SENT_ALERTS_FILE,
)
from ai_assistant.australia.australia_resume_builder import (
    AUSTRALIA_CV_HTML,
    ensure_australian_cv_pdf,
)
from ai_assistant.australia.seek_discovery import (
    SPONSORSHIP_PATTERNS,
    discover_australian_sponsored_jobs,
    extract_jobs_from_seek_data,
)


def test_australia_cv_eeo_compliance():
    """Australian CV must strictly comply with EEO legislation (no photo, birthdate, marital status)."""
    lowered = AUSTRALIA_CV_HTML.lower()
    assert "photo" not in lowered
    assert "date of birth" not in lowered
    assert "marital status" not in lowered
    assert "gender" not in lowered

    # Key technical domain skills for trade & field engineer sponsorship
    assert "vsat" in lowered
    assert "c-band" in lowered or "ku-band" in lowered
    assert "cisco" in lowered
    assert "mikrotik" in lowered
    assert "whs" in lowered or "swms" in lowered


def test_ensure_australian_cv_pdf():
    """Ensure Australian CV PDF exists and is non-empty."""
    pdf_path = ensure_australian_cv_pdf()
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 50000


def test_generate_australian_cover_letter():
    """Cover letter must emphasize 15+ years experience, Subclass 482 / 494 right to work, and WHS culture."""
    letter = generate_australian_cover_letter(
        company_name="Downer Telecommunications",
        role_title="Field Service Technician",
        location="Perth WA",
        job_reference="JOB-482-PERTH",
    )
    assert "Downer Telecommunications" in letter
    assert "Field Service Technician" in letter
    assert "Perth WA" in letter
    assert "JOB-482-PERTH" in letter
    assert "15 years" in letter
    assert "Subclass 482" in letter
    assert "Zero Harm" in letter
    assert "mikhailthaiban@gmail.com" in letter


def test_seek_job_extraction():
    """Verify parsing of SEEK Redux JSON payload."""
    mock_payload = {
        "results": {
            "results": {
                "jobs": [
                    {
                        "id": "12345678",
                        "title": "Telecommunications Field Technician - Visa Sponsorship Available",
                        "advertiser": {"description": "Apex Telecom Australia"},
                        "locations": [{"countryCode": "AU", "label": "Perth WA"}],
                        "salaryLabel": "AUD $95,000 - $110,000",
                        "bulletPoints": [
                            "Full Subclass 482 Visa Sponsorship available",
                            "Company vehicle + tools provided",
                        ],
                        "teaser": "Seeking experienced telecom technician for remote microwave and VSAT sites.",
                    },
                    {
                        "id": "87654321",
                        "title": "Quantitative Trading Internship - Summer 2026",
                        "advertiser": {"description": "Finance Corp"},
                        "locations": [{"countryCode": "AU", "label": "Sydney NSW"}],
                        "salaryLabel": "Competitive",
                        "teaser": "Internship for math students.",
                    },
                ]
            }
        }
    }

    jobs = extract_jobs_from_seek_data(mock_payload)
    # The quant internship must be excluded
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == "12345678"
    assert job["company"] == "Apex Telecom Australia"
    assert job["location"] == "Perth WA"
    assert job["salary"] == "AUD $95,000 - $110,000"
    assert len(job["matched_keywords"]) >= 1
    assert "https://www.seek.com.au/job/12345678" == job["url"]


def test_format_australian_job_card():
    """Verify Telegram alert card formatting."""
    job = {
        "id": "999000",
        "title": "Electronics Field Service Tech",
        "company": "Brunel Australasia",
        "location": "Mackay QLD",
        "salary": "AUD $105,000 / year",
        "url": "https://www.seek.com.au/job/999000",
        "bullet_points": ["482 sponsorship considered for overseas trades"],
        "matched_keywords": ["482\\s+visa"],
    }
    text, buttons = format_australian_job_card(job)
    assert "Electronics Field Service Tech" in text
    assert "Brunel Australasia" in text
    assert "Mackay QLD" in text
    assert "AUD $105,000" in text
    assert "Подтверждено спонсорство" in text
    assert len(buttons) >= 1
    assert buttons[0][0]["url"] == "https://www.seek.com.au/job/999000"


def test_build_application_dossier():
    """Verify application dossier bundling with CV and cover letter."""
    job = {
        "id": "112233",
        "title": "Telecom Specialist",
        "company": "Ventia Pty Ltd",
        "location": "Brisbane QLD",
        "url": "https://www.seek.com.au/job/112233",
    }
    dossier = build_application_dossier(job)
    assert dossier["job_id"] == "112233"
    assert "Ventia Pty Ltd" in dossier["cover_letter"]
    assert pathlib.Path(dossier["cv_pdf_path"]).exists()


def test_sent_alerts_persistence(tmp_path, monkeypatch):
    """Verify recording and deduplicating sent alerts."""
    test_file = tmp_path / "sent_alerts.json"
    monkeypatch.setattr("ai_assistant.australia.australia_outreach.SENT_ALERTS_FILE", test_file)
    monkeypatch.setattr("ai_assistant.australia.australia_outreach.DATA_DIR", tmp_path)

    assert load_sent_alerts() == set()
    record_sent_alert("job_alpha")
    assert "job_alpha" in load_sent_alerts()

    record_sent_alert("job_beta")
    alerts = load_sent_alerts()
    assert "job_alpha" in alerts
    assert "job_beta" in alerts
