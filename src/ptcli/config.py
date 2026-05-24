"""Configuration loading for the focused PT CLI."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def load_config(config_path: str | None = None) -> dict[str, Any]:
    path = Path(config_path or "data/config.py").expanduser()
    if not path.exists():
        raise ValueError(f"Config file not found: {path}")

    spec = importlib.util.spec_from_file_location("ptcli_config", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load config file: {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = getattr(module, "config", None)
    if not isinstance(config, dict):
        raise ValueError(f"Config file does not define a config dict: {path}")
    return config


def resolve_client_config(config: dict[str, Any], client_name: str) -> tuple[str, dict[str, Any]]:
    default_config = config.get("DEFAULT", {})
    torrent_clients = config.get("TORRENT_CLIENTS", {})
    if not isinstance(default_config, dict) or not isinstance(torrent_clients, dict):
        raise ValueError("Config must contain DEFAULT and TORRENT_CLIENTS dictionaries.")

    resolved_name = client_name
    if client_name == "default":
        default_client = default_config.get("default_torrent_client")
        if not isinstance(default_client, str) or not default_client:
            raise ValueError("DEFAULT.default_torrent_client is not configured.")
        resolved_name = default_client

    client_config = torrent_clients.get(resolved_name)
    if not isinstance(client_config, dict):
        raise ValueError(f"Torrent client not found in config: {resolved_name}")

    torrent_client = str(client_config.get("torrent_client", "")).lower()
    if torrent_client != "qbit":
        raise ValueError(f"ptcli read-only inspection currently supports qBittorrent only, got: {torrent_client or 'unknown'}")

    return resolved_name, client_config

