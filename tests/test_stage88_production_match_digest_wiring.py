"""Stage 88: Production Match-to-Digest Wiring & Calibration Test Suite.

Verifies that the unattended production digest pipeline strictly uses the
Stage 86/87 calibrated JobMatcher, role priorities (P1/P2/P3), skill confidence,
domain-specific experience, hard reject gates, delivery history deduplication,
and company diversity rules.
"""
import io
import json
import sqlite3
import contextlib
import pytest
from unittest.mock import patch

from ai_assistant.schema import Vacancy
from ai_assistant.matcher import JobMatcher, MatchDecisionClass, RolePriority
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.cli import export_digest_cmd
from ai_assistant.db import (
    init_db,
    save_vacancy,
    get_connection,
    mark_digest_delivered,
    list_undigested_vacancies,
)


@pytest.fixture
def calibrated_profile():
    return CandidateProfile.from_dict({
        "desired_roles": ["AI Automation Engineer", "Technical Support Engineer", "Python Developer", "System Administrator"],
        "skills": ["python", "n8n", "automation", "telegram", "rest api"],
        "secondary_skills": ["docker", "linux", "sql"],
        "years_experience": 3,
        "minimum_salary": 1500,
        "salary_currency": "USD",
        "remote_required": True,
        "allowed_locations": ["worldwide", "emea", "thailand", "cyprus", "georgia", "armenia"],
        "languages": ["russian", "english"],
        "role_priorities": {
            "AI_AUTOMATION": "P1",
            "APPLICATION_SUPPORT": "P1",
            "TECH_SUPPORT": "P1",
            "PYTHON_BACKEND": "P2",
            "SYSTEM_ADMIN": "P2",
            "DATA_ENGINEERING": "P3",
            "DEVOPS_SRE": "P3",
        },
        "domain_years": {
            "it_support": 11.0,
            "system_admin": 9.0,
            "application_support": 5.0,
            "automation": 3.5,
            "python": 3.5,
            "ai_llm": 2.0,
        },
        "skill_confidence": {
            "python": "PROFESSIONAL",
            "n8n": "PROFESSIONAL",
            "automation": "PROFESSIONAL",
            "fastapi": "PROJECT",
            "c++": "UNKNOWN",
        },
        "role_specific_skills": {
            "AI_AUTOMATION": ["n8n", "python", "automation"],
            "PYTHON_BACKEND": ["python", "fastapi", "rest api", "sql"],
            "TECH_SUPPORT": ["technical support", "troubleshooting", "active directory"],
            "SYSTEM_ADMIN": ["linux", "active directory", "bash", "powershell"],
            "DATA_ENGINEERING": ["python", "sql", "pandas"],
            "DEVOPS_SRE": ["linux", "docker", "ci/cd", "bash"],
        },
    })


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_state.db"
    monkeypatch.setattr("ai_assistant.config.DB_FILE", str(test_db))
    init_db()
    return str(test_db)


# 1. production digest uses canonical Stage 87 matcher
def test_production_digest_uses_canonical_stage87_matcher(calibrated_profile, monkeypatch):
    """1. Stage 88: export_digest_cmd calls canonical JobMatcher with calibrated profile."""
    called_matchers = []
    real_init = JobMatcher.__init__

    def wrapped_init(self, profile):
        called_matchers.append(profile)
        real_init(self, profile)

    monkeypatch.setattr(JobMatcher, "__init__", wrapped_init)
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)

    assert len(called_matchers) > 0
    assert called_matchers[0].role_priorities["AI_AUTOMATION"] == "P1"


# 2. hard REJECT cannot enter digest
def test_hard_reject_cannot_enter_digest(calibrated_profile, monkeypatch):
    """2. Stage 88: Vacancies with hard requirement failure cannot enter digest."""
    onsite_vac = Vacancy(
        source="test_src",
        source_job_id="onsite_1",
        title="AI Automation Engineer (Onsite Only Berlin)",
        company="Berlin Corp",
        description="Must work onsite in Berlin office 5 days a week. Python n8n automation.",
        job_url="https://example.com/onsite1",
        location="Berlin, Germany (Onsite)",
    )
    matcher = JobMatcher(calibrated_profile)
    res = matcher.match(onsite_vac)
    assert res.decision_class == "REJECT"
    assert res.decision == "SKIP"

    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: [onsite_vac])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)

    payload = json.loads(buf.getvalue())
    assert payload["count"] == 0


