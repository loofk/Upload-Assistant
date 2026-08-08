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
    required_promotions: tuple[str, ...] = ()
    forbidden_title_patterns: tuple[str, ...] = ()
    forbidden_release_groups: tuple[str, ...] = ()
    rule_review_fingerprint: str | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["notes"] = list(self.notes)
        payload["required_promotions"] = list(self.required_promotions)
        payload["forbidden_title_patterns"] = list(self.forbidden_title_patterns)
        payload["forbidden_release_groups"] = list(self.forbidden_release_groups)
        payload["download_rate_limit_human"] = format_rate_limit(self.download_rate_limit)
        payload["upload_rate_limit_human"] = format_rate_limit(self.upload_rate_limit)
        payload["automation"] = {
            "download": self.allow_auto_download,
            "upload": self.allow_auto_upload,
            "retorrent": self.allow_retorrent,
            "manual_review_required": self.manual_review_required,
        }
        payload["transfer_rules"] = {
            "freeleech_required": self.freeleech_required,
            "required_promotions": list(self.required_promotions),
            "forbidden_title_patterns": list(self.forbidden_title_patterns),
            "forbidden_release_groups": list(self.forbidden_release_groups),
        }
        payload["rule_obligations"] = build_rule_obligations(self, roles=("source", "target"))
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


def build_site_policy_config_audit(
    config: dict[str, Any] | None,
    policy: SitePolicy | dict[str, Any],
    *,
    roles: list[str] | tuple[str, ...] | None = None,
    accept_rules: bool = False,
) -> dict[str, Any]:
    """Return a stable audit of the local config shape behind an effective policy."""
    tracker = str(_policy_value(policy, "tracker") or "UNKNOWN")
    normalized_roles = _string_list(roles) or ["unknown"]
    override = _site_policy_overrides(config or {}).get(normalize_tracker(tracker))
    override = override if isinstance(override, dict) else {}
    field_sources = _site_policy_field_sources(override)
    configured_fields = sorted(field_sources)
    coverage = build_site_policy_coverage(_policy_dict(policy), roles=normalized_roles, accept_rules=accept_rules)
    required_fields = _policy_required_fields_for_roles(normalized_roles)
    missing_fields = _string_list(coverage.get("missing_fields"))
    disabled_automation = _string_list(coverage.get("disabled_automation"))
    defaulted_fields = [field for field in required_fields if field not in field_sources and field not in missing_fields]
    placeholder_fields = _site_policy_placeholder_fields(policy)
    blockers = list(dict.fromkeys([*missing_fields, *disabled_automation, *placeholder_fields]))
    shape = _site_policy_override_shape(override)
    ready = bool(coverage.get("complete")) and not placeholder_fields
    snapshot_documents = (config or {}).get("_PTCLI_SITE_POLICY_DOCUMENTS")
    document = snapshot_documents.get(normalize_tracker(tracker)) if isinstance(snapshot_documents, dict) else None
    snapshot_meta = (config or {}).get("_PTCLI_SITE_POLICY_SNAPSHOT_META")
    legacy_override = _site_policy_legacy_override(config or {}, normalize_tracker(tracker))
    if isinstance(document, dict) and legacy_override:
        policy_source = "markdown_snapshot+legacy_override"
    elif isinstance(document, dict):
        policy_source = "markdown_snapshot"
    elif override:
        policy_source = "legacy_config"
    else:
        policy_source = "builtin_default"
    return {
        "kind": "ptcli.site_policy_config_audit",
        "tracker": tracker,
        "roles": normalized_roles,
        "ready": ready,
        "shape": shape,
        "accepted_config_shapes": ["flat", "structured"],
        "config_path": f'config["PTCLI"]["SITE_POLICIES"]["{tracker}"]',
        "configured": bool(override),
        "policy_source": policy_source,
        "policy_document": document if isinstance(document, dict) else None,
        "policy_snapshot": snapshot_meta if isinstance(snapshot_meta, dict) else None,
        "configured_fields": configured_fields,
        "defaulted_fields": defaulted_fields,
        "missing_fields": missing_fields,
        "disabled_automation": disabled_automation,
        "placeholder_fields": placeholder_fields,
        "field_sources": field_sources,
        "automation_fields": _site_policy_group_sources(field_sources, ("manual_review_required", "allow_auto_download", "allow_auto_upload", "allow_retorrent")),
        "qbit_limit_fields": _site_policy_group_sources(field_sources, ("download_rate_limit", "upload_rate_limit")),
        "seeding_fields": _site_policy_group_sources(field_sources, ("min_seed_time_hours", "min_ratio")),
        "transfer_rule_fields": _site_policy_group_sources(
            field_sources,
            ("freeleech_required", "required_promotions", "forbidden_title_patterns", "forbidden_release_groups"),
        ),
        "rule_review": {
            "rules_url": _policy_value(policy, "rules_url"),
            "manual_review_required": _policy_value(policy, "manual_review_required") is True,
            "fingerprint": _policy_value(policy, "rule_review_fingerprint"),
            "missing": "rule_review_fingerprint" in missing_fields,
            "placeholder": "rule_review_fingerprint" in placeholder_fields,
            "accepted_rules": bool(accept_rules),
        },
        "blockers": blockers,
        "next_actions": _site_policy_config_audit_next_actions(tracker, shape, blockers, placeholder_fields),
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
            "rule_obligations": build_rule_obligations(policy, roles=(role,)),
            "transfer_rules": policy.to_dict().get("transfer_rules"),
        },
    }


