from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from websockets.asyncio.client import connect


ADMIN = "http://127.0.0.1:7801"
AGENT = "ws://127.0.0.1:7800"


def admin_api(path: str, body: dict | None = None) -> dict:
    url = ADMIN + path
    data = None if body is None else urllib.parse.urlencode(body).encode()
    request = urllib.request.Request(url, data=data)
    request.add_unredirected_header("X-Admin-Token", admin_token())
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read())


def admin_token() -> str:
    link = Path(tempfile.gettempdir()) / "RemoteCodex" / "admin-console.txt"
    if not link.exists():
        raise RuntimeError(f"admin link not found: {link}")
    return link.read_text(encoding="utf-8").splitlines()[1].split("token=", 1)[1]


async def request(websocket, message_type: str, payload: dict, request_id: str):
    await websocket.send(json.dumps({
        "v": 1,
        "id": request_id,
        "type": message_type,
        "payload": payload,
    }))
    while True:
        message = json.loads(await websocket.recv())
        if message.get("id") == request_id:
            if message.get("type") == "error":
                raise RuntimeError(message["payload"]["message"])
            return message


async def main() -> None:
    pairing = await asyncio.to_thread(
        admin_api, "/api/pairings", {"device_name": "sync-smoke"}
    )
    async with connect(AGENT) as websocket:
        await websocket.send(json.dumps({
            "v": 1,
            "id": "pair",
            "type": "pair.request",
            "payload": {
                "code": pairing["code"],
                "device_name": "sync-smoke",
            },
        }))
        await asyncio.sleep(0.15)
        pending = (await asyncio.to_thread(admin_api, "/api/state"))["pairings"]
        matching = [item for item in pending if item["id"] == pairing["pairing_id"]]
        if not matching:
            raise RuntimeError("pairing request did not appear")
        await asyncio.to_thread(
            admin_api,
            f"/api/pairings/{matching[0]['id']}/approve",
            {},
        )
        approved = json.loads(await websocket.recv())
        if approved.get("type") != "pair.approved":
            raise RuntimeError(f"pairing failed: {approved}")
        token = approved["payload"]["device_token"]
        device_id = approved["payload"]["device_id"]

    async with connect(AGENT) as websocket:
        await request(websocket, "hello", {"device_token": token}, "hello")
        projects = (await request(
            websocket, "project.list", {}, "projects"
        ))["payload"]["projects"]
        project = next(
            (item for item in projects if Path(item["normalized_path"]).name.lower() == "remoteai"),
            None,
        )
        if project is None:
            created = await request(
                websocket,
                "project.create",
                {"name": "remoteAi", "path": str(Path.cwd().parent)},
                "create-remoteai",
            )
            project = created["payload"]["selected"]

        history = (await request(
            websocket,
            "codex.history",
            {"project_id": project["id"], "limit": 20},
            "history",
        ))["payload"]
        files = (await request(
            websocket,
            "file.list",
            {"project_id": project["id"], "path": ""},
            "files",
        ))["payload"]

        print(json.dumps({
            "project": project["name"],
            "history_messages": len(history["messages"]),
            "history_preview": history["messages"][-2:],
            "matched_sessions": history["matched_sessions"],
            "file_entries": len(files["entries"]),
        }, ensure_ascii=False, indent=2))

    await asyncio.to_thread(
        admin_api,
        f"/api/devices/{device_id}/revoke",
        {},
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise
