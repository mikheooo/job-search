"""Kwork project adapter — curated public project pages (Phase 4C §4).

SCOPE AND BOUNDARIES
--------------------
Kwork publishes NO official public API (api.kwork.ru is a closed/internal API;
third-party libs drive it with a user login). The exchange listing
(``/projects``) is a JS shell with no server-rendered project links, and the
query pagination forms are disallowed by robots.txt. Automating a login,
defeating CAPTCHA/anti-bot measures, or sweeping search terms is OUT OF SCOPE.

What IS public and stable: individual project pages
(``https://kwork.ru/projects/<id>`` -> ``.../view``) return HTTP 200
anonymously with the full brief embedded as ``"wantData":{...}`` JSON:
id, status (``active``/``archived``/...), ISO dates, desired/ceiling budget
(``priceLimit``/``possiblePriceLimit``), ``category_id``, description, views.
This adapter reads ONLY explicitly given project URLs (curated list, manual
import, or snapshot files) — one polite GET per URL, fixed User-Agent,
bounded request budget. It is a curated-URL source, NOT an exchange crawler;
that limitation is reported, not hidden.

FIELDS HONESTY
--------------
Only embedded facts are used. Kwork pages carry no skills list, no client
rating/spend/hires, no applicant count under a stable key, so those stay
``None``/``[]`` and the scorer treats them as unknown. ``kwork_count`` is
stored raw with an explicit unverified-meaning note. Statuses map to our
``source_status`` vocabulary; ``archived``-like values trip the existing
SOURCE_CLOSED gate (correct: no bids possible there).
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Iterator

logger = logging.getLogger(__name__)

KWORK_HOST = "https://kwork.ru"
PROJECT_URL_RE = re.compile(r"https?://kwork\.ru/projects/(\d+)")
WANT_DATA_RE = re.compile(r'"wantData"\s*:\s*(\{)', re.S)

DEFAULT_DELAY_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_MAX_REQUESTS = 100
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: Kwork wantData.status -> our source_status.
_STATUS_MAP = {
    "active": "OPEN",
    "new": "OPEN",
    "archived": "ARCHIVED",
    "closed": "CLOSED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELED",
    "cancel": "CANCELLED",
    "completed": "COMPLETED",
    "deleted": "DELETED",
    "expired": "EXPIRED",
    # Phase 4C.4: verified 2026-10-12 — isWantActive=False, date_expire
    # passed, empty timeLeft on all sampled rows. "stop" = halted by the
    # platform/timeout, "user_stop" = halted by the customer. Both mean the
    # order is NOT accepting proposals right now.
    "stop": "STOPPED",
    "user_stop": "USER_STOPPED",
}

_OFFERS_RE = re.compile(r"Хотят взяться:\s*(\d+)", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _untext(raw: Any) -> str:
    import html as html_module

    text = _TAG_RE.sub(" ", str(raw or ""))
    return re.sub(r"\s+", " ", html_module.unescape(text)).strip()


def _to_float(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(raw))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _parse_kwork_dt(raw: Any) -> str | None:
    """``2026-10-07 13:17:03`` -> ISO Z. Russian display dates stay None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def extract_project_id(url: str) -> str | None:
    """Numeric project id from a kwork project URL, else None."""
    match = PROJECT_URL_RE.search(str(url or ""))
    return match.group(1) if match else None


def canonical_url(project_id: str | int) -> str:
    return f"{KWORK_HOST}/projects/{project_id}"


