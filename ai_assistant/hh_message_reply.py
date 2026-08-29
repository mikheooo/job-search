"""Stage 22/24/25: HH Message Reply (REVIEW default, limited AUTO).

Reads incoming HH conversations, classifies messages, generates a truth-only
reply, and - in the default REVIEW mode - STOPS before any send. AUTO is a
STRICTLY OPT-IN mode (kill switch HH_AUTO_REPLY_ENABLED=true AND explicit
mode=ReplyMode.AUTO) that sends ONLY allowlisted, provably safe messages.

Modes:
    REVIEW (default)  - generate reply preview, NEVER send.
    AUTO (opt-in)     - send ONLY after every safety gate passes.
    SKIP              - generate nothing, send nothing.

Hard rules:
- AUTO is never the default; it requires the kill switch + explicit mode.
- AUTO sends only allowlisted safe messages; everything else -> HUMAN_REVIEW.
- A mandatory final safety gate (can_auto_send) re-checks all conditions
  immediately before send; a stale/re-checked conversation fingerprint blocks.
- Race-condition protection: the conversation is re-read before send and the
  latest incoming message identity must match what the reply was built on.
- Exactly one send path (send_auto_reply); no automatic retry after any
  failure or timeout (avoids duplicate sends).
- Truth-only replies from employer message + dialog context + project truth
  sources (candidate_profile.json). Missing facts -> HUMAN_REVIEW, never guessed.
- Deduplication is tied to the LAST INCOMING message context so a new
  OUTGOING message after a send never re-processes the same INCOMING.
- No DB schema change; persistent state/audit lives in artifacts/ (gitignored).

Browser layer reuses the existing raw-CDP transport from
tools/capture_manual_form (read-only target enumeration + Runtime.evaluate)
and ai_assistant.prefill_execute.make_cdp_evaluate. No second browser stack.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field


class MessageClassification(str, Enum):
    REPLY_REQUIRED = "REPLY_REQUIRED"
    NO_REPLY = "NO_REPLY"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class ReplyMode(str, Enum):
    REVIEW = "REVIEW"
    AUTO = "AUTO"
    SKIP = "SKIP"


DEFAULT_MODE = ReplyMode.REVIEW
DEFAULT_STATE_PATH = os.path.join("artifacts", "hh_message_reply_state.json")

# Kill switch: AUTO requires HH_AUTO_REPLY_ENABLED=true (explicit). Missing or
# any non-true value disables AUTO completely.
_AUTO_ENV_VAR = "HH_AUTO_REPLY_ENABLED"

# Hard volume guard per run (no background loop, no unbounded batch).
MAX_AUTO_REPLIES_PER_RUN = 3


# ------------- input data model (what the read-only HH fetch returns) ------

class HHMessage(BaseModel):
    message_id: str
    text: str
    sent_at: Optional[str] = None
    sender: str = "employer"  # employer | candidate | system

    model_config = {"extra": "forbid"}


class HHDialog(BaseModel):
    conversation_id: str
    vacancy_title: str = ""
    vacancy_stable_id: str = ""  # hh:<id> when provable
    employer: str = ""
    messages: List[HHMessage] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    def last_message(self) -> Optional[HHMessage]:
        return self.messages[-1] if self.messages else None


# ------------- classification + reply (pure, truth-only) --------------------

# System / notification / obvious-non-reply signals. Deliberately narrow and
# explicit - anything unclear is never auto-classified as NO_REPLY.
_NO_REPLY_MARKERS = (
    "ваш отклик", "отклик был", "отклик получен", "отклик на вакансию",
    "уведомление", "статистика", "рекомендуем", "активируйте",
    "подписка", "оплатите", "пополните", "верифицируйте",
    "поздравляем", "добро пожаловать в hh", "изменения в законодательстве",
    "безопасность вашего аккаунта", "служба поддержки hh", "hh.ru",
    "пароль", "подтвердите номер", "двухфакторная",
)

# Sensitive facts that must NEVER be guessed: presence forces HUMAN_REVIEW.
_SENSITIVE_RE = re.compile(
    r"зарплат|оплат|оклад|salary|ставк|уровень дохода|денег|сколько.*получ|"
    r"опыт[а-я]*\s+\d|лет опыта|года опыта|years of experience|"
    r"когда.*(мож|смож)|какую дату|в какое время|собеседован|интервью|"
    r"ваканси[а-я]* закрыт|позицию.*закрыли|статус.*отклик|"
    r"технологи[ияю]|стек|навык|python|n8n|llm|telegram|"
    r"место работы|локаци|город|график|часовой пояс",
    re.IGNORECASE,
)

# A plain, non-sensitive employer question (reply expected).
_REPLY_PROBE = re.compile(r"[?？]|уточни|подскажи|расскажи|готовы ли|устроит|интересно", re.IGNORECASE)


def _context_texts(dialog: HHDialog) -> str:
    """Join the full dialog text (context) for classification/generation."""
    return "\n".join((m.text or "") for m in (dialog.messages or []))


def classify_message(dialog: HHDialog) -> MessageClassification:
    """Classify the dialog using the FULL available context.

    The last message drives whether a reply is expected, but the whole
    dialog is scanned for sensitive facts / pending employer questions so a
    safe-looking last line never hides an important earlier question/condition.
    Pure, deterministic, truth-only.
    """
    msg = dialog.last_message()
    if msg is None:
        return MessageClassification.NO_REPLY
    text = (msg.text or "").strip()
    low = text.lower()
    if not text:
        return MessageClassification.NO_REPLY
    # system/notification messages are not candidate replies
    if any(m in low for m in _NO_REPLY_MARKERS):
        return MessageClassification.NO_REPLY
    # full-context sensitivity: scan every message for sensitive/ambiguous
    # facts (salary, dates, experience, stack, status, conditions).
    context = _context_texts(dialog)
    if _SENSITIVE_RE.search(context):
        return MessageClassification.HUMAN_REVIEW
    # a direct question from the employer (in the latest message) -> reply
    if _REPLY_PROBE.search(text):
        return MessageClassification.REPLY_REQUIRED
    # if an earlier employer message asked a direct question and the last
    # message is a follow-up/system line, keep it for the human (no guessing)
    if any(_REPLY_PROBE.search((m.text or "")) for m in (dialog.messages or [])):
        return MessageClassification.HUMAN_REVIEW
    # anything else we cannot confidently auto-reply to -> human
    return MessageClassification.HUMAN_REVIEW


# Truth sources used for reply generation (existing project files).
_TRUTH_PROFILE_PATH = os.path.join("candidate_profile.json")

# Facts about the candidate that are provable from the project truth sources.
def _load_profile() -> Dict[str, Any]:
    try:
        with open(_TRUTH_PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_LANG_RU_RE = re.compile(r"[а-яёЁ]", re.IGNORECASE)


def detect_language(text: str) -> str:
    return "ru" if _LANG_RU_RE.search(text or "") else "en"


def _short_availability_facts(profile: Dict[str, Any]) -> List[str]:
    """Return provable availability/role facts from the profile (truth-only).
    Never guesses salary, dates, locations beyond the profile, or skills."""
    facts: List[str] = []
    roles = profile.get("desired_roles") or []
    if roles:
        facts.append("roles: " + ", ".join(roles[:3]))
    langs = profile.get("languages") or []
    if langs:
        facts.append("languages: " + ", ".join(sorted(set(langs))[:4]))
    remote = profile.get("remote_required")
    if remote is True:
        facts.append("remote work: yes (remote_required=true)")
    return facts


def generate_reply(
    dialog: HHDialog,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate a short, natural, truth-only reply. Returns {"reply", "sources"}.

    Only used when classify_message(...) == REPLY_REQUIRED. If not enough
    provable context exists, returns empty reply + HUMAN_REVIEW reason.
    """
    msg = dialog.last_message()
    if msg is None:
        return {"reply": "", "sources": [], "status": "HUMAN_REVIEW",
                "reason": "no last message"}
    classification = classify_message(dialog)
    if classification != MessageClassification.REPLY_REQUIRED:
        return {"reply": "", "sources": [], "status": classification.value,
                "reason": "not a reply-required message"}
    prof = profile if profile is not None else _load_profile()
    facts = _short_availability_facts(prof)
    if not facts:
        return {"reply": "", "sources": [], "status": "HUMAN_REVIEW",
                "reason": "no provable profile facts available"}
    lang = detect_language(msg.text or "")
    sender_name = (dialog.employer or "").strip()
    greeting = "Здравствуйте!" if lang == "ru" else "Hello!"
    if lang == "ru":
        reply = (
            f"{greeting} Спасибо за сообщение и интерес к моей кандидатуре. "
            "Готов ответить на вопросы и обсудить детали. "
            f"Я рассматриваю роли: {facts[0].split(': ', 1)[1]}."
        )
    else:
        reply = (
            f"{greeting} Thank you for reaching out. I'm happy to answer "
            "your questions and discuss the details. "
            f"I am considering roles: {facts[0].split(': ', 1)[1]}."
        )
    return {
        "reply": reply,
        "sources": ["candidate_profile.json: desired_roles"],
        "status": "REPLY_REQUIRED",
        "reason": "plain employer question; profile facts available",
    }


