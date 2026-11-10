"""T7.7 — geographic phrases read in context: object of work, and stated place.

Two defects, both measured on live HH rows on 2026-09-25.

T7.7-1 (Servers.com, ``hh:137574152``). The posting contains

    "system administration of servers from anywhere in the world"
    "администрировать серверы из любой точки мира"

Those sentences say where the *servers* can be reached from. They say nothing
about where the employee may live -- and the same posting names the position's
country three times:

    "Technical Support Engineer (Serbia)"
    "We are looking for a technical support engineer in Serbia."
    "Ищем в команду инженера технической поддержки в Сербии."

The worldwide pattern read the words and returned band A (``eligible``); nothing
in the module read any of the three Serbia statements. Fixed: a worldwide phrase
whose object is equipment, infrastructure, clients or systems is no longer
permission, and a place of work stated for the position itself is read.

T7.7-2 (YCLIENTS, ``hh:137448071``).

    "удаленный формат работы - строго в пределах Российской Федерации"

is a hard geographic restriction. ``_EMPLOYER_RESTRICTION_PATTERNS`` knew
"только на территории РФ" and the abbreviation but not the full name, so the row
was reported as merely unknown (band B) instead of ruled out (band C). Fixed: the
work-territory form and the full name of the federation are read.

Both fixes must not narrow the other direction, and the pins below hold them to
that: a posting that genuinely permits any country keeps its band A, a
headquarters address does not become a work restriction, and the T7.4 checks
(VXI, Porkbun, Habr Career) are re-asserted from this file so a future edit here
cannot quietly undo them.

Band vocabulary:

    A -- the employer explicitly allows Thailand or any country;
    B -- remote, but availability from Thailand is not established;
    C -- the employer restricts to another country, requires office presence,
         or the board's label contradicts the employer.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

import pytest

from ai_assistant import config, db
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.eligibility import (
    EligibilityStatus,
    GeoBand,
    GeoEvidence,
    RemoteMode,
    assess_vacancy_eligibility,
    classify_geo_band,
    geo_band_permits_auto_send,
)
from ai_assistant.job_analyzer import DeepAnalysisResult
from ai_assistant.matcher import (
    _coerce_profile,
    _hard_constraints,
    _hard_constraints_for_send,
)
from ai_assistant.schema import Vacancy

REPO_ROOT = Path(__file__).resolve().parents[1]

# The T7.4 fixtures live next door; re-using them (rather than copying the text)
# is what makes "the T7.4 checks are preserved" a real assertion instead of a
# second, drifting copy of the same strings.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_t74_geo_band import (
    HABR_TEMPLATE,
    PORKBUN_DESCRIPTION,
    PORKBUN_LOCATION,
    PORKBUN_TITLE,
    VXI_DESCRIPTION,
    VXI_LOCATION,
    VXI_TITLE,
)

ANALYZER_VERSION = "t77-test"


# --- fixtures ---------------------------------------------------------------

def _vac(
    *,
    sid: str = "t77-1",
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


def _band(*, title: str, description: str, location: str | None = None) -> GeoBand:
    return classify_geo_band(
        title=title,
        description=description,
        location=location,
        remote_mode=RemoteMode.REMOTE,
    ).band


# The live Servers.com text, shortened to the parts the decision rests on. Both
# languages are kept: the posting ships the English and the Russian body one
# after the other, and the phrase has to be caught in either.
SERVERS_TITLE = "Technical Support Engineer (Serbia)"
SERVERS_SERVERS_EN = "system administration of servers from anywhere in the world"
SERVERS_SERVERS_RU = "администрировать серверы из любой точки мира"
SERVERS_ROLE_EN = "We are looking for a technical support engineer in Serbia."
SERVERS_ROLE_RU = "Ищем в команду инженера технической поддержки в Сербии."


# --- T7.7-1: a worldwide phrase about the work object is not permission -------

@pytest.mark.parametrize(
    "phrase",
    [
        SERVERS_SERVERS_EN,
        SERVERS_SERVERS_RU,
        "diagnose hardware issues from anywhere in the world",
        "manage your clients from anywhere",
        "watch the infrastructure from anywhere in the world",
        "работать с мониторингом оборудования из любой точки мира",
    ],
)
def test_access_to_the_work_object_is_not_a_permission_to_live_anywhere(phrase):
    """The object of the work is not the employee's location."""
    band = _band(title="Support Engineer", description=f"We will teach you to {phrase}.")
    assert band != GeoBand.A, phrase


