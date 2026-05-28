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
from bs4 import BeautifulSoup

from src.ptcli.mainland import normalize_tracker
from src.ptcli.mteam_api import MTeamApiClient


class SourceTrackerProtocol(Protocol):
    tracker: str

    async def get_info_from_torrent_id(self, torrent_id: str, meta: dict[str, Any] | None = None) -> tuple[Any, ...]:
        ...


SOURCE_TRACKER_CLASSES: dict[str, type[Any]] = {}

NEXUS_DOWNLOAD_BASE_URLS: dict[str, str] = {
    "AUDIENCES": "https://audiences.me",
    "CHD": "https://ptchdbits.co",
    "HDSKY": "https://hdsky.me",
    "HHAN": "https://hhanclub.net",
    "OB": "https://ourbits.club",
    "PTER": "https://pterclub.com",
    "TJUPT": "https://www.tjupt.org",
    "U2": "https://u2.dmhy.org",
}

DIRECT_DOWNLOAD_TRACKER_CLASSES: dict[str, type[Any]] = {}

MTEAM_API_TRACKERS: dict[str, str] = {
    "MTEAM": "https://api.m-team.cc",
}

TTG_DOWNLOAD_BASE_URLS: dict[str, str] = {
    "TTG": "https://totheglory.im",
}

COOKIE_DOWNLOAD_URLS: dict[str, str] = {
    "HDS": "https://hd-space.org/download.php?id={torrent_id}",
}

GENERIC_DETAILS_BASE_URLS: dict[str, str] = {
    "AUDIENCES": "https://audiences.me",
    "CHD": "https://ptchdbits.co",
    "HDS": "https://hd-space.org",
    "HDSKY": "https://hdsky.me",
    "HHAN": "https://hhanclub.net",
    "OB": "https://ourbits.club",
    "PTER": "https://pterclub.com",
    "TJUPT": "https://www.tjupt.org",
    "TTG": "https://totheglory.im",
    "U2": "https://u2.dmhy.org",
}


def source_info_adapter(tracker: str) -> str | None:
    source_tracker = normalize_tracker(tracker)
    if source_tracker in MTEAM_API_TRACKERS:
        return "mteam_api"
    if source_tracker in GENERIC_DETAILS_BASE_URLS:
        return "generic_details_cookie"
    if source_tracker in SOURCE_TRACKER_CLASSES:
        return "tracker_class"
    return None


def source_download_adapter(tracker: str) -> str | None:
    source_tracker = normalize_tracker(tracker)
    if source_tracker in MTEAM_API_TRACKERS:
        return "mteam_api"
    if source_tracker in DIRECT_DOWNLOAD_TRACKER_CLASSES:
        return "tracker_class"
    if source_tracker in TTG_DOWNLOAD_BASE_URLS:
        return "ttg_passkey"
    if source_tracker in COOKIE_DOWNLOAD_URLS:
        return "cookie_download"
    if source_tracker in NEXUS_DOWNLOAD_BASE_URLS:
        return "nexusphp_passkey"
    return None


