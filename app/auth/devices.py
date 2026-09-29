from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Row

from ..storage.database import Database
from .tokens import iso_now, new_device_id, new_device_token, token_hash


@dataclass(slots=True)
class AuthenticatedDevice:
    id: str
    name: str


class DeviceService:
    def __init__(self, database: Database, pepper: str):
        self.db = database
        self.pepper = pepper

    async def create(self, name: str) -> tuple[str, str]:
        device_id = new_device_id()
        token = new_device_token()
        await self.db.execute(
            """INSERT INTO devices(id, name, token_hash, created_at)
               VALUES (?, ?, ?, ?)""",
            (device_id, name, token_hash(token, self.pepper), iso_now()),
        )
        return device_id, token

    async def authenticate(self, token: str | None) -> AuthenticatedDevice:
        if not token or not isinstance(token, str):
            raise ValueError("missing device token")
        row = await self.db.fetch_one(
            """SELECT id, name, token_hash, revoked_at
               FROM devices WHERE token_hash = ?""",
            (token_hash(token, self.pepper),),
        )
        if row is None or row["revoked_at"] is not None:
            raise ValueError("invalid or revoked device token")
        await self.db.execute(
            "UPDATE devices SET last_seen_at = ? WHERE id = ?",
            (iso_now(), row["id"]),
        )
        return AuthenticatedDevice(id=row["id"], name=row["name"])

    async def revoke(self, device_id: str) -> bool:
        cursor = await self.db.execute(
            "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (iso_now(), device_id),
        )
        return bool(cursor.rowcount)

    async def list(self) -> list[Row]:
        return await self.db.fetch_all(
            """SELECT id, name, created_at, last_seen_at, revoked_at
               FROM devices ORDER BY created_at DESC"""
        )
