from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

from ..auth.tokens import iso_now, new_id
from ..events.fanout import EventFanout
from ..events.store import EventStore
from ..protocol.errors import TurnBusyError, TurnNotFoundError
from ..storage.database import Database
from ..projects.manager import ProjectService
from .titles import SessionTitleService
from .gateway import CodexGateway, SandboxMode, ThreadHandle


class TurnStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    UNKNOWN = "unknown"


def _normalize_event(source) -> dict:
    if hasattr(source, "type") and hasattr(source, "payload") and not isinstance(source, dict):
        payload = source.payload if isinstance(source.payload, dict) else {"value": source.payload}
        return {"type": str(source.type), **payload}
    if isinstance(source, dict):
        return source
    if hasattr(source, "model_dump"):
        value = source.model_dump()
    elif hasattr(source, "to_dict"):
        value = source.to_dict()
    elif hasattr(source, "__dict__"):
        value = dict(source.__dict__)
    else:
        value = {"repr": repr(source)}
    if isinstance(value, dict):
        value.setdefault("type", getattr(source, "type", type(source).__name__))
        return value
    return {"type": getattr(source, "type", type(source).__name__), "value": value}


@dataclass(slots=True)
class RunningTurn:
    id: str
    project_id: str
    session_id: str | None
    handle: ThreadHandle
    task: asyncio.Task[None]
    interrupt_event: asyncio.Event


