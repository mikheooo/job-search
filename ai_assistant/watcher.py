"""Stage 31: Controlled Auto-Apply Watcher.

Periodically polls configured vacancy sources, ingests, normalizes,
filters via hard constraints & remote requirements, matches, performs deep analysis,
prepares application packages, and queues items for human review.

KEY INVARIANT:
The watcher can automatically search, filter, analyze, and prepare responses,
but CANNOT independently press the final Submit.
Final submission is strictly guarded by human confirmation:
    build_review_gate -> confirm_human_submission -> controlled_real_submit
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from enum import Enum
from typing import Any
from collections.abc import Callable

from pydantic import BaseModel, Field

from .adapters.habr_career import HabrCareerAdapter
from .adapters.himalayas import HimalayasAdapter
from .adapters.remoteok import RemoteOkAdapter
from .adapters.weworkremotely import WeWorkRemotelyAdapter
from .application_prep import (
    APPLICATION_PREP_VERSION,
    ApplicationPackage,
    prepare_application,
)
from .application_queue import (
    QUEUE_VERSION,
    QueueItem,
    compute_priority,
    list_queue,
    save_queue_item,
)
from .application_review import (
    ApplicationReview,
    ReviewStatus,
    save_application_review,
)
from .application_tracking import (
    ApplicationStatus,
    get_application_status,
    set_application_status,
    transition_application,
)
from .candidate_profile import load_candidate_profile
from .config import CANDIDATE_PROFILE_FILE
from .db import (
    _row_to_vacancy,
    get_application_package,
    get_deep_analysis,
    get_vacancy_by_id,
    get_vacancy_eligibility,
    init_db,
    is_dry_run,
    list_vacancies,
    save_application_package,
    save_deep_analysis,
    save_vacancy,
    save_vacancy_eligibility,
)
from .eligibility import EligibilityStatus, assess_vacancy_eligibility
from .job_analyzer import ANALYZER_VERSION, DeepAnalysisResult, analyze_job_deep
from .matcher import JobMatcher, _coerce_profile, _hard_constraints
from .normalizer import normalize_vacancy
from .remote_filter import is_strictly_remote
from .schema import Vacancy
from .vacancy_identity import (
    normalize_url,
    resolve_vacancy_identity,
)

logger = logging.getLogger(__name__)

DEFAULT_SOURCES = ["himalayas", "weworkremotely", "remoteok", "habrcareer"]

ADAPTER_MAP = {
    "himalayas": HimalayasAdapter,
    "weworkremotely": WeWorkRemotelyAdapter,
    "remoteok": RemoteOkAdapter,
    "habrcareer": HabrCareerAdapter,
}


class WatcherStatus(str, Enum):
    NEW = "NEW"
    DISCOVERED = "DISCOVERED"
    MATCHED = "MATCHED"
    ANALYZED = "ANALYZED"
    READY_TO_APPLY = "READY_TO_APPLY"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


class WatcherConfig(BaseModel):
    sources: list[str] = Field(default_factory=lambda: list(DEFAULT_SOURCES))
    poll_interval_seconds: int = 60
    max_iterations: int | None = None
    candidate_country: str = "TH"
    profile_path: str | None = None
    batch_limit: int = 20
    custom_adapters: dict[str, Any] | None = None
    evaluate_fn: Callable[[str], str] | None = None
    cdp_url: str | None = None

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}


class WatcherItem(BaseModel):
    vacancy_stable_id: str
    canonical_id: str | None = None
    title: str
    company: str
    url: str
    source: str = ""
    status: str = WatcherStatus.DISCOVERED.value
    match_decision: str = ""
    match_score: float | None = None
    deep_fit_score: float | None = None
    why_fit: list[str] = Field(default_factory=list)
    prepared_answers: list[dict[str, str]] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    review_gate_id: str | None = None
    review_fingerprint: str | None = None
    stop_reason: str = ""
    submit_attempted: bool = False
    submit_count: int = 0
    click_count: int = 0

    model_config = {"extra": "forbid"}


class WatcherCycleResult(BaseModel):
    iteration: int = 1
    timestamp: str = ""
    fetched_count: int = 0
    new_vacancies_count: int = 0
    duplicate_count: int = 0
    updated_count: int = 0
    invalid_count: int = 0
    conflict_count: int = 0
    rejected_count: int = 0
    matched_count: int = 0
    analyzed_count: int = 0
    prepared_count: int = 0
    ready_for_review_count: int = 0
    needs_human_review_count: int = 0
    blocked_count: int = 0
    items: list[WatcherItem] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


def _normalize_rejection_reasons(reasons: Any) -> list[str]:
    if isinstance(reasons, (list, tuple)):
        return [str(x) for x in reasons if str(x).strip()]
    if isinstance(reasons, str) and reasons.strip():
        return [reasons.strip()]
    return []


def run_watcher_cycle(
    config: WatcherConfig | None = None,
    iteration: int = 1,
    dry_run: bool = False,
) -> WatcherCycleResult:
    """Execute exactly ONE watcher polling cycle up to the human review gate.

    Pipeline:
        collect -> normalize -> deduplicate -> filter & match ->
        deep analyze -> prepare application -> human review queue ->
        STOP (no submit).

    Safety Guarantee:
        submit_attempted is ALWAYS False.
        submit_count is ALWAYS 0.
        click_count is ALWAYS 0.
    """
    from .db import is_dry_run as _is_dry_run
    from .db import set_dry_run as _set_dry_run
    previous_dry_run = _is_dry_run()
    _set_dry_run(dry_run)
    try:
        return _run_watcher_cycle_impl(config=config, iteration=iteration, dry_run=dry_run)
    finally:
        _set_dry_run(previous_dry_run)


def _run_watcher_cycle_impl(
    config: WatcherConfig | None = None,
    iteration: int = 1,
    dry_run: bool = False,
) -> WatcherCycleResult:
    if config is None:
        config = WatcherConfig()

    if not dry_run:
        init_db()

    result = WatcherCycleResult(
        iteration=iteration,
        timestamp=datetime.utcnow().isoformat(),
    )

    # 1. Load candidate profile
    profile = None
    if config.profile_path:
        profile = load_candidate_profile(config.profile_path)
    else:
        cfg_path = CANDIDATE_PROFILE_FILE if CANDIDATE_PROFILE_FILE and CANDIDATE_PROFILE_FILE.strip() else None
        if cfg_path:
            try:
                profile = load_candidate_profile(cfg_path)
            except Exception:
                profile = load_candidate_profile()
        else:
            profile = load_candidate_profile()

    matcher = JobMatcher(profile)

    # 2. Collect from configured sources
    adapters = {}
    if config.custom_adapters is not None:
        adapters = config.custom_adapters
    elif dry_run:
        # A dry-run is a local state/decision preview, not a live collection
        # run. Real adapters are intentionally not instantiated or called.
        # Tests may still provide explicit fake adapters via custom_adapters.
        logger.info("[DRY RUN] External vacancy adapters are disabled")
    else:
        from . import cli
        for name in config.sources:
            if hasattr(cli, "SOURCES") and name in cli.SOURCES:
                adapters[name] = cli.SOURCES[name]
            elif name in ADAPTER_MAP:
                adapters[name] = ADAPTER_MAP[name]()

    fetched_vacancies: list[Vacancy] = []

    for name, adapter in adapters.items():
        adapter_fetched = 0
        adapter_inserted = 0
        adapter_updated = 0
        adapter_unchanged = 0
        adapter_invalid = 0
        adapter_conflicts = 0
        try:
            raw_items = adapter.fetch_vacancies()
            adapter_fetched = len(raw_items)
            result.fetched_count += adapter_fetched
            
            seen_in_batch = set()
            for raw in raw_items:
                try:
                    raw_dict = raw.to_dict() if hasattr(raw, "to_dict") else raw
                    if not isinstance(raw_dict, dict) or not raw_dict.get("title") or not raw_dict.get("job_url"):
                        adapter_invalid += 1
                        result.invalid_count += 1
                        continue

                    vac = normalize_vacancy(raw_dict)
                    if not vac.job_url:
                        adapter_invalid += 1
                        result.invalid_count += 1
                        continue

                    norm_url = normalize_url(vac.job_url)
                    vac.job_url = norm_url

                    # Within-batch deduplication
                    batch_key = (vac.source, vac.stable_id(), norm_url)
                    if batch_key in seen_in_batch:
                        adapter_unchanged += 1
                        result.duplicate_count += 1
                        continue
                    seen_in_batch.add(batch_key)

                    if not is_dry_run():
                        status = save_vacancy(vac)
                        if status == "INSERTED":
                            adapter_inserted += 1
                            result.new_vacancies_count += 1
                            fetched_vacancies.append(vac)
                        elif status == "UPDATED":
                            adapter_updated += 1
                            result.updated_count += 1
                        elif status == "CONFLICT":
                            adapter_conflicts += 1
                            result.conflict_count += 1
                        else:  # UNCHANGED
                            adapter_unchanged += 1
                            result.duplicate_count += 1
                    else:
                        existing = get_vacancy_by_id(vac.stable_id())
                        if existing:
                            adapter_unchanged += 1
                            result.duplicate_count += 1
                        else:
                            adapter_inserted += 1
                            result.new_vacancies_count += 1
                            fetched_vacancies.append(vac)
                except Exception as item_err:
                    adapter_invalid += 1
                    result.invalid_count += 1
                    logger.warning(f"Item processing error in adapter {name}: {item_err}")
            
            summary_line = f"[INFO] {name}: fetched={adapter_fetched} inserted={adapter_inserted} updated={adapter_updated} unchanged={adapter_unchanged} invalid={adapter_invalid}"
            logger.info(summary_line)
        except Exception as e:
            err_msg = f"Adapter {name} fetch error: {e}"
            logger.error(err_msg)
            result.errors.append(err_msg)

    # If no new vacancies fetched in this cycle, also check unapplied / discovered vacancies in DB
    # up to batch_limit to ensure backlog can progress
    candidates_to_process: list[Vacancy] = list(fetched_vacancies)
    if not dry_run and len(candidates_to_process) < config.batch_limit:
        db_rows = list_vacancies(limit=config.batch_limit * 2)
        for row in db_rows:
            v = _row_to_vacancy(row)
            sid = v.stable_id()
            track = get_application_status(sid)
            # If already processed in terminal or ready state, skip
            if track and track.status in (
                ApplicationStatus.APPLIED,
                ApplicationStatus.SUBMITTED,
                ApplicationStatus.VERIFIED,
                ApplicationStatus.REJECTED,
                ApplicationStatus.WITHDRAWN,
                ApplicationStatus.READY_TO_APPLY,
            ):
                continue
            if not any(c.stable_id() == sid for c in candidates_to_process):
                candidates_to_process.append(v)
            if len(candidates_to_process) >= config.batch_limit:
                break

    # 3. Process candidate vacancies
    for vac in candidates_to_process:
        sid = vac.stable_id()

        # Check existing application tracking
        track = get_application_status(sid)
        if track and track.status in (
            ApplicationStatus.APPLIED,
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.VERIFIED,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
            ApplicationStatus.READY_TO_APPLY,
        ):
            # Already finalized or ready - idempotency skip
            continue

        # Canonical deduplication
        canonical_res = resolve_vacancy_identity(vac)
        canonical_id = canonical_res.canonical_id if canonical_res else None

        # Check Eligibility (Country & Remote restrictions)
        elig = get_vacancy_eligibility(sid)
        if not elig:
            assessment = assess_vacancy_eligibility(vac, candidate_country=config.candidate_country)
            if not is_dry_run():
                save_vacancy_eligibility(sid, assessment)
            elig_status = assessment.eligibility
        else:
            status_val = elig.get("status")
            if hasattr(status_val, "value"):
                status_val = status_val.value
            try:
                elig_status = EligibilityStatus(status_val)
            except Exception:
                elig_status = EligibilityStatus.UNKNOWN

        if elig_status == EligibilityStatus.INELIGIBLE:
            result.rejected_count += 1
            if not track and not is_dry_run():
                set_application_status(
                    sid,
                    ApplicationStatus.REJECTED,
                    company=vac.company,
                    title=vac.title,
                    source=vac.source,
                    vacancy_url=vac.job_url,
                    notes=f"Ineligible for candidate country {config.candidate_country}",
                )
            continue

        # Strict remote filter
        if profile.remote_required:
            is_rem, rem_reason = is_strictly_remote(vac)
            if not is_rem:
                result.rejected_count += 1
                if not track and not is_dry_run():
                    set_application_status(
                        sid,
                        ApplicationStatus.REJECTED,
                        company=vac.company,
                        title=vac.title,
                        source=vac.source,
                        vacancy_url=vac.job_url,
                        notes=f"Remote constraint violated: {rem_reason}",
                    )
                continue

        # Hard constraints filter
        hard_rej, rej_reasons = _hard_constraints(_coerce_profile(profile), vac)
        if hard_rej:
            result.rejected_count += 1
            if not track and not is_dry_run():
                set_application_status(
                    sid,
                    ApplicationStatus.REJECTED,
                    company=vac.company,
                    title=vac.title,
                    source=vac.source,
                    vacancy_url=vac.job_url,
                    notes=f"Hard constraints rejected: {', '.join(_normalize_rejection_reasons(rej_reasons))}",
                )
            continue

        # Matcher decision
        m = matcher.match(vac)
        if m.decision == "SKIP":
            result.rejected_count += 1
            if not track and not is_dry_run():
                set_application_status(
                    sid,
                    ApplicationStatus.REJECTED,
                    company=vac.company,
                    title=vac.title,
                    source=vac.source,
                    vacancy_url=vac.job_url,
                    match_score=float(m.score) if m.score is not None else None,
                    notes=f"Matcher SKIP: {', '.join(_normalize_rejection_reasons(m.reasons))}",
                )
            continue

        result.matched_count += 1

        # Track as DISCOVERED if not tracked yet
        if not track:
            if not is_dry_run():
                track = set_application_status(
                    sid,
                    ApplicationStatus.DISCOVERED,
                    company=vac.company,
                    title=vac.title,
                    source=vac.source,
                    vacancy_url=vac.job_url,
                    match_score=float(m.score) if m.score is not None else None,
                    notes="Discovered by Watcher",
                )

        # 4. Deep Analysis
        deep_row = get_deep_analysis(sid)
        deep_res = None
        if deep_row and deep_row[4]:
            try:
                deep_res = DeepAnalysisResult.model_validate_json(deep_row[4])
            except Exception:
                try:
                    deep_res = DeepAnalysisResult.model_validate(json.loads(deep_row[4]))
                except Exception:
                    pass

        deep_analysis_error = None
        if deep_res is None:
            try:
                deep_res = analyze_job_deep(vac, profile, m)
            except Exception as deep_exc:
                deep_analysis_error = deep_exc
                result.errors.append(f"Deep analysis failed for {sid}: {deep_exc}")
                deep_res = None

        if deep_res is not None and not is_dry_run():
            save_deep_analysis(
                sid,
                ANALYZER_VERSION,
                int(deep_res.fit_score),
                deep_res.recommendation,
                deep_res.model_dump_json(),
            )
            try:
                track = transition_application(
                    sid,
                    ApplicationStatus.ANALYZED,
                    note="Watcher deep analysis completed",
                    match_score=float(m.score) if m.score is not None else None,
                    deep_score=float(deep_res.fit_score) if deep_res.fit_score is not None else None,
                )
            except Exception:
                pass

        if deep_res is None:
            # Transient LLM failure — mark retryable instead of permanent rejection.
            if not is_dry_run():
                try:
                    set_application_status(
                        sid,
                        ApplicationStatus.ANALYSIS_FAILED,
                        company=vac.company,
                        title=vac.title,
                        source=vac.source,
                        vacancy_url=vac.job_url,
                        match_score=float(m.score) if m.score is not None else None,
                        notes=f"Deep analysis transient failure: {deep_analysis_error}" if deep_analysis_error else "Deep analysis unavailable",
                    )
                except Exception:
                    pass
            result.analyzed_count += 0
            continue

        result.analyzed_count += 1

        if deep_res.recommendation == "SKIP":
            result.rejected_count += 1
            continue

        # 5. Application Package Preparation
        pkg_row = get_application_package(sid)
        pkg = None
        if pkg_row and pkg_row[2]:
            try:
                pkg = ApplicationPackage.model_validate_json(pkg_row[2])
            except Exception:
                try:
                    pkg = ApplicationPackage.model_validate(json.loads(pkg_row[2]))
                except Exception:
                    pass

        if pkg is None:
            pkg = prepare_application(vac, deep_res, profile)
            if pkg is not None and not is_dry_run():
                save_application_package(
                    sid,
                    pkg.generator_version or APPLICATION_PREP_VERSION,
                    pkg.model_dump_json(),
                )
                try:
                    track = transition_application(
                        sid,
                        ApplicationStatus.READY_TO_APPLY,
                        note="Watcher application package prepared",
                        match_score=float(m.score) if m.score is not None else None,
                        deep_score=float(deep_res.fit_score) if deep_res.fit_score is not None else None,
                    )
                except Exception:
                    pass

        if pkg is None:
            continue

        result.prepared_count += 1

        # 6. Queue item & Review Gate
        priority, comps, reasons, warnings = compute_priority(
            vac,
            profile,
            float(m.score) if m.score is not None else None,
            float(deep_res.fit_score) if deep_res.fit_score is not None else None,
            deep_res,
        )

        qitem = QueueItem(
            vacancy_stable_id=sid,
            canonical_id=canonical_id or sid,
            representative_vacancy_stable_id=sid,
            priority_score=priority,
            match_score=float(m.score) if m.score is not None else None,
            deep_score=float(deep_res.fit_score) if deep_res.fit_score is not None else None,
            company=vac.company,
            title=vac.title,
            source=vac.source,
            vacancy_url=vac.job_url,
            reasons=reasons[:5],
            warnings=warnings[:5],
            rank=0,
            components=comps,
            application_strategy=deep_res.application_strategy,
            generated_at=datetime.utcnow().isoformat(),
            queue_version=QUEUE_VERSION,
        )
        if not is_dry_run():
            save_queue_item(qitem)

        # Build review information & check for unresolved items
        prepared_answers: list[dict[str, str]] = []
        unresolved_questions: list[str] = []
        for ans in getattr(pkg, "answers", []) or []:
            if getattr(ans, "requires_review", True) or not getattr(ans, "answer", None):
                unresolved_questions.append(getattr(ans, "question_id", "unknown"))
            else:
                prepared_answers.append({
                    "question_id": str(getattr(ans, "question_id", "")),
                    "value": str(getattr(ans, "answer", "")),
                })

        pkg_review_reasons = list(getattr(pkg, "review_reasons", []) or [])
        if pkg_review_reasons:
            unresolved_questions.extend(pkg_review_reasons)

        # Check CDP / browser target if evaluate_fn or cdp_url is provided
        cdp_error = False
        if config.evaluate_fn is not None:
            try:
                res = config.evaluate_fn("JSON.stringify({url: location.href})")
                parsed = json.loads(res)
                if not parsed.get("url"):
                    cdp_error = True
            except Exception:
                cdp_error = True

        # Determine review state
        if cdp_error:
            final_status = WatcherStatus.BLOCKED.value
            stop_reason = "CDP/Browser target unavailable or ambiguous - failed closed"
            result.blocked_count += 1
        elif unresolved_questions or elig_status == EligibilityStatus.UNKNOWN:
            final_status = WatcherStatus.NEEDS_HUMAN_REVIEW.value
            stop_reason = f"Stopped before submit: unresolved questions or validation needed ({len(unresolved_questions)} items)"
            result.needs_human_review_count += 1
        else:
            final_status = WatcherStatus.READY_FOR_REVIEW.value
            stop_reason = "Stopped before submit at Human Review Gate: ready for explicit human review"
            result.ready_for_review_count += 1

        # Create/save ApplicationReview in DB
        if not is_dry_run():
            app_rev = ApplicationReview(
                vacancy_stable_id=sid,
                company=vac.company,
                title=vac.title,
                source=vac.source,
                vacancy_url=vac.job_url,
                match_score=float(m.score) if m.score is not None else None,
                deep_score=float(deep_res.fit_score) if deep_res.fit_score is not None else None,
                priority_score=float(priority),
                rank=qitem.rank,
                application_strategy=deep_res.application_strategy,
                resume_summary=pkg.resume_summary,
                tailored_skills=pkg.tailored_skills,
                relevant_experience=pkg.relevant_experience,
                cover_letter=pkg.cover_letter,
                fields_filled=[a["question_id"] for a in prepared_answers],
                fields_skipped=unresolved_questions,
                warnings=warnings + pkg.warnings,
                status=ReviewStatus.PENDING_REVIEW,
                note=stop_reason,
                created_at=datetime.utcnow().isoformat(),
                updated_at=datetime.utcnow().isoformat(),
            )
            save_application_review(app_rev)

        watcher_item = WatcherItem(
            vacancy_stable_id=sid,
            canonical_id=canonical_id,
            title=vac.title or "",
            company=vac.company or "",
            url=vac.job_url or "",
            source=vac.source or "",
            status=final_status,
            match_decision=m.decision,
            match_score=float(m.score) if m.score is not None else None,
            deep_fit_score=float(deep_res.fit_score) if deep_res.fit_score is not None else None,
            why_fit=list(deep_res.why_fit or []),
            prepared_answers=prepared_answers,
            unresolved_questions=unresolved_questions,
            review_reasons=pkg_review_reasons,
            warnings=warnings + list(pkg.warnings or []),
            review_gate_id=f"review_{sid}",
            stop_reason=stop_reason,
            submit_attempted=False,  # HARD INVARIANT: NEVER TRUE
            submit_count=0,
            click_count=0,
        )
        result.items.append(watcher_item)

    # Sort persisted queue items only for a real local-state run. Dry-run must
    # not initialize or read a production DB as a hidden side effect.
    if not dry_run:
        all_queue = list_queue(limit=100)
        all_queue.sort(key=lambda x: (-x.priority_score, -(x.deep_score or 0), -(x.match_score or 0), x.vacancy_stable_id))
        for idx, item in enumerate(all_queue, start=1):
            item.rank = idx
            save_queue_item(item)

    return result


class Watcher:
    """Watcher service for running periodic polling loops."""

    def __init__(self, config: WatcherConfig | None = None):
        self.config = config or WatcherConfig()
        self._running = False

    def poll_once(self, iteration: int = 1, dry_run: bool = False) -> WatcherCycleResult:
        """Run a single polling cycle."""
        return run_watcher_cycle(self.config, iteration=iteration, dry_run=dry_run)

    def run(self, stop_callback: Callable[[], bool] | None = None) -> list[WatcherCycleResult]:
        """Run periodic polling until stopped or max_iterations reached."""
        self._running = True
        results: list[WatcherCycleResult] = []
        iteration = 0

        while self._running:
            iteration += 1
            res = self.poll_once(iteration=iteration)
            results.append(res)

            if self.config.max_iterations and iteration >= self.config.max_iterations:
                break

            if stop_callback and stop_callback():
                break

            time.sleep(self.config.poll_interval_seconds)

        self._running = False
        return results

    def stop(self) -> None:
        self._running = False
