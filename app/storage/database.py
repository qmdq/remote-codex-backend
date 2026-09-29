from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..auth.tokens import iso_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  last_seen_at TEXT,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS pairing_codes (
  id TEXT PRIMARY KEY,
  code_hash TEXT NOT NULL,
  device_name TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  consumed_at TEXT,
  approved_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  normalized_path TEXT NOT NULL UNIQUE,
  codex_thread_id TEXT,
  current_session_id TEXT,
  model TEXT,
  reasoning_effort TEXT,
  goal TEXT,
  default_sandbox TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_active_at TEXT,
  archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS codex_sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  codex_thread_id TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  title TEXT,
  model TEXT,
  reasoning_effort TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT,
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_codex_sessions_project
  ON codex_sessions(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS turns (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  session_id TEXT,
  thread_id TEXT,
  prompt_sha256 TEXT NOT NULL,
  sandbox TEXT NOT NULL,
  model TEXT,
  status TEXT NOT NULL,
  error TEXT,
  started_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  project_seq INTEGER NOT NULL,
  project_id TEXT NOT NULL,
  session_id TEXT,
  thread_id TEXT,
  turn_id TEXT,
  kind TEXT NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, project_seq)
);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, project_seq);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(project_id, thread_id, project_seq);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT,
  action TEXT NOT NULL,
  subject TEXT,
  result TEXT NOT NULL,
  detail_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class Database:
    """Small asyncio wrapper around SQLite's single-writer model."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(
            sqlite3.connect,
            str(self.path),
            timeout=10,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        async with self._lock:
            await asyncio.to_thread(self._conn.executescript, SCHEMA)
            await self._migrate()
            await asyncio.to_thread(self._conn.commit)

    async def _migrate(self) -> None:
        for table, column in (
            ("projects", "current_session_id"),
            ("projects", "model"),
            ("projects", "reasoning_effort"),
            ("projects", "goal"),
            ("turns", "session_id"),
            ("events", "session_id"),
            ("turns", "model"),
            ("codex_sessions", "deleted_at"),
        ):
            cursor = await asyncio.to_thread(self._conn.execute, f"PRAGMA table_info({table})")
            rows = await asyncio.to_thread(cursor.fetchall)
            columns = {row["name"] for row in rows}
            if column not in columns:
                await asyncio.to_thread(
                    self._conn.execute,
                    f"ALTER TABLE {table} ADD COLUMN {column} TEXT",
                )
        await asyncio.to_thread(
            self._conn.execute,
            "CREATE INDEX IF NOT EXISTS idx_events_session "
            "ON events(project_id, session_id, project_seq)",
        )
        await self._backfill_sessions()

    async def _backfill_sessions(self) -> None:
        """Associate legacy thread events with materialized conversations."""

        cursor = await asyncio.to_thread(
            self._conn.execute,
            """SELECT id, codex_thread_id, created_at FROM projects
               WHERE current_session_id IS NULL""",
        )
        projects = await asyncio.to_thread(cursor.fetchall)
        if not projects:
            return
        for project in projects:
            if not project["codex_thread_id"]:
                continue
            session_id = f"sess_{project['id'][4:]}_legacy"
            created_at = project["created_at"] or iso_now()
            await asyncio.to_thread(
                self._conn.execute,
                """INSERT OR IGNORE INTO codex_sessions(
                     id, project_id, codex_thread_id, status, title,
                     created_at, updated_at
                   ) VALUES (?, ?, ?, 'active', '导入会话', ?, ?)""",
                (
                    session_id,
                    project["id"],
                    project["codex_thread_id"],
                    created_at,
                    created_at,
                ),
            )
            await asyncio.to_thread(
                self._conn.execute,
                "UPDATE projects SET current_session_id = ? WHERE id = ?",
                (session_id, project["id"]),
            )
            thread_id = project["codex_thread_id"]
            if thread_id:
                await asyncio.to_thread(
                    self._conn.execute,
                    """UPDATE turns SET session_id = ?
                       WHERE project_id = ? AND thread_id = ? AND session_id IS NULL""",
                    (session_id, project["id"], thread_id),
                )
                await asyncio.to_thread(
                    self._conn.execute,
                    """UPDATE events SET session_id = ?
                       WHERE project_id = ? AND thread_id = ? AND session_id IS NULL""",
                    (session_id, project["id"], thread_id),
                )

    async def close(self) -> None:
        if self._conn is not None:
            async with self._lock:
                await asyncio.to_thread(self._conn.close)
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    async def execute(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> sqlite3.Cursor:
        async with self._lock:
            cursor = await asyncio.to_thread(self.conn.execute, sql, parameters)
            await asyncio.to_thread(self.conn.commit)
            return cursor

    async def executemany(
        self,
        sql: str,
        parameters: Iterable[Sequence[Any]],
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(self.conn.executemany, sql, parameters)
            await asyncio.to_thread(self.conn.commit)

    async def fetch_all(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> list[sqlite3.Row]:
        async with self._lock:
            cursor = await asyncio.to_thread(self.conn.execute, sql, parameters)
            return await asyncio.to_thread(cursor.fetchall)

    async def fetch_one(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> sqlite3.Row | None:
        rows = await self.fetch_all(sql, parameters)
        return rows[0] if rows else None

    async def transaction(self, operations) -> Any:
        """Run multiple statements while retaining the database lock."""

        async with self._lock:
            try:
                result = await operations(self.conn)
                await asyncio.to_thread(self.conn.commit)
                return result
            except Exception:
                await asyncio.to_thread(self.conn.rollback)
                raise
