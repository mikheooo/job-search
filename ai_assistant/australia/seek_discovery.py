"""Australian Job Discovery Engine for Visa-Sponsored Technical & Trade Roles.

Searches SEEK.com.au for active listings offering Employer Visa Sponsorship
(Skills in Demand Subclass 482 / DAMA Subclass 494 / TSS 482) across
telecommunications, field engineering, electronics, and technical trades.
"""
from __future__ import annotations

import datetime
import json
import logging
import pathlib
import re
import time
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("seek_discovery")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
CACHE_FILE = REPO_ROOT / "data" / "australia" / "seek_cache.json"

SEEK_BASE_URL = "https://www.seek.com.au"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

DEFAULT_SEARCH_QUERIES = [
    '"visa sponsorship" technician',
    'telecommunications sponsorship',
    '"482 visa" technician',
    '"visa sponsorship" field engineer',
    '"visa sponsorship" telecommunications',
    '"visa sponsorship" diesel fitter',
    '"sponsorship" electronics technician',
    '"employer sponsorship" field technician',
]

SPONSORSHIP_PATTERNS = [
    re.compile(r"visa\s+sponsorship", re.IGNORECASE),
    re.compile(r"482\s+visa", re.IGNORECASE),
    re.compile(r"subclass\s+482", re.IGNORECASE),
    re.compile(r"tss\s+482", re.IGNORECASE),
    re.compile(r"employer\s+sponsorship", re.IGNORECASE),
    re.compile(r"dama", re.IGNORECASE),
    re.compile(r"sponsor(?:ship)?\s+(?:available|provided|offered|considered)", re.IGNORECASE),
    re.compile(r"relocation\s+(?:assistance|package|provided)", re.IGNORECASE),
]

EXCLUDE_PATTERNS = [
    re.compile(r"\bintern(?:ship)?\b", re.IGNORECASE),
    re.compile(r"\bquant(?:itative)?\b", re.IGNORECASE),
    re.compile(r"\btrader\b", re.IGNORECASE),
    re.compile(r"\bphd\b", re.IGNORECASE),
]


def fetch_seek_search_page(query: str, page: int = 1, retries: int = 2) -> dict[str, Any] | None:
    """Fetch and parse SEEK search results JSON via embedded window.SEEK_REDUX_DATA."""
    encoded_query = urllib.parse.quote(query)
    url = f"{SEEK_BASE_URL}/jobs?keywords={encoded_query}&sortmode=Relevance"
    if page > 1:
        url += f"&page={page}"

    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    html = ""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                wait_sec = 2.0 * (attempt + 1)
                logger.info(f"SEEK rate-limit (429) on '{query}'. Backing off {wait_sec}s...")
                time.sleep(wait_sec)
                continue
            logger.warning(f"Error fetching SEEK page for query '{query}': {exc}")
            return None
        except Exception as exc:
            logger.warning(f"Error fetching SEEK page for query '{query}': {exc}")
            return None

    if not html:
        return None

    match = re.search(r"window\.SEEK_REDUX_DATA\s*=\s*(\{.*?\});\s*window\.SEEK_", html, re.DOTALL)
    if not match:
        logger.debug(f"SEEK_REDUX_DATA not found in HTML for query '{query}'")
        return None

    try:
        return json.loads(match.group(1))
    except Exception as exc:
        logger.warning(f"Failed to parse SEEK_REDUX_DATA JSON: {exc}")
        return None


def extract_jobs_from_seek_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract and normalize job cards from SEEK redux structure."""
    results = data.get("results", {})
    raw_jobs = results.get("results", {}).get("jobs") or []
    extracted = []

    for j in raw_jobs:
        jid = str(j.get("id") or "")
        if not jid:
            continue

        title = str(j.get("title") or "").strip()
        if any(p.search(title) for p in EXCLUDE_PATTERNS):
            continue
        company = (j.get("advertiser") or {}).get("description") or "Direct Employer"
        teaser = str(j.get("teaser") or "").strip()
        locations_raw = j.get("locations") or []
        if isinstance(locations_raw, list) and locations_raw:
            location = str(locations_raw[0].get("label") or "Australia")
        elif isinstance(locations_raw, dict):
            location = str(locations_raw.get("label") or locations_raw.get("city") or "Australia")
        else:
            location = str(j.get("location") or "Australia")

        salary_raw = j.get("salaryLabel") or j.get("salary")
        if isinstance(salary_raw, dict):
            salary = str(salary_raw.get("label") or "Market Rate (Min AUD $79,423)")
        else:
            salary = str(salary_raw) if salary_raw else "Market Rate (Min AUD $79,423)"

        bullet_points = [str(b).strip() for b in (j.get("bulletPoints") or []) if b]

        url = f"{SEEK_BASE_URL}/job/{jid}"

        # Combine text to evaluate sponsorship keywords
        combined = f"{title} {teaser} {' '.join(bullet_points)}".lower()
        matched_patterns = [p.pattern for p in SPONSORSHIP_PATTERNS if p.search(combined)]

        extracted.append({
            "id": jid,
            "title": title,
            "company": company,
            "location": location,
            "salary": salary,
            "teaser": teaser,
            "bullet_points": bullet_points,
            "url": url,
            "source": "seek_au",
            "matched_keywords": matched_patterns,
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })

    return extracted


def load_cached_jobs() -> dict[str, dict[str, Any]]:
    """Load previously discovered jobs from disk cache."""
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to read seek cache: %s", exc)
        return {}


def save_cached_jobs(jobs: dict[str, dict[str, Any]]) -> None:
    """Save discovered jobs to disk cache."""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(jobs, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to write seek cache: %s", exc)


def discover_australian_sponsored_jobs(
    queries: list[str] | None = None,
    limit: int = 20,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Scan SEEK.com.au across key queries and return viable sponsored vacancies."""
    cached = load_cached_jobs()
    all_jobs: dict[str, dict[str, Any]] = dict(cached)

    if not force_refresh and len(all_jobs) >= limit:
        logger.info("Using cached SEEK results (%d jobs)", len(all_jobs))
    else:
        search_queries = queries or DEFAULT_SEARCH_QUERIES
        for query in search_queries:
            logger.info("Scanning SEEK.com.au for: %s", query)
            page_data = fetch_seek_search_page(query=query, page=1)
            if not page_data:
                continue

            jobs = extract_jobs_from_seek_data(page_data)
            logger.info("Found %d jobs for query '%s'", len(jobs), query)

            for job in jobs:
                jid = job["id"]
                if jid not in all_jobs:
                    all_jobs[jid] = job

            time.sleep(1.5)

        save_cached_jobs(all_jobs)

    # Filter out excluded jobs (internships, quant traders, etc.)
    valid_jobs = [
        j for j in all_jobs.values()
        if not any(p.search(j.get("title", "")) for p in EXCLUDE_PATTERNS)
    ]

    # Sort by relevance (those with explicit sponsorship patterns in title/teaser first)
    sorted_jobs = sorted(
        valid_jobs,
        key=lambda j: len(j.get("matched_keywords", [])),
        reverse=True,
    )
    return sorted_jobs[:limit]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    found = discover_australian_sponsored_jobs(limit=10)
    print(f"\nDiscovered {len(found)} sponsored Australian jobs:")
    for idx, item in enumerate(found, 1):
        print(f"#{idx} [{item['id']}] {item['title']} | {item['company']}")
        print(f"   Location: {item['location']} | Salary: {item['salary']}")
        print(f"   URL: {item['url']}")
        print(f"   Keywords: {item.get('matched_keywords')}")
        print("-" * 60)
