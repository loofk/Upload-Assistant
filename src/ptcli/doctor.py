"""Live-run checklist helpers for ptcli."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from src.ptcli.credentials import build_flow_check
from src.ptcli.mainland import normalize_tracker, parse_tracker_list
from src.ptcli.rules import build_rule_check
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
    source_torrent_file: str | None = None,
    package_dir: str | None = None,
    target_torrent_file: str | None = None,
    accept_rules: bool = False,
    target_execute: bool = False,
    confirm_upload: bool = False,
    download_uploaded_torrent: bool = False,
    uploaded_torrent_id: str | None = None,
    uploaded_torrent_file: str | None = None,
    inject_uploaded_torrent: bool = False,
    uploaded_save_path: str | None = None,
    wait_uploaded_complete: bool = False,
    check_runtime: bool = False,
) -> dict[str, Any]:
    if target_execute:
        if not uploaded_torrent_file and not uploaded_torrent_id:
            download_uploaded_torrent = True
        inject_uploaded_torrent = True
        wait_uploaded_complete = True
    if uploaded_torrent_id:
        download_uploaded_torrent = True
    flow_check = build_flow_check(config, source_tracker, source_id, target_trackers, client, base_dir=base_dir)
    checks: list[dict[str, Any]] = []
    checks.append(_check("flow_check", bool(flow_check.get("ready")), "Reference flow config is ready." if flow_check.get("ready") else "Reference flow config has blockers."))
    checks.extend(_prefix_checks("flow.", flow_check.get("checks", [])))
    checks.append(_path_check("content_path", content_path, required=False))
    checks.append(_torrent_file_check("source_torrent_file", source_torrent_file, required=False))
    rule_check = build_rule_check(normalize_tracker(source_tracker), parse_tracker_list(target_trackers), accept_rules=accept_rules)
    checks.append(_rules_check(accept_rules))
    checks.append(_check("rule_check", bool(rule_check.get("ready")), "Executable rule check passed." if rule_check.get("ready") else "Executable rule check has blockers."))
    checks.extend(_prefix_checks("rule.", rule_check.get("checks", [])))
    checks.append(_confirmation_check(target_execute, confirm_upload))
    checks.append(_target_torrent_check(target_torrent_file, required=bool((package_dir or target_execute) and not uploaded_torrent_file and not uploaded_torrent_id)))
    checks.append(_torrent_file_check("uploaded_torrent_file", uploaded_torrent_file, required=False))
    package_preflight = _package_preflight(package_dir, target_execute, target_torrent_file or uploaded_torrent_file, recover_uploaded=bool(uploaded_torrent_id or uploaded_torrent_file))
    package_content_path = _package_content_path(package_preflight)
    effective_uploaded_save_path = uploaded_save_path or content_path or package_content_path
    if package_preflight:
        checks.append(package_preflight["check"])
        checks.extend(_target_material_checks(package_preflight.get("preflight"), target_execute))
        checks.append(_rule_obligations_check(package_preflight.get("preflight"), target_execute))
    else:
        checks.append(_check("target_package", False, "Target package directory was not provided."))
        checks.extend(_target_material_checks(None, target_execute))
        checks.append(_rule_obligations_check(None, target_execute))
    if check_runtime:
        checks.append(_runtime_dependency_check())
    checks.extend(_upload_followup_checks(download_uploaded_torrent, uploaded_torrent_id, uploaded_torrent_file, inject_uploaded_torrent, effective_uploaded_save_path, target_execute, wait_uploaded_complete))

    return {
        "status": "ok",
        "ready": all(check["ok"] for check in checks),
        "live_safe_to_attempt": _live_safe_to_attempt(checks, target_execute),
        "effective_uploaded_save_path": effective_uploaded_save_path,
        "flow_check": flow_check,
        "rule_check": rule_check,
        "package_preflight": package_preflight.get("preflight") if package_preflight else None,
        "compliance": _doctor_compliance_summary(rule_check, package_preflight.get("preflight") if package_preflight else None),
        "checks": checks,
        "next_actions": _next_actions(checks, target_execute),
    }


def extend_doctor_check(payload: dict[str, Any], checks: list[dict[str, Any]], *, target_execute: bool) -> dict[str, Any]:
    merged_checks = [*payload.get("checks", []), *checks]
    return {
        **payload,
        "ready": all(check.get("ok") for check in merged_checks),
        "live_safe_to_attempt": bool(payload.get("live_safe_to_attempt")) and all(check.get("ok") for check in checks),
        "checks": merged_checks,
        "next_actions": _next_actions(merged_checks, target_execute),
    }


_PTCLI_RUNTIME_MODULES: tuple[tuple[str, str], ...] = (
    ("aiofiles", "aiofiles"),
    ("beautifulsoup4", "bs4"),
    ("bencode.py", "bencodepy"),
    ("cli-ui", "cli_ui"),
    ("click", "click"),
    ("httpx", "httpx"),
    ("langcodes", "langcodes"),
    ("lxml", "lxml"),
    ("pymediainfo", "pymediainfo"),
    ("qbittorrent-api", "qbittorrentapi"),
    ("requests", "requests"),
    ("rich", "rich"),
    ("torf", "torf"),
    ("typing_extensions", "typing_extensions"),
    ("unidecode", "unidecode"),
)

_LEGACY_RUNTIME_MODULES: tuple[tuple[str, str], ...] = (
    ("discord.py", "discord"),
    ("flask", "flask"),
    ("waitress", "waitress"),
    ("deluge-client", "deluge_client"),
    ("transmission-rpc", "transmission_rpc"),
)

_PTCLI_INTERNAL_IMPORTS: tuple[tuple[str, str], ...] = (
    ("ptcli.cli", "src.ptcli.cli"),
    ("ptcli.materials", "src.ptcli.materials"),
    ("ptcli.metadata", "src.ptcli.metadata"),
    ("ptcli.source", "src.ptcli.source"),
    ("ptcli.target", "src.ptcli.target"),
    ("ptgen_adapter", "src.trackers.COMMON"),
)


def build_runtime_dependency_check() -> dict[str, Any]:
    required = [_module_status(package, module) for package, module in _PTCLI_RUNTIME_MODULES]
    legacy = [_module_status(package, module) for package, module in _LEGACY_RUNTIME_MODULES]
    internal = [_internal_import_status(name, module) for name, module in _PTCLI_INTERNAL_IMPORTS]
    missing = [item["package"] for item in required if not item["available"]]
    internal_missing = [item["name"] for item in internal if not item["available"]]
    legacy_present = [item["package"] for item in legacy if item["available"]]
    ok = not missing and not internal_missing
    message = "PTCLI runtime dependencies and internal imports are ready."
    if missing:
        message = f"Missing PTCLI runtime dependencies: {', '.join(missing)}"
        if internal_missing:
            message = f"{message}; failed internal imports: {', '.join(internal_missing)}"
    elif internal_missing:
        message = f"PTCLI runtime internal imports failed: {', '.join(internal_missing)}"
    return {
        "name": "runtime.ptcli_dependencies",
        "ok": ok,
        "message": message,
        "required": required,
        "internal_imports": internal,
        "legacy_optional": {
            "present": legacy_present,
            "message": "Legacy Web UI/Discord/client dependencies are not required for ptcli.",
        },
    }


def _runtime_dependency_check() -> dict[str, Any]:
    return build_runtime_dependency_check()


def _module_status(package: str, module: str) -> dict[str, Any]:
    return {
        "package": package,
        "module": module,
        "available": importlib.util.find_spec(module) is not None,
    }


def _internal_import_status(name: str, module: str) -> dict[str, Any]:
    try:
        importlib.import_module(module)
    except Exception as exc:
        return {"name": name, "module": module, "available": False, "error": str(exc)}
    return {"name": name, "module": module, "available": True}


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
    return _torrent_file_check("target_torrent_file", target_torrent_file, required=True)


def _torrent_file_check(name: str, torrent_file: str | None, required: bool) -> dict[str, Any]:
    if not torrent_file:
        return _check(name, not required, "Torrent file not provided." if not required else "Torrent file is required.")
    path = Path(torrent_file).expanduser()
    if not path.exists():
        return _check(name, False, f"Torrent file not found: {path}")
    if not path.is_file():
        return _check(name, False, f"Torrent path is not a file: {path}")
    data = path.read_bytes()
    return _check(name, data.startswith(b"d"), f"Torrent file: {path}" if data.startswith(b"d") else f"Torrent file does not look like a .torrent file: {path}")


def _package_preflight(package_dir: str | None, target_execute: bool, target_torrent_file: str | None, *, recover_uploaded: bool = False) -> dict[str, Any] | None:
    if not package_dir:
        return None
    try:
        preflight = build_mteam_upload_preflight(package_dir, execute=target_execute, torrent_file=target_torrent_file)
    except Exception as exc:
        return {"check": _check("target_package", False, str(exc)), "preflight": None}
    if recover_uploaded:
        preflight = _uploaded_recovery_preflight(preflight)
    return {
        "check": _check(
            "target_package",
            preflight.get("status") == "ready",
            _package_preflight_message(preflight),
        ),
        "preflight": preflight,
    }


def _uploaded_recovery_preflight(preflight: dict[str, Any]) -> dict[str, Any]:
    blockers = [blocker for blocker in _string_list(preflight.get("blockers")) if blocker != "MTEAM upload torrent file is required." and not blocker.startswith("materials.")]
    upload_payload = preflight.get("upload_payload")
    if isinstance(upload_payload, dict):
        upload_payload = {
            **upload_payload,
            "blockers": [blocker for blocker in _string_list(upload_payload.get("blockers")) if blocker != "MTEAM upload torrent file is required." and not blocker.startswith("materials.")],
        }
    return {
        **preflight,
        "uploaded_recovery": True,
        "status": "blocked" if blockers else "ready",
        "blockers": blockers,
        "upload_payload": upload_payload if isinstance(upload_payload, dict) else preflight.get("upload_payload"),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _package_content_path(package_preflight: dict[str, Any] | None) -> str | None:
    if not package_preflight:
        return None
    preflight = package_preflight.get("preflight")
    if not isinstance(preflight, dict):
        return None
    content_path = preflight.get("content_path")
    return str(content_path) if content_path else None


def _package_preflight_message(preflight: dict[str, Any]) -> str:
    if preflight.get("status") == "ready":
        return "Target package upload preflight is ready."
    blockers = preflight.get("blockers")
    if isinstance(blockers, list) and blockers:
        return f"Target package upload preflight has blockers: {'; '.join(str(blocker) for blocker in blockers)}"
    return "Target package upload preflight has blockers."


def _rule_obligations_check(preflight: dict[str, Any] | None, target_execute: bool) -> dict[str, Any]:
    if not target_execute:
        return _check("rule_obligations", True, "Live upload is not requested.")
    if not isinstance(preflight, dict):
        return _check("rule_obligations", False, "MTEAM rule obligations cannot be checked without a target package preflight.")
    review = preflight.get("rule_obligation_review")
    if not isinstance(review, dict):
        return _check("rule_obligations", False, "MTEAM rule obligation review is missing from target package preflight.")
    blockers = review.get("blockers")
    if review.get("ready"):
        return _check("rule_obligations", True, "MTEAM live upload rule obligations are complete and acknowledged.")
    if isinstance(blockers, list) and blockers:
        return _check("rule_obligations", False, f"MTEAM live upload rule obligations have blockers: {'; '.join(str(blocker) for blocker in blockers)}")
    return _check("rule_obligations", False, "MTEAM live upload rule obligations have blockers.")


def _target_material_checks(preflight: dict[str, Any] | None, target_execute: bool) -> list[dict[str, Any]]:
    if not target_execute:
        return [_check("target_materials", True, "Live upload is not requested.")]
    if not isinstance(preflight, dict):
        return [_check("target_materials", False, "MTEAM material readiness cannot be checked without a target package preflight.")]
    if preflight.get("uploaded_recovery"):
        return [_check("target_materials", True, "Uploaded torrent recovery does not re-submit MTEAM materials.")]
    upload_payload = preflight.get("upload_payload")
    if not isinstance(upload_payload, dict):
        return [_check("target_materials", False, "MTEAM upload payload summary is missing material checks.")]
    material_checks = upload_payload.get("material_checks")
    if not isinstance(material_checks, list):
        return [_check("target_materials", False, "MTEAM upload payload summary is missing material checks.")]
    normalized = [
        _check(
            f"target_{str(check.get('name', 'materials')).replace('.', '_')}",
            bool(check.get("ok")),
            str(check.get("message") or "MTEAM material check failed."),
        )
        for check in material_checks
        if isinstance(check, dict)
    ]
    failed = [check for check in normalized if not check["ok"]]
    aggregate = _check(
        "target_materials",
        not failed,
        "MTEAM material gate is ready." if not failed else f"MTEAM material gate has blockers: {'; '.join(check['message'] for check in failed)}",
    )
    return [aggregate, *normalized]


def _doctor_compliance_summary(rule_check: dict[str, Any], preflight: dict[str, Any] | None) -> dict[str, Any]:
    automation_scope = rule_check.get("automation_scope") if isinstance(rule_check.get("automation_scope"), dict) else {}
    obligation_review = preflight.get("rule_obligation_review") if isinstance(preflight, dict) else None
    obligations = rule_check.get("rule_obligations")
    if not isinstance(obligations, list):
        obligations = []
    acknowledged = [obligation for obligation in obligations if isinstance(obligation, dict) and obligation.get("acknowledged") is True]
    blockers = _failed_check_messages(rule_check.get("checks"))
    if isinstance(obligation_review, dict):
        review_blockers = obligation_review.get("blockers")
        if isinstance(review_blockers, list):
            blockers.extend(str(blocker) for blocker in review_blockers if isinstance(blocker, str))
    return {
        "ready": bool(rule_check.get("ready")) and not blockers and (not isinstance(obligation_review, dict) or bool(obligation_review.get("ready"))),
        "rules_acknowledged": _check_ok(rule_check.get("checks"), "rules_acknowledged"),
        "site_specific_rules_encoded": bool(automation_scope.get("site_specific_rules_encoded")),
        "policy_checks": automation_scope.get("concrete_policy_checks") or "unknown",
        "manual_review": rule_check.get("manual_review") if isinstance(rule_check.get("manual_review"), dict) else {"required": True, "acknowledged": False},
        "automation_scope": automation_scope,
        "rule_obligations": {
            "ready": bool(obligations) and len(acknowledged) == len([obligation for obligation in obligations if isinstance(obligation, dict)]),
            "count": len([obligation for obligation in obligations if isinstance(obligation, dict)]),
            "acknowledged_count": len(acknowledged),
            "items": obligations,
        },
        "target_rule_obligation_review": obligation_review if isinstance(obligation_review, dict) else None,
        "blockers": blockers,
        "disclaimer": "Site-specific tracker rules are not fully encoded; doctor verifies acknowledgements and adapter gates, but live runs still require manual source/target rule review.",
    }


def _failed_check_messages(checks: Any) -> list[str]:
    if not isinstance(checks, list):
        return []
    return [
        f"{check.get('name', 'check')}: {check.get('message', 'check failed')}"
        for check in checks
        if isinstance(check, dict) and not check.get("ok")
    ]


def _check_ok(checks: Any, name: str) -> bool:
    if not isinstance(checks, list):
        return False
    return any(isinstance(check, dict) and check.get("name") == name and bool(check.get("ok")) for check in checks)


def _upload_followup_checks(
    download_uploaded_torrent: bool,
    uploaded_torrent_id: str | None,
    uploaded_torrent_file: str | None,
    inject_uploaded_torrent: bool,
    effective_uploaded_save_path: str | None,
    target_execute: bool,
    wait_uploaded_complete: bool,
) -> list[dict[str, Any]]:
    checks = [
        _check(
            "download_uploaded_torrent",
            download_uploaded_torrent or bool(uploaded_torrent_file) or bool(uploaded_torrent_id) or not target_execute,
            "Uploaded target torrent will be downloaded."
            if download_uploaded_torrent
            else "Uploaded target torrent id is available for download."
            if uploaded_torrent_id
            else "Uploaded target torrent file is already available."
            if uploaded_torrent_file
            else "Uploaded target torrent download is required for full live retorrent closure."
            if target_execute
            else "Uploaded target torrent download is not requested.",
        )
    ]
    if inject_uploaded_torrent and not (download_uploaded_torrent or uploaded_torrent_file or uploaded_torrent_id):
        checks.append(_check("inject_uploaded_torrent", False, "--inject-uploaded-torrent requires --download-uploaded-torrent, --uploaded-torrent-id, or --uploaded-torrent-file."))
    elif target_execute and not inject_uploaded_torrent:
        checks.append(_check("inject_uploaded_torrent", False, "Uploaded target torrent injection is required for full live retorrent closure."))
    elif inject_uploaded_torrent and not effective_uploaded_save_path:
        checks.append(_check("inject_uploaded_torrent", False, "--uploaded-save-path, --path, or a target package content path is required with --inject-uploaded-torrent."))
    elif inject_uploaded_torrent:
        checks.append(_check("inject_uploaded_torrent", True, "Uploaded target torrent will be injected into qBittorrent."))
        checks.append(_path_check("uploaded_save_path", effective_uploaded_save_path, required=True))
    else:
        checks.append(_check("inject_uploaded_torrent", True, "Uploaded target torrent injection is not requested."))
    if wait_uploaded_complete and not inject_uploaded_torrent:
        checks.append(_check("wait_uploaded_complete", False, "--wait-uploaded-complete requires --inject-uploaded-torrent."))
    elif target_execute and not wait_uploaded_complete:
        checks.append(_check("wait_uploaded_complete", False, "Uploaded target torrent completion wait is required for full live retorrent closure."))
    elif wait_uploaded_complete:
        checks.append(_check("wait_uploaded_complete", True, "Uploaded target torrent will be waited until complete in qBittorrent."))
    else:
        checks.append(_check("wait_uploaded_complete", True, "Uploaded target torrent completion wait is not requested."))
    return checks


def _live_safe_to_attempt(checks: list[dict[str, Any]], target_execute: bool) -> bool:
    if not target_execute:
        return False
    required_names = {
        "flow_check",
        "rule_check",
        "rules_acknowledged",
        "rule_obligations",
        "live_upload_confirmation",
        "target_torrent_file",
        "target_package",
        "target_materials",
        "download_uploaded_torrent",
        "inject_uploaded_torrent",
        "wait_uploaded_complete",
    }
    checks_by_name = {str(check["name"]): bool(check["ok"]) for check in checks}
    if "runtime.ptcli_dependencies" in checks_by_name and not checks_by_name["runtime.ptcli_dependencies"]:
        return False
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
