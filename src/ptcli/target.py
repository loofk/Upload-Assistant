"""Target tracker dry-run preparation previews for ptcli."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from torf import Torrent

from src.ptcli.mteam_api import MTeamApiClient, MTeamApiError

REQUIRED_MTEAM_PACKAGE_FILES = {
    "preview": "mteam-prepare-preview.json",
    "meta_draft": "mteam-meta-draft.json",
    "field_mapping": "mteam-field-mapping.json",
    "materials": "mteam-materials.json",
    "description_draft": "mteam-description-draft.txt",
    "rule_review": "mteam-rule-review.json",
    "upload_gate": "mteam-upload-gate.json",
}

MTEAM_PACKAGE_MANIFEST_FILENAME = "mteam-package-manifest.json"
MTEAM_UPLOAD_ANNOUNCE = "https://fake.tracker"
MTEAM_SOURCE_FLAG = "MTEAM"
MTEAM_UPLOAD_TORRENT_ALLOWED_TOP_LEVEL_FIELDS = frozenset({"announce", "comment", "creation date", "created by", "encoding", "info"})
MTEAM_STANDARD_ID_TO_RESOLUTION = {
    "1": "1080p",
    "2": "1080i",
    "3": "720p",
    "5": "SD",
    "6": "2160p",
    "7": "8K",
}
MTEAM_SOURCE_ID_TO_TYPE = {
    "1": "BluRay",
    "3": "DVD",
    "4": "REMUX",
    "5": "HDTV",
    "6": "Other",
    "8": "WEBDL",
}


def create_mteam_upload_torrent_candidate(torrent_file: str, output_dir: str | None = None) -> dict[str, Any]:
    source_path = Path(torrent_file).expanduser()
    if not source_path.exists():
        raise ValueError(f"Torrent file not found: {source_path}")
    target_dir = Path(output_dir).expanduser() if output_dir else source_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / f"{source_path.stem}.mteam-upload.torrent"

    torrent = Torrent.read(str(source_path), validate=False)
    removed_fields = []
    for key in list(torrent.metainfo):
        if key not in {"announce", "comment", "creation date", "created by", "encoding", "info"}:
            removed_fields.append(key)
            torrent.metainfo.pop(key, None)
    torrent.metainfo["announce"] = MTEAM_UPLOAD_ANNOUNCE
    torrent.metainfo["comment"] = ""
    torrent.metainfo["info"]["source"] = MTEAM_SOURCE_FLAG
    Torrent.copy(torrent).write(str(output_path), overwrite=True)
    return {
        "source_path": str(source_path),
        "path": str(output_path),
        "announce": MTEAM_UPLOAD_ANNOUNCE,
        "source_flag": MTEAM_SOURCE_FLAG,
        "removed_fields": removed_fields,
    }


def mteam_upload_torrent_candidate_summary(torrent_file: str) -> dict[str, Any]:
    path = Path(torrent_file).expanduser()
    summary: dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "has_mteam_upload_suffix": path.name.endswith(".mteam-upload.torrent"),
    }
    if not path.exists() or not path.is_file():
        summary.update({"exists": False, "metadata_readable": False, "mteam_safe": False})
        return summary

    summary["exists"] = True
    summary.update(_torrent_metadata_summary(path))
    return summary


def write_mteam_prepare_package(
    source_info: dict[str, Any] | None,
    target_trackers: list[str],
    stages: list[dict[str, Any]],
    content_path: str | None,
    output_dir: str,
    accept_rules: bool = False,
    material_files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preview = build_mteam_prepare_preview(source_info, target_trackers, stages, content_path)
    package_dir = _prepare_package_dir(output_dir, source_info)
    preview_path = package_dir / "mteam-prepare-preview.json"
    meta_draft_path = package_dir / "mteam-meta-draft.json"
    field_mapping_path = package_dir / "mteam-field-mapping.json"
    materials_path = package_dir / "mteam-materials.json"
    description_path = package_dir / "mteam-description-draft.txt"
    rule_review_path = package_dir / "mteam-rule-review.json"
    upload_gate_path = package_dir / "mteam-upload-gate.json"
    manifest_path = package_dir / MTEAM_PACKAGE_MANIFEST_FILENAME
    rule_review = build_mteam_rule_review(stages, accept_rules=accept_rules)
    upload_gate = build_mteam_upload_gate(preview, stages, accept_rules=accept_rules)
    materials = build_mteam_materials_manifest(preview, source_info, content_path, material_files=material_files)
    package_blockers = _mteam_prepare_package_blockers(preview, rule_review, upload_gate)
    files = {
        "preview": str(preview_path),
        "meta_draft": str(meta_draft_path),
        "field_mapping": str(field_mapping_path),
        "materials": str(materials_path),
        "description_draft": str(description_path),
        "rule_review": str(rule_review_path),
        "upload_gate": str(upload_gate_path),
        "manifest": str(manifest_path),
    }

    _write_json(preview_path, preview)
    _write_json(meta_draft_path, preview["meta_draft"])
    _write_json(field_mapping_path, preview["field_mapping"])
    _write_json(materials_path, materials)
    description_path.write_text(build_mteam_description_draft(preview["meta_draft"], source_info, materials=materials), encoding="utf-8")
    _write_json(rule_review_path, rule_review)
    _write_json(upload_gate_path, upload_gate)
    package_manifest = build_mteam_package_manifest(preview, rule_review, upload_gate, package_dir, files, package_blockers)
    _write_json(manifest_path, package_manifest)

    return {
        **preview,
        "blockers": package_blockers,
        "rule_review": rule_review,
        "upload_gate": upload_gate,
        "materials": materials,
        "package_manifest": package_manifest,
        "package_dir": str(package_dir),
        "files": files,
    }


MTeamUploadCallable = Callable[[dict[str, Any], str, str], Awaitable[dict[str, Any]]]
MTeamDownloadCallable = Callable[[dict[str, Any], str, str], Awaitable[str]]


def build_mteam_upload_preflight(package_dir: str, execute: bool = False, torrent_file: str | None = None, write_payload: bool = False) -> dict[str, Any]:
    package = load_mteam_prepare_package(package_dir)
    gate = package.get("upload_gate", {})
    rule_review = package.get("rule_review", {})
    files = package.get("files", {})
    blockers = list(package.get("blockers", []))
    payload_summary = build_mteam_upload_payload_summary(package, torrent_file=torrent_file, require_materials=execute)
    _extend_unique(blockers, payload_summary["blockers"])

    if not isinstance(gate, dict) or not gate.get("ready"):
        _append_unique(blockers, "MTEAM upload gate is not ready.")
        if isinstance(gate, dict):
            _extend_unique(blockers, _mteam_upload_gate_blockers(gate))
    if not isinstance(rule_review, dict) or rule_review.get("blockers"):
        if isinstance(rule_review, dict):
            _extend_unique(blockers, rule_review.get("blockers", []))
        _append_unique(blockers, "MTEAM rule review has blockers.")
    obligation_review = _mteam_rule_obligation_review(rule_review if isinstance(rule_review, dict) else {})
    if execute:
        _extend_unique(blockers, obligation_review["blockers"])
    if write_payload:
        payload_path = Path(package_dir).expanduser() / "mteam-upload-payload.json"
        _write_json(payload_path, payload_summary)
        files = {**files, "upload_payload": str(payload_path)}

    return {
        "status": "blocked" if blockers else "ready",
        "target_tracker": "MTEAM",
        "dry_run": not execute,
        "package_dir": str(Path(package_dir).expanduser()),
        "content_path": _mteam_package_content_path(package),
        "files": files,
        "upload_gate": gate,
        "rule_review": rule_review if isinstance(rule_review, dict) else {},
        "package_manifest": package.get("package_manifest"),
        "rule_obligation_review": obligation_review,
        "upload_payload": payload_summary,
        "blockers": blockers,
        "next_actions": _upload_preflight_next_actions(blockers, execute),
    }


def _mteam_package_content_path(package: dict[str, Any]) -> str | None:
    preview = package.get("preview")
    if not isinstance(preview, dict):
        return None
    content_path = preview.get("content_path")
    return str(content_path) if content_path else None


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
    torrent_id = extract_mteam_uploaded_torrent_id(upload_result)
    submitted_torrent_hash = _torrent_hash_from_upload_payload(preflight.get("upload_payload"))
    result = {
        **preflight,
        "status": "uploaded",
        "dry_run": False,
        "upload_result": upload_result,
        "uploaded_torrent_id": torrent_id,
        "submitted_torrent_hash": submitted_torrent_hash,
    }
    if download_uploaded:
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


async def download_mteam_uploaded_torrent(config: dict[str, Any], torrent_id: str, output_dir: str) -> dict[str, Any]:
    downloaded_path = await _download_mteam_uploaded_torrent(config, torrent_id, output_dir)
    return {
        "status": "uploaded",
        "uploaded_torrent_id": str(torrent_id),
        "downloaded_torrent": {
            "torrent_id": str(torrent_id),
            "path": downloaded_path,
        },
    }


def extract_mteam_uploaded_torrent_id(upload_result: dict[str, Any]) -> str | None:
    response = upload_result.get("response") if isinstance(upload_result, dict) else None
    return _extract_mteam_torrent_id(response)


def _extract_mteam_torrent_id(response: Any) -> str | None:
    if isinstance(response, int):
        return str(response)
    if isinstance(response, str):
        value = response.strip()
        return value if value.isdigit() else None
    if not isinstance(response, dict):
        return None
    for key in ("id", "torrentId", "torrent_id"):
        value = response.get(key)
        if value:
            return str(value)
    data = response.get("data")
    if data is not response:
        return _extract_mteam_torrent_id(data)
    return None


def build_mteam_upload_payload_summary(package: dict[str, Any], torrent_file: str | None = None, require_materials: bool = False) -> dict[str, Any]:
    field_mapping = package.get("field_mapping", {})
    description_length = int(package.get("description_length", 0) or 0)
    materials = package.get("materials") if isinstance(package.get("materials"), dict) else {}
    form_fields = _mteam_upload_form_fields(field_mapping, description_length, materials=materials)
    description_summary = _mteam_description_summary(package, description_length)
    torrent_summary, torrent_blockers = _torrent_file_summary(torrent_file)
    field_checks = _mteam_upload_field_checks(form_fields)
    material_checks = _mteam_upload_material_checks(description_summary, description_length, materials=materials)
    blockers = [f"{check['name']}: {check['message']}" for check in field_checks if not check["ok"]]
    enforce_materials = require_materials
    blockers.extend(f"{check['name']}: {check['message']}" for check in material_checks if not check["ok"] and (enforce_materials or check["name"].startswith("payload.description_")))
    blockers.extend(torrent_blockers)

    return {
        "endpoint": "https://api.m-team.cc/api/torrent/createOredit",
        "method": "POST",
        "multipart": True,
        "form_fields": form_fields,
        "field_checks": field_checks,
        "material_checks": material_checks,
        "materials_ready_required": enforce_materials,
        "file_field": "file",
        "description_file": description_summary,
        "review": _mteam_upload_review_summary(form_fields, description_summary, materials),
        "torrent_file": torrent_summary,
        "blockers": blockers,
    }


def build_mteam_package_manifest(
    preview: dict[str, Any],
    rule_review: dict[str, Any],
    upload_gate: dict[str, Any],
    package_dir: Path,
    files: dict[str, str],
    blockers: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "ptcli.mteam.prepare_package",
        "target_tracker": "MTEAM",
        "package_dir": str(package_dir),
        "content_path": preview.get("content_path"),
        "source": preview.get("metadata", {}),
        "ready": not blockers,
        "blockers": blockers,
        "manual_review": rule_review.get("manual_review") if isinstance(rule_review, dict) else None,
        "rule_obligations": _manifest_rule_obligations(rule_review),
        "upload_gate": {
            "ready": bool(upload_gate.get("ready")) if isinstance(upload_gate, dict) else False,
            "dupe_count": upload_gate.get("dupe_count") if isinstance(upload_gate, dict) else None,
            "blockers": upload_gate.get("blockers", []) if isinstance(upload_gate, dict) else [],
        },
        "files": {key: _file_artifact(Path(path)) for key, path in files.items() if key != "manifest"},
        "manifest_file": files.get("manifest"),
        "commands": _package_manifest_commands(package_dir, preview.get("content_path")),
        "next_actions": _package_manifest_next_actions(blockers),
    }


def _manifest_rule_obligations(rule_review: dict[str, Any]) -> dict[str, Any]:
    obligations = rule_review.get("rule_obligations") if isinstance(rule_review, dict) else []
    acknowledged = [obligation for obligation in obligations if isinstance(obligation, dict) and obligation.get("acknowledged") is True]
    return {
        "count": len([obligation for obligation in obligations if isinstance(obligation, dict)]),
        "acknowledged_count": len(acknowledged),
        "ready": bool(obligations) and len(acknowledged) == len([obligation for obligation in obligations if isinstance(obligation, dict)]),
        "items": obligations if isinstance(obligations, list) else [],
    }


def _file_artifact(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "is_file": path.is_file(),
    }
    if path.is_file():
        data = path.read_bytes()
        payload.update(
            {
                "size_bytes": len(data),
                "sha1": hashlib.sha1(data).hexdigest(),
            }
        )
    return payload


def _package_manifest_next_actions(blockers: list[str]) -> list[str]:
    if blockers:
        return ["Fix package blockers, regenerate the MTEAM package, then rerun target-upload preflight."]
    return ["Review mteam-package-manifest.json, then run target-upload with the sanitized MTEAM torrent candidate."]


def _package_manifest_commands(package_dir: Path, content_path: Any) -> list[dict[str, str]]:
    package_arg = str(package_dir)
    target_torrent = "./tmp/exported/mteam.mteam-upload.torrent"
    uploaded_save_path = str(content_path or "/downloads")
    return [
        {
            "stage": "target-upload-preflight",
            "command": _ptcli_command(
                [
                    "target-upload",
                    "--package-dir",
                    package_arg,
                    "--torrent-file",
                    target_torrent,
                    "--write-payload",
                    "--json",
                ]
            ),
        },
        {
            "stage": "target-upload-live",
            "command": _ptcli_command(
                [
                    "target-upload",
                    "--package-dir",
                    package_arg,
                    "--torrent-file",
                    target_torrent,
                    "--execute",
                    "--confirm-upload",
                    "--download-uploaded-torrent",
                    "--inject-uploaded-torrent",
                    "--uploaded-save-path",
                    uploaded_save_path,
                    "--uploaded-qbit-category",
                    "MTEAM",
                    "--uploaded-qbit-tags",
                    "retorrent",
                    "--wait-uploaded-complete",
                    "--write-summary",
                    "--json",
                ]
            ),
        },
        {
            "stage": "resume-uploaded-torrent-id",
            "command": _ptcli_command(
                [
                    "target-upload",
                    "--package-dir",
                    package_arg,
                    "--uploaded-torrent-id",
                    "<id>",
                    "--download-uploaded-torrent",
                    "--inject-uploaded-torrent",
                    "--uploaded-save-path",
                    uploaded_save_path,
                    "--uploaded-qbit-category",
                    "MTEAM",
                    "--uploaded-qbit-tags",
                    "retorrent",
                    "--wait-uploaded-complete",
                    "--write-summary",
                    "--json",
                ]
            ),
        },
    ]


def _ptcli_command(args: list[str]) -> str:
    return "python3 ptcli.py " + " ".join(shlex.quote(arg) for arg in args)


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
    materials = _read_json(paths["materials"])
    rule_review = _read_json(paths["rule_review"])
    upload_gate = _read_json(paths["upload_gate"])
    manifest_path = root / MTEAM_PACKAGE_MANIFEST_FILENAME
    package_manifest = _read_json(manifest_path) if manifest_path.exists() else None
    description = paths["description_draft"].read_text(encoding="utf-8")

    blockers: list[str] = []
    if not description.strip():
        blockers.append("MTEAM description draft is empty.")
    if not isinstance(upload_gate, dict):
        blockers.append("MTEAM upload gate file is invalid.")
        upload_gate = {}
    if not isinstance(rule_review, dict):
        blockers.append("MTEAM rule review file is invalid.")
        rule_review = {}
    if not isinstance(field_mapping, dict) or not field_mapping.get("name") or not field_mapping.get("category"):
        blockers.append("MTEAM field mapping is missing name or category.")
    _extend_unique(blockers, _package_manifest_integrity_blockers(package_manifest, paths))

    return {
        "target_tracker": "MTEAM",
        "package_dir": str(root),
        "files": {**{key: str(path) for key, path in paths.items()}, **({"manifest": str(manifest_path)} if manifest_path.exists() else {})},
        "preview": preview,
        "meta_draft": meta_draft,
        "field_mapping": field_mapping,
        "materials": materials,
        "description_length": len(description),
        "rule_review": rule_review,
        "upload_gate": upload_gate,
        "package_manifest": package_manifest,
        "blockers": blockers,
    }


def _package_manifest_integrity_blockers(package_manifest: Any, paths: dict[str, Path]) -> list[str]:
    if package_manifest is None:
        return ["MTEAM package manifest is missing; regenerate the package before upload."]
    if not isinstance(package_manifest, dict):
        return ["MTEAM package manifest is invalid; regenerate the package before upload."]
    manifest_files = package_manifest.get("files")
    if not isinstance(manifest_files, dict):
        return ["MTEAM package manifest has no file integrity records; regenerate the package before upload."]

    blockers: list[str] = []
    for key, path in paths.items():
        expected = manifest_files.get(key)
        if not isinstance(expected, dict):
            blockers.append(f"MTEAM package manifest is missing integrity record for {key}.")
            continue
        actual = _file_artifact(path)
        for field in ("exists", "is_file", "size_bytes", "sha1"):
            if expected.get(field) != actual.get(field):
                blockers.append(f"MTEAM package file integrity mismatch for {key}: {field} changed.")
                break
    return blockers


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
    try:
        async with MTeamApiClient(config) as client:
            search_result = await client.search_by_imdb(f"tt{meta_for_search['imdb']}")
    except MTeamApiError as exc:
        return {"searched": False, "reason": exc.message, "count": 0, "dupes": []}

    dupe_list = _mteam_dupe_entries(search_result)
    return {
        "searched": True,
        "query": {"imdb": f"tt{meta_for_search['imdb']}"},
        "count": len(dupe_list),
        "dupes": dupe_list,
    }


def _mteam_dupe_entries(search_result: Any) -> list[dict[str, Any]]:
    torrents = search_result if isinstance(search_result, list) else (search_result.get("data", []) or search_result.get("torrents", []) if isinstance(search_result, dict) else [])
    if not isinstance(torrents, list):
        return []

    dupes: list[dict[str, Any]] = []
    for torrent in torrents:
        if not isinstance(torrent, dict):
            continue
        name = str(torrent.get("name") or torrent.get("title") or "").strip()
        if not name:
            continue
        torrent_id = torrent.get("id")
        numfiles = torrent.get("numfiles")
        file_count = int(numfiles) if numfiles is not None and str(numfiles).isdigit() else 0
        standard_id = torrent.get("standard")
        source_id = torrent.get("source")
        dupes.append(
            {
                "name": name,
                "size": torrent.get("size"),
                "files": [],
                "file_count": file_count,
                "trumpable": False,
                "link": f"https://kp.m-team.cc/details/{torrent_id}" if torrent_id else None,
                "download": None,
                "flags": list(torrent.get("labelsNew", [])) if isinstance(torrent.get("labelsNew"), list) else [],
                "id": torrent_id,
                "type": MTEAM_SOURCE_ID_TO_TYPE.get(str(source_id).strip(), "") if source_id is not None else _infer_type(name),
                "res": MTEAM_STANDARD_ID_TO_RESOLUTION.get(str(standard_id).strip(), "") if standard_id is not None else _infer_resolution(name),
                "internal": 0,
                "bd_info": None,
                "description": torrent.get("smallDescr"),
            }
        )
    return dupes


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
        "tracker": source_info.get("tracker") if source_info else None,
        "torrent_id": source_info.get("torrent_id") if source_info else None,
        "name": source_info.get("name") if source_info else None,
        "imdb_id": source_info.get("imdb_id") if source_info else None,
        "tmdb_id": source_info.get("tmdb_id") if source_info else None,
        "douban_id": source_info.get("douban_id") if source_info else None,
        "douban_url": source_info.get("douban_url") if source_info else None,
        "torrenthash": source_info.get("torrenthash") if source_info else None,
        "description_length": source_info.get("description_length") if source_info else 0,
        "ptgen_description_length": len(str(source_info.get("ptgen_description") or "")) if source_info else 0,
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


def build_mteam_description_draft(meta_draft: dict[str, Any], source_info: dict[str, Any] | None, materials: dict[str, Any] | None = None) -> str:
    external_link_lines = _mteam_description_external_link_lines(meta_draft)
    ptgen_lines = _mteam_description_ptgen_lines(source_info if isinstance(source_info, dict) else {})
    material_lines = _mteam_description_material_lines(materials if isinstance(materials, dict) else {})
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
        *external_link_lines,
        "",
        "[b]Source evidence[/b]",
        f"Source tracker: {source_info.get('tracker') if source_info else ''}",
        f"Source torrent id: {source_info.get('torrent_id') if source_info else ''}",
        f"Source torrent hash: {source_info.get('torrenthash') if source_info else ''}",
        f"Local content path: {meta_draft.get('content_path') or ''}",
        "",
        *ptgen_lines,
        "",
        *material_lines,
        "",
        "[b]Manual review required[/b]",
        "Confirm source-site and MTEAM rules, transfer permissions, description requirements, screenshots, subtitles, naming, and duplicate status before upload.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _mteam_description_external_link_lines(meta_draft: dict[str, Any]) -> list[str]:
    links = []
    imdb_id = _normalize_imdb_id(meta_draft.get("imdb_id") or meta_draft.get("imdb"))
    if imdb_id:
        links.append(f"IMDb: https://www.imdb.com/title/tt{imdb_id}")
    tmdb_id = meta_draft.get("tmdb_id")
    if tmdb_id:
        tmdb_kind = "tv" if meta_draft.get("category") == "TV" else "movie"
        links.append(f"TMDb: https://www.themoviedb.org/{tmdb_kind}/{tmdb_id}")
    douban_url = meta_draft.get("douban_url")
    if not douban_url and meta_draft.get("douban_id"):
        douban_url = f"https://movie.douban.com/subject/{meta_draft['douban_id']}/"
    if douban_url:
        links.append(f"Douban: {douban_url}")
    return ["[b]External links[/b]", *links] if links else ["[b]External links[/b]", "External links: missing"]


def _mteam_description_ptgen_lines(source_info: dict[str, Any]) -> list[str]:
    ptgen_text = str(source_info.get("ptgen_description") or "").strip()
    if not ptgen_text:
        return ["[b]Movie information[/b]", "PTGen/Douban description: missing"]
    return ["[b]Movie information[/b]", _normalize_ptgen_description_text(ptgen_text)]


def _normalize_ptgen_description_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized


def _mteam_description_material_lines(materials: dict[str, Any]) -> list[str]:
    assets = materials.get("assets") if isinstance(materials.get("assets"), dict) else {}
    lines = ["[b]Media materials[/b]"]
    mediainfo = assets.get("mediainfo") if isinstance(assets.get("mediainfo"), dict) else {}
    bdinfo = assets.get("bdinfo") if isinstance(assets.get("bdinfo"), dict) else {}
    screenshots = assets.get("screenshots") if isinstance(assets.get("screenshots"), dict) else {}
    image_hosts = assets.get("image_hosts") if isinstance(assets.get("image_hosts"), dict) else {}
    lines.append(f"MediaInfo: {'ready' if mediainfo.get('ready') else 'missing'}")
    lines.append(f"BDInfo: {'ready' if bdinfo.get('ready') else 'missing'}")
    lines.append(f"Screenshots: {int(screenshots.get('count', 0) or 0)} local file(s)")
    mediainfo_block = _mteam_description_material_text_block("MediaInfo", mediainfo)
    bdinfo_block = _mteam_description_material_text_block("BDInfo", bdinfo)
    if mediainfo_block:
        lines.extend(["", *mediainfo_block])
    if bdinfo_block:
        lines.extend(["", *bdinfo_block])
    items = image_hosts.get("items") if isinstance(image_hosts.get("items"), list) else []
    if items:
        lines.append("")
        lines.append("[b]Screenshots[/b]")
        for item in items:
            if not isinstance(item, dict):
                continue
            img_url, web_url = _image_host_item_urls(item)
            if img_url and web_url:
                lines.append(f"[url={web_url}][img]{img_url}[/img][/url]")
    else:
        lines.append("Image host uploads: missing")
    return lines


def _mteam_description_material_text_block(label: str, asset: dict[str, Any]) -> list[str]:
    if not asset.get("ready") or not asset.get("path"):
        return []
    text = _read_material_text_excerpt(str(asset["path"]))
    if not text:
        return []
    return [f"[b]{label}[/b]", "[quote]", text, "[/quote]"]


def _read_material_text_excerpt(path: str, limit: int = 20000) -> str:
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit].rstrip()}\n\n[ptcli truncated material text at {limit} characters]"


def build_mteam_materials_manifest(preview: dict[str, Any], source_info: dict[str, Any] | None, content_path: str | None, material_files: dict[str, Any] | None = None) -> dict[str, Any]:
    meta_draft = preview.get("meta_draft") if isinstance(preview.get("meta_draft"), dict) else {}
    material_files = material_files if isinstance(material_files, dict) else {}
    metadata_enrichment = source_info.get("metadata_enrichment") if isinstance(source_info, dict) and isinstance(source_info.get("metadata_enrichment"), dict) else {}
    source_description_length = int(source_info.get("description_length", 0) or 0) if isinstance(source_info, dict) else 0
    mediainfo = _material_file_asset(material_files.get("mediainfo_file"))
    bdinfo = _material_file_asset(material_files.get("bdinfo_file"))
    screenshots = _screenshots_asset(material_files.get("screenshot_files"))
    image_hosts = _image_host_asset(material_files.get("image_host_file"))
    disc_structure = _disc_structure_asset(content_path)
    ptgen_description_length = len(str(source_info.get("ptgen_description") or "")) if isinstance(source_info, dict) else 0
    metadata_checks = [
        _material_check("imdb", bool(meta_draft.get("imdb") or meta_draft.get("imdb_id")), "IMDb id is present.", "IMDb id is missing; fetch or supply IMDb metadata before live upload."),
        _material_check("tmdb", bool(meta_draft.get("tmdb_id")), "TMDb id is present.", "TMDb id is missing; fetch or supply TMDb metadata before live upload."),
        _material_check("douban", bool(meta_draft.get("douban_url") or meta_draft.get("douban_id")), "Douban id/url is present.", "Douban id/url is missing; fetch or supply Douban metadata before live upload."),
        _material_check("ptgen_description", ptgen_description_length > 0, "PTGen/Douban description text is present.", "PTGen/Douban description text is missing; run metadata enrichment with --fetch-ptgen before live upload."),
    ]
    asset_checks = [
        _material_check("mediainfo_or_bdinfo", bool(mediainfo.get("ready") or bdinfo.get("ready")), "MediaInfo/BDInfo is present.", "MediaInfo/BDInfo has not been generated into the package yet."),
        _material_check("bdinfo_for_disc", not disc_structure.get("bdmv") or bool(bdinfo.get("ready")), "BDInfo requirement for disc content is satisfied.", "BDMV disc content requires --bdinfo-file for MTEAM target preparation."),
        _material_check("screenshots", bool(screenshots.get("ready")), "Screenshots are present.", "Screenshots have not been generated into the package yet."),
        _material_check("image_host_uploads", bool(image_hosts.get("ready")), "Image host uploads are present.", "Screenshot image-host upload results are missing usable image URLs."),
        _material_check("description_draft", True, "MTEAM description draft has been generated.", "MTEAM description draft is missing."),
    ]
    all_checks = [*metadata_checks, *asset_checks]
    warnings = [check["message"] for check in [*metadata_checks, *asset_checks] if not check["ok"]]
    return {
        "schema_version": 1,
        "kind": "ptcli.mteam.materials",
        "target_tracker": "MTEAM",
        "content_path": content_path,
        "source": {
            "tracker": source_info.get("tracker") if isinstance(source_info, dict) else None,
            "torrent_id": source_info.get("torrent_id") if isinstance(source_info, dict) else None,
            "name": source_info.get("name") if isinstance(source_info, dict) else None,
            "description_length": source_description_length,
        },
        "metadata": {
            "imdb_id": meta_draft.get("imdb_id"),
            "tmdb_id": meta_draft.get("tmdb_id"),
            "douban_id": meta_draft.get("douban_id"),
            "douban_url": meta_draft.get("douban_url"),
            "source_description_available": source_description_length > 0,
            "ptgen_description_length": ptgen_description_length,
            "enrichment_status": metadata_enrichment.get("status"),
            "enrichment_ready": metadata_enrichment.get("ready") if isinstance(metadata_enrichment.get("ready"), bool) else None,
            "sources": metadata_enrichment.get("sources") if isinstance(metadata_enrichment.get("sources"), list) else [],
            "applied": metadata_enrichment.get("applied") if isinstance(metadata_enrichment.get("applied"), dict) else {},
            "readiness": metadata_enrichment.get("readiness") if isinstance(metadata_enrichment.get("readiness"), dict) else {},
            "field_evidence": metadata_enrichment.get("field_evidence") if isinstance(metadata_enrichment.get("field_evidence"), dict) else {},
            "missing": metadata_enrichment.get("missing") if isinstance(metadata_enrichment.get("missing"), list) else [],
            "blockers": metadata_enrichment.get("blockers") if isinstance(metadata_enrichment.get("blockers"), list) else [],
            "readiness_blockers": metadata_enrichment.get("readiness_blockers") if isinstance(metadata_enrichment.get("readiness_blockers"), list) else [],
        },
        "assets": {
            "mediainfo": mediainfo,
            "bdinfo": bdinfo,
            "screenshots": screenshots,
            "image_hosts": image_hosts,
            "disc_structure": disc_structure,
            "description": {"ready": True, "path": REQUIRED_MTEAM_PACKAGE_FILES["description_draft"]},
        },
        "checks": {
            "metadata": metadata_checks,
            "assets": asset_checks,
        },
        "ready": all(check["ok"] for check in all_checks),
        "missing": _mteam_material_missing(all_checks),
        "warnings": warnings,
        "next_actions": _mteam_material_next_actions(all_checks),
    }


def _material_check(name: str, ok: bool, ok_message: str, missing_message: str) -> dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "message": ok_message if ok else missing_message,
    }


def _mteam_material_missing(checks: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for check in checks:
        if check.get("ok"):
            continue
        name = str(check.get("name") or "")
        if name in {"imdb", "tmdb", "douban", "ptgen_description"}:
            missing.append(f"metadata.{name}")
        elif name == "description_draft":
            missing.append("description.content")
        elif name:
            missing.append(f"assets.{name}")
    return missing


def _material_file_asset(path_value: Any) -> dict[str, Any]:
    if not path_value:
        return {"ready": False, "path": None}
    artifact = _file_artifact(Path(str(path_value)).expanduser())
    return {**artifact, "ready": bool(artifact.get("is_file") and int(artifact.get("size_bytes", 0) or 0) > 0)}


def _screenshots_asset(paths_value: Any) -> dict[str, Any]:
    paths = paths_value if isinstance(paths_value, list) else []
    files = [_material_file_asset(path) for path in paths if path]
    return {
        "ready": bool(files) and all(file.get("ready") for file in files),
        "count": len(files),
        "files": files,
    }


def _image_host_asset(path_value: Any) -> dict[str, Any]:
    artifact = _material_file_asset(path_value)
    items: list[Any] = []
    if artifact.get("ready") and artifact.get("path"):
        try:
            payload = json.loads(Path(str(artifact["path"])).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            raw_items = payload.get("items") or payload.get("images") or payload.get("uploaded_images")
            if isinstance(raw_items, list):
                items = raw_items
    valid_items = [item for item in items if isinstance(item, dict) and all(_image_host_item_urls(item))]
    return {
        **artifact,
        "ready": bool(artifact.get("ready") and items and len(valid_items) == len(items)),
        "count": len(items),
        "valid_count": len(valid_items),
        "invalid_count": len(items) - len(valid_items),
        "items": items,
    }


def _image_host_item_urls(item: dict[str, Any]) -> tuple[str, str]:
    raw_url = _usable_web_url(item.get("raw_url") or item.get("url"))
    img_url = _usable_web_url(item.get("img_url")) or raw_url
    web_url = _usable_web_url(item.get("web_url")) or raw_url or img_url
    return img_url, web_url


def _usable_web_url(value: Any) -> str:
    text = str(value or "").strip()
    return text if re.match(r"^https?://", text, re.IGNORECASE) else ""


def _disc_structure_asset(content_path: str | None) -> dict[str, Any]:
    if not content_path:
        return {"ready": False, "path": None, "bdmv": False, "type": None}
    path = Path(content_path).expanduser()
    if not path.exists():
        return {"ready": False, "path": str(path), "bdmv": False, "type": None}
    if path.is_dir() and path.name.upper() == "BDMV":
        return {"ready": True, "path": str(path), "bdmv": True, "type": "BDMV"}
    bdmv_dir = path / "BDMV" if path.is_dir() else None
    if bdmv_dir and bdmv_dir.is_dir():
        return {"ready": True, "path": str(bdmv_dir), "bdmv": True, "type": "BDMV"}
    video_ts_dir = path / "VIDEO_TS" if path.is_dir() else None
    if video_ts_dir and video_ts_dir.is_dir():
        return {"ready": True, "path": str(video_ts_dir), "bdmv": False, "type": "VIDEO_TS"}
    return {"ready": False, "path": str(path), "bdmv": False, "type": None}


def _mteam_material_next_actions(checks: list[dict[str, Any]]) -> list[str]:
    missing = {str(check.get("name")) for check in checks if isinstance(check, dict) and not check.get("ok")}
    actions = []
    if missing.intersection({"imdb", "tmdb", "douban", "ptgen_description"}):
        actions.append("Fetch or supply IMDb/TMDb/Douban metadata before live upload.")
    if "mediainfo_or_bdinfo" in missing:
        actions.append("Generate MediaInfo or BDInfo and add it to the MTEAM package.")
    if "bdinfo_for_disc" in missing:
        actions.append("Provide a BDInfo text file with --bdinfo-file for BDMV disc content before live upload.")
    if "screenshots" in missing:
        actions.append("Generate video screenshots for the completed local content.")
    if "image_host_uploads" in missing:
        actions.append("Upload screenshots to the configured image host and record the links.")
    return actions or ["Review generated MTEAM materials before target-upload."]


def build_mteam_rule_review(stages: list[dict[str, Any]], accept_rules: bool) -> dict[str, Any]:
    rule_stage = _find_stage(stages, "rule-check")
    rule_result = rule_stage.get("result", {}) if rule_stage else {}
    rule_checks = rule_result.get("checks") if isinstance(rule_result, dict) else []
    rule_obligations = rule_result.get("rule_obligations") if isinstance(rule_result, dict) else []
    failed_checks = [check for check in rule_checks if isinstance(check, dict) and not check.get("ok")] if isinstance(rule_checks, list) else []
    blockers = [f"{check.get('name', 'rule_check')}: {check.get('message', 'Executable rule check did not pass.')}" for check in failed_checks]
    if not accept_rules:
        blockers.append("rules_acknowledged: Rules must be manually reviewed and acknowledged.")
    return {
        "target_tracker": "MTEAM",
        "rules_acknowledged": accept_rules,
        "rule_check_ready": bool(rule_result.get("ready")) if isinstance(rule_result, dict) else False,
        "source_tracker": rule_result.get("source_tracker") if isinstance(rule_result, dict) else None,
        "target_trackers": rule_result.get("target_trackers") if isinstance(rule_result, dict) else [],
        "rule_profiles": rule_result.get("rule_profiles") if isinstance(rule_result, dict) else [],
        "rule_obligations": rule_obligations if isinstance(rule_obligations, list) else [],
        "manual_review": _manual_rule_review_summary(rule_result, rule_obligations, accept_rules),
        "checks": rule_checks if isinstance(rule_checks, list) else [],
        "blockers": blockers,
    }


def _manual_rule_review_summary(rule_result: dict[str, Any], rule_obligations: Any, accept_rules: bool) -> dict[str, Any]:
    obligations = rule_obligations if isinstance(rule_obligations, list) else []
    return {
        "required": True,
        "acknowledged": accept_rules,
        "source_tracker": rule_result.get("source_tracker"),
        "target_trackers": rule_result.get("target_trackers") if isinstance(rule_result.get("target_trackers"), list) else [],
        "obligation_count": len([obligation for obligation in obligations if isinstance(obligation, dict)]),
        "rules_urls": sorted({str(obligation.get("rules_url")) for obligation in obligations if isinstance(obligation, dict) and obligation.get("rules_url")}),
        "required_confirmations": _manual_rule_review_confirmations(obligations),
        "acknowledgement_evidence": [obligation["acknowledgement_evidence"] for obligation in obligations if isinstance(obligation, dict) and isinstance(obligation.get("acknowledgement_evidence"), dict)],
        "review_fingerprints": [str(obligation["review_fingerprint"]) for obligation in obligations if isinstance(obligation, dict) and obligation.get("review_fingerprint")],
        "site_specific_rules_encoded": False,
        "message": "Manual source/target rule review has been acknowledged." if accept_rules else "Manual source/target rule review is required before live upload.",
    }


def _manual_rule_review_confirmations(obligations: list[Any]) -> list[dict[str, Any]]:
    confirmations = []
    for obligation in obligations:
        if not isinstance(obligation, dict):
            continue
        review_scope = obligation.get("review_scope")
        if not isinstance(review_scope, dict):
            continue
        required_confirmations = review_scope.get("required_confirmations")
        confirmations.append(
            {
                "tracker": obligation.get("tracker"),
                "role": obligation.get("role"),
                "action": obligation.get("action"),
                "rules_url": obligation.get("rules_url"),
                "review_fingerprint": obligation.get("review_fingerprint"),
                "required_confirmations": required_confirmations if isinstance(required_confirmations, list) else [],
            }
        )
    return confirmations


def build_mteam_upload_gate(preview: dict[str, Any], stages: list[dict[str, Any]], accept_rules: bool) -> dict[str, Any]:
    dupe_stage = _find_stage(stages, "target-dupe-check")
    dupe_result = dupe_stage.get("result", {}) if dupe_stage else {}
    dupe_count = int(dupe_result.get("count", 0) or 0) if isinstance(dupe_result, dict) else 0
    dupe_searched = bool(dupe_result.get("searched")) if isinstance(dupe_result, dict) else False
    rule_stage = _find_stage(stages, "rule-check")
    rule_result = rule_stage.get("result", {}) if rule_stage else {}
    rule_ready = bool(rule_result.get("ready")) if isinstance(rule_result, dict) else False
    checks = [
        {
            "name": "rules_acknowledged",
            "ok": accept_rules,
            "message": "Rules have been acknowledged." if accept_rules else "Rules must be manually reviewed and acknowledged.",
        },
        {
            "name": "executable_rule_check",
            "ok": rule_ready,
            "message": _rule_gate_message(rule_stage, rule_ready),
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
    blockers = _mteam_upload_gate_blockers({"checks": checks})
    return {
        "target_tracker": "MTEAM",
        "ready": all(check["ok"] for check in checks),
        "checks": checks,
        "dupe_count": dupe_count,
        "blockers": blockers,
    }


def build_mteam_meta_draft(source_info: dict[str, Any] | None, content_path: str | None) -> dict[str, Any]:
    name = str(source_info.get("name") or "").strip() if source_info else ""
    resolution = _infer_resolution(name)
    media_type = _infer_type(name)
    category = _infer_category(name)
    imdb_id = _normalize_imdb_id(source_info.get("imdb_id")) if source_info else None
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
        "ptgen_description_length": len(str(source_info.get("ptgen_description") or "")) if source_info else 0,
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
    source_content_verify = _find_stage(stages, "source-content-verify")
    if source_content_verify and not source_content_verify.get("ok"):
        return False
    for stage in stages:
        stage_name = stage.get("stage")
        result = stage.get("result", {})
        if stage_name == "wait-complete" and stage.get("ok") and isinstance(result, dict) and result.get("complete") and _has_match_evidence(result.get("matches")):
            return True
        if stage_name == "match" and stage.get("ok") and isinstance(result, dict) and _has_match_evidence(result.get("matches")):
            return True
    return False


def _has_match_evidence(matches: Any) -> bool:
    if not isinstance(matches, list):
        return False
    return any(_match_has_evidence(match) for match in matches)


def _match_has_evidence(match: Any) -> bool:
    if not isinstance(match, dict):
        return False
    return bool(match.get("hash") or match.get("torrent_hash") or match.get("torrenthash") or match.get("content_path"))


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


def _mteam_upload_gate_blockers(gate: dict[str, Any]) -> list[str]:
    checks = gate.get("checks")
    if not isinstance(checks, list):
        return ["upload_gate: MTEAM upload gate has no check details."]
    return [
        f"{check.get('name', 'upload_gate')}: {check.get('message', 'MTEAM upload gate check failed.')}"
        for check in checks
        if isinstance(check, dict) and not check.get("ok")
    ]


def _mteam_prepare_package_blockers(preview: dict[str, Any], rule_review: dict[str, Any], upload_gate: dict[str, Any]) -> list[str]:
    blockers = list(preview.get("blockers", []))
    _extend_unique(blockers, rule_review.get("blockers", []))
    _extend_unique(blockers, upload_gate.get("blockers", []))
    return blockers


def _mteam_rule_obligation_review(rule_review: dict[str, Any]) -> dict[str, Any]:
    obligations = rule_review.get("rule_obligations")
    checks = [
        _rule_obligation_check(
            "source_download_and_retorrent_rules",
            obligations,
            role="source",
            action="download_and_retorrent",
            tracker=str(rule_review.get("source_tracker") or ""),
        ),
        _rule_obligation_check(
            "mteam_upload_and_seed_rules",
            obligations,
            role="target",
            action="upload_and_seed",
            tracker="MTEAM",
        ),
    ]
    blockers = [
        f"{check['name']}: {check['message']}"
        for check in checks
        if not check["ok"]
    ]
    return {
        "ready": not blockers,
        "checks": checks,
        "blockers": blockers,
    }


def _rule_obligation_check(name: str, obligations: Any, *, role: str, action: str, tracker: str) -> dict[str, Any]:
    if not isinstance(obligations, list):
        return {"name": name, "ok": False, "message": "Rule obligations are missing from the MTEAM rule review package."}
    if not any(isinstance(obligation, dict) for obligation in obligations):
        return {"name": name, "ok": False, "message": "Rule obligations are missing from the MTEAM rule review package."}
    matching = [
        obligation
        for obligation in obligations
        if isinstance(obligation, dict)
        and obligation.get("role") == role
        and obligation.get("action") == action
        and (not tracker or obligation.get("tracker") == tracker)
    ]
    if not matching:
        tracker_label = tracker or role
        return {"name": name, "ok": False, "message": f"No acknowledged {tracker_label} {role} {action} rule obligation is present."}
    if not all(obligation.get("rules_url") for obligation in matching):
        return {"name": name, "ok": False, "message": f"{role} {action} rule obligation is missing a rules URL."}
    if not all(obligation.get("acknowledged") is True for obligation in matching):
        return {"name": name, "ok": False, "message": f"{role} {action} rule obligation has not been acknowledged."}
    if not all(_rule_obligation_has_review_scope(obligation) for obligation in matching):
        return {"name": name, "ok": False, "message": f"{role} {action} rule obligation is missing a concrete manual review scope."}
    return {"name": name, "ok": True, "message": f"{role} {action} rule obligation is acknowledged."}


def _rule_obligation_has_review_scope(obligation: dict[str, Any]) -> bool:
    review_scope = obligation.get("review_scope")
    if not isinstance(review_scope, dict):
        return False
    confirmations = review_scope.get("required_confirmations")
    return bool(review_scope.get("rules_url")) and isinstance(confirmations, list) and bool(confirmations)


def _extend_unique(items: list[str], additions: Any) -> None:
    if not isinstance(additions, list):
        return
    for item in additions:
        if isinstance(item, str):
            _append_unique(items, item)


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _rule_gate_message(rule_stage: dict[str, Any] | None, ready: bool) -> str:
    if not rule_stage or rule_stage.get("skipped"):
        return "Executable rule check has not been run."
    if not rule_stage.get("ok") or not ready:
        return "Executable rule check did not pass."
    return "Executable rule check passed."


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
    materials = package.get("materials") if isinstance(package.get("materials"), dict) else {}
    data = _mteam_upload_form_fields(package["field_mapping"], len(description), materials=materials)
    data["descr"] = description
    mediainfo_text = await asyncio.to_thread(_mteam_material_mediainfo_text, materials)
    if mediainfo_text:
        data["mediainfo"] = mediainfo_text
    files = {
        "file": (torrent_path.name, torrent_bytes, "application/x-bittorrent"),
    }

    async with MTeamApiClient(config) as client:
        response = await client.upload_torrent(data, files)

    return {
        "submitted": True,
        "response": _summarize_mteam_upload_response(response),
    }


async def _download_mteam_uploaded_torrent(config: dict[str, Any], torrent_id: str, output_dir: str) -> str:
    destination = await asyncio.to_thread(_uploaded_torrent_destination, output_dir, torrent_id)
    async with MTeamApiClient(config) as client:
        await client.download_torrent(torrent_id, destination)
    await asyncio.to_thread(_assert_torrent_file, destination)
    return str(destination)


def _mteam_upload_form_fields(field_mapping: dict[str, Any], description_length: int, materials: dict[str, Any] | None = None) -> dict[str, Any]:
    mediainfo_length = _mteam_material_mediainfo_length(materials if isinstance(materials, dict) else {})
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
    if mediainfo_length > 0:
        form_fields["mediainfo"] = {"source": _mteam_material_mediainfo_source(materials if isinstance(materials, dict) else {}), "length": mediainfo_length}
    for optional_field in ("imdb", "douban"):
        if field_mapping.get(optional_field):
            form_fields[optional_field] = field_mapping[optional_field]
    return form_fields


def _mteam_upload_field_checks(form_fields: dict[str, Any]) -> list[dict[str, Any]]:
    checks = [
        _payload_field_check("payload.name", _non_empty_string(form_fields.get("name")), "MTEAM upload payload name is present.", "MTEAM upload payload is missing required field: name."),
        _payload_field_check("payload.smallDescr", _non_empty_string(form_fields.get("smallDescr")), "MTEAM upload payload smallDescr is present.", "MTEAM upload payload is missing required field: smallDescr."),
        _payload_field_check("payload.descr", bool(form_fields.get("descr")), "MTEAM upload payload description is present.", "MTEAM upload payload is missing required field: descr."),
        _payload_field_check("payload.category", _mteam_category_payload_value_ok(form_fields.get("category")), "MTEAM upload payload category is a known MTEAM category id.", "MTEAM upload payload category is missing or unknown."),
        _payload_field_check("payload.standard", _mteam_standard_payload_value_ok(form_fields.get("standard")), "MTEAM upload payload standard is a known MTEAM resolution id.", "MTEAM upload payload standard is missing or unknown."),
        _payload_field_check("payload.anonymous", isinstance(form_fields.get("anonymous"), bool), "MTEAM upload payload anonymous flag is boolean.", "MTEAM upload payload anonymous flag must be boolean."),
    ]
    if form_fields.get("imdb"):
        imdb_url = str(form_fields["imdb"])
        checks.append(_payload_field_check("payload.imdb", bool(re.match(r"^https://www\.imdb\.com/title/tt\d+/?$", imdb_url)), "MTEAM upload payload IMDb URL is valid.", "MTEAM upload payload IMDb URL must look like https://www.imdb.com/title/tt1234567."))
    if form_fields.get("douban"):
        douban_url = str(form_fields["douban"])
        checks.append(_payload_field_check("payload.douban", bool(re.match(r"^https://movie\.douban\.com/subject/\d+/?$", douban_url)), "MTEAM upload payload Douban URL is valid.", "MTEAM upload payload Douban URL must look like https://movie.douban.com/subject/1234567/."))
    return checks


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _mteam_category_payload_value_ok(value: Any) -> bool:
    return value in {401, 402, 403, 419, 439}


def _mteam_standard_payload_value_ok(value: Any) -> bool:
    return value in {1, 2, 3, 5, 6, 7}


def _normalize_imdb_id(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"(?:tt)?(\d+)", str(value), flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _mteam_description_summary(package: dict[str, Any], expected_length: int) -> dict[str, Any]:
    files = package.get("files")
    description_path = files.get("description_draft") if isinstance(files, dict) else None
    if not description_path:
        return {
            "source": "mteam-description-draft.txt",
            "expected_length": expected_length,
            "exists": False,
            "blockers": ["MTEAM description draft path is missing from package files."],
        }

    path = Path(str(description_path)).expanduser()
    summary = {
        **_file_artifact(path),
        "source": "mteam-description-draft.txt",
        "expected_length": expected_length,
        "char_length": None,
        "blockers": [],
    }
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            summary["blockers"] = [f"MTEAM description draft is not valid UTF-8: {exc}"]
        else:
            summary["char_length"] = len(text)
            summary["content"] = _mteam_description_content_summary(text)
    return summary


def _mteam_description_content_summary(text: str) -> dict[str, Any]:
    image_urls = _mteam_description_image_urls(text)
    external_links = _mteam_description_external_links(text)
    external_id_readiness = {
        "imdb": bool(re.search(r"imdb\.com/title/tt\d+|^\[b\]IMDb\[/b\]:\s*\d+", text, flags=re.IGNORECASE | re.MULTILINE)),
        "tmdb": bool(re.search(r"themoviedb\.org/(?:movie|tv)/\d+|^\[b\]TMDb\[/b\]:\s*\d+", text, flags=re.IGNORECASE | re.MULTILINE)),
        "douban": bool(re.search(r"movie\.douban\.com/subject/\d+|^\[b\]Douban\[/b\]:\s*\d+", text, flags=re.IGNORECASE | re.MULTILINE)),
    }
    return {
        "has_ptgen_description": "[b]Movie information[/b]" in text and "PTGen/Douban description: missing" not in text,
        "has_screenshot_bbcode": "[b]Screenshots[/b]" in text and bool(image_urls),
        "bbcode_image_count": len(image_urls),
        "bbcode_image_urls": image_urls,
        "has_mediainfo_or_bdinfo": "[b]MediaInfo[/b]" in text or "[b]BDInfo[/b]" in text,
        "has_imdb": external_id_readiness["imdb"],
        "has_tmdb": external_id_readiness["tmdb"],
        "has_douban": external_id_readiness["douban"],
        "external_id_readiness": external_id_readiness,
        "external_id_missing": [name for name, ready in external_id_readiness.items() if not ready],
        "external_links": external_links,
    }


def _mteam_description_image_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"\[img(?:=[^\]]+)?\](.*?)\[/img\]", text, flags=re.IGNORECASE | re.DOTALL):
        url = match.group(1).strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _mteam_description_external_links(text: str) -> dict[str, str | None]:
    return {
        "imdb": _first_regex_match(r"https?://(?:www\.)?imdb\.com/title/tt\d+/?", text),
        "tmdb": _first_regex_match(r"https?://(?:www\.)?themoviedb\.org/(?:movie|tv)/\d+/?", text),
        "douban": _first_regex_match(r"https?://movie\.douban\.com/subject/\d+/?", text),
    }


def _first_regex_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(0) if match else None


def _mteam_upload_review_summary(form_fields: dict[str, Any], description_summary: dict[str, Any], materials: dict[str, Any]) -> dict[str, Any]:
    content = description_summary.get("content") if isinstance(description_summary.get("content"), dict) else {}
    metadata = materials.get("metadata") if isinstance(materials.get("metadata"), dict) else {}
    assets = materials.get("assets") if isinstance(materials.get("assets"), dict) else {}
    screenshots = assets.get("screenshots") if isinstance(assets.get("screenshots"), dict) else {}
    image_hosts = assets.get("image_hosts") if isinstance(assets.get("image_hosts"), dict) else {}
    expected_image_urls = _mteam_expected_image_urls(materials)
    description_image_urls = _mteam_description_image_urls_from_content(content)
    missing_image_urls = [url for url in expected_image_urls if url not in description_image_urls]
    screenshot_coverage = {
        "ready": not missing_image_urls,
        "expected_urls": expected_image_urls,
        "description_urls": description_image_urls,
        "missing_urls": missing_image_urls,
    }
    media_info_source = _mteam_material_mediainfo_source(materials)
    media_info_length = _mteam_material_mediainfo_length(materials)
    return {
        "description": {
            "path": description_summary.get("path"),
            "char_length": description_summary.get("char_length"),
            "external_links": content.get("external_links") if isinstance(content.get("external_links"), dict) else {},
            "external_id_readiness": content.get("external_id_readiness") if isinstance(content.get("external_id_readiness"), dict) else {},
            "external_id_missing": content.get("external_id_missing") if isinstance(content.get("external_id_missing"), list) else [],
            "has_ptgen_description": bool(content.get("has_ptgen_description")),
            "ptgen_description_length": metadata.get("ptgen_description_length"),
            "has_mediainfo_or_bdinfo": bool(content.get("has_mediainfo_or_bdinfo")),
            "has_screenshot_bbcode": bool(content.get("has_screenshot_bbcode")),
            "bbcode_image_count": int(content.get("bbcode_image_count", 0) or 0),
            "bbcode_image_urls": description_image_urls,
            "screenshot_coverage": screenshot_coverage,
            "evidence": _mteam_description_evidence_summary(
                content,
                metadata,
                media_info_source=media_info_source,
                media_info_length=media_info_length,
                screenshot_coverage=screenshot_coverage,
                image_host_count=int(image_hosts.get("count", 0) or 0),
                image_host_urls=expected_image_urls,
            ),
        },
        "materials": {
            "mediainfo_or_bdinfo_source": media_info_source,
            "mediainfo_or_bdinfo_length": media_info_length,
            "screenshot_file_count": int(screenshots.get("count", 0) or 0),
            "image_host_count": int(image_hosts.get("count", 0) or 0),
            "image_host_urls": expected_image_urls,
        },
        "form": {
            "name": form_fields.get("name"),
            "smallDescr": form_fields.get("smallDescr"),
            "category": form_fields.get("category"),
            "standard": form_fields.get("standard"),
            "imdb": form_fields.get("imdb"),
            "douban": form_fields.get("douban"),
        },
    }


def _mteam_description_evidence_summary(
    content: dict[str, Any],
    metadata: dict[str, Any],
    *,
    media_info_source: str | None,
    media_info_length: int,
    screenshot_coverage: dict[str, Any],
    image_host_count: int,
    image_host_urls: list[str],
) -> dict[str, Any]:
    external_id_readiness = content.get("external_id_readiness") if isinstance(content.get("external_id_readiness"), dict) else {}
    return {
        "ptgen_description": {
            "ready": bool(content.get("has_ptgen_description")),
            "length": metadata.get("ptgen_description_length"),
            "source": "metadata.ptgen_description" if content.get("has_ptgen_description") else None,
        },
        "external_ids": {
            "ready": all(external_id_readiness.get(name) is True for name in ("imdb", "tmdb", "douban")),
            "readiness": external_id_readiness,
            "missing": [name for name in ("imdb", "tmdb", "douban") if external_id_readiness.get(name) is not True],
            "links": content.get("external_links") if isinstance(content.get("external_links"), dict) else {},
        },
        "mediainfo_or_bdinfo": {
            "ready": bool(content.get("has_mediainfo_or_bdinfo")),
            "source": media_info_source,
            "length": media_info_length,
        },
        "screenshots": {
            "ready": bool(content.get("has_screenshot_bbcode")),
            "bbcode_image_count": int(content.get("bbcode_image_count", 0) or 0),
            "bbcode_image_urls": _mteam_description_image_urls_from_content(content),
        },
        "image_host": {
            "count": image_host_count,
            "urls": image_host_urls,
        },
        "screenshot_coverage": {
            "ready": screenshot_coverage.get("ready"),
            "expected_count": len(screenshot_coverage.get("expected_urls") if isinstance(screenshot_coverage.get("expected_urls"), list) else []),
            "description_count": len(screenshot_coverage.get("description_urls") if isinstance(screenshot_coverage.get("description_urls"), list) else []),
            "missing_count": len(screenshot_coverage.get("missing_urls") if isinstance(screenshot_coverage.get("missing_urls"), list) else []),
            "expected_urls": screenshot_coverage.get("expected_urls") if isinstance(screenshot_coverage.get("expected_urls"), list) else [],
            "description_urls": screenshot_coverage.get("description_urls") if isinstance(screenshot_coverage.get("description_urls"), list) else [],
            "missing_urls": screenshot_coverage.get("missing_urls") if isinstance(screenshot_coverage.get("missing_urls"), list) else [],
        },
    }


def _mteam_upload_material_checks(description_summary: dict[str, Any], expected_length: int, materials: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    exists = bool(description_summary.get("exists")) and bool(description_summary.get("is_file"))
    char_length = description_summary.get("char_length")
    blockers = description_summary.get("blockers") if isinstance(description_summary.get("blockers"), list) else []
    material_checks = [
        _payload_field_check(
            "payload.description_file",
            exists and not blockers,
            "MTEAM description draft file exists and is readable.",
            blockers[0] if blockers else "MTEAM description draft file is missing or not a file.",
        ),
        _payload_field_check(
            "payload.description_length",
            isinstance(char_length, int) and char_length > 0 and char_length == expected_length,
            "MTEAM description draft length matches the upload payload.",
            f"MTEAM description draft length mismatch: expected {expected_length}, got {char_length}.",
        ),
    ]
    content = description_summary.get("content") if isinstance(description_summary.get("content"), dict) else {}
    expected_image_urls = _mteam_expected_image_urls(materials)
    description_image_urls = _mteam_description_image_urls_from_content(content)
    missing_image_urls = [url for url in expected_image_urls if url not in description_image_urls]
    screenshot_coverage_check = _payload_field_check(
        "materials.description.screenshot_coverage",
        not missing_image_urls,
        "MTEAM description references every image-host screenshot URL.",
        "MTEAM description is missing one or more image-host screenshot URLs.",
    )
    screenshot_coverage_check.update(
        {
            "expected_urls": expected_image_urls,
            "description_urls": description_image_urls,
            "missing_urls": missing_image_urls,
        }
    )
    material_checks.extend(
        [
            _payload_field_check(
                "materials.description.ptgen_description",
                bool(content.get("has_ptgen_description")),
                "MTEAM description includes PTGen/Douban description text.",
                "MTEAM description is missing PTGen/Douban description text.",
            ),
            _payload_field_check(
                "materials.description.external_ids",
                bool(content.get("has_imdb")) and bool(content.get("has_tmdb")) and bool(content.get("has_douban")),
                "MTEAM description includes IMDb, TMDb, and Douban references.",
                "MTEAM description is missing one or more IMDb/TMDb/Douban references.",
            ),
            _payload_field_check(
                "materials.description.external_ids.imdb",
                bool(content.get("has_imdb")),
                "MTEAM description includes an IMDb reference.",
                "MTEAM description is missing an IMDb reference.",
            ),
            _payload_field_check(
                "materials.description.external_ids.tmdb",
                bool(content.get("has_tmdb")),
                "MTEAM description includes a TMDb reference.",
                "MTEAM description is missing a TMDb reference.",
            ),
            _payload_field_check(
                "materials.description.external_ids.douban",
                bool(content.get("has_douban")),
                "MTEAM description includes a Douban reference.",
                "MTEAM description is missing a Douban reference.",
            ),
            _payload_field_check(
                "materials.description.mediainfo_or_bdinfo",
                bool(content.get("has_mediainfo_or_bdinfo")),
                "MTEAM description includes MediaInfo/BDInfo excerpt.",
                "MTEAM description is missing a MediaInfo/BDInfo excerpt.",
            ),
            _payload_field_check(
                "materials.description.screenshot_bbcode",
                bool(content.get("has_screenshot_bbcode")),
                "MTEAM description includes screenshot BBCode from image-host uploads.",
                "MTEAM description is missing screenshot BBCode from image-host uploads.",
            ),
            screenshot_coverage_check,
        ]
    )
    materials = materials if isinstance(materials, dict) else {}
    checks = materials.get("checks") if isinstance(materials.get("checks"), dict) else {}
    for scope in ("metadata", "assets"):
        for check in checks.get(scope, []) if isinstance(checks.get(scope), list) else []:
            if isinstance(check, dict):
                name = str(check.get("name") or "")
                material_checks.append(
                    _payload_field_check(
                        f"materials.{scope}.{name}",
                        bool(check.get("ok")),
                        str(check.get("message") or f"{name} is ready."),
                        str(check.get("message") or f"{name} is not ready."),
                    )
                )
    return material_checks


def _mteam_expected_image_urls(materials: dict[str, Any] | None) -> list[str]:
    materials = materials if isinstance(materials, dict) else {}
    assets = materials.get("assets") if isinstance(materials.get("assets"), dict) else {}
    image_hosts = assets.get("image_hosts") if isinstance(assets.get("image_hosts"), dict) else {}
    items = image_hosts.get("items") if isinstance(image_hosts.get("items"), list) else []
    urls: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("img_url") or item.get("raw_url") or item.get("url") or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _mteam_description_image_urls_from_content(content: dict[str, Any]) -> list[str]:
    return [url for url in _target_string_list(content.get("bbcode_image_urls")) if url]


def _target_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _mteam_material_mediainfo_source(materials: dict[str, Any]) -> str | None:
    assets = materials.get("assets") if isinstance(materials.get("assets"), dict) else {}
    for key in ("bdinfo", "mediainfo"):
        asset = assets.get(key) if isinstance(assets.get(key), dict) else {}
        if asset.get("ready") and asset.get("path"):
            return str(asset["path"])
    return None


def _mteam_material_mediainfo_length(materials: dict[str, Any]) -> int:
    text = _mteam_material_mediainfo_text(materials)
    return len(text)


def _mteam_material_mediainfo_text(materials: dict[str, Any]) -> str:
    source = _mteam_material_mediainfo_source(materials)
    if not source:
        return ""
    path = Path(source).expanduser()
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _payload_field_check(name: str, ok: bool, ok_message: str, failed_message: str) -> dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "message": ok_message if ok else failed_message,
    }


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
    summary = {
        "path": str(path),
        "filename": path.name,
        "size": len(data),
        "sha1": hashlib.sha1(data).hexdigest(),
    }
    summary.update(_torrent_metadata_summary(path))
    if not summary.get("metadata_readable"):
        blockers.append("MTEAM upload torrent metadata is not readable.")
    elif not summary.get("mteam_safe"):
        blockers.append("MTEAM upload torrent is not a cleaned MTEAM upload candidate; run pipeline with --sanitize-target-torrent or use retorrent default sanitizing.")
    return summary, blockers


def _torrent_metadata_summary(path: Path) -> dict[str, Any]:
    try:
        torrent = Torrent.read(str(path), validate=False)
    except Exception as exc:
        return {
            "metadata_readable": False,
            "metadata_error": str(exc),
            "mteam_safe": False,
        }
    announce = torrent.metainfo.get("announce")
    comment = torrent.metainfo.get("comment")
    source_flag = torrent.metainfo.get("info", {}).get("source")
    extra_top_level_fields = sorted(str(key) for key in torrent.metainfo if key not in MTEAM_UPLOAD_TORRENT_ALLOWED_TOP_LEVEL_FIELDS)
    announce_list_present = "announce-list" in torrent.metainfo
    mteam_safe = announce == MTEAM_UPLOAD_ANNOUNCE and source_flag == MTEAM_SOURCE_FLAG and not comment and not extra_top_level_fields and not announce_list_present
    return {
        "metadata_readable": True,
        "torrent_hash": str(torrent.infohash),
        "infohash": str(torrent.infohash),
        "announce": announce,
        "comment_length": len(str(comment or "")),
        "source_flag": source_flag,
        "extra_top_level_fields": extra_top_level_fields,
        "announce_list_present": announce_list_present,
        "mteam_safe": mteam_safe,
    }


def _torrent_hash_from_upload_payload(upload_payload: Any) -> str | None:
    if not isinstance(upload_payload, dict):
        return None
    torrent_file = upload_payload.get("torrent_file")
    if not isinstance(torrent_file, dict):
        return None
    torrent_hash = torrent_file.get("torrent_hash") or torrent_file.get("infohash")
    return str(torrent_hash) if torrent_hash else None


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _summarize_mteam_upload_response(response: Any) -> dict[str, Any]:
    torrent_id = _extract_mteam_torrent_id(response)
    if not isinstance(response, dict):
        summary: dict[str, Any] = {"raw_type": type(response).__name__}
        if torrent_id:
            summary["id"] = torrent_id
        return summary
    summary: dict[str, Any] = {}
    for key in ("id", "torrentId", "torrent_id", "status", "message"):
        if key in response:
            summary[key] = response[key]
    if torrent_id and not any(key in summary for key in ("id", "torrentId", "torrent_id")):
        summary["id"] = torrent_id
    return summary or {"keys": sorted(str(key) for key in response)}


def _upload_preflight_next_actions(blockers: list[str], execute: bool) -> list[str]:
    if blockers:
        return [
            "Review every blocker before upload.",
            "Regenerate the MTEAM package after fixing source metadata, qBittorrent evidence, duplicate check, or rules acknowledgement.",
        ]
    if execute:
        return ["Check MTEAM upload result, then download the generated target torrent and inject it into qBittorrent for seeding."]
    return ["Review the package manually, then rerun with --execute --confirm-upload and the reviewed target torrent file when ready."]
