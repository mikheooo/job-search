"""Telegram Public Channels Vacancy & Gig Adapter.

Scrapes public web views of leading Russian IT job and freelance channels:
Job Channels:
- @remoteit
- @normrabota
- @sysadmin_jobs
- @forpython
- @geekjobs
- @backend_jobs
- @devops_jobs_feed
- @proglib_jobs

Freelance / Gig Channels:
- @freelance_orders
- @zakazy_it

Extracts task/vacancy details, contacts (@recruiter, email, link), and normalizes to Vacancy schema.
Requires no Telegram API credentials and has zero risk of account bans.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..normalizer import normalize_salary_text
from ..schema import Vacancy

logger = logging.getLogger("telegram_channels_adapter")

JOB_CHANNELS = [
    "remoteit",
    "normrabota",
    "sysadmin_jobs",
    "forpython",
    "geekjobs",
    "backend_jobs",
    "devops_jobs_feed",
    "proglib_jobs",
]

GIG_CHANNELS = [
    "freelance_orders",
    "zakazy_it",
]

DEFAULT_CHANNELS = JOB_CHANNELS + GIG_CHANNELS

TARGET_KEYWORDS = [
    "python", "linux", "админ", "sysadmin", "devops", "docker", "k8s", "kubernetes",
    "ci/cd", "ansible", "terraform", "bash", "сети", "network", "cisco", "vpn",
    "wireguard", "сервер", "vps", "crm", "битрикс", "bitrix", "amocrm", "парс",
    "scraping", "бот", "telegram", "fastapi", "django", "sql", "postgres", "support",
    "хелпдеск", "helpdesk", "l2", "l3", "lead", "инженер", "engineer", "backend", "скрипт"
]

# A single tag-shaped run. Deliberately applied *after* entity decoding: see
# ``clean_post_html`` for why the order is the security property, not an
# implementation detail.
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_POST_BREAK_RE = re.compile(r"<br\b[^>]*>|</(?:p|div)\s*>", re.IGNORECASE)
_CANDIDATE_POST_RE = re.compile(
    r"(?<!\w)#(?:резюме|ищу|ищуработу|resume|cv|opentowork)(?!\w)"
    r"|\bищу\s+(?:(?:удал[её]нную|новую|постоянную)\s+)?работу\b",
    re.IGNORECASE,
)
_POST_METADATA_RE = re.compile(
    r"^(?:публикатор|обсуждение|контакты?(?:\s+для\s+отклика)?|для\s+отклика|зарплата|локация)\s*:",
    re.IGNORECASE,
)


def is_candidate_post(text: str) -> bool:
    """Recognize explicit job-seeker markers, not employer requests for a resume."""
    return bool(_CANDIDATE_POST_RE.search(text or ""))


def extract_post_title(text: str) -> str | None:
    """Use an actual post line; never invent a role from publisher metadata."""
    if is_candidate_post(text):
        return None
    for line in (text or "").splitlines():
        title = re.sub(r"#\S+", "", line).strip(" |:—–\t")
        if not title or _POST_METADATA_RE.match(title):
            continue
        if title.lower() in {"вакансия", "vacancy", "job"}:
            continue
        title = re.sub(
            r"^(?:вакансия|позиция|должность|роль|vacancy|position)\s*[:—–-]\s*",
            "", title, flags=re.IGNORECASE,
        ).strip()
        if title.startswith(("http://", "https://", "@")):
            continue
        if title:
            return title[:87] + "..." if len(title) > 90 else title
    return None


def prepare_telegram_digest_title(vacancy: Vacancy) -> bool:
    """Validate old Telegram rows in memory without rewriting persisted data."""
    if is_candidate_post(f"{vacancy.title}\n{vacancy.description}"):
        return False
    title = vacancy.title or ""
    if not title.strip() or _POST_METADATA_RE.match(title.strip()):
        title = extract_post_title(vacancy.description or "")
        if not title:
            return False
        vacancy.title = title
    return True


def clean_post_html(raw_html: str) -> str:
    """Turn a Telegram post fragment into inert plain text.

    Entities are decoded **before** tags are stripped.  The old order -- strip
    first, unescape second -- let a post spell markup as text: ``&lt;img
    src=x onerror=...&gt;`` survived the strip (it looked like plain text) and
    the following ``html.unescape`` handed the caller a live ``<img onerror>``
    string, which then reached ``vacancies.title`` and the dashboard's
    ``innerHTML``.

    Decoding first makes the tag visible to the strip.  Exactly one decoding
    pass is performed, so double-encoded input (``&amp;lt;``) stays text
    instead of being decoded into markup, and ordinary text such as ``&amp;``
    still becomes ``&``.
    """
    if not raw_html:
        return ""
    decoded = html_lib.unescape(raw_html)
    stripped = _HTML_TAG_RE.sub(" ", _POST_BREAK_RE.sub("\n", decoded))
    return "\n".join(line.strip() for line in stripped.splitlines() if line.strip())


class TelegramChannelsAdapter:
    source = "telegram"

    def __init__(self, channels: list[str] | None = None, user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"):
        self.channels = channels or DEFAULT_CHANNELS
        self.headers = {"User-Agent": user_agent}

    def fetch_channel(self, channel: str) -> list[Vacancy]:
        """Fetch latest posts from a public channel web preview."""
        url = f"https://t.me/s/{channel}"
        req = urllib.request.Request(url, headers=self.headers)
        vacancies: list[Vacancy] = []

        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            logger.warning(f"Failed to fetch Telegram channel @{channel}: {e}")
            return []

        is_gig_source = channel in GIG_CHANNELS

        # Find post elements with post IDs
        post_blocks = re.findall(
            r'data-post="([^"]+)"[^>]*>.*?<div[^>]*class="[^"]*js-message_text[^"]*"[^>]*>(.*?)</div>',
            content,
            re.DOTALL
        )

        for post_id, raw_html in post_blocks:
            clean_text = clean_post_html(raw_html)
            if not clean_text or len(clean_text) < 30:
                continue

            lines = clean_text.split("\n")
            clean_low = clean_text.lower()

            title = extract_post_title(clean_text)
            if title is None:
                continue

            # Filter for remote or relevant IT skills
            is_remote = any(r in clean_low for r in ["удален", "remote", "удалён", "любая локация", "дистанцион", "фриланс", "проект"])
            matches_skills = any(k in clean_low for k in TARGET_KEYWORDS)

            if not is_gig_source and not is_remote:
                continue
            if not matches_skills:
                continue

            # Extract company / client
            company = "Заказчик" if is_gig_source else "Компания"
            for line in lines[:5]:
                comp_match = re.search(r"(?:компания|работодатель|проект|в\s+команду|в)\s*[:—–\-]?\s*([A-Za-zА-Яа-я0-9\s\-]+)", line, re.IGNORECASE)
                if comp_match:
                    found_comp = comp_match.group(1).strip()
                    if 2 < len(found_comp) < 40:
                        company = found_comp
                        break

            # Extract contact: @username, email, or link
            contacts = []
            usernames = re.findall(r"@([a-zA-Z0-9_]{4,})", clean_text)
            for u in usernames:
                if u.lower() not in ["channel", "tgme", channel.lower(), "gotoisland", "leadradar"]:
                    contacts.append(f"@{u}")
            emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", clean_text)
            contacts.extend(emails)

            contact_str = ", ".join(list(dict.fromkeys(contacts))[:3]) if contacts else f"t.me/{post_id}"

            # Salary / Budget normalization
            salary_info = normalize_salary_text(clean_text)

            post_url = f"https://t.me/{post_id}"
            desc = f"{clean_text}\n\nКонтакты для отклика: {contact_str}"

            vac = Vacancy(
                source=f"tg_{channel}",
                source_job_id=post_id,
                title=title,
                company=company,
                description=desc,
                salary_min=salary_info.get("min"),
                salary_max=salary_info.get("max"),
                salary_currency=salary_info.get("currency") or "RUB",
                employment_type="project" if is_gig_source else "full",
                location="Удаленно (РФ/СНГ)",
                job_url=post_url,
                published_at=datetime.now(timezone.utc),
                raw_data={
                    "channel": channel,
                    "contact": contact_str,
                    "is_gig": is_gig_source,
                    "target_matched": True,
                },
            )
            vacancies.append(vac)

        logger.info(f"Telegram @{channel}: found {len(vacancies)} target {'gigs' if is_gig_source else 'vacancies'}")
        return vacancies

    def fetch_all(self) -> list[Vacancy]:
        """Fetch all target postings across all monitored channels."""
        all_vacancies: list[Vacancy] = []
        for ch in self.channels:
            try:
                ch_vacs = self.fetch_channel(ch)
                all_vacancies.extend(ch_vacs)
            except Exception as e:
                logger.error(f"Error fetching channel @{ch}: {e}")
        return all_vacancies
