from __future__ import annotations

import asyncio
import codecs
import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from ..protocol.errors import ValidationError

MAX_INPUT_BYTES = 16 * 1024
MAX_OUTPUT_CHUNK_BYTES = 16 * 1024
OUTPUT_QUEUE_SIZE = 128
MIN_TERMINAL_COLS = 20
MAX_TERMINAL_COLS = 300
MIN_TERMINAL_ROWS = 8
MAX_TERMINAL_ROWS = 160


class TerminalService:
    def __init__(self) -> None:
        self._winpty = None
        if os.name == "nt":
            try:
                import winpty  # type: ignore

                self._winpty = winpty
            except ImportError:
                self._winpty = None
        self.pty_available = os.name != "nt" or self._winpty is not None

    async def create_session(
        self,
        cwd: str | Path,
        *,
        cols: int,
        rows: int,
        shell: str | None = None,
    ) -> TerminalSession:
        normalized_cwd = Path(cwd).expanduser().resolve()
        if not normalized_cwd.is_dir():
            raise ValidationError(
                "terminal working directory does not exist",
                code="terminal.invalid_cwd",
            )
        shell_path, shell_args = self._resolve_shell(shell)
        session = TerminalSession(
            cwd=normalized_cwd,
            cols=cols,
            rows=rows,
            shell=shell_path,
            shell_args=shell_args,
            winpty=self._winpty,
        )
        await session.start()
        return session

    def _resolve_shell(self, requested: str | None) -> tuple[str, list[str]]:
        if requested is not None:
            value = str(requested).strip()
            if not value or len(value) > 260 or any(char in value for char in "\x00\r\n"):
                raise ValidationError("invalid terminal shell", code="terminal.invalid_shell")
            resolved = shutil.which(value)
            if not resolved and Path(value).is_file():
                resolved = str(Path(value).resolve())
            if not resolved:
                raise ValidationError(
                    "terminal shell was not found",
                    code="terminal.invalid_shell",
                )
            name = Path(resolved).name.lower()
            return resolved, ["/Q"] if os.name == "nt" and name in {"cmd", "cmd.exe"} else []

        if os.name == "nt":
            shell = os.environ.get("COMSPEC") or shutil.which("cmd.exe") or "cmd.exe"
            return shell, ["/Q"]

        shell = os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        return shell, []


