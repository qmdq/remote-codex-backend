from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Any

from ..protocol.errors import (
    PathNotAllowedError,
    ProjectExistsError,
    ProjectNotFoundError,
)
from ..auth.tokens import iso_now
from ..auth.tokens import new_id
from ..storage.database import Database
from .paths import normalize_path


class ProjectService:
    def __init__(self, database: Database, allowed_roots: list[Path]):
        self.db = database
        self.allowed_roots = [normalize_path(path) for path in allowed_roots]

    def _validate_path(self, raw_path: str) -> Path:
        try:
            path = normalize_path(raw_path)
        except (OSError, ValueError) as exc:
            raise PathNotAllowedError("project path is not accessible") from exc
        if not path.exists() or not path.is_dir():
            raise PathNotAllowedError("project path must be an existing directory")
        if not self._is_allowed(path):
            raise PathNotAllowedError("project path is outside allowed roots")
        return path

    def _is_allowed(self, path: Path) -> bool:
        for root in self.allowed_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    async def _find_by_path(self, path: Path) -> dict[str, Any] | None:
        existing = await self.db.fetch_one(
            "SELECT * FROM projects WHERE normalized_path = ?",
            (str(path),),
        )
        return dict(existing) if existing else None

    async def _insert(self, name: str, path: Path) -> dict[str, Any]:
        project_id = f"prj_{secrets.token_hex(8)}"
        now = iso_now()
        await self.db.execute(
            """INSERT INTO projects(
                 id, name, normalized_path, current_session_id,
                 model, default_sandbox, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                name.strip() or path.name,
                str(path),
                None,
                None,
                "workspace_write",
                now,
            ),
        )
        return await self.get(project_id)

    async def create(self, name: str, raw_path: str) -> dict:
        path = self._validate_path(raw_path)
        existing = await self._find_by_path(path)
        if existing:
            raise ProjectExistsError("directory is already registered")
        return await self._insert(name, path)

    async def delete(self, project_id: str) -> None:
        """Remove a project from the remote registry without touching its files."""

        await self.get(project_id)
        await self.db.execute(
            "UPDATE projects SET archived = 1, current_session_id = NULL, codex_thread_id = NULL WHERE id = ?",
            (project_id,),
        )
    async def import_codex_project(self, raw_path: str, name: str = "") -> tuple[dict, bool]:
        """Register a discovered Codex directory, returning existing rows idempotently."""

        path = self._validate_path(raw_path)
        existing = await self._find_by_path(path)
        if existing:
            if existing.get("archived"):
                await self.db.execute(
                    "UPDATE projects SET archived = 0 WHERE id = ?",
                    (existing["id"],),
                )
                existing = await self.get(str(existing["id"]))
            return existing, False
        return await self._insert(name, path), True

    async def import_codex_sessions(
        self,
        project_id: str,
        records: list[Any],
    ) -> int:
        """Import local Codex rollouts as archived conversations.

        A newly registered project keeps its empty current conversation. Existing
        Codex rollouts are history: selecting one restores it into the active list.
        """

        await self.get(project_id)
        rows = await self.db.fetch_all(
            """SELECT id, codex_thread_id, title, status
               FROM codex_sessions WHERE project_id = ?""",
            (project_id,),
        )
        by_thread = {
            str(row["codex_thread_id"] or ""): dict(row)
            for row in rows
            if row["codex_thread_id"]
        }
        inserted = 0
        for record in records:
            thread_id = str(getattr(record, "thread_id", "") or "")
            if not thread_id:
                continue
            updated_at = str(getattr(record, "updated_at", "") or iso_now())
            title = str(getattr(record, "title", "") or "").strip()
            model = str(getattr(record, "model", "") or "") or None
            reasoning = str(getattr(record, "reasoning_effort", "") or "") or None
            existing = by_thread.get(thread_id)
            if existing is not None:
                if existing.get("status") == "deleted":
                    continue
                old_title = str(existing.get("title") or "").strip()
                if old_title in {"", "新会话", "导入会话"} and title:
                    await self.db.execute(
                        """UPDATE codex_sessions
                           SET title = ?, model = COALESCE(?, model),
                               reasoning_effort = COALESCE(?, reasoning_effort),
                               updated_at = ?
                           WHERE id = ?""",
                        (title, model, reasoning, updated_at, existing["id"]),
                    )
                continue

            session_id = new_id("sess")
            await self.db.execute(
                """INSERT INTO codex_sessions(
                     id, project_id, codex_thread_id, status, title, model,
                     reasoning_effort, created_at, updated_at, archived_at
                   ) VALUES (?, ?, ?, 'archived', ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    project_id,
                    thread_id,
                    title,
                    model,
                    reasoning,
                    updated_at,
                    updated_at,
                    updated_at,
                ),
            )
            by_thread[thread_id] = {"id": session_id, "codex_thread_id": thread_id}
            inserted += 1
        return inserted

    async def discover_codex_projects(self, history) -> dict[str, Any]:
        discovered = await asyncio.to_thread(history.discover_projects)
        registered = await self.list()
        registered_by_path = {
            str(normalize_path(project["normalized_path"])): project
            for project in registered
        }
        candidates: list[dict[str, Any]] = []
        for candidate in discovered.get("projects", []):
            try:
                path = normalize_path(str(candidate.get("path") or ""))
            except (OSError, ValueError):
                continue
            existing = registered_by_path.get(str(path))
            candidates.append({
                **candidate,
                "path": str(path),
                "imported": existing is not None,
                "importable": path.is_dir() and self._is_allowed(path),
                "project_id": existing.get("id") if existing else None,
            })
        return {
            "projects": candidates,
            "source": discovered.get("source", ""),
            "scanned_sessions": discovered.get("scanned_sessions", 0),
        }

    def set_allowed_roots(self, roots: list[str]) -> None:
        self.allowed_roots = [normalize_path(root) for root in roots]

    async def get(self, project_id: str) -> dict:
        row = await self._project_row(project_id)
        if row is None:
            raise ProjectNotFoundError("project not found")
        return dict(row)

    async def _project_row(self, project_id: str):
        return await self.db.fetch_one(
            """SELECT p.id, p.name, p.normalized_path,
                      p.current_session_id,
                      COALESCE(s.codex_thread_id, p.codex_thread_id) AS codex_thread_id,
                      s.status AS session_status,
                      s.title AS session_title,
                      COALESCE(s.updated_at, p.last_active_at, p.created_at) AS session_updated_at,
                      p.model, p.reasoning_effort, p.goal,
                      p.default_sandbox, p.created_at, p.last_active_at
               FROM projects p
               LEFT JOIN codex_sessions s ON s.id = p.current_session_id
               WHERE p.id = ? AND p.archived = 0""",
            (project_id,),
        )

    async def list(self) -> list[dict]:
        rows = await self.db.fetch_all(
            """SELECT p.id, p.name, p.normalized_path,
                      p.current_session_id,
                      COALESCE(s.codex_thread_id, p.codex_thread_id) AS codex_thread_id,
                      s.status AS session_status,
                      s.title AS session_title,
                      COALESCE(s.updated_at, p.last_active_at, p.created_at) AS session_updated_at,
                      p.model, p.reasoning_effort, p.goal,
                      p.default_sandbox, p.created_at, p.last_active_at
               FROM projects p
               LEFT JOIN codex_sessions s ON s.id = p.current_session_id
               WHERE p.archived = 0
               ORDER BY p.created_at DESC"""
        )
        return [dict(row) for row in rows]

    async def set_model(self, project_id: str, model: str | None) -> dict:
        await self.get(project_id)
        await self.db.execute(
            "UPDATE projects SET model = ? WHERE id = ?",
            (model, project_id),
        )
        return await self.get(project_id)

    async def set_reasoning_effort(self, project_id: str, effort: str | None) -> dict:
        await self.get(project_id)
        await self.db.execute(
            "UPDATE projects SET reasoning_effort = ? WHERE id = ?",
            (effort, project_id),
        )
        return await self.get(project_id)

    async def set_goal(self, project_id: str, goal: str) -> dict:
        await self.get(project_id)
        await self.db.execute(
            "UPDATE projects SET goal = ? WHERE id = ?",
            (goal, project_id),
        )
        return await self.get(project_id)

    async def new_thread(self, project_id: str) -> dict:
        await self.get(project_id)
        # A new conversation is UI state until its first prompt creates content.
        await self.db.execute(
            """UPDATE projects
               SET current_session_id = NULL, codex_thread_id = NULL, last_active_at = ?
               WHERE id = ?""",
            (iso_now(), project_id),
        )
        return await self.get(project_id)

    async def create_session_from_prompt(
        self,
        project_id: str,
        prompt: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Create the first database conversation for a real prompt."""

        project = await self.get(project_id)
        session_id = new_id("sess")
        now = iso_now()
        title = initial_session_title(prompt)
        await self.db.execute(
            """INSERT INTO codex_sessions(
                 id, project_id, codex_thread_id, status, title, model,
                 reasoning_effort, created_at, updated_at
               ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?)""",
            (
                session_id,
                project_id,
                thread_id,
                title,
                model or project.get("model"),
                reasoning_effort or project.get("reasoning_effort"),
                now,
                now,
            ),
        )
        if not project.get("current_session_id"):
            await self.db.execute(
                """UPDATE projects
                   SET current_session_id = ?, codex_thread_id = ?, last_active_at = ?
                   WHERE id = ?""",
                (session_id, thread_id, now, project_id),
            )
        else:
            await self.db.execute(
                "UPDATE projects SET last_active_at = ? WHERE id = ?",
                (now, project_id),
            )
        return session_id

    async def select_session(self, project_id: str, session_id: str, *, restore: bool = True) -> dict:
        session = await self.get_session(project_id, session_id)
        if session.get("status") == "deleted":
            raise ProjectNotFoundError("session not found")
        if restore and session.get("status") == "archived":
            await self.db.execute(
                """UPDATE codex_sessions
                   SET status = 'active', archived_at = NULL, updated_at = ?
                   WHERE id = ?""",
                (iso_now(), session_id),
            )
        await self.db.execute(
            """UPDATE projects
               SET current_session_id = ?, codex_thread_id = ?, last_active_at = ?
               WHERE id = ?""",
            (session_id, session.get("codex_thread_id"), iso_now(), project_id),
        )
        return await self.get(project_id)

    async def get_session_thread_id(self, project_id: str, session_id: str) -> str:
        await self.get(project_id)
        row = await self.db.fetch_one(
            """SELECT codex_thread_id FROM codex_sessions
               WHERE id = ? AND project_id = ?""",
            (session_id, project_id),
        )
        return str(row["codex_thread_id"] or "") if row else ""

    async def set_thread(self, project_id: str, thread_id: str, *, session_id: str | None = None) -> None:
        target_session = session_id or await self._scalar(project_id, "current_session_id")
        if not target_session:
            raise ProjectNotFoundError("project session not found")
        now = iso_now()
        await self.db.execute(
            """UPDATE codex_sessions
               SET codex_thread_id = ?, updated_at = ?
               WHERE id = ? AND project_id = ?""",
            (thread_id, now, target_session, project_id),
        )
        await self.db.execute(
            """UPDATE projects
               SET codex_thread_id = ?, current_session_id = ?, last_active_at = ?
               WHERE id = ?""",
            (thread_id, target_session, now, project_id),
        )

    async def get_session(self, project_id: str, session_id: str) -> dict:
        row = await self.db.fetch_one(
            """SELECT * FROM codex_sessions
               WHERE id = ? AND project_id = ?""",
            (session_id, project_id),
        )
        if row is None:
            raise ProjectNotFoundError("session not found")
        return dict(row)

    async def list_sessions(self, project_id: str, *, status: str = "active") -> list[dict]:
        rows = await self.db.fetch_all(
            """SELECT * FROM codex_sessions
               WHERE project_id = ? AND status = ?
               ORDER BY updated_at DESC, created_at DESC""",
            (project_id, status),
        )
        return [dict(row) for row in rows]

    async def archive_session(self, project_id: str, session_id: str) -> dict:
        session = await self.get_session(project_id, session_id)
        now = iso_now()
        await self.db.execute(
            """UPDATE codex_sessions
               SET status = 'archived', archived_at = ?, updated_at = ?
               WHERE id = ? AND project_id = ?""",
            (now, now, session_id, project_id),
        )
        if await self._scalar(project_id, "current_session_id") == session_id:
            await self.db.execute(
                """UPDATE projects
                   SET current_session_id = NULL, codex_thread_id = NULL
                   WHERE id = ?""",
                (project_id,),
            )
        session.update({"status": "archived", "archived_at": now, "updated_at": now})
        return session

    async def delete_session(self, project_id: str, session_id: str) -> dict:
        """Hide a conversation permanently for this agent without touching rollout files."""

        session = await self.get_session(project_id, session_id)
        now = iso_now()
        await self.db.execute(
            """UPDATE codex_sessions
               SET status = 'deleted', deleted_at = ?, updated_at = ?
               WHERE id = ? AND project_id = ?""",
            (now, now, session_id, project_id),
        )
        if await self._scalar(project_id, "current_session_id") == session_id:
            await self.db.execute(
                """UPDATE projects
                   SET current_session_id = NULL, codex_thread_id = NULL
                   WHERE id = ?""",
                (project_id,),
            )
        session.update({"status": "deleted", "deleted_at": now, "updated_at": now})
        return session

    async def set_session_title(self, session_id: str, title: str) -> None:
        clean = normalize_session_title(title)
        if not clean:
            return
        await self.db.execute(
            """UPDATE codex_sessions
               SET title = ?, updated_at = COALESCE(updated_at, ?)
               WHERE id = ? AND status != 'deleted'""",
            (clean, iso_now(), session_id),
        )

    async def purge_empty_sessions(self) -> int:
        """Remove legacy no-content placeholder conversations."""

        rows = await self.db.fetch_all(
            """SELECT s.id, s.project_id, s.title
               FROM codex_sessions s
               WHERE s.status = 'active'
                 AND s.codex_thread_id IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM turns t
                   WHERE t.session_id = s.id OR (
                     t.project_id = s.project_id AND t.thread_id IS NULL
                       AND t.created_at >= s.created_at
                   )
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM events e WHERE e.session_id = s.id
                 )"""
        )
        if not rows:
            return 0
        now = iso_now()
        for row in rows:
            await self.db.execute(
                """UPDATE projects
                   SET current_session_id = NULL, codex_thread_id = NULL
                   WHERE current_session_id = ?""",
                (row["id"],),
            )
            await self.db.execute(
                """UPDATE codex_sessions
                   SET status = 'deleted', deleted_at = ?, updated_at = ?
                   WHERE id = ?""",
                (now, now, row["id"]),
            )
        return len(rows)

    async def touch_session(self, session_id: str) -> None:
        await self.db.execute(
            "UPDATE codex_sessions SET updated_at = ? WHERE id = ?",
            (iso_now(), session_id),
        )

    async def archive(self, project_id: str) -> dict:
        project = await self.get(project_id)
        await self.db.execute(
            "UPDATE projects SET archived = 1, last_active_at = ? WHERE id = ?",
            (iso_now(), project_id),
        )
        return project

    async def touch(self, project_id: str) -> None:
        await self.db.execute(
            "UPDATE projects SET last_active_at = ? WHERE id = ?",
            (iso_now(), project_id),
        )

    async def _scalar(self, project_id: str, column: str) -> Any:
        row = await self.db.fetch_one(
            f"SELECT {column} AS value FROM projects WHERE id = ?", (project_id,)
        )
        return row["value"] if row else None


def initial_session_title(prompt: str) -> str:
    text = normalize_session_title(prompt)
    if not text:
        return "未命名对话"
    return text[:40]


def normalize_session_title(value: str) -> str:
    text = " ".join(str(value or "").split())
    return text.strip(" \t\r\n\"'“”‘’《》")
