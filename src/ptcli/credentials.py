"""Local configuration checks for ptcli reference flows."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

from src.ptcli.config import resolve_client_config
from src.ptcli.flows import get_flow_profiles
from src.ptcli.mainland import normalize_tracker, parse_tracker_list, unsupported_trackers
from src.ptcli.source import extract_torrent_id


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
        "source_torrent_id": source_torrent_id,
        "target_trackers": target_trackers,
        "ready": all(item.ok for item in checks),
        "checks": [item.to_dict() for item in checks],
    }


def _source_checks(config: dict[str, Any], source_tracker: str, base_dir: str | None) -> list[CheckItem]:
    tracker_config = config.get("TRACKERS", {}).get(source_tracker, {})
    checks: list[CheckItem] = []
    if source_tracker in {"CHD", "U2"}:
        passkey = str(tracker_config.get("passkey", "")).strip() if isinstance(tracker_config, dict) else ""
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

