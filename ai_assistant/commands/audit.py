"""Audit and verification subcommands."""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from ai_assistant.application_integrity import IntegritySeverity, run_integrity_audit
from ai_assistant.db import init_db

logger = logging.getLogger("commands.audit")


def audit(scope: str = "full") -> int:
    """Run full integrity audit."""
    init_db()
    report = run_integrity_audit(scope=scope)

    print("=== APPLICATION INTEGRITY AUDIT ===")
    print()
    print(f"Scope: {report.scope}")
    print(f"Generated: {report.generated_at}")
    print(f"Total vacancies checked: {report.total_checked}")
    print(f"Canonical vacancies checked: {report.canonical_checked}")
    print(
        f"Artifacts: queue={report.queue_items} reviews={report.reviews} "
        f"browser={report.browser_preparations} submissions={report.submissions} "
        f"verifications={report.verifications} aliases={report.aliases}"
    )
    print()
    print(f"INFO:    {report.info_count}")
    print(f"WARNING: {report.warning_count}")
    print(f"ERROR:   {report.error_count}")
    print()

    if report.issues:
        print("ISSUES:")
        for i, issue in enumerate(report.issues, 1):
            print(f"  {i}. [{issue.severity.value}] {issue.code}")
            print(f"     Vacancy: {issue.vacancy_stable_id}")
            if issue.canonical_id:
                print(f"     Canonical: {issue.canonical_id}")
            print(f"     {issue.message}")
            if issue.evidence:
                for k, v in issue.evidence.items():
                    print(f"     {k}: {v}")
            print()

    print(f"HEALTH: {'PASS' if report.healthy else 'FAIL'}")

    if report.error_count > 0:
        return 2
    elif report.warning_count > 0:
        return 1
    return 0


def audit_errors(scope: str = "full") -> int:
    """Show only ERROR severity issues."""
    init_db()
    report = run_integrity_audit(scope=scope)
    errors = [i for i in report.issues if i.severity == IntegritySeverity.ERROR]
    print(f"=== APPLICATION INTEGRITY AUDIT -- ERRORS ONLY ({len(errors)}) ===")
    print()
    for i, issue in enumerate(errors, 1):
        print(f"  {i}. [{issue.severity.value}] {issue.code}")
        print(f"     Vacancy: {issue.vacancy_stable_id}")
        if issue.canonical_id:
            print(f"     Canonical: {issue.canonical_id}")
        print(f"     {issue.message}")
        print()
    return 2 if errors else 0


def audit_warnings(scope: str = "full") -> int:
    """Show only WARNING severity issues."""
    init_db()
    report = run_integrity_audit(scope=scope)
    warnings = [i for i in report.issues if i.severity == IntegritySeverity.WARNING]
    print(f"=== APPLICATION INTEGRITY AUDIT -- WARNINGS ONLY ({len(warnings)}) ===")
    print()
    for i, issue in enumerate(warnings, 1):
        print(f"  {i}. [{issue.severity.value}] {issue.code}")
        print(f"     Vacancy: {issue.vacancy_stable_id}")
        if issue.canonical_id:
            print(f"     Canonical: {issue.canonical_id}")
        print(f"     {issue.message}")
        print()
    return 1 if warnings else 0


def audit_json(errors_only: bool = False, warnings_only: bool = False, scope: str = "full") -> int:
    """Output audit report as JSON."""
    init_db()
    report = run_integrity_audit(scope=scope)
    data = report.to_dict()
    if errors_only:
        data["issues"] = [i for i in data["issues"] if i["severity"] == "ERROR"]
    elif warnings_only:
        data["issues"] = [i for i in data["issues"] if i["severity"] == "WARNING"]
    print(json.dumps(data, indent=2, ensure_ascii=False))
    if report.error_count > 0:
        return 2
    elif report.warning_count > 0:
        return 1
    return 0


