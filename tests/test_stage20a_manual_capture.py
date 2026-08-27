"""Stage 20A tests: safe manual HH form capture.

Safety contract (fail-closed): the capture code path must NEVER call
goto / click / fill / type / set_input_files / check / uncheck / press /
keyboard / mouse. Test fakes raise on any of these - if the code under test
invoked one, the test errors out.

Also covers: BLOCKED_BY_MANUAL_FORM, forbidden response URLs, real options /
groups extraction, required-from-DOM-only, UNKNOWN controls, snapshot
sanitization, deterministic normalization.
"""

from __future__ import annotations

import json

import pytest

import tools.capture_manual_form as cap
from ai_assistant.hh_extractor import (
    ApplicationForm,
    QuestionSource,
    QuestionType,
)


# ---------- fakes ----------

class Forbidden(Exception):
    pass


class FakeElement:
    def __init__(self, text=""):
        self._text = text
    def inner_text(self):
        return self._text
    def get_attribute(self, name):
        return None
    def click(self):
        raise Forbidden("click called")
    def query_selector(self, sel):
        return None


class FakePage:
    """Page-like object: read APIs work, ALL mutation/navigation APIs raise."""

    def __init__(self, url, modal=None, controls=None, buttons=None, title="Vacancy"):
        self._url = url
        self._title = title
        self._modal = modal
        self._controls = controls or []
        self._buttons = buttons or []

    # --- read-only APIs (used by the tool) ---
    @property
    def url(self):
        return self._url

    def title(self):
        return self._title

    def eval_on_selector_all(self, selector, js):
        if selector == "button":
            return self._buttons
        if "role='dialog'" in selector and "modal" in selector:
            # presence probe used by find_form_pages
            if self._modal is not None:
                return {"found": True, "controls": 8}
            return {"found": False}
        # controls selector
        return self._controls

    def evaluate(self, js):
        return self._modal

    # --- FORBIDDEN APIs: must never be called by the tool ---
    def goto(self, *a, **k):
        raise Forbidden("goto called")

    def click(self, *a, **k):
        raise Forbidden("click called")

    def fill(self, *a, **k):
        raise Forbidden("fill called")

    def type(self, *a, **k):
        raise Forbidden("type called")

    def set_input_files(self, *a, **k):
        raise Forbidden("upload called")

    def check(self, *a, **k):
        raise Forbidden("check called")

    def uncheck(self, *a, **k):
        raise Forbidden("uncheck called")

    def press(self, *a, **k):
        raise Forbidden("press called")

    def query_selector_all(self, sel):
        raise Forbidden("query_selector_all used directly")

    @property
    def keyboard(self):
        raise Forbidden("keyboard used")

    @property
    def mouse(self):
        raise Forbidden("mouse used")


def _real_form_controls():
    """Fixture shaped like the real Magritte response form."""
    return [
        {"tag": "INPUT", "type": "text", "name": "phone", "id": None, "dataQa": None,
         "label": "Телефон", "ariaLabel": None, "ariaLabelledby": None, "required": True,
         "ariaRequired": None, "placeholder": "+7", "visible": True, "disabled": False,
         "readOnly": False, "options": None},
        {"tag": "TEXTAREA", "type": "textarea", "name": "letter", "id": "letter-id",
         "dataQa": "vacancy-response-letter", "label": "Сопроводительное письмо",
         "ariaLabel": None, "ariaLabelledby": "letter-lb", "required": False,
         "ariaRequired": None, "placeholder": "Письмо", "visible": True, "disabled": False,
         "readOnly": False, "options": None},
        {"tag": "SELECT", "type": "select", "name": "resume", "id": None,
         "dataQa": "vacancy-response-resume", "label": "Резюме", "ariaLabel": None,
         "ariaLabelledby": None, "required": True, "ariaRequired": None,
         "placeholder": None, "visible": True, "disabled": False, "readOnly": False,
         "options": [{"text": "AI Automation Engineer", "value": "r1", "disabled": False},
                     {"text": "Python Developer", "value": "r2", "disabled": False}]},
        {"tag": "INPUT", "type": "radio", "name": "relocation", "id": None, "dataQa": None,
         "label": "Готов к переезду", "ariaLabel": None, "ariaLabelledby": None,
         "required": False, "ariaRequired": None, "placeholder": None, "visible": True,
         "disabled": False, "readOnly": False, "options": None},
        {"tag": "INPUT", "type": "radio", "name": "relocation", "id": None, "dataQa": None,
         "label": "Не готов", "ariaLabel": None, "ariaLabelledby": None,
         "required": False, "ariaRequired": None, "placeholder": None, "visible": True,
         "disabled": False, "readOnly": False, "options": None},
        {"tag": "INPUT", "type": "checkbox", "name": "relocate_chk", "id": None,
         "dataQa": None, "label": "Командировки", "ariaLabel": None, "ariaLabelledby": None,
         "required": False, "ariaRequired": None, "placeholder": None, "visible": True,
         "disabled": False, "readOnly": False, "options": None},
        {"tag": "INPUT", "type": "file", "name": "resume_file", "id": None, "dataQa": None,
         "label": "Прикрепить файл", "ariaLabel": None, "ariaLabelledby": None,
         "required": False, "ariaRequired": None, "placeholder": None, "visible": True,
         "disabled": False, "readOnly": False, "options": None},
        {"tag": "INPUT", "type": "weird-widget", "name": "mystery", "id": None,
         "dataQa": None, "label": "Нечто неизвестное", "ariaLabel": None,
         "ariaLabelledby": None, "required": False, "ariaRequired": None,
         "placeholder": None, "visible": True, "disabled": False, "readOnly": False,
         "options": None},
    ]


