from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..auth.tokens import iso_now, utcnow
from ..storage.database import Database


@dataclass(slots=True)
class EventRecord:
    seq: int
    project_seq: int
    project_id: str
    session_id: str | None
    thread_id: str | None
    turn_id: str | None
    kind: str
    type: str
    payload: dict[str, Any]
    created_at: str

    def wire(self, *, history: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "v": 1,
            "type": self.kind,
            "seq": self.project_seq,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "ts": self.created_at,
        }
        if history:
            result["history"] = True
        if self.kind == "codex.event":
            result["event"] = self.payload
        else:
            result["payload"] = self.payload
        return result


class EventStore:
    def __init__(self, database: Database):
        self.db = database
        self._project_locks: dict[str, asyncio.Lock] = {}

    def _lock(self, project_id: str) -> asyncio.Lock:
        return self._project_locks.setdefault(project_id, asyncio.Lock())

    async def append(
        self,
        project_id: str,
        kind: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        thread_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> EventRecord:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        created_at = iso_now()
        async with self._lock(project_id):
            row = await self.db.fetch_one(
                """SELECT COALESCE(MAX(project_seq), 0) AS value
                   FROM events WHERE project_id = ?""",
                (project_id,),
            )
            next_seq = int(row["value"]) + 1
            cursor = await self.db.execute(
                """INSERT INTO events(
                     project_seq, project_id, session_id, thread_id, turn_id,
                     kind, type, payload_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    next_seq,
                    project_id,
                    session_id,
                    thread_id,
                    turn_id,
                    kind,
                    event_type,
                    encoded,
                    created_at,
                ),
            )
            return EventRecord(
                seq=int(cursor.lastrowid),
                project_seq=next_seq,
                project_id=project_id,
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
                kind=kind,
                type=event_type,
                payload=payload,
                created_at=created_at,
            )

    async def replay(
        self,
        project_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
        thread_id: str | None = None,
        session_id: str | None = None,
    ) -> list[EventRecord]:
        predicate = ""
        values: list[Any] = [project_id, after_seq]
        if thread_id:
            predicate = " AND thread_id = ?"
            values.append(thread_id)
        if session_id:
            predicate += " AND session_id = ?"
            values.append(session_id)
        rows = await self.db.fetch_all(
            f"""SELECT * FROM events
               WHERE project_id = ? AND project_seq > ?{predicate}
               ORDER BY project_seq LIMIT ?""",
            (*values, min(limit, 1000)),
        )
        return [
            EventRecord(
                seq=row["seq"],
                project_seq=row["project_seq"],
                project_id=row["project_id"],
                session_id=row["session_id"],
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                kind=row["kind"],
                type=row["type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def latest_seq(self, project_id: str) -> int:
        row = await self.db.fetch_one(
            """SELECT COALESCE(MAX(project_seq), 0) AS value
               FROM events WHERE project_id = ?""",
            (project_id,),
        )
        return int(row["value"]) if row else 0

    async def cleanup(self, retention_days: int) -> int:
        threshold = (utcnow() - timedelta(days=retention_days)).isoformat()
        cursor = await self.db.execute(
            "DELETE FROM events WHERE created_at < ?", (threshold,)
        )
        return cursor.rowcount
