"""Read-only qBittorrent helpers for ptcli."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import qbittorrentapi
from torf import Torrent


class QbitClientProtocol(Protocol):
    def auth_log_in(self) -> Any:
        ...

    def torrents_info(self, **kwargs: Any) -> list[Any]:
        ...

    def torrents_export(self, torrent_hash: str) -> bytes:
        ...

    def torrents_add(self, **kwargs: Any) -> Any:
        ...


@dataclass(frozen=True)
class QbitTorrentSummary:
    name: str
    hash: str
    save_path: str | None
    content_path: str | None
    size: int | None
    progress: float | None
    state: str | None
    category: str | None
    tags: str | None
    tracker: str | None


def _get_field(torrent: Any, field: str) -> Any:
    if isinstance(torrent, dict):
        return torrent.get(field)
    return getattr(torrent, field, None)


def summarize_torrent(torrent: Any) -> QbitTorrentSummary:
    torrent_hash = _get_field(torrent, "hash") or _get_field(torrent, "infohash_v1") or ""
    return QbitTorrentSummary(
        name=str(_get_field(torrent, "name") or ""),
        hash=str(torrent_hash),
        save_path=_optional_str(_get_field(torrent, "save_path")),
        content_path=_optional_str(_get_field(torrent, "content_path")),
        size=_optional_int(_get_field(torrent, "size")),
        progress=_optional_float(_get_field(torrent, "progress")),
        state=_optional_str(_get_field(torrent, "state")),
        category=_optional_str(_get_field(torrent, "category")),
        tags=_optional_str(_get_field(torrent, "tags")),
        tracker=_optional_str(_get_field(torrent, "tracker")),
    )


def match_torrents(torrents: list[QbitTorrentSummary], content_path: str) -> list[QbitTorrentSummary]:
    normalized_target = os.path.normpath(content_path)
    target_name = os.path.basename(normalized_target).lower()
    matches: list[QbitTorrentSummary] = []

    for torrent in torrents:
        candidates = [
            torrent.content_path or "",
            torrent.save_path or "",
            torrent.name,
        ]
        normalized_candidates = [os.path.normpath(candidate) for candidate in candidates if candidate]
        if any(candidate == normalized_target or candidate.startswith(f"{normalized_target}{os.sep}") for candidate in normalized_candidates):
            matches.append(torrent)
            continue
        if target_name and any(_basename_matches_target(os.path.basename(candidate).lower(), target_name) for candidate in normalized_candidates):
            matches.append(torrent)

    return matches


def summaries_to_dicts(torrents: list[QbitTorrentSummary]) -> list[dict[str, Any]]:
    return [asdict(torrent) for torrent in torrents]


def _basename_matches_target(candidate_name: str, target_name: str) -> bool:
    if candidate_name == target_name:
        return True
    if not candidate_name.startswith(target_name):
        return False
    suffix = candidate_name[len(target_name) :]
    return bool(suffix) and bool(re.match(r"^[\s._\-\[\]()]+", suffix))


class QbitReadOnlyService:
    def __init__(self, client_config: dict[str, Any], qbit_client: QbitClientProtocol | None = None) -> None:
        self.client_config = client_config
        self.qbit_client = qbit_client

    async def connect(self) -> QbitClientProtocol:
        if self.qbit_client is not None:
            return self.qbit_client

        client = qbittorrentapi.Client(
            host=self.client_config["qbit_url"],
            port=self.client_config["qbit_port"],
            username=self.client_config["qbit_user"],
            password=self.client_config["qbit_pass"],
            VERIFY_WEBUI_CERTIFICATE=self.client_config.get("VERIFY_WEBUI_CERTIFICATE", True),
        )
        await asyncio.to_thread(client.auth_log_in)
        self.qbit_client = client
        return client

    async def list_torrents(self, torrent_hash: str | None = None, limit: int | None = None) -> list[QbitTorrentSummary]:
        client = await self.connect()
        kwargs: dict[str, Any] = {}
        if torrent_hash:
            kwargs["torrent_hashes"] = torrent_hash

        raw_torrents = await asyncio.to_thread(client.torrents_info, **kwargs)
        summaries = [summarize_torrent(torrent) for torrent in raw_torrents]
        if limit is not None and limit >= 0:
            return summaries[:limit]
        return summaries

    async def export_torrent(self, torrent_hash: str, output_dir: str) -> Path:
        safe_hash = _safe_hash(torrent_hash)
        client = await self.connect()
        torrent_bytes = await asyncio.to_thread(client.torrents_export, torrent_hash=safe_hash)
        if not isinstance(torrent_bytes, bytes):
            raise ValueError("qBittorrent did not return torrent bytes.")

        return await asyncio.to_thread(_write_torrent_bytes, output_dir, safe_hash, torrent_bytes)

    async def add_torrent_file(
        self,
        torrent_path: str,
        save_path: str,
        category: str | None = None,
        tags: str | None = None,
        paused: bool = False,
        skip_checking: bool = False,
    ) -> dict[str, Any]:
        client = await self.connect()
        resolved_torrent_path, torrent_bytes, torrent_hash = await asyncio.to_thread(_read_torrent_payload, torrent_path)
        if not torrent_bytes.startswith(b"d"):
            raise ValueError("Torrent file does not look like a .torrent file.")

        add_kwargs: dict[str, Any] = {
            "torrent_files": torrent_bytes,
            "save_path": save_path,
            "is_skip_checking": skip_checking,
            "paused": paused,
        }
        if category:
            add_kwargs["category"] = category
        if tags:
            add_kwargs["tags"] = tags

        await asyncio.to_thread(client.torrents_add, **add_kwargs)
        client_matches = await self.list_torrents(torrent_hash=torrent_hash)
        return {
            "torrent_path": str(resolved_torrent_path),
            "hash": torrent_hash,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
            "skip_checking": skip_checking,
            "verified_in_client": bool(client_matches),
            "client_matches": summaries_to_dicts(client_matches),
        }

    async def wait_for_completion(
        self,
        torrent_hash: str | None = None,
        content_path: str | None = None,
        timeout: float = 3600.0,
        interval: float = 30.0,
    ) -> dict[str, Any]:
        if not torrent_hash and not content_path:
            raise ValueError("Either torrent_hash or content_path is required.")
        if timeout < 0:
            raise ValueError("timeout must be >= 0.")
        if interval <= 0:
            raise ValueError("interval must be > 0.")

        deadline = asyncio.get_running_loop().time() + timeout
        last_matches: list[QbitTorrentSummary] = []
        query = {
            "mode": "hash" if torrent_hash else "content_path",
            "torrent_hash": torrent_hash,
            "content_path": content_path,
            "timeout": timeout,
            "interval": interval,
        }

        while True:
            if torrent_hash:
                matches = await self.list_torrents(torrent_hash=torrent_hash)
            else:
                matches = match_torrents(await self.list_torrents(), str(content_path))
            last_matches = matches
            completed = [torrent for torrent in matches if _is_complete(torrent)]
            if completed:
                return {
                    "complete": True,
                    "query": query,
                    "matched_count": len(matches),
                    "matches": summaries_to_dicts(completed),
                }

            if asyncio.get_running_loop().time() >= deadline:
                return {
                    "complete": False,
                    "query": query,
                    "matched_count": len(last_matches),
                    "matches": summaries_to_dicts(last_matches),
                    "blockers": _wait_blockers(last_matches, torrent_hash=torrent_hash, content_path=content_path),
                }

            await asyncio.sleep(interval)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    string_value = str(value)
    return string_value if string_value else None


def _write_torrent_bytes(output_dir: str, torrent_hash: str, torrent_bytes: bytes) -> Path:
    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{torrent_hash}.torrent"
    destination.write_bytes(torrent_bytes)
    return destination


def _read_torrent_payload(torrent_path: str) -> tuple[Path, bytes, str]:
    resolved_torrent_path = Path(torrent_path).expanduser()
    torrent_bytes = resolved_torrent_path.read_bytes()
    torrent = Torrent.read(str(resolved_torrent_path), validate=False)
    return resolved_torrent_path, torrent_bytes, str(torrent.infohash)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_hash(value: str) -> str:
    torrent_hash = value.strip().lower()
    if len(torrent_hash) not in {32, 40}:
        raise ValueError("Torrent hash must be 32 or 40 hex characters.")
    if any(char not in "0123456789abcdef" for char in torrent_hash):
        raise ValueError("Torrent hash must contain only hex characters.")
    return torrent_hash


def _is_complete(torrent: QbitTorrentSummary) -> bool:
    if torrent.progress is not None and torrent.progress >= 1.0:
        return True
    return (torrent.state or "").lower() in {"uploading", "stalled_up", "forcedup", "queuedup", "checkingup"}


def _wait_blockers(matches: list[QbitTorrentSummary], *, torrent_hash: str | None, content_path: str | None) -> list[str]:
    if not matches:
        if torrent_hash:
            return [f"No qBittorrent torrent matched hash {torrent_hash}."]
        return [f"No qBittorrent torrent matched path {content_path}."]
    return ["qBittorrent matched the torrent but did not report it as complete before timeout."]
