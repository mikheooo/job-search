"""Phase 3: the multi-source Telegram flash is gated by its own flag.

TELEGRAM_FLASH_SUPPRESS_ENABLED must suppress the 2-hourly "new vacancies"
flash (items are already persisted as DISCOVERED and reach the daily digest),
while leaving every other discovery behaviour unchanged.
"""
from __future__ import annotations

import scripts.run_multi_source_discovery as msd


class _StubAdapter:
    def __init__(self, *a, **k):
        pass

    def fetch_vacancies(self, *a, **k):
        return []

    def fetch_all(self, *a, **k):
        return []


_ADAPTERS = (
    "HabrCareerAdapter", "HirifyAdapter", "GeekJobAdapter",
    "BangkokDirectCareersAdapter", "LinkedInBangkokAdapter",
    "TelegramChannelsAdapter",
)


def _patch(monkeypatch, *, new_saved, alerts):
    for name in _ADAPTERS:
        monkeypatch.setattr(msd, name, _StubAdapter)
    monkeypatch.setattr(msd, "save_vacancies_to_db", lambda vacs: new_saved)
    monkeypatch.setattr(msd, "send_telegram_alert",
                        lambda text, chat_id=None: alerts.append(text) or True)


def test_flash_sent_when_flag_off(monkeypatch):
    monkeypatch.setattr(msd.config, "TELEGRAM_FLASH_SUPPRESS_ENABLED", False, raising=False)
    alerts = []
    _patch(monkeypatch, new_saved=3, alerts=alerts)
    msd.run_discovery(notify=True)
    assert len(alerts) == 1


def test_flash_suppressed_when_flag_on(monkeypatch):
    monkeypatch.setattr(msd.config, "TELEGRAM_FLASH_SUPPRESS_ENABLED", True, raising=False)
    alerts = []
    _patch(monkeypatch, new_saved=3, alerts=alerts)
    msd.run_discovery(notify=True)
    assert alerts == []


def test_notify_false_never_sends(monkeypatch):
    monkeypatch.setattr(msd.config, "TELEGRAM_FLASH_SUPPRESS_ENABLED", False, raising=False)
    alerts = []
    _patch(monkeypatch, new_saved=3, alerts=alerts)
    msd.run_discovery(notify=False)
    assert alerts == []


def test_no_new_items_never_sends(monkeypatch):
    monkeypatch.setattr(msd.config, "TELEGRAM_FLASH_SUPPRESS_ENABLED", False, raising=False)
    alerts = []
    _patch(monkeypatch, new_saved=0, alerts=alerts)
    msd.run_discovery(notify=True)
    assert alerts == []
