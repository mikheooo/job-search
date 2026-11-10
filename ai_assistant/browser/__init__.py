"""Browser automation adapters, types, and classification package."""
from __future__ import annotations

from ai_assistant.browser.base import BrowserAdapter
from ai_assistant.browser.cdp_adapter import CDPBrowserAdapter
from ai_assistant.browser.mock_adapter import INSPECT_UNREADABLE, MockBrowserAdapter
from ai_assistant.browser.page_checks import _BLOCK_TEXT_MARKERS, _detect_page_blocked, _detect_site
from ai_assistant.browser.playwright_adapter import PlaywrightBrowserAdapter
from ai_assistant.browser.types import (
    EXECUTOR_VERSION,
    KNOWN_AGGREGATOR_DOMAINS,
    KNOWN_ATS_DOMAINS,
    AuthState,
    BrowserApplicationSession,
    BrowserResult,
    BrowserStatus,
    FlowClassification,
    FlowType,
    SubmitResult,
    SubmitStatus,
    _note_failure,
    classify_apply_flow,
)

__all__ = [
    "EXECUTOR_VERSION",
    "KNOWN_AGGREGATOR_DOMAINS",
    "KNOWN_ATS_DOMAINS",
    "AuthState",
    "BrowserAdapter",
    "BrowserApplicationSession",
    "BrowserResult",
    "BrowserStatus",
    "CDPBrowserAdapter",
    "FlowClassification",
    "FlowType",
    "INSPECT_UNREADABLE",
    "MockBrowserAdapter",
    "PlaywrightBrowserAdapter",
    "SubmitResult",
    "SubmitStatus",
    "_BLOCK_TEXT_MARKERS",
    "_detect_page_blocked",
    "_detect_site",
    "_note_failure",
    "classify_apply_flow",
]
