"""Compact public contracts for the PTCLI v1 HTTP and agent surfaces.

The orchestration layer keeps richer internal evidence for audit and recovery,
but agents should not have to download or reason over hundreds of duplicated
handoff fields for every poll.  This module is deliberately dependency free so
it can also be used by focused validation tooling.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

PUBLIC_SCHEMA_VERSION: Final[int] = 1
PUBLIC_JOB_STATUSES: Final[tuple[str, ...]] = ("queued", "running", "blocked", "failed", "complete", "cancelled")

JOB_STATUS_BUDGET_BYTES: Final[int] = 32 * 1024
JOB_SUMMARY_BUDGET_BYTES: Final[int] = 64 * 1024
JOB_LIST_BUDGET_BYTES: Final[int] = 64 * 1024
TOOLS_BUDGET_BYTES: Final[int] = 128 * 1024
AGENT_MANIFEST_BUDGET_BYTES: Final[int] = 256 * 1024
DEPLOYMENT_BUDGET_BYTES: Final[int] = 64 * 1024
READINESS_BUDGET_BYTES: Final[int] = 96 * 1024
GOAL_PROGRESS_BUDGET_BYTES: Final[int] = 16 * 1024

CORE_AGENT_TOOL_NAMES: Final[tuple[str, ...]] = (
    "source_url_retorrent_preflight",
    "source_url_check_and_submit",
    "daily_candidates_job",
    "submit_daily_candidate_job",
    "list_jobs",
    "get_job_status",
    "get_job_summary",
    "resume_job",
    "cancel_job",
    "readiness_bundle",
    "site_policies",
    "site_policy_rule_review",
    "site_policy_verify",
    "qbit_inspect",
    "deployment_check",
)


def response_envelope_schema(*, include_summary: bool = True) -> dict[str, Any]:
    """Return the stable, intentionally small PTCLI v1 response schema."""
    properties: dict[str, Any] = {
        "schema_version": {"type": "integer", "const": PUBLIC_SCHEMA_VERSION},
        "status": {"type": "string"},
        "ok": {"type": "boolean"},
        "job_id": {"type": ["string", "null"]},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "next_actions": {"type": "array", "items": {"type": "string"}},
        "resume_state": {"type": ["object", "null"]},
        "duplicate_check": {"type": ["object", "null"]},
        "completion": {"type": ["object", "null"]},
        "links": {"type": "object"},
        "evidence": {"type": "object"},
    }
    if include_summary:
        properties["summary"] = {"type": ["object", "null"]}
    return {
        "type": "object",
        "required": ["schema_version", "status", "ok", "blockers", "next_actions", "links", "evidence"],
        "properties": properties,
        "additionalProperties": True,
    }


def error_envelope(message: str, *, status: str = "error", blockers: list[str] | None = None) -> dict[str, Any]:
    resolved_blockers = blockers if blockers is not None else [message]
    return {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "status": status,
        "ok": False,
        "job_id": None,
        "message": message,
        "blockers": _strings(resolved_blockers),
        "next_actions": [],
        "summary": None,
        "resume_state": None,
        "duplicate_check": None,
        "completion": None,
        "links": {},
        "evidence": {},
    }


def compact_job_payload(payload: Mapping[str, Any], *, include_summary: bool = False) -> dict[str, Any]:
    """Project an internal job payload into the stable PTCLI v1 envelope."""
    job_id = _optional_string(payload.get("job_id"))
    status = _optional_string(payload.get("status")) or "failed"
    result = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
    summary_payload = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    duplicate_check = _first_mapping(payload.get("duplicate_check"), summary_payload.get("duplicate_check"), result.get("duplicate_check"))
    resume_state = _first_mapping(payload.get("resume_state"), summary_payload.get("resume_state"), result.get("resume_state"))
    blockers = _strings(payload.get("blockers")) or _strings(summary_payload.get("blockers")) or _strings(result.get("blockers"))
    next_actions = _strings(payload.get("next_actions")) or _strings(summary_payload.get("next_actions")) or _strings(result.get("next_actions"))
    summary = _compact_summary(payload, summary_payload, result) if include_summary else _compact_status_summary(payload, result)
    evidence = _compact_evidence(payload, summary_payload, result, include_summary=include_summary)
    completion = _compact_completion(payload, summary_payload, result)
    links = _job_links(job_id, payload)
    projected = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "status": status,
        "ok": bool(payload.get("ok")) if "ok" in payload else status == "complete",
        "job_id": job_id,
        "kind": _optional_string(payload.get("kind")),
        "blockers": blockers[:20],
        "next_actions": next_actions[:12],
        "summary": summary,
        "resume_state": _compact_value(resume_state, max_depth=3, max_items=20) if resume_state else None,
        "duplicate_check": _compact_value(duplicate_check, max_depth=3, max_items=20) if duplicate_check else None,
        "completion": completion,
        "links": links,
        "evidence": evidence,
        "timestamps": {
            key: payload.get(key)
            for key in ("created_at", "updated_at", "started_at", "completed_at")
            if payload.get(key) is not None
        },
    }
    return _fit_budget(projected, JOB_SUMMARY_BUDGET_BYTES if include_summary else JOB_STATUS_BUDGET_BYTES)


def compact_job_list_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
    compact_jobs = [compact_job_payload(job, include_summary=False) for job in jobs if isinstance(job, Mapping)]
    projected = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "status": _optional_string(payload.get("status")) or "ok",
        "ok": bool(payload.get("ok", True)),
        "job_id": None,
        "count": len(compact_jobs),
        "total": _optional_int(payload.get("total"), len(compact_jobs)),
        "limit": _optional_int(payload.get("limit"), len(compact_jobs)),
        "filters": _compact_value(payload.get("filters"), max_depth=2, max_items=10),
        "status_counts": _compact_value(payload.get("status_counts"), max_depth=2, max_items=10),
        "queue": _compact_value(payload.get("queue"), max_depth=2, max_items=10),
        "jobs": compact_jobs,
        "blockers": _strings(payload.get("blockers"))[:20],
        "next_actions": _strings(payload.get("next_actions"))[:12],
        "summary": {"returned": len(compact_jobs), "total": _optional_int(payload.get("total"), len(compact_jobs))},
        "resume_state": None,
        "duplicate_check": None,
        "completion": None,
        "links": {"self": "/v1/jobs"},
        "evidence": {},
    }
    return _fit_budget(projected, JOB_LIST_BUDGET_BYTES)


def compact_tool_schema(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Keep input fidelity while replacing verbose response/handoff prose."""
    safety = tool.get("safety") if isinstance(tool.get("safety"), Mapping) else {}
    return {
        "name": tool.get("name"),
        "description": _truncate_string(str(tool.get("description") or ""), 600),
        "method": tool.get("method"),
        "path": tool.get("path"),
        "input_schema": _compact_value(tool.get("input_schema"), max_depth=8, max_items=80, max_string=600),
        "response_schema": response_envelope_schema(),
        "safety": {
            key: safety.get(key)
            for key in ("read_only", "mutates_state", "contacts_trackers", "contacts_qbittorrent", "live_upload", "requires_user_review")
            if key in safety
        },
    }


