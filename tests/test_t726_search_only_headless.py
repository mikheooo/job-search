"""T7.26 — SEARCH_ONLY really searches HH headless, and navigation settles.

Two things are pinned here.

**§8/§9, the navigation settle.** ``hh_vacancy_navigator`` used to navigate by
setting ``location.href``, sleep a fixed 2.5 s and then read the page. A page slower
than the sleep returned the document we had just left, so the URL check reported a
false mismatch and the UI flags described the previous page. The tests below prove
the new wait is deterministic - it keeps polling until the URL really moved and the
document really rendered - and, explicitly, that the old shape fails on the very
same page.

**§3-§7, the owned search.** ``run_search_operation`` is the SEARCH_ONLY HH segment:
one ``hh_operation`` with an explicit owner and purpose, acquire -> search/read ->
release. It is driven end to end here against a scripted session, so "it acquires",
"it gives the browser back", "it never submits" and "it returns real results" are
measurements rather than claims.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from ai_assistant import hh_browser_lease as lease
from ai_assistant import hh_browser_provider as bp
from ai_assistant import hh_readonly_operation as ro
from ai_assistant import hh_structured_preflight as sp
from ai_assistant import hh_vacancy_navigator as nv

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ai_assistant"


# ---------------------------------------------------------------------------
# a controllable outside world
# ---------------------------------------------------------------------------

@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A private lease file and a world with no sockets in it."""
    monkeypatch.setattr(bp, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bp, "STATE_FILE", tmp_path / "runner-state.json")
    monkeypatch.setattr(bp, "CHROME_LOG", tmp_path / "chrome.log")
    monkeypatch.setattr(lease, "LEASE_FILE", tmp_path / "browser-lease.json")
    monkeypatch.setattr(bp, "pid_alive", lambda pid: True)
    monkeypatch.setattr(bp, "cdp_version", lambda *a, **k: None)
    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: False)
    monkeypatch.setattr(bp, "list_tabs", lambda *a, **k: [])
    monkeypatch.setattr(bp, "close_tab", lambda *a, **k: True)
    monkeypatch.setattr(bp, "quit_browser", lambda *a, **k: True)
    monkeypatch.setattr(bp, "wait_port_free", lambda *a, **k: True)
    monkeypatch.setattr(bp, "dedicated_profile_processes", lambda *a, **k: [])
    monkeypatch.setattr(bp, "_release_lease_after_stop", lambda *a, **k: True)
    monkeypatch.setattr(bp, "endpoint_is_foreign_browser", lambda *a, **k: False)
    return tmp_path


def snapshot_for(vacancy_id: str, *, title: str = "Системный администратор",
                 area: str = "Москва", archived=None, employer: str = "ООО Ромашка"):
    """A structured payload in the shape ``STRUCTURED_JS`` returns, plus §6 fields."""
    return {
        "vacancy_id": vacancy_id,
        "ok": True,
        "short_vacancy": {
            "workExperience": "between1And3",
            "civilLawContracts": [{"civilLawContractsElement": ["INDIVIDUAL_ENTREPRENEUR"]}],
            "acceptLaborContract": False,
            "workSchedule": "remote",
        },
        "vacancy": {
            "compensation": {"to": 120000, "currencyCode": "RUR", "gross": True},
            "area": {"name": area},
            "employment": "FULL",
            "workFormats": [{"workFormatsElement": ["REMOTE"]}],
            "name": title,
            "id": vacancy_id,
            "archived": archived,
        },
        "employer": {"visibleName": employer, "name": employer, "id": "42"},
        "applicant": {
            "test": {"hasTests": False},
            "vacancyTests": {vacancy_id: None},
            "responseAllowed": True,
            "responseImpossible": False,
        },
    }


def card(vacancy_id: str, *, title: str = "Системный администратор",
         employer: str = "ООО Ромашка", salary: str = "120 000 ₽"):
    return {
        "vacancy_id": vacancy_id,
        "title": title,
        "employer": employer,
        "salary": salary,
        "area": "Москва",
        "format_label": "Можно удалённо",
        "url": f"https://hh.ru/vacancy/{vacancy_id}",
    }