class TerminalSession:
    def __init__(
        self,
        *,
        cwd: Path,
        cols: int,
        rows: int,
        shell: str,
        shell_args: list[str],
        winpty,
    ) -> None:
        self.id = f"term_{uuid4().hex[:16]}"
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.shell = shell
        self.shell_args = shell_args
        self.mode = "pipe"
        self.output_queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(
            maxsize=OUTPUT_QUEUE_SIZE
        )
        self._winpty_module = winpty
        self._process = None
        self._stdin = None
        self._pty_master_fd: int | None = None
        self._reader_done: asyncio.Future[None] | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._closed = False
        self._closing = False
        self._exit_code: int | None = None
        self._exit_queued = False

    async def start(self) -> None:
        if os.name == "nt" and self._winpty_module is not None:
            await self._start_winpty()
        elif os.name != "nt":
            await self._start_posix_pty()
        else:
            await self._start_pipe()

        self._read_task = asyncio.create_task(
            self._read_loop(),
            name=f"terminal-read-{self.id}",
        )
        self._wait_task = asyncio.create_task(
            self._wait_loop(),
            name=f"terminal-wait-{self.id}",
        )

    async def write(self, data: str) -> None:
        if self._closed or self._closing:
            raise ValidationError("terminal session is not running", code="terminal.not_running")
        if not isinstance(data, str):
            raise ValidationError("terminal input must be text", code="terminal.invalid_input")
        encoded = data.encode("utf-8")
        if len(encoded) > MAX_INPUT_BYTES:
            raise ValidationError("terminal input is too large", code="terminal.invalid_input")
        if os.name == "nt":
            data = data.replace("\r\n", "\n").replace("\n", "\r\n")
        async with self._write_lock:
            try:
                if self._winpty_module is not None and self.mode == "pty":
                    await asyncio.to_thread(self._process.write, data)
                    return
                if self._stdin is not None:
                    self._stdin.write(data.encode("utf-8"))
                    await self._stdin.drain()
                    return
                if self._pty_master_fd is not None:
                    await asyncio.to_thread(os.write, self._pty_master_fd, data.encode("utf-8"))
                    return
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                raise ValidationError(
                    "terminal process is not accepting input",
                    code="terminal.not_running",
                ) from exc
        raise ValidationError("terminal process is not accepting input", code="terminal.not_running")

    async def resize(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        if self._closed or self.mode != "pty":
            return
        if self._winpty_module is not None and os.name == "nt":
            await asyncio.to_thread(self._process.set_size, cols, rows)
            return
        if self._pty_master_fd is not None:
            self._resize_posix_pty(self._pty_master_fd, cols, rows)

    async def close(self) -> None:
        if self._closed or self._closing:
            return
        self._closing = True
        try:
            async with self._write_lock:
                if self._winpty_module is not None and self.mode == "pty":
                    pid = getattr(self._process, "pid", None)
                    if pid:
                        try:
                            os.kill(int(pid), signal.SIGTERM)
                        except OSError:
                            pass
                    for _ in range(10):
                        try:
                            if not await asyncio.to_thread(self._process.isalive):
                                break
                        except Exception:
                            break
                        await asyncio.sleep(0.05)
                    try:
                        await asyncio.to_thread(self._process.cancel_io)
                    except Exception:
                        pass
                elif self._process is not None and self._process.returncode is None:
                    try:
                        self._process.terminate()
                        await asyncio.wait_for(self._process.wait(), timeout=1)
                    except (ProcessLookupError, TimeoutError):
                        try:
                            self._process.kill()
                        except ProcessLookupError:
                            pass
                        try:
                            await asyncio.wait_for(self._process.wait(), timeout=1)
                        except TimeoutError:
                            pass
        finally:
            self._remove_posix_reader()
            current = asyncio.current_task()
            tasks = [
                task
                for task in (self._read_task, self._wait_task)
                if task is not None and task is not current
            ]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._winpty_module is not None and self.mode == "pty":
                process = self._process
                if process is not None:
                    if self._exit_code is None:
                        try:
                            status = await asyncio.to_thread(process.get_exitstatus)
                            if status is not None:
                                self._exit_code = int(status)
                        except Exception:
                            pass
                    try:
                        await asyncio.to_thread(process.cancel_io)
                    except Exception:
                        pass
                self._process = None
            self._closed = True
            self._enqueue_exit(self._exit_code if self._exit_code is not None else -1)

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.setdefault("TERM", "xterm-256color")
        return environment

    async def _start_winpty(self) -> None:
        process = await asyncio.to_thread(self._winpty_module.PTY, self.cols, self.rows)
        environment = self._environment()
        env = "\0".join(f"{key}={value}" for key, value in environment.items()) + "\0"
        kwargs: dict[str, object] = {
            "cwd": str(self.cwd),
            "env": env,
        }
        if self.shell_args:
            kwargs["cmdline"] = " " + subprocess.list2cmdline(self.shell_args)
        spawned = await asyncio.to_thread(
            process.spawn,
            self.shell,
            **kwargs,
        )
        if not spawned:
            try:
                await asyncio.to_thread(process.cancel_io)
            except Exception:
                pass
            raise ValidationError(
                "terminal process could not be started",
                code="terminal.start_failed",
            )
        self._process = process
        self.mode = "pty"

    async def _start_posix_pty(self) -> None:
        import fcntl
        import pty
        import termios

        master_fd, slave_fd = pty.openpty()
        self._resize_posix_pty(master_fd, self.cols, self.rows)
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.shell,
                *self.shell_args,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(self.cwd),
                env=self._environment(),
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)

        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._pty_master_fd = master_fd
        self._reader_done = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().add_reader(master_fd, self._on_posix_pty_readable)
        self.mode = "pty"

    async def _start_pipe(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            self.shell,
            *self.shell_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.cwd),
            env=self._environment(),
        )
        self._stdin = self._process.stdin
        self.mode = "pipe"

    def _resize_posix_pty(self, master_fd: int, cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios

        packed = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
        except OSError:
            return

    def _on_posix_pty_readable(self) -> None:
        fd = self._pty_master_fd
        if fd is None:
            return
        try:
            data = os.read(fd, MAX_OUTPUT_CHUNK_BYTES)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            self._queue_output(data)
            return
        self._remove_posix_reader()
        if self._reader_done is not None and not self._reader_done.done():
            self._reader_done.set_result(None)

    def _remove_posix_reader(self) -> None:
        fd = self._pty_master_fd
        if fd is None:
            return
        self._pty_master_fd = None
        try:
            asyncio.get_running_loop().remove_reader(fd)
        except (RuntimeError, ValueError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    async def _read_loop(self) -> None:
        try:
            if self.mode == "pty" and self._pty_master_fd is not None:
                if self._reader_done is not None:
                    await self._reader_done
                return
            if self._winpty_module is not None and self.mode == "pty":
                while not self._closing:
                    try:
                        data = await asyncio.to_thread(self._process.read, True)
                    except EOFError:
                        break
                    if not data:
                        if not await asyncio.to_thread(self._process.isalive):
                            break
                        await asyncio.sleep(0.01)
                        continue
                    if isinstance(data, str):
                        data = data.encode("utf-8")
                    self._queue_output(data)
                return
            if self._process is None or self._process.stdout is None:
                return
            while True:
                data = await self._process.stdout.read(MAX_OUTPUT_CHUNK_BYTES)
                if not data:
                    break
                self._queue_output(data)
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, OSError):
            return

    async def _wait_loop(self) -> None:
        try:
            if self._winpty_module is not None and self.mode == "pty":
                process = self._process
                while await asyncio.to_thread(process.isalive):
                    await asyncio.sleep(0.1)
                self._exit_code = int(
                    await asyncio.to_thread(process.get_exitstatus) or 0
                )
            else:
                self._exit_code = int(await self._process.wait())
            if self._read_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._read_task), timeout=1)
                except TimeoutError:
                    self._read_task.cancel()
        except asyncio.CancelledError:
            raise
        finally:
            if not self._closing:
                self._enqueue_exit(self._exit_code if self._exit_code is not None else -1)

    def _queue_output(self, data: bytes) -> None:
        if not data:
            return
        text = self._decoder.decode(data, final=False)
        if text:
            self._enqueue(("output", text))

    def _enqueue_exit(self, exit_code: int) -> None:
        if self._exit_queued:
            return
        self._exit_queued = True
        self._enqueue(("exit", int(exit_code)))

    def _enqueue(self, event: tuple[str, object]) -> None:
        try:
            self.output_queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        try:
            self.output_queue.get_nowait()
            self.output_queue.task_done()
        except asyncio.QueueEmpty:
            pass
        try:
            self.output_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
