from __future__ import annotations

import secrets

from ..storage.database import Database


async def load_or_create_pepper(database: Database) -> str:
    row = await database.fetch_one(
        "SELECT value FROM settings WHERE key = 'server_pepper'"
    )
    if row:
        return str(row["value"])
    pepper = secrets.token_hex(32)
    await database.execute(
        "INSERT OR IGNORE INTO settings(key, value) VALUES ('server_pepper', ?)",
        (pepper,),
    )
    row = await database.fetch_one(
        "SELECT value FROM settings WHERE key = 'server_pepper'"
    )
    if row is None:
        raise RuntimeError("failed to persist server pepper")
    return str(row["value"])
