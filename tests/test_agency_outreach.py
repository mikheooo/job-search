"""Tests for Australian Sponsor Agency Outreach Engine."""
from __future__ import annotations

import pathlib
import pytest

from ai_assistant.australia.agency_outreach import (
    TARGET_AGENCIES,
    execute_agency_outreach,
    prepare_agency_dossier,
)


def test_prepare_agency_dossier_all_agencies():
    """Verify that every target sponsor agency produces valid tailored dossier with PDF & EML."""
    for agency in TARGET_AGENCIES:
        dossier = prepare_agency_dossier(agency)
        assert dossier.agency_id == agency["id"]
        assert dossier.recipient_email == agency["email"]
        assert dossier.pdf_cv_path.exists()
        assert dossier.pdf_cv_path.stat().st_size > 50000
        assert dossier.eml_file_path.exists()
        assert dossier.eml_file_path.stat().st_size > dossier.pdf_cv_path.stat().st_size
        assert "mail.google.com" in dossier.gmail_compose_url
        assert "mailto:" in dossier.mailto_url


def test_execute_agency_outreach_headless():
    """Test full execution batch without telegram network call."""
    dossiers = execute_agency_outreach(send_telegram=False)
    assert len(dossiers) == 4
    names = [d.agency_name for d in dossiers]
    assert "Nucleus International Recruitment" in names
    assert "Brunel Australasia" in names
    assert "WorkPac Group" in names
    assert "Ventia & Downer Group" in names
