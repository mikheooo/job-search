"""Stage 32: HH Message Watcher.

Dedicated, controlled watcher for HeadHunter (HH) messaging dialogs.
Monitors connected HH conversations, discovers NEW incoming employer messages,
checks deduplication against state.db, resolves vacancy context,
classifies message intent, prepares truth-only replies, and safely
stops before sending (READY_FOR_HUMAN_REVIEW / NEEDS_HUMAN_REVIEW / BLOCKED).

SAFETY INVARIANT:
- This watcher NEVER autonomously sends messages or clicks send buttons.
- Outgoing messages require explicit human approval and use the existing
  verified hh-message submission gate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from collections.abc import Callable

from pydantic import BaseModel, Field

from . import db
from .candidate_profile import load_candidate_profile
from .db import (
    get_hh_message_event,
    is_hh_message_processed,
    save_hh_message_event,
)
from .hh_message_reply import (
    HHDialog,
    HHMessage,
    classify_hh_conversation_detailed,
    fetch_hh_conversation_readonly,
    fetch_hh_conversations_list_readonly,
    resolve_vacancy_for_dialog,
    validate_hh_reply_draft,
)

logger = logging.getLogger(__name__)


class HHMessageWatcherStatus(str, Enum):
    NEW = "NEW"
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    NO_REPLY = "NO_REPLY"
    ALREADY_PROCESSED = "ALREADY_PROCESSED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


def compute_message_fingerprint(
    conversation_id: str,
    sender: str,
    sent_at: str | None,
    text: str,
    message_id: str | None = None,
) -> str:
    """Build a stable, collision-resistant SHA-256 fingerprint for an HH message.

    Uses conversation_id, normalized sender, timestamp, and message text.
    Does NOT rely on message text alone.
    """
    if message_id and not message_id.startswith("m") and not message_id.startswith("msg_") and len(message_id) > 10:
        return f"hh_mid_{message_id}"

    payload = json.dumps(
        {
            "conversation_id": str(conversation_id or "").strip(),
            "sender": str(sender or "employer").strip().lower(),
            "sent_at": str(sent_at or "").strip(),
            "text": str(text or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"hh_mfp_{digest[:24]}"


class HHMessageWatcherConfig(BaseModel):
    cdp_url: str | None = None
    url_substring: str | None = None
    poll_interval_seconds: int = 60
    max_iterations: int | None = None
    batch_limit: int = 20
    profile_path: str | None = None
    auto_start_browser: bool = True
    custom_evaluate_fn: Any | None = None  # Optional[Callable[[str], str]]
    on_new_message: Any | None = None  # Optional[Callable[[HHMessageWatcherItem], None]]

    model_config = {"arbitrary_types_allowed": True}


class HHMessageWatcherItem(BaseModel):
    conversation_id: str
    message_id: str
    message_fingerprint: str
    sender: str
    text: str
    sent_at: str | None = None
    vacancy_stable_id: str | None = None
    employer: str | None = None
    participant: str | None = None
    classification: str = ""
    confidence: float = 0.0
    question: str | None = None
    required_facts: list[str] = Field(default_factory=list)
    available_facts: list[str] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)
    reply_draft: str | None = None
    validation: str = ""
    status: str = ""
    stop_reason: str = ""
    reply_attempted: bool = False
    reply_sent: bool = False

    model_config = {"extra": "forbid"}


class HHMessageWatcherCycleResult(BaseModel):
    iteration: int = 1
    timestamp: str = ""
    conversations_checked: int = 0
    messages_seen: int = 0
    new_messages: int = 0
    already_processed: int = 0
    replies_prepared: int = 0
    ready_for_human_review: int = 0
    needs_human_review: int = 0
    replies_sent: int = 0
    blocked: int = 0
    errors: list[str] = Field(default_factory=list)
    items: list[HHMessageWatcherItem] = Field(default_factory=list)

    # Safety Invariants
    reply_sent_count: int = 0
    duplicate_reply_count: int = 0
    human_approval_bypass: bool = False

    model_config = {"extra": "forbid"}


def run_message_watcher_cycle(
    config: HHMessageWatcherConfig,
    evaluate_fn: Callable[[str], str] | None = None,
    iteration: int = 1,
) -> HHMessageWatcherCycleResult:
    """Execute a single polling and analysis cycle over connected HH conversations.

    Pure inspection and preparation. NEVER sends messages.
    """
    timestamp = datetime.utcnow().isoformat()
    result = HHMessageWatcherCycleResult(
        iteration=iteration,
        timestamp=timestamp,
        reply_sent_count=0,
        duplicate_reply_count=0,
        human_approval_bypass=False,
    )

    # 1. Load Candidate Profile
    profile_dict: dict[str, Any] = {}
    if config.profile_path:
        p = Path(config.profile_path)
        if p.exists():
            try:
                profile_dict = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Failed to load profile from {config.profile_path}: {e}")
    if not profile_dict:
        try:
            profile_obj = load_candidate_profile()
            profile_dict = profile_obj.to_dict()
        except Exception:
            profile_dict = {}

    db.init_db()

    # 2. Resolve CDP evaluate function
    ev = config.custom_evaluate_fn or evaluate_fn
    if ev is None:
        from .cli import (
            _DEFAULT_HH_CDP_URL,
            _DEFAULT_HH_MESSAGES_URL_SUBSTRING,
            _resolve_hh_evaluate,
        )
        from .hh_browser_launcher import (
            check_hh_session_authenticated,
            ensure_hh_browser,
        )

        cdp = config.cdp_url or _DEFAULT_HH_CDP_URL
        sub = config.url_substring or _DEFAULT_HH_MESSAGES_URL_SUBSTRING

        # Auto-ensure browser is running
        browser_info = ensure_hh_browser(
            cdp_url=cdp,
            auto_start=config.auto_start_browser,
        )
        if not browser_info.get("ok"):
            err_msg = f"BLOCKED: {browser_info.get('error', 'HH Chrome browser unavailable')}"
            logger.error(err_msg)
            result.errors.append(err_msg)
            result.blocked += 1
            return result

        try:
            ev = _resolve_hh_evaluate(
                cdp_url=cdp,
                url_substring=sub,
            )
        except Exception as e:
            err_str = str(e)
            if "no open tab matching" in err_str:
                err_msg = f"BLOCKED: connected to Chrome on {cdp}, but no open tab matching '{sub}' was found. Open hh.ru in Chrome."
            else:
                err_msg = f"BLOCKED: CDP/HH connection failed: {e}"

            logger.error(err_msg)
            result.errors.append(err_msg)
            result.blocked += 1
            return result

        # Verify session authentication
        try:
            auth_info = check_hh_session_authenticated(ev)
            if not auth_info.get("authenticated", True):
                err_msg = f"BLOCKED: HH session not authenticated ({auth_info.get('reason', 'Login required')}). Please log in to HeadHunter."
                logger.warning(err_msg)
                result.errors.append(err_msg)
                result.blocked += 1
                return result
        except Exception as e:
            logger.debug(f"Auth check notice: {e}")

    # 3. Discover conversations
    try:
        raw_list = fetch_hh_conversations_list_readonly(ev)
        raw_convs = raw_list.get("conversations") or []
    except Exception as e:
        err_msg = f"Failed to fetch HH conversations list: {e}"
        logger.error(err_msg)
        result.errors.append(err_msg)
        result.blocked += 1
        return result

    # Deduplicate conversations list
    seen_conv_ids = set()
    unique_convs = []
    for c in raw_convs:
        cid = str(c.get("conversation_id") or "").strip()
        if cid and cid not in seen_conv_ids:
            seen_conv_ids.add(cid)
            unique_convs.append(c)

    if config.batch_limit and config.batch_limit > 0:
        unique_convs = unique_convs[: config.batch_limit]

    result.conversations_checked = len(unique_convs)

    # 4. Process each conversation
    for c in unique_convs:
        cid = str(c.get("conversation_id") or "")
        title = c.get("title")
        employer = c.get("employer")
        is_sel = c.get("is_selected", False)

        try:
            # Fetch message history for conversation
            if is_sel:
                try:
                    fresh = fetch_hh_conversation_readonly(ev)
                    raw_msgs = fresh.get("messages") or []
                    if fresh.get("title") and "Чаты" not in fresh.get("title"):
                        title = fresh.get("title")
                    employer = fresh.get("employer") or employer
                except Exception:
                    raw_msgs = []
            else:
                raw_msgs = []

            # If no detailed messages, construct from card snippet if available
            dialog_msgs: list[HHMessage] = []
            if raw_msgs:
                for idx, m in enumerate(raw_msgs):
                    direction = (m.get("direction") or "").upper()
                    sender = "candidate" if direction == "OUTGOING" else "employer"
                    msg_text = (m.get("text") or "").strip()
                    sent_at = m.get("sent_at") or m.get("time")
                    mid = str(m.get("message_id") or f"m{idx}")
                    dialog_msgs.append(
                        HHMessage(
                            message_id=mid,
                            text=msg_text,
                            sent_at=sent_at,
                            sender=sender,
                        )
                    )
            elif c.get("snippet"):
                snippet = (c.get("snippet") or "").replace("\xa0", " ").strip()
                snippet_lower = snippet.lower()
                sender = "candidate" if "отклик на вакансию" in snippet_lower else "employer"
                dialog_msgs.append(
                    HHMessage(
                        message_id="m0",
                        text=snippet,
                        sent_at=None,
                        sender=sender,
                    )
                )

            result.messages_seen += len(dialog_msgs)

            if not dialog_msgs:
                continue

            # Identify the latest message
            last_msg = dialog_msgs[-1]

            # Rule: Outgoing messages from user/candidate are NOT incoming messages
            if last_msg.sender == "candidate":
                continue

            # Compute stable fingerprint
            mfp = compute_message_fingerprint(
                conversation_id=cid,
                sender=last_msg.sender,
                sent_at=last_msg.sent_at,
                text=last_msg.text,
                message_id=last_msg.message_id,
            )

            # Check if already processed
            if is_hh_message_processed(mfp):
                result.already_processed += 1
                # Stage 35/36: Ensure application state machine record exists for this conversation
                try:
                    from .hh_application_orchestrator import HHApplicationOrchestrator
                    app_existing = db.get_hh_application(f"app_{cid}")
                    if not app_existing:
                        ev_item = get_hh_message_event(mfp) or {}
                        orchestrator = HHApplicationOrchestrator()
                        orchestrator.orchestrate_incoming_event(
                            conversation_id=cid,
                            message_id=last_msg.message_id,
                            sender=last_msg.sender,
                            text=last_msg.text,
                            sent_at=last_msg.sent_at,
                            vacancy_stable_id=ev_item.get("vacancy_stable_id"),
                            title=title,
                            employer=ev_item.get("employer") or employer,
                            classification=ev_item.get("classification") or "GENERAL_INQUIRY",
                            draft=ev_item.get("reply_draft"),
                            validation_status=ev_item.get("validation"),
                        )
                except Exception as sync_err:
                    logger.debug(f"Orchestrator sync for existing event {cid}: {sync_err}")
                continue

            # New incoming message from employer!
            result.new_messages += 1

            # Construct dialog for analysis
            dialog = HHDialog(
                conversation_id=cid,
                vacancy_title=title or "",
                employer=employer or "",
                messages=dialog_msgs,
            )

            # Resolve linked vacancy
            v_match = resolve_vacancy_for_dialog(dialog) or {}
            vac_stable_id = v_match.get("stable_id")
            if v_match.get("employer") and not dialog.employer:
                dialog.employer = v_match.get("employer")
            resolved_employer = dialog.employer or v_match.get("employer") or employer or None

            # Classification & fact analysis
            det = classify_hh_conversation_detailed(dialog, profile=profile_dict)
            classification = det.get("classification", "HUMAN_REVIEW")
            confidence = float(det.get("confidence", 0.9))
            question = det.get("question")
            req_facts = det.get("required_facts") or []
            avail_facts = det.get("available_facts") or []
            miss_facts = det.get("missing_facts") or []
            draft = det.get("prepared_reply")

            # Validation & Status determination
            val_status = "REJECTED"
            if classification == "NEEDS_REPLY":
                val = validate_hh_reply_draft(
                    dialog,
                    draft=draft,
                    classification=classification,
                    profile=profile_dict,
                )
                val_status = val.get("validation", "REJECTED")
                if val_status == "APPROVED":
                    result.replies_prepared += 1
                    result.ready_for_human_review += 1
                    status = HHMessageWatcherStatus.READY_FOR_HUMAN_REVIEW.value
                    stop_reason = "Reply draft prepared and validated; stopped before send at Human Review Gate"
                else:
                    result.needs_human_review += 1
                    status = HHMessageWatcherStatus.NEEDS_HUMAN_REVIEW.value
                    stop_reason = f"Draft validation requires human review: {val.get('reason', 'unverified facts')}"
            elif classification == "HUMAN_REVIEW":
                val_status = "HUMAN_REVIEW"
                draft = None
                result.needs_human_review += 1
                status = HHMessageWatcherStatus.NEEDS_HUMAN_REVIEW.value
                stop_reason = f"Message classified as requiring human review: {det.get('reason', 'sensitive topic or salary')}"
            else:
                val_status = "NO_REPLY"
                draft = None
                status = HHMessageWatcherStatus.NO_REPLY.value
                stop_reason = "No reply required (system notification, auto-rejection, or closing)"

            # Save message event in state.db
            save_hh_message_event(
                message_fingerprint=mfp,
                conversation_id=cid,
                sender=last_msg.sender,
                text=last_msg.text,
                sent_at=last_msg.sent_at,
                processed=1,
                classification=classification,
                validation=val_status,
                reply_draft=draft,
                status=status,
                vacancy_stable_id=vac_stable_id,
                employer=resolved_employer,
            )

            # Stage 35/36: Sync with authoritative HH Application Orchestrator
            try:
                from .hh_application_orchestrator import HHApplicationOrchestrator
                orchestrator = HHApplicationOrchestrator()
                orchestrator.orchestrate_incoming_event(
                    conversation_id=cid,
                    message_id=last_msg.message_id,
                    sender=last_msg.sender,
                    text=last_msg.text,
                    sent_at=last_msg.sent_at,
                    vacancy_stable_id=vac_stable_id,
                    title=title,
                    employer=resolved_employer,
                    classification=classification,
                    draft=draft,
                    validation_status=val_status,
                )
            except Exception as orch_err:
                logger.warning(f"Orchestrator sync warning for conversation {cid}: {orch_err}")

            # Record item in result
            item = HHMessageWatcherItem(
                conversation_id=cid,
                message_id=last_msg.message_id,
                message_fingerprint=mfp,
                sender=last_msg.sender,
                text=last_msg.text,
                sent_at=last_msg.sent_at,
                vacancy_stable_id=vac_stable_id,
                employer=resolved_employer,
                participant=title or resolved_employer or None,
                classification=classification,
                confidence=confidence,
                question=question,
                required_facts=req_facts,
                available_facts=avail_facts,
                missing_facts=miss_facts,
                reply_draft=draft,
                validation=val_status,
                status=status,
                stop_reason=stop_reason,
                reply_attempted=False,
                reply_sent=False,
            )
            result.items.append(item)

        except Exception as e:
            err_msg = f"Error processing conversation {cid}: {e}"
            logger.error(err_msg)
            result.errors.append(err_msg)
            result.blocked += 1

    return result


class HHMessageWatcher:
    """Continuous or single-iteration HH message watcher (Stage 33)."""

    def __init__(self, config: HHMessageWatcherConfig):
        self.config = config
        self._running = False
        self._is_polling = False

    def poll_once(self, iteration: int = 1) -> HHMessageWatcherCycleResult:
        """Run a single polling cycle, preventing overlapping runs."""
        if self._is_polling:
            logger.warning("[HHMessageWatcher] Polling cycle already in progress, skipping overlapping run")
            return HHMessageWatcherCycleResult(
                iteration=iteration,
                timestamp=datetime.utcnow().isoformat(),
            )
        self._is_polling = True
        try:
            return run_message_watcher_cycle(self.config, iteration=iteration)
        finally:
            self._is_polling = False

    def run(self, stop_callback: Callable[[], bool] | None = None) -> list[HHMessageWatcherCycleResult]:
        """Run continuous polling loop until interrupted or max iterations reached."""
        self._running = True
        results: list[HHMessageWatcherCycleResult] = []
        iteration = 0

        print("=======================================================")
        print("   CONTINUOUS HH MESSAGE WATCHER (STAGE 33)")
        print("=======================================================")
        print(f"Interval:                  {self.config.poll_interval_seconds}s")
        print("Mode:                      READ-ONLY (stops at Human Review)")
        print("Safety Guarantee:          ZERO AUTONOMOUS SEND")
        print("-------------------------------------------------------")
        print("Press Ctrl+C to stop.")
        print("=======================================================\n")

        while self._running:
            iteration += 1
            try:
                res = self.poll_once(iteration=iteration)
                results.append(res)

                # Explicit notification for each new message discovered
                if res.items:
                    for it in res.items:
                        print("-------------------------------------------------------")
                        print("NEW HH MESSAGE")
                        print(f"Conversation: {it.conversation_id}")
                        print(f"Employer:     {it.employer or it.participant or 'Unknown'}")
                        print(f"Vacancy:      {it.vacancy_stable_id or it.participant or 'N/A'}")
                        print(f"Message:      {it.text}")
                        print(f"Draft:        {it.reply_draft or 'None'}")
                        print(f"Status:       {it.status}")
                        print("-------------------------------------------------------")

                        if self.config.on_new_message:
                            try:
                                self.config.on_new_message(it)
                            except Exception as cb_err:
                                logger.warning(f"Error in on_new_message callback: {cb_err}")

                # Heartbeat log / status report
                ts = datetime.utcnow().strftime("%H:%M:%S")
                if res.blocked > 0 and res.errors:
                    logger.warning(
                        f"[{ts}] Poll #{iteration}: browser/target unavailable or error ({res.errors[0]}). "
                        f"Will retry in {self.config.poll_interval_seconds}s..."
                    )
                else:
                    logger.info(
                        f"[{ts}] Poll #{iteration}: checked={res.conversations_checked}, "
                        f"seen={res.messages_seen}, new={res.new_messages}, "
                        f"already_processed={res.already_processed}, "
                        f"ready_for_review={res.ready_for_human_review}, "
                        f"needs_review={res.needs_human_review}"
                    )
            except Exception as e:
                logger.error(
                    f"[HHMessageWatcher] Iteration #{iteration} failed: {e}. "
                    f"Retrying in {self.config.poll_interval_seconds}s..."
                )

            if self.config.max_iterations and iteration >= self.config.max_iterations:
                break

            if stop_callback and stop_callback():
                break

            # Sleep in small slices to respond swiftly to stop() or Ctrl+C
            sleep_remaining = float(self.config.poll_interval_seconds)
            while sleep_remaining > 0 and self._running:
                if stop_callback and stop_callback():
                    self._running = False
                    break
                step = min(0.5, sleep_remaining)
                time.sleep(step)
                sleep_remaining -= step

        self._running = False
        return results

    def stop(self) -> None:
        """Gracefully signal the watcher loop to stop."""
        self._running = False