def qbit_limits_for_policy(policy: SitePolicy) -> dict[str, Any]:
    return {
        "download_limit": policy.download_rate_limit,
        "upload_limit": policy.upload_rate_limit,
        "download_limit_human": format_rate_limit(policy.download_rate_limit),
        "upload_limit_human": format_rate_limit(policy.upload_rate_limit),
    }


def build_site_policy_coverage(policy: dict[str, Any], *, roles: list[str] | tuple[str, ...] | None = None, accept_rules: bool = False) -> dict[str, Any]:
    """Return role-aware policy completeness hints for AI-safe automation."""
    tracker = str(policy.get("tracker") or "UNKNOWN")
    normalized_roles = _string_list(roles) or ["unknown"]
    rule_obligations = build_rule_obligations(policy, roles=normalized_roles, accept_rules=accept_rules)
    raw_automation = policy.get("automation")
    automation: dict[str, Any] = (
        raw_automation
        if isinstance(raw_automation, dict)
        else {
            "download": policy.get("allow_auto_download"),
            "upload": policy.get("allow_auto_upload"),
            "retorrent": policy.get("allow_retorrent"),
            "manual_review_required": policy.get("manual_review_required"),
        }
    )
    missing: list[str] = []
    disabled: list[str] = []
    recommendations: list[str] = []

    if not policy.get("rules_url"):
        missing.append("rules_url")
        recommendations.append(f"{tracker}: add rules_url so agents can point reviewers to the authoritative rule page.")
    if policy.get("manual_review_required") is True and not policy.get("rule_review_fingerprint"):
        missing.append("rule_review_fingerprint")
        recommendations.append(f"{tracker}: record rule_review_fingerprint after manual rule review.")
    if normalized_roles == ["unknown"] or "unknown" in normalized_roles:
        missing.append("source_or_target_role")
        recommendations.append(f"{tracker}: pass source_tracker/from and target/to so coverage can apply role-specific gates.")

    if "source" in normalized_roles:
        if automation.get("download") is not True:
            disabled.append("auto_download")
            recommendations.append(f"{tracker}: enable allow_auto_download before using it as an automated source tracker.")
        elif policy.get("download_rate_limit") is None:
            missing.append("download_rate_limit")
            recommendations.append(f"{tracker}: set download_rate_limit for source pulls on the seedbox.")
        if policy.get("min_seed_time_hours") is None:
            missing.append("min_seed_time_hours")
            recommendations.append(f"{tracker}: set min_seed_time_hours for source-side seeding obligations.")
    if "target" in normalized_roles:
        if automation.get("upload") is not True:
            disabled.append("auto_upload")
            recommendations.append(f"{tracker}: enable allow_auto_upload before using it as an automated target tracker.")
        elif policy.get("upload_rate_limit") is None:
            missing.append("upload_rate_limit")
            recommendations.append(f"{tracker}: set upload_rate_limit for target-side seeding after upload.")
        if policy.get("min_ratio") is None:
            missing.append("min_ratio")
            recommendations.append(f"{tracker}: set min_ratio for target-side seeding obligations.")

    return {
        "tracker": tracker,
        "complete": not missing and not disabled,
        "roles": normalized_roles,
        "missing_fields": missing,
        "disabled_automation": disabled,
        "rule_obligations": rule_obligations,
        "transfer_rules": _policy_transfer_rules(policy),
        "recommendations": recommendations,
    }