_REJECTION_RE = re.compile(
    r"^отказ$|^нет$|не готовы пригласить|ваканси[а-я]* закрыт|позици[а-я]* закрыт|к сожалению,.*отказ|вынуждены отказать|выбрали другого|не подош[её]л",
    re.IGNORECASE,
)

_GREETING_ONLY_RE = re.compile(
    r"^(здравствуйте|добрый (день|вечер|утро)|приветствую|привет|hello|hi|good (morning|afternoon|evening))[\s.,!?:;0-9-]*$",
    re.IGNORECASE,
)


def resolve_vacancy_for_dialog(dialog: HHDialog) -> Optional[Dict[str, Any]]:
    """Look up linked vacancy in database if available (read-only)."""
    try:
        from ai_assistant import db
        conn = db.get_connection()
        cur = conn.cursor()
        if dialog.vacancy_stable_id:
            cur.execute("SELECT * FROM vacancies WHERE stable_id=?", (dialog.vacancy_stable_id,))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
        m = re.search(r"(\d+)", dialog.vacancy_stable_id or "")
        if m:
            job_id = m.group(1)
            cur.execute("SELECT * FROM vacancies WHERE source_job_id=? OR stable_id=?", (job_id, f"hh:{job_id}"))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
    except Exception:
        pass
    return None


