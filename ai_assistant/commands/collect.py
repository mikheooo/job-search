"""Collection and ingestion subcommands."""
from __future__ import annotations

import logging

from ai_assistant.adapters.habr_career import HabrCareerAdapter
from ai_assistant.adapters.himalayas import HimalayasAdapter
from ai_assistant.adapters.remoteok import RemoteOkAdapter
from ai_assistant.adapters.weworkremotely import WeWorkRemotelyAdapter
from ai_assistant.db import get_vacancy_by_id, init_db, save_vacancy
from ai_assistant.normalizer import normalize_vacancy

logger = logging.getLogger("commands.collect")

SOURCES = {
    "himalayas": HimalayasAdapter(),
    "weworkremotely": WeWorkRemotelyAdapter(),
    "remoteok": RemoteOkAdapter(),
    "habrcareer": HabrCareerAdapter(),
}


def collect(sources: list[str]) -> int:
    """Fetch and ingest vacancies from specified sources into the database."""
    init_db()
    adapters = [SOURCES[name] for name in sources if name in SOURCES]
    if not adapters:
        raise ValueError(f"Unknown sources: {sources}")

    stats = {"fetched": 0, "new": 0, "duplicate": 0, "failed": 0}

    for adapter in adapters:
        try:
            vacancies = adapter.fetch_vacancies()
        except Exception as e:
            logger.error("Failed to fetch from %s: %s", adapter.source, e)
            stats["failed"] += 1
            continue

        for item in vacancies:
            stats["fetched"] += 1
            vacancy = normalize_vacancy(item.to_dict() if hasattr(item, "to_dict") else item)
            existing = get_vacancy_by_id(vacancy.stable_id())
            if existing:
                stats["duplicate"] += 1
                continue
            save_vacancy(vacancy)
            stats["new"] += 1

    logger.info("Collect stats: %s", stats)
    return stats["new"]
