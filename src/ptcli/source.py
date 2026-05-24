"""Source tracker metadata and torrent download helpers for ptcli."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import aiofiles
import httpx

from src.ptcli.mainland import normalize_tracker
from src.trackers.AUDIENCES import AUDIENCES
from src.trackers.CHD import CHD
from src.trackers.COMMON import COMMON
from src.trackers.HDSKY import HDSKY
from src.trackers.HHAN import HHAN
from src.trackers.MTEAM import MTEAM
from src.trackers.PTER import PTER
from src.trackers.TJUPT import TJUPT
from src.trackers.U2 import U2


class SourceTrackerProtocol(Protocol):
    tracker: str

    async def get_info_from_torrent_id(self, torrent_id: str, meta: dict[str, Any] | None = None) -> tuple[Any, ...]:
        ...


SOURCE_TRACKER_CLASSES: dict[str, type[Any]] = {
    "AUDIENCES": AUDIENCES,
    "CHD": CHD,
    "HDSKY": HDSKY,
    "HHAN": HHAN,
    "MTEAM": MTEAM,
    "PTER": PTER,
    "TJUPT": TJUPT,
    "U2": U2,
}

NEXUS_DOWNLOAD_BASE_URLS: dict[str, str] = {
    "AUDIENCES": "https://audiences.me",
    "CHD": "https://ptchdbits.co",
    "HDSKY": "https://hdsky.me",
    "HHAN": "https://hhanclub.net",
    "PTER": "https://pterclub.com",
    "TJUPT": "https://www.tjupt.org",
    "U2": "https://u2.dmhy.org",
}


@dataclass(frozen=True)
class SourceTorrentInfo:
    tracker: str
    torrent_id: str
    imdb_id: int | None
    tmdb_id: int | None
    name: str | None
    torrenthash: str | None
    description_length: int
    douban_id: str | None
    douban_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_torrent_id(value: str) -> str:
    raw_value = value.strip()
    if not raw_value:
        raise ValueError("Torrent id is required.")
    query_match = re.search(r"[?&]id=(\d+)", raw_value)
    if query_match:
        return query_match.group(1)
    path_match = re.search(r"/details/(\d+)", raw_value)
    if path_match:
        return path_match.group(1)
    if raw_value.isdigit():
        return raw_value
    raise ValueError(f"Could not extract torrent id from: {value}")


def create_source_meta(base_dir: str | None = None) -> dict[str, Any]:
    resolved_base_dir = os.path.abspath(base_dir or os.getcwd())
    return {
        "base_dir": resolved_base_dir,
        "uuid": "ptcli-source",
        "debug": False,
        "douban_id": None,
        "douban_url": None,
    }


def source_info_from_tuple(tracker: str, torrent_id: str, result: tuple[Any, ...], meta: dict[str, Any]) -> SourceTorrentInfo:
    imdb_id = _optional_int(result[0] if len(result) > 0 else None)
    tmdb_id = _optional_int(result[1] if len(result) > 1 else None)
    name = _optional_str(result[2] if len(result) > 2 else None)
    torrenthash = _optional_str(result[3] if len(result) > 3 else None)
    description = _optional_str(result[4] if len(result) > 4 else None)
    return SourceTorrentInfo(
        tracker=tracker,
        torrent_id=torrent_id,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        name=name,
        torrenthash=torrenthash,
        description_length=len(description or ""),
        douban_id=_optional_str(meta.get("douban_id")),
        douban_url=_optional_str(meta.get("douban_url")),
    )


def source_info_has_signal(info: SourceTorrentInfo) -> bool:
    return any(
        [
            info.imdb_id,
            info.tmdb_id,
            info.name,
            info.torrenthash,
            info.description_length,
            info.douban_id,
            info.douban_url,
        ]
    )


async def fetch_source_info(config: dict[str, Any], tracker: str, source_id: str, base_dir: str | None = None) -> SourceTorrentInfo:
    source_tracker = normalize_tracker(tracker)
    torrent_id = extract_torrent_id(source_id)
    tracker_class = SOURCE_TRACKER_CLASSES.get(source_tracker)
    if tracker_class is None:
        raise ValueError(f"Source metadata is not enabled for tracker: {source_tracker}")

    meta = create_source_meta(base_dir)
    tracker_instance = tracker_class(config=config)
    try:
        result = await tracker_instance.get_info_from_torrent_id(torrent_id, meta=meta)
        return source_info_from_tuple(source_tracker, torrent_id, result, meta)
    finally:
        await _close_tracker_session(tracker_instance)


async def download_source_torrent(config: dict[str, Any], tracker: str, source_id: str, output_dir: str, base_dir: str | None = None) -> Path:
    source_tracker = normalize_tracker(tracker)
    torrent_id = extract_torrent_id(source_id)
    destination = await asyncio.to_thread(_prepare_destination, output_dir, source_tracker, torrent_id)

    if source_tracker == "MTEAM":
        tracker_instance = MTEAM(config=config)
        try:
            await tracker_instance.download_new_torrent(torrent_id, str(destination))
        finally:
            await _close_tracker_session(tracker_instance)
        return _validate_downloaded_torrent(destination)

    if source_tracker in NEXUS_DOWNLOAD_BASE_URLS:
        await _download_nexus_torrent(config, source_tracker, torrent_id, destination, base_dir)
        return _validate_downloaded_torrent(destination)

    raise ValueError(f"Source torrent download is not enabled for tracker: {source_tracker}")


async def _close_tracker_session(tracker_instance: Any) -> None:
    session = getattr(tracker_instance, "session", None)
    close = getattr(session, "aclose", None)
    if not callable(close):
        return
    result = close()
    if hasattr(result, "__await__"):
        await result


async def _download_nexus_torrent(config: dict[str, Any], tracker: str, torrent_id: str, destination: Path, base_dir: str | None) -> None:
    passkey = str(config.get("TRACKERS", {}).get(tracker, {}).get("passkey", "")).strip()
    if not passkey:
        raise ValueError(f"{tracker} passkey is not configured.")

    meta = create_source_meta(base_dir)
    common = COMMON(config=config)
    cookiefile = os.path.join(meta["base_dir"], "data", "cookies", f"{tracker}.txt")
    cookie_exists = await asyncio.to_thread(os.path.exists, cookiefile)
    cookies = await common.parseCookieFile(cookiefile) if cookie_exists else {}
    download_url = f"{NEXUS_DOWNLOAD_BASE_URLS[tracker]}/download.php?id={torrent_id}&passkey={passkey}"

    async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
        response = await client.get(download_url)
    response.raise_for_status()
    await asyncio.to_thread(_assert_torrent_bytes, response.content)
    async with aiofiles.open(destination, "wb") as torrent_file:
        await torrent_file.write(response.content)


def _validate_downloaded_torrent(path: Path) -> Path:
    if not path.exists():
        raise ValueError(f"Torrent download did not create file: {path}")
    data = path.read_bytes()
    _assert_torrent_bytes(data)
    return path


def _prepare_destination(output_dir: str, tracker: str, torrent_id: str) -> Path:
    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    return destination_dir / f"{tracker}-{torrent_id}.torrent"


def _assert_torrent_bytes(data: bytes) -> None:
    if not data.startswith(b"d"):
        raise ValueError("Downloaded data does not look like a .torrent file.")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    string_value = str(value)
    return string_value if string_value else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
