"""Tests for Habr Career adapter."""

from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest

from ai_assistant.adapters.habr_career import HabrCareerAdapter


SAMPLE_HABR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Хабр Карьера</title>
    <link>https://career.habr.com/vacancies</link>
    <description>Свежие вакансии</description>
    <item>
      <title>Python / AI Developer (150 000 - 250 000 ₽)</title>
      <link>https://career.habr.com/vacancies/1000999999</link>
      <guid>https://career.habr.com/vacancies/1000999999</guid>
      <author>AI Tech Labs</author>
      <description><![CDATA[Ищем Python разработчика для создания AI агентов. Навыки: #python #llm #fastapi]]></description>
      <pubDate>Fri, 28 Aug 2026 12:00:00 +0300</pubDate>
      <category>python</category>
      <category>llm</category>
    </item>
  </channel>
</rss>
"""


def test_habr_career_adapter_fetches_and_parses():
    adapter = HabrCareerAdapter()
    assert adapter.source == "habrcareer"

    import feedparser
    parsed_feed = feedparser.parse(SAMPLE_HABR_RSS)

    with patch("feedparser.parse", return_value=parsed_feed):
        vacancies = adapter.fetch_vacancies()
        assert len(vacancies) == 1
        vac = vacancies[0]

        assert vac.source == "habrcareer"
        assert vac.source_job_id == "1000999999"
        assert vac.company == "AI Tech Labs"
        assert "Python / AI Developer" in vac.title
        assert vac.job_url == "https://career.habr.com/vacancies/1000999999"
        assert vac.salary_min == 150000
        assert vac.salary_max == 250000
        assert vac.salary_currency == "RUB"
        assert vac.published_at is not None
