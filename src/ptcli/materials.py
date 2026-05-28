"""Material generation helpers for focused ptcli retorrent packages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def _hidden_path(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return any(part.startswith(".") for part in relative.parts)
