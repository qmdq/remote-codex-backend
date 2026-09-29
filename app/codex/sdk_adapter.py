from __future__ import annotations

import inspect
from importlib import import_module
from pathlib import Path
from typing import Any, AsyncIterator

from .gateway import CodexEvent, SandboxMode, ThreadHandle


class OpenAICodexAdapter:
    """Thin runtime adapter for the installed ``openai-codex`` package.

    The public Python surface has been changing while the product evolves.
    Discovery happens at call time instead of guessing constructor or cwd
    argument names at import time.
    """

    def __init__(self) -> None:
        self._sdk: Any | None = None

    def _load(self) -> Any:
        if self._sdk is None:
            try:
                self._sdk = import_module("codex")
            except ImportError as exc:
                raise RuntimeError(
                    "openai-codex is not installed in this Python environment"
                ) from exc
        return self._sdk

    def diagnostics(self) -> dict[str, Any]:
        try:
            sdk = self._load()
        except Exception as exc:
            return {"available": False, "error": str(exc)}
        names = [name for name in dir(sdk) if any(part in name.lower() for part in ("thread", "turn", "sandbox", "login"))]
        return {
            "available": True,
            "module": sdk.__name__,
            "version": getattr(sdk, "__version__", None),
            "symbols": names,
        }

    @staticmethod
    def _call(target: Any, args: tuple = (), kwargs: dict | None = None) -> Any:
        result = target(*args, **(kwargs or {}))
        if inspect.isawaitable(result):
            return result
        return result

    def _start_factory(self) -> Any:
        sdk = self._load()
        for name in ("thread_start", "start_thread", "Thread"):
            candidate = getattr(sdk, name, None)
            if candidate is not None:
                return candidate
        raise RuntimeError("SDK thread start API not found")

    def _resume_factory(self) -> Any:
        sdk = self._load()
        for name in ("thread_resume", "resume_thread"):
            candidate = getattr(sdk, name, None)
            if candidate is not None:
                return candidate
        raise RuntimeError("SDK thread resume API not found")

    @staticmethod
    def _sandbox_value(sandbox: SandboxMode) -> Any:
        sdk = __import__("codex")
        sandbox_type = getattr(sdk, "SandboxMode", None)
        if sandbox_type is None:
            return sandbox.value
        for attr in ("WORKSPACE_WRITE", "READ_ONLY"):
            if getattr(sandbox_type, attr, None) == sandbox.value:
                return getattr(sandbox_type, attr)
        return sandbox.value

    async def start_thread(self, cwd: Path, sandbox: SandboxMode) -> ThreadHandle:
        factory = self._start_factory()
        kwargs: dict[str, Any] = {"cwd": str(cwd)}
        try:
            native = await self._call(factory, kwargs=kwargs)
        except TypeError:
            kwargs = {"working_directory": str(cwd)}
            native = await self._call(factory, kwargs=kwargs)
        thread_id = str(
            getattr(native, "id", None)
            or getattr(native, "thread_id", None)
            or getattr(native, "thread", None)
        )
        if not thread_id:
            raise RuntimeError("SDK did not return a thread id")
        return ThreadHandle(thread_id=thread_id, cwd=cwd, native=native)

    async def resume_thread(self, thread_id: str, cwd: Path) -> ThreadHandle:
        factory = self._resume_factory()
        native = await self._call(factory, (thread_id,))
        return ThreadHandle(thread_id=thread_id, cwd=cwd, native=native)

    async def run_turn(
        self,
        handle: ThreadHandle,
        prompt: str,
        *,
        sandbox: SandboxMode,
        external: bool = False,
    ) -> AsyncIterator[CodexEvent]:
        native = handle.native
        run = getattr(native, "run", None) or getattr(native, "turn", None)
        if run is None:
            raise RuntimeError("SDK thread run API not found")
        result = run(prompt, sandbox=self._sandbox_value(sandbox))
        if hasattr(result, "__aiter__"):
            async for event in result:
                yield _to_event(event)
        else:
            completed = await result if inspect.isawaitable(result) else result
            yield CodexEvent(
                "turn.completed",
                {"result": _jsonable(completed)},
            )

    async def interrupt(self, handle: ThreadHandle) -> None:
        native = handle.native
        for name in ("interrupt", "cancel", "stop"):
            method = getattr(native, name, None)
            if method is not None:
                result = method()
                if inspect.isawaitable(result):
                    await result
                return
        raise RuntimeError("SDK interrupt API not found")

    async def close(self, handle: ThreadHandle) -> None:
        close = getattr(handle.native, "close", None)
        if close:
            result = close()
            if inspect.isawaitable(result):
                await result
        handle.closed = True


def _to_event(source: Any) -> CodexEvent:
    if isinstance(source, dict):
        payload = source
        event_type = str(source.get("type", "unknown"))
    else:
        event_type = str(getattr(source, "type", type(source).__name__))
        payload = _jsonable(source)
    return CodexEvent(event_type, payload)


def _jsonable(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {"repr": repr(value)}
