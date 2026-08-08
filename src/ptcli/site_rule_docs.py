"""Local Markdown registry for auditable tracker-rule documents.

Rule prose is intentionally separated from executable policy.  A document may
be parsed and inspected while it is a draft, but only an explicitly approved
document with matching content/policy fingerprints can be compiled into the
runtime JSON snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Final

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by Python 3.9/3.10 deployments
    import tomli as tomllib  # type: ignore[no-redef]

from src.ptcli.mainland import CHINESE_PT_TRACKERS, normalize_tracker

SITE_RULE_DOCUMENT_KIND: Final[str] = "ptcli.site_rule_document.v1"
SITE_POLICY_SNAPSHOT_KIND: Final[str] = "ptcli.site_policy_snapshot.v1"
SITE_RULE_SCHEMA_VERSION: Final[int] = 1
SITE_RULE_REVIEW_STATUSES: Final[set[str]] = {"draft", "extracted", "approved", "superseded"}
SITE_RULE_VERIFICATION_MODES: Final[set[str]] = {"programmatic", "manual", "informational"}
SITE_RULE_RESOLUTIONS: Final[set[str]] = {"enforced", "accepted", "pending", "not_applicable"}
SITE_RULE_ROLES: Final[set[str]] = {"source", "target"}
_RATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:[kmgt]?i?b|[kmgt])?(?:/s|ps|/sec)?\s*$", re.IGNORECASE)
_ROOT_FIELDS: Final[tuple[str, ...]] = (
    "schema_version",
    "kind",
    "tracker",
    "display_name",
    "roles",
    "rules_url",
    "captured_at",
    "source_complete",
    "source_scope",
    "source_text_sha256",
    "review_status",
    "reviewer",
    "reviewed_at",
    "review_fingerprint",
    "notes",
)
_POLICY_SECTIONS: Final[tuple[str, ...]] = ("automation", "qbit_limits", "seeding_requirements", "transfer_rules")


class SiteRuleDocumentError(ValueError):
    """Raised when a rule document or snapshot cannot be parsed safely."""


def default_site_rules_dir() -> Path:
    return Path(os.environ.get("PTCLI_SITE_RULES_DIR") or "data/site-rules").expanduser()


def default_site_policy_snapshot_path(config_path: str | Path | None = None) -> Path:
    configured = os.environ.get("PTCLI_SITE_POLICY_SNAPSHOT")
    if configured:
        return Path(configured).expanduser()
    if config_path:
        return Path(config_path).expanduser().resolve().parent / "site-policies.generated.json"
    return Path("data/site-policies.generated.json")


def inspect_site_rule_documents(rules_dir: str | Path | None = None) -> dict[str, Any]:
    directory = Path(rules_dir).expanduser() if rules_dir else default_site_rules_dir()
    paths = sorted(path for path in directory.glob("*.md") if path.name.lower() != "readme.md") if directory.is_dir() else []
    documents = [validate_site_rule_document(path) for path in paths]
    ready_count = sum(1 for document in documents if document.get("ready_for_compile") is True)
    invalid_documents = [document for document in documents if document.get("valid") is not True]
    blockers = [
        f"{document.get('tracker') or Path(str(document.get('path'))).stem}: {blocker}"
        for document in invalid_documents
        for blocker in _strings(document.get("blockers"))
    ]
    return {
        "schema_version": SITE_RULE_SCHEMA_VERSION,
        "kind": "ptcli.site_rule_document_list",
        "status": "blocked" if blockers else "ok",
        "ok": not blockers,
        "rules_dir": str(directory),
        "exists": directory.is_dir(),
        "count": len(documents),
        "ready_count": ready_count,
        "draft_count": len(documents) - ready_count,
        "documents": documents,
        "blockers": blockers,
        "next_actions": ["Repair invalid documents, then review drafts and resolve blocking obligations before compiling a runtime policy snapshot."] if blockers else (["Review draft documents and resolve blocking obligations before compiling a runtime policy snapshot."] if documents and ready_count < len(documents) else []),
        "safety": {"read_only": True, "does_not_contact_trackers": True, "does_not_edit_documents": True, "does_not_enable_live_upload": True},
    }


def validate_site_rule_document(path: str | Path) -> dict[str, Any]:
    document_path = Path(path).expanduser()
    try:
        metadata, body = _read_document(document_path)
    except (OSError, SiteRuleDocumentError, tomllib.TOMLDecodeError) as exc:
        return _invalid_document(document_path, str(exc))

    blockers: list[str] = []
    warnings: list[str] = []
    schema_version = metadata.get("schema_version")
    if schema_version != SITE_RULE_SCHEMA_VERSION:
        blockers.append(f"schema_version must be {SITE_RULE_SCHEMA_VERSION}.")
    if metadata.get("kind") != SITE_RULE_DOCUMENT_KIND:
        blockers.append(f"kind must be {SITE_RULE_DOCUMENT_KIND}.")

    tracker = str(metadata.get("tracker") or "").strip().upper()
    try:
        tracker = normalize_tracker(tracker)
    except ValueError:
        blockers.append(f"Unsupported tracker: {tracker or '<missing>'}.")
    if tracker not in CHINESE_PT_TRACKERS:
        blockers.append(f"Tracker is outside the Chinese PT allowlist: {tracker or '<missing>'}.")

    roles = _strings(metadata.get("roles"))
    invalid_roles = sorted(set(roles).difference(SITE_RULE_ROLES))
    if not roles:
        blockers.append("roles must contain source and/or target.")
    if invalid_roles:
        blockers.append(f"Unsupported roles: {', '.join(invalid_roles)}.")
    rules_url = str(metadata.get("rules_url") or "").strip()
    if not rules_url.startswith(("https://", "http://")):
        blockers.append("rules_url must be an HTTP(S) URL.")

    review_status = str(metadata.get("review_status") or "draft").strip().lower()
    if review_status not in SITE_RULE_REVIEW_STATUSES:
        blockers.append(f"Unsupported review_status: {review_status}.")
    raw_rules = _markdown_section(body, "原始规则")
    if not raw_rules:
        blockers.append("Markdown section '# 原始规则' is required and must not be empty.")
    calculated_source_hash = _sha256_text(raw_rules) if raw_rules else None
    declared_source_hash = str(metadata.get("source_text_sha256") or "").strip().lower() or None
    source_hash_matches = bool(calculated_source_hash and declared_source_hash == calculated_source_hash)
    if not declared_source_hash:
        warnings.append("source_text_sha256 is empty; refresh it before approval.")
    elif not source_hash_matches:
        blockers.append("source_text_sha256 does not match the '# 原始规则' section.")

    policy = _document_policy(metadata)
    policy_blockers = _policy_validation_blockers(policy, roles)
    obligations = _normalize_obligations(metadata.get("obligations"))
    obligation_blockers, obligation_warnings = _obligation_validation(obligations)
    blockers.extend(policy_blockers)
    blockers.extend(obligation_blockers)
    warnings.extend(obligation_warnings)
    if metadata.get("source_complete") is not True:
        warnings.append("source_complete is not true; the pasted rule scope is incomplete.")

    expected_fingerprint = _review_fingerprint(metadata, raw_rules, policy, obligations) if calculated_source_hash else None
    declared_fingerprint = str(metadata.get("review_fingerprint") or "").strip().lower() or None
    fingerprint_matches = bool(expected_fingerprint and declared_fingerprint == expected_fingerprint)
    if review_status == "approved":
        if not str(metadata.get("reviewer") or "").strip():
            blockers.append("reviewer is required for an approved document.")
        reviewed_at = str(metadata.get("reviewed_at") or "").strip()
        if not reviewed_at:
            blockers.append("reviewed_at is required for an approved document.")
        elif not _valid_review_timestamp(reviewed_at):
            blockers.append("reviewed_at must be an ISO-8601 timestamp with an explicit timezone.")
        if not fingerprint_matches:
            blockers.append("review_fingerprint is missing or does not match the current source text and structured policy.")

    pending_obligations = [item for item in obligations if item.get("blocking") is True and item.get("resolution") == "pending"]
    compile_blockers = list(blockers)
    if review_status != "approved":
        compile_blockers.append("review_status must be approved before snapshot compilation.")
    if metadata.get("source_complete") is not True:
        compile_blockers.append("source_complete=true is required before snapshot compilation.")
    if not source_hash_matches:
        compile_blockers.append("source_text_sha256 must match before snapshot compilation.")
    if pending_obligations:
        compile_blockers.append("All blocking obligations must be enforced, accepted, or marked not_applicable before snapshot compilation.")
    compile_blockers = list(dict.fromkeys(compile_blockers))
    valid = not blockers
    ready = valid and not compile_blockers
    return {
        "schema_version": SITE_RULE_SCHEMA_VERSION,
        "kind": "ptcli.site_rule_document_validation",
        "status": "ok" if valid else "blocked",
        "ok": valid,
        "valid": valid,
        "ready_for_compile": ready,
        "path": str(document_path),
        "tracker": tracker or None,
        "display_name": metadata.get("display_name"),
        "roles": roles,
        "rules_url": rules_url or None,
        "captured_at": metadata.get("captured_at"),
        "source_complete": metadata.get("source_complete") is True,
        "source_scope": metadata.get("source_scope"),
        "review_status": review_status,
        "reviewer": metadata.get("reviewer") or None,
        "reviewed_at": metadata.get("reviewed_at") or None,
        "source_text_sha256": {"declared": declared_source_hash, "calculated": calculated_source_hash, "matches": source_hash_matches},
        "review_fingerprint": {"declared": declared_fingerprint, "calculated": expected_fingerprint, "matches": fingerprint_matches},
        "policy": policy,
        "obligations": obligations,
        "pending_obligations": pending_obligations,
        "warnings": list(dict.fromkeys(warnings)),
        "blockers": list(dict.fromkeys(blockers)),
        "compile_blockers": compile_blockers,
        "next_actions": _document_next_actions(review_status, blockers, compile_blockers),
        "safety": {"read_only": True, "does_not_contact_trackers": True, "does_not_edit_document": True, "does_not_approve_rules": True},
    }


def approve_site_rule_document(
    path: str | Path,
    *,
    reviewer: str,
    reviewed_at: str,
    write: bool = False,
) -> dict[str, Any]:
    document_path = Path(path).expanduser()
    metadata, body = _read_document(document_path)
    raw_rules = _markdown_section(body, "原始规则")
    metadata["source_text_sha256"] = _sha256_text(raw_rules)
    metadata["review_status"] = "approved"
    metadata["reviewer"] = reviewer.strip()
    metadata["reviewed_at"] = reviewed_at.strip()
    policy = _document_policy(metadata)
    obligations = _normalize_obligations(metadata.get("obligations"))
    metadata["review_fingerprint"] = _review_fingerprint(metadata, raw_rules, policy, obligations)
    rendered = render_site_rule_document(metadata, body)
    preview = _validate_rendered_document(document_path, rendered)
    blockers = list(preview.get("compile_blockers") or [])
    if not reviewer.strip():
        blockers.append("reviewer is required.")
    if not reviewed_at.strip():
        blockers.append("reviewed_at is required.")
    blockers = list(dict.fromkeys(blockers))
    written = False
    if write and not blockers:
        _atomic_write(document_path, rendered)
        written = True
    return {
        "schema_version": SITE_RULE_SCHEMA_VERSION,
        "kind": "ptcli.site_rule_document_review",
        "status": "ok" if not blockers else "blocked",
        "ok": not blockers,
        "ready": not blockers,
        "path": str(document_path),
        "write_requested": bool(write),
        "written": written,
        "reviewer": reviewer or None,
        "reviewed_at": reviewed_at or None,
        "source_text_sha256": metadata.get("source_text_sha256"),
        "review_fingerprint": metadata.get("review_fingerprint"),
        "document_preview": rendered if not write else None,
        "validation": preview,
        "blockers": blockers,
        "next_actions": ["Resolve validation.compile_blockers, then repeat explicit human approval."] if blockers else (["Compile the approved documents into a runtime snapshot."] if written else ["Review document_preview and repeat with write=true to persist approval."]),
        "safety": {"contacts_trackers": False, "live_upload": False, "requires_explicit_write": True, "human_approval_required": True},
    }


def compile_site_policy_snapshot(
    rules_dir: str | Path | None = None,
    *,
    output_path: str | Path | None = None,
    write: bool = False,
) -> dict[str, Any]:
    directory = Path(rules_dir).expanduser() if rules_dir else default_site_rules_dir()
    paths = sorted(path for path in directory.glob("*.md") if path.name.lower() != "readme.md") if directory.is_dir() else []
    validations = [validate_site_rule_document(path) for path in paths]
    blockers = [f"{item.get('tracker') or Path(str(item.get('path'))).stem}: {blocker}" for item in validations for blocker in _strings(item.get("compile_blockers"))]
    trackers = [str(item.get("tracker")) for item in validations if item.get("tracker")]
    if not paths:
        blockers.append(f"No site-rule Markdown documents found in {directory}.")
    duplicate_trackers = sorted({tracker for tracker in trackers if trackers.count(tracker) > 1})
    if duplicate_trackers:
        blockers.append(f"Duplicate tracker documents: {', '.join(duplicate_trackers)}.")
    documents: list[dict[str, Any]] = []
    policies: dict[str, Any] = {}
    for validation in validations:
        tracker = str(validation.get("tracker") or "")
        if not tracker or validation.get("ready_for_compile") is not True:
            continue
        raw_policy = validation.get("policy")
        policy: dict[str, Any] = deepcopy(raw_policy) if isinstance(raw_policy, dict) else {}
        fingerprint = (validation.get("review_fingerprint") or {}).get("declared") if isinstance(validation.get("review_fingerprint"), dict) else None
        policy["rule_review_fingerprint"] = fingerprint
        policies[tracker] = policy
        documents.append(
            {
                "tracker": tracker,
                "path": validation.get("path"),
                "roles": validation.get("roles"),
                "rules_url": validation.get("rules_url"),
                "captured_at": validation.get("captured_at"),
                "source_text_sha256": (validation.get("source_text_sha256") or {}).get("declared"),
                "reviewer": validation.get("reviewer"),
                "reviewed_at": validation.get("reviewed_at"),
                "review_fingerprint": fingerprint,
                "obligations": validation.get("obligations"),
            }
        )
    blockers = list(dict.fromkeys(blockers))
    reviewed_at_values = sorted(str(item.get("reviewed_at") or "") for item in documents if item.get("reviewed_at"))
    snapshot = {
        "schema_version": SITE_RULE_SCHEMA_VERSION,
        "kind": SITE_POLICY_SNAPSHOT_KIND,
        # Derive this from reviewed inputs so compiling unchanged documents is
        # reproducible and produces the same snapshot hash.
        "generated_at": reviewed_at_values[-1] if reviewed_at_values else None,
        "source": "approved_markdown_rule_documents",
        "documents": documents,
        "policies": policies,
    }
    snapshot["snapshot_sha256"] = _snapshot_hash(snapshot)
    destination = Path(output_path).expanduser() if output_path else default_site_policy_snapshot_path()
    written = False
    if write and not blockers:
        _atomic_write(destination, json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        written = True
    return {
        "schema_version": SITE_RULE_SCHEMA_VERSION,
        "kind": "ptcli.site_policy_snapshot_compile",
        "status": "ok" if not blockers else "blocked",
        "ok": not blockers,
        "ready": not blockers,
        "rules_dir": str(directory),
        "output_path": str(destination),
        "write_requested": bool(write),
        "written": written,
        "document_count": len(validations),
        "compiled_count": len(policies),
        "validations": validations,
        "snapshot": snapshot if not blockers else None,
        "blockers": blockers,
        "next_actions": ["Resolve document compile_blockers; a partial snapshot is never written."] if blockers else (["Restart or rerun policy inspection so the generated snapshot is loaded."] if written else ["Review snapshot and repeat with write=true."]),
        "safety": {"contacts_trackers": False, "live_upload": False, "partial_snapshot_write_forbidden": True, "requires_explicit_write": True},
    }


def load_site_policy_snapshot(path: str | Path) -> dict[str, Any]:
    snapshot_path = Path(path).expanduser()
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SiteRuleDocumentError(f"Unable to load site policy snapshot {snapshot_path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("kind") != SITE_POLICY_SNAPSHOT_KIND or payload.get("schema_version") != SITE_RULE_SCHEMA_VERSION:
        raise SiteRuleDocumentError(f"Unsupported site policy snapshot: {snapshot_path}")
    policies = payload.get("policies")
    documents = payload.get("documents")
    if not isinstance(policies, dict) or not isinstance(documents, list):
        raise SiteRuleDocumentError(f"Invalid site policy snapshot shape: {snapshot_path}")
    expected_hash = str(payload.get("snapshot_sha256") or "")
    if not expected_hash or expected_hash != _snapshot_hash(payload):
        raise SiteRuleDocumentError(f"Site policy snapshot hash mismatch: {snapshot_path}")
    return payload


def merge_site_policy_snapshot(config: dict[str, Any], snapshot: dict[str, Any], *, path: str | Path) -> dict[str, Any]:
    merged = deepcopy(config)
    policies = snapshot.get("policies") if isinstance(snapshot.get("policies"), dict) else {}
    merged["_PTCLI_SITE_POLICY_SNAPSHOT"] = deepcopy(policies)
    merged["_PTCLI_SITE_POLICY_DOCUMENTS"] = {
        str(item.get("tracker")): deepcopy(item) for item in snapshot.get("documents", []) if isinstance(item, dict) and item.get("tracker")
    }
    merged["_PTCLI_SITE_POLICY_SNAPSHOT_META"] = {
        "path": str(path),
        "snapshot_sha256": snapshot.get("snapshot_sha256"),
        "generated_at": snapshot.get("generated_at"),
        "source": snapshot.get("source"),
    }
    return merged


def render_site_rule_document(metadata: dict[str, Any], body: str) -> str:
    lines = ["+++\n"]
    lines.extend(f"{field} = {_toml_value(metadata[field])}\n" for field in _ROOT_FIELDS if field in metadata)
    for section in _POLICY_SECTIONS:
        value = metadata.get(section)
        if not isinstance(value, dict):
            continue
        lines.append(f"\n[{section}]\n")
        lines.extend(f"{key} = {_toml_value(item)}\n" for key, item in value.items())
    for obligation in _normalize_obligations(metadata.get("obligations")):
        lines.append("\n[[obligations]]\n")
        lines.extend(
            f"{key} = {_toml_value(obligation[key])}\n"
            for key in ("id", "scope", "verification", "blocking", "resolution", "description", "evidence_refs", "enforcement")
            if key in obligation
        )
    lines.append("+++\n\n")
    lines.append(body.lstrip("\n"))
    return "".join(lines).rstrip() + "\n"


def _read_document(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    return _parse_document_text(text)


def _parse_document_text(text: str) -> tuple[dict[str, Any], str]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("+++\n"):
        raise SiteRuleDocumentError("Document must start with TOML front matter delimited by +++.")
    end = normalized.find("\n+++\n", 4)
    if end < 0:
        raise SiteRuleDocumentError("TOML front matter closing delimiter +++ is missing.")
    metadata = tomllib.loads(normalized[4:end])
    if not isinstance(metadata, dict):
        raise SiteRuleDocumentError("TOML front matter must decode to an object.")
    return metadata, normalized[end + 5 :].lstrip("\n")


def _validate_rendered_document(path: Path, rendered: str) -> dict[str, Any]:
    metadata, body = _parse_document_text(rendered)
    temporary_path = path.with_name(f".{path.name}.validation")
    # Reuse the validator without changing the real document.
    try:
        _atomic_write(temporary_path, render_site_rule_document(metadata, body))
        return validate_site_rule_document(temporary_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _document_policy(metadata: dict[str, Any]) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "rules_url": metadata.get("rules_url"),
        "notes": _strings(metadata.get("notes")),
    }
    for section in _POLICY_SECTIONS:
        value = metadata.get(section)
        if isinstance(value, dict):
            policy[section] = deepcopy(value)
    return policy


def _policy_validation_blockers(policy: dict[str, Any], roles: list[str]) -> list[str]:
    blockers: list[str] = []
    automation = _dict_section(policy, "automation")
    qbit_limits = _dict_section(policy, "qbit_limits")
    seeding = _dict_section(policy, "seeding_requirements")
    transfer = _dict_section(policy, "transfer_rules")
    blockers.extend(f"automation.{key} must be boolean." for key in ("manual_review_required", "download", "upload", "retorrent") if not isinstance(automation.get(key), bool))
    if "source" in roles:
        _validate_rate(qbit_limits.get("download_limit"), "qbit_limits.download_limit", blockers)
        if not isinstance(seeding.get("min_seed_time_hours"), (int, float)):
            blockers.append("seeding_requirements.min_seed_time_hours is required for source policies.")
    if "target" in roles:
        _validate_rate(qbit_limits.get("upload_limit"), "qbit_limits.upload_limit", blockers)
        if not isinstance(seeding.get("min_ratio"), (int, float)):
            blockers.append("seeding_requirements.min_ratio is required for target policies.")
    for pattern in _strings(transfer.get("forbidden_title_patterns")):
        regex_error = _regex_validation_error(pattern)
        if regex_error:
            blockers.append(f"Invalid forbidden_title_patterns regex {pattern!r}: {regex_error}.")
    return blockers


def _regex_validation_error(pattern: str) -> str | None:
    try:
        re.compile(pattern)
    except re.error as exc:
        return str(exc)
    return None


def _dict_section(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def _valid_review_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _validate_rate(value: Any, field: str, blockers: list[str]) -> None:
    if isinstance(value, bool) or value in (None, ""):
        blockers.append(f"{field} is required.")
        return
    if isinstance(value, (int, float)):
        if value < 0:
            blockers.append(f"{field} must be >= 0.")
        return
    if not _RATE_PATTERN.match(str(value)):
        blockers.append(f"{field} must be bytes/sec or a value such as 20MiB/s.")


def _normalize_obligations(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _obligation_validation(obligations: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(obligations, start=1):
        obligation_id = str(item.get("id") or "").strip()
        if not obligation_id:
            blockers.append(f"obligations[{index}].id is required.")
        elif obligation_id in seen:
            blockers.append(f"Duplicate obligation id: {obligation_id}.")
        seen.add(obligation_id)
        verification = str(item.get("verification") or "").strip()
        if verification not in SITE_RULE_VERIFICATION_MODES:
            blockers.append(f"{obligation_id or index}: unsupported verification mode {verification or '<missing>'}.")
        resolution = str(item.get("resolution") or "pending").strip()
        if resolution not in SITE_RULE_RESOLUTIONS:
            blockers.append(f"{obligation_id or index}: unsupported resolution {resolution}.")
        if not str(item.get("description") or "").strip():
            blockers.append(f"{obligation_id or index}: description is required.")
        if not _strings(item.get("evidence_refs")):
            warnings.append(f"{obligation_id or index}: evidence_refs is empty.")
    return blockers, warnings


def _review_fingerprint(metadata: dict[str, Any], raw_rules: str, policy: dict[str, Any], obligations: list[dict[str, Any]]) -> str:
    payload = {
        "schema_version": SITE_RULE_SCHEMA_VERSION,
        "tracker": str(metadata.get("tracker") or "").strip().upper(),
        "roles": _strings(metadata.get("roles")),
        "rules_url": str(metadata.get("rules_url") or "").strip(),
        "source_text_sha256": _sha256_text(raw_rules),
        "policy": policy,
        "obligations": obligations,
        "reviewer": str(metadata.get("reviewer") or "").strip(),
        "reviewed_at": str(metadata.get("reviewed_at") or "").strip(),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    payload = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _markdown_section(body: str, heading: str) -> str:
    pattern = re.compile(rf"^#\s+{re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(body)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^#\s+.+$", body[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(body)
    return body[start:end].strip()


def _sha256_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").strip() + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if str(item).strip()]


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if value is None:
        return '""'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _invalid_document(path: Path, blocker: str) -> dict[str, Any]:
    return {
        "schema_version": SITE_RULE_SCHEMA_VERSION,
        "kind": "ptcli.site_rule_document_validation",
        "status": "blocked",
        "ok": False,
        "valid": False,
        "ready_for_compile": False,
        "path": str(path),
        "tracker": None,
        "warnings": [],
        "blockers": [blocker],
        "compile_blockers": [blocker],
        "next_actions": ["Repair the Markdown/TOML document, then validate it again."],
        "safety": {"read_only": True, "does_not_contact_trackers": True, "does_not_edit_document": True, "does_not_approve_rules": True},
    }


def _document_next_actions(review_status: str, blockers: list[str], compile_blockers: list[str]) -> list[str]:
    if blockers:
        return ["Repair document validation blockers before review."]
    if review_status != "approved":
        return ["Resolve pending obligations and complete explicit human review before approval."]
    if compile_blockers:
        return ["Resolve compile_blockers and repeat approval so hashes cover the final document."]
    return ["Compile this approved document into the runtime site-policy snapshot."]
