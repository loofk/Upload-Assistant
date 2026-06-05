"""Material generation helpers for focused ptcli retorrent packages."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiofiles
import httpx
from pymediainfo import MediaInfo

VIDEO_EXTENSIONS = {
    ".avi",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}


def find_primary_media_file(content_path: str) -> Path | None:
    root = Path(content_path).expanduser()
    if root.is_file():
        return root if root.suffix.lower() in VIDEO_EXTENSIONS else None
    if not root.is_dir():
        return None
    candidates = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and not _hidden_path(path, root)]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_size, str(path)))


async def generate_mediainfo_material(
    content_path: str,
    output_dir: str,
    *,
    parser: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(_generate_mediainfo_material_sync, content_path, output_dir, parser or MediaInfo.parse)


async def generate_screenshot_materials(
    content_path: str,
    output_dir: str,
    *,
    count: int = 3,
    parser: Callable[..., Any] | None = None,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    ffmpeg_binary: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(_generate_screenshot_materials_sync, content_path, output_dir, count, parser or MediaInfo.parse, runner, ffmpeg_binary)


async def generate_bdinfo_material(
    content_path: str,
    output_dir: str,
    *,
    base_dir: str | None = None,
    playlist: str | None = None,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    bdinfo_binary: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(_generate_bdinfo_material_sync, content_path, output_dir, base_dir, playlist, runner, bdinfo_binary)


async def upload_screenshot_image_hosts(
    config: dict[str, Any],
    screenshot_files: list[str],
    output_dir: str,
    *,
    image_host: str | None = None,
    uploader: Callable[[list[Any]], Any] | None = None,
) -> dict[str, Any]:
    if not screenshot_files:
        return {"status": "blocked", "host": image_host, "count": 0, "items": [], "blockers": ["No screenshot files were supplied for image-host upload."]}
    host = image_host or _default_image_host(config)
    if not host:
        return {"status": "blocked", "host": None, "count": 0, "items": [], "blockers": ["No image host was provided and DEFAULT.img_host_1 is empty."]}
    upload = uploader or _focused_upload_image_task
    items = []
    blockers = []
    for index, screenshot in enumerate(screenshot_files, start=1):
        result = await upload([screenshot, host, config, {"debug": False}])
        if not isinstance(result, dict) or result.get("status") != "success":
            reason = result.get("reason") if isinstance(result, dict) else "upload task returned a non-dict result"
            blockers.append(f"Screenshot {index} upload failed: {reason}")
            continue
        items.append(
            {
                "index": index,
                "host": host,
                "local_file": screenshot,
                "img_url": result.get("img_url"),
                "raw_url": result.get("raw_url"),
                "web_url": result.get("web_url"),
            }
        )
    payload = {
        "status": "uploaded" if len(items) == len(screenshot_files) and not blockers else "blocked",
        "host": host,
        "count": len(items),
        "requested_count": len(screenshot_files),
        "items": items,
        "blockers": blockers,
    }
    output_path = await asyncio.to_thread(_write_image_host_payload, output_dir, payload)
    return {**payload, "image_host_file": str(output_path)}


def _generate_mediainfo_material_sync(content_path: str, output_dir: str, parser: Callable[..., Any]) -> dict[str, Any]:
    media_file = find_primary_media_file(content_path)
    if media_file is None:
        return {
            "status": "blocked",
            "content_path": str(Path(content_path).expanduser()),
            "blockers": ["No supported video file was found for MediaInfo generation."],
        }

    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    try:
        full_text = parser(str(media_file), output="STRING", full=True)
        regular_text = parser(str(media_file), output="STRING", full=False)
        json_text = parser(str(media_file), output="JSON")
    except Exception as exc:
        return {
            "status": "blocked",
            "content_path": str(Path(content_path).expanduser()),
            "media_file": str(media_file),
            "blockers": [f"MediaInfo generation failed: {exc}"],
        }

    full_path = destination_dir / "MI_FULL_00.txt"
    mediainfo_path = destination_dir / "MEDIAINFO.txt"
    json_path = destination_dir / "MediaInfo.json"
    full_path.write_text(_clean_mediainfo_text(str(full_text), media_file), encoding="utf-8")
    mediainfo_path.write_text(_clean_mediainfo_text(str(regular_text), media_file), encoding="utf-8")
    json_payload = _json_payload(json_text)
    json_path.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "generated",
        "content_path": str(Path(content_path).expanduser()),
        "media_file": str(media_file),
        "mediainfo_file": str(full_path),
        "mediainfo_summary_file": str(mediainfo_path),
        "mediainfo_json_file": str(json_path),
        "sha1": _file_sha1(full_path),
        "size_bytes": full_path.stat().st_size,
        "blockers": [],
    }


def _generate_screenshot_materials_sync(
    content_path: str,
    output_dir: str,
    count: int,
    parser: Callable[..., Any],
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    ffmpeg_binary: str | None,
) -> dict[str, Any]:
    media_file = find_primary_media_file(content_path)
    if media_file is None:
        return {
            "status": "blocked",
            "content_path": str(Path(content_path).expanduser()),
            "blockers": ["No supported video file was found for screenshot generation."],
        }
    screenshot_count = max(1, int(count or 1))
    ffmpeg = ffmpeg_binary or shutil.which("ffmpeg")
    if not ffmpeg:
        return {
            "status": "blocked",
            "content_path": str(Path(content_path).expanduser()),
            "media_file": str(media_file),
            "blockers": ["ffmpeg binary was not found for screenshot generation."],
        }

    duration = _media_duration_seconds(media_file, parser)
    timestamps = _screenshot_timestamps(duration, screenshot_count)
    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    run = runner or _run_subprocess
    files = []
    blockers = []
    for index, timestamp in enumerate(timestamps, start=1):
        output_path = destination_dir / f"screenshot-{index:02d}.png"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(media_file),
            "-frames:v",
            "1",
            "-compression_level",
            "6",
            str(output_path),
        ]
        result = run(command)
        if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
            stderr = (result.stderr or "").strip()
            blockers.append(f"Screenshot {index} failed at {timestamp:.3f}s" + (f": {stderr}" if stderr else "."))
            continue
        files.append(
            {
                "path": str(output_path),
                "timestamp_seconds": timestamp,
                "sha1": _file_sha1(output_path),
                "size_bytes": output_path.stat().st_size,
            }
        )
    return {
        "status": "generated" if len(files) == screenshot_count and not blockers else "blocked",
        "content_path": str(Path(content_path).expanduser()),
        "media_file": str(media_file),
        "duration_seconds": duration,
        "count": len(files),
        "requested_count": screenshot_count,
        "screenshot_files": [file["path"] for file in files],
        "files": files,
        "blockers": blockers,
    }


def _generate_bdinfo_material_sync(
    content_path: str,
    output_dir: str,
    base_dir: str | None,
    playlist: str | None,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    bdinfo_binary: str | None,
) -> dict[str, Any]:
    disc = _find_bdmv_dir(content_path)
    if disc is None:
        return {"status": "skipped", "content_path": str(Path(content_path).expanduser()), "blockers": ["No BDMV directory was found for BDInfo generation."]}
    selected_playlist = _bdinfo_playlist(disc, playlist)
    if selected_playlist is None:
        return {
            "status": "blocked",
            "content_path": str(Path(content_path).expanduser()),
            "bdmv_dir": str(disc),
            "blockers": ["No .mpls playlist was found under BDMV/PLAYLIST for BDInfo generation."],
        }
    binary = bdinfo_binary or _bdinfo_binary(base_dir)
    if not binary:
        return {
            "status": "blocked",
            "content_path": str(Path(content_path).expanduser()),
            "bdmv_dir": str(disc),
            "playlist": selected_playlist.name,
            "blockers": ["bdinfo/BDInfo binary was not found for BDInfo generation."],
        }
    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    command = [binary, str(disc), "-m", selected_playlist.name, str(destination_dir)]
    result = (runner or _run_subprocess)(command)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return {
            "status": "blocked",
            "content_path": str(Path(content_path).expanduser()),
            "bdmv_dir": str(disc),
            "playlist": selected_playlist.name,
            "command": command,
            "blockers": ["BDInfo generation failed" + (f": {stderr}" if stderr else ".")],
        }
    generated = _latest_bdinfo_output(destination_dir)
    if generated is None:
        return {
            "status": "blocked",
            "content_path": str(Path(content_path).expanduser()),
            "bdmv_dir": str(disc),
            "playlist": selected_playlist.name,
            "command": command,
            "blockers": ["BDInfo command completed but no BDINFO*.txt output was found."],
        }
    output_path = destination_dir / "BD_FULL_00.txt"
    text = generated.read_text(encoding="utf-8", errors="replace")
    output_path.write_text(text, encoding="utf-8")
    return {
        "status": "generated",
        "content_path": str(Path(content_path).expanduser()),
        "bdmv_dir": str(disc),
        "playlist": selected_playlist.name,
        "bdinfo_file": str(output_path),
        "raw_bdinfo_file": str(generated),
        "sha1": _file_sha1(output_path),
        "size_bytes": output_path.stat().st_size,
        "command": command,
        "blockers": [],
    }


def _clean_mediainfo_text(text: str, media_file: Path) -> str:
    cleaned = "\n".join(line for line in text.splitlines() if not line.strip().startswith(("ReportBy", "Report created by ")))
    return cleaned.replace(str(media_file), media_file.name)


def _json_payload(text: Any) -> Any:
    try:
        return json.loads(str(text))
    except json.JSONDecodeError:
        return {"raw": str(text)}


def _file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _media_duration_seconds(media_file: Path, parser: Callable[..., Any]) -> float | None:
    try:
        payload = _json_payload(parser(str(media_file), output="JSON"))
    except Exception:
        return None
    tracks = payload.get("media", {}).get("track", []) if isinstance(payload, dict) else []
    if not isinstance(tracks, list):
        return None
    durations = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        raw_duration = track.get("Duration") or track.get("duration")
        if raw_duration in (None, ""):
            continue
        try:
            duration = float(str(raw_duration).split(" / ")[0])
        except ValueError:
            continue
        if duration > 0:
            durations.append(duration / 1000 if duration > 86400 else duration)
    return max(durations) if durations else None


def _screenshot_timestamps(duration: float | None, count: int) -> list[float]:
    if duration and duration > 20:
        start = duration * 0.12
        end = duration * 0.88
        step = (end - start) / (count + 1)
        return [max(1.0, start + step * index) for index in range(1, count + 1)]
    return [float(30 * index) for index in range(1, count + 1)]


def _run_subprocess(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)


def _find_bdmv_dir(content_path: str) -> Path | None:
    root = Path(content_path).expanduser()
    if root.is_dir() and root.name.upper() == "BDMV":
        return root
    bdmv = root / "BDMV"
    return bdmv if bdmv.is_dir() else None


def _bdinfo_playlist(bdmv_dir: Path, playlist: str | None) -> Path | None:
    playlist_dir = bdmv_dir / "PLAYLIST"
    if playlist:
        candidate = playlist_dir / Path(playlist).name
        return candidate if candidate.is_file() else None
    playlists = sorted(path for path in playlist_dir.glob("*.mpls") if path.is_file())
    return playlists[0] if playlists else None


def _bdinfo_binary(base_dir: str | None) -> str | None:
    bundled = _bundled_bdinfo_binary(base_dir)
    if bundled:
        return bundled
    return shutil.which("bdinfo") or shutil.which("BDInfo")


def _bundled_bdinfo_binary(base_dir: str | None) -> str | None:
    if not base_dir:
        return None
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        folder = "linux/amd64" if machine in {"x86_64", "amd64"} else "linux/arm64" if machine in {"arm64", "aarch64"} else "linux/arm"
        candidate = Path(base_dir) / "bin" / "bdinfo" / folder / "bdinfo"
    elif system == "darwin":
        folder = "macos/arm64" if machine == "arm64" else "macos/x86_64"
        candidate = Path(base_dir) / "bin" / "bdinfo" / folder / "bdinfo"
    elif system == "windows":
        candidate = Path(base_dir) / "bin" / "bdinfo" / "windows" / "x86_64" / "bdinfo.exe"
    else:
        return None
    return str(candidate) if candidate.is_file() else None


def _latest_bdinfo_output(output_dir: Path) -> Path | None:
    candidates = [path for path in output_dir.glob("BDINFO*.txt") if path.is_file()]
    return max(candidates, key=lambda path: (path.stat().st_mtime, str(path))) if candidates else None


def _write_image_host_payload(output_dir: str, payload: dict[str, Any]) -> Path:
    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    output_path = destination_dir / "image-host-uploads.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def _default_image_host(config: dict[str, Any]) -> str | None:
    default = config.get("DEFAULT", {}) if isinstance(config, dict) else {}
    for key in ("img_host_1", "img_host", "imghost"):
        value = default.get(key) if isinstance(default, dict) else None
        if value:
            return str(value)
    return None


async def _focused_upload_image_task(args: list[Any]) -> dict[str, Any]:
    image, img_host, config, _meta = args
    host = str(img_host or "").strip().lower()
    if not host:
        return {"status": "failed", "reason": "No image host was supplied."}
    if host == "ptpimg":
        return await _upload_ptpimg(str(image), config)
    if host in {"imgbb", "dalexni"}:
        endpoint = "https://api.imgbb.com/1/upload" if host == "imgbb" else "https://dalexni.com/1/upload"
        api_key = _default_config_value(config, f"{host}_api")
        return await _upload_chevereto_base64(str(image), endpoint, api_key, key_field="key", image_field="image", host_name=host)
    if host in {"ptscreens", "utppm", "onlyimage", "lensdump", "passtheimage"}:
        return await _upload_chevereto_api(str(image), host, config)
    if host == "zipline":
        return await _upload_zipline(str(image), config)
    if host == "sharex":
        return await _upload_sharex(str(image), config)
    if host == "pixhost":
        return await _upload_pixhost(str(image))
    if host == "imgbox":
        return {
            "status": "failed",
            "reason": "imgbox upload requires the legacy pyimgbox dependency; choose an API image host for focused ptcli or supply --image-host-file.",
        }
    return {"status": "failed", "reason": f"Unsupported focused ptcli image host: {host}"}


async def _upload_ptpimg(image: str, config: dict[str, Any]) -> dict[str, Any]:
    api_key = _default_config_value(config, "ptpimg_api")
    if not api_key:
        return {"status": "failed", "reason": "Missing ptpimg API key in config DEFAULT.ptpimg_api"}
    try:
        async with aiofiles.open(image, "rb") as file:
            files = {"file-upload[0]": (os.path.basename(image), await file.read())}
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://ptpimg.me/upload.php",
                headers={"referer": "https://ptpimg.me/index.php"},
                data={"format": "json", "api_key": api_key},
                files=files,
                timeout=60,
            )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException:
        return {"status": "failed", "reason": "Request timed out"}
    except (httpx.RequestError, ValueError) as exc:
        return {"status": "failed", "reason": f"ptpimg upload failed: {exc}"}
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict) or not payload[0].get("code"):
        return {"status": "failed", "reason": "Invalid JSON response from ptpimg"}
    code = payload[0]["code"]
    ext = payload[0].get("ext") or "png"
    url = f"https://ptpimg.me/{code}.{ext}"
    return {"status": "success", "img_url": url, "raw_url": url, "web_url": url, "local_file_path": image}


async def _upload_chevereto_api(image: str, host: str, config: dict[str, Any]) -> dict[str, Any]:
    endpoints = {
        "ptscreens": ("https://ptscreens.com/api/1/upload", "ptscreens_api", "source", "X-API-Key"),
        "utppm": ("https://utp.pm/api/1/upload", "utppm_api", "source", "X-API-Key"),
        "onlyimage": ("https://onlyimage.org/api/1/upload", "onlyimage_api", "image", "X-API-Key"),
        "lensdump": ("https://lensdump.com/api/1/upload", "lensdump_api", "image", "X-API-Key"),
        "passtheimage": ("https://passtheima.ge/api/1/upload", "passtheima_ge_api", "source", "X-API-Key"),
    }
    endpoint, key_name, image_field, header_name = endpoints[host]
    api_key = _default_config_value(config, key_name)
    if not api_key:
        return {"status": "failed", "reason": f"Missing {host} API key in config DEFAULT.{key_name}"}
    async with aiofiles.open(image, "rb") as file:
        encoded = base64.b64encode(await file.read()).decode("utf8")
    return await _post_chevereto_payload(endpoint, {image_field: encoded}, {header_name: api_key}, host, image)


async def _upload_chevereto_base64(image: str, endpoint: str, api_key: str | None, *, key_field: str, image_field: str, host_name: str) -> dict[str, Any]:
    if not api_key:
        return {"status": "failed", "reason": f"Missing {host_name} API key in config DEFAULT.{host_name}_api"}
    async with aiofiles.open(image, "rb") as file:
        encoded = base64.b64encode(await file.read()).decode("utf8")
    return await _post_chevereto_payload(endpoint, {key_field: api_key, image_field: encoded}, {}, host_name, image)


async def _post_chevereto_payload(endpoint: str, data: dict[str, str], headers: dict[str, str], host_name: str, image: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(endpoint, data=data, headers=headers, timeout=60)
        payload = response.json()
    except httpx.TimeoutException:
        return {"status": "failed", "reason": "Request timed out"}
    except (httpx.RequestError, ValueError) as exc:
        return {"status": "failed", "reason": f"{host_name} upload failed: {exc}"}
    if response.status_code not in (200, 201):
        return {"status": "failed", "reason": f"{host_name} upload failed with status code {response.status_code}"}
    urls = _chevereto_urls(payload)
    if not urls:
        return {"status": "failed", "reason": f"No valid URLs returned from {host_name}"}
    return {"status": "success", **urls, "local_file_path": image}


async def _upload_zipline(image: str, config: dict[str, Any]) -> dict[str, Any]:
    url = _default_config_value(config, "zipline_url")
    api_key = _default_config_value(config, "zipline_api_key")
    if not url or not api_key:
        return {"status": "failed", "reason": "Missing Zipline URL or API key in config DEFAULT.zipline_url/zipline_api_key"}
    try:
        async with aiofiles.open(image, "rb") as file:
            files = {"file": (os.path.basename(image), await file.read())}
        async with httpx.AsyncClient() as client:
            response = await client.post(url, files=files, headers={"Authorization": api_key}, timeout=60)
        payload = response.json()
    except httpx.TimeoutException:
        return {"status": "failed", "reason": "Request timed out"}
    except (httpx.RequestError, ValueError) as exc:
        return {"status": "failed", "reason": f"zipline upload failed: {exc}"}
    if response.status_code not in (200, 201) or not isinstance(payload, dict) or not payload.get("files"):
        return {"status": "failed", "reason": "No valid URL returned from Zipline"}
    url = str(payload["files"][0])
    return {"status": "success", "img_url": url, "raw_url": url.replace("/u/", "/r/"), "web_url": url.replace("/u/", "/r/"), "local_file_path": image}


async def _upload_sharex(image: str, config: dict[str, Any]) -> dict[str, Any]:
    url = _default_config_value(config, "sharex_url") or "https://img.digitalcore.club/api/upload"
    api_key = _default_config_value(config, "sharex_api_key")
    if not api_key:
        return {"status": "failed", "reason": "Missing ShareX image host token in config DEFAULT.sharex_api_key"}
    try:
        async with aiofiles.open(image, "rb") as file:
            files = {"file": (os.path.basename(image), await file.read())}
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers={"Authorization": api_key}, data={"title": "Upload-Assistant screenshot"}, files=files, timeout=60)
        payload = response.json()
    except httpx.TimeoutException:
        return {"status": "failed", "reason": "Request timed out"}
    except (httpx.RequestError, ValueError) as exc:
        return {"status": "failed", "reason": f"sharex upload failed: {exc}"}
    link = payload.get("data", {}).get("link") if isinstance(payload.get("data"), dict) else None
    link = link or payload.get("link")
    if response.status_code not in (200, 201) or not link:
        return {"status": "failed", "reason": "No link in sharex response"}
    return {"status": "success", "img_url": link, "raw_url": link, "web_url": link, "local_file_path": image}


async def _upload_pixhost(image: str) -> dict[str, Any]:
    try:
        async with aiofiles.open(image, "rb") as file:
            files = {"img": ("file-upload[0]", await file.read())}
        async with httpx.AsyncClient() as client:
            response = await client.post("https://api.pixhost.to/images", data={"content_type": "0", "max_th_size": "350"}, files=files, timeout=60)
        payload = response.json()
    except httpx.TimeoutException:
        return {"status": "failed", "reason": "Request timed out"}
    except (httpx.RequestError, ValueError) as exc:
        return {"status": "failed", "reason": f"pixhost upload failed: {exc}"}
    if response.status_code != 200 or not isinstance(payload, dict) or not payload.get("th_url"):
        return {"status": "failed", "reason": "Invalid response from pixhost"}
    img_url = payload["th_url"]
    raw_url = str(img_url).replace("https://t", "https://img").replace("/thumbs/", "/images/")
    return {"status": "success", "img_url": img_url, "raw_url": raw_url, "web_url": payload.get("show_url") or raw_url, "local_file_path": image}


def _chevereto_urls(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload.get("image") if isinstance(payload.get("image"), dict) else None
    if not isinstance(data, dict):
        return None
    image = data.get("image") if isinstance(data.get("image"), dict) else {}
    medium = data.get("medium") if isinstance(data.get("medium"), dict) else {}
    thumb = data.get("thumb") if isinstance(data.get("thumb"), dict) else {}
    img_url = medium.get("url") or thumb.get("url") or image.get("url") or data.get("url")
    raw_url = image.get("url") or data.get("url")
    web_url = data.get("url_viewer") or raw_url
    if not img_url or not raw_url or not web_url:
        return None
    return {"img_url": img_url, "raw_url": raw_url, "web_url": web_url}


def _default_config_value(config: dict[str, Any], key: str) -> str | None:
    default = config.get("DEFAULT", {}) if isinstance(config, dict) else {}
    if not isinstance(default, dict):
        return None
    value = default.get(key)
    return str(value).strip() if value else None


def _hidden_path(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return any(part.startswith(".") for part in relative.parts)
