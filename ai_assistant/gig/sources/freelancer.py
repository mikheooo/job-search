"""Адаптер live-источника Freelancer.com (публичная выдача, без логина).

ЧТО ЧИТАЕТ
----------
``/jobs/`` и ``/jobs/<N>`` — публичный список проектов (50 карточек на страницу).
Карточка даёт URL проекта, заголовок, навыки, число ставок, «среднюю ставку» и
остаток дней, но НЕ даёт стабильного числового id и НЕ даёт бюджет заказчика.
Поэтому бюджет, валюта, тип, сроки, клиент и полное описание читаются со страницы
проекта (``/projects/<category>/<slug>``), где они лежат в hydration-состоянии
``<script id="webapp-state">`` -> ``NGRX_STATE.projectsSeo[0].documents[<slug>].rawDocument``.

ПОЧЕМУ НЕ HTML-СКРЕЙПИНГ КАРТОЧЕК ДЛЯ ЧИСЕЛ
--------------------------------------------
``$29`` в карточке — это **Average bid** (средняя ставка исполнителей), а не бюджет
заказчика. Выдавать её за бюджет значило бы выдумать данные, поэтому источник
бюджета — только ``rawDocument.budget``.

ГРАНИЦЫ
-------
robots.txt Freelancer запрещает ``/jobs/?*`` (query-форму списка). Поэтому
пагинация идёт ТОЛЬКО путём ``/jobs/<N>`` — эта форма разрешена. Логина, cookies,
браузерной автоматизации и обхода ограничений здесь нет: только публичные
GET-запросы с паузой между ними.

Оплата: ``verification.paymentVerified`` у клиента и способы выплат площадки —
это факты о площадке и клиенте, а НЕ подтверждение того, что наш аккаунт готов
получать деньги. Поэтому ``payment_readiness`` никогда не становится ``ready``
из этих данных (см. ``assessment.assess_payment_readiness``).
"""
from __future__ import annotations

import json
import gzip
import time
import logging
import re
import urllib.error
import urllib.request
import urllib.robotparser
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Iterator

logger = logging.getLogger(__name__)

FREELANCER_HOST = "https://www.freelancer.com"

#: Публичная выдача: 50 карточек на страницу, пагинация путём (query-форма закрыта robots).
DEFAULT_LISTING_PATH = "/jobs/"
DEFAULT_LISTING_PAGES = 2

#: P5: целевые направления. Публичные страницы ``/jobs/<skill>/`` ограничивают выдачу
#: одной областью, поэтому сбор идёт адресно, а не только по общей ленте. robots.txt
#: запрещает лишь query-форму (``/jobs/?*``), путевая разрешена.
DEFAULT_TARGET_SKILLS: tuple[str, ...] = (
    "python",
    "api",
    "automation",
    "rest-api",
    "web-scraping",
    "data-processing",
    "excel",
    "data-entry",
    "django",
    "flask",
    "fastapi",
    "postgresql",
    "sql",
    "telegram",
    "chatgpt",
    "openai",
    "llm",
    "artificial-intelligence",
    "n8n",
    "zapier",
)

#: Сколько страниц читать внутри каждого направления. Глубже — больше проектов,
#: но и больше запросов: см. ``max_requests``.
DEFAULT_SKILL_PAGES = 1
DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 25.0
#: Жёсткий потолок на число запросов за прогон: список + детальные страницы.
#: 2 страницы выдачи = 2 + до 100 страниц проектов, поэтому дефолт должен это
#: вмещать, иначе бюджет срабатывает раньше, чем заканчивается выдача.
DEFAULT_MAX_REQUESTS = 260

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: Платформенные способы выплат Freelancer. Это capability площадки, а не
#: подтверждение готовности нашего аккаунта (P3 §7).
FREELANCER_PLATFORM_PAYMENT_OPTIONS: tuple[str, ...] = (
    "platform_milestones",
    "platform_escrow",
    "bank_transfer",
    "paypal",
    "payoneer",
    "skrill",
)

#: Пороги конкуренции по числу ставок. Настраиваются (P3 §6).
DEFAULT_COMPETITION_THRESHOLDS: dict[str, int] = {"low_max": 10, "medium_max": 30}