def test_the_rejected_phrase_is_reported_rather_than_dropped():
    """Silent suppression would look exactly like "the text says nothing"."""
    verdict = classify_geo_band(
        title="Support Engineer",
        description=SERVERS_SERVERS_EN,
        location=None,
        remote_mode=RemoteMode.REMOTE,
    )
    assert verdict.band == GeoBand.B
    assert any("GEO_NOTE" in r for r in verdict.reasons), verdict.reasons
    assert any("servers" in r for r in verdict.reasons), verdict.reasons


@pytest.mark.parametrize(
    "description",
    [
        "Work from anywhere in the world.",
        "Work from anywhere, on your schedule.",
        "We hire globally and you may work from any country.",
        "You can work from any location.",
        "Remote worldwide.",
        "Сотрудник может работать из любой страны.",
        "Мы нанимаем по всему миру.",
    ],
)
def test_a_genuine_worldwide_permission_still_reaches_band_a(description):
    """The guard must narrow the phrase, not the permission.

    Every string here is about the *candidate*. Removing them from band A would
    turn the fix into a blanket ban and lose the vacancies it exists to find.
    """
    band = _band(title="Support Engineer", description=description)
    assert band == GeoBand.A, description


def test_a_posting_that_says_both_keeps_its_band_a():
    """Object-of-work phrase first, real permission second: still permission.

    ``_employee_worldwide_match`` skips the rejected match and keeps looking, so
    the order of the two sentences must not decide the verdict.
    """
    description = (
        f"You will learn to {SERVERS_SERVERS_EN}. "
        "We are a remote-first company and you may work from anywhere."
    )
    assert _band(title="Support Engineer", description=description) == GeoBand.A


def test_the_guard_does_not_fire_on_the_verb_work():
    """A counter-check on the window: "work" is the verb, not the object."""
    for description in (
        "Work from anywhere. Fully remote.",
        "We hire globally and you may work from any country.",
    ):
        assert _band(title="Support Engineer", description=description) == GeoBand.A


# --- T7.7-2: the explicit RF work-territory restriction ----------------------

@pytest.mark.parametrize(
    "phrase",
    [
        "удаленный формат работы - строго в пределах Российской Федерации",
        "Работа строго в пределах Российской Федерации.",
        "Формат работы: в пределах РФ.",
        "Только на территории Российской Федерации.",
        "Требуется гражданство Российской Федерации.",
        "Налоговый резидент Российской Федерации.",
        "Только для резидентов РФ.",
    ],
)
def test_an_explicit_rf_restriction_is_band_c(phrase):
    """Both spellings of the jurisdiction, and the work-territory form."""
    band = _band(title="Инженер", description=f"Удаленная работа. {phrase}")
    assert band == GeoBand.C, phrase


def test_the_full_name_is_read_exactly_like_the_abbreviation():
    """The gap the defect lived in: only the abbreviation was known."""
    assert _band(title="Инженер", description="Только на территории РФ.") == GeoBand.C
    assert _band(title="Инженер", description="Только на территории России.") == GeoBand.C


# --- T7.7-2b: where the employer says the position is ------------------------

@pytest.mark.parametrize(
    "title, description",
    [
        (SERVERS_TITLE, "Join our team."),
        ("Support Engineer", SERVERS_ROLE_EN),
        ("Инженер поддержки", SERVERS_ROLE_RU),
        ("Support Engineer", "The position is based in Serbia."),
        ("Support Engineer", "Location: Serbia"),
        ("Support Engineer", "Место работы: Сербия"),
    ],
)
def test_a_stated_place_of_work_is_band_c(title, description):
    """A place named for the role itself answers the same question as a rule."""
    band = _band(title=title, description=description)
    assert band == GeoBand.C, (title, description)


def test_a_headquarters_address_is_not_a_work_restriction():
    """The task's explicit warning: an HQ country is not a work restriction.

    The first two are the T7.4 rows; their band comes from the employer's own
    rule ("US Remote", "must be based in the USA"), not from the address.
    """
    for description in (
        "Headquarters: Oregon USA URL: https://porkbun.com We build tools.",
        "Headquarters: Los Angeles, CA URL: https://example.com Location: Remote",
        "Our head office is in Warsaw. We are a fully distributed team.",
    ):
        band = _band(title="Support Engineer", description=description)
        assert band != GeoBand.C, description