class _FakeSession:
    """A headless session whose pages are scripted, not fetched.

    Implements exactly the surface ``run_search_operation`` uses, so the operation
    can be driven end to end with no socket and no Chrome.

    The knobs are **class** attributes on purpose: the operation builds its own
    session through ``bp.HeadlessBrowserSession``, so a test configures the class and
    then reads the instance the operation actually used from ``instances``.
    """

    instances: ClassVar[list[_FakeSession]] = []

    # -- knobs a test sets before running the operation ---------------------
    ready_after_polls: ClassVar[int] = 1   # 1 = the page is ready on the first poll
    stuck_on_about_blank: ClassVar[bool] = False  # never leaves about:blank
    cards: ClassVar[list[dict]] = []
    snapshots: ClassVar[dict[str, dict]] = {}
    descriptions: ClassVar[dict[str, str]] = {}
    raise_on_navigate: ClassVar[Exception | None] = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cdp_url = kwargs.get("cdp_url") or bp.headless_cdp_url()
        self.owner_type = kwargs.get("owner_type")
        self.work_tab = None
        self.owned: list[str] = []
        self.stopped = False
        self.started = False
        self.url = "about:blank"
        self.polls = 0
        type(self).instances.append(self)

    # -- the surface run_search_operation uses -----------------------------
    def start(self):
        self.started = True
        return {"ok": True, "status": "RUNNING", "reused": False,
                "auth": {"authenticated": True, "verified": True}}

    def ensure_work_tab(self, url=None, purpose=bp.PURPOSE_WORK):
        if self.work_tab is None:
            self.work_tab = "t1"
            self.owned.append("t1")
        if url:
            self.navigate(self.work_tab, url)
        return self.work_tab

    def repurpose_work_tab(self, purpose, url=""):
        return None

    def navigate(self, target_id, url, timeout=25.0):
        if type(self).raise_on_navigate is not None:
            raise type(self).raise_on_navigate
        self.url = "about:blank" if type(self).stuck_on_about_blank else url
        self.polls = 0
        return {"url": self.url, "ready": "loading"}

    def evaluate_on(self, target_id, expression):
        if expression == bp._READY_JS:
            self.polls += 1
            ready = "complete" if self.polls >= type(self).ready_after_polls else "loading"
            return json.dumps({"href": self.url, "ready": ready})
        if expression == ro.READ_CARDS_JS:
            return json.dumps({"url": self.url, "count": len(type(self).cards),
                               "cards": list(type(self).cards)})
        if expression == ro.VACANCY_DESCRIPTION_JS:
            vid = self.url.rstrip("/").split("/")[-1]
            return json.dumps({"from_dom": type(self).descriptions.get(vid, ""),
                               "from_payload": None})
        if "const vid = '" in expression:
            vid = expression.split("const vid = '")[1].split("'")[0]
            snap = type(self).snapshots.get(vid)
            if snap is None:
                return json.dumps({"vacancy_id": vid, "ok": False,
                                   "error": "no payload"})
            return json.dumps(snap)
        return json.dumps({"ok": True})

    def ownership_registry(self):
        return [{"target_id": t, "purpose": bp.PURPOSE_SEARCH, "url": self.url}
                for t in self.owned]

    def close_all_owned_tabs(self):
        closed, self.owned = list(self.owned), []
        return {"closed": closed, "failed": [], "closed_count": len(closed),
                "failed_count": 0}

    def stop(self):
        self.stopped = True
        return {"port_freed": True, "profile_unlocked": True, "owned_processes": 0,
                "status": "STOPPED"}


@pytest.fixture()
def session(world, monkeypatch):
    """A scripted session class, wired in as the one ``hh_operation`` builds."""
    _FakeSession.instances = []
    _FakeSession.ready_after_polls = 1
    _FakeSession.stuck_on_about_blank = False
    _FakeSession.cards = []
    _FakeSession.snapshots = {}
    _FakeSession.descriptions = {}
    _FakeSession.raise_on_navigate = None
    monkeypatch.setattr(bp, "HeadlessBrowserSession", _FakeSession)
    # Keep the waits short: the polls are what matter, not the wall clock.
    monkeypatch.setattr(ro, "_WAIT_ATTEMPTS", 3)
    monkeypatch.setattr(ro, "_WAIT_DELAY", 0.001)
    return _FakeSession


