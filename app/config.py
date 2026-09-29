from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 7800
    admin_enabled: bool = True
    admin_host: str = "127.0.0.1"
    admin_port: int = 7801
    heartbeat_interval_sec: float = 15.0
    handshake_timeout_sec: float = 5.0
    max_message_bytes: int = 1_048_576


@dataclass(slots=True)
class SecurityConfig:
    pairing_code_ttl_sec: float = 300.0
    pairing_confirm_ttl_sec: float = 60.0
    pairing_max_attempts: int = 5
    auth_failure_lock_sec: float = 300.0


@dataclass(slots=True)
class ProjectsConfig:
    allowed_roots: list[str] = field(default_factory=list)
    default_sandbox: str = "workspace_write"
    fallback_to_read_only: bool = True


@dataclass(slots=True)
class CodexConfig:
    mode: str = "sdk"
    fallback_to_fake: bool = True
    max_running_turns: int = 2
    turn_timeout_sec: float = 1800.0


@dataclass(slots=True)
class ModelsConfig:
    default_model: str = "default"
    choices: list[str] = field(default_factory=lambda: [
        "default", "gpt-5-codex", "gpt-5", "gpt-5-mini",
    ])


@dataclass(slots=True)
class MonitorConfig:
    metrics_interval_sec: float = 1.0
    screen_default_fps: float = 30.0
    screen_max_fps: float = 60.0
    screen_max_width: int = 1920
    screen_jpeg_quality: int = 60


@dataclass(slots=True)
class StorageConfig:
    database_path: str | None = None
    event_retention_days: int = 30


@dataclass(slots=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    projects: ProjectsConfig = field(default_factory=ProjectsConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    config_path: str | None = None


def _merge_section(instance: Any, values: dict[str, Any]) -> None:
    for key, value in values.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if isinstance(current, list) and isinstance(value, list):
            setattr(instance, key, value)
        elif not isinstance(current, (dict, list)) and not isinstance(value, (dict, list)):
            setattr(instance, key, value)


def load_config(path: str | Path | None = None) -> AppConfig:
    config = AppConfig()
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        for section in ("server", "security", "projects", "codex", "models", "monitor", "storage"):
            values = raw.get(section)
            if isinstance(values, dict):
                _merge_section(getattr(config, section), values)

    if not config.projects.allowed_roots:
        config.projects.allowed_roots.append(os.getcwd())
    return config


def save_config(config: AppConfig, path: str | Path) -> None:
    """Persist enough state to restore the current mobile setup after restart."""

    target = Path(path)
    if target.exists():
        raw = json.loads(target.read_text(encoding="utf-8"))
    else:
        raw = {}
    raw["server"] = {
        **raw.get("server", {}),
        "host": config.server.host,
        "port": config.server.port,
        "admin_enabled": config.server.admin_enabled,
        "admin_host": config.server.admin_host,
        "admin_port": config.server.admin_port,
    }
    raw["projects"] = {
        **raw.get("projects", {}),
        "allowed_roots": list(config.projects.allowed_roots),
        "default_sandbox": config.projects.default_sandbox,
        "fallback_to_read_only": config.projects.fallback_to_read_only,
    }
    raw["codex"] = {
        **raw.get("codex", {}),
        "mode": config.codex.mode,
        "fallback_to_fake": config.codex.fallback_to_fake,
        "max_running_turns": config.codex.max_running_turns,
        "turn_timeout_sec": config.codex.turn_timeout_sec,
    }
    raw["models"] = {
        **raw.get("models", {}),
        "default_model": config.models.default_model,
        "choices": list(config.models.choices),
    }
    raw["monitor"] = {
        **raw.get("monitor", {}),
        "metrics_interval_sec": config.monitor.metrics_interval_sec,
        "screen_default_fps": config.monitor.screen_default_fps,
        "screen_max_fps": config.monitor.screen_max_fps,
        "screen_max_width": config.monitor.screen_max_width,
        "screen_jpeg_quality": config.monitor.screen_jpeg_quality,
    }
    raw["storage"] = {
        **raw.get("storage", {}),
        "database_path": config.storage.database_path,
        "event_retention_days": config.storage.event_retention_days,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_database_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".remote-codex"
    return root / "RemoteCodex" / "agent.db3"
