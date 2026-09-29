from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, eq=False)
class Subscriber:
    device_id: str
    project_id: str
    queue: asyncio.Queue[dict[str, Any]]


class EventFanout:
    def __init__(self, max_queue: int = 1000):
        self.max_queue = max_queue
        self._subscribers: set[Subscriber] = set()

    def subscribe(self, device_id: str, project_id: str) -> Subscriber:
        subscriber = Subscriber(device_id, project_id, asyncio.Queue(maxsize=self.max_queue))
        self._subscribers.add(subscriber)
        return subscriber

    def create_queue(self) -> asyncio.Queue[dict[str, Any]]:
        return asyncio.Queue(maxsize=self.max_queue)

    def subscribe_with_queue(
        self,
        device_id: str,
        project_id: str,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> Subscriber:
        subscriber = Subscriber(device_id, project_id, queue)
        self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    def publish(self, project_id: str, message: dict[str, Any]) -> None:
        for subscriber in list(self._subscribers):
            if subscriber.project_id != project_id:
                continue
            try:
                subscriber.queue.put_nowait(message)
            except asyncio.QueueFull:
                subscriber.queue.put_nowait({
                    "v": 1,
                    "type": "error",
                    "payload": {
                        "code": "event.overflow",
                        "message": "event buffer overflow",
                        "retryable": True,
                    },
                })