def _prime(ids: list[str], *, cards: int | None = None) -> None:
    """Give the fake a result page and a payload for each vacancy id."""
    _FakeSession.cards = [card(v) for v in ids[: cards if cards is not None else len(ids)]]
    for v in ids:
        _FakeSession.snapshots[v] = snapshot_for(v)
        _FakeSession.descriptions[v] = (
            "Работа удалённо из любой точки мира, оформление по договору ГПХ с ИП."
        )


# ===========================================================================
# §8 / §9  — the navigation settle
# ===========================================================================

def test_the_settle_waits_past_the_old_two_and_a_half_seconds():
    """§9: a page that becomes ready later than 2.5 s must still be waited for.

    The old idiom slept 2.5 s and read once. Here the page needs 300 polls, and at
    the delay used that is 3.0 s of waiting - comfortably past the budget the fixed
    sleep allowed - and the settle still returns the page instead of a mismatch.
    """
    delay = 0.01
    polls = 0

    def evaluate(_js):
        nonlocal polls
        polls += 1
        ready = "complete" if polls >= 300 else "loading"
        return json.dumps({"url": "https://hh.ru/vacancy/136704137",
                           "title": "Python developer middle",
                           "is_vacancy_page": True, "ready": ready, "has_ssr": True})

    state = nv._settle_after_navigation(evaluate, "https://hh.ru/chat",
                                        attempts=400, delay=delay)

    assert state.get("timeout") is not True, "the settle gave up on a slow page"
    assert polls >= 300, f"the settle stopped after {polls} polls"
    assert polls * delay > 2.5, (
        "the simulated wait did not exceed the 2.5s the old fixed sleep allowed, so "
        "this test would not have caught the defect"
    )


def test_the_old_fixed_sleep_plus_immediate_read_would_have_read_the_old_page():
    """§9: the regression is real - the old shape fails on the same page.

    The old code read once after 2.5 s. On a page that needs longer, that single read
    returns a document that is still loading, so ``_document_settled`` is False and
    the caller would have reported a mismatch for a page that was loading fine.
    """
    polls = 0

    def evaluate(_js):
        nonlocal polls
        polls += 1
        ready = "complete" if polls >= 300 else "loading"
        return json.dumps({"url": "https://hh.ru/vacancy/136704137",
                           "title": "Python developer middle",
                           "is_vacancy_page": True, "ready": ready, "has_ssr": True})

    # The old idiom, exactly: navigate, sleep 2.5s, read once.
    one_read = json.loads(evaluate(nv._INSPECT_VACANCY_PAGE_JS))
    assert one_read["ready"] == "loading", "the page under test is not actually slow"
    assert nv._document_settled(one_read, "https://hh.ru/chat") is False, (
        "the old single read was accepted, so this test proves nothing"
    )

    # The new idiom waits, and gets there.
    settled = nv._settle_after_navigation(evaluate, "https://hh.ru/chat",
                                          attempts=400, delay=0.001)
    assert settled.get("timeout") is not True
    assert nv._document_settled(settled, "https://hh.ru/chat") is True


def test_a_redirect_still_settles():
    """§9: HH redirects, so the wait compares against the URL we came from."""
    state = nv._settle_after_navigation(
        lambda _js: json.dumps({
            "url": "https://hh.ru/vacancy/136704137?from=serp&hhtmFrom=vacancy_search",
            "title": "Python developer middle",
            "is_vacancy_page": True, "ready": "complete", "has_ssr": True,
        }),
        "https://hh.ru/search/vacancy?text=python",
    )
    assert state.get("timeout") is not True, "a redirected page was not accepted"
    assert "136704137" in state["url"]


def test_a_page_that_never_settles_is_reported_not_assumed():
    """§9: never settling is a measured timeout, not a silent pass."""
    state = nv._settle_after_navigation(
        lambda _js: json.dumps({"url": "https://hh.ru/chat", "title": "Чаты",
                                "is_vacancy_page": False, "ready": "complete"}),
        "https://hh.ru/chat", attempts=3, delay=0.001,
    )
    assert state.get("timeout") is True


