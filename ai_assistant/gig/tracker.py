"""Submission tracker — журнал подачи и money-метрики (P6 §11–12).

ЗАЧЕМ
-----
Без журнала система не знает, что уже отправлено: один и тот же заказ предлагается повторно,
а понять, работает ли подход, невозможно. Это начало money-воронки:

    READY → SUBMITTED → CLIENT_REPLIED → INTERVIEW → AWARDED
                                      ↘ REJECTED / CLOSED

ЧТО ХРАНИТСЯ
------------
Ключевые моменты времени (обнаружен, проверен, отклик готов, отправлен) и число ставок в
каждый из них — чтобы потом измерить, насколько быстро росла конкуренция между решением и
отправкой, и не мешает ли это выигрывать.

МЕТРИКИ (§12)
-------------
``projects_seen``, ``live_verified``, ``apply_first``, ``proposals_ready``,
``proposals_submitted``, ``client_replies``, ``interviews``, ``awards``, ``revenue_usd``
и производные ``reply_rate``, ``award_rate``, ``revenue_per_10_bids``.
Именно они определяют, хороша ли система, — не количество тестов.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Допустимые статусы воронки. Порядок отражает движение к деньгам.
STATUSES: tuple[str, ...] = (
    "READY",
    "SUBMITTED",
    "CLIENT_REPLIED",
    "INTERVIEW",
    "AWARDED",
    "REJECTED",
    "CLOSED",
)

#: Статусы, означающие активное движение (используются в метриках).
REPLIED_STATUSES = frozenset({"CLIENT_REPLIED", "INTERVIEW", "AWARDED"})


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_tracker_path() -> Path:
    """Файл журнала рядом с базой проекта, чтобы не потерялся."""
    from .. import config

    try:
        base = Path(config.DB_FILE).parent
    except Exception:                           # noqa: BLE001
        base = Path(".")
    return base / "gig_submissions.sqlite"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    project_url TEXT,
    title TEXT NOT NULL,
    source TEXT,
    discovered_at TEXT,
    verified_at TEXT,
    bids_at_discovery INTEGER,
    bids_at_submission INTEGER,
    first_money_score REAL,
    capability_fit TEXT,
    client_risk TEXT,
    proposal_created_at TEXT,
    proposal_text TEXT,
    recommended_bid REAL,
    recommended_currency TEXT,
    actual_bid REAL,
    submitted_at TEXT,
    status TEXT NOT NULL DEFAULT 'READY',
    revenue_usd REAL,
    note TEXT,
    updated_at TEXT,
    api_data_expires_at TEXT,
    UNIQUE(source, project_id)
);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
"""


class _ClosingConnection(sqlite3.Connection):
    """Make `with connection` release the Windows file handle after commit/rollback."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class _DPAPIConnection(_ClosingConnection):
    """In-memory SQLite image whose only on-disk form is the DPAPI envelope."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dpapi_owner = None
        self._dpapi_lock = None
        self._persist_on_close = False
        self._dpapi_closed = False

    def close(self):
        if self._dpapi_closed:
            return
        owner, lock = self._dpapi_owner, self._dpapi_lock
        try:
            if owner is not None and self._persist_on_close:
                owner._write_snapshot(self.serialize())
        finally:
            self._dpapi_closed = True
            try:
                super().close()
            finally:
                if lock is not None:
                    lock.release()


@dataclass
class SubmissionRecord:
    """Одна запись журнала."""

    project_id: str
    title: str
    source: str = "freelancer"
    project_url: str | None = None
    discovered_at: str | None = None
    verified_at: str | None = None
    bids_at_discovery: int | None = None
    bids_live: int | None = None
    action_score: float | None = None
    recommended_delivery: str | None = None
    actual_delivery: str | None = None
    payment_date: str | None = None
    provider_bid_id: str | None = None
    provider_response_id: str | None = None
    proposal_hash: str | None = None
    project_description_hash: str | None = None
    bids_at_submission: int | None = None
    first_money_score: float | None = None
    capability_fit: str | None = None
    client_risk: str | None = None
    proposal_created_at: str | None = None
    proposal_text: str | None = None
    recommended_bid: float | None = None
    recommended_currency: str | None = None
    actual_bid: float | None = None
    submitted_at: str | None = None
    status: str = "READY"
    revenue_usd: float | None = None
    note: str | None = None
    updated_at: str | None = None
    api_data_expires_at: str | None = None
    row_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "row_id"}


