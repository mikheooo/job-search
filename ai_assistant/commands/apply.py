"""Application preparation and submission command helpers."""
from __future__ import annotations

import logging
import sys

from ai_assistant.browser import (
    CDPBrowserAdapter,
    MockBrowserAdapter,
    PlaywrightBrowserAdapter,
)

logger = logging.getLogger("commands.apply")


def get_default_browser_adapter(adapter_name: str | None = None):
    """Instantiate appropriate browser adapter based on name."""
    if adapter_name == "mock":
        return MockBrowserAdapter()
    elif adapter_name == "cdp":
        from ai_assistant.cli import _default_hh_cdp_url
        from ai_assistant.hh_browser_launcher import CdpUrlRequired
        try:
            return CDPBrowserAdapter(_default_hh_cdp_url())
        except CdpUrlRequired as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return None
    elif adapter_name == "playwright":
        from ai_assistant.hh_browser_launcher import CdpUrlRequired
        try:
            return PlaywrightBrowserAdapter()
        except CdpUrlRequired as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return None
    return None