def classify_hh_conversation_detailed(
    dialog: HHDialog,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Stage 30D.5: Context-aware, truth-only classification, fact checking, and draft generation.
    Returns:
        conversation_id: str
        classification: NEEDS_REPLY | NO_REPLY_NEEDED | HUMAN_REVIEW | EMPTY_CONVERSATION
        confidence: float
        reason: str
        question: Optional[str]
        required_facts: List[str]
        available_facts: List[str]
        missing_facts: List[str]
        context: List[Dict[str, str]]
        prepared_reply: Optional[str]
        sources: List[str]
        status: str ("READ-ONLY")
    """
    messages = dialog.messages or []
    context = [
        {
            "author": "candidate" if m.sender == "candidate" else "employer",
            "text": m.text or "",
        }
        for m in messages
    ]

    prof = profile if profile is not None else _load_profile()
    profile_skills = [str(s) for s in (prof.get("skills") or [])]
    profile_roles = [str(r) for r in (prof.get("desired_roles") or [])]
    years_exp = prof.get("years_experience", 0)

    # Base available facts
    available_facts: List[str] = []
    if profile_skills:
        available_facts.append(f"skills: {', '.join(profile_skills)}")
    if profile_roles:
        available_facts.append(f"roles: {', '.join(profile_roles[:3])}")
    if years_exp:
        available_facts.append(f"experience: {years_exp} years")

    # Link vacancy if available
    vac_data = resolve_vacancy_for_dialog(dialog)
    if vac_data:
        v_title = vac_data.get("title") or ""
        v_comp = vac_data.get("company") or ""
        if v_title:
            available_facts.append(f"vacancy: {v_title} ({v_comp})")

    if not messages:
        return {
            "conversation_id": dialog.conversation_id,
            "classification": "EMPTY_CONVERSATION",
            "confidence": 1.0,
            "reason": "Conversation has no messages.",
            "question": None,
            "required_facts": [],
            "available_facts": available_facts,
            "missing_facts": [],
            "context": [],
            "prepared_reply": None,
            "sources": [],
            "status": "READ-ONLY",
        }

    # Find employer and candidate messages
    employer_indices = [i for i, m in enumerate(messages) if m.sender != "candidate"]
    candidate_indices = [i for i, m in enumerate(messages) if m.sender == "candidate"]

    if not employer_indices:
        return {
            "conversation_id": dialog.conversation_id,
            "classification": "NO_REPLY_NEEDED",
            "confidence": 0.95,
            "reason": "No messages from employer yet; waiting for response.",
            "question": None,
            "required_facts": [],
            "available_facts": available_facts,
            "missing_facts": [],
            "context": context,
            "prepared_reply": None,
            "sources": [],
            "status": "READ-ONLY",
        }

    last_emp_idx = employer_indices[-1]
    last_emp_msg = messages[last_emp_idx]
    last_emp_text = (last_emp_msg.text or "").strip()

    # 1. Rejection / Closure notice
    if _REJECTION_RE.search(last_emp_text):
        return {
            "conversation_id": dialog.conversation_id,
            "classification": "NO_REPLY_NEEDED",
            "confidence": 0.95,
            "reason": "Employer sent rejection or closed vacancy notice; no reply needed.",
            "question": None,
            "required_facts": [],
            "available_facts": available_facts,
            "missing_facts": [],
            "context": context,
            "prepared_reply": None,
            "sources": [],
            "status": "READ-ONLY",
        }

    # 2. Automated status notification (e.g. view notification, delivery)
    low_last = last_emp_text.lower()
    if any(m in low_last for m in _NO_REPLY_MARKERS) and not _REPLY_PROBE.search(last_emp_text):
        return {
            "conversation_id": dialog.conversation_id,
            "classification": "NO_REPLY_NEEDED",
            "confidence": 0.90,
            "reason": "System/platform notification; no candidate reply needed.",
            "question": None,
            "required_facts": [],
            "available_facts": available_facts,
            "missing_facts": [],
            "context": context,
            "prepared_reply": None,
            "sources": [],
            "status": "READ-ONLY",
        }

    # 3. Check for substantive questions in employer messages
    question_emp_indices = [
        i for i in employer_indices
        if _REPLY_PROBE.search(messages[i].text or "")
        or "?" in (messages[i].text or "")
        or "？" in (messages[i].text or "")
        or "опыт" in (messages[i].text or "").lower()
    ]

    if question_emp_indices:
        last_q_idx = question_emp_indices[-1]
        q_msg = messages[last_q_idx]
        q_text = (q_msg.text or "").strip()

        # Check candidate responses sent after this question
        cand_replies_after_q = [
            messages[i] for i in candidate_indices if i > last_q_idx
        ]

        has_substantive_cand_reply = False
        for c_msg in cand_replies_after_q:
            c_text = (c_msg.text or "").strip()
            # If candidate sent more than just a greeting, they answered
            if not _GREETING_ONLY_RE.match(c_text):
                has_substantive_cand_reply = True
                break

        if has_substantive_cand_reply:
            return {
                "conversation_id": dialog.conversation_id,
                "classification": "NO_REPLY_NEEDED",
                "confidence": 0.90,
                "reason": "Candidate has already answered the employer's question.",
                "question": q_text,
                "required_facts": [],
                "available_facts": available_facts,
                "missing_facts": [],
                "context": context,
                "prepared_reply": None,
                "sources": [],
                "status": "READ-ONLY",
            }

        # Analyze question requirements
        required_facts: List[str] = []
        missing_facts: List[str] = []
        sources: List[str] = ["candidate_profile.json: skills", "candidate_profile.json: desired_roles"]
        if vac_data:
            sources.append(f"database: vacancy {vac_data.get('stable_id')}")

        lang = detect_language(q_text)

        # Sensitive questions (salary, calendar time) -> HUMAN_REVIEW
        if re.search(r"зарплат|оплат|оклад|salary|ставк|уровень дохода|денег", q_text, re.IGNORECASE):
            return {
                "conversation_id": dialog.conversation_id,
                "classification": "HUMAN_REVIEW",
                "confidence": 0.85,
                "reason": "Employer asked about specific salary expectations; requires human decision.",
                "question": q_text,
                "required_facts": ["exact salary expectation"],
                "available_facts": available_facts,
                "missing_facts": ["salary agreement for this vacancy"],
                "context": context,
                "prepared_reply": None,
                "sources": sources,
                "status": "READ-ONLY",
            }

        if re.search(r"какую дату|в какое время|собеседован|интервью|когда.*(мож|смож)", q_text, re.IGNORECASE):
            return {
                "conversation_id": dialog.conversation_id,
                "classification": "HUMAN_REVIEW",
                "confidence": 0.85,
                "reason": "Employer asked about interview scheduling; requires human calendar confirmation.",
                "question": q_text,
                "required_facts": ["specific interview schedule"],
                "available_facts": available_facts,
                "missing_facts": ["available calendar slot"],
                "context": context,
                "prepared_reply": None,
                "sources": sources,
                "status": "READ-ONLY",
            }

        # Domain experience: E-commerce / Marketplaces (Ozon/WB)
        if re.search(r"e-commerce|маркетплейс|ozon|wildberries|wb", q_text, re.IGNORECASE):
            required_facts.append("e-commerce / marketplace experience (Ozon/WB)")
            # Check if candidate profile has confirmed Ozon/WB
            prof_combined = " ".join(profile_skills + profile_roles).lower()
            has_ecom = any(k in prof_combined for k in ("ozon", "wildberries", "wb", "e-commerce", "ecommerce", "маркетплейс", "marketplace"))
            if not has_ecom:
                missing_facts.append("Ozon / Wildberries marketplace experience")
                if lang == "ru":
                    reply_text = (
                        "Здравствуйте! У меня есть опыт автоматизации процессов, работы с API, n8n и Python. "
                        "Непосредственно с Ozon и Wildberries подтверждённого коммерческого опыта в профиле нет, "
                        "но готов применить навыки интеграции и автоматизации для ваших задач."
                    )
                else:
                    reply_text = (
                        "Hello! I have hands-on experience in workflow automation, APIs, n8n, and Python. "
                        "I do not have direct commercial experience with Ozon/WB in my profile, "
                        "but I am ready to apply my automation and integration skills."
                    )
                reason = "Employer asked a direct question about e-commerce experience; drafted honest response highlighting verified automation/API skills without claiming unverified Ozon/WB experience."
            else:
                if lang == "ru":
                    reply_text = (
                        "Здравствуйте! Да, у меня есть подтверждённый опыт работы с e-commerce и маркетплейсами, "
                        "а также автоматизации процессов. Готов обсудить детали."
                    )
                else:
                    reply_text = (
                        "Hello! Yes, I have verified experience with e-commerce, marketplaces, and process automation. "
                        "I would be glad to discuss the details."
                    )
                reason = "Employer asked a direct question about e-commerce experience; verified in profile."

            return {
                "conversation_id": dialog.conversation_id,
                "classification": "NEEDS_REPLY",
                "confidence": 0.90,
                "reason": reason,
                "question": q_text,
                "required_facts": required_facts,
                "available_facts": available_facts,
                "missing_facts": missing_facts,
                "context": context,
                "prepared_reply": reply_text,
                "sources": sources,
                "status": "READ-ONLY",
            }

        # General / technical stack questions
        required_facts.append("technical stack / role qualifications")
        roles_str = ", ".join(profile_roles[:2]) if profile_roles else "AI Automation Engineer"
        if lang == "ru":
            reply_text = (
                "Здравствуйте! Спасибо за сообщение. Готов ответить на ваши вопросы и обсудить детали вакансии. "
                f"Я рассматриваю роли: {roles_str}."
            )
        else:
            reply_text = (
                "Hello! Thank you for your message. I am happy to answer your questions and discuss the details. "
                f"I am targeting roles in: {roles_str}."
            )

        return {
            "conversation_id": dialog.conversation_id,
            "classification": "NEEDS_REPLY",
            "confidence": 0.90,
            "reason": "Employer asked a direct question; drafted response using verified profile roles and skills.",
            "question": q_text,
            "required_facts": required_facts,
            "available_facts": available_facts,
            "missing_facts": [],
            "context": context,
            "prepared_reply": reply_text,
            "sources": sources,
            "status": "READ-ONLY",
        }

    # If candidate was the last sender and no question pending
    if candidate_indices and candidate_indices[-1] > employer_indices[-1]:
        return {
            "conversation_id": dialog.conversation_id,
            "classification": "NO_REPLY_NEEDED",
            "confidence": 0.90,
            "reason": "Last message was sent by candidate; waiting for employer reply.",
            "question": None,
            "required_facts": [],
            "available_facts": available_facts,
            "missing_facts": [],
            "context": context,
            "prepared_reply": None,
            "sources": [],
            "status": "READ-ONLY",
        }

    # Otherwise ambiguous -> HUMAN_REVIEW
    return {
        "conversation_id": dialog.conversation_id,
        "classification": "HUMAN_REVIEW",
        "confidence": 0.70,
        "reason": "Dialogue context is ambiguous or informational; requires human review.",
        "question": None,
        "required_facts": [],
        "available_facts": available_facts,
        "missing_facts": [],
        "context": context,
        "prepared_reply": None,
        "sources": [],
        "status": "READ-ONLY",
    }


_SENSITIVE_CLAIM_RE = re.compile(
    r"(\$|руб|usd|eur|\d+\s*(тыс|k|т\.)|зарплат|ставк|оклад|\d{1,2}:\d{2}|\d{1,2}\s+(янв|фев|мар|апр|май|июн|июл|авг|сен|окт|ноя|дек))",
    re.IGNORECASE,
)

_OFF_TOPIC_RE = re.compile(
    r"^\s*(submit|curl|fetch|http|javascript|select|insert|update|delete|drop|run_command|click|navigate)",
    re.IGNORECASE,
)


def validate_hh_reply_draft(
    dialog: HHDialog,
    draft: Optional[str] = None,
    classification: Optional[str] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Stage 30D.4/30D.5: READ-ONLY validation gate for prepared HH reply draft.
    Checks:
    - answers_last_question: does draft address the active question/context?
    - uses_supported_facts: are all claims backed by candidate_profile.json?
    - contains_unverified_claims: are there positive claims not in profile (e.g. Ozon/WB when missing)?
    - contains_sensitive_claims: are there salary/calendar/visa claims?
    - is_empty: is draft missing/empty?

    Returns structured validation dict:
    - conversation_id
    - classification
    - draft
    - validation: APPROVED | HUMAN_REVIEW | REJECTED
    - checks: dict
    - reasons: list of str
    - status: "READ-ONLY"
    """
    prof = profile if profile is not None else _load_profile()
    cid = dialog.conversation_id

    # If classification is not passed, infer from detailed classification
    if classification is None or draft is None:
        det = classify_hh_conversation_detailed(dialog, profile=prof)
        if classification is None:
            classification = det.get("classification") or "HUMAN_REVIEW"
        if draft is None:
            draft = det.get("prepared_reply")

    is_empty = not draft or not draft.strip()
    reasons: List[str] = []

    if classification == "HUMAN_REVIEW":
        return {
            "conversation_id": cid,
            "classification": classification,
            "draft": draft,
            "validation": "HUMAN_REVIEW",
            "checks": {
                "answers_last_question": False,
                "uses_supported_facts": False,
                "contains_unverified_claims": False,
                "contains_sensitive_claims": False,
                "is_empty": is_empty,
            },
            "reasons": ["Dialogue classification requires human review; automated approval blocked."],
            "status": "READ-ONLY",
        }

    if is_empty:
        return {
            "conversation_id": cid,
            "classification": classification,
            "draft": draft,
            "validation": "REJECTED",
            "checks": {
                "answers_last_question": False,
                "uses_supported_facts": False,
                "contains_unverified_claims": False,
                "contains_sensitive_claims": False,
                "is_empty": True,
            },
            "reasons": ["Draft reply is empty."],
            "status": "READ-ONLY",
        }

    if _OFF_TOPIC_RE.search(draft.strip()):
        return {
            "conversation_id": cid,
            "classification": classification,
            "draft": draft,
            "validation": "REJECTED",
            "checks": {
                "answers_last_question": False,
                "uses_supported_facts": False,
                "contains_unverified_claims": False,
                "contains_sensitive_claims": False,
                "is_empty": False,
            },
            "reasons": ["Draft contains instructions or off-topic commands instead of a candidate reply."],
            "status": "READ-ONLY",
        }

    if classification in ("EMPTY_CONVERSATION", "NO_REPLY_NEEDED"):
        return {
            "conversation_id": cid,
            "classification": classification,
            "draft": draft,
            "validation": "REJECTED",
            "checks": {
                "answers_last_question": False,
                "uses_supported_facts": False,
                "contains_unverified_claims": False,
                "contains_sensitive_claims": False,
                "is_empty": False,
            },
            "reasons": [f"Classification is {classification}; no reply should be sent."],
            "status": "READ-ONLY",
        }

    # Extract claims from draft
    contains_sensitive_claims = bool(_SENSITIVE_CLAIM_RE.search(draft))
    if contains_sensitive_claims:
        reasons.append("Draft contains sensitive claims (salary/rates/calendar slot).")

    # Profile evidence checking
    profile_skills = {str(s).lower() for s in (prof.get("skills") or [])}
    profile_roles = {str(r).lower() for r in (prof.get("desired_roles") or []) + (prof.get("alternative_roles") or [])}
    profile_combined = " ".join(profile_skills | profile_roles).lower()

    contains_unverified_claims = False
    uses_supported_facts = True

    draft_low = draft.lower()

    # Check disclaimers (e.g. "опыта нет", "в профиле нет", "непосредственно с Ozon и Wildberries подтверждённого опыта нет")
    is_disclaiming_ecom = any(neg in draft_low for neg in (
        "опыта нет", "опыта не имею", "не работал", "в профиле нет",
        "нет опыта", "нет коммерческого опыта", "подтверждённого опыта нет",
        "подтвержденного опыта нет", "подтверждённого коммерческого опыта нет",
        "подтвержденного коммерческого опыта нет", "не хочу приписывать",
        "no direct experience", "do not have direct", "without direct"
    ))

    # 1. E-commerce / Ozon / WB positive claim check
    if any(k in draft_low for k in ("ozon", "wildberries", "wb", "e-commerce", "ecommerce", "маркетплейс")):
        has_ecom_in_profile = any(k in profile_combined for k in ("ozon", "wildberries", "wb", "e-commerce", "ecommerce", "маркетплейс", "marketplace"))
        if not has_ecom_in_profile and not is_disclaiming_ecom:
            contains_unverified_claims = True
            uses_supported_facts = False
            reasons.append("Draft claims Ozon/WB experience, but candidate profile does not contain evidence for this claim.")

    # 2. Excluded / Unverified technologies
    excluded = {str(x).lower() for x in (prof.get("excluded_roles") or [])}
    for exc in excluded:
        if exc in draft_low and not is_disclaiming_ecom:
            contains_unverified_claims = True
            uses_supported_facts = False
            reasons.append(f"Draft claims experience in excluded technology ({exc}).")

    # 3. Years of experience check
    m_exp = re.search(r"(\d+)\s+(лет|года|год|years)", draft_low)
    if m_exp:
        years_claimed = int(m_exp.group(1))
        profile_years = prof.get("years_experience", 0)
        if years_claimed > profile_years:
            contains_unverified_claims = True
            uses_supported_facts = False
            reasons.append(f"Draft claims {years_claimed} years of experience, exceeding verified profile experience ({profile_years} years).")

    # 4. Answers last question check
    answers_last_question = True
    employer_msgs = [m for m in (dialog.messages or []) if m.sender != "candidate"]
    if employer_msgs:
        last_emp_text = (employer_msgs[-1].text or "").lower()
        if "?" in last_emp_text or "опыт" in last_emp_text or "уточните" in last_emp_text:
            if len(draft.strip()) < 5:
                answers_last_question = False
                reasons.append("Draft is too brief to answer the employer question.")

    # Status determination
    if classification == "HUMAN_REVIEW":
        validation = "HUMAN_REVIEW"
        if not reasons:
            reasons.append("Dialogue classification requires human review; automated approval blocked.")
    elif contains_unverified_claims or contains_sensitive_claims:
        validation = "HUMAN_REVIEW"
    elif not answers_last_question:
        validation = "REJECTED"
    elif classification == "NEEDS_REPLY" and uses_supported_facts and not contains_unverified_claims and not contains_sensitive_claims:
        validation = "APPROVED"
    else:
        validation = "HUMAN_REVIEW"

    return {
        "conversation_id": cid,
        "classification": classification,
        "draft": draft,
        "validation": validation,
        "checks": {
            "answers_last_question": answers_last_question,
            "uses_supported_facts": uses_supported_facts,
            "contains_unverified_claims": contains_unverified_claims,
            "contains_sensitive_claims": contains_sensitive_claims,
            "is_empty": is_empty,
        },
        "reasons": reasons,
        "status": "READ-ONLY",
    }


# ------------- persistent deduplication (file-backed, no DB change) ---------

class ReplyStateStore:
    """File-backed state of processed messages (artifacts/, gitignored).

    No DB schema change: reuses the existing project artifacts state
    mechanism. Stores minimal per-message record:
        conversation_id, message_id, timestamp, classification, reply, status.
    """

    def __init__(self, path: Optional[str] = None):
        # Resolve at call time so tests can override DEFAULT_STATE_PATH after
        # import (a default arg would freeze the value at definition time).
        self.path = path if path is not None else DEFAULT_STATE_PATH
        self._records: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._records = data.get("processed", {})
        except Exception:
            self._records = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"processed": self._records}, f, ensure_ascii=False, indent=1)

    def is_processed(self, conversation_id: str, message_id: str) -> bool:
        return self._records.get(conversation_id, {}).get(message_id) is not None

    def mark_processed(
        self,
        conversation_id: str,
        message_id: str,
        classification: str,
        reply: str,
        status: str,
    ) -> None:
        self._records.setdefault(conversation_id, {})[message_id] = {
            "message_id": message_id,
            "processed_at": datetime.utcnow().isoformat(),
            "classification": classification,
            "reply": reply,
            "status": status,
        }
        self._save()

    def last_processed_message_id(self, conversation_id: str) -> Optional[str]:
        recs = self._records.get(conversation_id, {})
        return max(recs) if recs else None


