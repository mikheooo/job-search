from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from .adapters.himalayas import HimalayasAdapter
from .adapters.weworkremotely import WeWorkRemotelyAdapter
from .adapters.remoteok import RemoteOkAdapter
from .adapters.habr_career import HabrCareerAdapter
from .schema import Vacancy
from .normalizer import normalize_vacancy
from .matcher import JobMatcher, JobProfile
from .candidate_profile import load_candidate_profile
from .prefill_execute import make_cdp_evaluate, make_isolated_world_evaluate
from . import prefill_execute
from . import hh_message_reply, email_message_reply, gmail_readonly_connector
from .db import (
    init_db,
    save_vacancy,
    get_vacancy_by_id,
    list_vacancies,
    get_deep_analysis,
    save_deep_analysis,
    get_application_package,
    save_application_package,
    get_submission,
    list_submissions,
    get_verification,
    list_verifications,
    _row_to_vacancy,
    list_undigested_vacancies,
    mark_digest_delivered,
    is_digest_delivered,
    record_digest_attempt,
    record_digest_failed,
    record_digest_ambiguous,
    list_digest_attempts,
    reconcile_digest_attempt,
    get_production_health,
)
from .config import BATCH_LIMIT, CANDIDATE_PROFILE_FILE
from .application_review import create_application_review, get_application_review, list_application_reviews, approve_review, reject_review, REVIEW_VERSION
from .application_tracking import (
    get_application_status as _get_app_status,
    list_applications as _list_apps,
    get_application_history as _get_app_history,
    transition_application as _transition_app,
    sync_application_tracking as _sync_tracking,
    verify_and_apply,
    ApplicationStatus,
)
from .submission_verifier import verify_submission as _verify_submission
from .application_dashboard import (
    build_dashboard,
    get_dashboard_show,
    get_dashboard_history,
    get_dashboard_queue,
    get_dashboard_actions_only,
    ApplicationDashboard,
    ActionType,
)
from .application_integrity import (
    run_integrity_audit,
    IntegrityReport,
    IntegritySeverity,
)
from .vacancy_identity import (
    resolve_vacancy_identity,
    sync_identity_from_vacancies,
    get_canonical_by_id,
    get_aliases_for_canonical,
    get_all_canonical_vacancies,
    normalize_url,
    normalize_company,
    normalize_title,
    MatchType,
)


SOURCES = {
    "himalayas": HimalayasAdapter(),
    "weworkremotely": WeWorkRemotelyAdapter(),
    "remoteok": RemoteOkAdapter(),
    "habrcareer": HabrCareerAdapter(),
}


def collect(sources: List[str]) -> int:
    init_db()
    adapters = [SOURCES[name] for name in sources if name in SOURCES]
    if not adapters:
        raise ValueError(f"Unknown sources: {sources}")

    stats = {"fetched": 0, "new": 0, "duplicate": 0, "failed": 0}

    for adapter in adapters:
        try:
            vacancies = adapter.fetch_vacancies()
        except Exception as e:
            logging.error("Failed to fetch from %s: %s", adapter.source, e)
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

    logging.info("Collect stats: %s", stats)
    return stats["new"]


def reclassify_eligibility_cmd(candidate_country: str = "TH", profile_path: str | None = None) -> int:
    """Reclassify all stored vacancies using the multi-dimensional Remote Eligibility Engine.
    
    1. Iterates over all stored vacancies in the database.
    2. Runs assess_vacancy_eligibility(vac, candidate_country=candidate_country).
    3. Saves structured assessment in vacancy_eligibility table.
    4. Removes INELIGIBLE and UNKNOWN items from active application queue.
    5. Preserves all vacancies (0 physical deletions).
    6. Is completely idempotent.
    """
    try:
        init_db()
    except Exception as e:
        print(f"Database initialization error: {e}", file=sys.stderr)
        return 1

    from .eligibility import assess_vacancy_eligibility, EligibilityStatus
    from .db import (
        list_vacancies,
        _row_to_vacancy,
        save_vacancy_eligibility,
        delete_queue_item,
        get_connection,
    )

    rows = list_vacancies(limit=50000)
    total = len(rows)
    if total == 0:
        print("No vacancies found in database.")
        return 0

    counts = {
        EligibilityStatus.ELIGIBLE.value: 0,
        EligibilityStatus.ELIGIBLE_WITH_WARNING.value: 0,
        EligibilityStatus.UNKNOWN.value: 0,
        EligibilityStatus.INELIGIBLE.value: 0,
    }

    # Count before in queue
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM application_queue")
    queue_before = cur.fetchone()[0]
    conn.close()

    for row in rows:
        vac = _row_to_vacancy(row)
        sid = vac.stable_id()
        assessment = assess_vacancy_eligibility(vac, candidate_country=candidate_country)
        save_vacancy_eligibility(sid, assessment)
        
        status_key = assessment.eligibility.value
        counts[status_key] = counts.get(status_key, 0) + 1

        # If INELIGIBLE or UNKNOWN, purge from active queue
        if assessment.eligibility in (EligibilityStatus.INELIGIBLE, EligibilityStatus.UNKNOWN):
            delete_queue_item(sid)

    # Count after in queue
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM application_queue")
    queue_after = cur.fetchone()[0]
    conn.close()
    
    removed_from_queue = max(0, queue_before - queue_after)

    print("\n=======================================================")
    print("   REMOTE ELIGIBILITY RECLASSIFICATION REPORT")
    print("=======================================================")
    print(f"Total vacancies assessed:           {total}")
    print(f"Candidate location:                 {candidate_country} (Thailand)")
    print("-------------------------------------------------------")
    print(f"  [+] ELIGIBLE:                     {counts.get('eligible', 0):4d} (Active in queue)")
    print(f"  [!] ELIGIBLE_WITH_WARNING:        {counts.get('eligible_with_warning', 0):4d} (Active with notes)")
    print(f"  [?] UNKNOWN (Requires Review):    {counts.get('unknown', 0):4d} (Excluded from active queue)")
    print(f"  [-] INELIGIBLE:                   {counts.get('ineligible', 0):4d} (Excluded from active queue)")
    print("-------------------------------------------------------")
    print(f"Active queue size before:           {queue_before}")
    print(f"Active queue size after:            {queue_after} (-{removed_from_queue} excluded)")
    print("Physical DB record deletions:       0 (100% historical retention)")
    print("=======================================================\n")

    return 0


def list_cmd(limit: int = 20, state: str | None = None, eligibility: str | None = None) -> int:
    """List stored vacancies with eligibility indicators (read-only).

    Fulfils the `python -m ai_assistant.cli list [--limit N] [--state S] [--eligibility E]`
    subcommand registered in `main()`. Reads vacancy rows from the DB via the
    existing `list_vacancies` query and renders them with `_row_to_vacancy`.
    Pure read: no fetch, no write, no send/submit.
    """
    try:
        init_db()
    except Exception as e:  # DB unavailable -> command fails, nothing mutated
        print(f"Failed to open vacancy DB: {e}", file=sys.stderr)
        return 1

    try:
        from .db import get_all_vacancy_eligibilities
        elig_map = get_all_vacancy_eligibilities()
    except Exception:
        elig_map = {}

    rows = list_vacancies(limit=50000 if eligibility else limit, state=state)
    vacancies = [_row_to_vacancy(row) for row in rows if row]
    if not vacancies:
        print("No stored vacancies found.", file=sys.stderr)
        return 0

    if eligibility and eligibility.lower() != "all":
        target = eligibility.lower().strip()
        vacancies = [v for v in vacancies if elig_map.get(v.stable_id(), {}).get("status", "unknown").lower() == target]
        vacancies = vacancies[:limit]

    print(f"{'SOURCE':12} | {'ELIGIBILITY':14} | {'COMPANY':25} | TITLE")
    print("-" * 110)
    for vac in vacancies:
        company = (vac.company or "")[:25]
        e_info = elig_map.get(vac.stable_id(), {})
        e_status = (e_info.get("status") or "UNKNOWN")[:14].upper()
        print(f"{vac.source:12} | {e_status:14} | {company:25} | {(vac.title or '')[:50]}")
        loc = vac.location or ""
        print(f"             | {'':14} | {loc[:25]:25} | {vac.job_url}")
    print(f"\n{len(vacancies)} vacancy(ies) listed (limit={limit}, state={state or 'all'}, eligibility={eligibility or 'all'}).")
    return 0


