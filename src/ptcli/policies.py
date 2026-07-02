"""Configurable site automation policies for focused Chinese PT workflows."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Final

from src.ptcli.flows import MTEAM_SOURCE_FLOW_TRACKERS
from src.ptcli.mainland import CHINESE_PT_TRACKERS, normalize_tracker
from src.ptcli.rules import get_rule_profile


@dataclass(frozen=True)
class SitePolicy:
    tracker: str
    rules_url: str
    manual_review_required: bool = True
    allow_auto_download: bool = False
    allow_auto_upload: bool = False
    allow_retorrent: bool = False
    download_rate_limit: int | None = None
    upload_rate_limit: int | None = None
    min_seed_time_hours: float | None = None
    min_ratio: float | None = None
    freeleech_required: bool = False
    rule_review_fingerprint: str | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["notes"] = list(self.notes)
        payload["download_rate_limit_human"] = format_rate_limit(self.download_rate_limit)
        payload["upload_rate_limit_human"] = format_rate_limit(self.upload_rate_limit)
        payload["automation"] = {
            "download": self.allow_auto_download,
            "upload": self.allow_auto_upload,
            "retorrent": self.allow_retorrent,
            "manual_review_required": self.manual_review_required,
        }
        return payload


_REFERENCE_SOURCE_TRACKERS: Final[set[str]] = set(MTEAM_SOURCE_FLOW_TRACKERS)
_REFERENCE_TARGET_TRACKERS: Final[set[str]] = {"MTEAM"}
_RATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b|[kmgt])?(?:/s|ps|/sec)?\s*$", re.IGNORECASE)
_RATE_UNITS: Final[dict[str, int]] = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gib": 1024**3,
    "t": 1000**4,
    "tb": 1000**4,
    "tib": 1024**4,
}


def build_site_policy(config: dict[str, Any] | None, tracker: str) -> SitePolicy:
    normalized = normalize_tracker(tracker)
    if normalized not in CHINESE_PT_TRACKERS:
        raise ValueError(f"Unsupported tracker for Chinese PT policy scope: {tracker}")
    policy = _default_site_policy(normalized)
    overrides = _site_policy_overrides(config or {}).get(normalized)
    if isinstance(overrides, dict):
        policy = _apply_policy_override(policy, overrides)
    return policy


def build_site_policy_report(config: dict[str, Any] | None, trackers: list[str], *, accept_rules: bool = False) -> dict[str, Any]:
    policies = [build_site_policy(config, tracker) for tracker in trackers]
    blockers = _policy_blockers(policies, accept_rules=accept_rules)
    return {
        "status": "ok" if not blockers else "blocked",
        "ready": not blockers,
        "accept_rules": bool(accept_rules),
        "trackers": [policy.tracker for policy in policies],
        "site_policies": [policy.to_dict() for policy in policies],
        "qbit_limits": {policy.tracker: qbit_limits_for_policy(policy) for policy in policies},
        "blockers": blockers,
        "next_actions": _policy_next_actions(blockers),
    }


def qbit_limits_for_tracker(config: dict[str, Any] | None, tracker: str, *, role: str) -> dict[str, Any]:
    policy = build_site_policy(config, tracker)
    limits = qbit_limits_for_policy(policy)
    return {
        **limits,
        "role": role,
        "tracker": policy.tracker,
        "policy": {
            "allow_auto_download": policy.allow_auto_download,
            "allow_auto_upload": policy.allow_auto_upload,
            "allow_retorrent": policy.allow_retorrent,
            "manual_review_required": policy.manual_review_required,
            "rules_url": policy.rules_url,
            "rule_review_fingerprint": policy.rule_review_fingerprint,
        },
    }


def qbit_limits_for_policy(policy: SitePolicy) -> dict[str, Any]:
    return {
        "download_limit": policy.download_rate_limit,
        "upload_limit": policy.upload_rate_limit,
        "download_limit_human": format_rate_limit(policy.download_rate_limit),
        "upload_limit_human": format_rate_limit(policy.upload_rate_limit),
    }


def merge_qbit_limits(policy_limits: dict[str, Any], *, upload_limit: Any = None, download_limit: Any = None) -> dict[str, Any]:
    merged = dict(policy_limits)
    if upload_limit is not None and upload_limit != "":
        merged["upload_limit"] = parse_rate_limit(upload_limit)
        merged["upload_limit_human"] = format_rate_limit(merged["upload_limit"])
        merged["upload_limit_source"] = "request"
    elif merged.get("upload_limit") is not None:
        merged["upload_limit_source"] = "site_policy"
    if download_limit is not None and download_limit != "":
        merged["download_limit"] = parse_rate_limit(download_limit)
        merged["download_limit_human"] = format_rate_limit(merged["download_limit"])
        merged["download_limit_source"] = "request"
    elif merged.get("download_limit") is not None:
        merged["download_limit_source"] = "site_policy"
    return merged


def parse_rate_limit(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Rate limit must be bytes per second or a string like '500 KiB/s'.")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("Rate limit must be >= 0.")
        return value
    if isinstance(value, float):
        if value < 0:
            raise ValueError("Rate limit must be >= 0.")
        return int(value)
    match = _RATE_PATTERN.match(str(value))
    if not match:
        raise ValueError(f"Invalid rate limit: {value!r}. Use bytes/sec or values like '500 KiB/s'.")
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if number < 0:
        raise ValueError("Rate limit must be >= 0.")
    return int(number * _RATE_UNITS[unit])


def format_rate_limit(value: int | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "unlimited"
    for unit, factor in (("GiB/s", 1024**3), ("MiB/s", 1024**2), ("KiB/s", 1024)):
        if value >= factor and value % factor == 0:
            return f"{value // factor} {unit}"
    return f"{value} B/s"


def _default_site_policy(tracker: str) -> SitePolicy:
    profile = get_rule_profile(tracker)
    source_enabled = tracker in _REFERENCE_SOURCE_TRACKERS
    target_enabled = tracker in _REFERENCE_TARGET_TRACKERS
    return SitePolicy(
        tracker=tracker,
        rules_url=profile.rules_url,
        manual_review_required=True,
        allow_auto_download=source_enabled,
        allow_auto_upload=target_enabled,
        allow_retorrent=source_enabled or target_enabled,
        notes=(
            "Default policy only enables currently implemented reference automation paths.",
            "Configure PTCLI.SITE_POLICIES to add per-site limits or stricter local gates.",
        ),
    )


def _site_policy_overrides(config: dict[str, Any]) -> dict[str, Any]:
    ptcli_config = config.get("PTCLI")
    nested = ptcli_config.get("SITE_POLICIES") if isinstance(ptcli_config, dict) else None
    top_level = config.get("SITE_POLICIES")
    merged: dict[str, Any] = {}
    for candidate in (top_level, nested):
        if isinstance(candidate, dict):
            for tracker, value in candidate.items():
                normalized = str(tracker).strip().upper()
                if normalized in CHINESE_PT_TRACKERS:
                    merged[normalize_tracker(normalized)] = value
    return merged


def _apply_policy_override(policy: SitePolicy, override: dict[str, Any]) -> SitePolicy:
    fields: dict[str, Any] = {}
    for key in (
        "rules_url",
        "manual_review_required",
        "allow_auto_download",
        "allow_auto_upload",
        "allow_retorrent",
        "min_seed_time_hours",
        "min_ratio",
        "freeleech_required",
        "rule_review_fingerprint",
    ):
        if key in override:
            fields[key] = override[key]
    for key in ("download_rate_limit", "download_limit"):
        if key in override:
            fields["download_rate_limit"] = parse_rate_limit(override[key])
            break
    for key in ("upload_rate_limit", "upload_limit"):
        if key in override:
            fields["upload_rate_limit"] = parse_rate_limit(override[key])
            break
    if "notes" in override:
        notes = override["notes"]
        fields["notes"] = tuple(str(item) for item in notes) if isinstance(notes, (list, tuple)) else (str(notes),)
    return replace(policy, **fields)


def _policy_blockers(policies: list[SitePolicy], *, accept_rules: bool) -> list[str]:
    blockers: list[str] = []
    for policy in policies:
        if policy.manual_review_required and not accept_rules:
            blockers.append(f"{policy.tracker}: manual rule review is required before automation.")
        if not policy.allow_retorrent:
            blockers.append(f"{policy.tracker}: retorrent automation is not enabled by site policy.")
    return blockers


def _policy_next_actions(blockers: list[str]) -> list[str]:
    if not blockers:
        return []
    return [
        "Review every involved tracker rule page, then rerun with --accept-rules.",
        "Add or update PTCLI.SITE_POLICIES/SITE_POLICIES in data/config.py for local automation and qBittorrent rate limits.",
    ]
