"""Target tracker dry-run preparation previews for ptcli."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.trackers.MTEAM import MTEAM

REQUIRED_MTEAM_PACKAGE_FILES = {
    "preview": "mteam-prepare-preview.json",
    "meta_draft": "mteam-meta-draft.json",
    "field_mapping": "mteam-field-mapping.json",
    "description_draft": "mteam-description-draft.txt",
    "upload_gate": "mteam-upload-gate.json",
}


def write_mteam_prepare_package(
    source_info: dict[str, Any] | None,
    target_trackers: list[str],
    stages: list[dict[str, Any]],
    content_path: str | None,
    output_dir: str,
    accept_rules: bool = False,
) -> dict[str, Any]:
    preview = build_mteam_prepare_preview(source_info, target_trackers, stages, content_path)
    package_dir = _prepare_package_dir(output_dir, source_info)
    preview_path = package_dir / "mteam-prepare-preview.json"
    meta_draft_path = package_dir / "mteam-meta-draft.json"
    field_mapping_path = package_dir / "mteam-field-mapping.json"
    description_path = package_dir / "mteam-description-draft.txt"
    upload_gate_path = package_dir / "mteam-upload-gate.json"
    upload_gate = build_mteam_upload_gate(preview, stages, accept_rules=accept_rules)

    _write_json(preview_path, preview)
    _write_json(meta_draft_path, preview["meta_draft"])
    _write_json(field_mapping_path, preview["field_mapping"])
    description_path.write_text(build_mteam_description_draft(preview["meta_draft"], source_info), encoding="utf-8")
    _write_json(upload_gate_path, upload_gate)

    return {
        **preview,
        "upload_gate": upload_gate,
        "package_dir": str(package_dir),
        "files": {
            "preview": str(preview_path),
            "meta_draft": str(meta_draft_path),
            "field_mapping": str(field_mapping_path),
            "description_draft": str(description_path),
            "upload_gate": str(upload_gate_path),
        },
    }


MTeamUploadCallable = Callable[[dict[str, Any], str, str], Awaitable[dict[str, Any]]]
MTeamDownloadCallable = Callable[[dict[str, Any], str, str], Awaitable[str]]


def build_mteam_upload_preflight(package_dir: str, execute: bool = False, torrent_file: str | None = None, write_payload: bool = False) -> dict[str, Any]:
    package = load_mteam_prepare_package(package_dir)
    gate = package.get("upload_gate", {})
    files = package.get("files", {})
    blockers = list(package.get("blockers", []))
    payload_summary = build_mteam_upload_payload_summary(package, torrent_file=torrent_file)
    blockers.extend(payload_summary["blockers"])

    if not isinstance(gate, dict) or not gate.get("ready"):
        blockers.append("MTEAM upload gate is not ready.")
    if write_payload:
        payload_path = Path(package_dir).expanduser() / "mteam-upload-payload.json"
        _write_json(payload_path, payload_summary)
        files = {**files, "upload_payload": str(payload_path)}

    return {
        "status": "blocked" if blockers else "ready",
        "target_tracker": "MTEAM",
        "dry_run": not execute,
        "package_dir": str(Path(package_dir).expanduser()),
        "files": files,
        "upload_gate": gate,
        "upload_payload": payload_summary,
        "blockers": blockers,
        "next_actions": _upload_preflight_next_actions(blockers, execute),
    }


async def upload_mteam_from_package(
    config: dict[str, Any],
    package_dir: str,
    torrent_file: str,
    *,
    execute: bool = False,
    confirm_upload: bool = False,
    write_payload: bool = False,
    download_uploaded: bool = False,
    uploaded_output_dir: str | None = None,
    uploader: MTeamUploadCallable | None = None,
    downloader: MTeamDownloadCallable | None = None,
) -> dict[str, Any]:
    preflight = build_mteam_upload_preflight(package_dir, execute=execute, torrent_file=torrent_file, write_payload=write_payload)
    blockers = list(preflight["blockers"])
    if not execute:
        return preflight
    if not confirm_upload:
        blockers.append("MTEAM live upload requires --confirm-upload.")
    if blockers:
        return {**preflight, "status": "blocked", "dry_run": False, "blockers": blockers}

    upload_func = uploader or _submit_mteam_upload
    upload_result = await upload_func(config, package_dir, torrent_file)
    result = {
        **preflight,
        "status": "uploaded",
        "dry_run": False,
        "upload_result": upload_result,
    }
    if download_uploaded:
        torrent_id = extract_mteam_uploaded_torrent_id(upload_result)
        if not torrent_id:
            return {
                **result,
                "status": "uploaded-needs-review",
                "blockers": ["MTEAM upload response did not include a torrent id for target torrent download."],
            }
        download_func = downloader or _download_mteam_uploaded_torrent
        default_output_dir = await asyncio.to_thread(_expand_path_string, package_dir)
        downloaded_path = await download_func(config, torrent_id, uploaded_output_dir or default_output_dir)
        return {
            **result,
            "downloaded_torrent": {
                "torrent_id": torrent_id,
                "path": downloaded_path,
            },
        }
    return result


def extract_mteam_uploaded_torrent_id(upload_result: dict[str, Any]) -> str | None:
    response = upload_result.get("response") if isinstance(upload_result, dict) else None
    if not isinstance(response, dict):
        return None
    for key in ("id", "torrentId", "torrent_id"):
        value = response.get(key)
        if value:
            return str(value)
    return None


def build_mteam_upload_payload_summary(package: dict[str, Any], torrent_file: str | None = None) -> dict[str, Any]:
    field_mapping = package.get("field_mapping", {})
    description_length = int(package.get("description_length", 0) or 0)
    form_fields = _mteam_upload_form_fields(field_mapping, description_length)
    torrent_summary, torrent_blockers = _torrent_file_summary(torrent_file)
    missing_fields = [field for field in ("name", "smallDescr", "descr", "category") if not form_fields.get(field)]
    blockers = [f"MTEAM upload payload is missing required field(s): {', '.join(missing_fields)}."] if missing_fields else []
    blockers.extend(torrent_blockers)

    return {
        "endpoint": "https://api.m-team.cc/api/torrent/createOredit",
        "method": "POST",
        "multipart": True,
        "form_fields": form_fields,
        "file_field": "file",
        "torrent_file": torrent_summary,
        "blockers": blockers,
    }


def load_mteam_prepare_package(package_dir: str) -> dict[str, Any]:
    root = Path(package_dir).expanduser()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"MTEAM package directory not found: {root}")

    paths = {key: root / filename for key, filename in REQUIRED_MTEAM_PACKAGE_FILES.items()}
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise ValueError(f"MTEAM package is missing required file(s): {', '.join(missing)}")

    preview = _read_json(paths["preview"])
    meta_draft = _read_json(paths["meta_draft"])
    field_mapping = _read_json(paths["field_mapping"])
    upload_gate = _read_json(paths["upload_gate"])
    description = paths["description_draft"].read_text(encoding="utf-8")

    blockers: list[str] = []
    if not description.strip():
        blockers.append("MTEAM description draft is empty.")
    if not isinstance(upload_gate, dict):
        blockers.append("MTEAM upload gate file is invalid.")
        upload_gate = {}
    if not isinstance(field_mapping, dict) or not field_mapping.get("name") or not field_mapping.get("category"):
        blockers.append("MTEAM field mapping is missing name or category.")

    return {
        "target_tracker": "MTEAM",
        "package_dir": str(root),
        "files": {key: str(path) for key, path in paths.items()},
        "preview": preview,
        "meta_draft": meta_draft,
        "field_mapping": field_mapping,
        "description_length": len(description),
        "upload_gate": upload_gate,
        "blockers": blockers,
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


def build_mteam_description_draft(meta_draft: dict[str, Any], source_info: dict[str, Any] | None) -> str:
    lines = [
        "[b]Retorrent review draft[/b]",
        "",
        f"[b]Title[/b]: {meta_draft.get('title') or ''}",
        f"[b]Release name[/b]: {meta_draft.get('name') or ''}",
        f"[b]Category[/b]: {meta_draft.get('category') or ''}",
        f"[b]Type[/b]: {meta_draft.get('type') or ''}",
        f"[b]Resolution[/b]: {meta_draft.get('resolution') or ''}",
        f"[b]IMDb[/b]: {meta_draft.get('imdb') or ''}",
        f"[b]TMDb[/b]: {meta_draft.get('tmdb_id') or ''}",
        f"[b]Douban[/b]: {meta_draft.get('douban_url') or meta_draft.get('douban_id') or ''}",
        "",
        "[b]Source evidence[/b]",
        f"Source tracker: {source_info.get('tracker') if source_info else ''}",
        f"Source torrent id: {source_info.get('torrent_id') if source_info else ''}",
        f"Source torrent hash: {source_info.get('torrenthash') if source_info else ''}",
        f"Local content path: {meta_draft.get('content_path') or ''}",
        "",
        "[b]Manual review required[/b]",
        "Confirm source-site and MTEAM rules, transfer permissions, description requirements, screenshots, subtitles, naming, and duplicate status before upload.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def build_mteam_upload_gate(preview: dict[str, Any], stages: list[dict[str, Any]], accept_rules: bool) -> dict[str, Any]:
    dupe_stage = _find_stage(stages, "target-dupe-check")
    dupe_result = dupe_stage.get("result", {}) if dupe_stage else {}
    dupe_count = int(dupe_result.get("count", 0) or 0) if isinstance(dupe_result, dict) else 0
    dupe_searched = bool(dupe_result.get("searched")) if isinstance(dupe_result, dict) else False
    checks = [
        {
            "name": "rules_acknowledged",
            "ok": accept_rules,
            "message": "Rules have been acknowledged." if accept_rules else "Rules must be manually reviewed and acknowledged.",
        },
        {
            "name": "verified_content",
            "ok": bool(preview.get("verified_content")),
            "message": "qBittorrent has verified matching or complete content evidence.",
        },
        {
            "name": "target_fields",
            "ok": not preview.get("blockers") and not preview.get("missing_fields"),
            "message": "MTEAM required fields are present.",
        },
        {
            "name": "duplicate_check",
            "ok": dupe_searched and dupe_count == 0,
            "message": _dupe_gate_message(dupe_stage, dupe_searched, dupe_count),
        },
    ]
    return {
        "target_tracker": "MTEAM",
        "ready": all(check["ok"] for check in checks),
        "checks": checks,
        "dupe_count": dupe_count,
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


def _find_stage(stages: list[dict[str, Any]], stage_name: str) -> dict[str, Any] | None:
    for stage in stages:
        if stage.get("stage") == stage_name:
            return stage
    return None


def _dupe_gate_message(dupe_stage: dict[str, Any] | None, searched: bool, count: int) -> str:
    if not dupe_stage or dupe_stage.get("skipped"):
        return "MTEAM duplicate search has not been run."
    if not dupe_stage.get("ok") or not searched:
        return "MTEAM duplicate search did not complete successfully."
    if count > 0:
        return f"MTEAM duplicate search found {count} possible existing torrent(s)."
    return "MTEAM duplicate search found no existing torrents."


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


async def _submit_mteam_upload(config: dict[str, Any], package_dir: str, torrent_file: str) -> dict[str, Any]:
    package = load_mteam_prepare_package(package_dir)
    torrent_path, torrent_bytes, description = await asyncio.to_thread(_read_mteam_upload_files, package, torrent_file)
    data = _mteam_upload_form_fields(package["field_mapping"], len(description))
    data["descr"] = description
    files = {
        "file": (torrent_path.name, torrent_bytes, "application/x-bittorrent"),
    }

    tracker = MTEAM(config=config)
    try:
        response = await tracker._request("https://api.m-team.cc/api/torrent/createOredit", data=data, files=files)
    finally:
        await tracker.session.aclose()

    return {
        "submitted": True,
        "response": _summarize_mteam_upload_response(response),
    }


async def _download_mteam_uploaded_torrent(config: dict[str, Any], torrent_id: str, output_dir: str) -> str:
    destination = await asyncio.to_thread(_uploaded_torrent_destination, output_dir, torrent_id)
    tracker = MTEAM(config=config)
    try:
        await tracker.download_new_torrent(torrent_id, str(destination))
    finally:
        await tracker.session.aclose()
    await asyncio.to_thread(_assert_torrent_file, destination)
    return str(destination)


def _mteam_upload_form_fields(field_mapping: dict[str, Any], description_length: int) -> dict[str, Any]:
    form_fields: dict[str, Any] = {
        "name": field_mapping.get("name"),
        "smallDescr": field_mapping.get("smallDescr"),
        "descr": {"source": "mteam-description-draft.txt", "length": description_length} if description_length > 0 else None,
        "category": field_mapping.get("category"),
        "standard": field_mapping.get("standard"),
        "anonymous": field_mapping.get("anonymous", True),
        "dmmCode": "",
        "tags": "",
        "aids": "",
        "mediainfoAnalysisResult": None,
    }
    for optional_field in ("imdb", "douban"):
        if field_mapping.get(optional_field):
            form_fields[optional_field] = field_mapping[optional_field]
    return form_fields


def _read_mteam_upload_files(package: dict[str, Any], torrent_file: str) -> tuple[Path, bytes, str]:
    torrent_path = Path(torrent_file).expanduser()
    torrent_bytes = torrent_path.read_bytes()
    description = Path(package["files"]["description_draft"]).read_text(encoding="utf-8")
    return torrent_path, torrent_bytes, description


def _uploaded_torrent_destination(output_dir: str, torrent_id: str) -> Path:
    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    return destination_dir / f"MTEAM-{torrent_id}.torrent"


def _expand_path_string(path: str) -> str:
    return str(Path(path).expanduser())


def _assert_torrent_file(path: Path) -> None:
    if not path.exists():
        raise ValueError(f"MTEAM uploaded torrent download did not create file: {path}")
    data = path.read_bytes()
    if not data.startswith(b"d"):
        raise ValueError("MTEAM uploaded torrent download does not look like a .torrent file.")


def _torrent_file_summary(torrent_file: str | None) -> tuple[dict[str, Any] | None, list[str]]:
    if not torrent_file:
        return None, ["MTEAM upload torrent file is required."]

    path = Path(torrent_file).expanduser()
    if not path.exists() or not path.is_file():
        return {"path": str(path)}, [f"MTEAM upload torrent file does not exist: {path}"]

    data = path.read_bytes()
    blockers: list[str] = []
    if not data.startswith(b"d"):
        blockers.append("MTEAM upload torrent file does not look like a .torrent file.")
    return {
        "path": str(path),
        "filename": path.name,
        "size": len(data),
        "sha1": hashlib.sha1(data).hexdigest(),
    }, blockers


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _summarize_mteam_upload_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {"raw_type": type(response).__name__}
    summary: dict[str, Any] = {}
    for key in ("id", "torrentId", "torrent_id", "status", "message"):
        if key in response:
            summary[key] = response[key]
    return summary or {"keys": sorted(str(key) for key in response)}


def _upload_preflight_next_actions(blockers: list[str], execute: bool) -> list[str]:
    if blockers:
        return [
            "Review every blocker before upload.",
            "Regenerate the MTEAM package after fixing source metadata, qBittorrent evidence, duplicate check, or rules acknowledgement.",
        ]
    if execute:
        return ["Check MTEAM upload result, then download the generated target torrent and inject it into qBittorrent for seeding."]
    return ["Review the package manually, then rerun with the future live upload flag after upload support is enabled."]
