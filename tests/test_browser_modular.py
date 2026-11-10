"""Test suite verifying modular browser package exports and behavior."""
from __future__ import annotations

import pytest

from ai_assistant.browser import (
    BrowserAdapter,
    CDPBrowserAdapter,
    FlowType,
    MockBrowserAdapter,
    PlaywrightBrowserAdapter,
    SubmitStatus,
    classify_apply_flow,
)


def test_modular_browser_exports():
    """All core browser adapters and types are exportable from ai_assistant.browser."""
    assert issubclass(MockBrowserAdapter, BrowserAdapter)
    assert issubclass(CDPBrowserAdapter, BrowserAdapter)
    assert issubclass(PlaywrightBrowserAdapter, BrowserAdapter)


def test_modular_mock_browser_adapter_flow():
    """MockBrowserAdapter operates correctly through the modular interface."""
    adapter = MockBrowserAdapter({"final_url": "https://hh.ru/vacancy/123", "page_title": "Python Dev"})
    res = adapter.open("https://hh.ru/vacancy/123")
    assert res["final_url"] == "https://hh.ru/vacancy/123"
    assert res["blocked"] is False
    assert adapter.get_title() == "Python Dev"


def test_modular_classify_apply_flow():
    """Flow classification functions cleanly in the modular browser sub-package."""
    cls = classify_apply_flow(
        source_url="https://jobs.lever.co/company/job123",
        has_form=True,
    )
    assert cls.flow_type == FlowType.EXTERNAL_ATS
    assert cls.is_external_application is False
