import asyncio
import json
import os
import socket
import shutil
import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig
from app.main import Application


async def _send(websocket, message_type: str, payload: dict, request_id: str):
    await websocket.send(json.dumps({
        "v": 1,
        "id": request_id,
        "type": message_type,
        "payload": payload,
    }))
    while True:
        message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))
        if message.get("id") == request_id:
            return message


async def _turn_done(app, turn_id: str):
    task = next(
        item.task
        for item in app.turns._running.values()
        if item.id == turn_id
    )
    await task


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ConversationSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import websockets
        self.websockets = websockets
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-conversations-"))
        self.allowed = self.tmp / "project"
        self.allowed.mkdir()
        self.codex_home = self.tmp / "codex-home"
        (self.codex_home / "sessions").mkdir(parents=True)
        self.old_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.codex_home)

        self.config = AppConfig()
        self.config.codex.mode = "fake"
        self.config.server.port = _free_port()
        self.config.server.admin_port = _free_port()
        self.config.projects.allowed_roots = [str(self.tmp)]
        self.config.storage.database_path = str(self.tmp / "agent.db3")
        self.app = Application(self.config)
        await self.app.start()
        _, self.token = await self.app.devices.create("session-phone")
        self.socket = await self.websockets.connect(
            f"ws://127.0.0.1:{self.config.server.port}"
        )
        await _send(self.socket, "hello", {"device_token": self.token}, "hello")
        created = await _send(
            self.socket,
            "project.create",
            {"name": "demo", "path": str(self.allowed)},
            "create",
        )
        self.project = created["payload"]["selected"]
        self.assertIsNone(self.project["current_session_id"])
        started = await _send(self.socket, "turn.start", {
            "project_id": self.project["id"],
            "prompt": "你好",
            "sandbox": "read_only",
        }, "initial-turn")
        self.assertEqual(started["type"], "ok")
        self.project = started["payload"]["selected"]
        self.first_session_id = self.project["current_session_id"]
        await _turn_done(self.app, started["payload"]["turn_id"])

    async def asyncTearDown(self):
        await self.socket.close()
        await self.app.stop()
        if self.old_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_sessions_are_conversations_not_chat_messages(self):
        await self.app.session_titles.shutdown()
        session = await self.app.projects.get_session(
            self.project["id"], self.first_session_id
        )
        self.assertNotEqual(session["title"], "新会话")
        self.assertNotEqual(session["title"], "你好")

        created = await _send(
            self.socket,
            "thread.new",
            {"project_id": self.project["id"]},
            "new",
        )
        second = created["payload"]["selected"]["current_session_id"]
        self.assertIsNone(second)

        replayed = await _send(self.socket, "event.replay", {
            "project_id": self.project["id"],
            "session_id": second,
            "after_seq": 0,
        }, "replay")
        self.assertEqual(replayed["type"], "event.synced")
        self.assertEqual(replayed["payload"]["latest_seq"], 0)

        unbound_session = await self.app.projects.create_session_from_prompt(
            self.project["id"], "unbound session"
        )
        await self.app.projects.select_session(self.project["id"], unbound_session)
        history = await _send(self.socket, "codex.history", {
            "project_id": self.project["id"],
            "session_id": unbound_session,
        }, "history")
        self.assertEqual(history["type"], "codex.history.snapshot")
        self.assertEqual(history["payload"]["messages"], [])
        self.assertEqual(history["payload"]["source"], "awaiting-thread-binding")
        await self.app.database.execute(
            "DELETE FROM codex_sessions WHERE id = ?", (unbound_session,)
        )
        await self.app.database.execute(
            """UPDATE projects
               SET current_session_id = NULL, codex_thread_id = NULL
               WHERE id = ?""",
            (self.project["id"],),
        )

        archived = await _send(self.socket, "thread.archive", {
            "project_id": self.project["id"],
            "session_id": self.first_session_id,
        }, "archive")
        self.assertEqual(archived["type"], "thread.archived")
        self.assertIsNone(archived["payload"]["selected"]["current_session_id"])
        active_ids = [
            row["id"]
            for row in await self.app.projects.list_sessions(self.project["id"])
        ]
        self.assertEqual(active_ids, [])

        snapshot = await _send(
            self.socket,
            "thread.sessions",
            {"project_id": self.project["id"]},
            "sessions",
        )
        active_ids = [item["session_id"] for item in snapshot["payload"]["sessions"]]
        archived_ids = [item["session_id"] for item in snapshot["payload"]["archived_sessions"]]
        self.assertEqual(active_ids, [])
        self.assertIn(self.first_session_id, archived_ids)

        selected = await _send(self.socket, "thread.select", {
            "project_id": self.project["id"],
            "session_id": self.first_session_id,
        }, "select")
        self.assertEqual(
            selected["payload"]["selected"]["current_session_id"],
            self.first_session_id,
        )
        restored = await self.app.projects.list_sessions(self.project["id"])
        self.assertIn(self.first_session_id, [item["id"] for item in restored])

    async def test_project_can_be_removed_without_deleting_local_directory(self):
        removed = await _send(
            self.socket,
            "project.delete",
            {"project_id": self.project["id"]},
            "delete-project",
        )
        self.assertEqual(removed["type"], "project.deleted")
        self.assertIsNone(removed["payload"]["selected"])
        self.assertEqual(removed["payload"]["projects"], [])
        self.assertTrue(self.allowed.is_dir())
        with self.assertRaises(Exception):
            await self.app.projects.get(self.project["id"])
    async def test_side_thread_does_not_write_to_current_conversation(self):
        baseline = await self.app.database.fetch_all(
            "SELECT seq FROM events WHERE project_id = ?",
            (self.project["id"],),
        )
        started = await _send(self.socket, "thread.side", {
            "project_id": self.project["id"],
            "prompt": "side question",
            "thread_id": "codex-side",
            "sandbox": "read_only",
        }, "side")
        self.assertEqual(started["type"], "ok")
        await _turn_done(self.app, started["payload"]["turn_id"])

        rows = await self.app.database.fetch_all(
            "SELECT session_id FROM events WHERE project_id = ? AND seq > ?",
            (self.project["id"], baseline[-1]["seq"] if baseline else 0),
        )
        self.assertTrue(rows)
        self.assertTrue(all(row["session_id"] is None for row in rows))

    async def test_deleted_session_is_hidden_without_creating_empty_session(self):
        deleted = await _send(self.socket, "thread.delete", {
            "project_id": self.project["id"],
            "session_id": self.first_session_id,
        }, "delete-current")
        self.assertEqual(deleted["type"], "thread.deleted", deleted["payload"])
        self.assertTrue(deleted["payload"]["deleted_current"])
        self.assertIsNone(deleted["payload"]["selected"]["current_session_id"])
        self.assertIsNone(deleted["payload"]["replacement_session_id"])

        active_ids = [
            row["id"]
            for row in await self.app.projects.list_sessions(self.project["id"])
        ]
        self.assertEqual(active_ids, [])
        self.assertNotIn(self.first_session_id, active_ids)

        rejected = await _send(self.socket, "thread.select", {
            "project_id": self.project["id"],
            "session_id": self.first_session_id,
        }, "select-deleted")
        self.assertEqual(rejected["type"], "error")
