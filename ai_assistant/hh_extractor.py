"""HH application form extraction.

Stage 17B: real extraction + normalization of the HH application form.

IMPORTANT (safety / scope):
- This module is EXTRACTION ONLY. It never submits, never clicks Apply,
  never fills fields, never uploads, never calls an LLM, never mutates DB.
- It only opens the page and reads the DOM.

Confirmed from real HH inspection (2026-08-25, active vacancy 135112049):
- Screening question LABELS are rendered on the vacancy page WITHOUT login:
    <div data-qa="vacancy-response-question vacancy-response-question_work_place_location"
         class="magritte-text___pbpft_5-3-11 ...">Где располагается место работы?</div>
  The second whitespace-separated token of data-qa is a STABLE slug, e.g.
  work_place_location, employment_and_work_mode, is_vacancy_open,
  salary_options, how_to_contact, other.
- All question containers share an ancestor div.vacancy-response-suggest--* .
- Apply link (read-only reference, never clicked):
    <a data-qa="vacancy-response-link-top" href="/applicant/vacancy_response?vacancyId=...&employerId=...">
- WITHOUT login, the ANSWER CONTROLS (inputs/textareas/selects/radios/options)
  are NOT rendered: an auth-form is injected instead:
    <div data-qa="auth-form"> ... <input name="login" data-qa="account-signup-email"> ...
  => answer type/options/required cannot be determined without auth.
  Per the truth-only rule, such fields are emitted as UNKNOWN with
  requires_review=True and a clear reason. We DO NOT guess them.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class QuestionType(str, Enum):
    TEXT = "TEXT"
    TEXTAREA = "TEXTAREA"
    NUMBER = "NUMBER"
    SELECT = "SELECT"
    RADIO = "RADIO"
    CHECKBOX = "CHECKBOX"
    FILE = "FILE"
    COVER_LETTER = "COVER_LETTER"
    UNKNOWN = "UNKNOWN"


class QuestionSource(str, Enum):
    PROFILE = "PROFILE"
    SCREENING = "SCREENING"
    SYSTEM = "SYSTEM"


class ApplicationType(str, Enum):
    resume_only = "resume_only"
    cover_letter = "cover_letter"
    screening_questions = "screening_questions"
    resume_and_cover_letter = "resume_and_cover_letter"
    resume_and_questions = "resume_and_questions"
    full_application = "full_application"
    unknown = "unknown"


class ApplicationQuestion(BaseModel):
    id: str
    label: str = ""
    normalized_type: QuestionType = QuestionType.UNKNOWN
    # Tri-state: True/False = proven from DOM attributes; None = UNKNOWN
    # (HH questionnaires enforce requiredness client-side and expose nothing).
    required: Optional[bool] = None
    options: List[str] = Field(default_factory=list)
    source: QuestionSource = QuestionSource.SCREENING
    generated_answer: Optional[str] = None
    answer_type: Optional[QuestionType] = None
    confidence: float = 0.0
    requires_review: bool = True
    reason: str = ""
    # Stage 20C: id of the associated "Свой вариант" free-text textarea
    # (name = "<group>_text"), when provably linked in the DOM.
    custom_option_text_id: Optional[str] = None

    model_config = {"extra": "forbid"}


class ApplicationAnswer(BaseModel):
    question_id: str
    answer: Optional[str] = None
    answer_type: QuestionType = QuestionType.UNKNOWN
    confidence: float = 0.0
    requires_review: bool = True
    reason: str = ""

    model_config = {"extra": "forbid"}


class ApplicationForm(BaseModel):
    source: str = "hh"
    vacancy_stable_id: str = ""
    canonical_id: Optional[str] = None
    application_type: ApplicationType = ApplicationType.unknown
    questions: List[ApplicationQuestion] = Field(default_factory=list)
    extraction_meta: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


# Real, observed HH screening-question slugs (second token of data-qa).
# These are the standard "questions" HH renders on every vacancy.
OBSERVED_HH_QUESTION_SLUGS = [
    "work_place_location",
    "employment_and_work_mode",
    "is_vacancy_open",
    "salary_options",
    "how_to_contact",
    "other",
]


def _stable_id(label: str, slug: Optional[str]) -> str:
    """Deterministic, stable question id.

    Prefer the stable data-qa slug when present (HH provides it). Otherwise
    derive a stable hash from the label. Never random.
    """
    if slug:
        return f"hh__{slug}"
    base = (label or "").strip().lower()
    h = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
    return f"hh__hash_{h}"


def _control_stable_id(ctrl: Dict[str, Any], label: str) -> str:
    """Deterministic id for a real form control.

    Priority: data-qa > name > id > hash(label+type). Never random.
    """
    for key in ("dataQa", "name", "id"):
        v = (ctrl.get(key) or "").strip()
        if v:
            return f"hh__ctrl_{v}"
    return _stable_id(label, None)


_INPUT_TYPE_MAP = {
    "text": QuestionType.TEXT,
    "email": QuestionType.TEXT,
    "tel": QuestionType.TEXT,
    "url": QuestionType.TEXT,
    "number": QuestionType.NUMBER,
    "date": QuestionType.TEXT,
    "file": QuestionType.FILE,
    "textarea": QuestionType.TEXTAREA,
    "select": QuestionType.SELECT,
    "radio": QuestionType.RADIO,
    "checkbox": QuestionType.CHECKBOX,
}


def _coerce_options(options: Any) -> List[str]:
    """Options as plain strings. Accepts Stage 18 strings and Stage 20A rich
    dicts ({text, value, disabled}); never invents entries."""
    out: List[str] = []
    for o in options or []:
        if isinstance(o, dict):
            t = (o.get("text") or o.get("value") or "").strip()
            if t:
                out.append(t)
        else:
            s = str(o).strip()
            if s:
                out.append(s)
    return out


def _tri_state_required(ctrl: Dict[str, Any]) -> Optional[bool]:
    """Tri-state required: True/False when provable from DOM, None when unknown.

    Backward compatible: legacy captures without `requiredAttr` fall back to
    the boolean `required` field (Stage 18 behavior).
    """
    if "requiredAttr" in ctrl:
        ra = ctrl.get("requiredAttr")
        if ra is True or ra is False:
            return bool(ra)
        return None
    return bool(ctrl.get("required"))


def _question_from_control(ctrl: Dict[str, Any]) -> Optional[ApplicationQuestion]:
    """Build an ApplicationQuestion from a REAL DOM control.

    Reads only what the DOM exposes: type, required attribute, options.
    Anything not determinable stays UNKNOWN + requires_review.
    """
    tag = (ctrl.get("tag") or "").upper()
    raw_type = (ctrl.get("type") or "").lower()
    label = (ctrl.get("label") or "").strip()
    required = _tri_state_required(ctrl)
    options = _coerce_options(ctrl.get("options")) or None

    qtype = _INPUT_TYPE_MAP.get(raw_type)
    if tag == "TEXTAREA":
        qtype = QuestionType.TEXTAREA
    elif tag == "SELECT":
        qtype = QuestionType.SELECT

    if qtype is None:
        return ApplicationQuestion(
            id=_control_stable_id(ctrl, label),
            label=label,
            normalized_type=QuestionType.UNKNOWN,
            required=None,
            options=[],
            source=QuestionSource.SCREENING,
            confidence=0.0,
            requires_review=True,
            reason=f"Control type '{raw_type or tag}' not recognized - kept UNKNOWN",
        )

    # Option-constrained types: options must come from the DOM.
    if qtype in (QuestionType.SELECT, QuestionType.RADIO, QuestionType.CHECKBOX):
        if not options:
            return ApplicationQuestion(
                id=_control_stable_id(ctrl, label),
                label=label,
                normalized_type=qtype,
                required=required,
                options=[],
                source=QuestionSource.SCREENING,
                confidence=0.0,
                requires_review=True,
                reason="Option-constrained control without visible options in DOM - cannot resolve safely",
            )
        return ApplicationQuestion(
            id=_control_stable_id(ctrl, label),
            label=label,
            normalized_type=qtype,
            required=required,
            options=list(options),
            source=QuestionSource.SCREENING,
            confidence=1.0,
            requires_review=False,
            reason="",
        )
    # Free-text / file types: type and required are visible in DOM.
    return ApplicationQuestion(
        id=_control_stable_id(ctrl, label),
        label=label,
        normalized_type=qtype,
        required=required,
        options=[],
        source=QuestionSource.SCREENING,
        confidence=1.0,
        requires_review=False,
        reason="",
    )


def _choice_groups(controls: List[Dict[str, Any]], input_type: str) -> List[Dict[str, Any]]:
    """Group radio OR checkbox inputs by name into a single logical question
    with options built from the REAL labels of each member."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for c in controls:
        if (c.get("tag") or "").upper() == "INPUT" and (c.get("type") or "").lower() == input_type:
            key = (c.get("name") or c.get("dataQa") or c.get("id") or "").strip()
            if not key:
                key = f"__anon_{c.get('id') or c.get('dataQa') or ''}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(c)
    result = []
    for key in order:
        members = groups[key]
        options = []
        for m in members:
            lab = (m.get("label") or "").strip()
            if lab and lab not in options:
                options.append(lab)
        first = members[0]
        # tri-state group required: True only if some member proves it;
        # None when no member carries an explicit DOM marker (HH case)
        reqs = [_tri_state_required(m) for m in members]
        if any(r is True for r in reqs):
            group_required: Optional[bool] = True
        elif all(r is False for r in reqs):
            group_required = False
        else:
            group_required = None
        result.append({
            "tag": "INPUT",
            "type": input_type,
            "name": (first.get("name") or key),
            "id": first.get("id"),
            "dataQa": first.get("dataQa"),
            "required": group_required,
            "requiredAttr": group_required,
            "label": next(((m.get("label") or "").strip() for m in members if (m.get("label") or "").strip()), key),
            "options": options,
        })
    return result


