from __future__ import annotations

import base64
import binascii
import mimetypes
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from ..protocol.errors import PathNotAllowedError, ValidationError


IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
TEXT_EXTENSIONS = {
    ".bat", ".c", ".cc", ".cfg", ".cpp", ".cs", ".css", ".csv", ".env", ".go",
    ".h", ".hpp", ".htm", ".html", ".ini", ".java", ".js", ".json", ".jsonc",
    ".jsx", ".log", ".md", ".php", ".properties", ".ps1", ".py", ".rb", ".rs",
    ".sh", ".sql", ".svg", ".toml", ".ts", ".tsx", ".txt", ".vue", ".xml",
    ".yaml", ".yml",
}
MAX_UPLOAD_SIZE = 20 * 1024 * 1024
MAX_UPLOAD_CHUNK_SIZE = 256 * 1024
MAX_WRITE_SIZE = 2 * 1024 * 1024


@dataclass(slots=True)
class FileUpload:
    id: str
    project_id: str
    target: Path
    temporary: Path
    size: int
    overwrite: bool
    received: int = 0
    next_index: int = 0


class FileService:
    def start_upload(
        self,
        project: dict[str, Any],
        relative_path: str,
        size: int,
        *,
        overwrite: bool = False,
    ) -> FileUpload:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValidationError("file size must be a non-negative integer")
        if size > MAX_UPLOAD_SIZE:
            raise ValidationError("file exceeds the 20 MB upload limit")

        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValidationError("a file path is required")
        root = self._project_root(project)
        target = self._resolve_inside(root, relative_path)
        if target == root or not target.name:
            raise ValidationError("a file name is required")
        if not target.parent.is_dir():
            raise ValidationError("upload directory does not exist")
        if target.exists() and target.is_dir():
            raise ValidationError("target path is a directory")
        if target.exists() and not overwrite:
            raise ValidationError("a file with this name already exists", code="file.exists")

        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{target.name}.remote-upload-",
                dir=str(target.parent),
            )
            os.close(descriptor)
        except OSError as exc:
            raise ValidationError("cannot create upload file") from exc

        return FileUpload(
            id=uuid.uuid4().hex,
            project_id=str(project["id"]),
            target=target,
            temporary=Path(temporary),
            size=size,
            overwrite=overwrite,
        )

    def append_upload_chunk(self, upload: FileUpload, index: int, encoded: str) -> dict[str, int]:
        if isinstance(index, bool) or not isinstance(index, int) or index != upload.next_index:
            raise ValidationError("upload chunk is out of order")
        if not isinstance(encoded, str) or not encoded or len(encoded) > 350_000:
            raise ValidationError("upload chunk is invalid")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValidationError("upload chunk is not valid base64") from exc
        if not chunk or len(chunk) > MAX_UPLOAD_CHUNK_SIZE:
            raise ValidationError("upload chunk size is invalid")
        if upload.received + len(chunk) > upload.size:
            raise ValidationError("upload exceeds the declared file size")

        try:
            with upload.temporary.open("ab") as handle:
                handle.write(chunk)
        except OSError as exc:
            raise ValidationError("cannot write upload chunk") from exc
        upload.received += len(chunk)
        upload.next_index += 1
        return {"received_size": upload.received, "next_index": upload.next_index}

    def save_upload(
        self,
        project: dict[str, Any],
        relative_path: str,
        data: bytes,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Write a complete multipart upload directly to the project directory."""

        if not isinstance(data, bytes):
            raise ValidationError("uploaded file data is required")
        if len(data) > MAX_UPLOAD_SIZE:
            raise ValidationError("file exceeds the 20 MB upload limit")
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValidationError("a file path is required")

        root = self._project_root(project)
        target = self._resolve_inside(root, relative_path)
        if target == root or not target.name:
            raise ValidationError("a file name is required")
        if not target.parent.is_dir():
            raise ValidationError("upload directory does not exist")
        if target.exists() and target.is_dir():
            raise ValidationError("target path is a directory")
        if target.exists() and not overwrite:
            raise ValidationError("a file with this name already exists", code="file.exists")

        temporary: Path | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.remote-upload-",
                dir=str(target.parent),
            )
            temporary = Path(temp_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary, target)
            temporary = None
            size = target.stat().st_size
        except OSError as exc:
            raise ValidationError("cannot finalize uploaded file") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

        return {
            "project_id": project["id"],
            "path": self._relative_text(root, target),
            "name": target.name,
            "size": size,
        }

    def finish_upload(self, project: dict[str, Any], upload: FileUpload) -> dict[str, Any]:
        if str(project.get("id", "")) != upload.project_id:
            raise PathNotAllowedError("upload belongs to a different project")
        if upload.received != upload.size:
            raise ValidationError("uploaded file size does not match")
        root = self._project_root(project)
        try:
            upload.target.relative_to(root)
        except ValueError as exc:
            raise PathNotAllowedError("path is outside the project") from exc
        relative_path = self._relative_text(root, upload.target)
        current_target = self._resolve_inside(root, relative_path)
        if current_target != upload.target:
            raise PathNotAllowedError("upload target changed during transfer")
        if upload.target.exists() and upload.target.is_dir():
            raise ValidationError("target path is a directory")
        if upload.target.exists() and not upload.overwrite:
            raise ValidationError("a file with this name already exists", code="file.exists")

        try:
            os.replace(upload.temporary, upload.target)
            size = upload.target.stat().st_size
        except OSError as exc:
            raise ValidationError("cannot finalize uploaded file") from exc
        return {
            "project_id": project["id"],
            "path": self._relative_text(root, upload.target),
            "name": upload.target.name,
            "size": size,
        }

    def cancel_upload(self, upload: FileUpload) -> None:
        try:
            upload.temporary.unlink(missing_ok=True)
        except OSError:
            pass

    def list(self, project: dict[str, Any], relative_path: str = "") -> dict[str, Any]:
        root = self._project_root(project)
        target = self._resolve_inside(root, relative_path)
        if not target.exists():
            raise ValidationError("path does not exist")
        if not target.is_dir():
            raise ValidationError("path is not a directory")

        entries: list[dict[str, Any]] = []
        try:
            children = list(target.iterdir())
        except OSError as exc:
            raise ValidationError("cannot read directory") from exc

        for child in children:
            try:
                stat = child.stat()
                is_dir = child.is_dir()
            except OSError:
                continue
            entries.append({
                "name": child.name,
                "path": self._relative_text(root, child),
                "type": "directory" if is_dir else "file",
                "size": 0 if is_dir else stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })

        entries.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
        return {
            "project_id": project["id"],
            "path": self._relative_text(root, target),
            "entries": entries[:500],
            "truncated": len(entries) > 500,
        }

    def read(
        self,
        project: dict[str, Any],
        relative_path: str,
        *,
        max_bytes: int = 320 * 1024,
    ) -> dict[str, Any]:
        root = self._project_root(project)
        target = self._resolve_inside(root, relative_path)
        if not target.exists() or not target.is_file():
            raise ValidationError("file does not exist")

        try:
            stat = target.stat()
        except OSError as exc:
            raise ValidationError("cannot inspect file") from exc

        suffix = target.suffix.lower()
        requested = max(1024, min(int(max_bytes), 1024 * 1024))
        if suffix in IMAGE_EXTENSIONS:
            return self._read_image(project, root, target, stat.st_size)

        if suffix in TEXT_EXTENSIONS or suffix == "":
            return self._read_text(project, root, target, stat.st_size, requested)

        return {
            "project_id": project["id"],
            "path": self._relative_text(root, target),
            "name": target.name,
            "kind": "binary",
            "mime": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            "size": stat.st_size,
            "truncated": False,
            "content": "",
            "data_url": "",
        }

    def write(
        self,
        project: dict[str, Any],
        relative_path: str,
        content: str,
        *,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        if not isinstance(content, str):
            raise ValidationError("file content must be a string")
        if len(content.encode(encoding, errors="strict")) > MAX_WRITE_SIZE:
            raise ValidationError("file exceeds the 2 MB edit limit")
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValidationError("a file path is required")
        if encoding.lower().replace("-", "") != "utf8":
            raise ValidationError("only utf-8 encoding is supported")

        root = self._project_root(project)
        target = self._resolve_inside(root, relative_path)
        if target == root or not target.name:
            raise ValidationError("a file name is required")
        if target.exists() and target.is_dir():
            raise ValidationError("target path is a directory")
        if target.suffix.lower() not in TEXT_EXTENSIONS and target.suffix:
            raise ValidationError("only text files can be edited")
        if not target.parent.is_dir():
            raise ValidationError("edit directory does not exist")

        temporary: Path | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.remote-edit-",
                dir=str(target.parent),
            )
            temporary = Path(temp_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            os.replace(temporary, target)
            temporary = None
            stat = target.stat()
        except (OSError, UnicodeError) as exc:
            raise ValidationError("cannot save file") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

        return {
            "project_id": project["id"],
            "path": self._relative_text(root, target),
            "name": target.name,
            "size": stat.st_size,
            "kind": "text",
            "mime": mimetypes.guess_type(target.name)[0] or "text/plain",
        }

    def diff(self, project: dict[str, Any], relative_path: str) -> dict[str, Any]:
        root = self._project_root(project)
        target = self._resolve_inside(root, relative_path)
        if target == root or not target.name:
            raise ValidationError("a file path is required")
        target_exists = target.exists()
        if target_exists and target.is_dir():
            raise ValidationError("path is a directory")
        self._require_git(root)
        path_text = self._relative_text(root, target)
        if not target_exists:
            tracked = self._run_git(
                root,
                ["git", "ls-files", "--error-unmatch", "--", path_text],
                check=False,
            )
            if tracked.returncode != 0:
                raise ValidationError("file does not exist")
        result = self._run_git(
            root,
            ["git", "diff", "--no-ext-diff", "HEAD", "--", path_text],
            check=False,
        )
        diff = result.stdout if result.returncode == 0 else ""
        if not diff:
            result = self._run_git(
                root,
                ["git", "diff", "--no-ext-diff", "--", path_text],
                check=False,
            )
            diff = result.stdout
        if not diff:
            staged = self._run_git(
                root,
                ["git", "diff", "--no-ext-diff", "--cached", "--", path_text],
                check=False,
            )
            diff = staged.stdout
        if not diff and target.exists() and target.is_file():
            tracked = self._run_git(
                root,
                ["git", "ls-files", "--error-unmatch", "--", path_text],
                check=False,
            )
            if tracked.returncode != 0:
                untracked = self._run_git(
                    root,
                    ["git", "diff", "--no-ext-diff", "--no-index", "--", "/dev/null", path_text],
                    check=False,
                )
                diff = untracked.stdout
        additions, deletions = self._diff_counts(diff)
        return {
            "project_id": project["id"],
            "path": path_text,
            "name": target.name,
            "diff": diff,
            "additions": additions,
            "deletions": deletions,
        }

    def _diff_counts(self, diff: str) -> tuple[int, int]:
        additions = 0
        deletions = 0
        for line in diff.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1
        return additions, deletions

    def revert(self, project: dict[str, Any], relative_path: str) -> dict[str, Any]:
        root = self._project_root(project)
        target = self._resolve_inside(root, relative_path)
        if target == root or not target.name:
            raise ValidationError("a file path is required")
        if not target.exists():
            raise ValidationError("file does not exist")
        self._require_git(root)
        path_text = self._relative_text(root, target)
        status = self._run_git(root, ["git", "status", "--porcelain", "--", path_text]).stdout
        if not status.strip():
            raise ValidationError("file has no tracked changes")
        self._run_git(root, ["git", "restore", "--", path_text])
        return {
            "project_id": project["id"],
            "path": path_text,
            "name": target.name,
        }

    def _require_git(self, root: Path) -> None:
        result = self._run_git(
            root,
            ["git", "rev-parse", "--show-toplevel"],
            check=False,
        )
        if result.returncode != 0:
            raise ValidationError("revert requires a git project")
        try:
            Path(result.stdout.strip()).resolve().relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValidationError("project is not inside a git repository") from exc

    def _run_git(self, root: Path, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                cwd=str(root),
                check=check,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise ValidationError("git is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValidationError("git operation timed out") from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "git operation failed").strip()
            raise ValidationError(message or "git operation failed") from exc
        return result

    def _read_text(
        self,
        project: dict[str, Any],
        root: Path,
        target: Path,
        size: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        try:
            with target.open("rb") as handle:
                payload = handle.read(max_bytes + 1)
        except OSError as exc:
            raise ValidationError("cannot read file") from exc

        truncated = len(payload) > max_bytes or size > max_bytes
        payload = payload[:max_bytes]
        if b"\x00" in payload:
            return {
                "project_id": project["id"],
                "path": self._relative_text(root, target),
                "name": target.name,
                "kind": "binary",
                "mime": "application/octet-stream",
                "size": size,
                "truncated": truncated,
                "content": "",
                "data_url": "",
            }

        text = payload.decode("utf-8", errors="replace")
        return {
            "project_id": project["id"],
            "path": self._relative_text(root, target),
            "name": target.name,
            "kind": "text",
            "mime": mimetypes.guess_type(target.name)[0] or "text/plain",
            "size": size,
            "truncated": truncated,
            "content": text,
            "data_url": "",
        }

    def _read_image(
        self,
        project: dict[str, Any],
        root: Path,
        target: Path,
        size: int,
    ) -> dict[str, Any]:
        limit = 3 * 1024 * 1024
        if size > limit:
            return {
                "project_id": project["id"],
                "path": self._relative_text(root, target),
                "name": target.name,
                "kind": "binary",
                "mime": "image/*",
                "size": size,
                "truncated": True,
                "content": "",
                "data_url": "",
            }
        try:
            payload = target.read_bytes()
        except OSError as exc:
            raise ValidationError("cannot read image") from exc

        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(payload).decode("ascii")
        return {
            "project_id": project["id"],
            "path": self._relative_text(root, target),
            "name": target.name,
            "kind": "image",
            "mime": mime,
            "size": size,
            "truncated": False,
            "content": "",
            "data_url": f"data:{mime};base64,{encoded}",
        }

    def _project_root(self, project: dict[str, Any]) -> Path:
        raw_root = Path(str(project.get("normalized_path") or ""))
        if not raw_root.is_absolute():
            raise PathNotAllowedError("project path is invalid")
        try:
            return raw_root.resolve()
        except OSError as exc:
            raise PathNotAllowedError("project path is not accessible") from exc

    def _resolve_inside(self, root: Path, relative_path: Any) -> Path:
        raw = str(relative_path or "").strip().replace("\\", "/")
        if raw in {"", "."}:
            return root
        if (
            Path(raw).is_absolute()
            or PureWindowsPath(raw).is_absolute()
            or raw.startswith("/")
        ):
            try:
                absolute = Path(raw).resolve()
                absolute.relative_to(root)
                return absolute
            except (OSError, ValueError):
                raise PathNotAllowedError("path is outside the project") from None
        if ".." in Path(raw).parts:
            raise PathNotAllowedError("path is outside the project")
        candidate = root.joinpath(*Path(raw).parts)
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise PathNotAllowedError("path is outside the project") from exc
        return resolved

    def _relative_text(self, root: Path, path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return ""


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def is_probably_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS or path.suffix == ""


def safe_stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None