def source_credential_requirements(tracker: str) -> list[str]:
    source_tracker = normalize_tracker(tracker)
    if source_tracker in MTEAM_API_TRACKERS:
        return [f"TRACKERS.{source_tracker}.api_key"]
    if source_tracker in COOKIE_DOWNLOAD_URLS:
        return [f"data/cookies/{source_tracker}.txt"]
    if source_tracker in TTG_DOWNLOAD_BASE_URLS:
        return [f"TRACKERS.{source_tracker}.announce_url or TRACKERS.{source_tracker}.passkey", f"data/cookies/{source_tracker}.txt"]
    if source_tracker in NEXUS_DOWNLOAD_BASE_URLS:
        return [f"TRACKERS.{source_tracker}.passkey", f"data/cookies/{source_tracker}.txt"]
    if source_tracker in GENERIC_DETAILS_BASE_URLS:
        return [f"data/cookies/{source_tracker}.txt"]
    return []


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
    meta = create_source_meta(base_dir)
    if source_tracker in MTEAM_API_TRACKERS:
        return await _fetch_mteam_source_info(config, source_tracker, torrent_id)

    if source_tracker in GENERIC_DETAILS_BASE_URLS:
        return await _fetch_generic_source_info(config, source_tracker, torrent_id, meta)

    tracker_class = SOURCE_TRACKER_CLASSES.get(source_tracker)
    if tracker_class is None:
        raise ValueError(f"Source metadata is not enabled for tracker: {source_tracker}")

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

    if source_tracker in MTEAM_API_TRACKERS:
        await _download_mteam_source_torrent(config, torrent_id, destination)
        return _validate_downloaded_torrent(destination)

    if source_tracker in DIRECT_DOWNLOAD_TRACKER_CLASSES:
        tracker_instance = DIRECT_DOWNLOAD_TRACKER_CLASSES[source_tracker](config=config)
        try:
            await tracker_instance.download_new_torrent(torrent_id, str(destination))
        finally:
            await _close_tracker_session(tracker_instance)
        return _validate_downloaded_torrent(destination)

    if source_tracker in TTG_DOWNLOAD_BASE_URLS:
        await _download_ttg_torrent(config, source_tracker, torrent_id, destination, base_dir)
        return _validate_downloaded_torrent(destination)

    if source_tracker in COOKIE_DOWNLOAD_URLS:
        await _download_cookie_torrent(source_tracker, torrent_id, destination, base_dir)
        return _validate_downloaded_torrent(destination)

    if source_tracker in NEXUS_DOWNLOAD_BASE_URLS:
        await _download_nexus_torrent(config, source_tracker, torrent_id, destination, base_dir)
        return _validate_downloaded_torrent(destination)

    raise ValueError(f"Source torrent download is not enabled for tracker: {source_tracker}")


async def _fetch_mteam_source_info(config: dict[str, Any], tracker: str, torrent_id: str) -> SourceTorrentInfo:
    async with MTeamApiClient(config) as client:
        data = await client.torrent_detail(torrent_id)
    return _mteam_source_info_from_detail(tracker, torrent_id, data)


def _mteam_source_info_from_detail(tracker: str, torrent_id: str, data: Any) -> SourceTorrentInfo:
    detail = data if isinstance(data, dict) else {}
    douban_id, douban_url = _extract_douban_from_value(detail.get("douban"))
    return SourceTorrentInfo(
        tracker=tracker,
        torrent_id=torrent_id,
        imdb_id=_extract_id_from_url(detail.get("imdb"), r"tt(\d+)"),
        tmdb_id=_extract_id_from_url(detail.get("tmdb"), r"/(?:movie|tv)/(\d+)"),
        name=_optional_str(detail.get("name") or detail.get("title")),
        torrenthash=_optional_str(detail.get("hash") or detail.get("infoHash")),
        description_length=len(_optional_str(detail.get("descr") or detail.get("description")) or ""),
        douban_id=douban_id,
        douban_url=douban_url,
    )


async def _download_mteam_source_torrent(config: dict[str, Any], torrent_id: str, destination: Path) -> None:
    async with MTeamApiClient(config) as client:
        await client.download_torrent(torrent_id, destination)


async def _fetch_generic_source_info(config: dict[str, Any], tracker: str, torrent_id: str, meta: dict[str, Any]) -> SourceTorrentInfo:
    _ = config
    cookiefile = os.path.join(meta["base_dir"], "data", "cookies", f"{tracker}.txt")
    cookie_exists = await asyncio.to_thread(os.path.exists, cookiefile)
    if not cookie_exists:
        return SourceTorrentInfo(
            tracker=tracker,
            torrent_id=torrent_id,
            imdb_id=None,
            tmdb_id=None,
            name=None,
            torrenthash=None,
            description_length=0,
            douban_id=None,
            douban_url=None,
        )

    cookies = await _load_cookie_file(cookiefile)
    async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
        response = await client.get(_generic_details_url(tracker, torrent_id))
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    page_text = soup.get_text("\n", strip=True)
    douban_id, douban_url = _extract_douban(page_text, response.text)
    return SourceTorrentInfo(
        tracker=tracker,
        torrent_id=torrent_id,
        imdb_id=_extract_imdb_id(response.text, page_text),
        tmdb_id=_extract_tmdb_id(response.text, page_text),
        name=_extract_generic_name(soup, page_text),
        torrenthash=_extract_torrent_hash(page_text),
        description_length=len(_extract_generic_description(soup, page_text)),
        douban_id=douban_id,
        douban_url=douban_url,
    )


