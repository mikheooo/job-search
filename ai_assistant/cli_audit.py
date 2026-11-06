"""Integrity-audit CLI commands.

Split out of ai_assistant.cli so the audit domain has a real home.
cli.py re-exports these names; call sites and `python -m ai_assistant.cli audit`
stay the same.
"""

from __future__ import annotations

import sys

from .application_integrity import IntegritySeverity, run_integrity_audit
from .db import init_db


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
    print(f"Artifacts: queue={report.queue_items} reviews={report.reviews} browser={report.browser_preparations} submissions={report.submissions} verifications={report.verifications} aliases={report.aliases}")
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
    try:
        report = run_integrity_audit(scope=scope)
    except Exception as e:
        print(f"AUDIT FAILURE: {e}", file=sys.stderr)
        return 3
    
    errors = [i for i in report.issues if i.severity == IntegritySeverity.ERROR]
    if not errors:
        print("No ERROR issues found.")
        return 0
    
    print(f"ERROR issues ({len(errors)}):")
    for i, issue in enumerate(errors, 1):
        print(f"  {i}. [{issue.code}] {issue.vacancy_stable_id}")
        if issue.canonical_id:
            print(f"     Canonical: {issue.canonical_id}")
        print(f"     {issue.message}")
        if issue.evidence:
            for k, v in issue.evidence.items():
                print(f"     {k}: {v}")
        print()
    return 2


def audit_warnings(scope: str = "full") -> int:
    """Show only WARNING severity issues."""

    init_db()
    try:
        report = run_integrity_audit(scope=scope)
    except Exception as e:
        print(f"AUDIT FAILURE: {e}", file=sys.stderr)
        return 3
    
    warnings = [i for i in report.issues if i.severity == IntegritySeverity.WARNING]
    if not warnings:
        print("No WARNING issues found.")
        return 0
    
    print(f"WARNING issues ({len(warnings)}):")
    for i, issue in enumerate(warnings, 1):
        print(f"  {i}. [{issue.code}] {issue.vacancy_stable_id}")
        if issue.canonical_id:
            print(f"     Canonical: {issue.canonical_id}")
        print(f"     {issue.message}")
        if issue.evidence:
            for k, v in issue.evidence.items():
                print(f"     {k}: {v}")
        print()
    return 1


def audit_json(errors_only: bool = False, warnings_only: bool = False, scope: str = "full") -> int:
    """Output audit report as JSON."""
    import json

    init_db()
    try:
        report = run_integrity_audit(scope=scope)
    except Exception as e:
        print(json.dumps({"error": f"AUDIT FAILURE: {e}"}, ensure_ascii=False))
        return 3
    
    issues = report.issues
    if errors_only:
        issues = [i for i in issues if i.severity == IntegritySeverity.ERROR]
    elif warnings_only:
        issues = [i for i in issues if i.severity == IntegritySeverity.WARNING]
    
    output = {
        "generated_at": report.generated_at,
        "scope": report.scope,
        "total_checked": report.total_checked,
        "canonical_checked": report.canonical_checked,
        "info_count": report.info_count,
        "warning_count": report.warning_count,
        "error_count": report.error_count,
        "healthy": report.healthy,
        "queue_items": report.queue_items,
        "reviews": report.reviews,
        "browser_preparations": report.browser_preparations,
        "submissions": report.submissions,
        "verifications": report.verifications,
        "aliases": report.aliases,
        "issues": [
            {
                "severity": issue.severity.value,
                "code": issue.code,
                "vacancy_stable_id": issue.vacancy_stable_id,
                "canonical_id": issue.canonical_id,
                "message": issue.message,
                "evidence": issue.evidence,
            }
            for issue in issues
        ],
    }
    
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if report.error_count > 0:
        return 2
    elif report.warning_count > 0:
        return 1
    return 0


def audit_show(vacancy_stable_id: str) -> int:
    """Show audit details for a specific vacancy."""

    init_db()
    report = run_integrity_audit()
    
    issues = [i for i in report.issues if i.vacancy_stable_id == vacancy_stable_id]
    if not issues:
        print(f"No issues found for {vacancy_stable_id}")
        return 0
    
    print(f"=== AUDIT FOR {vacancy_stable_id} ===")
    for i, issue in enumerate(issues, 1):
        print(f"  {i}. [{issue.severity.value}] {issue.code}")
        if issue.canonical_id:
            print(f"     Canonical: {issue.canonical_id}")
        print(f"     {issue.message}")
        if issue.evidence:
            for k, v in issue.evidence.items():
                print(f"     {k}: {v}")
        print()
    return 0


def audit_canonical(canonical_id: str) -> int:
    """Show audit for a canonical vacancy."""

    from .vacancy_identity import get_aliases_for_canonical
    init_db()
    report = run_integrity_audit()
    
    aliases = get_aliases_for_canonical(canonical_id)
    sids = [a['vacancy_stable_id'] for a in aliases]
    issues = [i for i in report.issues if i.vacancy_stable_id in sids or i.canonical_id == canonical_id]
    
    if not issues:
        print(f"No issues found for canonical {canonical_id}")
        return 0
    
    print(f"=== AUDIT FOR CANONICAL {canonical_id} ===")
    print(f"Aliases: {len(aliases)}")
    for alias in aliases:
        print(f"  {alias['vacancy_stable_id']} ({alias['source']}) - {alias['match_type']} ({alias['confidence']}%)")
    print()
    
    for i, issue in enumerate(issues, 1):
        print(f"  {i}. [{issue.severity.value}] {issue.code}")
        print(f"     Vacancy: {issue.vacancy_stable_id}")
        print(f"     {issue.message}")
        if issue.evidence:
            for k, v in issue.evidence.items():
                print(f"     {k}: {v}")
        print()
    return 0
