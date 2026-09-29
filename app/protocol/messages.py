from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import ValidationError


@dataclass(slots=True)
class Envelope:
    v: int
    type: str
    payload: dict[str, Any]
    id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"v": self.v, "type": self.type}
        if self.id is not None:
            result["id"] = self.id
        result["payload"] = self.payload
        return result

    def json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


def parse_envelope(raw: str | bytes) -> Envelope:
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("message must be valid JSON") from exc
    if not isinstance(data, dict):
        raise ValidationError("message must be a JSON object")
    version = data.get("v")
    if version != 1:
        raise UnsupportedVersionError("only protocol version 1 is supported")
    message_type = data.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise ValidationError("type is required")
    payload = data.get("payload", {})
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValidationError("payload must be an object")
    message_id = data.get("id")
    if message_id is not None and not isinstance(message_id, (str, int)):
        raise ValidationError("id must be a string or integer")
    return Envelope(
        v=1,
        type=message_type,
        payload=payload,
        id=str(message_id) if message_id is not None else None,
    )


def response(
    message_type: str,
    payload: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
) -> Envelope:
    return Envelope(v=1, type=message_type, payload=payload or {}, id=request_id)


def error_payload(exc: Exception) -> dict[str, Any]:
    code = getattr(exc, "code", "agent.error")
    retryable = getattr(exc, "retryable", False)
    return {"code": code, "message": str(exc), "retryable": retryable}