def json_size_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def compact_deployment_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project the verbose deployment audit into the default HTTP contract."""
    ready = payload.get("ready") is True
    agent = _first_mapping(payload.get("agent_summary"))
    final_report = _first_mapping(payload.get("deployment_final_report"))
    decision = _first_mapping(payload.get("seedbox_deployment_final_decision"))
    bootstrap = _first_mapping(payload.get("seedbox_bootstrap_handoff"))
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    compact_checks = [
        {
            key: _compact_value(item.get(key), max_depth=2, max_items=8, max_string=400)
            for key in ("name", "ok", "blocking", "message", "path")
            if item.get(key) is not None
        }
        for item in checks
        if isinstance(item, Mapping)
    ]
    failed = [item for item in compact_checks if item.get("ok") is False]
    next_call = _compact_call(decision.get("recommended_call"), final_report.get("recommended_call"), bootstrap.get("next_step"))
    projected = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "kind": "ptcli.deployment_check.compact",
        "status": _optional_string(payload.get("status")) or ("ok" if ready else "blocked"),
        "ok": bool(payload.get("ok", ready)),
        "ready": ready,
        "job_id": None,
        "blockers": _strings(payload.get("blockers"))[:20],
        "warnings": _strings(payload.get("warnings"))[:12],
        "next_actions": _strings(payload.get("next_actions"))[:8],
        "summary": {
            "ready_for_ai": agent.get("ready_for_ai"),
            "manual_workflow_ready": agent.get("manual_workflow_ready"),
            "daily_workflow_ready": agent.get("daily_workflow_ready"),
            "compose_deployable": agent.get("compose_deployable"),
            "qbit_configured": agent.get("qbit_configured"),
            "api_local_only": agent.get("api_local_only"),
            "api_auth_ready": agent.get("api_auth_ready"),
            "checks_total": len(compact_checks),
            "checks_failed": len(failed),
        },
        "checks": compact_checks,
        "next_call": next_call,
        "seedbox_handoff": {
            "status": decision.get("status") or final_report.get("deployment_status"),
            "action": decision.get("action"),
            "verdict": decision.get("verdict") or final_report.get("verdict"),
            "safe_to_call_now": decision.get("safe_to_call_now"),
            "requires_user_review": decision.get("requires_user_review"),
            "missing_mounts": _compact_value(agent.get("missing_mounts"), max_depth=2, max_items=12),
            "mkdir_commands": _compact_value(bootstrap.get("mkdir_commands"), max_depth=2, max_items=12, max_string=600),
            "verification_requests": _compact_value(bootstrap.get("verification_requests"), max_depth=3, max_items=12, max_string=600),
            "next_call": next_call,
        },
        "resume_state": None,
        "duplicate_check": None,
        "completion": None,
        "links": {"self": "/v1/deployment/check", "detail": "/v1/deployment/check?view=detail", "readiness": "/v1/readiness/bundle"},
        "evidence": {
            "configured_paths": _compact_value(agent.get("configured_paths"), max_depth=2, max_items=12, max_string=600),
            "failed_checks": failed,
        },
        "safety": {"read_only": True, "contacts_trackers": False, "contacts_qbittorrent": False, "live_upload": False},
    }
    return _fit_budget(projected, DEPLOYMENT_BUDGET_BYTES)


def compact_readiness_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded seedbox operator handoff without duplicated evidence trees."""
    ready = payload.get("ready") is True
    request = _first_mapping(payload.get("request"))
    deployment = _first_mapping(payload.get("deployment"))
    deployment_agent = _first_mapping(deployment.get("agent_summary"))
    policies = _first_mapping(payload.get("site_policies"))
    live = _first_mapping(payload.get("live_readiness"))
    validation = _first_mapping(payload.get("live_validation_summary"))
    start = _first_mapping(payload.get("seedbox_live_validation_start_report"))
    execution = _first_mapping(payload.get("seedbox_live_validation_execution_handoff"))
    next_call = _compact_call(payload.get("next_call"), start.get("recommended_call"), execution.get("recommended_call"))
    source_reference = _first_mapping(request.get("source"))
    source_tracker = _first_string(request.get("source_tracker"), source_reference.get("tracker"))
    target = _first_string(request.get("target"), request.get("target_trackers")) or "MTEAM"
    source_id = _first_string(request.get("source_id"), source_reference.get("source_id"), source_reference.get("torrent_id"))
    source_url = _first_string(request.get("source_url"), source_reference.get("requested_source"), source_reference.get("details_url"))
    handoff_context = {
        "source_tracker": source_tracker,
        "source_id": source_id,
        "source_url": source_url,
        "target": target,
        "accept_rules": bool(request.get("accept_rules")),
        "confirm_upload": bool(request.get("confirm_upload")),
    }
    handoff_id = hashlib.sha256(json.dumps(handoff_context, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    gates = {
        "deployment_ready": deployment.get("ready") is True,
        "qbit_configured": deployment_agent.get("qbit_configured") is True,
        "source_resolved": bool(source_url or (source_tracker and source_id)),
        "site_policies_ready": policies.get("ready") is True,
        "accept_rules": bool(request.get("accept_rules")),
        "confirm_upload": bool(request.get("confirm_upload")),
        "duplicate_check_required": True,
        "final_completion_audit_required": True,
    }
    operator_package = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "kind": "ptcli.seedbox_handoff",
        "handoff_id": handoff_id,
        "selected_flow": handoff_context,
        "reference_flows": [
            {"source_tracker": "U2", "target": "MTEAM", "source_url_required": True},
            {"source_tracker": "CHD", "target": "MTEAM", "source_url_required": True},
        ],
        "gates": gates,
        "start_allowed": start.get("start_allowed") is True and all(gates[key] for key in ("deployment_ready", "source_resolved", "site_policies_ready", "accept_rules", "confirm_upload")),
        "safe_to_auto_execute": False,
        "requires_operator_submission": True,
        "next_call": next_call,
        "steps": [
            {"index": 1, "name": "review_rules", "tool": "site_policy_rule_review", "endpoint": "/v1/site-policies/rule-review", "requires_user_review": True},
            {"index": 2, "name": "verify_readiness", "tool": "deployment_check", "endpoint": "/v1/deployment/check", "requires_user_review": False},
            {"index": 3, "name": "check_and_submit", "tool": "source_url_check_and_submit", "endpoint": "/v1/jobs/retorrent/from-url/check-and-submit", "requires_user_review": True},
            {"index": 4, "name": "poll_or_resume", "tool": "get_job_status", "endpoint": "/v1/jobs/{job_id}", "requires_user_review": False},
            {"index": 5, "name": "audit_summary", "tool": "get_job_summary", "endpoint": "/v1/jobs/{job_id}/summary", "requires_user_review": False},
        ],
        "completion_contract": {
            "read": "get_job_summary.completion",
            "status": "not_evaluated",
            "required_values": {"report_allowed": True, "failed_checks": [], "missing_evidence": [], "blockers": []},
            "required_evidence": ["source torrent hash/path", "target torrent hash", "target torrent injection", "qBittorrent limits", "target seeding state", "duplicate_check.exists=false"],
        },
        "stop_when": ["duplicate_check.exists=true", "blockers is non-empty", "accept_rules or confirm_upload is not true", "completion.report_allowed is not true"],
        "safety": {"does_not_execute": True, "does_not_contact_trackers": True, "does_not_contact_qbittorrent": True, "does_not_upload": True},
    }
    projected = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "kind": "ptcli.readiness_bundle.compact",
        "status": _optional_string(payload.get("status")) or ("ok" if ready else "blocked"),
        "ok": bool(payload.get("ok", ready)),
        "ready": ready,
        "job_id": None,
        "blockers": _strings(payload.get("blockers"))[:20],
        "warnings": _strings(payload.get("warnings"))[:12],
        "next_actions": _strings(payload.get("next_actions"))[:8],
        "summary": {
            "phase": validation.get("phase"),
            "first_blocker": validation.get("first_blocker"),
            "ready_count": validation.get("ready_count"),
            "blocked_count": validation.get("blocked_count"),
            "can_run_doctor": validation.get("can_run_doctor"),
            "can_submit_after_doctor": validation.get("can_submit_after_doctor"),
        },
        "gates": gates,
        "next_call": next_call,
        "operator_package": operator_package,
        "resume_state": None,
        "duplicate_check": None,
        "completion": None,
        "links": {"self": "/v1/readiness/bundle", "detail": "/v1/readiness/bundle?view=detail", "deployment": "/v1/deployment/check", "jobs": "/v1/jobs"},
        "evidence": {
            "live_status": live.get("status"),
            "start_status": start.get("status"),
            "execution_status": execution.get("status"),
            "handoff_id": handoff_id,
        },
        "safety": operator_package["safety"],
    }
    return _fit_budget(projected, READINESS_BUDGET_BYTES)