def test_about_blank_cannot_count_as_a_loaded_vacancy(monkeypatch):
    """§9: a fresh tab is already ``complete`` on ``about:blank``.

    The old wait accepted it; the new one must not - at the settle level, and end to
    end through the navigator.
    """
    assert nv._document_settled(
        {"url": "about:blank", "title": "New tab", "is_vacancy_page": True,
         "ready": "complete"},
        "https://hh.ru/chat",
    ) is False

    monkeypatch.setattr(nv, "_SETTLE_ATTEMPTS", 3)
    monkeypatch.setattr(nv, "_SETTLE_DELAY", 0.001)

    def evaluate(js):
        if "window.location.href =" in js:
            return json.dumps({"ok": True, "navigated_to": "https://hh.ru/vacancy/135112049"})
        return json.dumps({"url": "about:blank", "title": "", "has_submit_btn": False,
                           "has_apply_btn": False, "is_vacancy_page": False,
                           "ready": "complete"})

    res = nv.verify_and_navigate_hh_vacancy(
        target="hh:135112049", evaluate_fn=evaluate, navigate_if_needed=True,
    )
    assert res.ok is False
    assert res.status == "BLOCKED"
    assert "settle" in res.reason


def test_the_navigator_waits_and_then_reports_ready_on_a_slow_page(monkeypatch):
    """§9 end to end: the navigator itself waits, instead of reading too early."""
    monkeypatch.setattr(nv, "_SETTLE_ATTEMPTS", 400)
    monkeypatch.setattr(nv, "_SETTLE_DELAY", 0.001)
    polls = {"n": 0}
    state = {"url": "https://hh.ru/chat", "title": "Чаты"}

    def evaluate(js):
        if "window.location.href =" in js:
            state["url"] = "https://hh.ru/vacancy/135112049"
            state["title"] = "Senior AI Automation Engineer"
            return json.dumps({"ok": True})
        polls["n"] += 1
        # The new document is not rendered until well past the old 2.5s budget.
        ready = "complete" if polls["n"] >= 300 else "loading"
        return json.dumps({
            "url": state["url"], "title": state["title"],
            "has_submit_btn": ready == "complete", "has_apply_btn": False,
            "already_responded": False,
            "is_vacancy_page": state["url"].startswith("https://hh.ru/vacancy/"),
            "ready": ready, "has_ssr": True,
        })

    res = nv.verify_and_navigate_hh_vacancy(
        target="hh:135112049", evaluate_fn=evaluate, navigate_if_needed=True,
    )
    assert polls["n"] >= 300, "the navigator did not wait for the slow page"
    assert res.ok is True, f"a slow but fine page was rejected: {res.reason}"
    assert res.status == "READY"


# ===========================================================================
# §3 / §14 — the owned search segment
# ===========================================================================

def test_the_search_segment_acquires_an_owned_browser(session):
    """§3/§14: the segment acquires through ``hh_operation``, explicitly owned."""
    _prime(["136704137", "136704138"])

    report = ro.run_search_operation(
        ["IT Support"], read_limit=2,
        owner_type=lease.OWNER_AUTONOMOUS, purpose="search",
    )

    assert report["owner_type"] == lease.OWNER_AUTONOMOUS
    assert report["purpose"] == "search"
    assert report["lease_status"] in (lease.STATUS_ACQUIRED, lease.STATUS_REUSED)
    assert report["start"]["ok"] is True
    assert report["auth_ok"] is True
    assert report["session_id"]
    assert _FakeSession.instances[-1].started is True


def test_the_search_segment_releases_the_browser_on_success(session):
    """§3/§4/§14: acquire -> search/read -> release, and the release is measured."""
    _prime(["136704137", "136704138", "136704139"])

    report = ro.search_operation_report(
        ["IT Support"], read_limit=3,
        owner_type=lease.OWNER_AUTONOMOUS, purpose="search",
    )

    assert _FakeSession.instances[-1].stopped is True, "the browser was not stopped"
    assert report["release"]["stopped"] is True
    assert report["after"]["lease_present"] is False
    assert report["after"]["lease_state"] == lease.STATE_ABSENT
    assert report["after"]["headless_processes"] == 0
    assert report["after"]["port_listening"] is False
    assert lease.read_lease() is None