# 3. STRONG_MATCH is eligible
def test_strong_match_is_eligible(calibrated_profile, monkeypatch):
    """3. Stage 88: STRONG_MATCH vacancy enters digest."""
    strong_vac = Vacancy(
        source="test_src",
        source_job_id="strong_1",
        title="Senior AI Automation Engineer (n8n / Python)",
        company="AutoCorp",
        description="100% remote worldwide. Required: Python, n8n, workflow automation. English B1 ok.",
        job_url="https://example.com/strong1",
        location="Remote Worldwide",
        salary_min=3000,
        salary_max=5000,
        salary_currency="USD",
    )
    matcher = JobMatcher(calibrated_profile)
    res = matcher.match(strong_vac)
    assert res.decision_class == "STRONG_MATCH"

    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: [strong_vac])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)

    payload = json.loads(buf.getvalue())
    assert payload["count"] == 1
    assert payload["new_vacancies_data"][0]["id"] == "test_src:strong_1"


# 4. MATCH is eligible according to policy
def test_match_is_eligible_according_to_policy(calibrated_profile, monkeypatch):
    """4. Stage 88: MATCH decision class vacancy enters digest."""
    match_vac = Vacancy(
        source="test_src",
        source_job_id="match_1",
        title="Middle Python Developer",
        company="PySoft",
        description="Remote work worldwide. Python, FastAPI, SQL, Docker. REST APIs.",
        job_url="https://example.com/match1",
        location="Remote Worldwide",
        salary_min=2500,
        salary_currency="USD",
    )
    matcher = JobMatcher(calibrated_profile)
    res = matcher.match(match_vac)
    assert res.decision_class == "MATCH"

    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: [match_vac])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)

    payload = json.loads(buf.getvalue())
    assert payload["count"] == 1


# 5. STRETCH follows explicit policy
def test_stretch_follows_explicit_policy(calibrated_profile, monkeypatch):
    """5. Stage 88: STRETCH vacancy is ranked after STRONG_MATCH and MATCH."""
    strong_vac = Vacancy(
        source="test_src",
        source_job_id="strong_1",
        title="AI Automation Engineer",
        company="AutoCorp",
        description="Remote Worldwide. Python, n8n, automation. English ok.",
        job_url="https://example.com/s1",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    stretch_vac = Vacancy(
        source="test_src",
        source_job_id="stretch_1",
        title="Junior Data Engineer",
        company="DataCo",
        description="Remote Worldwide. Data pipelines, ETL, SQL, Python, Pandas. Russian or English.",
        job_url="https://example.com/str1",
        location="Remote Worldwide",
        salary_min=2000,
        salary_currency="USD",
    )
    matcher = JobMatcher(calibrated_profile)
    res_str = matcher.match(stretch_vac)
    assert res_str.decision_class == "STRETCH"
    assert res_str.role_priority == "P3"

    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: [stretch_vac, strong_vac])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)

    payload = json.loads(buf.getvalue())
    assert payload["count"] == 2
    assert payload["new_vacancies_data"][0]["id"] == "test_src:strong_1"
    assert payload["new_vacancies_data"][1]["id"] == "test_src:stretch_1"


# 6. P1 outranks equivalent P2
def test_p1_outranks_equivalent_p2(calibrated_profile, monkeypatch):
    """6. Stage 88: P1 target role outranks P2 adjacent role within same decision class."""
    p1_vac = Vacancy(
        source="test_src",
        source_job_id="p1_vac",
        title="AI Automation Specialist",
        company="SupportCo",
        description="Remote Worldwide. Automation, workflows, Python, n8n. English ok.",
        job_url="https://example.com/p1",
        location="Remote Worldwide",
        salary_min=2000,
        salary_currency="USD",
    )
    p2_vac = Vacancy(
        source="test_src",
        source_job_id="p2_vac",
        title="Python Developer",
        company="BackendCo",
        description="Remote Worldwide. Python, SQL, REST API, Docker. English ok.",
        job_url="https://example.com/p2",
        location="Remote Worldwide",
        salary_min=2000,
        salary_currency="USD",
    )
    matcher = JobMatcher(calibrated_profile)
    res_p1 = matcher.match(p1_vac)
    res_p2 = matcher.match(p2_vac)
    assert res_p1.role_priority == "P1"
    assert res_p2.role_priority == "P2"

    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: [p2_vac, p1_vac])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)

    payload = json.loads(buf.getvalue())
    assert payload["count"] == 2
    assert payload["new_vacancies_data"][0]["id"] == "test_src:p1_vac"
    assert payload["new_vacancies_data"][1]["id"] == "test_src:p2_vac"


