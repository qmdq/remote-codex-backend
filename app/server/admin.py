from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import socket
import threading
import mimetypes
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..projects.paths import normalize_path


class AdminServer:
    def __init__(self, app: Any, host: str = "127.0.0.1", port: int = 7801):
        self.app = app
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.lan_ip = detect_lan_ip()
        self._server: ThreadingHTTPServer | None = None
        self._preview_server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._preview_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = self._bind_with_fallback()
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="remote-codex-admin",
            daemon=True,
        )
        self._thread.start()
        self._preview_server = self._bind_preview_with_fallback()
        self._preview_thread = threading.Thread(
            target=self._preview_server.serve_forever,
            name="remote-codex-preview",
            daemon=True,
        )
        self._preview_thread.start()

    def _bind_preview_with_fallback(self) -> "PreviewHTTPServer":
        last_error: Exception | None = None
        for offset in range(20):
            port = self.port + 1 + offset
            try:
                return PreviewHTTPServer(("0.0.0.0", port), self)
            except OSError as exc:
                last_error = exc
        raise OSError(f"no available preview port near {self.port + 1}") from last_error

    def _bind_with_fallback(self) -> "ConsoleHTTPServer":
        last_error: Exception | None = None
        for offset in range(20):
            port = self.port + offset
            try:
                return ConsoleHTTPServer((self.host, port), self)
            except OSError as exc:
                last_error = exc
        raise OSError(f"no available admin port near {self.port}") from last_error

    async def stop(self) -> None:
        if self._server is not None:
            await asyncio.to_thread(self._server.shutdown)
            self._server.server_close()
        if self._preview_server is not None:
            await asyncio.to_thread(self._preview_server.shutdown)
            self._preview_server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._preview_thread is not None:
            self._preview_thread.join(timeout=2)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/admin?token={self.token}"

    @property
    def preview_port(self) -> int:
        if self._preview_server is None:
            return self.port + 1
        return int(self._preview_server.server_address[1])

    async def get_state(self) -> dict[str, Any]:
        return {
            "admin": {
                "host": self.host,
                "port": self.port,
            },
            "server": {
                "host": self.app.config.server.host,
                "port": self.app.config.server.port,
                "running": bool(self.app._serve_task and not self.app._serve_task.done()),
            },
            "phone_url": phone_url(self.app.config, self.lan_ip),
            "preview_url": f"http://{self.lan_ip}:{self.preview_port}/preview/",
            "allowed_roots": [
                str(normalize_path(root)) for root in self.app.config.projects.allowed_roots
            ],
            "pairings": [dict(row) for row in await self.app.pairing.pending()],
            "directory_authorizations": await self.app.directory_authorizations.pending(),
            "devices": [
                {**dict(row), "revoked": bool(row["revoked_at"])}
                for row in await self.app.devices.list()
            ],
            "config": {
                "Agent 地址": f"ws://{format_host(self.app.config.server.host)}:{self.app.config.server.port}",
                "手机连接": phone_url(self.app.config, self.lan_ip),
                "允许目录": ", ".join(
                    str(normalize_path(root)) for root in self.app.config.projects.allowed_roots
                ),
                "Codex 模式": self.app.config.codex.mode,
                "SDK 不可用时降级": "是" if self.app.config.codex.fallback_to_fake else "否",
                "最大并发任务": self.app.config.codex.max_running_turns,
                "任务超时": f"{int(self.app.config.codex.turn_timeout_sec / 60)} 分钟",
                "数据库": str(self.app.database.path),
                "屏幕帧率上限": f"{self.app.config.monitor.screen_max_fps} fps",
                "配对码有效期": f"{int(self.app.config.security.pairing_code_ttl_sec)} 秒",
                "审批等待": f"{int(self.app.config.security.pairing_confirm_ttl_sec)} 秒",
            },
        }

    async def issue_pairing(self, device_name: str) -> dict[str, Any]:
        pairing_id, code = await self.app.pairing.issue(device_name)
        return {"pairing_id": pairing_id, "code": code}

    async def approve_authorization(self, request_id: str, raw_path: str) -> dict[str, Any]:
        project, request = await self.app.directory_authorizations.approve(
            request_id, raw_path, self.app.projects
        )
        request = {**request, "status": "approved", "project": project}
        await self.app.server.broadcast_authorization(request)
        return request

    async def reject_authorization(self, request_id: str) -> dict[str, Any]:
        request = await self.app.directory_authorizations.reject(request_id)
        request = {**request, "status": "rejected"}
        await self.app.server.broadcast_authorization(request)
        return request

    async def add_allowed_root(self, raw_path: str) -> dict[str, Any]:
        root = normalize_path(raw_path)
        if not root.exists() or not root.is_dir():
            raise ValueError("目录不存在，请先在 PC 上创建它")
        roots = [str(normalize_path(item)) for item in self.app.config.projects.allowed_roots]
        if str(root) not in roots:
            roots.append(str(root))
            self.app.config.projects.allowed_roots = roots
            self.app.projects.set_allowed_roots(roots)
            self._save()
        return {"allowed_roots": roots}

    async def list_directory(self, raw_path: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._list_directory, raw_path)

    def _list_directory(self, raw_path: str) -> dict[str, Any]:
        if not raw_path.strip():
            drives = []
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                candidate = Path(f"{letter}:/")
                try:
                    if candidate.exists():
                        drives.append(str(candidate))
                except OSError:
                    continue
            return {
                "path": "",
                "parent": "",
                "directories": drives,
                "selectable": False,
            }

        try:
            path = normalize_path(raw_path)
            if not path.exists() or not path.is_dir():
                raise ValueError("目录不存在")
            entries = []
            for item in path.iterdir():
                try:
                    if item.is_dir():
                        entries.append(str(item))
                except OSError:
                    continue
            entries.sort(key=lambda value: Path(value).name.casefold())
            parent = path.parent if path != path.parent else ""
            return {
                "path": str(path),
                "parent": parent,
                "directories": entries,
                "selectable": True,
            }
        except (OSError, ValueError) as exc:
            raise ValueError("无法读取这个目录") from exc

    async def remove_allowed_root(self, raw_path: str) -> dict[str, Any]:
        target = str(normalize_path(raw_path))
        roots = [str(normalize_path(item)) for item in self.app.config.projects.allowed_roots]
        if target not in roots:
            raise ValueError("目录不存在")
        if len(roots) == 1:
            raise ValueError("至少保留一个允许目录")
        roots.remove(target)
        self.app.config.projects.allowed_roots = roots
        self.app.projects.set_allowed_roots(roots)
        self._save()
        return {"allowed_roots": roots}

    def _save(self) -> None:
        from ..config import save_config

        if not self.app.config.config_path:
            raise ValueError("当前服务未指定配置文件")
        save_config(self.app.config, self.app.config.config_path)


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], admin: AdminServer):
        self.admin = admin
        super().__init__(address, AdminHandler)


class PreviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], admin: AdminServer):
        self.admin = admin
        super().__init__(address, PreviewHandler)


class AdminHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def admin(self) -> AdminServer:
        return self.server.admin  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        if not self._authorized():
            return self._json({"message": "unauthorized"}, 401)
        if self.path == "/":
            return self._redirect(self.admin.url)
        if self.path.startswith("/admin"):
            return self._html()
        if self.path == "/api/state":
            return self._call(self.admin.get_state())
        if self.path.startswith("/api/filesystem"):
            query = parse_qs(urlsplit(self.path).query)
            return self._call(self.admin.list_directory(query.get("path", [""])[0]))
        return self._json({"message": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._authorized():
            return self._json({"message": "unauthorized"}, 401)
        body = self._read_json()
        if self.path == "/api/pairings":
            name = str(body.get("device_name") or "Mobile device")
            return self._call(self.admin.issue_pairing(name))
        match = re.fullmatch(r"/api/pairings/([^/]+)/(approve|reject)", self.path)
        if match:
            pairing_id = unquote(match.group(1))
            if match.group(2) == "approve":
                return self._call(self._approve(pairing_id))
            return self._call(self._reject(pairing_id))
        match = re.fullmatch(r"/api/devices/([^/]+)/revoke", self.path)
        if match:
            return self._call(self._revoke(unquote(match.group(1))))
        if self.path == "/api/allowed-roots":
            return self._call(self._add_allowed_root(body.get("path", "")))
        match = re.fullmatch(
            r"/api/directory-authorizations/([^/]+)/(approve|reject)", self.path
        )
        if match:
            request_id = unquote(match.group(1))
            if match.group(2) == "approve":
                return self._call(self.admin.approve_authorization(
                    request_id, str(body.get("path", ""))
                ))
            return self._call(self.admin.reject_authorization(request_id))
        return self._json({"message": "not found"}, 404)

    def do_DELETE(self) -> None:
        if not self._authorized():
            return self._json({"message": "unauthorized"}, 401)
        if self.path.startswith("/api/allowed-roots?path="):
            path = unquote(urlsplit(self.path).query.removeprefix("path="))
            return self._call(self._remove_allowed_root(path))
        return self._json({"message": "not found"}, 404)

    async def _approve(self, pairing_id: str) -> dict[str, Any]:
        approved = await self.admin.app.pairing.approve(pairing_id)
        if not approved:
            raise ValueError("配对请求不存在或已失效")
        return {"approved": True}

    async def _reject(self, pairing_id: str) -> dict[str, Any]:
        rejected = await self.admin.app.pairing.reject(pairing_id)
        if not rejected:
            raise ValueError("配对请求不存在或已失效")
        return {"approved": False}

    async def _revoke(self, device_id: str) -> dict[str, Any]:
        revoked = await self.admin.app.devices.revoke(device_id)
        if not revoked:
            raise ValueError("设备不存在或已吊销")
        return {"revoked": True}

    async def _add_allowed_root(self, raw_path: Any) -> dict[str, Any]:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("请填写 PC 目录")
        return await self.admin.add_allowed_root(raw_path)

    async def _remove_allowed_root(self, raw_path: str) -> dict[str, Any]:
        return await self.admin.remove_allowed_root(raw_path)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Admin-Token", "")
        if supplied:
            return hmac.compare_digest(supplied.encode(), self.admin.token.encode())
        token = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
        return hmac.compare_digest(token.encode(), self.admin.token.encode())

    def _call(self, coroutine) -> None:
        loop = self.admin._loop
        if loop is None or loop.is_closed():
            return self._json({"message": "admin server stopped"}, 503)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            return self._json(future.result(timeout=8))
        except Exception as exc:
            return self._json({"message": str(exc) or "operation failed"}, 400)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
            return value if isinstance(value, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _html(self) -> None:
        path = Path(__file__).with_name("admin.html")
        content = path.read_text(encoding="utf-8").replace("__ADMIN_TOKEN__", self.admin.token)
        self._bytes(content.encode("utf-8"), "text/html; charset=utf-8")

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _json(self, value: dict[str, Any], status: int = 200) -> None:
        self._bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def _bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


class PreviewHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def admin(self) -> AdminServer:
        return self.server.admin  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        token = query.get("token", [""])[0]
        project_id = query.get("project_id", [""])[0]
        from_cookie = False
        if not token or not project_id:
            token, project_id = self._cookie_credentials()
            from_cookie = True
        if not token or not project_id:
            return self._json({"message": "unauthorized"}, 401)
        loop = self.admin._loop
        if loop is None or loop.is_closed():
            return self._json({"message": "preview server stopped"}, 503)
        try:
            auth_future = asyncio.run_coroutine_threadsafe(
                self.admin.app.devices.authenticate(token), loop
            )
            try:
                auth_future.result(timeout=5)
            except Exception:
                return self._json({"message": "unauthorized"}, 401)
            project = asyncio.run_coroutine_threadsafe(
                self.admin.app.projects.get(project_id), loop
            ).result(timeout=5)
            if int(project.get("is_temporary") or 0):
                return self._json({"message": "临时聊天未授权目录，无法预览文件"}, 400)
            if parsed.path.startswith("/preview/"):
                raw_path = unquote(parsed.path.removeprefix("/preview/")).strip("/")
            elif parsed.path in {"/preview", "/preview/"}:
                raw_path = "index.html"
            else:
                # Keep root-relative assets working while the cookie limits access.
                raw_path = unquote(parsed.path).lstrip("/")
                if Path(raw_path).is_absolute() or PureWindowsPath(raw_path).is_absolute():
                    return self._json({"message": "not found"}, 404)
            if not raw_path:
                raw_path = "index.html"
            root = self.admin.app.files._project_root(project)
            target = self.admin.app.files._resolve_inside(root, raw_path)
            if not target.exists() or not target.is_file():
                if target.suffix or raw_path.endswith("/"):
                    return self._json({"message": "file does not exist"}, 404)
                target = self.admin.app.files._resolve_inside(root, "index.html")
                if not target.exists() or not target.is_file():
                    return self._json({"message": "file does not exist"}, 404)
            payload = target.read_bytes()
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if mime == "text/html":
                mime = "text/html; charset=utf-8"
            cookies = None if from_cookie else (
                f"remote_preview_token={token}; Path=/; HttpOnly; SameSite=Lax",
                f"remote_preview_project={project_id}; Path=/; HttpOnly; SameSite=Lax",
            )
            return self._bytes(payload, mime, cookies=cookies)
        except Exception as exc:
            return self._json({"message": str(exc) or "preview failed"}, 400)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path not in {"/upload", "/upload/"}:
            return self._json({"message": "not found"}, 404)

        token = self.headers.get("X-Device-Token", "")
        if not token:
            token = parse_qs(parsed.query).get("token", [""])[0]
        if not token:
            return self._json({"message": "unauthorized"}, 401)

        loop = self.admin._loop
        if loop is None or loop.is_closed():
            return self._json({"message": "preview server stopped"}, 503)
        try:
            asyncio.run_coroutine_threadsafe(
                self.admin.app.devices.authenticate(token), loop,
            ).result(timeout=5)
        except Exception:
            return self._json({"message": "unauthorized"}, 401)

        try:
            content_type = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length", "0"))
            if not content_type.lower().startswith("multipart/form-data"):
                return self._json({"message": "expected multipart/form-data"}, 400)
            if length <= 0:
                return self._json({"message": "empty upload"}, 400)
            if length > 25 * 1024 * 1024:
                return self._json({"message": "file exceeds the 20 MB upload limit"}, 400)
            raw = self.rfile.read(length)
        except Exception:
            return self._json({"message": "failed to read upload"}, 400)

        try:
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: "
                + content_type.encode("ascii", "ignore")
                + b"\r\n\r\n"
                + raw,
            )
            if not message.is_multipart():
                return self._json({"message": "invalid multipart body"}, 400)

            fields: dict[str, str] = {}
            file_data: bytes | None = None
            file_name = ""
            for part in message.iter_parts():
                disposition = str(part.get("Content-Disposition", ""))
                if "form-data" not in disposition:
                    continue
                match = re.search(r'name="([^"]+)"', disposition)
                if not match:
                    continue
                name = match.group(1)
                if name == "file":
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        continue
                    file_data = payload
                    file_name = part.get_filename() or ""
                else:
                    payload = part.get_payload(decode=True)
                    if payload is not None:
                        fields[name] = payload.decode("utf-8", "replace")

            if file_data is None:
                return self._json({"message": "file part is missing"}, 400)

            project_id = fields.get("project_id") or parse_qs(parsed.query).get("project_id", [""])[0]
            relative_path = fields.get("path") or file_name
            overwrite = fields.get("overwrite", "false").lower() in {"1", "true", "yes"}
            if not project_id:
                return self._json({"message": "project_id is required"}, 400)
            if not relative_path:
                return self._json({"message": "file path is required"}, 400)

            project = asyncio.run_coroutine_threadsafe(
                self.admin.app.projects.get(project_id), loop,
            ).result(timeout=5)
            if int(project.get("is_temporary") or 0):
                return self._json({
                    "message": "临时聊天未授权目录，请先在 PC 控制台完成目录授权",
                    "code": "project.authorization_required",
                }, 400)
            result = asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(
                    self.admin.app.files.save_upload,
                    project,
                    relative_path,
                    file_data,
                    overwrite=overwrite,
                ),
                loop,
            ).result(timeout=10)
            return self._json(result)
        except Exception as exc:
            code = getattr(exc, "code", None)
            return self._json({"message": str(exc) or "upload failed", "code": code}, 400)

    def _cookie_credentials(self) -> tuple[str, str]:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return "", ""
        token = cookie.get("remote_preview_token")
        project = cookie.get("remote_preview_project")
        return token.value if token else "", project.value if project else ""

    def _json(self, value: dict[str, Any], status: int = 200) -> None:
        self._bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def _bytes(
        self,
        payload: bytes,
        content_type: str,
        status: int = 200,
        cookies: tuple[str, ...] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for cookie in cookies or ():
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


def format_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def phone_url(config: Any, lan_ip: str) -> str:
    return f"ws://{lan_ip}:{config.server.port}"


def detect_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()
