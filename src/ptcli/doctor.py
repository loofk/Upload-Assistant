"""Live-run checklist helpers for ptcli."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.ptcli.credentials import build_flow_check
from src.ptcli.target import build_mteam_upload_preflight


def build_doctor_check(
    config: dict[str, Any],
    *,
    source_tracker: str,
    source_id: str,
    target_trackers: str,
    client: str,
    base_dir: str | None = None,
    content_path: str | None = None,
    package_dir: str | None = None,
    target_torrent_file: str | None = None,
    accept_rules: bool = False,
    target_execute: bool = False,
    confirm_upload: bool = False,
    download_uploaded_torrent: bool = False,
    inject_uploaded_torrent: bool = False,
    uploaded_save_path: str | None = None,
) -> dict[str, Any]:
    flow_check = build_flow_check(config, source_tracker, source_id, target_trackers, client, base_dir=base_dir)
    checks: list[dict[str, Any]] = []
    checks.append(_check("flow_check", bool(flow_check.get("ready")), "Reference flow config is ready." if flow_check.get("ready") else "Reference flow config has blockers."))
    checks.extend(_prefix_checks("flow.", flow_check.get("checks", [])))
    checks.append(_path_check("content_path", content_path, required=False))
    checks.append(_rules_check(accept_rules))
    checks.append(_confirmation_check(target_execute, confirm_upload))
    checks.append(_target_torrent_check(target_torrent_file, required=bool(package_dir or target_execute)))
    package_preflight = _package_preflight(package_dir, target_execute, target_torrent_file)
    if package_preflight:
        checks.append(package_preflight["check"])
    else:
        checks.append(_check("target_package", False, "Target package directory was not provided."))
    checks.extend(_upload_followup_checks(download_uploaded_torrent, inject_uploaded_torrent, uploaded_save_path, content_path))

    return {
        "status": "ok",
        "ready": all(check["ok"] for check in checks),
        "live_safe_to_attempt": _live_safe_to_attempt(checks, target_execute),
        "flow_check": flow_check,
        "package_preflight": package_preflight.get("preflight") if package_preflight else None,
        "checks": checks,
        "next_actions": _next_actions(checks, target_execute),
    }


def _prefix_checks(prefix: str, checks: list[Any]) -> list[dict[str, Any]]:
    prefixed: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        prefixed.append(
            _check(
                f"{prefix}{check.get('name', 'unknown')}",
                bool(check.get("ok")),
                str(check.get("message", "")),
            )
        )
    return prefixed


def _path_check(name: str, path: str | None, required: bool) -> dict[str, Any]:
    if not path:
        return _check(name, not required, "Path not provided." if not required else "Path is required.")
    resolved = Path(path).expanduser()
    return _check(name, resolved.exists(), f"Path exists: {resolved}" if resolved.exists() else f"Path not found: {resolved}")


def _rules_check(accept_rules: bool) -> dict[str, Any]:
    return _check(
        "rules_acknowledged",
        accept_rules,
        "Rules have been acknowledged." if accept_rules else "Rules must be manually reviewed and acknowledged before live upload.",
    )


def _confirmation_check(target_execute: bool, confirm_upload: bool) -> dict[str, Any]:
    if not target_execute:
        return _check("live_upload_confirmation", True, "Live upload is not requested.")
    return _check(
        "live_upload_confirmation",
        confirm_upload,
        "Live upload confirmation is present." if confirm_upload else "Live upload requires --confirm-upload.",
    )


def _target_torrent_check(target_torrent_file: str | None, required: bool) -> dict[str, Any]:
    if not target_torrent_file:
        return _check("target_torrent_file", not required, "Target torrent file not provided." if not required else "Target torrent file is required.")
    path = Path(target_torrent_file).expanduser()
    if not path.exists():
        return _check("target_torrent_file", False, f"Target torrent file not found: {path}")
    data = path.read_bytes()
    return _check("target_torrent_file", data.startswith(b"d"), f"Target torrent file: {path}")


def _package_preflight(package_dir: str | None, target_execute: bool, target_torrent_file: str | None) -> dict[str, Any] | None:
    if not package_dir:
        return None
    try:
        preflight = build_mteam_upload_preflight(package_dir, execute=target_execute, torrent_file=target_torrent_file)
    except Exception as exc:
        return {"check": _check("target_package", False, str(exc)), "preflight": None}
    return {
        "check": _check(
            "target_package",
            preflight.get("status") == "ready",
            "Target package upload preflight is ready." if preflight.get("status") == "ready" else "Target package upload preflight has blockers.",
        ),
        "preflight": preflight,
    }


def _upload_followup_checks(download_uploaded_torrent: bool, inject_uploaded_torrent: bool, uploaded_save_path: str | None, content_path: str | None) -> list[dict[str, Any]]:
    checks = [
        _check(
            "download_uploaded_torrent",
            True,
            "Uploaded target torrent will be downloaded." if download_uploaded_torrent else "Uploaded target torrent download is not requested.",
        )
    ]
    if inject_uploaded_torrent and not download_uploaded_torrent:
        checks.append(_check("inject_uploaded_torrent", False, "--inject-uploaded-torrent requires --download-uploaded-torrent."))
    elif inject_uploaded_torrent and not (uploaded_save_path or content_path):
        checks.append(_check("inject_uploaded_torrent", False, "--uploaded-save-path or --path is required with --inject-uploaded-torrent."))
    elif inject_uploaded_torrent:
        save_path = uploaded_save_path or content_path
        checks.append(_check("inject_uploaded_torrent", True, "Uploaded target torrent will be injected into qBittorrent."))
        checks.append(_path_check("uploaded_save_path", save_path, required=True))
    else:
        checks.append(_check("inject_uploaded_torrent", True, "Uploaded target torrent injection is not requested."))
    return checks


def _live_safe_to_attempt(checks: list[dict[str, Any]], target_execute: bool) -> bool:
    if not target_execute:
        return False
    required_names = {
        "flow_check",
        "rules_acknowledged",
        "live_upload_confirmation",
        "target_torrent_file",
        "target_package",
    }
    checks_by_name = {str(check["name"]): bool(check["ok"]) for check in checks}
    return all(checks_by_name.get(name, False) for name in required_names)


def _next_actions(checks: list[dict[str, Any]], target_execute: bool) -> list[str]:
    blockers = [check for check in checks if not check["ok"]]
    if blockers:
        return [f"Fix {check['name']}: {check['message']}" for check in blockers]
    if target_execute:
        return ["Run pipeline or target-upload with the same reviewed parameters."]
    return ["Run a dry-run pipeline, then add --target-execute --confirm-upload only after manual rule review."]


def _check(name: str, ok: bool, message: str) -> dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "message": message,
    }
