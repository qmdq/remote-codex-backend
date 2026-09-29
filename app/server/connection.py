from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.exceptions import ConnectionClosed


class Connection:
    def __init__(self, websocket: Any, device_id: str, max_message_bytes: int):
        self.websocket = websocket
        self.device_id = device_id
        self.max_message_bytes = max_message_bytes
        self.fanout_subscriber: Any | None = None
        self.fanout_queue: asyncio.Queue[dict[str, Any]] | None = None
        self.closed = False
        self._send_lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        if self.closed:
            return
        text = message.json() if hasattr(message, "json") else json.dumps(
            message, ensure_ascii=False, separators=(",", ":")
        )
        async with self._send_lock:
            await self.websocket.send(text)

    async def receive_loop(self, handler) -> None:
        try:
            async for raw in self.websocket:
                if isinstance(raw, bytes) and len(raw) > self.max_message_bytes:
                    await self.websocket.close(code=1009, reason="message too large")
                    break
                await handler(raw)
        except ConnectionClosed:
            pass
        finally:
            self.closed = True
