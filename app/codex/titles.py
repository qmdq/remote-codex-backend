from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..projects.manager import normalize_session_title
from ..storage.database import Database
from .gateway import CodexGateway, SandboxMode

logger = logging.getLogger(__name__)


class SessionTitleService:
    """Generate conversation titles with a separate ephemeral Codex turn."""

    def __init__(
        self,
        database: Database,
        gateway: CodexGateway,
        *,
        timeout_sec: float = 60.0,
        max_pending: int = 8,
    ):
        self.db = database
        self.gateway = gateway
        self.timeout_sec = timeout_sec
        self.max_pending = max_pending
        self._pending: set[asyncio.Task[None]] = set()

    def generate(self, project: dict, session_id: str, prompt: str) -> None:
        if not session_id or len(self._pending) >= self.max_pending:
            return
        task = asyncio.create_task(
            self._generate(project, session_id, prompt),
            name=f"codex-session-title-{session_id}",
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _generate(self, project: dict, session_id: str, prompt: str) -> None:
        cwd = Path(str(project.get("normalized_path") or "."))
        handle = None
        title = ""
        try:
            handle = await self.gateway.start_thread(cwd, SandboxMode.READ_ONLY)
            generator = self.gateway.run_turn(
                handle,
                self._prompt(prompt),
                sandbox=SandboxMode.READ_ONLY,
                model=None,
                external=True,
                ephemeral=True,
            )
            title = await asyncio.wait_for(
                self._first_agent_message(generator),
                timeout=self.timeout_sec,
            )
            if title:
                await self.db.execute(
                    """UPDATE codex_sessions
                       SET title = ?
                       WHERE id = ? AND status != 'deleted'""",
                    (title[:80], session_id),
                )
        except Exception:
            logger.debug("Session title generation failed", exc_info=True)
        finally:
            if handle is not None:
                try:
                    await self.gateway.close(handle)
                except Exception:
                    pass

    async def shutdown(self) -> None:
        tasks = set(self._pending)
        if tasks:
            await asyncio.wait(tasks, timeout=5)

    @staticmethod
    async def _first_agent_message(generator) -> str:
        async for source in generator:
            event_type = str(getattr(source, "type", ""))
            raw_payload = getattr(source, "payload", None)
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            item = payload.get("item") or {}
            if event_type != "item.completed":
                continue
            if str(item.get("type") or "") != "agent_message":
                continue
            return normalize_session_title(
                str(payload.get("text") or item.get("text") or "")
            )
        return ""

    @staticmethod
    def _prompt(prompt: str) -> str:
        excerpt = " ".join(prompt.split())[:1200]
        return (
            "根据下面的用户请求生成一个不超过 16 个字的会话标题。"
            "只输出标题正文，不要解释、标点、引号或换行。\n\n"
            f"用户请求：{excerpt}"
        )
