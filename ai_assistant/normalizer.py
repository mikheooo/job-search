from __future__ import annotations

import html
import re
from typing import Any, Dict

from .schema import Vacancy


def normalize_salary_text(raw: Any) -> Dict[str, Any]:
    text = str(raw or "").strip()
    cleaned = html.unescape(text)
    cleaned = cleaned.replace("—", "-").replace("–", "-")
    cleaned = re.sub(r"\s+", " ", cleaned)

    currency = None
    if re.search(r"[₽]|руб|\b(RUB|RUR)\b", cleaned, re.IGNORECASE):
        currency = "RUB"
    else:
        m = re.search(r"\b(USD|EUR|GBP|CAD|AUD|CHF|NZD|ZAR|INR|BRL|CZK|PLN|SGD|HKD)\b|([$€£])", cleaned, re.IGNORECASE)
        if m:
            sym_map = {"$": "USD", "€": "EUR", "£": "GBP"}
            currency = sym_map.get(m.group(0), m.group(0).upper())

    num_cleaned = re.sub(r"(\d+)\s+(\d{3})\b", r"\1\2", cleaned)
    num_cleaned = re.sub(r"(\d+)\s+(\d{3})\b", r"\1\2", num_cleaned)
    numbers = re.findall(r"(?<!\d)([\d,]+)(?:\.\d+)?(?!\d)", num_cleaned)
    salary_min = None
    salary_max = None
    if numbers:
        parsed = []
        for n in numbers:
            try:
                parsed.append(int(n.replace(",", "")))
            except ValueError:
                continue
        if parsed:
            if len(parsed) == 1:
                salary_min = parsed[0]
                salary_max = parsed[0]
            else:
                salary_min = min(parsed)
                salary_max = max(parsed)

    return {"salary_text": cleaned, "salary_currency": currency, "salary_min": salary_min, "salary_max": salary_max}


def normalize_description(raw: Any) -> str:
    text = str(raw or "")
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_employment_type(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    mapping = {
        "full-time": "Full Time",
        "full time": "Full Time",
        "part-time": "Part Time",
        "part time": "Part Time",
        "contract": "Contract",
        "freelance": "Freelance",
        "internship": "Internship",
    }
    return mapping.get(text.lower(), text)


def normalize_vacancy(item: Dict[str, Any]) -> Vacancy:
    return Vacancy(
        source=str(item.get("source", "")),
        source_job_id=str(item.get("source_job_id", "")),
        title=str(item.get("title", "")).strip(),
        company=str(item.get("company", "")).strip(),
        description=normalize_description(item.get("description")),
        job_url=str(item.get("job_url", "")).strip(),
        application_url=str(item.get("application_url") or item.get("job_url", "")).strip() or None,
        location=str(item.get("location") or "").strip() or None,
        country_restrictions=[str(x).strip() for x in (item.get("country_restrictions") or []) if str(x).strip()],
        timezone_restrictions=[str(x) for x in (item.get("timezone_restrictions") or [])],
        salary_min=item.get("salary_min"),
        salary_max=item.get("salary_max"),
        salary_currency=str(item.get("salary_currency") or "").strip() or None,
        employment_type=normalize_employment_type(item.get("employment_type")),
        published_at=item.get("published_at"),
        first_seen_at=item.get("first_seen_at"),
        last_seen_at=item.get("last_seen_at"),
        raw_data=item.get("raw_data") or {},
    )
