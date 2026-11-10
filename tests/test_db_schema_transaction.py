"""Регрессия на границу транзакции в db._ensure_schema_on.

Повод (замер 2026-09-24): apply_schema — это 90 DDL-операторов, и каждый
фиксировался своей транзакцией, потому что sqlite3 не открывает транзакцию
под DDL. На свежую базу это 86–88 созданий и удалений <база>-journal и 2,6–3,3 с
вместо 0,06–0,13 с.

Что здесь зафиксировано:
  * схема, которую даёт новый путь, совпадает со схемой прежнего пути;
  * повторная инициализация ничего не делает;
  * падение в середине DDL не оставляет ни части схемы, ни отметки о ней;
  * после падения инициализация доводится до конца и даёт полную схему;
  * миграционные ALTER TABLE по-прежнему доезжают;
  * в apply_schema нет PRAGMA и executescript — конструкций, которые
    управляют транзакцией сами и сломали бы внешний BEGIN.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Self

import pytest

from ai_assistant import config, db
from ai_assistant.db_schema import schema_fingerprint

# Число DDL-операторов в apply_schema. Растёт только вместе с DDL, и тогда
# растёт заодно и порог journal-циклов ниже.
DDL_COUNT = 92

# Отпечаток исходного текста apply_schema. Правка DDL меняет его — это и есть
# смысл отпечатка, поэтому при осознанном изменении схемы пин обновляют руками.
# Здесь он доказывает, что правка границы транзакции DDL не тронула.
# c00fdd4156b0a7e4 — добавление таблиц gigs/gig_events, followups, tracking, alerts.
# e930e9a0e0c4f123 — добавление таблицы gig_events и индекса idx_gig_events_gig_id.
FINGERPRINT_PIN = "e930e9a0e0c4f123"


class _Injected(Exception):
    """Сбой, вброшенный в середину apply_schema."""


class _FailingProxy:
    """Прокси соединения: N-й DDL-оператор падает.

    apply_schema трогает у соединения только .cursor() и .commit(),
    поэтому прокси достаточно перехватить cursor() и пробросить остальное.
    """

    def __init__(self, conn: sqlite3.Connection, fail_at: int | None) -> None:
        self._conn = conn
        self._fail_at = fail_at
        self.n_ddl = 0

    def cursor(self) -> sqlite3.Cursor:
        return _FailingCursor(self._conn.cursor(), self)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class _FailingCursor:
    def __init__(self, cur: sqlite3.Cursor, owner: _FailingProxy) -> None:
        self._cur = cur
        self._owner = owner

    def execute(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith(("CREATE", "ALTER", "DROP")):
            self._owner.n_ddl += 1
            if self._owner._fail_at is not None and self._owner.n_ddl == self._owner._fail_at:
                raise _Injected(f"сбой на {self._owner.n_ddl}-м DDL")
        return self._cur.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._cur, name)


class JournalWatcher:
    """Считает появления <база>-journal, опрашивая каталог.

    Счётчик может недосчитать (журнал живёт доли миллисекунды на быстром
    диске), но не может пересчитать — поэтому верхняя граница ниже надёжна,
    а нижнюю здесь никто не проверяет.
    """

    def __init__(self, db_path: pathlib.Path) -> None:
        self.journal = pathlib.Path(str(db_path) + "-journal")
        self.created = 0
        self._stop = threading.Event()
        self._alive = False
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            exists = self.journal.exists()
            if exists and not self._alive:
                self.created += 1
                self._alive = True
            elif not exists and self._alive:
                self._alive = False
            time.sleep(0.001)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


@contextmanager
def _fail_apply_at(fail_at: int):
    """Ломает apply_schema на N-м DDL, сохраняя настоящую реализацию.

    Без monkeypatch: части тестов нужно снять подмену внутри себя, а
    monkeypatch.undo() снял бы заодно изоляцию путей из conftest — и тест
    ушёл бы писать в боевую базу.
    """
    real_apply = db.apply_schema
    seen: dict = {}

    def wrapper(conn: sqlite3.Connection) -> None:
        proxy = _FailingProxy(conn, fail_at)
        try:
            real_apply(proxy)
        finally:
            seen["n_ddl"] = proxy.n_ddl

    db.apply_schema = wrapper  # type: ignore[assignment]
    try:
        yield seen
    finally:
        db.apply_schema = real_apply  # type: ignore[assignment]


def _legacy_ensure_schema_on(conn: sqlite3.Connection) -> None:
    """Прежняя реализация — оракул для проверки эквивалентности схемы.

    Отметка о схеме писалась после DDL, отдельным commit().
    """
    from datetime import datetime, timezone

    from ai_assistant.db_schema import apply_schema

    fingerprint = schema_fingerprint()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _schema_meta (id INTEGER PRIMARY KEY,"
        " fingerprint TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT fingerprint FROM _schema_meta WHERE id = 1").fetchone()
    if row is not None and row[0] == fingerprint:
        return
    apply_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO _schema_meta (id, fingerprint, applied_at) VALUES (1, ?, ?)",
        (fingerprint, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _dump(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """Слепок схемы, которую создаёт apply_schema.

    `_schema_meta` исключается намеренно: её создаёт db.py, а не apply_schema,
    и в sqlite_master лежит ИСХОДНЫЙ текст оператора — сравнение тогда
    зависело бы от форматирования строкового литерала, а не от схемы.
    Что таблица есть и заполнена правильно, проверяют отдельные тесты.
    """
    rows = conn.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master"
        " WHERE name NOT LIKE 'sqlite_%' AND name != '_schema_meta' ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], r[2], " ".join(r[3].split())) for r in rows]


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


# --- 1. Создание базы с нуля --------------------------------------------


def test_ensure_schema_creates_full_schema_from_scratch():
    db.ensure_schema()
    conn = sqlite3.connect(config.DB_FILE)
    try:
        tables = _tables(conn)
        assert len(tables) >= 25
        for expected in (
            "vacancies",
            "processed_vacancies",
            "application_queue",
            "application_tracking",
            "application_submissions",
            "vacancy_eligibility",
            "autonomous_cycle_runs",
            "_schema_meta",
        ):
            assert expected in tables, f"нет таблицы {expected}"
        row = conn.execute("SELECT fingerprint FROM _schema_meta WHERE id = 1").fetchone()
        assert row is not None
        assert row[0] == schema_fingerprint()
    finally:
        conn.close()


def test_schema_matches_the_legacy_path(tmp_path):
    """Итоговая схема нового пути совпадает со схемой прежнего пути."""
    old_file = tmp_path / "old.db"
    new_file = tmp_path / "new.db"

    conn = sqlite3.connect(str(old_file))
    _legacy_ensure_schema_on(conn)
    old_dump = _dump(conn)
    conn.close()

    conn = sqlite3.connect(str(new_file))
    db._ensure_schema_on(conn)
    new_dump = _dump(conn)
    conn.close()

    assert new_dump == old_dump
    assert len(new_dump) == len(old_dump) >= 25


# --- 2. Повторная инициализация -----------------------------------------


def test_second_init_is_a_noop():
    db.ensure_schema()
    conn = sqlite3.connect(config.DB_FILE)
    before = conn.execute("SELECT fingerprint, applied_at FROM _schema_meta WHERE id = 1").fetchone()
    before_dump = _dump(conn)
    conn.close()

    db.ensure_schema()

    conn = sqlite3.connect(config.DB_FILE)
    try:
        after = conn.execute("SELECT fingerprint, applied_at FROM _schema_meta WHERE id = 1").fetchone()
        assert after == before, "повторная инициализация переписала отметку о схеме"
        assert _dump(conn) == before_dump
    finally:
        conn.close()


def test_second_init_writes_nothing(tmp_path, monkeypatch):
    """На готовой базе журнал не создаётся вообще: писать нечего."""
    target = tmp_path / "repeat.db"
    monkeypatch.setattr(config, "DB_FILE", str(target))

    db.ensure_schema()
    with JournalWatcher(target) as watcher:
        db.ensure_schema()
    assert watcher.created == 0


def test_stale_fingerprint_reapplies_schema():
    db.ensure_schema()
    conn = sqlite3.connect(config.DB_FILE)
    conn.execute("UPDATE _schema_meta SET fingerprint = 'ustarevshiy' WHERE id = 1")
    conn.commit()
    conn.close()

    db.ensure_schema()

    conn = sqlite3.connect(config.DB_FILE)
    try:
        row = conn.execute("SELECT fingerprint FROM _schema_meta WHERE id = 1").fetchone()
        assert row is not None and row[0] == schema_fingerprint()
    finally:
        conn.close()


# --- 3. Ошибка на промежуточном DDL -------------------------------------


def test_failure_mid_ddl_leaves_no_partial_schema():
    with _fail_apply_at(50) as seen, pytest.raises(_Injected):
        db.ensure_schema()

    assert seen["n_ddl"] == 50, "сбой должен случиться именно на 50-м DDL"

    conn = sqlite3.connect(config.DB_FILE)
    try:
        assert _tables(conn) == {"_schema_meta"}, "осталась часть схемы"
        indexes = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        assert indexes == 0
        assert conn.execute("SELECT count(*) FROM _schema_meta").fetchone()[0] == 0
    finally:
        conn.close()


def test_failed_init_leaves_no_write_lock():
    """После сбоя база не заперта: другая связь пишет и читает."""
    with _fail_apply_at(50), pytest.raises(_Injected):
        db.ensure_schema()

    conn = sqlite3.connect(config.DB_FILE, timeout=2.0)
    try:
        conn.execute("CREATE TABLE probe(a)")
        conn.commit()
        assert conn.execute("SELECT count(*) FROM probe").fetchone()[0] == 0
    finally:
        conn.close()


def test_failure_before_any_ddl_leaves_empty_db():
    with _fail_apply_at(1), pytest.raises(_Injected):
        db.ensure_schema()

    conn = sqlite3.connect(config.DB_FILE)
    try:
        assert _tables(conn) == {"_schema_meta"}
    finally:
        conn.close()


# --- 4. Атомарность и повторный запуск после ошибки ---------------------


def test_recovery_after_failed_init_gives_full_schema(tmp_path):
    reference = tmp_path / "reference.db"
    conn = sqlite3.connect(str(reference))
    _legacy_ensure_schema_on(conn)
    expected = _dump(conn)
    conn.close()

    with _fail_apply_at(50), pytest.raises(_Injected):
        db.ensure_schema()

    db.ensure_schema()

    conn = sqlite3.connect(config.DB_FILE)
    try:
        assert _dump(conn) == expected
        row = conn.execute("SELECT fingerprint FROM _schema_meta WHERE id = 1").fetchone()
        assert row is not None and row[0] == schema_fingerprint()
    finally:
        conn.close()


def test_ddl_is_one_transaction_not_one_per_statement(tmp_path, monkeypatch):
    """Журнал создаётся единицы раз, а не по разу на каждый DDL."""
    target = tmp_path / "one_txn.db"
    monkeypatch.setattr(config, "DB_FILE", str(target))

    with JournalWatcher(target) as watcher:
        db.ensure_schema()

    assert watcher.created <= 2, (
        f"journal создан {watcher.created} раз: DDL снова идёт по транзакции на оператор"
    )
    assert watcher.created < DDL_COUNT // 10


# --- 5. Миграции ---------------------------------------------------------


def test_migration_columns_are_added_to_old_table():
    """ALTER TABLE из apply_schema доезжает и внутри общей транзакции."""
    conn = sqlite3.connect(config.DB_FILE)
    conn.execute(
        "CREATE TABLE application_queue ("
        " vacancy_stable_id TEXT PRIMARY KEY, priority_score INTEGER, rank INTEGER,"
        " queue_json TEXT, generated_at TEXT, queue_version TEXT)"
    )
    conn.commit()
    conn.close()

    db.ensure_schema()

    conn = sqlite3.connect(config.DB_FILE)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(application_queue)")}
        assert "canonical_id" in columns
        assert "representative_vacancy_stable_id" in columns
    finally:
        conn.close()


# --- 6. Границы применимости правки -------------------------------------


def test_apply_schema_has_no_transaction_managing_constructs():
    """Внешний BEGIN допустим только потому, что внутри нет PRAGMA/executescript."""
    import inspect

    from ai_assistant.db_schema import apply_schema

    src = inspect.getsource(apply_schema)
    assert "executescript" not in src
    assert "PRAGMA" not in src
    assert "commit()" in src, "apply_schema обязан сам фиксировать DDL"


def test_fingerprint_pin():
    """Отпечаток DDL не изменился: правка транзакции схему не тронула."""
    assert schema_fingerprint() == FINGERPRINT_PIN


def test_schema_dump_matches_stored_reference():
    """Слепок схемы, снятый до правки, совпадает с текущим."""
    reference = pathlib.Path(__file__).with_name("db_schema_reference.json")
    if not reference.exists():
        pytest.skip("слепок схемы не сохранён")
    expected = json.loads(reference.read_text(encoding="utf-8"))

    db.ensure_schema()
    conn = sqlite3.connect(config.DB_FILE)
    try:
        actual = [list(row) for row in _dump(conn)]
    finally:
        conn.close()
    assert actual == expected