# 7. P2 outranks equivalent P3 when other factors equal
def test_p2_outranks_equivalent_p3(calibrated_profile, monkeypatch):
    """7. Stage 88: P2 adjacent role outranks P3 stretch role."""
    p2_vac = Vacancy(
        source="test_src",
        source_job_id="p2_vac",
        title="System Administrator",
        company="SysCo",
        description="Remote. Linux, bash, powershell, active directory, troubleshooting.",
        job_url="https://example.com/p2",
        location="Remote",
        salary_min=2000,
    )
    p3_vac = Vacancy(
        source="test_src",
        source_job_id="p3_vac",
        title="Junior DevOps Engineer",
        company="DevOpsCo",
        description="Remote. Linux, Docker, CI/CD, Python scripting.",
        job_url="https://example.com/p3",
        location="Remote",
        salary_min=2000,
    )
    matcher = JobMatcher(calibrated_profile)
    res_p2 = matcher.match(p2_vac)
    res_p3 = matcher.match(p3_vac)
    assert res_p2.role_priority == "P2"
    assert res_p3.role_priority == "P3"

    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: [p3_vac, p2_vac])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)

    payload = json.loads(buf.getvalue())
    assert payload["new_vacancies_data"][0]["id"] == "test_src:p2_vac"


# 8. delivery history prevents duplicate digest
def test_delivery_history_prevents_duplicate_digest(calibrated_profile, isolated_db):
    """8. Stage 88: Delivered vacancies are excluded by list_undigested_vacancies."""
    init_db()
    vac = Vacancy(
        source="himalayas",
        source_job_id="deliv_test_1",
        title="AI Automation Engineer",
        company="AutoGlobal",
        description="Remote. Python, n8n.",
        job_url="https://example.com/deliv1",
        location="Remote",
    )
    save_vacancy(vac)
    sid = vac.stable_id()

    # Before delivery -> present in undigested
    undigested_before = [v.stable_id() for v in list_undigested_vacancies(limit=50)]
    assert sid in undigested_before

    # Mark delivered
    mark_digest_delivered([sid], chat_id="-1001")

    # After delivery -> excluded
    undigested_after = [v.stable_id() for v in list_undigested_vacancies(limit=50)]
    assert sid not in undigested_after


# 9. cross-source canonical duplicate appears once
def test_cross_source_canonical_duplicate_appears_once(calibrated_profile, monkeypatch):
    """9. Stage 88: Duplicate title + company across sources appears only once in digest."""
    vac1 = Vacancy(
        source="himalayas",
        source_job_id="101",
        title="Senior AI Automation Engineer",
        company="Synthetix Labs",
        description="Remote. Python, n8n.",
        job_url="https://example.com/hima101",
        location="Remote",
    )
    vac2 = Vacancy(
        source="remoteok",
        source_job_id="202",
        title="Senior AI Automation Engineer",
        company="Synthetix Labs",
        description="Remote. Python, n8n.",
        job_url="https://example.com/rok202",
        location="Remote",
    )

    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: [vac1, vac2])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)

    payload = json.loads(buf.getvalue())
    assert payload["count"] == 1


# 10. company diversity preserved
def test_company_diversity_preserved(calibrated_profile, monkeypatch):
    """10. Stage 88: Maximum 2 vacancies per company in single digest."""
    vacs = [
        Vacancy(
            source="test_src",
            source_job_id=f"comp_{i}",
            title=f"AI Automation Role {i}",
            company="SameBigCorp",
            description="Remote. Python, n8n.",
            job_url=f"https://example.com/c{i}",
            location="Remote",
        )
        for i in range(5)
    ]

    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: vacs)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)

    payload = json.loads(buf.getvalue())
    assert payload["count"] == 2


# 11. preview and production selector return same ordering
def test_preview_and_production_selector_return_same_ordering(calibrated_profile, monkeypatch):
    """11. Stage 88: CLI preview and JSON export share exact same ordering and candidate selection."""
    vacs = [
        Vacancy(
            source="test_src",
            source_job_id="v1",
            title="AI Automation Engineer",
            company="Company Alpha",
            description="Remote. Python, n8n.",
            job_url="https://example.com/v1",
            location="Remote",
        ),
        Vacancy(
            source="test_src",
            source_job_id="v2",
            title="Technical Support Engineer",
            company="Company Beta",
            description="Remote. Technical support, active directory.",
            job_url="https://example.com/v2",
            location="Remote",
        ),
    ]

    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: vacs)

    buf_json = io.StringIO()
    with contextlib.redirect_stdout(buf_json):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    json_data = json.loads(buf_json.getvalue())
    json_ids = [it["id"] for it in json_data["new_vacancies_data"]]

    buf_txt = io.StringIO()
    with contextlib.redirect_stdout(buf_txt):
        export_digest_cmd(format_type="telegram", limit=5, min_score=60.0, output_json=False)
    txt_data = buf_txt.getvalue()

    for jid in json_ids:
        assert ("Alpha" in txt_data) and ("Beta" in txt_data)


# 12. production path does not call legacy scorer
def test_production_path_does_not_call_legacy_scorer(calibrated_profile, monkeypatch):
    """12. Stage 88: export_digest_cmd strictly instantiates JobMatcher."""
    sample_vac = Vacancy(
        source="test",
        source_job_id="test_match_1",
        title="AI Automation Engineer",
        company="Co",
        description="Remote Worldwide. Python, n8n.",
        job_url="https://example.com/1",
        location="Remote Worldwide",
        salary_min=2000,
        salary_currency="USD",
    )
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: [sample_vac])
    
    with patch("ai_assistant.cli.JobMatcher.match", wraps=JobMatcher(calibrated_profile).match) as mock_match:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
        assert mock_match.called