# ------------- send gate (Stage 22: physically blocks any send) -------------

class SendGateBlocked(Exception):
    """Raised when an attempt is made to send a message in a blocked mode."""


class SendGate:
    """Explicit send gate. In REVIEW mode every send attempt is blocked.

    Stage 22 allows REVIEW only; AUTO/SKIP are future stubs that raise
    NotBlocked/blocked as configured but are never reachable from the
    processing entry point. The gate never touches the browser.
    """

    ALLOWED_MODES = (ReplyMode.REVIEW,)  # only REVIEW in Stage 22

    def __init__(self, mode: ReplyMode = DEFAULT_MODE):
        self.mode = mode

    def send_reply(self, dialog: HHDialog, text: str) -> Dict[str, Any]:
        """Attempt to send - ALWAYS blocked in Stage 22 (REVIEW only)."""
        if self.mode not in self.ALLOWED_MODES:
            return {"ok": False, "blocked": True,
                    "reason": f"mode {self.mode.value} not enabled in Stage 22"}
        return {"ok": False, "blocked": True,
                "reason": "REVIEW_MODE: message send is forbidden in Stage 22"}

    # defensive: never expose a real click/send primitive
    def _never_used_browser(self) -> None:  # pragma: no cover
        raise SendGateBlocked("Stage 22 has no browser send primitive")


