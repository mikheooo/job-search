"""Owner-facing HH authentication notifications with dedup and recovery.

The autopilot used to write its suppression marker before Telegram confirmed
delivery, so a failed send silently muted the next 12 hours. This module only
marks an outage as notified after a confirmed send, suppresses repeats inside
the cooldown, and emits exactly one recovery message when the gate passes
again.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import pathlib
from typing import Callable

from ai_assistant import telegram_routing

AUTH_FAILURE_MESSAGE = (
    "⚠️ <b>HH Autopilot: подтверждена потеря авторизации</b>\n\n"
    "Диагностика: на странице hh.ru обнаружена форма входа (hhrole=anonymous / login card).\n"
    "Действие: требуется ручной вход в профиль JobSearchHeadless.\n"
    "Запустите artifacts\\headless-poc\\hh-login-once.cmd и подтвердите вход."
)

AUTH_RECOVERED_MESSAGE = (
    "✅ <b>HH Autopilot: авторизация подтверждена</b>\n\n"
    "Сессия в профиле JobSearchHeadless активна, регулярные циклы работают в штатном режиме."
)

DEFAULT_STATE_FILE = pathlib.Path("logs") / "hh_auth_alert_state.json"
DEFAULT_COOLDOWN_SECONDS = 12 * 3600


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class AuthNotifier:
    def __init__(
        self,
        *,
        state_path: pathlib.Path | str = DEFAULT_STATE_FILE,
        send: Callable[[str], bool] | None = None,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        now: Callable[[], _dt.datetime] | None = None,
    ) -> None:
        self.state_path = pathlib.Path(state_path)
        self.send = send or (lambda text: telegram_routing.send_job_message(text))
        self.cooldown_seconds = int(cooldown_seconds)
        self._now = now or _utcnow

    def _read_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, state: dict) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(f".{id(self)}.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    @staticmethod
    def fingerprint(source: str, error_text: str) -> str:
        raw = f"{source}\n{(error_text or '')[:500]}".encode("utf-8", errors="replace")
        return hashlib.sha256(raw).hexdigest()[:16]

    def notify_failure(self, error_text: str, source: str = "hh_autopilot") -> bool:
        state = self._read_state()
        now = self._now()
        fingerprint = self.fingerprint(source, error_text)

        if state.get("active"):
            last = state.get("last_sent_at")
            try:
                sent_at = _dt.datetime.fromisoformat(str(last))
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=_dt.timezone.utc)
                age = (now - sent_at).total_seconds()
            except (TypeError, ValueError):
                age = self.cooldown_seconds + 1
            if age < self.cooldown_seconds:
                return False

        if not self.send(AUTH_FAILURE_MESSAGE):
            return False

        state.update(
            {
                "active": True,
                "last_sent_at": now.isoformat(),
                "last_source": source,
                "last_fingerprint": fingerprint,
                "last_error": (error_text or "")[:500],
            }
        )
        self._write_state(state)
        return True

    def notify_success(self, source: str = "hh_autopilot") -> bool:
        state = self._read_state()
        if not state.get("active"):
            return False

        if not self.send(AUTH_RECOVERED_MESSAGE):
            return False

        state.update(
            {
                "active": False,
                "recovered_at": self._now().isoformat(),
                "recovered_source": source,
            }
        )
        self._write_state(state)
        return True

    def is_active(self) -> bool:
        return bool(self._read_state().get("active"))
