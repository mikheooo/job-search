"""CLI-команды контура Gig Search (``ai_assistant.cli gig ...``).

Подготовлено по ТЗ GIG-SEARCH-P1:
- ``gig run``         — прогон pipeline по источникам (по умолчанию fixture);
- ``gig list``        — ранжированный список сохранённых гигов;
- ``gig set-status``  — человеческое решение NEW/SHORTLISTED/SKIPPED.

Контур prepare-only: ни одна команда ничего не отправляет наружу.
Единственные побочные эффекты — записи в таблицах ``gigs``/``gig_events``
(и то подавляются ``--no-persist``/dry-run).
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from .models import Gig
from .pipeline import run_fixture_pipeline, run_source_pipeline
from .store import get_gig, gig_stats, list_gig_events, list_gigs, set_gig_status


def _gig_brief(gig: Gig) -> dict[str, Any]:
    """Компактная выдача одного гига для CLI/UI-ответа (ТЗ §9)."""
    return {
        "id": gig.id,
        "title": gig.title,
        "source": gig.source,
        "source_mode": gig.source_mode.value,
        "source_scope": gig.source_scope,
        "is_authenticated_feed": gig.source_scope == "authenticated_feed",
        "source_status": gig.source_status,
        "is_live": gig.source_mode.value == "live",
        "url": gig.url,
        "budget": gig.budget_text,
        "budget_type": gig.budget_type.value,
        "currency": gig.currency,
        "opportunity_score": gig.opportunity_score,
        "match_score": gig.match_score,
        "can_deliver": gig.can_deliver.value,
        "ai_automation_potential": gig.ai_automation_potential,
        "estimated_effort_hours": gig.estimated_effort_hours,
        "effective_hourly_rate": gig.effective_hourly_rate,
        "competition": gig.competition,
        "client_quality": gig.client_quality,
        "payment_readiness": gig.payment_readiness.value,
        "platform_payment_options": list(gig.platform_payment_options),
        "risk_level": gig.risk_level.value,
        "risks": list(gig.risks),
        "why_matched": list(gig.why_matched),
        "why_risky": list(gig.why_risky),
        "rejection_reasons": list(gig.rejection_reasons),
        "score_breakdown": dict(gig.score_breakdown),
        "status": gig.status.value,
        "published_at": gig.published_at,
        "first_seen_at": gig.first_seen_at,
        "last_seen_at": gig.last_seen_at,
        "fetched_at": gig.fetched_at,
        # P3 §5: реальные факты источника отдаются отдельно, чтобы UI/API показывали
        # то, что источник действительно сообщил (ставки, срок, статус клиента), а не
        # пересказ. Пустой словарь значит «источник ничего не дал».
        "source_facts": dict(gig.source_facts),
        "derived_fields": list(gig.derived_fields),
        "source_url": gig.source_url,
    }


def _print_gig_table(gigs: list[Gig]) -> None:
    if not gigs:
        print("no gigs found")
        return
    header = (
        f"{'score':>5}  {'deliver':<7} {'pay':<8} {'risk':<6} "
        f"{'eff.h':>5} {'rate/h':>7} {'budget':<15} {'source':<8} title"
    )
    print(header)
    print("-" * len(header))
    for gig in gigs:
        score = f"{gig.opportunity_score:.0f}" if gig.opportunity_score is not None else "—"
        effort = f"{gig.estimated_effort_hours:g}" if gig.estimated_effort_hours is not None else "—"
        rate = f"{gig.effective_hourly_rate:g}" if gig.effective_hourly_rate is not None else "—"
        budget = gig.budget_text[:15]
        title = gig.title[:60] + ("…" if len(gig.title) > 60 else "")
        print(
            f"{score:>5}  {gig.can_deliver.value:<7} {gig.payment_readiness.value:<8} "
            f"{gig.risk_level.value:<6} {effort:>5} {rate:>7} {budget:<15} {gig.source:<8} {title}"
        )
        if gig.rejection_reasons:
            print(f"      rejected: {', '.join(gig.rejection_reasons)}")


def _run_command(args: argparse.Namespace) -> int:
    result = run_fixture_pipeline(
        fixture_path=args.dataset,
        limit=args.limit,
        persist=not args.no_persist,
        min_budget=args.min_budget,
    )

    if args.json:
        payload = {
            "stats": result.stats(),
            "persisted": result.persisted,
            "errors": [{"id": gid, "error": msg} for gid, msg in result.error_records],
            "ranked_gigs": [_gig_brief(g) for g in result.ranked_gigs],
            "rejected_gigs": [_gig_brief(g) for g in result.rejected_gigs],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    mode = "DRY-RUN (nothing persisted)" if not result.persisted else "persisted"
    print(f"[gig] pipeline run complete ({mode})")
    stats = result.stats()
    print(
        f"  total: {stats['total']}  accepted: {stats['accepted']}  "
        f"rejected: {stats['rejected']}  duplicates: {stats['duplicates']}  "
        f"errors: {stats['errors']}"
    )
    if result.error_records:
        print("  errors:")
        for gid, msg in result.error_records[:10]:
            print(f"    - {gid}: {msg}")
    print()
    print("top gigs by opportunity_score:")
    _print_gig_table(result.ranked_gigs[:20])
    if result.rejected_gigs:
        print()
        print("rejected (hard gates):")
        for gig in result.rejected_gigs[:15]:
            reasons = ", ".join(gig.rejection_reasons) or "—"
            print(f"  - [{reasons}] {gig.title[:70]}")
    return 0


def _list_command(args: argparse.Namespace) -> int:
    gigs = list_gigs(
        limit=args.limit,
        status=args.status,
        source=args.source,
        include_rejected=args.include_rejected,
        order_by=args.order,
        source_mode=getattr(args, "source_mode", None),
        source_scope=getattr(args, "scope", None),
        exclude_scope=getattr(args, "exclude_scope", None),
    )
    if args.json:
        print(json.dumps([_gig_brief(g) for g in gigs], indent=2, ensure_ascii=False))
        return 0
    stats = gig_stats()
    by_mode = stats.get("by_source_mode") or {}
    mode_text = " ".join(f"{k}={v}" for k, v in sorted(by_mode.items())) or "none"
    print(
        f"[gig] stored: total={stats.get('total', 0)} accepted={stats.get('accepted', 0)} "
        f"rejected={stats.get('rejected', 0)} duplicates={stats.get('duplicates', 0)}"
    )
    print(f"[gig] by source mode: {mode_text}")
    by_scope = stats.get("by_source_scope") or {}
    scope_text = " ".join(f"{k}={v}" for k, v in sorted(by_scope.items())) or "none"
    print(f"[gig] by source scope: {scope_text}")
    print()
    _print_gig_table(gigs)
    return 0


def _print_gig_detail(gig: Gig) -> None:
    """Полная карточка гига с breakdown (ТЗ §9, `gig show`)."""
    print(f"{gig.title}")
    print(f"  id:       {gig.id}")
    print(f"  source:   {gig.source}:{gig.source_id}")
    print(f"  url:      {gig.url or '—'}")
    print(f"  budget:   {gig.budget_text} ({gig.budget_type.value})")
    print(f"  status:   {gig.status.value}  published: {gig.published_at or '—'}")
    print()
    score = f"{gig.opportunity_score:.1f}" if gig.opportunity_score is not None else "—"
    match = f"{gig.match_score:.1f}" if gig.match_score is not None else "—"
    print(f"  opportunity_score: {score}   match_score: {match}")
    print(
        f"  can_deliver={gig.can_deliver.value}  ai_potential={gig.ai_automation_potential}  "
        f"effort={gig.estimated_effort_hours}h  rate={gig.effective_hourly_rate}/h"
    )
    print(
        f"  client_quality={gig.client_quality}  competition={gig.competition}  "
        f"payment={gig.payment_readiness.value}  risk={gig.risk_level.value}"
    )
    if gig.rejection_reasons:
        print(f"  rejected: {', '.join(gig.rejection_reasons)}")
    if gig.why_matched:
        print("  why matched:")
        for item in gig.why_matched:
            print(f"    + {item}")
    if gig.why_risky:
        print("  why risky:")
        for item in gig.why_risky:
            print(f"    - {item}")
    if gig.risks:
        print("  risks:")
        for item in gig.risks:
            print(f"    ! {item}")
    if gig.score_breakdown:
        components = gig.score_breakdown.get("components") or {}
        weights = gig.score_breakdown.get("weights") or {}
        notes = gig.score_breakdown.get("notes") or {}
        if components:
            print("  score breakdown:")
            for name, value in components.items():
                weight = weights.get(name, "?")
                note = notes.get(name, "")
                print(f"    {name:<24} {value:>6}  x{weight}  {note}")
            if "rejected_before_scoring" in gig.score_breakdown:
                print("    (rejected before scoring)")


def _show_command(args: argparse.Namespace) -> int:
    gig = get_gig(args.gig_id)
    if gig is None:
        print(f"[gig] gig not found: {args.gig_id}")
        return 1
    if args.json:
        payload = _gig_brief(gig)
        payload["events"] = list_gig_events(gig.id)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    _print_gig_detail(gig)
    return 0


def _stats_command(args: argparse.Namespace) -> int:
    stats = gig_stats()
    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0
    print("[gig] pipeline statistics")
    print(
        f"  total: {stats.get('total', 0)}  accepted: {stats.get('accepted', 0)}  "
        f"rejected: {stats.get('rejected', 0)}  duplicates: {stats.get('duplicates', 0)}"
    )
    by_status = stats.get("by_status") or {}
    if by_status:
        print("  by status: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    by_source = stats.get("by_source") or {}
    if by_source:
        print("  by source: " + ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())))
    by_reason = stats.get("by_rejection_reason") or {}
    if by_reason:
        print("  rejection reasons:")
        for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            print(f"    {reason:<24} {count}")
    return 0


def _build_source(args: argparse.Namespace):
    """Собрать источник из аргументов CLI.

    ``contra`` — реальный live-источник (P2). ``contra-auth`` — авторизованная
    лента (P2.1). ``freelancer`` — публичная выдача Freelancer.com (P3, без
    логина). ``fixture`` — синтетический датасет P1 (тесты и regression).
    Никакие другие значения не угадываются.
    """
    name = str(getattr(args, "source", None) or "fixture").strip().lower()
    if name == "freelancer":
        # P3: публичный список /jobs + публичные страницы проектов. Только GET,
        # логина нет, robots.txt соблюдается, пагинация — путём (/jobs/N), потому
        # что query-форму robots.txt запрещает.
        from .sources.freelancer import FreelancerGigSource

        # P5: адресный сбор по целевым направлениям (--skills). Общую ленту
        # можно отключить: она даёт рынок, а цель — целенаправленный поиск.
        skills_arg = getattr(args, "skills", None)
        skills = None
        if skills_arg is not None:
            skills = [s.strip() for s in str(skills_arg).split(",") if s.strip()]
        return FreelancerGigSource(
            listing_pages=int(getattr(args, "listing_pages", 2) or 2),
            delay_seconds=float(getattr(args, "delay", 1.0) or 0.0),
            snapshot_path=getattr(args, "snapshot", None),
            listing_snapshot_path=getattr(args, "listing_snapshot", None),
            target_skills=skills,
            skill_pages=int(getattr(args, "skill_pages", 1) or 1),
            include_general_listing=not bool(getattr(args, "no_general_listing", False)),
        )
    if name == "contra":
        from .sources.contra import ContraGigSource

        pages = None
        pages_arg = getattr(args, "pages", None)
        if pages_arg:
            pages = [p.strip() for p in str(pages_arg).split(",") if p.strip()]
        return ContraGigSource(
            pages=pages,
            delay_seconds=float(getattr(args, "delay", 1.0) or 0.0),
            max_pages=int(getattr(args, "max_pages", 40) or 40),
            snapshot_path=getattr(args, "snapshot", None),
        )
    if name == "contra-auth":
        # P2.1: the authenticated /jobs feed, read through the user's own
        # already-open browser session. No login is automated; see contra_auth.py.
        from .sources.contra_auth import ContraAuthenticatedSource

        evaluate = _auth_evaluate()
        return ContraAuthenticatedSource(
            evaluate,
            preset=str(getattr(args, "preset", None) or "ALL"),
            sort=str(getattr(args, "sort", None) or "NEWEST"),
        )
    if name == "kwork":
        # Phase 4C §4: curated public project pages (no login, no listing
        # crawl — the exchange listing is a JS shell). URLs come from
        # --urls/--urls-file (manual curation or user-pasted links).
        from .sources.kwork import KworkGigSource

        urls_arg = getattr(args, "urls", None)
        urls = [u.strip() for u in str(urls_arg or "").split(",") if u.strip()]
        return KworkGigSource(
            urls=urls or None,
            urls_file=getattr(args, "urls_file", None),
            snapshot_dir=getattr(args, "snapshot_dir", None),
            delay_seconds=float(getattr(args, "delay", 2.0) or 0.0),
        )
    if name == "fixture":
        from .sources.fixture import FixtureGigSource

        return FixtureGigSource(getattr(args, "dataset", None))
    raise SystemExit(
        f"unknown gig source: {name!r} (expected 'contra', 'contra-auth', "
        f"'freelancer', 'kwork' or 'fixture')"
    )


def _auth_evaluate():
    """Bind a read-only evaluator to the user's logged-in Contra tab.

    Reuses the project's existing CDP transport (``make_cdp_evaluate``) instead of
    reimplementing browser plumbing. Fails with a message a human can act on when
    no logged-in Contra tab is present - never by opening a login form.
    """
    import os

    from ..prefill_execute import make_cdp_evaluate

    cdp_url = (os.getenv("CONTRA_CDP_URL") or "").strip() or "http://127.0.0.1:9113"
    try:
        return make_cdp_evaluate(cdp_url, "contra.com")
    except Exception as exc:  # noqa: BLE001 - surfacing the actionable cause matters
        raise SystemExit(
            "contra-auth: no logged-in Contra tab found on "
            f"{cdp_url} ({type(exc).__name__}: {exc}).\n"
            "  Open the dedicated browser profile and log in by hand first:\n"
            "    chrome --remote-debugging-port=9113 "
            '--user-data-dir="%LOCALAPPDATA%\\JobSearchContra\\User Data" '
            "https://contra.com/jobs\n"
            "  The session persists in that profile; a new login is only needed "
            "when Contra invalidates it."
        ) from exc


def _fetch_command(args: argparse.Namespace) -> int:
    """``gig fetch <source>`` — обновить набор из источника (P2 §8, §9).

    Повторный запуск не создаёт дубли, обновляет существующие гиги, добавляет
    новые и НЕ трогает пользовательские статусы. Записи, исчезнувшие с текущей
    страницы источника, не удаляются: они остаются с прежним last_seen_at.
    """
    from .sources.contra import ContraUnavailable
    from .sources.kwork import KworkUnavailable

    source = _build_source(args)
    source_name = getattr(source, "key", "?")

    try:
        result = run_source_pipeline(
            source,
            limit=args.limit,
            persist=not args.no_persist,
            min_budget=args.min_budget,
            include_closed=bool(getattr(args, "include_closed", False)),
        )
    except (ContraUnavailable, KworkUnavailable) as exc:
        # P2 §9: недоступность источника не должна чистить БД и не должна
        # ломать уже сохранённые результаты. Сообщаем явно и выходим кодом 1.
        print(f"[gig] {source_name} unavailable: {exc}")
        stats = gig_stats()
        print(
            "  stored gigs preserved: total={} accepted={} rejected={}".format(
                stats.get("total", 0), stats.get("accepted", 0), stats.get("rejected", 0)
            )
        )
        print(f"  error detail: {'; '.join(getattr(source, 'last_fetch_errors', [])[:3])}")
        return 1

    if args.json:
        payload = {
            "source": source_name,
            "stats": result.stats(),
            "persisted": result.persisted,
            "errors": [{"id": gid, "error": msg} for gid, msg in result.error_records],
            "ranked_gigs": [_gig_brief(g) for g in result.ranked_gigs],
        }
        try:
            payload["source_health"] = source.health().to_dict()
        except Exception:
            payload["source_health"] = {"status": "UNKNOWN", "produced": 0, "errors": []}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    mode = "DRY-RUN (nothing persisted)" if not result.persisted else "persisted"
    print(f"[gig] fetch complete: source={source_name} ({mode})")
    stats = result.stats()
    print(
        f"  fetched: {stats['total']}  accepted: {stats['accepted']}  "
        f"rejected: {stats['rejected']}  duplicates: {stats['duplicates']}  "
        f"errors: {stats['errors']}"
    )
    print(f"  live: {stats.get('live', 0)}  fixture: {stats.get('fixture', 0)}")
    try:
        health = source.health().to_dict()
    except Exception:
        health = {"status": "UNKNOWN", "produced": 0, "errors": []}
    print(f"  source health: {health.get('status')} "
          f"(produced={health.get('produced', 0)}, errors={len(health.get('errors', []))})")
    for gid, msg in result.error_records[:10]:
        print(f"    - {gid}: {msg}")
    print()
    print("top gigs:")
    _print_gig_table(result.ranked_gigs[:15])
    if result.rejected_gigs:
        print()
        print("rejected:")
        for gig in result.rejected_gigs[:10]:
            print(f"  - [{', '.join(gig.rejection_reasons)}] {gig.title[:70]}")
    return 0


def _set_status_command(args: argparse.Namespace) -> int:
    updated = set_gig_status(args.gig_id, args.status, note=args.note)
    if updated is None:
        print(f"[gig] gig not found (or dry-run): {args.gig_id}")
        return 1
    print(f"[gig] {updated.id}: status -> {updated.status.value}")
    return 0


def run_gig_command(args: argparse.Namespace) -> int:
    """Точка входа из ``ai_assistant.cli`` (dispatch по ``args.gig_command``)."""
    if getattr(args, "gig_command", None) in {"mark-submitted", "mark-replied", "mark-interview", "mark-awarded", "mark-rejected", "mark-closed", "mark-paid", "funnel"}:
        from .submission_cli import run_tracking_command
        return run_tracking_command(args)
    if getattr(args, "gig_command", None) == "run":
        return _run_command(args)
    if getattr(args, "gig_command", None) == "list":
        return _list_command(args)
    if getattr(args, "gig_command", None) == "set-status":
        return _set_status_command(args)
    if getattr(args, "gig_command", None) == "show":
        return _show_command(args)
    if getattr(args, "gig_command", None) == "stats":
        return _stats_command(args)
    if getattr(args, "gig_command", None) == "fetch":
        return _fetch_command(args)
    print("usage: gig {run,fetch,list,show,stats,set-status} ...")
    return 2


__all__ = ["run_gig_command", "_gig_brief", "_print_gig_detail", "_print_gig_table"]