def analyze(top_n: int = 20, profile_path: str | None = None, persist: bool = False) -> None:
    init_db()
    rows = list_vacancies(limit=50000)
    vacancies = [_row_to_vacancy(row) for row in rows if row]

    if profile_path:
        profile = load_candidate_profile(profile_path)
    else:
        # try CANDIDATE_PROFILE_FILE from config, then default search
        cfg_path = CANDIDATE_PROFILE_FILE if CANDIDATE_PROFILE_FILE and CANDIDATE_PROFILE_FILE.strip() else None
        if cfg_path:
            try:
                profile = load_candidate_profile(cfg_path)
            except Exception as e:
                logging.warning("Failed to load profile from CANDIDATE_PROFILE_FILE %s: %s, using default", cfg_path, e)
                profile = load_candidate_profile()
        else:
            profile = load_candidate_profile()
    matcher = JobMatcher(profile)

    results = []
    for vacancy in vacancies:
        result = matcher.match(vacancy)
        results.append((result.score, result.decision, vacancy, result))
        if persist:
            # persist match results to DB
            try:
                import json
                import sqlite3
                from . import config
                conn = sqlite3.connect(config.DB_FILE)
                cur = conn.cursor()
                cur.execute(
                    "UPDATE vacancies SET match_score=?, match_decision=?, match_reasons=?, match_strengths=?, match_gaps=? WHERE stable_id=?",
                    (
                        result.score,
                        result.decision,
                        json.dumps(result.reasons, ensure_ascii=False),
                        json.dumps(result.strengths, ensure_ascii=False),
                        json.dumps(result.gaps, ensure_ascii=False),
                        vacancy.stable_id(),
                    ),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                logging.warning("Failed to persist match for %s: %s", vacancy.stable_id(), e)

    results.sort(key=lambda x: x[0], reverse=True)
    # header
    print(f"Analyzed {len(vacancies)} vacancies with profile: desired_roles={getattr(profile, 'desired_roles', [])[:3]}")
    print("=" * 120)
    for score, decision, vacancy, result in results[:top_n]:
        print(f"{score:>3} {decision:<7} {vacancy.title[:60]:60} | {vacancy.company[:25]:25} | {vacancy.source:15} | {vacancy.location or ''}")
        for reason in result.reasons[:3]:
            print(f"      - {reason}")
        if result.strengths:
            print(f"        strengths: {', '.join(result.strengths[:3])}")
        if result.gaps:
            print(f"        gaps: {', '.join(result.gaps[:3])}")
        print()
    return results  # for programmatic use


def analyze_deep(top_n: int = 20, profile_path: str | None = None, force: bool = False) -> None:
    import json as _json
    from .job_analyzer import ANALYZER_VERSION, analyze_job_deep, should_analyze, get_resume_text

    init_db()
    rows = list_vacancies(limit=50000)
    vacancies = [_row_to_vacancy(row) for row in rows if row]

    if profile_path:
        profile = load_candidate_profile(profile_path)
    else:
        cfg_path = CANDIDATE_PROFILE_FILE if CANDIDATE_PROFILE_FILE and CANDIDATE_PROFILE_FILE.strip() else None
        if cfg_path:
            try:
                profile = load_candidate_profile(cfg_path)
            except Exception as e:
                logging.warning("Failed to load profile %s: %s", cfg_path, e)
                profile = load_candidate_profile()
        else:
            profile = load_candidate_profile()
    matcher = JobMatcher(profile)
    resume_text = get_resume_text(profile)

    # matcher pass
    scored = []
    for vac in vacancies:
        m = matcher.match(vac)
        if should_analyze(m):
            scored.append((m.score, vac, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = scored[:top_n]

    print(f"[Deep] Analyzer version: {ANALYZER_VERSION} | Candidates APPLY/REVIEW: {len(scored)} | Top: {len(candidates)}")
    print("=" * 120)

    analyzed = 0
    skipped = 0
    for score, vac, match in candidates:
        sid = vac.stable_id()
        existing = get_deep_analysis(sid, ANALYZER_VERSION)
        if existing and not force:
            skipped += 1
            # load existing for report
            try:
                data = _json.loads(existing[4]) if existing[4] else {}
                deep_score = existing[2]
                rec = existing[3]
                print(f"{score:>3} {match.decision:<7} {vac.title[:55]:55} | {vac.company[:22]:22}")
                print(f"     Deep score: {deep_score}  Recommendation: {rec}  (cached {ANALYZER_VERSION})")
                # brief from cached
                if data.get("why_fit"):
                    print(f"     Strong: {', '.join(data.get('why_fit', [])[:3])}")
                if data.get("gaps"):
                    print(f"     Gaps: {', '.join(data.get('gaps', [])[:2])}")
                print()
            except Exception:
                print(f"{score:>3} {match.decision:<7} {vac.title[:55]} (cached, parse error)")
            continue

        # run LLM analysis
        try:
            deep = analyze_job_deep(vac, profile, match, resume_text=resume_text)
        except Exception as e:
            logging.error("Deep analysis failed for %s: %s", sid, e)
            continue

        # persist
        try:
            save_deep_analysis(
                vacancy_stable_id=sid,
                analyzer_version=ANALYZER_VERSION,
                fit_score=deep.fit_score,
                recommendation=deep.recommendation,
                analysis_json=deep.model_dump_json(),
                analyzed_at=None,
            )
        except Exception as e:
            logging.warning("Failed to save deep analysis %s: %s", sid, e)

        analyzed += 1
        # report
        print(f"{score:>3} {match.decision:<7} {vac.title[:55]:55} | {vac.company[:22]:22}")
        print(f"     {vac.job_url}")
        print(f"     Deep score: {deep.fit_score}")
        print(f"     Recommendation: {deep.recommendation}")
        print()
        if deep.why_fit:
            print("     Strong:")
            for s in deep.why_fit[:4]:
                print(f"       + {s}")
            print()
        if deep.gaps:
            print("     Gaps:")
            for g in deep.gaps[:4]:
                print(f"       - {g}")
            print()
        print(f"     Resume adaptation: {'YES' if deep.resume_adaptation_needed else 'NO'}")
        if deep.resume_adaptation_reasons:
            for r in deep.resume_adaptation_reasons[:2]:
                print(f"       * {r}")
        if deep.application_strategy:
            print(f"     Strategy: {deep.application_strategy}")
        print("-" * 120)

    print(f"\n[Deep] Done. Analyzed: {analyzed}, Skipped cached: {skipped}, Total candidates: {len(candidates)}")
    return {"analyzed": analyzed, "skipped": skipped, "candidates": len(candidates)}


def prepare_applications(top_n: int = 20, profile_path: str | None = None, force: bool = False) -> None:
    import json as _json
    from .job_analyzer import ANALYZER_VERSION, analyze_job_deep, should_analyze as deep_should
    from .job_analyzer import get_resume_text as deep_resume
    from .application_prep import APPLICATION_PREP_VERSION, prepare_application
    from .job_analyzer import DeepAnalysisResult

    init_db()
    rows = list_vacancies(limit=50000)
    vacancies = [_row_to_vacancy(row) for row in rows if row]

    if profile_path:
        profile = load_candidate_profile(profile_path)
    else:
        cfg_path = CANDIDATE_PROFILE_FILE if CANDIDATE_PROFILE_FILE and CANDIDATE_PROFILE_FILE.strip() else None
        if cfg_path:
            try:
                profile = load_candidate_profile(cfg_path)
            except Exception as e:
                logging.warning("Failed to load profile %s: %s", cfg_path, e)
                profile = load_candidate_profile()
        else:
            profile = load_candidate_profile()
    matcher = JobMatcher(profile)
    resume_text = deep_resume(profile)

    scored = []
    for vac in vacancies:
        m = matcher.match(vac)
        if deep_should(m):
            scored.append((m.score, vac, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = scored[:top_n]

    print(f"[Prep] Generator {APPLICATION_PREP_VERSION} | Deep {ANALYZER_VERSION} | Matcher candidates {len(scored)} | Top {len(candidates)}")
    print("=" * 120)

    prepared = 0
    skipped = 0
    deep_created = 0
    for score, vac, match in candidates:
        sid = vac.stable_id()
        # --- ensure deep analysis exists ---
        deep_row = get_deep_analysis(sid, ANALYZER_VERSION)
        if deep_row:
            try:
                deep = DeepAnalysisResult.model_validate_json(deep_row[4])
            except Exception as e:
                logging.warning("Failed to parse deep analysis %s: %s", sid, e)
                deep = None
        else:
            deep = None

        if deep is None:
            # run deep analysis (two-stage)
            try:
                deep = analyze_job_deep(vac, profile, match, resume_text=resume_text)
                save_deep_analysis(sid, ANALYZER_VERSION, deep.fit_score, deep.recommendation, deep.model_dump_json())
                deep_created += 1
            except Exception as e:
                logging.error("Deep analysis failed for prep %s: %s", sid, e)
                continue

        # filter deep recommendation
        if deep.recommendation not in ("APPLY", "REVIEW"):
            # skip package for SKIP
            print(f"{score:>3} {match.decision:<7} {vac.title[:50]:50} | {vac.company[:20]:20} -> Deep {deep.recommendation} SKIP package")
            continue

        # check application cache
        existing_app = get_application_package(sid, APPLICATION_PREP_VERSION)
        if existing_app and not force:
            skipped += 1
            try:
                data = _json.loads(existing_app[2]) if existing_app[2] else {}
                print(f"{score:>3} {match.decision:<7} {vac.title[:50]:50} | Deep {deep.recommendation} | Package cached {APPLICATION_PREP_VERSION}")
                print(f"     Cover: {data.get('cover_letter','')[:120]}...")
                print(f"     Skills: {', '.join(data.get('tailored_skills', [])[:4])}")
                if data.get("warnings"):
                    print(f"     Warnings: {', '.join(data.get('warnings', [])[:2])}")
                print()
            except Exception:
                print(f"{score:>3} {vac.title[:50]} (cached parse error)")
            continue

        # prepare package
        try:
            pkg = prepare_application(vac, deep, profile, resume_text=resume_text)
        except Exception as e:
            logging.error("Prepare failed for %s: %s", sid, e)
            continue
        if pkg is None:
            print(f"{score:>3} {vac.title[:50]} -> SKIP no package (deep {deep.recommendation})")
            continue

        # Stage 17D: extract HH form -> resolve answers -> validate package.
        # Read-only extraction; failure leaves the package NEEDS_REVIEW.
        try:
            from .application_qa import prepare_package_with_form
            pkg = prepare_package_with_form(
                pkg, sid, vac.job_url, profile, resume_text,
                deep=deep, vacancy=vac,
            )
        except Exception as e:
            logging.warning("Form extraction/validation failed for %s: %s", sid, e)
            pkg.validation_status = "NEEDS_REVIEW"
            pkg.review_reasons = list(pkg.review_reasons or []) + [f"Form extraction/validation failed: {e}"]

        try:
            save_application_package(sid, APPLICATION_PREP_VERSION, pkg.model_dump_json())
        except Exception as e:
            logging.warning("Failed to save package %s: %s", sid, e)

        prepared += 1
        print(f"{score:>3} {match.decision:<7} {vac.title[:50]:50} | {vac.company[:20]:20}")
        print(f"     Deep: {deep.fit_score} {deep.recommendation} | Prep {APPLICATION_PREP_VERSION}")
        print(f"     Target: {pkg.adaptation.target_title}")
        print(f"     Summary: {pkg.resume_summary[:140]}")
        print(f"     Skills: {', '.join(pkg.tailored_skills[:5])}")
        print(f"     Cover ({len(pkg.cover_letter.split())} words): {pkg.cover_letter[:160]}...")
        if pkg.form is not None:
            print(f"     Form: {pkg.application_type.value} | questions={len(pkg.form.questions)} | validation={pkg.validation_status}")
        if pkg.warnings:
            print(f"     Warnings: {'; '.join(pkg.warnings[:2])}")
        if pkg.review_reasons:
            print(f"     Review reasons: {'; '.join(pkg.review_reasons[:2])}")
        print(f"     URL: {vac.job_url}")
        print("-" * 120)

    print(f"\n[Prep] Done. Prepared: {prepared}, Skipped cached: {skipped}, Deep created: {deep_created}, Candidates: {len(candidates)}")
    return {"prepared": prepared, "skipped": skipped, " deep_created": deep_created}


def applications_list(limit: int = 50, status_filter: str | None = None) -> None:
    init_db()
    records = _list_apps(status=status_filter, limit=limit)
    # Header
    print(f"{'STATUS':15} | {'SCORE':9} | {'COMPANY':22} | TITLE")
    print("-" * 90)
    for r in records:
        score_str = f"{int(r.match_score) if r.match_score is not None else '-'}/{int(r.deep_score) if r.deep_score is not None else '-'}"
        print(f"{r.status.value if hasattr(r.status, 'value') else str(r.status):15} | {score_str:9} | {(r.company or '')[:22]:22} | {(r.title or '')[:50]}")

def applications_status(vacancy_stable_id: str) -> int:
    init_db()
    rec = _get_app_status(vacancy_stable_id)
    if not rec:
        print(f"No tracking record for {vacancy_stable_id}", file=sys.stderr)
        return 1
    print(f"Vacancy: {rec.vacancy_stable_id}")
    print(f"Title: {rec.title}")
    print(f"Company: {rec.company}")
    print(f"Source: {rec.source}")
    print(f"URL: {rec.vacancy_url}")
    print(f"Status: {rec.status.value if hasattr(rec.status, 'value') else rec.status}")
    print(f"Match score: {rec.match_score}")
    print(f"Deep score: {rec.deep_score}")
    print(f"Created: {rec.created_at}")
    print(f"Updated: {rec.updated_at}")
    print(f"Applied: {rec.applied_at}")
    print(f"Last change: {rec.last_status_change_at}")
    print(f"Notes: {rec.notes}")
    print("\nHistory:")
    hist = _get_app_history(vacancy_stable_id)
    if not hist:
        print("  (no history)")
    else:
        for h in hist:
            print(f"  {h.changed_at} {h.old_status or 'None'} -> {h.new_status} note={h.note or ''}")
    return 0

def applications_move(vacancy_stable_id: str, new_status: str, note: str | None = None) -> int:
    init_db()
    try:
        rec = _transition_app(vacancy_stable_id, new_status, note=note)
        print(f"Moved {vacancy_stable_id} to {rec.status.value if hasattr(rec.status, 'value') else rec.status}")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

def applications_sync(profile_path: str | None = None) -> int:
    init_db()
    result = _sync_tracking(profile_path=profile_path)
    print(f"Created: {result.get('Created', 0)}")
    print(f"Updated: {result.get('Updated', 0)}")
    print(f"Unchanged: {result.get('Unchanged', 0)}")
    return 0

def queue_list(top: int = 20, status_filter: str | None = None, profile_path: str | None = None) -> None:
    from .application_queue import generate_queue as _gen_queue, list_queue as _list_queue, QUEUE_VERSION
    init_db()
    # generate queue (includes sync and only READY_TO_APPLY)
    items = _gen_queue(top_n=top, profile_path=profile_path, status_filter=status_filter or "READY_TO_APPLY")
    # if status filter is provided, filter already; else generate already filtered
    # For display, use persisted list sorted by rank
    if not items:
        # try loading existing persisted
        items = _list_queue(limit=top, queue_version=QUEUE_VERSION)
    print(f"[Queue] v{QUEUE_VERSION} | Top {len(items)} | Status {status_filter or 'READY_TO_APPLY'}")
    print(f"{'RANK':4} | {'PRIO':6} | {'MATCH':5} | {'DEEP':4} | {'COMPANY':22} | TITLE")
    print("-" * 110)
    for it in items[:top]:
        print(f"{it.rank:4} | {it.priority_score:6} | {int(it.match_score) if it.match_score is not None else '-':5} | {int(it.deep_score) if it.deep_score is not None else '-':4} | {(it.company or '')[:22]:22} | {(it.title or '')[:45]}")

def queue_show(vacancy_stable_id: str) -> int:
    from .application_queue import get_queue_item, QUEUE_VERSION
    init_db()
    item = get_queue_item(vacancy_stable_id, queue_version=QUEUE_VERSION)
    if not item:
        # try without version
        item = get_queue_item(vacancy_stable_id)
    if not item:
        print(f"No queue item for {vacancy_stable_id}", file=sys.stderr)
        return 1
    print(f"Vacancy: {item.vacancy_stable_id}")
    print(f"Title: {item.title}")
    print(f"Company: {item.company}")
    print(f"Rank: {item.rank}  Priority: {item.priority_score}")
    print(f"Match: {item.match_score}  Deep: {item.deep_score}")
    print(f"URL: {item.vacancy_url}")
    print(f"Generated: {item.generated_at}  Version: {item.queue_version}")
    print("\nReasons:")
    for r in item.reasons:
        print(f"  + {r}")
    print("\nWarnings:")
    for w in item.warnings:
        print(f"  - {w}")
    if item.application_strategy:
        print(f"\nStrategy: {item.application_strategy}")
    if item.components:
        print("\nComponents:")
        for k, v in item.components.items():
            print(f"  {k}: {v}")
    return 0

def browser_prepare(vacancy_stable_id: str, force: bool = False) -> int:
    from .browser_executor import prepare_application_in_browser
    try:
        result = prepare_application_in_browser(vacancy_stable_id, force=force)
        from .db import get_vacancy_by_id
        from .application_tracking import get_application_status
        row = get_vacancy_by_id(vacancy_stable_id)
        from .db import _row_to_vacancy
        vac = _row_to_vacancy(row) if row else None
        track = get_application_status(vacancy_stable_id)
        print(f"Vacancy: {vac.title if vac else vacancy_stable_id}")
        print(f"Company: {vac.company if vac else ''}")
        print(f"URL: {result.url}")
        print(f"Status: {track.status.value if track and hasattr(track.status, 'value') else (track.status if track else 'UNKNOWN')}")
        print()
        print("Browser:")
        print(f"Site: {result.site}")
        print(f"Final URL: {result.final_url}")
        print(f"Page title: {result.page_title}")
        print()
        print("Application:")
        apply_found = getattr(result, 'apply_button_found', False) or any('Apply button FOUND' in w for w in result.warnings)
        print(f"Apply button: {'FOUND' if apply_found else 'NOT FOUND'}")
        print(f"Form: {'FOUND' if result.form_detected else 'NOT FOUND'}")
        print(f"Fields: {len(result.fields_detected)}")
        print()
        print("Filled:")
        for f in result.fields_filled:
            print(f"- {f}")
        if not result.fields_filled:
            print("- (none)")
        print()
        print("Skipped:")
        for f in result.fields_skipped:
            print(f"- {f}")
        if not result.fields_skipped:
            print("- (none)")
        print()
        print("Warnings:")
        for w in result.warnings:
            print(f"- {w}")
        if not result.warnings:
            print("- (none)")
        print()
        # Normalize status for display
        status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
        if status_val in ["READY_FOR_REVIEW", "COMPLETED"]:
            print("Application ready for review.")
            print()
            print("Action:")
            print("PREPARED - no submission performed")
            print("Manual submission required.")
        elif status_val=="BLOCKED":
            print("Action:")
            print("BLOCKED - application form not found")
        else:
            print(f"Action: {status_val}")
        print()
        print("SUBMIT NOT CLICKED")
        print("APPLICATION NOT SENT")
        print(f"STATUS NOT CHANGED TO APPLIED (current: {track.status.value if track else 'unknown'})")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        return 1

def browser_prepare_next(top: int = 20) -> int:
    from .browser_executor import prepare_next_in_queue
    try:
        result = prepare_next_in_queue(top_n=top)
        if not result:
            print("No READY_TO_APPLY vacancy found for browser preparation", file=__import__('sys').stderr)
            return 1
        return browser_prepare(result.vacancy_stable_id)
    except Exception as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        return 1

def review_show(vacancy_stable_id: str) -> int:
    from .application_review import create_application_review, get_application_review
    try:
        # Try to get existing, else create
        rev = get_application_review(vacancy_stable_id)
        if not rev:
            rev = create_application_review(vacancy_stable_id)
        # Display
        print("=== APPLICATION REVIEW ===")
        print()
        print(f"Company: {rev.company}")
        print(f"Title: {rev.title}")
        print(f"Source: {rev.source}")
        print(f"URL: {rev.vacancy_url}")
        print()
        print(f"Match: {rev.match_score}")
        print(f"Deep: {rev.deep_score}")
        print(f"Priority: {rev.priority_score}")
        print(f"Queue rank: {rev.rank}")
        print()
        print("Application strategy:")
        print(rev.application_strategy or "(none)")
        print()
        print("Resume summary:")
        print(rev.resume_summary or "(none)")
        print()
        print("Tailored skills:")
        for s in rev.tailored_skills:
            print(f"- {s}")
        if not rev.tailored_skills:
            print("- (none)")
        print()
        print("Relevant experience:")
        for e in rev.relevant_experience:
            print(f"- {e}")
        if not rev.relevant_experience:
            print("- (none)")
        print()
        print("Cover letter:")
        print(rev.cover_letter or "(none)")
        print()
        print("Fields to fill:")
        for f in rev.fields_filled:
            print(f"- {f}")
        if not rev.fields_filled:
            print("- (none)")
        print()
        print("Fields skipped:")
        for f in rev.fields_skipped:
            w = next((w for w in rev.warnings if f in w), "not confirmed")
            print(f"- {f} — {w}")
        if not rev.fields_skipped:
            print("- (none)")
        print()
        print("Warnings:")
        for w in rev.warnings:
            print(f"- {w}")
        if not rev.warnings:
            print("- (none)")
        print()
        print(f"Screenshot: {rev.screenshot_path or '(none)'}")
        print()
        print(f"Status: {rev.status.value if hasattr(rev.status, 'value') else rev.status}")
        print()
        print("IMPORTANT:")
        print("APPLICATION WILL NOT BE SUBMITTED.")
        print("HUMAN REVIEW REQUIRED.")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        return 1

def review_list(limit: int = 50, status_filter: str | None = None) -> None:
    from .application_review import list_application_reviews
    recs = list_application_reviews(status=status_filter, limit=limit)
    print(f"{'STATUS':15} | {'RANK':4} | {'PRIORITY':8} | {'MATCH':5} | {'DEEP':4} | {'COMPANY':20} | TITLE")
    print("-" * 110)
    for r in recs:
        print(f"{r.status.value if hasattr(r.status,'value') else r.status:15} | {str(r.rank) if r.rank is not None else '-':4} | {str(int(r.priority_score)) if r.priority_score is not None else '-':8} | {str(int(r.match_score)) if r.match_score is not None else '-':5} | {str(int(r.deep_score)) if r.deep_score is not None else '-':4} | {(r.company or '')[:20]:20} | {(r.title or '')[:40]}")

def review_approve(vacancy_stable_id: str) -> int:
    from .application_review import approve_review
    try:
        rev = approve_review(vacancy_stable_id)
        print(f"Approved {vacancy_stable_id} -> {rev.status.value}")
        print("Review status: APPROVED")
        print("APPLICATION WILL NOT BE SUBMITTED AUTOMATICALLY.")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        return 1

def review_reject(vacancy_stable_id: str, note: str | None = None) -> int:
    from .application_review import reject_review
    try:
        rev = reject_review(vacancy_stable_id, note=note)
        print(f"Rejected {vacancy_stable_id} -> {rev.status.value} note={note or ''}")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        return 1

def review_reject(vacancy_stable_id: str, note: str | None = None) -> int:
    from .application_review import reject_review
    try:
        rev = reject_review(vacancy_stable_id, note=note)
        print(f"Rejected {vacancy_stable_id} -> {rev.status.value} note={note or ''}")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        return 1


def submit_vacancy(vacancy_stable_id: str, confirm_submit: bool = False, force: bool = False, profile_path: str | None = None) -> int:
    """Submit a single vacancy application."""
    if not confirm_submit:
        print("Submit confirmation required. Use --confirm-submit to proceed.")
        print("No browser action performed.")
        return 1
    from .browser_executor import submit_application_in_browser
    try:
        result = submit_application_in_browser(vacancy_stable_id, confirm_submit=True, force=force, profile_path=None)
        if result.status == "SUBMITTED":
            print(f"SUBMISSION: SUBMITTED")
            print(f"Vacancy: {result.vacancy_stable_id}")
            print(f"Final URL: {result.final_url}")
            print(f"Application submitted successfully.")
            print(f"Tracking: APPLIED")
            print()
            print("Safety:")
            print("SUBMIT CLICKED: YES")
            print("APPLICATION SENT: YES")
            return 0
        elif result.status == "BLOCKED":
            print(f"SUBMISSION BLOCKED: {result.error}")
            print("SUBMIT CLICKED: NO")
            print("APPLICATION NOT SENT")
            print("STATUS NOT CHANGED TO APPLIED")
            return 1
        elif result.status == "FAILED":
            print(f"SUBMISSION FAILED: {result.error}")
            print("SUBMIT CLICKED: YES")
            print("APPLICATION SENT: UNKNOWN")
            print("STATUS NOT CHANGED TO APPLIED")
            return 1
        elif result.status == "AMBIGUOUS":
            print(f"SUBMISSION AMBIGUOUS: {result.error}")
            print("SUBMIT CLICKED: YES")
            print("APPLICATION SENT: UNKNOWN")
            print("STATUS NOT CHANGED TO APPLIED")
            return 1
        elif result.status == "BLOCKED":
            print(f"SUBMISSION BLOCKED: {result.error}")
            print("SUBMIT CLICKED: NO")
            print("APPLICATION NOT SENT")
            print("STATUS NOT CHANGED TO APPLIED")
            return 1
        else:
            print(f"Submission status: {result.status}")
            print("SUBMIT NOT CLICKED")
            print("APPLICATION NOT SENT")
            print("STATUS NOT CHANGED TO APPLIED")
            return 1
    except ValueError as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=__import__('sys').stderr)
        return 1
    """Run full integrity audit."""
    init_db()
    items = generate_queue(top_n=top)
    for item in items:
        sid = item.vacancy_stable_id
        # Check if already blocked/completed? Skip if already has browser session with BLOCKED and not force?
        # For now, try each in rank order
        try:
            # Check if already prepared and not blocked? If blocked, try next
            existing = get_browser_session(sid, "v1")
            if existing and existing.status in (BrowserStatus.READY_FOR_REVIEW, BrowserStatus.COMPLETED):
                continue
            if existing and existing.status == BrowserStatus.BLOCKED:
                continue
            return submit_vacancy(sid, confirm_submit=True, force=False, profile_path=None)
        except Exception as e:
            logging.warning(f"submit_next failed for {sid}: {e}")
            continue
    print("No READY_TO_APPLY vacancy found for browser preparation", file=__import__('sys').stderr)
    return 1


def submissions_list(limit: int = 50) -> None:
    """List all submissions with verification status."""
    init_db()
    submissions = list_submissions(limit=limit)
    verifications = list_verifications(limit=limit)
    
    # Build verification lookup
    ver_lookup = {}
    for v in verifications:
        key = (v.vacancy_stable_id, v.submission_id)
        ver_lookup[key] = v
    
    # Header
    print(f"{'STATUS':15} | {'VERIFICATION':12} | {'COMPANY':22} | {'TITLE':45} | {'SUBMITTED_AT'}")
    print("-" * 120)
    
    for sub in submissions:
        try:
            import json
            # New schema: 0=vacancy_stable_id, 1=submission_id, 2=executor_version, 3=submission_json, 4=status, 5=submitted_at
            sub_id = sub[1]
            vacancy_stable_id = sub[0]
            status = sub[4]
            submitted_at = sub[5] or ""
            
            # Get company and title from vacancy
            from .db import get_vacancy_by_id
            from .db import _row_to_vacancy
            row = get_vacancy_by_id(vacancy_stable_id)
            company = ""
            title = ""
            if row:
                vac = _row_to_vacancy(row)
                company = vac.company or ""
                title = vac.title or ""
            
            # Get verification status
            key = (vacancy_stable_id, sub_id)
            ver = ver_lookup.get(key)
            ver_status = ver.verification_status.value if ver else "PENDING"
            
            print(f"{status:15} | {ver_status:12} | {company[:22]:22} | {title[:45]:45} | {submitted_at[:19]}")
        except Exception as e:
            print(f"Error displaying submission {sub[0]}: {e}")


def submissions_show(vacancy_stable_id: str) -> int:
    """Show detailed submission and verification info."""
    import json
    init_db()
    
    # Get submission
    sub_row = get_submission(vacancy_stable_id)
    if not sub_row:
        print(f"No submission found for {vacancy_stable_id}", file=__import__('sys').stderr)
        return 1
    
    # New schema: 0=vacancy_stable_id, 1=submission_id, 2=executor_version, 3=submission_json, 4=status, 5=submitted_at, 6=created_at, 7=updated_at
    sub_data = json.loads(sub_row[3]) if sub_row[3] else {}
    sub_id = sub_row[1]
    status = sub_row[4]
    submitted_at = sub_row[5]
    created_at = sub_row[6]
    updated_at = sub_row[7]
    executor_version = sub_row[2]
    
    # Get vacancy info
    from .db import get_vacancy_by_id
    from .db import _row_to_vacancy
    row = get_vacancy_by_id(vacancy_stable_id)
    if row:
        vac = _row_to_vacancy(row)
        print(f"Vacancy: {vacancy_stable_id}")
        print(f"Title: {vac.title}")
        print(f"Company: {vac.company}")
        print(f"Source: {vac.source}")
        print(f"URL: {vac.job_url}")
        print()
    
    print(f"Submission ID: {sub_id}")
    print(f"Submission Status: {status}")
    print(f"Submitted At: {submitted_at}")
    print(f"Created At: {created_at}")
    print(f"Updated At: {updated_at}")
    print(f"Executor Version: {executor_version}")
    print()
    
    # Get verification
    ver = get_verification(vacancy_stable_id, sub_id)
    if ver:
        print("=== VERIFICATION ===")
        print(f"Verification Status: {ver.verification_status.value}")
        print(f"Verification Version: {ver.verification_version}")
        print(f"Verified At: {ver.verified_at}")
        print(f"Final URL: {ver.final_url}")
        print(f"Page Title: {ver.page_title}")
        print(f"Success Signal: {ver.success_signal}")
        print(f"Screenshot: {ver.screenshot_path}")
        print()
        print("Evidence:")
        for k, v in ver.evidence.items():
            print(f"  {k}: {v}")
        print()
        print("Warnings:")
        for w in ver.warnings:
            print(f"  - {w}")
        if not ver.warnings:
            print("  (none)")
        print()
        
        # Get tracking status
        track = _get_app_status(vacancy_stable_id)
        if track:
            print(f"Tracking Status: {track.status.value if hasattr(track.status, 'value') else track.status}")
    else:
        print("=== VERIFICATION ===")
        print("No verification performed yet.")
        print()
        print("Run: python -m ai_assistant.cli submissions verify <vacancy_stable_id>")
    
    return 0


def submissions_verify(vacancy_stable_id: str) -> int:
    """Verify a submission - checks the page for success/error signals. Does NOT re-submit."""
    init_db()
    
    # Get submission
    sub_row = get_submission(vacancy_stable_id)
    if not sub_row:
        print(f"No submission found for {vacancy_stable_id}", file=__import__('sys').stderr)
        return 1
    
    import json
    sub_data = json.loads(sub_row[1]) if sub_row[1] else {}
    sub_id = sub_data.get("submission_id", sub_row[0])
    
    print(f"Verifying submission {sub_id} for {vacancy_stable_id}...")
    print("NOTE: This only checks the current page state, does NOT re-submit.")
    print()
    
    ver = _verify_submission(vacancy_stable_id, sub_id)
    
    print(f"Verification Status: {ver.verification_status.value}")
    print(f"Verified At: {ver.verified_at}")
    print(f"Final URL: {ver.final_url}")
    print(f"Page Title: {ver.page_title}")
    print(f"Success Signal: {ver.success_signal}")
    print(f"Screenshot: {ver.screenshot_path}")
    print()
    
    if ver.warnings:
        print("Warnings:")
        for w in ver.warnings:
            print(f"  - {w}")
    else:
        print("Warnings: (none)")
    print()
    
    # Update tracking based on verification
    if ver.verification_status.value == "VERIFIED":
        print("Verification SUCCESSFUL - transitioning tracking: SUBMITTED -> VERIFIED -> APPLIED")
        try:
            verify_and_apply(vacancy_stable_id, "VERIFIED", note=f"Verified: {ver.success_signal}")
            print("Tracking updated to APPLIED")
        except Exception as e:
            print(f"Warning: Could not update tracking: {e}")
    elif ver.verification_status.value in ("FAILED", "AMBIGUOUS", "BLOCKED"):
        print(f"Verification {ver.verification_status.value} - tracking will NOT be moved to APPLIED")
        try:
            verify_and_apply(vacancy_stable_id, ver.verification_status.value, note=f"Verification: {ver.verification_status.value}")
            print(f"Tracking updated to READY_TO_APPLY for retry")
        except Exception as e:
            print(f"Warning: Could not update tracking: {e}")
    
    return 0


def submissions_recover(vacancy_stable_id: str) -> int:
    """Inspect submission state and recommend action (read-only, never submits)."""
    from .submission_recovery import inspect_submission_state, RecoveryStatus
    init_db()
    
    result = inspect_submission_state(vacancy_stable_id)
    
    # Get vacancy info
    from .db import get_vacancy_by_id
    from .db import _row_to_vacancy
    row = get_vacancy_by_id(vacancy_stable_id)
    if row:
        vac = _row_to_vacancy(row)
        print(f"Vacancy: {vacancy_stable_id}")
        print(f"Title: {vac.title}")
        print(f"Company: {vac.company}")
        print()
    
    print(f"Tracking: {result.current_tracking_status or 'NONE'}")
    print()
    
    print("Last submission:")
    if result.last_submission:
        print(f"  submission_id: {result.last_submission.get('submission_id')}")
        print(f"  submitted_at: {result.last_submission.get('submitted_at')}")
        print(f"  status: {result.last_submission.get('status')}")
    else:
        print("  (none)")
    print()
    
    print("Last verification:")
    if result.last_verification:
        print(f"  status: {result.last_verification.get('verification_status')}")
        print(f"  verified_at: {result.last_verification.get('verified_at')}")
        print(f"  success_signal: {result.last_verification.get('success_signal')}")
        print(f"  final_url: {result.last_verification.get('final_url')}")
        print(f"  page_title: {result.last_verification.get('page_title')}")
    else:
        print("  (none)")
    print()
    
    print(f"Recovery: {result.recovery_status.value}")
    print()
    print(f"Reason: {result.reason}")
    print()
    print(f"Recommended action: {result.recommended_action}")
    
    if result.warnings:
        print()
        print("Warnings:")
        for w in result.warnings:
            print(f"  - {w}")
    
    return 0


def submissions_reconcile(vacancy_stable_id: str) -> int:
    """Reconcile tracking with verified state (only VERIFIED -> APPLIED)."""
    from .submission_recovery import reconcile_submission_state
    init_db()
    
    result = reconcile_submission_state(vacancy_stable_id)
    
    print(f"Vacancy: {vacancy_stable_id}")
    print(f"Tracking before: {result.current_tracking_status}")
    print()
    
    if result.last_verification:
        print(f"Last verification: {result.last_verification.get('verification_status')}")
        print()
    
    if result.current_tracking_status == "APPLIED":
        print("Already APPLIED - no action needed")
    elif result.recovery_status.value == "NO_ACTION" and result.last_verification and result.last_verification.get('verification_status') == "VERIFIED":
        print("Reconciled: VERIFIED -> APPLIED")
        print(f"Tracking now: APPLIED")
    else:
        print(f"No reconciliation performed. Recovery status: {result.recovery_status.value}")
        print(f"Reason: {result.reason}")
    
    return 0


def submissions_audit(vacancy_stable_id: str) -> int:
    """Show full chronological audit trail."""
    from .submission_recovery import get_submission_audit
    init_db()
    
    # Get vacancy info
    from .db import get_vacancy_by_id
    from .db import _row_to_vacancy
    row = get_vacancy_by_id(vacancy_stable_id)
    if row:
        vac = _row_to_vacancy(row)
        print(f"Vacancy: {vacancy_stable_id}")
        print(f"Title: {vac.title}")
        print(f"Company: {vac.company}")
        print(f"URL: {vac.job_url}")
        print()
    
    events = get_submission_audit(vacancy_stable_id)
    
    if not events:
        print("No audit events found.")
        return 0
    
    print("=== CHRONOLOGICAL AUDIT TRAIL ===")
    print()
    
    for event in events:
        ts = event.get("timestamp", "unknown")
        etype = event.get("type", "UNKNOWN")
        status = event.get("status", "")
        detail = event.get("detail", "")
        
        print(f"[{ts}] {etype}")
        if status:
            print(f"  Status: {status}")
        if detail:
            print(f"  Detail: {detail}")
        print()
    
    return 0


def dashboard() -> int:
    """Show full application dashboard."""
    init_db()
    dash = build_dashboard()
    
    print("=== APPLICATION DASHBOARD ===")
    print()
    print(f"Generated: {dash.generated_at}")
    print()
    print("Vacancies:")
    print(f"  Total: {dash.total_vacancies}")
    print()
    print("Pipeline:")
    print(f"  DISCOVERED       {dash.discovered:3d}")
    print(f"  ANALYZED          {dash.analyzed:3d}")
    print(f"  READY_TO_APPLY   {dash.ready_to_apply:3d}")
    print(f"  PENDING_REVIEW    {dash.pending_review:3d}")
    print(f"  APPROVED          {dash.approved:3d}")
    print(f"  SUBMITTED         {dash.submitted:3d}")
    print(f"  VERIFIED          {dash.verified:3d}")
    print(f"  APPLIED           {dash.applied:3d}")
    print(f"  REJECTED          {dash.rejected:3d}")
    print(f"  INTERVIEW         {dash.interview:3d}")
    print(f"  OFFER             {dash.offer:3d}")
    print(f"  WITHDRAWN         {dash.withdrawn:3d}")
    print()
    print("Queue:")
    print(f"  READY: {dash.queue_size:3d}")
    print(f"  Top priority: {dash.top_priority:3d}")
    print(f"  Average match: {dash.average_match:.0f}")
    print(f"  Average deep: {dash.average_deep:.0f}")
    print()
    print("Verification:")
    print(f"  BLOCKED    {dash.blocked:3d}")
    print(f"  AMBIGUOUS  {dash.ambiguous:3d}")
    print(f"  FAILED     {dash.failed:3d}")
    print()
    if dash.action_items:
        print("ACTION REQUIRED:")
        for i, item in enumerate(dash.action_items, 1):
            print(f"  {i}. {item.action.value}")
            print(f"     {item.company} — {item.title}")
            print(f"     Priority: {item.priority}")
            print(f"     Match: {item.match_score or 'N/A'}")
            print(f"     Deep: {item.deep_score or 'N/A'}")
            print(f"     Reason: {item.reason}")
            print()
    else:
        print("ACTION REQUIRED: (none)")
    return 0


def dashboard_actions() -> int:
    """Show only action items."""
    init_db()
    actions = get_dashboard_actions_only()
    
    if not actions:
        print("No actions required.")
        return 0
    
    print("ACTION REQUIRED:")
    for i, item in enumerate(actions, 1):
        print(f"  {i}. {item.action.value}")
        print(f"     {item.company} — {item.title}")
        print(f"     Current status: {item.current_status}")
        print(f"     Priority: {item.priority}")
        print(f"     Match: {item.match_score or 'N/A'}")
        print(f"     Deep: {item.deep_score or 'N/A'}")
        print(f"     Reason: {item.reason}")
        print()
    return 0


def dashboard_queue(limit: int = 50) -> int:
    """Show queue summary."""
    init_db()
    queue = get_dashboard_queue()[:limit]
    
    print(f"QUEUE (Top {len(queue)})")
    print(f"{'RANK':4} | {'PRIO':6} | {'MATCH':5} | {'DEEP':4} | {'COMPANY':22} | TITLE")
    print("-" * 110)
    for q in queue:
        match_str = str(int(q.match_score)) if q.match_score is not None else "-"
        deep_str = str(int(q.deep_score)) if q.deep_score is not None else "-"
        print(f"{q.rank:4} | {q.priority_score:6} | {match_str:5} | {deep_str:4} | {q.company[:22]:22} | {q.title[:45]}")
    return 0


def dashboard_history(limit: int = 50) -> int:
    """Show recent lifecycle events."""
    init_db()
    events = get_dashboard_history(limit)
    
    print(f"LIFECYCLE HISTORY (Last {len(events)})")
    print(f"{'TIME':25} | {'VACANCY':30} | {'OLD':15} -> {'NEW':15} | NOTE")
    print("-" * 120)
    for e in events:
        ts = e.get("changed_at", "")[:25]
        vac = e.get("vacancy_stable_id", "")[:30]
        old = e.get("old_status", "")[:15]
        new = e.get("new_status", "")[:15]
        note = e.get("note", "")[:40]
        print(f"{ts:25} | {vac:30} | {old:15} -> {new:15} | {note}")
    return 0


def dashboard_show(vacancy_stable_id: str) -> int:
    """Show detailed view for a single vacancy."""
    init_db()
    detail = get_dashboard_show(vacancy_stable_id)
    if not detail:
        print(f"No data found for {vacancy_stable_id}", file=__import__('sys').stderr)
        return 1
    
    print("=== APPLICATION ===")
    print()
    print(f"Company: {detail.get('company', '')}")
    print(f"Title: {detail.get('title', '')}")
    print(f"URL: {detail.get('job_url', '')}")
    print()
    
    # Match
    match_score = detail.get('match_score')
    match_decision = detail.get('match_decision')
    if match_score is not None:
        print("MATCH")
        print(f"  score: {match_score}")
        print(f"  decision: {match_decision or 'N/A'}")
        print()
    
    # Deep Analysis
    deep = detail.get('deep_analysis')
    if deep:
        print("DEEP ANALYSIS")
        print(f"  score: {deep.get('fit_score', 'N/A')}")
        print(f"  recommendation: {deep.get('recommendation', 'N/A')}")
        print(f"  analyzed_at: {deep.get('analyzed_at', 'N/A')}")
        print()
    
    # Queue
    queue = detail.get('queue')
    if queue:
        print("QUEUE")
        print(f"  rank: {queue.get('rank', 'N/A')}")
        print(f"  priority: {queue.get('priority_score', 'N/A')}")
        print()
    
    # Application Package
    pkg = detail.get('application_package')
    if pkg:
        print("APPLICATION PACKAGE")
        print(f"  prepared: {pkg.get('prepared', 'N/A')}")
        print(f"  resume adaptation: {pkg.get('resume_adaptation', 'N/A')[:80] if pkg.get('resume_adaptation') else 'N/A'}")
        print(f"  cover letter: {pkg.get('cover_letter', 'N/A')[:80] if pkg.get('cover_letter') else 'N/A'}")
        print()
    
    # Browser
    browser = detail.get('browser')
    if browser:
        print("BROWSER")
        print(f"  status: {browser.get('status', 'N/A')}")
        print(f"  form: {'FOUND' if browser.get('form_detected') else 'NOT FOUND'}")
        print(f"  screenshot: {browser.get('screenshot_path', 'N/A')}")
        print()
    
    # Review
    review = detail.get('review')
    if review:
        print("REVIEW")
        print(f"  status: {review.get('status', 'N/A')}")
        print(f"  note: {review.get('note', 'N/A')}")
        print()
    
    # Submissions
    subs = detail.get('submissions')
    if subs:
        print("SUBMISSIONS")
        print(f"  attempts: {detail.get('submissions_count', 0)}")
        last = detail.get('last_submission')
        if last:
            print(f"  last submission: {last.get('submission_id')}")
            print(f"  status: {last.get('status', 'N/A')}")
            print(f"  submitted_at: {last.get('submitted_at', 'N/A')}")
        print()
    
    # Verification
    ver = detail.get('verification')
    if ver:
        print("VERIFICATION")
        print(f"  status: {ver.get('status', 'N/A')}")
        print(f"  success signal: {ver.get('success_signal', 'N/A')}")
        print(f"  final url: {ver.get('final_url', 'N/A')}")
        print(f"  page title: {ver.get('page_title', 'N/A')}")
        print()
    
    # Tracking
    track = detail.get('tracking')
    if track:
        print("TRACKING")
        print(f"  current status: {track.get('current_status', 'N/A')}")
        print(f"  applied_at: {track.get('applied_at', 'N/A')}")
        print(f"  verified_at: {track.get('verified_at', 'N/A')}")
        print()
    
    # Timeline
    timeline = detail.get('timeline')
    if timeline:
        print("TIMELINE")
        for h in timeline:
            print(f"  {h.get('changed_at', '')}  {h.get('old_status', 'NONE')} -> {h.get('new_status', 'N/A')}  {h.get('note', '')}")
        print()
    
    # Action
    action = detail.get('action')
    if action:
        print("ACTION")
        print(f"  {action.get('action', 'N/A')}")
        print(f"  Reason: {action.get('reason', 'N/A')}")
        print()
    
    return 0


def dashboard_show_canonical(canonical_id: str) -> int:
    """Show detailed view for a canonical vacancy."""
    init_db()
    from .vacancy_identity import get_canonical_by_id, get_aliases_for_canonical
    from .application_queue import get_queue_item, list_queue
    from .application_tracking import get_application_status
    
    canon = get_canonical_by_id(canonical_id)
    if not canon:
        print(f"No canonical vacancy found for {canonical_id}", file=__import__('sys').stderr)
        return 1
    
    print("=== CANONICAL QUEUE INFO ===")
    print()
    print(f"Canonical ID: {canonical_id}")
    print(f"Company: {canon.normalized_company}")
    print(f"Title: {canon.normalized_title}")
    print(f"Normalized URL: {canon.normalized_url}")
    print(f"Location: {canon.location or 'N/A'}")
    print()
    
    # Show aliases
    aliases = get_aliases_for_canonical(canonical_id)
    print(f"Aliases ({len(aliases)}):")
    for alias in aliases:
        print(f"  {alias['vacancy_stable_id']} ({alias['source']}) - {alias['match_type']} ({alias['confidence']}%)")
    print()
    
    # Show queue status for each alias
    print("Queue Status:")
    for alias in aliases:
        sid = alias['vacancy_stable_id']
        queue_item = get_queue_item(sid, "v2")
        track = get_application_status(sid)
        track_status = track.status.value if track and hasattr(track.status, 'value') else (str(track.status) if track else 'NONE')
        print(f"  {sid} ({alias['source']})")
        print(f"    Tracking: {track_status}")
        if queue_item:
            print(f"    Queue: Rank {queue_item.rank}, Priority {queue_item.priority_score}")
        else:
            print(f"    Queue: NOT IN QUEUE")
    print()
    
    # Show canonical queue item if exists
    # Check if any alias is in queue
    for alias in aliases:
        queue_item = get_queue_item(alias['vacancy_stable_id'], "v2")
        if queue_item:
            print("Canonical Queue Item:")
            print(f"  Rank: {queue_item.rank}")
            print(f"  Priority: {queue_item.priority_score}")
            print(f"  Representative: {queue_item.representative_vacancy_stable_id}")
            print(f"  Match: {queue_item.match_score}")
            print(f"  Deep: {queue_item.deep_score}")
            break
    
    return 0
    from .schema import Vacancy
    return Vacancy(
        source=row[1],
        source_job_id=row[2],
        title=row[3],
        company=row[4] or "",
        description=row[5] or "",
        job_url=row[13],
        application_url=row[14],
        location=row[6],
        country_restrictions=[x.strip() for x in (row[7] or "").split(",") if x.strip()],
        timezone_restrictions=[x.strip() for x in (row[8] or "").split(",") if x.strip()],
        salary_min=row[9],
        salary_max=row[10],
        salary_currency=row[11],
        employment_type=row[12],
        published_at=row[15],
        first_seen_at=row[16],
        last_seen_at=row[17],
        raw_data=row[19] or {},
    )


# ---------------------------------------------------------------------------
# Stage 30C Phase 1: REVIEW/READ-ONLY CLI wiring for Stage 21-29 modules.
# These handlers MUST NOT send/submit anything. AUTO paths (process_auto_reply,
# run_auto_apply, send_auto_reply, can_auto_send, confirm_live_send) are never
# imported or called here. All evaluation is read-only via injected
# evaluate_fn / transport / status_fn (fakes in tests; a live CDP/Gmail in use).
# ---------------------------------------------------------------------------

_DEFAULT_HH_CDP_URL = os.getenv("HH_CDP_URL", "http://127.0.0.1:9222")
_DEFAULT_HH_MESSAGES_URL_SUBSTRING = "hh.ru"
# Stage 30C-2: substrings used to locate the chatik iframe (the cross-origin
# frame that actually renders the HH conversation) inside the hh.ru tab.
_DEFAULT_CHATIK_FRAME_SUBSTRINGS = ("chatik.hh.ru", "/chat/")


def _resolve_hh_evaluate(cdp_url=None, url_substring=None, evaluate_fn=None):
    """Return the read-only HH evaluate_fn. Injected fake wins; otherwise build
    from env/(optional args) via make_cdp_evaluate (Runtime.evaluate, no send)."""
    if evaluate_fn is not None:
        return evaluate_fn
    sub = url_substring or _DEFAULT_HH_MESSAGES_URL_SUBSTRING
    cdp = cdp_url or _DEFAULT_HH_CDP_URL
    return make_cdp_evaluate(cdp, sub)


def _resolve_chatik_evaluate(cdp_url=None, url_substring=None, evaluate_fn=None,
                             isolate_substrings=None):
    """Return a read-only evaluate_fn bound to the chatik iframe's isolated
    world (Stage 30C-2). Injected fake wins; otherwise build an isolated-world
    evaluate via make_isolated_world_evaluate so _CONVERSATION_JS can actually
    see the chatik message DOM (a main-frame Runtime.evaluate cannot reach the
    cross-origin chatik iframe). Still read-only: no send, no navigation."""
    if evaluate_fn is not None:
        return evaluate_fn
    sub = url_substring or _DEFAULT_HH_MESSAGES_URL_SUBSTRING
    cdp = cdp_url or _DEFAULT_HH_CDP_URL
    isubs = list(isolate_substrings) if isolate_substrings else _DEFAULT_CHATIK_FRAME_SUBSTRINGS
    return make_isolated_world_evaluate(cdp, sub, isubs)


def _resolve_email_transport(transport=None, max_emails=None):
    """Return a read-only email transport. Injected fake wins; otherwise the
    Stage 28 Gmail read-only connector transport (gmail.readonly, no send)."""
    if transport is not None:
        return transport
    limit = int(max_emails) if max_emails else gmail_readonly_connector.DEFAULT_MAX_LIVE_EMAILS
    return gmail_readonly_connector.GmailReadOnlyConnector(max_live_emails=limit).transport()


def hh_message_list(cdp_url=None, url_substring=None, evaluate_fn=None) -> int:
    """List accessible HH dialog cards. READ-ONLY: no navigation, no send."""
    try:
        ev = _resolve_hh_evaluate(cdp_url=cdp_url, url_substring=url_substring,
                                  evaluate_fn=evaluate_fn)
        res = hh_message_reply.fetch_hh_dialogs_readonly(ev)
    except Exception as e:  # read access failure; never a send attempt
        print(f"[hh-message] list failed (read-only access error): {e}")
        return 1
    if "dialogs" not in res:
        print(f"[hh-message] list unavailable: {res.get('error') or res}")
        return 1
    print(f"page: {res.get('url')}")
    print(f"title: {res.get('title')}")
    dialogs = res.get("dialogs") or []
    if not dialogs:
        print("no dialogs detected (open the HH messages page in the CDP browser)")
    for i, d in enumerate(dialogs):
        print(f"[{i}] {d.get('qa') or d.get('tag')} :: {(d.get('text') or '')[:120]}")
    print("status: READ-ONLY — nothing sent.")
    return 0


def hh_message_preview(conversation_id: str | None = None, cdp_url=None, url_substring=None,
                       evaluate_fn=None, limit: int | None = None, as_json: bool = False,
                       profile=None) -> int:
    """Preview one conversation's context + truth-only reply. READ-ONLY:
    never sends, never calls confirm_live_send, never touches AUTO gates."""
    errors = []
    fresh: Dict[str, Any] = {}
    try:
        ev = _resolve_chatik_evaluate(cdp_url=cdp_url, url_substring=url_substring,
                                      evaluate_fn=evaluate_fn)
        fresh = hh_message_reply.fetch_hh_conversation_readonly(ev)
        if fresh.get("error"):
            errors.append(str(fresh.get("error")))
    except Exception as e:  # read access failure; never a send attempt
        errors.append(f"read-only access error: {e}")

    msgs = fresh.get("messages") or [] if not errors else []
    cid = fresh.get("conversation_id") or conversation_id
    url = fresh.get("url")
    participant = fresh.get("participant")
    composer_present = bool(fresh.get("composer_present", False))
    message_count = len(msgs)

    # Format structured message list
    formatted_msgs = []
    for m in msgs:
        dir_val = (m.get("direction") or "").upper()
        author = "candidate" if dir_val == "OUTGOING" else "employer"
        formatted_msgs.append({
            "author": author,
            "text": m.get("text") or "",
            "timestamp": m.get("timestamp"),
        })

    # Apply limit if requested
    if limit is not None and limit > 0:
        displayed_msgs = formatted_msgs[-limit:]
    else:
        displayed_msgs = formatted_msgs

    if as_json:
        import json
        payload = {
            "conversation_id": cid,
            "url": url,
            "participant": participant,
            "message_count": message_count,
            "composer_present": composer_present,
            "messages": displayed_msgs,
            "errors": errors,
            "status": "READ-ONLY",
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if errors:
        print(f"[hh-message] preview failed (read-only access error): {'; '.join(errors)}")
        print("status: READ-ONLY — nothing sent.")
        return 1

    if not msgs:
        print("[hh-message] preview unavailable: no messages could be read. "
              "Open the target conversation in the CDP browser so the chatik "
              "iframe or /chat/ page is present (the isolated-world evaluate reads the chatik "
              "frame; if the chat page is not open, no messages are found). "
              "PREVIEW ONLY, nothing sent.")
        print("status: PREVIEW ONLY — nothing sent, no AUTO path.")
        return 1

    dialog = hh_message_reply.HHDialog(
        conversation_id=str(cid or ""),
        vacancy_title=fresh.get("title") or "",
        messages=[
            hh_message_reply.HHMessage(
                message_id=f"m{i}",
                text=(m.get("text") or ""),
                sender="candidate" if (m.get("direction") or "") == "OUTGOING" else "employer",
            )
            for i, m in enumerate(msgs)
        ],
    )
    shown = str(fresh.get("conversation_id") or "")
    if shown and conversation_id and shown != str(conversation_id):
        print(f"[hh-message] note: open conversation id={shown} != requested "
              f"{conversation_id}; showing the open one.")

    cls = hh_message_reply.classify_message(dialog)
    gen = hh_message_reply.generate_reply(dialog, profile=profile)

    print("[hh-message] preview (READ-ONLY)")
    print(f"conversation: {cid or '(unknown)'}")
    print(f"url: {url or '(unknown)'}")
    print(f"participant: {participant or '(none)'}")
    print(f"messages: {message_count}")
    print(f"composer: {'available' if composer_present else 'unavailable'}")
    print()
    print("--- last messages ---")
    for m in displayed_msgs:
        snippet = m["text"].replace("\n", " ")[:120]
        print(f"[{m['author']}] {snippet}")
    print()
    print(f"conversation_id: {dialog.conversation_id}")
    print(f"classification: {cls.value}")
    print("context:")
    for m in dialog.messages[- (limit or len(dialog.messages)):]:
        who = "me" if m.sender == "candidate" else "them"
        print(f"  [{who}] {m.text[:160]}")
    print(f"prepared reply: {gen.get('reply') or '(none — needs human review)'}")
    print(f"sources: {gen.get('sources')}")
    print("status: PREVIEW ONLY — nothing sent, no AUTO path.")
    return 0


def email_list(transport=None, max_emails=None) -> int:
    """List incoming emails via a read-only transport. NEVER sends."""
    try:
        tr = _resolve_email_transport(transport=transport, max_emails=max_emails)
        res = email_message_reply.fetch_incoming_emails_readonly(transport=tr)
    except Exception as e:  # read access failure; never a send attempt
        print(f"[email] list failed (read-only access error): {e}")
        return 1
    if res.get("verdict") != "OK":
        print(f"[email] list blocked: {res.get('reason')}")
        return 1
    emails = res.get("emails") or []
    if not emails:
        print("no incoming emails.")
    for i, e in enumerate(emails):
        print(f"[{i}] {e.sender_email or '?'} :: {e.subject or '(no subject)'}")
    print("status: READ-ONLY — nothing sent (EmailSendGate always blocks any send).")
    return 0


def email_preview(target: str, transport=None, max_emails=None, profile=None) -> int:
    """Preview a reply for the target email (index from 'email list'). READ-ONLY:
    never sends; EmailSendGate is not instantiated/bypassed (reply is pure truth-only)."""
    try:
        tr = _resolve_email_transport(transport=transport, max_emails=max_emails)
        res = email_message_reply.fetch_incoming_emails_readonly(transport=tr)
    except Exception as e:  # read access failure; never a send attempt
        print(f"[email] preview failed (read-only access error): {e}")
        return 1
    emails = res.get("emails") or []
    if not emails:
        print(f"[email] preview blocked: {res.get('reason', 'no emails')}")
        return 1
    try:
        idx = int(target)
    except Exception:
        idx = -1
    if not (0 <= idx < len(emails)):
        print(f"[email] target {target!r} out of range (0..{len(emails) - 1}). "
              f"Run 'email list' first — nothing sent.")
        return 1
    e = emails[idx]
    ctx = email_message_reply.EmailContext(message=e, thread_messages=[e])
    cls = email_message_reply.classify_email(ctx)
    gen = email_message_reply.generate_email_reply(ctx, profile=profile)
    print(f"to: {e.sender_email} ({e.sender_name})")
    print(f"subject: {e.subject}")
    print(f"classification: {cls.value}")
    print(f"prepared reply: {gen.get('reply') or '(none — needs human review)'}")
    print(f"sources: {gen.get('sources')}")
    print("status: PREVIEW ONLY — EmailSendGate blocks any send.")
    return 0


def gmail_status(status_fn=None) -> int:
    """Show gmail.readonly auth/connection status. READ-ONLY: no send/modify/delete."""
    st = (status_fn or gmail_readonly_connector.gmail_provider_status)()
    print(f"gmail status: {st.get('status')}")
    print(f"reason: {st.get('reason')}")
    print(f"scope: {gmail_readonly_connector.GMAIL_READONLY_SCOPE}")
    return 0 if st.get("status") == "READY" else 1


# ---------------------------------------------------------------------------
# Stage 30C Phase 2A — REVIEW/READ-ONLY runtime wiring (new safe previews).
# Only pure read-only helpers that do NOT send, NOT submit, NOT mutate Gmail,
# NOT enable AUTO, NOT bypass any safety gate are exposed here.
# ---------------------------------------------------------------------------

## Stage 30C Phase 2A — not wired / gap:
# - Stage 21 dual-mode apply orchestration (HH form prefill + submit pipeline)
#   — not wired: requires browser submit + review gate + controlled submit;
#   no safe REVIEW-only interpretation; AUTO never default.
# - HH AUTO reply path (process_auto_reply / send_auto_reply / can_auto_send /
#   is_safe_for_auto_reply etc) — not wired: AUTO execution with kill-switch
#   HH_AUTO_REPLY_ENABLED and live send; REVIEW preview already covers read-only
#   classify+generate. No AUTO wiring in this phase.
# - Email send path and HH submit modules (send gate / controlled submit /
#   hh submission flows / Gmail send/modify/delete) — not wired: physical send
#   is blocked; no READ-ONLY entry. Gmail transport stays gmail.readonly only.
# - Phase 1 known gap RESOLVED in 30C-2: hh-message preview/classify now use an
#   isolated-world evaluate (make_isolated_world_evaluate -> Page.createIsolatedWorld
#   on the chatik iframe) instead of main-frame-only make_cdp_evaluate, so the
#   read-only _CONVERSATION_JS actually sees the chatik message DOM. Still no send.
# - process_incoming_message / process_incoming_email deduplication stores
#   (artifacts/*) — not wired as CLI: they persist state and mix preview+dedup;
#   the stateless classify/generate/link helpers are wired instead (pure, no DB).
# ---------------------------------------------------------------------------


def hh_message_classify(conversation_id: str | None = None, cdp_url=None, url_substring=None,
                        evaluate_fn=None, limit: int | None = None, as_json: bool = False,
                        profile=None) -> int:
    """Classify one conversation's context and draft reply. READ-ONLY: never sends."""
    errors = []
    fresh: Dict[str, Any] = {}
    try:
        ev = _resolve_chatik_evaluate(cdp_url=cdp_url, url_substring=url_substring,
                                      evaluate_fn=evaluate_fn)
        fresh = hh_message_reply.fetch_hh_conversation_readonly(ev)
        if fresh.get("error"):
            errors.append(str(fresh.get("error")))
    except Exception as e:  # read access failure; never a send attempt
        errors.append(f"read-only access error: {e}")

    msgs = fresh.get("messages") or [] if not errors else []
    cid = fresh.get("conversation_id") or conversation_id

    if as_json and errors:
        import json
        payload = {
            "conversation_id": cid,
            "classification": "HUMAN_REVIEW",
            "confidence": 0.0,
            "reason": "; ".join(errors),
            "context": [],
            "prepared_reply": None,
            "sources": [],
            "status": "READ-ONLY",
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if errors:
        print(f"[hh-message] classify failed (read-only access error): {'; '.join(errors)}")
        print("status: READ-ONLY — nothing sent.")
        return 1

    if not msgs:
        if as_json:
            import json
            payload = {
                "conversation_id": cid,
                "classification": "EMPTY_CONVERSATION",
                "confidence": 1.0,
                "reason": "Conversation has no messages.",
                "context": [],
                "prepared_reply": None,
                "sources": [],
                "status": "READ-ONLY",
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        else:
            print("[hh-message] classify unavailable: no messages could be read. "
                  "Open the target conversation in the CDP browser so the chatik "
                  "iframe or /chat/ page is present (the isolated-world evaluate reads the chatik "
                  "frame; if the chat page is not open, no messages are found). "
                  "PREVIEW ONLY, nothing sent.")
            print("status: PREVIEW ONLY — nothing sent, no AUTO path.")
            return 1

    vac_id = fresh.get("vacancy_id")
    vac_stable_id = f"hh:{vac_id}" if vac_id else ""
    dialog = hh_message_reply.HHDialog(
        conversation_id=str(cid or ""),
        vacancy_title=fresh.get("title") or "",
        vacancy_stable_id=vac_stable_id,
        employer=fresh.get("employer") or "",
        messages=[
            hh_message_reply.HHMessage(
                message_id=f"m{i}",
                text=(m.get("text") or ""),
                sender="candidate" if (m.get("direction") or "") == "OUTGOING" else "employer",
            )
            for i, m in enumerate(msgs)
        ],
    )

    det = hh_message_reply.classify_hh_conversation_detailed(dialog, profile=profile)
    context_list = det.get("context", [])
    if limit is not None and limit > 0:
        displayed_context = context_list[-limit:]
    else:
        displayed_context = context_list

    if as_json:
        import json
        payload = {
            "conversation_id": cid,
            "classification": det.get("classification"),
            "confidence": det.get("confidence"),
            "reason": det.get("reason"),
            "question": det.get("question"),
            "required_facts": det.get("required_facts", []),
            "available_facts": det.get("available_facts", []),
            "missing_facts": det.get("missing_facts", []),
            "context": displayed_context,
            "prepared_reply": det.get("prepared_reply"),
            "sources": det.get("sources"),
            "status": "READ-ONLY",
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print("[hh-message] classify (READ-ONLY)")
    print()
    print(f"conversation: {cid or '(unknown)'}")
    print(f"classification: {det.get('classification')}")
    print(f"confidence: {det.get('confidence', 0.0):.2f}")
    print(f"reason: {det.get('reason')}")
    print()
    print("context:")
    for c in displayed_context:
        who = "me" if c.get("author") == "candidate" else "them"
        snippet = (c.get("text") or "").replace("\n", " ")[:160]
        print(f"  [{who}] {snippet}")
    print()
    print("prepared reply:")
    if det.get("prepared_reply"):
        for line in det.get("prepared_reply").splitlines():
            print(f"  {line}")
    else:
        print("  (none — needs human review)")
    print()
    print("sources:")
    sources = det.get("sources") or []
    if sources:
        for s in sources:
            print(f"  {s}")
    else:
        print("  none")
    print()
    print(f"conversation_id: {dialog.conversation_id}")
    legacy_cls = hh_message_reply.classify_message(dialog)
    print(f"classification: {legacy_cls.value}")
    last = dialog.messages[-1].text if dialog.messages else ""
    print(f"last_message: {last[:200]}")
    print("status: PREVIEW ONLY — nothing sent, no AUTO path.")
    return 0


def hh_message_validate(conversation_id: str | None = None, cdp_url=None, url_substring=None,
                        evaluate_fn=None, limit: int | None = None, as_json: bool = False,
                        profile=None) -> int:
    """Validate prepared reply draft against safety rules and profile evidence. READ-ONLY: never sends."""
    errors = []
    fresh: Dict[str, Any] = {}
    try:
        ev = _resolve_chatik_evaluate(cdp_url=cdp_url, url_substring=url_substring,
                                      evaluate_fn=evaluate_fn)
        fresh = hh_message_reply.fetch_hh_conversation_readonly(ev)
        if fresh.get("error"):
            errors.append(str(fresh.get("error")))
    except Exception as e:
        errors.append(f"read-only access error: {e}")

    msgs = fresh.get("messages") or [] if not errors else []
    cid = fresh.get("conversation_id") or conversation_id

    if as_json and errors:
        import json
        payload = {
            "conversation_id": cid,
            "classification": "HUMAN_REVIEW",
            "draft": None,
            "validation": "HUMAN_REVIEW",
            "checks": {
                "answers_last_question": False,
                "uses_supported_facts": False,
                "contains_unverified_claims": False,
                "contains_sensitive_claims": False,
                "is_empty": True,
            },
            "reasons": errors,
            "status": "READ-ONLY",
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if errors:
        print(f"[hh-message] validate failed (read-only access error): {'; '.join(errors)}")
        print("status: READ-ONLY — nothing sent.")
        return 1

    if not msgs:
        if as_json:
            import json
            payload = {
                "conversation_id": cid,
                "classification": "EMPTY_CONVERSATION",
                "draft": None,
                "validation": "REJECTED",
                "checks": {
                    "answers_last_question": False,
                    "uses_supported_facts": False,
                    "contains_unverified_claims": False,
                    "contains_sensitive_claims": False,
                    "is_empty": True,
                },
                "reasons": ["Conversation has no messages."],
                "status": "READ-ONLY",
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        else:
            print("[hh-message] validate unavailable: no messages could be read. "
                  "Open the target conversation in the CDP browser so the chatik "
                  "iframe or /chat/ page is present. PREVIEW ONLY, nothing sent.")
            print("status: PREVIEW ONLY — nothing sent, no AUTO path.")
            return 1

    vac_id = fresh.get("vacancy_id")
    vac_stable_id = f"hh:{vac_id}" if vac_id else ""
    dialog = hh_message_reply.HHDialog(
        conversation_id=str(cid or ""),
        vacancy_title=fresh.get("title") or "",
        vacancy_stable_id=vac_stable_id,
        employer=fresh.get("employer") or "",
        messages=[
            hh_message_reply.HHMessage(
                message_id=f"m{i}",
                text=(m.get("text") or ""),
                sender="candidate" if (m.get("direction") or "") == "OUTGOING" else "employer",
            )
            for i, m in enumerate(msgs)
        ],
    )

    det = hh_message_reply.classify_hh_conversation_detailed(dialog, profile=profile)
    val = hh_message_reply.validate_hh_reply_draft(
        dialog,
        draft=det.get("prepared_reply"),
        classification=det.get("classification"),
        profile=profile,
    )

    if as_json:
        import json
        payload = {
            "conversation_id": cid,
            "classification": val.get("classification"),
            "draft": val.get("draft"),
            "validation": val.get("validation"),
            "checks": val.get("checks"),
            "reasons": val.get("reasons"),
            "status": "READ-ONLY",
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print("[hh-message] validate (READ-ONLY)")
    print()
    print(f"conversation: {cid or '(unknown)'}")
    print(f"classification: {val.get('classification')}")
    print(f"validation: {val.get('validation')}")
    print()
    print("checks:")
    for k, v in (val.get("checks") or {}).items():
        print(f"  {k}: {v}")
    print()
    print("reasons:")
    reasons = val.get("reasons") or []
    if reasons:
        for r in reasons:
            print(f"  - {r}")
    else:
        print("  none")
    print()
    print("draft:")
    if val.get("draft"):
        for line in val.get("draft").splitlines():
            print(f"  {line}")
    else:
        print("  (none)")
    print()
    print("status: READ-ONLY — nothing sent.")
    return 0


def hh_message_send(conversation_id: str | None = None, confirm: bool = False,
                    cdp_url=None, url_substring=None, evaluate_fn=None,
                    limit: int | None = None, as_json: bool = False,
                    profile=None) -> int:
    """Stage 30D.6: Controlled human-confirmed HH reply send.
    Requires --confirm to send. Without --confirm, returns AWAITING_CONFIRMATION (dry-run).
    """
    ev = evaluate_fn
    if ev is None:
        try:
            ev = _resolve_chatik_evaluate(cdp_url=cdp_url, url_substring=url_substring)
        except Exception as e:
            if as_json:
                import json
                payload = {
                    "conversation_id": conversation_id,
                    "classification": None,
                    "validation": None,
                    "draft": None,
                    "confirmed": confirm,
                    "sent": False,
                    "post_send_verified": False,
                    "errors": [f"CDP connection failed: {e}"],
                    "status": "BLOCKED_TARGET_NOT_FOUND",
                }
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                return 1
            else:
                print(f"[hh-message] send blocked: CDP connection failed ({e}).")
                print("status: BLOCKED — nothing sent.")
                return 1

    try:
        fresh = hh_message_reply.fetch_hh_conversation_readonly(ev)
    except Exception as e:
        if as_json:
            import json
            payload = {
                "conversation_id": conversation_id,
                "classification": None,
                "validation": None,
                "draft": None,
                "confirmed": confirm,
                "sent": False,
                "post_send_verified": False,
                "errors": [f"Failed to read conversation DOM: {e}"],
                "status": "BLOCKED_DOM_INACCESSIBLE",
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 1
        else:
            print(f"[hh-message] send blocked: failed to read conversation DOM ({e}).")
            print("status: BLOCKED — nothing sent.")
            return 1

    cid = fresh.get("conversation_id")
    msgs = fresh.get("messages") or []

    if not cid or not msgs:
        err = "No active conversation or messages found in DOM."
        if as_json:
            import json
            payload = {
                "conversation_id": cid or conversation_id,
                "classification": None,
                "validation": None,
                "draft": None,
                "confirmed": confirm,
                "sent": False,
                "post_send_verified": False,
                "errors": [err],
                "status": "BLOCKED_DOM_INACCESSIBLE",
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 1
        else:
            print(f"[hh-message] send blocked: {err}")
            print("status: BLOCKED — nothing sent.")
            return 1

    if conversation_id and str(conversation_id).strip() != str(cid).strip():
        err = f"Specified conversation ID ({conversation_id}) does not match open chat ({cid})."
        if as_json:
            import json
            payload = {
                "conversation_id": cid,
                "classification": None,
                "validation": None,
                "draft": None,
                "confirmed": confirm,
                "sent": False,
                "post_send_verified": False,
                "errors": [err],
                "status": "BLOCKED_CONVERSATION_MISMATCH",
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 1
        else:
            print(f"[hh-message] send blocked: {err}")
            print("status: BLOCKED — nothing sent.")
            return 1

    vac_id = fresh.get("vacancy_id")
    vac_stable_id = f"hh:{vac_id}" if vac_id else ""
    dialog = hh_message_reply.HHDialog(
        conversation_id=str(cid or ""),
        vacancy_title=fresh.get("title") or "",
        vacancy_stable_id=vac_stable_id,
        employer=fresh.get("employer") or "",
        messages=[
            hh_message_reply.HHMessage(
                message_id=f"m{i}",
                text=(m.get("text") or ""),
                sender="candidate" if (m.get("direction") or "") == "OUTGOING" else "employer",
            )
            for i, m in enumerate(msgs)
        ],
    )

    det = hh_message_reply.classify_hh_conversation_detailed(dialog, profile=profile)
    draft = det.get("prepared_reply")
    classification = det.get("classification")

    val = hh_message_reply.validate_hh_reply_draft(
        dialog,
        draft=draft,
        classification=classification,
        profile=profile,
    )
    validation = val.get("validation")
    reasons = val.get("reasons") or []

    # Check if eligible for send
    is_eligible = (
        classification == "NEEDS_REPLY"
        and validation == "APPROVED"
        and bool(draft and draft.strip())
    )

    if not is_eligible:
        errs = list(reasons)
        if classification != "NEEDS_REPLY" and not errs:
            errs.append(f"Classification is {classification}; reply is not needed.")
        if validation != "APPROVED" and not errs:
            errs.append(f"Validation status is {validation}; automated send blocked.")

        status = f"BLOCKED_VALIDATION_{validation}" if validation != "APPROVED" else "BLOCKED_CLASSIFICATION"
        if as_json:
            import json
            payload = {
                "conversation_id": cid,
                "classification": classification,
                "validation": validation,
                "draft": draft,
                "confirmed": confirm,
                "sent": False,
                "post_send_verified": False,
                "errors": errs,
                "status": status,
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 1
        else:
            print(f"[hh-message] send blocked: {', '.join(errs)}")
            print()
            print(f"conversation: {cid}")
            print(f"classification: {classification}")
            print(f"validation: {validation}")
            print(f"status: {status} — nothing sent.")
            return 1

    # If NOT confirmed -> Dry-run review
    if not confirm:
        if as_json:
            import json
            payload = {
                "conversation_id": cid,
                "classification": classification,
                "validation": validation,
                "draft": draft,
                "confirmed": False,
                "sent": False,
                "post_send_verified": False,
                "errors": [],
                "status": "AWAITING_CONFIRMATION",
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        else:
            print("[hh-message] send (AWAITING CONFIRMATION)")
            print()
            print(f"conversation: {cid}")
            print(f"classification: {classification}")
            print(f"validation: {validation}")
            print()
            print("draft:")
            for line in (draft or "").splitlines():
                print(f"  {line}")
            print()
            print("status: AWAITING CONFIRMATION — run with --confirm to send.")
            return 0

    # Confirmed! Perform minimal isolated DOM send
    pre_count = len(msgs)
    pre_outgoing_count = sum(1 for m in msgs if m.get("direction") == "OUTGOING")
    pre_fingerprints = {(m.get("direction"), (m.get("text") or "").strip()) for m in msgs}

    send_res = hh_message_reply.send_confirmed_hh_reply(ev, draft)
    if not send_res.get("ok"):
        err = send_res.get("reason", "DOM send operation failed")
        if as_json:
            import json
            payload = {
                "conversation_id": cid,
                "classification": classification,
                "validation": validation,
                "draft": draft,
                "confirmed": True,
                "sent": False,
                "post_send_verified": False,
                "errors": [err],
                "status": "SEND_FAILED",
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 1
        else:
            print(f"[hh-message] send failed: {err}")
            print("status: SEND_FAILED")
            return 1

    # Robust differential post-send verification
    import time
    post_send_verified = False
    errors = []
    draft_clean = draft.strip()
    draft_prefix = draft_clean[:40].lower()

    for attempt in range(3):
        time.sleep(0.7)
        try:
            fresh_post = hh_message_reply.fetch_hh_conversation_readonly(ev)
            post_cid = fresh_post.get("conversation_id")
            post_msgs = fresh_post.get("messages") or []
            post_count = len(post_msgs)
            post_outgoing_count = sum(1 for m in post_msgs if m.get("direction") == "OUTGOING")

            # 1. Target conversation ID check
            if post_cid and str(post_cid).strip() != str(cid).strip():
                errors.append(f"Conversation ID shifted during verification ({cid} -> {post_cid}).")
                break

            # 2. Check for new messages
            if post_count > pre_count or post_outgoing_count > pre_outgoing_count:
                new_msgs = post_msgs[pre_count:] if post_count > pre_count else [
                    m for m in post_msgs if (m.get("direction"), (m.get("text") or "").strip()) not in pre_fingerprints
                ]
                new_outgoing = [m for m in new_msgs if m.get("direction") == "OUTGOING"]
                for nm in new_outgoing:
                    n_text = (nm.get("text") or "").strip()
                    if draft_prefix in n_text.lower() or draft_clean == n_text:
                        post_send_verified = True
                        break
                if post_send_verified:
                    break
        except Exception as e:
            errors.append(f"Post-send evaluation error: {e}")

    if post_send_verified:
        status = "SENT"
        errors = []
        rc = 0
    else:
        status = "SEND_UNVERIFIED"
        if not errors:
            errors.append("Message submitted to DOM but no new outgoing message matching draft appeared in conversation DOM.")
        rc = 1

    if as_json:
        import json
        payload = {
            "conversation_id": cid,
            "classification": classification,
            "validation": validation,
            "draft": draft,
            "confirmed": True,
            "sent": True,
            "post_send_verified": post_send_verified,
            "errors": errors,
            "status": status,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return rc
    else:
        print(f"[hh-message] send ({status})")
        print()
        print(f"conversation: {cid}")
        print(f"classification: {classification}")
        print(f"validation: {validation}")
        print(f"post_send_verified: {post_send_verified}")
        print()
        print("draft:")
        for line in (draft or "").splitlines():
            print(f"  {line}")
        print()
        if errors:
            print("errors:")
            for e in errors:
                print(f"  - {e}")
            print()
        print(f"status: {status}")
        return rc


def hh_message_triage(conversation_id: str | None = None, limit: int | None = None,
                      cdp_url=None, url_substring=None, evaluate_fn=None,
                      as_json: bool = False, profile=None) -> int:
    """Stage 30D.9: Multi-conversation read-only triage.
    Discovers available HH conversations, resolves metadata/vacancies,
    performs classification, facts analysis, draft generation and validation.
    Strictly READ-ONLY: never modifies DOM, never navigates, never sends.
    """
    ev = evaluate_fn
    if ev is None:
        try:
            from .hh_browser_launcher import ensure_hh_browser
            ensure_hh_browser(cdp_url=cdp_url)
            ev = _resolve_chatik_evaluate(cdp_url=cdp_url, url_substring=url_substring)
        except Exception as e:
            if as_json:
                import json
                payload = {
                    "status": "READ-ONLY",
                    "conversation_count": 0,
                    "items": [],
                    "errors": [f"CDP connection failed: {e}"],
                }
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                return 1
            else:
                print(f"[hh-message] triage failed: CDP connection failed ({e}).")
                print("status: READ-ONLY — nothing sent.")
                return 1

    errors = []
    try:
        raw_list = hh_message_reply.fetch_hh_conversations_list_readonly(ev)
        raw_convs = raw_list.get("conversations") or []
    except Exception as e:
        raw_convs = []
        errors.append(f"Failed to fetch conversation list: {e}")

    # Deduplicate by conversation_id
    seen_ids = set()
    unique_convs = []
    for c in raw_convs:
        cid = str(c.get("conversation_id") or "").strip()
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            unique_convs.append(c)

    # Filter by conversation_id if specified
    if conversation_id:
        target_cid = str(conversation_id).strip()
        filtered = [c for c in unique_convs if str(c.get("conversation_id")).strip() == target_cid]
        if not filtered:
            # Fallback: construct from active open chat
            filtered = [{
                "conversation_id": target_cid,
                "url": f"https://hh.ru/chat/{target_cid}",
                "title": None,
                "employer": None,
                "snippet": None,
                "is_selected": True,
            }]
        unique_convs = filtered

    # Apply limit
    if limit is not None and limit > 0:
        unique_convs = unique_convs[:limit]

    items = []
    for c in unique_convs:
        cid = str(c.get("conversation_id") or "")
        try:
            is_sel = c.get("is_selected", False)
            title = c.get("title")
            employer = c.get("employer")

            if is_sel:
                try:
                    fresh = hh_message_reply.fetch_hh_conversation_readonly(ev)
                    msgs = fresh.get("messages") or []
                    if fresh.get("title") and "Чаты" not in fresh.get("title"):
                        title = fresh.get("title")
                    employer = fresh.get("employer") or employer
                except Exception:
                    msgs = []
                
                dialog = hh_message_reply.HHDialog(
                    conversation_id=cid,
                    vacancy_title=title or "",
                    employer=employer or "",
                    messages=[
                        hh_message_reply.HHMessage(
                            message_id=f"m{i}",
                            text=(m.get("text") or ""),
                            sender="candidate" if (m.get("direction") or "") == "OUTGOING" else "employer",
                        )
                        for i, m in enumerate(msgs)
                    ],
                )
            else:
                snippet = (c.get("snippet") or "").replace("\xa0", " ").strip()
                snippet_lower = snippet.lower()
                if "отклик на вакансию" in snippet_lower:
                    sender = "candidate"
                else:
                    sender = "employer"

                dialog = hh_message_reply.HHDialog(
                    conversation_id=cid,
                    vacancy_title=title or "",
                    employer=employer or "",
                    messages=[
                        hh_message_reply.HHMessage(
                            message_id="m0",
                            text=snippet,
                            sender=sender,
                        )
                    ] if snippet else [],
                )

            # Link vacancy
            v_match = hh_message_reply.resolve_vacancy_for_dialog(dialog) or {}
            vac_stable_id = v_match.get("stable_id")
            if v_match.get("employer") and not dialog.employer:
                dialog.employer = v_match.get("employer")
            resolved_employer = dialog.employer or v_match.get("employer") or employer or None

            # Classification
            det = hh_message_reply.classify_hh_conversation_detailed(dialog, profile=profile)
            classification = det.get("classification")
            confidence = det.get("confidence", 0.9)
            question = det.get("question")
            req_facts = det.get("required_facts", [])
            avail_facts = det.get("available_facts", [])
            miss_facts = det.get("missing_facts", [])
            draft = det.get("prepared_reply")

            # Validation
            if classification == "NEEDS_REPLY":
                val = hh_message_reply.validate_hh_reply_draft(
                    dialog,
                    draft=draft,
                    classification=classification,
                    profile=profile,
                )
                validation = val.get("validation", "REJECTED")
            elif classification == "HUMAN_REVIEW":
                validation = "HUMAN_REVIEW"
                draft = None
            else:
                validation = "REJECTED"
                draft = None

            item = {
                "conversation_id": cid,
                "participant": title or resolved_employer or None,
                "vacancy_stable_id": vac_stable_id or None,
                "employer": resolved_employer,
                "classification": classification,
                "confidence": confidence,
                "question": question,
                "required_facts": req_facts,
                "available_facts": avail_facts,
                "missing_facts": miss_facts,
                "draft": draft,
                "validation": validation,
            }
            items.append(item)
        except Exception as e:
            items.append({
                "conversation_id": cid,
                "participant": c.get("title") or c.get("employer") or None,
                "vacancy_stable_id": None,
                "employer": c.get("employer"),
                "classification": "ERROR",
                "confidence": 0.0,
                "question": None,
                "required_facts": [],
                "available_facts": [],
                "missing_facts": [],
                "draft": None,
                "validation": "REJECTED",
                "error": str(e),
            })

    if as_json:
        import json
        payload = {
            "status": "READ-ONLY",
            "conversation_count": len(items),
            "items": items,
            "errors": errors,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    else:
        print("[hh-message] triage (READ-ONLY)")
        print()
        print(f"found: {len(unique_convs)} conversations")
        print(f"triaged: {len(items)}")
        print()
        if items:
            print("items:")
            for it in items:
                cid = it.get("conversation_id")
                part = it.get("participant") or "Unknown"
                emp = f" ({it.get('employer')})" if it.get("employer") else ""
                cls = it.get("classification")
                conf = it.get("confidence", 0.0)
                val = it.get("validation")
                draft = it.get("draft")
                print(f"  [{cid}] {part}{emp}")
                print(f"    classification: {cls} (conf: {conf:.2f})")
                print(f"    validation: {val}")
                if draft:
                    print(f"    draft: {draft[:80]}...")
                else:
                    print("    draft: (none)")
                print()

        # Summary
        counts = {"NEEDS_REPLY": 0, "NO_REPLY_NEEDED": 0, "HUMAN_REVIEW": 0, "ERROR": 0}
        for it in items:
            c_type = it.get("classification")
            counts[c_type] = counts.get(c_type, 0) + 1

        print("summary:")
        for k, v in counts.items():
            print(f"  {k}: {v}")
        print(f"  errors: {len(errors)}")
        print()
        print("status: READ-ONLY — nothing sent.")
        return 0


def email_classify(target: str, transport=None, max_emails=None, profile=None) -> int:
    """Classify one email by index. READ-ONLY: never sends, never mutates."""
    try:
        tr = _resolve_email_transport(transport=transport, max_emails=max_emails)
        res = email_message_reply.fetch_incoming_emails_readonly(transport=tr)
    except Exception as e:  # read access failure; never a send attempt
        print(f"[email] classify failed (read-only access error): {e}")
        return 1
    if res.get("verdict") != "OK":
        print(f"[email] classify blocked: {res.get('reason')}")
        return 1
    emails = res.get("emails") or []
    if not emails:
        print("[email] classify blocked: no emails — nothing sent.")
        return 1
    try:
        idx = int(target)
    except Exception:
        idx = -1
    if not (0 <= idx < len(emails)):
        print(f"[email] target {target!r} out of range (0..{len(emails) - 1}). "
              f"Run 'email list' first — nothing sent.")
        return 1
    e = emails[idx]
    ctx = email_message_reply.EmailContext(message=e, thread_messages=[e])
    cls = email_message_reply.classify_email(ctx)
    print(f"to: {e.sender_email} ({e.sender_name})")
    print(f"subject: {e.subject}")
    print(f"classification: {cls.value}")
    print("status: READ-ONLY — nothing sent.")
    return 0


def email_link(target: str, transport=None, max_emails=None) -> int:
    """Show linkage of one email to a vacancy/company. READ-ONLY: never sends."""
    try:
        tr = _resolve_email_transport(transport=transport, max_emails=max_emails)
        res = email_message_reply.fetch_incoming_emails_readonly(transport=tr)
    except Exception as e:  # read access failure; never a send attempt
        print(f"[email] link failed (read-only access error): {e}")
        return 1
    if res.get("verdict") != "OK":
        print(f"[email] link blocked: {res.get('reason')}")
        return 1
    emails = res.get("emails") or []
    if not emails:
        print("[email] link blocked: no emails — nothing sent.")
        return 1
    try:
        idx = int(target)
    except Exception:
        idx = -1
    if not (0 <= idx < len(emails)):
        print(f"[email] target {target!r} out of range (0..{len(emails) - 1}). "
              f"Run 'email list' first — nothing sent.")
        return 1
    e = emails[idx]
    ctx = email_message_reply.EmailContext(message=e, thread_messages=[e])
    link = email_message_reply.link_email_to_vacancy(ctx)
    print(f"to: {e.sender_email} ({e.sender_name})")
    print(f"subject: {e.subject}")
    print(f"linked_company: {link.get('linked_company') or '(none)'}")
    print(f"linked_vacancy: {link.get('linked_vacancy') or '(none)'}")
    print(f"confidence: {link.get('confidence')}")
    print(f"note: {link.get('note')}")
    print("status: READ-ONLY — nothing sent.")
    return 0


# ---------------------------------------------------------------------------
# Stage 30D — hh-message diagnose (READ-ONLY probe)
# ---------------------------------------------------------------------------

def hh_message_diagnose(
    cdp_url: str | None = None,
    url_substring: str | None = None,
    frame_substrings: Any = None,
    evaluate_fn: Any = None,
    frame_probe_fn: Any = None,
    targets: Any = None,
    as_json: bool = False,
) -> int:
    """Stage 30D: Probe HH tab, messages page, chatik frame, isolated world, and conversation DOM.
    Strictly READ-ONLY: no clicks, no sends, no navigation, no DB writes."""
    cdp = cdp_url or _DEFAULT_HH_CDP_URL
    url_sub = url_substring or _DEFAULT_HH_MESSAGES_URL_SUBSTRING
    if isinstance(frame_substrings, str):
        f_subs = [s.strip() for s in frame_substrings.split(",") if s.strip()]
    elif frame_substrings:
        f_subs = list(frame_substrings)
    else:
        f_subs = list(_DEFAULT_CHATIK_FRAME_SUBSTRINGS)

    errors: List[str] = []

    cdp_reachable = False
    matching_tabs: List[Dict[str, str]] = []
    hh_page_present = False
    page_url: str | None = None
    page_title: str | None = None
    page_is_messages: bool | None = None
    frames: List[Dict[str, Any]] = []
    chatik_frame_found = False
    chatik_frame_url: str | None = None
    isolated_world_ok: bool | None = None
    conversation_dom_ok: bool | None = None
    conversation_id: str | None = None
    composer_present: bool | None = None
    message_count: int = 0
    dialogs_visible: int | None = None

    # Step 1: CDP Reachability / Targets
    target_list: List[Dict[str, Any]] = []
    if targets is not None:
        target_list = targets
        cdp_reachable = True
    else:
        try:
            target_list = prefill_execute._cdp_list_targets(cdp)
            cdp_reachable = True
        except Exception as e:
            errors.append(f"CDP unreachable at {cdp}: {e}")
            cdp_reachable = False

    # Step 2: Tab matching
    ws_url = None
    if cdp_reachable:
        page_targets = [t for t in target_list if t.get("type") == "page"]
        for t in page_targets:
            t_url = t.get("url") or ""
            t_title = t.get("title") or ""
            if url_sub.lower() in t_url.lower():
                matching_tabs.append({"url": t_url, "title": t_title})

        hh_page_present = len(matching_tabs) > 0
        if hh_page_present:
            best_target = prefill_execute.select_best_hh_target(page_targets, url_sub)
            if best_target:
                ws_url = best_target.get("webSocketDebuggerUrl")
                page_url = best_target.get("url")
                page_title = best_target.get("title")
            else:
                ws_url = matching_tabs[0].get("webSocketDebuggerUrl") if hasattr(matching_tabs[0], "get") else None
                page_url = matching_tabs[0]["url"]
                page_title = matching_tabs[0]["title"]
        else:
            errors.append(f"No open tab matching {url_sub!r} found among {len(page_targets)} page tab(s)")

    # Step 3: Main frame / Messages page check
    if hh_page_present:
        try:
            main_ev = _resolve_hh_evaluate(cdp_url=cdp, url_substring=url_sub, evaluate_fn=evaluate_fn)
            dialog_res = hh_message_reply.fetch_hh_dialogs_readonly(main_ev)
            if dialog_res.get("error"):
                errors.append(f"Dialog list evaluation error: {dialog_res.get('error')}")
                page_is_messages = False
                dialogs_visible = 0
            else:
                page_is_messages = bool(dialog_res.get("pageIsMessages", False))
                dialogs = dialog_res.get("dialogs") or []
                dialogs_visible = len(dialogs)
                if not page_is_messages:
                    errors.append(f"Matching tab is open but not on messages section (url={page_url})")
        except Exception as e:
            errors.append(f"Main frame evaluation failed: {e}")
            page_is_messages = False
            dialogs_visible = 0

    # Step 4: Frame Tree & Isolated World Probe
    if page_is_messages:
        if frame_probe_fn is not None:
            try:
                probe_res = frame_probe_fn(ws_url, f_subs)
                frames = probe_res.get("frames", [])
                chatik_frame_found = bool(probe_res.get("chatik_frame_found", False))
                chatik_frame_url = probe_res.get("chatik_frame_url")
                isolated_world_ok = probe_res.get("isolated_world_ok")
                if not chatik_frame_found:
                    errors.append(f"Chatik frame matching {f_subs} not found")
                elif isolated_world_ok is False:
                    errors.append("Isolated world creation or 1+1 test evaluation failed")
            except Exception as e:
                errors.append(f"Frame probe error: {e}")
                chatik_frame_found = False
                isolated_world_ok = False
        elif evaluate_fn is not None:
            # evaluate_fn injected in tests without explicit frame_probe_fn
            try:
                test_1plus1 = evaluate_fn("1+1")
                test_val = str(test_1plus1).strip()
                isolated_world_ok = (test_val == "2")
                chatik_frame_found = True
                chatik_frame_url = "https://chatik.hh.ru/chat/mock"
                frames = [{"frameId": "mock-chatik", "url": chatik_frame_url, "matched": True}]
                if not isolated_world_ok:
                    errors.append("Isolated world evaluation '1+1' failed")
            except prefill_execute.ChatikFrameNotFound:
                chatik_frame_found = False
                chatik_frame_url = None
                isolated_world_ok = False
                errors.append(f"Chatik frame matching {f_subs} not found")
            except Exception as e:
                chatik_frame_found = True
                chatik_frame_url = "https://chatik.hh.ru/chat/mock"
                frames = [{"frameId": "mock-chatik", "url": chatik_frame_url, "matched": True}]
                isolated_world_ok = False
                errors.append(f"Isolated world evaluation failed: {e}")
        else:
            # Live WebSocket probe
            try:
                probe_res = prefill_execute.probe_frames_and_world(cdp, ws_url, f_subs)
                frames = probe_res.get("frames", [])
                chatik_frame_found = bool(probe_res.get("chatik_frame_found", False))
                chatik_frame_url = probe_res.get("chatik_frame_url")
                isolated_world_ok = bool(probe_res.get("isolated_world_ok", False))
                if not chatik_frame_found:
                    errors.append(f"Chatik frame matching {f_subs} not found in frame tree")
                elif not isolated_world_ok:
                    errors.append("Isolated world creation or 1+1 test evaluation failed")
            except Exception as e:
                errors.append(f"Frame tree / isolated world probe failed: {e}")
                chatik_frame_found = False
                isolated_world_ok = False

    # Step 5: Conversation DOM
    if page_is_messages and chatik_frame_found and isolated_world_ok:
        try:
            chatik_ev = _resolve_chatik_evaluate(cdp_url=cdp, url_substring=url_sub, evaluate_fn=evaluate_fn, isolate_substrings=f_subs)
            conv_res = hh_message_reply.fetch_hh_conversation_readonly(chatik_ev)
            if conv_res.get("error"):
                conversation_dom_ok = False
                errors.append(f"Conversation DOM extraction error: {conv_res.get('error')}")
            else:
                conversation_dom_ok = True
                conversation_id = conv_res.get("conversation_id")
                composer_present = bool(conv_res.get("composer_present", False))
                messages = conv_res.get("messages") or []
                message_count = len(messages)
                if message_count == 0:
                    errors.append("Conversation DOM is accessible, but 0 messages were found (open conversation might be empty)")
        except Exception as e:
            conversation_dom_ok = False
            errors.append(f"Conversation DOM evaluation failed: {e}")

    # Determine Verdict in strict precedence order
    if not cdp_reachable:
        verdict = "CDP_UNAVAILABLE"
        hint = "start Chrome with --remote-debugging-port=9222"
    elif not hh_page_present:
        verdict = "HH_NOT_OPEN"
        hint = "open hh.ru in the CDP browser"
    elif not page_is_messages:
        verdict = "HH_WRONG_PAGE"
        hint = "open the hh.ru messages page (dialog list) in the tab"
    elif not chatik_frame_found:
        verdict = "CHATIK_FRAME_ABSENT"
        hint = "open a specific conversation in the hh.ru tab"
    elif not isolated_world_ok:
        verdict = "ISOLATED_WORLD_UNAVAILABLE"
        hint = "chatik frame exists but CDP could not create an isolated world"
    elif not conversation_dom_ok:
        verdict = "CONVERSATION_DOM_INACCESSIBLE"
        hint = "chatik iframe reachable but message DOM could not be read"
    elif message_count == 0:
        verdict = "NO_MESSAGES"
        hint = "conversation is open but empty (or DOM selectors changed — compare with Stage 24 evidence)"
    else:
        verdict = "HEALTHY"
        hint = "preview/classify should work on this tab."

    from datetime import datetime, timezone
    checked_at = datetime.now(timezone.utc).isoformat()

    payload = {
        "cdp_reachable": cdp_reachable,
        "matching_tabs": matching_tabs,
        "hh_page_present": hh_page_present,
        "page_url": page_url,
        "page_title": page_title,
        "page_is_messages": page_is_messages,
        "frames": frames,
        "chatik_frame_found": chatik_frame_found,
        "chatik_frame_url": chatik_frame_url,
        "isolated_world_ok": isolated_world_ok,
        "conversation_dom_ok": conversation_dom_ok,
        "conversation_id": conversation_id,
        "composer_present": composer_present,
        "message_count": message_count,
        "dialogs_visible": dialogs_visible,
        "errors": errors,
        "verdict": verdict,
        "checked_at": checked_at,
    }

    if as_json:
        import json
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    # Human format (§4)
    import json
    print("[hh-message] diagnose (READ-ONLY probe)")
    if cdp_reachable:
        t_count = len(target_list) if targets is None else len(targets)
        print(f"cdp:            {cdp} — reachable ({t_count} targets)")
    else:
        err_msg = errors[0] if errors else "unreachable"
        print(f"cdp:            {cdp} — unreachable ({err_msg})")

    if hh_page_present:
        print(f"hh tab:         YES — {page_title!r} ({page_url})")
    elif not cdp_reachable:
        print("hh tab:         NO (n/a)")
    else:
        print(f"hh tab:         NO — no tab matching {url_sub!r}")

    if page_is_messages is True:
        print("page is messages: YES")
    elif page_is_messages is False:
        print("page is messages: NO")
    else:
        print("page is messages: n/a")

    if frames:
        matched_str = f"chatik matched: {chatik_frame_url}" if chatik_frame_found else "chatik matched: NO"
        print(f"frames:         {len(frames)} ({matched_str})")
    elif chatik_frame_found is False and page_is_messages:
        print("frames:         0 (chatik matched: NO)")
    else:
        print("frames:         n/a")

    if isolated_world_ok is True:
        print("isolated world: OK (executionContextId acquired, evaluate 1+1 = 2)")
    elif isolated_world_ok is False:
        print("isolated world: NO (creation failed or 1+1 test failed)")
    else:
        print("isolated world: n/a")

    if conversation_dom_ok is True:
        comp_str = "YES" if composer_present else "NO"
        print(f"conversation DOM: OK — conversation_id={conversation_id}, messages={message_count}, composer={comp_str}")
    elif conversation_dom_ok is False:
        print("conversation DOM: NO (failed to read conversation DOM)")
    else:
        print("conversation DOM: n/a")

    if dialogs_visible is not None:
        print(f"dialogs visible: {dialogs_visible}")
    else:
        print("dialogs visible: n/a")

    if errors:
        print(f"errors:         {'; '.join(errors)}")
    else:
        print("errors:         none")

    print(f"verdict: {verdict} — {hint}")
    print("status: READ-ONLY — nothing sent.")
    return 0


# ---------------------------------------------------------------------------
# Stage 30C — system-info diagnostic (READ-ONLY, no network, no DB writes,
# no env mutation, no HH/Gmail/email send). Safe standalone handler.
# ---------------------------------------------------------------------------

def system_info() -> int:
    """Print environment/app diagnostics. READ-ONLY: no network, no DB, no send."""
    import platform as _platform
    import importlib as _importlib

    print(f"Python: {sys.version}")
    print(f"python_version: {_platform.python_version()}")
    print(f"platform: {_platform.platform()}")

    # Detect app_version only if a genuine version constant exists; do not invent one.
    _app_version = None
    for _mod_name, _attr in (
        ("ai_assistant", "__version__"),
        ("ai_assistant", "APP_VERSION"),
        ("ai_assistant", "__VERSION__"),
        ("ai_assistant.config", "__version__"),
        ("ai_assistant.config", "APP_VERSION"),
    ):
        try:
            _mod = _importlib.import_module(_mod_name)
            _val = getattr(_mod, _attr, None)
            if isinstance(_val, str) and _val.strip():
                _app_version = _val.strip()
                break
        except Exception:
            continue
    if _app_version:
        print(f"app_version: {_app_version}")
    else:
        print("app_version: (not defined - none found)")
    print("status: READ-ONLY")
    return 0


def ui_cmd(host: str = "127.0.0.1", port: int = 8000) -> int:
    """Launch the interactive web dashboard."""
    try:
        import uvicorn
        print(f"Starting Job-Search Web Dashboard on http://{host}:{port}")
        uvicorn.run("ai_assistant.ui.app:app", host=host, port=port, reload=False)
        return 0
    except Exception as e:
        print(f"Failed to launch Web UI: {e}", file=sys.stderr)
        return 1


def watch_cmd(
    sources: Optional[List[str]] = None,
    interval: int = 60,
    once: bool = False,
    limit: int = 20,
    candidate_country: str = "TH",
    profile_path: Optional[str] = None,
    output_json: bool = False,
) -> int:
    """Run the controlled application watcher (READ-ONLY review queueing; NO auto-submit)."""
    from .watcher import Watcher, WatcherConfig

    cfg = WatcherConfig(
        sources=sources if sources else list(SOURCES.keys()),
        poll_interval_seconds=interval,
        max_iterations=1 if once else None,
        candidate_country=candidate_country,
        profile_path=profile_path,
        batch_limit=limit,
    )

    watcher = Watcher(cfg)

    if once:
        res = watcher.poll_once(iteration=1)
        if output_json:
            print(res.model_dump_json(indent=2))
            return 0

        print("\n=======================================================")
        print("   CONTROLLED APPLICATION WATCHER REPORT (POLL CYCLE)")
        print("=======================================================")
        print(f"Timestamp:              {res.timestamp}")
        print(f"Sources polled:         {', '.join(cfg.sources)}")
        print(f"Candidate location:     {cfg.candidate_country}")
        print("-------------------------------------------------------")
        print(f"  [+] Fetched vacancies:          {res.fetched_count:4d}")
        print(f"  [+] New unique vacancies:       {res.new_vacancies_count:4d}")
        print(f"  [=] Duplicates ignored:         {res.duplicate_count:4d}")
        print(f"  [-] Rejected by constraints:    {res.rejected_count:4d}")
        print(f"  [*] Matched (APPLY/REVIEW):     {res.matched_count:4d}")
        print(f"  [*] Deep Analyzed:              {res.analyzed_count:4d}")
        print(f"  [*] Application Prepared:       {res.prepared_count:4d}")
        print("-------------------------------------------------------")
        print(f"  [!] READY FOR HUMAN REVIEW:     {res.ready_for_review_count:4d}")
        print(f"  [?] NEEDS HUMAN REVIEW:         {res.needs_human_review_count:4d}")
        print(f"  [X] BLOCKED (fail-closed):      {res.blocked_count:4d}")
        print("-------------------------------------------------------")
        print("Safety Invariants:")
        print("  SUBMIT CLICKED:                 NO (0)")
        print("  APPLICATION SENT:               NO (0)")
        print("  HUMAN APPROVAL BYPASS:          NONE")
        print("=======================================================\n")

        if res.items:
            print("Items queued for review:")
            for idx, it in enumerate(res.items, 1):
                print(f"  {idx}. [{it.status}] {it.vacancy_stable_id} | {it.company} - {it.title}")
                print(f"     URL: {it.url}")
                print(f"     Match: {it.match_decision} ({it.match_score}) | Deep Fit: {it.deep_fit_score}")
                if it.why_fit:
                    print(f"     Why fit: {'; '.join(it.why_fit[:2])}")
                if it.prepared_answers:
                    print(f"     Prepared answers: {len(it.prepared_answers)} fields")
                if it.unresolved_questions:
                    print(f"     Unresolved/review items: {', '.join(it.unresolved_questions[:3])}")
                print(f"     Stop reason: {it.stop_reason}")
                print()
        return 0
    else:
        print(f"Starting controlled application watcher (interval: {interval}s). Press Ctrl+C to stop.")
        try:
            watcher.run()
        except KeyboardInterrupt:
            print("\nWatcher stopped by user.")
        return 0


def message_watch_cmd(
    cdp_url: Optional[str] = None,
    url_substring: Optional[str] = None,
    interval: int = 60,
    once: bool = False,
    continuous: bool = False,
    limit: int = 20,
    iterations: Optional[int] = None,
    profile_path: Optional[str] = None,
    output_json: bool = False,
    evaluate_fn: Optional[Any] = None,
    stop_callback: Optional[Callable[[], bool]] = None,
) -> int:
    """Run the controlled HH message watcher (READ-ONLY review queueing; NO auto-send)."""
    from .hh_message_watcher import HHMessageWatcher, HHMessageWatcherConfig

    is_once = once and not continuous
    cfg = HHMessageWatcherConfig(
        cdp_url=cdp_url,
        url_substring=url_substring,
        poll_interval_seconds=interval,
        max_iterations=1 if is_once else iterations,
        batch_limit=limit,
        profile_path=profile_path,
        custom_evaluate_fn=evaluate_fn,
    )

    watcher = HHMessageWatcher(cfg)

    if is_once:
        res = watcher.poll_once(iteration=1)
        if output_json:
            print(res.model_dump_json(indent=2))
            return 0

        print("\n=======================================================")
        print("   CONTROLLED HH MESSAGE WATCHER REPORT (POLL CYCLE)")
        print("=======================================================")
        print(f"Timestamp:                 {res.timestamp}")
        print("Mode:                      READ-ONLY (stops at Human Review)")
        print("-------------------------------------------------------")
        print(f"  [+] Conversations checked:       {res.conversations_checked:4d}")
        print(f"  [+] Messages seen:               {res.messages_seen:4d}")
        print(f"  [+] New incoming messages:       {res.new_messages:4d}")
        print(f"  [=] Already processed:           {res.already_processed:4d}")
        print(f"  [*] Replies prepared:            {res.replies_prepared:4d}")
        print("-------------------------------------------------------")
        print(f"  [!] READY FOR HUMAN REVIEW:      {res.ready_for_human_review:4d}")
        print(f"  [?] NEEDS HUMAN REVIEW:          {res.needs_human_review:4d}")
        print(f"  [X] BLOCKED / Errors:            {res.blocked:4d}")
        print("-------------------------------------------------------")
        print("Safety Invariants:")
        print(f"  REPLY SENT:                      NO ({res.replies_sent})")
        print(f"  DUPLICATE REPLY:                 NO ({res.duplicate_reply_count})")
        print("  HUMAN APPROVAL BYPASS:           NONE")
        print("=======================================================\n")

        if res.items:
            print("Message Items:")
            for idx, it in enumerate(res.items, 1):
                print(f"  {idx}. [{it.status}] Conv: {it.conversation_id} | From: {it.sender} ({it.employer or it.participant or 'Unknown'})")
                print(f"     Text: {it.text[:80] + ('...' if len(it.text) > 80 else '')}")
                print(f"     Class: {it.classification} (conf: {it.confidence}) | Validation: {it.validation}")
                if it.reply_draft:
                    print(f"     Draft: {it.reply_draft[:80] + ('...' if len(it.reply_draft) > 80 else '')}")
                print(f"     Stop reason: {it.stop_reason}")
                print()
        if res.errors:
            print("Errors encountered:")
            for err in res.errors:
                print(f"  - {err}")
        return 0
    else:
        try:
            watcher.run(stop_callback=stop_callback)
        except KeyboardInterrupt:
            print("\nMessage watcher stopped by user.")
        return 0


def questionnaire_list_cmd(status: Optional[str] = None, limit: int = 50) -> int:
    """List stored questionnaires."""
    from . import db
    db.init_db()
    items = db.list_hh_questionnaires(status=status, limit=limit)
    if not items:
        print("No stored questionnaires found.")
        return 0
    print(f"\n{'ID':<22} | {'STATUS':<22} | {'VACANCY / CONV':<35} | {'QUESTIONS':<10}")
    print("-" * 96)
    for it in items:
        target = it.get("vacancy_stable_id") or it.get("conversation_id") or "N/A"
        q_count = len(it.get("questions") or [])
        print(f"{it['questionnaire_id']:<22} | {it['status']:<22} | {target:<35} | {q_count:<10}")
    print()
    return 0


def questionnaire_show_cmd(target_id: str) -> int:
    """Show questionnaire details."""
    from . import db
    from .hh_questionnaire import HHQuestionnaire, format_questionnaire_cli_output
    db.init_db()
    data = db.get_hh_questionnaire(target_id)
    if not data:
        data = db.get_hh_questionnaire_by_vacancy(target_id)
    if not data:
        data = db.get_hh_questionnaire_by_conversation(target_id)
    if not data:
        print(f"Error: Questionnaire not found for '{target_id}'", file=sys.stderr)
        return 1
    q = HHQuestionnaire(**data)
    print()
    print(format_questionnaire_cli_output(q))
    if q.answers:
        print("\nRecorded Human Answers:")
        for k, v in q.answers.items():
            print(f"  {k}: {v}")
    print()
    return 0


def questionnaire_suggest_cmd(target_id: str, apply_answers: bool = False) -> int:
    """Generate and display smart tailored questionnaire answer suggestions."""
    import json
    from . import db
    from .hh_questionnaire import HHQuestionnaire, generate_suggested_answers, validate_human_answers
    db.init_db()
    data = db.get_hh_questionnaire(target_id)
    if not data:
        data = db.get_hh_questionnaire_by_vacancy(target_id)
    if not data:
        data = db.get_hh_questionnaire_by_conversation(target_id)
    if not data:
        print(f"Error: Questionnaire not found for '{target_id}'", file=sys.stderr)
        return 1
    quest = HHQuestionnaire(**data)
    suggested = generate_suggested_answers(quest)

    print("\n-------------------------------------------------------")
    print("TAILORED QUESTIONNAIRE SUGGESTIONS (FROM RESUME & PROFILE)")
    print("-------------------------------------------------------")
    print(f"Vacancy:       {quest.title or quest.vacancy_stable_id or 'N/A'}")
    print(f"Questionnaire: {quest.questionnaire_id}\n")

    for q in quest.questions:
        ans = suggested.get(q.question_id)
        req_marker = "[required]" if q.required else "[optional]"
        print(f"{q.question_id} {req_marker}: {q.text}")
        print(f"  Suggested Answer: {ans}\n")

    val = validate_human_answers(quest, suggested)
    print("-------------------------------------------------------")
    print(f"Validation: {'APPROVED' if val.ok else 'INVALID'}")
    if not val.ok:
        print(f"Reason:     {val.reason}")
    print("-------------------------------------------------------")

    if apply_answers and val.ok:
        db.update_hh_questionnaire_answers(quest.questionnaire_id, suggested, new_status=val.status)
        print(f"[+] Suggested answers automatically applied to {quest.questionnaire_id}!")
        print(f"Status updated to: {val.status}")
        print(f"Ready for submit with: python -m ai_assistant.cli questionnaire submit {quest.questionnaire_id} --confirm-submit\n")
    elif not apply_answers:
        print("To apply these suggestions, run:")
        print(f"  python -m ai_assistant.cli questionnaire suggest {quest.questionnaire_id} --apply\n")
    return 0


def questionnaire_answer_cmd(
    target_id: str,
    answers_json: Optional[str] = None,
    single_answers: Optional[List[str]] = None,
) -> int:
    """Validate and record human answers for a questionnaire."""
    import json
    from . import db
    from .hh_questionnaire import HHQuestionnaire, validate_human_answers
    db.init_db()
    data = db.get_hh_questionnaire(target_id)
    if not data:
        data = db.get_hh_questionnaire_by_vacancy(target_id)
    if not data:
        data = db.get_hh_questionnaire_by_conversation(target_id)
    if not data:
        print(f"Error: Questionnaire not found for '{target_id}'", file=sys.stderr)
        return 1

    quest = HHQuestionnaire(**data)
    answers: Dict[str, Any] = dict(quest.answers or {})
    
    if answers_json:
        try:
            parsed = json.loads(answers_json)
            if isinstance(parsed, dict):
                answers.update(parsed)
            else:
                print("Error: --answers must be a JSON object mapping question_id -> answer", file=sys.stderr)
                return 1
        except Exception as e:
            print(f"Error: Failed to parse JSON answers: {e}", file=sys.stderr)
            return 1

    for pair in (single_answers or []):
        if "=" in pair:
            k, v = pair.split("=", 1)
            answers[k.strip()] = v.strip()
        else:
            print(f"Warning: Ignoring malformed answer pair '{pair}' (expected key=value)", file=sys.stderr)

    val = validate_human_answers(quest, answers)
    if not val.ok:
        print(f"\n[!] Questionnaire Answers Validation FAILED: {val.reason}")
        print(f"Status: {val.status}")
        db.update_hh_questionnaire_answers(quest.questionnaire_id, answers, new_status=val.status)
        return 1

    db.update_hh_questionnaire_answers(quest.questionnaire_id, answers, new_status=val.status)
    print(f"\n[+] Answers successfully recorded and validated for questionnaire {quest.questionnaire_id}!")
    print(f"Status: {val.status}")
    print(f"Ready to submit with: python -m ai_assistant.cli questionnaire submit {quest.questionnaire_id} --confirm-submit\n")
    return 0


def questionnaire_submit_cmd(
    target_id: str,
    confirm_submit: bool = False,
    answers_json: Optional[str] = None,
    evaluate_fn: Optional[Any] = None,
) -> int:
    """Submit questionnaire response with explicit human confirmation."""
    import json
    from . import db
    from .hh_questionnaire import HHQuestionnaire, submit_questionnaire_response
    db.init_db()
    data = db.get_hh_questionnaire(target_id)
    if not data:
        data = db.get_hh_questionnaire_by_vacancy(target_id)
    if not data:
        data = db.get_hh_questionnaire_by_conversation(target_id)
    if not data:
        print(f"Error: Questionnaire not found for '{target_id}'", file=sys.stderr)
        return 1

    quest = HHQuestionnaire(**data)
    answers: Dict[str, Any] = dict(quest.answers or {})
    if answers_json:
        try:
            parsed = json.loads(answers_json)
            if isinstance(parsed, dict):
                answers.update(parsed)
        except Exception as e:
            print(f"Error parsing JSON answers: {e}", file=sys.stderr)
            return 1

    if not confirm_submit:
        print("\n=======================================================")
        print("   SUBMISSION GATE: EXPLICIT CONFIRMATION REQUIRED")
        print("=======================================================")
        print(f"Questionnaire:             {quest.questionnaire_id}")
        print(f"Vacancy / Conv:            {quest.vacancy_stable_id or quest.conversation_id or 'N/A'}")
        print(f"Status:                    READY_TO_SUBMIT (gated)")
        print("Submit Action:             BLOCKED (Submit = 0)")
        print("-------------------------------------------------------")
        print("To proceed with actual submit, run:")
        print(f"  python -m ai_assistant.cli questionnaire submit {quest.questionnaire_id} --confirm-submit")
        print("=======================================================\n")
        return 1

    if evaluate_fn is None:
        try:
            from .hh_browser_launcher import ensure_hh_browser
            from .hh_vacancy_navigator import ensure_open_vacancy_tab
            ensure_hh_browser()
            ensure_open_vacancy_tab(_DEFAULT_HH_CDP_URL, quest.vacancy_stable_id or quest.questionnaire_id)
        except Exception as e:
            logger.debug(f"ensure_open_vacancy_tab error: {e}")
        
        vac_sub = quest.vacancy_stable_id.split(":")[-1] if quest.vacancy_stable_id else "vacancy"
        evaluate_fn = _resolve_hh_evaluate(_DEFAULT_HH_CDP_URL, vac_sub)
        if not evaluate_fn:
            evaluate_fn = _resolve_hh_evaluate(_DEFAULT_HH_CDP_URL, "hh.ru")

    res = submit_questionnaire_response(
        questionnaire_id=quest.questionnaire_id,
        human_answers=answers,
        evaluate_fn=evaluate_fn,
        confirm_submit=confirm_submit,
    )

    print("\n=======================================================")
    print("   HH QUESTIONNAIRE SUBMISSION REPORT")
    print("=======================================================")
    print(f"Questionnaire:             {res.questionnaire_id}")
    print(f"Verdict:                   {res.verdict}")
    print(f"Status:                    {res.status}")
    print(f"Submit Count:              {res.submit_count}")
    print(f"Details:                   {res.reason}")
    if res.errors:
        print("Errors:")
        for err in res.errors:
            print(f"  - {err}")
    print("=======================================================\n")
    return 0 if res.verdict in ("SUBMITTED", "ALREADY_SUBMITTED") else 1


def application_submit_cmd(
    target_id: str,
    confirm_submit: bool = False,
    answers_json: Optional[str] = None,
    evaluate_fn: Optional[Callable[[str], str]] = None,
) -> int:
    """Submit an HH application with mandatory confirmation gate."""
    from . import db
    from .hh_application_orchestrator import transition_application, HHApplicationState
    db.init_db()
    data = db.get_hh_application(target_id)
    if not data:
        data = db.get_hh_application_by_vacancy(target_id)
    if not data:
        data = db.get_hh_application_by_conversation(target_id)
    if not data:
        print(f"Error: HH Application not found for '{target_id}'", file=sys.stderr)
        return 1

    app_id = data.get("application_id", target_id)
    current_state = data.get("state")
    qid = data.get("questionnaire_id")

    if current_state == "SUBMITTED":
        print(f"\n=======================================================", file=sys.stderr)
        print(f"   SUBMISSION BLOCKED: APPLICATION ALREADY SUBMITTED", file=sys.stderr)
        print(f"=======================================================", file=sys.stderr)
        print(f"Application:               {app_id}", file=sys.stderr)
        print(f"Current State:             SUBMITTED", file=sys.stderr)
        print(f"Reason:                    application_already_submitted", file=sys.stderr)
        print(f"Submit Action:             BLOCKED (Submit = 0)", file=sys.stderr)
        print(f"=======================================================\n", file=sys.stderr)
        return 1

    if current_state != "READY_TO_SUBMIT":
        print(f"\n[!] Submission Blocked: Application {app_id} is in state '{current_state}' (must be 'READY_TO_SUBMIT')", file=sys.stderr)
        return 1

    if not confirm_submit:
        print("\n=======================================================")
        print("   SUBMISSION GATE: EXPLICIT CONFIRMATION REQUIRED")
        print("=======================================================")
        print(f"Application:               {app_id}")
        print(f"Vacancy:                   {data.get('title') or 'N/A'}")
        print(f"Status:                    READY_TO_SUBMIT (gated)")
        print("Submit Action:             BLOCKED (Submit = 0)")
        print("-------------------------------------------------------")
        print("To proceed with actual submit, run:")
        print(f"  python -m ai_assistant.cli application submit {app_id} --confirm-submit")
        print("=======================================================\n")
        return 1

    # If questionnaire exists, route via questionnaire submit
    if qid:
        ret = questionnaire_submit_cmd(
            target_id=qid,
            confirm_submit=confirm_submit,
            answers_json=answers_json,
            evaluate_fn=evaluate_fn,
        )
        if ret == 0:
            transition_application(
                application_id=app_id,
                to_state=HHApplicationState.SUBMITTED,
                reason="questionnaire_submitted_with_human_confirmation",
                evidence={"questionnaire_id": qid},
                confirm_submit=confirm_submit,
            )
        else:
            q_after = db.get_hh_questionnaire(qid)
            q_status = q_after.get("status") if q_after else None
            to_st = HHApplicationState.BLOCKED if q_status == "BLOCKED" else HHApplicationState.FAILED
            transition_application(
                application_id=app_id,
                to_state=to_st,
                reason="submit_blocked_pre_execution" if to_st == HHApplicationState.BLOCKED else "submit_failed_during_browser_execution",
                evidence={"questionnaire_id": qid, "questionnaire_status": q_status},
            )
        return ret

    # Direct reply or message submit
    cid = data.get("conversation_id")
    if cid:
        from .hh_message_reply import send_hh_reply_confirmed
        if evaluate_fn is None:
            evaluate_fn = _resolve_hh_evaluate(_DEFAULT_HH_CDP_URL, _DEFAULT_HH_MESSAGES_URL_SUBSTRING)
        if not evaluate_fn:
            print("Error: Could not connect to HH Chrome CDP", file=sys.stderr)
            transition_application(app_id, HHApplicationState.FAILED, reason="cdp_connection_failed")
            return 1
        res_reply = send_hh_reply_confirmed(cid, reply_text=data.get("reply_draft", ""), evaluate_fn=evaluate_fn)
        if res_reply.get("success"):
            transition_application(app_id, HHApplicationState.SUBMITTED, reason="reply_sent_with_human_confirmation", confirm_submit=confirm_submit)
            return 0
        else:
            transition_application(app_id, HHApplicationState.FAILED, reason="reply_send_failed")
            return 1

    print(f"Application {app_id} has no questionnaire or message to submit.", file=sys.stderr)
    return 1


def application_list_cmd(state: Optional[str] = None, limit: int = 50) -> int:
    """List stored HH applications with their current state."""
    from . import db
    db.init_db()
    apps = db.list_hh_applications(state=state, limit=limit)
    if not apps:
        print("No stored HH applications found.")
        return 0
    print(f"\n{'APPLICATION ID':<24} | {'STATE':<24} | {'CONVERSATION':<16} | {'VACANCY':<28} | {'SUBMIT ALLOWED':<14}")
    print("-" * 115)
    for a in apps:
        app_id = a.get("application_id", "")
        cur_state = a.get("state", "NEW")
        conv = a.get("conversation_id") or "N/A"
        vac = (a.get("title") or a.get("vacancy_stable_id") or "N/A")[:26]
        allowed = "YES" if cur_state == "READY_TO_SUBMIT" else "NO"
        print(f"{app_id:<24} | {cur_state:<24} | {conv:<16} | {vac:<28} | {allowed:<14}")
    print()
    return 0


def application_show_cmd(target_id: str) -> int:
    """Show detailed status and human action required for an HH application."""
    from . import db
    from .hh_application_orchestrator import HHApplication, format_application_cli_output
    db.init_db()
    data = db.get_hh_application(target_id)
    if not data:
        data = db.get_hh_application_by_conversation(target_id)
    if not data:
        data = db.get_hh_application_by_vacancy(target_id)
    if not data:
        print(f"Error: HH Application not found for '{target_id}'", file=sys.stderr)
        return 1
    app = HHApplication(**data)
    print()
    print(format_application_cli_output(app))
    return 0


def application_transitions_cmd(target_id: str, limit: int = 100) -> int:
    """Show chronological state transition audit trail for an HH application."""
    import json
    from . import db
    db.init_db()
    data = db.get_hh_application(target_id)
    if not data:
        data = db.get_hh_application_by_conversation(target_id)
    if not data:
        data = db.get_hh_application_by_vacancy(target_id)
    if not data:
        print(f"Error: HH Application not found for '{target_id}'", file=sys.stderr)
        return 1
    app_id = data["application_id"]
    transitions = db.list_hh_application_transitions(app_id, limit=limit)
    if not transitions:
        print(f"No transition history recorded for application '{app_id}'.")
        return 0
    print("\n-------------------------------------------------------")
    print("HH APPLICATION TRANSITIONS AUDIT TRAIL")
    print(f"Application:   {app_id}")
    print(f"Current State: {data.get('state', 'UNKNOWN')}")
    print("-------------------------------------------------------")
    for idx, t in enumerate(transitions, 1):
        prev = t.get("previous_state") or "INITIAL"
        curr = t.get("state", "UNKNOWN")
        ts = t.get("created_at", "")
        reason = t.get("reason", "")
        print(f"{idx}. [{ts}] {prev} -> {curr}")
        print(f"   Reason: {reason}")
        if t.get("evidence"):
            ev_str = json.dumps(t["evidence"], ensure_ascii=False)
            if len(ev_str) > 90:
                ev_str = ev_str[:87] + "..."
            print(f"   Evidence: {ev_str}")
        print()
    print("-------------------------------------------------------\n")
    return 0


def questionnaire_audit_cmd(target_id: str) -> int:
    """Run a pre-submit audit on questionnaire answers against candidate profile and facts."""
    from .hh_questionnaire_audit import audit_questionnaire
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
    from . import db
    from .hh_questionnaire_audit import audit_questionnaire
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


def application_status_cmd(target_id: str) -> int:
    """Show brief application status."""
    return application_show_cmd(target_id)


def application_verify_submit_cmd(target_id: str, evaluate_fn: Optional[Callable[[str], str]] = None) -> int:
    """Verify factual post-submit status of an application on HeadHunter."""
    from .hh_post_submit_verifier import verify_hh_submitted_application
    res = verify_hh_submitted_application(target_id, evaluate_fn=evaluate_fn)

    print("\n=======================================================")
    print("   HH APPLICATION POST-SUBMIT VERIFICATION")
    print("=======================================================")
    print(f"Application:               {res.application_id}")
    print(f"Vacancy URL:               {res.vacancy_url or 'N/A'}")
    print(f"Current State:             {res.current_state}")
    print(f"HH Status:                 {res.hh_status}")
    print(f"Evidence:                  {res.evidence_text or 'N/A'}")
    print(f"Verification:              {res.verification_verdict}")
    print(f"Timestamp:                 {res.timestamp}")
    print(f"Real Submit Count:         {res.submit_count}")
    print(f"Details:                   {res.reason}")
    print("=======================================================\n")
    return 0 if res.verification_verdict == "PASS" else 1


def application_queue_cmd(as_json: bool = False, ready_only: bool = False, human_review_only: bool = False) -> int:
    """Show controlled HH application queue (Stage 45)."""
    import json
    from .hh_application_queue import (
        get_controlled_application_queue,
        format_queue_cli,
        format_ready_queue_cli,
        format_human_review_queue_cli,
    )
    filter_mode = None
    if ready_only:
        filter_mode = "ready"
    elif human_review_only:
        filter_mode = "human_review"

    items = get_controlled_application_queue(filter_mode=filter_mode)

    if as_json:
        print(json.dumps([item.model_dump() for item in items], indent=2, ensure_ascii=False))
        return 0

    if ready_only:
        print(format_ready_queue_cli(items))
    elif human_review_only:
        print(format_human_review_queue_cli(items))
    else:
        print(format_queue_cli(items))
    return 0


def application_runner_cmd(
    command: str,
    confirm_submit: bool = False,
    as_json: bool = False,
    evaluate_fn: Optional[Callable[[str], str]] = None,
) -> int:
    """Execute controlled application runner command (Stage 46)."""
    import json
    from .hh_application_runner import (
        preview_next_application,
        run_next_application,
        format_runner_result_cli,
    )
    if command == "preview":
        res = preview_next_application()
    elif command == "next":
        if evaluate_fn is None:
            try:
                from .cli import _resolve_hh_evaluate, _DEFAULT_HH_CDP_URL
                from .hh_browser_launcher import ensure_hh_browser
                ensure_hh_browser()
                evaluate_fn = _resolve_hh_evaluate(_DEFAULT_HH_CDP_URL, "hh.ru")
            except Exception:
                evaluate_fn = None
        res = run_next_application(confirm_submit=confirm_submit, evaluate_fn=evaluate_fn)
    else:
        print(f"Unknown runner command: {command}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(res.model_dump(), indent=2, ensure_ascii=False))
        return 0

    print(format_runner_result_cli(res))
    return 0

def export_digest_cmd(
    format_type: str = "telegram",
    limit: int = 10,
    min_score: float = 60.0,
    profile_path: Optional[str] = None,
    output_json: bool = False,
    mark_delivered: bool = False,
    include_legacy: bool = False,
) -> int:
    """Export validated vacancy digest from state.db (Stage 75, Stage 78).
    
    Pure read-only query against state.db by default.
    Selects fresh undigested vacancies (or all if include_legacy=True).
    Applies CandidateProfile hard constraints and matching score.
    Outputs structured Markdown for Telegram @remotejobd or JSON.
    Only marks delivered if mark_delivered=True (explicit atomic delivery).
    """
    try:
        init_db()
    except Exception as e:
        print(f"Failed to open vacancy DB: {e}", file=sys.stderr)
        return 1

    try:
        if profile_path:
            profile = load_candidate_profile(profile_path)
        else:
            profile = load_candidate_profile()
    except Exception:
        profile = None

    if include_legacy:
        rows = list_vacancies(limit=5000)
        vacancies = [_row_to_vacancy(row) for row in rows if row]
    else:
        vacancies = list_undigested_vacancies(limit=5000)

    matcher = JobMatcher(profile) if profile is not None else None
    matched_candidates = []

    for v in vacancies:
        if not include_legacy:
            from .schema import is_genuine_production_vacancy
            is_gen, _ = is_genuine_production_vacancy(v)
            if not is_gen:
                continue

        if matcher is not None:
            m_res = matcher.match(v)
            score = m_res.score
            decision = m_res.decision
            decision_class = getattr(m_res, "decision_class", "MATCH" if score >= 75 else "BORDERLINE")
            eligibility = getattr(m_res, "eligibility", "ELIGIBLE")
            role_family = getattr(m_res, "role_family", "OTHER")
            role_priority = getattr(m_res, "role_priority", "P1")
            reasons = m_res.reasons
        else:
            score = v.match_score if v.match_score is not None else 70.0
            decision = v.match_decision if v.match_decision else "APPLY"
            decision_class = "MATCH" if score >= 75 else "BORDERLINE"
            eligibility = "ELIGIBLE"
            role_family = "OTHER"
            role_priority = "P1"
            reasons = [v.match_reasons] if v.match_reasons else []

        if decision in ("APPLY", "REVIEW") and score >= min_score:
            matched_candidates.append({
                "score": score,
                "decision": decision,
                "decision_class": decision_class,
                "eligibility": eligibility,
                "role_family": role_family,
                "role_priority": role_priority,
                "vacancy": v,
                "reasons": reasons,
            })

    # Ranking priority:
    # 1. Decision class (STRONG_MATCH > MATCH > STRETCH > BORDERLINE > REJECT)
    # 2. Role priority (P1 > P2 > P3)
    # 3. Score descending
    # 4. Eligibility confidence (ELIGIBLE > BORDERLINE)
    # 5. Recency (published_at / first_seen_at)
    decision_class_rank = {
        "STRONG_MATCH": 5,
        "MATCH": 4,
        "STRETCH": 3,
        "BORDERLINE": 2,
        "REJECT": 1,
    }
    role_priority_rank = {
        "P1": 3,
        "P2": 2,
        "P3": 1,
        "NOT_TARGET": 0,
    }
    eligibility_rank = {
        "ELIGIBLE": 2,
        "BORDERLINE": 1,
        "INELIGIBLE": 0,
    }

    matched_candidates.sort(
        key=lambda x: (
            decision_class_rank.get(x["decision_class"], 0),
            role_priority_rank.get(x.get("role_priority", "P1"), 0),
            x["score"],
            eligibility_rank.get(x["eligibility"], 0),
            str(x["vacancy"].published_at or x["vacancy"].first_seen_at or "")
        ),
        reverse=True
    )

    # Diversity & Duplicate Role Control:
    # - Cap max 2 per company
    # - Cap max 4 per role family (unless score >= 90)
    # - Deduplicate near-identical title + company
    company_counts: Dict[str, int] = {}
    family_counts: Dict[str, int] = {}
    seen_normalized_keys: Set[str] = set()

    top_items = []
    for cand in matched_candidates:
        if len(top_items) >= limit:
            break
        v = cand["vacancy"]
        comp = (v.company or "").strip().lower()
        fam = cand["role_family"]
        norm_title = re.sub(r"[^\w\s]", "", (v.title or "").lower()).strip()
        norm_key = f"{comp}::{norm_title}"

        if norm_key in seen_normalized_keys:
            continue

        if comp and company_counts.get(comp, 0) >= 2:
            continue

        if fam and fam != "OTHER" and family_counts.get(fam, 0) >= 4 and cand["score"] < 90:
            continue

        company_counts[comp] = company_counts.get(comp, 0) + 1
        family_counts[fam] = family_counts.get(fam, 0) + 1
        seen_normalized_keys.add(norm_key)
        top_items.append((cand["score"], v, cand["reasons"]))


    formatted_items = []
    for score, vac, reasons in top_items:
        sal = "не указана"
        if vac.salary_min is not None and vac.salary_max is not None:
            curr = vac.salary_currency or "USD"
            if vac.salary_min == vac.salary_max:
                sal = f"${vac.salary_min:,.0f} {curr}" if curr == "USD" else f"{vac.salary_min:,.0f} {curr}"
            else:
                sal = f"${vac.salary_min:,.0f} - ${vac.salary_max:,.0f} {curr}" if curr == "USD" else f"{vac.salary_min:,.0f} - {vac.salary_max:,.0f} {curr}"
        elif vac.salary_min is not None:
            curr = vac.salary_currency or "USD"
            sal = f"от ${vac.salary_min:,.0f} {curr}" if curr == "USD" else f"от {vac.salary_min:,.0f} {curr}"
        elif vac.salary_max is not None:
            curr = vac.salary_currency or "USD"
            sal = f"до ${vac.salary_max:,.0f} {curr}" if curr == "USD" else f"до {vac.salary_max:,.0f} {curr}"

        loc = vac.location or "Remote"
        reason_text = reasons[0] if reasons else "Соответствует профилю AI / Python / Automation"

        formatted_items.append({
            "id": vac.stable_id(),
            "title": vac.title,
            "company": vac.company or "Компания",
            "url": vac.job_url or vac.application_url or "",
            "salary": sal,
            "location": loc,
            "score": score,
            "reason": reason_text,
        })

    if not formatted_items:
        post_text = "Сегодня новых подходящих вакансий не найдено — все уже обработаны."
    else:
        post_lines = ["🚀 **Дайджест новых удалённых вакансий** (AI Automation / Python / n8n)\n"]
        for idx, item in enumerate(formatted_items, 1):
            post_lines.append(f"**🎯 {item['title']}**")
            post_lines.append(f"🏢 Company: {item['company']}")
            post_lines.append(f"💰 Salary: {item['salary']}")
            post_lines.append(f"📍 Location: {item['location']}")
            post_lines.append(f"🔗 URL: {item['url']}")
            post_lines.append(f"📝 Why it fits: {item['reason']}")
            post_lines.append("")
        post_text = "\n".join(post_lines).strip()

    # If explicit atomic delivery was requested, record delivery in database
    if mark_delivered and formatted_items:
        mark_digest_delivered([it["id"] for it in formatted_items])

    from .telegram_notifier import TelegramNotifier
    reply_markup = TelegramNotifier.build_digest_inline_keyboard(formatted_items) if formatted_items else None

    if output_json or format_type == "json":
        out = {
            "telegram_post": post_text,
            "new_vacancies_data": formatted_items,
            "count": len(formatted_items),
            "reply_markup": reply_markup,
        }
        try:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        except UnicodeEncodeError:
            print(json.dumps(out, ensure_ascii=True, indent=2))
    else:
        try:
            print(post_text)
        except UnicodeEncodeError:
            print(post_text.encode("ascii", errors="replace").decode("ascii"))

    return 0


def digest_attempts_cmd(
    action: str = "list",
    batch_key: Optional[str] = None,
    new_status: Optional[str] = None,
    limit: int = 50,
    output_json: bool = False,
) -> int:
    """Inspect or reconcile digest delivery attempts (Stage 80/81)."""
    try:
        init_db()
    except Exception as e:
        print(f"Failed to open DB: {e}", file=sys.stderr)
        return 1

    if action == "list" or not action:
        attempts = list_digest_attempts(limit=limit)
        if output_json:
            print(json.dumps({"attempts": attempts, "count": len(attempts)}, ensure_ascii=False, indent=2))
        else:
            if not attempts:
                print("No digest delivery attempts recorded.")
            else:
                print(f"=== DIGEST DELIVERY ATTEMPTS (Total: {len(attempts)}) ===")
                for att in attempts:
                    retry_str = "[RETRY PERMITTED]" if att["retry_permitted"] else "[NO AUTO RETRY]"
                    stale_str = " [STALE]" if att.get("stale") else ""
                    reconcile_str = " [ACTION REQUIRED]" if att.get("requires_reconciliation") else ""
                    print(f"Batch: {att['batch_key']} | Persisted: {att['status']} | Effective: {att['effective_status']}{stale_str}{reconcile_str}")
                    print(f"  Age: {att.get('age_minutes', 0)} min | Created: {att.get('created_at')} | Updated: {att.get('last_updated_at')}")
                    print(f"  Chat: {att['chat_id']} | Vacancies: {att['vacancy_count']} | Retry: {retry_str}")
                    if att["vacancies"]:
                        print(f"  IDs: {', '.join(att['vacancies'][:5])}{'...' if len(att['vacancies']) > 5 else ''}")
                    print("-" * 60)
        return 0

    elif action == "recover":
        if not batch_key or not new_status:
            print("Error: --batch and --status [DELIVERED|FAILED|AMBIGUOUS] are required for recover.", file=sys.stderr)
            return 1
        if new_status == "FAILED":
            print("WARNING: Marking an ambiguous/stale batch FAILED permits Telegram resend.")
            print("Only do this after confirming the previous message was not delivered.")
        try:
            reconcile_digest_attempt(batch_key, new_status)
            print(f"[SUCCESS] Reconciled batch '{batch_key}' and associated vacancies to status '{new_status}'.")
            return 0
        except KeyError as k_err:
            print(f"[ERROR] {k_err}", file=sys.stderr)
            return 1
        except Exception as err:
            print(f"[ERROR] Failed to reconcile batch '{batch_key}': {err}", file=sys.stderr)
            return 1
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        return 1


def production_health_cmd(output_json: bool = False) -> int:
    """Inspect production state and evaluate overall operational health (Stage 83)."""
    from .db import get_production_health
    res = get_production_health()
    
    if output_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        status_color = res["health"]
        print(f"=== PRODUCTION PIPELINE HEALTH: {status_color} ===")
        print(f"Database Path: {res['db_path']} (Accessible: {res['db_accessible']})")
        print(f"Evaluation Timestamp: {res['evaluation_timestamp']}")
        print("-" * 60)
        m = res["metrics"]
        print(f"Delivery Records Total: {m['total_delivery_records']} (Batches: {m['digest_batch_records']}, Vacancies: {m['job_digest_records']})")
        print(f"Attempt States: Delivered={m['delivered_count']}, Attempting={m['attempting_count']}, Stale={m['stale_count']}, Ambiguous={m['ambiguous_count']}, Failed={m['failed_count']}")
        print(f"Duplicate Delivery Keys: {m['duplicate_delivery_keys_count']}")
        print(f"Consecutive Failures: {m['consecutive_failures']}")
        print(
            "Production Circuit: "
            f"{'OPEN' if m['production_circuit_open'] else 'CLOSED'} "
            f"(threshold: {m['production_circuit_threshold']})"
        )
        print(f"Last Attempt: {m['last_digest_attempt_at'] or 'Never'}")
        print(f"Last Successful Delivery: {m['last_successful_digest_at'] or 'Never'}")
        print(f"Pending Undigested Vacancies: {m['pending_undigested_vacancies_count']}")
        print("-" * 60)
        if res["alerts"]:
            print(f"ACTIVE ALERTS ({len(res['alerts'])}):")
            for alt in res["alerts"]:
                print(f"  [{alt['severity']}] {alt['message']}")
        else:
            print("Active Alerts: None (All systems operational)")
        print("=" * 60)
        
    return 0 if res["health"] == "HEALTHY" else (1 if res["health"] == "DEGRADED" else 2)


def production_run_cmd(dry_run: bool = False, fetcher_script: str | None = None) -> int:
    """Run the canonical production wrapper and propagate its exact outcome."""
    from .runner import run_production_pipeline

    return run_production_pipeline(fetcher_script=fetcher_script, dry_run=dry_run)


def production_control_cmd(
    action: str = "status",
    output_json: bool = False,
    storage_dir: str | None = None,
) -> int:
    """Inspect or explicitly resume the persistent production circuit breaker."""
    from .runner import ConsecutiveFailureTracker

    tracker = ConsecutiveFailureTracker(storage_dir=storage_dir)
    if action == "resume":
        status = tracker.resume_after_operator_review()
    elif action == "status":
        status = tracker.get_status()
    else:
        print(f"Unknown production-control action: {action}", file=sys.stderr)
        return 3

    payload = {
        "action": action,
        "circuit_open": status["circuit_open"],
        "circuit_threshold": status["circuit_threshold"],
        "consecutive_failures": status["consecutive_failures"],
        "circuit_opened_at": status.get("circuit_opened_at"),
        "last_failure_at": status.get("last_failure_at"),
        "last_error": status.get("last_error"),
        "last_operator_resume_at": status.get("last_operator_resume_at"),
    }
    if output_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Production circuit: {'OPEN' if payload['circuit_open'] else 'CLOSED'}")
        print(f"Consecutive failures: {payload['consecutive_failures']} / {payload['circuit_threshold']}")
        if action == "resume":
            print("Operator resume recorded. The next scheduled live run is allowed.")
        elif payload["circuit_open"]:
            print("Review production-health and logs; use an offline production-run --dry-run probe before resuming.")
    return 0




def feedback_cmd(
    action: str = "list",
    limit: int = 50,
    vacancy_id: Optional[str] = None,
    output_json: bool = False,
    profile_path: Optional[str] = None,
) -> int:
    """Inspect, summarize, and analyze Telegram human feedback & preference calibration (Stage 89/90)."""
    try:
        init_db()
    except Exception as e:
        print(f"Failed to open DB: {e}", file=sys.stderr)
        return 1

    from . import db
    from .db import list_telegram_feedback, get_telegram_feedback_summary

    if action == "summary":
        summary = get_telegram_feedback_summary()
        if output_json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print("📊 Telegram Feedback Summary:")
            print(f"  Total feedbacks recorded: {summary['total_feedbacks']}")
            for act, cnt in summary["action_counts"].items():
                print(f"  - {act}: {cnt}")
            if summary.get("breakdown"):
                print("\n  Breakdown:")
                for b in summary["breakdown"][:10]:
                    print(f"    • {b['action']} | {b['source']} | {b['company']} | {b['decision']} -> {b['count']}")
        return 0

    if action == "analytics":
        from .feedback_analytics import build_preference_profile
        prof = build_preference_profile()
        p_dict = prof.to_dict()
        if output_json:
            print(json.dumps(p_dict, ensure_ascii=False, indent=2))
        else:
            print("============================================================")
            print("       STAGE 91: FEEDBACK ANALYTICS & CALIBRATION READINESS  ")
            print("============================================================")
            print(f"Generated At:              {prof.generated_at}")
            print(f"Canonical Human Events:    {prof.total_evidence_events} / 5 ({prof.events_needed_for_threshold} more needed for threshold)")
            print(f"Unique Vacancies:          {prof.unique_vacancies_evaluated}")
            print(f"Calibration Readiness:     {prof.calibration_readiness}")
            print(f"Calibration Status:        {prof.calibration_status}")
            print("-" * 60)
            print("ROLE FAMILY PREFERENCES:")
            if prof.role_families:
                for k, sig in prof.role_families.items():
                    print(f"  • {k:25} signal={sig.raw_signal:+.2f} conf={sig.confidence:.2f} (pos={sig.positive_count}, neg={sig.negative_count}, total={sig.total_evidence}) [{sig.status} | {sig.readiness}]")
            else:
                print("  No role family signals recorded yet.")
            print("-" * 60)
            print("ROLE CONCEPT PREFERENCES:")
            if prof.role_concepts:
                for k, sig in prof.role_concepts.items():
                    print(f"  • {k:28} signal={sig.raw_signal:+.2f} conf={sig.confidence:.2f} (n={sig.total_evidence}) [{sig.status}]")
            else:
                print("  No role concept signals recorded yet.")
            print("-" * 60)
            print("TOP SKILL / TECHNOLOGY PREFERENCES:")
            if prof.skills:
                for k, sig in sorted(prof.skills.items(), key=lambda x: x[1].total_evidence, reverse=True)[:10]:
                    print(f"  • {k:20} signal={sig.raw_signal:+.2f} conf={sig.confidence:.2f} (n={sig.total_evidence}) [{sig.status}]")
            else:
                print("  No skill signals recorded yet.")
            print("-" * 60)
            print("COMPANY SIGNALS:")
            if prof.companies:
                for k, sig in prof.companies.items():
                    print(f"  • {k:25} signal={sig.raw_signal:+.2f} (n={sig.total_evidence}) [{sig.status}]")
            else:
                print("  No company signals recorded yet.")
            print("-" * 60)
            if prof.feedback_reasons:
                print("FEEDBACK REASONS:")
                for r, cnt in sorted(prof.feedback_reasons.items()):
                    print(f"  • {r:25}: {cnt}")
                print("-" * 60)
            print("BIAS & CALIBRATION NOTES:")
            for note in prof.selection_bias_notes:
                print(f"  ℹ {note}")
            print("============================================================")
        return 0

    if action == "coverage":
        from .feedback_analytics import get_feedback_coverage_metrics
        metrics = get_feedback_coverage_metrics()
        if output_json:
            print(json.dumps(metrics, ensure_ascii=False, indent=2))
        else:
            print("============================================================")
            print("       STAGE 91.1: FEEDBACK & HUMAN EVIDENCE COVERAGE       ")
            print("============================================================")
            print(f"Delivered Vacancies:            {metrics['delivered_vacancies']}")
            print(f"Telegram Feedback Vacancies:    {metrics['telegram_feedback_vacancies']} ({metrics['telegram_feedback_coverage_rate']:.1%})")
            print(f"Confirmed Real Applications:    {metrics['confirmed_applications']}")
            print(f"Canonical Human Evidence Total: {metrics['canonical_human_evidence_vacancies']} ({metrics['human_evidence_coverage_rate']:.1%})")
            print("-" * 60)
            print("ACTION DISTRIBUTION:")
            print(f"  • Explicit Positive (👍/📄):  {metrics['explicit_positive']}")
            print(f"  • Explicit Negative (👎):     {metrics['explicit_negative']}")
            print(f"  • Contextual Skip (⏭):       {metrics['skip']}")
            print(f"  • Unanswered Digest Items:   {metrics['no_feedback']} (never treated as dislike)")
            print("-" * 60)
            if metrics["reasons_breakdown"]:
                print("FEEDBACK REASONS BREAKDOWN:")
                for r, cnt in sorted(metrics["reasons_breakdown"].items()):
                    print(f"  • {r:25}: {cnt}")
            else:
                print("  No structured reasons recorded yet.")
            print("============================================================")
        return 0

    if action == "provenance":
        from .feedback_analytics import extract_all_preference_evidence, build_preference_profile
        all_raw = extract_all_preference_evidence(include_non_production=True)
        prof = build_preference_profile()
        if output_json:
            print(json.dumps({
                "raw_evidence_count": len(all_raw),
                "production_eligible_count": prof.production_eligible_events_count,
                "excluded_count": prof.excluded_events_count,
                "calibration_readiness": prof.calibration_readiness,
                "provenance_summary": prof.provenance_summary,
                "raw_events": [e.to_dict() for e in all_raw],
                "eligible_events": [e.to_dict() for e in prof.evidence_events],
            }, ensure_ascii=False, indent=2))
        else:
            print("============================================================")
            print("       STAGE 90.1: FEEDBACK EVIDENCE PROVENANCE AUDIT        ")
            print("============================================================")
            print(f"Total Raw Evidence Rows:       {len(all_raw)}")
            print(f"Production Eligible Events:    {prof.production_eligible_events_count}")
            print(f"Excluded Rows:                 {prof.excluded_events_count}")
            print(f"Calibration Readiness:         {prof.calibration_readiness}")
            print("-" * 60)
            print("PROVENANCE BREAKDOWN:")
            for p_name, cnt in sorted(prof.provenance_summary.items()):
                print(f"  • {p_name:35} : {cnt}")
            print("-" * 60)
            print("VALID PRODUCTION EVIDENCE GROUND TRUTH:")
            if prof.evidence_events:
                for ev in prof.evidence_events:
                    print(f"  [{ev.occurred_at}] {ev.vacancy_stable_id} | {ev.action} ({ev.signal_strength.value})")
                    print(f"      Title: {ev.title} @ {ev.company} [{ev.source}]")
                    print(f"      Provenance: {ev.provenance.value} | Human Confirmed: {ev.human_confirmed}")
                    for n in ev.notes:
                        print(f"      • {n}")
            else:
                print("  No production-eligible evidence events recorded yet.")
            print("-" * 60)
            print("EXCLUDED EVIDENCE ROWS:")
            excluded = [e for e in all_raw if not e.is_production_eligible]
            for ev in excluded[:10]:
                print(f"  [{ev.source_table}] {ev.vacancy_stable_id} -> {ev.provenance.value}")
                for n in ev.notes:
                    print(f"      • {n}")
            if len(excluded) > 10:
                print(f"  ... and {len(excluded) - 10} more excluded rows.")
            print("============================================================")
        return 0

    if action == "simulate":
        from .feedback_analytics import build_preference_profile, calculate_preference_adjustment
        from .matcher import JobMatcher
        from .candidate_profile import load_candidate_profile
        from .db import _row_to_vacancy

        prof = build_preference_profile()
        cand_prof = load_candidate_profile(path=profile_path)
        matcher = JobMatcher(cand_prof)

        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM vacancies ORDER BY match_score DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        conn.close()

        simulations = []
        for r in rows:
            vac = _row_to_vacancy(r)
            m_res = matcher.match(vac)
            adj, reasons = calculate_preference_adjustment(
                vacancy=vac,
                profile=prof,
                base_match_score=m_res.score,
                decision_class=m_res.decision_class,
                eligibility=m_res.eligibility,
                enabled=True,
            )
            sid = vac.stable_id() if callable(getattr(vac, "stable_id", None)) else getattr(vac, "stable_id", "")
            simulations.append({
                "stable_id": sid,
                "title": vac.title,
                "company": vac.company,
                "role_family": m_res.role_family,
                "base_match_score": m_res.score,
                "preference_adjustment": adj,
                "ranking_score": int(max(0, min(100, round(m_res.score + adj)))),
                "decision_class": m_res.decision_class,
                "eligibility": m_res.eligibility,
                "reasons": reasons,
            })

        if output_json:
            print(json.dumps({
                "calibration_status": prof.calibration_status,
                "total_evidence_events": prof.total_evidence_events,
                "simulations": simulations,
            }, ensure_ascii=False, indent=2))
        else:
            print("============================================================")
            print("       STAGE 90: PREFERENCE CALIBRATION SIMULATION          ")
            print("============================================================")
            print(f"Global Evidence Events:    {prof.total_evidence_events}")
            print(f"Calibration Status:        {prof.calibration_status}")
            print("-" * 60)
            for s in simulations:
                diff = s["ranking_score"] - s["base_match_score"]
                diff_str = f"({diff:+d})" if diff != 0 else "(no change)"
                print(f"[{s['stable_id']}] {s['title'][:40]} @ {s['company'][:20]}")
                print(f"  Base Match: {s['base_match_score']} -> Ranking Score: {s['ranking_score']} {diff_str} | Class: {s['decision_class']}")
                for reas in s["reasons"][:2]:
                    print(f"    • {reas}")
            print("============================================================")
        return 0

    # Default action: list
    records = list_telegram_feedback(limit=limit, vacancy_stable_id=vacancy_id)
    if output_json:
        print(json.dumps({"records": records, "count": len(records)}, ensure_ascii=False, indent=2))
    else:
        if not records:
            print("No feedback records recorded yet.")
            return 0
        print(f"📋 Last {len(records)} Telegram Feedback Records:")
        for r in records:
            print(f"  [{r['created_at']}] ID: {r['id']} | Action: {r['action']} | Vacancy: {r['vacancy_stable_id']}")
            print(f"      Transition: {r['previous_status']} -> {r['new_status']} | User: {r['telegram_user_id']} | CB: {r['callback_query_id']}")
    return 0


def hermes_cmd(
    action: str = "status",
    dry_run: bool = False,
    output_json: bool = False,
) -> int:
    """Inspect and synchronize external Hermes runtime integration (Stage 89.2)."""
    from .hermes_integration import get_hermes_integration_status, sync_hermes_integration

    if action == "sync":
        res = sync_hermes_integration(dry_run=dry_run)
        if output_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print("=== HERMES INTEGRATION SYNC ===")
            print(f"Dry Run: {'YES' if dry_run else 'NO'}")
            print(f"Success: {'YES' if res['success'] else 'NO'}")
            if res.get("actions_taken"):
                print("Actions Taken:")
                for a in res["actions_taken"]:
                    print(f"  • {a}")
            else:
                print("Actions Taken: None (Already in sync)")
            print(f"Overall Status: {res['status_after']['status']}")
            print("=" * 60)
        return 0 if res["success"] else 1

    # Default action: status
    res = get_hermes_integration_status()
    if output_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print("=== HERMES INTEGRATION STATUS ===")
        print(f"Overall Status: {res['status']}")
        print("-" * 60)
        f = res["fetcher"]
        print(f"Fetcher Script: {f['status']}")
        print(f"  Path:               {f['deployed_path']}")
        print(f"  Exists:             {'YES' if f['exists'] else 'NO'}")
        print(f"  Keyboard Capable:   {'YES' if f['keyboard_capable'] else 'NO'}")
        print(f"  Attempt Locking:    {'YES' if f['attempt_locking'] else 'NO'}")
        print(f"  Deployed SHA256:    {f['deployed_sha256'] or 'N/A'}")
        print(f"  Canonical SHA256:   {f['canonical_sha256'] or 'N/A'}")
        print("-" * 60)
        a = res["adapter"]
        print(f"Telegram Adapter: {a['status']}")
        print(f"  Path:               {a['deployed_path']}")
        print(f"  Exists:             {'YES' if a['exists'] else 'NO'}")
        print(f"  Routing Capable:    {'YES' if a['routing_capable'] else 'NO'}")
        print(f"  Deployed SHA256:    {a['deployed_sha256'] or 'N/A'}")
        print("=" * 60)
    return 0 if res["status"] == "HEALTHY" else 1


def main() -> int:
    # Handle direct `review <id>` as `review show <id>`
    if len(sys.argv) >= 3 and sys.argv[1] == "review" and sys.argv[2] not in ["list", "show", "approve", "reject", "-h", "--help"]:
        sys.argv.insert(2, "show")
    # Ensure utf-8 output on Windows
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Job search CLI")
    subparsers = parser.add_subparsers(dest="command")

    collect_parser = subparsers.add_parser("collect", help="Collect vacancies from sources")
    collect_parser.add_argument("--sources", nargs="*", default=list(SOURCES.keys()))

    analyze_parser = subparsers.add_parser("analyze", help="Run matcher on stored vacancies")
    analyze_parser.add_argument("--top", type=int, default=20)
    analyze_parser.add_argument("--profile", type=str, default=None, help="Path to candidate_profile.json")
    analyze_parser.add_argument("--persist", action="store_true", help="Persist scores to DB")

    deep_parser = subparsers.add_parser("analyze-deep", help="Run deep LLM analysis on top APPLY/REVIEW vacancies")
    deep_parser.add_argument("--top", type=int, default=20)
    deep_parser.add_argument("--profile", type=str, default=None, help="Path to candidate_profile.json")
    deep_parser.add_argument("--force", action="store_true", help="Force re-analyze even if cached")

    prep_parser = subparsers.add_parser("prepare-applications", help="Prepare application packages for APPLY/REVIEW vacancies")
    prep_parser.add_argument("--top", type=int, default=20)
    prep_parser.add_argument("--profile", type=str, default=None, help="Path to candidate_profile.json")
    prep_parser.add_argument("--force", action="store_true", help="Force regeneration even if cached")

    list_parser = subparsers.add_parser("list", help="List stored vacancies")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--state", default=None)
    list_parser.add_argument("--eligibility", default=None, help="Filter by eligibility (eligible, warning, unknown, ineligible, all)")

    reclassify_parser = subparsers.add_parser("reclassify-eligibility", help="Reclassify stored vacancies with Remote Eligibility Engine")
    reclassify_parser.add_argument("--candidate-country", default="TH", help="Candidate location country code (default: TH)")
    reclassify_parser.add_argument("--profile", default=None, help="Path to candidate profile")

    apps_parser = subparsers.add_parser("applications", help="Application tracking lifecycle")
    app_sub = apps_parser.add_subparsers(dest="app_command")

    app_list_p = app_sub.add_parser("list", help="List tracked applications")
    app_list_p.add_argument("--limit", type=int, default=50)
    app_list_p.add_argument("--status", type=str, default=None, help="Filter by status")

    app_status_p = app_sub.add_parser("status", help="Show application status and history")
    app_status_p.add_argument("vacancy_stable_id", type=str)

    app_move_p = app_sub.add_parser("move", help="Move application to new status")
    app_move_p.add_argument("vacancy_stable_id", type=str)
    app_move_p.add_argument("new_status", type=str)
    app_move_p.add_argument("--note", type=str, default=None)

    app_sync_p = app_sub.add_parser("sync", help="Sync tracking with matcher/deep/package")
    app_sync_p.add_argument("--profile", type=str, default=None)

    queue_parser = subparsers.add_parser("queue", help="Application queue prioritization")
    queue_parser.add_argument("--top", type=int, default=20, help="Top N")
    queue_parser.add_argument("--status", type=str, default=None, help="Filter by status (default READY_TO_APPLY)")
    queue_parser.add_argument("--profile", type=str, default=None, help="Path to profile")
    queue_parser.add_argument("--duplicates", action="store_true", help="Show canonical queue duplicates")
    queue_sub = queue_parser.add_subparsers(dest="queue_command")
    queue_show_p = queue_sub.add_parser("show", help="Show queue item")
    queue_show_p.add_argument("vacancy_stable_id", type=str)

    review_parser = subparsers.add_parser("review", help="Application review (human gate)")
    review_sub = review_parser.add_subparsers(dest="review_command")
    review_list_p = review_sub.add_parser("list", help="List reviews")
    review_list_p.add_argument("--limit", type=int, default=50)
    review_list_p.add_argument("--status", type=str, default=None)
    review_show_p = review_sub.add_parser("show", help="Show review")
    review_show_p.add_argument("vacancy_stable_id", type=str)
    review_approve_p = review_sub.add_parser("approve", help="Approve review")
    review_approve_p.add_argument("vacancy_stable_id", type=str)
    review_reject_p = review_sub.add_parser("reject", help="Reject review")
    review_reject_p.add_argument("vacancy_stable_id", type=str)
    review_reject_p.add_argument("--note", type=str, default=None)
    # Also support direct `review <id>` as show (without subcommand)
    review_parser.add_argument("vacancy_stable_id_direct", nargs="?", help="Vacancy ID to show (alternative to show subcommand)")

    browser_parser = subparsers.add_parser("browser", help="Browser application preparation (no auto-submit)")
    browser_sub = browser_parser.add_subparsers(dest="browser_command")
    browser_prepare_p = browser_sub.add_parser("prepare", help="Prepare vacancy in browser (no submit)")
    browser_prepare_p.add_argument("vacancy_stable_id", type=str)
    browser_prepare_p.add_argument("--force", action="store_true", help="Force re-prepare")
    browser_prepare_p.add_argument("--real", action="store_true", help="Use real Playwright browser (not mock)")
    browser_prepare_next_p = browser_sub.add_parser("prepare-next", help="Prepare next READY vacancy in browser")
    browser_prepare_next_p.add_argument("--top", type=int, default=20, help="Top N queue")
    browser_prepare_next_p.add_argument("--real", action="store_true", help="Use real Playwright browser")

    submit_parser = subparsers.add_parser("submit", help="Submit application (requires --confirm-submit)")
    submit_parser.add_argument("vacancy_stable_id", type=str)
    submit_parser.add_argument("--confirm-submit", action="store_true", help="Explicitly confirm submission")
    submit_parser.add_argument("--profile", type=str, default=None, help="Path to candidate_profile.json")
    submit_parser.add_argument("--force", action="store_true", help="Force re-submit if needed")

    submit_next_parser = subparsers.add_parser("submit-next", help="Submit next APPROVED + READY_FOR_REVIEW vacancy")
    submit_next_parser.add_argument("--top", type=int, default=1, help="Number of vacancies to try")
    submit_next_parser.add_argument("--confirm-submit", action="store_true", help="Explicitly confirm submission")
    submit_next_parser.add_argument("--profile", type=str, default=None, help="Path to candidate_profile.json")

    submissions_parser = subparsers.add_parser("submissions", help="Submission management and verification")
    submissions_sub = submissions_parser.add_subparsers(dest="submissions_command")
    submissions_list_p = submissions_sub.add_parser("list", help="List submissions with verification status")
    submissions_list_p.add_argument("--limit", type=int, default=50, help="Limit results")
    submissions_show_p = submissions_sub.add_parser("show", help="Show submission and verification details")
    submissions_show_p.add_argument("vacancy_stable_id", type=str)
    submissions_verify_p = submissions_sub.add_parser("verify", help="Verify submission (checks page, does NOT re-submit)")
    submissions_verify_p.add_argument("vacancy_stable_id", type=str)
    submissions_recover_p = submissions_sub.add_parser("recover", help="Inspect submission state and recommend action (read-only)")
    submissions_recover_p.add_argument("vacancy_stable_id", type=str)
    submissions_reconcile_p = submissions_sub.add_parser("reconcile", help="Sync tracking with verified state (VERIFIED -> APPLIED)")
    submissions_reconcile_p.add_argument("vacancy_stable_id", type=str)
    submissions_audit_p = submissions_sub.add_parser("audit", help="Show full chronological audit trail")
    submissions_audit_p.add_argument("vacancy_stable_id", type=str)

    dashboard_parser = subparsers.add_parser("dashboard", help="Application lifecycle dashboard")
    dashboard_sub = dashboard_parser.add_subparsers(dest="dashboard_command")
    dashboard_parser.add_argument("--actions", action="store_true", help="Show only action items requiring attention")
    dashboard_parser.add_argument("--queue", action="store_true", help="Show queue summary")
    dashboard_parser.add_argument("--history", action="store_true", help="Show recent lifecycle events")
    dashboard_parser.add_argument("--limit", type=int, default=50, help="Limit for history/queue")
    dashboard_show_p = dashboard_sub.add_parser("show", help="Show detailed view for a vacancy")
    dashboard_show_p.add_argument("vacancy_stable_id", type=str, help="Vacancy ID for detailed view")
    dashboard_canonical_p = dashboard_sub.add_parser("canonical", help="Show detailed view for a canonical vacancy")
    dashboard_canonical_p.add_argument("canonical_id", type=str, help="Canonical ID for detailed view")

    identity_parser = subparsers.add_parser("identity", help="Vacancy canonical identity and deduplication")
    identity_sub = identity_parser.add_subparsers(dest="identity_command")
    identity_show_p = identity_sub.add_parser("show", help="Show canonical identity for a vacancy")
    identity_show_p.add_argument("vacancy_stable_id", type=str)
    identity_sync_p = identity_sub.add_parser("sync", help="Sync canonical identity from all vacancies")
    identity_queue_p = identity_sub.add_parser("queue", help="Show queue info for a canonical vacancy")
    identity_queue_p.add_argument("canonical_id", type=str)
    identity_queue_p.add_argument("--limit", type=int, default=50, help="Limit for history/queue")

    audit_parser = subparsers.add_parser("audit", help="Application lifecycle integrity audit (read-only)")
    audit_parser.add_argument("--errors", action="store_true", help="Show only ERROR severity issues")
    audit_parser.add_argument("--warnings", action="store_true", help="Show only WARNING severity issues")
    audit_parser.add_argument("--json", action="store_true", help="Output as JSON")
    audit_parser.add_argument("--tracked", action="store_true", help="Audit only tracked applications / queue workflow artifacts")
    audit_parser.add_argument("--canonical", type=str, help="Audit specific canonical vacancy")
    audit_sub = audit_parser.add_subparsers(dest="audit_command")
    audit_show_p = audit_sub.add_parser("show", help="Show detailed audit for a vacancy")
    audit_show_p.add_argument("vacancy_stable_id", type=str)
    audit_canonical_p = audit_sub.add_parser("canonical", help="Audit specific canonical vacancy")
    audit_canonical_p.add_argument("canonical_id", type=str)

    duplicates_parser = subparsers.add_parser("duplicates", help="List probable and exact duplicate vacancies")

    # Stage 30C Phase 1: REVIEW/READ-ONLY messaging & email/gmail wiring (NO send, NO AUTO).
    hh_message_parser = subparsers.add_parser(
        "hh-message", help="HH messaging (REVIEW-only; read-only list/preview, NO send)")
    hh_message_sub = hh_message_parser.add_subparsers(dest="hh_message_command")
    hh_message_list_p = hh_message_sub.add_parser("list", help="List HH dialog cards (read-only)")
    hh_message_list_p.add_argument("--cdp-url", default=None, help="CDP endpoint (default: HH_CDP_URL or 127.0.0.1:9222)")
    hh_message_list_p.add_argument("--url-substring", default=None, help="Page-tab URL substring to attach to (default: hh.ru)")
    hh_message_prev_p = hh_message_sub.add_parser("preview", help="Preview a conversation context + reply (read-only)")
    hh_message_prev_p.add_argument("conversation_id", nargs="?", default=None, type=str, help="Optional conversation ID (default: open chat)")
    hh_message_prev_p.add_argument("--conversation-id", dest="conversation_id_opt", default=None, type=str, help="Optional conversation ID")
    hh_message_prev_p.add_argument("--limit", type=int, default=None, help="Limit number of recent messages to show")
    hh_message_prev_p.add_argument("--json", action="store_true", help="Output preview as machine-readable JSON")
    hh_message_prev_p.add_argument("--cdp-url", default=None)
    hh_message_prev_p.add_argument("--url-substring", default=None)
    # Stage 30C Phase 2A: additional REVIEW-only wiring (pure read-only helpers)
    hh_message_classify_p = hh_message_sub.add_parser("classify", help="Classify conversation and draft reply (read-only, no send)")
    hh_message_classify_p.add_argument("conversation_id", nargs="?", default=None, type=str, help="Optional conversation ID (default: open chat)")
    hh_message_classify_p.add_argument("--conversation-id", dest="conversation_id_opt", default=None, type=str, help="Optional conversation ID")
    hh_message_classify_p.add_argument("--limit", type=int, default=None, help="Limit number of context messages")
    hh_message_classify_p.add_argument("--json", action="store_true", help="Output classification as machine-readable JSON")
    hh_message_classify_p.add_argument("--cdp-url", default=None)
    hh_message_classify_p.add_argument("--url-substring", default=None)

    # Stage 30D.4: validate command (validate prepared reply against profile evidence and safety gates)
    hh_message_validate_p = hh_message_sub.add_parser("validate", help="Validate prepared reply draft against safety rules (read-only, no send)")
    hh_message_validate_p.add_argument("conversation_id", nargs="?", default=None, type=str, help="Optional conversation ID (default: open chat)")
    hh_message_validate_p.add_argument("--conversation-id", dest="conversation_id_opt", default=None, type=str, help="Optional conversation ID")
    hh_message_validate_p.add_argument("--limit", type=int, default=None, help="Limit number of context messages")
    hh_message_validate_p.add_argument("--json", action="store_true", help="Output validation as machine-readable JSON")
    hh_message_validate_p.add_argument("--cdp-url", default=None)
    hh_message_validate_p.add_argument("--url-substring", default=None)

    # Stage 30D.6: send command (controlled, human-confirmed reply send)
    hh_message_send_p = hh_message_sub.add_parser("send", help="Controlled human-confirmed reply send (requires --confirm)")
    hh_message_send_p.add_argument("conversation_id", nargs="?", default=None, type=str, help="Optional conversation ID (default: open chat)")
    hh_message_send_p.add_argument("--conversation-id", dest="conversation_id_opt", default=None, type=str, help="Optional conversation ID")
    hh_message_send_p.add_argument("--confirm", action="store_true", help="Explicit human confirmation to execute send")
    hh_message_send_p.add_argument("--limit", type=int, default=None, help="Limit number of context messages")
    hh_message_send_p.add_argument("--json", action="store_true", help="Output result as machine-readable JSON")
    hh_message_send_p.add_argument("--cdp-url", default=None)
    hh_message_send_p.add_argument("--url-substring", default=None)

    # Stage 30D.9: triage command (multi-conversation read-only triage)
    hh_message_triage_p = hh_message_sub.add_parser("triage", help="Multi-conversation read-only triage (READ-ONLY, no send)")
    hh_message_triage_p.add_argument("conversation_id", nargs="?", default=None, type=str, help="Optional conversation ID (filter to single dialog)")
    hh_message_triage_p.add_argument("--conversation-id", dest="conversation_id_opt", default=None, type=str, help="Optional conversation ID")
    hh_message_triage_p.add_argument("--limit", type=int, default=None, help="Limit number of conversations to triage")
    hh_message_triage_p.add_argument("--json", action="store_true", help="Output triage result as machine-readable JSON")
    hh_message_triage_p.add_argument("--cdp-url", default=None)
    hh_message_triage_p.add_argument("--url-substring", default=None)

    # Stage 30D: diagnose command (probe health of HH message / chatik flow)
    hh_message_diag_p = hh_message_sub.add_parser("diagnose", help="Probe HH message flow health (READ-ONLY probe)")
    hh_message_diag_p.add_argument("--cdp-url", default=None, help="CDP endpoint (default: HH_CDP_URL or http://127.0.0.1:9222)")
    hh_message_diag_p.add_argument("--url-substring", default=None, help="Page-tab URL substring to attach to (default: hh.ru)")
    hh_message_diag_p.add_argument("--frame-substrings", default=None, help="Comma-separated chatik frame substrings (default: chatik.hh.ru,/chat/)")
    hh_message_diag_p.add_argument("--json", action="store_true", help="Output diagnostic result as machine-readable JSON")

    email_parser = subparsers.add_parser("email", help="Email messaging (REVIEW-only; read-only list/preview, NO send)")
    email_sub = email_parser.add_subparsers(dest="email_command")
    email_list_p = email_sub.add_parser("list", help="List incoming emails (read-only)")
    email_list_p.add_argument("--max-emails", type=int, default=None)
    email_prev_p = email_sub.add_parser("preview", help="Preview reply for an email by index from 'email list' (read-only)")
    email_prev_p.add_argument("target", type=str)
    email_prev_p.add_argument("--max-emails", type=int, default=None)
    # Stage 30C Phase 2A: additional REVIEW-only email helpers (classify/link)
    email_classify_p = email_sub.add_parser("classify", help="Classify email by index (read-only, no send)")
    email_classify_p.add_argument("target", type=str, help="Index from 'email list'")
    email_classify_p.add_argument("--max-emails", type=int, default=None)
    email_link_p = email_sub.add_parser("link", help="Show email linkage to company/vacancy (read-only)")
    email_link_p.add_argument("target", type=str, help="Index from 'email list'")
    email_link_p.add_argument("--max-emails", type=int, default=None)

    gmail_parser = subparsers.add_parser("gmail", help="Gmail read-only status")
    gmail_sub = gmail_parser.add_subparsers(dest="gmail_command")
    gmail_sub.add_parser("status", help="Show gmail.readonly auth/connection status (read-only)")

    # Stage 30C — standalone diagnostic (no args, read-only)
    subparsers.add_parser("system-info", help="Show environment diagnostics (READ-ONLY, no network/DB/send)")

    # Web Dashboard UI
    ui_parser = subparsers.add_parser("ui", help="Start local web dashboard")
    ui_parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    ui_parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")

    # Stage 31 — Controlled Auto-Apply Watcher
    watch_parser = subparsers.add_parser("watch", help="Controlled application watcher (polls sources, filters, prepares, stops at review gate)")
    watch_parser.add_argument("--sources", nargs="*", default=list(SOURCES.keys()), help="Sources to poll")
    watch_parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds (default: 60)")
    watch_parser.add_argument("--once", action="store_true", help="Run a single poll cycle and exit")
    watch_parser.add_argument("--limit", type=int, default=20, help="Batch limit per cycle")
    watch_parser.add_argument("--country", type=str, default="TH", help="Candidate country code (default: TH)")
    watch_parser.add_argument("--profile", type=str, default=None, help="Path to candidate_profile.json")
    watch_parser.add_argument("--json", action="store_true", help="Output cycle result as JSON")

    # Stage 32 / 33 — HH Message Watcher
    msg_watch_parser = subparsers.add_parser("message-watch", help="Controlled HH message watcher (polls conversations, detects new incoming messages, prepares replies, stops at human review)")
    msg_watch_parser.add_argument("--cdp-url", default=None, help="CDP endpoint (default: HH_CDP_URL or 127.0.0.1:9222)")
    msg_watch_parser.add_argument("--url-substring", default=None, help="Page-tab URL substring to attach to (default: hh.ru)")
    msg_watch_parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds (default: 60)")
    msg_watch_parser.add_argument("--once", action="store_true", help="Run a single poll cycle and exit")
    msg_watch_parser.add_argument("--continuous", action="store_true", help="Run continuous polling watcher")
    msg_watch_parser.add_argument("--limit", type=int, default=20, help="Batch limit per cycle")
    msg_watch_parser.add_argument("--iterations", type=int, default=None, help="Maximum iterations to run in continuous mode")
    msg_watch_parser.add_argument("--profile", type=str, default=None, help="Path to candidate_profile.json")
    msg_watch_parser.add_argument("--json", action="store_true", help="Output cycle result as JSON")

    # Stage 34 — HH Screening Questionnaire (Human-in-the-Loop)
    quest_parser = subparsers.add_parser("questionnaire", help="HH Screening Questionnaire (Human-in-the-Loop review and answers)")
    quest_sub = quest_parser.add_subparsers(dest="questionnaire_command")
    quest_list_p = quest_sub.add_parser("list", help="List stored questionnaires")
    quest_list_p.add_argument("--status", type=str, default=None, help="Filter by status (NEEDS_HUMAN_REVIEW, READY_TO_SUBMIT, SUBMITTED, BLOCKED)")
    quest_list_p.add_argument("--limit", type=int, default=50, help="Limit results")
    
    quest_show_p = quest_sub.add_parser("show", help="Show questionnaire and required questions")
    quest_show_p.add_argument("target_id", type=str, help="Questionnaire ID, Vacancy ID, or Conversation ID")
    
    quest_ans_p = quest_sub.add_parser("answer", help="Provide human answers for questionnaire questions")
    quest_ans_p.add_argument("target_id", type=str, help="Questionnaire ID, Vacancy ID, or Conversation ID")
    quest_ans_p.add_argument("--answers", type=str, default=None, help="JSON string of answers mapping question_id -> answer")
    quest_ans_p.add_argument("--answer", action="append", default=[], help="Single answer in format key=value (can be repeated)")
    
    quest_sug_p = quest_sub.add_parser("suggest", help="Generate tailored answer suggestions from resume and vacancy profile")
    quest_sug_p.add_argument("target_id", type=str, help="Questionnaire ID, Vacancy ID, or Conversation ID")
    quest_sug_p.add_argument("--apply", action="store_true", help="Automatically apply validated suggestions to the questionnaire")

    quest_audit_p = quest_sub.add_parser("audit", help="Run a pre-submit audit on questionnaire answers against candidate profile")
    quest_audit_p.add_argument("target_id", type=str, help="Questionnaire ID, Vacancy ID, or Conversation ID")

    quest_sub_p = quest_sub.add_parser("submit", help="Submit confirmed questionnaire application")
    quest_sub_p.add_argument("target_id", type=str, help="Questionnaire ID, Vacancy ID, or Conversation ID")
    quest_sub_p.add_argument("--confirm-submit", action="store_true", help="Explicit human confirmation to proceed with submit")
    quest_sub_p.add_argument("--answers", type=str, default=None, help="Optional JSON string of answers")

    # Stage 35 — HH Application State Machine & Orchestrator
    single_app_parser = subparsers.add_parser("application", help="HH Application state machine and lifecycle (Stage 35)")
    single_app_sub = single_app_parser.add_subparsers(dest="single_app_command")
    
    single_app_list_p = single_app_sub.add_parser("list", help="List stored HH applications")
    single_app_list_p.add_argument("--state", type=str, default=None, help="Filter by state (NEW, MESSAGE_DETECTED, ANALYZED, DRAFT_READY, QUESTIONNAIRE_REQUIRED, NEEDS_HUMAN_REVIEW, READY_TO_SUBMIT, SUBMITTED, BLOCKED, FAILED, STALE)")
    single_app_list_p.add_argument("--limit", type=int, default=50, help="Limit results")
    
    single_app_show_p = single_app_sub.add_parser("show", help="Show HH application state and action items")
    single_app_show_p.add_argument("target_id", type=str, help="Application ID, Conversation ID, or Vacancy ID")
    
    single_app_trans_p = single_app_sub.add_parser("transitions", help="Show chronological state transition audit trail")
    single_app_trans_p.add_argument("target_id", type=str, help="Application ID, Conversation ID, or Vacancy ID")
    single_app_trans_p.add_argument("--limit", type=int, default=100, help="Limit history records")
    
    single_app_status_p = single_app_sub.add_parser("status", help="Show HH application status")
    single_app_status_p.add_argument("target_id", type=str, help="Application ID, Conversation ID, or Vacancy ID")

    single_app_audit_p = single_app_sub.add_parser("audit", help="Run a pre-submit audit on application questionnaire answers")
    single_app_audit_p.add_argument("target_id", type=str, help="Application ID, Conversation ID, or Vacancy ID")

    single_app_submit_p = single_app_sub.add_parser("submit", help="Submit confirmed application to HH with safety gate")
    single_app_submit_p.add_argument("target_id", type=str, help="Application ID, Conversation ID, or Vacancy ID")
    single_app_submit_p.add_argument("--confirm-submit", action="store_true", help="Explicit human confirmation to proceed with submit")
    single_app_submit_p.add_argument("--answers", type=str, default=None, help="Optional JSON string of answers")

    single_app_verify_p = single_app_sub.add_parser("verify-submit", help="Verify factual post-submit status of application on HH (read-only)")
    single_app_verify_p.add_argument("target_id", type=str, help="Application ID, Conversation ID, or Vacancy ID")

    single_app_queue_p = single_app_sub.add_parser("queue", help="Show controlled HH application queue")
    single_app_queue_p.add_argument("--json", dest="as_json", action="store_true", help="Output queue in machine-readable JSON")
    single_app_queue_p.add_argument("--ready", action="store_true", help="Show only applications ready to submit")
    single_app_queue_p.add_argument("--human-review", action="store_true", help="Show only applications requiring human review")

    single_app_runner_p = single_app_sub.add_parser("runner", help="Controlled batch application execution runner (Stage 46)")
    runner_sub = single_app_runner_p.add_subparsers(dest="runner_command")
    runner_prev_p = runner_sub.add_parser("preview", help="Preview the next READY_TO_SUBMIT application in the queue")
    runner_prev_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")
    runner_next_p = runner_sub.add_parser("next", help="Execute pre-checks for the next application without submitting (or submit with --confirm-submit)")
    runner_next_p.add_argument("--confirm-submit", action="store_true", help="Explicit human confirmation to proceed with submit")
    runner_next_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    # Stage 51 — Autonomous Job Application Agent
    auto_parser = subparsers.add_parser("autonomous", help="Fully Autonomous Job Application Agent (Stage 51)")
    auto_sub = auto_parser.add_subparsers(dest="autonomous_command")

    auto_once_p = auto_sub.add_parser("once", help="Run a single complete autonomous cycle")
    auto_once_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")
    auto_once_p.add_argument("--limit", type=int, default=5, help="Max applications per cycle")

    auto_start_p = auto_sub.add_parser("start", help="Start recurring autonomous agent daemon")
    auto_start_p.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60)")

    auto_status_p = auto_sub.add_parser("status", help="Show autonomous agent operations status and history")
    auto_status_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    auto_notif_p = auto_sub.add_parser("notifications", help="List recent high-priority autonomous notifications")
    auto_notif_p.add_argument("--limit", type=int, default=20, help="Max records")
    auto_notif_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    auto_conv_p = auto_sub.add_parser("conversations", help="Show full conversation and auto-reply audit trail (Stage 52)")
    auto_conv_p.add_argument("--limit", type=int, default=50, help="Max records (default: 50)")
    auto_conv_p.add_argument("--application-id", type=str, default=None, help="Filter by application ID")
    auto_conv_p.add_argument("--conversation-id", type=str, default=None, help="Filter by conversation ID")
    auto_conv_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    auto_replies_p = auto_sub.add_parser("replies", help="Show autonomous recruiter replies sent (Stage 53)")
    auto_replies_p.add_argument("--limit", type=int, default=50, help="Max records (default: 50)")
    auto_replies_p.add_argument("--application-id", type=str, default=None, help="Filter by application ID")
    auto_replies_p.add_argument("--conversation-id", type=str, default=None, help="Filter by conversation ID")
    auto_replies_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    tg_parser = subparsers.add_parser("telegram", help="Production Telegram Bot management (Stage 56)")
    tg_sub = tg_parser.add_subparsers(dest="telegram_command", help="Telegram subcommands")
    tg_status_p = tg_sub.add_parser("status", help="Check Telegram Bot configuration and connectivity")
    tg_status_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")
    tg_test_p = tg_sub.add_parser("test", help="Send a test notification to target chat")
    tg_test_p.add_argument("--message", type=str, default="Production Telegram Notifier connection test.", help="Test message")

    digest_parser = subparsers.add_parser("export-digest", help="Export validated vacancy digest from state.db (Stage 75, Stage 78)")
    digest_parser.add_argument("--format", dest="format_type", choices=["telegram", "json", "markdown"], default="telegram", help="Output format (default: telegram)")
    digest_parser.add_argument("--limit", type=int, default=10, help="Maximum vacancies in digest (default: 10)")
    digest_parser.add_argument("--min-score", type=float, default=60.0, help="Minimum match score threshold (default: 60.0)")
    digest_parser.add_argument("--profile", type=str, default=None, help="Path to candidate_profile.json")
    digest_parser.add_argument("--json", dest="as_json", action="store_true", help="Output machine-readable JSON structure")
    digest_parser.add_argument("--mark-delivered", dest="mark_delivered", action="store_true", help="Mark exported vacancies as delivered in database")
    digest_parser.add_argument("--include-legacy", dest="include_legacy", action="store_true", help="Include legacy historical vacancies")

    digest_att_parser = subparsers.add_parser("digest-attempts", help="Inspect and reconcile digest delivery attempts (Stage 80)")
    digest_att_sub = digest_att_parser.add_subparsers(dest="attempts_action", help="Digest attempts action")
    digest_att_list_p = digest_att_sub.add_parser("list", help="List digest delivery attempts")
    digest_att_list_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")
    digest_att_list_p.add_argument("--limit", type=int, default=50, help="Maximum attempts to list")
    digest_att_rec_p = digest_att_sub.add_parser("recover", help="Reconcile an unresolved digest attempt")
    digest_att_rec_p.add_argument("--batch", type=str, required=True, help="Batch key to reconcile")
    digest_att_rec_p.add_argument("--status", choices=["DELIVERED", "FAILED", "AMBIGUOUS"], required=True, help="Target status")

    health_parser = subparsers.add_parser("production-health", help="Inspect production state and evaluate operational health (Stage 83)")
    health_parser.add_argument("--json", dest="as_json", action="store_true", help="Output machine-readable JSON structure")

    production_control_parser = subparsers.add_parser(
        "production-control",
        help="Inspect or explicitly resume the fail-closed production circuit",
    )
    production_control_sub = production_control_parser.add_subparsers(
        dest="production_control_action",
        help="Circuit action",
    )
    production_control_status = production_control_sub.add_parser("status", help="Show circuit state")
    production_control_status.add_argument("--json", dest="as_json", action="store_true", help="Output machine-readable JSON structure")
    production_control_resume = production_control_sub.add_parser(
        "resume",
        help="Allow live runs after the operator has reviewed the failure",
    )
    production_control_resume.add_argument("--json", dest="as_json", action="store_true", help="Output machine-readable JSON structure")

    # Stage 89 — Telegram Feedback Inspector
    fb_parser = subparsers.add_parser("feedback", help="Inspect Telegram human feedback (Stage 89)")
    fb_sub = fb_parser.add_subparsers(dest="feedback_command", help="Feedback action")
    fb_list_p = fb_sub.add_parser("list", help="List recorded feedback events")
    fb_list_p.add_argument("--limit", type=int, default=50, help="Maximum records to list")
    fb_list_p.add_argument("--vacancy-id", type=str, default=None, help="Filter by vacancy stable_id")
    fb_list_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    fb_sum_p = fb_sub.add_parser("summary", help="Show feedback aggregation summary")
    fb_sum_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    fb_analytics_p = fb_sub.add_parser("analytics", help="Show feedback preference analytics and derived profile (Stage 90)")
    fb_analytics_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    fb_coverage_p = fb_sub.add_parser("coverage", help="Show feedback delivery and coverage metrics (Stage 91)")
    fb_coverage_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    fb_prov_p = fb_sub.add_parser("provenance", help="Audit feedback evidence provenance and label quality (Stage 90.1)")
    fb_prov_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    fb_sim_p = fb_sub.add_parser("simulate", help="Simulate preference-calibrated ranking comparison (Stage 90)")
    fb_sim_p.add_argument("--limit", type=int, default=20, help="Number of top vacancies to simulate")
    fb_sim_p.add_argument("--profile", type=str, default=None, help="Path to candidate profile")
    fb_sim_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    # Stage 89.2 — Hermes Integration & Drift Subsystem
    hermes_parser = subparsers.add_parser("hermes", help="Hermes external runtime persistence & drift detection (Stage 89.2)")
    hermes_sub = hermes_parser.add_subparsers(dest="hermes_command", help="Hermes integration action")
    hermes_status_p = hermes_sub.add_parser("status", help="Inspect Hermes integration status and detect drift")
    hermes_status_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")
    hermes_sync_p = hermes_sub.add_parser("sync", help="Sync canonical integration files to Hermes runtime")
    hermes_sync_p.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    hermes_sync_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    production_run_parser = subparsers.add_parser("production-run", help="Run the fail-closed production wrapper (Stage 83)")
    production_run_parser.add_argument("--dry-run", action="store_true", help="Disable external delivery and live vacancy adapters")
    production_run_parser.add_argument("--fetcher", dest="fetcher_script", default=None, help=argparse.SUPPRESS)

    args = None
    try:
        args = parser.parse_args()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
        return 0 if code == 0 else 3
    if args.command == "collect":
        collect(args.sources)
    elif args.command == "analyze":
        analyze(args.top, profile_path=args.profile, persist=args.persist)
    elif args.command == "analyze-deep":
        analyze_deep(args.top, profile_path=args.profile, force=args.force)
    elif args.command == "prepare-applications":
        prepare_applications(args.top, profile_path=args.profile, force=args.force)
    elif args.command == "list":
        return list_cmd(args.limit, args.state, getattr(args, "eligibility", None))
    elif args.command == "reclassify-eligibility":
        return reclassify_eligibility_cmd(candidate_country=args.candidate_country, profile_path=args.profile)
    elif args.command == "applications":
        if args.app_command == "list":
            applications_list(limit=args.limit, status_filter=args.status)
        elif args.app_command == "status":
            return applications_status(args.vacancy_stable_id)
        elif args.app_command == "move":
            return applications_move(args.vacancy_stable_id, args.new_status, note=args.note)
        elif args.app_command == "sync":
            return applications_sync(profile_path=args.profile)
        else:
            apps_parser.print_help()
            return 1
    elif args.command == "queue":
        if args.queue_command == "show":
            return queue_show(args.vacancy_stable_id)
        else:
            queue_list(top=args.top, status_filter=args.status, profile_path=args.profile)
    elif args.command == "review":
        # Direct `review <vacancy_id>` without subcommand: vacancy id is parsed as review_command
        if args.review_command and args.review_command not in ["list", "show", "approve", "reject"]:
            return review_show(args.review_command)
        if args.review_command == "list":
            review_list(limit=args.limit, status_filter=args.status)
        elif args.review_command == "approve":
            return review_approve(args.vacancy_stable_id)
        elif args.review_command == "reject":
            return review_reject(args.vacancy_stable_id, note=args.note)
        elif args.review_command == "show":
            return review_show(args.vacancy_stable_id)
        elif getattr(args, "vacancy_stable_id_direct", None):
            return review_show(args.vacancy_stable_id_direct)
        else:
            # Default: if no subcommand but vacancy_stable_id_direct is set, show
            # Also support `review <id>` without subcommand via direct arg
            review_parser.print_help()
            return 1
    elif args.command == "browser":
        if args.browser_command == "prepare":
            if getattr(args, "real", False):
                import os
                os.environ["BROWSER_USE_PLAYWRIGHT"] = "1"
            return browser_prepare(args.vacancy_stable_id, force=args.force)
        elif args.browser_command == "prepare-next":
            if getattr(args, "real", False):
                import os
                os.environ["BROWSER_USE_PLAYWRIGHT"] = "1"
            return browser_prepare_next(top=args.top)
        else:
            browser_parser.print_help()
            return 1
    elif args.command == "submit":
        if not getattr(args, "confirm_submit", False):
            print("Submit confirmation required. Use --confirm-submit to proceed.")
            print("No browser action performed.")
            return 1
        from .browser_executor import submit_application_in_browser
        return submit_vacancy(args.vacancy_stable_id, confirm_submit=True, force=args.force)
    elif args.command == "submit-next":
        if not getattr(args, "confirm_submit", False):
            print("Submit confirmation required. Use --confirm-submit to proceed.")
            print("No browser action performed.")
            return 1
        from .browser_executor import submit_next_in_queue
        return submit_next_in_queue(top=args.top, profile_path=args.profile)
    elif args.command == "submissions":
        if args.submissions_command == "list":
            submissions_list(limit=args.limit)
        elif args.submissions_command == "show":
            return submissions_show(args.vacancy_stable_id)
        elif args.submissions_command == "verify":
            return submissions_verify(args.vacancy_stable_id)
        elif args.submissions_command == "recover":
            return submissions_recover(args.vacancy_stable_id)
        elif args.submissions_command == "reconcile":
            return submissions_reconcile(args.vacancy_stable_id)
        elif args.submissions_command == "audit":
            return submissions_audit(args.vacancy_stable_id)
        else:
            submissions_parser.print_help()
            return 1
    elif args.command == "dashboard":
        if args.dashboard_command == "show":
            return dashboard_show(args.vacancy_stable_id)
        elif args.dashboard_command == "canonical":
            return dashboard_show_canonical(args.canonical_id)
        elif args.actions:
            return dashboard_actions()
        elif args.queue:
            return dashboard_queue(args.limit)
        elif args.history:
            return dashboard_history(args.limit)
        else:
            return dashboard()
    elif args.command == "identity":
        if args.identity_command == "show":
            return identity_show(args.vacancy_stable_id)
        elif args.identity_command == "sync":
            return identity_sync()
        elif args.identity_command == "queue":
            return identity_queue(args.canonical_id, args.limit)
        else:
            identity_parser.print_help()
            return 1
    elif args.command == "duplicates":
        return duplicates_list()
    elif args.command == "queue":
        if args.queue_command == "show":
            return queue_show(args.vacancy_stable_id)
        elif args.duplicates:
            return queue_duplicates()
        else:
            queue_list(top=args.top, status_filter=args.status, profile_path=args.profile)
    elif args.command == "audit":
        scope = "tracked" if getattr(args, "tracked", False) else "full"
        if args.audit_command == "show":
            return audit_show(args.vacancy_stable_id)
        elif args.audit_command == "canonical":
            return audit_canonical(args.canonical_id)
        elif args.json:
            return audit_json(args.errors, args.warnings, scope=scope)
        elif args.errors:
            return audit_errors(scope=scope)
        elif args.warnings:
            return audit_warnings(scope=scope)
        else:
            return audit(scope=scope)
    elif args.command == "hh-message":
        if args.hh_message_command == "list":
            return hh_message_list(cdp_url=args.cdp_url, url_substring=args.url_substring)
        elif args.hh_message_command == "preview":
            conv_id = getattr(args, "conversation_id_opt", None) or getattr(args, "conversation_id", None)
            return hh_message_preview(conv_id, cdp_url=args.cdp_url,
                                      url_substring=args.url_substring,
                                      limit=getattr(args, "limit", None),
                                      as_json=getattr(args, "json", False))
        elif args.hh_message_command == "classify":
            conv_id = getattr(args, "conversation_id_opt", None) or getattr(args, "conversation_id", None)
            return hh_message_classify(conv_id, cdp_url=args.cdp_url,
                                       url_substring=args.url_substring,
                                       limit=getattr(args, "limit", None),
                                       as_json=getattr(args, "json", False))
        elif args.hh_message_command == "validate":
            conv_id = getattr(args, "conversation_id_opt", None) or getattr(args, "conversation_id", None)
            return hh_message_validate(conv_id, cdp_url=args.cdp_url,
                                       url_substring=args.url_substring,
                                       limit=getattr(args, "limit", None),
                                       as_json=getattr(args, "json", False))
        elif args.hh_message_command == "send":
            conv_id = getattr(args, "conversation_id_opt", None) or getattr(args, "conversation_id", None)
            return hh_message_send(conv_id, confirm=getattr(args, "confirm", False),
                                   cdp_url=args.cdp_url,
                                   url_substring=args.url_substring,
                                   limit=getattr(args, "limit", None),
                                   as_json=getattr(args, "json", False))
        elif args.hh_message_command == "triage":
            conv_id = getattr(args, "conversation_id_opt", None) or getattr(args, "conversation_id", None)
            return hh_message_triage(conv_id, limit=getattr(args, "limit", None),
                                     cdp_url=args.cdp_url,
                                     url_substring=args.url_substring,
                                     as_json=getattr(args, "json", False))
        elif args.hh_message_command == "diagnose":
            return hh_message_diagnose(cdp_url=args.cdp_url, url_substring=args.url_substring,
                                       frame_substrings=args.frame_substrings, as_json=args.json)
        hh_message_parser.print_help()
        return 1
    elif args.command == "email":
        if args.email_command == "list":
            return email_list(max_emails=args.max_emails)
        elif args.email_command == "preview":
            return email_preview(args.target, max_emails=args.max_emails)
        elif args.email_command == "classify":
            return email_classify(args.target, max_emails=args.max_emails)
        elif args.email_command == "link":
            return email_link(args.target, max_emails=args.max_emails)
        email_parser.print_help()
        return 1
    elif args.command == "gmail":
        if args.gmail_command == "status":
            return gmail_status()
        gmail_parser.print_help()
        return 1
    elif args.command == "system-info":
        return system_info()
    elif args.command == "ui":
        return ui_cmd(args.host, args.port)
    elif args.command == "watch":
        return watch_cmd(
            sources=args.sources,
            interval=args.interval,
            once=args.once,
            limit=args.limit,
            candidate_country=args.country,
            profile_path=args.profile,
            output_json=args.json,
        )
    elif args.command == "message-watch":
        return message_watch_cmd(
            cdp_url=args.cdp_url,
            url_substring=args.url_substring,
            interval=args.interval,
            once=args.once,
            continuous=args.continuous,
            limit=args.limit,
            iterations=args.iterations,
            profile_path=args.profile,
            output_json=args.json,
        )
    elif args.command == "questionnaire":
        if args.questionnaire_command == "list":
            return questionnaire_list_cmd(status=args.status, limit=args.limit)
        elif args.questionnaire_command == "show":
            return questionnaire_show_cmd(args.target_id)
        elif args.questionnaire_command == "answer":
            return questionnaire_answer_cmd(
                args.target_id,
                answers_json=args.answers,
                single_answers=args.answer,
            )
        elif args.questionnaire_command == "suggest":
            return questionnaire_suggest_cmd(args.target_id, apply_answers=args.apply)
        elif args.questionnaire_command == "audit":
            return questionnaire_audit_cmd(args.target_id)
        elif args.questionnaire_command == "submit":
            return questionnaire_submit_cmd(
                args.target_id,
                confirm_submit=args.confirm_submit,
                answers_json=args.answers,
            )
        else:
            quest_parser.print_help()
            return 1
    elif args.command == "application":
        if args.single_app_command == "list":
            return application_list_cmd(state=args.state, limit=args.limit)
        elif args.single_app_command == "show":
            return application_show_cmd(args.target_id)
        elif args.single_app_command == "transitions":
            return application_transitions_cmd(args.target_id, limit=args.limit)
        elif args.single_app_command == "status":
            return application_status_cmd(args.target_id)
        elif args.single_app_command == "audit":
            return application_audit_cmd(args.target_id)
        elif args.single_app_command == "submit":
            return application_submit_cmd(
                args.target_id,
                confirm_submit=args.confirm_submit,
                answers_json=args.answers,
            )
        elif args.single_app_command == "verify-submit":
            return application_verify_submit_cmd(args.target_id)
        elif args.single_app_command == "queue":
            return application_queue_cmd(
                as_json=getattr(args, "as_json", False),
                ready_only=getattr(args, "ready", False),
                human_review_only=getattr(args, "human_review", False),
            )
        elif args.single_app_command == "runner":
            if not getattr(args, "runner_command", None):
                single_app_runner_p.print_help()
                return 1
            return application_runner_cmd(
                command=args.runner_command,
                confirm_submit=getattr(args, "confirm_submit", False),
                as_json=getattr(args, "as_json", False),
            )
        else:
            single_app_parser.print_help()
            return 1
    elif args.command == "autonomous":
        if args.autonomous_command == "once":
            return autonomous_once_cmd(as_json=getattr(args, "as_json", False), limit=getattr(args, "limit", 5))
        elif args.autonomous_command == "start":
            return autonomous_start_cmd(interval=getattr(args, "interval", 60))
        elif args.autonomous_command == "status":
            return autonomous_status_cmd(as_json=getattr(args, "as_json", False))
        elif args.autonomous_command == "notifications":
            return autonomous_notifications_cmd(limit=getattr(args, "limit", 20), as_json=getattr(args, "as_json", False))
        elif args.autonomous_command == "conversations":
            return autonomous_conversations_cmd(
                limit=getattr(args, "limit", 50),
                application_id=getattr(args, "application_id", None),
                conversation_id=getattr(args, "conversation_id", None),
                as_json=getattr(args, "as_json", False),
            )
        elif args.autonomous_command == "replies":
            return autonomous_replies_cmd(
                limit=getattr(args, "limit", 50),
                application_id=getattr(args, "application_id", None),
                conversation_id=getattr(args, "conversation_id", None),
                as_json=getattr(args, "as_json", False),
            )
        else:
            auto_parser.print_help()
            return 1
    elif args.command == "telegram":
        if args.telegram_command == "status":
            return telegram_status_cmd(as_json=getattr(args, "as_json", False))
        elif args.telegram_command == "test":
            return telegram_test_cmd(message=getattr(args, "message", "Test"))
        else:
            tg_parser.print_help()
            return 1
    elif args.command == "export-digest":
        return export_digest_cmd(
            format_type=getattr(args, "format_type", "telegram"),
            limit=getattr(args, "limit", 10),
            min_score=getattr(args, "min_score", 60.0),
            profile_path=getattr(args, "profile", None),
            output_json=getattr(args, "as_json", False),
            mark_delivered=getattr(args, "mark_delivered", False),
            include_legacy=getattr(args, "include_legacy", False),
        )
    elif args.command == "digest-attempts":
        action = getattr(args, "attempts_action", "list") or "list"
        return digest_attempts_cmd(
            action=action,
            batch_key=getattr(args, "batch", None),
            new_status=getattr(args, "status", None),
            limit=getattr(args, "limit", 50),
            output_json=getattr(args, "as_json", False),
        )
    elif args.command == "production-health":
        return production_health_cmd(
            output_json=getattr(args, "as_json", False),
        )
    elif args.command == "production-control":
        return production_control_cmd(
            action=getattr(args, "production_control_action", "status") or "status",
            output_json=getattr(args, "as_json", False),
        )
    elif args.command == "feedback":
        cmd = getattr(args, "feedback_command", "list") or "list"
        limit = getattr(args, "limit", 50)
        vac_id = getattr(args, "vacancy_id", None)
        as_json = getattr(args, "as_json", False)
        profile_path = getattr(args, "profile", None)
        return feedback_cmd(action=cmd, limit=limit, vacancy_id=vac_id, output_json=as_json, profile_path=profile_path)
    elif args.command == "hermes":
        cmd = getattr(args, "hermes_command", "status") or "status"
        dry_run = getattr(args, "dry_run", False)
        as_json = getattr(args, "as_json", False)
        return hermes_cmd(action=cmd, dry_run=dry_run, output_json=as_json)
    elif args.command == "production-run":
        return production_run_cmd(
            dry_run=getattr(args, "dry_run", False),
            fetcher_script=getattr(args, "fetcher_script", None),
        )
    else:
        parser.print_help()
        return 1
    return 0


def identity_show(vacancy_stable_id: str) -> int:
    """Show canonical identity for a vacancy."""
    init_db()
    from .db import get_vacancy_by_id
    from .db import _row_to_vacancy
    from .vacancy_identity import get_canonical_by_id, get_aliases_for_canonical
    
    row = get_vacancy_by_id(vacancy_stable_id)
    if not row:
        print(f"No vacancy found for {vacancy_stable_id}", file=__import__('sys').stderr)
        return 1
    
    vac = _row_to_vacancy(row)
    
    # Resolve identity
    result = resolve_vacancy_identity(vac)
    
    print("=== VACANCY IDENTITY ===")
    print()
    print(f"Canonical: {result.canonical_id}")
    print()
    print(f"Stable: {vacancy_stable_id}")
    print()
    print(f"Company: {vac.company}")
    print(f"Title: {vac.title}")
    print(f"URL: {vac.job_url}")
    print()
    print(f"Normalized URL: {normalize_url(vac.job_url)}")
    print(f"Normalized Company: {normalize_company(vac.company)}")
    print(f"Normalized Title: {normalize_title(vac.title)}")
    print()
    print(f"Match: {result.match_type.value}")
    print(f"Confidence: {result.confidence}")
    print()
    print("Reasons:")
    for reason in result.reasons:
        print(f"  - {reason}")
    print()
    
    if result.existing_canonical:
        print("Existing Canonical:")
        print(f"  ID: {result.existing_canonical.get('canonical_id', 'N/A')}")
        print(f"  Company: {result.existing_canonical.get('company', 'N/A')}")
        print(f"  Title: {result.existing_canonical.get('title', 'N/A')}")
        print(f"  Location: {result.existing_canonical.get('location', 'N/A')}")
        print()
    
    # Show aliases
    if result.match_type.value != "DISTINCT":
        aliases = get_aliases_for_canonical(result.canonical_id)
        if aliases:
            print("Aliases:")
            for alias in aliases:
                print(f"  {alias['vacancy_stable_id']} ({alias['source']}) - {alias['match_type']} ({alias['confidence']}%)")
            print()
    
    return 0


def identity_sync() -> int:
    """Sync canonical identity from all existing vacancies."""
    init_db()
    print("Syncing canonical identity from all vacancies...")
    stats = sync_identity_from_vacancies()
    print()
    print("Sync Results:")
    print(f"  Canonical created: {stats['created']}")
    print(f"  Exact duplicates:  {stats['exact_duplicates']}")
    print(f"  Probable duplicates: {stats['probable_duplicates']}")
    print(f"  Distinct: {stats['distinct']}")
    return 0


def duplicates_list() -> int:
    """List all probable and exact duplicates."""
    init_db()
    
    from .vacancy_identity import get_all_canonical_vacancies, get_aliases_for_canonical, MatchType
    
    all_canonical = get_all_canonical_vacancies()
    
    exact_duplicates = []
    probable_duplicates = []
    
    for canon in all_canonical:
        aliases = get_aliases_for_canonical(canon.canonical_id)
        if len(aliases) > 1:
            # Check match types
            exact_count = sum(1 for a in aliases if a['match_type'] == MatchType.EXACT.value)
            probable_count = sum(1 for a in aliases if a['match_type'] == MatchType.PROBABLE.value)
            
            if exact_count > 1:
                exact_duplicates.append((canon, aliases))
            elif probable_count > 0:
                probable_duplicates.append((canon, aliases))
    
    if not exact_duplicates and not probable_duplicates:
        print("No duplicates found.")
        return 0
    
    if exact_duplicates:
        print("=== EXACT DUPLICATES ===")
        print(f"{'TYPE':8} | {'CONF':5} | {'COMPANY':22} | {'TITLE':45} | {'EXISTING':18} | {'NEW':18}")
        print("-" * 130)
        for canon, aliases in exact_duplicates:
            for alias in aliases:
                print(f"{'EXACT':8} | {alias['confidence']:5} | {canon.normalized_company[:22]:22} | {canon.normalized_title[:45]:45} | {aliases[0]['vacancy_stable_id'][:18]:18} | {alias['vacancy_stable_id'][:18]:18}")
        print()
    
    if probable_duplicates:
        print("=== PROBABLE DUPLICATES ===")
        print(f"{'TYPE':8} | {'CONF':5} | {'COMPANY':22} | {'TITLE':45} | {'EXISTING':18} | {'NEW':18}")
        print("-" * 130)
        for canon, aliases in probable_duplicates:
            for alias in aliases:
                if alias['match_type'] == MatchType.PROBABLE.value:
                    print(f"{'PROBABLE':8} | {alias['confidence']:5} | {canon.normalized_company[:22]:22} | {canon.normalized_title[:45]:45} | {aliases[0]['vacancy_stable_id'][:18]:18} | {alias['vacancy_stable_id'][:18]:18}")
        print()
    
    return 0


def identity_queue(canonical_id: str, limit: int = 50) -> int:
    """Show queue info for a canonical vacancy."""
    from .vacancy_identity import get_canonical_by_id, get_aliases_for_canonical
    from .application_queue import get_queue_item, list_queue
    from .application_tracking import get_application_status
    init_db()
    
    canon = get_canonical_by_id(canonical_id)
    if not canon:
        print(f"No canonical vacancy found for {canonical_id}", file=__import__('sys').stderr)
        return 1
    
    print("=== CANONICAL QUEUE INFO ===")
    print()
    print(f"Canonical ID: {canonical_id}")
    print(f"Company: {canon.normalized_company}")
    print(f"Title: {canon.normalized_title}")
    print(f"Normalized URL: {canon.normalized_url}")
    print(f"Location: {canon.location or 'N/A'}")
    print()
    
    # Show aliases
    aliases = get_aliases_for_canonical(canonical_id)
    print(f"Aliases ({len(aliases)}):")
    for alias in aliases:
        print(f"  {alias['vacancy_stable_id']} ({alias['source']}) - {alias['match_type']} ({alias['confidence']}%)")
    print()
    
    # Show queue status for each alias
    print("Queue Status:")
    for alias in aliases:
        sid = alias['vacancy_stable_id']
        queue_item = get_queue_item(sid, "v2")
        track = get_application_status(sid)
        track_status = track.status.value if track and hasattr(track.status, 'value') else (str(track.status) if track else 'NONE')
        print(f"  {sid} ({alias['source']})")
        print(f"    Tracking: {track_status}")
        if queue_item:
            print(f"    Queue: Rank {queue_item.rank}, Priority {queue_item.priority_score}")
        else:
            print(f"    Queue: NOT IN QUEUE")
    print()
    
    # Show canonical queue item if exists
    # Check if any alias is in queue
    for alias in aliases:
        queue_item = get_queue_item(alias['vacancy_stable_id'], "v2")
        if queue_item:
            print("Canonical Queue Item:")
            print(f"  Rank: {queue_item.rank}")
            print(f"  Priority: {queue_item.priority_score}")
            print(f"  Representative: {queue_item.representative_vacancy_stable_id}")
            print(f"  Match: {queue_item.match_score}")
            print(f"  Deep: {queue_item.deep_score}")
            break
    
    return 0


def queue_duplicates() -> int:
    """Show canonical queue duplicates (EXACT only)."""
    from .vacancy_identity import get_all_canonical_vacancies, get_aliases_for_canonical, MatchType
    from .application_queue import list_queue
    init_db()
    
    all_canonical = get_all_canonical_vacancies()
    queue_items = {item.vacancy_stable_id: item for item in list_queue(queue_version="v2")}
    
    print("=== CANONICAL QUEUE DUPLICATES (EXACT) ===")
    print(f"{'CANONICAL':20} | {'ALIASES':3} | {'REPRESENTATIVE':22} | {'PRIORITY':6} | {'COMPANY':22} | {'TITLE':45}")
    print("-" * 140)
    
    found = False
    for canon in all_canonical:
        aliases = get_aliases_for_canonical(canon.canonical_id)
        if len(aliases) > 1:
            # Check if any alias is in queue
            in_queue = [a for a in aliases if a['vacancy_stable_id'] in queue_items]
            if in_queue:
                rep = queue_items.get(in_queue[0]['vacancy_stable_id'])
                print(f"{canon.canonical_id[:20]:20} | {len(aliases):3} | {in_queue[0]['vacancy_stable_id'][:22]:22} | {rep.priority_score if rep else 0:6} | {canon.normalized_company[:22]:22} | {canon.normalized_title[:45]:45}")
                found = True
    
    if not found:
        print("No canonical vacancies with multiple aliases in queue.")
    
    return 0
def audit(scope: str = "full") -> int:
    """Run full integrity audit."""
    from .application_integrity import run_integrity_audit
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
    from .application_integrity import run_integrity_audit, IntegritySeverity
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
    from .application_integrity import run_integrity_audit, IntegritySeverity
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
    from .application_integrity import run_integrity_audit, IntegritySeverity
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
    from .application_integrity import run_integrity_audit
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
    from .application_integrity import run_integrity_audit
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


def queue_duplicates() -> int:
    """Show canonical queue duplicates (EXACT only)."""
    from .vacancy_identity import get_all_canonical_vacancies, get_aliases_for_canonical, MatchType
    from .application_queue import list_queue
    init_db()
    
    all_canonical = get_all_canonical_vacancies()
    queue_items = {item.vacancy_stable_id: item for item in list_queue(queue_version="v2")}
    
    print("=== CANONICAL QUEUE DUPLICATES (EXACT) ===")
    print(f"{'CANONICAL':20} | {'ALIASES':3} | {'REPRESENTATIVE':22} | {'PRIORITY':6} | {'COMPANY':22} | {'TITLE':45}")
    print("-" * 140)
    
    found = False
    for canon in all_canonical:
        aliases = get_aliases_for_canonical(canon.canonical_id)
        if len(aliases) > 1:
            in_queue = [a for a in aliases if a['vacancy_stable_id'] in queue_items]
            if in_queue:
                rep = queue_items.get(in_queue[0]['vacancy_stable_id'])
                print(f"{canon.canonical_id[:20]:20} | {len(aliases):3} | {in_queue[0]['vacancy_stable_id'][:22]:22} | {rep.priority_score if rep else 0:6} | {canon.normalized_company[:22]:22} | {canon.normalized_title[:45]:45}")
                found = True
    
    if not found:
        print("No canonical vacancies with multiple aliases in queue.")
    
    return 0


# ---------------------------------------------------------------------------
# Stage 51 — Autonomous Job Application Agent Commands
# ---------------------------------------------------------------------------

def autonomous_once_cmd(as_json: bool = False, limit: int = 5) -> int:
    """Execute a single complete autonomous cycle."""
    from .hh_autonomous_agent import AutonomousConfig, run_autonomous_cycle
    import json
    cfg = AutonomousConfig(max_applications_per_cycle=limit)
    res = run_autonomous_cycle(config=cfg)

    if as_json:
        print(json.dumps(res.model_dump(), indent=2, ensure_ascii=False))
        return 0 if res.status == "SUCCESS" else 1

    print("=" * 60)
    print("      STAGE 51 FULLY AUTONOMOUS JOB AGENT CYCLE       ")
    print("=" * 60)
    print(f"Status:             {res.status}")
    print(f"Started:            {res.started_at}")
    print(f"Completed:          {res.completed_at}")
    print("-" * 60)
    print(f"Discovered:         {res.discovered_count}")
    print(f"Matched:            {res.matched_count}")
    print(f"Applied:            {res.applied_count}")
    print(f"Verified:           {res.verified_count}")
    print(f"Messages checked:   {res.messages_checked}")
    print(f"Auto-replies sent:  {res.auto_replies_count}")
    print(f"Interviews found:   {res.interviews_detected}")
    print(f"Rejections:         {res.rejections_count}")
    print(f"Unanswered (held):  {res.unanswered_questions_count}")
    print("-" * 60)
    print(f"Summary:            {res.summary}")
    print("=" * 60)
    return 0 if res.status == "SUCCESS" else 1


def autonomous_start_cmd(interval: int = 60) -> int:
    """Start continuous autonomous agent loop."""
    from .hh_autonomous_agent import AutonomousConfig, start_autonomous_daemon
    cfg = AutonomousConfig(poll_interval_seconds=interval)
    start_autonomous_daemon(config=cfg)
    return 0


def autonomous_status_cmd(as_json: bool = False) -> int:
    """Show autonomous agent status, recent cycles, and interview invites."""
    import json
    from . import db
    init_db()
    cycles = db.list_autonomous_cycle_runs(limit=5)
    interviews = db.list_interview_events(limit=10)
    notifs = db.list_autonomous_notifications(limit=10)

    if as_json:
        out = {
            "recent_cycles": cycles,
            "interview_events": interviews,
            "notifications": notifs,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    print("=" * 70)
    print("             AUTONOMOUS JOB AGENT STATUS & HISTORY            ")
    print("=" * 70)
    print(f"Total cycles recorded: {len(cycles)}")
    print(f"Interview invitations: {len(interviews)}")
    print(f"Pending notifications: {len(notifs)}")
    print("-" * 70)
    
    if interviews:
        print("\n📢 DETECTED INTERVIEW INVITATIONS:")
        for iv in interviews:
            print(f"  • [{iv['detected_at']}] {iv['company']} — {iv['vacancy_title']}")
            print(f"    Message: {iv['invitation_text'][:100]}...")
            if iv['invitation_url']:
                print(f"    Link: {iv['invitation_url']}")
            print()
    else:
        print("\nNo interview invitations recorded yet.")

    if cycles:
        print("\n🔄 RECENT AUTONOMOUS RUNS:")
        for c in cycles:
            print(f"  • Run #{c['id']} [{c['status']}] {c['started_at']}")
            print(f"    Discovered: {c['discovered_count']} | Applied: {c['applied_count']} | Verified: {c['verified_count']} | Interviews: {c['interviews_detected']}")
    print("=" * 70)
    return 0


def autonomous_notifications_cmd(limit: int = 20, as_json: bool = False) -> int:
    """List recent high-priority notifications."""
    import json
    from . import db
    init_db()
    notifs = db.list_autonomous_notifications(limit=limit)

    if as_json:
        print(json.dumps(notifs, indent=2, ensure_ascii=False))
        return 0

    print("=" * 70)
    print("                  HIGH-PRIORITY NOTIFICATIONS                 ")
    print("=" * 70)
    if not notifs:
        print("No active notifications.")
    else:
        for n in notifs:
            print(f"[{n['priority']}] {n['title']} ({n['created_at']})")
            print(f"  Message: {n['message']}")
            if n.get("action_required"):
                print(f"  Action:  {n['action_required']}")
            if n.get("vacancy_url"):
                print(f"  URL:     {n['vacancy_url']}")
            print("-" * 70)
    return 0


def autonomous_conversations_cmd(
    limit: int = 50,
    application_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    as_json: bool = False,
) -> int:
    """Show full conversation and auto-reply audit trail."""
    import json
    from . import db
    init_db()
    audits = db.list_conversation_audits(
        application_id=application_id,
        conversation_id=conversation_id,
        limit=limit,
    )

    if as_json:
        print(json.dumps(audits, indent=2, ensure_ascii=False))
        return 0

    print("=" * 70)
    print("                 AUTONOMOUS CONVERSATION AUDIT                ")
    print("=" * 70)
    if not audits:
        print("No conversation audits found matching the criteria.")
        print("=" * 70)
        return 0

    for a in audits:
        print(f"--- AUTONOMOUS CONVERSATION #{a['id']} ---")
        print(f"Company:        {a['employer']}")
        if a.get('vacancy_id'):
            print(f"Vacancy:        {a['vacancy_id']}")
        if a.get('application_id'):
            print(f"Application:    {a['application_id']}")
        print(f"Conversation:   {a['conversation_id']}")
        print(f"Timestamp:      {a.get('incoming_message_timestamp') or a['created_at']}")
        print()
        print("Employer:")
        print(f"  {a['incoming_message']}")
        print()
        print(f"Classification: {a['message_classification']}")
        if a.get('generated_reply'):
            print("Agent reply:")
            for line in a['generated_reply'].splitlines():
                print(f"  {line}")
            print()
        print(f"Status:         {a['status']}")
        if a.get('decision_reason'):
            print(f"Reason:         {a['decision_reason']}")
        if a.get('profile_facts_used'):
            print(f"Profile Facts:  {a['profile_facts_used']}")
        if a.get('error'):
            print(f"Error:          {a['error']}")
        print("=" * 70)
    return 0


def autonomous_replies_cmd(
    limit: int = 50,
    application_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    as_json: bool = False,
) -> int:
    """Show autonomous recruiter replies sent with full visibility (Stage 53)."""
    import json
    from . import db
    init_db()
    audits = db.list_conversation_audits(
        application_id=application_id,
        conversation_id=conversation_id,
        status="SENT",
        limit=limit,
    )

    if as_json:
        print(json.dumps(audits, indent=2, ensure_ascii=False))
        return 0

    print("=" * 70)
    print("                 AUTONOMOUS RECRUITER REPLIES SENT                ")
    print("=" * 70)
    if not audits:
        print("No sent recruiter replies found matching the criteria.")
        print("=" * 70)
        return 0

    for a in audits:
        print(f"--- RECRUITER REPLY #{a['id']} ---")
        print(f"Company:        {a['employer']}")
        if a.get('vacancy_id'):
            print(f"Vacancy:        {a['vacancy_id']}")
        if a.get('application_id'):
            print(f"Application:    {a['application_id']}")
        print(f"Conversation:   {a['conversation_id']}")
        print(f"Sent At:        {a.get('sent_at') or a['created_at']}")
        print()
        print("Employer:")
        print(f"  {a['incoming_message']}")
        print()
        print("My reply (SENT):")
        for line in (a.get('sent_reply') or a.get('generated_reply') or '').splitlines():
            print(f"  {line}")
        print()
        print(f"Status:         {a['status']}")
        if a.get('decision_reason'):
            print(f"Reason:         {a['decision_reason']}")
        if a.get('profile_facts_used'):
            print(f"Profile Facts:  {a['profile_facts_used']}")
        print("=" * 70)
    return 0


def telegram_status_cmd(as_json: bool = False) -> int:
    """Check Telegram Bot configuration and API reachability."""
    from .telegram_notifier import get_telegram_notifier
    notifier = get_telegram_notifier()
    configured = notifier.is_configured()

    status_data = {
        "configured": configured,
        "bot_token_present": bool(notifier.bot_token),
        "chat_id_present": bool(notifier.chat_id),
        "target_chat_id": notifier.chat_id if notifier.chat_id else None,
    }

    if as_json:
        print(json.dumps(status_data, indent=2, ensure_ascii=False))
        return 0

    print("=" * 60)
    print("           STAGE 56 TELEGRAM BOT STATUS           ")
    print("=" * 60)
    print(f"Configured:          {'YES' if configured else 'NO'}")
    print(f"Bot Token Present:   {'YES' if notifier.bot_token else 'NO'}")
    print(f"Target Chat ID:      {notifier.chat_id or 'Not set'}")
    if not configured:
        print("\nNote: Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env file to enable.")
    print("=" * 60)
    return 0


def telegram_test_cmd(message: str) -> int:
    """Send a test message via TelegramNotifier to verify connectivity."""
    from .telegram_notifier import get_telegram_notifier
    notifier = get_telegram_notifier()
    if not notifier.is_configured():
        print("Error: Telegram Bot Token or Chat ID not configured in .env file.", file=sys.stderr)
        return 1

    print(f"Sending test notification to chat {notifier.chat_id}...")
    res = notifier.send_message(text=f"🤖 *JOB AGENT TEST NOTIFICATION*\n\n{message}")
    if res.get("ok"):
        print("✅ Telegram message delivered successfully!")
        return 0
    else:
        print(f"❌ Failed to deliver message: {res.get('error')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
