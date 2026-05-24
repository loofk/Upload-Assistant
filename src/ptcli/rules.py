"""Rule profile metadata for Chinese-language PT trackers.

This module deliberately avoids inventing tracker rules. It records where rule
review must happen and whether automation is enabled for the focused CLI.
Tracker adapters remain responsible for concrete upload/download validation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final

from src.ptcli.flows import get_flow_profiles
from src.ptcli.mainland import CHINESE_PT_TRACKERS


@dataclass(frozen=True)
class RuleProfile:
    tracker: str
    rules_url: str
    review_required: bool
    automation_status: str
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["notes"] = list(self.notes)
        return payload


_RULE_URLS: Final[dict[str, str]] = {
    "AUDIENCES": "https://audiences.me/rules.php",
    "CHD": "https://ptchdbits.co/rules.php",
    "HDS": "https://hd-space.org/index.php?page=rules",
    "HDSKY": "https://hdsky.me/rules.php",
    "HHAN": "https://hhanclub.net/rules.php",
    "MTEAM": "https://kp.m-team.cc/rules",
    "OB": "https://ourbits.club/rules.php",
    "PTER": "https://pterclub.com/rules.php",
    "TJUPT": "https://www.tjupt.org/rules.php",
    "TTG": "https://totheglory.im/rules.php",
    "U2": "https://u2.dmhy.org/rules.php",
}

_DEFAULT_NOTES: Final[tuple[str, ...]] = (
    "Do not bypass source or target tracker restrictions.",
    "Do not upload if the source forbids reposting or the target category is not allowed.",
    "Let the tracker adapter perform concrete field/category/description validation before upload.",
)

_ENABLED_AUTOMATION_TRACKERS: Final[set[str]] = {"CHD", "MTEAM", "U2"}


def get_rule_profile(tracker: str) -> RuleProfile:
    if tracker not in CHINESE_PT_TRACKERS:
        raise ValueError(f"Unsupported tracker for Chinese PT CLI scope: {tracker}")

    return RuleProfile(
        tracker=tracker,
        rules_url=_RULE_URLS.get(tracker, ""),
        review_required=True,
        automation_status="enabled" if tracker in _ENABLED_AUTOMATION_TRACKERS else "planning",
        notes=_DEFAULT_NOTES,
    )


def get_rule_profiles(trackers: list[str]) -> list[RuleProfile]:
    return [get_rule_profile(tracker) for tracker in trackers]


def rule_profiles_to_dicts(profiles: list[RuleProfile]) -> list[dict[str, Any]]:
    return [profile.to_dict() for profile in profiles]


def build_rule_check(source_tracker: str, target_trackers: list[str], *, accept_rules: bool) -> dict[str, Any]:
    """Build machine-checkable rule gates without inventing tracker-specific policy."""
    trackers = [source_tracker, *target_trackers]
    profiles = get_rule_profiles(trackers)
    automation_enabled = all(profile.automation_status == "enabled" for profile in profiles)
    reference_flow_enabled = bool(get_flow_profiles(source_tracker, target_trackers))
    target_adapter_enabled = target_trackers == ["MTEAM"]
    source_adapter_enabled = source_tracker in _ENABLED_AUTOMATION_TRACKERS
    checks = [
        {
            "name": "rules_acknowledged",
            "ok": accept_rules,
            "message": "Rules have been manually acknowledged." if accept_rules else "Manual source/target rule acknowledgement is required before upload automation.",
        },
        {
            "name": "automation_enabled",
            "ok": automation_enabled,
            "message": "All involved trackers have enabled automation profiles." if automation_enabled else "One or more tracker rule profiles are still planning-only.",
        },
        {
            "name": "reference_flow_enabled",
            "ok": reference_flow_enabled,
            "message": "This source/target pair is an enabled reference flow." if reference_flow_enabled else "This source/target pair is not enabled as a reference flow.",
        },
        {
            "name": "source_adapter_enabled",
            "ok": source_adapter_enabled,
            "message": f"{source_tracker} source metadata/download adapter is enabled." if source_adapter_enabled else f"{source_tracker} source adapter is not enabled for automation.",
        },
        {
            "name": "target_adapter_enabled",
            "ok": target_adapter_enabled,
            "message": "MTEAM target prepare/check/upload adapter is enabled." if target_adapter_enabled else "Only the MTEAM target upload adapter is enabled right now.",
        },
    ]
    return {
        "status": "ok",
        "ready": all(check["ok"] for check in checks),
        "source_tracker": source_tracker,
        "target_trackers": target_trackers,
        "rule_profiles": rule_profiles_to_dicts(profiles),
        "checks": checks,
        "next_actions": _rule_check_next_actions(checks),
    }


def _rule_check_next_actions(checks: list[dict[str, Any]]) -> list[str]:
    blockers = [check for check in checks if not check["ok"]]
    if not blockers:
        return ["Proceed only with the same reviewed source and target tracker parameters."]
    return [f"Fix {check['name']}: {check['message']}" for check in blockers]