#: Статусы Freelancer -> наш source_status.
_OPEN_STATUSES = {"active", "open"}
_CLOSED_STATUSES = {"closed", "completed", "cancelled", "canceled", "deleted", "expired", "frozen"}

_CARD_SPLIT_RE = re.compile(r'<div class="JobSearchCard-item-inner"')
_CARD_URL_RE = re.compile(r'href="(/projects/[^"?#]+)"')
_CARD_TITLE_RE = re.compile(r'data-heading-link="true"[^>]*>\s*([^<]+?)\s*</a>')
_CARD_DAYS_RE = re.compile(r'heading-days">([^<]+)<')
_CARD_SKILL_RE = re.compile(r'JobSearchCard-primary-tagsLink"[^>]*>([^<]+)</a>')
_CARD_BIDS_RE = re.compile(r'JobSearchCard-secondary-entry">\s*(\d+)\s+bids?\s*<')
_CARD_DESC_RE = re.compile(r'JobSearchCard-primary-description">\s*(.*?)\s*</p>', re.S)

_WEBAPP_STATE_RE = re.compile(r'<script id="webapp-state"[^>]*>(.*?)</script>', re.S)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _untext(raw: Any) -> str:
    """Снимает HTML-сущности и теги; пустая строка остаётся пустой."""
    import html as html_module

    text = str(raw or "")
    text = _HTML_TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


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


def _to_int(raw: Any) -> int | None:
    value = _to_float(raw)
    return None if value is None else int(value)


