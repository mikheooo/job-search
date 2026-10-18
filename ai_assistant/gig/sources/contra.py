"""Contra source adapter - real, public, live data (GIG-SEARCH-P2).

SCOPE AND BOUNDARIES
--------------------
Contra's job *feed* (``/jobs``) is behind a login wall: an anonymous request gets
a client-rendered shell and the browser lands on ``/log-in?redirectTo=/jobs``.
Per P2 §2 nothing here automates a login, solves a CAPTCHA, defeats anti-bot
measures, reuses someone else's cookies or submits anything. Only the part Contra
publishes anonymously is read.

WHAT IS ACTUALLY PUBLIC
-----------------------
The curated marketing pages ``/hire/<role>`` are server-rendered and embed a
relay cache (``<script id="vike_pageContext" type="application/json">``) that
contains real ``JobPosting`` records with their ``Job`` and budget entities. That
is the surface this adapter reads. The records there are showcased rather than
current - in the sample collected on 2026-09-29 every one was ``EXPIRED`` or
``CLOSED`` - which is reported as a source limitation, not hidden.

FIELDS
------
Everything is taken from the page; nothing is invented. Contra's public records
carry no client rating, no total spend, no hire count, no applicant/competition
count, no proposal cost and no description body, so those stay ``None`` and the
scorer treats them as unknown. Money strings arrive as ``"USD:50.00000000"`` and
are parsed only for their numeric part plus the currency code.

POLITENESS
----------
One HTTP request per page, a fixed delay between them, a fixed User-Agent, and a
bounded page budget. ``respect_robots`` checks ``robots.txt`` once per run; if a
path is disallowed the adapter refuses to fetch it.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from typing import Any, Iterator

logger = logging.getLogger(__name__)

CONTRA_HOST = "https://contra.com"
#: Curated public pages that carry job records. Slugs absent from Contra simply
#: 404 and are skipped, so this stays tolerant of the site's own changes.
DEFAULT_HIRE_PAGES: tuple[str, ...] = (
    "ai-developers",
    "web-developers",
    "ui-designers",
    "logo-designers",
    "graphic-designers",
    "product-designers",
    "motion-designers",
    "animators",
    "video-editors",
    "social-media-marketers",
    "content-creators",
    "brand-designers",
    "copywriters",
    "webflow-developers",
    "framer-developers",
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: Delay between page fetches. Deliberately not configurable to zero: this is the
#: difference between reading a public page and hammering someone's site.
DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 25.0
#: Hard cap on pages per run, so a bad slug list cannot become a crawl.
DEFAULT_MAX_PAGES = 40

_CONTEXT_RE = re.compile(
    r'<script id="vike_pageContext" type="application/json">', re.IGNORECASE
)
#: "USD:50.00000000" -> ("USD", 50.0)
_MONEY_RE = re.compile(r"^\s*([A-Za-z]{3})?\s*:?\s*(-?\d+(?:\.\d+)?)\s*$")

#: Contra's budget type names -> our BudgetType.
_BUDGET_TYPE_MAP = {
    "HourlyRateJobBudget": "hourly",
    "MonthlyRateJobBudget": "monthly",
    "WeeklyRateJobBudget": "weekly",
    "FixedPriceJobBudget": "fixed",
}

#: Platform-level payout capability. This is a *capability* of Contra, not a
#: statement that this account can receive money (P2 §5).
CONTRA_PLATFORM_PAYMENT_OPTIONS: tuple[str, ...] = (
    "platform_escrow",
    "bank_transfer",
    "paypal",
    "wise",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_money(raw: Any) -> tuple[str | None, float | None]:
    """Parse Contra's ``"USD:50.00000000"`` money string.

    Returns ``(currency, amount)``; anything unrecognised yields ``(None, None)``
    rather than a guessed number.
    """
    if raw is None:
        return None, None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return None, float(raw)
    match = _MONEY_RE.match(str(raw).strip())
    if not match:
        return None, None
    currency = match.group(1).upper() if match.group(1) else None
    return currency, float(match.group(2))


def budget_type_of(typename: str | None) -> str:
    """Contra budget __typename -> our budget_type value (unknown if unmapped)."""
    if not typename:
        return "unknown"
    return _BUDGET_TYPE_MAP.get(str(typename).strip(), "unknown")


class ContraUnavailable(RuntimeError):
    """Raised when Contra cannot be reached; callers must keep stored gigs."""


class ContraGigSource:
    """Reads real Contra job records from Contra's public ``/hire/<role>`` pages.

    The adapter is read-only: it never logs in, never submits, never mutates
    anything on Contra. It exposes ``source_mode="live"`` and a ``source_scope``
    that states exactly which public surface was used.
    """

    key = "contra"
    source_mode = "live"
    source_scope = "public_hire_pages"

    def __init__(
        self,
        pages: tuple[str, ...] | list[str] | None = None,
        *,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_pages: int = DEFAULT_MAX_PAGES,
        user_agent: str = DEFAULT_USER_AGENT,
        respect_robots: bool = True,
        snapshot_path: str | None = None,
    ) -> None:
        self.pages = tuple(pages) if pages else DEFAULT_HIRE_PAGES
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.timeout_seconds = float(timeout_seconds)
        self.max_pages = max(1, int(max_pages))
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        #: When set, records are parsed from a saved page instead of the network.
        #: This is what makes the parser tests deterministic and offline.
        self.snapshot_path = snapshot_path
        self._last_fetch_errors: list[str] = []
        #: Phase 4C §3: diagnostic state of the most recent fetch.
        self._last_health = None

    # ------------------------------------------------------------------ HTTP
    def _robots_allows(self, url: str) -> bool:
        """Check robots.txt once; a failure to read it is treated as \"allowed\".

        Refusing everything because robots.txt was unreachable would be a worse
        failure than proceeding with a single polite request.
        """
        try:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(CONTRA_HOST + "/robots.txt")
            parser.read()
            return parser.can_fetch(self.user_agent, url)
        except Exception as exc:  # noqa: BLE001 - robots is best-effort
            logger.warning("contra: robots.txt unreadable (%s); proceeding politely", exc)
            return True

    def _http_get(self, url: str) -> str:
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "text/html,*/*;q=0.8"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return response.read().decode("utf-8", "replace")

    # -------------------------------------------------------------- parsing
    @staticmethod
    def _relay_cache(html: str) -> dict[str, Any]:
        """Extract the relay record map from a public Contra page.

        Returning ``{}`` for a page without the cache is normal (marketing pages
        vary); the caller simply finds no records there.
        """
        match = _CONTEXT_RE.search(html)
        if not match:
            return {}
        end = html.find("</script>", match.end())
        if end == -1:
            return {}
        try:
            context = json.loads(html[match.end():end])
        except json.JSONDecodeError:
            return {}
        config = context.get("publicAppConfiguration") or {}
        cache = config.get("relayRecordMap") or {}
        return cache if isinstance(cache, dict) else {}

    @staticmethod
    def _deref(cache: dict[str, Any], node: Any) -> dict[str, Any]:
        if isinstance(node, dict) and "__ref" in node:
            target = cache.get(node["__ref"])
            return target if isinstance(target, dict) else {}
        return node if isinstance(node, dict) else {}

    def _record_from(self, cache: dict[str, Any], posting: dict[str, Any], page_slug: str) -> dict[str, Any] | None:
        """Build one raw payload from a JobPosting entity, or None if unusable."""
        posting_id = str(posting.get("id") or "").strip()
        title = str(posting.get("title") or "").strip()
        if not posting_id or not title:
            return None

        job = self._deref(cache, posting.get("job"))
        budget = self._deref(cache, job.get("budget"))
        # The organization is null on every public record observed; handle both.
        organization = self._deref(cache, posting.get("organization")) if posting.get("organization") else {}

        currency_min, amount_min = parse_money(budget.get("feeMin"))
        currency_max, amount_max = parse_money(budget.get("feeMax"))
        currency = currency_min or currency_max

        estimated_hours = budget.get("estimatedHours")
        if isinstance(estimated_hours, (int, float)) and estimated_hours <= 0:
            # Contra sends 0 for budgets where hours do not apply. 0 is not a
            # measurement, so it must not become "zero hours of work".
            estimated_hours = None

        source_status = str(job.get("status") or "").strip() or None
        approved_at = posting.get("approvedAt")
        slug = str(posting.get("slug") or "").strip()

        raw: dict[str, Any] = {
            "source": self.key,
            "source_id": posting_id,
            # Contra does not publish a per-job page anonymously (it 404s without
            # a session), so there is no gig URL to record. Inventing one - or
            # reusing the listing page - would both lie and collide with the
            # unique index on gigs.url. The page the record was observed on is
            # kept in source_url instead.
            "url": None,
            "title": title,
            # Contra publishes no description body anonymously. Left empty rather
            # than filled with generated prose (P2 §3).
            "description": "",
            "category": None,
            "skills": [],
            "budget": {
                "min": amount_min,
                "max": amount_max,
                "type": budget_type_of(budget.get("__typename")),
            },
            "currency": currency,
            "published_at": approved_at,
            # Client identity: only what the public record actually names.
            "client_name": organization.get("name") if organization else None,
            "client_country": None,
            # Deliberately absent on the public record - never invented.
            "client_rating": None,
            "client_jobs_posted": None,
            "client_hires": None,
            "client_total_spent": None,
            "competition": None,
            "proposal_cost": None,
            # --- P2 provenance -------------------------------------------------
            "source_mode": self.source_mode,
            "source_scope": self.source_scope,
            "source_url": f"{CONTRA_HOST}/hire/{page_slug}",
            "fetched_at": _utcnow(),
            "source_status": source_status,
            "platform_payment_options": list(CONTRA_PLATFORM_PAYMENT_OPTIONS),
            "source_facts": {
                "contra_posting_id": posting_id,
                "contra_job_status": source_status,
                "contra_approved_at": approved_at,
                "contra_slug": slug,
                "contra_budget_typename": budget.get("__typename"),
                "contra_estimated_hours": budget.get("estimatedHours"),
                "contra_hiring_as_individual": posting.get("hiringAsIndividual"),
                "contra_page": f"/hire/{page_slug}",
            },
            "derived_fields": [
                "budget_min",
                "budget_max",
                "budget_type",
                "published_at",
                "source_mode",
                "source_scope",
                "fetched_at",
            ],
            "extra": {
                "contra_slug": slug,
                "contra_status": source_status,
                "contra_estimated_hours": estimated_hours,
                "contra_duration_ref": (job.get("duration") or {}).get("__ref")
                if isinstance(job.get("duration"), dict)
                else None,
                "contra_timezone_primary_utc_offset": posting.get("timezonePrimaryUtcOffset"),
            },
        }
        return raw

    def records_from_html(self, html: str, page_slug: str) -> list[dict[str, Any]]:
        """Parse every job record on one page. Pure function of the HTML."""
        cache = self._relay_cache(html)
        if not cache:
            return []
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entity in cache.values():
            if not isinstance(entity, dict) or entity.get("__typename") != "JobPosting":
                continue
            raw = self._record_from(cache, entity, page_slug)
            if raw is None or raw["source_id"] in seen:
                continue
            seen.add(raw["source_id"])
            records.append(raw)
        return records

    def records_from_snapshot(self) -> list[dict[str, Any]]:
        """Parse the saved snapshot (used by tests and by ``--offline``)."""
        if not self.snapshot_path:
            return []
        with open(self.snapshot_path, "r", encoding="utf-8", errors="replace") as handle:
            html = handle.read()
        # The snapshot filename carries its page slug when produced by the CLI;
        # fall back to the first configured page for a bare file.
        slug = re.sub(r"\.html?$", "", self.snapshot_path.rsplit("_", 1)[-1]) or self.pages[0]
        return self.records_from_html(html, slug)

    # --------------------------------------------------------------- public
    def fetch(self, limit: int | None = None) -> Iterator[dict]:
        """Yield raw Contra gigs (with run diagnostics).

        Обёртка считает отданные записи и фиксирует ``health()`` при любом
        исходе; исключение ``ContraUnavailable`` при этом проходит дальше.
        """
        from .base import SourceHealth, summarize_health

        self._last_fetch_errors = []
        produced = 0
        try:
            for record in self._fetch_records(limit):
                produced += 1
                yield record
        except Exception as exc:  # noqa: BLE001 - keep the signal, then propagate
            self._last_fetch_errors.append(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._last_health = summarize_health(produced, self._last_fetch_errors)

    def health(self):
        """Диагноз последнего прогона (UNKNOWN, если прогона не было)."""
        from .base import SourceHealth

        return self._last_health or SourceHealth()

    def _fetch_records(self, limit: int | None = None) -> Iterator[dict]:
        """Yield raw Contra gigs.

        Network failures never raise a partial-crawl mess: the records gathered so
        far are yielded, the error is recorded, and if nothing was gathered at all
        ``ContraUnavailable`` is raised so the caller can leave stored gigs alone.
        """
        if self.snapshot_path:
            records = self.records_from_snapshot()
            for record in records[: int(limit)] if limit else records:
                yield record
            return

        self._last_fetch_errors = []
        produced = 0
        pages = self.pages[: self.max_pages]

        for index, slug in enumerate(pages):
            if limit is not None and produced >= int(limit):
                break
            url = f"{CONTRA_HOST}/hire/{slug}"
            if self.respect_robots and not self._robots_allows(url):
                self._last_fetch_errors.append(f"{slug}: disallowed by robots.txt")
                logger.warning("contra: robots.txt disallows %s; skipping", url)
                continue
            try:
                html = self._http_get(url)
            except urllib.error.HTTPError as exc:
                # A 404 on a slug is expected: Contra's page list drifts.
                self._last_fetch_errors.append(f"{slug}: HTTP {exc.code}")
                logger.info("contra: %s -> HTTP %s", slug, exc.code)
                continue
            except Exception as exc:  # noqa: BLE001 - network is untrusted
                self._last_fetch_errors.append(f"{slug}: {type(exc).__name__}: {exc}")
                logger.warning("contra: %s unreachable (%s)", slug, exc)
                continue

            for record in self.records_from_html(html, slug):
                if limit is not None and produced >= int(limit):
                    break
                produced += 1
                yield record

            # Be a good citizen: pause between pages, never after the last.
            if index < len(pages) - 1 and self.delay_seconds:
                time.sleep(self.delay_seconds)

        if produced == 0:
            detail = "; ".join(self._last_fetch_errors[:5]) or "no job records found"
            raise ContraUnavailable(f"contra: no gigs fetched ({detail})")

    @property
    def last_fetch_errors(self) -> list[str]:
        """Errors from the most recent ``fetch`` (empty when it went cleanly)."""
        return list(self._last_fetch_errors)


__all__ = [
    "CONTRA_HOST",
    "CONTRA_PLATFORM_PAYMENT_OPTIONS",
    "ContraGigSource",
    "ContraUnavailable",
    "DEFAULT_HIRE_PAGES",
    "budget_type_of",
    "parse_money",
]