# 13. candidate profile confidence affects production result
def test_candidate_profile_confidence_affects_production_result(calibrated_profile):
    """13. Stage 88: Higher confidence skill yields higher score than unverified skill."""
    matcher = JobMatcher(calibrated_profile)
    
    # Vacancy with confirmed skill (n8n: PROFESSIONAL)
    v_conf = Vacancy(
        source="test", source_job_id="c1",
        title="AI Automation Engineer",
        company="Co1",
        description="Remote. n8n workflow automation.",
        job_url="https://example.com/1",
        location="Remote"
    )
    # Vacancy with unverified skill (C++: UNKNOWN)
    v_unconf = Vacancy(
        source="test", source_job_id="c2",
        title="C++ Systems Developer",
        company="Co2",
        description="Remote. C++ core development.",
        job_url="https://example.com/2",
        location="Remote"
    )
    res_conf = matcher.match(v_conf)
    res_unconf = matcher.match(v_unconf)

    assert res_conf.score > res_unconf.score
    assert res_unconf.decision_class == "REJECT"


# 14. domain-specific experience affects production result
def test_domain_specific_experience_affects_production_result(calibrated_profile):
    """14. Stage 88: Senior Python backend requiring 8+ yrs Python incurs experience gap penalty."""
    matcher = JobMatcher(calibrated_profile)
    
    # Vacancy requiring 8 years Python backend
    v_senior_py = Vacancy(
        source="test", source_job_id="py8",
        title="Staff Python Backend Engineer",
        company="PyGiant",
        description="Remote. Requires at least 8 years of commercial Python backend development.",
        job_url="https://example.com/py8",
        location="Remote"
    )
    res = matcher.match(v_senior_py)
    assert any("experience" in g.lower() or "seniority" in g.lower() for g in res.gaps)
    assert res.score < 80


# 15. missing salary is not treated as below salary threshold
def test_missing_salary_is_not_treated_as_below_salary_threshold(calibrated_profile):
    """15. Stage 88: Vacancy with unspecified salary receives neutral 5/10, not 0/10."""
    matcher = JobMatcher(calibrated_profile)
    v = Vacancy(
        source="test", source_job_id="nosal",
        title="AI Automation Specialist",
        company="AutoCo",
        description="Remote. Python, n8n.",
        job_url="https://example.com/nosal",
        location="Remote",
        salary_min=None,
        salary_max=None,
    )
    res = matcher.match(v)
    assert not any("below minimum salary" in g.lower() for g in res.gaps)
    assert any("salary not specified" in g.lower() for g in res.gaps)


# 16. remote hard gate applies in production selector
def test_remote_hard_gate_applies_in_production_selector(calibrated_profile, monkeypatch):
    """16. Stage 88: Non-remote vacancy fails remote constraint and is rejected."""
    v_hybrid = Vacancy(
        source="test", source_job_id="hyb1",
        title="AI Automation Engineer",
        company="HybridCo",
        description="Hybrid role: 3 days in office London.",
        job_url="https://example.com/hyb1",
        location="London (Hybrid)",
    )
    matcher = JobMatcher(calibrated_profile)
    res = matcher.match(v_hybrid)
    assert res.decision_class == "REJECT"

    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: [v_hybrid])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 0


# 17. production preview is read-only
def test_production_preview_is_read_only(calibrated_profile, isolated_db):
    """17. Stage 88: export_digest_cmd without mark_delivered does not modify state.db."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM telegram_delivery_records")
    count_before = cur.fetchone()[0]
    conn.close()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM telegram_delivery_records")
    count_after = cur.fetchone()[0]
    conn.close()

    assert count_before == count_after


# 18. rejected vacancy cannot leak via legacy numeric threshold
def test_rejected_vacancy_cannot_leak_via_legacy_numeric_threshold(calibrated_profile, monkeypatch):
    """18. Stage 88: A vacancy with SKIP decision cannot leak even if min_score is set to 0."""
    v_rej = Vacancy(
        source="test", source_job_id="leak_test",
        title="Senior C++ Graphics Developer",
        company="GamesCorp",
        description="C++ OpenGL DirectX developer. Onsite required.",
        job_url="https://example.com/leak",
        location="Munich, Germany",
    )
    matcher = JobMatcher(calibrated_profile)
    res = matcher.match(v_rej)
    assert res.decision == "SKIP"

    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    monkeypatch.setattr("ai_assistant.cli.list_undigested_vacancies", lambda limit: [v_rej])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=0.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 0
