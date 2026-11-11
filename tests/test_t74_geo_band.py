"""T7.4 — remote eligibility: employer-first geography and the A/B/C bands.

Two defects are pinned here.

T7.4-1 (geography). The engine reported a vacancy as available from Thailand
because it was published on a remote-only board, even when the employer's own
text requires the candidate to be in the United States. The live examples were
WeWorkRemotely's VXI ("US Remote Technical Support Advisor") and Porkbun
("Applicants must be based in the USA"), both of which the engine used to return
as ``eligible`` with ``geo_scope == worldwide``.

T7.4-2 (Habr Career). The board's structural format sentence
"Полный рабочий день. Можно удалённо." was read as a conditional offer in the
employer's prose and rejected, so no Habr Career vacancy could ever be
recognised as remote.

Band vocabulary used throughout:

    A -- the employer explicitly allows Thailand or any country;
    B -- remote, but availability from Thailand is not established;
    C -- the employer restricts to another country, requires office presence,
         or the board's label contradicts the employer.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from ai_assistant import config, db
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.eligibility import (
    EligibilityStatus,
    GeoBand,
    GeoEvidence,
    GeoScope,
    RemoteMode,
    assess_vacancy_eligibility,
    classify_geo_band,
    platform_place_tags,
)
from ai_assistant.job_analyzer import DeepAnalysisResult
from ai_assistant.matcher import _coerce_profile, _hard_constraints
from ai_assistant.remote_filter import classify_work_format, is_strictly_remote
from ai_assistant.schema import Vacancy

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DB = REPO_ROOT / "state.db"


def _has_state_db() -> bool:
    return STATE_DB.exists() and STATE_DB.stat().st_size > 0

ANALYZER_VERSION = "t74-test"


# --- fixtures ---------------------------------------------------------------

def _vac(
    *,
    sid: str = "t74-1",
    source: str = "custom",
    title: str = "Support Engineer",
    description: str = "",
    location: str | None = None,
    company: str = "Acme",
) -> Vacancy:
    return Vacancy(
        source=source,
        source_job_id=sid,
        title=title,
        company=company,
        description=description,
        job_url=f"https://example.com/{source}/{sid}",
        location=location,
        country_restrictions=[],
        timezone_restrictions=[],
        salary_min=None,
        salary_max=None,
        salary_currency=None,
        employment_type="Full Time",
    )


def _profile() -> CandidateProfile:
    return CandidateProfile(
        desired_roles=["Support Engineer"],
        alternative_roles=[],
        skills=["python"],
        preferred_seniority=[],
        remote_required=True,
        allowed_locations=["Remote"],
        allowed_timezones=[],
        languages=[],
        employment_types=[],
        minimum_salary=1000,
        salary_currency="USD",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )


# The real WeWorkRemotely rows, copied verbatim from the board (state.db), so the
# defect is reproduced with the employer's own wording rather than an invented one.
VXI_TITLE = "VXI Global Solutions: US Remote Technical Support Advisor"
VXI_LOCATION = "Anywhere in the World, \U0001f1fa\U0001f1f8 United States of America, California"
VXI_DESCRIPTION = (
    "Headquarters: Los Angeles, CA URL: https://vxi.com/ Location: Remote "
    "Join Us as a Technical Support Advisor! ... Diagnose, troubleshoot, and resolve "
    "issues related to iOS, macOS, smartphones, tablets, or PCs. ... "
    "Competitive Pay: $17.31 per hour, with opportunities for performance-based incentives."
)

PORKBUN_TITLE = (
    "Porkbun: Live Technical Support Representative - Full-Time - $40,000/year + Benefits"
)
PORKBUN_LOCATION = "Anywhere in the World, \U0001f1fa\U0001f1f8 United States of America, Oregon"
PORKBUN_DESCRIPTION = (
    "Headquarters: Oregon USA URL: https://porkbun.com "
    "We're looking to add multiple U.S.-based, full-time Technical Support Representatives. "
    "Applicants must be based in the USA and eligible for employment."
)

# The Habr Career RSS summary template, verbatim.
HABR_TEMPLATE = (
    "Компания «Авито» ищет хорошего специалиста на вакансию «Системный администратор». "
    "Полный рабочий день. Можно удалённо. Требуемые навыки: #linux, #windows."
)


# --- T7.4-1: the employer's restriction outranks the board's label -----------

def test_vxi_us_restriction_is_not_read_as_worldwide():
    """VXI: the board says "Anywhere in the World", the employer says US Remote."""
    vac = _vac(
        sid="vxi-global-solutions-us-remote-technical-support-advisor",
        source="weworkremotely",
        title=VXI_TITLE,
        description=VXI_DESCRIPTION,
        location=VXI_LOCATION,
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_band == GeoBand.C
    assert res.eligibility == EligibilityStatus.INELIGIBLE
    assert res.geo_scope == GeoScope.COUNTRY_SPECIFIC
    assert "US" in res.matched_countries
    # The board label is still visible, and the contradiction is stated.
    assert res.geo_conflict is True
    assert any("US Remote" in r for r in res.geo_band_reasons)


def test_porkbun_us_residency_requirement_is_not_ignored():
    """Porkbun: "Applicants must be based in the USA" must not survive as eligible."""
    vac = _vac(
        sid="porkbun-live-technical-support-representative",
        source="weworkremotely",
        title=PORKBUN_TITLE,
        description=PORKBUN_DESCRIPTION,
        location=PORKBUN_LOCATION,
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_band == GeoBand.C
    assert res.eligibility == EligibilityStatus.INELIGIBLE
    assert res.geo_scope == GeoScope.COUNTRY_SPECIFIC
    assert "US" in res.matched_countries
    assert any("must be based in the USA" in r for r in res.geo_band_reasons)


def test_the_two_probe_rows_in_the_live_database_are_band_c():
    """Same two rows, read from the real database, so the fix is not fixture-only."""
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM vacancies WHERE stable_id LIKE '%vxi-global-solutions%' "
            "OR stable_id LIKE '%porkbun%'"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        pytest.skip("probe vacancies are not in this database")

    from ai_assistant.db import _row_to_vacancy

    for row in rows:
        vac = _row_to_vacancy(row)
        res = assess_vacancy_eligibility(vac, candidate_country="TH")
        assert res.geo_band == GeoBand.C, f"{vac.stable_id()} is {res.geo_band}"
        assert res.eligibility == EligibilityStatus.INELIGIBLE
        assert res.geo_conflict is True, f"{vac.stable_id()} lost the conflict report"


def test_a_board_label_can_never_create_band_a():
    """Three remote-only boards, no employer statement: B, never A."""
    for source in ("remoteok", "weworkremotely", "himalayas"):
        vac = _vac(
            sid=f"label-only-{source}",
            source=source,
            title="Senior Support Engineer",
            description="Maintain the support queue and answer tickets.",
            location="Anywhere in the World",
        )
        res = assess_vacancy_eligibility(vac, candidate_country="TH")
        assert res.geo_band == GeoBand.B, source
        assert res.geo_evidence == GeoEvidence.PLATFORM, source
        assert res.eligibility == EligibilityStatus.UNKNOWN, source


def test_unknown_geography_is_band_b_and_never_band_a():
    vac = _vac(
        sid="no-geo",
        source="custom",
        title="Python Engineer",
        description="Удаленная работа. Разработка микросервисов на Python.",
        location="Remote",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_band == GeoBand.B
    assert res.eligibility == EligibilityStatus.UNKNOWN
    assert res.geo_evidence == GeoEvidence.NONE


def test_the_employers_own_permission_does_create_band_a():
    vac = _vac(
        sid="employer-permits",
        source="weworkremotely",
        title="Head of Technical Support",
        description="Work from anywhere, on your schedule. We hire worldwide contractors.",
        location="Anywhere in the World",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_band == GeoBand.A
    assert res.geo_evidence == GeoEvidence.EMPLOYER
    assert res.eligibility in (
        EligibilityStatus.ELIGIBLE,
        EligibilityStatus.ELIGIBLE_WITH_WARNING,
    )


def test_a_restriction_never_becomes_band_a_through_board_metadata():
    """The board says "anywhere", the employer says "US only" -- the employer wins."""
    vac = _vac(
        sid="conflict",
        source="weworkremotely",
        title="Backend Engineer",
        description="Fully remote role. US only. Candidates must be located in the US.",
        location="Anywhere in the World",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_band == GeoBand.C
    assert res.geo_conflict is True
    assert any("GEO_CONFLICT" in r for r in res.eligibility_reasons)


def test_a_foreign_place_in_the_board_location_field_lowers_the_band():
    """The board's structured location field names a country the candidate is not in."""
    vac = _vac(
        sid="board-place",
        source="himalayas",
        title="Support Engineer",
        description="Join our support team. Remote position with flexible hours.",
        location="United States",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.platform_places
    assert res.geo_band in (GeoBand.B, GeoBand.C)
    assert res.geo_band != GeoBand.A


def test_band_a_always_settles_the_geography():
    """A band-A verdict must never be reported together with an unknown geography."""
    vac = _vac(
        sid="a-not-unknown",
        source="custom",
        title="Automation Engineer",
        description="Fully remote. We hire globally and you may work from any country.",
        location=None,
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_band == GeoBand.A
    assert res.eligibility != EligibilityStatus.UNKNOWN


def test_an_unconfirmed_format_keeps_a_band_a_vacancy_out_of_the_queue():
    """Geography and format are independent gates; both must pass.

    A posting can permit any country and still fail the zero-tolerance remote
    format check. T7.4 must not let the geographic verdict imply the format.
    """
    vac = _vac(
        sid="a-format-unknown",
        source="custom",
        title="Automation Engineer",
        description="We hire globally and you may work from any country.",
        location=None,
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_band == GeoBand.A
    assert res.remote_mode == RemoteMode.UNKNOWN
    assert res.eligibility == EligibilityStatus.UNKNOWN
    assert any("UNKNOWN_REMOTE" in r for r in res.eligibility_reasons)


# --- T7.4-2: Habr Career's structural format marker --------------------------

def test_habr_structural_marker_is_not_a_false_rejection():
    """The board's own "Можно удалённо." sentence must confirm the remote format."""
    ok, reason = classify_work_format(
        title="Требуется «Системный администратор»",
        description=HABR_TEMPLATE,
        location="Удаленно",
        source="habrcareer",
    )
    assert ok is True
    assert "structural" in reason


def test_habr_structural_marker_does_not_confirm_thailand():
    """Confirmed remote format is not a permission to work from Thailand."""
    vac = _vac(
        sid="1000168291",
        source="habrcareer",
        title="Требуется «Системный администратор»",
        description=HABR_TEMPLATE,
        location="Удаленно",
        company="Авито",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.remote_mode == RemoteMode.REMOTE
    assert res.geo_band == GeoBand.B
    assert res.eligibility == EligibilityStatus.UNKNOWN
    # The format marker carries no geographic evidence at all.
    assert res.platform_places == []
    assert res.geo_evidence == GeoEvidence.NONE


def test_habr_marker_rule_is_scoped_to_the_source_and_the_template():
    """A bare "Можно удалённо" is only structural on the board that publishes it."""
    ok, reason = classify_work_format(
        title="x",
        description="Полный рабочий день. Можно удалённо.",
        location=None,
        source="habrcareer",
    )
    assert ok is False
    assert "Rejected non-remote pattern" in reason

    ok, reason = classify_work_format(
        title="x",
        description=HABR_TEMPLATE,
        location="Удаленно",
        source="custom",
    )
    assert ok is False
    assert "Rejected non-remote pattern" in reason


def test_habr_conditional_remote_in_employer_prose_is_still_rejected():
    """Remote allowed only after a condition is not the board's format marker."""
    text = (
        "Компания «X» ищет хорошего специалиста на вакансию «Y». "
        "Работать можно удалённо после испытательного срока. Полный рабочий день."
    )
    ok, reason = classify_work_format(
        title="x", description=text, location="Удаленно", source="habrcareer"
    )
    assert ok is False
    assert "Rejected non-remote pattern" in reason


def test_habr_office_vacancy_is_still_rejected():
    text = (
        "Компания «X» ищет хорошего специалиста на вакансию «Y». "
        "Работа в офисе, Москва. Полный рабочий день."
    )
    ok, _ = classify_work_format(
        title="x", description=text, location="Москва", source="habrcareer"
    )
    assert ok is False


def test_habr_office_row_in_another_country_is_band_c():
    """Office presence in Russia is C, not merely "unknown"."""
    vac = _vac(
        sid="1000168826",
        source="habrcareer",
        title="Требуется «Менеджер по продажам»",
        description=(
            "Компания «ЛАНИТ» ищет хорошего специалиста на вакансию «Менеджер по продажам». "
            "Москва (Россия). Полный рабочий день. Требуемые навыки: #продажи."
        ),
        location="Москва",
        company="ЛАНИТ",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_band == GeoBand.C
    assert res.eligibility == EligibilityStatus.INELIGIBLE


def test_live_habrcareer_rows_are_no_longer_all_refused():
    """Before T7.4 no Habr Career row was recognised as remote; now some are."""
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM vacancies WHERE source = 'habrcareer' LIMIT 400"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        pytest.skip("no habrcareer vacancies in this database")

    from ai_assistant.db import _row_to_vacancy

    remote_ok = 0
    band_a = 0
    for row in rows:
        vac = _row_to_vacancy(row)
        is_rem, _ = is_strictly_remote(vac)
        remote_ok += 1 if is_rem else 0
        res = assess_vacancy_eligibility(vac, candidate_country="TH")
        band_a += 1 if res.geo_band == GeoBand.A else 0

    assert remote_ok > 0, "the structural marker fix did not recognise any habrcareer row"
    assert band_a == 0, "a Russian-market posting must never be band A"


# --- the band must hold as an invariant --------------------------------------

def test_band_c_always_means_ineligible():
    """C is the one band that must never be offered; check it across the live set."""
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM vacancies LIMIT 600").fetchall()
    finally:
        conn.close()

    from ai_assistant.db import _row_to_vacancy

    checked = 0
    for row in rows:
        vac = _row_to_vacancy(row)
        res = assess_vacancy_eligibility(vac, candidate_country="TH")
        checked += 1
        if res.geo_band == GeoBand.C:
            assert res.eligibility == EligibilityStatus.INELIGIBLE, vac.stable_id()
    assert checked > 0


def test_band_a_is_never_unknown():
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM vacancies LIMIT 600").fetchall()
    finally:
        conn.close()

    from ai_assistant.db import _row_to_vacancy

    for row in rows:
        vac = _row_to_vacancy(row)
        res = assess_vacancy_eligibility(vac, candidate_country="TH")
        if res.geo_band == GeoBand.A:
            assert res.eligibility != EligibilityStatus.UNKNOWN, vac.stable_id()


# --- unit level: place tags ---------------------------------------------------

def test_platform_place_tags_ignore_format_labels():
    assert platform_place_tags("Удаленно") == []
    assert platform_place_tags("Remote") == []
    assert platform_place_tags("Anywhere in the World") == []
    assert platform_place_tags(None) == []


def test_platform_place_tags_read_countries_and_cyrillic_cities():
    assert "United States" in platform_place_tags("Anywhere in the World, United States")
    assert "Москва" in platform_place_tags("Москва")
    assert "RU/CIS" in platform_place_tags("Новосибирск")


def test_place_tags_are_anchored_on_word_boundaries():
    """A country name must not be found inside a longer word.

    "Prussia" contains "russia" and "us"; neither may be reported. "Belarus" is a
    real country and must be reported as itself, not as the "us" inside it.
    "Thailand" is deliberately not a "foreign place": it is where the candidate
    lives, and it is handled by its own branch.
    """
    assert platform_place_tags("Prussia") == []
    assert platform_place_tags("Belarus") == ["Belarus"]
    assert platform_place_tags("Campus") == []
    assert platform_place_tags("Thailand") == []
    assert platform_place_tags("Remote, US")


def test_classify_geo_band_reports_its_evidence():
    verdict = classify_geo_band(
        title="Support Engineer",
        description="Work from anywhere in the world.",
        location="Anywhere in the World",
        remote_mode=RemoteMode.REMOTE,
    )
    assert verdict.band == GeoBand.A
    assert verdict.evidence == GeoEvidence.EMPLOYER
    assert verdict.conflict is False


# --- the band reaches the real selection and submission paths -----------------

def test_hard_constraints_refuse_band_c():
    """application_queue, browser_executor and watcher all go through this gate."""
    profile = _coerce_profile(_profile())
    vac = _vac(
        sid="vxi-gate",
        source="weworkremotely",
        title=VXI_TITLE,
        description=VXI_DESCRIPTION,
        location=VXI_LOCATION,
    )
    rejected, reason = _hard_constraints(profile, vac)
    assert rejected is True
    assert "band C" in reason


def test_hard_constraints_still_accept_a_band_a_vacancy():
    profile = _coerce_profile(_profile())
    vac = _vac(
        sid="a-gate",
        source="weworkremotely",
        title="Support Engineer",
        description="Work from anywhere. Fully remote.",
        location="Anywhere in the World",
    )
    rejected, reason = _hard_constraints(profile, vac)
    assert rejected is False, reason


def test_the_queue_admits_band_a_and_refuses_band_b_and_c():
    """End to end: only the employer-permitted vacancy reaches the queue."""
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        config.DB_FILE = str(Path(tmp) / "t74.db")
        db.init_db()

        prof = _profile()
        prof_path = Path(tmp) / "profile.json"
        prof_path.write_text(json.dumps(prof.to_dict()), encoding="utf-8")

        vac_a = _vac(
            sid="queue-a",
            source="weworkremotely",
            title="Support Engineer",
            description="Work from anywhere. Fully remote support role. Python.",
            location="Anywhere in the World",
        )
        vac_b = _vac(
            sid="queue-b",
            source="remoteok",
            title="Support Engineer",
            description="Remote support role. Python.",
            location="Worldwide",
        )
        vac_c = _vac(
            sid="queue-c",
            source="weworkremotely",
            title="Support Engineer",
            description="Remote support role. Applicants must be based in the USA. Python.",
            location="Anywhere in the World",
        )

        deep = DeepAnalysisResult(
            fit_score=90,
            recommendation="APPLY",
            why_fit=["fit"],
            gaps=[],
            must_have_requirements=["python"],
            nice_to_have_requirements=[],
            matched_skills=["python"],
            missing_skills=[],
            seniority_assessment="",
            remote_assessment="",
            salary_assessment="",
            resume_adaptation_needed=False,
            resume_adaptation_reasons=[],
            application_strategy="apply",
        )

        for vac in (vac_a, vac_b, vac_c):
            db.save_vacancy(vac)
            db.save_deep_analysis(
                vac.stable_id(), ANALYZER_VERSION, 90, "APPLY", deep.model_dump_json()
            )
            set_application_status(
                vac.stable_id(),
                ApplicationStatus.READY_TO_APPLY,
                company=vac.company,
                title=vac.title,
                source=vac.source,
                vacancy_url=vac.job_url,
            )

        from ai_assistant.application_queue import generate_queue

        ids = {i.vacancy_stable_id for i in generate_queue(top_n=10, profile_path=str(prof_path))}

        assert vac_a.stable_id() in ids, "band A must reach the queue"
        assert vac_b.stable_id() not in ids, "band B must not be auto-approved"
        assert vac_c.stable_id() not in ids, "band C must not reach the queue"
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_search_cycle_is_still_separated_from_submission():
    """T7.4 must not have loosened the submit fuses."""
    from ai_assistant import cli
    from ai_assistant.hh_autonomous_agent import AutonomousConfig

    assert AutonomousConfig().submit_enabled is False
    assert cli.submit_vacancy("t74-nonexistent", confirm_submit=False) == 1


# --- T7.4 follow-up: a place name is not permission, and a multi-country ----
# --- board label is not a contradiction                                      ---
#
# Found while building the shortlist: five Lemon.io rows came out band A while
# carrying geo_conflict, i.e. the classifier recorded a contradiction and then
# auto-approved anyway. Two separate causes, both fixed here.

# The board renders a multi-country scope as one field: an openness tag followed
# by the countries the employer can hire in. This is how the real Lemon.io rows
# arrive from WeWorkRemotely (verbatim shape, shortened list).
LEMON_LOCATION = (
    "Anywhere in the World, \U0001f1e6\U0001f1f7 Argentina, \U0001f1e9\U0001f1ea Germany, "
    "\U0001f1f9\U0001f1ed Thailand, \U0001f1fa\U0001f1f8 United States of America, Delaware"
)
# The employer's own description names Thailand among the countries it hires in.
LEMON_DESCRIPTION = (
    "Fully remote contract work. We can hire contractors in these regions: "
    "Asia (South Korea, the Philippines, Indonesia, Malaysia, Vietnam, Thailand, "
    "and Israel), Oceania (Australia, New Zealand, and Papua New Guinea). "
    "Senior Java & React Developer."
)


def test_a_multi_country_board_label_is_not_a_contradiction_by_itself():
    """Openness plus country tags in one field is a rendering, not a conflict.

    It used to be recorded as "the board label contradicts itself", which flagged
    58 live rows and would have blocked a genuine band A.
    """
    vac = _vac(
        sid="multi-country-label",
        source="weworkremotely",
        title="Senior Java & React Developer",
        description="Fully remote. Build services with Java and React.",
        location=LEMON_LOCATION,
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_conflict is False
    assert res.geo_conflict_details == []
    # And it still does not become A: only the board's field names Thailand.
    assert res.geo_band == GeoBand.B
    assert res.eligibility == EligibilityStatus.UNKNOWN


def test_the_employers_own_country_list_including_thailand_is_band_a():
    """The employer's own words name Thailand, so the geography is settled."""
    vac = _vac(
        sid="lemon-io-shape",
        source="weworkremotely",
        title="Lemon.io: Senior Java & React Developer",
        description=LEMON_DESCRIPTION,
        location=LEMON_LOCATION,
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_band == GeoBand.A
    assert res.geo_evidence == GeoEvidence.EMPLOYER
    assert res.geo_conflict is False, "a rendering artefact must not read as a conflict"
    assert res.eligibility == EligibilityStatus.ELIGIBLE


def test_a_negated_thailand_mention_is_not_permission():
    """"Unable to hire in Thailand" must not lift the row to band A.

    ``_THAILAND_PATTERNS`` is a bare place name, so the negation guard is what
    keeps a refusal from reading as a permission.
    """
    for phrase in (
        "We are unable to hire in Thailand.",
        "This role is open in most countries, excluding Thailand.",
        "Not available in Thailand.",
        "We cannot employ anyone based in Thailand.",
    ):
        vac = _vac(
            sid="negated-thailand",
            source="weworkremotely",
            title="Technical Support Specialist",
            description=f"Fully remote. {phrase}",
            location="Anywhere in the World",
        )
        res = assess_vacancy_eligibility(vac, candidate_country="TH")

        assert res.geo_band != GeoBand.A, phrase
        assert res.eligibility != EligibilityStatus.ELIGIBLE, phrase


def test_the_negation_guard_reads_the_employers_own_text():
    """Unit level: the guard is about the neighbourhood of the mention."""
    from ai_assistant.eligibility import employer_allows_thailand

    assert employer_allows_thailand("Hiring in Vietnam, Thailand, and Israel.") is True
    assert employer_allows_thailand("Bangkok-based team, fully remote.") is True
    assert employer_allows_thailand("We are unable to hire in Thailand.") is False
    assert employer_allows_thailand("Excluding Thailand.") is False
    assert employer_allows_thailand("No candidates from Thailand.") is False
    assert employer_allows_thailand("Не можем нанимать в Таиланде.") is False
    assert employer_allows_thailand("Fully remote, no country restrictions.") is False


def test_conflicting_evidence_is_never_auto_approved():
    """Structural invariant: a recorded conflict can never carry an approval.

    The guard sits in ``_result``, so it holds for every return path rather than
    only for the paths that happen to end on band C today.
    """
    # VXI: the board's permissive label against the employer's US restriction.
    vac = _vac(
        sid="conflict-is-not-approval",
        source="weworkremotely",
        title=VXI_TITLE,
        description=VXI_DESCRIPTION,
        location=VXI_LOCATION,
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_conflict is True
    assert res.eligibility not in (
        EligibilityStatus.ELIGIBLE,
        EligibilityStatus.ELIGIBLE_WITH_WARNING,
    )


def test_no_live_conflict_row_is_auto_approved():
    """The same invariant across the live set, not just the crafted fixture."""
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM vacancies LIMIT 600").fetchall()
    finally:
        conn.close()

    from ai_assistant.db import _row_to_vacancy

    seen_conflict = 0
    for row in rows:
        vac = _row_to_vacancy(row)
        res = assess_vacancy_eligibility(vac, candidate_country="TH")
        if res.geo_conflict:
            seen_conflict += 1
            assert res.eligibility not in (
                EligibilityStatus.ELIGIBLE,
                EligibilityStatus.ELIGIBLE_WITH_WARNING,
            ), vac.stable_id()
    assert seen_conflict > 0, "the live set must still exercise the conflict path"


def test_a_place_in_a_remote_phrasing_never_reaches_band_a():
    """A measured precision gap, pinned so that it can never become an approval.

    WeWorkRemotely renders the employer's location statement as
    "Headquarters: Remote - United States". The restriction patterns do not read
    that phrasing, so such a row lands at band B rather than band C. Measured on
    the live set on 2026-09-25: 150 rows match the phrasing, 69 of them sit at
    band B, and none of them reaches band A. Band B is not auto-approved, so the
    safe direction holds; this test keeps it that way until the phrasing is read
    properly, rather than letting the gap widen silently.
    """
    for header, place in (
        ("Headquarters: Remote - United States URL: https://example.com", "United States"),
        ("Headquarters: Remote - USA Ready to do the most impactful work", "United States of America"),
        ("Headquarters: Remote, UK About us we build tools", "United Kingdom of Great Britain and Northern Ireland"),
    ):
        vac = _vac(
            sid="remote-place-phrasing",
            source="weworkremotely",
            title="Technical Support Specialist",
            description=f"{header} Fully remote. Provide technical support.",
            location=f"Anywhere in the World, {place}",
        )
        res = assess_vacancy_eligibility(vac, candidate_country="TH")

        assert res.geo_band != GeoBand.A, header
        assert res.eligibility not in (
            EligibilityStatus.ELIGIBLE,
            EligibilityStatus.ELIGIBLE_WITH_WARNING,
        ), header
