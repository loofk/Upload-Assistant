"""Target tracker dry-run preparation previews for ptcli."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.trackers.MTEAM import MTEAM


def write_mteam_prepare_package(
    source_info: dict[str, Any] | None,
    target_trackers: list[str],
    stages: list[dict[str, Any]],
    content_path: str | None,
    output_dir: str,
) -> dict[str, Any]:
    preview = build_mteam_prepare_preview(source_info, target_trackers, stages, content_path)
    package_dir = _prepare_package_dir(output_dir, source_info)
    preview_path = package_dir / "mteam-prepare-preview.json"
    meta_draft_path = package_dir / "mteam-meta-draft.json"
    field_mapping_path = package_dir / "mteam-field-mapping.json"

    _write_json(preview_path, preview)
    _write_json(meta_draft_path, preview["meta_draft"])
    _write_json(field_mapping_path, preview["field_mapping"])

    return {
        **preview,
        "package_dir": str(package_dir),
        "files": {
            "preview": str(preview_path),
            "meta_draft": str(meta_draft_path),
            "field_mapping": str(field_mapping_path),
        },
    }


async def search_mteam_duplicates(config: dict[str, Any], source_info: dict[str, Any] | None) -> dict[str, Any]:
    if not source_info:
        return {"searched": False, "reason": "Source metadata is not available.", "count": 0, "dupes": []}
    imdb_id = source_info.get("imdb_id")
    if not imdb_id:
        return {"searched": False, "reason": "MTEAM duplicate search requires an IMDb id.", "count": 0, "dupes": []}

    meta = build_mteam_meta_draft(source_info, source_info.get("content_path"))
    meta_for_search = {
        **meta,
        "imdb_id": imdb_id,
        "imdb": str(imdb_id).removeprefix("tt"),
        "uuid": source_info.get("name") or "ptcli-mteam-dupe-check",
        "debug": False,
    }
    tracker = MTEAM(config=config)
    try:
        dupes = await tracker.search_existing(meta_for_search, "")
    finally:
        await tracker.session.aclose()

    dupe_list = dupes if isinstance(dupes, list) else []
    return {
        "searched": True,
        "query": {"imdb": f"tt{meta_for_search['imdb']}"},
        "count": len(dupe_list),
        "dupes": dupe_list,
    }


def build_mteam_prepare_preview(source_info: dict[str, Any] | None, target_trackers: list[str], stages: list[dict[str, Any]], content_path: str | None) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []

    if "MTEAM" not in target_trackers:
        blockers.append("MTEAM is not in target trackers.")
    if not source_info:
        blockers.append("Source metadata is not available.")

    verified_content = _has_verified_content(stages)
    if not verified_content:
        warnings.append("No completed qBittorrent match/wait evidence is available yet.")

    metadata = {
        "name": source_info.get("name") if source_info else None,
        "imdb_id": source_info.get("imdb_id") if source_info else None,
        "tmdb_id": source_info.get("tmdb_id") if source_info else None,
        "douban_id": source_info.get("douban_id") if source_info else None,
        "torrenthash": source_info.get("torrenthash") if source_info else None,
        "description_length": source_info.get("description_length") if source_info else 0,
    }

    if not any([metadata["imdb_id"], metadata["tmdb_id"], metadata["douban_id"], metadata["name"]]):
        blockers.append("MTEAM preview needs at least one usable title or metadata identifier.")

    meta_draft = build_mteam_meta_draft(source_info, content_path)
    field_mapping = build_mteam_field_mapping(meta_draft)
    missing_fields = [key for key in ("name", "smallDescr", "category") if not field_mapping.get(key)]
    if missing_fields:
        blockers.append(f"MTEAM field mapping is missing required field(s): {', '.join(missing_fields)}.")

    return {
        "target_tracker": "MTEAM",
        "dry_run": True,
        "content_path": content_path,
        "verified_content": verified_content,
        "metadata": metadata,
        "meta_draft": meta_draft,
        "field_mapping": field_mapping,
        "missing_fields": missing_fields,
        "blockers": blockers,
        "warnings": warnings,
        "next_actions": [
            "Review source and target tracker rules manually.",
            "Confirm qBittorrent has the complete payload before enabling upload.",
            "Generate tracker-specific MTEAM upload metadata and run duplicate checks.",
        ],
    }


def build_mteam_meta_draft(source_info: dict[str, Any] | None, content_path: str | None) -> dict[str, Any]:
    name = str(source_info.get("name") or "").strip() if source_info else ""
    resolution = _infer_resolution(name)
    media_type = _infer_type(name)
    category = _infer_category(name)
    imdb_id = source_info.get("imdb_id") if source_info else None
    douban_id = source_info.get("douban_id") if source_info else None
    return {
        "name": name or None,
        "title": _infer_title(name) if name else None,
        "category": category,
        "type": media_type,
        "resolution": resolution,
        "imdb_id": imdb_id,
        "imdb": str(imdb_id) if imdb_id else None,
        "tmdb_id": source_info.get("tmdb_id") if source_info else None,
        "douban_id": douban_id,
        "douban_url": source_info.get("douban_url") if source_info else None,
        "torrenthash": source_info.get("torrenthash") if source_info else None,
        "content_path": content_path,
    }


def build_mteam_field_mapping(meta_draft: dict[str, Any]) -> dict[str, Any]:
    douban_url = meta_draft.get("douban_url")
    if not douban_url and meta_draft.get("douban_id"):
        douban_url = f"https://movie.douban.com/subject/{meta_draft['douban_id']}/"

    mapping: dict[str, Any] = {
        "name": meta_draft.get("name"),
        "smallDescr": meta_draft.get("title") or meta_draft.get("name"),
        "category": _mteam_category_id(meta_draft),
        "standard": _mteam_standard_id(str(meta_draft.get("resolution") or "")),
        "anonymous": True,
    }
    if meta_draft.get("imdb"):
        mapping["imdb"] = f"https://www.imdb.com/title/tt{meta_draft['imdb']}"
    if douban_url:
        mapping["douban"] = douban_url
    return mapping


def _has_verified_content(stages: list[dict[str, Any]]) -> bool:
    for stage in stages:
        stage_name = stage.get("stage")
        result = stage.get("result", {})
        if stage_name == "wait-complete" and stage.get("ok") and isinstance(result, dict) and result.get("complete"):
            return True
        if stage_name == "match" and stage.get("ok") and isinstance(result, dict) and int(result.get("count", 0) or 0) > 0:
            return True
    return False


def _infer_resolution(name: str) -> str | None:
    lowered = name.lower()
    if "2160p" in lowered or "4k" in lowered:
        return "2160p"
    if "1080p" in lowered:
        return "1080p"
    if "1080i" in lowered:
        return "1080i"
    if "720p" in lowered:
        return "720p"
    if any(token in lowered for token in ("576p", "576i", "480p", "480i")):
        return "SD"
    return None


def _infer_type(name: str) -> str | None:
    lowered = name.lower()
    if "remux" in lowered:
        return "REMUX"
    if "web-dl" in lowered or "webdl" in lowered:
        return "WEBDL"
    if "webrip" in lowered:
        return "WEBRIP"
    if "hdtv" in lowered:
        return "HDTV"
    if "blu-ray" in lowered or "bluray" in lowered:
        return "DISC"
    return None


def _infer_category(name: str) -> str:
    lowered = name.lower()
    if re.search(r"\bs\d{1,2}\b|\bs\d{1,2}e\d{1,3}\b", lowered):
        return "TV"
    return "MOVIE"


def _infer_title(name: str) -> str | None:
    if not name:
        return None
    title = re.split(r"\b(?:19|20)\d{2}\b", name, maxsplit=1)[0].strip(". -_")
    return title.replace(".", " ") if title else name


def _mteam_category_id(meta_draft: dict[str, Any]) -> int | None:
    category = meta_draft.get("category")
    media_type = meta_draft.get("type")
    resolution = str(meta_draft.get("resolution") or "").lower()
    if category == "TV":
        return 402 if resolution in {"2160p", "1080p", "1080i", "720p"} else 403
    if media_type == "REMUX":
        return 439
    if resolution in {"2160p", "1080p", "1080i", "720p"}:
        return 419
    return 401 if category == "MOVIE" else None


def _mteam_standard_id(resolution: str) -> int | None:
    lowered = resolution.lower()
    if "2160p" in lowered or "4k" in lowered:
        return 6
    if "1080p" in lowered:
        return 1
    if "1080i" in lowered:
        return 2
    if "720p" in lowered:
        return 3
    if lowered == "sd":
        return 5
    return None


def _prepare_package_dir(output_dir: str, source_info: dict[str, Any] | None) -> Path:
    tracker = str(source_info.get("tracker") or "SOURCE") if source_info else "SOURCE"
    torrent_id = str(source_info.get("torrent_id") or "unknown") if source_info else "unknown"
    package_dir = Path(output_dir).expanduser() / f"{tracker}-{torrent_id}-to-MTEAM"
    package_dir.mkdir(parents=True, exist_ok=True)
    return package_dir


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