def test_the_search_segment_releases_the_browser_on_exception(session):
    """§14: an operation that raises still gives the browser back."""
    _FakeSession.raise_on_navigate = RuntimeError("the page blew up")

    report = ro.search_operation_report(
        ["IT Support", "Bitrix24"], read_limit=2,
        owner_type=lease.OWNER_AUTONOMOUS, purpose="search",
    )

    assert _FakeSession.instances[-1].stopped is True, "an exception leaked the browser"
    assert report["release"]["stopped"] is True
    assert lease.read_lease() is None
    assert all(row["status"] == "ERROR" for row in report["queries"])


def test_the_search_segment_returns_real_search_results(session):
    """§5/§14: per query - results, unique and reads; no zeroes by construction."""
    _prime(["136704137", "136704138", "136704139", "136704140", "136704141"])

    report = ro.run_search_operation(
        ["IT Support", "System Administrator", "Bitrix24", "CRM Support"],
        read_limit=5, owner_type=lease.OWNER_AUTONOMOUS, purpose="search",
    )

    assert len(report["queries"]) == 4
    assert all(row["status"] == "OK" for row in report["queries"])
    assert all(row["result_count"] == 5 for row in report["queries"])
    # The same five cards from four queries are one set of five unique vacancies.
    assert report["unique_count"] == 5
    assert report["read_count"] == 5
    assert report["submitted"] == 0
    assert report["messages_sent"] == 0
    assert report["mouse_events_dispatched"] == 0
    assert all(row["settled_url"].startswith("https://hh.ru/search/vacancy")
               for row in report["queries"])


def test_the_search_segment_deduplicates_across_queries(session):
    """§5: UNIQUE COUNT is a count of distinct vacancies, not of cards seen."""
    _prime(["136704137", "136704138"], cards=2)

    report = ro.run_search_operation(
        ["IT Support", "System Administrator"], read_limit=0,
        owner_type=lease.OWNER_AUTONOMOUS, purpose="search",
    )

    assert report["result_count"] == 4          # 2 cards x 2 queries
    assert report["unique_count"] == 2          # but only two vacancies
    assert report["queries"][0]["unique_count"] == 2
    assert report["queries"][1]["unique_count"] == 0


def test_a_query_that_cannot_settle_is_recorded_not_hidden(session):
    """§9/§5: a query whose page never settles is an ERROR row, not a silent zero."""
    _FakeSession.stuck_on_about_blank = True

    report = ro.run_search_operation(
        ["IT Support"], read_limit=0,
        owner_type=lease.OWNER_AUTONOMOUS, purpose="search",
    )

    row = report["queries"][0]
    assert row["status"] == "ERROR"
    assert "settled" in row["error"]
    assert report["unique_count"] == 0
    assert _FakeSession.instances[-1].stopped is True


def test_the_structured_preflight_receives_the_full_payload(session):
    """§6/§14: every required key reaches the preflight, plus employer and archived."""
    _prime(["136704137"])
    _FakeSession.snapshots["136704137"] = snapshot_for("136704137", archived=False)

    report = ro.run_search_operation(
        ["IT Support"], read_limit=1,
        owner_type=lease.OWNER_AUTONOMOUS, purpose="search",
    )

    entry = report["vacancies"][0]
    assert entry["ok"] is True, entry.get("error")
    preflight = entry["preflight"]
    assert preflight["fields_missing"] == [], (
        f"the payload did not carry every required key: {preflight['fields_missing']}"
    )
    assert preflight["read_complete"] is True
    assert preflight["employer"]["visibleName"] == "ООО Ромашка"
    assert preflight["archived"] is False
    # §6: the material fields, lifted out for the report.
    read = entry["read"]
    assert read["id"] == "136704137"
    assert read["title"] == "Системный администратор"
    assert read["salary"]["to"] == 120000
    assert read["work_experience"] == "between1And3"
    assert read["employment"] == "Договор ГПХ с ИП"
    assert read["area"] == "Москва"
    assert read["response_allowed"] is True
    assert read["archived"] is False
    assert read["description_chars"] > 0


