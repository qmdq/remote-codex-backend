from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def discover_codex_models() -> dict[str, Any]:
    """Read model names and the configured default from Codex's local config."""

    home = codex_home()
    config_path = home / "config.toml"
    configured = _top_level_toml_string(config_path, "model")
    catalog_name = _top_level_toml_string(config_path, "model_catalog_json")
    catalog_path = Path(catalog_name) if Path(catalog_name or "").is_absolute() else home / (catalog_name or "")

    choices: list[str] = []
    if catalog_path.is_file():
        choices = _read_catalog(catalog_path)

    if configured and configured not in choices:
        choices.insert(0, configured)
    return {
        "default_model": configured or "default",
        "choices": choices,
        "source": str(catalog_path) if choices and catalog_path.is_file() else str(config_path),
    }


def _read_catalog(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []

    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []

    choices: list[str] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        if item.get("visibility") not in (None, "list"):
            continue
        if item.get("supported_in_api") is False:
            continue
        slug = str(item.get("slug") or item.get("display_name") or "").strip()
        if slug and slug not in choices:
            choices.append(slug)
    return choices


def _top_level_toml_string(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return None

    pattern = re.compile(rf'^\s*{re.escape(key)}\s*=\s*(.+?)\s*(?:#.*)?$')
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            break
        match = pattern.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
            if value[0] == '"':
                value = value.replace('\\"', '"').replace("\\\\", "\\")
        return value or None
    return None
