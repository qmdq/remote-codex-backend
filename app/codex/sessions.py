from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass(slots=True)
class SessionRecord:
    thread_id: str
    path: Path
    cwd: Path
    model: str
    reasoning_effort: str
    updated_at: str
    title: str
    context_window: int | None = None
    used_tokens: int | None = None
    rate_limits: dict[str, Any] | None = None


class CodexSessionService:
    """Read the local Codex rollout files used by the desktop app."""

    def sessions_for_project(self, project_path: Path) -> list[SessionRecord]:
        sessions_dir = self._codex_home() / "sessions"
        records: list[SessionRecord] = []
        for path in self._session_files(sessions_dir):
            record = self._read_session(path)
            if record and self._same_path(record.cwd, project_path):
                records.append(record)
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records

    def find_session(self, thread_id: str, project_path: Path | None = None) -> SessionRecord | None:
        sessions_dir = self._codex_home() / "sessions"
        fallback: SessionRecord | None = None
        for path in self._session_files(sessions_dir):
            record = self._read_session(path, want_thread_id=thread_id)
            if record is None:
                continue
            if record.thread_id == thread_id:
                if project_path is None or self._same_path(record.cwd, project_path):
                    return record
                if fallback is None:
                    fallback = record
        return fallback

    def status(self, project: dict[str, Any], thread_id: str | None = None) -> dict[str, Any]:
        wanted = str(thread_id or project.get("codex_thread_id") or "")
        record = self.find_session(wanted) if wanted else None
        if record is None and project.get("normalized_path"):
            sessions = self.sessions_for_project(Path(str(project["normalized_path"])))
            record = sessions[0] if sessions else None
        if record is None:
            return {"available": False, "thread_id": wanted or None}

        context_window = record.context_window
        used_tokens = record.used_tokens
        percent = None
        if context_window and used_tokens is not None:
            percent = max(0.0, min(100.0, used_tokens / context_window * 100))
        return {
            "available": True,
            "thread_id": record.thread_id,
            "model": record.model,
            "reasoning_effort": record.reasoning_effort,
            "context_window": context_window,
            "used_tokens": used_tokens,
            "context_percent": round(percent, 1) if percent is not None else None,
            "rate_limits": record.rate_limits or {},
            "updated_at": record.updated_at,
            "title": record.title,
            "source": str(record.path),
        }

    def sessions_snapshot(self, project: dict[str, Any]) -> dict[str, Any]:
        records = self.sessions_for_project(Path(str(project.get("normalized_path", ""))))
        current = str(project.get("codex_thread_id") or "")
        return {
            "project_id": str(project.get("id", "")),
            "current_thread_id": current or None,
            "is_new_thread": not current,
            "sessions": [
                {
                    "thread_id": record.thread_id,
                    "current": record.thread_id == current,
                    "title": record.title,
                    "model": record.model,
                    "reasoning_effort": record.reasoning_effort,
                    "updated_at": record.updated_at,
                    "context_percent": self._percent(record),
                }
                for record in records[:80]
            ],
        }

    def database_snapshot(
        self,
        project: dict[str, Any],
        active_sessions: list[dict[str, Any]],
        archived_sessions: list[dict[str, Any]],
        running_turns: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build a PC-Codex style conversation snapshot from database rows."""

        current_session_id = str(project.get("current_session_id") or "")
        running_by_session = {
            str(item.get("session_id") or ""): item
            for item in running_turns or []
            if item.get("session_id")
        }
        records = {
            str(row.get("codex_thread_id") or ""): record
            for row in [*active_sessions, *archived_sessions]
            if row.get("codex_thread_id")
            if (record := self.find_session(str(row["codex_thread_id"]))) is not None
        }

        def summary(row: dict[str, Any], *, current: bool) -> dict[str, Any]:
            thread_id = str(row.get("codex_thread_id") or "")
            record = records.get(thread_id)
            updated_at = str(
                record.updated_at
                if record
                else row.get("updated_at")
                or row.get("archived_at")
                or row.get("created_at")
                or ""
            )
            return {
                "session_id": str(row.get("id") or ""),
                "thread_id": thread_id or None,
                "current": current,
                "status": str(row.get("status") or "active"),
                "title": self._row_title(row, updated_at),
                "model": record.model if record else str(row.get("model") or "default"),
                "reasoning_effort": (
                    record.reasoning_effort if record
                    else str(row.get("reasoning_effort") or "default")
                ),
                "updated_at": updated_at,
                "archived_at": row.get("archived_at"),
                "context_percent": self._percent(record) if record else None,
                "running": str(row.get("id") or "") in running_by_session,
                "running_turn_id": running_by_session.get(
                    str(row.get("id") or ""), {}
                ).get("turn_id"),
            }

        active = [
            summary(row, current=str(row.get("id")) == current_session_id)
            for row in active_sessions
        ]
        archived = [summary(row, current=False) for row in archived_sessions]
        current_thread = next((item["thread_id"] for item in active if item["current"]), None)
        return {
            "project_id": str(project.get("id", "")),
            "current_session_id": current_session_id or None,
            "current_thread_id": current_thread,
            "is_new_thread": not current_thread,
            "sessions": active,
            "archived_sessions": archived,
        }

    def _row_title(self, row: dict[str, Any], updated_at: str) -> str:
        title = str(row.get("title") or "").strip()
        if title and title not in {"新会话", "导入会话"}:
            return title
        return self._session_label(str(row.get("codex_thread_id") or ""), updated_at)

    @staticmethod
    def _percent(record: SessionRecord) -> float | None:
        if not record.context_window or record.used_tokens is None:
            return None
        return round(max(0.0, min(100.0, record.used_tokens / record.context_window * 100)), 1)

    def _session_files(self, sessions_dir: Path) -> list[Path]:
        if not sessions_dir.exists():
            return []
        try:
            paths = [path for path in sessions_dir.rglob("*.jsonl") if path.is_file()]
            paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            return paths[:240]
        except OSError:
            return []

    def _read_session(self, path: Path, *, want_thread_id: str = "") -> SessionRecord | None:
        thread_id = ""
        cwd = ""
        model = ""
        reasoning_effort = ""
        updated_at = ""
        context_window = None
        used_tokens = None
        rate_limits = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(raw, dict):
                        continue
                    timestamp = str(raw.get("timestamp") or updated_at)
                    record_type = str(raw.get("type", "")).casefold()
                    payload = raw.get("payload")
                    payload = payload if isinstance(payload, dict) else {}

                    if record_type == "session_meta" and not thread_id:
                        thread_id = str(payload.get("id") or payload.get("session_id") or "")
                        cwd = str(payload.get("cwd") or "")
                        model = self._model_from_meta(payload) or model
                        if not timestamp:
                            timestamp = str(payload.get("timestamp") or "")
                    elif record_type in ("turn_context", "session_context"):
                        model = str(payload.get("model") or model)
                        reasoning_effort = str(
                            payload.get("reasoning_effort")
                            or payload.get("model_reasoning_effort")
                            or reasoning_effort
                        )
                    elif record_type in ("token_count", "token_usage"):
                        info = payload.get("info")
                        info = info if isinstance(info, dict) else {}
                        usage = info.get("last_token_usage")
                        usage = usage if isinstance(usage, dict) else info.get("total_token_usage")
                        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
                            used_tokens = max(0, int(usage.get("total_tokens") or 0))
                        if info.get("model_context_window") is not None:
                            context_window = max(0, int(info.get("model_context_window") or 0))
                        if isinstance(payload.get("rate_limits"), dict):
                            rate_limits = payload["rate_limits"]
                    if timestamp:
                        updated_at = timestamp
                    if thread_id and want_thread_id and thread_id != want_thread_id:
                        return None
        except OSError:
            return None
        if not thread_id:
            match = re.search(r"rollout-.*-([0-9a-f-]{36})\.jsonl$", path.name, re.I)
            thread_id = match.group(1) if match else ""
        if not thread_id or not cwd:
            return None
        return SessionRecord(
            thread_id=thread_id,
            path=path,
            cwd=Path(cwd),
            model=model or "default",
            reasoning_effort=reasoning_effort or "default",
            updated_at=updated_at,
            title=self._session_label(thread_id, updated_at),
            context_window=context_window,
            used_tokens=used_tokens,
            rate_limits=rate_limits,
        )

    def _session_label(self, thread_id: str, updated_at: str) -> str:
        """Return a stable session label, never a chat message preview."""
        if updated_at:
            try:
                value = updated_at.replace("Z", "+00:00")
                hour = value[11:13]
                minute = value[14:16]
                if len(hour) == 2 and len(minute) == 2:
                    return f"会话 · {hour}:{minute}"
            except (IndexError, TypeError):
                pass
        suffix = thread_id[-8:] if thread_id else "unknown"
        return f"会话 · {suffix}"

    def _model_from_meta(self, payload: dict[str, Any]) -> str:
        provenance = payload.get("base_instructions")
        provenance = provenance.get("provenance") if isinstance(provenance, dict) else {}
        candidates = [
            payload.get("model"),
            provenance.get("model") if isinstance(provenance, dict) else None,
        ]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value:
                return value
        return ""

    def _codex_home(self) -> Path:
        return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))

    def _same_path(self, left: Path, right: Path) -> bool:
        try:
            left = left.resolve()
            right = right.resolve()
        except OSError:
            pass
        if os.name == "nt":
            return str(left).lower() == str(right).lower()
        return left == right
