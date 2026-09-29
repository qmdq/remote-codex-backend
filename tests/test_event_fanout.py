import asyncio
import unittest
from types import SimpleNamespace

from app.events.fanout import EventFanout


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class EventFanoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_reused_queue_survives_project_switch(self):
        fanout = EventFanout()
        connection = SimpleNamespace(
            device_id="device",
            fanout_subscriber=None,
            fanout_queue=None,
        )

        connection.fanout_queue = fanout.create_queue()
        connection.fanout_subscriber = fanout.subscribe_with_queue(
            connection.device_id, "project-a", connection.fanout_queue
        )
        fanout.publish("project-a", {"type": "event.synced"})
        self.assertEqual(await asyncio.wait_for(connection.fanout_queue.get(), 1), {
            "type": "event.synced",
        })

        fanout.unsubscribe(connection.fanout_subscriber)
        connection.fanout_subscriber = fanout.subscribe_with_queue(
            connection.device_id, "project-b", connection.fanout_queue
        )
        fanout.publish("project-b", {"type": "codex.event"})
        self.assertEqual(await asyncio.wait_for(connection.fanout_queue.get(), 1), {
            "type": "codex.event",
        })
        self.assertEqual(len(fanout._subscribers), 1)
