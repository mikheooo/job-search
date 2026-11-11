"""T7.5: the manual-review list must be a separate mode from the submission queue.

The list exists so band B can be *looked at* without being *acted on*. These tests
pin the three properties that make that true:

* band C is absent from the review list;
* band B is present, flagged for manual review, and never auto-queued;
* building the list cannot approve anything -- the module imports no queue writer
  and leaves ``application_queue`` untouched.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

from ai_assistant import config
from ai_assistant.eligibility_review import (
    AUTO_QUEUE_ELIGIBLE_CATEGORIES,
    CATEGORY_LOCAL_THAILAND,
    CATEGORY_REMOTE_CONFIRMED,
    CATEGORY_REMOTE_UNCLEAR,
    DIRECTIONS,
    NEGATIVE_TITLE_PATTERNS,
    REVIEW_BANDS,
    _direction,
    _hard_exclusion_hit,
    _industry_keywords,
    _is_thailand_local,
    _title_is_negative,
    load_hard_exclusions,
    summarise,
)
from ai_assistant.eligibility_review import build_review_list as _build_review_list
from ai_assistant.schema import Vacancy

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DB = REPO_ROOT / "state.db"
MODULE_PATH = REPO_ROOT / "ai_assistant" / "eligibility_review.py"


def _has_state_db() -> bool:
    return STATE_DB.exists() and STATE_DB.stat().st_size > 0


def build_review_list(*, db_path=None, **kwargs):
    """The review list, read from the real database.

    ``conftest``'s autouse isolation redirects ``config.DB_FILE`` to an empty
    temporary database for every test, and the review module deliberately refuses
    to create a database it cannot find -- so a test that wants the live set has
    to name the file. Tests that want a specific database pass ``db_path``.
    """
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")
    return _build_review_list(db_path=db_path or STATE_DB, **kwargs)


def _vac(
    *,
    sid: str = "t75-1",
    source: str = "custom",
    title: str = "Support Engineer",
    description: str = "",
    location: str | None = None,
    company: str = "Acme",
    country_restrictions: list[str] | None = None,
) -> Vacancy:
    return Vacancy(
        source=source,
        source_job_id=sid,
        title=title,
        company=company,
        description=description,
        job_url=f"https://example.com/{source}/{sid}",
        location=location,
        country_restrictions=list(country_restrictions or []),
        timezone_restrictions=[],
        salary_min=None,
        salary_max=None,
        salary_currency=None,
        employment_type="Full Time",
    )


# --- the band C exclusion is real, not accidental ---------------------------

def test_band_c_is_absent_from_the_review_list_by_default():
    """Band C must not appear in the list of suitable vacancies."""
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    items = build_review_list(limit=500)
    assert items, "the live set must produce a review list"
    assert all(i["band"] != "C" for i in items)


def test_the_exclusion_is_a_real_filter_and_not_an_empty_rule():
    """Turning the exclusion off must actually bring band C rows back.

    Without this, the previous test would pass even if the review list were
    empty, or if every record happened to be A or B for unrelated reasons.
    """
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    with_c = build_review_list(limit=500, include_band_c_foreign=True)
    without_c = build_review_list(limit=500)
    c_rows = [i for i in with_c if i["band"] == "C"]
    assert c_rows, "the live set must contain band C rows for this test to mean anything"
    assert len(with_c) > len(without_c)
    assert not any(i["band"] == "C" for i in without_c)


def test_band_c_is_never_auto_queue_allowed_even_when_included():
    """The escape hatch is for tests only, and still never grants the queue."""
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    for item in build_review_list(limit=500, include_band_c_foreign=True):
        if item["band"] == "C":
            assert item["auto_queue_allowed"] is False


# --- band B is visible, and explicitly not queued ---------------------------

def test_band_b_is_visible_and_marked_for_manual_review():
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    items = build_review_list(limit=500)
    b_items = [i for i in items if i["band"] == "B"]
    assert b_items, "band B must be visible in the review list"
    for item in b_items:
        assert item["category"] == CATEGORY_REMOTE_UNCLEAR
        assert item["manual_review_required"] is True
        assert item["auto_queue_allowed"] is False


def test_only_the_confirmed_remote_category_may_reach_the_queue():
    assert AUTO_QUEUE_ELIGIBLE_CATEGORIES == frozenset({CATEGORY_REMOTE_CONFIRMED})
    assert CATEGORY_REMOTE_UNCLEAR not in AUTO_QUEUE_ELIGIBLE_CATEGORIES
    assert CATEGORY_LOCAL_THAILAND not in AUTO_QUEUE_ELIGIBLE_CATEGORIES


def test_review_bands_exclude_c_by_construction():
    assert "C" not in REVIEW_BANDS
    assert set(REVIEW_BANDS) == {"A", "B"}


def test_band_b_asks_the_geography_question_and_does_not_assume_an_answer():
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    for item in build_review_list(limit=500):
        if item["category"] == CATEGORY_REMOTE_UNCLEAR:
            assert any("Таиланд" in q for q in item["open_questions"])
            break
    else:
        pytest.skip("no band B row in the live set")


def test_a_missing_salary_is_reported_as_a_question_not_as_a_match():
    """An absent figure is an open question, never a confirmed condition."""
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    items = build_review_list(limit=500)
    no_salary = [i for i in items if i["salary_min"] is None and i["salary_max"] is None]
    assert no_salary, "the live set has rows without a salary"
    for item in no_salary:
        assert any("Зарплата" in q for q in item["open_questions"])


# --- the module cannot approve anything -------------------------------------

def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_review_module_imports_no_queue_writer():
    """Structural, not a matter of discipline: the list cannot queue anything.

    The check reads the AST rather than the file text, so the names mentioned in
    the module docstring do not count as imports.
    """
    imported = _imported_modules(MODULE_PATH)
    forbidden = {
        "ai_assistant.application_queue",
        ".application_queue",
        "ai_assistant.matcher",
        ".matcher",
        "ai_assistant.browser_executor",
        ".browser_executor",
    }
    assert not (imported & forbidden), f"review module must stay read-only, imports: {imported}"


def test_building_the_review_list_does_not_touch_the_queue():
    """Measured on a copy of the real database: the queue is byte-for-byte stable.

    The configured database is the one under test here, so the copy is named
    explicitly -- reading the live ``state.db`` would prove nothing about writes.
    """
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    tmp = tempfile.mkdtemp()
    copy = Path(tmp) / "copy.db"
    copy.write_bytes(STATE_DB.read_bytes())
    before = _queue_snapshot(copy)
    items = build_review_list(limit=500, db_path=str(copy))
    after = _queue_snapshot(copy)
    assert items
    assert before == after, "the review list must not modify application_queue"


def test_the_configured_database_is_the_default_source(monkeypatch):
    """With no ``db_path`` the module reads ``config.DB_FILE`` and nothing else."""
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    tmp = tempfile.mkdtemp()
    copy = Path(tmp) / "copy.db"
    copy.write_bytes(STATE_DB.read_bytes())
    monkeypatch.setattr(config, "DB_FILE", str(copy))
    assert _build_review_list(limit=50) == _build_review_list(limit=50, db_path=str(copy))


def test_a_missing_database_is_reported_rather_than_created():
    """Read-only means read-only: no database file is conjured into existence."""
    tmp = Path(tempfile.mkdtemp()) / "absent.db"
    with pytest.raises(FileNotFoundError, match="will not create it"):
        _build_review_list(limit=10, db_path=str(tmp))
    assert not tmp.exists()


def _queue_snapshot(path: Path) -> list[tuple]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return sorted(conn.execute("SELECT vacancy_stable_id, priority_score FROM application_queue"))
    finally:
        conn.close()


# --- the Thailand-local category is a separate question ---------------------

def test_a_multi_country_remote_label_is_not_a_thailand_posting():
    """The Lemon.io trap: "Anywhere in the World, <73 countries>, Delaware".

    Reading the country list would file a worldwide remote posting as local work
    in Thailand -- the same rendering artefact that T7.4 had to fix for conflicts.
    """
    lemon = (
        "Anywhere in the World, \U0001f1e6\U0001f1f7 Argentina, \U0001f1e9\U0001f1ea Germany, "
        "\U0001f1f9\U0001f1ed Thailand, \U0001f1fa\U0001f1f8 United States of America, Delaware"
    )
    assert _is_thailand_local(lemon, []) is False
    assert _is_thailand_local("Anywhere in the World", []) is False
    assert _is_thailand_local("Удаленно", []) is False
    assert _is_thailand_local("Москва", []) is False


def test_a_thailand_scoped_posting_is_local():
    assert _is_thailand_local("Thailand", ["Thailand"]) is True
    assert _is_thailand_local("Bangkok", []) is True
    assert _is_thailand_local("Bangkok, Thailand", []) is True
    assert _is_thailand_local("Chiang Mai", []) is True
    assert _is_thailand_local("Remote, Thailand", []) is True


def test_the_local_category_is_never_auto_queued():
    """A Thai posting is not a free pass either: workplace, language and permit
    still need checking, and the employer is not assumed to arrange the permit."""
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    local = build_review_list(limit=500, categories=(CATEGORY_LOCAL_THAILAND,))
    for item in local:
        assert item["category"] == CATEGORY_LOCAL_THAILAND
        assert item["auto_queue_allowed"] is False
        assert item["manual_review_required"] is True
        assert any("разрешение на работу" in q for q in item["open_questions"])


def test_the_local_category_is_kept_apart_from_the_remote_categories():
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    items = build_review_list(limit=500)
    local = [i for i in items if i["category"] == CATEGORY_LOCAL_THAILAND]
    remote = [i for i in items if i["category"] != CATEGORY_LOCAL_THAILAND]
    assert not ({i["stable_id"] for i in local} & {i["stable_id"] for i in remote})


# --- the list is title-first, and the filters are honest --------------------

def test_a_body_only_match_does_not_enter_the_list_by_default():
    """Measured on the live set: body matching gave 113 false hits out of 131.

    A "Building Maintenance Technician" whose blurb says "troubleshoot" is not a
    support vacancy, so the default list reads titles only.
    """
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    strict = build_review_list(limit=500, title_only=True)
    loose = build_review_list(limit=500, title_only=False)
    assert len(loose) > len(strict)
    assert all(
        any(
            any(re.search(p, i["title"], re.IGNORECASE) for p in tp)
            for _n, _pr, tp, _dp in DIRECTIONS
        )
        for i in strict
        if i["category"] != CATEGORY_LOCAL_THAILAND
    ), "every remote item must match a direction in its title"


def test_only_the_local_category_may_match_without_a_title_hit():
    """The one deliberate exception, pinned so it cannot spread.

    The Thailand-local category reads the body because a posting written for the
    Thai market often does not name the direction in its title. That exception
    must stay confined to that category: a *remote* row reaching the default list
    without a title match would mean body matching had leaked back in, which is
    what produced 113 false hits out of 131.
    """
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    items = build_review_list(limit=500, title_only=True)
    title_less = [
        i
        for i in items
        if not any(
            re.search(p, i["title"], re.IGNORECASE)
            for _n, _pr, tp, _dp in DIRECTIONS
            for p in tp
        )
    ]
    assert all(i["category"] == CATEGORY_LOCAL_THAILAND for i in title_less), [
        (i["category"], i["title"]) for i in title_less
    ]


def test_negative_title_patterns_drop_the_professions_the_task_names():
    """Salesforce Developer, Mac MSP and management roles are not padding."""
    for bad in (
        "Senior Salesforce CRM Developer",
        "Salesforce Developer",
        "Salesforce Consultant",
        "Mac MSP Help Desk Guru",
        "DevOps Engineer",
        "Head of Technical Support",
        "Director of Accounting",
        "Junior Crypto Trader",
    ):
        assert any(
            re.search(p, bad, re.IGNORECASE)
            for p in NEGATIVE_TITLE_PATTERNS
        ), bad


def test_crm_administration_is_not_swept_out_with_salesforce_development():
    """The exclusion is about the *profession*, not the vendor.

    CRM administration is one of the target directions, so a Salesforce
    administrator must stay in scope; only developer/consultant roles are ruled
    out. Pinning this keeps a future widening of the pattern from silently
    removing a whole direction.
    """
    for good in (
        "Salesforce Administrator",
        "CRM Administrator (Bitrix24)",
        "CRM Support Specialist",
    ):
        assert not any(
            re.search(p, good, re.IGNORECASE) for p in NEGATIVE_TITLE_PATTERNS
        ), good


def test_management_roles_are_not_offered_as_support():
    """A "Head of" posting is not a CRM Support vacancy just because it says CRM."""
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    for item in build_review_list(limit=500):
        assert not re.search(
            r"\b(?:head\s+of|director|vp|chief|president)\b", item["title"], re.IGNORECASE
        ), item["title"]


# --- the endpoint contract ---------------------------------------------------

@pytest.fixture
def live_dashboard(monkeypatch):
    """A dashboard client pointed at the real database.

    Two things have to be arranged. ``conftest`` redirects ``config.DB_FILE`` to
    an empty temporary database, so the config is put back; and the endpoint calls
    ``init_db`` for its other routes, which would open the production database for
    writing -- a test must not migrate it, so that call is replaced.
    """
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    fastapi = pytest.importorskip("fastapi.testclient")
    monkeypatch.setenv("DASHBOARD_REQUIRE_AUTH_FOR_READS", "false")
    monkeypatch.setattr(config, "DB_FILE", str(STATE_DB))
    import ai_assistant.ui.app as app_module

    monkeypatch.setattr(app_module, "init_db", lambda *a, **k: None)
    return fastapi.TestClient(app_module.app)


def test_the_review_endpoint_returns_a_c_free_list(live_dashboard):
    res = live_dashboard.get("/api/review-list?limit=20")
    assert res.status_code == 200
    payload = res.json()
    assert set(payload) >= {"items", "summary", "category_labels"}
    assert payload["items"]
    assert all(i["band"] != "C" for i in payload["items"])
    assert payload["summary"]["total"] == len(payload["items"])
    assert payload["summary"]["needs_manual_review"] >= 1


def test_the_review_endpoint_can_serve_the_local_category(live_dashboard):
    res = live_dashboard.get(f"/api/review-list?category={CATEGORY_LOCAL_THAILAND}")
    assert res.status_code == 200
    for item in res.json()["items"]:
        assert item["category"] == CATEGORY_LOCAL_THAILAND


def test_the_review_endpoint_rejects_an_unknown_category(live_dashboard):
    """An unknown category must not quietly return an empty list."""
    res = live_dashboard.get("/api/review-list?category=not_a_category")
    assert res.status_code == 422
    assert "not_a_category" in res.text


def test_the_review_endpoint_rejects_an_unknown_direction(live_dashboard):
    res = live_dashboard.get("/api/review-list?direction=not_a_direction")
    assert res.status_code == 422
    assert "not_a_direction" in res.text


def test_the_review_endpoint_accepts_every_direction_it_advertises(live_dashboard):
    """The filter names the endpoint advertises must all be real ones."""
    from ai_assistant.eligibility_review import DIRECTION_NAMES

    assert DIRECTION_NAMES, "the directions list must not be empty"
    for name in sorted(DIRECTION_NAMES):
        res = live_dashboard.get(f"/api/review-list?limit=5&direction={name}")
        assert res.status_code == 200, (name, res.text)


def test_the_dashboard_page_carries_the_review_tab(live_dashboard):
    html = live_dashboard.get("/").text
    assert 'id="tab-review"' in html
    assert "loadReviewList" in html


# --- what the list must not offer -------------------------------------------

def test_management_titles_are_kept_out_in_both_languages():
    """The task names руководящие позиции as padding; a board writes them in Russian."""
    for title in (
        "Head of Technical Support",
        "Director of IT",
        "Руководитель технической поддержки",
        "Начальник отдела сопровождения",
        "Директор по ИТ",
    ):
        assert _title_is_negative(title), title


def test_a_manager_title_in_english_is_left_to_the_reader():
    """The asymmetry is deliberate, and this is where it is written down.

    «Руководитель» in a Russian title is a management post. In English titles
    "manager" is routinely a senior individual contributor -- a "CRM Manager" is
    often the administrator this candidate is looking for -- so those stay in the
    list and are kept out of the shortlist by hand instead of by a rule that
    would delete a target role.
    """
    for title in ("CRM Manager", "Technical Support Manager", "CRM Automation Manager"):
        assert not _title_is_negative(title), title


def test_a_development_job_is_not_the_administration_job_that_was_asked_for():
    """Bitrix24 is a target direction *as administration and support*, not as PHP."""
    for title in (
        "Разработчик Bitrix24",
        "Backend-разработчик BItrix/PHP",
        "PHP/Bitrix24 разработчик (Коробка/On-premise)",
    ):
        assert _title_is_negative(title), title
    for title in ("Bitrix24 администратор", "Специалист поддержки Bitrix24"):
        assert not _title_is_negative(title), title


def test_a_crm_developer_is_not_a_crm_administrator_in_either_word_order():
    for title in (
        "Senior CRM Developer, Salesforce and HubSpot",
        "Salesforce CRM Developer",
        "CRM Architect",
    ):
        assert _title_is_negative(title), title
    for title in ("CRM Administrator (Bitrix24)", "CRM Support Specialist", "Salesforce Administrator"):
        assert not _title_is_negative(title), title


def test_a_non_it_domain_is_not_an_it_vacancy_unless_it_names_an_it_context():
    """The veto is conditional, or it would delete a whole legitimate sector."""
    assert _title_is_negative("Behavioral Health Integration Specialist")
    assert _title_is_negative("Clinical Systems Coordinator")
    assert not _title_is_negative("Healthcare IT Support Engineer")
    assert not _title_is_negative("Medical Device Software Support Engineer")


def test_the_profile_exclusions_are_read_from_the_profile_not_invented():
    exclusions = load_hard_exclusions()
    assert "php" in exclusions["excluded_roles"]
    assert "gambling" in exclusions["excluded_industries"]
    assert exclusions["source"]


def test_an_excluded_industry_is_matched_through_its_synonyms():
    """The profile says "gambling"; a live posting says "iGaming"."""
    exclusions = load_hard_exclusions()
    assert "igaming" in _industry_keywords(exclusions)
    hit = _hard_exclusion_hit(
        "Customer Support Agent",
        "…previous igaming experience is a plus; english level: b2…",
        "Giti2c",
        exclusions,
    )
    assert hit is not None and hit.startswith("excluded_industries")


def test_an_excluded_role_is_matched_in_the_body_as_well_as_the_title():
    """The submission gate reads the body; a list that reads less is inconsistent."""
    exclusions = load_hard_exclusions()
    assert _hard_exclusion_hit("Support Engineer", "стек: php, bitrix", "X", exclusions)
    assert _hard_exclusion_hit("Support Engineer", "windows, active directory", "X", exclusions) is None


def test_it_support_outranks_customer_support():
    """Helping end users of a product is adjacent, not the same job."""
    it_direction = _direction("IT Support Engineer", "", title_only=True)
    cs_direction = _direction("Customer Support Specialist", "", title_only=True)
    assert it_direction is not None and it_direction[0] == "it_support"
    assert cs_direction is not None and cs_direction[0] == "customer_support"
    assert it_direction[1] < cs_direction[1], "IT support must rank above customer support"


def test_the_dropped_rows_are_counted_rather_than_disappearing():
    """A list that silently omits rows looks exactly like a list with nothing in it."""
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    stats: dict[str, int] = {}
    items = build_review_list(limit=500, stats=stats)
    assert items
    assert stats, "the caller must be able to see what was dropped and why"
    assert sum(stats.values()) > len(items)
    assert any(k.startswith("profile_exclusion") for k in stats)


def test_no_band_c_row_reaches_the_default_list_even_with_the_exclusions_on():
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    items = build_review_list(limit=500)
    assert all(i["band"] != "C" for i in items)
    assert all(i["auto_queue_allowed"] is False or i["category"] == CATEGORY_REMOTE_CONFIRMED
               for i in items)


# --- summarise ---------------------------------------------------------------

def test_summarise_counts_what_the_list_contains():
    if not _has_state_db():
        pytest.skip("state.db is not present in this checkout")

    items = build_review_list(limit=50)
    s = summarise(items)
    assert s["total"] == len(items)
    assert sum(s["by_category"].values()) == len(items)
    assert sum(s["by_direction"].values()) == len(items)
    assert s["needs_manual_review"] == sum(1 for i in items if i["manual_review_required"])