class SubmissionTracker:
    """Минимальный журнал подачи поверх SQLite.

    Отдельная таблица, а не расширение ``gigs``: журнал живёт дольше конкретной записи о
    заказе и описывает наши действия, а не факты источника.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_tracker_path()
        self.encrypted_at_rest = False
        self.retention_seconds = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            existing = {r[1] for r in conn.execute("PRAGMA table_info(submissions)")}
            for name, kind in {"bids_live": "INTEGER", "action_score": "REAL",
                               "recommended_delivery": "TEXT", "actual_delivery": "TEXT", "payment_date": "TEXT",
                               "provider_bid_id": "TEXT", "provider_response_id": "TEXT",
                               "proposal_hash": "TEXT", "project_description_hash": "TEXT",
                               "api_data_expires_at": "TEXT"}.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE submissions ADD COLUMN {name} {kind}")
            conn.execute("CREATE TABLE IF NOT EXISTS funnel_events (source TEXT, project_id TEXT, status TEXT, at TEXT, UNIQUE(source, project_id, status))")
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ запись
    def upsert(self, record: SubmissionRecord) -> int:
        """Добавляет запись или обновляет существующую по (source, project_id)."""
        if record.status not in STATUSES:
            raise ValueError(f"unknown status: {record.status!r} (expected one of {STATUSES})")
        record.updated_at = _utcnow()
        payload = record.to_dict()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO submissions (
                    project_id, project_url, title, source, discovered_at, verified_at,
                    bids_at_discovery, bids_at_submission, first_money_score,
                    capability_fit, client_risk, proposal_created_at, proposal_text,
                    recommended_bid, recommended_currency, actual_bid, submitted_at,
                    status, revenue_usd, note, updated_at, api_data_expires_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source, project_id) DO UPDATE SET
                    project_url=excluded.project_url,
                    title=excluded.title,
                    discovered_at=COALESCE(submissions.discovered_at, excluded.discovered_at),
                    verified_at=COALESCE(excluded.verified_at, submissions.verified_at),
                    bids_at_discovery=COALESCE(submissions.bids_at_discovery, excluded.bids_at_discovery),
                    bids_at_submission=COALESCE(excluded.bids_at_submission, submissions.bids_at_submission),
                    first_money_score=COALESCE(excluded.first_money_score, submissions.first_money_score),
                    capability_fit=COALESCE(excluded.capability_fit, submissions.capability_fit),
                    client_risk=COALESCE(excluded.client_risk, submissions.client_risk),
                    proposal_created_at=COALESCE(excluded.proposal_created_at, submissions.proposal_created_at),
                    proposal_text=COALESCE(excluded.proposal_text, submissions.proposal_text),
                    recommended_bid=COALESCE(excluded.recommended_bid, submissions.recommended_bid),
                    recommended_currency=COALESCE(excluded.recommended_currency, submissions.recommended_currency),
                    actual_bid=COALESCE(excluded.actual_bid, submissions.actual_bid),
                    submitted_at=COALESCE(excluded.submitted_at, submissions.submitted_at),
                    status=CASE WHEN submissions.status != 'READY' AND excluded.status = 'READY'
                                THEN submissions.status ELSE excluded.status END,
                    revenue_usd=COALESCE(excluded.revenue_usd, submissions.revenue_usd),
                    note=COALESCE(excluded.note, submissions.note),
                    updated_at=excluded.updated_at,
                    api_data_expires_at=COALESCE(excluded.api_data_expires_at, submissions.api_data_expires_at)
                """,
                (
                    payload["project_id"], payload["project_url"], payload["title"],
                    payload["source"], payload["discovered_at"], payload["verified_at"],
                    payload["bids_at_discovery"], payload["bids_at_submission"],
                    payload["first_money_score"], payload["capability_fit"],
                    payload["client_risk"], payload["proposal_created_at"],
                    payload["proposal_text"], payload["recommended_bid"],
                    payload["recommended_currency"], payload["actual_bid"],
                    payload["submitted_at"], payload["status"], payload["revenue_usd"],
                    payload["note"], payload["updated_at"],
                    payload["api_data_expires_at"],
                ),
            )
            conn.execute("UPDATE submissions SET bids_live=COALESCE(?, bids_live), action_score=COALESCE(?, action_score), recommended_delivery=COALESCE(?, recommended_delivery), actual_delivery=COALESCE(?, actual_delivery) WHERE source=? AND project_id=?",
                         (record.bids_live, record.action_score, record.recommended_delivery,
                          record.actual_delivery, record.source, record.project_id))
            conn.commit()
            row = conn.execute(
                "SELECT id FROM submissions WHERE source=? AND project_id=?",
                (payload["source"], payload["project_id"]),
            ).fetchone()
        return int(row["id"]) if row else 0

    def mark_submitted(
        self,
        project_id: str,
        *,
        actual_bid: float | None = None,
        bids_at_submission: int | None = None,
        actual_delivery: str | None = None,
        source: str = "freelancer",
    ) -> bool:
        """Отмечает отправку. Возвращает False, если записи нет."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE submissions
                   SET status='SUBMITTED',
                       submitted_at=?,
                       actual_bid=COALESCE(?, actual_bid),
                       bids_at_submission=COALESCE(?, bids_at_submission),
                       updated_at=?
                 WHERE source=? AND project_id=?
                """,
                (_utcnow(), actual_bid, bids_at_submission, _utcnow(), source, project_id),
            )
            if cur.rowcount:
                conn.execute("UPDATE submissions SET actual_delivery=COALESCE(?, actual_delivery) WHERE source=? AND project_id=?", (actual_delivery, source, project_id))
                conn.execute("INSERT OR IGNORE INTO funnel_events VALUES (?,?,?,?)", (source, project_id, "SUBMITTED", _utcnow()))
            conn.commit()
            return cur.rowcount > 0

    def set_status(
        self,
        project_id: str,
        status: str,
        *,
        revenue_usd: float | None = None,
        note: str | None = None,
        source: str = "freelancer",
    ) -> bool:
        """Переводит запись в новый статус воронки."""
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status!r}")
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE submissions
                   SET status=?, revenue_usd=COALESCE(?, revenue_usd),
                       note=COALESCE(?, note), updated_at=?
                 WHERE source=? AND project_id=?
                """,
                (status, revenue_usd, note, _utcnow(), source, project_id),
            )
            if cur.rowcount:
                conn.execute("INSERT OR IGNORE INTO funnel_events VALUES (?,?,?,?)", (source, project_id, status, _utcnow()))
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ чтение
    def mark_paid(self, project_id: str, *, revenue_usd: float, payment_date: str,
                  source: str = "freelancer") -> bool:
        """Record total actually received USD, idempotently; never infer an award."""
        import math
        if isinstance(revenue_usd, bool) or not math.isfinite(float(revenue_usd)) or revenue_usd <= 0:
            raise ValueError("received revenue must be positive and finite")
        datetime.fromisoformat(payment_date.replace("Z", "+00:00"))
        with self._connect() as conn:
            cur=conn.execute("UPDATE submissions SET revenue_usd=?, payment_date=?, updated_at=? WHERE source=? AND project_id=? AND submitted_at IS NOT NULL",
                             (revenue_usd,payment_date,_utcnow(),source,project_id))
            conn.commit()
            return cur.rowcount>0

    def list(self, status: str | None = None, limit: int = 200) -> list[SubmissionRecord]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM submissions WHERE status=? ORDER BY first_money_score DESC "
                    "LIMIT ?", (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM submissions ORDER BY first_money_score DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get(self, project_id: str, source: str = "freelancer") -> SubmissionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM submissions WHERE source=? AND project_id=?",
                (source, project_id),
            ).fetchone()
        return _row_to_record(row) if row else None

    def already_tracked(self, project_id: str, source: str = "freelancer") -> bool:
        """Отправляли ли уже по этому проекту — защита от повторного отклика."""
        rec = self.get(project_id, source)
        return bool(rec and rec.status in ("SUBMITTED", "CLIENT_REPLIED", "INTERVIEW",
                                           "AWARDED", "REJECTED", "CLOSED"))

    # ----------------------------------------------------------------- метрики
    def metrics(self, *, projects_seen: int = 0, projects_live_verified: int = 0,
                apply_first_count: int = 0) -> dict[str, Any]:
        """Метрики money-воронки (ТЗ §12)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM submissions").fetchall()
            events = conn.execute("SELECT source, project_id, status FROM funnel_events").fetchall()
        records = [_row_to_record(r) for r in rows]

        submitted = [r for r in records if r.submitted_at]
        event_keys = {(r["source"], r["project_id"], r["status"]) for r in events}
        replied = [r for r in submitted if r.status == "CLIENT_REPLIED" or (r.source, r.project_id, "CLIENT_REPLIED") in event_keys]
        interviews = [r for r in submitted if r.status == "INTERVIEW" or (r.source, r.project_id, "INTERVIEW") in event_keys]
        awards = [r for r in submitted if r.status == "AWARDED" or (r.source, r.project_id, "AWARDED") in event_keys]
        revenue = sum(float(r.revenue_usd or 0.0) for r in records)

        metrics: dict[str, Any] = {
            "projects_seen": projects_seen,
            "projects_live_verified": projects_live_verified,
            "apply_first_count": apply_first_count,
            "proposals_ready": len([r for r in records if r.proposal_text]),
            "proposals_submitted": len(submitted),
            "client_replies": len(replied),
            "interviews": len(interviews),
            "awards": len(awards),
            "revenue_usd": round(revenue, 2),
        }
        metrics["reply_rate"] = (
            round(len(replied) / len(submitted), 3) if submitted else None
        )
        metrics["award_rate"] = (
            round(len(awards) / len(submitted), 3) if submitted else None
        )
        metrics["interview_rate"] = round(len(interviews) / len(submitted), 3) if submitted else None
        metrics["revenue_per_10_bids"] = (
            round(revenue / len(submitted) * 10, 2) if submitted else None
        )
        return metrics

    def summary_line(self) -> str:
        m = self.metrics()
        return (
            f"ready={m['proposals_ready']} submitted={m['proposals_submitted']} "
            f"replies={m['client_replies']} interviews={m['interviews']} "
            f"awards={m['awards']} revenue=${m['revenue_usd']}"
        )


