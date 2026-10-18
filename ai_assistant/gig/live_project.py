"""P6 authoritative public project read, beyond cached SEO HTML.

Only anonymous GET is used. A failed API read never falls back to claiming that
cached SEO bid counts are live. HTML still supplies the public client snapshot.
"""
from __future__ import annotations

import copy
import gzip
import json
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .provider_policy import retention_seconds


def project_api_url(project_id: str) -> str:
    number = str(project_id).removeprefix("fl_")
    if not number.isdigit():
        raise ValueError("Freelancer project id must be numeric")
    return (f"https://www.freelancer.com/api/projects/0.1/projects/{number}/"
            "?full_description=true&job_details=true")


def overlay_public_project(page_record: dict[str, Any], result: dict[str, Any], *,
                           observed_at: str, api_url: str) -> dict[str, Any]:
    expected = str(page_record["source_id"]).removeprefix("fl_")
    if str(result.get("id")) != expected:
        raise ValueError("API project identity does not match the opened page")
    record = copy.deepcopy(page_record)
    record["description"] = result.get("description") or ""
    record["title"] = result.get("title") or ""
    record["skills"] = [j["name"] for j in result.get("jobs") or [] if j.get("name")]
    budget = result.get("budget") or {}
    record["budget"] = {"min": budget.get("minimum"), "max": budget.get("maximum"),
                        "type": result.get("type")}
    currency = result.get("currency") or {}
    record["currency"] = currency.get("code")
    record["competition"] = (result.get("bid_stats") or {}).get("bid_count")
    frontend = str(result.get("frontend_project_status") or "").lower()
    if result.get("deleted") or result.get("nonpublic"):
        status = "CLOSED"
    elif frontend == "open" and result.get("status") == "active":
        status = "OPEN"
    elif frontend in {"awarded", "in progress", "in_progress"} or result.get("sub_status") == "awarded":
        status = "AWARDED"
    elif frontend in {"closed", "complete", "completed", "cancelled"}:
        status = "CLOSED"
    else:
        status = "UNKNOWN"
    record["source_status"] = status
    stamp = result.get("time_submitted") or result.get("submitdate")
    record["published_at"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp else None
    record["fetched_at"] = observed_at
    facts = record.setdefault("source_facts", {})
    facts.update({"freelancer_page_bids_cached": page_record.get("competition"),
                  "freelancer_api_bids_live": record["competition"],
                  "freelancer_currency_exchange_rate": currency.get("exchange_rate"),
                  "freelancer_status": result.get("status"),
                  "freelancer_api_url": api_url, "freelancer_api_verified_at": observed_at,
                  "freelancer_frontend_status": frontend,
                  "client_data_surface": "public HTML snapshot; API does not expose owner history"})
    return record


def p6_api_access_reasons(policy, *, encrypted_storage_available=False, now=None):
    policy = policy or {}
    reasons = []
    if policy.get('automated_access_allowed') is not True or not policy.get('express_written_permission_reference'):
        reasons.append('AUTOMATED_ACCESS_EXPRESS_WRITTEN_PERMISSION_REQUIRED')
    if policy.get('api_project_read_allowed') is not True:
        reasons.append('PROVIDER_PERMISSION_UNKNOWN:api_project_read_allowed')
    if policy.get('api_data_storage_compliant') is not True or not policy.get('storage_evidence_reference'):
        reasons.append('API_DATA_STORAGE_COMPLIANCE_UNVERIFIED')
    if (policy.get('api_data_retention_permitted') is not True
            or not policy.get('retention_permission_reference')
            or retention_seconds(policy) is None):
        reasons.append('API_DATA_RETENTION_PERMISSION_UNVERIFIED')
    if not encrypted_storage_available:
        reasons.append('ENCRYPTED_ARTIFACT_STORAGE_REQUIRED')
    try:
        if datetime.fromisoformat(policy['expires_at'].replace('Z', '+00:00')) <= (now or datetime.now(timezone.utc)):
            reasons.append('POLICY_EVIDENCE_EXPIRED')
    except (KeyError, TypeError, ValueError, AttributeError):
        reasons.append('POLICY_EVIDENCE_EXPIRY_UNKNOWN')
    return reasons


def fetch_public_project(page_record: dict[str, Any], *, source: Any,
                         access_policy: dict[str, Any] | None = None,
                         encrypted_storage_available=False) -> tuple[dict, dict]:
    reasons = p6_api_access_reasons(access_policy, encrypted_storage_available=encrypted_storage_available)
    if reasons:
        raise PermissionError('PUBLIC_API_ACCESS_BLOCKED: ' + ','.join(reasons))
    url = project_api_url(page_record["source_id"])
    if not source._robots_allows(url):
        raise ValueError("robots.txt disallows the public project API URL")
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "Accept-Encoding": "gzip",
                                                   "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    payload = json.loads(body)
    if payload.get("status") != "success" or not isinstance(payload.get("result"), dict):
        raise ValueError("public project API did not return a successful observation")
    when = datetime.now(timezone.utc).isoformat()
    result = payload["result"]
    return overlay_public_project(page_record, result, observed_at=when, api_url=url), result
