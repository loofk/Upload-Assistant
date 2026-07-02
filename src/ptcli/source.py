"""Source tracker metadata and torrent download helpers for ptcli."""

from __future__ import annotations

import asyncio
import html as html_lib
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

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

MTEAM_DETAIL_HOSTS: frozenset[str] = frozenset({"m-team.cc", "kp.m-team.cc", "pt.m-team.cc", "api.m-team.cc"})


def _tracker_url_hosts() -> dict[str, str]:
    hosts: dict[str, str] = {}
    url_maps = (GENERIC_DETAILS_BASE_URLS, NEXUS_DOWNLOAD_BASE_URLS, TTG_DOWNLOAD_BASE_URLS, MTEAM_API_TRACKERS)
    for url_map in url_maps:
        for tracker, base_url in url_map.items():
            _register_tracker_host(hosts, tracker, urlparse(base_url).hostname)
    for template in COOKIE_DOWNLOAD_URLS.values():
        _register_tracker_host(hosts, "HDS", urlparse(template).hostname)
    for host in MTEAM_DETAIL_HOSTS:
        _register_tracker_host(hosts, "MTEAM", host)
    return hosts


def _register_tracker_host(hosts: dict[str, str], tracker: str, host: str | None) -> None:
    if not host:
        return
    normalized = host.lower().strip()
    if not normalized:
        return
    hosts[normalized] = tracker
    if normalized.startswith("www."):
        hosts[normalized.removeprefix("www.")] = tracker


TRACKER_URL_HOSTS: dict[str, str] = _tracker_url_hosts()


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
    tmdb_type: str | None
    name: str | None
    torrenthash: str | None
    description_length: int
    douban_id: str | None
    douban_url: str | None
    ptgen_description: str | None = None
    details_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_torrent_id(value: str) -> str:
    raw_value = value.strip()
    if not raw_value:
        raise ValueError("Torrent id is required.")
    query_match = re.search(r"[?&](?:id|torrentid|torrent_id|tid)=(\d+)", raw_value, flags=re.IGNORECASE)
    if query_match:
        return query_match.group(1)
    for pattern in (
        r"/details/(\d+)",
        r"/detail/(\d+)",
        r"/torrent/(\d+)",
        r"/torrents/(\d+)",
        r"/download/(\d+)",
        r"/dl/(\d+)",
    ):
        path_match = re.search(pattern, raw_value, flags=re.IGNORECASE)
        if path_match:
            return path_match.group(1)
    if raw_value.isdigit():
        return raw_value
    raise ValueError(f"Could not extract torrent id from: {value}")


def infer_tracker_from_url(value: str) -> str:
    """Infer a focused tracker code from a source details/download URL."""
    raw_value = value.strip()
    if not raw_value:
        raise ValueError("Source URL is required.")
    parsed = urlparse(raw_value if "://" in raw_value else f"https://{raw_value}")
    host = (parsed.hostname or "").lower().strip()
    if not host:
        raise ValueError(f"Could not infer tracker from source URL: {value}")
    candidates = [host]
    if host.startswith("www."):
        candidates.append(host.removeprefix("www."))
    for candidate in candidates:
        if candidate in TRACKER_URL_HOSTS:
            return TRACKER_URL_HOSTS[candidate]
    for known_host, tracker in TRACKER_URL_HOSTS.items():
        if host.endswith(f".{known_host}"):
            return tracker
    raise ValueError(f"Unsupported or unknown source tracker host: {host}")


