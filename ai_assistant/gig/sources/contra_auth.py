"""Contra authenticated live source (GIG-SEARCH-P2.1).

WHY THIS EXISTS
---------------
P2 concluded "Contra does not publish open jobs" from the *anonymous* surface.
That conclusion was incomplete: after an ordinary user login, ``https://contra.com/jobs``
opens and the site itself reports 233 open postings. This module reads that
authenticated feed, so the P2 verdict can be corrected with evidence rather than
left standing on an untested assumption.

SCOPE AND BOUNDARIES (P2.1 §3, §6)
----------------------------------
* The session is the user's own, created by hand in a visible browser. This module
  never performs a login, never solves a CAPTCHA/2FA, never copies another
  profile's cookies and never touches anti-bot measures.
* It is strictly read-only: only GraphQL *queries* are issued, never a mutation.
  No application is submitted, nothing in the account is changed. ``Apply`` is
  never clicked.
* The plan limit is respected, not worked around. Contra's free tier returns 3 of
  233 postings; the API reports the true total and paginates to nothing extra.
  This module reports that boundary. It does not sweep hundreds of search terms to
  reconstruct the paid list, because that would defeat the limit rather than use
  the product.

WHAT IS READABLE
----------------
``JobFeed_jobFeedPaginationQuery`` (POST /api/?operationName=...) returns, per
record: job id (stable base64 ``["Job",<n>]``), title, slug, createdAt, budget
(``feeMin``/``feeMax``/``__typename``), duration, organization, roles and tools.
No description body and no per-job page: ``/jobs/<slug>`` 404s even authenticated,
so ``url`` stays empty rather than pointing at a route that does not exist.

ARCHITECTURE NOTE
-----------------
The retrieval path is deliberately split from the parsing path. ``records_from_payload``
is a pure function of the JSON the feed returned, which is what the tests exercise
offline; the CDP/browser plumbing only feeds it. The existing project transport
(``make_cdp_evaluate``) is reused rather than reimplemented.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterator

logger = logging.getLogger(__name__)

CONTRA_HOST = "https://contra.com"
SOURCE_KEY = "contra"
#: Distinguishes the authenticated feed from the archive read in P2.
SOURCE_SCOPE = "authenticated_feed"
SOURCE_MODE = "live"

#: The persisted Relay operation the /jobs page itself calls.
JOB_FEED_DOC_ID = "bd0d3aa180ccca2b973c48ab245fab2a"
JOB_FEED_OPERATION = "JobFeed_jobFeedPaginationQuery"
JOB_FEED_URL = f"/api/?operationName={JOB_FEED_OPERATION}"

#: Contra's budget type names -> our BudgetType values.
_BUDGET_TYPE_MAP = {
    "HourlyRateJobBudget": "hourly",
    "MonthlyRateJobBudget": "monthly",
    "WeeklyRateJobBudget": "weekly",
    "FixedPriceJobBudget": "fixed",
}

#: Platform-level payout capability. A capability of Contra - never evidence that
#: this account is verified to receive money (P2 §5).
CONTRA_PLATFORM_PAYMENT_OPTIONS: tuple[str, ...] = (
    "platform_escrow",
    "bank_transfer",
    "paypal",
    "wise",
)

_MONEY_RE = re.compile(r"^\s*([A-Za-z]{3})?\s*:?\s*(-?\d+(?:\.\d+)?)\s*$")
_NUMERIC_ID_RE = re.compile(r"\d+")


class ContraAuthUnavailable(RuntimeError):
    """Raised when the authenticated feed cannot be read.

    Callers must treat this as "leave stored gigs alone": an inaccessible session
    is not evidence that a gig disappeared.
    """


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_money(raw: Any) -> tuple[str | None, float | None]:
    """Parse Contra's ``"USD:2000.00000000"`` into ``(currency, amount)``.

    Anything unrecognised yields ``(None, None)``; a number is never invented.
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
    if not typename:
        return "unknown"
    return _BUDGET_TYPE_MAP.get(str(typename).strip(), "unknown")


def source_id_of(raw_id: str) -> str:
    """Stable ``source_id`` for an authenticated feed record.

    Contra sends a base64 Relay id that decodes to ``["Job",<n>]``. The numeric id
    is stable and readable; if decoding ever fails we fall back to the raw value
    rather than dropping the record or fabricating an id.
    """
    value = str(raw_id or "").strip()
    if not value:
        return ""
    try:
        import base64

        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", "replace")
        match = _NUMERIC_ID_RE.search(decoded)
        if match:
            return f"job_{match.group(0)}"
    except Exception:  # noqa: BLE001 - decoding is best-effort
        pass
    return value


