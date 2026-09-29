import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.storage.database import Database


class DatabaseMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-db-migration-"))
        self.path = self.tmp / "legacy.db3"

    async def asyncTearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_connect_adds_session_columns_before_session_indexes(self):
        connection = sqlite3.connect(self.path)
        connection.executescript("""
            CREATE TABLE projects (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              normalized_path TEXT NOT NULL UNIQUE,
              codex_thread_id TEXT,
              default_sandbox TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_active_at TEXT,
              archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE turns (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              thread_id TEXT,
              prompt_sha256 TEXT NOT NULL,
              sandbox TEXT NOT NULL,
              status TEXT NOT NULL,
              error TEXT,
              started_at TEXT,
              completed_at TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              project_seq INTEGER NOT NULL,
              project_id TEXT NOT NULL,
              thread_id TEXT,
              turn_id TEXT,
              kind TEXT NOT NULL,
              type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(project_id, project_seq)
            );
        """)
        connection.close()

        database = Database(self.path)
        await database.connect()
        try:
            event_columns = {
                row["name"]
                for row in await database.fetch_all("PRAGMA table_info(events)")
            }
            self.assertIn("session_id", event_columns)
            indexes = await database.fetch_all(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND name = 'idx_events_session'"
            )
            self.assertEqual(len(indexes), 1)
        finally:
            await database.close()
