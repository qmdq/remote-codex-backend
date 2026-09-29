from __future__ import annotations

import asyncio
import os
import json
import os
import re
import shutil
from pathlib import Path
from typing import AsyncIterator

from .gateway import CodexEvent, SandboxMode, ThreadHandle


class CodexCliGateway:
    """Gateway for the locally installed Codex CLI."""

    def __init__(self, command: str | None = None):
        self.command = command or self._discover()
        if not self.command:
            raise RuntimeError("codex CLI not found")
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def diagnostics(self) -> dict:
        try:
            process = await asyncio.create_subprocess_exec(
                self.command,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
        except OSError as exc:
            return {"available": False, "error": str(exc)}
        return {
            "available": process.returncode == 0,
            "command": self.command,
            "version": stdout.decode("utf-8", "replace").strip(),
        }

    @staticmethod
    def _discover() -> str | None:
        found = shutil.which("codex")
        if found:
            return found
        candidates = []
        home = os.environ.get("USERPROFILE") or str(Path.home())
        local_app_data = os.environ.get("LOCALAPPDATA") or str(Path(home) / "AppData/Local")
        candidates.append(Path(local_app_data) / "OpenAI/Codex/bin")
        for root in candidates:
            if not root.exists():
                continue
            matches = sorted(
                (path for path in root.rglob("codex.exe") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if matches:
                return str(matches[0])
        return None

    async def start_thread(self, cwd: Path, sandbox: SandboxMode) -> ThreadHandle:
        return ThreadHandle(thread_id="", cwd=cwd, native={"sandbox": sandbox})

    async def run_cli(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        stdin: str | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, str, str]:
        """Run a short Codex CLI command and return its decoded output."""

        environment = os.environ.copy()
        environment.setdefault("HOME", environment.get("USERPROFILE", ""))
        environment.setdefault("CODEX_HOME", str(Path.home() / ".codex"))
        process = await asyncio.create_subprocess_exec(
            self.command,
            *args,
            cwd=str(cwd) if cwd else None,
            env=environment,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode() if stdin else None), timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        return (
            process.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    def _config_args(self, reasoning_effort: str | None) -> list[str]:
        if not reasoning_effort or reasoning_effort == "default":
            return []
        return ["-c", f'model_reasoning_effort="{reasoning_effort}"']

    async def resume_thread(self, thread_id: str, cwd: Path) -> ThreadHandle:
        return ThreadHandle(thread_id=thread_id, cwd=cwd, native={"sandbox": None})

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
        env = os.environ.copy()
        env.setdefault("HOME", env.get("USERPROFILE", ""))
        env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))
        args = ["exec"]
        args += [
            *(["--model", model] if model else []),
            *self._config_args(reasoning_effort),
            "--json",
            "--skip-git-repo-check",
            "-C", str(handle.cwd),
            "-s", self._sandbox_value(sandbox),
        ]
        if ephemeral:
            args += ["--ephemeral", "-"]
        elif handle.thread_id:
            args += ["resume", handle.thread_id, "-"]
        elif fork_from:
            args += ["fork", fork_from, "-"]
        else:
            args.append("-")

        command_args = [self.command, *args]
        process = await asyncio.create_subprocess_exec(
            *command_args,
            cwd=str(handle.cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        process_key = str(id(process))
        native = handle.native or {}
        native["process"] = process
        native["sandbox"] = sandbox
        self._processes[process_key] = process
        try:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            process.kill()
            await process.wait()
            self._processes.pop(process_key, None)
            raise RuntimeError("could not send prompt to Codex CLI")

        try:
            assert process.stdout is not None
            async for line in process.stdout:
                decoded = line.decode("utf-8", "replace").strip()
                if not decoded.startswith("{"):
                    continue
                try:
                    payload = json.loads(decoded)
                except json.JSONDecodeError:
                    continue
                event = self._normalize(payload)
                if event:
                    yield event
                if event and event.type in {
                    "turn.completed", "turn.failed", "turn.interrupted"
                }:
                    break
            await process.wait()
            if process.returncode != 0:
                stderr = await process.stderr.read() if process.stderr else b""
                message = stderr.decode("utf-8", "replace").strip() or "Codex CLI failed"
                yield CodexEvent("turn.failed", {"message": message})
        finally:
            self._processes.pop(process_key, None)

    async def interrupt(self, handle: ThreadHandle) -> None:
        process = (handle.native or {}).get("process")
        if process and process.returncode is None:
            process.kill()
            await process.wait()

    async def close(self, handle: ThreadHandle) -> None:
        handle.closed = True

    def _sandbox_value(self, sandbox: SandboxMode) -> str:
        return "read-only" if sandbox is SandboxMode.READ_ONLY else "workspace-write"

    def _normalize(self, payload: dict) -> CodexEvent | None:
        event_type = str(payload.get("type", ""))
        if not event_type:
            return None
        if event_type == "thread.started":
            return CodexEvent("thread.started", {"thread_id": payload.get("thread_id", "")})
        if event_type in {"turn.started", "turn.completed", "turn.failed", "turn.interrupted"}:
            payload.pop("type", None)
            return CodexEvent(event_type, payload)
        if event_type == "item.started":
            return CodexEvent("item.started", {
                "item": self._normalize_item(payload.get("item", {}))
            })
        if event_type == "item.completed":
            item = payload.get("item", {})
            event = CodexEvent("item.completed", {
                "item": self._normalize_item(item)
            })
            if item.get("type") == "agent_message":
                event.payload["text"] = item.get("text", "")
            return event
        if event_type == "error":
            return CodexEvent("turn.failed", {"message": payload.get("message", "Codex error")})
        return CodexEvent(event_type, payload)

    def _normalize_item(self, item: dict) -> dict:
        if not isinstance(item, dict):
            return {}
        normalized = dict(item)
        source_type = str(normalized.get("type", ""))
        normalized["type"] = re.sub(
            r"([a-z0-9])([A-Z])",
            r"\1_\2",
            source_type.replace("-", "_"),
        ).lower()
        if normalized["type"] == "file_change":
            changes = normalized.get("changes")
            if isinstance(changes, dict):
                paths = [str(path) for path in changes.keys() if str(path)]
                diffs = [
                    str(change.get("unified_diff") or change.get("diff") or "")
                    for change in changes.values()
                    if isinstance(change, dict)
                ]
                diffs = [diff for diff in diffs if diff]
                if paths and not normalized.get("path"):
                    normalized["path"] = paths[0]
                if paths:
                    normalized["paths"] = paths
                if diffs and not normalized.get("unified_diff"):
                    normalized["unified_diff"] = "\n".join(diffs)
        return normalized
