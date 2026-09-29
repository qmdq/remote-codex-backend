from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def token_hash(token: str, pepper: str) -> str:
    return hmac.new(pepper.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def new_device_id() -> str:
    return f"dev_{secrets.token_hex(8)}"


def new_device_token() -> str:
    return f"rtc_{secrets.token_urlsafe(32)}"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def constant_time_equals(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