_TRACKER_THREAD_LOCKS: dict[str, threading.RLock] = {}
_TRACKER_THREAD_LOCKS_GUARD = threading.Lock()


class _TrackerFileLock:
    """Serialize encrypted database snapshots across threads and processes."""
    def __init__(self, path: Path):
        self.path = path
        with _TRACKER_THREAD_LOCKS_GUARD:
            self.thread_lock = _TRACKER_THREAD_LOCKS.setdefault(str(path.resolve()), threading.RLock())
        self.thread_lock.acquire()
        self.handle = None
        try:
            lock_path = path.with_suffix(path.suffix + '.lock')
            self.handle = lock_path.open('a+b')
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write(b'0'); self.handle.flush()
                self.handle.seek(0)
                while True:
                    try:
                        msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            if self.handle:
                self.handle.close()
            self.thread_lock.release()
            raise

    def release(self):
        try:
            if self.handle:
                if os.name == 'nt':
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                self.handle.close()
        finally:
            self.thread_lock.release()


class EncryptedSubmissionTracker(SubmissionTracker):
    """DPAPI-protected P8 journal with a bounded, enforced data lifetime.

    The database exists only in memory while a connection is open. Existing
    plaintext SQLite files are rejected and never migrated or overwritten.
    """
    encrypted_at_rest = True

    def __init__(self, path: str | Path | None = None, *, retention_seconds=None):
        if path is None:
            base = default_tracker_path()
            path = base.with_name('gig_submissions.dpapi.sqlite')
        self._schema_ready = False
        self.retention_seconds = retention_seconds
        super().__init__(path)
        self.encrypted_at_rest = True
        self.retention_seconds = retention_seconds
        if retention_seconds is not None:
            from .provider_policy import retention_seconds as valid_retention_seconds
            if valid_retention_seconds({'api_data_retention_seconds': retention_seconds}) is None:
                raise ValueError('P8 tracker retention must be positive and no longer than 24 hours')
        self._schema_ready = True
        self.purge_expired()

    def _write_snapshot(self, raw: bytes):
        from .submission.secure_store import save_bytes
        save_bytes(self.path, raw)

    def _connect(self) -> sqlite3.Connection:
        from .submission.secure_store import load_bytes
        lock = _TrackerFileLock(self.path)
        conn = None
        try:
            conn = sqlite3.connect(':memory:', factory=_DPAPIConnection)
            conn._dpapi_owner = self
            conn._dpapi_lock = lock
            if self.path.exists():
                conn.deserialize(load_bytes(self.path))
            conn._persist_on_close = True
            conn.row_factory = sqlite3.Row
            if self._schema_ready:
                self._purge_expired_on_connection(conn)
            return conn
        except Exception:
            if conn is not None:
                conn._persist_on_close = False
                conn.close()
            else:
                lock.release()
            raise

    def _purge_expired_on_connection(self, conn):
        now = datetime.now(timezone.utc).isoformat()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'submissions' in tables:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(submissions)')}
            if 'api_data_expires_at' in columns:
                conn.execute('DELETE FROM submissions WHERE api_data_expires_at IS NOT NULL AND api_data_expires_at<=?', (now,))
                if 'funnel_events' in tables:
                    conn.execute('''DELETE FROM funnel_events WHERE NOT EXISTS (
                        SELECT 1 FROM submissions s WHERE s.source=funnel_events.source
                        AND s.project_id=funnel_events.project_id)''')
        if 'api_bid_outbox' in tables:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(api_bid_outbox)')}
            if 'expires_at' in columns:
                conn.execute('DELETE FROM api_bid_outbox WHERE expires_at IS NOT NULL AND expires_at<=?', (now,))
        if 'api_monitor_events' in tables:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(api_monitor_events)')}
            if 'expires_at' in columns:
                conn.execute('DELETE FROM api_monitor_events WHERE expires_at IS NOT NULL AND expires_at<=?', (now,))

    def purge_expired(self):
        with self._connect() as conn:
            self._purge_expired_on_connection(conn)

    def retention_expires_at(self):
        if self.retention_seconds is None:
            return None
        return (datetime.now(timezone.utc) + timedelta(seconds=self.retention_seconds)).isoformat()

    def upsert(self, record: SubmissionRecord) -> int:
        if self.retention_seconds is not None:
            expiry = datetime.now(timezone.utc) + timedelta(seconds=self.retention_seconds)
            record.api_data_expires_at = expiry.isoformat()
        return super().upsert(record)