class TurnSupervisor:
    def __init__(
        self,
        database: Database,
        gateway: CodexGateway,
        events: EventStore,
        fanout: EventFanout,
        projects: ProjectService,
        titles: SessionTitleService,
        *,
        max_running: int = 2,
        timeout_sec: float = 1800,
    ):
        self.db = database
        self.gateway = gateway
        self.events = events
        self.fanout = fanout
        self.projects = projects
        self.titles = titles
        self.max_running = max_running
        self.timeout_sec = timeout_sec
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._running: dict[str, RunningTurn] = {}

    def _running_key(self, project_id: str, session_id: str | None, *, ephemeral: bool) -> str:
        if ephemeral:
            return f"{project_id}:__ephemeral__"
        return f"{project_id}:{session_id or '__project__'}"

    def _turn_lock(self, running_key: str) -> asyncio.Lock:
        return self._turn_locks.setdefault(running_key, asyncio.Lock())

    def running_turns(self) -> list[dict[str, str | None]]:
        """Return all in-flight turns for project and session list snapshots."""

        return [
            {
                "turn_id": running.id,
                "project_id": running.project_id,
                "session_id": running.session_id,
                "thread_id": running.handle.thread_id or None,
            }
            for running in self._running.values()
        ]

    async def turn_identity(self, turn_id: str) -> dict[str, str | None]:
        row = await self.db.fetch_one(
            """SELECT project_id, session_id, thread_id
               FROM turns WHERE id = ?""",
            (turn_id,),
        )
        if row is None:
            return {"project_id": None, "session_id": None, "thread_id": None}
        return {
            "project_id": str(row["project_id"] or ""),
            "session_id": row["session_id"],
            "thread_id": row["thread_id"],
        }

    async def _session_thread_id(self, project: dict, session_id: str | None) -> str:
        if not session_id:
            return str(project.get("codex_thread_id") or "")
        return await self.projects.get_session_thread_id(
            project["id"], session_id
        )

    async def start(
        self,
        project: dict,
        prompt: str,
        sandbox: SandboxMode,
        model: str | None = None,
        *,
        external: bool = False,
        reasoning_effort: str | None = None,
        fork_from: str | None = None,
        ephemeral: bool = False,
        session_id: str | None = None,
    ) -> str:
        project_id = project["id"]
        prompt = prompt.strip() or "未命名对话"
        requested_session_id = session_id or project.get("current_session_id")
        running_key = self._running_key(
            project_id,
            requested_session_id,
            ephemeral=ephemeral,
        )
        if len(self._running) >= self.max_running:
            raise TurnBusyError("global turn limit reached")

        async with self._turn_lock(running_key):
            if running_key in self._running:
                raise TurnBusyError("session already has a running turn")

            active_session_id = None if ephemeral else requested_session_id
            if not ephemeral and not active_session_id:
                active_session_id = await self.projects.create_session_from_prompt(
                    project_id,
                    prompt,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                running_key = self._running_key(
                    project_id,
                    active_session_id,
                    ephemeral=ephemeral,
                )

            handle = await self._ensure_thread(
                project,
                sandbox,
                fork_from,
                ephemeral,
                active_session_id,
            )
            if not ephemeral:
                requested_session_id = active_session_id
            turn_id = new_id("turn")
            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            await self.db.execute(
                """INSERT INTO turns(
                     id, project_id, session_id, thread_id, prompt_sha256, sandbox,
                     model, status, started_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    turn_id,
                    project_id,
                    active_session_id,
                    handle.thread_id or None,
                    prompt_hash,
                    sandbox.value,
                    model,
                    TurnStatus.RUNNING.value,
                    iso_now(),
                    iso_now(),
                ),
            )
            if not ephemeral:
                await self._bind_thread(
                    project_id,
                    active_session_id,
                    handle.thread_id,
                    only_project=not handle.thread_id,
                )
            interrupt_event = asyncio.Event()
            task = asyncio.create_task(
                self._run(
                    project_id,
                    active_session_id,
                    handle,
                    turn_id,
                    prompt,
                    sandbox,
                    model,
                    external,
                    reasoning_effort,
                    fork_from,
                    ephemeral,
                    interrupt_event,
                ),
                name=f"codex-turn-{turn_id}",
            )
            self._running[running_key] = RunningTurn(
                id=turn_id,
                project_id=project_id,
                session_id=active_session_id,
                handle=handle,
                task=task,
                interrupt_event=interrupt_event,
            )

        return turn_id

    async def _bind_thread(
        self,
        project_id: str,
        session_id: str | None,
        thread_id: str | None,
        *,
        only_project: bool = False,
    ) -> None:
        if not thread_id:
            return
        now = iso_now()
        if session_id and not only_project:
            await self.db.execute(
                """UPDATE codex_sessions
                   SET codex_thread_id = ?, updated_at = ?
                   WHERE id = ? AND project_id = ?""",
                (thread_id, now, session_id, project_id),
            )
        await self.db.execute(
            """UPDATE projects
               SET codex_thread_id = ?, last_active_at = ?
               WHERE id = ? AND current_session_id = ?""",
            (thread_id, now, project_id, session_id),
        )

    async def _ensure_thread(
        self,
        project: dict,
        sandbox: SandboxMode,
        fork_from: str | None = None,
        ephemeral: bool = False,
        session_id: str | None = None,
    ) -> ThreadHandle:
        cwd = Path(project["normalized_path"])
        if ephemeral:
            return await self.gateway.start_thread(cwd, sandbox)
        thread_id = await self._session_thread_id(project, session_id)
        if fork_from:
            return await self.gateway.start_thread(cwd, sandbox)
        if thread_id:
            return await self.gateway.resume_thread(thread_id, cwd)
        return await self.gateway.start_thread(cwd, sandbox)

    async def _run(
        self,
        project_id: str,
        session_id: str | None,
        handle: ThreadHandle,
        turn_id: str,
        prompt: str,
        sandbox: SandboxMode,
        model: str | None,
        external: bool,
        reasoning_effort: str | None,
        fork_from: str | None,
        ephemeral: bool,
        interrupt_event: asyncio.Event,
    ) -> None:
        await self._append_agent_event(
            project_id,
            session_id,
            handle,
            turn_id,
            "turn.started",
            {"turn_id": turn_id, "sandbox": sandbox.value, "model": model},
        )
        final_payload: dict | None = None
        status = TurnStatus.FAILED
        error: str | None = None
        try:
            async for source in self._bounded_events(
                self.gateway.run_turn(
                    handle,
                    prompt,
                    sandbox=sandbox,
                    model=model,
                    external=external,
                    reasoning_effort=reasoning_effort,
                    fork_from=fork_from,
                    ephemeral=ephemeral,
                ),
                handle,
            ):
                if interrupt_event.is_set():
                    await self.gateway.interrupt(handle)
                payload = _normalize_event(source)
                if payload.get("type") == "thread.started":
                    thread_id = str(payload.get("thread_id") or "")
                    if thread_id and handle.thread_id != thread_id:
                        handle.thread_id = thread_id
                        if not ephemeral:
                            await self._bind_thread(
                                project_id,
                                session_id,
                                thread_id,
                            )
                            await self.db.execute(
                                "UPDATE turns SET thread_id = ? WHERE id = ?",
                                (thread_id, turn_id),
                            )
                record = await self.events.append(
                    project_id,
                    "codex.event",
                    str(payload.get("type", "unknown")),
                    payload,
                    thread_id=handle.thread_id or None,
                    session_id=session_id,
                    turn_id=turn_id,
                )
                self.fanout.publish(project_id, record.wire())
                if payload.get("type") in {
                    "turn.completed",
                    "turn.failed",
                    "turn.interrupted",
                }:
                    final_payload = payload
                    break

            if final_payload is None:
                final_payload = {
                    "type": "turn.failed",
                    "message": "event stream ended without completion",
                }
            event_type = str(final_payload.get("type"))
            status = {
                "turn.completed": TurnStatus.COMPLETED,
                "turn.interrupted": TurnStatus.INTERRUPTED,
                "turn.failed": TurnStatus.FAILED,
            }.get(event_type, TurnStatus.FAILED)
            if interrupt_event.is_set() and status is TurnStatus.COMPLETED:
                status = TurnStatus.INTERRUPTED
                final_payload = {
                    "type": "turn.interrupted",
                    "message": "turn interrupted",
                }
            if status is TurnStatus.FAILED:
                error = str(final_payload.get("message", "turn failed"))

            await self.db.execute(
                """UPDATE turns SET status = ?, error = ?, completed_at = ?
                   WHERE id = ?""",
                (status.value, error, iso_now(), turn_id),
            )
            await self.touch_session(session_id)
            if not ephemeral and session_id:
                row = await self.db.fetch_one(
                    "SELECT COUNT(*) AS value FROM turns WHERE session_id = ?",
                    (session_id,),
                )
                if row and int(row["value"]) <= 1:
                    self.titles.generate(
                        await self.projects.get(project_id),
                        session_id,
                        prompt,
                    )
            await self._append_agent_event(
                project_id,
                session_id,
                handle,
                turn_id,
                "turn.status",
                {
                    "turn_id": turn_id,
                    "status": status.value,
                    **({"message": error} if error else {}),
                },
            )
        except Exception as exc:
            status = TurnStatus.FAILED
            error = str(exc)
            with suppress(Exception):
                await self.gateway.interrupt(handle)
            await self.db.execute(
                """UPDATE turns SET status = ?, error = ?, completed_at = ?
                   WHERE id = ?""",
                (status.value, error, iso_now(), turn_id),
            )
            record = await self.events.append(
                project_id,
                "codex.event",
                "turn.failed",
                {"type": "turn.failed", "message": error},
                thread_id=handle.thread_id or None,
                session_id=session_id,
                turn_id=turn_id,
            )
            self.fanout.publish(project_id, record.wire())
        finally:
            self._running.pop(
                self._running_key(
                    project_id,
                    session_id,
                    ephemeral=ephemeral,
                ),
                None,
            )

    async def _bounded_events(
        self,
        events: AsyncIterator[Any],
        handle: Any,
    ) -> AsyncIterator[Any]:
        """Bound each gateway yield so a hung child cannot occupy a slot forever."""

        if not self.timeout_sec or self.timeout_sec <= 0:
            async for source in events:
                yield source
            await events.aclose()
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_sec
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RuntimeError("turn timed out")
                try:
                    source = await asyncio.wait_for(anext(events), remaining)
                except (asyncio.TimeoutError, TimeoutError):
                    raise RuntimeError("turn timed out") from None
                yield source
        finally:
            with suppress(Exception):
                await self.gateway.interrupt(handle)
            try:
                await asyncio.wait_for(events.aclose(), timeout=0.25)
            except Exception:
                with suppress(Exception):
                    await self.gateway.interrupt(handle)

    async def _append_agent_event(
        self,
        project_id: str,
        session_id: str | None,
        handle: ThreadHandle,
        turn_id: str,
        code: str,
        detail: dict,
    ) -> None:
        record = await self.events.append(
            project_id,
            "agent.event",
            code,
            {"level": "info", "code": code, **detail},
            thread_id=handle.thread_id or None,
            session_id=session_id,
            turn_id=turn_id,
        )
        self.fanout.publish(project_id, record.wire())

    async def touch_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        await self.db.execute(
            "UPDATE codex_sessions SET updated_at = ? WHERE id = ? AND status = 'active'",
            (iso_now(), session_id),
        )

    async def interrupt(
        self,
        project_id: str,
        turn_id: str | None = None,
        *,
        session_id: str | None = None,
    ) -> str:
        if session_id:
            running = self._running.get(
                self._running_key(project_id, session_id, ephemeral=False)
            )
            if running is None or (turn_id and running.id != turn_id):
                raise TurnNotFoundError("running turn not found")
        else:
            running = next(
                (
                    item
                    for item in self._running.values()
                    if item.project_id == project_id
                    and (not turn_id or item.id == turn_id)
                ),
                None,
            )
            if running is None:
                raise TurnNotFoundError("running turn not found")
        if running is None or (turn_id and running.id != turn_id):
            raise TurnNotFoundError("running turn not found")
        running.interrupt_event.set()
        await self.gateway.interrupt(running.handle)
        return running.id

    async def recover(self) -> None:
        await self.db.execute(
            """UPDATE turns SET status = ?, error = ?, completed_at = ?
               WHERE status = ?""",
            (
                TurnStatus.UNKNOWN.value,
                "agent restarted while turn was running",
                iso_now(),
                TurnStatus.RUNNING.value,
            ),
        )

    async def shutdown(self) -> None:
        for running in list(self._running.values()):
            running.interrupt_event.set()
            await self.gateway.interrupt(running.handle)
        tasks = [running.task for running in self._running.values()]
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        await self.titles.shutdown()