def test_the_band_is_classified_from_the_employers_own_words(session):
    """§7: the A/B/C band is a real classification, not a guess from workFormats."""
    _prime(["136704137"])
    _FakeSession.descriptions["136704137"] = (
        "Мы работаем по всему миру и нанимаем сотрудников из любой страны."
    )

    report = ro.run_search_operation(
        ["IT Support"], read_limit=1,
        owner_type=lease.OWNER_AUTONOMOUS, purpose="search",
    )

    geo = report["vacancies"][0]["geo"]
    assert geo["band"] in {"A", "B", "C"}
    assert geo["reasons"], "a band was reported with no evidence behind it"


def test_the_search_segment_never_submits_and_never_sends(session):
    """§2/§13/§14: read-only by construction, and by measurement."""
    source = (PKG / "hh_readonly_operation.py").read_text(encoding="utf-8")
    for banned in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent",
                   "Input.insertText", "Input.dispatchDragEvent"):
        assert banned not in source, f"{banned} must never appear in the search segment"

    called = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name:
                called.add(name)
    for forbidden in ("submit_questionnaire_response", "send_message", "click",
                      "submit_application", "apply"):
        assert forbidden not in called, f"the search segment calls {forbidden}"

    _prime(["136704137"])
    report = ro.run_search_operation(["IT Support"], read_limit=1,
                                     owner_type=lease.OWNER_AUTONOMOUS, purpose="search")
    assert report["submitted"] == 0
    assert report["messages_sent"] == 0


def test_three_search_cycles_do_not_leave_a_browser(session):
    """§10/§14: three cycles in a row, each giving the browser back."""
    for _ in range(3):
        _prime(["136704137", "136704138"])
        report = ro.search_operation_report(
            ["IT Support"], read_limit=2,
            owner_type=lease.OWNER_AUTONOMOUS, purpose="search",
        )
        assert report["after"]["lease_present"] is False
        assert report["after"]["headless_processes"] == 0
        assert report["after"]["port_listening"] is False
        assert lease.read_lease() is None

    assert len(_FakeSession.instances) == 3
    assert all(inst.stopped for inst in _FakeSession.instances)


# ===========================================================================
# the wiring: SEARCH_ONLY actually uses the owned segment
# ===========================================================================

def test_search_only_uses_the_owned_segment_and_not_the_old_discovery():
    """§3/§14: the SEARCH_ONLY cycle routes through the owned search operation."""
    from ai_assistant.hh_autonomous_agent import AutonomousJobAgent

    source = inspect.getsource(AutonomousJobAgent._search_only_cycle)
    assert "search_operation_report" in source
    assert "OWNER_AUTONOMOUS" in source
    assert 'purpose="search"' in source
    assert "_discover_fresh_vacancies" not in source, (
        "SEARCH_ONLY still uses the discovery path that never acquired a browser"
    )


def test_the_autonomous_agent_calls_no_launch_primitive_directly():
    """§3/§14: no business logic reaches for the launch primitives."""
    called = set()
    for node in ast.walk(ast.parse((PKG / "hh_autonomous_agent.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name:
                called.add(name)
    for primitive in ("ensure_hh_browser", "ensure_headless_browser", "launch_headless"):
        assert primitive not in called, (
            f"the autonomous agent calls {primitive} directly instead of hh_operation"
        )


def test_the_ownership_guard_still_sees_zero_unowned_launch_paths():
    """§14: the AST ownership guard is green after this task's edits."""
    result = subprocess.run(
        [sys.executable, str(REPO / "tools" / ".t725_launch_map.py"), "--expect-zero"],
        cwd=str(REPO), capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, (
        "the ownership guard found an unowned production launch path:\n"
        + result.stdout[-4000:] + result.stderr[-2000:]
    )
    assert re.search(r"PRODUCTION UNOWNED LAUNCH PATHS\s*:\s*0", result.stdout), (
        result.stdout[-2000:]
    )


def test_the_snapshot_reader_carries_the_two_added_material_fields():
    """§6: the structured reader really reads the two fields §6 needed added."""
    js = sp.structured_js("136704137")
    assert "archived" in js
    assert "company" in js
    pre = sp.structured_preflight(snapshot_for("136704137", archived=True), object())
    assert pre["archived"] is True
    assert pre["employer"]["visibleName"] == "ООО Ромашка"