_MODAL = {"selector": "[role='dialog']", "tag": "DIV", "role": "dialog",
          "cls": "magritte-modal", "dataQa": "vacancy-response-modal",
          "controlCount": 8, "textHead": "Откликнуться"}

_BUTTONS = [
    {"tag": "BUTTON", "type": "submit", "text": "Откликнуться",
     "dataQa": "vacancy-response-submit", "disabled": False, "visible": True,
     "cls": "magritte-button_mode-primary"},
    {"tag": "BUTTON", "type": "button", "text": "Отмена",
     "dataQa": "vacancy-response-cancel", "disabled": False, "visible": True,
     "cls": "magritte-button_mode-secondary"},
]


def _page(url="https://hh.ru/vacancy/136582669"):
    return FakePage(url=url, modal=_MODAL, controls=_real_form_controls(), buttons=_BUTTONS)


# ---------- 1-2. form open / not open ----------

def test_manual_form_open_inspection_works():
    res = cap.find_form_pages([_page()])
    assert res["verdict"] == "FORM_OPEN"
    snap = cap.inspect_page_dom(_page())
    assert snap["form_detected"] is True
    assert len(snap["controls"]) == 8
    assert snap["modal"]["dataQa"] == "vacancy-response-modal"


def test_form_not_open_blocked_by_manual_form():
    # vacancy page without an open modal
    page = FakePage(url="https://hh.ru/vacancy/1", modal=None, controls=[])
    res = cap.find_form_pages([page])
    assert res["verdict"] == "BLOCKED_BY_MANUAL_FORM"
    # non-hh pages also don't count
    res2 = cap.find_form_pages([FakePage(url="https://example.com", modal=_MODAL, controls=_real_form_controls())])
    assert res2["verdict"] == "BLOCKED_BY_MANUAL_FORM"


# ---------- 3. forbidden response URL ----------

def test_forbidden_response_url_inspected_with_caution():
    # Navigation guard primitive still hard-refuses:
    with pytest.raises(cap.CaptureSafetyError):
        cap._guard_url("https://hh.ru/applicant/vacancy_response?vacancyId=1")
    # A tab the USER opened at a response-flow URL is a valid candidate with a
    # caution note (the tool never navigates; reading its DOM is safe).
    page = FakePage(url="https://hh.ru/applicant/vacancy_response?vacancyId=1",
                    modal=_MODAL, controls=_real_form_controls())
    res = cap.find_form_pages([page])
    assert res["verdict"] == "FORM_OPEN"
    assert res["candidates"][0]["caution"] and "response-flow" in res["candidates"][0]["caution"]
    snap = cap.inspect_page_dom(page)
    assert snap["caution"] and "never navigates" in snap["caution"]
    assert snap["form_detected"] is True


# ---------- 4-8. no click / fill / upload / submit / login ----------

def test_no_click_fill_upload_submit_login_ever_called():
    # FakePage raises Forbidden on ANY mutation/navigation API.
    # If the tool used any of them, this test would error out.
    res = cap.find_form_pages([_page()])
    assert res["verdict"] == "FORM_OPEN"
    snap = cap.inspect_page_dom(_page())
    assert snap["form_detected"] is True
    form = cap.normalize_to_application_form(snap, "hh:test")
    assert isinstance(form, ApplicationForm)


def test_capture_module_has_no_forbidden_api_calls():
    import inspect as _inspect
    import pathlib
    src = pathlib.Path("tools/capture_manual_form.py").read_text(encoding="utf-8")
    # strip docstrings/comments crudely by checking statement-level usage
    for banned in [".goto(", ".click(", ".fill(", ".type(", ".set_input_files(",
                   ".check(", ".uncheck(", ".press(", ".keyboard", ".mouse"]:
        assert banned not in src, f"forbidden browser API in tool: {banned}"


# ---------- 9. SELECT options ----------

def test_real_select_options_extracted():
    snap = cap.inspect_page_dom(_page())
    sel = [c for c in snap["controls"] if c["type"] == "select"][0]
    assert [o["text"] for o in sel["options"]] == ["AI Automation Engineer", "Python Developer"]
    form = cap.normalize_to_application_form(snap, "hh:test")
    q = [q for q in form.questions if q.normalized_type == QuestionType.SELECT][0]
    assert q.options == ["AI Automation Engineer", "Python Developer"]
    assert q.required is True  # from DOM
    assert q.requires_review is False