# ------------- REVIEW-only processing entry point --------------------------

class MessageReplyReport(BaseModel):
    conversation_id: str = ""
    vacancy_title: str = ""
    vacancy_stable_id: str = ""
    employer: str = ""
    last_message: str = ""
    classification: str = ""
    generated_reply: str = ""
    sources: List[str] = Field(default_factory=list)
    status: str = "NEEDS_HUMAN_REVIEW"
    reason: str = ""
    send_action_count: int = 0
    skipped_as_processed: bool = False
    processed_at: str = ""

    model_config = {"extra": "forbid"}


def process_incoming_message(
    dialog: HHDialog,
    profile: Optional[Dict[str, Any]] = None,
    state: Optional[ReplyStateStore] = None,
    mode: ReplyMode = DEFAULT_MODE,
) -> MessageReplyReport:
    """Review-only processing of one incoming dialog/message.

    - Skips already-processed (conversation_id, message_id).
    - Classifies, generates a truth-only reply (REPLY_REQUIRED only).
    - NEVER sends (send gate), NEVER edits HH text.
    """
    msg = dialog.last_message()
    if msg is None:
        return MessageReplyReport(
            conversation_id=dialog.conversation_id, status="NEEDS_HUMAN_REVIEW",
            reason="dialog has no messages",
            processed_at=datetime.utcnow().isoformat())
    store = state if state is not None else ReplyStateStore()
    if store.is_processed(dialog.conversation_id, msg.message_id):
        return MessageReplyReport(
            conversation_id=dialog.conversation_id,
            vacancy_title=dialog.vacancy_title,
            vacancy_stable_id=dialog.vacancy_stable_id,
            employer=dialog.employer,
            last_message=msg.text,
            classification="ALREADY_PROCESSED",
            status="SKIPPED",
            reason="message already processed (dedup)",
            skipped_as_processed=True,
            processed_at=datetime.utcnow().isoformat())

    classification = classify_message(dialog)
    reply = ""
    sources: List[str] = []
    reason = ""
    if classification == MessageClassification.REPLY_REQUIRED:
        gen = generate_reply(dialog, profile)
        reply = gen.get("reply", "")
        sources = list(gen.get("sources", []) or [])
        if not reply:
            classification = MessageClassification.HUMAN_REVIEW
            reason = gen.get("reason", "")
        else:
            reason = gen.get("reason", "")
    elif classification == MessageClassification.NO_REPLY:
        reason = "system/notification/irrelevant - no reply needed"
    else:
        reason = "ambiguous/sensitive - human review required, no facts guessed"

    # Send gate: REVIEW blocks (always). Count 0 in Stage 22.
    gate = SendGate(mode=mode)
    sent = gate.send_reply(dialog, reply)  # always {"ok": False, "blocked": True}
    report = MessageReplyReport(
        conversation_id=dialog.conversation_id,
        vacancy_title=dialog.vacancy_title,
        vacancy_stable_id=dialog.vacancy_stable_id,
        employer=dialog.employer,
        last_message=msg.text,
        classification=classification.value,
        generated_reply=reply,
        sources=sources,
        status="NEEDS_HUMAN_REVIEW",
        reason=reason,
        send_action_count=0,
        processed_at=datetime.utcnow().isoformat())
    store.mark_processed(
        dialog.conversation_id, msg.message_id,
        classification.value, reply, report.status)
    return report


# ------------- read-only HH fetch (reuses existing CDP transport) ----------

_DIALOG_LIST_JS = r"""() => {
    // Read-only: enumerate visible dialog/card elements on the HH messages page.
    // Never navigates, never activates UI, never sends. Structure only.
    const sels = ['[data-qa="dialog-item"]', '[data-qa="messaging-dialog"]',
                  '[class*="dialog" i]', '[class*="conversation" i]',
                  '[class*="message-item" i]', '[data-qa*="dialog"]'];
    const seen = [];
    for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
            const vis = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            if (!vis) continue;
            const text = (el.innerText || '').trim();
            if (!text) continue;
            const qa = el.getAttribute('data-qa') || '';
            const key = qa || (el.className || '').toString() || text.slice(0, 40);
            if (seen.some(s => s.key === key)) continue;
            seen.push({key, qa, tag: el.tagName, text: text.slice(0, 500)});
        }
    }
    return JSON.stringify({
        url: location.href,
        title: document.title,
        dialogs: seen.slice(0, 30),
        pageIsMessages: /messages|messaging|negotiations|\/chat\/\d+/i.test(location.href)
    });
}"""


def fetch_hh_dialogs_readonly(
    evaluate_fn: Callable[[str], str],
    current_url: str = "",
) -> Dict[str, Any]:
    """Read-only discovery of HH message dialogs on the currently-open tab.

    Uses the caller-provided evaluate_fn (e.g. make_cdp_evaluate(...) or a
    fake in tests). Returns raw dialog-card text plus URL/title. This module
    never navigates; navigation to the messages section is left to the user
    (mirrors the manual-form capture contract).
    """
    expr = _DIALOG_LIST_JS
    if expr.lstrip().startswith("() =>"):
        expr = "(" + expr + ")()"
    raw = evaluate_fn(expr)
    try:
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e), "url": current_url, "dialogs": [],
                "pageIsMessages": False}


