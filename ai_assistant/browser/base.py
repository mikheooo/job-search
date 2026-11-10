"""Abstract base class for browser automation adapters."""
from __future__ import annotations

from typing import Any


class BrowserAdapter:
    """Abstract BrowserAdapter interface: navigation, inspection, interaction, and submit."""

    def open(self, url: str) -> dict[str, Any]:
        raise NotImplementedError

    def inspect_page(self) -> dict[str, Any]:
        raise NotImplementedError

    def fill_field(self, selector: str, value: str) -> bool:
        raise NotImplementedError

    def upload_file(self, selector: str, path: str) -> bool:
        raise NotImplementedError

    def screenshot(self, path: str) -> str | None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def get_current_url(self) -> str:
        raise NotImplementedError

    def get_title(self) -> str:
        raise NotImplementedError

    def submit_application(self) -> dict[str, Any]:
        raise NotImplementedError

    def extract_application_form(self) -> dict[str, Any]:
        """Platform-aware: read current page and return normalized DOM snapshot."""
        raise NotImplementedError
