from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Protocol


class SandboxMode(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


@dataclass(slots=True)
class ThreadHandle:
    thread_id: str
    cwd: Path
    native: Any = None
    closed: bool = False


@dataclass(slots=True)
class CodexEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


class CodexGateway(Protocol):
    async def start_thread(self, cwd: Path, sandbox: SandboxMode) -> ThreadHandle: ...

    async def resume_thread(self, thread_id: str, cwd: Path) -> ThreadHandle: ...

    async def run_turn(
        self,
        handle: ThreadHandle,
        prompt: str,
        *,
        sandbox: SandboxMode,
        model: str | None = None,
        external: bool = False,
    ) -> AsyncIterator[CodexEvent]: ...

    async def interrupt(self, handle: ThreadHandle) -> None: ...

    async def close(self, handle: ThreadHandle) -> None: ...


class FakeCodexGateway:
    """Deterministic gateway for protocol and lifecycle tests."""

    def __init__(self) -> None:
        self.started: list[tuple[Path, SandboxMode]] = []
        self.resumed: list[str] = []
        self.prompts: list[str] = []
        self.interrupted: list[str] = []
        self.models: list[str | None] = []
        self.active: dict[str, asyncio.Event] = {}

    async def start_thread(self, cwd: Path, sandbox: SandboxMode) -> ThreadHandle:
        thread_id = f"thr_fake_{len(self.started) + 1:04d}"
        self.started.append((cwd, sandbox))
        return ThreadHandle(thread_id=thread_id, cwd=cwd)

    async def resume_thread(self, thread_id: str, cwd: Path) -> ThreadHandle:
        self.resumed.append(thread_id)
        return ThreadHandle(thread_id=thread_id, cwd=cwd)

    async def run_turn(
        self,
        handle: ThreadHandle,
        prompt: str,
        *,
        sandbox: SandboxMode,
        model: str | None = None,
        external: bool = False,
        reasoning_effort: str | None = None,
        fork_from: str | None = None,
        ephemeral: bool = False,
    ) -> AsyncIterator[CodexEvent]:
        self.prompts.append(prompt)
        done = asyncio.Event()
        self.active[handle.thread_id] = done
        try:
            self.models.append(model)
            yield CodexEvent("turn.started", {
                "thread_id": handle.thread_id,
                "model": model,
            })
            yield CodexEvent("item.started", {
                "item": {"type": "command_execution", "command": "pytest"}
            })
            try:
                await asyncio.wait_for(done.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass
            interrupted = done.is_set()
            yield CodexEvent("item.completed", {"item": {
                "type": "agent_message",
                "text": "Ran pytest. All checks passed.",
            }})
            yield CodexEvent("item.completed", {"item": {
                "type": "command_execution",
                "command": "pytest",
                "status": "interrupted" if interrupted else "completed",
                "exit_code": 0,
            }})
            if interrupted:
                yield CodexEvent("turn.interrupted", {"thread_id": handle.thread_id})
            else:
                yield CodexEvent("turn.completed", {"usage": {"tokens": 18}})
        finally:
            self.active.pop(handle.thread_id, None)

    async def interrupt(self, handle: ThreadHandle) -> None:
        self.interrupted.append(handle.thread_id)
        event = self.active.get(handle.thread_id)
        if event:
            event.set()

    async def close(self, handle: ThreadHandle) -> None:
        handle.closed = True
