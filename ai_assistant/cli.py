from __future__ import annotations

import argparse
import logging
import sys
from typing import List

from .adapters.himalayas import HimalayasAdapter
from .adapters.weworkremotely import WeWorkRemotelyAdapter
from .adapters.remoteok import RemoteOkAdapter
from .schema import Vacancy
from .normalizer import normalize_vacancy
from .matcher import JobMatcher, JobProfile
from .candidate_profile import load_candidate_profile
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


def analyze(top_n: int = 20, profile_path: str | None = None, persist: bool = False) -> None:
    init_db()
    rows = list_vacancies(limit=BATCH_LIMIT * 10)
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
    rows = list_vacancies(limit=BATCH_LIMIT * 20)
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
    rows = list_vacancies(limit=BATCH_LIMIT * 20)
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
        list_cmd(args.limit, args.state)
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


if __name__ == "__main__":
    sys.exit(main())
