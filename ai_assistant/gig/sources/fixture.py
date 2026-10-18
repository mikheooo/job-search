"""Fixture-адаптер источника гигов (вариант B по ТЗ §8).

P1 не пишет парсеры реальных площадок: этот адаптер читает JSON-файл с
реалистичными заказами (структура повторяет выдачу Upwork-подобных API,
без реальных персональных данных). Данные реалистичны, но синтетические —
это честно помечено в отчёте и в поле ``source=fixture``.

Файл по умолчанию: ``artifacts/gig_fixture_sample.json`` — переопределяется
аргументом/переменной ``GIG_FIXTURE_FILE``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

DEFAULT_FIXTURE = Path(__file__).resolve().parents[3] / "artifacts" / "gig_fixture_sample.json"


class FixtureGigSource:
    """Источник гигов из JSON-фикстуры."""

    key = "fixture"

    def __init__(self, path: str | Path | None = None) -> None:
        env = os.getenv("GIG_FIXTURE_FILE", "").strip()
        self.path = Path(path) if path else (Path(env) if env else DEFAULT_FIXTURE)
        #: Phase 4C §3: fixture is synthetic but reports health like others.
        self._last_health = None

    def load(self) -> list[dict]:
        if not self.path.exists():
            raise FileNotFoundError(
                f"fixture-файл гигов не найден: {self.path}; создайте его или задайте GIG_FIXTURE_FILE"
            )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("gigs") or []
        if not isinstance(payload, list):
            raise ValueError(f"fixture {self.path}: ожидается список заказов")
        return payload

    def fetch(self, limit: int | None = None) -> Iterator[dict]:
        from .base import SourceHealth, summarize_health

        produced = 0
        try:
            for item in self._fetch_records(limit):
                produced += 1
                yield item
        finally:
            self._last_health = summarize_health(produced, [])

    def health(self):
        from .base import SourceHealth

        return self._last_health or SourceHealth()

    def _fetch_records(self, limit: int | None = None) -> Iterator[dict]:
        items = self.load()
        if limit is not None:
            items = items[: int(limit)]
        for item in items:
            if not isinstance(item, dict):
                continue
            # источник проставляем централизованно, чтобы fixture-файл был
            # нейтральным к смене ключа источника; source_mode=fixture (P2 §7)
            # гарантирует, что тестовые данные отличимы от реальных.
            yield {
                "source": self.key,
                "source_mode": "fixture",
                "source_scope": "synthetic_fixture",
                **item,
            }
