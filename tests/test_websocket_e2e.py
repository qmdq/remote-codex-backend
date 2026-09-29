import asyncio
import base64
import json
import os
import socket
import urllib.request
from urllib.error import HTTPError

import websockets
from websockets.exceptions import ConnectionClosedError

import tempfile
import unittest
from pathlib import Path
import shutil
from app.config import AppConfig
from app.main import Application


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _send(websocket, message_type: str, payload: dict | None = None, request_id="r1"):
    request = {
        "v": 1,
        "id": request_id,
        "type": message_type,
        "payload": payload or {},
    }
    await websocket.send(json.dumps(request))
    while True:
        raw = await asyncio.wait_for(websocket.recv(), timeout=2)
        message = json.loads(raw)
        if message.get("id") == request_id:
            return message


async def _recv_type(websocket, expected_type: str, timeout=2):
    while True:
        message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=timeout))
        if message.get("type") == expected_type:
            return message


class WebSocketE2ETests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-codex-e2e-"))
        allowed = self.tmp / "project"
        allowed.mkdir()
        self.second_allowed = self.tmp / "second-project"
        self.second_allowed.mkdir()
        self.codex_home = self.tmp / "codex"
        sessions = self.codex_home / "sessions" / "2026" / "09"
        sessions.mkdir(parents=True)
        outside = self.tmp / "private"
        outside.mkdir()
        self.thread_one = "b78a2d8c-1111-4222-8333-111111111111"
        self.thread_two = "b78a2d8c-2222-4222-8333-222222222222"
        for index, thread_id in enumerate((self.thread_one, self.thread_two)):
            text = "history one" if index == 0 else "history two"
            for project_dir in (allowed, self.second_allowed):
                records = [
                    {"type": "session_meta", "payload": {"cwd": str(project_dir), "id": thread_id}},
                    {"type": "response_item", "timestamp": "2026-09-25T01:02:03Z", "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }},
                ]
                filename = (
                    f"rollout-2026-09-25T01-00-0{index}-{thread_id}.jsonl"
                    if project_dir == allowed
                    else f"rollout-2026-09-25T02-00-0{index}-{thread_id}.jsonl"
                )
                (sessions / filename).write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
                )
        (sessions / "outside.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"cwd": str(outside)}}) + "\n",
            encoding="utf-8",
        )
        self.original_codex_home = os.environ.get("CODEX_HOME")
        port = _free_port()
        config = AppConfig()
        config.server.port = port
        config.server.admin_port = _free_port()
        config.codex.mode = "fake"
        config.projects.allowed_roots = [str(allowed), str(self.second_allowed)]
        config.storage.database_path = str(self.tmp / "agent.db3")
        self.app = Application(config)
        await self.app.start()
        os.environ["CODEX_HOME"] = str(self.codex_home)

    async def asyncTearDown(self):
        await self.app.stop()
        if self.original_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.original_codex_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_codex_projects_can_be_discovered_and_imported(self):
        _, token = await self.app.devices.create("sync-phone")
        websocket = await websockets.connect(
            f"ws://127.0.0.1:{self.app.config.server.port}"
        )
        try:
            ready = await _send(websocket, "hello", {"device_token": token})
            self.assertTrue(ready["payload"]["capabilities"]["codex_project_sync"])
            listed = await _send(websocket, "codex.project.list", {}, "codex-list")
            self.assertEqual(listed["type"], "codex.project.snapshot")
            candidates = {item["name"]: item for item in listed["payload"]["projects"]}
            self.assertTrue(candidates["project"]["importable"])
            self.assertFalse(candidates["private"]["importable"])

            imported = await _send(
                websocket,
                "codex.project.import",
                {"path": str(self.tmp / "project")},
                "codex-import",
            )
            self.assertEqual(imported["type"], "project.snapshot")
            project_id = imported["payload"]["selected"]["id"]
            self.assertEqual(imported["payload"]["selected"]["name"], "project")

            session_snapshot = await _send(
                websocket,
                "thread.sessions",
                {"project_id": project_id},
                "imported-sessions",
            )
            self.assertEqual(len(session_snapshot["payload"]["sessions"]), 0)
            self.assertEqual(len(session_snapshot["payload"]["archived_sessions"]), 2)

            empty_history = await _send(
                websocket,
                "codex.history",
                {"project_id": project_id},
                "empty-history",
            )
            self.assertEqual(empty_history["payload"]["messages"], [])

            restored = await _send(
                websocket,
                "thread.select",
                {
                    "project_id": project_id,
                    "session_id": session_snapshot["payload"]["archived_sessions"][0]["session_id"],
                },
                "restore-history",
            )
            self.assertEqual(restored["type"], "thread.snapshot")
            restored_history = await _send(
                websocket,
                "codex.history",
                {"project_id": project_id},
                "restored-history",
            )
            self.assertEqual(len(restored_history["payload"]["messages"]), 1)

            repeated = await _send(
                websocket,
                "codex.project.import",
                {"path": str(self.tmp / "project")},
                "codex-import-again",
            )
            self.assertEqual(repeated["payload"]["selected"]["id"], project_id)
            self.assertEqual(len(repeated["payload"]["projects"]), 1)

            rejected = await _send(
                websocket,
                "codex.project.import",
                {"path": str(self.tmp / "private")},
                "codex-import-outside",
            )
            self.assertEqual(rejected["type"], "error")
            self.assertEqual(rejected["payload"]["code"], "project.not_allowed")
        finally:
            await websocket.close()

    async def test_project_sync_imports_projects_and_sessions_without_changing_current(self):
        _, token = await self.app.devices.create("batch-sync-phone")
        websocket = await websockets.connect(
            f"ws://127.0.0.1:{self.app.config.server.port}"
        )
        try:
            await _send(websocket, "hello", {"device_token": token})
            imported = await _send(
                websocket,
                "codex.project.import",
                {"path": str(self.tmp / "project")},
                "import-current",
            )
            project_id = imported["payload"]["selected"]["id"]
            initial_snapshot = await _send(
                websocket,
                "thread.sessions",
                {"project_id": project_id},
                "initial-sessions",
            )
            history_session = initial_snapshot["payload"]["archived_sessions"][0]["session_id"]
            selected = await _send(
                websocket,
                "thread.select",
                {"project_id": project_id, "session_id": history_session},
                "select-history",
            )
            current_session = selected["payload"]["selected"]["current_session_id"]

            synced = await _send(
                websocket,
                "codex.project.sync",
                {},
                "batch-sync",
            )
            self.assertEqual(synced["type"], "codex.project.synced")
            self.assertEqual(synced["payload"]["synced_projects"], 2)
            self.assertEqual(synced["payload"]["synced_sessions"], 4)
            self.assertEqual(len(synced["payload"]["registered_projects"]), 2)

            current = await self.app.projects.get(project_id)
            self.assertEqual(current["current_session_id"], current_session)

            second = next(
                item for item in synced["payload"]["registered_projects"]
                if Path(item["normalized_path"]).name == self.second_allowed.name
            )
            second_snapshot = await _send(
                websocket,
                "thread.sessions",
                {"project_id": second["id"]},
                "second-sessions",
            )
            self.assertEqual(len(second_snapshot["payload"]["archived_sessions"]), 2)

            first_delete_target = initial_snapshot["payload"]["archived_sessions"][1]["session_id"]
            deleted = await _send(
                websocket,
                "thread.delete",
                {"project_id": project_id, "session_id": first_delete_target},
                "delete-session",
            )
            self.assertEqual(deleted["type"], "thread.deleted")

            synced_again = await _send(
                websocket,
                "codex.project.sync",
                {},
                "batch-sync-again",
            )
            self.assertEqual(synced_again["type"], "codex.project.synced")
            after_delete = await _send(
                websocket,
                "thread.sessions",
                {"project_id": project_id},
                "sessions-after-delete",
            )
            visible_ids = [
                item["session_id"]
                for item in after_delete["payload"]["sessions"]
            ] + [
                item["session_id"]
                for item in after_delete["payload"]["archived_sessions"]
            ]
            self.assertIn(history_session, visible_ids)
            self.assertNotIn(first_delete_target, visible_ids)
        finally:
            await websocket.close()

    async def test_hello_create_replay_and_turn_push(self):
        allowed = self.tmp / "project"
        port = self.app.config.server.port

        websocket = await websockets.connect(f"ws://127.0.0.1:{port}")
        try:
            await websocket.send(json.dumps({
                "v": 1, "id": "bad", "type": "hello",
                "payload": {"device_token": "invalid"},
            }))
            error = await _recv_type(websocket, "error")
            self.assertEqual(error["payload"]["code"], "auth.invalid_token")
        except ConnectionClosedError as exc:
            self.assertIsNotNone(exc.rcvd)
            self.assertEqual(exc.rcvd.code, 4401)
            self.assertEqual(exc.rcvd.reason, "auth.invalid_token")
        finally:
            await websocket.close()

        device_id, token = await self.app.devices.create("test-phone")
        websocket = await websockets.connect(f"ws://127.0.0.1:{port}")
        try:
            ready = await _send(websocket, "hello", {"device_token": token})
            self.assertEqual(ready["type"], "ready")

            created = await _send(
                websocket,
                "project.create",
                {"name": "demo", "path": str(allowed)},
                "create",
            )
            self.assertEqual(created["type"], "project.snapshot")
            project = created["payload"]["selected"]
            self.assertEqual(created["payload"]["latest_seq"], 0)

            synced = await _send(
                websocket,
                "event.replay",
                {"project_id": project["id"], "after_seq": 0},
                "replay",
            )
            self.assertEqual(synced["type"], "event.synced")

            started = await _send(
                websocket,
                "turn.start",
                {
                    "project_id": project["id"],
                    "prompt": "run fake task",
                    "sandbox": "read_only",
                },
                "start",
            )
            self.assertEqual(started["type"], "ok")

            events = []
            while True:
                message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2))
                if message.get("type") in {"codex.event", "agent.event"}:
                    events.append(message["type"])
                if message.get("type") == "agent.event" and message.get("payload", {}).get("code") == "turn.status":
                    break
            self.assertIn("codex.event", events)

            synced = await _send(
                websocket,
                "event.replay",
                {"project_id": project["id"], "after_seq": 0},
                "replay-after-turn",
            )
            self.assertEqual(synced["type"], "event.synced")
            self.assertGreater(synced["payload"]["latest_seq"], 0)
        finally:
            await websocket.close()

    async def test_agent_message_text_is_stored_for_mobile_reply(self):
        allowed = self.tmp / "project"
        port = self.app.config.server.port

        websocket = await websockets.connect(f"ws://127.0.0.1:{port}")
        try:
            _, token = await self.app.devices.create("reply-phone")
            await _send(websocket, "hello", {"device_token": token})
            created = await _send(
                websocket,
                "project.create",
                {"name": "reply", "path": str(allowed)},
            )
            project = created["payload"]["selected"]
            await websocket.send(json.dumps({
                "v": 1,
                "id": "start-reply",
                "type": "turn.start",
                "payload": {
                    "project_id": project["id"],
                    "prompt": "hello",
                    "sandbox": "read_only",
                },
            }))

            reply = None
            while reply is None:
                message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2))
                if message.get("id") == "start-reply" and message.get("type") == "ok":
                    continue
                if message.get("type") != "codex.event":
                    continue
                payload = message.get("event", {})
                if payload.get("type") == "item.completed":
                    item = payload.get("item", {})
                    if item.get("type") == "agent_message":
                        reply = item
            self.assertEqual(reply["text"], "Ran pytest. All checks passed.")
        finally:
            await websocket.close()

    async def test_project_model_is_persisted_and_turn_can_follow_default(self):
        allowed = self.tmp / "project"
        port = self.app.config.server.port
        configured_model = next(
            (model for model in self.app.config.models.choices if model != "default"),
            "default",
        )

        websocket = await websockets.connect(f"ws://127.0.0.1:{port}")
        try:
            _, token = await self.app.devices.create("model-phone")
            await _send(websocket, "hello", {"device_token": token})
            created = await _send(
                websocket,
                "project.create",
                {"name": "modelled", "path": str(allowed)},
            )
            project = created["payload"]["selected"]
            self.assertIsNone(project["model"])

            selected = await _send(
                websocket,
                "project.model",
                {"project_id": project["id"], "model": configured_model},
                "select-model",
            )
            expected_model = (
                configured_model
                if configured_model in self.app.config.models.choices
                else None
            )
            self.assertEqual(selected["payload"]["selected"]["model"], expected_model)

            rejected = await _send(
                websocket,
                "project.model",
                {"project_id": project["id"], "model": "not-configured"},
                "bad-model",
            )
            self.assertEqual(rejected["type"], "error")

            started = await _send(
                websocket,
                "turn.start",
                {
                    "project_id": project["id"],
                    "prompt": "hello",
                    "sandbox": "read_only",
                    "model": "default",
                },
                "start-default",
            )
            self.assertEqual(started["type"], "ok")
            await next(
                item.task
                for item in self.app.turns._running.values()
                if item.id == started["payload"]["turn_id"]
            )
            self.assertIsNone(self.app.gateway.models[-1])

            persisted = await self.app.projects.get(project["id"])
            self.assertEqual(persisted["model"], expected_model)
        finally:
            await websocket.close()

    async def test_file_upload_supports_chunks_empty_files_and_file_preview(self):
        allowed = self.tmp / "project"
        (allowed / "docs").mkdir()
        _, token = await self.app.devices.create("upload-phone")
        websocket = await websockets.connect(
            f"ws://127.0.0.1:{self.app.config.server.port}"
        )
        try:
            await _send(websocket, "hello", {"device_token": token})
            created = await _send(
                websocket,
                "project.create",
                {"name": "uploads", "path": str(allowed)},
                "upload-project",
            )
            project_id = created["payload"]["selected"]["id"]
            content = b"# Uploaded\nTwo chunks.\n"
            started = await _send(
                websocket,
                "file.upload.start",
                {"project_id": project_id, "path": "docs/readme.md", "size": len(content)},
                "upload-start",
            )
            self.assertEqual(started["type"], "file.upload.started")
            upload_id = started["payload"]["upload_id"]

            split = 8
            first = await _send(
                websocket,
                "file.upload.chunk",
                {
                    "upload_id": upload_id,
                    "index": 0,
                    "data": base64.b64encode(content[:split]).decode("ascii"),
                },
                "upload-chunk-0",
            )
            second = await _send(
                websocket,
                "file.upload.chunk",
                {
                    "upload_id": upload_id,
                    "index": 1,
                    "data": base64.b64encode(content[split:]).decode("ascii"),
                },
                "upload-chunk-1",
            )
            self.assertEqual(first["payload"]["received_size"], split)
            self.assertEqual(second["payload"]["received_size"], len(content))

            completed = await _send(
                websocket,
                "file.upload.finish",
                {"project_id": project_id, "upload_id": upload_id},
                "upload-finish",
            )
            self.assertEqual(completed["type"], "file.upload.completed")
            preview = await _send(
                websocket,
                "file.read",
                {"project_id": project_id, "path": "docs/readme.md"},
                "upload-preview",
            )
            self.assertEqual(preview["payload"]["kind"], "text")
            self.assertEqual(preview["payload"]["content"].encode("utf-8"), content)
            listing = await _send(
                websocket,
                "file.list",
                {"project_id": project_id, "path": "docs"},
                "upload-list",
            )
            entries = {item["name"]: item for item in listing["payload"]["entries"]}
            self.assertEqual(entries["readme.md"]["size"], len(content))

            written = await _send(
                websocket,
                "file.write",
                {
                    "project_id": project_id,
                    "path": "docs/readme.md",
                    "content": "# Edited\nSaved from RemoteCodex.\n",
                },
                "file-write",
            )
            self.assertEqual(written["type"], "file.write.snapshot")
            edited = await _send(
                websocket,
                "file.read",
                {"project_id": project_id, "path": "docs/readme.md"},
                "file-read-after-write",
            )
            self.assertEqual(edited["payload"]["content"], "# Edited\nSaved from RemoteCodex.\n")
            self.assertEqual(edited["payload"]["size"], len("# Edited\nSaved from RemoteCodex.\n".encode("utf-8")))

            empty = await _send(
                websocket,
                "file.upload.start",
                {"project_id": project_id, "path": "docs/empty.txt", "size": 0},
                "empty-upload-start",
            )
            empty_done = await _send(
                websocket,
                "file.upload.finish",
                {
                    "project_id": project_id,
                    "upload_id": empty["payload"]["upload_id"],
                },
                "empty-upload-finish",
            )
            self.assertEqual(empty_done["payload"]["size"], 0)
            listing = await _send(
                websocket,
                "file.list",
                {"project_id": project_id, "path": "docs"},
                "upload-list-after-write",
            )
            entries = {item["name"]: item for item in listing["payload"]["entries"]}
            self.assertEqual(entries["readme.md"]["size"], len("# Edited\nSaved from RemoteCodex.\n".encode("utf-8")))
            self.assertEqual(entries["empty.txt"]["size"], 0)
        finally:
            await websocket.close()

    async def test_file_upload_rejects_traversal_bad_chunks_and_incomplete_finish(self):
        allowed = self.tmp / "project"
        _, token = await self.app.devices.create("upload-validation-phone")
        websocket = await websockets.connect(
            f"ws://127.0.0.1:{self.app.config.server.port}"
        )
        try:
            await _send(websocket, "hello", {"device_token": token})
            created = await _send(
                websocket,
                "project.create",
                {"name": "uploads", "path": str(allowed)},
                "validation-project",
            )
            project_id = created["payload"]["selected"]["id"]

            traversal = await _send(
                websocket,
                "file.upload.start",
                {"project_id": project_id, "path": "../escape.txt", "size": 1},
                "upload-traversal",
            )
            self.assertEqual(traversal["type"], "error")
            self.assertEqual(traversal["payload"]["code"], "project.not_allowed")

            started = await _send(
                websocket,
                "file.upload.start",
                {"project_id": project_id, "path": "partial.txt", "size": 4},
                "partial-upload-start",
            )
            upload_id = started["payload"]["upload_id"]
            bad_chunk = await _send(
                websocket,
                "file.upload.chunk",
                {"upload_id": upload_id, "index": 1, "data": "YQ=="},
                "upload-out-of-order",
            )
            self.assertEqual(bad_chunk["type"], "error")
            self.assertFalse((allowed / "partial.txt").exists())
            self.assertFalse(list(allowed.glob(".partial.txt.remote-upload-*")))

            incomplete = await _send(
                websocket,
                "file.upload.start",
                {"project_id": project_id, "path": "incomplete.txt", "size": 4},
                "incomplete-upload-start",
            )
            incomplete_done = await _send(
                websocket,
                "file.upload.finish",
                {
                    "project_id": project_id,
                    "upload_id": incomplete["payload"]["upload_id"],
                },
                "incomplete-upload-finish",
            )
            self.assertEqual(incomplete_done["type"], "error")
            self.assertFalse((allowed / "incomplete.txt").exists())
            self.assertFalse(list(allowed.glob(".incomplete.txt.remote-upload-*")))
        finally:
            await websocket.close()

    async def test_html_preview_serves_files_and_relative_assets_with_cookie(self):
        allowed = self.tmp / "project"
        (allowed / "remote-uploads").mkdir()
        (allowed / "index.html").write_text(
            '<!doctype html><link rel="stylesheet" href="/site.css">',
            encoding="utf-8",
        )
        (allowed / "site.css").write_text("body{color:#e8703a}", encoding="utf-8")
        _, token = await self.app.devices.create("preview-phone")
        websocket = await websockets.connect(
            f"ws://127.0.0.1:{self.app.config.server.port}"
        )
        try:
            ready = await _send(websocket, "hello", {"device_token": token})
            preview = ready["payload"]["preview"]
            self.assertEqual(preview["host"], self.app.admin.lan_ip)
            self.assertEqual(preview["port"], self.app.admin.preview_port)
            created = await _send(
                websocket,
                "project.create",
                {"name": "preview", "path": str(allowed)},
                "preview-project",
            )
            project_id = created["payload"]["selected"]["id"]
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPCookieProcessor(),
            )
            base = f"http://127.0.0.1:{preview['port']}"
            try:
                html = await asyncio.to_thread(
                    opener.open,
                    f"{base}/preview/index.html?token={token}&project_id={project_id}",
                    timeout=3,
                )
            except HTTPError as exc:
                print("PORTS", self.app.admin.port, self.app.admin.preview_port, preview)
                print("REMOTE_TEST_401", exc.headers, exc.read())
                raise
            self.assertEqual(html.headers.get_content_type(), "text/html")
            css = await asyncio.to_thread(opener.open, f"{base}/site.css", timeout=3)
            self.assertEqual(css.read(), b"body{color:#e8703a}")
            with self.assertRaises(HTTPError) as invalid:
                await asyncio.to_thread(
                    urllib.request.urlopen,
                    f"{base}/preview/index.html?token=invalid&project_id={project_id}",
                    timeout=3,
                )
            self.assertEqual(invalid.exception.code, 401)
            with self.assertRaises(HTTPError) as missing:
                await asyncio.to_thread(
                    opener.open,
                    f"{base}/preview/missing.html",
                    timeout=3,
                )
            self.assertEqual(missing.exception.code, 404)
            with self.assertRaises(HTTPError) as traversal:
                await asyncio.to_thread(
                    opener.open,
                    f"{base}/preview/%2e%2e/private",
                    timeout=3,
                )
            self.assertEqual(traversal.exception.code, 400)
        finally:
            await websocket.close()

    async def test_file_upload_requires_explicit_overwrite(self):
        allowed = self.tmp / "project"
        target = allowed / "existing.txt"
        target.write_text("before", encoding="utf-8")
        _, token = await self.app.devices.create("overwrite-phone")
        websocket = await websockets.connect(
            f"ws://127.0.0.1:{self.app.config.server.port}"
        )
        try:
            await _send(websocket, "hello", {"device_token": token})
            created = await _send(
                websocket,
                "project.create",
                {"name": "uploads", "path": str(allowed)},
                "overwrite-project",
            )
            project_id = created["payload"]["selected"]["id"]
            rejected = await _send(
                websocket,
                "file.upload.start",
                {"project_id": project_id, "path": "existing.txt", "size": 5},
                "overwrite-rejected",
            )
            self.assertEqual(rejected["type"], "error")
            self.assertEqual(rejected["payload"]["code"], "file.exists")
            self.assertEqual(target.read_text(encoding="utf-8"), "before")

            started = await _send(
                websocket,
                "file.upload.start",
                {
                    "project_id": project_id,
                    "path": "existing.txt",
                    "size": 5,
                    "overwrite": True,
                },
                "overwrite-start",
            )
            upload_id = started["payload"]["upload_id"]
            await _send(
                websocket,
                "file.upload.chunk",
                {
                    "upload_id": upload_id,
                    "index": 0,
                    "data": base64.b64encode(b"after").decode("ascii"),
                },
                "overwrite-chunk",
            )
            completed = await _send(
                websocket,
                "file.upload.finish",
                {"project_id": project_id, "upload_id": upload_id},
                "overwrite-finish",
            )
            self.assertEqual(completed["type"], "file.upload.completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "after")
        finally:
            await websocket.close()

    async def test_terminal_runs_in_project_and_is_cleaned_up(self):
        allowed = self.tmp / "project"
        _, token = await self.app.devices.create("terminal-phone")
        websocket = await websockets.connect(
            f"ws://127.0.0.1:{self.app.config.server.port}"
        )
        try:
            ready = await _send(websocket, "hello", {"device_token": token})
            self.assertTrue(ready["payload"]["capabilities"]["terminal"])
            self.assertIsInstance(
                ready["payload"]["capabilities"]["terminal_pty"],
                bool,
            )
            created = await _send(
                websocket,
                "project.create",
                {"name": "terminal", "path": str(allowed)},
                "terminal-project",
            )
            project_id = created["payload"]["selected"]["id"]
            started = await _send(
                websocket,
                "terminal.start",
                {"project_id": project_id, "cols": 80, "rows": 24},
                "terminal-start",
            )
            self.assertEqual(started["type"], "terminal.ready")
            self.assertEqual(started["payload"]["cwd"], str(allowed))
            self.assertIn(started["payload"]["line_ending"], {"\n", "\r\n"})
            session_id = started["payload"]["session_id"]
            self.assertTrue(session_id)

            await websocket.send(json.dumps({
                "v": 1,
                "id": "terminal-input",
                "type": "terminal.input",
                "payload": {"data": "echo terminal-ok\r\n"},
            }))
            output = ""
            acknowledged = False
            while "terminal-ok" not in output or not acknowledged:
                message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
                if message.get("id") == "terminal-input":
                    self.assertEqual(message["type"], "ok")
                    acknowledged = True
                if message.get("type") == "terminal.output":
                    self.assertEqual(message["payload"]["session_id"], session_id)
                    output += str(message["payload"].get("data") or "")
            self.assertIn("terminal-ok", output)

            resized = await _send(
                websocket,
                "terminal.resize",
                {"cols": 100, "rows": 30},
                "terminal-resize",
            )
            self.assertEqual(resized["type"], "ok")
            self.assertEqual(resized["payload"]["cols"], 100)
            self.assertEqual(resized["payload"]["rows"], 30)

            await websocket.send(json.dumps({
                "v": 1,
                "id": "terminal-close",
                "type": "terminal.close",
                "payload": {},
            }))
            exited = False
            closed = False
            while not exited or not closed:
                message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))
                if message.get("type") == "terminal.exit":
                    self.assertEqual(message["payload"]["session_id"], session_id)
                    exited = True
                if message.get("id") == "terminal-close":
                    self.assertEqual(message["type"], "ok")
                    closed = True
            self.assertFalse(self.app.server._terminal_sessions)
            self.assertFalse(self.app.server._terminal_tasks)

            rejected = await _send(
                websocket,
                "terminal.input",
                {"data": "echo should-not-run\r\n"},
                "terminal-after-close",
            )
            self.assertEqual(rejected["type"], "error")
            self.assertEqual(rejected["payload"]["code"], "terminal.not_running")

            restarted = await _send(
                websocket,
                "terminal.start",
                {"project_id": project_id, "cols": 80, "rows": 24},
                "terminal-restart",
            )
            self.assertEqual(restarted["type"], "terminal.ready")
            restarted_session_id = restarted["payload"]["session_id"]
            line_ending = restarted["payload"]["line_ending"]
            await websocket.send(json.dumps({
                "v": 1,
                "id": "terminal-exit",
                "type": "terminal.input",
                "payload": {"data": f"exit{line_ending}"},
            }))
            exited = False
            acknowledged = False
            while not exited or not acknowledged:
                message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
                if message.get("id") == "terminal-exit":
                    self.assertEqual(message["type"], "ok")
                    acknowledged = True
                if message.get("type") == "terminal.exit":
                    self.assertEqual(message["payload"]["session_id"], restarted_session_id)
                    exited = True
            for _ in range(20):
                if (
                    not self.app.server._terminal_sessions
                    and not self.app.server._terminal_tasks
                ):
                    break
                await asyncio.sleep(0.05)
            self.assertFalse(self.app.server._terminal_sessions)
            self.assertFalse(self.app.server._terminal_tasks)
        finally:
            await websocket.close()

        for _ in range(20):
            if not self.app.server._terminal_sessions:
                break
            await asyncio.sleep(0.1)
        self.assertFalse(self.app.server._terminal_sessions)
        self.assertFalse(self.app.server._terminal_tasks)
