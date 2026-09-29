from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .auth.devices import DeviceService
from .auth.pairing import PairingService
from .auth.settings import load_or_create_pepper
from .codex.cli_gateway import CodexCliGateway
from .codex.models import discover_codex_models
from .codex.gateway import FakeCodexGateway
from .codex.sdk_adapter import OpenAICodexAdapter
from .codex.history import CodexHistoryService
from .codex.sessions import CodexSessionService
from .codex.titles import SessionTitleService
from .codex.turns import TurnSupervisor
from .files.service import FileService
from .config import AppConfig, default_database_path, load_config
from .events.fanout import EventFanout
from .events.store import EventStore
from .input_service import system_input
from .monitors.metrics import MetricsMonitor
from .monitors.screen import ScreenMonitor
from .projects.manager import ProjectService
from .server.admin import AdminServer
from .server.websocket import AgentServer
from .storage.database import Database
from .terminal.service import TerminalService


def configure_logging() -> None:
    log_dir = Path(os.environ.get("TEMP") or Path.cwd()) / "RemoteCodex"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "agent.log",
        maxBytes=1_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler],
    )


class Application:
    def __init__(self, config: AppConfig):
        configure_logging()
        self.config = config
        self._apply_codex_models()
        self.database = Database(config.storage.database_path or default_database_path())
        self.devices = DeviceService(self.database, "pending-pepper")
        self.pairing = PairingService(
            self.database,
            self.devices,
            ttl_sec=config.security.pairing_code_ttl_sec,
            confirm_ttl_sec=config.security.pairing_confirm_ttl_sec,
            max_attempts=config.security.pairing_max_attempts,
        )
        self.projects = ProjectService(
            self.database,
            [Path(path) for path in config.projects.allowed_roots],
        )
        if config.codex.mode == "fake":
            self.gateway = FakeCodexGateway()
        else:
            self.gateway = CodexCliGateway()
        self.files = FileService()
        self.history = CodexHistoryService()
        self.sessions = CodexSessionService()
        self.events = EventStore(self.database)
        self.fanout = EventFanout()
        self.session_titles = SessionTitleService(self.database, self.gateway)
        self.turns = TurnSupervisor(
            self.database,
            self.gateway,
            self.events,
            self.fanout,
            self.projects,
            self.session_titles,
            max_running=config.codex.max_running_turns,
            timeout_sec=config.codex.turn_timeout_sec,
        )
        self.metrics = MetricsMonitor(config.monitor.metrics_interval_sec)
        self.screen = ScreenMonitor(
            default_fps=config.monitor.screen_default_fps,
            max_fps=config.monitor.screen_max_fps,
            max_width=config.monitor.screen_max_width,
            jpeg_quality=config.monitor.screen_jpeg_quality,
        )
        self.terminal = TerminalService()
        self.server = AgentServer(
            config=config,
            database=self.database,
            devices=self.devices,
            pairing=self.pairing,
            projects=self.projects,
            turns=self.turns,
            events=self.events,
            fanout=self.fanout,
            metrics=self.metrics,
            screen=self.screen,
            gateway=self.gateway,
            files=self.files,
            history=self.history,
            sessions=self.sessions,
            system_input=system_input,
            terminal=self.terminal,
        )
        self.admin = AdminServer(
            self,
            host=config.server.admin_host,
            port=config.server.admin_port,
        )
        self.server.preview_info_getter = lambda: {
            "host": self.admin.lan_ip,
            "port": self.admin.preview_port,
        }
        self._serve_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._server_stop: asyncio.Event | None = None
        self._server_ready: asyncio.Event | None = None
        self._server_error: BaseException | None = None
        database_dir = Path(config.storage.database_path or default_database_path()).parent
        self._admin_link_path = Path(os.environ.get("TEMP") or database_dir) / "RemoteCodex" / "admin-console.txt"

    async def start(self) -> None:
        self._apply_codex_models()
        await self.database.connect()
        self._admin_link_path.parent.mkdir(parents=True, exist_ok=True)
        self.devices.pepper = await load_or_create_pepper(self.database)
        await self.turns.recover()
        await self.projects.purge_empty_sessions()
        await self._import_local_codex_sessions()
        await self.admin.start()
        self._server_stop = asyncio.Event()
        self._server_ready = asyncio.Event()
        self._server_error = None
        self._serve_task = asyncio.create_task(self._serve())
        try:
            await asyncio.wait_for(self._server_ready.wait(), timeout=5)
        except TimeoutError as exc:
            self._serve_task.cancel()
            await asyncio.gather(self._serve_task, return_exceptions=True)
            raise RuntimeError("WebSocket server did not become ready") from exc
        if self._server_error is not None:
            error = self._server_error
            self._serve_task = None
            raise RuntimeError(f"WebSocket server failed to start: {error}") from error
        self._admin_link_path.write_text(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{self.admin.url}\n",
            encoding="utf-8",
        )

    async def _import_local_codex_sessions(self) -> None:
        """Keep database history aligned with local Codex rollout files."""

        try:
            for project in await self.projects.list():
                records = await asyncio.to_thread(
                    self.sessions.sessions_for_project,
                    Path(project["normalized_path"]),
                )
                await self.projects.import_codex_sessions(project["id"], records)
        except Exception:
            logging.getLogger(__name__).exception("Failed to import local Codex sessions")

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        try:
            async with serve(
                self.server.handle,
                self.config.server.host,
                self.config.server.port,
                max_size=self.config.server.max_message_bytes,
                reuse_address=False,
            ):
                if self._server_ready is not None:
                    self._server_ready.set()
                await self._server_stop.wait()
        except BaseException as exc:
            self._server_error = exc
            if self._server_ready is not None:
                self._server_ready.set()
            raise

    async def stop(self) -> None:
        self._stop.set()
        if self._server_stop is not None:
            self._server_stop.set()
        if self._serve_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._serve_task), timeout=2)
            except TimeoutError:
                self._serve_task.cancel()
                await self._serve_task
        await self.turns.shutdown()
        await self.admin.stop()
        if self._admin_link_path.exists():
            self._admin_link_path.unlink()
        await self.metrics.close()
        await self.screen.close()
        await self.database.close()

    def _apply_codex_models(self) -> None:
        models = discover_codex_models()
        choices = [str(item) for item in models.get("choices", []) if str(item).strip()]
        default = str(models.get("default_model") or "default").strip() or "default"
        self.config.models.choices = choices
        self.config.models.default_model = default
        self.models_source = str(models.get("source", ""))