def _radio_groups(controls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Backward-compatible wrapper: radio-only grouping."""
    return _choice_groups(controls, "radio")


def _checkbox_groups(controls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stage 20C: checkbox inputs grouped by name - one multi-select question
    with the REAL option labels (never one question per checkbox)."""
    return _choice_groups(controls, "checkbox")


def build_questions_from_controls(
    controls: List[Dict[str, Any]],
    question_groups: Optional[List[Dict[str, Any]]] = None,
) -> List[ApplicationQuestion]:
    """Normalize REAL DOM controls into questions.

    - radio AND checkbox inputs are grouped by name (Stage 18 + Stage 20C):
      one question per group with the real option labels;
    - ``question_groups`` carries DOM-proven question stems (Stage 20C live
      capture: sibling heading of the options container) and is the ONLY
      source of question text; without it the group label falls back to the
      first option text and the question is flagged requires_review;
    - a textarea named ``<group>_text`` is linked to its group via
      ``custom_option_text_id`` (the "Свой вариант" free-text field) and is
      NOT emitted as an independent question;
    - standalone textareas keep their own (stem-derived) label when provable.
    """
    qg_by_name: Dict[str, Dict[str, Any]] = {}
    for g in question_groups or []:
        if g.get("name"):
            qg_by_name[g["name"]] = g

    questions: List[ApplicationQuestion] = []
    consumed_textareas: set = set()
    seen_group_names: set = set()

    radio_groups = {g["name"]: g for g in _choice_groups(controls, "radio")}
    checkbox_groups = {g["name"]: g for g in _choice_groups(controls, "checkbox")}

    def _emit_group(group: Dict[str, Any]) -> None:
        name = group["name"]
        if name in seen_group_names:
            return
        seen_group_names.add(name)
        q = _question_from_control(group)
        meta = qg_by_name.get(name)
        if meta and meta.get("stem"):
            q.label = meta["stem"]
        else:
            # No DOM-provable stem: label is only the first option text.
            q.requires_review = True
            q.reason = (q.reason or "Question stem not determinable from DOM; "
                        "label is the first option text - needs review")
        # link "<name>_text" custom-variant textarea when present
        custom_name = f"{name}_text"
        custom_ctrl = next((c for c in controls
                            if (c.get("tag") or "").upper() == "TEXTAREA"
                            and (c.get("name") or "") == custom_name), None)
        if custom_ctrl is not None:
            q.custom_option_text_id = f"hh__ctrl_{custom_name}"
            consumed_textareas.add(custom_name)
        questions.append(q)

    for c in controls or []:
        tag = (c.get("tag") or "").upper()
        ctype = (c.get("type") or "").lower()
        name = (c.get("name") or "").strip()
        if tag == "INPUT" and ctype in ("radio", "checkbox") and name:
            group_map = radio_groups if ctype == "radio" else checkbox_groups
            if name in group_map:
                _emit_group(group_map[name])
                continue
        if tag == "TEXTAREA":
            if name in consumed_textareas:
                continue  # consumed by its group ("Свой вариант" field)
            q = _question_from_control(c)
            meta = qg_by_name.get(name)
            if meta and meta.get("stem"):
                q.label = meta["stem"]
            else:
                q.requires_review = True
                q.reason = (q.reason or "Question stem not determinable from DOM; "
                            "label may be placeholder text - needs review")
            questions.append(q)
            continue
        questions.append(_question_from_control(c))
    return questions


def _classify_known_slug(slug: str) -> ApplicationType:
    """Map observed HH slugs to an application_type (best-effort, no guessing
    of individual field types)."""
    # These are the standard employer screening questions HH asks.
    return ApplicationType.screening_questions


def _detect_blocked(html: str, body_text: str) -> Dict[str, Any]:
    """Detect CAPTCHA / login / Cloudflare from real DOM markers.

    NOTE: on the real active vacancy, the string 'captcha' appears inside the
    JS i18n bundle (error-message translations), NOT as an active challenge.
    So we only treat it as blocked when it is inside an active challenge
    container (data-qa / known class), never from raw text alone.
    """
    low_html = html.lower()
    captcha_active = ("data-qa=\"captcha" in low_html) or ("captcha" in low_html and "bloko-modal" in low_html)
    cloudflare = "cf-challenge" in low_html or "challenge-platform" in low_html or "cf-error" in low_html
    # Login/account required is driven by presence of auth-form (not text).
    return {
        "captcha": captcha_active,
        "cloudflare": cloudflare,
    }


def extract_application_form(
    vacancy_stable_id: str,
    url: str,
    dom_snapshot: Dict[str, Any],
    canonical_id: Optional[str] = None,
) -> ApplicationForm:
    """Normalize a real DOM snapshot into an ApplicationForm.

    dom_snapshot is produced by the browser layer (see BrowserAdapter
    .extract_application_form) and contains fields like:
      - html: full page HTML
      - body_text: innerText of body
      - questions: list of {label, slug} from data-qa vacancy-response-question
      - auth_form: bool (whether an auth-form was present)
      - apply_link: {href, text} | None
      - final_url, title
      - site

    This function performs ONLY normalization + classification. It performs
    no network/browser/LLM work.
    """
    html = dom_snapshot.get("html", "") or ""
    body_text = dom_snapshot.get("body_text", "") or ""
    blocked = _detect_blocked(html, body_text)
    auth_form = bool(dom_snapshot.get("auth_form"))
    raw_controls = dom_snapshot.get("controls") or []

    raw_questions = dom_snapshot.get("questions") or []
    questions: List[ApplicationQuestion] = []

    if raw_controls and not auth_form:
        # Stage 18/20C: authenticated (or otherwise exposed) form - real
        # controls are visible. Build questions from REAL controls with real
        # types, options and required flags. Never invent anything.
        questions = build_questions_from_controls(
            raw_controls, question_groups=dom_snapshot.get("question_groups"))
    else:
        for i, q in enumerate(raw_questions):
            label = (q.get("label") or "").strip()
            slug = (q.get("slug") or "").strip() or None

            # Answer controls are NOT rendered without auth. We cannot determine
            # type / options / required truthfully. Mark UNKNOWN + review.
            if auth_form:
                questions.append(
                    ApplicationQuestion(
                        id=_stable_id(label, slug),
                        label=label,
                        normalized_type=QuestionType.UNKNOWN,
                        required=None,
                        options=[],
                        source=QuestionSource.SCREENING,
                        confidence=0.0,
                        requires_review=True,
                        reason="Answer controls hidden behind auth-form; type/options/required not extractable without login",
                    )
                )
                continue

            # No auth: on a normal HH page there is always a question container.
            # Keep UNKNOWN for safety (no guessed type).
            questions.append(
                ApplicationQuestion(
                    id=_stable_id(label, slug),
                    label=label,
                    normalized_type=QuestionType.UNKNOWN,
                    required=None,
                    options=[],
                    source=QuestionSource.SCREENING,
                    confidence=0.0,
                    requires_review=True,
                    reason="Field type/options/required not determinable from DOM without authenticated form",
                )
            )

    # application_type: best-effort, no guessing of individual field types.
    if not questions:
        app_type = ApplicationType.unknown
    elif any(q.label and q.id for q in questions):
        app_type = ApplicationType.screening_questions
    else:
        app_type = ApplicationType.unknown

    form = ApplicationForm(
        source="hh",
        vacancy_stable_id=vacancy_stable_id,
        canonical_id=canonical_id,
        application_type=app_type,
        questions=questions,
        extraction_meta={
            "url": dom_snapshot.get("final_url") or url,
            "title": dom_snapshot.get("title") or "",
            "site": dom_snapshot.get("site") or "hh.ru",
            "auth_form": auth_form,
            "captcha": blocked.get("captcha", False),
            "cloudflare": blocked.get("cloudflare", False),
            "apply_link_href": (dom_snapshot.get("apply_link") or {}).get("href"),
            "observed_slug_count": len(raw_questions),
            "observed_control_count": len(raw_controls),
            "controls_used": bool(raw_controls) and not auth_form,
        },
    )
    return form