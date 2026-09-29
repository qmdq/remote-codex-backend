import asyncio
import json
import shutil
import tempfile
import unittest
import socket
from pathlib import Path

import websockets

from app.config import AppConfig
from app.main import Application
from app.projects.authorizations import DirectoryAuthorizationService
from app.projects.manager import ProjectService
from app.storage.database import Database


async def _send(websocket, message_type: str, payload: dict | None = None, request_id: str = "r"):
    await websocket.send(json.dumps({
        "v": 1, "id": request_id, "type": message_type, "payload": payload or {}
    }))
    while True:
        message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))
        if message.get("id") == request_id:
            return message


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TemporaryChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-codex-temp-chat-"))
        self.allowed = self.tmp / "project"
        self.allowed.mkdir()
        config = AppConfig()
        config.server.port = _free_port()
        config.server.admin_port = _free_port()
        config.codex.mode = "fake"
        config.projects.allowed_roots = [str(self.allowed)]
        config.storage.database_path = str(self.tmp / "agent.db3")
        self.app = Application(config)
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_temporary_chat_requires_pc_directory_authorization(self):
        _, token = await self.app.devices.create("temporary-phone")
        async with websockets.connect(f"ws://127.0.0.1:{self.app.config.server.port}") as socket:
            ready = await _send(socket, "hello", {"device_token": token}, "hello")
            self.assertTrue(ready["payload"]["capabilities"]["temporary_chats"])
            created = await _send(socket, "project.temporary.create", {}, "create")
            project = created["payload"]["selected"]
            self.assertEqual(project["is_temporary"], 1)
            self.assertEqual(project["default_sandbox"], "read_only")

            denied = await _send(socket, "file.list", {
                "project_id": project["id"], "path": ""
            }, "files")
            self.assertEqual(denied["payload"]["code"], "project.authorization_required")

            writable = await _send(socket, "turn.start", {
                "project_id": project["id"], "prompt": "hello", "sandbox": "workspace_write"
            }, "writable")
            self.assertEqual(writable["payload"]["code"], "project.authorization_required")

            read_only = await _send(socket, "turn.start", {
                "project_id": project["id"], "prompt": "hello", "sandbox": "read_only"
            }, "readonly")
            self.assertEqual(read_only["type"], "ok")
            for _ in range(100):
                if not self.app.turns.running_turns():
                    break
                await asyncio.sleep(0.02)
            self.assertFalse(self.app.turns.running_turns())

            request = await _send(socket, "project.authorization.request", {
                "project_id": project["id"], "reason": "需要编辑项目文件"
            }, "request")
            authorization = request["payload"]["requests"][0]
            self.assertEqual(authorization["status"], "pending")

            approved = await self.app.admin.approve_authorization(
                authorization["request_id"], str(self.allowed)
            )
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(approved["project"]["is_temporary"], 0)
            self.assertEqual(
                Path(approved["project"]["normalized_path"]).resolve(),
                self.allowed.resolve(),
            )

            allowed_files = await _send(socket, "file.list", {
                "project_id": project["id"], "path": ""
            }, "allowed-files")
            self.assertEqual(allowed_files["type"], "file.list.snapshot")

            writable_turn = await _send(socket, "turn.start", {
                "project_id": project["id"], "prompt": "edit", "sandbox": "workspace_write"
            }, "allowed-turn")
            self.assertEqual(writable_turn["type"], "ok")

    async def test_rejected_authorization_keeps_temporary_chat_isolated(self):
        projects = ProjectService(
            self.app.database,
            [self.allowed],
            temporary_root=self.tmp / "scratch",
        )
        project = await projects.create_temporary("question")
        authorizations = DirectoryAuthorizationService()
        request = await authorizations.request(project, "read files")
        rejected = await authorizations.reject(request["request_id"])

        self.assertEqual(rejected["status"], "rejected")
        current = await projects.get(project["id"])
        self.assertEqual(current["is_temporary"], 1)
        self.assertEqual(current["default_sandbox"], "read_only")


class TemporaryProjectMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-codex-migration-"))
        self.db = Database(self.tmp / "test.db3")

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_is_temporary_column_has_not_null_default(self):
        await self.db.connect()
        columns = {row["name"]: row for row in await self.db.fetch_all("PRAGMA table_info(projects)")}
        self.assertEqual(columns["is_temporary"]["notnull"], 1)
        self.assertEqual(columns["is_temporary"]["dflt_value"], "0")
