from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..auth.tokens import iso_now
from ..auth.tokens import new_id
from .manager import ProjectService


@dataclass(slots=True)
class DirectoryAuthorizationRequest:
    id: str
    project_id: str
    project_name: str
    reason: str
    status: str
    created_at: str
    expires_at: str

    def wire(self) -> dict[str, Any]:
        return {
            "request_id": self.id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "reason": self.reason,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class DirectoryAuthorizationService:
    """In-memory, short-lived directory grants confirmed from the PC console."""

    def __init__(self, *, ttl_sec: float = 600):
        self.ttl_sec = ttl_sec
        self._requests: dict[str, DirectoryAuthorizationRequest] = {}

    def _purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [
            item for item in self._requests.values()
            if datetime.fromisoformat(item.expires_at.replace("Z", "+00:00")) <= now
        ]
        for item in expired:
            if item.status == "pending":
                item.status = "expired"
            self._requests.pop(item.id, None)

    async def request(self, project: dict[str, Any], reason: str) -> dict[str, Any]:
        if int(project.get("is_temporary") or 0) != 1:
            raise ValueError("this conversation does not require directory authorization")
        self._purge_expired()
        pending = next(
            (
                item for item in self._requests.values()
                if item.project_id == str(project["id"]) and item.status == "pending"
            ),
            None,
        )
        if pending:
            pending.reason = reason or pending.reason
            return pending.wire()
        now = datetime.now(timezone.utc)
        request = DirectoryAuthorizationRequest(
            id=new_id("authreq"),
            project_id=str(project["id"]),
            project_name=str(project.get("name") or "临时聊天"),
            reason=reason.strip() or "访问 PC 目录",
            status="pending",
            created_at=now.isoformat().replace("+00:00", "Z"),
            expires_at=(now + timedelta(seconds=self.ttl_sec)).isoformat().replace("+00:00", "Z"),
        )
        self._requests[request.id] = request
        return request.wire()

    async def pending(self) -> list[dict[str, Any]]:
        self._purge_expired()
        return [
            item.wire() for item in self._requests.values()
            if item.status == "pending"
        ]

    async def get(self, request_id: str) -> DirectoryAuthorizationRequest:
        self._purge_expired()
        request = self._requests.get(request_id)
        if request is None or request.status != "pending":
            raise ValueError("authorization request does not exist or has expired")
        return request

    async def approve(
        self,
        request_id: str,
        raw_path: str,
        projects: ProjectService,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        request = await self.get(request_id)
        project = await projects.authorize_temporary(request.project_id, raw_path)
        request.status = "approved"
        return project, request.wire()

    async def reject(self, request_id: str) -> dict[str, Any]:
        request = await self.get(request_id)
        request.status = "rejected"
        return request.wire()