def _ms_to_iso(raw: Any) -> str | None:
    """Freelancer отдаёт время в миллисекундах epoch. Ноль — не дата."""
    value = _to_float(raw)
    if value is None or value <= 0:
        return None
    seconds = value / 1000.0 if value > 1e11 else value
    try:
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_source_status(raw: Any) -> str | None:
    """Статус проекта Freelancer -> наш source_status (``OPEN`` для активных)."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    if text in _OPEN_STATUSES:
        return "OPEN"
    return text.upper()


def competition_level(
    bids_received: int | None,
    thresholds: dict[str, int] | None = None,
) -> str:
    """``bids_received`` -> ``low`` / ``medium`` / ``high`` (пороги настраиваемы).

    Отсутствие данных даёт ``unknown``: неизвестность не приравнивается к низкой
    конкуренции, иначе заказ без ставок выглядел бы привлекательнее реального.
    """
    limits = dict(thresholds or DEFAULT_COMPETITION_THRESHOLDS)
    low_max = int(limits.get("low_max", 10))
    medium_max = int(limits.get("medium_max", 30))
    if bids_received is None:
        return "unknown"
    count = max(0, int(bids_received))
    if count <= low_max:
        return "low"
    if count <= medium_max:
        return "medium"
    return "high"


class FreelancerUnavailable(RuntimeError):
    """Источник недоступен; вызывающий обязан сохранить уже лежащие в БД гиги."""


class FreelancerGigSource:
    """Публичный live-источник Freelancer.com.

    Только чтение: логина нет, ничего не отправляется, аккаунт не меняется.
    Объявляет ``source_mode="live"`` и ``source_scope="public_jobs"``.
    """

    key = "freelancer"
    source_mode = "live"
    source_scope = "public_jobs"

    def __init__(
        self,
        listing_pages: int = DEFAULT_LISTING_PAGES,
        *,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        user_agent: str = DEFAULT_USER_AGENT,
        respect_robots: bool = True,
        competition_thresholds: dict[str, int] | None = None,
        snapshot_path: str | None = None,
        listing_snapshot_path: str | None = None,
        fetch_details: bool = True,
        fetch_html: Callable[[str], str] | None = None,
        target_skills: tuple[str, ...] | list[str] | None = None,
        skill_pages: int = DEFAULT_SKILL_PAGES,
        include_general_listing: bool = True,
        access_policy: dict[str, Any] | None = None,
    ) -> None:
        #: P5: направления адресного поиска. Пустой список — только общая лента.
        self.target_skills = tuple(target_skills) if target_skills is not None \
            else DEFAULT_TARGET_SKILLS
        self.skill_pages = max(1, int(skill_pages))
        #: Общую ленту можно отключить, когда собираем только целевые направления.
        self.include_general_listing = include_general_listing
        self.access_policy = access_policy or {}
        self.listing_pages = max(1, int(listing_pages))
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.timeout_seconds = float(timeout_seconds)
        self.max_requests = max(1, int(max_requests))
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self.competition_thresholds = dict(competition_thresholds or DEFAULT_COMPETITION_THRESHOLDS)
        #: Детерминированный офлайн-режим: разбирается сохранённая страница проекта.
        self.snapshot_path = snapshot_path
        #: Офлайн-режим для списка: сохранённая страница выдачи.
        self.listing_snapshot_path = listing_snapshot_path
        #: ``False`` — взять только карточки списка (быстро, но без бюджета).
        self.fetch_details = fetch_details
        #: Инъекция транспорта для deterministic-тестов. Когда задана, сеть не
        #: трогается вообще — ни для robots.txt, ни для страниц.
        self._fetch_html_override = fetch_html
        self._last_fetch_errors: list[str] = []
        self._requests_made = 0
        #: Phase 4C §3: diagnostic state of the most recent fetch (UNKNOWN = never ran).
        self._last_health = None
        #: URL проекта -> направления, через которые он найден (P5 §2).
        self._found_via: dict[str, list[str]] = {}
        self._robots_parser: urllib.robotparser.RobotFileParser | None = None

    # ------------------------------------------------------------------ HTTP
    def _robots_allows(self, url: str) -> bool:
        """Проверяет robots.txt один раз. Недоступный robots.txt не блокирует чтение.

        Отказ от всего из-за недоступного robots.txt был бы худшим сбоем, чем один
        вежливый запрос, поэтому ошибка чтения трактуется как «разрешено».
        """
        if self._fetch_html_override is not None:
            # Инъецированный транспорт означает офлайн-прогон: robots.txt читать
            # неоткуда, а ходить в сеть из теста нельзя.
            return True
        from ..provider_policy import website_access_reasons
        access_reasons = website_access_reasons(self.access_policy)
        if access_reasons:
            raise FreelancerUnavailable('WEBSITE_AUTOMATION_BLOCKED: ' + ','.join(access_reasons))
        if self._robots_parser is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(FREELANCER_HOST + "/robots.txt")
            try:
                parser.read()
            except Exception as exc:  # noqa: BLE001 - robots best-effort
                logger.warning("freelancer: robots.txt unreadable (%s); proceeding politely", exc)
                parser = None
            self._robots_parser = parser
        if self._robots_parser is None:
            return True
        try:
            return self._robots_parser.can_fetch(self.user_agent, url)
        except Exception:  # noqa: BLE001
            return True

    def _http_get(self, url: str) -> str:
        if self._fetch_html_override is None:
            from ..provider_policy import website_access_reasons
            access_reasons = website_access_reasons(self.access_policy)
            if access_reasons:
                raise FreelancerUnavailable('WEBSITE_AUTOMATION_BLOCKED: ' + ','.join(access_reasons))
        if self._requests_made >= self.max_requests:
            raise FreelancerUnavailable(
                f"freelancer: request budget exhausted ({self.max_requests}); "
                "raise max_requests or lower the page count"
            )
        self._requests_made += 1
        if self._fetch_html_override is not None:
            return self._fetch_html_override(url)
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "text/html,*/*;q=0.8",
                          "Accept-Encoding": "gzip"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            # A socket timeout applies between packets, not to the whole body.
            # Bound slow streaming responses too; never rank a partial document.
            if hasattr(response, "read1"):
                deadline = time.monotonic() + 30
                chunks = []
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("public page body exceeded 30 seconds")
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
            else:
                body = response.read()
            if str(response.headers.get("Content-Encoding", "")).lower() == "gzip":
                body = gzip.decompress(body)
            return body.decode("utf-8", "replace")

    # -------------------------------------------------------------- разбор
    @staticmethod
    def project_links(html: str) -> list[str]:
        """URL проектов со страницы выдачи, в порядке появления и без дублей."""
        seen: set[str] = set()
        result: list[str] = []
        for match in _CARD_URL_RE.finditer(html):
            url = match.group(1)
            if url not in seen:
                seen.add(url)
                result.append(url)
        return result

    @staticmethod
    def cards_from_listing(html: str) -> list[dict[str, Any]]:
        """Карточки страницы выдачи — то, что список отдаёт сам по себе.

        Числа ставок и «средняя ставка» здесь именно с карточки; бюджет заказчика
        в карточке отсутствует, поэтому он сюда не попадает.
        """
        cards: list[dict[str, Any]] = []
        for block in _CARD_SPLIT_RE.split(html)[1:]:
            url_match = _CARD_URL_RE.search(block)
            if not url_match:
                continue
            title_match = _CARD_TITLE_RE.search(block)
            days_match = _CARD_DAYS_RE.search(block)
            bids_match = _CARD_BIDS_RE.search(block)
            desc_match = _CARD_DESC_RE.search(block)
            cards.append({
                "url": url_match.group(1),
                "title": _untext(title_match.group(1)) if title_match else "",
                "days_left_text": _untext(days_match.group(1)) if days_match else None,
                "skills": [_untext(s) for s in _CARD_SKILL_RE.findall(block)],
                "bids_received": _to_int(bids_match.group(1)) if bids_match else None,
                "description_preview": _untext(desc_match.group(1)) if desc_match else "",
            })
        return cards

    @staticmethod
    def webapp_state(html: str) -> dict[str, Any] | None:
        """Hydration-состояние страницы проекта, либо ``None``.

        Отсутствие блока — не ошибка разбора, а реальный вариант страницы
        (наблюдались ответы 200 без состояния). Возвращаем ``None``, чтобы
        вызывающий отличил «нет данных» от «данные пустые».
        """
        match = _WEBAPP_STATE_RE.search(html)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def raw_document(state: dict[str, Any]) -> dict[str, Any] | None:
        """Достаёт ``rawDocument`` проекта из ``NGRX_STATE.projectsSeo``."""
        ngrx = state.get("NGRX_STATE") if isinstance(state.get("NGRX_STATE"), dict) else state
        seo = (ngrx or {}).get("projectsSeo") or {}
        if isinstance(seo, dict):
            entries = [seo.get(key) for key in sorted(seo)]
        elif isinstance(seo, list):
            entries = seo
        else:
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            documents = entry.get("documents") or {}
            if not isinstance(documents, dict):
                continue
            for payload in documents.values():
                doc = (payload or {}).get("rawDocument") if isinstance(payload, dict) else None
                if isinstance(doc, dict) and doc.get("projectId"):
                    return doc
        return None

    def record_from_document(
        self,
        doc: dict[str, Any],
        *,
        page_url: str,
        fetched_at: str,
        found_via: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """``rawDocument`` -> сырой dict для ``normalize_gig``.

        Отсутствующие поля остаются отсутствующими: ни бюджет, ни клиент, ни сроки
        не додумываются. ``found_via`` — это через какие направления проект найден
        (P5 §2); он хранится отдельно и **не участвует** в score как множитель.
        """
        project_id = doc.get("projectId")
        title = _untext(doc.get("title"))
        if project_id in (None, "") or not title:
            return None
        source_id = f"fl_{int(project_id)}" if str(project_id).isdigit() else f"fl_{project_id}"

        budget = doc.get("budget") if isinstance(doc.get("budget"), dict) else {}
        currency_details = doc.get("currencyDetails") if isinstance(doc.get("currencyDetails"), dict) else {}
        currency = str(currency_details.get("code") or "").strip() or None
        # P4: Freelancer сам отдаёт курс этой валюты к USD. Без него бюджет в INR
        # несопоставим с USD-бюджетом, и scoring либо завышал его в десятки раз,
        # либо (после калибровки) оставлял неопределённым. Курс сохраняется как
        # факт источника — конверсия остаётся проверяемой.
        exchange_rate = _to_float(currency_details.get("exchangeRate"))

        bid_stats = doc.get("bidStats") if isinstance(doc.get("bidStats"), dict) else {}
        bids_received = _to_int(bid_stats.get("bidCount"))
        # Фолбэк на длину списка ставок — только когда список НЕ пуст. Пустой
        # список означает «ставки не показаны», а не «ставок ноль»: принять []
        # за ноль значило бы выдумать измерение и завысить привлекательность.
        if bids_received is None and isinstance(doc.get("bids"), list) and doc["bids"]:
            bids_received = len(doc["bids"])
        bid_average = _to_float(bid_stats.get("bidAvg"))

        client = doc.get("client") if isinstance(doc.get("client"), dict) else {}
        client_rating_block = client.get("rating") if isinstance(client.get("rating"), dict) else {}
        client_address = client.get("address") if isinstance(client.get("address"), dict) else {}
        client_verification = client.get("verification") if isinstance(client.get("verification"), dict) else {}

        client_rating = _to_float(client_rating_block.get("average"))
        # Рейтинг с нулём отзывов — это отсутствие рейтинга, а не «ноль звёзд».
        if client_rating == 0 and not _to_int(client_rating_block.get("reviewCount")):
            client_rating = None

        skills = []
        if isinstance(doc.get("skills"), list):
            for item in doc["skills"]:
                if isinstance(item, dict) and item.get("name"):
                    skills.append(_untext(item["name"]))
                elif isinstance(item, str):
                    skills.append(_untext(item))

        project_type = str(doc.get("type") or "").strip().lower() or None
        budget_min = _to_float(budget.get("min"))
        budget_max = _to_float(budget.get("max"))
        # Freelancer отдаёт 0 для границ, которые не заданы. Ноль здесь не сумма.
        if budget_min == 0:
            budget_min = None
        if budget_max == 0:
            budget_max = None
        if project_type is None:
            project_type = "fixed" if (budget_min is not None or budget_max is not None) else None

        slug = str(doc.get("seoUrl") or "").strip() or page_url.rsplit("/projects/", 1)[-1]
        bids_list = doc.get("bids") if isinstance(doc.get("bids"), list) else []
        top_bid = bids_list[0] if bids_list and isinstance(bids_list[0], dict) else {}

        return {
            "source": self.key,
            "source_id": source_id,
            "url": f"{FREELANCER_HOST}/projects/{slug}",
            "title": title,
            "description": _untext(doc.get("description")),
            "category": _untext((doc.get("breadcrumb") or {}).get("skill"))
            if isinstance(doc.get("breadcrumb"), dict) else None,
            "skills": skills,
            "budget": {"min": budget_min, "max": budget_max, "type": project_type},
            "currency": currency,
            "published_at": _ms_to_iso(doc.get("startTime")),
            # Конкуренция берётся из публичного числа ставок, а уровень считается из него.
            "competition": bids_received,
            "client_name": None,
            "client_country": _untext(client_address.get("country")) or None,
            "client_rating": client_rating,
            "client_jobs_posted": None,
            "client_hires": None,
            "client_total_spent": None,
            "proposal_cost": None,
            # --- провенанс -----------------------------------------------------
            "source_mode": self.source_mode,
            "source_scope": self.source_scope,
            "source_url": page_url,
            "fetched_at": fetched_at,
            "source_status": normalize_source_status(doc.get("status")),
            "platform_payment_options": list(FREELANCER_PLATFORM_PAYMENT_OPTIONS),
            "source_facts": {
                "freelancer_project_id": project_id,
                "freelancer_slug": slug,
                "freelancer_status": doc.get("status"),
                "freelancer_type": doc.get("type"),
                "freelancer_bids_received": bids_received,
                "freelancer_bid_average": bid_average,
                "freelancer_deadline": _ms_to_iso(doc.get("endTime")),
                "freelancer_days_left": _to_int(doc.get("daysLeft")),
                "freelancer_hours_left": _to_int(doc.get("hoursLeft")),
                "freelancer_latest_activity": _ms_to_iso(doc.get("latestActivityTime")),
                "freelancer_formatted_budget": doc.get("formattedBudget"),
                "freelancer_currency": currency,
                "freelancer_currency_exchange_rate": exchange_rate,
                "freelancer_competition_level": competition_level(
                    bids_received, self.competition_thresholds
                ),
                # Публичные признаки клиента. Это факты о клиенте, а не о нашем аккаунте.
                "freelancer_client_country_code": (client_address.get("countryCode") or None),
                "freelancer_client_city": _untext(client_address.get("city")) or None,
                "freelancer_client_registered_at": _ms_to_iso(client.get("registrationTime")),
                "freelancer_client_reviews": _to_int(client_rating_block.get("reviewCount")),
                "freelancer_client_payment_verified": client_verification.get("paymentVerified"),
                "freelancer_client_email_verified": client_verification.get("emailVerified"),
                "freelancer_client_phone_verified": client_verification.get("phoneVerified"),
                "freelancer_client_deposit_made": client_verification.get("depositMade"),
                "freelancer_client_profile_complete": client_verification.get("profileComplete"),
                "freelancer_top_bid": _to_float(top_bid.get("amount")),
                "freelancer_top_bidder_rating": _to_float(
                    (top_bid.get("sellerRating") or {}).get("average")
                ) if isinstance(top_bid.get("sellerRating"), dict) else None,
                "freelancer_source_surface": "public /jobs listing + public project page",
                # P5 §2: через какие целевые направления проект найден. Хранится как
                # факт; scoring его как множитель НЕ использует, иначе один и тот же
                # проект получал бы разные баллы в зависимости от числа совпавших
                # направлений, а не от своей сути.
                "found_via": sorted(found_via or []),
                "found_via_count": len(set(found_via or [])),
            },
            "derived_fields": [
                "source_id",
                "budget_min",
                "budget_max",
                "budget_type",
                "published_at",
                "competition",
                "source_mode",
                "source_scope",
                "fetched_at",
                "source_status",
            ],
        }

    def record_from_html(
        self,
        html: str,
        *,
        page_url: str,
        fetched_at: str,
        found_via: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Страница проекта -> один сырой dict, либо ``None`` если состояния нет."""
        state = self.webapp_state(html)
        if state is None:
            return None
        doc = self.raw_document(state)
        if doc is None:
            return None
        return self.record_from_document(
            doc, page_url=page_url, fetched_at=fetched_at, found_via=found_via
        )

    # ------------------------------------------------------------- снимки
    def records_from_snapshot(self) -> list[dict[str, Any]]:
        """Офлайн-разбор сохранённой страницы проекта (детерминированные тесты)."""
        if not self.snapshot_path:
            return []
        with open(self.snapshot_path, "r", encoding="utf-8", errors="replace") as handle:
            html = handle.read()
        url = f"{FREELANCER_HOST}/projects/snapshot/snapshot"
        record = self.record_from_html(html, page_url=url, fetched_at=_utcnow())
        return [record] if record else []

    def records_from_listing_snapshot(self) -> list[dict[str, Any]]:
        """Офлайн-обход: разбор сохранённой выдачи без сети.

        Даёт записи уровня карточки (без бюджета) — этого достаточно для
        детерминированных тестов пагинации, дедупа и стабильности id.
        """
        if not self.listing_snapshot_path:
            return []
        with open(self.listing_snapshot_path, "r", encoding="utf-8", errors="replace") as handle:
            html = handle.read()
        fetched_at = _utcnow()
        records: list[dict[str, Any]] = []
        for card in self.cards_from_listing(html):
            record = self.card_record(card, fetched_at=fetched_at)
            if record:
                records.append(record)
        return records

    def card_record(
        self,
        card: dict[str, Any],
        *,
        fetched_at: str,
        found_via: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Карточка выдачи -> сырой dict (без бюджета: список его не публикует)."""
        url = str(card.get("url") or "").strip()
        title = str(card.get("title") or "").strip()
        if not url or not title:
            return None
        # Стабильный id берётся из URL только если он там реально есть; иначе
        # источником идентичности служит сам путь проекта.
        numeric = re.search(r"-(\d{6,})$", url)
        source_id = f"fl_{numeric.group(1)}" if numeric else f"fl_slug_{url.rsplit('/', 1)[-1]}"
        return {
            "source": self.key,
            "source_id": source_id,
            "url": f"{FREELANCER_HOST}{url}",
            "title": title,
            "description": str(card.get("description_preview") or ""),
            "category": None,
            "skills": list(card.get("skills") or []),
            # Бюджет заказчика в карточке не публикуется — остаётся неизвестным.
            "budget": {"min": None, "max": None, "type": None},
            "currency": None,
            "published_at": None,
            "competition": card.get("bids_received"),
            "client_name": None,
            "client_country": None,
            "client_rating": None,
            "client_jobs_posted": None,
            "client_hires": None,
            "client_total_spent": None,
            "proposal_cost": None,
            "source_mode": self.source_mode,
            "source_scope": self.source_scope,
            "source_url": f"{FREELANCER_HOST}{url}",
            "fetched_at": fetched_at,
            "source_status": "OPEN",
            "platform_payment_options": list(FREELANCER_PLATFORM_PAYMENT_OPTIONS),
            "source_facts": {
                "freelancer_slug": url.rsplit("/", 1)[-1],
                "freelancer_bids_received": card.get("bids_received"),
                "freelancer_days_left_text": card.get("days_left_text"),
                "freelancer_competition_level": competition_level(
                    card.get("bids_received"), self.competition_thresholds
                ),
                "freelancer_source_surface": "public /jobs listing card (no budget published)",
                "found_via": sorted(found_via or []),
                "found_via_count": len(set(found_via or [])),
            },
            "derived_fields": [
                "source_id",
                "competition",
                "source_mode",
                "source_scope",
                "fetched_at",
                "source_status",
            ],
        }

    # --------------------------------------------------------------- public
    def _listing_urls(self) -> list[tuple[str, str | None]]:
        """URL выдачи + направление, к которому она относится (``None`` для общей)."""
        urls: list[tuple[str, str | None]] = []
        if self.include_general_listing:
            for page in range(1, self.listing_pages + 1):
                url = (f"{FREELANCER_HOST}{DEFAULT_LISTING_PATH}" if page == 1
                       else f"{FREELANCER_HOST}{DEFAULT_LISTING_PATH}{page}")
                urls.append((url, None))
        for skill in self.target_skills:
            for page in range(1, self.skill_pages + 1):
                url = f"{FREELANCER_HOST}/jobs/{skill}/"
                if page > 1:
                    url = f"{url}{page}"
                urls.append((url, skill))
        return urls

    def fetch(self, limit: int | None = None) -> Iterator[dict]:
        """Отдаёт сырые заказы Freelancer (с диагностикой прогона).

        Обёртка считает отданные записи и фиксирует ``health()`` при любом
        исходе — включая исключение: вызывающий видит и ошибку, и диагноз.
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
        """Отдаёт сырые заказы Freelancer.

        P5: сначала читаются целевые направления (``/jobs/<skill>/``), затем общая
        лента. Один проект может встретиться в нескольких направлениях: тогда он
        отдаётся один раз, а список направлений накапливается в ``found_via``.

        Сетевые сбои не превращаются в частичный мусор: уже собранные записи
        отдаются, причина пишется в ``last_fetch_errors``, а если не собрано
        ничего — поднимается ``FreelancerUnavailable``, чтобы вызывающий не
        тронул уже сохранённые гиги.
        """
        self._last_fetch_errors = []
        self._requests_made = 0
        wanted = int(limit) if limit is not None else None

        # --- офлайн-режимы ---------------------------------------------------
        if self.snapshot_path:
            for record in self.records_from_snapshot()[:wanted] if wanted else self.records_from_snapshot():
                yield record
            return
        if self.listing_snapshot_path:
            records = self.records_from_listing_snapshot()
            yield from (records[:wanted] if wanted is not None else records)
            return

        fetched_at = _utcnow()
        produced = 0
        #: project_url -> накопленные направления. Именно здесь один проект,
        #: найденный в нескольких областях, остаётся одной записью.
        found_via: dict[str, list[str]] = {}
        cards_by_url: dict[str, dict[str, Any]] = {}

        # --- ФАЗА 1: обойти все страницы выдачи и построить карту направлений.
        #
        # Порядок важен. Если тянуть детали сразу, проект будет отдан при первой
        # встрече, и вторая находка того же проекта в другом направлении уже не
        # попадёт в found_via — поле останется с одним значением. Поэтому сначала
        # собирается вся карта, а детали читаются один раз на уникальный проект:
        # это и корректнее, и экономит запросы.
        for listing_url, skill in self._listing_urls():
            if self.respect_robots and not self._robots_allows(listing_url):
                self._last_fetch_errors.append(f"robots.txt disallows {listing_url}")
                continue
            try:
                html = self._http_get(listing_url)
            except FreelancerUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_fetch_errors.append(f"{listing_url}: {type(exc).__name__}: {exc}")
                continue

            for card in self.cards_from_listing(html):
                url = card["url"]
                if url not in cards_by_url:
                    cards_by_url[url] = card
                if skill and skill not in found_via.setdefault(url, []):
                    found_via[url].append(skill)

            if self.delay_seconds:
                import time as _time

                _time.sleep(self.delay_seconds)

        # --- ФАЗА 2: уникальные проекты, по одному запросу на каждый.
        #
        # Дедуп идёт по двум ключам, а не только по URL выдачи:
        #   * URL — чтобы один проект, найденный в нескольких направлениях, читался
        #     один раз (это же даёт полный found_via);
        #   * ``source_id`` — потому что источник может отдать два разных URL на один
        #     проект, и без этой проверки дубль дошёл бы до pipeline.
        # Измерено: при одинаковом ответе на разные URL получалось 5 записей с одним
        # ``fl_40741883`` вместо одной — то есть дедуп по URL этого не ловил.
        emitted_source_ids: set[str] = set()
        for url, card in cards_by_url.items():
            if wanted is not None and produced >= wanted:
                break
            project_url = f"{FREELANCER_HOST}{url}"
            via = found_via.get(url, [])
            record: dict[str, Any] | None = None

            if self.fetch_details:
                if self.respect_robots and not self._robots_allows(project_url):
                    self._last_fetch_errors.append(f"robots.txt disallows {project_url}")
                    continue
                try:
                    page_html = self._http_get(project_url)
                    record = self.record_from_html(
                        page_html, page_url=project_url, fetched_at=fetched_at,
                        found_via=via,
                    )
                    if record is None:
                        # Страница без hydration-состояния — реальный вариант
                        # ответа 200. Падаем на данные карточки, а не выдумываем.
                        self._last_fetch_errors.append(
                            f"{project_url}: no hydration state; fell back to card fields"
                        )
                        record = self.card_record(card, fetched_at=fetched_at, found_via=via)
                except FreelancerUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._last_fetch_errors.append(
                        f"{project_url}: {type(exc).__name__}: {exc}"
                    )
                    record = self.card_record(card, fetched_at=fetched_at, found_via=via)
            else:
                record = self.card_record(card, fetched_at=fetched_at, found_via=via)

            if record is None:
                continue

            source_id = str(record.get("source_id") or "")
            if source_id and source_id in emitted_source_ids:
                # Два URL одного проекта: запись уже отдана, но направление может
                # быть новым — оно сохраняется в карте, а не теряется.
                self._last_fetch_errors.append(
                    f"{project_url}: duplicate source_id {source_id}; merged into the first record"
                )
                continue
            if source_id:
                emitted_source_ids.add(source_id)

            produced += 1
            yield record

            if self.delay_seconds and self.fetch_details:
                import time as _time

                _time.sleep(self.delay_seconds)

        self._found_via = {url: list(v) for url, v in found_via.items()}

        if produced == 0:
            detail = "; ".join(self._last_fetch_errors[:3]) or "no records parsed"
            raise FreelancerUnavailable(f"freelancer: nothing fetched ({detail})")

    @property
    def found_via_map(self) -> dict[str, list[str]]:
        """Направления по URL проекта — доступно после ``fetch`` (P5 §2)."""
        return {url: list(v) for url, v in self._found_via.items()}

    @property
    def last_fetch_errors(self) -> list[str]:
        return list(self._last_fetch_errors)

    @property
    def requests_made(self) -> int:
        return self._requests_made


__all__ = [
    "DEFAULT_COMPETITION_THRESHOLDS",
    "DEFAULT_SKILL_PAGES",
    "DEFAULT_TARGET_SKILLS",
    "FREELANCER_HOST",
    "FREELANCER_PLATFORM_PAYMENT_OPTIONS",
    "FreelancerGigSource",
    "FreelancerUnavailable",
    "competition_level",
    "normalize_source_status",
]
