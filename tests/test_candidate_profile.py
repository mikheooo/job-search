from __future__ import annotations

import json
import pytest
from pathlib import Path
from ai_assistant.candidate_profile import CandidateProfile


def test_candidate_profile_from_dict_and_to_dict():
    raw = {
        "desired_roles": ["AI Automation Engineer"],
        "skills": ["python", "n8n"],
        "years_experience": 3,
        "remote_required": True,
        "allowed_locations": ["Remote"],
        "minimum_salary": 1500,
        "salary_currency": "USD",
        "name": "Mikhail Kolesnikov",
        "email": "mikhailthaiban@gmail.com",
        "phone_ru": "+79933397628",
        "phone_th": "+66815036090",
        "linkedin": "https://www.linkedin.com/in/mikheooo",
        "github": "https://github.com/mikheooo",
    }

    prof = CandidateProfile.from_dict(raw)
    assert prof.name == "Mikhail Kolesnikov"
    assert prof.email == "mikhailthaiban@gmail.com"
    assert prof.phone_ru == "+79933397628"
    assert prof.phone_th == "+66815036090"
    assert prof.phone is None
    assert prof.linkedin == "https://www.linkedin.com/in/mikheooo"
    assert prof.github == "https://github.com/mikheooo"
    assert prof.portfolio is None

    out = prof.to_dict()
    assert out["name"] == "Mikhail Kolesnikov"
    assert out["email"] == "mikhailthaiban@gmail.com"
    assert out["phone_ru"] == "+79933397628"
    assert out["phone_th"] == "+66815036090"
    assert out["linkedin"] == "https://www.linkedin.com/in/mikheooo"
    assert out["github"] == "https://github.com/mikheooo"
    assert "portfolio" not in out


def test_candidate_profile_from_json_file():
    p = Path("candidate_profile.json")
    if p.exists():
        prof = CandidateProfile.from_json_file(p)
        assert prof.name == "Mikhail Kolesnikov"
        assert prof.email == "mikhailthaiban@gmail.com"
        assert prof.phone_ru == "+79933397628"
        assert prof.phone_th == "+66815036090"
        assert prof.linkedin == "https://www.linkedin.com/in/mikheooo"
        assert prof.github == "https://github.com/mikheooo"
        assert prof.portfolio is None


def test_candidate_profile_backward_compat():
    prof_minimal = CandidateProfile.from_dict({"desired_roles": ["Python Dev"]})
    assert prof_minimal.name is None
    assert prof_minimal.email is None
    assert prof_minimal.phone_ru is None
    assert prof_minimal.phone_th is None
    assert prof_minimal.phone is None
    assert prof_minimal.linkedin is None
    assert prof_minimal.github is None
    assert prof_minimal.portfolio is None