def build_rule_obligations(policy: SitePolicy | dict[str, Any], *, roles: list[str] | tuple[str, ...] | None = None, accept_rules: bool = False) -> dict[str, Any]:
    """Return explicit manual rule obligations for source/target automation scopes."""
    tracker = str(_policy_value(policy, "tracker") or "UNKNOWN")
    normalized_roles = _string_list(roles) or ["unknown"]
    rules_url = _policy_value(policy, "rules_url")
    fingerprint = _policy_value(policy, "rule_review_fingerprint")
    manual_review_required = _policy_value(policy, "manual_review_required") is True
    automation = _policy_automation(policy)
    missing_fields: list[str] = []
    missing_confirmations: list[str] = []
    if not rules_url:
        missing_fields.append("rules_url")
    if manual_review_required and not fingerprint:
        missing_fields.append("rule_review_fingerprint")
    if manual_review_required and not accept_rules:
        missing_confirmations.append("accept_rules")

    scopes = [_rule_obligation_scope(policy, role, automation, missing_fields, missing_confirmations) for role in normalized_roles]
    blockers = list(dict.fromkeys([blocker for scope in scopes for blocker in _string_list(scope.get("blockers"))]))
    return {
        "kind": "ptcli.site_policy_rule_obligations",
        "tracker": tracker,
        "roles": normalized_roles,
        "ready": not missing_fields and not missing_confirmations and not blockers,
        "accepted_rules": bool(accept_rules),
        "rules_url": rules_url,
        "manual_review_required": manual_review_required,
        "rule_review_fingerprint": fingerprint,
        "missing_fields": missing_fields,
        "missing_confirmations": missing_confirmations,
        "blockers": blockers,
        "scopes": scopes,
        "required_confirmations": list(dict.fromkeys([confirmation for scope in scopes for confirmation in _string_list(scope.get("required_confirmations"))])),
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
    snapshot = config.get("_PTCLI_SITE_POLICY_SNAPSHOT")
    merged: dict[str, Any] = {}
    for candidate in (snapshot, top_level, nested):
        if isinstance(candidate, dict):
            for tracker, value in candidate.items():
                normalized = str(tracker).strip().upper()
                if normalized in CHINESE_PT_TRACKERS:
                    merged[normalize_tracker(normalized)] = value
    return merged


def _site_policy_legacy_override(config: dict[str, Any], tracker: str) -> bool:
    ptcli_config = config.get("PTCLI")
    nested = ptcli_config.get("SITE_POLICIES") if isinstance(ptcli_config, dict) else None
    top_level = config.get("SITE_POLICIES")
    for candidate in (top_level, nested):
        if not isinstance(candidate, dict):
            continue
        for raw_tracker, value in candidate.items():
            normalized = str(raw_tracker).strip().upper()
            if normalized in CHINESE_PT_TRACKERS and normalize_tracker(normalized) == tracker and isinstance(value, dict):
                return True
    return False


def _apply_policy_override(policy: SitePolicy, override: dict[str, Any]) -> SitePolicy:
    fields: dict[str, Any] = {}
    automation = _dict_section(override, "automation")
    qbit_limits = _dict_section(override, "qbit_limits")
    seeding = _dict_section(override, "seeding_requirements")
    transfer_rules = _dict_section(override, "transfer_rules")
    nested_bool_fields = {
        "manual_review_required": automation.get("manual_review_required"),
        "allow_auto_download": automation.get("download"),
        "allow_auto_upload": automation.get("upload"),
        "allow_retorrent": automation.get("retorrent"),
        "freeleech_required": transfer_rules.get("freeleech_required"),
    }
    fields.update({key: value for key, value in nested_bool_fields.items() if value is not None})
    nested_value_fields = {
        "min_seed_time_hours": seeding.get("min_seed_time_hours"),
        "min_ratio": seeding.get("min_ratio"),
    }
    fields.update({key: value for key, value in nested_value_fields.items() if value is not None})
    if "download_limit" in qbit_limits:
        fields["download_rate_limit"] = parse_rate_limit(qbit_limits["download_limit"])
    elif "download_rate_limit" in qbit_limits:
        fields["download_rate_limit"] = parse_rate_limit(qbit_limits["download_rate_limit"])
    if "upload_limit" in qbit_limits:
        fields["upload_rate_limit"] = parse_rate_limit(qbit_limits["upload_limit"])
    elif "upload_rate_limit" in qbit_limits:
        fields["upload_rate_limit"] = parse_rate_limit(qbit_limits["upload_rate_limit"])
    for key in ("required_promotions", "forbidden_title_patterns", "forbidden_release_groups"):
        if key in transfer_rules:
            fields[key] = tuple(str(item) for item in _as_list(transfer_rules[key]) if str(item).strip())
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
    for key in ("required_promotions", "forbidden_title_patterns", "forbidden_release_groups"):
        if key in override:
            fields[key] = tuple(str(item) for item in _as_list(override[key]) if str(item).strip())
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


def _policy_transfer_rules(policy: dict[str, Any]) -> dict[str, Any]:
    nested = _dict_section(policy, "transfer_rules")
    return {
        "freeleech_required": bool(policy.get("freeleech_required") or nested.get("freeleech_required")),
        "required_promotions": _string_list(policy.get("required_promotions") or nested.get("required_promotions")),
        "forbidden_title_patterns": _string_list(policy.get("forbidden_title_patterns") or nested.get("forbidden_title_patterns")),
        "forbidden_release_groups": _string_list(policy.get("forbidden_release_groups") or nested.get("forbidden_release_groups")),
    }


def _policy_value(policy: SitePolicy | dict[str, Any], key: str) -> Any:
    if isinstance(policy, SitePolicy):
        return getattr(policy, key)
    return policy.get(key)


def _policy_automation(policy: SitePolicy | dict[str, Any]) -> dict[str, Any]:
    if isinstance(policy, SitePolicy):
        return {
            "download": policy.allow_auto_download,
            "upload": policy.allow_auto_upload,
            "retorrent": policy.allow_retorrent,
            "manual_review_required": policy.manual_review_required,
        }
    automation = policy.get("automation")
    if isinstance(automation, dict):
        return automation
    return {
        "download": policy.get("allow_auto_download"),
        "upload": policy.get("allow_auto_upload"),
        "retorrent": policy.get("allow_retorrent"),
        "manual_review_required": policy.get("manual_review_required"),
    }


def _policy_dict(policy: SitePolicy | dict[str, Any]) -> dict[str, Any]:
    if isinstance(policy, SitePolicy):
        return policy.to_dict()
    return dict(policy)


def _policy_required_fields_for_roles(roles: list[str]) -> list[str]:
    fields = ["rules_url", "rule_review_fingerprint", "allow_retorrent"]
    if "source" in roles:
        fields.extend(["allow_auto_download", "download_rate_limit", "min_seed_time_hours"])
    if "target" in roles:
        fields.extend(["allow_auto_upload", "upload_rate_limit", "min_ratio"])
    if "unknown" in roles:
        fields.append("source_or_target_role")
    return list(dict.fromkeys(fields))


def _site_policy_override_shape(override: dict[str, Any]) -> str:
    if not override:
        return "default"
    has_structured = any(isinstance(override.get(section), dict) for section in ("automation", "qbit_limits", "seeding_requirements", "transfer_rules"))
    has_flat = any(key in override for key in _SITE_POLICY_FLAT_SHAPE_FIELDS)
    if has_structured and has_flat:
        return "mixed"
    if has_structured:
        return "structured"
    if has_flat:
        return "flat"
    return "custom"


_SITE_POLICY_FLAT_SOURCE_FIELDS: Final[dict[str, str]] = {
    "rules_url": "rules_url",
    "manual_review_required": "manual_review_required",
    "allow_auto_download": "allow_auto_download",
    "allow_auto_upload": "allow_auto_upload",
    "allow_retorrent": "allow_retorrent",
    "download_rate_limit": "download_rate_limit",
    "download_limit": "download_rate_limit",
    "upload_rate_limit": "upload_rate_limit",
    "upload_limit": "upload_rate_limit",
    "min_seed_time_hours": "min_seed_time_hours",
    "min_ratio": "min_ratio",
    "freeleech_required": "freeleech_required",
    "required_promotions": "required_promotions",
    "forbidden_title_patterns": "forbidden_title_patterns",
    "forbidden_release_groups": "forbidden_release_groups",
    "rule_review_fingerprint": "rule_review_fingerprint",
    "notes": "notes",
}


_SITE_POLICY_FLAT_SHAPE_FIELDS: Final[set[str]] = {
    "manual_review_required",
    "allow_auto_download",
    "allow_auto_upload",
    "allow_retorrent",
    "download_rate_limit",
    "download_limit",
    "upload_rate_limit",
    "upload_limit",
    "min_seed_time_hours",
    "min_ratio",
    "freeleech_required",
    "required_promotions",
    "forbidden_title_patterns",
    "forbidden_release_groups",
}


_SITE_POLICY_STRUCTURED_SOURCE_FIELDS: Final[dict[tuple[str, str], str]] = {
    ("automation", "manual_review_required"): "manual_review_required",
    ("automation", "download"): "allow_auto_download",
    ("automation", "upload"): "allow_auto_upload",
    ("automation", "retorrent"): "allow_retorrent",
    ("qbit_limits", "download_limit"): "download_rate_limit",
    ("qbit_limits", "download_rate_limit"): "download_rate_limit",
    ("qbit_limits", "upload_limit"): "upload_rate_limit",
    ("qbit_limits", "upload_rate_limit"): "upload_rate_limit",
    ("seeding_requirements", "min_seed_time_hours"): "min_seed_time_hours",
    ("seeding_requirements", "min_ratio"): "min_ratio",
    ("transfer_rules", "freeleech_required"): "freeleech_required",
    ("transfer_rules", "required_promotions"): "required_promotions",
    ("transfer_rules", "forbidden_title_patterns"): "forbidden_title_patterns",
    ("transfer_rules", "forbidden_release_groups"): "forbidden_release_groups",
}


def _site_policy_field_sources(override: dict[str, Any]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for config_key, policy_field in _SITE_POLICY_FLAT_SOURCE_FIELDS.items():
        if config_key in override:
            sources[policy_field] = f"flat.{config_key}"
    for (section_name, config_key), policy_field in _SITE_POLICY_STRUCTURED_SOURCE_FIELDS.items():
        section = override.get(section_name)
        if isinstance(section, dict) and config_key in section:
            sources[policy_field] = f"structured.{section_name}.{config_key}"
    return sources


def _site_policy_group_sources(field_sources: dict[str, str], fields: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    return {field: {"configured": field in field_sources, "source": field_sources.get(field)} for field in fields}


def _site_policy_placeholder_fields(policy: SitePolicy | dict[str, Any]) -> list[str]:
    value = str(_policy_value(policy, "rule_review_fingerprint") or "").strip()
    if not value:
        return []
    normalized = value.lower()
    if "yyyy" in normalized or normalized in {"manual-review", "manual-review-yyyy-mm-dd", "reviewed-yyyy-mm-dd"}:
        return ["rule_review_fingerprint"]
    return []


def _site_policy_config_audit_next_actions(tracker: str, shape: str, blockers: list[str], placeholder_fields: list[str]) -> list[str]:
    if not blockers:
        return [f"{tracker}: site policy config audit is ready for AI-gated automation."]
    actions = [f"{tracker}: update PTCLI.SITE_POLICIES using flat_template or structured_template, then rerun site_policies."]
    if placeholder_fields:
        actions.append(f"{tracker}: replace rule_review_fingerprint placeholder with a real manual rule review marker.")
    if shape == "default":
        actions.append(f"{tracker}: add an explicit local site policy instead of relying on default reference automation values.")
    return actions


def _rule_obligation_scope(
    policy: SitePolicy | dict[str, Any],
    role: str,
    automation: dict[str, Any],
    missing_fields: list[str],
    missing_confirmations: list[str],
) -> dict[str, Any]:
    tracker = str(_policy_value(policy, "tracker") or "UNKNOWN")
    if role == "source":
        action = "download_and_retorrent"
        required_confirmations = [
            "source_rules_reviewed",
            "source_download_allowed",
            "source_retorrent_allowed",
            "source_seeding_obligations_accepted",
        ]
        role_blockers = []
        if automation.get("download") is not True:
            role_blockers.append("allow_auto_download")
        if automation.get("retorrent") is not True:
            role_blockers.append("allow_retorrent")
    elif role == "target":
        action = "upload_and_seed"
        required_confirmations = [
            "target_rules_reviewed",
            "target_upload_allowed",
            "target_retorrent_allowed",
            "target_seeding_obligations_accepted",
        ]
        role_blockers = []
        if automation.get("upload") is not True:
            role_blockers.append("allow_auto_upload")
        if automation.get("retorrent") is not True:
            role_blockers.append("allow_retorrent")
    else:
        action = "role_unknown"
        required_confirmations = ["source_or_target_role_selected"]
        role_blockers = ["source_or_target_role"]
    blockers = list(dict.fromkeys([*missing_fields, *missing_confirmations, *role_blockers]))
    return {
        "tracker": tracker,
        "role": role,
        "scope": action,
        "action": action,
        "ready": not blockers,
        "rules_url": _policy_value(policy, "rules_url"),
        "review_fingerprint": _policy_value(policy, "rule_review_fingerprint"),
        "required_confirmations": required_confirmations,
        "missing_fields": list(missing_fields),
        "missing_confirmations": list(missing_confirmations),
        "blockers": blockers,
    }


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _dict_section(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def _policy_blockers(policies: list[SitePolicy], *, accept_rules: bool) -> list[str]:
    blockers: list[str] = []
    for policy in policies:
        if not policy.rules_url:
            blockers.append(f"{policy.tracker}: rules_url is required before automation.")
        if policy.manual_review_required and not policy.rule_review_fingerprint:
            blockers.append(f"{policy.tracker}: rule_review_fingerprint is required before automation.")
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


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]
