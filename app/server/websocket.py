from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve

from ..auth.devices import DeviceService
from ..auth.pairing import PairingService
from ..codex.gateway import SandboxMode
from ..codex.sessions import CodexSessionService
from ..codex.turns import TurnSupervisor
from ..config import AppConfig
from ..events.fanout import EventFanout
from ..events.store import EventStore
from ..files.service import FileUpload, MAX_UPLOAD_CHUNK_SIZE
from ..input_service import SystemInputService, InputValidationError
from ..monitors.metrics import MetricsMonitor
from ..monitors.screen import ScreenMonitor
from ..projects.manager import ProjectService
from ..protocol.errors import (
    AgentError,
    AuthenticationError,
    PairingError,
    ValidationError,
)
from ..protocol.messages import Envelope, error_payload, parse_envelope, response
from ..terminal.service import (
    MAX_TERMINAL_COLS,
    MAX_TERMINAL_ROWS,
    MIN_TERMINAL_COLS,
    MIN_TERMINAL_ROWS,
    TerminalService,
    TerminalSession,
)
from .connection import Connection

logger = logging.getLogger(__name__)


class AgentServer:
    def __init__(
        self,
        config: AppConfig,
        database,
        devices: DeviceService,
        pairing: PairingService,
        projects: ProjectService,
        turns: TurnSupervisor,
        events: EventStore,
        fanout: EventFanout,
        metrics: MetricsMonitor,
        screen: ScreenMonitor,
        gateway,
        files,
        history,
        sessions: CodexSessionService,
        system_input: SystemInputService,
        terminal: TerminalService,
    ):
        self.config = config
        self.db = database
        self.devices = devices
        self.pairing = pairing
        self.projects = projects
        self.turns = turns
        self.events = events
        self.fanout = fanout
        self.metrics = metrics
        self.screen = screen
        self.gateway = gateway
        self.files = files
        self.history = history
        self.sessions = sessions
        self.system_input = system_input
        self.terminal = terminal
        self.preview_info_getter = None
        self._connections: set[Connection] = set()
        self._event_tasks: dict[Connection, asyncio.Task[None]] = {}
        self._monitor_tasks: dict[Connection, list[asyncio.Task[None]]] = {}
        self._terminal_tasks: dict[Connection, asyncio.Task[None]] = {}
        self._terminal_sessions: dict[Connection, TerminalSession] = {}
        self._auth_failures: dict[str, int] = {}
        self._uploads: dict[Connection, dict[str, FileUpload]] = {}

    async def handle(self, websocket: Any) -> None:
        peer = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
        connection: Connection | None = None
        try:
            first = await asyncio.wait_for(websocket.recv(), timeout=self.config.server.handshake_timeout_sec)
            envelope = parse_envelope(first)
            if envelope.type == "pair.request":
                await self._pair(websocket, envelope)
                return
            if envelope.type != "hello":
                raise AuthenticationError("first message must be hello")
            token = envelope.payload.get("device_token")
            failures = self._auth_failures.get(peer, 0)
            if failures >= 5:
                raise AuthenticationError("authentication locked")
            try:
                device = await self.devices.authenticate(token)
            except ValueError as exc:
                self._auth_failures[peer] = failures + 1
                raise AuthenticationError(str(exc)) from exc
            connection = Connection(
                websocket,
                device.id,
                self.config.server.max_message_bytes,
            )
            self._connections.add(connection)
            await connection.send(response("ready", {
                "agent_version": "0.2.0",
                "protocol_version": 1,
                "capabilities": {
                    "codex_project_sync": True,
                    "codex_history": True,
                    "file_preview": True,
                    "file_web_preview": True,
                    "screen_input": True,
                    "session_actions": True,
                    "codex_sessions": True,
                    "terminal": True,
                    "terminal_pty": self.terminal.pty_available,
                },
                "device_id": device.id,
                "preview": self.preview_info_getter() if self.preview_info_getter else None,
                "server_time": _iso_now(),
                "models": {
                    "choices": self.config.models.choices,
                    "default": self.config.models.default_model,
                },
            }, request_id=envelope.id))
            await self._subscribe_default_project(connection)
            tasks = [
                asyncio.create_task(connection.receive_loop(self._make_handler(connection))),
                asyncio.create_task(self._pump_events(connection)),
            ]
            try:
                _, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in tasks:
                    if not task.cancelled() and task.exception():
                        raise task.exception()
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except AuthenticationError as exc:
            await self._send_raw_error(websocket, exc)
            await websocket.close(code=4401, reason=exc.code)
        except TimeoutError:
            await websocket.close(code=4408, reason="handshake timeout")
        except AgentError as exc:
            await self._send_raw_error(websocket, exc)
            await websocket.close(code=4400)
        except Exception:
            if connection is not None:
                connection.closed = True
        finally:
            if connection is not None:
                self._connections.discard(connection)
                for upload in self._uploads.pop(connection, {}).values():
                    await asyncio.to_thread(self.files.cancel_upload, upload)
                task = self._event_tasks.pop(connection, None)
                if task:
                    task.cancel()
                for monitor_task in self._monitor_tasks.pop(connection, []):
                    monitor_task.cancel()
                terminal_task = self._terminal_tasks.pop(connection, None)
                if terminal_task:
                    terminal_task.cancel()
                terminal_session = self._terminal_sessions.pop(connection, None)
                if terminal_session is not None:
                    await terminal_session.close()

    def _make_handler(self, connection: Connection):
        async def handler(raw: str | bytes) -> None:
            envelope: Envelope | None = None
            try:
                envelope = parse_envelope(raw)
                await self._dispatch(connection, envelope)
            except AgentError as exc:
                await connection.send(response(
                    "error", error_payload(exc), request_id=envelope.id if envelope else None
                ))
            except Exception:
                logger.exception(
                    "Unhandled agent request failed: %s", envelope.type if envelope else "unknown"
                )
                await connection.send(response("error", {
                    "code": "agent.error", "message": "internal agent error", "retryable": False
                }, request_id=envelope.id if envelope else None))
        return handler

    async def _dispatch(self, connection: Connection, envelope: Envelope) -> None:
        payload = envelope.payload
        message_type = envelope.type
        if message_type == "ping":
            await connection.send(response("pong", {}, request_id=envelope.id))
            return
        if message_type == "project.list":
            await connection.send(response("project.snapshot", {
                "projects": await self.projects.list(),
                "running_turns": self.turns.running_turns(),
            }, request_id=envelope.id))
            return
        if message_type == "codex.project.list":
            result = await self.projects.discover_codex_projects(self.history)
            await connection.send(response("codex.project.snapshot", result, request_id=envelope.id))
            return
        if message_type == "codex.project.sync":
            discovered = await self.projects.discover_codex_projects(self.history)
            synced_projects = 0
            synced_sessions = 0
            candidates: list[dict[str, Any]] = []
            for candidate in discovered.get("projects", []):
                if not candidate.get("importable"):
                    candidates.append(candidate)
                    continue
                try:
                    project, _ = await self.projects.import_codex_project(
                        str(candidate.get("path", "")),
                        str(candidate.get("name", "")),
                    )
                    records = await asyncio.to_thread(
                        self.sessions.sessions_for_project,
                        Path(project["normalized_path"]),
                    )
                    await self.projects.import_codex_sessions(project["id"], records)
                    synced_projects += 1
                    synced_sessions += len(records)
                    candidate = {
                        **candidate,
                        "imported": True,
                        "project_id": project["id"],
                        "session_count": len(records),
                    }
                except Exception:
                    logger.exception(
                        "Failed to sync Codex project %s", candidate.get("path")
                    )
                candidates.append(candidate)
            await connection.send(response("codex.project.synced", {
                "projects": candidates,
                "registered_projects": await self.projects.list(),
                "source": discovered.get("source", ""),
                "scanned_sessions": discovered.get("scanned_sessions", 0),
                "synced_projects": synced_projects,
                "synced_sessions": synced_sessions,
                "running_turns": self.turns.running_turns(),
            }, request_id=envelope.id))
            return
        if message_type == "codex.project.import":
            project, _ = await self.projects.import_codex_project(
                str(payload.get("path", "")),
                str(payload.get("name", "")),
            )
            records = await asyncio.to_thread(
                self.sessions.sessions_for_project,
                Path(project["normalized_path"]),
            )
            await self.projects.import_codex_sessions(project["id"], records)
            project = await self.projects.get(project["id"])
            self._subscribe_project(connection, project["id"])
            await connection.send(response("project.snapshot", {
                "selected": project,
                "latest_seq": await self.events.latest_seq(project["id"]),
                "projects": await self.projects.list(),
                "running_turns": self.turns.running_turns(),
            }, request_id=envelope.id))
            return
        if message_type == "project.create":
            project = await self.projects.create(
                str(payload.get("name", "")), str(payload.get("path", ""))
            )
            self._subscribe_project(connection, project["id"])
            await connection.send(response("project.snapshot", {
                "selected": project,
                "latest_seq": await self.events.latest_seq(project["id"]),
                "projects": await self.projects.list(),
                "running_turns": self.turns.running_turns(),
            }, request_id=envelope.id))
            return
        if message_type == "project.delete":
            project_id = str(payload.get("project_id", "")).strip()
            current_project_id = str(getattr(connection.fanout_subscriber, "project_id", ""))
            await self.projects.delete(project_id)
            projects = await self.projects.list()
            selected = next(
                (item for item in projects if item["id"] == current_project_id),
                None,
            )
            if selected is None and projects:
                selected = projects[0]
            if selected:
                self._subscribe_project(connection, selected["id"])
                latest_seq = await self.events.latest_seq(selected["id"])
            else:
                old = connection.fanout_subscriber
                if old is not None:
                    self.fanout.unsubscribe(old)
                    connection.fanout_subscriber = None
                latest_seq = 0
            await connection.send(response("project.deleted", {
                "project_id": project_id,
                "selected": selected,
                "latest_seq": latest_seq,
                "projects": projects,
                "running_turns": self.turns.running_turns(),
            }, request_id=envelope.id))
            return
        if message_type == "project.model":
            requested = self._requested_model(payload.get("model"))
            if requested and requested not in self.config.models.choices:
                raise ValidationError("model is not available")
            project = await self.projects.set_model(
                str(payload.get("project_id", "")), requested
            )
            await connection.send(response("project.snapshot", {
                "selected": project,
                "projects": await self.projects.list(),
                "running_turns": self.turns.running_turns(),
            }, request_id=envelope.id))
            return
        if message_type == "project.select":
            project = await self.projects.get(str(payload.get("project_id", "")))
            latest_seq = await self.events.latest_seq(project["id"])
            self._subscribe_project(connection, project["id"])
            await connection.send(response("project.snapshot", {
                "selected": project,
                "latest_seq": latest_seq,
                "projects": await self.projects.list(),
                "running_turns": self.turns.running_turns(),
            }, request_id=envelope.id))
            return
        if message_type == "event.replay":
            project = await self.projects.get(str(payload.get("project_id", "")))
            self._subscribe_project(connection, project["id"])
            requested_thread = str(payload.get("thread_id") or project.get("codex_thread_id") or "")
            requested_session = str(payload.get("session_id") or project.get("current_session_id") or "")
            if requested_session:
                records = await self.events.replay(
                    project["id"],
                    after_seq=int(payload.get("after_seq", 0)),
                    limit=min(int(payload.get("limit", 200)), 1000),
                    thread_id=requested_thread or None,
                    session_id=requested_session,
                )
            else:
                records = []
            for record in records:
                await connection.send(record.wire(history=True))
            replay_latest = records[-1].project_seq if records else int(payload.get("after_seq", 0))
            await connection.send(response("event.synced", {
                "project_id": project["id"],
                "latest_seq": replay_latest,
                "session_id": requested_session or None,
            }, request_id=envelope.id))
            return
        if message_type == "codex.history":
            project = await self.projects.get(str(payload.get("project_id", "")))
            requested_session = str(payload.get("session_id") or project.get("current_session_id") or "")
            if requested_session:
                session = await self.projects.get_session(project["id"], requested_session)
                requested_thread = str(payload.get("thread_id") or session.get("codex_thread_id") or "")
                if requested_thread:
                    result = await asyncio.to_thread(
                        self.history.list_messages,
                        project,
                        limit=payload.get("limit", 100),
                        thread_id=requested_thread,
                    )
                else:
                    result = {"messages": [], "source": "awaiting-thread-binding"}
            else:
                result = {"messages": [], "source": ""}
            await connection.send(response("codex.history.snapshot", {
                **result,
            }, request_id=envelope.id))
            return
        if message_type == "file.list":
            project = await self.projects.get(str(payload.get("project_id", "")))
            result = await asyncio.to_thread(
                self.files.list,
                project,
                str(payload.get("path", "")),
            )
            await connection.send(response("file.list.snapshot", result, request_id=envelope.id))
            return
        if message_type == "file.upload.start":
            project = await self.projects.get(str(payload.get("project_id", "")))
            relative_path = payload.get("path")
            size = payload.get("size")
            overwrite = payload.get("overwrite", False)
            if not isinstance(relative_path, str):
                raise ValidationError("path must be a string")
            if not isinstance(overwrite, bool):
                raise ValidationError("overwrite must be a boolean")
            upload = await asyncio.to_thread(
                self.files.start_upload,
                project,
                relative_path,
                size,
                overwrite=overwrite,
            )
            self._uploads.setdefault(connection, {})[upload.id] = upload
            await connection.send(response("file.upload.started", {
                "upload_id": upload.id,
                "project_id": project["id"],
                "path": relative_path.replace("\\", "/"),
                "size": upload.size,
                "received_size": 0,
                "next_index": 0,
                "chunk_size": MAX_UPLOAD_CHUNK_SIZE,
            }, request_id=envelope.id))
            return
        if message_type == "file.upload.chunk":
            upload_id = str(payload.get("upload_id", ""))
            upload = self._uploads.get(connection, {}).get(upload_id)
            if upload is None:
                raise ValidationError("upload session does not exist", code="file.upload_not_found")
            try:
                result = await asyncio.to_thread(
                    self.files.append_upload_chunk,
                    upload,
                    payload.get("index"),
                    payload.get("data"),
                )
            except AgentError:
                self._uploads.get(connection, {}).pop(upload_id, None)
                await asyncio.to_thread(self.files.cancel_upload, upload)
                raise
            await connection.send(response("file.upload.progress", {
                "upload_id": upload_id,
                **result,
            }, request_id=envelope.id))
            return
        if message_type == "file.upload.finish":
            upload_id = str(payload.get("upload_id", ""))
            upload = self._uploads.get(connection, {}).get(upload_id)
            if upload is None:
                raise ValidationError("upload session does not exist", code="file.upload_not_found")
            try:
                project = await self.projects.get(str(payload.get("project_id", "")))
                result = await asyncio.to_thread(self.files.finish_upload, project, upload)
            except AgentError:
                self._uploads.get(connection, {}).pop(upload_id, None)
                await asyncio.to_thread(self.files.cancel_upload, upload)
                raise
            self._uploads.get(connection, {}).pop(upload_id, None)
            await connection.send(response("file.upload.completed", result, request_id=envelope.id))
            return
        if message_type == "file.upload.cancel":
            upload_id = str(payload.get("upload_id", ""))
            upload = self._uploads.get(connection, {}).pop(upload_id, None)
            if upload is not None:
                await asyncio.to_thread(self.files.cancel_upload, upload)
            await connection.send(response("ok", {"upload_id": upload_id}, request_id=envelope.id))
            return
        if message_type == "file.read":
            project = await self.projects.get(str(payload.get("project_id", "")))
            max_bytes = payload.get("max_bytes", 320 * 1024)
            try:
                requested_bytes = int(max_bytes)
            except (TypeError, ValueError) as exc:
                raise ValidationError("max_bytes must be a number") from exc
            result = await asyncio.to_thread(
                self.files.read,
                project,
                str(payload.get("path", "")),
                max_bytes=requested_bytes,
            )
            await connection.send(response("file.read.snapshot", result, request_id=envelope.id))
            return
        if message_type == "file.write":
            project = await self.projects.get(str(payload.get("project_id", "")))
            content = payload.get("content")
            if not isinstance(content, str):
                raise ValidationError("content must be a string")
            result = await asyncio.to_thread(
                self.files.write,
                project,
                str(payload.get("path", "")),
                content,
                encoding=str(payload.get("encoding", "utf-8")),
            )
            await connection.send(response("file.write.snapshot", result, request_id=envelope.id))
            return
        if message_type == "file.diff":
            project = await self.projects.get(str(payload.get("project_id", "")))
            result = await asyncio.to_thread(
                self.files.diff,
                project,
                str(payload.get("path", "")),
            )
            await connection.send(response("file.diff.snapshot", result, request_id=envelope.id))
            return
        if message_type == "file.revert":
            project = await self.projects.get(str(payload.get("project_id", "")))
            result = await asyncio.to_thread(
                self.files.revert,
                project,
                str(payload.get("path", "")),
            )
            await connection.send(response("file.revert.snapshot", result, request_id=envelope.id))
            return
        if message_type == "turn.start":
            project = await self.projects.get(str(payload.get("project_id", "")))
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValidationError("prompt is required")
            sandbox_value = payload.get("sandbox", project.get("default_sandbox", "workspace_write"))
            try:
                sandbox = SandboxMode(sandbox_value)
            except ValueError as exc:
                raise ValidationError("invalid sandbox mode") from exc
            model = self._requested_model(
                payload.get("model"),
                fallback=str(project.get("model") or self.config.models.default_model),
            )
            turn_id = await self.turns.start(
                project,
                prompt,
                sandbox,
                model,
                external=bool(payload.get("external", False)),
                reasoning_effort=str(project.get("reasoning_effort") or "") or None,
                session_id=str(payload.get("session_id") or "") or None,
            )
            await connection.send(response("ok", {
                "turn_id": turn_id,
                **await self.turns.turn_identity(turn_id),
                "selected": await self.projects.get(project["id"]),
                "projects": await self.projects.list(),
                "running_turns": self.turns.running_turns(),
            }, request_id=envelope.id))
            return
        if message_type == "turn.interrupt":
            project = await self.projects.get(str(payload.get("project_id", "")))
            turn_id = payload.get("turn_id")
            interrupted = await self.turns.interrupt(
                project["id"],
                str(turn_id) if turn_id else None,
                session_id=str(payload.get("session_id") or "") or None,
            )
            await connection.send(response("ok", {"turn_id": interrupted}, request_id=envelope.id))
            return
        if message_type == "thread.resume":
            project = await self.projects.get(str(payload.get("project_id", "")))
            thread_id = project.get("codex_thread_id")
            if not thread_id:
                await connection.send(response("ok", {"thread_id": None}, request_id=envelope.id))
                return
            handle = await self.gateway.resume_thread(
                thread_id, Path(project["normalized_path"])
            )
            await connection.send(response("ok", {"thread_id": handle.thread_id}, request_id=envelope.id))
            return
        if message_type == "thread.new":
            project = await self.projects.new_thread(str(payload.get("project_id", "")))
            await connection.send(response("thread.snapshot", {
                "selected": project,
                "latest_seq": await self.events.latest_seq(project["id"]),
                "projects": await self.projects.list(),
            }, request_id=envelope.id))
            return
        if message_type == "thread.select":
            project = await self.projects.get(str(payload.get("project_id", "")))
            session_id = str(payload.get("session_id") or "").strip()
            if session_id:
                selected = await self.projects.select_session(
                    project["id"],
                    session_id,
                    restore=True,
                )
            else:
                session_id = ""
                thread_id = str(payload.get("thread_id", "")).strip()
                if not thread_id:
                    raise ValidationError("session_id or thread_id is required")
                record = await asyncio.to_thread(
                    self.sessions.find_session,
                    thread_id,
                    Path(project["normalized_path"]),
                )
                if record is None:
                    raise ValidationError("Codex session not found")
                sessions = await self.projects.list_sessions(project["id"], status="active")
                sessions.extend(
                    await self.projects.list_sessions(project["id"], status="archived")
                )
                matching = [
                    row for row in sessions
                    if str(row.get("codex_thread_id") or "") == thread_id
                ]
                if matching:
                    session_id = str(matching[0]["id"])
                    selected = await self.projects.select_session(
                        project["id"],
                        session_id,
                        restore=True,
                    )
                else:
                    session_id = await self.projects.create_session_from_prompt(
                        project["id"],
                        str(record.title or "Codex 会话"),
                        model=str(record.model or "") or None,
                        reasoning_effort=str(record.reasoning_effort or "") or None,
                        thread_id=thread_id,
                    )
                    selected = await self.projects.select_session(
                        project["id"], session_id, restore=False
                    )
            await connection.send(response("thread.snapshot", {
                "selected": selected,
                "latest_seq": await self.events.latest_seq(project["id"]),
                "projects": await self.projects.list(),
            }, request_id=envelope.id))
            return
        if message_type in ("thread.fork", "thread.side"):
            project = await self.projects.get(str(payload.get("project_id", "")))
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValidationError("prompt is required")
            sandbox_value = payload.get("sandbox", project.get("default_sandbox", "workspace_write"))
            try:
                sandbox = SandboxMode(sandbox_value)
            except ValueError as exc:
                raise ValidationError("invalid sandbox mode") from exc
            model = self._requested_model(
                payload.get("model"),
                fallback=str(project.get("model") or self.config.models.default_model),
            )
            fork_from = str(payload.get("thread_id") or project.get("codex_thread_id") or "")
            if not fork_from:
                raise ValidationError("there is no Codex session to fork")
            fork_session = None
            if message_type == "thread.fork":
                fork_session = await self.projects.new_thread(project["id"])
            turn_id = await self.turns.start(
                fork_session or project,
                prompt.strip(),
                sandbox,
                model,
                external=bool(payload.get("external", False)),
                reasoning_effort=str(project.get("reasoning_effort") or "") or None,
                fork_from=fork_from,
                ephemeral=message_type == "thread.side",
                session_id=(str(fork_session.get("current_session_id") or "") or None)
                    if fork_session else None,
            )
            await connection.send(response("ok", {"turn_id": turn_id}, request_id=envelope.id))
            return
        if message_type == "thread.compress":
            project = await self.projects.get(str(payload.get("project_id", "")))
            turn_id = await self._start_special_turn(
                project,
                payload,
                prompt=(
                    "Compress this conversation into a compact handoff summary. "
                    "Preserve the current goal, completed work, open questions, "
                    "important files, commands, and next steps. Keep it concise."
                ),
            )
            await connection.send(response("ok", {"turn_id": turn_id}, request_id=envelope.id))
            return
        if message_type == "thread.archive":
            project = await self.projects.get(str(payload.get("project_id", "")))
            session_id = str(
                payload.get("session_id")
                or project.get("current_session_id")
                or ""
            ).strip()
            if not session_id:
                raise ValidationError("session_id is required")
            session = await self.projects.get_session(project["id"], session_id)
            thread_id = str(session.get("codex_thread_id") or "")
            if thread_id and hasattr(self.gateway, "run_cli"):
                try:
                    await self._run_codex_cli(["archive", thread_id], timeout=30)
                except Exception:
                    logger.exception("Failed to archive Codex thread %s", thread_id)
            await self.projects.archive_session(project["id"], session_id)
            selected = await self.projects.get(project["id"])
            await connection.send(response("thread.archived", {
                "project_id": project["id"],
                "session_id": session_id,
                "thread_id": thread_id or None,
                "selected": selected,
                "projects": await self.projects.list(),
            }, request_id=envelope.id))
            return
        if message_type == "thread.delete":
            project = await self.projects.get(str(payload.get("project_id", "")))
            session_id = str(
                payload.get("session_id")
                or project.get("current_session_id")
                or ""
            ).strip()
            if not session_id:
                raise ValidationError("session_id is required")
            deleted_current = session_id == str(project.get("current_session_id") or "")
            session = await self.projects.delete_session(project["id"], session_id)
            selected = await self.projects.get(project["id"])
            await connection.send(response("thread.deleted", {
                "project_id": project["id"],
                "session_id": session_id,
                "deleted_current": deleted_current,
                "replacement_session_id": None,
                "selected": selected,
                "projects": await self.projects.list(),
            }, request_id=envelope.id))
            return
        if message_type == "thread.status":
            project = await self.projects.get(str(payload.get("project_id", "")))
            result = await asyncio.to_thread(
                self.sessions.status,
                project,
                str(payload.get("thread_id") or "") or None,
            )
            await connection.send(response("thread.status.snapshot", result, request_id=envelope.id))
            return
        if message_type == "thread.sessions":
            project = await self.projects.get(str(payload.get("project_id", "")))
            result = await asyncio.to_thread(
                self.sessions.database_snapshot,
                project,
                await self.projects.list_sessions(project["id"], status="active"),
                await self.projects.list_sessions(project["id"], status="archived"),
                self.turns.running_turns(),
            )
            await connection.send(response("thread.sessions.snapshot", result, request_id=envelope.id))
            return
        if message_type == "thread.goal.set":
            goal = payload.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                raise ValidationError("goal is required")
            project = await self.projects.set_goal(
                str(payload.get("project_id", "")), goal.strip()
            )
            await connection.send(response("thread.goal.snapshot", {
                "project_id": project["id"],
                "goal": project.get("goal"),
            }, request_id=envelope.id))
            return
        if message_type == "thread.goal.get":
            project = await self.projects.get(str(payload.get("project_id", "")))
            await connection.send(response("thread.goal.snapshot", {
                "project_id": project["id"],
                "goal": project.get("goal"),
            }, request_id=envelope.id))
            return
        if message_type == "thread.reasoning.set":
            effort = str(payload.get("effort") or "default").strip().lower()
            if effort not in {"default", "minimal", "low", "medium", "high", "xhigh"}:
                raise ValidationError("unsupported reasoning effort")
            project = await self.projects.set_reasoning_effort(
                str(payload.get("project_id", "")),
                None if effort == "default" else effort,
            )
            await connection.send(response("thread.reasoning.snapshot", {
                "project_id": project["id"],
                "reasoning_effort": project.get("reasoning_effort") or "default",
            }, request_id=envelope.id))
            return
        if message_type == "thread.reasoning.get":
            project = await self.projects.get(str(payload.get("project_id", "")))
            await connection.send(response("thread.reasoning.snapshot", {
                "project_id": project["id"],
                "reasoning_effort": project.get("reasoning_effort") or "default",
            }, request_id=envelope.id))
            return
        if message_type == "mcp.list":
            returncode, stdout, stderr = await self._run_codex_cli(
                ["mcp", "list", "--json"], timeout=30
            )
            await connection.send(response(
                "mcp.snapshot",
                self._parse_mcp_list(stdout, stderr),
                request_id=envelope.id,
            ))
            return
        if message_type == "metrics.subscribe":
            subscriber = self.metrics.subscribe(connection.device_id)
            self._monitor_tasks.setdefault(connection, []).append(asyncio.create_task(
                self._pump_monitor(connection, subscriber.queue)
            ))
            await connection.send(response("ok", {}, request_id=envelope.id))
            return
        if message_type == "metrics.unsubscribe":
            self.metrics.unsubscribe(connection.device_id)
            await connection.send(response("ok", {}, request_id=envelope.id))
            return
        if message_type == "screen.subscribe":
            subscriber = self.screen.subscribe(
                connection.device_id,
                payload.get("fps"),
                payload.get("max_width"),
            )
            self._monitor_tasks.setdefault(connection, []).append(asyncio.create_task(
                self._pump_monitor(connection, subscriber.queue)
            ))
            await connection.send(response("ok", {}, request_id=envelope.id))
            return
        if message_type == "screen.unsubscribe":
            self.screen.unsubscribe(connection.device_id)
            await connection.send(response("ok", {}, request_id=envelope.id))
            return
        if message_type == "screen.input":
            try:
                result = await self.system_input.handle(payload)
            except (InputValidationError, ValueError) as exc:
                raise ValidationError(str(exc)) from exc
            except Exception as exc:
                raise ValidationError(
                    "PC screen input is unavailable. Install the desktop extra with: pip install -e \".[desktop]\""
                ) from exc
            await connection.send(response("ok", result, request_id=envelope.id))
            return
        if message_type == "terminal.start":
            if connection in self._terminal_sessions:
                raise ValidationError(
                    "a terminal session is already running",
                    code="terminal.already_running",
                )
            project = await self.projects.get(str(payload.get("project_id", "")))
            cols = self._terminal_dimension(payload.get("cols", 80), MIN_TERMINAL_COLS, MAX_TERMINAL_COLS)
            rows = self._terminal_dimension(payload.get("rows", 24), MIN_TERMINAL_ROWS, MAX_TERMINAL_ROWS)
            requested_shell = payload.get("shell")
            if requested_shell is not None and not isinstance(requested_shell, str):
                raise ValidationError("shell must be a string", code="terminal.invalid_shell")
            session = await self.terminal.create_session(
                project["normalized_path"],
                cols=cols,
                rows=rows,
                shell=requested_shell,
            )
            self._terminal_sessions[connection] = session
            self._terminal_tasks[connection] = asyncio.create_task(
                self._pump_terminal(connection, session),
                name=f"terminal-pump-{session.id}",
            )
            await connection.send(response("terminal.ready", {
                "session_id": session.id,
                "cwd": str(session.cwd),
                "shell": session.shell,
                "mode": session.mode,
                "cols": session.cols,
                "rows": session.rows,
                "line_ending": "\r\n" if connection and session.mode == "pipe" and _is_windows() else "\n",
                "pty": session.mode == "pty",
            }, request_id=envelope.id))
            return
        if message_type == "terminal.input":
            session = self._terminal_sessions.get(connection)
            if session is None:
                raise ValidationError("terminal session is not running", code="terminal.not_running")
            data = payload.get("data")
            if not isinstance(data, str):
                raise ValidationError("data must be a string", code="terminal.invalid_input")
            await session.write(data)
            await connection.send(response("ok", {"session_id": session.id}, request_id=envelope.id))
            return
        if message_type == "terminal.resize":
            session = self._terminal_sessions.get(connection)
            if session is None:
                raise ValidationError("terminal session is not running", code="terminal.not_running")
            cols = self._terminal_dimension(payload.get("cols"), MIN_TERMINAL_COLS, MAX_TERMINAL_COLS)
            rows = self._terminal_dimension(payload.get("rows"), MIN_TERMINAL_ROWS, MAX_TERMINAL_ROWS)
            await session.resize(cols, rows)
            await connection.send(response("ok", {
                "session_id": session.id,
                "cols": session.cols,
                "rows": session.rows,
            }, request_id=envelope.id))
            return
        if message_type == "terminal.close":
            session = self._terminal_sessions.pop(connection, None)
            terminal_task = self._terminal_tasks.pop(connection, None)
            if session is None:
                await connection.send(response("ok", {}, request_id=envelope.id))
                return
            await session.close()
            if terminal_task is not None and not terminal_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(terminal_task), timeout=1)
                except TimeoutError:
                    terminal_task.cancel()
                    await asyncio.gather(terminal_task, return_exceptions=True)
            await connection.send(response("ok", {"session_id": session.id}, request_id=envelope.id))
            return
        raise ValidationError("unknown message type")

    def _requested_model(self, value: Any, *, fallback: str | None = None) -> str | None:
        model = str(value if value is not None else fallback or "").strip()
        if not model or model.lower() == "default":
            return None
        if len(model) > 120:
            raise ValidationError("model name is too long")
        return model

    async def _start_special_turn(self, project: dict, payload: dict, *, prompt: str) -> str:
        sandbox_value = payload.get("sandbox", project.get("default_sandbox", "workspace_write"))
        try:
            sandbox = SandboxMode(sandbox_value)
        except ValueError as exc:
            raise ValidationError("invalid sandbox mode") from exc
        model = self._requested_model(
            payload.get("model"),
            fallback=str(project.get("model") or self.config.models.default_model),
        )
        return await self.turns.start(
            project,
            prompt,
            sandbox,
            model,
            external=bool(payload.get("external", False)),
            reasoning_effort=str(project.get("reasoning_effort") or "") or None,
        )

    async def _run_codex_cli(self, args: list[str], *, timeout: float) -> tuple[int, str, str]:
        if not hasattr(self.gateway, "run_cli"):
            raise ValidationError("当前 Codex 网关不支持该操作")
        try:
            return await self.gateway.run_cli(args, timeout=timeout)
        except TimeoutError as exc:
            raise ValidationError("Codex CLI command timed out") from exc

    @staticmethod
    def _parse_mcp_list(stdout: str, stderr: str) -> dict[str, Any]:
        text = stdout.strip()
        try:
            parsed = json.loads(text) if text else []
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            rows = [
                {
                    "name": str(item.get("name", "")),
                    "detail": str(
                        item.get("disabled_reason")
                        or item.get("auth_status")
                        or item.get("transport", {}).get("type", "")
                        or ""
                    ),
                    "enabled": bool(item.get("enabled", False)),
                }
                for item in parsed
                if isinstance(item, dict) and item.get("name")
            ]
            if rows:
                return {"available": True, "servers": rows, "detail": ""}
            if text:
                return {
                    "available": True,
                    "servers": [],
                    "detail": "No MCP servers configured",
                }
        rows = []
        if text:
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                parts = line.split(maxsplit=1)
                if parts[0].lower() in {"mcp", "servers", "name"}:
                    continue
                name = parts[0]
                detail = parts[1] if len(parts) > 1 else ""
                enabled = not detail.lower().startswith("disabled")
                rows.append({"name": name, "detail": detail, "enabled": enabled})
        if not rows:
            detail = stderr.strip() or text or "No MCP servers configured"
            return {"available": False, "servers": [], "detail": detail}
        return {"available": True, "servers": rows, "detail": ""}

    async def _pair(self, websocket: Any, envelope: Envelope) -> None:
        code = envelope.payload.get("code")
        device_name = envelope.payload.get("device_name", "Mobile device")
        if not isinstance(code, str) or not code:
            raise PairingError("pairing code is required")
        try:
            device_id, token = await self.pairing.wait_for_approval(code, str(device_name))
        except (ValueError, TimeoutError) as exc:
            raise PairingError(str(exc)) from exc
        await websocket.send(json.dumps(response("pair.approved", {
            "device_id": device_id,
            "device_token": token,
        }, request_id=envelope.id).to_dict(), ensure_ascii=False))

    def _subscribe_project(self, connection: Connection, project_id: str) -> None:
        old = connection.fanout_subscriber
        if (
            old is not None
            and getattr(old, "project_id", None) == project_id
            and connection.fanout_queue is not None
        ):
            return
        if connection.fanout_queue is None:
            connection.fanout_queue = self.fanout.create_queue()
        else:
            self.fanout.unsubscribe(old)
        connection.fanout_subscriber = self.fanout.subscribe_with_queue(
            connection.device_id,
            project_id,
            connection.fanout_queue,
        )

    async def _subscribe_default_project(self, connection: Connection) -> None:
        projects = await self.projects.list()
        if projects:
            self._subscribe_project(connection, str(projects[0]["id"]))

    async def _pump_events(self, connection: Connection) -> None:
        while True:
            subscriber = connection.fanout_subscriber
            if subscriber is None:
                await asyncio.sleep(0.05)
                continue
            message = await connection.fanout_queue.get()
            await connection.send(message)

    async def _pump_monitor(self, connection: Connection, queue: asyncio.Queue) -> None:
        while True:
            message = await queue.get()
            await connection.websocket.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def _pump_terminal(self, connection: Connection, session: TerminalSession) -> None:
        pending = ""
        pending_size = 0

        async def flush() -> None:
            nonlocal pending
            nonlocal pending_size
            if not pending:
                return
            data = pending
            pending = ""
            pending_size = 0
            await connection.send(response("terminal.output", {
                "session_id": session.id,
                "data": data,
            }))

        try:
            while True:
                event_type, value = await session.output_queue.get()
                if event_type == "output":
                    chunk = str(value)
                    pending += chunk
                    pending_size += len(chunk.encode("utf-8"))
                    if pending_size < 16 * 1024:
                        await asyncio.sleep(0.016)
                    await flush()
                    continue
                await flush()
                await connection.send(response("terminal.exit", {
                    "session_id": session.id,
                    "exit_code": int(value),
                }))
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            current_task = asyncio.current_task()
            if self._terminal_tasks.get(connection) is current_task:
                self._terminal_tasks.pop(connection, None)
            if self._terminal_sessions.get(connection) is session:
                self._terminal_sessions.pop(connection, None)
            await session.close()

    @staticmethod
    def _terminal_dimension(value: Any, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError("terminal dimensions must be numbers") from exc
        if number < minimum or number > maximum:
            raise ValidationError(
                f"terminal dimension must be between {minimum} and {maximum}"
            )
        return number

    async def _send_raw_error(self, websocket: Any, exc: Exception) -> None:
        await websocket.send(json.dumps(response("error", error_payload(exc)).to_dict()))


def _iso_now() -> str:
    from ..auth.tokens import iso_now

    return iso_now()


def _is_windows() -> bool:
    import os

    return os.name == "nt"