def resolve_source_reference(value: str, tracker: str | None = None) -> dict[str, Any]:
    """Normalize a user/API source reference into tracker, torrent id, and details URL."""
    source_tracker = normalize_tracker(tracker) if tracker else infer_tracker_from_url(value)
    torrent_id = extract_torrent_id(value)
    raw_value = value.strip()
    parsed = urlparse(raw_value)
    details_url = raw_value if parsed.scheme and parsed.netloc else source_details_url(source_tracker, torrent_id)
    return {
        "tracker": source_tracker,
        "source_id": torrent_id,
        "torrent_id": torrent_id,
        "requested_source": value,
        "details_url": details_url,
        "inferred_tracker": tracker is None,
    }


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
    metadata_text = "\n".join(_optional_str(value) or "" for value in (description, meta.get("description"), meta.get("bdinfo"), meta.get("mediainfo")))
    douban_id, douban_url = _extract_douban(metadata_text, metadata_text)
    ptgen_description = _optional_str(meta.get("ptgen_description")) or _extract_ptgen_description(metadata_text)
    extracted_tmdb_id, extracted_tmdb_type = _extract_tmdb_ref(metadata_text, metadata_text)
    return SourceTorrentInfo(
        tracker=tracker,
        torrent_id=torrent_id,
        imdb_id=imdb_id or _optional_int(meta.get("imdb_id")) or _extract_imdb_id(metadata_text, metadata_text),
        tmdb_id=tmdb_id or _optional_int(meta.get("tmdb_id")) or extracted_tmdb_id,
        tmdb_type=_normalize_tmdb_type(meta.get("tmdb_type")) or extracted_tmdb_type,
        name=name,
        torrenthash=torrenthash,
        description_length=len(description or ""),
        douban_id=_optional_str(meta.get("douban_id")) or douban_id,
        douban_url=_optional_str(meta.get("douban_url")) or douban_url,
        ptgen_description=ptgen_description,
        details_url=source_details_url(tracker, torrent_id),
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
    description = _optional_str(detail.get("descr") or detail.get("description"))
    tmdb_id, tmdb_type = _extract_tmdb_ref(str(detail.get("tmdb") or ""), description or "")
    return SourceTorrentInfo(
        tracker=tracker,
        torrent_id=torrent_id,
        imdb_id=_extract_id_from_url(detail.get("imdb"), r"tt(\d+)"),
        tmdb_id=tmdb_id,
        tmdb_type=tmdb_type,
        name=_optional_str(detail.get("name") or detail.get("title")),
        torrenthash=_optional_str(detail.get("hash") or detail.get("infoHash")),
        description_length=len(description or ""),
        douban_id=douban_id,
        douban_url=douban_url,
        ptgen_description=_extract_ptgen_description(description or ""),
        details_url=source_details_url(tracker, torrent_id),
    )


async def _download_mteam_source_torrent(config: dict[str, Any], torrent_id: str, destination: Path) -> None:
    async with MTeamApiClient(config) as client:
        await client.download_torrent(torrent_id, destination)


async def _fetch_generic_source_info(config: dict[str, Any], tracker: str, torrent_id: str, meta: dict[str, Any]) -> SourceTorrentInfo:
    _ = config
    cookiefile = os.path.join(meta["base_dir"], "data", "cookies", f"{tracker}.txt")
    details_url = source_details_url(tracker, torrent_id)
    cookie_exists = await asyncio.to_thread(os.path.exists, cookiefile)
    if not cookie_exists:
        return SourceTorrentInfo(
            tracker=tracker,
            torrent_id=torrent_id,
            imdb_id=None,
            tmdb_id=None,
            tmdb_type=None,
            name=None,
            torrenthash=None,
            description_length=0,
            douban_id=None,
            douban_url=None,
            details_url=details_url,
        )

    cookies = await _load_cookie_file(cookiefile)
    async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
        response = await client.get(details_url or _generic_details_url(tracker, torrent_id))
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    page_text = soup.get_text("\n", strip=True)
    description = _extract_generic_description(soup, page_text)
    douban_id, douban_url = _extract_douban(page_text, response.text)
    tmdb_id, tmdb_type = _extract_tmdb_ref(response.text, page_text)
    return SourceTorrentInfo(
        tracker=tracker,
        torrent_id=torrent_id,
        imdb_id=_extract_imdb_id(response.text, page_text),
        tmdb_id=tmdb_id,
        tmdb_type=tmdb_type,
        name=_extract_generic_name(soup, page_text),
        torrenthash=_extract_torrent_hash(page_text, response.text),
        description_length=len(description),
        douban_id=douban_id,
        douban_url=douban_url,
        ptgen_description=_extract_ptgen_description(description),
        details_url=details_url,
    )


def source_details_url(tracker: str, torrent_id: str) -> str | None:
    source_tracker = normalize_tracker(tracker)
    if source_tracker in MTEAM_API_TRACKERS:
        return f"{MTEAM_API_TRACKERS[source_tracker]}/api/torrent/detail"
    if source_tracker in GENERIC_DETAILS_BASE_URLS:
        return _generic_details_url(source_tracker, torrent_id)
    return None


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


def _decoded_search_text(*parts: str | None) -> str:
    candidates: list[str] = []
    for part in parts:
        if not part:
            continue
        raw = str(part)
        candidates.append(raw)
        decoded = raw
        for _ in range(3):
            next_decoded = html_lib.unescape(unquote(decoded))
            if next_decoded == decoded:
                break
            decoded = next_decoded
            candidates.append(decoded)
    return "\n".join(candidates)


def _extract_imdb_id(html: str, page_text: str) -> int | None:
    search_text = _decoded_search_text(html, page_text)
    for pattern in (
        r"imdb\.com/title/tt(\d{5,10})",
        r"\btt(\d{5,10})\b",
        r"imdb[^0-9t]{0,80}(?:tt)?(\d{5,10})\b",
        r"\bimdb(?:[_\s-]*id)?\b[^0-9t]{0,40}(?:tt)?(\d{5,10})\b",
    ):
        value = _extract_first_int(pattern, search_text)
        if value:
            return value
    return None


def _extract_tmdb_id(html: str, page_text: str) -> int | None:
    tmdb_id, _tmdb_type = _extract_tmdb_ref(html, page_text)
    return tmdb_id


def _extract_tmdb_ref(html: str, page_text: str) -> tuple[int | None, str | None]:
    search_text = _decoded_search_text(html, page_text)
    url_match = re.search(r"themoviedb\.org/(movie|tv)/(\d{2,10})", search_text, flags=re.IGNORECASE)
    if url_match:
        return int(url_match.group(2)), url_match.group(1).lower()
    for pattern in (
        r"tmdb(?:[_\s-]*(?:id|编号|鏈接|链接))?[^0-9]{0,80}(\d{2,10})\b",
        r"\btmdb(?:[_\s-]*id)?\b[^0-9]{0,40}(\d{2,10})\b",
        r"themoviedb[^0-9]{0,80}(\d{2,10})\b",
        r"\bthe\s*movie\s*db\b[^0-9]{0,40}(\d{2,10})\b",
    ):
        value = _extract_first_int(pattern, search_text)
        if value:
            return value, None
    return None, None


def _normalize_tmdb_type(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"movie", "film"}:
        return "movie"
    if text in {"tv", "show", "series"}:
        return "tv"
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
    search_text = _decoded_search_text(html, page_text)
    match = re.search(r"douban\.com/subject/(\d+)", search_text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"(?:douban|豆瓣(?:编号|鏈接|链接)?)[^\d]{0,80}(\d{5,})", search_text, flags=re.IGNORECASE)
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


def _extract_torrent_hash(page_text: str, html: str | None = None) -> str | None:
    search_text = _decoded_search_text(page_text, html)
    for pattern in (
        r"(?:info\s*hash|infohash|torrent\s*hash|torrenthash|hash\s*(?:码|碼|值)?)[^A-Fa-f0-9]{0,48}([A-Fa-f0-9]{40})",
        r"(?:种子\s*(?:hash|哈希)|種子\s*(?:hash|哈希)|信息\s*(?:hash|哈希)|特征码|特徵碼|哈希(?:值|码|碼)?|散列值?)[^A-Fa-f0-9]{0,48}([A-Fa-f0-9]{40})",
    ):
        match = re.search(pattern, search_text, flags=re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return None


def _extract_generic_description(soup: BeautifulSoup, page_text: str) -> str:
    for selector in ("#kdescr", "div.nfo", "td.embedded", "div.torrent-description"):
        node = soup.select_one(selector)
        if node is not None:
            return node.get_text("\n", strip=True)
    return page_text


def _extract_ptgen_description(text: str) -> str | None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return None
    markers = (
        "◎译",
        "◎片",
        "◎年",
        "◎产",
        "◎类",
        "◎语",
        "◎上映",
        "◎IMDb",
        "◎豆瓣",
        "◎导",
        "◎主",
        "◎简",
    )
    marker_count = sum(1 for marker in markers if marker in normalized)
    if marker_count >= 2:
        return normalized
    if "◎" in normalized and re.search(r"(?:豆瓣|IMDb|简介|片\s*名|译\s*名)", normalized, flags=re.IGNORECASE):
        return normalized
    return None


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
