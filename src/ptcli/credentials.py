"""Local configuration checks for ptcli reference flows."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

from src.ptcli.config import resolve_client_config
from src.ptcli.flows import MTEAM_SOURCE_FLOW_TRACKERS, get_flow_profiles
from src.ptcli.mainland import normalize_tracker, parse_tracker_list, unsupported_trackers
from src.ptcli.source import extract_torrent_id, source_credential_requirements, source_download_adapter, source_info_adapter


@dataclass(frozen=True)
class CheckItem:
    name: str
    ok: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_flow_check(config: dict[str, Any], source_tracker_raw: str, source_id_raw: str, target_trackers_raw: str, client_name: str, base_dir: str | None = None) -> dict[str, Any]:
    source_tracker = normalize_tracker(source_tracker_raw)
    source_torrent_id = extract_torrent_id(source_id_raw)
    target_trackers = parse_tracker_list(target_trackers_raw)
    invalid = unsupported_trackers([source_tracker, *target_trackers])
    if invalid:
        raise ValueError(f"Unsupported tracker(s) for focused CLI scope: {', '.join(invalid)}")

    flow_profiles = get_flow_profiles(source_tracker, target_trackers)
    source_capability = _source_capability(source_tracker)
    target_capabilities = [_target_capability(tracker) for tracker in target_trackers]
    checks: list[CheckItem] = []
    checks.extend(_source_checks(config, source_tracker, base_dir))
    checks.extend(_target_checks(config, target_trackers))
    checks.append(_qbit_check(config, client_name))
    checks.append(
        CheckItem(
            name="reference_flow",
            ok=bool(flow_profiles),
            message="Reference flow is enabled." if flow_profiles else "This source/target combination is not enabled as a reference flow.",
        )
    )

    return {
        "status": "ok",
        "source_tracker": source_tracker,
        "requested_source_id": source_id_raw,
        "input_source_id": source_id_raw,
        "source_torrent_id": source_torrent_id,
        "target_trackers": target_trackers,
        "source_capability": source_capability,
        "target_capabilities": target_capabilities,
        "credential_requirements": _flow_credential_requirements(source_capability, target_capabilities),
        "ready": all(item.ok for item in checks),
        "checks": [item.to_dict() for item in checks],
    }


def _source_checks(config: dict[str, Any], source_tracker: str, base_dir: str | None) -> list[CheckItem]:
    tracker_config = config.get("TRACKERS", {}).get(source_tracker, {})
    checks: list[CheckItem] = []
    download_adapter = source_download_adapter(source_tracker)
    if source_tracker in MTEAM_SOURCE_FLOW_TRACKERS:
        if download_adapter in {"nexusphp_passkey", "ttg_passkey"}:
            passkey = _source_passkey(tracker_config, source_tracker)
            checks.append(CheckItem(name=f"{source_tracker}.passkey", ok=bool(passkey), message="Passkey configured." if passkey else "Passkey missing."))
        resolved_base_dir = os.path.abspath(base_dir or os.getcwd())
        cookiefile = os.path.join(resolved_base_dir, "data", "cookies", f"{source_tracker}.txt")
        checks.append(CheckItem(name=f"{source_tracker}.cookie", ok=os.path.exists(cookiefile), message=f"Cookie file: {cookiefile}"))
    elif source_tracker == "MTEAM":
        api_key = str(tracker_config.get("api_key", "")).strip() if isinstance(tracker_config, dict) else ""
        checks.append(CheckItem(name="MTEAM.api_key", ok=bool(api_key), message="API key configured." if api_key else "API key missing."))
    else:
        checks.append(CheckItem(name=f"{source_tracker}.source", ok=False, message="Source checks are not implemented for this tracker."))
    return checks


def _source_passkey(tracker_config: Any, source_tracker: str) -> str:
    if not isinstance(tracker_config, dict):
        return ""
    if source_tracker == "TTG":
        return str(tracker_config.get("announce_url") or tracker_config.get("passkey") or "").strip()
    return str(tracker_config.get("passkey", "")).strip()


def _source_capability(source_tracker: str) -> dict[str, Any]:
    return {
        "tracker": source_tracker,
        "source_info_adapter": source_info_adapter(source_tracker),
        "source_download_adapter": source_download_adapter(source_tracker),
        "credential_requirements": source_credential_requirements(source_tracker),
    }


def _target_capability(target_tracker: str) -> dict[str, Any]:
    if target_tracker == "MTEAM":
        return {
            "tracker": target_tracker,
            "target_upload_adapter": "mteam_api",
            "credential_requirements": ["TRACKERS.MTEAM.api_key"],
        }
    return {
        "tracker": target_tracker,
        "target_upload_adapter": None,
        "credential_requirements": [],
    }


def _flow_credential_requirements(source_capability: dict[str, Any], target_capabilities: list[dict[str, Any]]) -> list[str]:
    requirements: list[str] = []
    for requirement in source_capability.get("credential_requirements", []):
        if isinstance(requirement, str) and requirement not in requirements:
            requirements.append(requirement)
    for target_capability in target_capabilities:
        for requirement in target_capability.get("credential_requirements", []):
            if isinstance(requirement, str) and requirement not in requirements:
                requirements.append(requirement)
    return requirements


def _target_checks(config: dict[str, Any], target_trackers: list[str]) -> list[CheckItem]:
    checks: list[CheckItem] = []
    for tracker in target_trackers:
        tracker_config = config.get("TRACKERS", {}).get(tracker, {})
        if tracker == "MTEAM":
            api_key = str(tracker_config.get("api_key", "")).strip() if isinstance(tracker_config, dict) else ""
            checks.append(CheckItem(name="MTEAM.api_key", ok=bool(api_key), message="API key configured." if api_key else "API key missing."))
        else:
            checks.append(CheckItem(name=f"{tracker}.target", ok=False, message="Target checks are not implemented for this tracker."))
    return checks


def _qbit_check(config: dict[str, Any], client_name: str) -> CheckItem:
    try:
        resolved_name, _client_config = resolve_client_config(config, client_name)
    except ValueError as exc:
        return CheckItem(name="qbit.client", ok=False, message=str(exc))
    return CheckItem(name="qbit.client", ok=True, message=f"qBittorrent client configured: {resolved_name}")
