from __future__ import annotations

import asyncio
import secrets
import string
from datetime import datetime, timedelta

from ..storage.database import Database
from .devices import DeviceService
from .tokens import iso_now, new_id, token_hash, utcnow


class PairingService:
    def __init__(
        self,
        database: Database,
        devices: DeviceService,
        *,
        ttl_sec: float = 300,
        max_attempts: int = 5,
        poll_interval: float = 0.5,
        confirm_ttl_sec: float = 60,
    ):
        self.db = database
        self.devices = devices
        self.ttl_sec = ttl_sec
        self.max_attempts = max_attempts
        self.poll_interval = poll_interval
        self.wait_timeout = confirm_ttl_sec

    async def issue(self, device_name: str = "Mobile device") -> tuple[str, str]:
        code = "".join(secrets.choice(string.digits) for _ in range(6))
        pairing_id = new_id("pair")
        expires = utcnow() + timedelta(seconds=self.ttl_sec)
        await self.db.execute(
            """INSERT INTO pairing_codes(
                 id, code_hash, device_name, expires_at, created_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                pairing_id,
                token_hash(code, self.pepper()),
                device_name,
                expires.isoformat(),
                iso_now(),
            ),
        )
        return pairing_id, code

    def pepper(self) -> str:
        return self.devices.pepper

    async def _find_valid(self, code: str):
        hashed = token_hash(code, self.pepper())
        return await self.db.fetch_one(
            """SELECT * FROM pairing_codes
               WHERE code_hash = ?
               ORDER BY created_at DESC LIMIT 1""",
            (hashed,),
        )

    async def wait_for_approval(self, code: str, device_name: str) -> tuple[str, str]:
        row = await self._find_valid(code)
        now = utcnow()
        if row is None:
            raise ValueError("invalid pairing code")
        if row["consumed_at"] is not None:
            raise ValueError("pairing code already used")
        if row["attempts"] >= self.max_attempts:
            raise ValueError("pairing code is locked")
        expires = datetime.fromisoformat(row["expires_at"])
        if now > expires:
            raise ValueError("pairing code expired")
        if row["approved_at"] is None:
            await self.db.execute(
                "UPDATE pairing_codes SET attempts = attempts + 1 WHERE id = ?",
                (row["id"],),
            )

        deadline = utcnow() + timedelta(seconds=self.wait_timeout)
        while utcnow() < deadline:
            current = await self.db.fetch_one(
                "SELECT * FROM pairing_codes WHERE id = ?", (row["id"],)
            )
            if current is None or current["consumed_at"] is not None:
                raise ValueError("pairing request is no longer valid")
            if current["attempts"] >= self.max_attempts:
                raise ValueError("pairing code is locked")
            if current["approved_at"] is not None:
                await self.db.execute(
                    """UPDATE pairing_codes
                       SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL""",
                    (iso_now(), row["id"]),
                )
                return await self.devices.create(device_name or current["device_name"])
            await asyncio.sleep(self.poll_interval)
        raise TimeoutError("pairing approval timed out")

    async def approve(self, pairing_id: str) -> bool:
        cursor = await self.db.execute(
            """UPDATE pairing_codes
               SET approved_at = ?
               WHERE id = ? AND approved_at IS NULL AND consumed_at IS NULL
                 AND expires_at > ? AND attempts < ?""",
            (iso_now(), pairing_id, iso_now(), self.max_attempts),
        )
        return bool(cursor.rowcount)

    async def reject(self, pairing_id: str) -> bool:
        cursor = await self.db.execute(
            """UPDATE pairing_codes
               SET consumed_at = ?
               WHERE id = ? AND consumed_at IS NULL""",
            (iso_now(), pairing_id),
        )
        return bool(cursor.rowcount)

    async def pending(self) -> list:
        return await self.db.fetch_all(
            """SELECT id, device_name, expires_at, attempts, created_at
               FROM pairing_codes
               WHERE approved_at IS NULL AND consumed_at IS NULL
                 AND expires_at > ? AND attempts < ?
               ORDER BY created_at""",
            (iso_now(), self.max_attempts),
        )
