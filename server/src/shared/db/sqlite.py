"""SQLite connection helper + migration runner.

Everything agent-owned lives in one SQLite file at app_config.DB_PATH:
wallets, strategies, allocations, evaluations, trades, positions — plus
APScheduler's own tables (created separately by the scheduler service).

Connection rules:
- PRAGMA foreign_keys = ON
- PRAGMA journal_mode = WAL (better concurrency for the scheduler + HTTP)
- Row factory = sqlite3.Row (dict-style access)

Migrations:
- SQL files live in migrations/ named NNN_<description>.sql, applied in
  lexicographic order.
- Applied migrations are recorded in a `_migrations` table.
- `init_db()` is idempotent: calling twice is a no-op.
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

from src.config import app_config
from src.shared.logging import get_logger

_log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _db_path() -> str:
    return str(app_config.DB_PATH)


@lru_cache(maxsize=1)
def get_connection() -> sqlite3.Connection:
    """Return the singleton SQLite connection.

    Opened with WAL + foreign keys on. Test configs using DB_PATH=":memory:"
    get an in-memory database (single connection, single-session scope).
    """
    path = _db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False so APScheduler threads can read/write.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL not supported on :memory: — skip the pragma there.
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def reset_connection() -> None:
    """Close + clear the cached connection. Used by tests to flip DB_PATH."""
    try:
        get_connection().close()
    except sqlite3.Error as exc:
        _log.warning("db.connection.close_failed", error=str(exc))
    get_connection.cache_clear()


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            filename TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def _applied_migrations(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT filename FROM _migrations").fetchall()
    return {r["filename"] for r in rows}


def _available_migrations() -> list[Path]:
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def init_db() -> list[str]:
    """Run any unapplied migrations. Returns the list of filenames applied this call."""
    conn = get_connection()
    _ensure_migrations_table(conn)
    applied = _applied_migrations(conn)
    pending = [p for p in _available_migrations() if p.name not in applied]

    newly_applied: list[str] = []
    for path in pending:
        sql = path.read_text()
        conn.executescript(sql)
        conn.execute("INSERT INTO _migrations (filename) VALUES (?)", (path.name,))
        conn.commit()
        newly_applied.append(path.name)

    _log.info(
        "db.migrated",
        applied_now=newly_applied,
        already_applied=sorted(applied),
        db_path=_db_path(),
    )
    return newly_applied