def test_the_servers_posting_is_band_c_end_to_end():
    """The live row, both languages, exactly as the decision has to read it."""
    description = (
        f"{SERVERS_ROLE_EN}\n\n"
        "Servers.com is an international hosting provider with data centers in the US, "
        "Europe and Asia.\n\n"
        "We will welcome you to our team if you want to learn:\n\n"
        f"{SERVERS_SERVERS_EN}\n\n"
        f"{SERVERS_ROLE_RU}\n\n"
        f"{SERVERS_SERVERS_RU}"
    )
    vac = _vac(sid="137574152", source="hh", title=SERVERS_TITLE, description=description)
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_band == GeoBand.C
    assert res.eligibility == EligibilityStatus.INELIGIBLE
    assert res.geo_evidence == GeoEvidence.EMPLOYER
    assert any("Serbia" in r for r in res.geo_band_reasons), res.geo_band_reasons
    assert any("GEO_NOTE" in r for r in res.geo_band_reasons), res.geo_band_reasons


def test_the_yclients_posting_is_band_c_end_to_end():
    """The live row whose restriction the module used to read as "unknown"."""
    vac = _vac(
        sid="137448071",
        source="hh",
        title="Специалист по сопровождению и автоматизации CRM",
        description=(
            "Полный рабочий день, график 5\\2 с 10:00-19:00 по мск, удаленный формат "
            "работы - строго в пределах Российской Федерации."
        ),
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")

    assert res.geo_band == GeoBand.C
    assert res.eligibility == EligibilityStatus.INELIGIBLE
    assert any("пределах Российской Федерации" in r for r in res.geo_band_reasons)


# --- the rule, and the two gates that have to agree on it --------------------

def test_only_band_a_may_be_sent_without_a_human():
    """The predicate, one row per band."""
    rows = {
        GeoBand.A: _vac(
            sid="pred-a",
            source="weworkremotely",
            description="Work from anywhere. Fully remote.",
            location="Anywhere in the World",
        ),
        GeoBand.B: _vac(
            sid="pred-b",
            source="remoteok",
            description="Remote support role. Fully remote.",
            location="Worldwide",
        ),
        GeoBand.C: _vac(
            sid="pred-c",
            source="weworkremotely",
            title=SERVERS_TITLE,
            description=f"Fully remote. {SERVERS_ROLE_EN}",
            location="Anywhere in the World",
        ),
    }
    for expected_band, vac in rows.items():
        res = assess_vacancy_eligibility(vac, candidate_country="TH")
        assert res.geo_band == expected_band, (vac.stable_id(), res.geo_band)
        allowed, reason = geo_band_permits_auto_send(res)
        if expected_band == GeoBand.A:
            assert allowed is True and reason == ""
        else:
            assert allowed is False, f"band {expected_band.value} was allowed"
            assert f"band {expected_band.value}" in reason, reason


def test_the_send_gate_refuses_band_b_and_c_and_admits_band_a():
    profile = _coerce_profile(_profile())

    allowed = _vac(
        sid="send-a",
        source="weworkremotely",
        description="Work from anywhere. Fully remote support role.",
        location="Anywhere in the World",
    )
    unknown = _vac(
        sid="send-b",
        source="remoteok",
        description="Remote support role. Fully remote.",
        location="Worldwide",
    )
    refused = _vac(
        sid="send-c",
        source="weworkremotely",
        title=SERVERS_TITLE,
        description=f"Fully remote. {SERVERS_ROLE_EN} You will learn to {SERVERS_SERVERS_EN}.",
        location="Anywhere in the World",
    )

    ok, reason = _hard_constraints_for_send(profile, allowed)
    assert ok is False, reason

    # Band B: the status ladder alone lets it through (UNKNOWN is not a refusal),
    # so this refusal can only come from the geographic send gate.
    rejected, reason = _hard_constraints_for_send(profile, unknown)
    assert rejected is True, f"{unknown.stable_id()} must not be auto-sendable"
    assert "geo band B" in reason, reason

    # Band C: refused, and by the older INELIGIBLE ladder as well.
    rejected, reason = _hard_constraints_for_send(profile, refused)
    assert rejected is True, f"{refused.stable_id()} must not be auto-sendable"
    assert "band C" in reason or "geo band C" in reason, reason


def test_analysis_is_not_narrowed_by_the_send_gate():
    """Band B must stay readable.

    ``eligibility_review`` offers band B rows for a human to look at, and the
    shortlist is built from them. If the send gate's rule leaked into
    ``_hard_constraints``, every band B row would become INELIGIBLE and the
    review list would empty out -- which is why the two paths use two names.
    """
    profile = _coerce_profile(_profile())
    unknown = _vac(
        sid="analyse-b",
        source="remoteok",
        description="Remote support role. Fully remote.",
        location="Worldwide",
    )
    assert _hard_constraints(profile, unknown)[0] is False
    assert _hard_constraints_for_send(profile, unknown)[0] is True


def test_the_queue_admits_band_a_and_refuses_band_b_and_c():
    """End to end through the real ``generate_queue``."""
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        config.DB_FILE = str(Path(tmp) / "t77.db")
        db.init_db()

        prof = _profile()
        prof_path = Path(tmp) / "profile.json"
        prof_path.write_text(json.dumps(prof.to_dict()), encoding="utf-8")

        vac_a = _vac(
            sid="q-a",
            source="weworkremotely",
            description="Work from anywhere. Fully remote support role. Python.",
            location="Anywhere in the World",
        )
        vac_b = _vac(
            sid="q-b",
            source="remoteok",
            description="Remote support role. Fully remote. Python.",
            location="Worldwide",
        )
        vac_c = _vac(
            sid="q-c",
            source="weworkremotely",
            title=SERVERS_TITLE,
            description=f"Fully remote. {SERVERS_ROLE_EN} {SERVERS_SERVERS_EN}. Python.",
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
        assert vac_b.stable_id() not in ids, "band B must not be auto-sendable"
        assert vac_c.stable_id() not in ids, "band C must not reach the queue"
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)


class _StaleEligible:
    """A stored record claiming the old, optimistic verdict."""

    eligibility = EligibilityStatus.ELIGIBLE
    eligibility_reasons: ClassVar[list[str]] = ["seeded: verdict written before the T7.7 fix"]

    def to_dict(self) -> dict:
        return {
            "eligibility": "eligible",
            "geo_band": "A",
            "geo_band_reasons": ["seeded: verdict written before the T7.7 fix"],
            "geo_scope": "worldwide",
            "remote_mode": "remote",
        }


def test_a_stale_stored_band_cannot_smuggle_a_row_into_the_queue():
    """The stored record is written once and never revisited.

    A row saved under the older geographic rule keeps its optimistic band, and
    ``generate_queue``'s status filter reads exactly that stored value -- so a
    queue that trusts it alone admits the row. The gate now recomputes the
    verdict instead of trusting the record, and this is the test that fails
    without it.

    What it deliberately does **not** claim: the stored record is left as it is.
    ``generate_queue`` rewrites it only when it is missing, and making it rewrite
    always would add lines to a file whose grep line count is pinned at 738 by
    ``tests/test_report_line_refs.py``. The consequence is recorded as a risk
    below, and ``tools/.t77_refresh_eligibility.py`` is the remedy.
    """
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        config.DB_FILE = str(Path(tmp) / "t77_stale.db")
        db.init_db()

        prof = _profile()
        prof_path = Path(tmp) / "profile.json"
        prof_path.write_text(json.dumps(prof.to_dict()), encoding="utf-8")

        # Band B under the current classifier, band A under the old one: the
        # only worldwide phrase is about the servers.
        vac = _vac(
            sid="stale-b",
            source="weworkremotely",
            description="Fully remote support role. You will learn to "
            f"{SERVERS_SERVERS_EN}. Python.",
            location="Anywhere in the World",
        )
        db.save_vacancy(vac)
        assert assess_vacancy_eligibility(vac, candidate_country="TH").geo_band == GeoBand.B

        # Overwrite with the stale, optimistic record the old code would have left.
        db.save_vacancy_eligibility(vac.stable_id(), _StaleEligible())
        assert db.get_vacancy_eligibility(vac.stable_id())["status"] == "eligible"

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
        db.save_deep_analysis(vac.stable_id(), ANALYZER_VERSION, 90, "APPLY", deep.model_dump_json())
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
        assert vac.stable_id() not in ids, "a stale band A record admitted a band B row"
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)


