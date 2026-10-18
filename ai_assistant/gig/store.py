"""Хранилище гигов (SQLite).

Изолировано от `db.py`: модуль пишет только в таблицы ``gigs`` / ``gig_events``
и никогда не трогает ``vacancies``/``canonical_vacancies``. Схема применяется
идемпотентно через общий ``init_db()``.

Семантика ``save_gig`` (по аналогии с ``save_vacancy``):
- INSERT нового гига;
- UPDATE при изменении содержимого (content_hash);
- "UNCHANGED", если ничего не поменялось;
- статус, выставленный человеком (SHORTLISTED/SKIPPED), повторным
  ingestion НЕ перезаписывается.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import config
from ..db import get_connection, init_db, is_dry_run
from .models import (
    BudgetType,
    CanDeliver,
    Gig,
    GigStatus,
    PaymentReadiness,
    RejectionReason,
    RiskLevel,
    SourceMode,
)
from .normalizer import content_hash

#: Колонки, которые являются результатом вычисления на текущих правилах, а не
#: содержимым гига. Пересчёт scoring обязан их обновлять даже при неизменном контенте,
#: иначе калибровка не доходит до БД (P4).
_SCORE_COLUMNS = (
    "payment_readiness",
    "can_deliver",
    "ai_automation_potential",
    "estimated_effort_hours",
    "effective_hourly_rate",
    "client_quality",
    "risk_level",
    "risks_json",
    "match_score",
    "opportunity_score",
    "score_breakdown_json",
    "why_matched_json",
    "why_risky_json",
)

_JSON_FIELDS = (
    ("skills_json", "skills"),
    ("risks_json", "risks"),
    ("score_breakdown_json", "score_breakdown"),
    ("why_matched_json", "why_matched"),
    ("why_risky_json", "why_risky"),
    ("rejection_reasons_json", "rejection_reasons"),
    ("platform_payment_options_json", "platform_payment_options"),
    ("source_facts_json", "source_facts"),
    ("derived_fields_json", "derived_fields"),
)
_SCALAR_FIELDS = (
    "id",
    "source",
    "source_id",
    "url",
    "title",
    "description",
    "category",
    "budget_min",
    "budget_max",
    "currency",
    "budget_type",
    "published_at",
    "client_name",
    "client_country",
    "client_rating",
    "client_jobs_posted",
    "client_hires",
    "client_total_spent",
    "competition",
    "proposal_cost",
    "payment_readiness",
    "can_deliver",
    "ai_automation_potential",
    "estimated_effort_hours",
    "effective_hourly_rate",
    "client_quality",
    "risk_level",
    "match_score",
    "opportunity_score",
    "status",
    "created_at",
    "updated_at",
    # P2 provenance / observation window
    "source_mode",
    "source_scope",
    "source_url",
    "fetched_at",
    "source_status",
    "first_seen_at",
    "last_seen_at",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    init_db()
    return get_connection()


def _to_cell(field: str, gig: Gig) -> Any:
    value = getattr(gig, field)
    if hasattr(value, "value"):  # enum
        return value.value
    # `url` carries a UNIQUE index. SQLite allows many NULLs there but only one
    # empty string, so an absent URL must be stored as NULL rather than "" -
    # otherwise every url-less gig collides with the first one (P2: Contra
    # publishes no per-job page, so every live gig has no url).
    if field == "url" and not str(value or "").strip():
        return None
    return value


def _row_to_gig(row: sqlite3.Row | tuple) -> Gig:
    """Строка БД -> ``Gig``.

    Колонки читаются по именам ``cursor.description``, поэтому порядок SELECT
    (``gigs.*``) не критичен.
    """
    data = dict(row) if not isinstance(row, sqlite3.Row) else dict(row)
    gig = Gig(
        id=data.get("id") or "",
        source=data.get("source") or "",
        source_id=data.get("source_id") or "",
        url=data.get("url") or "",
        title=data.get("title") or "",
        description=data.get("description") or "",
        category=data.get("category"),
        skills=json.loads(data.get("skills_json") or "[]"),
        budget_min=data.get("budget_min"),
        budget_max=data.get("budget_max"),
        currency=data.get("currency"),
        budget_type=BudgetType.parse(data.get("budget_type")),
        published_at=data.get("published_at"),
        client_name=data.get("client_name"),
        client_country=data.get("client_country"),
        client_rating=data.get("client_rating"),
        client_jobs_posted=data.get("client_jobs_posted"),
        client_hires=data.get("client_hires"),
        client_total_spent=data.get("client_total_spent"),
        competition=data.get("competition"),
        proposal_cost=data.get("proposal_cost"),
        payment_readiness=PaymentReadiness.parse(data.get("payment_readiness")),
        can_deliver=CanDeliver.parse(data.get("can_deliver")),
        ai_automation_potential=data.get("ai_automation_potential"),
        estimated_effort_hours=data.get("estimated_effort_hours"),
        effective_hourly_rate=data.get("effective_hourly_rate"),
        client_quality=data.get("client_quality"),
        risk_level=RiskLevel.parse(data.get("risk_level")),
        risks=json.loads(data.get("risks_json") or "[]"),
        match_score=data.get("match_score"),
        opportunity_score=data.get("opportunity_score"),
        score_breakdown=json.loads(data.get("score_breakdown_json") or "{}"),
        rejection_reasons=json.loads(data.get("rejection_reasons_json") or "[]"),
        why_matched=json.loads(data.get("why_matched_json") or "[]"),
        why_risky=json.loads(data.get("why_risky_json") or "[]"),
        status=GigStatus.parse(data.get("status")),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        # P2 (NULL for rows written by P1 - the enum parser maps that to UNKNOWN)
        source_mode=SourceMode.parse(data.get("source_mode")),
        source_scope=data.get("source_scope"),
        source_url=data.get("source_url"),
        fetched_at=data.get("fetched_at"),
        source_status=data.get("source_status"),
        first_seen_at=data.get("first_seen_at"),
        last_seen_at=data.get("last_seen_at"),
        platform_payment_options=json.loads(data.get("platform_payment_options_json") or "[]"),
        source_facts=json.loads(data.get("source_facts_json") or "{}"),
        derived_fields=json.loads(data.get("derived_fields_json") or "[]"),
    )
    return gig


def _select_gig(conn: sqlite3.Connection, gig_id: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM gigs WHERE id = ?", (gig_id,))
    return cur.fetchone()


def save_gig(gig: Gig) -> str:
    """Сохраняет гиг идемпотентно. Возвращает INSERTED/UPDATED/UNCHANGED.

    В dry-run режиме мутации запрещены: возвращает "DRY_RUN".
    """
    if is_dry_run():
        return "DRY_RUN"
    if not gig.id:
        raise ValueError("save_gig: у гига отсутствует id")

    conn = _connect()
    try:
        existing = _select_gig(conn, gig.id)
        now = _utcnow()

        columns = list(_SCALAR_FIELDS) + [name for name, _ in _JSON_FIELDS]
        columns_with_extra = columns + ["content_hash", "raw_json"]
        values: dict[str, Any] = {}
        for field in _SCALAR_FIELDS:
            values[field] = _to_cell(field, gig)
        for column, attr in _JSON_FIELDS:
            values[column] = json.dumps(getattr(gig, attr), ensure_ascii=False)
        values["content_hash"] = content_hash(gig)
        values["raw_json"] = json.dumps(gig.raw_data, ensure_ascii=False)

        if existing is None:
            if not values.get("created_at"):
                values["created_at"] = now
            values["updated_at"] = now
            # P2 §8: the first time we ever stored this gig.
            if not values.get("first_seen_at"):
                values["first_seen_at"] = values.get("fetched_at") or now
            if not values.get("last_seen_at"):
                values["last_seen_at"] = values.get("fetched_at") or now
            placeholders = ", ".join("?" for _ in columns_with_extra)
            conn.execute(
                f"INSERT INTO gigs ({', '.join(columns_with_extra)}) VALUES ({placeholders})",
                [values[c] for c in columns_with_extra],
            )
            conn.commit()
            return "INSERTED"

        existing_dict = dict(existing)

        # P2: provenance is observation metadata, not content. Refreshing it on a
        # re-fetch is exactly right - the same gig may be observed again from a
        # source that now states its mode/scope/status. User status and content
        # are deliberately left untouched.
        _PROVENANCE_COLUMNS = (
            "source_mode",
            "source_scope",
            "source_url",
            "fetched_at",
            "source_status",
            "platform_payment_options_json",
            "source_facts_json",
            "derived_fields_json",
        )

        # P2 §8 / P3 §10: last_seen_at advances from a real observation and never
        # moves backwards. The observation timestamp is ``fetched_at`` — when we
        # actually saw the record — because no source populates last_seen_at in
        # its own payload. Deriving it from fetched_at is what makes a re-fetch
        # advance the value at all; reading it from the record left it frozen at
        # first insert forever.
        existing_seen = existing_dict.get("last_seen_at")
        observed_at = values.get("fetched_at")
        candidates = [str(v) for v in (existing_seen, observed_at) if v]
        if candidates:
            values["last_seen_at"] = max(candidates)
        else:
            values["last_seen_at"] = existing_seen
        # first_seen_at is immutable once set.
        values["first_seen_at"] = existing_dict.get("first_seen_at") or values.get("first_seen_at") or now

        new_hash = values["content_hash"]
        if existing_dict.get("content_hash") == new_hash:
            # Содержимое не изменилось; статус, выставленный человеком, не трогаем.
            # Но наблюдаемость и провенанс могли обновиться, поэтому они пишутся
            # отдельно - без изменения контента и без понижения статуса.
            touched = {
                col: values[col] for col in _PROVENANCE_COLUMNS
                if values.get(col) != existing_dict.get(col)
            }
            # P4: пересчитанные оценки тоже сохраняются. Раньше эта ветка писала
            # только провенанс, поэтому после калибровки scoring в БД оставались
            # старые баллы: `can_deliver=high` и 79.4 у заказа, который новая
            # логика оценивает в 34.4 / low.
            #
            # Важно: DUPLICATE — это не новая оценка того же гига, а признание,
            # что запись уже есть. Такая запись scoring не проходит (балл = None),
            # и писать её нельзя — иначе повторный fetch обнулял бы балл уже
            # лежащей канонической записи.
            incoming_rejections = list(gig.rejection_reasons or [])
            is_duplicate = RejectionReason.DUPLICATE.value in incoming_rejections
            if not is_duplicate:
                for col in _SCORE_COLUMNS:
                    if values.get(col) != existing_dict.get(col):
                        touched[col] = values[col]
            if values.get("last_seen_at") and values["last_seen_at"] != existing_dict.get("last_seen_at"):
                touched["last_seen_at"] = values["last_seen_at"]
            if touched:
                assignments = ", ".join(f"{col} = ?" for col in touched)
                conn.execute(
                    f"UPDATE gigs SET {assignments} WHERE id = ?",
                    list(touched.values()) + [gig.id],
                )
                conn.commit()
            return "UNCHANGED"

        # Статус — пользовательское поле: не понижаем его повторным ingestion.
        preserved_status = GigStatus.parse(existing_dict.get("status"))
        if preserved_status is not GigStatus.NEW:
            values["status"] = preserved_status.value
        values["updated_at"] = now
        values["created_at"] = existing_dict.get("created_at") or values.get("created_at") or now

        assign = ", ".join(f"{c} = ?" for c in columns_with_extra)
        conn.execute(
            f"UPDATE gigs SET {assign} WHERE id = ?",
            [values[c] for c in columns_with_extra] + [gig.id],
        )
        conn.commit()
        return "UPDATED"
    finally:
        conn.close()


def get_gig(gig_id: str) -> Gig | None:
    conn = _connect()
    try:
        row = _select_gig(conn, gig_id)
        return _row_to_gig(row) if row else None
    finally:
        conn.close()


def find_gig_by_source_id(source: str, source_id: str) -> Gig | None:
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM gigs WHERE source = ? AND source_id = ?",
            (str(source).strip().lower(), str(source_id).strip()),
        )
        row = cur.fetchone()
        return _row_to_gig(row) if row else None
    finally:
        conn.close()


def find_gig_by_url(url: str) -> Gig | None:
    if not url:
        return None
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM gigs WHERE url = ?", (url.strip(),))
        row = cur.fetchone()
        return _row_to_gig(row) if row else None
    finally:
        conn.close()


def list_gigs(
    limit: int = 50,
    status: str | None = None,
    source: str | None = None,
    include_rejected: bool = False,
    order_by: str = "opportunity_score",
    source_mode: str | None = None,
    source_scope: str | None = None,
    exclude_scope: str | None = None,
) -> list[Gig]:
    """Список гигов. По умолчанию — только прошедшие hard gates, по убыванию score.

    ``source_mode`` (P2 §7) отделяет реальные заказы от тестовой фикстуры:
    ``"live"`` / ``"fixture"``.

    ``source_scope`` / ``exclude_scope`` (P2.1 §9) разделяют поверхности одного
    живого источника: ``authenticated_feed`` — актуальные заказы, ``public_hire_pages`` —
    архив P2. Рабочий список может исключать архив явно, а не полагаться на то,
    что его случайно не видно.
    """
    order_map = {
        "opportunity_score": "opportunity_score DESC NULLS LAST, budget_max DESC NULLS LAST",
        "published_at": "published_at DESC NULLS LAST",
        "effective_hourly_rate": "effective_hourly_rate DESC NULLS LAST",
    }
    order = order_map.get(order_by, order_map["opportunity_score"])

    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(GigStatus.parse(status).value)
    if source:
        clauses.append("source = ?")
        params.append(str(source).strip().lower())
    if source_mode:
        clauses.append("source_mode = ?")
        params.append(SourceMode.parse(source_mode).value)
    if source_scope:
        clauses.append("source_scope = ?")
        params.append(str(source_scope).strip())
    if exclude_scope:
        clauses.append("(source_scope IS NULL OR source_scope != ?)")
        params.append(str(exclude_scope).strip())
    if not include_rejected:
        # reject-гиги хранят свои причины в rejection_reasons_json == 'null'/'[]'
        clauses.append("(rejection_reasons_json IS NULL OR rejection_reasons_json IN ('[]', 'null', ''))")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM gigs {where} ORDER BY {order} LIMIT ?", params + [int(limit)])
        return [_row_to_gig(row) for row in cur.fetchall()]
    finally:
        conn.close()


def set_gig_status(gig_id: str, status: str, note: str | None = None) -> Gig | None:
    """Меняет статус гига (человеческое решение) и пишет событие в ``gig_events``."""
    new_status = GigStatus.parse(status)
    if is_dry_run():
        return None
    conn = _connect()
    try:
        existing = _select_gig(conn, gig_id)
        if existing is None:
            return None
        old_status = GigStatus.parse(dict(existing).get("status")).value
        now = _utcnow()
        conn.execute("UPDATE gigs SET status = ?, updated_at = ? WHERE id = ?", (new_status.value, now, gig_id))
        conn.execute(
            "INSERT INTO gig_events (gig_id, event_type, detail_json, created_at) VALUES (?, ?, ?, ?)",
            (
                gig_id,
                "STATUS_CHANGED",
                json.dumps({"from": old_status, "to": new_status.value, "note": note}, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        row = _select_gig(conn, gig_id)
        return _row_to_gig(row) if row else None
    finally:
        conn.close()


def record_gig_event(gig_id: str, event_type: str, detail: dict[str, Any] | None = None) -> None:
    """Аудит-событие по гигу (например, REJECTED с причинами)."""
    if is_dry_run():
        return
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO gig_events (gig_id, event_type, detail_json, created_at) VALUES (?, ?, ?, ?)",
            (gig_id, str(event_type), json.dumps(detail or {}, ensure_ascii=False), _utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def list_gig_events(gig_id: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM gig_events WHERE gig_id = ? ORDER BY id DESC LIMIT ?",
            (gig_id, int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def count_gigs() -> int:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM gigs")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def gig_stats() -> dict[str, Any]:
    """Статистика контура: total/accepted/rejected/duplicates + разрезы."""
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM gigs")
        total = int(cur.fetchone()["n"])

        cur.execute(
            "SELECT COUNT(*) AS n FROM gigs WHERE rejection_reasons_json = '[\"DUPLICATE\"]'"
        )
        duplicates = int(cur.fetchone()["n"])

        cur.execute(
            """SELECT COUNT(*) AS n FROM gigs
               WHERE rejection_reasons_json IS NOT NULL
                 AND rejection_reasons_json NOT IN ('[]', 'null', '')
                 AND rejection_reasons_json != '[\"DUPLICATE\"]'"""
        )
        rejected = int(cur.fetchone()["n"])

        accepted = total - duplicates - rejected

        cur.execute("SELECT status, COUNT(*) AS n FROM gigs GROUP BY status")
        by_status = {r["status"]: int(r["n"]) for r in cur.fetchall()}

        cur.execute("SELECT source, COUNT(*) AS n FROM gigs GROUP BY source")
        by_source = {r["source"]: int(r["n"]) for r in cur.fetchall()}

        cur.execute("SELECT COALESCE(source_mode, 'unknown') AS m, COUNT(*) AS n FROM gigs GROUP BY m")
        by_mode = {r["m"]: int(r["n"]) for r in cur.fetchall()}

        cur.execute(
            "SELECT COALESCE(source_scope, 'unknown') AS s, COUNT(*) AS n FROM gigs GROUP BY s"
        )
        by_scope = {r["s"]: int(r["n"]) for r in cur.fetchall()}

        cur.execute(
            """SELECT rejection_reasons_json, COUNT(*) AS n FROM gigs
               WHERE rejection_reasons_json IS NOT NULL AND rejection_reasons_json NOT IN ('[]', 'null', '')
               GROUP BY rejection_reasons_json"""
        )
        by_reason: dict[str, int] = {}
        for r in cur.fetchall():
            for reason in json.loads(r["rejection_reasons_json"] or "[]"):
                by_reason[reason] = by_reason.get(reason, 0) + int(r["n"])

        return {
            "total": total,
            "accepted": accepted,
            "rejected": rejected,
            "duplicates": duplicates,
            "by_status": by_status,
            "by_source": by_source,
            "by_source_mode": by_mode,
            "by_source_scope": by_scope,
            "by_rejection_reason": by_reason,
        }
    finally:
        conn.close()


def iter_existing_gigs_for_dedup() -> list[Gig]:
    """Все гиги, нужные для сравнения похожести (без reject-гигов)."""
    return list_gigs(limit=100000, include_rejected=False)
