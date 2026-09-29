from __future__ import annotations

import json
import hashlib
import os
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..protocol.errors import ValidationError
from ..projects.paths import normalize_path


WRAPPER_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<skills_instructions>",
    "<user_instructions>",
    "<turn_context>",
    "<system>",
)


class CodexHistoryService:
    def discover_projects(self) -> dict[str, Any]:
        """Discover project directories referenced by local Codex sessions."""

        sessions_dir = self._codex_home() / "sessions"
        if not sessions_dir.exists():
            return {
                "projects": [],
                "source": str(sessions_dir),
                "scanned_sessions": 0,
            }

        projects: dict[str, dict[str, Any]] = {}
        scanned_sessions = 0
        for path in self._session_files(sessions_dir):
            metadata = self._read_session_meta(path)
            if metadata is None:
                continue
            scanned_sessions += 1
            cwd, timestamp = metadata
            try:
                project_path = normalize_path(cwd)
            except (OSError, ValueError):
                continue
            if not project_path.is_absolute() or not project_path.is_dir():
                continue

            key = self._path_key(project_path)
            last_used_at = self._file_timestamp(path) or timestamp
            display_name = Path(cwd).name or project_path.name or str(project_path)
            current = projects.get(key)
            if current is None:
                projects[key] = {
                    "name": display_name,
                    "path": str(project_path),
                    "last_used_at": last_used_at,
                    "session_count": 1,
                }
                continue
            current["session_count"] = int(current["session_count"]) + 1
            if self._is_newer(last_used_at, str(current.get("last_used_at") or "")):
                current["last_used_at"] = last_used_at

        discovered = sorted(
            projects.values(),
            key=lambda item: (
                str(item.get("last_used_at") or ""),
                str(item.get("name") or "").lower(),
            ),
            reverse=True,
        )
        return {
            "projects": discovered,
            "source": str(sessions_dir),
            "scanned_sessions": scanned_sessions,
        }

    def list_messages(
        self,
        project: dict[str, Any],
        *,
        limit: int = 100,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            requested = max(1, min(int(limit), 300))
        except (TypeError, ValueError) as exc:
            raise ValidationError("limit must be a number") from exc

        root = self._project_path(project)
        sessions = self._matching_sessions(root, thread_id)
        messages: deque[dict[str, Any]] = deque(maxlen=requested)
        scanned_files = 0

        for path in sessions:
            before_count = len(messages)
            for record in self._iter_messages(path):
                if record is not None:
                    messages.append(record)
            if len(messages) > before_count:
                scanned_files += 1
            # Rollout files are newest first, so once the bounded queue is
            # full the remaining older sessions would only be evicted.
            if len(messages) == requested:
                break

        return {
            "project_id": str(project.get("id", "")),
            "source": str(self._codex_home() / "sessions"),
            "messages": list(messages),
            "thread_id": thread_id or project.get("codex_thread_id") or None,
            "scanned_sessions": scanned_files,
            "matched_sessions": len(sessions),
        }

    def _matching_sessions(
        self,
        project_path: Path,
        thread_id: str | None = None,
    ) -> list[Path]:
        sessions_dir = self._codex_home() / "sessions"
        if not sessions_dir.exists():
            return []

        paths = [path for path in self._session_files(sessions_dir)
                 if self._session_path_matches(path, project_path, thread_id)]
        paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return paths[:80]

    def _session_path_matches(
        self,
        path: Path,
        project_path: Path,
        thread_id: str | None = None,
    ) -> bool:
        metadata = self._read_session_meta(path)
        if metadata is None:
            return False
        if thread_id and self._thread_id(path) != thread_id:
            return False
        return self._same_path(Path(metadata[0]), project_path)

    def _thread_id(self, path: Path) -> str:
        match = re.search(r"rollout-.*-([0-9a-f-]{36})\.jsonl$", path.name, re.I)
        return match.group(1) if match else ""

    def _session_files(self, sessions_dir: Path) -> list[Path]:
        try:
            return [path for path in sessions_dir.rglob("*.jsonl") if path.is_file()]
        except OSError:
            return []

    def _read_session_meta(self, path: Path) -> tuple[str, str] | None:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        return None
                    if str(record.get("type", "")) != "session_meta":
                        return None
                    payload = record.get("payload") or {}
                    if not isinstance(payload, dict):
                        return None
                    cwd = str(payload.get("cwd") or "").strip()
                    if not cwd:
                        return None
                    timestamp = str(record.get("timestamp") or payload.get("timestamp") or "")
                    return cwd, timestamp
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return None

    def _file_timestamp(self, path: Path) -> str:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            return ""

    def _is_newer(self, candidate: str, current: str) -> bool:
        if not candidate:
            return False
        if not current:
            return True
        try:
            left = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            right = datetime.fromisoformat(current.replace("Z", "+00:00"))
            return left > right
        except ValueError:
            return candidate > current

    def _path_key(self, path: Path) -> str:
        return str(path).lower() if os.name == "nt" else str(path)

    def _iter_messages(self, path: Path):
        response_ids: set[str] = set()
        response_times: dict[bytes, list[datetime]] = {}
        for record_type, record, payload in self._rollout_records(path):
            if record_type != "response_item":
                continue
            parsed = self._parse_message(payload)
            if parsed is None:
                continue
            message_id = str(parsed.get("message_id") or "")
            if message_id:
                response_ids.add(message_id)
            timestamp = self._parse_timestamp(record.get("timestamp"))
            if timestamp is not None:
                key = self._message_key(parsed)
                response_times.setdefault(key, []).append(timestamp)

        for record_type, record, payload in self._rollout_records(path):
            if record_type == "response_item":
                parsed = self._parse_message(payload)
            elif record_type == "event_msg":
                event_type = str(payload.get("type", "")).casefold()
                item = payload.get("item")
                if event_type not in ("item_completed", "item.completed") or not isinstance(item, dict):
                    continue
                parsed = self._parse_message(item)
                if parsed is not None and self._duplicates_response(
                    parsed, record, response_ids, response_times
                ):
                    continue
            else:
                continue
            if parsed is not None:
                parsed["ts"] = str(record.get("timestamp") or "")
                yield parsed

    def _rollout_records(self, path: Path):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    record_type = str(record.get("type", "")).casefold()
                    payload = record.get("payload") or {}
                    if record_type in ("response_item", "event_msg") and isinstance(payload, dict):
                        yield record_type, record, payload
        except OSError:
            return

    def _message_key(self, message: dict[str, Any]) -> bytes:
        value = f"{message.get('role', '')}\0{message.get('text', '')}"
        return hashlib.sha256(value.encode("utf-8")).digest()

    def _parse_timestamp(self, value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None

    def _duplicates_response(
        self,
        message: dict[str, Any],
        record: dict[str, Any],
        response_ids: set[str],
        response_times: dict[bytes, list[datetime]],
    ) -> bool:
        message_id = str(message.get("message_id") or "")
        if message_id and message_id in response_ids:
            return True
        timestamp = self._parse_timestamp(record.get("timestamp"))
        if timestamp is None:
            return False
        return any(
            abs((timestamp - response_time).total_seconds()) <= 2
            for response_time in response_times.get(self._message_key(message), [])
        )

    def _parse_message(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        payload_type = str(payload.get("type", "")).casefold().replace("-", "_")
        role = str(payload.get("role", "")).casefold()
        if payload_type in ("agent_message", "agentmessage") and not role:
            role = "assistant"
        elif payload_type in ("user_message", "usermessage") and not role:
            role = "user"
        if role not in ("user", "assistant"):
            return None
        if payload_type not in ("message", "agent_message", "agentmessage", "user_message", "usermessage"):
            return None
        text = self._message_text(payload)
        if not text or text.lower().startswith(WRAPPER_PREFIXES):
            return None
        return {
            "role": role,
            "text": text[:20000],
            # Keep the UI-facing alias for older mobile builds that read
            # `body` instead of the native Codex `text` field.
            "body": text[:20000],
            "message_id": str(payload.get("id") or ""),
        }

    def _message_text(self, payload: dict[str, Any]) -> str:
        parts = payload.get("content")
        if isinstance(parts, list):
            text = "\n".join(
                part
                for part in (self._content_text(item) for item in parts)
                if part
            )
        else:
            text = self._scalar_text(payload.get("text"))
            if not text:
                text = self._scalar_text(payload.get("body"))
            if not text:
                text = self._scalar_text(payload.get("message"))
            if not text:
                text = self._scalar_text(payload.get("content"))
        return text.strip()

    def _scalar_text(self, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            return str(value.get("text") or value.get("body") or "").strip()
        return ""

    def _content_text(self, item: Any) -> str:
        if isinstance(item, str):
            return item.strip()
        if not isinstance(item, dict):
            return ""
        if str(item.get("type", "")).casefold() not in ("input_text", "output_text", "text"):
            return ""
        return self._scalar_text(item.get("text") or item.get("body"))

    def _codex_home(self) -> Path:
        return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))

    def _project_path(self, project: dict[str, Any]) -> Path:
        raw = str(project.get("normalized_path") or "")
        path = Path(raw)
        if not path.is_absolute():
            raise ValidationError("project path is invalid")
        return path

    def _same_path(self, left: Path, right: Path) -> bool:
        try:
            left = left.resolve()
            right = right.resolve()
        except OSError:
            pass
        if os.name == "nt":
            return str(left).lower() == str(right).lower()
        return left == right