class ContraAuthenticatedSource:
    """Reads the authenticated Contra /jobs feed through the user's own session.

    ``evaluate`` is an injected callable ``(js_expression) -> str`` bound to an
    already-open, already-logged-in Contra tab (the project's ``make_cdp_evaluate``
    provides exactly that shape). Injecting it keeps this class free of browser
    plumbing and testable offline.
    """

    key = SOURCE_KEY
    source_mode = SOURCE_MODE
    source_scope = SOURCE_SCOPE

    def __init__(
        self,
        evaluate: Any,
        *,
        preset: str = "ALL",
        sort: str = "NEWEST",
        page_size: int = 150,
    ) -> None:
        self._evaluate = evaluate
        self.preset = preset
        self.sort = sort
        self.page_size = int(page_size)
        self._last_feed_count: int | None = None
        self._last_errors: list[str] = []
        #: Phase 4C §3: diagnostic state of the most recent fetch.
        self._last_health = None

    # ------------------------------------------------------------- fetching
    @staticmethod
    def _feed_expression(preset: str, sort: str, first: int) -> str:
        """The JS that performs the site's own feed query (a *query*, not a mutation).

        Synchronous XHR on purpose: the project's CDP transport evaluates with
        ``awaitPromise: False`` and expects a string back, so an async/promise
        expression would arrive as an opaque object. Sync XHR returns the response
        text directly and keeps this compatible with the existing transport.
        """
        variables = {
            "after": None,
            "filter": {},
            "first": int(first),
            "preset": preset,
            "search": None,
            "sort": sort,
        }
        body = {
            "doc_id": JOB_FEED_DOC_ID,
            "operationName": JOB_FEED_OPERATION,
            "variables": variables,
        }
        return (
            "(() => {"
            "  try {"
            "    const xhr = new XMLHttpRequest();"
            "    xhr.open('POST', " + json.dumps(JOB_FEED_URL) + ", false);"
            "    xhr.setRequestHeader('Content-Type', 'application/json');"
            "    xhr.send(" + json.dumps(json.dumps(body)) + ");"
            "    return xhr.responseText;"
            "  } catch (err) {"
            "    return JSON.stringify({__contra_error: String(err && err.message ? err.message : err)});"
            "  }"
            "})()"
        )

    def fetch_payload(self) -> dict[str, Any]:
        """Return the feed JSON. Raises ``ContraAuthUnavailable`` when unreadable."""
        expression = self._feed_expression(self.preset, self.sort, self.page_size)
        try:
            raw = self._evaluate(expression)
        except Exception as exc:  # noqa: BLE001 - transport is external
            raise ContraAuthUnavailable(
                f"contra: could not evaluate the feed query ({type(exc).__name__}: {exc})"
            ) from exc

        if isinstance(raw, dict):
            return raw
        # Some transports hand back a non-string object when the page returned
        # something unexpected; surface it as an explicit failure, not silence.
        if not isinstance(raw, (str, bytes)):
            raise ContraAuthUnavailable(
                f"contra: feed query returned an unusable result ({type(raw).__name__})"
            )
        text = str(raw or "").strip()
        if not text:
            raise ContraAuthUnavailable("contra: feed query returned nothing")
        # The transport may hand back a JSON string containing a promise result.
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ContraAuthUnavailable(
                f"contra: feed query did not return JSON ({text[:120]!r})"
            ) from exc
        # A page-side exception is reported as JSON so the cause survives the trip.
        if isinstance(parsed, dict) and parsed.get("__contra_error"):
            raise ContraAuthUnavailable(
                f"contra: feed query failed in page: {parsed['__contra_error']}"
            )
        return parsed

    # -------------------------------------------------------------- parsing
    def records_from_payload(self, payload: dict[str, Any], *, fetched_at: str | None = None) -> list[dict[str, Any]]:
        """Pure parse: authenticated feed JSON -> raw gig payloads.

        Kept free of any browser or network access so tests can pin it exactly.
        """
        when = fetched_at or _utcnow()
        errors = payload.get("errors") or []
        if errors:
            messages = "; ".join(str(e.get("message", e))[:120] for e in errors[:3])
            self._last_errors = [f"graphql: {messages}"]
            raise ContraAuthUnavailable(f"contra: feed query failed: {messages}")

        feed = ((payload.get("data") or {}).get("jobFeed")) or {}
        self._last_feed_count = feed.get("count")

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for edge in feed.get("edges") or []:
            node = (edge or {}).get("node") or {}
            job = node.get("job") or {}
            raw = self._record_from(job, node, when)
            if raw is None or raw["source_id"] in seen:
                continue
            seen.add(raw["source_id"])
            records.append(raw)
        return records

    def _record_from(self, job: dict[str, Any], node: dict[str, Any], when: str) -> dict[str, Any] | None:
        source_id = source_id_of(job.get("id"))
        title = str(job.get("title") or "").strip()
        if not source_id or not title:
            return None

        budget = job.get("budget") or {}
        currency_min, amount_min = parse_money(budget.get("feeMin"))
        currency_max, amount_max = parse_money(budget.get("feeMax"))
        duration = job.get("duration") or {}
        organization = job.get("organization") or {}
        roles = [e["node"]["name"] for e in ((job.get("roles") or {}).get("edges") or [])
                 if isinstance(e, dict) and isinstance(e.get("node"), dict)]
        tools = [e["node"]["name"] for e in ((job.get("tools") or {}).get("edges") or [])
                 if isinstance(e, dict) and isinstance(e.get("node"), dict)]

        return {
            "source": self.key,
            "source_id": source_id,
            # Contra publishes no per-job page that resolves (both /jobs/<slug> and
            # the numeric form 404 even authenticated), so there is no gig URL to
            # record. The feed URL is kept in source_url instead of inventing one.
            "url": None,
            "title": title,
            # The feed carries no description body; left empty, never generated.
            "description": "",
            "category": job.get("contraLabsCategory"),
            "skills": tools,
            "budget": {
                "min": amount_min,
                "max": amount_max,
                "type": budget_type_of(budget.get("__typename")),
            },
            "currency": currency_min or currency_max,
            "published_at": job.get("createdAt"),
            "client_name": (str(organization.get("name")).strip() or None)
            if organization.get("name") else None,
            "client_country": None,
            # Absent from the feed; never invented.
            "client_rating": None,
            "client_jobs_posted": None,
            "client_hires": None,
            "client_total_spent": None,
            "competition": None,
            "proposal_cost": None,
            # --- provenance ------------------------------------------------------
            "source_mode": SOURCE_MODE,
            "source_scope": SOURCE_SCOPE,
            "source_url": f"{CONTRA_HOST}/jobs",
            "fetched_at": when,
            # The feed returns only currently-open postings, so the record is open
            # by construction. Recorded explicitly instead of left blank.
            "source_status": "OPEN",
            "platform_payment_options": list(CONTRA_PLATFORM_PAYMENT_OPTIONS),
            "source_facts": {
                "contra_job_id": job.get("id"),
                "contra_feed_entry_id": node.get("id"),
                "contra_slug": job.get("slug"),
                "contra_created_at": job.get("createdAt"),
                "contra_budget_typename": budget.get("__typename"),
                "contra_duration": duration or None,
                "contra_organization": organization or None,
                "contra_roles": roles,
                "contra_tools": tools,
                "contra_timezone_primary_utc_offset": job.get("timezonePrimaryUtcOffset"),
                "contra_visitor_did_apply": node.get("visitorDidApply"),
                "contra_visitor_was_matched": node.get("visitorWasMatched"),
                "contra_feed_total_count": self._last_feed_count,
                "contra_feed_scope": "authenticated /jobs feed",
            },
            "derived_fields": [
                "source_id",
                "budget_min",
                "budget_max",
                "budget_type",
                "published_at",
                "source_mode",
                "source_scope",
                "fetched_at",
                "source_status",
            ],
            "extra": {
                "contra_slug": job.get("slug"),
                "contra_duration": duration or None,
                "contra_roles": roles,
                "contra_tools": tools,
            },
        }

    # --------------------------------------------------------------- public
    def fetch(self, limit: int | None = None) -> Iterator[dict]:
        """Yield authenticated open gigs (with run diagnostics)."""
        from .base import SourceHealth, summarize_health

        self._last_errors = []
        produced = 0
        try:
            for record in self._fetch_records(limit):
                produced += 1
                yield record
        except Exception as exc:  # noqa: BLE001 - keep the signal, then propagate
            self._last_errors.append(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._last_health = summarize_health(produced, self._last_errors)

    def health(self):
        """Диагноз последнего прогона (UNKNOWN, если прогона не было)."""
        from .base import SourceHealth

        return self._last_health or SourceHealth()

    def _fetch_records(self, limit: int | None = None) -> Iterator[dict]:
        """Yield authenticated open gigs. Raises ``ContraAuthUnavailable`` on failure."""
        self._last_errors = []
        payload = self.fetch_payload()
        records = self.records_from_payload(payload)
        if limit is not None:
            records = records[: int(limit)]
        if not records:
            raise ContraAuthUnavailable(
                "contra: authenticated session returned no job records "
                "(session expired, or the plan returns nothing for this preset)"
            )
        yield from records

    @property
    def last_feed_count(self) -> int | None:
        """Total open postings the site reports (may exceed what the plan returns)."""
        return self._last_feed_count

    @property
    def last_errors(self) -> list[str]:
        return list(self._last_errors)


__all__ = [
    "CONTRA_PLATFORM_PAYMENT_OPTIONS",
    "ContraAuthenticatedSource",
    "ContraAuthUnavailable",
    "JOB_FEED_DOC_ID",
    "JOB_FEED_OPERATION",
    "SOURCE_SCOPE",
    "budget_type_of",
    "parse_money",
    "source_id_of",
]
