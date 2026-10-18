"""Bounded prepare-only P6 run; public GET collection, no proposal submission.

Run: python -m ai_assistant.gig.first_money_run --limit 100 --output artifacts/p6
The result remains a QA candidate queue until a separate review is completed.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .assessment import load_candidate_profile, skill_confidences
from .capability import assess_capability
from .client import assess_client_risk
from .first_money import build_shortlist, first_money_score_of
from .grounded import analyze
from .jit import apply_verification, verify_gig
from .live_project import fetch_public_project, p6_api_access_reasons
from .pipeline import GigPipeline
from .sources.freelancer import FreelancerGigSource
from .action import action_score_of
from .assessment import effective_hourly_rate
from .provider_policy import retention_seconds, website_access_reasons
from .submission.secure_store import load_retained, purge_expired_artifacts, save_retained


def prepare_run(*, limit: int = 100, output: str = "artifacts/p6", resume_collection: bool = False,
                review_file: str | None = None, access_policy: dict | None = None) -> dict:
    site_reasons = website_access_reasons(access_policy)
    api_reasons = p6_api_access_reasons(access_policy, encrypted_storage_available=(os.name == 'nt'))
    if site_reasons or api_reasons:
        raise PermissionError('P6 live collection blocked: ' + ','.join(dict.fromkeys(site_reasons + api_reasons)))
    from .submission import secure_store
    probe = b'gig-search-p6-dpapi-storage-check'
    try:
        if secure_store._crypt(secure_store._crypt(probe), decrypt=True) != probe:
            raise RuntimeError('DPAPI round-trip mismatch')
    except Exception:
        raise PermissionError('P6 live collection blocked: DPAPI storage unavailable') from None
    ttl = retention_seconds(access_policy)
    expires_at = (datetime.now(timezone.utc).timestamp() + ttl)
    expires_at = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
    dest = Path(output)
    dest.mkdir(parents=True, exist_ok=True)
    purge_expired_artifacts(dest)
    scope_reviews = json.loads(Path(review_file).read_text(encoding="utf-8")) if review_file else {}
    profile = load_candidate_profile()
    confidence = skill_confidences(profile)
    levels = profile.get("skill_confidence") or {}
    pipeline = GigPipeline(profile=profile, persist=False)
    source = FreelancerGigSource(target_skills=["python", "web-scraping", "automation", "n8n", "telegram", "excel", "api"],
                                 include_general_listing=False, listing_pages=0,
                                 skill_pages=1, delay_seconds=1, max_requests=160, timeout_seconds=8,
                                 access_policy=access_policy)
    print("Collecting current public listings and project pages", flush=True)
    raw_records = []
    if resume_collection:
        raw_records = load_retained(dest / "fresh_collection.dpapi.json")
    else:
        for record in source.fetch(limit=limit):
            raw_records.append(record)
            if len(raw_records) % 10 == 0:
                print(f"Collected {len(raw_records)} fresh project pages", flush=True)
    save_retained(dest / "fresh_collection.dpapi.json", raw_records, expires_at=expires_at)
    now = datetime.now(timezone.utc)
    scored = []
    for raw in raw_records:
        gig = pipeline.process_raw(raw)
        analysis = analyze(gig.description, gig.title)
        cap = assess_capability(analysis, confidence, profile_skills=levels)
        risk = assess_client_risk(gig, now=now)
        score = first_money_score_of(gig, analysis, cap, risk, now=now)
        scored.append((score.score, gig, cap.fit))
    scored.sort(key=lambda item: -item[0])
    summary = [{"id": g.source_id, "title": g.title, "fit": fit, "score": score, "url": g.url,
                "bids": g.competition, "budget": g.budget_text} for score, g, fit in scored]
    save_retained(dest / "collection_ranking.dpapi.json", summary, expires_at=expires_at)
    print(f"Fresh collection: {len(raw_records)} projects, {source.requests_made} requests", flush=True)
    entries = []
    observations = []
    for _, gig, fit in scored:
        if fit not in {"proven", "strong", "learnable"}:
            continue
        if len(entries) >= 20:
            break
        html = source._http_get(gig.url)
        fresh = source.record_from_html(html, page_url=gig.url,
                                       fetched_at=datetime.now(timezone.utc).isoformat())
        api_result = None
        try:
            if fresh is not None:
                fresh, api_result = fetch_public_project(fresh, source=source,
                    access_policy=access_policy, encrypted_storage_available=True)
        except Exception as exc:
            print(f"Authoritative verification failed for {gig.source_id}: {type(exc).__name__}: {exc}", flush=True)
            fresh = None
        checked = datetime.now(timezone.utc)
        verification = verify_gig(gig, fresh, now=checked)
        apply_verification(gig, verification, fresh)
        # Assess effort/rate against the current description; retain P5 formulas.
        gig = pipeline.process_gig(gig, existing=[])
        if gig.source_id in scope_reviews:
            review = scope_reviews[gig.source_id]
            if not review.get("rationale") or float(review.get("hours", 0)) <= 0:
                raise ValueError("reviewed effort needs positive hours and a scope rationale")
            gig.source_facts["p6_action_score_at_refresh"] = action_score_of(gig, now=checked).action_score
            gig.source_facts["p6_effort_review"] = dict(review, p5_estimate_hours=gig.estimated_effort_hours)
            gig.estimated_effort_hours = float(review["hours"])
            gig.effective_hourly_rate = effective_hourly_rate(gig, gig.estimated_effort_hours)
        analysis = analyze(gig.description, gig.title)
        cap = assess_capability(analysis, confidence, profile_skills=levels)
        risk = assess_client_risk(gig, now=checked)
        entries.append((gig, analysis, cap, risk))
        observations.append({"source_id": gig.source_id, "url": gig.url, "raw_live": fresh,
                             "api_result": api_result,
                             "verification": verification.to_dict(),
                             "analysis": analysis.to_dict(), "capability": cap.to_dict()})
        print(f"Verified {gig.source_id}: {cap.fit}, {gig.competition} bids, {gig.title}", flush=True)
    shortlist = build_shortlist(entries, now=datetime.now(timezone.utc))
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "fresh_collection_count": len(raw_records), "requests": source.requests_made,
               "collection_reused_from_this_p6_run": resume_collection,
               "api_data_retention_expires_at": expires_at,
               "api_verification_count": sum(o["api_result"] is not None for o in observations),
               "errors": source.last_fetch_errors, "counts": shortlist.counts(),
               "qa_status": "PENDING separate live review; not final APPLY_FIRST",
               "candidates": [e.to_dict() for e in shortlist.apply_first],
               "backup": [e.to_dict() for e in shortlist.backup],
               "skip": [e.to_dict() for e in shortlist.skip], "observations": observations}
    save_retained(dest / "prepared_run.dpapi.json", payload, expires_at=expires_at)
    print(json.dumps(shortlist.counts()), flush=True)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output", default="artifacts/p6")
    parser.add_argument("--resume-collection", action="store_true")
    parser.add_argument("--review-file")
    parser.add_argument("--access-policy", help="Written access, encrypted-storage, and retention evidence JSON")
    args = parser.parse_args()
    access_policy = json.loads(Path(args.access_policy).read_text(encoding='utf-8')) if args.access_policy else None
    prepare_run(limit=args.limit, output=args.output, resume_collection=args.resume_collection,
                review_file=args.review_file, access_policy=access_policy)
