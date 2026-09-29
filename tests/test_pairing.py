import asyncio
import tempfile
import unittest
from pathlib import Path
import shutil

from app.auth.devices import DeviceService
from app.auth.pairing import PairingService
from app.storage.database import Database


class PairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-codex-pair-"))
        self.db = Database(self.tmp / "pairing.db3")
        await self.db.connect()
        self.devices = DeviceService(self.db, "pepper")
        self.pairing = PairingService(
            self.db,
            self.devices,
            poll_interval=0.01,
            confirm_ttl_sec=0.08,
        )

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_invalid_code_is_rejected(self):
        await self.pairing.issue("phone")
        with self.assertRaises(ValueError):
            await self.pairing.wait_for_approval("000000", "phone")

    async def test_wait_returns_after_approval(self):
        pairing_id, code = await self.pairing.issue("phone")
        pending = asyncio.create_task(
            self.pairing.wait_for_approval(code, "E2E Phone")
        )
        await asyncio.sleep(0.01)
        self.assertFalse(pending.done())

        self.assertTrue(await self.pairing.approve(pairing_id))
        device_id, token = await asyncio.wait_for(pending, timeout=0.2)
        self.assertTrue(device_id.startswith("dev_"))
        self.assertTrue(token.startswith("rtc_"))

        row = await self.db.fetch_one(
            "SELECT * FROM pairing_codes WHERE id = ?", (pairing_id,)
        )
        self.assertIsNotNone(row["consumed_at"])