def _generic_details_url(tracker: str, torrent_id: str) -> str:
    base_url = GENERIC_DETAILS_BASE_URLS[tracker]
    if tracker == "HDS":
        return f"{base_url}/index.php?page=torrent-details&id={torrent_id}"
    return f"{base_url}/details.php?id={torrent_id}"


def _extract_first_int(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _extract_id_from_url(value: Any, pattern: str) -> int | None:
    if not isinstance(value, str):
        return None
    return _extract_first_int(pattern, value)


def _extract_imdb_id(html: str, page_text: str) -> int | None:
    for pattern in (
        r"imdb\.com/title/tt(\d{5,10})",
        r"\btt(\d{5,10})\b",
        r"\bimdb(?:[_\s-]*id)?\b[^0-9t]{0,40}(?:tt)?(\d{5,10})\b",
    ):
        value = _extract_first_int(pattern, html)
        if value:
            return value
    return _extract_first_int(r"\bimdb(?:[_\s-]*id)?\b[^0-9t]{0,40}(?:tt)?(\d{5,10})\b", page_text)


def _extract_tmdb_id(html: str, page_text: str) -> int | None:
    for pattern in (
        r"themoviedb\.org/(?:movie|tv)/(\d{2,10})",
        r"\btmdb(?:[_\s-]*id)?\b[^0-9]{0,40}(\d{2,10})\b",
        r"\bthe\s*movie\s*db\b[^0-9]{0,40}(\d{2,10})\b",
    ):
        value = _extract_first_int(pattern, html)
        if value:
            return value
    for pattern in (
        r"\btmdb(?:[_\s-]*id)?\b[^0-9]{0,40}(\d{2,10})\b",
        r"\bthe\s*movie\s*db\b[^0-9]{0,40}(\d{2,10})\b",
    ):
        value = _extract_first_int(pattern, page_text)
        if value:
            return value
    return None


def _extract_douban_from_value(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    douban_value = str(value).strip()
    if not douban_value:
        return None, None
    match = re.search(r"(?:/subject/)?(\d+)", douban_value)
    if not match:
        return None, None
    douban_id = match.group(1)
    return douban_id, f"https://movie.douban.com/subject/{douban_id}/"


def _extract_douban(page_text: str, html: str) -> tuple[str | None, str | None]:
    match = re.search(r"douban\.com/subject/(\d+)", html, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"(?:douban|豆瓣)[^\d]{0,32}(\d{5,})", page_text, flags=re.IGNORECASE)
    if not match:
        return None, None
    douban_id = match.group(1)
    return douban_id, f"https://movie.douban.com/subject/{douban_id}/"


def _extract_generic_name(soup: BeautifulSoup, page_text: str) -> str | None:
    candidates = [
        soup.select_one("h1"),
        soup.select_one("td.rowhead + td"),
        soup.select_one("title"),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        name = " ".join(candidate.get_text(" ", strip=True).split())
        if name and len(name) <= 240:
            return name
    first_line = next((line.strip() for line in page_text.splitlines() if line.strip()), "")
    return first_line[:240] or None


def _extract_torrent_hash(page_text: str) -> str | None:
    match = re.search(r"(?:info\s*hash|torrent\s*hash|hash)[^A-Fa-f0-9]{0,48}([A-Fa-f0-9]{40})", page_text, flags=re.IGNORECASE)
    return match.group(1).lower() if match else None


def _extract_generic_description(soup: BeautifulSoup, page_text: str) -> str:
    for selector in ("#kdescr", "div.nfo", "td.embedded", "div.torrent-description"):
        node = soup.select_one(selector)
        if node is not None:
            return node.get_text("\n", strip=True)
    return page_text


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
    cookiefile = os.path.join(meta["base_dir"], "data", "cookies", f"{tracker}.txt")
    cookie_exists = await asyncio.to_thread(os.path.exists, cookiefile)
    cookies = await _load_cookie_file(cookiefile) if cookie_exists else {}
    download_url = f"{NEXUS_DOWNLOAD_BASE_URLS[tracker]}/download.php?id={torrent_id}&passkey={passkey}"

    async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
        response = await client.get(download_url)
    response.raise_for_status()
    await asyncio.to_thread(_assert_torrent_bytes, response.content)
    async with aiofiles.open(destination, "wb") as torrent_file:
        await torrent_file.write(response.content)


async def _download_ttg_torrent(config: dict[str, Any], tracker: str, torrent_id: str, destination: Path, base_dir: str | None) -> None:
    passkey = _ttg_passkey(config.get("TRACKERS", {}).get(tracker, {}))
    if not passkey:
        raise ValueError("TTG passkey or announce_url is not configured.")

    meta = create_source_meta(base_dir)
    cookiefile = os.path.join(meta["base_dir"], "data", "cookies", f"{tracker}.txt")
    cookie_exists = await asyncio.to_thread(os.path.exists, cookiefile)
    cookies = await _load_cookie_file(cookiefile) if cookie_exists else {}
    download_url = f"{TTG_DOWNLOAD_BASE_URLS[tracker]}/dl/{torrent_id}/{passkey}"

    async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
        response = await client.get(download_url)
    response.raise_for_status()
    await asyncio.to_thread(_assert_torrent_bytes, response.content)
    async with aiofiles.open(destination, "wb") as torrent_file:
        await torrent_file.write(response.content)


async def _download_cookie_torrent(tracker: str, torrent_id: str, destination: Path, base_dir: str | None) -> None:
    meta = create_source_meta(base_dir)
    cookiefile = os.path.join(meta["base_dir"], "data", "cookies", f"{tracker}.txt")
    cookie_exists = await asyncio.to_thread(os.path.exists, cookiefile)
    if not cookie_exists:
        raise ValueError(f"{tracker} cookie file is required for source torrent download.")
    cookies = await _load_cookie_file(cookiefile)
    download_url = COOKIE_DOWNLOAD_URLS[tracker].format(torrent_id=torrent_id)

    async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
        response = await client.get(download_url)
    response.raise_for_status()
    await asyncio.to_thread(_assert_torrent_bytes, response.content)
    async with aiofiles.open(destination, "wb") as torrent_file:
        await torrent_file.write(response.content)


def _ttg_passkey(tracker_config: Any) -> str:
    if not isinstance(tracker_config, dict):
        return ""
    announce_url = str(tracker_config.get("announce_url", "")).strip()
    if announce_url:
        return announce_url.rstrip("/").split("/")[-1]
    return str(tracker_config.get("passkey", "")).strip()


async def _load_cookie_file(cookiefile: str | Path) -> dict[str, str]:
    cookies: dict[str, str] = {}
    async with aiofiles.open(cookiefile, encoding="utf-8") as cookie_file:
        content = await cookie_file.read()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif line.startswith("#"):
            continue
        if _parse_netscape_cookie_line(line, cookies):
            continue
        _parse_cookie_header_line(line, cookies)
    return cookies


def _parse_netscape_cookie_line(line: str, cookies: dict[str, str]) -> bool:
    fields = [field for field in re.split(r"\s+", line) if field]
    if len(fields) < 7:
        return False
    name = fields[5].strip()
    value = fields[6].strip()
    if not name:
        return False
    cookies[name] = value
    return True


def _parse_cookie_header_line(line: str, cookies: dict[str, str]) -> None:
    for pair in line.split(";"):
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()


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
