"""Modular domain-specific database storage package for JobSearch Assistant."""
from __future__ import annotations

from ai_assistant.storage.applications import (
    get_hh_application,
    get_hh_application_by_vacancy,
    list_hh_applications,
)
from ai_assistant.storage.core import (
    db_cursor,
    db_session,
    get_connection,
    init_db,
    is_dry_run,
    set_dry_run,
)
from ai_assistant.storage.delivery import (
    list_autonomous_cycle_runs,
    list_interview_events,
)
from ai_assistant.storage.questionnaires import (
    get_hh_questionnaire,
    list_hh_questionnaires,
)
from ai_assistant.storage.vacancies import (
    count_vacancies_by_status,
    get_vacancy_by_id,
    list_vacancies,
    update_vacancy_state,
)

__all__ = [
    "count_vacancies_by_status",
    "db_cursor",
    "db_session",
    "get_connection",
    "get_hh_application",
    "get_hh_application_by_vacancy",
    "get_hh_questionnaire",
    "get_vacancy_by_id",
    "init_db",
    "is_dry_run",
    "list_autonomous_cycle_runs",
    "list_hh_applications",
    "list_hh_questionnaires",
    "list_interview_events",
    "list_vacancies",
    "set_dry_run",
    "update_vacancy_state",
]