def _row_to_record(row: sqlite3.Row) -> SubmissionRecord:
    return SubmissionRecord(
        row_id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        source=row["source"] or "freelancer",
        project_url=row["project_url"],
        discovered_at=row["discovered_at"],
        verified_at=row["verified_at"],
        bids_at_discovery=row["bids_at_discovery"],
        bids_live=row["bids_live"], action_score=row["action_score"],
        recommended_delivery=row["recommended_delivery"], actual_delivery=row["actual_delivery"],
        payment_date=row["payment_date"],
        provider_bid_id=row["provider_bid_id"], provider_response_id=row["provider_response_id"],
        proposal_hash=row["proposal_hash"], project_description_hash=row["project_description_hash"],
        bids_at_submission=row["bids_at_submission"],
        first_money_score=row["first_money_score"],
        capability_fit=row["capability_fit"],
        client_risk=row["client_risk"],
        proposal_created_at=row["proposal_created_at"],
        proposal_text=row["proposal_text"],
        recommended_bid=row["recommended_bid"],
        recommended_currency=row["recommended_currency"],
        actual_bid=row["actual_bid"],
        submitted_at=row["submitted_at"],
        status=row["status"],
        revenue_usd=row["revenue_usd"],
        note=row["note"],
        updated_at=row["updated_at"],
        api_data_expires_at=row["api_data_expires_at"],
    )


__all__ = [
    "REPLIED_STATUSES",
    "STATUSES",
    "SubmissionRecord",
    "EncryptedSubmissionTracker",
    "SubmissionTracker",
    "default_tracker_path",
]
