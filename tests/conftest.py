import sys
import os
from pathlib import Path
import pytest

ai_assistant_dir = Path(__file__).resolve().parent.parent / "ai_assistant"
if str(ai_assistant_dir) not in sys.path:
    sys.path.insert(0, str(ai_assistant_dir))


@pytest.fixture(autouse=True)
def global_telegram_test_isolation(monkeypatch):
    """Global fixture ensuring that NO test in pytest can ever make live network calls to Telegram."""
    import urllib.request

    orig_urlopen = urllib.request.urlopen

    def safe_urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "api.telegram.org" in url:
            raise RuntimeError(
                f"SAFETY VIOLATION: Unmocked live Telegram HTTP request attempted during pytest: {url}"
            )
        return orig_urlopen(req, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", safe_urlopen)