def compact_goal_progress_payload(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Return the default goal router response; detail evidence stays opt-in."""
    next_work = _first_mapping(summary.get("next_work"))
    recommended_call = _compact_call(summary.get("recommended_call"))
    projected = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "kind": "ptcli.goal_progress.compact",
        "status": _optional_string(summary.get("status")) or "blocked",
        "ok": bool(summary.get("ok")),
        "job_id": None,
        "objective": summary.get("objective"),
        "estimated_percent": summary.get("estimated_percent"),
        "remaining_percent": summary.get("remaining_percent"),
        "current_phase": _compact_value(summary.get("current_phase"), max_depth=3, max_items=12),
        "remaining_capability_ids": _strings(summary.get("remaining_capability_ids"))[:20],
        "blockers": _strings(summary.get("blockers"))[:12],
        "next_actions": _strings(summary.get("next_actions"))[:8],
        "next_work": {
            key: next_work.get(key)
            for key in ("id", "name", "status", "action", "primary_capability_id", "recommended_tool", "recommended_endpoint", "recommended_method", "reason")
            if next_work.get(key) is not None
        }
        or None,
        "next_call": recommended_call,
        "summary": {
            "objective": summary.get("objective"),
            "estimated_percent": summary.get("estimated_percent"),
            "remaining_percent": summary.get("remaining_percent"),
            "current_phase": _compact_value(summary.get("current_phase"), max_depth=3, max_items=12),
        },
        "resume_state": None,
        "duplicate_check": None,
        "completion": None,
        "links": {"self": "/v1/goal/progress", "detail": "/v1/goal/progress?view=detail", "deployment": "/v1/deployment/check", "readiness": "/v1/readiness/bundle"},
        "evidence": {"source_context": _compact_value(summary.get("source_context"), max_depth=2, max_items=12)},
        "safety": {"read_only": True, "live_upload": False, "live_upload_requires_confirm_upload": True, "rules_gate_must_be_ready": True},
    }
    return _fit_budget(projected, GOAL_PROGRESS_BUDGET_BYTES)


def _compact_status_summary(payload: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any] | None:
    agent_summary = _first_mapping(payload.get("agent_summary"), result.get("agent_summary"))
    source_reference = _first_mapping(payload.get("source_reference"), result.get("source_reference"))
    values = {
        "message": _first_string(payload.get("message"), result.get("message"), agent_summary.get("message")),
        "stage": _first_string(payload.get("next_stage"), result.get("next_stage"), agent_summary.get("stage")),
        "source": _compact_value(source_reference, max_depth=2, max_items=10) if source_reference else None,
    }
    compacted = {key: value for key, value in values.items() if value not in (None, "", {}, [])}
    return compacted or None


def _compact_summary(payload: Mapping[str, Any], summary: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any] | None:
    preferred = _first_mapping(
        summary.get("summary"),
        result.get("summary"),
        payload.get("agent_summary"),
        result.get("agent_summary"),
        payload.get("closure_summary"),
        result.get("closure_summary"),
    )
    base = _compact_value(preferred, max_depth=4, max_items=30, max_string=2000) if preferred else {}
    if not isinstance(base, dict):
        base = {"value": base}
    for key, value in (
        ("summary_file", _first_string(payload.get("summary_file"), summary.get("summary_file"), result.get("summary_file"))),
        ("next_stage", _first_string(payload.get("next_stage"), summary.get("next_stage"), result.get("next_stage"))),
        ("automation_action", _first_string(payload.get("automation_action"), summary.get("automation_action"), result.get("automation_action"))),
    ):
        if value is not None:
            base.setdefault(key, value)
    return base or None


def _compact_evidence(payload: Mapping[str, Any], summary: Mapping[str, Any], result: Mapping[str, Any], *, include_summary: bool) -> dict[str, Any]:
    evidence = _first_mapping(payload.get("evidence"), summary.get("evidence"), result.get("evidence"))
    compacted = _compact_value(evidence, max_depth=5 if include_summary else 3, max_items=30 if include_summary else 15, max_string=2000)
    if not isinstance(compacted, dict):
        compacted = {}
    summary_file = _first_string(payload.get("summary_file"), summary.get("summary_file"), result.get("summary_file"))
    if summary_file:
        compacted.setdefault("summary_file", summary_file)
    return compacted


def _compact_completion(payload: Mapping[str, Any], summary: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any] | None:
    audit = _first_mapping(
        payload.get("_public_completion"),
        payload.get("live_validation_completion_audit"),
        summary.get("live_validation_completion_audit"),
        result.get("live_validation_completion_audit"),
    )
    request = _first_mapping(payload.get("request"))
    kind = _optional_string(payload.get("kind")) or ""
    live_required = bool(
        request.get("confirm_upload") is True
        or request.get("execute") is True
        or request.get("target_execute") is True
        or request.get("live_validation_submission")
        or kind in {"ptcli.retorrent", "ptcli.source_url_retorrent", "ptcli.checked_retorrent", "ptcli.target_upload", "ptcli.manual_retorrent", "ptcli.candidate_retorrent"}
    )
    if not audit and not live_required:
        return None
    failed_checks = _strings(audit.get("failed_checks"))
    missing_evidence = _strings(audit.get("missing_evidence"))
    blockers = _strings(audit.get("blockers"))
    report_allowed = audit.get("report_allowed") is True
    return {
        "required": live_required,
        "status": "verified" if report_allowed and not failed_checks and not missing_evidence and not blockers else "blocked" if audit else "unverified",
        "report_allowed": report_allowed,
        "verdict": audit.get("verdict") if audit else "live_validation_evidence_required",
        "failed_checks": failed_checks[:20],
        "missing_evidence": missing_evidence[:20],
        "blockers": blockers[:20],
        "source": _compact_value(audit.get("source"), max_depth=3, max_items=16, max_string=1000),
        "target": _compact_value(audit.get("target"), max_depth=3, max_items=16, max_string=1000),
        "duplicate_check": _compact_value(audit.get("duplicate_check"), max_depth=3, max_items=16, max_string=1000),
        "qbit": _compact_value(audit.get("qbit"), max_depth=3, max_items=16, max_string=1000),
        "summary_file": audit.get("summary_file"),
        "next_call": _compact_call(audit.get("recommended_call")),
        "complete_when": ["report_allowed=true", "failed_checks=[]", "missing_evidence=[]", "blockers=[]"],
    }


def _compact_call(*values: Any) -> dict[str, Any] | None:
    call = _first_mapping(*values)
    if not call:
        return None
    return {
        key: value
        for key, value in {
            "tool": call.get("tool") or call.get("recommended_tool"),
            "endpoint": call.get("endpoint") or call.get("recommended_endpoint"),
            "method": call.get("method") or call.get("recommended_method"),
            "request": _compact_request(call.get("request") if isinstance(call.get("request"), Mapping) else call.get("recommended_request")),
            "reason": _truncate_string(str(call.get("reason") or ""), 400) or None,
            "safe_to_call_now": call.get("safe_to_call_now"),
            "requires_user_review": call.get("requires_user_review"),
        }.items()
        if value is not None
    }


def _compact_request(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    allowed = (
        "source_url",
        "source_tracker",
        "source_id",
        "target",
        "accept_rules",
        "confirm_upload",
        "save_path",
        "path",
        "client",
        "job_id",
        "rank",
        "dry_run",
        "summary_file",
        "config",
        "config_path",
        "blocked_trackers",
        "missing_by_category",
    )
    selected = {key: value.get(key) for key in allowed if value.get(key) is not None}
    return _compact_value(selected or value, max_depth=3, max_items=20, max_string=600)


def _job_links(job_id: str | None, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not job_id:
        return {}
    return {
        "self": payload.get("status_endpoint") or f"/v1/jobs/{job_id}",
        "summary": payload.get("summary_endpoint") or f"/v1/jobs/{job_id}/summary",
        "resume": payload.get("resume_endpoint") or f"/v1/jobs/{job_id}/resume",
        "cancel": f"/v1/jobs/{job_id}/cancel",
    }


def _fit_budget(payload: dict[str, Any], budget: int) -> dict[str, Any]:
    if json_size_bytes(payload) <= budget:
        return payload
    payload["evidence"] = {"truncated": True, "reason": "public_response_size_budget"}
    if json_size_bytes(payload) <= budget:
        return payload
    payload["summary"] = {"truncated": True, "reason": "public_response_size_budget"}
    payload["resume_state"] = None
    payload["next_actions"] = payload.get("next_actions", [])[:4]
    payload["blockers"] = payload.get("blockers", [])[:8]
    return payload


def _compact_value(value: Any, *, max_depth: int, max_items: int, max_string: int = 1000) -> Any:
    if max_depth <= 0:
        return "<truncated>"
    if isinstance(value, Mapping):
        items = list(value.items())
        compacted = {
            str(key): _compact_value(item, max_depth=max_depth - 1, max_items=max_items, max_string=max_string)
            for key, item in items[:max_items]
        }
        if len(items) > max_items:
            compacted["_truncated_items"] = len(items) - max_items
        return compacted
    if isinstance(value, (list, tuple)):
        items = list(value)
        compacted = [_compact_value(item, max_depth=max_depth - 1, max_items=max_items, max_string=max_string) for item in items[:max_items]]
        if len(items) > max_items:
            compacted.append({"_truncated_items": len(items) - max_items})
        return compacted
    if isinstance(value, str):
        return _truncate_string(value, max_string)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _truncate_string(str(value), max_string)


def _truncate_string(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 14)]}...<truncated>"


def _first_mapping(*values: Any) -> Mapping[str, Any]:
    for value in values:
        if isinstance(value, Mapping):
            return value
    return {}


def _first_string(*values: Any) -> str | None:
    for value in values:
        resolved = _optional_string(value)
        if resolved is not None:
            return resolved
    return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    resolved = str(value).strip()
    return resolved or None


def _optional_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(item) for item in value if str(item).strip()))
