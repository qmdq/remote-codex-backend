import tempfile
import unittest
from pathlib import Path
import shutil

from app.auth.devices import DeviceService
from app.auth.settings import load_or_create_pepper
from app.projects.manager import ProjectService
from app.protocol.errors import PathNotAllowedError
from app.storage.database import Database


class AuthAndProjectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-codex-test-"))
        self.db = Database(self.tmp / "test.db3")
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_device_token_is_only_returned_once(self):
        devices = DeviceService(self.db, "pepper")
        device_id, token = await devices.create("phone")
        self.assertTrue(token.startswith("rtc_"))
        authenticated = await devices.authenticate(token)
        self.assertEqual(authenticated.id, device_id)
        await devices.revoke(device_id)
        with self.assertRaises(ValueError):
            await devices.authenticate(token)

    async def test_pepper_is_stable(self):
        first = await load_or_create_pepper(self.db)
        second = await load_or_create_pepper(self.db)
        self.assertEqual(first, second)

    async def test_project_must_be_inside_allowed_root(self):
        allowed = self.tmp / "allowed"
        outside = self.tmp / "outside"
        allowed.mkdir()
        outside.mkdir()
        projects = ProjectService(self.db, [allowed])
        project = await projects.create("demo", str(allowed))
        self.assertEqual(project["name"], "demo")
        with self.assertRaises(PathNotAllowedError):
            await projects.create("outside", str(outside))

    async def test_codex_project_import_is_allowed_and_idempotent(self):
        allowed = self.tmp / "allowed"
        outside = self.tmp / "outside"
        allowed.mkdir()
        outside.mkdir()
        projects = ProjectService(self.db, [allowed])

        imported, created = await projects.import_codex_project(str(allowed), "codex")
        duplicate, created_again = await projects.import_codex_project(str(allowed), "ignored")

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(imported["id"], duplicate["id"])
        self.assertEqual(imported["name"], "codex")
        with self.assertRaises(PathNotAllowedError):
            await projects.import_codex_project(str(outside))

    async def test_codex_import_restores_archived_project(self):
        allowed = self.tmp / "allowed"
        allowed.mkdir()
        projects = ProjectService(self.db, [allowed])
        original = await projects.create("saved-name", str(allowed))
        await self.db.execute(
            "UPDATE projects SET archived = 1, model = ? WHERE id = ?",
            ("saved-model", original["id"]),
        )

        restored, created = await projects.import_codex_project(str(allowed), "codex-name")

        self.assertFalse(created)
        self.assertEqual(restored["id"], original["id"])
        self.assertEqual(restored["name"], "saved-name")
        self.assertEqual(restored["model"], "saved-model")