def audit_show(vacancy_stable_id: str) -> int:
    """Show detailed audit for a single vacancy."""
    from ai_assistant.application_integrity import audit_vacancy
    init_db()
    issues = audit_vacancy(vacancy_stable_id)
    print(f"=== AUDIT: {vacancy_stable_id} ===")
    print(f"Issues: {len(issues)}")
    for i, issue in enumerate(issues, 1):
        print(f"  {i}. [{issue.severity.value}] {issue.code}: {issue.message}")
        if issue.evidence:
            for k, v in issue.evidence.items():
                print(f"     {k}: {v}")
    has_errors = any(i.severity == IntegritySeverity.ERROR for i in issues)
    has_warnings = any(i.severity == IntegritySeverity.WARNING for i in issues)
    if has_errors:
        return 2
    elif has_warnings:
        return 1
    return 0


def audit_canonical(canonical_id: str) -> int:
    """Show detailed audit for a canonical vacancy cluster."""
    from ai_assistant.application_integrity import audit_canonical as _audit_canonical
    init_db()
    issues = _audit_canonical(canonical_id)
    print(f"=== AUDIT CANONICAL: {canonical_id} ===")
    print(f"Issues: {len(issues)}")
    for i, issue in enumerate(issues, 1):
        print(f"  {i}. [{issue.severity.value}] {issue.code}: {issue.message}")
    has_errors = any(i.severity == IntegritySeverity.ERROR for i in issues)
    has_warnings = any(i.severity == IntegritySeverity.WARNING for i in issues)
    if has_errors:
        return 2
    elif has_warnings:
        return 1
    return 0


def submissions_audit(vacancy_stable_id: str) -> int:
    """Show full chronological audit trail."""
    from ai_assistant.db import _row_to_vacancy, get_vacancy_by_id
    from ai_assistant.submission_recovery import get_submission_audit

    init_db()
    row = get_vacancy_by_id(vacancy_stable_id)
    if row:
        vac = _row_to_vacancy(row)
        print(f"Vacancy: {vacancy_stable_id}")
        print(f"Title: {vac.title}")
        print(f"Company: {vac.company}")
        print(f"URL: {vac.job_url}")
        print()

    events = get_submission_audit(vacancy_stable_id)
    incomplete = [e for e in events if e.get("type") == "AUDIT_INCOMPLETE"]
    if incomplete:
        print("WARNING: Incomplete audit trail detected:")
        for inc in incomplete:
            print(f"  - {inc.get('message')}")
        print()

    print(f"Audit events ({len(events)}):")
    for ev in events:
        ts = ev.get("timestamp", "unknown")
        tp = ev.get("type", "unknown")
        dt = ev.get("details", {})
        print(f"  [{ts}] {tp}: {dt}")
    return 0


def questionnaire_audit_cmd(target_id: str) -> int:
    """Run a pre-submit audit on questionnaire answers against candidate profile and facts."""
    from ai_assistant.hh_questionnaire_audit import audit_questionnaire

    try:
        report = audit_questionnaire(target_id)
        print()
        print(report.format_cli_output())
        print()
        return 0 if report.overall.value == "SAFE_TO_SUBMIT" else 1
    except Exception as e:
        print(f"Error during questionnaire audit: {e}", file=sys.stderr)
        return 1


def application_audit_cmd(target_id: str) -> int:
    """Run a pre-submit audit on application questionnaire answers."""
    import ai_assistant.db as db
    from ai_assistant.hh_questionnaire_audit import audit_questionnaire

    db.init_db()
    data = db.get_hh_application(target_id)
    if not data:
        data = db.get_hh_application_by_vacancy(target_id)
    if not data:
        data = db.get_hh_application_by_conversation(target_id)

    qid = data.get("questionnaire_id") if data else target_id
    if not qid:
        print(f"Error: No questionnaire found associated with application '{target_id}'", file=sys.stderr)
        return 1

    try:
        report = audit_questionnaire(qid, application_id=data.get("application_id") if data else target_id)
        print()
        print(report.format_cli_output())
        print()
        return 0 if report.overall.value == "SAFE_TO_SUBMIT" else 1
    except Exception as e:
        print(f"Error during application audit: {e}", file=sys.stderr)
        return 1