# --- the entry point that clicks, not just the helper that decides ----------

def test_the_real_submit_entry_point_refuses_band_b_and_c():
    """The rule only protects the queue if the function that clicks consults it.

    `_hard_constraints_for_send` is the rule, but a fuse is only real when the
    entry point that can click actually calls it. The assertion is on the
    *reason* as well as the status: BLOCKED alone would also be satisfied by the
    kill switch or the duplicate guard, and the point here is that the refusal
    names the geography.

    There is no band A row in this test on purpose. Band A passes this gate and
    goes on to the browser, and the suite's isolation refuses real socket work -
    the band A control lives in
    `test_the_send_gate_refuses_band_b_and_c_and_admits_band_a`.
    """
    from ai_assistant import browser_executor as be

    for sid, description, expected in (
        ("t77-send-b", "Python, PostgreSQL, FastAPI.", GeoBand.B),
        (
            "t77-send-c",
            "Удаленная работа строго в пределах Российской Федерации.",
            GeoBand.C,
        ),
    ):
        vac = _vac(sid=sid, source="hh", description=description, location="Remote")
        db.save_vacancy(vac)
        assert assess_vacancy_eligibility(vac, candidate_country="TH").geo_band == expected, sid

        res = be.submit_application_in_browser(
            vacancy_stable_id=vac.stable_id(), confirm_submit=True, dry_run=True
        )
        assert res.status == "BLOCKED", (sid, res.status, res.error)
        # Band B is refused by the T7.7 predicate ("geo band B is not
        # auto-sendable"). Band C is refused one step earlier, by the older
        # INELIGIBLE ladder ("Ineligible remote vacancy (band C)"). Both spell the
        # band, which is what has to be true here: the refusal names the geography
        # rather than something incidental.
        assert f"band {expected.value}" in (res.error or ""), (sid, res.error)