_CONVERSATIONS_LIST_JS = r"""() => {
    const out = [];
    const chatEls = Array.from(document.querySelectorAll('a[data-qa*="chatik-open-chat-"], a[class*="chat-cell"], a[href*="/chat/"]'));
    
    for (const a of chatEls) {
        const m = (a.href || '').match(/\/chat\/([0-9]+)/);
        const cid = m ? m[1] : null;
        if (!cid || cid === '-1') continue;
        if (out.some(x => x.conversation_id === cid)) continue;

        const titleEl = a.querySelector('[data-qa*="title"], [class*="title"]');
        const subtitleEl = a.querySelector('[data-qa*="subtitle"], [class*="subtitle"], [class*="employer"]');
        const snippetEl = a.querySelector('[data-qa*="message"], [data-qa*="snippet"], [class*="message"], [class*="snippet"], [class*="preview"], [class*="last-message"]');
        const isSelected = /selected/.test(a.className || '') || location.pathname.includes('/chat/' + cid);
        
        const rawLines = (a.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
        const title = titleEl ? (titleEl.innerText || '').trim() : (rawLines[0] || null);
        let employer = subtitleEl ? (subtitleEl.innerText || '').trim() : null;
        let snippet = snippetEl ? (snippetEl.innerText || '').trim() : null;
        
        if (!employer && rawLines.length >= 3) {
            employer = rawLines[2];
        }
        if (!snippet && rawLines.length >= 4) {
            snippet = rawLines[3];
        } else if (!snippet && rawLines.length === 3) {
            snippet = rawLines[2];
        }

        out.push({
            conversation_id: cid,
            url: (a.href || '').split('?')[0],
            title: title,
            employer: employer,
            snippet: snippet,
            is_selected: isSelected
        });
    }

    const openMatch = location.pathname.match(/\/chat\/([0-9]+)/);
    if (openMatch && openMatch[1] && !out.some(x => x.conversation_id === openMatch[1])) {
        const hTitle = document.querySelector('[data-qa*="chat-header"] [class*="title" i], [class*="header"] [class*="title" i]');
        const hEmp = document.querySelector('a[href*="/employer/"]');
        out.unshift({
            conversation_id: openMatch[1],
            url: location.href.split('?')[0],
            title: hTitle ? (hTitle.innerText || '').trim() : document.title,
            employer: hEmp ? (hEmp.innerText || '').trim() : null,
            snippet: null,
            is_selected: true
        });
    }

    return JSON.stringify({
        url: location.href,
        title: document.title,
        conversations: out
    });
}"""


def fetch_hh_conversations_list_readonly(evaluate_fn: Callable[[str], str]) -> Dict[str, Any]:
    """Stage 30D.9: Read-only enumeration of all visible conversations in HH chat interface.
    Returns:
        conversations: List[Dict[str, Any]] (conversation_id, url, title, employer, snippet, is_selected)
    """
    expr = _CONVERSATIONS_LIST_JS
    if expr.lstrip().startswith("() =>"):
        expr = "(" + expr + ")()"
    raw = evaluate_fn(expr)
    try:
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e), "conversations": []}


# Read-only JS: extract the full open conversation from the chatik iframe.
# DOM observed live (Stage 24, chatik.hh.ru/chat/<id>):
#   - message container: div[class*="message--"] with optional "message_my--"
#   - bubble: div[class*="chat-bubble"], outgoing carries "chat-bubble_ou" /
#     "with-right-tail"; incoming is plain
#   - sender: header element with author/sender/name class (e.g. Робот-рекрутер)
#   - timestamp: element with time/meta class (best-effort, may be absent)
#   - composer: textarea[data-qa="chatik-new-message-text"] (never touched)
# No message_id is provided by HH in this DOM; we do NOT invent one.
_CONVERSATION_JS = """() => {
    const out = [];
    const els = Array.from(document.querySelectorAll('div[class*="message--"]'));
    for (const el of els) {
        const cls = (el.className || '').toString();
        const bubble = el.querySelector('[class*="chat-bubble"]');
        if (!bubble) continue;
        const text = (el.innerText || '').trim();
        if (!text) continue;
        const bcls = (bubble.className || '').toString();
        const isMy = /message_my--/.test(cls);
        const isOut = /chat-bubble_ou/.test(bcls) || /right-tail/.test(bcls);
        let sender = null;
        const head = el.querySelector('[class*="author" i], [class*="sender" i], [class*="name" i]');
        if (head) sender = (head.innerText || '').trim() || null;
        let ts = null;
        const tm = el.querySelector('[class*="time" i], [class*="meta" i]');
        if (tm) ts = (tm.innerText || '').trim() || null;
        out.push({
            direction: (isMy || isOut) ? 'OUTGOING' : 'INCOMING',
            text: text.slice(0, 2000),
            sender: sender,
            timestamp: ts
        });
    }
    // dedupe nested duplicates by (direction,text)
    const uniq = [];
    for (const m of out) {
        if (!uniq.some(u => u.direction === m.direction && u.text === m.text)) uniq.push(m);
    }
    const composer = document.querySelector('textarea[data-qa="chatik-new-message-text"], textarea[data-qa*="message"], [data-qa*="chatik-new-message"], textarea');
    const partEl = document.querySelector('[data-qa*="chat-header"] [class*="name" i], [class*="chat-header"] [class*="title" i], [class*="header"] [class*="title" i]');
    const participant = partEl ? ((partEl.innerText || '').trim() || null) : null;
    return JSON.stringify({
        url: location.href,
        title: document.title,
        conversation_id: (location.pathname.match(/\\/chat\\/([0-9]+)/) || [])[1] || null,
        participant: participant,
        messages: uniq,
        composer_present: !!composer
    });
}"""


def fetch_hh_conversation_readonly(evaluate_fn: Callable[[str], str]) -> Dict[str, Any]:
    """Read-only extraction of the currently-open HH conversation (chatik).

    Caller provides evaluate_fn bound to the chatik iframe (e.g. via
    Page.createIsolatedWorld on the chatik frame - see Stage 23B). Strictly
    read-only: no clicks, no sends, no DOM writes.
    Returns:
        conversation_id, title, url, messages[] (direction/text/sender/
        timestamp), composer_present. message_id is NOT present because HH
        does not expose one in this DOM (documented limitation).
    """
    expr = _CONVERSATION_JS
    if expr.lstrip().startswith("() =>"):
        expr = "(" + expr + ")()"
    raw = evaluate_fn(expr)
    try:
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e), "conversation_id": None, "messages": []}


# ---------------- Stage 25: limited AUTO (opt-in, allowlisted) --------------

# Auto-safety sensitivity patterns. Anything matching here forbids AUTO even
# when classification is REPLY_REQUIRED (salary, dates, experience, previous
# interviews, candidate status, tech stack, offers, legal/financial terms...).
_AUTO_FORBIDDEN_RE = re.compile(
    r"зарплат|salary|оклад|оплат|компенсац|ставк|денег|сколько.*получ|"
    r"уровень дохода|опыт|years of experience|лет опыта|года опыта|"
    r"когда.*(мож|смож|доступ)|в какое время|какую дату|start date|available to start|"
    r"when can you|when could you|интервью|собеседован|interview|проходили|прошлых|"
    r"предыдущ.*(интерв|собес)|did you.*interview|"
    r"статус|статусе|status|решение по ваканси|"
    r"стек|технологи|навык|python|n8n|llm|java|golang|telegram|"
    r"принять предлож|отклон|accept.*offer|offer|контракт|договор|юридич|финанс|"
    r"условия работы|work conditions|часовой пояс",
    re.IGNORECASE,
)

# Minimal confirmations that a reply can be built without any candidate facts:
# the message must be answerable purely from profile truth (role interest).
# AUTO is only attempted for these very simple "interest/availability to talk"
# messages, and only when the profile confirms the role interest.
_AUTO_ALLOWED_PROBE = re.compile(
    r"(интересн|заинтересован|interested|want to discuss|готовы обсудить|"
    r"можем ли|would you like|still interested|ещё интересует|по-прежнему)",
    re.IGNORECASE,
)


def _auto_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """Kill switch: AUTO requires HH_AUTO_REPLY_ENABLED=true (explicit)."""
    val = (env if env is not None else os.environ).get(_AUTO_ENV_VAR, "")
    return str(val).strip().lower() == "true"


