"""Stage 28: Gmail read-only connector (transport for Stage 27).

Adapts the real Gmail provider to the Stage 27 email_message_reply transport
interface. STRICTLY READ-ONLY: this module has NO send, createDraft, modify,
delete, or label-mutation capability. The only Google API methods used are
messages.get / messages.list / threads.get (read-only) and users.getProfile.

Credentials: uses the existing Google Application Default Credentials
(google.auth.default) - never stores tokens/secrets in the repo. If no
credentials / scopes are available the connector reports an explicit blocker
(EMAIL_PROVIDER_UNCONFIGURED / EMAIL_OAUTH_BLOCKED) instead of inventing a
fallback transport.

Privacy: only non-secret metadata is logged; full email bodies are never
persisted by this module.
"""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# Gmail read-only scope - the MINIMAL scope that allows reading messages.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

DEFAULT_MAX_LIVE_EMAILS = 5


class GmailProviderError(Exception):
    """Raised for provider-level failures (credentials, scopes, API errors)."""


class GmailReadOnlyConnector:
    """Read-only Gmail transport adapter.

    Exposes a transport callable for
    email_message_reply.fetch_incoming_emails_readonly(transport). The
    transport returns raw email dicts; it cannot send/modify/delete.
    """

    def __init__(
        self,
        credentials=None,
        service: Optional[Any] = None,
        max_live_emails: int = DEFAULT_MAX_LIVE_EMAILS,
        query: str = "in:inbox is:unread newer_than:7d",
    ):
        self._credentials = credentials  # google-auth credentials (optional injection)
        self._service = service  # googleapiclient Gmail service (optional injection)
        self.max_live_emails = int(max_live_emails)
        self.query = query
        # instrumentation counters (read-only by construction)
        self.read_calls = 0
        self.mutation_calls = 0
        self.send_calls = 0
        self.delete_calls = 0
        self.modify_calls = 0

    # -- provider bootstrap --------------------------------------------------
    def _service_or_default(self):
        if self._service is not None:
            return self._service
        try:
            import google.auth
            import google.auth.transport.requests
            from googleapiclient.discovery import build
        except ImportError as e:
            raise GmailProviderError(
                f"EMAIL_OAUTH_BLOCKED: google libraries unavailable: {e}") from e
        creds = self._credentials
        if creds is None:
            try:
                creds, _project = google.auth.default(
                    scopes=[GMAIL_READONLY_SCOPE])
            except Exception as e:
                raise GmailProviderError(
                    f"EMAIL_OAUTH_BLOCKED: ADC unavailable: {e}") from e
        try:
            if creds.token is None and hasattr(creds, "refresh"):
                creds.refresh(google.auth.transport.requests.Request())
        except Exception as e:
            raise GmailProviderError(
                f"EMAIL_OAUTH_BLOCKED: token refresh failed (no gmail.readonly "
                f"scope?): {e}") from e
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # -- read-only API helpers -----------------------------------------------
    def _messages_list(self, service, user_id: str = "me") -> List[Dict[str, Any]]:
        """incoming-only discovery (search query), capped; never changes state."""
        self.read_calls += 1
        results = service.users().messages().list(
            userId=user_id, q=self.query, maxResults=self.max_live_emails
        ).execute()
        return results.get("messages", []) or []

    def _message_get(self, service, msg_id: str, user_id: str = "me") -> Dict[str, Any]:
        """read-only message fetch (no modify/trash/labels)."""
        self.read_calls += 1
        return service.users().messages().get(
            userId=user_id, id=msg_id, format="full"
        ).execute()

    def _thread_get(self, service, thread_id: str, user_id: str = "me") -> Dict[str, Any]:
        """read-only thread fetch."""
        self.read_calls += 1
        return service.users().threads().get(
            userId=user_id, id=thread_id, format="full"
        ).execute()

    # -- transport callable (Stage 27 contract) ------------------------------
    def transport(self) -> Callable[[], List[Dict[str, Any]]]:
        """Return a Stage 27-compatible transport (list of raw email dicts)."""

        def _run() -> List[Dict[str, Any]]:
            service = self._service_or_default()
            meta = service.users().getProfile(userId="me").execute()  # read-only
            self.read_calls += 1
            out: List[Dict[str, Any]] = []
            for m in self._messages_list(service):
                try:
                    full = self._message_get(service, m["id"])
                except Exception:
                    continue  # safe skip on malformed message
                out.append(self._to_email_dict(full))
            return out

        return _run

    # -- mapping helpers -----------------------------------------------------
    def _to_email_dict(self, full_msg: Dict[str, Any]) -> Dict[str, Any]:
        headers = {}
        for h in full_msg.get("payload", {}).get("headers", []) or []:
            headers[(h.get("name") or "").lower()] = h.get("value") or ""

        sender = headers.get("from", "")
        sender_name, sender_email = self._split_sender(sender)
        body = self._extract_body(full_msg.get("payload") or {})

        return {
            "provider": "gmail",
            "message_id": full_msg.get("id"),
            "thread_id": full_msg.get("threadId"),
            "sender_name": sender_name,
            "sender_email": sender_email,
            "subject": headers.get("subject", ""),
            "timestamp": headers.get("date"),
            "body_text": body,
            "reply_to": headers.get("reply-to") or headers.get("reply_to"),
            "has_real_message_id": bool(full_msg.get("id")),
        }

    @staticmethod
    def _split_sender(sender: str) -> (str, str):
        m = re.search(r"([^<]*)\s*<([^>]+)>", sender or "")
        if m:
            return m.group(1).strip(' "\''), m.group(2).strip()
        s = (sender or "").strip()
        if "@" in s and "<" not in s:
            return "", s
        return s, ""

    @staticmethod
    def _extract_body(payload: Dict[str, Any]) -> str:
        """Extract plain-text body (read-only). Falls back gracefully."""
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data")
            if data:
                try:
                    return base64.urlsafe_b64decode(data).decode("utf-8", "replace")
                except Exception:
                    return ""
        for part in payload.get("parts", []) or []:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data")
                if data:
                    try:
                        return base64.urlsafe_b64decode(data).decode("utf-8", "replace")
                    except Exception:
                        return ""
            nested = GmailReadOnlyConnector._extract_body(part)
            if nested:
                return nested
        return ""


# ---------------- provider status helpers -----------------------------------

def gmail_provider_status(credentials=None) -> Dict[str, Any]:
    """Determine Gmail access status WITHOUT exposing secrets.

    Returns one of:
      EMAIL_PROVIDER_UNCONFIGURED - no google libs / no ADC
      EMAIL_OAUTH_BLOCKED         - ADC present but gmail.readonly scope/refresh fails
      READY                       - read-only access available
    """
    try:
        import google.auth
    except ImportError:
        return {"status": "EMAIL_PROVIDER_UNCONFIGURED",
                "reason": "google-auth not installed"}
    try:
        creds = credentials
        if creds is None:
            creds, _ = google.auth.default(scopes=[GMAIL_READONLY_SCOPE])
        return {"status": "READY",
                "reason": "ADC present; gmail.readonly scope configured"}
    except Exception as e:
        return {"status": "EMAIL_OAUTH_BLOCKED",
                "reason": f"ADC/scope error: {e}"}


__all__ = [
    "DEFAULT_MAX_LIVE_EMAILS",
    "GMAIL_READONLY_SCOPE",
    "GmailProviderError",
    "GmailReadOnlyConnector",
    "gmail_provider_status",
]