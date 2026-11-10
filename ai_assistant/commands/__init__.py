"""Domain-specific CLI command implementations for JobSearch Assistant."""
from __future__ import annotations

from ai_assistant.commands.apply import get_default_browser_adapter
from ai_assistant.commands.audit import (
    application_audit_cmd,
    audit,
    audit_canonical,
    audit_errors,
    audit_json,
    audit_show,
    audit_warnings,
    questionnaire_audit_cmd,
    submissions_audit,
)
from ai_assistant.commands.collect import collect
from ai_assistant.commands.production import (
    autonomous_once_cmd,
    autonomous_start_cmd,
    autonomous_status_cmd,
    production_control_cmd,
    production_health_cmd,
    production_run_cmd,
)

__all__ = [
    "application_audit_cmd",
    "audit",
    "audit_canonical",
    "audit_errors",
    "audit_json",
    "audit_show",
    "audit_warnings",
    "autonomous_once_cmd",
    "autonomous_start_cmd",
    "autonomous_status_cmd",
    "collect",
    "get_default_browser_adapter",
    "production_control_cmd",
    "production_health_cmd",
    "production_run_cmd",
    "questionnaire_audit_cmd",
    "submissions_audit",
]
