"""Rule profile metadata for Chinese-language PT trackers.

This module deliberately avoids inventing tracker rules. It records where rule
review must happen and whether automation is enabled for the focused CLI.
Tracker adapters remain responsible for concrete upload/download validation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final

from src.ptcli.flows import NEXUSPHP_MTEAM_SOURCE_TRACKERS, get_flow_profiles
from src.ptcli.mainland import CHINESE_PT_TRACKERS


@dataclass(frozen=True)
class RuleProfile:
    tracker: str
    rules_url: str
    review_required: bool
    automation_status: str
    notes: tuple[str, ...]
    source_review_items: tuple[str, ...]
    target_review_items: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["notes"] = list(self.notes)
        payload["source_review_items"] = list(self.source_review_items)
        payload["target_review_items"] = list(self.target_review_items)
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

_SOURCE_REVIEW_ITEMS: Final[tuple[str, ...]] = (
    "The source tracker rules page has been reviewed for download, repost, and retention requirements.",
    "The selected torrent is not marked as forbidden to repost or otherwise restricted by the source tracker.",
    "The automation will keep the downloaded source torrent compliant with source tracker requirements.",
)

_TARGET_REVIEW_ITEMS: Final[tuple[str, ...]] = (
    "The target tracker rules page has been reviewed for upload, category, description, and seeding requirements.",
    "The selected content is allowed in the target tracker category chosen by the adapter.",
    "The uploader will seed the uploaded torrent according to the target tracker requirements.",
)

_ENABLED_AUTOMATION_TRACKERS: Final[set[str]] = {*NEXUSPHP_MTEAM_SOURCE_TRACKERS, "MTEAM"}


def get_rule_profile(tracker: str) -> RuleProfile:
    if tracker not in CHINESE_PT_TRACKERS:
        raise ValueError(f"Unsupported tracker for Chinese PT CLI scope: {tracker}")

    return RuleProfile(
        tracker=tracker,
        rules_url=_RULE_URLS.get(tracker, ""),
        review_required=True,
        automation_status="enabled" if tracker in _ENABLED_AUTOMATION_TRACKERS else "planning",
        notes=_DEFAULT_NOTES,
        source_review_items=_SOURCE_REVIEW_ITEMS,
        target_review_items=_TARGET_REVIEW_ITEMS,
    )


def get_rule_profiles(trackers: list[str]) -> list[RuleProfile]:
    return [get_rule_profile(tracker) for tracker in trackers]


def rule_profiles_to_dicts(profiles: list[RuleProfile]) -> list[dict[str, Any]]:
    return [profile.to_dict() for profile in profiles]


def build_rule_check(source_tracker: str, target_trackers: list[str], *, accept_rules: bool) -> dict[str, Any]:
    """Build machine-checkable rule gates without inventing tracker-specific policy."""
    trackers = [source_tracker, *target_trackers]
    profiles = get_rule_profiles(trackers)
    obligations = _rule_obligations(source_tracker, target_trackers, profiles, accept_rules=accept_rules)
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
        {
            "name": "site_rule_obligations_acknowledged",
            "ok": all(obligation["acknowledged"] for obligation in obligations),
            "message": "Every source/target rule obligation has been acknowledged." if accept_rules else "Every involved site rule obligation must be acknowledged before automation.",
        },
        {
            "name": "site_rule_review_scopes_present",
            "ok": all(_obligation_review_scope_ready(obligation) for obligation in obligations),
            "message": "Every source/target rule obligation includes a concrete manual review scope.",
        },
    ]
    return {
        "status": "ok",
        "ready": all(check["ok"] for check in checks),
        "source_tracker": source_tracker,
        "target_trackers": target_trackers,
        "manual_review": _manual_review_summary(source_tracker, target_trackers, obligations, accept_rules),
        "automation_scope": {
            "site_specific_rules_encoded": False,
            "concrete_policy_checks": "tracker_adapters",
            "reference_flow_enabled": reference_flow_enabled,
            "source_adapter_enabled": source_adapter_enabled,
            "target_adapter_enabled": target_adapter_enabled,
        },
        "rule_profiles": rule_profiles_to_dicts(profiles),
        "rule_obligations": obligations,
        "checks": checks,
        "next_actions": _rule_check_next_actions(checks),
    }


def _rule_obligations(source_tracker: str, target_trackers: list[str], profiles: list[RuleProfile], *, accept_rules: bool) -> list[dict[str, Any]]:
    profiles_by_tracker = {profile.tracker: profile for profile in profiles}
    obligations = [
        _rule_obligation(
            source_tracker,
            profiles_by_tracker[source_tracker],
            role="source",
            action="download_and_retorrent",
            accept_rules=accept_rules,
        )
    ]
    obligations.extend(
        _rule_obligation(
            tracker,
            profiles_by_tracker[tracker],
            role="target",
            action="upload_and_seed",
            accept_rules=accept_rules,
        )
        for tracker in target_trackers
    )
    return obligations


def _rule_obligation(tracker: str, profile: RuleProfile, *, role: str, action: str, accept_rules: bool) -> dict[str, Any]:
    return {
        "tracker": tracker,
        "role": role,
        "action": action,
        "rules_url": profile.rules_url,
        "acknowledged": accept_rules,
        "acknowledgement_evidence": _acknowledgement_evidence(tracker, role, action, profile.rules_url, accept_rules),
        "review_scope": _review_scope(profile, role=role, action=action),
        "message": f"{tracker} {role} {action} rules have been acknowledged." if accept_rules else f"Review and acknowledge {tracker} {role} {action} rules before automation.",
    }


def _manual_review_summary(source_tracker: str, target_trackers: list[str], obligations: list[dict[str, Any]], accept_rules: bool) -> dict[str, Any]:
    rules_urls = sorted({str(obligation.get("rules_url")) for obligation in obligations if obligation.get("rules_url")})
    return {
        "required": True,
        "acknowledged": accept_rules,
        "source_tracker": source_tracker,
        "target_trackers": target_trackers,
        "obligation_count": len(obligations),
        "acknowledged_count": len([obligation for obligation in obligations if obligation.get("acknowledged") is True]),
        "rules_urls": rules_urls,
        "required_confirmations": _manual_review_confirmations(obligations),
        "acknowledgement_evidence": [obligation["acknowledgement_evidence"] for obligation in obligations if isinstance(obligation.get("acknowledgement_evidence"), dict)],
        "site_specific_rules_encoded": False,
        "message": "Manual source/target rule review has been acknowledged." if accept_rules else "Manual source/target rule review is required before automation.",
    }


def _acknowledgement_evidence(tracker: str, role: str, action: str, rules_url: str, accept_rules: bool) -> dict[str, Any]:
    return {
        "mode": "--accept-rules",
        "acknowledged": accept_rules,
        "tracker": tracker,
        "role": role,
        "action": action,
        "rules_url": rules_url,
        "site_specific_rules_encoded": False,
        "message": "Manual review flag applies only to this tracker/action/rules URL scope.",
    }


def _review_scope(profile: RuleProfile, *, role: str, action: str) -> dict[str, Any]:
    required_confirmations = profile.source_review_items if role == "source" else profile.target_review_items
    return {
        "role": role,
        "action": action,
        "rules_url": profile.rules_url,
        "required_confirmations": list(required_confirmations),
        "site_specific_rules_encoded": False,
        "encoded_checks": [
            "tracker_scope_allowlist",
            "rules_url_present",
            "manual_acknowledgement_scope",
            "adapter_preflight_required",
        ],
    }


def _manual_review_confirmations(obligations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    confirmations = []
    for obligation in obligations:
        review_scope = obligation.get("review_scope")
        if not isinstance(review_scope, dict):
            continue
        confirmations.append(
            {
                "tracker": obligation.get("tracker"),
                "role": obligation.get("role"),
                "action": obligation.get("action"),
                "rules_url": obligation.get("rules_url"),
                "required_confirmations": review_scope.get("required_confirmations") if isinstance(review_scope.get("required_confirmations"), list) else [],
            }
        )
    return confirmations


def _obligation_review_scope_ready(obligation: dict[str, Any]) -> bool:
    review_scope = obligation.get("review_scope")
    if not isinstance(review_scope, dict):
        return False
    confirmations = review_scope.get("required_confirmations")
    return bool(review_scope.get("rules_url")) and isinstance(confirmations, list) and bool(confirmations)


def _rule_check_next_actions(checks: list[dict[str, Any]]) -> list[str]:
    blockers = [check for check in checks if not check["ok"]]
    if not blockers:
        return ["Proceed only with the same reviewed source and target tracker parameters."]
    return [f"Fix {check['name']}: {check['message']}" for check in blockers]