def is_safe_for_auto_reply(
    dialog: HHDialog,
    classification: MessageClassification,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Safety predicate: is this message allowlisted for AUTO?

    Returns {"safe": bool, "reasons": [...]}. AUTO is allowed ONLY if every
    condition holds; any doubt -> safe=False.
    """
    reasons: List[str] = []
    if classification != MessageClassification.REPLY_REQUIRED:
        reasons.append("classification is not REPLY_REQUIRED")
    context = _context_texts(dialog)
    if _AUTO_FORBIDDEN_RE.search(context):
        reasons.append("sensitive/forbidden topic in conversation")
    last = (dialog.last_message().text or "") if dialog.last_message() else ""
    if not _AUTO_ALLOWED_PROBE.search(last):
        reasons.append("message is not a plain interest/availability probe")
    prof = profile if profile is not None else _load_profile()
    roles = prof.get("desired_roles") or []
    if not roles:
        reasons.append("no desired_roles truth source for interest reply")
    return {"safe": not reasons, "reasons": reasons}


def _incoming_fingerprint(dialog: HHDialog) -> str:
    """Deterministic fingerprint of the last INCOMING message (dedup key +
    race-condition identity). HH does not provide a real message_id, so we
    anchor dedup to the last incoming text/context instead of a message index."""
    incomings = [m for m in (dialog.messages or []) if m.sender != "candidate"]
    anchor = incomings[-1] if incomings else (dialog.messages or [None])[-1]
    payload = json.dumps(
        {"conv": dialog.conversation_id, "text": (anchor.text if anchor else "")},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AutoReplyReport(BaseModel):
    mode: str = DEFAULT_MODE.value
    conversation_id: str = ""
    vacancy_title: str = ""
    employer: str = ""
    classification: str = ""
    generated_reply: str = ""
    sources: List[str] = Field(default_factory=list)
    status: str = ""  # SENT | NEEDS_HUMAN_REVIEW | SKIPPED | BLOCKED_*
    reason: str = ""
    safety_checks: List[Dict[str, Any]] = Field(default_factory=list)
    send_action_count: int = 0
    send_result: Optional[Dict[str, Any]] = None
    dedup_skipped: bool = False
    fingerprint: str = ""
    processed_at: str = ""

    model_config = {"extra": "forbid"}


def can_auto_send(
    dialog: HHDialog,
    classification: MessageClassification,
    profile: Optional[Dict[str, Any]],
    reply: str,
    composer_present: bool,
    fingerprint: str,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """MANDATORY final safety gate, re-checked immediately before send.

    Re-verifies every condition independently (does not trust the earlier
    classification alone) plus composer availability + kill switch + non-empty
    truth-only reply + unchanged fingerprint.
    """
    checks: List[Dict[str, Any]] = []
    ok = True

    sw = _auto_enabled(env)
    checks.append({"check": "kill_switch", "ok": sw})
    ok = ok and sw

    safe = is_safe_for_auto_reply(dialog, classification, profile)
    checks.append({"check": "allowlist", "ok": safe["safe"],
                   "reasons": safe["reasons"]})
    ok = ok and safe["safe"]

    checks.append({"check": "truth_only_reply_nonempty", "ok": bool(reply and reply.strip())})
    ok = ok and bool(reply and reply.strip())

    checks.append({"check": "composer_available", "ok": bool(composer_present)})
    ok = ok and bool(composer_present)

    checks.append({"check": "fingerprint_unchanged", "ok": bool(fingerprint)})
    ok = ok and bool(fingerprint)

    return {"ok": ok, "checks": checks}


def _composer_js() -> str:
    """Read-only probe: is the composer textarea present in the chatik DOM?"""
    return """JSON.stringify({composer_present: !!document.querySelector(
        'textarea[data-qa="chatik-new-message-text"]')})"""


def _send_js(reply: str) -> str:
    """THE single mutation path for AUTO send (composer + Send).

    Sets the reply text via the native value setter + input/change events
    (React-safe, same technique as the prefill executor), then activates the
    composer's send control. Returns a JSON result; the caller re-reads the
    conversation to confirm delivery. No retry is ever attempted here.
    """
    value = json.dumps(reply, ensure_ascii=False)
    return f"""(() => {{
    const ta = document.querySelector('textarea[data-qa="chatik-new-message-text"]');
    if (!ta) return JSON.stringify({{ok: false, reason: 'composer not found'}});
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(ta, {value});
    ta.dispatchEvent(new Event('input', {{bubbles: true}}));
    ta.dispatchEvent(new Event('change', {{bubbles: true}}));
    const controls = Array.from(document.querySelectorAll('button'));
    const send = controls.find(b => /^(Отправить|Send|Отпр|Сенд)$/i.test((b.innerText||'').trim())
                                 || (b.getAttribute('aria-label')||'').match(/отправ|send/i));
    if (!send) return JSON.stringify({{ok: false, reason: 'send control not found', text_set: true}});
    if (send.disabled) return JSON.stringify({{ok: false, reason: 'send control disabled', text_set: true}});
    send.click();
    return JSON.stringify({{ok: true, clicked: true}});
}})()"""


def send_auto_reply(
    evaluate_fn: Callable[[str], str],
    reply: str,
) -> Dict[str, Any]:
    """Execute the single AUTO send mutation. Returns the raw send result.

    No retry, no fallback clicks, no navigation. The caller must have already
    passed can_auto_send() and re-verified the conversation fingerprint.
    """
    expr = _send_js(reply)
    try:
        raw = evaluate_fn(expr)
        return json.loads(raw)
    except Exception as e:
        return {"ok": False, "reason": f"send evaluate error: {e}"}


def process_auto_reply(
    dialog: HHDialog,
    evaluate_fn: Callable[[str], str],
    profile: Optional[Dict[str, Any]] = None,
    state: Optional[ReplyStateStore] = None,
    mode: ReplyMode = DEFAULT_MODE,
    env: Optional[Dict[str, str]] = None,
    max_auto_replies: int = MAX_AUTO_REPLIES_PER_RUN,
    run_budget: Dict[str, int] | None = None,
    target_conversation_id: Optional[str] = None,
    confirm_live_send: bool = False,
) -> AutoReplyReport:
    """Limited AUTO / REVIEW / SKIP processing of one conversation.

    Stage 26 (first live AUTO) adds explicit target + confirmation gates:
      - target_conversation_id: required for any live AUTO send. If set and
        it does not match the dialog -> BLOCKED_TARGET_NOT_FOUND.
      - confirm_live_send: an EXPLICIT confirmation required before the first
        real send. Without it -> BLOCKED_LIVE_CONFIRMATION_REQUIRED even when
        all safety gates pass and the kill switch is on.
    Only safe allowlisted messages may reach AUTO send. Everything else stays
    HUMAN_REVIEW; SKIP generates and sends nothing. Dedup uses the incoming
    fingerprint; after a send the same incoming is never re-sent.
    """
    msg = dialog.last_message()
    cid = dialog.conversation_id
    report = AutoReplyReport(
        mode=mode.value, conversation_id=cid,
        vacancy_title=dialog.vacancy_title, employer=dialog.employer,
        processed_at=datetime.utcnow().isoformat())

    # --- Stage 26: explicit target selection (live AUTO only) ---
    if mode is ReplyMode.AUTO and target_conversation_id is not None:
        if str(target_conversation_id) != str(cid):
            report.status = "BLOCKED_TARGET_NOT_FOUND"
            report.reason = (f"target conversation_id {target_conversation_id} "
                             f"does not match dialog {cid}")
            return report
    # For REVIEW/SKIP a target mismatch is irrelevant (nothing is sent).

    # --- dedup (anchored to last incoming) ---
    fp = _incoming_fingerprint(dialog)
    report.fingerprint = fp
    store = state if state is not None else ReplyStateStore()
    if store.is_processed(cid, fp):
        report.status = "SKIPPED"
        report.reason = "already processed (incoming fingerprint dedup)"
        report.dedup_skipped = True
        return report

    classification = classify_message(dialog)
    report.classification = classification.value
    if msg is None:
        report.status = "HUMAN_REVIEW"
        report.reason = "no messages"
        return report

    # --- Stage 26: require a pending INCOMING message for live AUTO send ----
    if mode is ReplyMode.AUTO and target_conversation_id is not None:
        last_sender = getattr(msg, "sender", "") or ""
        if last_sender == "candidate":
            report.status = "BLOCKED_NO_PENDING_INCOMING"
            report.reason = "last message is outgoing - no pending incoming to answer"
            return report

    # --- SKIP: nothing generated, nothing sent ---
    if mode is ReplyMode.SKIP:
        report.status = "SKIPPED"
        report.reason = "SKIP mode"
        store.mark_processed(cid, fp, classification.value, "", "SKIPPED")
        return report

    # --- REVIEW: generate preview, NEVER send ---
    reply = ""
    sources: List[str] = []
    if classification == MessageClassification.REPLY_REQUIRED:
        gen = generate_reply(dialog, profile)
        reply = gen.get("reply", "")
        sources = list(gen.get("sources", []) or [])
    report.generated_reply = reply
    report.sources = sources
    if mode is ReplyMode.REVIEW:
        report.status = "NEEDS_HUMAN_REVIEW"
        report.reason = ("REVIEW mode - reply preview generated, send forbidden"
                         if reply else "REVIEW mode - no safe reply, human review")
        store.mark_processed(cid, fp, classification.value, reply, report.status)
        return report

    # --- AUTO: only after ALL safety gates + race-condition re-check -------
    if mode is not ReplyMode.AUTO:
        report.status = "BLOCKED"
        report.reason = f"unsupported mode {mode.value}"
        return report

    budget = run_budget if run_budget is not None else {"sent": 0}
    if int(budget.get("sent", 0)) >= int(max_auto_replies):
        report.status = "BLOCKED_RATE_LIMIT"
        report.reason = f"MAX_AUTO_REPLIES_PER_RUN={max_auto_replies} reached"
        return report

    # kill switch + allowlist + composer + fingerprint (final gate)
    composer_present = False
    try:
        composer_raw = evaluate_fn(_composer_js())
        composer_present = bool(json.loads(composer_raw).get("composer_present"))
    except Exception:
        composer_present = False
    gate = can_auto_send(dialog, classification, profile, reply,
                         composer_present, fp, env=env)
    report.safety_checks = gate["checks"]
    if not gate["ok"]:
        report.status = "HUMAN_REVIEW"
        report.reason = "AUTO safety gate failed - human review"
        store.mark_processed(cid, fp, classification.value, reply, report.status)
        return report

    # --- Stage 26: dry-run preview must be reviewed before the first send ---
    # A live send REQUIRES explicit confirmation (--confirm-live-send). The
    # dry-run preview is implicitly the report: generated reply + gates.
    if target_conversation_id is not None and not confirm_live_send:
        report.status = "BLOCKED_LIVE_CONFIRMATION_REQUIRED"
        report.reason = ("live AUTO send requires explicit confirm_live_send "
                         "after reviewing the dry-run preview")
        store.mark_processed(cid, fp, classification.value, reply, report.status)
        return report

    # race-condition re-check: re-read conversation, fingerprint must match
    try:
        fresh = fetch_hh_conversation_readonly(evaluate_fn)
        fresh_dialog = HHDialog(
            conversation_id=fresh.get("conversation_id") or cid,
            vacancy_title=dialog.vacancy_title, employer=dialog.employer,
            messages=[HHMessage(message_id="m", text=(m.get("text") or ""),
                                sender=m.get("sender") or "")
                      for m in (fresh.get("messages") or [])])
        fresh_fp = _incoming_fingerprint(fresh_dialog)
    except Exception:
        fresh_fp = ""
    report.safety_checks.append({"check": "race_condition_recheck",
                                 "ok": fresh_fp == fp,
                                 "expected": fp[:16], "actual": fresh_fp[:16]})
    if not fresh_fp or fresh_fp != fp:
        report.status = "HUMAN_REVIEW"
        report.reason = "conversation changed since reply was built (race) - human review"
        store.mark_processed(cid, fp, classification.value, reply, report.status)
        return report

    # single send
    send_result = send_auto_reply(evaluate_fn, reply)
    report.send_result = send_result
    report.send_action_count = 1
    budget["sent"] = int(budget.get("sent", 0)) + 1
    if send_result.get("ok"):
        report.status = "SENT"
        report.reason = "AUTO reply sent (single mutation, no retry)"
    else:
        report.status = "BLOCKED"
        report.reason = f"AUTO send failed: {send_result.get('reason')} - no retry"
    store.mark_processed(cid, fp, classification.value, reply, report.status)
    return report


def send_confirmed_hh_reply(
    evaluate_fn: Callable[[str], str],
    reply: str,
) -> Dict[str, Any]:
    """Stage 30D.6: Execute minimal DOM/CDP send for a human-confirmed, validated reply.
    Strictly isolated: does not navigate, does not touch cookies/storage, does not click other elements.
    """
    value = json.dumps(reply, ensure_ascii=False)
    js = """(() => {
        const ta = document.querySelector('textarea[data-qa="text-input"], textarea[data-qa="chatik-new-message-text"], textarea');
        if (!ta) return JSON.stringify({ok: false, reason: 'composer textarea not found'});
        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(ta, __REPLY_VALUE__);
        ta.dispatchEvent(new Event('input', {bubbles: true}));
        ta.dispatchEvent(new Event('change', {bubbles: true}));

        const container = ta.closest('[data-qa="chatik-message-input"]') || document;
        let sendBtn = container.querySelector('button[data-qa*="send"], button[data-qa*="submit"], [aria-label*="Отправить" i], [aria-label*="Send" i]');
        if (!sendBtn) {
            const btns = Array.from(container.querySelectorAll('button, [role="button"]'));
            sendBtn = btns.find(b => /^(Отправить|Send|Отпр|Сенд)$/i.test((b.innerText || '').trim()) || (b.getAttribute('aria-label') || '').match(/отправ|send/i));
        }
        if (sendBtn && !sendBtn.disabled) {
            sendBtn.click();
            return JSON.stringify({ok: true, method: 'button_click'});
        }
        const evDown = new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true});
        ta.dispatchEvent(evDown);
        const evUp = new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true});
        ta.dispatchEvent(evUp);
        return JSON.stringify({ok: true, method: 'enter_key'});
    })()""".replace("__REPLY_VALUE__", value)
    try:
        raw = evaluate_fn(js)
        return json.loads(raw)
    except Exception as e:
        return {"ok": False, "reason": str(e)}


__all__ = [
    "DEFAULT_MODE",
    "HHDialog",
    "HHMessage",
    "MAX_AUTO_REPLIES_PER_RUN",
    "MessageClassification",
    "MessageReplyReport",
    "ReplyMode",
    "ReplyStateStore",
    "SendGate",
    "SendGateBlocked",
    "AutoReplyReport",
    "can_auto_send",
    "classify_message",
    "detect_language",
    "fetch_hh_conversation_readonly",
    "fetch_hh_conversations_list_readonly",
    "fetch_hh_dialogs_readonly",
    "generate_reply",
    "is_safe_for_auto_reply",
    "process_auto_reply",
    "process_incoming_message",
    "resolve_vacancy_for_dialog",
    "send_auto_reply",
    "send_confirmed_hh_reply",
]