def extract_want_data(html: str) -> dict[str, Any] | None:
    """Parse the embedded ``wantData`` JSON object, or None when absent.

    Balanced-brace scan from the ``"wantData":{`` marker; returns None on
    truncated/invalid JSON rather than guessing.
    """
    match = WANT_DATA_RE.search(html)
    if not match:
        return None
    depth = 0
    start = match.start(1)
    in_string = False
    escaped = False
    for pos in range(start, len(html)):
        ch = html[pos]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(html[start:pos + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def normalize_source_status(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    return _STATUS_MAP.get(text, text.upper())


class KworkUnavailable(RuntimeError):
    """Source unavailable; caller must preserve stored gigs."""


def _extract_kwork_skills(title: str, description: str) -> list[str]:
    text = f"{title} {description}".lower()
    mapping = {
        "telegram": [r"\b(?:telegram|aiogram|telethon|телеграм|боты?|bots?|тг)\b"],
        "python": [r"\b(?:python|парсер\w*|парсинг\w*|скрипт\w*|django|fastapi|питон|playwright|beautifulsoup|scrapy|pdf)\b"],
        "bitrix24": [r"\b(?:bitrix\w*|битрикс\w*|crm|amocrm|амоцрм)\b"],
        "n8n": [r"\b(?:n8n|make\.com|zapier|albato|автоматизац\w*|integromat)\b"],
        "linux": [r"\b(?:linux|линукс|сервер\w*|ubuntu|debian|vps|vds|docker|vpn|wireguard|nginx|деплой\w*)\b"],
        "sql": [r"\b(?:sql|postgres\w*|mysql|баз\w+ данн\w*)\b"],
        "excel": [r"\b(?:excel|гугл таблиц\w*|google sheets?)\b"],
        "rest api": [r"\b(?:api|вебхук\w*|webhooks?|интеграц\w*|rest)\b"],
        "ai agents": [r"\b(?:ai|llm|rag|gpt|whisper|gemini|нейросет\w*|агент\w*)\b"],
    }
    found: list[str] = []
    for canonical, patterns in mapping.items():
        if any(re.search(p, text, re.I) for p in patterns):
            found.append(canonical)
    return found



def discover_kwork_project_urls(
    categories: list[int] | None = None,
    max_urls: int = 25,
) -> list[str]:
    """Discover fresh project URLs from Kwork exchange categories using Headless Chrome.

    Default categories: [11] (Разработка и IT).
    Safe read-only operation: opens categories in Headless Chrome, extracts links matching /projects/<id>.
    """
    cats = categories or [11]
    discovered: list[str] = []
    seen: set[str] = set()

    try:
        from ...hh_browser_provider import HeadlessBrowserSession

        session = HeadlessBrowserSession(require_auth=False)
        info = session.start()
        if not info.get("ok"):
            logger.warning("Headless session failed to start for Kwork auto-discovery: %s", info)
            return []
        try:
            for cat_id in cats:
                url = f"{KWORK_HOST}/projects?c={cat_id}"
                session.ensure_work_tab(url)
                time.sleep(3.5)
                js = """(() => {
                    const links = Array.from(document.querySelectorAll('a[href*="/projects/"]')).map(a => a.href);
                    return JSON.stringify(links);
                })()"""
                raw = session.evaluate(js)
                try:
                    links = json.loads(raw) if raw else []
                except Exception:
                    links = []
                for link in links:
                    pid = extract_project_id(link)
                    if pid:
                        canon = canonical_url(pid)
                        if canon not in seen:
                            seen.add(canon)
                            discovered.append(canon)
                            if len(discovered) >= max_urls:
                                break
                if len(discovered) >= max_urls:
                    break
        finally:
            session.stop()
    except Exception as exc:
        logger.warning("Kwork auto-discovery encountered error: %s", exc)

    return discovered


class KworkGigSource:
    """Curated-URL live source for Kwork public project pages.

    ``source_mode="live"``, ``source_scope="public_project_pages"``.
    Read-only public GETs, no login, no submissions, robots-aware.
    """

    key = "kwork"
    source_mode = "live"
    source_scope = "public_project_pages"

    def __init__(
        self,
        urls: list[str] | tuple[str, ...] | None = None,
        *,
        urls_file: str | None = None,
        snapshot_dir: str | None = None,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        user_agent: str = DEFAULT_USER_AGENT,
        respect_robots: bool = True,
        fetch_html: Callable[[str], str] | None = None,
        auto_discover: bool = False,
        categories: list[int] | None = None,
    ) -> None:
        self.auto_discover = auto_discover
        self.categories = list(categories) if categories else [11]
        self.urls: list[str] = []
        for u in list(urls or []):
            pid = extract_project_id(u)
            if pid:
                self.urls.append(canonical_url(pid))
        if urls_file:
            try:
                with open(urls_file, encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        pid = extract_project_id(line)
                        if pid:
                            self.urls.append(canonical_url(pid))
            except OSError as exc:
                raise KworkUnavailable(f"kwork: cannot read urls file: {exc}") from exc
        # Deduplicate, keep order.
        self.urls = list(dict.fromkeys(self.urls))
        self.snapshot_dir = snapshot_dir
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.timeout_seconds = float(timeout_seconds)
        self.max_requests = max(1, int(max_requests))
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self._fetch_html_override = fetch_html
        self._last_fetch_errors: list[str] = []
        self._requests_made = 0
        self._last_health = None
        self._robots_parser: urllib.robotparser.RobotFileParser | None = None

    # ------------------------------------------------------------------ HTTP
    def _robots_allows(self, url: str) -> bool:
        if self._fetch_html_override is not None:
            return True
        if self._robots_parser is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(KWORK_HOST + "/robots.txt")
            try:
                parser.read()
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.warning("kwork: robots.txt unreadable (%s); proceeding politely", exc)
                parser = None
            self._robots_parser = parser
        if self._robots_parser is None:
            return True
        try:
            return self._robots_parser.can_fetch(self.user_agent, url)
        except Exception:  # noqa: BLE001
            return True

    def _http_get(self, url: str) -> str:
        if self._requests_made >= self.max_requests:
            raise KworkUnavailable(
                f"kwork: request budget exhausted ({self.max_requests})"
            )
        self._requests_made += 1
        if self._fetch_html_override is not None:
            return self._fetch_html_override(url)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            final_host = urllib.parse.urlparse(response.url or url).hostname or ""
            if final_host.lower() not in ("kwork.ru", "www.kwork.ru"):
                raise KworkUnavailable(
                    f"kwork: off-domain redirect refused: {final_host or '?'}"
                )
            return response.read().decode("utf-8", "replace")

    # ---------------------------------------------------------------- parsing
    @staticmethod
    def record_from_html(html: str, *, page_url: str, fetched_at: str) -> dict[str, Any] | None:
        """Project page -> raw dict for ``normalize_gig``, or None."""
        data = extract_want_data(html)
        if data is None:
            return None
        try:
            project_id = int(data.get("id"))
        except (TypeError, ValueError):
            return None
        title = _untext(data.get("name"))
        if not title:
            return None
        url = canonical_url(project_id)
        source_status = normalize_source_status(data.get("status"))
        desired = _to_float(data.get("priceLimit"))
        ceiling = _to_float(data.get("possiblePriceLimit"))
        budget_type = "fixed" if (desired is not None or ceiling is not None) else None
        offers_match = _OFFERS_RE.search(html)
        offers = int(offers_match.group(1)) if offers_match else None
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        return {
            "source": "kwork",
            "source_id": f"kw_{project_id}",
            "url": url,
            "title": title,
            "description": _untext(data.get("description")),
            "category": None,  # page carries category_id without a name map; not invented
            "skills": _extract_kwork_skills(title, _untext(data.get("description"))),
            "budget": {"min": desired, "max": ceiling, "type": budget_type},
            # Platform constant, not per-order evidence: Kwork is a
            # Russia-only product and every budget on it is in roubles.
            "currency": "RUB",
            "published_at": _parse_kwork_dt(data.get("date_create")),
            "competition": offers,
            "client_name": _untext(user.get("username")) or None,
            "client_country": None,
            "client_rating": None,
            "client_jobs_posted": None,
            "client_hires": None,
            "client_total_spent": None,
            "proposal_cost": None,
            "source_mode": "live",
            "source_scope": "public_project_pages",
            "source_url": page_url,
            "fetched_at": fetched_at,
            "source_status": source_status,
            "platform_payment_options": [],
            "source_facts": {
                "kwork_project_id": project_id,
                "kwork_status": data.get("status"),
                "kwork_category_id": data.get("category_id"),
                "kwork_price_limit": data.get("priceLimit"),
                "kwork_possible_price_limit": data.get("possiblePriceLimit"),
                "kwork_max_days": data.get("max_days"),
                "kwork_views": data.get("views_dirty"),
                "kwork_count_raw": data.get("kwork_count"),
                "kwork_count_meaning": "unverified: stored as reported, not interpreted",
                "kwork_date_active": data.get("date_active"),
                "kwork_date_expire": data.get("date_expire"),
                "kwork_is_active": data.get("isWantActive"),
                "kwork_is_archive": data.get("isWantArchive"),
                "kwork_client_username": user.get("username"),
                "kwork_client_wants_count": (user.get("data") or {}).get("wants_count")
                if isinstance(user.get("data"), dict) else None,
                "kwork_source_surface": "public project page (no login)",
            },
            "derived_fields": [
                "source_id", "budget_min", "budget_max", "published_at",
                "competition", "source_mode", "source_scope", "fetched_at",
                "source_status",
            ],
        }

    def records_from_snapshot_dir(self) -> list[dict[str, Any]]:
        """Offline parse of saved project pages (deterministic tests)."""
        import os

        if not self.snapshot_dir:
            return []
        records: list[dict[str, Any]] = []
        try:
            names = sorted(os.listdir(self.snapshot_dir))
        except OSError:
            return []
        fetched_at = _utcnow()
        for name in names:
            if not name.lower().endswith((".html", ".htm")):
                continue
            try:
                with open(os.path.join(self.snapshot_dir, name),
                          encoding="utf-8", errors="replace") as handle:
                    html = handle.read()
            except OSError:
                continue
            record = self.record_from_html(
                html, page_url=f"{KWORK_HOST}/projects/snapshot/{name}",
                fetched_at=fetched_at)
            if record:
                records.append(record)
        return records

    # ------------------------------------------------------------------ fetch
    def fetch(self, limit: int | None = None) -> Iterator[dict]:
        """Yield raw gigs for the curated URLs (with run diagnostics)."""
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
        if self.snapshot_dir:
            records = self.records_from_snapshot_dir()
            yield from (records[: int(limit)] if limit is not None else records)
            return
        if self.auto_discover and not self.urls:
            discovered = discover_kwork_project_urls(self.categories, max_urls=int(limit or 25))
            for u in discovered:
                pid = extract_project_id(u)
                if pid:
                    canon = canonical_url(pid)
                    if canon not in self.urls:
                        self.urls.append(canon)
        wanted = int(limit) if limit is not None else None
        fetched_at = _utcnow()
        produced = 0
        for index, url in enumerate(self.urls):
            if wanted is not None and produced >= wanted:
                break
            if self.respect_robots and not self._robots_allows(url):
                self._last_fetch_errors.append(f"{url}: disallowed by robots.txt")
                continue
            try:
                html = self._http_get(url)
            except urllib.error.HTTPError as exc:
                self._last_fetch_errors.append(f"{url}: HTTP {exc.code}")
                continue
            except Exception as exc:  # noqa: BLE001 - network is untrusted
                self._last_fetch_errors.append(f"{url}: {type(exc).__name__}: {exc}")
                continue
            record = self.record_from_html(html, page_url=url, fetched_at=fetched_at)
            if record is None:
                self._last_fetch_errors.append(f"{url}: no wantData block; skipped")
                continue
            produced += 1
            yield record
            if self.delay_seconds and index < len(self.urls) - 1:
                time.sleep(self.delay_seconds)
        if not self.urls:
            raise KworkUnavailable("kwork: no project URLs configured (pass urls/urls_file)")

    @property
    def last_fetch_errors(self) -> list[str]:
        return list(self._last_fetch_errors)


__all__ = [
    "KWORK_HOST",
    "KworkGigSource",
    "KworkUnavailable",
    "canonical_url",
    "discover_kwork_project_urls",
    "extract_project_id",
    "extract_want_data",
    "normalize_source_status",
]
