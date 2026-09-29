from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil


@dataclass(slots=True)
class MetricsSubscriber:
    device_id: str
    queue: asyncio.Queue[dict[str, Any]]


class MetricsMonitor:
    def __init__(self, interval: float = 1.0, max_queue: int = 32):
        self.interval = interval
        self.max_queue = max_queue
        self._subscribers: dict[str, MetricsSubscriber] = {}
        self._task: asyncio.Task[None] | None = None
        self._last_net: tuple[int, int, float] | None = None
        self._cpu_started = False

    def subscribe(self, device_id: str) -> MetricsSubscriber:
        subscriber = MetricsSubscriber(device_id, asyncio.Queue(maxsize=self.max_queue))
        self._subscribers[device_id] = subscriber
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="metrics-monitor")
        return subscriber

    def unsubscribe(self, device_id: str) -> None:
        self._subscribers.pop(device_id, None)
        if not self._subscribers and self._task is not None:
            self._task.cancel()
            self._task = None

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._subscribers.clear()

    async def _run(self) -> None:
        while self._subscribers:
            try:
                payload = await self._collect()
                for subscriber in list(self._subscribers.values()):
                    try:
                        subscriber.queue.put_nowait({
                            "v": 1,
                            "type": "metrics",
                            "payload": payload,
                        })
                    except asyncio.QueueFull:
                        pass
            except Exception:
                payload = {
                    "cpu_percent": None,
                    "memory_percent": None,
                    "memory_used_bytes": None,
                    "disk_percent": None,
                    "net_sent_bytes_per_sec": None,
                    "net_recv_bytes_per_sec": None,
                    "process_count": None,
                }
            await asyncio.sleep(self.interval)

    async def _collect(self) -> dict[str, Any]:
        if not self._cpu_started:
            psutil.cpu_percent(interval=None)
            self._cpu_started = True
        cpu = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(str(Path.home().anchor or "/"))
        counters = psutil.net_io_counters()
        now = asyncio.get_running_loop().time()
        sent_rate = recv_rate = 0.0
        if self._last_net:
            last_sent, last_recv, last_time = self._last_net
            elapsed = max(now - last_time, 0.001)
            sent_rate = max(0, counters.bytes_sent - last_sent) / elapsed
            recv_rate = max(0, counters.bytes_recv - last_recv) / elapsed
        self._last_net = (counters.bytes_sent, counters.bytes_recv, now)
        return {
            "cpu_percent": cpu,
            "memory_percent": memory.percent,
            "memory_used_bytes": memory.used,
            "disk_percent": disk.percent,
            "net_sent_bytes_per_sec": round(sent_rate, 1),
            "net_recv_bytes_per_sec": round(recv_rate, 1),
            "process_count": len(psutil.pids()),
        }