# ---------- 10. RADIO / CHECKBOX groups ----------

def test_radio_checkbox_groups_extracted():
    snap = cap.inspect_page_dom(_page())
    rg = [g for g in snap["radio_groups"] if g["name"] == "relocation"][0]
    assert rg["labels"] == ["Готов к переезду", "Не готов"]
    cg = [g for g in snap["checkbox_groups"] if g["name"] == "relocate_chk"][0]
    assert cg["labels"] == ["Командировки"]
    # normalization: radio group -> ONE question with real options
    form = cap.normalize_to_application_form(snap, "hh:test")
    rq = [q for q in form.questions if q.normalized_type == QuestionType.RADIO]
    assert len(rq) == 1
    assert rq[0].options == ["Готов к переезду", "Не готов"]


# ---------- 11. required only from DOM ----------

def test_required_only_from_dom():
    snap = cap.inspect_page_dom(_page())
    by_name = {c["name"]: c for c in snap["controls"] if c.get("name")}
    assert by_name["phone"]["required"] is True       # DOM required attr
    assert by_name["resume_file"]["required"] is False  # not marked -> False, not guessed
    form = cap.normalize_to_application_form(snap, "hh:test")
    q = {x.id: x for x in form.questions}
    # phone control has no data-qa -> hash id; find by label
    phone_q = [x for x in form.questions if x.label == "Телефон"][0]
    assert phone_q.required is True


# ---------- 12. unknown control ----------

def test_unknown_control_requires_review():
    form = cap.normalize_to_application_form(cap.inspect_page_dom(_page()), "hh:test")
    uq = [q for q in form.questions if q.label == "Нечто неизвестное"][0]
    assert uq.normalized_type == QuestionType.UNKNOWN
    assert uq.requires_review is True


# ---------- 13. snapshot sanitization ----------

def test_snapshot_contains_no_secrets():
    dirty = {
        "url": "https://hh.ru/vacancy/1",
        "controls": [{"tag": "INPUT", "label": "x"}],
        "cookies": [{"name": "hhtoken", "value": "SECRET"}],
        "storage_state": {"cookies": []},
        "authorization": "Bearer abc",
        "password": "pw",
        "my_token_field": "t",
        "html": "<html>secret</html>",
    }
    clean = cap._sanitize(dirty)
    s = json.dumps(clean, ensure_ascii=False)
    for marker in ["SECRET", "Bearer", "pw", "hhtoken", "<html>"]:
        assert marker not in s
    for key in ["cookies", "storage_state", "authorization", "password", "my_token_field", "html"]:
        assert key not in clean
    # real snapshot path is sanitized too
    snap = cap.inspect_page_dom(_page())
    s2 = json.dumps(snap, ensure_ascii=False)
    assert "hhtoken" not in s2 and "Bearer" not in s2


# ---------- 14. deterministic normalization ----------

def test_deterministic_normalization():
    f1 = cap.normalize_to_application_form(cap.inspect_page_dom(_page()), "hh:test")
    f2 = cap.normalize_to_application_form(cap.inspect_page_dom(_page()), "hh:test")
    assert f1.model_dump_json() == f2.model_dump_json()


# ---------- source classification (17A contract) ----------

def test_source_classification_profile_system_screening():
    assert cap.classify_question_source("Телефон", "TEXT") == "PROFILE"
    assert cap.classify_question_source("Электронная почта / Email", "TEXT") == "PROFILE"
    assert cap.classify_question_source("Сопроводительное письмо", "TEXTAREA") == "SYSTEM"
    assert cap.classify_question_source("Резюме", "SELECT") == "SYSTEM"
    assert cap.classify_question_source("Готовы ли вы к переезду?", "RADIO") == "SCREENING"


def test_buttons_read_only_in_snapshot():
    snap = cap.inspect_page_dom(_page())
    submit = [b for b in snap["buttons"] if b["dataQa"] == "vacancy-response-submit"][0]
    assert submit["text"] == "Откликнуться"
    assert submit["type"] == "submit"
    # buttons are data only - no handles, no click capability stored
    assert isinstance(submit, dict)


# ---------- fail-closed on capture with unreachable CDP ----------

def test_capture_fail_closed_when_cdp_unreachable(monkeypatch):
    class BrokenChromium:
        @staticmethod
        def connect_over_cdp(url):
            raise RuntimeError("connection refused")

    class BrokenPW:
        chromium = BrokenChromium

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import playwright.sync_api as papi
    monkeypatch.setattr(papi, "sync_playwright", lambda: BrokenPW())
    res = cap.capture(cdp_url="http://127.0.0.1:9999", out_path=None)
    assert res["verdict"] == "BLOCKED_BY_MANUAL_FORM"
    assert "CDP not reachable" in res["reason"]