def test_the_prepare_path_is_not_geo_gated():
    """Preparing is not sending, and the human review has to stay reachable.

    T7.5 built the manual review list for band B on purpose. If prepare refused
    band B, a vacancy whose geography was never established could never reach a
    human - the approval bridge would be dead code. The fuse sits on the send
    paths; this pins that prepare is not one of them.
    """
    import inspect

    from ai_assistant import browser_executor as be

    src = inspect.getsource(be.prepare_application_in_browser)
    assert "_hard_constraints(_coerce_profile(profile), vac)" in src
    assert "_hard_constraints_for_send" not in src

    submit_src = inspect.getsource(be.submit_application_in_browser)
    assert "_hard_constraints_for_send" in submit_src


# --- the T7.4 checks are preserved ------------------------------------------

def test_the_t74_vxi_row_is_still_band_c():
    vac = _vac(
        sid="vxi-preserved",
        source="weworkremotely",
        title=VXI_TITLE,
        description=VXI_DESCRIPTION,
        location=VXI_LOCATION,
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.geo_band == GeoBand.C
    assert res.eligibility == EligibilityStatus.INELIGIBLE
    assert res.geo_conflict is True


def test_the_t74_porkbun_row_is_still_band_c():
    vac = _vac(
        sid="porkbun-preserved",
        source="weworkremotely",
        title=PORKBUN_TITLE,
        description=PORKBUN_DESCRIPTION,
        location=PORKBUN_LOCATION,
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.geo_band == GeoBand.C
    assert any("must be based in the USA" in r for r in res.geo_band_reasons)


def test_the_t74_habr_row_is_still_band_b():
    """Habr's structural marker confirms the format, never the geography."""
    vac = _vac(
        sid="habr-preserved",
        source="habrcareer",
        title="Требуется «Системный администратор»",
        description=HABR_TEMPLATE,
        location="Удаленно",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.geo_band == GeoBand.B
    assert res.eligibility == EligibilityStatus.UNKNOWN


# --- recorded risks: measured, deliberately NOT fixed here -------------------
#
# T7.7 §4 asks for these to be recorded as separate risks rather than repaired
# inside a geographic hotfix. Each pin states the current behaviour so that the
# day someone fixes it, the test fails and the report gets updated -- an
# unrecorded defect that silently disappears is indistinguishable from one that
# was never there.

def test_recorded_risk_the_queue_gate_recomputes_but_the_record_stays_stale():
    """Admission is safe; the *displayed* band can still be the old one.

    ``generate_queue`` refuses on a freshly computed verdict, so a stale record
    can no longer admit anything. It does not rewrite the record -- it saves only
    when the record is missing -- so ``eligibility_review``, which reads the
    stored assessment, can still show a row under its pre-fix band. The remedy is
    the one-off refresh in ``tools/.t77_refresh_eligibility.py``; making the
    queue rewrite always would add lines to a file whose grep line count
    ``tests/test_report_line_refs.py`` pins at 738.
    """
    import inspect

    from ai_assistant import application_queue

    source = inspect.getsource(application_queue.generate_queue)
    save_sites = source.count("save_vacancy_eligibility(")
    assert save_sites == 2, "the save sites moved; re-measure before trusting this note"
    assert "if not elig:" in source, "the conditional save is gone"


def test_recorded_risk_an_upper_bound_is_read_as_a_lower_bound():
    """``normalize_salary_text("до 85 000 ₽")`` -> min = max = 85 000.

    "до" is a ceiling. Recording it as a floor makes the vacancy look like it
    clears a minimum-salary filter it may not clear at all.
    """
    from ai_assistant.normalizer import normalize_salary_text

    got = normalize_salary_text("до 85 000 ₽")
    assert got["salary_currency"] == "RUB"
    assert got["salary_min"] == 85_000
    assert got["salary_max"] == 85_000


def test_recorded_risk_the_autonomous_default_port_is_not_the_live_one():
    """T7.21 fixed this recorded risk, so the pin moved with it.

    The risk was real: ``AutonomousConfig().cdp_url`` was the historical 9222 -
    a browser nobody had started - while the live browser was BrowserOS on 9110,
    so the daemon defaulted to a dead port. The default is now the headless
    instance the runner starts itself, and the historical endpoint is reachable
    only by asking for ``browseros`` mode explicitly. The risk is not deleted,
    it is *measured in its new shape* - if the default ever drifts back to a
    browser nobody starts, this fails again.
    """
    from ai_assistant.hh_autonomous_agent import AutonomousConfig
    from ai_assistant.hh_browser_launcher import BROWSEROS_CDP_PORT
    from ai_assistant.hh_browser_provider import HEADLESS_CDP_URL

    assert AutonomousConfig().cdp_url == HEADLESS_CDP_URL
    assert HEADLESS_CDP_URL.endswith(":9111")
    assert BROWSEROS_CDP_PORT == 9110


def test_recorded_risk_the_autonomous_cycle_reaches_the_message_agent():
    """Auto-replies are sent by a cycle whose submission is disabled by design.

    ``run_cycle`` raises when ``submit_enabled`` is on, so no application can be
    submitted -- but step 4 runs unconditionally, and
    ``max_auto_replies_per_cycle`` is 3. The cycle is deliberately **not**
    executed here (T7.7 forbids it); the pin is on the shape of the code, which
    is what the risk actually is.
    """
    import inspect

    from ai_assistant.hh_autonomous_agent import AutonomousConfig, AutonomousJobAgent

    cfg = AutonomousConfig()
    assert cfg.submit_enabled is False
    assert cfg.max_auto_replies_per_cycle > 0

    # T7.25 §5 split the cycle body out of `run_cycle` so the browser-ownership
    # wrapper could be the only thing in it. The submit fuse stayed in
    # `run_cycle`, in front of the body; the message agent runs inside the body.
    # The ordering this pins is therefore "fuse first, body second" - pinned as
    # such, rather than by line offsets inside a single function.
    run_source = inspect.getsource(AutonomousJobAgent.run_cycle)
    guard = run_source.index("if self.config.submit_enabled")
    assert "NotImplementedError" in run_source[guard : guard + 300], "the submit fuse is gone"
    body = run_source.index("_cycle_body(")
    assert body > guard, "the cycle body runs before the submit guard"

    body_source = inspect.getsource(AutonomousJobAgent._cycle_body)
    assert "self._process_messages()" in body_source, (
        "the message agent is no longer reached from the cycle body"
    )
