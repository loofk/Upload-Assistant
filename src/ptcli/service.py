"""Small JSON HTTP service for AI-driven ptcli automation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.ptcli.candidates import DEFAULT_CANDIDATE_LIMIT, build_daily_candidates
from src.ptcli.cli import build_parser, build_sites_payload, pipeline_payload, retorrent_payload
from src.ptcli.config import load_config, resolve_client_config
from src.ptcli.credentials import build_flow_check
from src.ptcli.doctor import build_runtime_dependency_check
from src.ptcli.mainland import CHINESE_PT_TRACKERS, parse_tracker_list
from src.ptcli.policies import build_rule_obligations, build_site_policy_coverage, build_site_policy_report, parse_rate_limit, qbit_limits_for_tracker
from src.ptcli.qbit import QbitReadOnlyService, match_torrents, summaries_to_dicts
from src.ptcli.source import resolve_source_reference
from src.ptcli.target import create_mteam_upload_torrent_candidate

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
DEFAULT_SERVICE_PORT = 8080
JOB_SCHEMA_VERSION = 1
DEFAULT_JOB_POLL_AFTER_SECONDS = 5
DEFAULT_MAX_CONCURRENT_JOBS = 1
DAILY_CANDIDATE_SCHEDULE_ENV = "PTCLI_DAILY_CANDIDATE_SCHEDULES"
JOB_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
RESUME_COMMAND_ALLOWLIST = {"pipeline", "target-upload", "doctor", "summary-check", "retorrent"}
JOB_STATUS_VALUES = ["queued", "running", "blocked", "failed", "complete", "cancelled"]
RESUME_BOOLEAN_FLAG_OVERRIDES = {
    "accept_rules": "--accept-rules",
    "confirm_upload": "--confirm-upload",
    "target_execute": "--target-execute",
    "enrich_metadata": "--enrich-metadata",
    "fetch_ptgen": "--fetch-ptgen",
    "generate_bdinfo": "--generate-bdinfo",
    "generate_mediainfo": "--generate-mediainfo",
    "generate_screenshots": "--generate-screenshots",
    "upload_screenshots": "--upload-screenshots",
    "download_uploaded_torrent": "--download-uploaded-torrent",
    "inject_uploaded_torrent": "--inject-uploaded-torrent",
    "wait_uploaded_complete": "--wait-uploaded-complete",
}
RESUME_VALUE_FLAG_OVERRIDES = {
    "path": "--path",
    "content_path": "--path",
    "save_path": "--save-path",
    "source_torrent_file": "--source-torrent-file",
    "package_dir": "--package-dir",
    "target_torrent_file": "--target-torrent-file",
    "uploaded_torrent_file": "--uploaded-torrent-file",
    "uploaded_torrent_id": "--uploaded-torrent-id",
    "uploaded_save_path": "--uploaded-save-path",
    "qbit_category": "--qbit-category",
    "qbit_tags": "--qbit-tags",
    "qbit_upload_limit": "--qbit-upload-limit",
    "qbit_download_limit": "--qbit-download-limit",
    "uploaded_qbit_category": "--uploaded-qbit-category",
    "uploaded_qbit_tags": "--uploaded-qbit-tags",
    "uploaded_qbit_upload_limit": "--uploaded-qbit-upload-limit",
    "uploaded_qbit_download_limit": "--uploaded-qbit-download-limit",
    "target_output_dir": "--target-output-dir",
    "target_torrent_output_dir": "--target-torrent-output-dir",
    "uploaded_output_dir": "--uploaded-output-dir",
    "summary_output_dir": "--summary-output-dir",
    "metadata_file": "--metadata-file",
    "ptgen_description_file": "--ptgen-description-file",
    "mediainfo_file": "--mediainfo-file",
    "bdinfo_file": "--bdinfo-file",
    "image_host_file": "--image-host-file",
    "image_host": "--image-host",
    "screenshot_count": "--screenshot-count",
    "imdb_id": "--imdb-id",
    "tmdb_id": "--tmdb-id",
    "tmdb_type": "--tmdb-type",
    "douban_id": "--douban-id",
    "douban_url": "--douban-url",
}
RESUME_REPEATABLE_FLAG_OVERRIDES = {
    "screenshot_file": "--screenshot-file",
    "screenshot_files": "--screenshot-file",
}
AGENT_MANIFEST_PATHS = {
    "/.well-known/ptcli-agent.json",
    "/v1/agent-manifest",
    "/v1/openclaw/skill.json",
    "/v1/hermes/skill.json",
}


class ServiceError(Exception):
    """Error that should be returned as a structured API response."""

    def __init__(self, message: str, *, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


class JobStore:
    """Tiny file-backed job store for long-running ptcli service tasks."""

    def __init__(self, root: str | Path | None = None, *, run_inline: bool = False, recover_interrupted: bool = True, max_concurrent_jobs: int | None = None) -> None:
        self.root = _resolve_job_dir(root)
        self.run_inline = run_inline
        self.max_concurrent_jobs = _resolve_max_concurrent_jobs(max_concurrent_jobs)
        self._lock = threading.Lock()
        self._run_slots = threading.BoundedSemaphore(self.max_concurrent_jobs)
        self.root.mkdir(parents=True, exist_ok=True)
        if recover_interrupted:
            self.recover_interrupted_jobs()

    def create(self, kind: str, request: dict[str, Any], command_argv: list[str], runner: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        now = int(time.time())
        job = {
            "schema_version": JOB_SCHEMA_VERSION,
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "ok": False,
            "request": request,
            "command_argv": command_argv,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "blockers": [],
            "next_actions": [],
            "summary_file": None,
            "resume_state": None,
            "agent_summary": None,
            "agent_decision": None,
            "duplicate_check": None,
            "result": None,
        }
        self._write(job)
        if self.run_inline:
            self._run(job_id, runner)
        else:
            thread = threading.Thread(target=self._run_when_slot_available, args=(job_id, runner), daemon=True)
            thread.start()
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        job = self._read(job_id)
        return _job_public_payload(job, self._job_lineage(job))

    def recover_interrupted_jobs(self) -> dict[str, Any]:
        recovered: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") not in {"queued", "running"}:
                continue
            now = int(time.time())
            previous_status = str(job.get("status") or "unknown")
            blocker = f"Job was {previous_status} when the ptcli service restarted; the in-process runner is no longer attached."
            next_actions = ["Inspect summary_file/result evidence, then resume with resume_endpoint if an allowlisted resume command is available; otherwise submit a new job."]
            job.setdefault("interruption", {})
            if isinstance(job["interruption"], dict):
                job["interruption"].update({"detected_at": now, "previous_status": previous_status, "reason": "service_startup_recovery"})
            job.update(
                {
                    "status": "blocked",
                    "ok": False,
                    "updated_at": now,
                    "completed_at": now,
                    "blockers": [blocker],
                    "next_actions": next_actions,
                    "result": {
                        "status": "blocked",
                        "blockers": [blocker],
                        "next_actions": next_actions,
                        "interruption": job.get("interruption"),
                        "next_command_argv": _resume_argv_from_job(job),
                    },
                }
            )
            job["agent_decision"] = _agent_decision(job)
            self._write(job)
            recovered.append(_job_list_item(job, self._job_lineage(job)))
        return {
            "status": "ok",
            "ok": True,
            "count": len(recovered),
            "recovered_jobs": recovered,
            "next_actions": ["Review recovered jobs with GET /v1/jobs?status=blocked before resuming."] if recovered else [],
        }

    def list(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        status_filter = str(request.get("status") or "").strip()
        kind_filter = str(request.get("kind") or "").strip()
        limit = _bounded_int(request.get("limit"), default=20, minimum=1, maximum=100)
        jobs: list[dict[str, Any]] = []
        status_counts: dict[str, int] = {}
        total = 0
        for path in sorted(self.root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = str(job.get("status") or "unknown")
            kind = str(job.get("kind") or "")
            status_counts[status] = status_counts.get(status, 0) + 1
            if status_filter and status != status_filter:
                continue
            if kind_filter and kind != kind_filter:
                continue
            total += 1
            if len(jobs) < limit:
                jobs.append(_job_list_item(job, self._job_lineage(job)))
        return {
            "status": "ok",
            "ok": True,
            "count": len(jobs),
            "total": total,
            "limit": limit,
            "filters": {"status": status_filter or None, "kind": kind_filter or None},
            "status_counts": status_counts,
            "queue": _job_queue_summary(status_counts, self.max_concurrent_jobs),
            "jobs": jobs,
            "next_actions": _job_list_next_actions(jobs, total, limit),
        }

    def summary(self, job_id: str) -> dict[str, Any]:
        job = self._read(job_id)
        summary_file = _job_summary_file(job)
        summary_payload = None
        if summary_file:
            path = Path(summary_file).expanduser()
            if path.is_file():
                try:
                    summary_payload = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    summary_payload = {"status": "error", "message": "Summary file is not valid JSON.", "path": str(path)}
        return {
            "status": job.get("status"),
            "ok": job.get("status") == "complete",
            "job_id": job.get("job_id"),
            "kind": job.get("kind"),
            "summary_file": summary_file,
            "summary": summary_payload,
            "agent_summary": _agent_summary(summary_payload) or _agent_summary(job.get("result")),
            "agent_decision": _agent_decision(job),
            "candidate_digest": _candidate_digest_from_payload(summary_payload) or _candidate_digest_from_payload(job.get("result")),
            "submit_if_clear_handoff": _job_submit_if_clear_handoff(job),
            "policy_coverage": _job_policy_coverage(job),
            "policy_handoff": _job_policy_handoff(job),
            "policy_qbit_defaults": _job_policy_qbit_defaults(job),
            "qbit_plan": _job_qbit_plan(job),
            "qbit_limit_audit": _job_qbit_limit_audit(job, summary_payload),
            "qbit_handoff": _job_qbit_handoff(job, summary_payload),
            "qbit_enforcement_summary": _job_qbit_enforcement_summary(job, summary_payload),
            "materials_handoff": _job_materials_handoff(job, summary_payload),
            "target_upload_handoff": _job_target_upload_handoff(job, summary_payload),
            "closure_handoff": _job_closure_handoff(job, summary_payload),
            "closure_summary": _job_closure_summary(job, summary_payload),
            "manual_retorrent_handoff": _job_manual_retorrent_handoff(job, summary_payload),
            "runtime": _job_runtime(job),
            "resume_plan": _job_resume_plan(job),
            "resume_requirements": _job_resume_requirements(job, summary_payload),
            "resume_execution_handoff": _job_resume_execution_handoff(job, summary_payload),
            "resume_lineage": _job_resume_lineage(job),
            "job_lineage": self._job_lineage(job),
            "resume_context": _job_resume_context(job),
            "resume_audit": _job_resume_audit(job),
            "resume_summary": _job_resume_summary(job),
            "material_resolution": _job_material_resolution(job),
            "candidate_submission": _job_candidate_submission(job),
            "check_submission": _job_check_submission(job),
            "candidate_batch_handoff": _job_candidate_batch_handoff(job, summary_payload),
            "candidate_submission_handoff": _job_candidate_submission_handoff(job, summary_payload),
            "candidate_submission_summary": _job_candidate_submission_summary(job, summary_payload),
            "source_reference": _job_source_reference(job),
            "workflow_context": _job_workflow_context(job, summary_payload),
            "job_handoff": _job_handoff(job, summary_payload),
            "recovery_handoff": _job_recovery_handoff(job, summary_payload),
            "result": job.get("result"),
            "blockers": _string_list(job.get("blockers")),
            "next_actions": _string_list(job.get("next_actions")),
            "cancellation": job.get("cancellation") if isinstance(job.get("cancellation"), dict) else None,
        }

    def resume(self, job_id: str, request_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        request_overrides = request_overrides or {}
        parent = self._read(job_id)
        status = str(parent.get("status") or "")
        if status in {"queued", "running"}:
            raise ServiceError(f"Job {job_id} is still {status}; wait before resuming.", status=HTTPStatus.CONFLICT)
        original_argv = _resume_argv_from_job(parent)
        override_result = _apply_resume_overrides(original_argv, request_overrides)
        argv = override_result["argv"]
        allowed, reason = _resume_command_allowed(argv)
        resume_context = _resume_context(parent, parent_job_id=job_id, argv=argv, allowed=allowed, reason=reason)
        material_resolution = _resume_material_resolution(parent, override_result)
        resume_context["original_next_command_argv"] = original_argv
        resume_context["resume_overrides"] = override_result["provided"]
        resume_context["applied_overrides"] = override_result["applied"]
        resume_context["ignored_overrides"] = override_result["ignored"]
        resume_context["material_resolution"] = material_resolution
        resume_lineage = _resume_lineage(parent, parent_job_id=job_id, argv=argv, allowed=allowed, reason=reason)
        request = {
            "parent_job_id": job_id,
            "parent_status": parent.get("status"),
            "parent_kind": parent.get("kind"),
            "parent_summary_file": _job_summary_file(parent),
            "parent_source_reference": _job_source_reference(parent),
            "parent_workflow_context": resume_lineage.get("parent_workflow_context"),
            "original_next_command_argv": original_argv,
            "next_command_argv": argv,
            "resume_overrides": override_result["provided"],
            "applied_overrides": override_result["applied"],
            "ignored_overrides": override_result["ignored"],
            "resume_allowed": allowed,
            "resume_blocker": reason,
            "parent_policy_coverage": _job_policy_coverage(parent),
            "parent_policy_qbit_defaults": _job_policy_qbit_defaults(parent),
            "parent_materials_handoff": _job_materials_handoff(parent),
            "material_resolution": material_resolution,
            "resume_context": resume_context,
            "resume_lineage": resume_lineage,
        }
        if _truthy(request_overrides.get("dry_run")):
            return _resume_preview(parent, request, argv, allowed=allowed, reason=reason)
        if not allowed:
            return self.create(
                "ptcli.resume",
                request,
                argv or [],
                lambda: {
                    "kind": "ptcli.service.resume",
                    "status": "blocked",
                    "ok": False,
                    "command_argv": argv or [],
                    "blockers": [reason or "No executable resume command is available."],
                    "next_actions": _string_list(parent.get("next_actions")),
                    "parent_job_id": job_id,
                },
            )
        return self.create("ptcli.resume", request, argv or [], lambda: _run_resume_command(argv or [], parent_job_id=job_id))

    def cancel(self, job_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        job = self._read(job_id)
        status = str(job.get("status") or "")
        if status != "queued":
            raise ServiceError(f"Only queued jobs can be cancelled; job {job_id} is {status}.", status=HTTPStatus.CONFLICT)
        now = int(time.time())
        reason = str(request.get("reason") or "").strip() or "cancelled by API request"
        cancellation = {"cancelled_at": now, "reason": reason, "previous_status": status}
        next_actions = ["Submit a new job when you are ready; cancelled jobs are terminal and cannot be resumed."]
        job.update(
            {
                "status": "cancelled",
                "ok": False,
                "updated_at": now,
                "completed_at": now,
                "cancellation": cancellation,
                "blockers": [],
                "next_actions": next_actions,
                "result": {
                    "status": "cancelled",
                    "ok": False,
                    "cancellation": cancellation,
                    "next_actions": next_actions,
                },
            }
        )
        job["agent_decision"] = _agent_decision(job)
        self._write(job)
        return self.get(job_id)

    def _run_when_slot_available(self, job_id: str, runner: Callable[[], dict[str, Any]]) -> None:
        with self._run_slots:
            self._run(job_id, runner)

    def _run(self, job_id: str, runner: Callable[[], dict[str, Any]]) -> None:
        job = self._read(job_id)
        if job.get("status") != "queued":
            return
        now = int(time.time())
        job.update({"status": "running", "updated_at": now, "started_at": now})
        self._write(job)
        try:
            result = runner()
            completed_at = int(time.time())
            status = _job_status_from_result(result)
            job.update(
                {
                    "status": status,
                    "ok": status == "complete",
                    "updated_at": completed_at,
                    "completed_at": completed_at,
                    "blockers": _result_blockers(result),
                    "next_actions": _string_list(result.get("next_actions")),
                    "summary_file": _result_summary_file(result),
                    "resume_state": _result_resume_state(result),
                    "agent_summary": _agent_summary(result),
                    "agent_decision": None,
                    "duplicate_check": result.get("duplicate_check") if isinstance(result.get("duplicate_check"), dict) else None,
                    "result": result,
                }
            )
            job["agent_decision"] = _agent_decision(job)
        except Exception as exc:
            completed_at = int(time.time())
            job.update(
                {
                    "status": "failed",
                    "ok": False,
                    "updated_at": completed_at,
                    "completed_at": completed_at,
                    "blockers": [str(exc)],
                    "next_actions": ["Inspect the job error, fix configuration or runtime blockers, then submit a new request."],
                    "result": {"status": "error", "message": str(exc)},
                }
            )
            job["agent_decision"] = _agent_decision(job)
        self._write(job)

    def _read(self, job_id: str) -> dict[str, Any]:
        _validate_job_id(job_id)
        path = self.root / f"{job_id}.json"
        if not path.is_file():
            raise ServiceError(f"Job not found: {job_id}", status=HTTPStatus.NOT_FOUND)
        return json.loads(path.read_text(encoding="utf-8"))

    def _read_all_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json"), key=lambda item: item.stat().st_mtime):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                jobs.append(payload)
        return jobs

    def _job_lineage(self, job: dict[str, Any]) -> dict[str, Any]:
        return _job_lineage_summary(job, self._read_all_jobs())

    def _write(self, job: dict[str, Any]) -> None:
        job_id = str(job["job_id"])
        _validate_job_id(job_id)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{job_id}.json"
        tmp_path = path.with_suffix(".json.tmp")
        with self._lock:
            tmp_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(path)


def run_service(host: str, port: int, *, api_token: str | None = None, job_dir: str | None = None, max_concurrent_jobs: int | None = None) -> None:
    """Run the local ptcli JSON API service."""
    job_store = JobStore(job_dir, max_concurrent_jobs=max_concurrent_jobs)
    handler = _handler_class(api_token, job_store)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"ptcli service listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _handler_class(api_token: str | None, job_store: JobStore) -> type[BaseHTTPRequestHandler]:
    class PtcliServiceHandler(BaseHTTPRequestHandler):
        server_version = "ptcli-service/1"

        def do_GET(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            if path == "/health":
                self._send_json(HTTPStatus.OK, health_payload())
                return
            if path == "/openapi.json":
                self._send_json(HTTPStatus.OK, openapi_payload(require_auth=bool(api_token)))
                return
            if path == "/v1/tools":
                self._send_json(HTTPStatus.OK, tools_payload())
                return
            if path in AGENT_MANIFEST_PATHS:
                self._send_json(HTTPStatus.OK, agent_manifest_payload(base_url=self._request_base_url()))
                return
            if path == "/v1/sites":
                if not self._authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                    return
                query = {key: values[-1] for key, values in parse_qs(parsed_url.query).items() if values}
                try:
                    self._send_json(HTTPStatus.OK, sites_payload(query))
                except ServiceError as exc:
                    self._send_json(exc.status, {"status": "error", "message": str(exc)})
                return
            if path == "/v1/qbit/inspect":
                if not self._authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                    return
                query = {key: values[-1] for key, values in parse_qs(parsed_url.query).items() if values}
                try:
                    self._send_json(HTTPStatus.OK, asyncio.run(qbit_inspect_payload(query)))
                except ServiceError as exc:
                    self._send_json(exc.status, {"status": "error", "message": str(exc)})
                return
            if path == "/v1/qbit/match":
                if not self._authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                    return
                query = {key: values[-1] for key, values in parse_qs(parsed_url.query).items() if values}
                try:
                    self._send_json(HTTPStatus.OK, asyncio.run(qbit_match_payload(query)))
                except ServiceError as exc:
                    self._send_json(exc.status, {"status": "error", "message": str(exc)})
                return
            if path == "/v1/qbit/export":
                if not self._authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                    return
                query = {key: values[-1] for key, values in parse_qs(parsed_url.query).items() if values}
                try:
                    self._send_json(HTTPStatus.OK, asyncio.run(qbit_export_payload(query)))
                except ServiceError as exc:
                    self._send_json(exc.status, {"status": "error", "message": str(exc)})
                return
            if path == "/v1/deployment/check":
                if not self._authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                    return
                query = {key: values[-1] for key, values in parse_qs(parsed_url.query).items() if values}
                self._send_json(HTTPStatus.OK, deployment_check_payload(query))
                return
            if path == "/v1/readiness/bundle":
                if not self._authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                    return
                query = {key: values[-1] for key, values in parse_qs(parsed_url.query).items() if values}
                self._send_json(HTTPStatus.OK, readiness_bundle_payload(query))
                return
            if path == "/v1/site-policies":
                if not self._authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                    return
                query = {key: values[-1] for key, values in parse_qs(parsed_url.query).items() if values}
                try:
                    self._send_json(HTTPStatus.OK, site_policies_payload(query))
                except ServiceError as exc:
                    self._send_json(exc.status, {"status": "error", "message": str(exc)})
                return
            if path == "/v1/candidates/daily/schedule":
                if not self._authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                    return
                try:
                    self._send_json(HTTPStatus.OK, daily_candidate_schedule_payload({}))
                except ServiceError as exc:
                    self._send_json(exc.status, {"status": "error", "message": str(exc)})
                return
            if path == "/v1/jobs":
                if not self._authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                    return
                query = {key: values[-1] for key, values in parse_qs(parsed_url.query).items() if values}
                self._send_json(HTTPStatus.OK, job_store.list(query))
                return
            if path.startswith("/v1/jobs/"):
                if not self._authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                    return
                try:
                    self._send_json(HTTPStatus.OK, self._job_get(path))
                except ServiceError as exc:
                    self._send_json(exc.status, {"status": "error", "message": str(exc)})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Endpoint not found."})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                return
            path = urlparse(self.path).path
            handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
                "/v1/retorrent/check": lambda payload: asyncio.run(retorrent_check(payload)),
                "/v1/retorrent": lambda payload: asyncio.run(retorrent(payload)),
                "/v1/agent/run-preview": agent_run_preview_payload,
                "/v1/retorrent/source-url/preflight": source_url_retorrent_preflight_payload,
                "/v1/sites": sites_payload,
                "/v1/site-policies": site_policies_payload,
                "/v1/qbit/inspect": lambda payload: asyncio.run(qbit_inspect_payload(payload)),
                "/v1/qbit/match": lambda payload: asyncio.run(qbit_match_payload(payload)),
                "/v1/qbit/export": lambda payload: asyncio.run(qbit_export_payload(payload)),
                "/v1/qbit/inject": lambda payload: asyncio.run(qbit_inject_payload(payload)),
                "/v1/qbit/wait": lambda payload: asyncio.run(qbit_wait_payload(payload)),
                "/v1/readiness/bundle": readiness_bundle_payload,
                "/v1/candidates/daily": lambda payload: asyncio.run(daily_candidates(payload)),
                "/v1/candidates/daily/schedule": daily_candidate_schedule_payload,
                "/v1/jobs/retorrent/check": lambda payload: create_retorrent_check_job(job_store, payload),
                "/v1/jobs/retorrent": lambda payload: create_retorrent_job(job_store, payload),
                "/v1/jobs/retorrent/submit": lambda payload: create_manual_retorrent_job(job_store, payload),
                "/v1/jobs/retorrent/from-url": lambda payload: create_source_url_retorrent_job(job_store, payload),
                "/v1/jobs/retorrent/from-url/check-and-submit": lambda payload: asyncio.run(create_source_url_check_and_submit_job(job_store, payload)),
                "/v1/jobs/candidates/daily": lambda payload: create_daily_candidates_job(job_store, payload),
                "/v1/jobs/candidates/daily/schedule": lambda payload: create_daily_candidate_schedule_jobs(job_store, payload),
            }
            if path.startswith("/v1/jobs/candidates/") and path.endswith("/submit"):
                try:
                    self._send_json(HTTPStatus.OK, self._candidate_submit(path, self._read_json()))
                except ServiceError as exc:
                    self._send_json(exc.status, {"status": "error", "message": str(exc)})
                return
            if path.startswith("/v1/jobs/retorrent/check/") and path.endswith("/submit"):
                try:
                    self._send_json(HTTPStatus.OK, self._retorrent_check_submit(path, self._read_json()))
                except ServiceError as exc:
                    self._send_json(exc.status, {"status": "error", "message": str(exc)})
                return
            if path.startswith("/v1/jobs/") and path.endswith("/resume"):
                try:
                    self._send_json(HTTPStatus.ACCEPTED, self._job_resume(path, self._read_optional_json()))
                except ServiceError as exc:
                    self._send_json(exc.status, {"status": "error", "message": str(exc)})
                return
            if path.startswith("/v1/jobs/") and path.endswith("/cancel"):
                try:
                    self._send_json(HTTPStatus.OK, self._job_cancel(path, self._read_optional_json()))
                except ServiceError as exc:
                    self._send_json(exc.status, {"status": "error", "message": str(exc)})
                return
            handler = handlers.get(path)
            if handler is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Endpoint not found."})
                return
            try:
                self._send_json(HTTPStatus.OK, handler(self._read_json()))
            except ServiceError as exc:
                self._send_json(exc.status, {"status": "error", "message": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "message": str(exc)})

        def _job_get(self, path: str) -> dict[str, Any]:
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["v1", "jobs"]:
                return job_store.get(parts[2])
            if len(parts) == 4 and parts[:2] == ["v1", "jobs"] and parts[3] == "summary":
                return job_store.summary(parts[2])
            raise ServiceError("Job endpoint not found.", status=HTTPStatus.NOT_FOUND)

        def _job_resume(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["v1", "jobs"] and parts[3] == "resume":
                return job_store.resume(parts[2], payload)
            raise ServiceError("Job resume endpoint not found.", status=HTTPStatus.NOT_FOUND)

        def _job_cancel(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["v1", "jobs"] and parts[3] == "cancel":
                return job_store.cancel(parts[2], payload)
            raise ServiceError("Job cancel endpoint not found.", status=HTTPStatus.NOT_FOUND)

        def _candidate_submit(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[:3] == ["v1", "jobs", "candidates"] and parts[4] == "submit":
                return create_candidate_retorrent_job(job_store, parts[3], payload)
            raise ServiceError("Candidate submit endpoint not found.", status=HTTPStatus.NOT_FOUND)

        def _retorrent_check_submit(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            parts = path.strip("/").split("/")
            if len(parts) == 6 and parts[:4] == ["v1", "jobs", "retorrent", "check"] and parts[5] == "submit":
                return create_retorrent_from_check_job(job_store, parts[4], payload)
            raise ServiceError("Retorrent check submit endpoint not found.", status=HTTPStatus.NOT_FOUND)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _authorized(self) -> bool:
            if not api_token:
                return True
            return self.headers.get("Authorization", "") == f"Bearer {api_token}"

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                raise ServiceError("JSON request body is required.")
            if length > 1024 * 1024:
                raise ServiceError("JSON request body is too large.", status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ServiceError(f"Invalid JSON request body: {exc.msg}") from exc
            if not isinstance(payload, dict):
                raise ServiceError("JSON request body must be an object.")
            return payload

        def _read_optional_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            if length > 1024 * 1024:
                raise ServiceError("JSON request body is too large.", status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ServiceError(f"Invalid JSON request body: {exc.msg}") from exc
            if not isinstance(payload, dict):
                raise ServiceError("JSON request body must be an object.")
            return payload

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", JSON_CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _request_base_url(self) -> str:
            configured = os.environ.get("PTCLI_PUBLIC_BASE_URL")
            if configured:
                return configured.rstrip("/")
            host = self.headers.get("Host") or f"{self.server.server_address[0]}:{self.server.server_address[1]}"
            proto = self.headers.get("X-Forwarded-Proto") or ("https" if self.headers.get("X-Forwarded-Ssl") == "on" else "http")
            return f"{proto}://{host}".rstrip("/")

    return PtcliServiceHandler


def create_retorrent_check_job(job_store: JobStore, request: dict[str, Any]) -> dict[str, Any]:
    _, normalized_request, argv = _pipeline_check_args(request)
    return job_store.create(
        "ptcli.retorrent_check",
        normalized_request,
        ["ptcli", *argv],
        lambda: asyncio.run(retorrent_check(request)),
    )


def create_retorrent_job(job_store: JobStore, request: dict[str, Any]) -> dict[str, Any]:
    execute = bool(request.get("execute") or request.get("execute_if_no_duplicate") or request.get("auto_retorrent"))
    if execute:
        source = _resolve_request_source(request)
        target_trackers = _target_trackers(request)
        request = _request_with_policy_qbit_defaults(request, source, target_trackers)
        _, normalized_request, argv = _retorrent_execute_args(request)
        kind = "ptcli.retorrent"
    else:
        _, normalized_request, argv = _pipeline_check_args(request)
        kind = "ptcli.retorrent_check"
    return job_store.create(
        kind,
        normalized_request,
        ["ptcli", *argv],
        lambda: asyncio.run(retorrent(request)),
    )


def create_manual_retorrent_job(job_store: JobStore, request: dict[str, Any]) -> dict[str, Any]:
    """Create the AI-facing manual retorrent job: source URL + target, then execute if gates allow."""
    return _create_ai_retorrent_job(job_store, request, kind="ptcli.manual_retorrent", mode="manual_retorrent")


def create_source_url_retorrent_job(job_store: JobStore, request: dict[str, Any]) -> dict[str, Any]:
    """Create a retorrent job from a source details URL plus target tracker."""
    source_url = request.get("source_url") or request.get("source") or request.get("source_link") or request.get("url")
    if not source_url:
        raise ServiceError("source_url or source is required.")
    effective = {**request, "source": str(source_url), "source_url": str(source_url)}
    return _create_ai_retorrent_job(job_store, effective, kind="ptcli.source_url_retorrent", mode="source_url_retorrent")


async def create_source_url_check_and_submit_job(job_store: JobStore, request: dict[str, Any]) -> dict[str, Any]:
    """Check target duplicates now, then create a live source-url job only when clear."""
    source_url = request.get("source_url") or request.get("source") or request.get("source_link") or request.get("url")
    if not source_url:
        raise ServiceError("source_url or source is required.")
    effective = {**request, "source": str(source_url), "source_url": str(source_url)}
    check_request = {**effective, "execute": False, "execute_if_no_duplicate": False, "auto_retorrent": False}
    check_result = await retorrent_check(check_request)
    duplicate_check = check_result.get("duplicate_check") if isinstance(check_result.get("duplicate_check"), dict) else {}
    handoff = check_result.get("submit_if_clear_handoff") if isinstance(check_result.get("submit_if_clear_handoff"), dict) else _submit_if_clear_handoff(check_result.get("request") if isinstance(check_result.get("request"), dict) else effective, duplicate_check, kind=str(check_result.get("kind") or "ptcli.service.retorrent_check"))
    blockers = _source_url_check_and_submit_blockers(check_result, handoff)
    if blockers:
        return _source_url_check_and_submit_response(
            check_result=check_result,
            handoff=handoff,
            submitted_job=None,
            blockers=blockers,
        )

    submit_request = handoff.get("request") if isinstance(handoff, dict) and isinstance(handoff.get("request"), dict) else None
    if not submit_request:
        blockers = ["submit_if_clear_handoff.request is missing."]
        return _source_url_check_and_submit_response(check_result=check_result, handoff=handoff, submitted_job=None, blockers=blockers)
    submit_request = {
        **submit_request,
        "check_submission": _inline_retorrent_check_submission_payload(check_result, handoff, submit_request),
    }
    submitted_job = create_source_url_retorrent_job(job_store, submit_request)
    return _source_url_check_and_submit_response(
        check_result=check_result,
        handoff=handoff,
        submitted_job=submitted_job,
        blockers=[],
    )


def source_url_retorrent_preflight_payload(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the AI-facing, no-mutation gate summary before creating a source-url job."""
    request = request if isinstance(request, dict) else {}
    source_url = request.get("source_url") or request.get("source") or request.get("source_link") or request.get("url")
    effective_request = dict(request)
    if source_url:
        effective_request["source_url"] = str(source_url)
        effective_request["source"] = str(source_url)
    readiness = readiness_bundle_payload(effective_request)
    live_readiness = readiness.get("live_readiness") if isinstance(readiness.get("live_readiness"), dict) else {}
    site_policies = readiness.get("site_policies") if isinstance(readiness.get("site_policies"), dict) else None
    policy_execution_summary = live_readiness.get("policy_execution_summary") if isinstance(live_readiness.get("policy_execution_summary"), dict) else None
    if policy_execution_summary is None and isinstance(site_policies, dict) and isinstance(site_policies.get("policy_execution_summary"), dict):
        policy_execution_summary = site_policies["policy_execution_summary"]
    policy_execution_handoff = live_readiness.get("policy_execution_handoff") if isinstance(live_readiness.get("policy_execution_handoff"), dict) else None
    if policy_execution_handoff is None and isinstance(site_policies, dict) and isinstance(site_policies.get("policy_execution_handoff"), dict):
        policy_execution_handoff = site_policies["policy_execution_handoff"]
    source_reference = live_readiness.get("source") if isinstance(live_readiness.get("source"), dict) else None
    target_trackers = live_readiness.get("target_trackers")
    manual_job_template = live_readiness.get("manual_job_template") if isinstance(live_readiness.get("manual_job_template"), dict) else None
    ready_to_create_job = bool(live_readiness.get("ready_for_manual_retorrent")) and bool(manual_job_template)
    duplicate_check = _source_url_preflight_duplicate_check(request, source_reference, target_trackers, manual_job_template, ready_to_create_job)
    duplicate_check_handoff = _source_url_preflight_duplicate_handoff(duplicate_check, manual_job_template, ready_to_create_job)
    next_step = _source_url_preflight_next_step(ready_to_create_job, readiness, duplicate_check_handoff)
    blockers = _source_url_preflight_blockers(readiness, source_reference, target_trackers)
    status = "ok" if ready_to_create_job else "blocked"
    return {
        "kind": "ptcli.source_url_retorrent_preflight",
        "status": status,
        "ok": ready_to_create_job,
        "ready": ready_to_create_job,
        "dry_run": True,
        "mutates_state": False,
        "live_upload": False,
        "request": {
            "source_url": str(source_url) if source_url else None,
            "source_tracker": request.get("source_tracker") or request.get("from"),
            "target": target_trackers,
            "accept_rules": _truthy(request.get("accept_rules")),
            "confirm_upload": _truthy(request.get("confirm_upload")),
            "save_path": request.get("save_path"),
            "path": request.get("path") or request.get("content_path"),
        },
        "source_reference": source_reference,
        "target_trackers": target_trackers,
        "ready_to_create_job": ready_to_create_job,
        "ready_for_live_upload": ready_to_create_job,
        "duplicate_check": duplicate_check,
        "duplicate_check_handoff": duplicate_check_handoff,
        "readiness_bundle": readiness,
        "policy_execution_summary": policy_execution_summary,
        "policy_execution_handoff": policy_execution_handoff,
        "job_template": manual_job_template,
        "job_creation_handoff": _source_url_preflight_job_creation_handoff(manual_job_template, ready_to_create_job),
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "agent_decision": _source_url_preflight_agent_decision(ready_to_create_job, duplicate_check_handoff, next_step, blockers),
        "blockers": blockers,
        "warnings": _string_list(readiness.get("warnings")),
        "next_actions": _source_url_preflight_next_actions(ready_to_create_job, next_step, blockers),
        "safety": {
            "does_not_create_job": True,
            "does_not_contact_trackers": True,
            "does_not_contact_qbittorrent": True,
            "live_job_requires": ["accept_rules=true", "confirm_upload=true", "site policy ready", "target duplicate check clear"],
        },
    }


def _source_url_preflight_duplicate_check(
    request: dict[str, Any],
    source_reference: dict[str, Any] | None,
    target_trackers: Any,
    manual_job_template: dict[str, Any] | None,
    ready_to_create_job: bool,
) -> dict[str, Any]:
    check_request = _source_url_preflight_duplicate_check_request(request, source_reference, target_trackers, manual_job_template) if ready_to_create_job else None
    return {
        "searched": False,
        "status": "not_checked",
        "exists": None,
        "count": None,
        "dupes": [],
        "reason": "source_url_preflight_does_not_contact_trackers",
        "ready_to_check": bool(check_request),
        "next_tool": "retorrent_check",
        "next_endpoint": "/v1/retorrent/check",
        "next_request": check_request,
        "continue_when": "duplicate_check.searched=true and duplicate_check.exists=false",
        "stop_when": "duplicate_check.exists=true",
    }


def _source_url_preflight_duplicate_check_request(
    request: dict[str, Any],
    source_reference: dict[str, Any] | None,
    target_trackers: Any,
    manual_job_template: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not source_reference or source_reference.get("error") or not target_trackers:
        return None
    job_request = manual_job_template.get("request") if isinstance(manual_job_template, dict) and isinstance(manual_job_template.get("request"), dict) else {}
    source_url = job_request.get("source_url") or source_reference.get("details_url") or source_reference.get("requested_source") or request.get("source_url") or request.get("source")
    if not source_url:
        return None
    check_request: dict[str, Any] = {
        "source": str(source_url),
        "source_url": str(source_url),
        "source_tracker": source_reference.get("tracker") or request.get("source_tracker") or request.get("from"),
        "target": target_trackers,
        "accept_rules": _truthy(request.get("accept_rules")),
        "confirm_upload": _truthy(request.get("confirm_upload")),
    }
    for key in ("config", "base_dir", "client", "path", "content_path", "save_path", "metadata_file", "imdb_id", "tmdb_id", "tmdb_type", "douban_id", "douban_url"):
        if request.get(key) is not None:
            check_request[key] = request[key]
    return {key: value for key, value in check_request.items() if value is not None}


def _source_url_preflight_duplicate_handoff(duplicate_check: dict[str, Any], manual_job_template: dict[str, Any] | None, ready_to_create_job: bool) -> dict[str, Any]:
    return {
        "ready": bool(ready_to_create_job and duplicate_check.get("next_request")),
        "tool": "retorrent_check",
        "endpoint": "/v1/retorrent/check",
        "method": "POST",
        "request": duplicate_check.get("next_request"),
        "read": ["duplicate_check.searched", "duplicate_check.exists", "duplicate_check.count", "duplicate_check.dupes", "blockers", "next_actions"],
        "continue_when": duplicate_check.get("continue_when"),
        "stop_when": duplicate_check.get("stop_when"),
        "then_tool": "source_url_retorrent_job",
        "then_endpoint": "/v1/jobs/retorrent/from-url",
        "then_request": manual_job_template.get("request") if isinstance(manual_job_template, dict) else None,
    }


def _source_url_preflight_job_creation_handoff(manual_job_template: dict[str, Any] | None, ready_to_create_job: bool) -> dict[str, Any]:
    return {
        "ready_after_duplicate_clear": bool(ready_to_create_job and isinstance(manual_job_template, dict)),
        "tool": "source_url_retorrent_job",
        "endpoint": "/v1/jobs/retorrent/from-url",
        "method": "POST",
        "request": manual_job_template.get("request") if isinstance(manual_job_template, dict) else None,
        "requires_before_call": ["duplicate_check.searched=true", "duplicate_check.exists=false", "accept_rules=true", "confirm_upload=true"],
    }


def _source_url_preflight_next_step(ready_to_create_job: bool, readiness: dict[str, Any], duplicate_check_handoff: dict[str, Any]) -> dict[str, Any]:
    if ready_to_create_job and duplicate_check_handoff.get("ready"):
        return {
            "tool": "retorrent_check",
            "endpoint": "/v1/retorrent/check",
            "method": "POST",
            "request": duplicate_check_handoff.get("request"),
            "reason": "check_target_duplicates_before_job_creation",
        }
    next_step = readiness.get("next_step") if isinstance(readiness.get("next_step"), dict) else {}
    if next_step:
        return {**next_step, "reason": next_step.get("reason") or "source_url_preflight_blocked"}
    return {
        "tool": "readiness_bundle",
        "endpoint": "/v1/readiness/bundle",
        "method": "POST",
        "request": readiness.get("request"),
        "reason": "inspect_readiness_bundle",
    }


def _source_url_preflight_blockers(readiness: dict[str, Any], source_reference: dict[str, Any] | None, target_trackers: Any) -> list[str]:
    blockers = _string_list(readiness.get("blockers"))
    if not source_reference:
        blockers.append("source_url is required before creating a source-url retorrent job.")
    elif source_reference.get("error"):
        blockers.append(f"source_url could not be resolved: {source_reference.get('error')}")
    if not target_trackers:
        blockers.append("target is required before creating a source-url retorrent job.")
    return list(dict.fromkeys(blockers))


def _source_url_preflight_next_actions(ready_to_create_job: bool, next_step: dict[str, Any], blockers: list[str]) -> list[str]:
    if ready_to_create_job:
        return ["Call duplicate_check_handoff.request with retorrent_check; if duplicate_check.exists=false, submit job_creation_handoff.request to source_url_retorrent_job."]
    if next_step.get("tool"):
        return [f"Call next_step with {next_step['tool']} after resolving source_url_preflight.blockers."]
    if blockers:
        return ["Resolve source_url_preflight.blockers before creating a live-capable retorrent job."]
    return ["Inspect readiness_bundle and source_url_preflight before creating a live-capable retorrent job."]


def _source_url_preflight_agent_decision(ready_to_create_job: bool, duplicate_check_handoff: dict[str, Any], next_step: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    if ready_to_create_job and duplicate_check_handoff.get("ready"):
        decision = "check_duplicates_before_job"
        recommended_action = "Run retorrent_check with duplicate_check_handoff.request; create the source-url job only when duplicate_check.exists=false."
    else:
        decision = "resolve_preflight_blockers"
        recommended_action = "Resolve preflight blockers before checking duplicates or creating a live-capable job."
    return {
        "workflow": "source_url_retorrent",
        "decision": decision,
        "recommended_action": recommended_action,
        "can_check_duplicates": bool(duplicate_check_handoff.get("ready")),
        "can_create_job_after_duplicate_clear": bool(ready_to_create_job),
        "next_tool": next_step.get("tool"),
        "next_endpoint": next_step.get("endpoint"),
        "blockers": blockers,
    }


def create_candidate_retorrent_job(job_store: JobStore, candidate_job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Create a retorrent job from a ranked item in a completed daily-candidates job."""
    candidate_job = job_store._read(candidate_job_id)
    candidate_status = str(candidate_job.get("status") or "")
    if candidate_status in {"queued", "running"}:
        raise ServiceError(f"Candidate job {candidate_job_id} is still {candidate_status}; poll it before submitting a candidate.", status=HTTPStatus.CONFLICT)
    digest = _candidate_digest_from_payload(candidate_job.get("result"))
    if not digest:
        raise ServiceError(f"Job {candidate_job_id} does not contain a daily candidate digest.", status=HTTPStatus.BAD_REQUEST)
    candidate_item = _candidate_submit_item(digest, request)
    submit_request = candidate_item.get("submit_request") if isinstance(candidate_item.get("submit_request"), dict) else None
    if not submit_request:
        raise ServiceError("Selected candidate is not submittable; inspect push_items[].blockers before creating a live retorrent job.", status=HTTPStatus.CONFLICT)
    submit_overrides = _candidate_submit_overrides(request)
    effective_request = {**submit_request, **submit_overrides}
    effective_request["candidate_submission"] = _candidate_submission_payload(candidate_job_id, candidate_item, digest, submit_request, submit_overrides, effective_request)
    return _create_ai_retorrent_job(job_store, effective_request, kind="ptcli.candidate_retorrent", mode="candidate_retorrent")


def create_retorrent_from_check_job(job_store: JobStore, check_job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Create a live retorrent job only after a completed duplicate-check job is clear."""
    check_job = job_store._read(check_job_id)
    check_status = str(check_job.get("status") or "")
    if check_status in {"queued", "running"}:
        raise ServiceError(f"Check job {check_job_id} is still {check_status}; poll it before submitting a retorrent job.", status=HTTPStatus.CONFLICT)
    handoff = _job_submit_if_clear_handoff(check_job)
    if not isinstance(handoff, dict):
        raise ServiceError(f"Job {check_job_id} does not expose submit_if_clear_handoff.", status=HTTPStatus.BAD_REQUEST)
    if handoff.get("ready") is not True:
        blockers = ", ".join(_string_list(handoff.get("blockers"))) or "duplicate check is not clear"
        raise ServiceError(f"Check job {check_job_id} is not ready to submit: {blockers}", status=HTTPStatus.CONFLICT)
    submit_request = handoff.get("request") if isinstance(handoff.get("request"), dict) else None
    if not submit_request:
        raise ServiceError(f"Job {check_job_id} does not contain a submit request.", status=HTTPStatus.BAD_REQUEST)
    submit_overrides = _candidate_submit_overrides(request)
    effective_request = {**submit_request, **submit_overrides}
    effective_request["check_submission"] = _retorrent_check_submission_payload(check_job_id, check_job, handoff, submit_overrides, effective_request)
    return _create_ai_retorrent_job(job_store, effective_request, kind="ptcli.checked_retorrent", mode="checked_retorrent")


def _retorrent_check_submission_payload(check_job_id: str, check_job: dict[str, Any], handoff: dict[str, Any], submit_overrides: dict[str, Any], effective_request: dict[str, Any]) -> dict[str, Any]:
    qbit_keys = ("qbit_category", "qbit_tags", "qbit_upload_limit", "qbit_download_limit", "uploaded_qbit_category", "uploaded_qbit_tags", "uploaded_qbit_upload_limit", "uploaded_qbit_download_limit")
    return {
        "check_job_id": check_job_id,
        "check_status": check_job.get("status"),
        "check_kind": check_job.get("kind"),
        "check_summary_file": _job_summary_file(check_job),
        "duplicate_check": _job_duplicate_check(check_job),
        "inherited_request": handoff.get("request") if isinstance(handoff.get("request"), dict) else {},
        "submitted_overrides": submit_overrides,
        "material_options": _request_material_options(effective_request),
        "qbit_overrides": {key: effective_request.get(key) for key in qbit_keys if effective_request.get(key) is not None},
    }


def _inline_retorrent_check_submission_payload(check_result: dict[str, Any], handoff: dict[str, Any], effective_request: dict[str, Any]) -> dict[str, Any]:
    qbit_keys = ("qbit_category", "qbit_tags", "qbit_upload_limit", "qbit_download_limit", "uploaded_qbit_category", "uploaded_qbit_tags", "uploaded_qbit_upload_limit", "uploaded_qbit_download_limit")
    return {
        "mode": "inline_check_and_submit",
        "check_kind": check_result.get("kind"),
        "check_status": check_result.get("status"),
        "check_ok": check_result.get("ok"),
        "check_summary_file": check_result.get("summary_file"),
        "duplicate_check": check_result.get("duplicate_check"),
        "inherited_request": handoff.get("request") if isinstance(handoff, dict) else {},
        "material_options": _request_material_options(effective_request),
        "qbit_overrides": {key: effective_request.get(key) for key in qbit_keys if effective_request.get(key) is not None},
    }


def _source_url_check_and_submit_blockers(check_result: dict[str, Any], handoff: dict[str, Any] | None) -> list[str]:
    blockers = _string_list(check_result.get("blockers"))
    if check_result.get("ok") is False and not isinstance(handoff, dict):
        blockers.append("retorrent_check did not finish successfully.")
    if not isinstance(handoff, dict):
        blockers.append("submit_if_clear_handoff is missing.")
        return list(dict.fromkeys(blockers))
    blockers.extend(_string_list(handoff.get("blockers")))
    if handoff.get("ready") is not True:
        blockers.append("submit_if_clear_handoff.ready is not true.")
    return list(dict.fromkeys(blockers))


def _source_url_check_and_submit_response(
    *,
    check_result: dict[str, Any],
    handoff: dict[str, Any] | None,
    submitted_job: dict[str, Any] | None,
    blockers: list[str],
) -> dict[str, Any]:
    duplicate_check = check_result.get("duplicate_check") if isinstance(check_result.get("duplicate_check"), dict) else {}
    ready = bool(submitted_job and not blockers)
    return {
        "kind": "ptcli.source_url_check_and_submit",
        "status": "ok" if ready else "blocked",
        "ok": ready,
        "mutates_state": True,
        "live_upload": bool(ready),
        "check_result": check_result,
        "duplicate_check": duplicate_check,
        "submit_if_clear_handoff": handoff,
        "job_id": submitted_job.get("job_id") if isinstance(submitted_job, dict) else None,
        "submitted_job": submitted_job,
        "status_endpoint": f"/v1/jobs/{submitted_job.get('job_id')}" if isinstance(submitted_job, dict) and submitted_job.get("job_id") else None,
        "summary_endpoint": f"/v1/jobs/{submitted_job.get('job_id')}/summary" if isinstance(submitted_job, dict) and submitted_job.get("job_id") else None,
        "agent_summary": _source_url_check_and_submit_agent_summary(duplicate_check, handoff, submitted_job, blockers),
        "blockers": blockers,
        "next_actions": _source_url_check_and_submit_next_actions(duplicate_check, handoff, submitted_job, blockers),
    }


def _source_url_check_and_submit_agent_summary(duplicate_check: dict[str, Any], handoff: dict[str, Any] | None, submitted_job: dict[str, Any] | None, blockers: list[str]) -> dict[str, Any]:
    return {
        "ready": bool(submitted_job and not blockers),
        "duplicate_searched": duplicate_check.get("searched") is True,
        "duplicate_exists": duplicate_check.get("exists"),
        "duplicate_count": duplicate_check.get("count"),
        "submit_ready": isinstance(handoff, dict) and handoff.get("ready") is True,
        "job_id": submitted_job.get("job_id") if isinstance(submitted_job, dict) else None,
        "job_status": submitted_job.get("status") if isinstance(submitted_job, dict) else None,
        "blocker_count": len(blockers),
    }


def _source_url_check_and_submit_next_actions(duplicate_check: dict[str, Any], handoff: dict[str, Any] | None, submitted_job: dict[str, Any] | None, blockers: list[str]) -> list[str]:
    if submitted_job and not blockers:
        job_id = submitted_job.get("job_id")
        return [f"Poll get_job_status at /v1/jobs/{job_id}; when terminal, read /v1/jobs/{job_id}/summary and follow closure_summary.next_step."]
    if duplicate_check.get("exists") is True:
        return ["Do not upload; target duplicate exists. Report duplicate_check.dupes to the user."]
    if isinstance(handoff, dict) and _string_list(handoff.get("blockers")):
        return ["Resolve submit_if_clear_handoff.blockers, then retry check-and-submit or use submit_checked_retorrent_job after a clear check job."]
    return ["Inspect check_result.blockers and duplicate_check before retrying source_url_check_and_submit."]


def _candidate_submission_payload(candidate_job_id: str, candidate_item: dict[str, Any], digest: dict[str, Any], submit_request: dict[str, Any], submit_overrides: dict[str, Any], effective_request: dict[str, Any]) -> dict[str, Any]:
    inherited_keys = ("source", "source_url", "source_tracker", "target", "target_tracker", "target_trackers")
    qbit_keys = ("qbit_category", "qbit_tags", "qbit_upload_limit", "qbit_download_limit", "uploaded_qbit_category", "uploaded_qbit_tags", "uploaded_qbit_upload_limit", "uploaded_qbit_download_limit")
    policy_execution_handoff = _candidate_item_policy_execution_handoff(candidate_item)
    return {
        "candidate_job_id": candidate_job_id,
        "candidate_rank": candidate_item.get("rank"),
        "candidate_source_id": candidate_item.get("source_id"),
        "candidate_title": candidate_item.get("title"),
        "candidate_summary_text": candidate_item.get("summary_text"),
        "candidate_digest_kind": digest.get("kind"),
        "inherited_request": {key: submit_request.get(key) for key in inherited_keys if submit_request.get(key) is not None},
        "submitted_overrides": submit_overrides,
        "material_options": _request_material_options(effective_request),
        "qbit_overrides": {key: effective_request.get(key) for key in qbit_keys if effective_request.get(key) is not None},
        "policy_execution_handoff": policy_execution_handoff,
    }


def _candidate_item_policy_execution_handoff(candidate_item: dict[str, Any]) -> dict[str, Any]:
    if isinstance(candidate_item.get("policy_execution_handoff"), dict):
        return candidate_item["policy_execution_handoff"]
    policy_summary = candidate_item.get("policy_summary") if isinstance(candidate_item.get("policy_summary"), dict) else {}
    if isinstance(policy_summary.get("policy_execution_handoff"), dict):
        return policy_summary["policy_execution_handoff"]
    policy_execution = candidate_item.get("policy_execution") if isinstance(candidate_item.get("policy_execution"), dict) else {}
    if isinstance(policy_execution.get("policy_execution_handoff"), dict):
        return policy_execution["policy_execution_handoff"]
    return {}


def _create_ai_retorrent_job(job_store: JobStore, request: dict[str, Any], *, kind: str, mode: str) -> dict[str, Any]:
    effective_request = {**request, "execute": True, "execute_if_no_duplicate": True, "manual_retorrent": True}
    source = _resolve_request_source(effective_request)
    target_trackers = _target_trackers(effective_request)
    effective_request = _request_with_policy_qbit_defaults(effective_request, source, target_trackers)
    _, normalized_request, argv = _retorrent_execute_args(effective_request)
    normalized_request = {**normalized_request, "mode": mode, "execute_if_no_duplicate": True}
    if isinstance(effective_request.get("candidate_submission"), dict):
        normalized_request["candidate_submission"] = effective_request["candidate_submission"]
    if isinstance(effective_request.get("check_submission"), dict):
        normalized_request["check_submission"] = effective_request["check_submission"]
    return job_store.create(
        kind,
        normalized_request,
        ["ptcli", *argv],
        lambda: asyncio.run(retorrent(effective_request)),
    )


def create_daily_candidates_job(job_store: JobStore, request: dict[str, Any]) -> dict[str, Any]:
    normalized_request = _candidate_request_context(request)
    return job_store.create(
        "ptcli.daily_candidates",
        normalized_request,
        ["ptcli-service", "daily-candidates"],
        lambda: asyncio.run(daily_candidates(request)),
    )


def create_daily_candidate_schedule_jobs(job_store: JobStore, request: dict[str, Any]) -> dict[str, Any]:
    """Create daily candidate jobs for each enabled schedule entry; never performs uploads."""
    plan = daily_candidate_schedule_payload(request)
    jobs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    blockers = _string_list(plan.get("blockers"))
    for schedule in plan.get("schedules", []):
        if not isinstance(schedule, dict):
            continue
        schedule_blockers = _string_list(schedule.get("blockers"))
        if schedule.get("enabled") is not True or schedule_blockers:
            skipped.append({"name": schedule.get("name"), "blockers": schedule_blockers or ["schedule is disabled"]})
            continue
        job = create_daily_candidates_job(job_store, schedule.get("job_request") if isinstance(schedule.get("job_request"), dict) else {})
        jobs.append(
            {
                "schedule_name": schedule.get("name"),
                "job_id": job.get("job_id"),
                "status": job.get("status"),
                "ok": job.get("ok"),
                "job_endpoint": schedule.get("job_endpoint"),
                "status_endpoint": f"/v1/jobs/{job.get('job_id')}",
                "summary_endpoint": f"/v1/jobs/{job.get('job_id')}/summary",
                "job_request": schedule.get("job_request"),
                "candidate_digest": job.get("candidate_digest"),
                "agent_decision": job.get("agent_decision"),
            }
        )
    if not jobs and not blockers:
        blockers.append("No enabled daily candidate schedules were available to run.")
    schedule_digest = _daily_candidate_schedule_job_digest(jobs, skipped, blockers)
    agent_decision = _daily_candidate_schedule_job_decision(schedule_digest, blockers)
    notification_payload = _daily_candidate_schedule_notification_payload(schedule_digest, agent_decision)
    delivery_handoff = _daily_candidate_schedule_delivery_handoff(schedule_digest, notification_payload, agent_decision, blockers)
    return {
        "kind": "ptcli.daily_candidate_schedule_jobs",
        "status": "ok" if jobs and not blockers else "partial" if jobs else "blocked",
        "ok": bool(jobs),
        "plan": plan,
        "job_count": len(jobs),
        "jobs": jobs,
        "skipped": skipped,
        "schedule_digest": schedule_digest,
        "notification_payload": notification_payload,
        "delivery_handoff": delivery_handoff,
        "agent_decision": agent_decision,
        "blockers": blockers,
        "next_actions": _daily_candidate_schedule_run_next_actions(jobs, skipped, blockers),
    }


async def retorrent_check(request: dict[str, Any]) -> dict[str, Any]:
    """Check source metadata and target duplicates without uploading."""
    args, normalized_request, argv = _pipeline_check_args(request)
    started_at = time.time()
    result = await pipeline_payload(args)
    return _service_result(
        kind="ptcli.service.retorrent_check",
        request=normalized_request,
        argv=argv,
        result=result,
        started_at=started_at,
    )


async def retorrent(request: dict[str, Any]) -> dict[str, Any]:
    """Run a one-call retorrent request, or fall back to a duplicate check."""
    execute = bool(request.get("execute") or request.get("execute_if_no_duplicate") or request.get("auto_retorrent"))
    if not execute:
        return await retorrent_check(request)
    args, normalized_request, argv = _retorrent_execute_args(request)
    started_at = time.time()
    result = await retorrent_payload(args)
    return _service_result(
        kind="ptcli.service.retorrent",
        request=normalized_request,
        argv=argv,
        result=result,
        started_at=started_at,
    )


async def daily_candidates(request: dict[str, Any]) -> dict[str, Any]:
    """Return daily retorrent candidates for a source/target pair."""
    started_at = time.time()
    context = _candidate_request_context(request)
    config = load_config(context.get("config"))
    result = await build_daily_candidates(
        config,
        str(context["source_tracker"]),
        str(context["target_trackers"]),
        limit=int(context["limit"]),
        base_dir=context.get("base_dir"),
        accept_rules=bool(context.get("accept_rules")),
        check_dupes=bool(context.get("check_dupes")),
    )
    return {
        "kind": "ptcli.service.daily_candidates",
        "status": result.get("status", "ok"),
        "ok": bool(result.get("ok")),
        "request": context,
        "blockers": _result_blockers(result),
        "next_actions": _string_list(result.get("next_actions")),
        "elapsed_seconds": round(time.time() - started_at, 3),
        "result": result,
        "site_policy": result.get("site_policy"),
        "ranking": result.get("ranking"),
        "digest": result.get("digest"),
        "candidates": result.get("candidates", []),
        "count": result.get("count", 0),
        "target_count": result.get("target_count"),
        "scan_count": result.get("scan_count"),
        "ready_count": result.get("ready_count", 0),
        "shortfall_count": result.get("shortfall_count"),
        "target_met": result.get("target_met"),
        "target_summary": result.get("target_summary"),
    }


def daily_candidate_schedule_payload(request: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized daily candidate schedule plan without executing jobs."""
    raw_schedules = request.get("schedules") if isinstance(request, dict) else None
    source = "request"
    if raw_schedules is None:
        raw_schedules = _daily_candidate_schedules_from_env()
        source = "env"
    schedules: list[dict[str, Any]] = []
    blockers: list[str] = []
    for index, raw_schedule in enumerate(_schedule_list(raw_schedules)):
        if not isinstance(raw_schedule, dict):
            blockers.append(f"schedule[{index}] must be an object.")
            continue
        try:
            schedules.append(_normalized_daily_candidate_schedule(raw_schedule, index=index))
        except ServiceError as exc:
            blockers.append(f"schedule[{index}]: {exc}")
    if not schedules and not blockers:
        blockers.append(f"No daily candidate schedules configured. Set {DAILY_CANDIDATE_SCHEDULE_ENV} or POST schedules.")
    return {
        "kind": "ptcli.daily_candidate_schedule",
        "status": "ok" if schedules and not blockers else "partial" if schedules else "blocked",
        "ok": bool(schedules),
        "source": source,
        "env": DAILY_CANDIDATE_SCHEDULE_ENV,
        "count": len(schedules),
        "schedules": schedules,
        "blockers": blockers,
        "next_actions": _daily_candidate_schedule_next_actions(schedules, blockers),
    }


def sites_payload(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return Chinese PT site adapter/profile capabilities for AI and extension work."""
    request = request or {}
    context = _sites_request_context(request)
    config = load_config(context.get("config"))
    base = build_sites_payload()
    capabilities = base.get("capabilities") if isinstance(base.get("capabilities"), dict) else {}
    report = build_site_policy_report(config, context["trackers"], accept_rules=bool(context.get("accept_rules")))
    roles = context.get("roles") if isinstance(context.get("roles"), dict) else {}
    policy_matrix = [
        _site_policy_matrix_item(policy, roles=_string_list(roles.get(str(policy.get("tracker")))), accept_rules=bool(report.get("accept_rules")))
        for policy in report.get("site_policies", [])
        if isinstance(policy, dict)
    ]
    policy_gap_summary = _site_policy_gap_summary(policy_matrix)
    execution_readiness = _site_policy_execution_readiness(policy_matrix, report)
    policy_handoff = _site_policy_handoff(policy_matrix, policy_gap_summary, execution_readiness, report, context)
    policy_by_tracker = {str(item.get("tracker")): item for item in policy_matrix if item.get("tracker")}
    capability_matrix = [
        _site_capability_matrix_item(tracker, capabilities.get(tracker) if isinstance(capabilities.get(tracker), dict) else {}, policy_by_tracker.get(tracker))
        for tracker in context["trackers"]
    ]
    flow_matrix = _site_flow_matrix(base.get("flows") if isinstance(base.get("flows"), list) else [], context["trackers"])
    blockers = _sites_payload_blockers(capability_matrix, context)
    extension_plan = _sites_extension_plan(capability_matrix, policy_matrix, flow_matrix, context)
    extension_handoff = _sites_extension_handoff(extension_plan, flow_matrix, context)
    return {
        "kind": "ptcli.sites",
        "status": "ok" if not blockers else "blocked",
        "ok": not blockers,
        "ready": not blockers,
        "request": context,
        "sites": context["trackers"],
        "all_sites": base.get("sites", []),
        "capability_matrix": capability_matrix,
        "adapter_profiles": {str(item["tracker"]): item["adapter_profile"] for item in capability_matrix if item.get("tracker")},
        "policy_matrix": policy_matrix,
        "policy_gap_summary": policy_gap_summary,
        "policy_execution_summary": _site_policy_execution_summary(policy_matrix, policy_gap_summary, execution_readiness, policy_handoff, report),
        "extension_plan": extension_plan,
        "extension_handoff": extension_handoff,
        "flow_matrix": flow_matrix,
        "reference_flows": base.get("flows", []),
        "source_info_trackers": base.get("source_info_trackers", []),
        "source_download_trackers": base.get("source_download_trackers", []),
        "target_upload_trackers": base.get("target_upload_trackers", []),
        "mteam_flow_sources": base.get("mteam_flow_sources", []),
        "full_live_closure_sources": base.get("full_live_closure_sources", []),
        "agent_summary": _sites_agent_summary(capability_matrix, policy_matrix, flow_matrix, blockers, extension_plan),
        "blockers": blockers,
        "next_actions": _sites_next_actions(blockers, capability_matrix, extension_plan),
    }


async def qbit_inspect_payload(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return qBittorrent torrent state without mutating the client."""
    request = request or {}
    context = _qbit_inspect_request_context(request)
    client_name, client_config = resolve_client_config(load_config(context.get("config")), context["client"])
    service = QbitReadOnlyService(client_config)
    torrents = await service.list_torrents(torrent_hash=context.get("hash"), limit=context.get("limit"))
    torrent_dicts = summaries_to_dicts(torrents)
    return {
        "kind": "ptcli.qbit_inspect",
        "status": "ok",
        "ok": True,
        "read_only": True,
        "client": client_name,
        "request": context,
        "count": len(torrent_dicts),
        "torrents": torrent_dicts,
        "agent_summary": _qbit_inspect_agent_summary(torrent_dicts, context),
        "blockers": [],
        "next_actions": _qbit_inspect_next_actions(torrent_dicts, context),
    }


async def qbit_match_payload(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return qBittorrent torrents matching a seedbox content path."""
    request = request or {}
    context = _qbit_match_request_context(request)
    client_name, client_config = resolve_client_config(load_config(context.get("config")), context["client"])
    service = QbitReadOnlyService(client_config)
    torrents = await service.list_torrents()
    matches = match_torrents(torrents, context["path"])
    match_dicts = summaries_to_dicts(matches)
    blockers = [] if match_dicts else [f"No qBittorrent torrent matched path {context['path']}."]
    return {
        "kind": "ptcli.qbit_match",
        "status": "ok" if match_dicts else "blocked",
        "ok": bool(match_dicts),
        "read_only": True,
        "client": client_name,
        "request": context,
        "path": context["path"],
        "count": len(match_dicts),
        "matched": bool(match_dicts),
        "matches": match_dicts,
        "agent_summary": _qbit_match_agent_summary(match_dicts, context, blockers),
        "blockers": blockers,
        "next_actions": _qbit_match_next_actions(match_dicts, context),
    }


async def qbit_export_payload(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Export a qBittorrent .torrent and create a target upload candidate."""
    request = request or {}
    context = _qbit_export_request_context(request)
    client_name, client_config = resolve_client_config(load_config(context.get("config")), context["client"])
    service = QbitReadOnlyService(client_config)
    exported_path = await service.export_torrent(context["hash"], context["output_dir"])
    candidate = None
    if context["sanitize_for"] == "MTEAM":
        candidate = await asyncio.to_thread(create_mteam_upload_torrent_candidate, str(exported_path), context["output_dir"])
    target_torrent_file = str(candidate.get("path")) if isinstance(candidate, dict) and candidate.get("path") else str(exported_path)
    evidence = _qbit_export_file_evidence(exported_path, candidate)
    return {
        "kind": "ptcli.qbit_export",
        "status": "ok",
        "ok": True,
        "read_only_client": True,
        "mutates_filesystem": True,
        "client": client_name,
        "request": context,
        "hash": context["hash"],
        "exported_path": str(exported_path),
        "path": target_torrent_file,
        "target_torrent_file": target_torrent_file,
        "candidate": candidate,
        "evidence": evidence,
        "target_upload_handoff": _qbit_export_target_upload_handoff(target_torrent_file, context, evidence),
        "agent_summary": _qbit_export_agent_summary(target_torrent_file, context, evidence),
        "blockers": [],
        "next_actions": ["Use target_torrent_file as target_torrent_file for source_url_retorrent_job, retorrent_job, pipeline, or target-upload after duplicate/rule gates are ready."],
    }


async def qbit_inject_payload(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add a .torrent file to qBittorrent and verify it is visible."""
    request = request or {}
    context = _qbit_inject_request_context(request)
    client_name, client_config = resolve_client_config(load_config(context.get("config")), context["client"])
    service = QbitReadOnlyService(client_config)
    try:
        result = await service.add_torrent_file(
            torrent_path=context["torrent_file"],
            save_path=context["save_path"],
            category=context.get("category"),
            tags=context.get("tags"),
            upload_limit=context.get("upload_limit"),
            download_limit=context.get("download_limit"),
            paused=context["paused"],
            skip_checking=context["skip_checking"],
            verify_timeout=context["verify_timeout"],
            verify_interval=context["verify_interval"],
        )
    except ValueError as exc:
        raise ServiceError(str(exc), status=HTTPStatus.BAD_REQUEST) from exc

    blockers = _qbit_inject_blockers(result)
    return {
        "kind": "ptcli.qbit_inject",
        "status": "ok" if not blockers else "blocked",
        "ok": not blockers,
        "mutates_qbittorrent": True,
        "live_upload": False,
        "client": client_name,
        "request": context,
        **result,
        "agent_summary": _qbit_inject_agent_summary(result, context, blockers),
        "blockers": blockers,
        "next_actions": _qbit_inject_next_actions(blockers),
    }


async def qbit_wait_payload(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Wait for a qBittorrent torrent to complete by hash or content path."""
    request = request or {}
    context = _qbit_wait_request_context(request)
    client_name, client_config = resolve_client_config(load_config(context.get("config")), context["client"])
    service = QbitReadOnlyService(client_config)
    try:
        result = await service.wait_for_completion(
            torrent_hash=context.get("hash"),
            content_path=context.get("path"),
            timeout=context["timeout"],
            interval=context["interval"],
        )
    except ValueError as exc:
        raise ServiceError(str(exc), status=HTTPStatus.BAD_REQUEST) from exc

    blockers = _string_list(result.get("blockers"))
    ready = result.get("complete") is True and not blockers
    return {
        "kind": "ptcli.qbit_wait",
        "status": "ok" if ready else "blocked",
        "ok": ready,
        "read_only": True,
        "client": client_name,
        "request": context,
        **result,
        "agent_summary": _qbit_wait_agent_summary(result, context, blockers),
        "blockers": blockers,
        "next_actions": _qbit_wait_next_actions(result, blockers),
    }


def site_policies_payload(request: dict[str, Any]) -> dict[str, Any]:
    """Return a tracker policy matrix for AI-safe automation decisions."""
    context = _site_policy_request_context(request)
    config = load_config(context.get("config"))
    report = build_site_policy_report(config, context["trackers"], accept_rules=bool(context.get("accept_rules")))
    roles = context.get("roles") if isinstance(context.get("roles"), dict) else {}
    matrix = [
        _site_policy_matrix_item(policy, roles=_string_list(roles.get(str(policy.get("tracker")))), accept_rules=bool(report.get("accept_rules")))
        for policy in report.get("site_policies", [])
        if isinstance(policy, dict)
    ]
    policy_gap_summary = _site_policy_gap_summary(matrix)
    execution_readiness = _site_policy_execution_readiness(matrix, report)
    policy_handoff = _site_policy_handoff(matrix, policy_gap_summary, execution_readiness, report, context)
    policy_execution_summary = _site_policy_execution_summary(matrix, policy_gap_summary, execution_readiness, policy_handoff, report)
    policy_setup_summary = _site_policy_setup_summary(matrix, policy_gap_summary, execution_readiness, policy_handoff, report)
    policy_execution_handoff = _site_policy_execution_handoff(policy_execution_summary, policy_handoff, policy_setup_summary, context)
    overall_ready = bool(report.get("ready")) and bool(policy_setup_summary.get("ready"))
    rule_obligations = {str(item.get("tracker")): item.get("rule_obligations") for item in matrix if item.get("tracker")}
    return {
        "kind": "ptcli.site_policies",
        "status": "ok" if overall_ready else "blocked",
        "ok": overall_ready,
        "ready": overall_ready,
        "request": context,
        "policy_matrix": matrix,
        "rule_obligations": rule_obligations,
        "config_templates": _site_policy_config_templates(matrix),
        "site_policies": report.get("site_policies", []),
        "qbit_limits": report.get("qbit_limits", {}),
        "policy_gap_summary": policy_gap_summary,
        "execution_readiness": execution_readiness,
        "policy_execution_summary": policy_execution_summary,
        "policy_setup_summary": policy_setup_summary,
        "policy_execution_handoff": policy_execution_handoff,
        "policy_handoff": policy_handoff,
        "next_step": policy_handoff.get("next_step"),
        "recommended_tool": policy_handoff.get("recommended_tool"),
        "recommended_endpoint": policy_handoff.get("recommended_endpoint"),
        "recommended_request": policy_handoff.get("recommended_request"),
        "blockers": _string_list(report.get("blockers")) + _string_list(policy_setup_summary.get("blockers")),
        "next_actions": _string_list(report.get("next_actions")) or _string_list(policy_setup_summary.get("next_actions")),
        "agent_summary": _site_policy_agent_summary(matrix, report, policy_gap_summary, execution_readiness, policy_execution_summary),
        "report": report,
    }


def readiness_bundle_payload(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Aggregate non-live readiness signals for AI/seedbox handoff."""
    request = request or {}
    deployment = deployment_check_payload(request)
    source = _readiness_bundle_source(request)
    target_trackers = _readiness_bundle_target(request)
    site_policies = _readiness_bundle_site_policies(request, source, target_trackers)
    daily_schedule = _readiness_bundle_daily_schedule(request)
    live_verification = _readiness_bundle_live_verification(request, deployment, source, target_trackers)
    live_readiness = _readiness_bundle_live_readiness(request, deployment, source, target_trackers, site_policies, daily_schedule, live_verification)
    agent_decision = _readiness_bundle_agent_decision(live_readiness)
    live_test_handoff = _readiness_bundle_live_test_handoff(live_readiness, agent_decision, deployment, site_policies, live_verification)
    seedbox_live_validation_handoff = _readiness_bundle_seedbox_live_validation_handoff(deployment, live_readiness, live_test_handoff, site_policies, live_verification)
    return {
        "kind": "ptcli.readiness_bundle",
        "status": "ok" if live_readiness.get("ready_for_ai") else "blocked",
        "ok": bool(live_readiness.get("ready_for_ai")),
        "ready": bool(live_readiness.get("ready_for_ai")),
        "request": _readiness_bundle_request_context(request, source, target_trackers),
        "deployment": deployment,
        "site_policies": site_policies,
        "daily_schedule": daily_schedule,
        "live_verification": live_verification,
        "live_readiness": live_readiness,
        "live_test_handoff": live_test_handoff,
        "seedbox_live_validation_handoff": seedbox_live_validation_handoff,
        "next_step": live_test_handoff.get("next_step"),
        "recommended_tool": live_test_handoff.get("recommended_tool"),
        "recommended_endpoint": live_test_handoff.get("recommended_endpoint"),
        "recommended_request": live_test_handoff.get("recommended_request"),
        "agent_decision": agent_decision,
        "blockers": _string_list(live_readiness.get("blockers")),
        "warnings": _string_list(live_readiness.get("warnings")),
        "next_actions": _string_list(agent_decision.get("next_actions")),
    }


def _readiness_bundle_source(request: dict[str, Any]) -> dict[str, Any] | None:
    source = request.get("source") or request.get("source_url") or request.get("source_link") or request.get("url") or request.get("source_id")
    tracker = request.get("source_tracker") or request.get("from")
    if not source:
        return None
    try:
        return resolve_source_reference(str(source), str(tracker) if tracker else None)
    except Exception as exc:
        return {"error": str(exc), "requested_source": source, "tracker": tracker}


def _readiness_bundle_target(request: dict[str, Any]) -> str | None:
    try:
        return _target_trackers(request)
    except ServiceError:
        return None


def _readiness_bundle_site_policies(request: dict[str, Any], source: dict[str, Any] | None, target_trackers: str | None) -> dict[str, Any] | None:
    if not source or source.get("error") or not target_trackers:
        return None
    policy_request = {
        "source_tracker": source.get("tracker"),
        "target": target_trackers,
        "accept_rules": _truthy(request.get("accept_rules")),
    }
    if request.get("config"):
        policy_request["config"] = request.get("config")
    try:
        return site_policies_payload(policy_request)
    except Exception as exc:
        return {
            "kind": "ptcli.site_policies",
            "status": "blocked",
            "ok": False,
            "ready": False,
            "blockers": [str(exc)],
            "next_actions": ["Fix site policy config before attempting live retorrent automation."],
        }


def _readiness_bundle_daily_schedule(request: dict[str, Any]) -> dict[str, Any]:
    schedule_request: dict[str, Any] = {}
    if "schedules" in request:
        schedule_request["schedules"] = request.get("schedules")
    elif "daily_candidate_schedules" in request:
        schedule_request["schedules"] = request.get("daily_candidate_schedules")
    try:
        return daily_candidate_schedule_payload(schedule_request)
    except Exception as exc:
        return {
            "kind": "ptcli.daily_candidate_schedule",
            "status": "blocked",
            "ok": False,
            "count": 0,
            "schedules": [],
            "blockers": [str(exc)],
            "next_actions": [f"Fix {DAILY_CANDIDATE_SCHEDULE_ENV} JSON or POST valid schedules."],
        }


def _readiness_bundle_live_verification(
    request: dict[str, Any],
    deployment: dict[str, Any],
    source: dict[str, Any] | None,
    target_trackers: str | None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    flow_check: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    config_path = ((deployment.get("paths") or {}).get("config") if isinstance(deployment.get("paths"), dict) else None) or request.get("config")
    if source and not source.get("error") and target_trackers:
        try:
            config = load_config(str(config_path) if config_path else None)
            flow_check = build_flow_check(
                config,
                str(source.get("tracker")),
                str(source.get("source_id")),
                target_trackers,
                str(request.get("client") or "default"),
                base_dir=str(request.get("base_dir") or ((deployment.get("paths") or {}).get("base_dir") if isinstance(deployment.get("paths"), dict) else "") or os.getcwd()),
            )
            checks.extend(_readiness_bundle_check_items(flow_check.get("checks"), category="credentials"))
        except Exception as exc:
            checks.append(
                {
                    "name": "credentials.flow_check",
                    "category": "credentials",
                    "ok": False,
                    "blocking": True,
                    "message": f"Flow credential check could not run: {exc}",
                }
            )
    else:
        checks.append(
            {
                "name": "credentials.flow_check",
                "category": "credentials",
                "ok": False,
                "blocking": True,
                "message": "source and target are required before checking live credentials.",
            }
        )

    if config is None and config_path:
        try:
            config = load_config(str(config_path))
        except Exception:
            config = None
    checks.extend(_readiness_bundle_material_checks(config, request))
    blockers = [str(check.get("message")) for check in checks if check.get("ok") is False and check.get("blocking", True) is not False]
    warnings = [str(check.get("message")) for check in checks if check.get("ok") is False and check.get("blocking") is False]
    return {
        "kind": "ptcli.live_verification_checklist",
        "status": "ok" if not blockers else "blocked",
        "ok": not blockers,
        "ready": not blockers,
        "connectivity_checked": False,
        "checks": checks,
        "credential_requirements": _string_list(flow_check.get("credential_requirements")) if isinstance(flow_check, dict) else [],
        "flow_check": flow_check,
        "materials": _readiness_bundle_material_summary(checks),
        "blockers": list(dict.fromkeys(blockers)),
        "warnings": list(dict.fromkeys(warnings)),
        "next_actions": _readiness_bundle_live_verification_next_actions(checks),
    }


def _readiness_bundle_check_items(items: Any, *, category: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return checks
    for item in items:
        if not isinstance(item, dict):
            continue
        check = dict(item)
        check.setdefault("category", category)
        check.setdefault("blocking", True)
        checks.append(check)
    return checks


def _readiness_bundle_material_checks(config: dict[str, Any] | None, request: dict[str, Any]) -> list[dict[str, Any]]:
    default_config = config.get("DEFAULT", {}) if isinstance(config, dict) and isinstance(config.get("DEFAULT"), dict) else {}
    image_hosts = [str(default_config.get(f"img_host_{index}") or "").strip() for index in range(1, 7)]
    configured_image_hosts = [host for host in image_hosts if host]
    upload_screenshots = not _truthy(request.get("no_upload_screenshots"))
    checks = [
        {
            "name": "materials.image_host",
            "category": "materials",
            "ok": bool(configured_image_hosts) or not upload_screenshots,
            "blocking": upload_screenshots,
            "message": "Image host is configured for screenshot upload." if configured_image_hosts else "No DEFAULT.img_host_1..6 configured; screenshot upload will be blocked unless upload_screenshots is disabled or hosted screenshot files are supplied.",
            "configured_hosts": configured_image_hosts,
        },
        {
            "name": "materials.metadata_chain",
            "category": "materials",
            "ok": True,
            "blocking": False,
            "message": "IMDb/TMDb/豆瓣/PTGen metadata is collected during prepare-target; provide explicit metadata files/ids only when automatic lookup is blocked.",
            "runtime_required": True,
        },
        {
            "name": "materials.media_info",
            "category": "materials",
            "ok": True,
            "blocking": False,
            "message": "MediaInfo/BDInfo and screenshot generation are runtime checks; use doctor or the job summary for concrete file evidence.",
            "runtime_required": True,
        },
    ]
    if request.get("path") or request.get("content_path"):
        checks.append(
            {
                "name": "materials.content_path",
                "category": "materials",
                "ok": True,
                "blocking": False,
                "message": "Existing content path was provided; runtime job will verify it before material generation.",
                "path": request.get("path") or request.get("content_path"),
            }
        )
    else:
        checks.append(
            {
                "name": "materials.content_path",
                "category": "materials",
                "ok": True,
                "blocking": False,
                "message": "No content path provided; live job must download or match source content through qBittorrent before material generation.",
                "runtime_required": True,
            }
        )
    return checks


def _readiness_bundle_material_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    material_checks = [check for check in checks if check.get("category") == "materials"]
    return {
        "ready": not any(check.get("ok") is False and check.get("blocking", True) is not False for check in material_checks),
        "image_host_ready": any(check.get("name") == "materials.image_host" and check.get("ok") is True for check in material_checks),
        "runtime_material_generation_required": any(check.get("runtime_required") for check in material_checks),
        "checks": material_checks,
    }


def _readiness_bundle_live_verification_next_actions(checks: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for check in checks:
        if check.get("ok") is not False:
            continue
        name = str(check.get("name") or "")
        if name.endswith(".cookie"):
            actions.append("Place a fresh source tracker cookie file under data/cookies/<TRACKER>.txt.")
        elif name.endswith(".passkey"):
            actions.append("Add the source tracker passkey/announce URL to data/config.py.")
        elif name == "MTEAM.api_key":
            actions.append("Add TRACKERS.MTEAM.api_key to data/config.py.")
        elif name == "qbit.client":
            actions.append("Configure the qBittorrent client in data/config.py TORRENT_CLIENTS.")
        elif name == "materials.image_host":
            actions.append("Configure DEFAULT.img_host_1..6 or provide already hosted screenshot/material files before live upload.")
        else:
            actions.append(str(check.get("message") or f"Fix {name}."))
    return list(dict.fromkeys(actions))


def _readiness_bundle_live_readiness(
    request: dict[str, Any],
    deployment: dict[str, Any],
    source: dict[str, Any] | None,
    target_trackers: str | None,
    site_policies: dict[str, Any] | None,
    daily_schedule: dict[str, Any],
    live_verification: dict[str, Any],
) -> dict[str, Any]:
    blockers = _string_list(deployment.get("blockers"))
    warnings = _string_list(deployment.get("warnings"))
    policy_execution_summary = site_policies.get("policy_execution_summary") if isinstance(site_policies, dict) and isinstance(site_policies.get("policy_execution_summary"), dict) else None
    policy_setup_summary = site_policies.get("policy_setup_summary") if isinstance(site_policies, dict) and isinstance(site_policies.get("policy_setup_summary"), dict) else None
    policy_execution_handoff = site_policies.get("policy_execution_handoff") if isinstance(site_policies, dict) and isinstance(site_policies.get("policy_execution_handoff"), dict) else None
    if source is None:
        blockers.append("source_url or source/source_id with source_tracker is required for manual live readiness.")
    elif source.get("error"):
        blockers.append(f"Source could not be resolved: {source.get('error')}")
    if not target_trackers:
        blockers.append("target is required for manual live readiness.")
    if site_policies and (site_policies.get("ready") is not True or (site_policies.get("execution_readiness") or {}).get("ready") is not True):
        blockers.extend(_string_list(site_policies.get("blockers")))
        blockers.extend(_string_list((policy_execution_summary or {}).get("blockers")))
        blockers.extend(_string_list((site_policies.get("agent_summary") or {}).get("policy_recommendations")))
    if live_verification.get("ready") is not True:
        blockers.extend(_string_list(live_verification.get("blockers")))
    warnings.extend(_string_list(live_verification.get("warnings")))
    accept_rules = _truthy(request.get("accept_rules"))
    confirm_upload = _truthy(request.get("confirm_upload"))
    if not accept_rules:
        blockers.append("accept_rules=true is required before live execution.")
    if not confirm_upload:
        blockers.append("confirm_upload=true is required before live upload.")
    doctor_template = _readiness_bundle_doctor_template(request, source, target_trackers)
    manual_job_template = _readiness_bundle_manual_job_template(request, source, target_trackers)
    daily_ready = bool(daily_schedule.get("ok")) and not bool(daily_schedule.get("blockers"))
    return {
        "ready_for_ai": bool(deployment.get("ready")),
        "ready_for_manual_retorrent": not blockers and bool(source) and not bool(source.get("error")) and bool(target_trackers),
        "ready_for_daily_candidates": bool(deployment.get("ready")) and daily_ready,
        "source": source,
        "target_trackers": target_trackers,
        "site_policy_ready": bool(site_policies.get("ready")) and bool((site_policies.get("execution_readiness") or {}).get("ready")) if isinstance(site_policies, dict) else False,
        "policy_execution_summary": policy_execution_summary,
        "policy_setup_summary": policy_setup_summary,
        "policy_execution_handoff": policy_execution_handoff,
        "live_verification_ready": bool(live_verification.get("ready")),
        "credential_requirements": _string_list(live_verification.get("credential_requirements")),
        "accept_rules": accept_rules,
        "confirm_upload": confirm_upload,
        "doctor_template": doctor_template,
        "manual_job_template": manual_job_template,
        "daily_schedule_ready": daily_ready,
        "blockers": list(dict.fromkeys(blockers)),
        "warnings": warnings,
        "next_actions": _string_list(live_verification.get("next_actions")),
    }


def _readiness_bundle_doctor_template(request: dict[str, Any], source: dict[str, Any] | None, target_trackers: str | None) -> dict[str, Any] | None:
    if not source or source.get("error") or not target_trackers:
        return None
    argv = [
        "ptcli",
        "doctor",
        "--from",
        str(source.get("tracker")),
        "--source-id",
        str(source.get("source_id")),
        "--to",
        target_trackers,
        "--target-execute",
        "--download-uploaded-torrent",
        "--inject-uploaded-torrent",
        "--wait-uploaded-complete",
        "--connect-qbit",
        "--probe-source",
        "--probe-target",
        "--json",
    ]
    _append_common_options(argv, request)
    if request.get("path") or request.get("content_path"):
        _append_optional(argv, "--path", request.get("path") or request.get("content_path"))
    else:
        _append_optional(argv, "--save-path", request.get("save_path") or "/downloads")
    if _truthy(request.get("accept_rules")):
        argv.append("--accept-rules")
    if _truthy(request.get("confirm_upload")):
        argv.append("--confirm-upload")
    _append_optional(argv, "--uploaded-qbit-category", request.get("uploaded_qbit_category") or target_trackers)
    _append_optional(argv, "--uploaded-qbit-tags", request.get("uploaded_qbit_tags") or "retorrent")
    return {
        "tool": "ptcli doctor",
        "argv": argv,
        "requires_before_live": ["accept_rules=true", "confirm_upload=true", "site policy ready", "non-duplicate target"],
    }


def _readiness_bundle_manual_job_template(request: dict[str, Any], source: dict[str, Any] | None, target_trackers: str | None) -> dict[str, Any] | None:
    if not source or source.get("error") or not target_trackers:
        return None
    template = {
        "source_url": source.get("details_url") or source.get("requested_source"),
        "target": target_trackers,
        "accept_rules": True,
        "confirm_upload": True,
        "save_path": request.get("save_path") or "/downloads",
        "uploaded_qbit_category": request.get("uploaded_qbit_category") or target_trackers,
        "uploaded_qbit_tags": request.get("uploaded_qbit_tags") or "retorrent",
    }
    for key in ("path", "content_path", "source_torrent_file", "target_torrent_file", "uploaded_torrent_file"):
        if request.get(key):
            template[key] = request[key]
    return {
        "tool": "source_url_retorrent_job",
        "endpoint": "/v1/jobs/retorrent/from-url",
        "request": template,
    }


def _readiness_bundle_agent_decision(live_readiness: dict[str, Any]) -> dict[str, Any]:
    if not live_readiness.get("ready_for_ai"):
        decision = "fix_deployment"
        recommended_action = "Resolve deployment blockers before asking an AI agent to run PT automation."
    elif live_readiness.get("ready_for_manual_retorrent"):
        decision = "ready_for_manual_retorrent"
        recommended_action = "Run site_policies if needed, then submit manual_job_template.request to source_url_retorrent_job."
    elif live_readiness.get("ready_for_daily_candidates"):
        decision = "ready_for_daily_candidates"
        recommended_action = "Create daily candidate schedule jobs, then submit approved candidates through submission_handoff."
    else:
        decision = "collect_missing_inputs"
        recommended_action = "Provide source_url, target, accept_rules=true, confirm_upload=true, and complete site policies before live execution."
    next_actions = [recommended_action, *_string_list(live_readiness.get("next_actions")), *_string_list(live_readiness.get("blockers"))]
    return {
        "workflow": "ptcli.readiness_bundle",
        "decision": decision,
        "recommended_action": recommended_action,
        "runbook_ref": "source_url_retorrent" if decision == "ready_for_manual_retorrent" else "daily_candidates" if decision == "ready_for_daily_candidates" else None,
        "next_tool": "source_url_retorrent_job" if decision == "ready_for_manual_retorrent" else "daily_candidates_schedule_job" if decision == "ready_for_daily_candidates" else "readiness_bundle",
        "can_create_manual_job": bool(live_readiness.get("ready_for_manual_retorrent")),
        "can_run_daily_candidates": bool(live_readiness.get("ready_for_daily_candidates")),
        "should_fix_deployment": decision == "fix_deployment",
        "next_actions": list(dict.fromkeys(next_actions)),
    }


def _readiness_bundle_live_test_handoff(
    live_readiness: dict[str, Any],
    agent_decision: dict[str, Any],
    deployment: dict[str, Any],
    site_policies: dict[str, Any] | None,
    live_verification: dict[str, Any],
) -> dict[str, Any]:
    next_step = _readiness_bundle_live_test_next_step(live_readiness, agent_decision, deployment, site_policies, live_verification)
    doctor_template = live_readiness.get("doctor_template") if isinstance(live_readiness.get("doctor_template"), dict) else None
    manual_job_template = live_readiness.get("manual_job_template") if isinstance(live_readiness.get("manual_job_template"), dict) else None
    policy_execution_summary = live_readiness.get("policy_execution_summary") if isinstance(live_readiness.get("policy_execution_summary"), dict) else None
    policy_execution_handoff = live_readiness.get("policy_execution_handoff") if isinstance(live_readiness.get("policy_execution_handoff"), dict) else None
    preflight_checklist = _readiness_bundle_live_test_checklist(live_readiness, deployment, site_policies, live_verification, doctor_template, manual_job_template)
    execution_plan = _readiness_bundle_live_test_execution_plan(next_step, doctor_template, manual_job_template, preflight_checklist)
    return {
        "kind": "ptcli.live_test_handoff",
        "ready": bool(live_readiness.get("ready_for_manual_retorrent")),
        "doctor_ready": bool(doctor_template),
        "manual_job_ready": bool(manual_job_template) and bool(live_readiness.get("ready_for_manual_retorrent")),
        "connectivity_checked": bool(live_verification.get("connectivity_checked")),
        "preflight_checklist": preflight_checklist,
        "execution_plan": execution_plan,
        "doctor_template": doctor_template,
        "manual_job_template": manual_job_template,
        "policy_execution_summary": policy_execution_summary,
        "policy_execution_handoff": policy_execution_handoff,
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "blockers": _string_list(live_readiness.get("blockers")),
        "warnings": _string_list(live_readiness.get("warnings")),
        "after_doctor": {
            "summary_check_tool": "summary_check",
            "summary_check_argv_template": ["python3", "ptcli.py", "summary-check", "--summary-file", "<ptcli-doctor-summary.json>", "--json"],
            "read_fields": ["doctor_result_handoff", "live_safe_to_attempt", "blockers", "credential_requirements", "summary_file", "next_actions"],
            "continue_when": "live_safe_to_attempt=true",
            "then_tool": "source_url_retorrent_job",
            "then_request": manual_job_template.get("request") if isinstance(manual_job_template, dict) else None,
        },
    }


def _readiness_bundle_seedbox_live_validation_handoff(
    deployment: dict[str, Any],
    live_readiness: dict[str, Any],
    live_test_handoff: dict[str, Any],
    site_policies: dict[str, Any] | None,
    live_verification: dict[str, Any],
) -> dict[str, Any]:
    """Summarize the first safe seedbox live validation attempt for AI agents."""
    next_step = live_test_handoff.get("next_step") if isinstance(live_test_handoff.get("next_step"), dict) else {}
    doctor_template = live_test_handoff.get("doctor_template") if isinstance(live_test_handoff.get("doctor_template"), dict) else None
    manual_job_template = live_test_handoff.get("manual_job_template") if isinstance(live_test_handoff.get("manual_job_template"), dict) else None
    preflight_checklist = live_test_handoff.get("preflight_checklist") if isinstance(live_test_handoff.get("preflight_checklist"), dict) else {}
    execution_plan = live_test_handoff.get("execution_plan") if isinstance(live_test_handoff.get("execution_plan"), dict) else {}
    deployment_handoff = deployment.get("deployment_handoff") if isinstance(deployment.get("deployment_handoff"), dict) else {}
    agent_handoff = deployment.get("agent_handoff") if isinstance(deployment.get("agent_handoff"), dict) else {}
    docker_compose = deployment.get("docker_compose") if isinstance(deployment.get("docker_compose"), dict) else {}
    qbit = deployment.get("qbit") if isinstance(deployment.get("qbit"), dict) else {}
    policy_execution_summary = live_readiness.get("policy_execution_summary") if isinstance(live_readiness.get("policy_execution_summary"), dict) else {}
    policy_setup_summary = live_readiness.get("policy_setup_summary") if isinstance(live_readiness.get("policy_setup_summary"), dict) else {}
    policy_execution_handoff = live_readiness.get("policy_execution_handoff") if isinstance(live_readiness.get("policy_execution_handoff"), dict) else {}
    ready = bool(live_readiness.get("ready_for_manual_retorrent") and preflight_checklist.get("ready") and doctor_template and manual_job_template)
    phase = "run_doctor" if ready else str(next_step.get("reason") or "fix_preflight")
    doctor_request = {"argv": doctor_template.get("argv")} if isinstance(doctor_template, dict) and doctor_template.get("argv") else None
    manual_request = manual_job_template.get("request") if isinstance(manual_job_template, dict) else None
    validation_plan = _seedbox_live_validation_plan(ready, next_step, doctor_request, manual_request, live_test_handoff)
    post_submit_handoff = _seedbox_post_submit_handoff(manual_request)
    return {
        "kind": "ptcli.seedbox_live_validation_handoff",
        "ready": ready,
        "phase": phase,
        "connectivity_checked": bool(live_verification.get("connectivity_checked")),
        "preflight_ready": bool(preflight_checklist.get("ready")),
        "preflight_checklist": preflight_checklist,
        "execution_plan": execution_plan,
        "docker_compose": {
            "api_ready": bool(docker_compose.get("ptcli_api_service_ready")),
            "api_healthcheck": bool(docker_compose.get("ptcli_api_healthcheck")),
            "localhost_port": bool(docker_compose.get("ptcli_api_localhost_port")),
            "downloads_mount": bool(docker_compose.get("downloads_mount")),
            "config_mount": bool(docker_compose.get("config_mount")),
            "cookies_mount": bool(docker_compose.get("cookies_mount")),
            "tmp_mount": bool(docker_compose.get("tmp_mount")),
            "daily_ready": bool(docker_compose.get("daily_scheduler_service_ready") or docker_compose.get("daily_schedule_service_ready")),
            "api": deployment_handoff.get("api") or (agent_handoff.get("api") if isinstance(agent_handoff.get("api"), dict) else None),
        },
        "qbit": {
            "configured": bool(qbit.get("configured")),
            "client": qbit.get("client"),
            "torrent_client": qbit.get("torrent_client"),
            "qbit_url": qbit.get("qbit_url"),
            "qbit_port": qbit.get("qbit_port"),
            "connectivity_checked": bool(qbit.get("connectivity_checked")),
        },
        "site_policy": {
            "ready": bool(live_readiness.get("site_policy_ready")),
            "policy_execution_summary": policy_execution_summary,
            "policy_setup_summary": policy_setup_summary,
            "policy_execution_handoff": policy_execution_handoff,
            "rule_obligations": (site_policies or {}).get("rule_obligations") if isinstance(site_policies, dict) else None,
        },
        "credentials": {
            "ready": bool(live_verification.get("ready")),
            "credential_requirements": _string_list(live_verification.get("credential_requirements")),
            "materials": live_verification.get("materials") if isinstance(live_verification.get("materials"), dict) else {},
            "blockers": _string_list(live_verification.get("blockers")),
        },
        "doctor": {
            "ready": bool(doctor_request),
            "tool": "ptcli_doctor",
            "method": "CLI",
            "request": doctor_request,
            "continue_when": "doctor_result_handoff.live_safe_to_attempt=true",
            "summary_check": (live_test_handoff.get("after_doctor") or {}).get("summary_check_argv_template") if isinstance(live_test_handoff.get("after_doctor"), dict) else None,
        },
        "manual_job": {
            "ready": bool(manual_request) and ready,
            "tool": "source_url_check_and_submit",
            "endpoint": "/v1/jobs/retorrent/from-url/check-and-submit",
            "method": "POST",
            "request": manual_request,
            "continue_when": "job_id is returned; poll get_job_status then read get_job_summary.closure_handoff",
        },
        "validation_plan": validation_plan,
        "post_submit_handoff": post_submit_handoff,
        "evidence_contract": _seedbox_live_evidence_contract(),
        "recommended_tool": "ptcli_doctor" if ready else next_step.get("tool"),
        "recommended_endpoint": None if ready else next_step.get("endpoint"),
        "recommended_request": doctor_request if ready else next_step.get("request"),
        "next_step": {
            "tool": "ptcli_doctor",
            "method": "CLI",
            "request": doctor_request,
            "reason": "run_seedbox_live_doctor_before_submission",
        }
        if ready
        else next_step,
        "continue_when": "doctor_result_handoff.live_safe_to_attempt=true, then submit manual_job.request through source_url_check_and_submit",
        "stop_when": "any blocker is present, duplicate_check.exists=true, or doctor_result_handoff.live_safe_to_attempt=false",
        "blockers": _string_list(live_test_handoff.get("blockers")) or _string_list(live_readiness.get("blockers")),
        "warnings": _string_list(live_test_handoff.get("warnings")) or _string_list(live_readiness.get("warnings")),
        "next_actions": _string_list(preflight_checklist.get("next_actions")) or _string_list(live_readiness.get("next_actions")),
    }


def _seedbox_live_validation_plan(
    ready: bool,
    next_step: dict[str, Any],
    doctor_request: dict[str, Any] | None,
    manual_request: dict[str, Any] | None,
    live_test_handoff: dict[str, Any],
) -> dict[str, Any]:
    summary_check = (live_test_handoff.get("after_doctor") or {}).get("summary_check_argv_template") if isinstance(live_test_handoff.get("after_doctor"), dict) else None
    steps = [
        {
            "index": 1,
            "name": "preflight",
            "tool": next_step.get("tool") if not ready else "readiness_bundle",
            "endpoint": next_step.get("endpoint") if not ready else "/v1/readiness/bundle",
            "method": next_step.get("method") if not ready else "POST",
            "request": next_step.get("request") if not ready else None,
            "read": ["seedbox_live_validation_handoff.preflight_checklist", "seedbox_live_validation_handoff.blockers"],
            "continue_when": "seedbox_live_validation_handoff.preflight_ready=true and seedbox_live_validation_handoff.ready=true",
            "stop_when": "seedbox_live_validation_handoff.blockers is non-empty",
        },
        {
            "index": 2,
            "name": "doctor",
            "tool": "ptcli_doctor",
            "method": "CLI",
            "request": doctor_request,
            "read": ["ptcli-doctor-summary.json", "doctor_result_handoff.live_safe_to_attempt", "doctor_result_handoff.blockers"],
            "continue_when": "doctor_result_handoff.live_safe_to_attempt=true",
            "stop_when": "doctor_result_handoff.live_safe_to_attempt=false or doctor_result_handoff.blockers is non-empty",
            "summary_check": summary_check,
        },
        {
            "index": 3,
            "name": "check_and_submit",
            "tool": "source_url_check_and_submit",
            "endpoint": "/v1/jobs/retorrent/from-url/check-and-submit",
            "method": "POST",
            "request": manual_request,
            "read": ["duplicate_check", "submit_if_clear_handoff", "job_id", "status_endpoint", "summary_endpoint"],
            "continue_when": "duplicate_check.exists=false and job_id is returned",
            "stop_when": "duplicate_check.exists=true or submit_if_clear_handoff.ready=false",
        },
        {
            "index": 4,
            "name": "poll_job",
            "tool": "get_job_status",
            "endpoint": "/v1/jobs/{job_id}",
            "method": "GET",
            "request": {"job_id": "<job_id from check_and_submit>"},
            "read": ["status", "recovery_handoff", "job_handoff", "blockers", "next_actions"],
            "repeat_when": "recovery_handoff.should_poll=true",
            "continue_when": "status in blocked,failed,complete,cancelled",
            "stop_when": "status=cancelled or recovery_handoff.action=stop",
        },
        {
            "index": 5,
            "name": "recover_or_finish",
            "tool": "resume_job or get_job_summary",
            "endpoint": "/v1/jobs/{job_id}/resume or /v1/jobs/{job_id}/summary",
            "method": "POST or GET",
            "request": "recovery_handoff.dry_run_request, recovery_handoff.execute_request, or null for summary",
            "read": ["recovery_handoff", "closure_summary", "closure_handoff", "qbit_enforcement_summary", "evidence"],
            "continue_when": "closure_summary.complete=true and closure_summary.blockers=[]",
            "stop_when": "recovery_handoff.blockers remain after allowed resume or closure_summary.blockers is non-empty",
        },
    ]
    return {
        "kind": "ptcli.seedbox_live_validation_plan",
        "ready": ready,
        "first_step": "doctor" if ready else "preflight",
        "steps": steps,
        "required_order": ["preflight", "doctor", "check_and_submit", "poll_job", "recover_or_finish"],
        "read_first": ["seedbox_live_validation_handoff", "validation_plan", "evidence_contract"],
    }


def _seedbox_post_submit_handoff(manual_request: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "kind": "ptcli.seedbox_post_submit_handoff",
        "ready": bool(manual_request),
        "submit_tool": "source_url_check_and_submit",
        "submit_endpoint": "/v1/jobs/retorrent/from-url/check-and-submit",
        "submit_method": "POST",
        "submit_request": manual_request,
        "after_submit_read": ["duplicate_check", "job_id", "status_endpoint", "summary_endpoint"],
        "poll_tool": "get_job_status",
        "poll_endpoint_template": "/v1/jobs/{job_id}",
        "poll_until": "recovery_handoff.should_poll=false",
        "resume_tool": "resume_job",
        "resume_endpoint_template": "/v1/jobs/{job_id}/resume",
        "resume_when": "recovery_handoff.should_resume=true and recovery_handoff.dry_run_request is present",
        "resume_order": ["call dry_run_request", "review command_argv and ignored_overrides", "call execute_request only after user approval", "poll child job"],
        "finish_tool": "get_job_summary",
        "finish_endpoint_template": "/v1/jobs/{job_id}/summary",
        "complete_when": "closure_summary.complete=true and closure_summary.blockers=[]",
        "stop_when": ["duplicate_check.exists=true", "recovery_handoff.action=stop", "closure_summary.blockers is non-empty"],
    }


def _seedbox_live_evidence_contract() -> dict[str, Any]:
    return {
        "kind": "ptcli.seedbox_live_evidence_contract",
        "final_read": "get_job_summary",
        "complete_when": ["closure_summary.complete=true", "closure_summary.blockers=[]", "qbit_enforcement_summary.ready=true when rate limits are configured"],
        "required_fields": [
            "source_reference",
            "duplicate_check.searched",
            "duplicate_check.exists",
            "closure_summary.source.torrent_hash",
            "closure_summary.source.content_path",
            "closure_summary.target.uploaded_torrent_hash",
            "closure_summary.target.injected_torrent_hash",
            "materials_handoff.ready",
            "target_upload_handoff.uploaded_seeding_ready",
            "qbit_enforcement_summary.status",
            "summary_file",
        ],
        "audit_notes": [
            "Do not treat the live validation as complete when duplicate_check.exists=true.",
            "Do not skip accept_rules, confirm_upload, policy_execution_handoff, or rule_obligations.",
            "Report missing hash/path/size/sha1 evidence from closure_summary or evidence before considering the first live run verified.",
        ],
    }


def _readiness_bundle_live_test_checklist(
    live_readiness: dict[str, Any],
    deployment: dict[str, Any],
    site_policies: dict[str, Any] | None,
    live_verification: dict[str, Any],
    doctor_template: dict[str, Any] | None,
    manual_job_template: dict[str, Any] | None,
) -> dict[str, Any]:
    items = [
        _readiness_checklist_item(
            "deployment",
            "Docker/API deployment is ready",
            deployment.get("ready") is True,
            blockers=_string_list(deployment.get("blockers")),
            next_actions=_string_list(deployment.get("next_actions")),
            evidence={"status": deployment.get("status"), "deployment_handoff": deployment.get("deployment_handoff")},
        ),
        _readiness_checklist_item(
            "site_policy",
            "Source/target site policy gate is ready",
            bool(site_policies and site_policies.get("ready") is True and (site_policies.get("execution_readiness") or {}).get("ready") is True),
            blockers=_string_list(((site_policies or {}).get("policy_execution_handoff") or {}).get("blockers")) or _string_list((site_policies or {}).get("blockers")) + _string_list(((site_policies or {}).get("policy_execution_summary") or {}).get("blockers")),
            next_actions=_string_list(((site_policies or {}).get("policy_execution_handoff") or {}).get("next_actions")) or _string_list((site_policies or {}).get("next_actions")) or _string_list(((site_policies or {}).get("policy_handoff") or {}).get("next_actions")),
            evidence={"policy_execution_summary": (site_policies or {}).get("policy_execution_summary"), "policy_setup_summary": (site_policies or {}).get("policy_setup_summary"), "policy_execution_handoff": (site_policies or {}).get("policy_execution_handoff")},
        ),
        _readiness_checklist_item(
            "credentials",
            "Tracker and qBittorrent credentials are present",
            live_verification.get("ready") is True and not any(check.get("category") == "credentials" and check.get("ok") is False for check in live_verification.get("checks", []) if isinstance(check, dict)),
            blockers=[str(check.get("message")) for check in live_verification.get("checks", []) if isinstance(check, dict) and check.get("category") == "credentials" and check.get("ok") is False],
            next_actions=_string_list(live_verification.get("next_actions")),
            evidence={"credential_requirements": live_verification.get("credential_requirements"), "connectivity_checked": live_verification.get("connectivity_checked")},
        ),
        _readiness_checklist_item(
            "materials",
            "Material prerequisites are ready or recoverable",
            (live_verification.get("materials") or {}).get("ready") is True,
            blockers=[str(check.get("message")) for check in ((live_verification.get("materials") or {}).get("checks") or []) if isinstance(check, dict) and check.get("ok") is False and check.get("blocking", True) is not False],
            next_actions=_string_list(live_verification.get("next_actions")),
            evidence=live_verification.get("materials") if isinstance(live_verification.get("materials"), dict) else {},
        ),
        _readiness_checklist_item(
            "confirmations",
            "Live confirmations are explicit",
            live_readiness.get("accept_rules") is True and live_readiness.get("confirm_upload") is True,
            blockers=[blocker for blocker in _string_list(live_readiness.get("blockers")) if "accept_rules" in blocker or "confirm_upload" in blocker],
            next_actions=["Rerun readiness_bundle with accept_rules=true and confirm_upload=true after manual rule review."],
            evidence={"accept_rules": live_readiness.get("accept_rules"), "confirm_upload": live_readiness.get("confirm_upload")},
        ),
        _readiness_checklist_item(
            "doctor",
            "Live doctor command is available",
            bool(doctor_template and doctor_template.get("argv")),
            blockers=[] if doctor_template and doctor_template.get("argv") else ["doctor_template.argv is missing."],
            next_actions=["Provide source_url/source_id, target, and seedbox paths so readiness_bundle can build a doctor command."],
            evidence=doctor_template or {},
        ),
        _readiness_checklist_item(
            "manual_job",
            "Manual job request is available after doctor passes",
            bool(manual_job_template and manual_job_template.get("request")),
            blockers=[] if manual_job_template and manual_job_template.get("request") else ["manual_job_template.request is missing."],
            next_actions=["Resolve readiness blockers before creating a live retorrent job."],
            evidence=manual_job_template or {},
        ),
    ]
    blockers = [blocker for item in items for blocker in _string_list(item.get("blockers"))]
    ready_count = sum(1 for item in items if item.get("ready") is True)
    return {
        "kind": "ptcli.live_test_preflight_checklist",
        "ready": not blockers and all(item.get("ready") is True for item in items),
        "ready_count": ready_count,
        "total_count": len(items),
        "blocked_count": len(items) - ready_count,
        "items": items,
        "blockers": list(dict.fromkeys(blockers)),
        "next_actions": list(dict.fromkeys(action for item in items for action in _string_list(item.get("next_actions")) if item.get("ready") is not True)),
    }


def _readiness_checklist_item(name: str, label: str, ready: bool, *, blockers: list[str], next_actions: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "ready": bool(ready),
        "blockers": list(dict.fromkeys(blockers)),
        "next_actions": list(dict.fromkeys(next_actions)) if not ready else [],
        "evidence": evidence,
    }


def _readiness_bundle_live_test_execution_plan(
    next_step: dict[str, Any],
    doctor_template: dict[str, Any] | None,
    manual_job_template: dict[str, Any] | None,
    preflight_checklist: dict[str, Any],
) -> dict[str, Any]:
    doctor_argv = doctor_template.get("argv") if isinstance(doctor_template, dict) else None
    manual_request = manual_job_template.get("request") if isinstance(manual_job_template, dict) else None
    steps = [
        {
            "index": 1,
            "name": "fix_preflight",
            "ready": bool(preflight_checklist.get("ready")),
            "tool": next_step.get("tool"),
            "endpoint": next_step.get("endpoint"),
            "method": next_step.get("method"),
            "request": next_step.get("request"),
            "continue_when": "preflight_checklist.ready=true",
            "blockers": _string_list(preflight_checklist.get("blockers")),
        },
        {
            "index": 2,
            "name": "run_doctor",
            "ready": bool(doctor_argv),
            "tool": "ptcli_doctor",
            "method": "CLI",
            "request": {"argv": doctor_argv} if doctor_argv else None,
            "continue_when": "doctor_result_handoff.live_safe_to_attempt=true",
        },
        {
            "index": 3,
            "name": "submit_checked_manual_job",
            "ready": bool(manual_request),
            "tool": "source_url_check_and_submit",
            "endpoint": "/v1/jobs/retorrent/from-url/check-and-submit",
            "method": "POST",
            "request": manual_request,
            "continue_when": "job_id is returned; then poll get_job_status and read get_job_summary",
        },
    ]
    return {
        "kind": "ptcli.live_test_execution_plan",
        "ready": bool(preflight_checklist.get("ready") and doctor_argv and manual_request),
        "recommended_first_step": next_step,
        "steps": steps,
        "blockers": _string_list(preflight_checklist.get("blockers")),
    }


def _readiness_bundle_live_test_next_step(
    live_readiness: dict[str, Any],
    agent_decision: dict[str, Any],
    deployment: dict[str, Any],
    site_policies: dict[str, Any] | None,
    live_verification: dict[str, Any],
) -> dict[str, Any]:
    if deployment.get("ready") is not True:
        return {
            "tool": "deployment_check",
            "endpoint": "/v1/deployment/check",
            "method": "GET",
            "request": None,
            "reason": "deployment_not_ready",
            "blockers": _string_list(deployment.get("blockers")),
        }
    if isinstance(site_policies, dict) and (site_policies.get("ready") is not True or (site_policies.get("execution_readiness") or {}).get("ready") is not True):
        policy_execution_summary = site_policies.get("policy_execution_summary") if isinstance(site_policies.get("policy_execution_summary"), dict) else {}
        policy_execution_handoff = site_policies.get("policy_execution_handoff") if isinstance(site_policies.get("policy_execution_handoff"), dict) else {}
        policy_handoff = site_policies.get("policy_handoff") if isinstance(site_policies.get("policy_handoff"), dict) else {}
        policy_step = policy_execution_handoff.get("next_step") if isinstance(policy_execution_handoff.get("next_step"), dict) else policy_execution_summary.get("next_step") if isinstance(policy_execution_summary.get("next_step"), dict) else policy_handoff.get("next_step") if isinstance(policy_handoff.get("next_step"), dict) else {}
        return {
            "tool": policy_step.get("tool") or "site_policies",
            "endpoint": policy_step.get("endpoint") or "/v1/site-policies",
            "method": policy_step.get("method") or "POST",
            "request": policy_step.get("request") or site_policies.get("request"),
            "reason": "site_policy_not_ready",
            "blockers": _string_list(policy_execution_handoff.get("blockers")) or _string_list(policy_execution_summary.get("blockers")) or _string_list(site_policies.get("blockers")) or _string_list((site_policies.get("agent_summary") or {}).get("policy_recommendations")),
            "policy_execution_summary": policy_execution_summary,
            "policy_execution_handoff": policy_execution_handoff,
        }
    if live_verification.get("ready") is not True:
        return {
            "tool": "readiness_bundle",
            "endpoint": "/v1/readiness/bundle",
            "method": "POST",
            "request": None,
            "reason": "live_verification_not_ready",
            "blockers": _string_list(live_verification.get("blockers")),
            "next_actions": _string_list(live_verification.get("next_actions")),
        }
    if not live_readiness.get("accept_rules") or not live_readiness.get("confirm_upload"):
        return {
            "tool": "readiness_bundle",
            "endpoint": "/v1/readiness/bundle",
            "method": "POST",
            "request": {"accept_rules": True, "confirm_upload": True},
            "reason": "missing_live_confirmations",
            "blockers": [blocker for blocker in _string_list(live_readiness.get("blockers")) if "accept_rules" in blocker or "confirm_upload" in blocker],
        }
    doctor_template = live_readiness.get("doctor_template") if isinstance(live_readiness.get("doctor_template"), dict) else {}
    if live_readiness.get("ready_for_manual_retorrent") and doctor_template.get("argv"):
        return {
            "tool": "ptcli_doctor",
            "endpoint": None,
            "method": "CLI",
            "request": {"argv": doctor_template.get("argv")},
            "reason": "run_seedbox_live_doctor_before_submission",
        }
    return {
        "tool": agent_decision.get("next_tool") or "readiness_bundle",
        "endpoint": None,
        "method": None,
        "request": None,
        "reason": "inspect_readiness_bundle",
        "blockers": _string_list(live_readiness.get("blockers")),
    }


def _readiness_bundle_request_context(request: dict[str, Any], source: dict[str, Any] | None, target_trackers: str | None) -> dict[str, Any]:
    return {
        "source": source,
        "target_trackers": target_trackers,
        "accept_rules": _truthy(request.get("accept_rules")),
        "confirm_upload": _truthy(request.get("confirm_upload")),
        "config": request.get("config"),
        "base_dir": request.get("base_dir"),
        "client": request.get("client") or "default",
    }


def _pipeline_check_args(request: dict[str, Any]) -> tuple[argparse.Namespace, dict[str, Any], list[str]]:
    source = _resolve_request_source(request)
    target_trackers = _target_trackers(request)
    argv = [
        "pipeline",
        "--from",
        source["tracker"],
        "--source-id",
        source["source_id"],
        "--to",
        target_trackers,
        "--check-dupes",
        "--json",
    ]
    _append_common_options(argv, request)
    _append_optional(argv, "--path", request.get("path") or request.get("content_path"))
    if request.get("accept_rules"):
        argv.append("--accept-rules")
    if request.get("enrich_metadata"):
        argv.append("--enrich-metadata")
    if request.get("fetch_ptgen"):
        argv.append("--fetch-ptgen")
    _append_metadata_options(argv, request)
    return _parse_args(argv), _normalized_request(request, source, target_trackers, execute=False), argv


def _retorrent_execute_args(request: dict[str, Any]) -> tuple[argparse.Namespace, dict[str, Any], list[str]]:
    source = _resolve_request_source(request)
    target_trackers = _target_trackers(request)
    request = _request_with_policy_qbit_defaults(request, source, target_trackers)
    argv = [
        "retorrent",
        "--from",
        source["tracker"],
        "--source-id",
        source["source_id"],
        "--to",
        target_trackers,
        "--execute",
        "--json",
    ]
    _append_common_options(argv, request)
    _append_optional(argv, "--path", request.get("path") or request.get("content_path"))
    _append_optional(argv, "--save-path", request.get("save_path"))
    _append_optional(argv, "--output-dir", request.get("output_dir"))
    _append_optional(argv, "--source-torrent-file", request.get("source_torrent_file"))
    _append_optional(argv, "--package-dir", request.get("package_dir"))
    _append_optional(argv, "--target-output-dir", request.get("target_output_dir"))
    _append_optional(argv, "--target-torrent-file", request.get("target_torrent_file"))
    _append_optional(argv, "--target-torrent-output-dir", request.get("target_torrent_output_dir"))
    _append_optional(argv, "--uploaded-output-dir", request.get("uploaded_output_dir"))
    _append_optional(argv, "--uploaded-torrent-id", request.get("uploaded_torrent_id"))
    _append_optional(argv, "--uploaded-torrent-file", request.get("uploaded_torrent_file"))
    _append_optional(argv, "--uploaded-save-path", request.get("uploaded_save_path"))
    _append_optional(argv, "--qbit-category", request.get("qbit_category"))
    _append_optional(argv, "--qbit-tags", request.get("qbit_tags"))
    _append_optional(argv, "--qbit-upload-limit", request.get("qbit_upload_limit"))
    _append_optional(argv, "--qbit-download-limit", request.get("qbit_download_limit"))
    _append_optional(argv, "--uploaded-qbit-category", request.get("uploaded_qbit_category"))
    _append_optional(argv, "--uploaded-qbit-tags", request.get("uploaded_qbit_tags"))
    _append_optional(argv, "--uploaded-qbit-upload-limit", request.get("uploaded_qbit_upload_limit"))
    _append_optional(argv, "--uploaded-qbit-download-limit", request.get("uploaded_qbit_download_limit"))
    _append_optional(argv, "--metadata-file", request.get("metadata_file"))
    _append_optional(argv, "--ptgen-description-file", request.get("ptgen_description_file"))
    _append_optional(argv, "--mediainfo-file", request.get("mediainfo_file"))
    _append_optional(argv, "--bdinfo-file", request.get("bdinfo_file"))
    _append_optional(argv, "--image-host-file", request.get("image_host_file"))
    _append_optional(argv, "--image-host", request.get("image_host"))
    _append_optional(argv, "--bdinfo-playlist", request.get("bdinfo_playlist"))
    _append_optional(argv, "--screenshot-count", request.get("screenshot_count"))
    _append_optional(argv, "--wait-timeout", request.get("wait_timeout"))
    _append_optional(argv, "--wait-interval", request.get("wait_interval"))
    _append_optional(argv, "--uploaded-wait-timeout", request.get("uploaded_wait_timeout"))
    _append_optional(argv, "--uploaded-wait-interval", request.get("uploaded_wait_interval"))
    for screenshot_file in _list_value(request.get("screenshot_file") or request.get("screenshot_files")):
        _append_optional(argv, "--screenshot-file", screenshot_file)
    _append_metadata_options(argv, request)
    _append_execute_bool_options(argv, request)
    return _parse_args(argv), _normalized_request(request, source, target_trackers, execute=True), argv


def _parse_args(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _resolve_request_source(request: dict[str, Any]) -> dict[str, Any]:
    source = request.get("source") or request.get("source_url") or request.get("source_link") or request.get("url")
    if not source:
        raise ServiceError("source or source_url is required.")
    source_tracker = request.get("source_tracker") or request.get("from")
    return resolve_source_reference(str(source), str(source_tracker) if source_tracker else None)


def _target_trackers(request: dict[str, Any]) -> str:
    target = request.get("target") or request.get("target_tracker") or request.get("target_trackers") or request.get("to")
    if isinstance(target, list):
        target = ",".join(str(item) for item in target)
    if not target:
        raise ServiceError("target or target_trackers is required.")
    return str(target)


def _candidate_request_context(request: dict[str, Any]) -> dict[str, Any]:
    source = request.get("source_tracker") or request.get("source") or request.get("from")
    if not source:
        raise ServiceError("source_tracker or source is required for daily candidates.")
    target = _target_trackers(request)
    limit = int(request.get("limit") or DEFAULT_CANDIDATE_LIMIT)
    limit = max(1, min(limit, DEFAULT_CANDIDATE_LIMIT))
    return {
        "source_tracker": str(source),
        "target_trackers": target,
        "limit": limit,
        "config": request.get("config"),
        "base_dir": request.get("base_dir"),
        "accept_rules": bool(request.get("accept_rules")),
        "check_dupes": request.get("check_dupes", True) is not False,
    }


def _candidate_submit_item(digest: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    push_items = digest.get("push_items") if isinstance(digest.get("push_items"), list) else []
    rank = _bounded_int(request.get("rank") or request.get("candidate_rank"), default=1, minimum=1, maximum=DEFAULT_CANDIDATE_LIMIT)
    source_id = request.get("source_id") or request.get("candidate_source_id")
    selected: dict[str, Any] | None = None
    if source_id is not None:
        source_id_text = str(source_id)
        selected = next((item for item in push_items if isinstance(item, dict) and str(item.get("source_id") or "") == source_id_text), None)
    if selected is None:
        selected = next((item for item in push_items if isinstance(item, dict) and _candidate_item_rank(item) == rank), None)
    if selected is None:
        raise ServiceError(f"Candidate rank/source_id not found in digest: rank={rank}, source_id={source_id}", status=HTTPStatus.NOT_FOUND)
    return selected


def _candidate_item_rank(item: dict[str, Any]) -> int | None:
    try:
        return int(item.get("rank") or 0)
    except (TypeError, ValueError):
        return None


def _candidate_submit_overrides(request: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "accept_rules",
        "confirm_upload",
        "path",
        "content_path",
        "save_path",
        "output_dir",
        "source_torrent_file",
        "package_dir",
        "target_output_dir",
        "target_torrent_file",
        "target_torrent_output_dir",
        "uploaded_output_dir",
        "uploaded_torrent_id",
        "uploaded_torrent_file",
        "uploaded_save_path",
        "qbit_category",
        "qbit_tags",
        "qbit_upload_limit",
        "qbit_download_limit",
        "uploaded_qbit_category",
        "uploaded_qbit_tags",
        "uploaded_qbit_upload_limit",
        "uploaded_qbit_download_limit",
        "metadata_file",
        "ptgen_description_file",
        "mediainfo_file",
        "bdinfo_file",
        "image_host_file",
        "image_host",
        "bdinfo_playlist",
        "screenshot_count",
        "screenshot_file",
        "screenshot_files",
        "wait_timeout",
        "wait_interval",
        "uploaded_wait_timeout",
        "uploaded_wait_interval",
        "client",
        "config",
        "imdb_id",
        "tmdb_id",
        "tmdb_type",
        "douban_id",
        "douban_url",
        "enrich_metadata",
        "fetch_ptgen",
        "generate_bdinfo",
        "generate_mediainfo",
        "generate_screenshots",
        "upload_screenshots",
    }
    overrides: dict[str, Any] = {}
    nested = request.get("overrides")
    if isinstance(nested, dict):
        overrides.update({key: value for key, value in nested.items() if key in allowed_keys})
    overrides.update({key: value for key, value in request.items() if key in allowed_keys})
    return overrides


def _site_policy_request_context(request: dict[str, Any]) -> dict[str, Any]:
    raw_trackers = request.get("trackers") or request.get("tracker")
    roles: dict[str, set[str]] = {}
    if isinstance(raw_trackers, list):
        trackers = parse_tracker_list(",".join(str(item) for item in raw_trackers))
    elif raw_trackers:
        trackers = parse_tracker_list(str(raw_trackers))
    else:
        trackers = []
        source = request.get("source_tracker") or request.get("source") or request.get("from")
        target = request.get("target") or request.get("target_tracker") or request.get("target_trackers") or request.get("to")
        if source:
            source_trackers = parse_tracker_list(str(source))
            trackers.extend(source_trackers)
            _add_site_policy_roles(roles, source_trackers, "source")
        if isinstance(target, list):
            target_trackers = parse_tracker_list(",".join(str(item) for item in target))
            trackers.extend(target_trackers)
            _add_site_policy_roles(roles, target_trackers, "target")
        elif target:
            target_trackers = parse_tracker_list(str(target))
            trackers.extend(target_trackers)
            _add_site_policy_roles(roles, target_trackers, "target")
    deduped: list[str] = []
    seen: set[str] = set()
    for tracker in trackers:
        if tracker and tracker not in seen:
            deduped.append(tracker)
            seen.add(tracker)
    if not deduped:
        raise ServiceError("trackers or source/target is required for site policy report.")
    for tracker in deduped:
        roles.setdefault(tracker, {"unknown"})
    return {
        "trackers": deduped,
        "roles": {tracker: sorted(roles.get(tracker, {"unknown"})) for tracker in deduped},
        "config": request.get("config"),
        "accept_rules": _truthy(request.get("accept_rules")),
    }


def _sites_request_context(request: dict[str, Any]) -> dict[str, Any]:
    try:
        context = _site_policy_request_context(request)
    except ServiceError:
        context = {"trackers": sorted(CHINESE_PT_TRACKERS), "roles": {}, "config": request.get("config"), "accept_rules": _truthy(request.get("accept_rules"))}
    trackers = [tracker for tracker in context["trackers"] if tracker in CHINESE_PT_TRACKERS]
    unsupported = [tracker for tracker in context["trackers"] if tracker not in CHINESE_PT_TRACKERS]
    if not trackers:
        raise ServiceError("No supported Chinese PT trackers found in request.", status=HTTPStatus.BAD_REQUEST)
    roles = context.get("roles") if isinstance(context.get("roles"), dict) else {}
    return {
        "trackers": trackers,
        "roles": {tracker: _string_list(roles.get(tracker)) or ["unknown"] for tracker in trackers},
        "unsupported_trackers": unsupported,
        "config": context.get("config"),
        "accept_rules": bool(context.get("accept_rules")),
    }


def _site_capability_matrix_item(tracker: str, capability: dict[str, Any], policy_item: dict[str, Any] | None) -> dict[str, Any]:
    target_upload = bool(capability.get("target_upload"))
    roles = _string_list(policy_item.get("roles")) if isinstance(policy_item, dict) else []
    extension_checklist = _site_extension_checklist(tracker, capability, roles or ["unknown"])
    _apply_policy_profile_to_extension_checklist(extension_checklist, policy_item)
    adapter_profile = {
        "kind": "ptcli.site_adapter_profile",
        "tracker": tracker,
        "source_info": bool(capability.get("source_info")),
        "source_info_adapter": capability.get("source_info_adapter"),
        "source_download": bool(capability.get("source_download")),
        "source_download_adapter": capability.get("source_download_adapter"),
        "target_upload": target_upload,
        "target_upload_adapter": "mteam_api" if tracker == "MTEAM" and target_upload else None,
        "credential_requirements": _string_list(capability.get("credential_requirements")),
        "mteam_source_flow": bool(capability.get("mteam_source_flow")),
        "full_live_closure_to_mteam": bool(capability.get("full_live_closure_to_mteam")),
        "implemented_roles": _site_implemented_roles(capability),
        "extension_notes": _site_extension_notes(tracker, capability),
        "extension_checklist": extension_checklist,
    }
    policy_profile = policy_item.get("policy_profile") if isinstance(policy_item, dict) and isinstance(policy_item.get("policy_profile"), dict) else None
    execution_readiness = policy_item.get("execution_readiness") if isinstance(policy_item, dict) and isinstance(policy_item.get("execution_readiness"), dict) else None
    return {
        "tracker": tracker,
        "capabilities": capability,
        "adapter_profile": adapter_profile,
        "policy_profile": policy_profile,
        "execution_readiness": execution_readiness,
        "ready_for_source": bool(capability.get("source_info")) and bool(capability.get("source_download")),
        "ready_for_mteam_target_flow": bool(capability.get("full_live_closure_to_mteam")),
        "ready_as_target": target_upload,
    }


def _site_implemented_roles(capability: dict[str, Any]) -> list[str]:
    roles: list[str] = []
    if capability.get("source_info") or capability.get("source_download"):
        roles.append("source")
    if capability.get("target_upload"):
        roles.append("target")
    if capability.get("mteam_source_flow"):
        roles.append("mteam_source_flow")
    if capability.get("full_live_closure_to_mteam"):
        roles.append("full_live_closure_to_mteam")
    return roles


def _site_extension_notes(tracker: str, capability: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if not capability.get("source_info"):
        notes.append("Add source info adapter before using this tracker as a source.")
    if not capability.get("source_download"):
        notes.append("Add source torrent download adapter before automated source pulls.")
    if tracker != "MTEAM" and not capability.get("target_upload"):
        notes.append("Target upload adapter is not implemented yet; current live target closure is MTEAM-focused.")
    if capability.get("full_live_closure_to_mteam"):
        notes.append("Reference full live closure to MTEAM is available for this source tracker.")
    return notes


def _site_extension_checklist(tracker: str, capability: dict[str, Any], roles: list[str] | None = None) -> list[dict[str, Any]]:
    roles = roles or ["source", "target"]
    checklist: list[dict[str, Any]] = []
    if "source" in roles or "unknown" in roles:
        checklist.extend(
            [
                {
                    "key": "source_info_adapter",
                    "ready": bool(capability.get("source_info")),
                    "required_for": ["source_url_retorrent", "daily_candidates"],
                    "current_adapter": capability.get("source_info_adapter"),
                    "implementation_hint": "Add or wire a source metadata adapter that returns torrent id, title, IMDb/TMDb/Douban hints, details URL, and description evidence.",
                },
                {
                    "key": "source_download_adapter",
                    "ready": bool(capability.get("source_download")),
                    "required_for": ["source_url_retorrent", "daily_candidates", "qbit_source_injection"],
                    "current_adapter": capability.get("source_download_adapter"),
                    "implementation_hint": "Add a source torrent download adapter, usually cookie/passkey based for NexusPHP-style trackers.",
                },
            ]
        )
    if "target" in roles or "unknown" in roles:
        checklist.append(
            {
                "key": "target_upload_adapter",
                "ready": bool(capability.get("target_upload")),
                "required_for": ["target_upload", "full_live_closure"],
                "current_adapter": "mteam_api" if tracker == "MTEAM" and capability.get("target_upload") else capability.get("target_upload_adapter"),
                "implementation_hint": "Implement target upload, duplicate handling, uploaded torrent download, and target seeding evidence before enabling this tracker as a live target.",
            }
        )
    checklist.append(
        {
            "key": "policy_profile",
            "ready": False,
            "required_for": ["rule_gate", "qbit_rate_limits", "seeding_obligations"],
            "current_adapter": None,
            "implementation_hint": "Configure PTCLI.SITE_POLICIES with rule_review_fingerprint, role automation switches, qBittorrent rate limits, and seeding requirements.",
        }
    )
    return checklist


def _apply_policy_profile_to_extension_checklist(checklist: list[dict[str, Any]], policy_item: dict[str, Any] | None) -> None:
    policy_profile = policy_item.get("policy_profile") if isinstance(policy_item, dict) and isinstance(policy_item.get("policy_profile"), dict) else {}
    execution = policy_item.get("execution_readiness") if isinstance(policy_item, dict) and isinstance(policy_item.get("execution_readiness"), dict) else {}
    for item in checklist:
        if item.get("key") != "policy_profile":
            continue
        item["ready"] = execution.get("ready") is True
        item["missing_fields"] = policy_profile.get("missing_fields") if isinstance(policy_profile.get("missing_fields"), list) else []
        item["template"] = policy_profile.get("template") if isinstance(policy_profile.get("template"), dict) else {}


def _site_flow_matrix(flows: list[dict[str, Any]], trackers: list[str]) -> list[dict[str, Any]]:
    tracker_set = set(trackers)
    return [
        flow
        for flow in flows
        if str(flow.get("source_tracker")) in tracker_set and str(flow.get("target_tracker")) in tracker_set
    ]


def _sites_payload_blockers(capability_matrix: list[dict[str, Any]], context: dict[str, Any]) -> list[str]:
    blockers = [f"{tracker}: unsupported by Chinese PT allowlist." for tracker in _string_list(context.get("unsupported_trackers"))]
    for item in capability_matrix:
        tracker = item.get("tracker")
        adapter = item.get("adapter_profile") if isinstance(item.get("adapter_profile"), dict) else {}
        if not adapter.get("source_info") and not adapter.get("target_upload"):
            blockers.append(f"{tracker}: no source_info or target_upload adapter is available.")
    return list(dict.fromkeys(blockers))


def _sites_extension_plan(capability_matrix: list[dict[str, Any]], policy_matrix: list[dict[str, Any]], flow_matrix: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
    policy_by_tracker = {str(item.get("tracker")): item for item in policy_matrix if item.get("tracker")}
    items = [_site_extension_plan_item(item, policy_by_tracker.get(str(item.get("tracker"))), flow_matrix) for item in capability_matrix]
    ready_sources = [item["tracker"] for item in items if item.get("source_ready")]
    ready_targets = [item["tracker"] for item in items if item.get("target_ready")]
    reference_sources = [item["tracker"] for item in items if item.get("full_live_closure_to_mteam")]
    blockers = [blocker for item in items for blocker in _string_list(item.get("blockers"))]
    next_item = next((item for item in items if item.get("blockers")), None)
    return {
        "kind": "ptcli.site_extension_plan",
        "ready": not blockers,
        "trackers": _string_list(context.get("trackers")),
        "ready_sources": ready_sources,
        "ready_targets": ready_targets,
        "reference_sources_to_mteam": reference_sources,
        "items": items,
        "next_item": next_item,
        "blockers": list(dict.fromkeys(blockers)),
        "next_actions": _site_extension_plan_next_actions(next_item, blockers),
    }


def _sites_extension_handoff(extension_plan: dict[str, Any], flow_matrix: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
    items = extension_plan.get("items") if isinstance(extension_plan.get("items"), list) else []
    blocked_items = [item for item in items if isinstance(item, dict) and item.get("missing_components")]
    next_item = extension_plan.get("next_item") if isinstance(extension_plan.get("next_item"), dict) else (blocked_items[0] if blocked_items else None)
    reference_flow = _site_extension_reference_flow(flow_matrix, extension_plan)
    recommended_tracker = str(next_item.get("tracker")) if isinstance(next_item, dict) and next_item.get("tracker") else None
    return {
        "kind": "ptcli.site_extension_handoff",
        "ready": extension_plan.get("ready") is True,
        "phase": "validate_ready_sites" if extension_plan.get("ready") is True else "extend_site_adapter_or_policy",
        "recommended_next_tracker": recommended_tracker,
        "reference_flow": reference_flow,
        "implementation_order": _site_extension_implementation_order(items),
        "tracker_steps": _site_extension_tracker_steps(items),
        "endpoint_sequence": [
            {"tool": "site_profiles", "endpoint": "/v1/sites", "purpose": "inspect adapter/profile gaps for the requested Chinese PT trackers"},
            {"tool": "site_policies", "endpoint": "/v1/site-policies", "purpose": "copy SITE_POLICIES templates and confirm rule obligations"},
            {"tool": "readiness_bundle", "endpoint": "/v1/readiness/bundle", "purpose": "validate deployment, qBittorrent, credentials, policy gates, and live handoff"},
            {"tool": "source_url_retorrent_preflight", "endpoint": "/v1/retorrent/source-url/check", "purpose": "validate a concrete source URL before creating or resuming a live job"},
        ],
        "validation_sequence": [
            "adapter profile exposes required source_info/source_download/target_upload fields",
            "SITE_POLICIES has a non-placeholder rule_review_fingerprint and required rate/seeding fields",
            "readiness_bundle reports ready_for_ai=true for the concrete source and target",
            "source_url_retorrent_preflight reports ready_to_create_job=true before live automation",
        ],
        "continue_when": "extension_plan.ready=true and readiness_bundle.live_readiness.ready_for_ai=true",
        "stop_when": "extension_plan.blockers is non-empty or policy obligations require manual rule review",
        "request": {"trackers": _string_list(context.get("trackers")), "roles": context.get("roles")},
        "blockers": _string_list(extension_plan.get("blockers")),
        "next_actions": _string_list(extension_plan.get("next_actions")),
    }


def _site_extension_reference_flow(flow_matrix: list[dict[str, Any]], extension_plan: dict[str, Any]) -> dict[str, Any] | None:
    reference_sources = set(_string_list(extension_plan.get("reference_sources_to_mteam")))
    for flow in flow_matrix:
        if not isinstance(flow, dict):
            continue
        if flow.get("source_tracker") in reference_sources and flow.get("target_tracker") == "MTEAM":
            return flow
    return None


def _site_extension_implementation_order(items: list[Any]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        missing = _string_list(item.get("missing_components"))
        if not missing:
            continue
        ordered.append(
            {
                "tracker": item.get("tracker"),
                "roles": _string_list(item.get("roles")),
                "missing_components": missing,
                "first_step": _site_extension_item_next_action(str(item.get("tracker") or "tracker"), missing),
            }
        )
    return ordered


def _site_extension_tracker_steps(items: list[Any]) -> dict[str, Any]:
    steps: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("tracker"):
            continue
        tracker = str(item.get("tracker"))
        steps[tracker] = {
            "roles": _string_list(item.get("roles")),
            "ready": not _string_list(item.get("missing_components")),
            "missing_components": _string_list(item.get("missing_components")),
            "checklist": item.get("checklist") if isinstance(item.get("checklist"), list) else [],
            "next_action": item.get("next_action"),
        }
    return steps


def _site_extension_plan_item(capability_item: dict[str, Any], policy_item: dict[str, Any] | None, flow_matrix: list[dict[str, Any]]) -> dict[str, Any]:
    tracker = str(capability_item.get("tracker") or "")
    adapter = capability_item.get("adapter_profile") if isinstance(capability_item.get("adapter_profile"), dict) else {}
    roles = _string_list(policy_item.get("roles")) if isinstance(policy_item, dict) else ["unknown"]
    checklist = _site_extension_checklist(tracker, adapter, roles or ["unknown"])
    _apply_policy_profile_to_extension_checklist(checklist, policy_item)
    missing = [item["key"] for item in checklist if item.get("ready") is not True]
    blockers = [f"{tracker}: {key}" for key in missing]
    return {
        "tracker": tracker,
        "roles": roles or ["unknown"],
        "source_ready": bool(adapter.get("source_info")) and bool(adapter.get("source_download")),
        "target_ready": bool(adapter.get("target_upload")),
        "full_live_closure_to_mteam": bool(adapter.get("full_live_closure_to_mteam")),
        "has_reference_flow": any(flow.get("source_tracker") == tracker or flow.get("target_tracker") == tracker for flow in flow_matrix),
        "implemented_roles": _string_list(adapter.get("implemented_roles")),
        "missing_components": missing,
        "checklist": checklist,
        "blockers": blockers,
        "next_action": _site_extension_item_next_action(tracker, missing),
    }


def _site_extension_item_next_action(tracker: str, missing: list[str]) -> str:
    if not missing:
        return f"{tracker}: use readiness_bundle or source_url_retorrent_preflight for live validation."
    if "source_info_adapter" in missing:
        return f"{tracker}: implement source_info_adapter first so AI can inspect metadata and duplicate keys."
    if "source_download_adapter" in missing:
        return f"{tracker}: implement source_download_adapter and credential requirements before automated pulls."
    if "target_upload_adapter" in missing:
        return f"{tracker}: implement target_upload_adapter before using this tracker as a live target."
    return f"{tracker}: complete SITE_POLICIES fields and rule review before enabling automation."


def _site_extension_plan_next_actions(next_item: dict[str, Any] | None, blockers: list[str]) -> list[str]:
    if next_item:
        return [_site_extension_item_next_action(str(next_item.get("tracker") or "tracker"), _string_list(next_item.get("missing_components")))]
    if blockers:
        return ["Resolve extension_plan.items[].missing_components before enabling new site automation."]
    return ["Use extension_plan.reference_sources_to_mteam as implementation references for additional Chinese PT trackers."]


def _sites_agent_summary(capability_matrix: list[dict[str, Any]], policy_matrix: list[dict[str, Any]], flow_matrix: list[dict[str, Any]], blockers: list[str], extension_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ready": not blockers,
        "site_count": len(capability_matrix),
        "source_info_count": sum(1 for item in capability_matrix if (item.get("adapter_profile") or {}).get("source_info")),
        "source_download_count": sum(1 for item in capability_matrix if (item.get("adapter_profile") or {}).get("source_download")),
        "target_upload_count": sum(1 for item in capability_matrix if (item.get("adapter_profile") or {}).get("target_upload")),
        "full_live_closure_to_mteam_count": sum(1 for item in capability_matrix if (item.get("adapter_profile") or {}).get("full_live_closure_to_mteam")),
        "policy_profile_count": len(policy_matrix),
        "reference_flow_count": len(flow_matrix),
        "extension_ready": bool((extension_plan or {}).get("ready")),
        "extension_blocker_count": len(_string_list((extension_plan or {}).get("blockers"))) if isinstance(extension_plan, dict) else 0,
        "recommended_next_tool": "site_policies" if blockers else "readiness_bundle",
        "blocker_count": len(blockers),
    }


def _sites_next_actions(blockers: list[str], capability_matrix: list[dict[str, Any]], extension_plan: dict[str, Any] | None = None) -> list[str]:
    if blockers:
        return ["Review adapter_profiles and policy_profile templates before enabling live automation for blocked trackers."]
    extension_actions = _string_list((extension_plan or {}).get("next_actions")) if isinstance(extension_plan, dict) else []
    if extension_actions and (extension_plan or {}).get("ready") is not True:
        return extension_actions
    if any((item.get("adapter_profile") or {}).get("full_live_closure_to_mteam") for item in capability_matrix):
        return ["Use readiness_bundle or source_url_retorrent_preflight for a concrete source URL and target before live work."]
    return ["Pick a tracker with full_live_closure_to_mteam=true or implement the missing source/target adapter profile."]


def _qbit_inspect_request_context(request: dict[str, Any]) -> dict[str, Any]:
    torrent_hash = request.get("hash") or request.get("torrent_hash") or request.get("infohash")
    limit = _bounded_int(request.get("limit"), default=20, minimum=1, maximum=100)
    return {
        "config": request.get("config"),
        "client": str(request.get("client") or "default"),
        "hash": str(torrent_hash).strip().lower() if torrent_hash else None,
        "limit": limit,
    }


def _qbit_match_request_context(request: dict[str, Any]) -> dict[str, Any]:
    path = request.get("path") or request.get("content_path")
    if not path or not str(path).strip():
        raise ServiceError("path is required for qBittorrent match.", status=HTTPStatus.BAD_REQUEST)
    return {
        "config": request.get("config"),
        "client": str(request.get("client") or "default"),
        "path": str(path).strip(),
    }


def _qbit_export_request_context(request: dict[str, Any]) -> dict[str, Any]:
    torrent_hash = request.get("hash") or request.get("torrent_hash") or request.get("infohash")
    if not torrent_hash or not str(torrent_hash).strip():
        raise ServiceError("hash is required for qBittorrent export.", status=HTTPStatus.BAD_REQUEST)
    output_dir = request.get("output_dir") or request.get("target_torrent_output_dir") or "./tmp/exported"
    sanitize_for = str(request.get("sanitize_for") or request.get("target") or "MTEAM").strip().upper()
    if sanitize_for != "MTEAM":
        raise ServiceError("qBittorrent export currently supports sanitize_for=MTEAM only.", status=HTTPStatus.BAD_REQUEST)
    return {
        "config": request.get("config"),
        "client": str(request.get("client") or "default"),
        "hash": str(torrent_hash).strip().lower(),
        "output_dir": str(output_dir),
        "sanitize_for": sanitize_for,
    }


def _qbit_inject_request_context(request: dict[str, Any]) -> dict[str, Any]:
    torrent_file = request.get("torrent_file") or request.get("torrent_path")
    save_path = request.get("save_path")
    if not torrent_file or not str(torrent_file).strip():
        raise ServiceError("torrent_file is required for qBittorrent inject.", status=HTTPStatus.BAD_REQUEST)
    if not save_path or not str(save_path).strip():
        raise ServiceError("save_path is required for qBittorrent inject.", status=HTTPStatus.BAD_REQUEST)
    return {
        "config": request.get("config"),
        "client": str(request.get("client") or "default"),
        "torrent_file": str(torrent_file).strip(),
        "save_path": str(save_path).strip(),
        "category": _optional_nonempty_string(request.get("category")),
        "tags": _optional_tags(request.get("tags")),
        "upload_limit": _optional_rate_limit(request.get("upload_limit")),
        "download_limit": _optional_rate_limit(request.get("download_limit")),
        "paused": _truthy(request.get("paused")),
        "skip_checking": _truthy(request.get("skip_checking")),
        "verify_timeout": _bounded_float(request.get("verify_timeout"), default=5.0, minimum=0.0, maximum=3600.0),
        "verify_interval": _bounded_float(request.get("verify_interval"), default=0.5, minimum=0.1, maximum=300.0),
    }


def _qbit_wait_request_context(request: dict[str, Any]) -> dict[str, Any]:
    torrent_hash = request.get("hash") or request.get("torrent_hash") or request.get("infohash")
    path = request.get("path") or request.get("content_path")
    if (not torrent_hash or not str(torrent_hash).strip()) and (not path or not str(path).strip()):
        raise ServiceError("hash or path is required for qBittorrent wait.", status=HTTPStatus.BAD_REQUEST)
    return {
        "config": request.get("config"),
        "client": str(request.get("client") or "default"),
        "hash": str(torrent_hash).strip().lower() if torrent_hash else None,
        "path": str(path).strip() if path else None,
        "timeout": _bounded_float(request.get("timeout"), default=3600.0, minimum=0.0, maximum=86400.0),
        "interval": _bounded_float(request.get("interval"), default=30.0, minimum=0.1, maximum=3600.0),
    }


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ServiceError(f"Expected integer value, got {value!r}.", status=HTTPStatus.BAD_REQUEST) from exc
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ServiceError(f"Expected numeric value, got {value!r}.", status=HTTPStatus.BAD_REQUEST) from exc
    return max(minimum, min(maximum, parsed))


def _optional_nonempty_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _optional_tags(value: Any) -> str | None:
    if isinstance(value, list):
        tags = [str(item).strip() for item in value if str(item).strip()]
        return ",".join(tags) if tags else None
    return _optional_nonempty_string(value)


def _optional_rate_limit(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return parse_rate_limit(value)
    except ValueError as exc:
        raise ServiceError(str(exc), status=HTTPStatus.BAD_REQUEST) from exc


def _qbit_inspect_agent_summary(torrents: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
    return {
        "ready": True,
        "read_only": True,
        "client": context.get("client"),
        "query_hash": context.get("hash"),
        "torrent_count": len(torrents),
        "complete_count": sum(1 for torrent in torrents if _torrent_dict_complete(torrent)),
        "incomplete_count": sum(1 for torrent in torrents if not _torrent_dict_complete(torrent)),
        "hashes": [torrent.get("hash") for torrent in torrents if torrent.get("hash")],
    }


def _qbit_match_agent_summary(matches: list[dict[str, Any]], context: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    return {
        "ready": bool(matches),
        "read_only": True,
        "client": context.get("client"),
        "path": context.get("path"),
        "matched": bool(matches),
        "matched_count": len(matches),
        "complete_count": sum(1 for match in matches if _torrent_dict_complete(match)),
        "hashes": [match.get("hash") for match in matches if match.get("hash")],
        "blocker_count": len(blockers),
    }


def _torrent_dict_complete(torrent: dict[str, Any]) -> bool:
    progress = torrent.get("progress")
    if isinstance(progress, (int, float)) and progress >= 1:
        return True
    return str(torrent.get("state") or "").lower() in {"uploading", "stalled_up", "paused_up", "queued_up", "forced_up", "checking_up"}


def _qbit_inspect_next_actions(torrents: list[dict[str, Any]], context: dict[str, Any]) -> list[str]:
    if context.get("hash") and not torrents:
        return ["Check the requested hash or use qbit_match with a content path to locate the torrent."]
    if torrents:
        return ["Use hashes/content_path from torrents[] as evidence for source matching, export, wait, or resume steps."]
    return ["No torrents returned; verify qBittorrent config and client filter before relying on qBittorrent evidence."]


def _qbit_match_next_actions(matches: list[dict[str, Any]], context: dict[str, Any]) -> list[str]:
    if matches:
        return ["Use matches[].hash and matches[].content_path as qBittorrent evidence for retorrent pipeline or resume."]
    return [f"Inject or download the source torrent, then retry qbit_match for {context['path']}."]


def _qbit_export_file_evidence(exported_path: Path, candidate: dict[str, Any] | None) -> dict[str, Any]:
    candidate_path = Path(str(candidate.get("path"))).expanduser() if isinstance(candidate, dict) and candidate.get("path") else None
    return {
        "exported": _path_evidence(exported_path),
        "candidate": _path_evidence(candidate_path) if candidate_path else None,
        "candidate_mteam_safe": True if isinstance(candidate, dict) and candidate.get("source_flag") == "MTEAM" else None,
    }


def _qbit_export_target_upload_handoff(target_torrent_file: str, context: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    ready = bool((evidence.get("candidate") or evidence.get("exported") or {}).get("is_file"))
    return {
        "kind": "ptcli.qbit_export_target_upload_handoff",
        "ready": ready,
        "target": context.get("sanitize_for"),
        "target_torrent_file": target_torrent_file,
        "request_fields": {"target_torrent_file": target_torrent_file, "target_torrent_output_dir": context.get("output_dir")},
        "requires_before_upload": ["duplicate_check.exists=false", "accept_rules=true", "confirm_upload=true", "site_policy_ready=true", "target package/materials ready"],
        "next_step": {
            "tool": "source_url_retorrent_job",
            "endpoint": "/v1/jobs/retorrent/from-url",
            "method": "POST",
            "request": {"target_torrent_file": target_torrent_file, "target_torrent_output_dir": context.get("output_dir")},
        },
        "blockers": [] if ready else ["exported target torrent file is missing"],
    }


def _qbit_export_agent_summary(target_torrent_file: str, context: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "ready": bool((evidence.get("candidate") or evidence.get("exported") or {}).get("is_file")),
        "client": context.get("client"),
        "hash": context.get("hash"),
        "target": context.get("sanitize_for"),
        "target_torrent_file": target_torrent_file,
        "mteam_safe": evidence.get("candidate_mteam_safe"),
        "recommended_next_tool": "source_url_retorrent_job",
    }


def _qbit_inject_blockers(result: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if not result.get("visible_in_client"):
        blockers.append("Injected torrent is not visible in qBittorrent.")
    verification = result.get("client_verification") if isinstance(result.get("client_verification"), dict) else {}
    if not result.get("verified_in_client"):
        blockers.extend(
            f"Injected torrent client verification failed: {key}."
            for key in ("hash_matched", "save_path_matched", "category_matched", "tags_matched")
            if verification.get(key) is False
        )
        if not blockers:
            blockers.append("Injected torrent client metadata is not verified.")
    rate_limits = result.get("rate_limits") if isinstance(result.get("rate_limits"), dict) else {}
    requested_limits = rate_limits.get("requested") if isinstance(rate_limits.get("requested"), dict) else {}
    if any(value is not None for value in requested_limits.values()) and not rate_limits.get("applied"):
        blockers.append("Requested qBittorrent rate limits were not applied.")
    return list(dict.fromkeys(blockers))


def _qbit_inject_agent_summary(result: dict[str, Any], context: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    rate_limits = result.get("rate_limits") if isinstance(result.get("rate_limits"), dict) else {}
    return {
        "ready": not blockers,
        "mutates_qbittorrent": True,
        "client": context.get("client"),
        "hash": result.get("hash"),
        "torrent_file": result.get("torrent_path") or context.get("torrent_file"),
        "save_path": result.get("save_path") or context.get("save_path"),
        "category": result.get("category"),
        "tags": result.get("tags"),
        "visible_in_client": bool(result.get("visible_in_client")),
        "verified_in_client": bool(result.get("verified_in_client")),
        "rate_limits_requested": rate_limits.get("requested"),
        "rate_limits_applied": bool(rate_limits.get("applied")),
        "blocker_count": len(blockers),
    }


def _qbit_inject_next_actions(blockers: list[str]) -> list[str]:
    if blockers:
        return ["Inspect client_verification and qbit_inspect the returned hash before continuing the retorrent closure."]
    return ["Use hash with qbit_wait_complete, qbit_inspect, or a retorrent resume step that needs qBittorrent evidence."]


def _qbit_wait_agent_summary(result: dict[str, Any], context: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    verification = result.get("completion_verification") if isinstance(result.get("completion_verification"), dict) else {}
    return {
        "ready": result.get("complete") is True and not blockers,
        "read_only": True,
        "client": context.get("client"),
        "hash": context.get("hash"),
        "path": context.get("path"),
        "complete": result.get("complete") is True,
        "matched_count": result.get("matched_count", 0),
        "complete_count": verification.get("complete_count"),
        "observed_hashes": verification.get("observed_hashes", []),
        "observed_content_paths": verification.get("observed_content_paths", []),
        "blocker_count": len(blockers),
    }


def _qbit_wait_next_actions(result: dict[str, Any], blockers: list[str]) -> list[str]:
    if result.get("complete") is True and not blockers:
        return ["Use completion_verification and matches as qBittorrent evidence for the next retorrent or uploaded-seeding step."]
    if blockers:
        return ["Inspect completion_verification.observed_* and retry qbit_wait_complete with the observed hash/path if this was a query mismatch."]
    return ["Poll qbit_wait_complete again or use qbit_inspect to inspect current progress."]


def _path_evidence(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"path": None, "exists": False, "is_file": False, "size_bytes": 0}
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_file": path.is_file(),
        "size_bytes": path.stat().st_size if path.exists() and path.is_file() else 0,
    }


def _add_site_policy_roles(roles: dict[str, set[str]], trackers: list[str], role: str) -> None:
    for tracker in trackers:
        roles.setdefault(tracker, set()).add(role)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _daily_candidate_schedules_from_env() -> Any:
    raw = os.environ.get(DAILY_CANDIDATE_SCHEDULE_ENV)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceError(f"{DAILY_CANDIDATE_SCHEDULE_ENV} is not valid JSON: {exc.msg}") from exc


def _schedule_list(raw_schedules: Any) -> list[Any]:
    if isinstance(raw_schedules, dict) and isinstance(raw_schedules.get("schedules"), list):
        return raw_schedules["schedules"]
    if isinstance(raw_schedules, list):
        return raw_schedules
    if raw_schedules in (None, ""):
        return []
    return [raw_schedules]


def _normalized_daily_candidate_schedule(schedule: dict[str, Any], *, index: int) -> dict[str, Any]:
    request = _candidate_request_context(schedule)
    enabled = schedule.get("enabled", True) is not False
    schedule_time = str(schedule.get("time") or schedule.get("at") or schedule.get("schedule_time") or "09:00")
    timezone = str(schedule.get("timezone") or os.environ.get("TZ") or "Asia/Shanghai")
    name = str(schedule.get("name") or f"{request['source_tracker']}-to-{request['target_trackers']}-daily")
    return {
        "name": name,
        "enabled": enabled,
        "schedule": {
            "frequency": "daily",
            "time": schedule_time,
            "timezone": timezone,
        },
        "job_endpoint": "/v1/jobs/candidates/daily",
        "job_tool": "daily_candidates_job",
        "job_request": request,
        "poll_with": "get_job_status",
        "read_digest_from": ["candidate_digest", "agent_summary.digest", "result.digest"],
        "submit_top_candidate_with": "source_url_retorrent_job",
        "push_contract": {
            "items": "candidate_digest.push_items",
            "top_candidate": "candidate_digest.top_candidate",
            "top_submit_request": "candidate_digest.top_submit_request",
            "agent_decision": "agent_decision",
        },
        "blockers": [] if enabled else ["schedule is disabled"],
        "source_index": index,
    }


def _daily_candidate_schedule_next_actions(schedules: list[dict[str, Any]], blockers: list[str]) -> list[str]:
    if blockers and not schedules:
        return [f"Set {DAILY_CANDIDATE_SCHEDULE_ENV} to a JSON array of daily candidate schedules, or POST schedules to /v1/candidates/daily/schedule."]
    actions = ["Create daily jobs by POSTing each enabled schedule.job_request to /v1/jobs/candidates/daily, then poll job status and read candidate_digest."]
    if any(schedule.get("enabled") is False for schedule in schedules):
        actions.append("Enable disabled schedules before expecting daily candidate output.")
    return actions


def _daily_candidate_schedule_job_digest(jobs: list[dict[str, Any]], skipped: list[dict[str, Any]], blockers: list[str]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    push_items: list[dict[str, Any]] = []
    top_submit_requests: list[dict[str, Any]] = []
    submission_items: list[dict[str, Any]] = []
    approval_items: list[dict[str, Any]] = []
    for job in jobs:
        digest = job.get("candidate_digest") if isinstance(job.get("candidate_digest"), dict) else {}
        decision = job.get("agent_decision") if isinstance(job.get("agent_decision"), dict) else {}
        request = job.get("job_request") if isinstance(job.get("job_request"), dict) else {}
        top_candidate = digest.get("top_candidate") if isinstance(digest.get("top_candidate"), dict) else {}
        top_submit_request = digest.get("top_submit_request") if isinstance(digest.get("top_submit_request"), dict) else None
        digest_push_items = digest.get("push_items") if isinstance(digest.get("push_items"), list) else []
        digest_approval_queue = digest.get("approval_queue") if isinstance(digest.get("approval_queue"), dict) else {}
        digest_safe_candidates = digest.get("top_safe_candidates") if isinstance(digest.get("top_safe_candidates"), list) else digest_approval_queue.get("top_safe_candidates") if isinstance(digest_approval_queue.get("top_safe_candidates"), list) else []
        selected_count = int(digest.get("selected_count") or len(digest_push_items))
        item = {
            "schedule_name": job.get("schedule_name"),
            "job_id": job.get("job_id"),
            "status": job.get("status"),
            "ok": job.get("ok"),
            "source_tracker": request.get("source_tracker"),
            "target_trackers": request.get("target_trackers"),
            "status_endpoint": job.get("status_endpoint"),
            "summary_endpoint": job.get("summary_endpoint"),
            "recommendation": digest.get("recommendation"),
            "target_count": int(digest.get("target_count") or request.get("limit") or DEFAULT_CANDIDATE_LIMIT),
            "scan_count": int(digest.get("scan_count") or 0),
            "selected_count": selected_count,
            "ready_count": int(digest.get("ready_count") or decision.get("ready_count") or 0),
            "shortfall_count": int(digest.get("shortfall_count") or 0),
            "target_met": bool(digest.get("target_met")),
            "target_summary": digest.get("target_summary") if isinstance(digest.get("target_summary"), dict) else None,
            "top_candidate": top_candidate or None,
            "top_submit_request": top_submit_request,
            "top_submit_job_endpoint": digest.get("top_submit_job_endpoint"),
            "top_submit_tool": digest.get("top_submit_tool"),
            "approval_queue": digest_approval_queue or None,
            "top_safe_candidates": digest_safe_candidates,
            "agent_decision": decision.get("decision"),
            "can_submit_job": bool(decision.get("can_submit_job")),
            "missing_confirmations": _string_list(decision.get("missing_confirmations")),
            "push_count": len(digest_push_items),
            "blockers": _string_list(decision.get("blockers") or digest.get("blockers")),
            "next_actions": _string_list(digest.get("next_actions")),
        }
        items.append(item)
        push_items.extend(
            {
                "schedule_name": job.get("schedule_name"),
                "job_id": job.get("job_id"),
                "status_endpoint": job.get("status_endpoint"),
                "summary_endpoint": job.get("summary_endpoint"),
                **push_item,
            }
            for push_item in digest_push_items
            if isinstance(push_item, dict)
        )
        if top_submit_request and item["can_submit_job"]:
            top_submit_requests.append(
                {
                    "schedule_name": job.get("schedule_name"),
                    "job_id": job.get("job_id"),
                    "submit_tool": digest.get("top_submit_tool") or "source_url_retorrent_job",
                    "submit_endpoint": digest.get("top_submit_job_endpoint") or "/v1/jobs/retorrent/from-url",
                    "request": top_submit_request,
                    "missing_confirmations": item["missing_confirmations"],
                }
            )
        safe_sources = [candidate for candidate in digest_safe_candidates if isinstance(candidate, dict)]
        if item["can_submit_job"] and safe_sources:
            for safe_candidate in safe_sources:
                digest_item = _daily_candidate_push_item_for_safe_candidate(digest_push_items, safe_candidate)
                submission_source = {
                    **digest_item,
                    **safe_candidate,
                    "submit_request": safe_candidate.get("request") if isinstance(safe_candidate.get("request"), dict) else digest_item.get("submit_request"),
                }
                submission_item = _daily_candidate_schedule_submission_item(job, request, submission_source, {**item, **digest_item})
                submission_items.append(submission_item)
                approval_items.append(_daily_candidate_schedule_approval_item(submission_item, safe_candidate, item))
        elif top_submit_request and item["can_submit_job"]:
            submission_source = {
                **(digest_push_items[0] if digest_push_items and isinstance(digest_push_items[0], dict) else {}),
                **top_candidate,
            }
            submission_item = _daily_candidate_schedule_submission_item(job, request, submission_source, item)
            submission_items.append(submission_item)
            approval_items.append(_daily_candidate_schedule_approval_item(submission_item, submission_source, item))
    approval_queue = _daily_candidate_schedule_approval_queue(approval_items, items, blockers)
    submission_handoff = _daily_candidate_schedule_submission_handoff(submission_items, blockers, approval_queue=approval_queue)
    push_payload = _daily_candidate_schedule_push_payload(push_items, items, submission_handoff, blockers, approval_queue=approval_queue)
    target_count = sum(int(item.get("target_count") or 0) for item in items)
    selected_count = sum(int(item.get("selected_count") or 0) for item in items)
    ready_count = sum(int(item.get("ready_count") or 0) for item in items)
    shortfall_count = sum(max(0, int(item.get("target_count") or 0) - int(item.get("selected_count") or 0)) for item in items)
    return {
        "kind": "ptcli.daily_candidate_schedule_digest",
        "job_count": len(jobs),
        "ready_job_count": sum(1 for item in items if item.get("ready_count", 0) > 0),
        "pending_job_count": sum(1 for item in items if item.get("status") in {"queued", "running"}),
        "blocked_job_count": sum(1 for item in items if item.get("status") in {"blocked", "failed"} or item.get("blockers")),
        "skipped_count": len(skipped),
        "target_count": target_count,
        "selected_count": selected_count,
        "ready_count": ready_count,
        "shortfall_count": shortfall_count,
        "target_met": bool(items) and selected_count >= target_count,
        "push_count": len(push_items),
        "push_payload": push_payload,
        "approval_queue": approval_queue,
        "top_safe_candidates": approval_queue["top_safe_candidates"],
        "submit_request_count": len(top_submit_requests),
        "items": items,
        "push_items": push_items,
        "top_submit_requests": top_submit_requests,
        "submission_handoff": submission_handoff,
        "skipped": skipped,
        "blockers": blockers,
}


def _daily_candidate_push_item_for_safe_candidate(push_items: list[Any], safe_candidate: dict[str, Any]) -> dict[str, Any]:
    source_id = safe_candidate.get("source_id")
    rank = safe_candidate.get("rank")
    for item in push_items:
        if not isinstance(item, dict):
            continue
        if source_id is not None and item.get("source_id") == source_id:
            return item
        if rank is not None and item.get("rank") == rank:
            return item
    return {}


def _daily_candidate_schedule_approval_queue(approval_items: list[dict[str, Any]], schedule_items: list[dict[str, Any]], blockers: list[str]) -> dict[str, Any]:
    pending_count = sum(1 for item in schedule_items if item.get("status") in {"queued", "running"})
    blocked_source_ids = [
        item.get("source_id")
        for item in schedule_items
        if item.get("source_id") and (item.get("blockers") or item.get("can_submit_job") is not True)
    ]
    next_actions = ["Ask the user to approve one approval_queue.items[] entry, then POST its request_template to submit_daily_candidate_job."]
    if not approval_items and pending_count:
        next_actions = ["Poll pending schedule jobs, then refresh schedule_digest.approval_queue."]
    elif not approval_items:
        next_actions = ["Resolve schedule or candidate blockers before submitting any daily candidate."]
    return {
        "kind": "ptcli.daily_candidate_schedule_approval_queue",
        "ready": bool(approval_items) and not blockers,
        "safe_count": len(approval_items),
        "guarded_count": 0,
        "blocked_count": len(blocked_source_ids) + len(blockers),
        "pending_job_count": pending_count,
        "recommended_count": len(approval_items),
        "items": approval_items,
        "top_safe_candidates": approval_items,
        "blocked_source_ids": blocked_source_ids,
        "submit_tool": "submit_daily_candidate_job",
        "submit_endpoint_template": "/v1/jobs/candidates/{candidate_job_id}/submit",
        "requires_confirmation": ["confirm_upload=true", "save_path or path"],
        "continue_when": "approval_queue.ready=true and the user approves one approval_queue.items[] entry",
        "stop_when": ["approval_queue.blockers is non-empty", "candidate duplicate_clear=false", "candidate policy_risk_level=high"],
        "blockers": blockers,
        "next_actions": next_actions,
    }


def _daily_candidate_schedule_approval_item(submission_item: dict[str, Any], source_item: dict[str, Any], schedule_item: dict[str, Any]) -> dict[str, Any]:
    policy_risk_summary = source_item.get("policy_risk_summary") if isinstance(source_item.get("policy_risk_summary"), dict) else {}
    decision_summary = source_item.get("decision_summary") if isinstance(source_item.get("decision_summary"), dict) else {}
    return {
        "schedule_name": schedule_item.get("schedule_name") or submission_item.get("schedule_name"),
        "candidate_job_id": submission_item.get("candidate_job_id"),
        "rank": (submission_item.get("selector") or {}).get("rank") if isinstance(submission_item.get("selector"), dict) else source_item.get("rank"),
        "source_tracker": source_item.get("source_tracker") or schedule_item.get("source_tracker"),
        "target_trackers": schedule_item.get("target_trackers"),
        "source_id": source_item.get("source_id"),
        "source_url": source_item.get("source_url"),
        "title": source_item.get("title"),
        "score": source_item.get("score"),
        "risk_level": source_item.get("risk_level") or decision_summary.get("risk_level"),
        "policy_risk_level": source_item.get("policy_risk_level") or policy_risk_summary.get("risk_level"),
        "execution_priority": source_item.get("execution_priority") or policy_risk_summary.get("execution_priority"),
        "duplicate_clear": source_item.get("duplicate_clear") if "duplicate_clear" in source_item else decision_summary.get("duplicate_clear"),
        "metadata": source_item.get("metadata") if isinstance(source_item.get("metadata"), dict) else {},
        "policy_risk_summary": policy_risk_summary,
        "submit_tool": submission_item.get("submit_tool"),
        "submit_endpoint": submission_item.get("submit_endpoint"),
        "request_template": submission_item.get("request_template"),
        "source_url_retorrent_request": source_item.get("request") if isinstance(source_item.get("request"), dict) else source_item.get("submit_request") if isinstance(source_item.get("submit_request"), dict) else None,
        "requires_confirmation": submission_item.get("required_overrides") if isinstance(submission_item.get("required_overrides"), list) else ["confirm_upload=true", "save_path or path"],
        "after_submit": submission_item.get("after_submit") if isinstance(submission_item.get("after_submit"), dict) else {},
    }


def _daily_candidate_schedule_push_payload(push_items: list[dict[str, Any]], items: list[dict[str, Any]], submission_handoff: dict[str, Any], blockers: list[str], *, approval_queue: dict[str, Any]) -> dict[str, Any]:
    ready_items = [item for item in push_items if item.get("can_submit") is True]
    blocked_items = [item for item in push_items if item.get("can_submit") is not True]
    pending_count = sum(1 for item in items if item.get("status") in {"queued", "running"})
    target_count = sum(int(item.get("target_count") or 0) for item in items)
    selected_count = sum(int(item.get("selected_count") or 0) for item in items)
    shortfall_count = sum(max(0, int(item.get("target_count") or 0) - int(item.get("selected_count") or 0)) for item in items)
    title = "Daily PT candidate schedule"
    summary = f"{selected_count}/{target_count} candidate(s) across {len(items)} schedule job(s): {len(ready_items)} ready, {len(blocked_items)} blocked/review, {pending_count} pending, shortfall {shortfall_count}."
    lines = [summary, *[str(item.get("summary_text")) for item in push_items if item.get("summary_text")]]
    if submission_handoff.get("ready"):
        recommended_action = "Review push_payload.items and submit approved entries through submission_handoff.items[].submit_endpoint."
    elif pending_count:
        recommended_action = "Poll pending schedule jobs, then read schedule_digest.push_payload again."
    else:
        recommended_action = "Resolve blockers or adjust daily candidate schedules before submitting."
    return {
        "kind": "ptcli.daily_candidate_schedule_push_payload",
        "title": title,
        "summary": summary,
        "message": "\n".join(lines),
        "format": "text/plain",
        "schedule_job_count": len(items),
        "target_count": target_count,
        "selected_count": selected_count,
        "shortfall_count": shortfall_count,
        "target_met": bool(items) and selected_count >= target_count,
        "item_count": len(push_items),
        "ready_count": len(ready_items),
        "blocked_count": len(blocked_items),
        "pending_job_count": pending_count,
        "submission_ready": bool(submission_handoff.get("ready")),
        "approval_queue": approval_queue,
        "top_safe_candidates": approval_queue["top_safe_candidates"],
        "recommended_action": recommended_action,
        "top_item": ready_items[0] if ready_items else push_items[0] if push_items else None,
        "items": push_items,
        "blockers": blockers,
        "next_actions": [recommended_action],
    }


def _daily_candidate_schedule_notification_payload(schedule_digest: dict[str, Any], agent_decision: dict[str, Any]) -> dict[str, Any]:
    push_payload = schedule_digest.get("push_payload") if isinstance(schedule_digest.get("push_payload"), dict) else {}
    submission_handoff = schedule_digest.get("submission_handoff") if isinstance(schedule_digest.get("submission_handoff"), dict) else {}
    approval_queue = schedule_digest.get("approval_queue") if isinstance(schedule_digest.get("approval_queue"), dict) else push_payload.get("approval_queue") if isinstance(push_payload.get("approval_queue"), dict) else {}
    execution_summary = submission_handoff.get("execution_summary") if isinstance(submission_handoff.get("execution_summary"), dict) else {}
    top_item = push_payload.get("top_item") if isinstance(push_payload.get("top_item"), dict) else None
    items = push_payload.get("items") if isinstance(push_payload.get("items"), list) else []
    ready_items = [item for item in items if isinstance(item, dict) and item.get("can_submit") is True]
    blocked_items = [item for item in items if isinstance(item, dict) and item.get("can_submit") is not True]
    pending_count = int(schedule_digest.get("pending_job_count") or 0)
    target_count = int(schedule_digest.get("target_count") or push_payload.get("target_count") or 0)
    selected_count = int(schedule_digest.get("selected_count") or push_payload.get("selected_count") or 0)
    shortfall_count = int(schedule_digest.get("shortfall_count") or push_payload.get("shortfall_count") or 0)
    submit_items = submission_handoff.get("items") if isinstance(submission_handoff.get("items"), list) else []
    submission_ready = bool(submission_handoff.get("ready"))
    ready_count = len(ready_items) if ready_items else len(submit_items) if submission_ready else 0
    return {
        "kind": "ptcli.daily_candidate_notification_payload",
        "channel": "daily_candidates",
        "format": "text/plain",
        "title": push_payload.get("title") or "Daily PT candidate schedule",
        "summary": push_payload.get("summary") or "",
        "message": push_payload.get("message") or "",
        "status": "ready" if submission_ready and not schedule_digest.get("blockers") else "pending" if pending_count else "blocked" if schedule_digest.get("blockers") or blocked_items else "empty",
        "ready": submission_ready and pending_count == 0 and not schedule_digest.get("blockers"),
        "submission_ready": submission_ready,
        "recommended_action": agent_decision.get("recommended_action") or push_payload.get("recommended_action"),
        "decision": agent_decision.get("decision"),
        "counts": {
            "schedule_jobs": schedule_digest.get("job_count", 0),
            "ready_jobs": schedule_digest.get("ready_job_count", 0),
            "pending_jobs": pending_count,
            "blocked_jobs": schedule_digest.get("blocked_job_count", 0),
            "target_candidates": target_count,
            "candidates": schedule_digest.get("push_count", 0),
            "selected_candidates": selected_count,
            "ready_candidates": ready_count,
            "safe_candidates": approval_queue.get("safe_count", 0),
            "blocked_candidates": len(blocked_items),
            "shortfall_candidates": shortfall_count,
            "target_met": bool(schedule_digest.get("target_met")),
            "submit_requests": schedule_digest.get("submit_request_count", 0),
        },
        "top_item": top_item,
        "items": items,
        "approval_queue": approval_queue,
        "top_safe_candidates": approval_queue.get("top_safe_candidates", []),
        "submission_handoff": submission_handoff,
        "execution_summary": execution_summary,
        "next_step": submission_handoff.get("next_step"),
        "recommended_tool": submission_handoff.get("recommended_tool"),
        "recommended_endpoint": submission_handoff.get("recommended_endpoint"),
        "recommended_request": submission_handoff.get("recommended_request"),
        "submit_items": submit_items,
        "top_submit_requests": schedule_digest.get("top_submit_requests", []),
        "blockers": _string_list(schedule_digest.get("blockers")),
        "next_actions": _daily_candidate_notification_next_actions(agent_decision, submission_handoff, pending_count),
    }


def _daily_candidate_schedule_delivery_handoff(schedule_digest: dict[str, Any], notification_payload: dict[str, Any], agent_decision: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    submission_handoff = schedule_digest.get("submission_handoff") if isinstance(schedule_digest.get("submission_handoff"), dict) else {}
    approval_queue = schedule_digest.get("approval_queue") if isinstance(schedule_digest.get("approval_queue"), dict) else notification_payload.get("approval_queue") if isinstance(notification_payload.get("approval_queue"), dict) else {}
    execution_summary = submission_handoff.get("execution_summary") if isinstance(submission_handoff.get("execution_summary"), dict) else {}
    pending_count = int(schedule_digest.get("pending_job_count") or 0)
    target_count = int(schedule_digest.get("target_count") or 0)
    selected_count = int(schedule_digest.get("selected_count") or 0)
    notification_counts = notification_payload.get("counts") if isinstance(notification_payload.get("counts"), dict) else {}
    ready_count = int(schedule_digest.get("ready_count") or notification_counts.get("ready_candidates") or 0)
    shortfall_count = int(schedule_digest.get("shortfall_count") or 0)
    target_met = bool(schedule_digest.get("target_met"))
    submission_ready = bool(submission_handoff.get("ready"))
    publish_ready = bool(notification_payload.get("ready")) and pending_count == 0
    return {
        "kind": "ptcli.daily_candidate_delivery_handoff",
        "ready": publish_ready,
        "publish_ready": publish_ready,
        "submission_ready": submission_ready,
        "target_met": target_met,
        "status": notification_payload.get("status"),
        "recommended_tool": submission_handoff.get("recommended_tool") if submission_ready else "get_job_status" if pending_count else "daily_candidates_schedule_job",
        "recommended_endpoint": submission_handoff.get("recommended_endpoint") if submission_ready else None,
        "recommended_request": submission_handoff.get("recommended_request") if submission_ready else None,
        "counts": {
            "target_candidates": target_count,
            "selected_candidates": selected_count,
            "ready_candidates": ready_count,
            "safe_candidates": approval_queue.get("safe_count", 0),
            "shortfall_candidates": shortfall_count,
            "pending_jobs": pending_count,
            "blocked_jobs": schedule_digest.get("blocked_job_count", 0),
            "submit_requests": schedule_digest.get("submit_request_count", 0),
        },
        "notification_payload": notification_payload,
        "approval_queue": approval_queue,
        "top_safe_candidates": approval_queue.get("top_safe_candidates", []),
        "submission_handoff": submission_handoff,
        "execution_summary": execution_summary,
        "top_submit_requests": schedule_digest.get("top_submit_requests", []),
        "publish_contract": {
            "payload_field": "delivery_handoff.notification_payload",
            "format": notification_payload.get("format") or "text/plain",
            "target": "external webhook, IM bridge, OpenClaw/Hermes message, or local file",
            "safe_to_publish": publish_ready,
        },
        "continue_when": "delivery_handoff.publish_ready=true and user approves submitting a candidate through submission_handoff",
        "stop_when": [
            "delivery_handoff.blockers is non-empty",
            "submission_handoff.ready=false and pending_jobs=0",
            "candidate policy_execution.ready=false",
        ],
        "blockers": list(dict.fromkeys(_string_list(blockers) + _string_list(schedule_digest.get("blockers")))),
        "next_actions": _daily_candidate_delivery_next_actions(publish_ready, submission_ready, pending_count, shortfall_count, agent_decision),
    }


def _daily_candidate_delivery_next_actions(publish_ready: bool, submission_ready: bool, pending_count: int, shortfall_count: int, agent_decision: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if publish_ready:
        actions.append("Publish delivery_handoff.notification_payload to the configured daily candidate channel or local handoff file.")
    if submission_ready:
        actions.append("After explicit user approval, submit an approved item via delivery_handoff.submission_handoff.items[].request_template.")
    elif pending_count:
        actions.append("Poll pending daily candidate jobs before publishing or submitting.")
    else:
        actions.append("Inspect delivery_handoff.blockers and schedule_digest.push_items before rerunning the schedule.")
    if shortfall_count:
        actions.append(f"Daily target is short by {shortfall_count} candidate(s); treat the push as partial coverage.")
    actions.extend(_string_list(agent_decision.get("recommended_action")))
    return list(dict.fromkeys(action for action in actions if action))


def _daily_candidate_notification_next_actions(agent_decision: dict[str, Any], submission_handoff: dict[str, Any], pending_count: int) -> list[str]:
    actions = _string_list(agent_decision.get("recommended_action"))
    if submission_handoff.get("ready"):
        actions.append("Review notification_payload.submit_items, then POST an approved item request_template to submit_endpoint with explicit user confirmation.")
    elif pending_count:
        actions.append("Poll notification_payload.items[].status_endpoint until pending jobs finish, then refresh the daily schedule summary.")
    elif not actions:
        actions.append("Inspect notification_payload.items and rerun the daily schedule later.")
    return list(dict.fromkeys(actions))


def _job_candidate_batch_handoff(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if str(job.get("kind") or "") != "ptcli.daily_candidates":
        return None
    job_id = str(job.get("job_id") or "")
    status = str(job.get("status") or "")
    digest = _candidate_digest_from_payload(summary_payload) or _candidate_digest_from_payload(job.get("result")) or {}
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    push_items = digest.get("push_items") if isinstance(digest.get("push_items"), list) else []
    blockers = list(dict.fromkeys([*_string_list(job.get("blockers")), *_string_list(digest.get("blockers"))]))
    if status in {"queued", "running"}:
        blockers.append(f"Candidate job {job_id} is still {status}; poll before submitting candidates.")
    submission_items: list[dict[str, Any]] = []
    for item in push_items:
        if not isinstance(item, dict) or item.get("can_submit") is not True:
            continue
        enriched_item = {
            "source_tracker": request.get("source_tracker"),
            "target_trackers": request.get("target_trackers") or request.get("target"),
            **item,
        }
        submission_items.append(_daily_candidate_schedule_submission_item(_job_submission_stub(job), request, enriched_item, enriched_item))
    next_step = _daily_candidate_submission_next_step(submission_items, blockers)
    return {
        "kind": "ptcli.daily_candidate_batch_handoff",
        "ready": bool(submission_items) and not blockers,
        "candidate_job_id": job_id or None,
        "status": status or None,
        "submit_count": len(submission_items),
        "submit_tool": "submit_daily_candidate_job",
        "submit_endpoint": f"/v1/jobs/candidates/{job_id}/submit" if job_id else None,
        "submit_endpoint_template": "/v1/jobs/candidates/{candidate_job_id}/submit",
        "preferred_flow": "Review candidate_digest.push_items, choose an item with can_submit=true, then POST its request_template to submit_daily_candidate_job. The source and target identity are inherited from the completed daily candidate job.",
        "required_overrides": ["confirm_upload=true", "save_path or path"],
        "allowed_selector_fields": ["rank", "source_id"],
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "items": submission_items,
        "blockers": list(dict.fromkeys(blockers)),
        "next_actions": _job_candidate_batch_next_actions(status, submission_items, blockers),
    }


def _job_submission_stub(job: dict[str, Any]) -> dict[str, Any]:
    job_id = job.get("job_id")
    return {
        "job_id": job_id,
        "schedule_name": None,
        "status": job.get("status"),
        "status_endpoint": f"/v1/jobs/{job_id}" if job_id else None,
        "summary_endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None,
    }


def _job_candidate_batch_next_actions(status: str, submission_items: list[dict[str, Any]], blockers: list[str]) -> list[str]:
    if status in {"queued", "running"}:
        return ["Poll this daily candidate job until it completes, then read candidate_batch_handoff again."]
    if blockers:
        return ["Resolve candidate_batch_handoff.blockers before submitting a candidate."]
    if submission_items:
        return ["Review candidate_batch_handoff.items, keep confirm_upload explicit, set save_path or path, then POST the chosen request_template to candidate_batch_handoff.recommended_endpoint."]
    return ["No submittable daily candidates are available; inspect candidate_digest.push_items[].blockers or rerun the daily candidate scan later."]


def _daily_candidate_schedule_submission_item(job: dict[str, Any], request: dict[str, Any], top_candidate: dict[str, Any], digest_item: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    selector = {
        "rank": top_candidate.get("rank"),
        "source_id": top_candidate.get("source_id"),
    }
    return {
        "schedule_name": job.get("schedule_name"),
        "candidate_job_id": job_id,
        "submit_tool": "submit_daily_candidate_job",
        "submit_endpoint": f"/v1/jobs/candidates/{job_id}/submit",
        "method": "POST",
        "selector": {key: value for key, value in selector.items() if value not in {None, ""}},
        "request_template": {
            **({key: value for key, value in selector.items() if value not in {None, ""}}),
            "confirm_upload": True,
            "save_path": "/downloads",
            "overrides": {
                "uploaded_qbit_category": request.get("target_trackers") or request.get("target"),
                "uploaded_qbit_tags": "retorrent",
            },
        },
        "identity_inherited_from_candidate": {
            "source_tracker": digest_item.get("source_tracker"),
            "target_trackers": digest_item.get("target_trackers"),
            "source_id": top_candidate.get("source_id"),
            "title": top_candidate.get("title"),
        },
        "policy_execution": _daily_candidate_submission_policy_execution({**digest_item, **top_candidate}),
        "required_overrides": ["confirm_upload=true", "save_path or path"],
        "allowed_overrides": [
            "confirm_upload",
            "path",
            "save_path",
            "source_torrent_file",
            "target_torrent_file",
            "uploaded_torrent_file",
            "qbit_category",
            "qbit_tags",
            "qbit_upload_limit",
            "qbit_download_limit",
            "uploaded_qbit_category",
            "uploaded_qbit_tags",
            "uploaded_qbit_upload_limit",
            "uploaded_qbit_download_limit",
            "metadata/imdb/tmdb/douban/material overrides",
        ],
        "missing_confirmations": _string_list(digest_item.get("missing_confirmations")),
        "after_submit": {
            "read_fields": ["job_handoff", "job_handoff.action", "job_handoff.recommended_tool", "job_handoff.recommended_request", "job_handoff.material_input_template", "candidate_submission_summary", "candidate_submission_summary.execution_state", "candidate_submission_summary.execution_handoff", "candidate_submission_handoff", "manual_retorrent_handoff", "materials_handoff", "agent_decision", "status_endpoint", "summary_endpoint"],
            "poll_with": "get_job_status",
            "summary_with": "get_job_summary",
            "stop_when": ["job_handoff.action=stop_duplicate", "job_handoff.action=collect_confirmations", "job_handoff.action=configure_policy"],
            "resume_when": "job_handoff.recommended_tool=resume_job and job_handoff.recommended_request is present",
            "material_resume_request": "job_handoff.recommended_request when job_handoff.action=prepare_materials",
        },
        "status_endpoint": job.get("status_endpoint"),
        "summary_endpoint": job.get("summary_endpoint"),
    }


def _daily_candidate_submission_policy_execution(digest_item: dict[str, Any]) -> dict[str, Any]:
    policy_summary = digest_item.get("policy_summary") if isinstance(digest_item.get("policy_summary"), dict) else {}
    policy_coverage = policy_summary.get("policy_coverage") if isinstance(policy_summary.get("policy_coverage"), dict) else {}
    policy_execution_handoff = digest_item.get("policy_execution_handoff") if isinstance(digest_item.get("policy_execution_handoff"), dict) else policy_summary.get("policy_execution_handoff") if isinstance(policy_summary.get("policy_execution_handoff"), dict) else {}
    submit_request = digest_item.get("submit_request") if isinstance(digest_item.get("submit_request"), dict) else {}
    qbit_keys = ("qbit_upload_limit", "qbit_download_limit", "uploaded_qbit_upload_limit", "uploaded_qbit_download_limit", "qbit_category", "qbit_tags", "uploaded_qbit_category", "uploaded_qbit_tags")
    return {
        "ready": policy_coverage.get("ready") is True,
        "policy_coverage_ready": policy_coverage.get("ready"),
        "rule_obligations_ready": policy_coverage.get("rule_obligations_ready"),
        "manual_review_ready": policy_summary.get("manual_review_ready"),
        "qbit_limits": policy_summary.get("qbit_limits") if isinstance(policy_summary.get("qbit_limits"), dict) else {},
        "seeding_requirements": policy_summary.get("seeding_requirements") if isinstance(policy_summary.get("seeding_requirements"), dict) else {},
        "transfer_rules": policy_summary.get("transfer_rules") if isinstance(policy_summary.get("transfer_rules"), dict) else {},
        "rules": policy_summary.get("rules") if isinstance(policy_summary.get("rules"), dict) else {},
        "policy_execution_handoff": policy_execution_handoff,
        "inherited_qbit_request": {key: submit_request.get(key) for key in qbit_keys if submit_request.get(key) is not None},
        "blockers": _string_list(digest_item.get("blockers")),
    }


def _daily_candidate_schedule_submission_handoff(submission_items: list[dict[str, Any]], blockers: list[str], *, approval_queue: dict[str, Any] | None = None) -> dict[str, Any]:
    next_step = _daily_candidate_submission_next_step(submission_items, blockers)
    execution_summary = _daily_candidate_submission_execution_summary(submission_items, blockers, next_step)
    queue = approval_queue if isinstance(approval_queue, dict) else {}
    return {
        "kind": "ptcli.daily_candidate_submission_handoff",
        "ready": bool(submission_items) and not blockers,
        "submit_count": len(submission_items),
        "submit_tool": "submit_daily_candidate_job",
        "submit_endpoint_template": "/v1/jobs/candidates/{candidate_job_id}/submit",
        "preferred_flow": "Poll daily candidate jobs until complete, review candidate rules, then submit via submit_daily_candidate_job so source/target identity is inherited from the candidate job.",
        "required_overrides": ["confirm_upload=true", "save_path or path"],
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "approval_queue": queue,
        "top_safe_candidates": queue.get("top_safe_candidates", []),
        "execution_summary": execution_summary,
        "items": submission_items,
        "blockers": blockers,
    }


def _daily_candidate_submission_execution_summary(submission_items: list[dict[str, Any]], blockers: list[str], next_step: dict[str, Any]) -> dict[str, Any]:
    first_after_submit = next((item.get("after_submit") for item in submission_items if isinstance(item.get("after_submit"), dict)), {})
    ready = bool(submission_items) and not blockers
    items = [_daily_candidate_submission_execution_item(item) for item in submission_items]
    post_submit_flow = {
        "primary_read": "job_handoff",
        "read_fields": first_after_submit.get("read_fields", []),
        "poll_with": first_after_submit.get("poll_with") or "get_job_status",
        "summary_with": first_after_submit.get("summary_with") or "get_job_summary",
        "resume_when": first_after_submit.get("resume_when"),
        "resume_request": "job_handoff.recommended_request",
        "material_resume_request": first_after_submit.get("material_resume_request"),
        "stop_when": first_after_submit.get("stop_when", []),
        "state_source": "job_handoff.action",
    }
    return {
        "kind": "ptcli.daily_candidate_submission_execution_summary",
        "ready": ready,
        "submit_count": len(submission_items),
        "blocked_count": len(blockers),
        "counts": {
            "submittable": len(submission_items),
            "blocked": len(blockers),
            "policy_ready": sum(1 for item in submission_items if isinstance(item.get("policy_execution"), dict) and item["policy_execution"].get("ready") is True),
            "needs_policy": sum(1 for item in submission_items if isinstance(item.get("policy_execution"), dict) and item["policy_execution"].get("ready") is not True),
        },
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "post_submit_flow": post_submit_flow,
        "actions": {
            "submit_candidates": "POST execution_summary.items[].request_template to execution_summary.items[].submit_endpoint after explicit user approval.",
            "poll_submitted_jobs": "Use post_submit_flow.poll_with on the returned retorrent job id, then read post_submit_flow.read_fields.",
            "resume_materials": "When job_handoff.action=prepare_materials, call resume_job with job_handoff.recommended_request after providing material files.",
            "configure_policy": "When job_handoff.action=configure_policy, update site policy/rule review data before retrying.",
            "stop_duplicate": "When job_handoff.action=stop_duplicate, do not upload; report duplicate evidence.",
        },
        "items": items,
        "blockers": blockers,
        "next_actions": _daily_candidate_submission_execution_next_actions(ready, submission_items, blockers),
    }


def _daily_candidate_submission_execution_item(item: dict[str, Any]) -> dict[str, Any]:
    after_submit = item.get("after_submit") if isinstance(item.get("after_submit"), dict) else {}
    policy_execution = item.get("policy_execution") if isinstance(item.get("policy_execution"), dict) else {}
    return {
        "candidate_job_id": item.get("candidate_job_id"),
        "selector": item.get("selector") if isinstance(item.get("selector"), dict) else {},
        "can_submit": True,
        "policy_ready": policy_execution.get("ready"),
        "submit_tool": item.get("submit_tool"),
        "submit_endpoint": item.get("submit_endpoint"),
        "request_template": item.get("request_template"),
        "post_submit_read": "job_handoff",
        "poll_with": after_submit.get("poll_with") or "get_job_status",
        "summary_with": after_submit.get("summary_with") or "get_job_summary",
        "resume_when": after_submit.get("resume_when"),
        "material_resume_request": after_submit.get("material_resume_request"),
        "stop_when": after_submit.get("stop_when", []),
        "identity_inherited_from_candidate": item.get("identity_inherited_from_candidate") if isinstance(item.get("identity_inherited_from_candidate"), dict) else {},
    }


def _daily_candidate_submission_execution_next_actions(ready: bool, submission_items: list[dict[str, Any]], blockers: list[str]) -> list[str]:
    if blockers:
        return ["Resolve execution_summary.blockers before submitting any daily candidate."]
    if not submission_items:
        return ["No submittable candidates are available; poll pending jobs or rerun the daily schedule later."]
    if ready:
        return ["Submit an approved execution_summary.items[] request, then poll the created retorrent job and read job_handoff for duplicate, policy, material, or live-upload decisions."]
    return ["Review execution_summary.items[].policy_ready and candidate blockers before submission."]


def _daily_candidate_submission_next_step(submission_items: list[dict[str, Any]], blockers: list[str]) -> dict[str, Any]:
    if blockers:
        return {
            "tool": None,
            "endpoint": None,
            "method": None,
            "request": None,
            "reason": "schedule_blocked",
            "blockers": blockers,
        }
    if not submission_items:
        return {
            "tool": None,
            "endpoint": None,
            "method": None,
            "request": None,
            "reason": "no_submittable_candidates",
            "blockers": [],
        }
    item = submission_items[0]
    return {
        "tool": item.get("submit_tool") or "submit_daily_candidate_job",
        "endpoint": item.get("submit_endpoint"),
        "method": item.get("method") or "POST",
        "request": item.get("request_template"),
        "reason": "submit_top_candidate_when_user_confirms",
        "candidate_job_id": item.get("candidate_job_id"),
        "selector": item.get("selector") if isinstance(item.get("selector"), dict) else {},
        "required_overrides": item.get("required_overrides") if isinstance(item.get("required_overrides"), list) else ["confirm_upload=true", "save_path or path"],
        "identity_inherited_from_candidate": item.get("identity_inherited_from_candidate") if isinstance(item.get("identity_inherited_from_candidate"), dict) else {},
    }


def _daily_candidate_schedule_job_decision(schedule_digest: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    if schedule_digest.get("pending_job_count"):
        decision = "wait"
        recommended_action = "Poll schedule_digest.items[].status_endpoint until each daily candidate job completes, then read schedule_digest.push_items."
    elif schedule_digest.get("submit_request_count"):
        decision = "review_candidates"
        recommended_action = "Review schedule_digest.submission_handoff.items, add confirm_upload=true plus save_path or path, then POST approved items to submit_daily_candidate_job."
    elif blockers or schedule_digest.get("blocked_job_count"):
        decision = "blocked"
        recommended_action = "Resolve schedule_digest.blockers and per-item blockers before rerunning the schedule job."
    else:
        decision = "inspect"
        recommended_action = "Inspect schedule_digest.items and rerun later or adjust daily candidate schedules."
    return {
        "workflow": "ptcli.daily_candidate_schedule_jobs",
        "decision": decision,
        "recommended_action": recommended_action,
        "can_submit_any": bool(schedule_digest.get("submit_request_count")),
        "should_poll": bool(schedule_digest.get("pending_job_count")),
        "job_count": schedule_digest.get("job_count", 0),
        "ready_job_count": schedule_digest.get("ready_job_count", 0),
        "submit_request_count": schedule_digest.get("submit_request_count", 0),
        "submission_handoff_ready": bool((schedule_digest.get("submission_handoff") or {}).get("ready")) if isinstance(schedule_digest.get("submission_handoff"), dict) else False,
        "submit_tool": "submit_daily_candidate_job" if schedule_digest.get("submit_request_count") else None,
        "target_count": schedule_digest.get("target_count", 0),
        "selected_count": schedule_digest.get("selected_count", 0),
        "ready_count": schedule_digest.get("ready_count", 0),
        "shortfall_count": schedule_digest.get("shortfall_count", 0),
        "target_met": bool(schedule_digest.get("target_met")),
        "push_count": schedule_digest.get("push_count", 0),
        "blocker_count": len(blockers),
    }


def _daily_candidate_schedule_run_next_actions(jobs: list[dict[str, Any]], skipped: list[dict[str, Any]], blockers: list[str]) -> list[str]:
    if not jobs:
        return ["Fix schedule blockers, then POST schedules to /v1/jobs/candidates/daily/schedule again."]
    actions = ["Poll each jobs[].status_endpoint until complete, then read candidate_digest and agent_decision from the job status or summary."]
    actions.append("If schedule_digest.submission_handoff.ready is true, review rules and POST an approved handoff item to /v1/jobs/candidates/{candidate_job_id}/submit with confirm_upload=true and a save_path or path.")
    if skipped:
        actions.append("Inspect skipped schedules before expecting candidates from every configured source/target pair.")
    if blockers:
        actions.append("Resolve top-level schedule blockers before treating the run as complete.")
    return actions


def _site_policy_matrix_item(policy: dict[str, Any], *, roles: list[str] | None = None, accept_rules: bool = False) -> dict[str, Any]:
    policy_roles = roles or ["unknown"]
    rule_obligations = build_rule_obligations(policy, roles=policy_roles, accept_rules=accept_rules)
    item = {
        "tracker": policy.get("tracker"),
        "roles": policy_roles,
        "rules_url": policy.get("rules_url"),
        "manual_review_required": policy.get("manual_review_required"),
        "rule_review_fingerprint": policy.get("rule_review_fingerprint"),
        "rule_obligations": rule_obligations,
        "automation": policy.get("automation") if isinstance(policy.get("automation"), dict) else {
            "download": policy.get("allow_auto_download"),
            "upload": policy.get("allow_auto_upload"),
            "retorrent": policy.get("allow_retorrent"),
            "manual_review_required": policy.get("manual_review_required"),
        },
        "qbit_limits": {
            "download_limit": policy.get("download_rate_limit"),
            "download_limit_human": policy.get("download_rate_limit_human"),
            "upload_limit": policy.get("upload_rate_limit"),
            "upload_limit_human": policy.get("upload_rate_limit_human"),
        },
        "seeding_requirements": {
            "min_seed_time_hours": policy.get("min_seed_time_hours"),
            "min_ratio": policy.get("min_ratio"),
            "freeleech_required": policy.get("freeleech_required"),
        },
        "transfer_rules": policy.get("transfer_rules") if isinstance(policy.get("transfer_rules"), dict) else {
            "freeleech_required": policy.get("freeleech_required"),
            "required_promotions": policy.get("required_promotions") or [],
            "forbidden_title_patterns": policy.get("forbidden_title_patterns") or [],
            "forbidden_release_groups": policy.get("forbidden_release_groups") or [],
        },
        "notes": _string_list(policy.get("notes")),
    }
    item["policy_coverage"] = build_site_policy_coverage(policy, roles=policy_roles, accept_rules=accept_rules)
    item["execution_readiness"] = _site_policy_item_execution_readiness(item)
    item["policy_profile"] = _site_policy_profile(item)
    return item


def _site_policy_profile(item: dict[str, Any]) -> dict[str, Any]:
    tracker = str(item.get("tracker") or "")
    roles = _string_list(item.get("roles")) or ["unknown"]
    coverage = item.get("policy_coverage") if isinstance(item.get("policy_coverage"), dict) else {}
    return {
        "kind": "ptcli.site_policy_profile",
        "tracker": tracker,
        "roles": roles,
        "purpose": "copyable local automation policy template for Chinese PT retorrent workflows",
        "config_path": f'config["PTCLI"]["SITE_POLICIES"]["{tracker}"]',
        "required_fields": _site_policy_required_fields_for_roles(roles),
        "optional_fields": ["freeleech_required", "required_promotions", "forbidden_title_patterns", "forbidden_release_groups", "notes"],
        "accepted_config_shapes": ["flat", "structured"],
        "missing_fields": _string_list(coverage.get("missing_fields")),
        "disabled_automation": _string_list(coverage.get("disabled_automation")),
        "template": _site_policy_config_template(item),
        "flat_template": _site_policy_config_template(item),
        "structured_template": _site_policy_structured_config_template(item),
        "current_values": {
            "rules_url": item.get("rules_url"),
            "automation": item.get("automation"),
            "qbit_limits": item.get("qbit_limits"),
            "seeding_requirements": item.get("seeding_requirements"),
            "transfer_rules": item.get("transfer_rules"),
            "rule_review_fingerprint": item.get("rule_review_fingerprint"),
            "rule_obligations": item.get("rule_obligations"),
        },
        "next_actions": _site_policy_profile_next_actions(tracker, roles, coverage),
    }


def _site_policy_required_fields_for_roles(roles: list[str]) -> list[str]:
    fields = ["rules_url", "rule_review_fingerprint", "allow_retorrent"]
    if "source" in roles:
        fields.extend(["allow_auto_download", "download_rate_limit", "min_seed_time_hours"])
    if "target" in roles:
        fields.extend(["allow_auto_upload", "upload_rate_limit", "min_ratio"])
    if "unknown" in roles:
        fields.append("source_or_target_role")
    return list(dict.fromkeys(fields))


def _site_policy_config_template(item: dict[str, Any]) -> dict[str, Any]:
    tracker = str(item.get("tracker") or "")
    roles = _string_list(item.get("roles")) or ["unknown"]
    automation = item.get("automation") if isinstance(item.get("automation"), dict) else {}
    qbit_limits = item.get("qbit_limits") if isinstance(item.get("qbit_limits"), dict) else {}
    seeding = item.get("seeding_requirements") if isinstance(item.get("seeding_requirements"), dict) else {}
    transfer_rules = item.get("transfer_rules") if isinstance(item.get("transfer_rules"), dict) else {}
    template = {
        "rules_url": item.get("rules_url") or "",
        "manual_review_required": True,
        "allow_retorrent": automation.get("retorrent") is True,
        "rule_review_fingerprint": item.get("rule_review_fingerprint") or "manual-review-YYYY-MM-DD",
    }
    if "source" in roles:
        template.update(
            {
                "allow_auto_download": automation.get("download") is True,
                "download_rate_limit": qbit_limits.get("download_limit_human") or "20MiB/s",
                "min_seed_time_hours": seeding.get("min_seed_time_hours") or 72,
            }
        )
    if "target" in roles:
        template.update(
            {
                "allow_auto_upload": automation.get("upload") is True,
                "upload_rate_limit": qbit_limits.get("upload_limit_human") or "2MiB/s",
                "min_ratio": seeding.get("min_ratio") or 1.0,
            }
        )
    if transfer_rules.get("freeleech_required") is True:
        template["freeleech_required"] = True
    if _string_list(transfer_rules.get("required_promotions")):
        template["required_promotions"] = _string_list(transfer_rules.get("required_promotions"))
    if _string_list(transfer_rules.get("forbidden_title_patterns")):
        template["forbidden_title_patterns"] = _string_list(transfer_rules.get("forbidden_title_patterns"))
    if _string_list(transfer_rules.get("forbidden_release_groups")):
        template["forbidden_release_groups"] = _string_list(transfer_rules.get("forbidden_release_groups"))
    template["notes"] = [f"Verify {tracker} rules manually before enabling live automation."]
    return template


def _site_policy_structured_config_template(item: dict[str, Any]) -> dict[str, Any]:
    flat = _site_policy_config_template(item)
    roles = _string_list(item.get("roles")) or ["unknown"]
    template: dict[str, Any] = {
        "rules_url": flat.get("rules_url") or "",
        "manual_review_required": flat.get("manual_review_required", True),
        "rule_review_fingerprint": flat.get("rule_review_fingerprint") or "manual-review-YYYY-MM-DD",
        "automation": {
            "retorrent": flat.get("allow_retorrent") is True,
            "download": flat.get("allow_auto_download") is True if "source" in roles else False,
            "upload": flat.get("allow_auto_upload") is True if "target" in roles else False,
        },
        "qbit_limits": {},
        "seeding_requirements": {},
        "transfer_rules": {},
        "notes": flat.get("notes") or [],
    }
    if "source" in roles:
        template["qbit_limits"]["download_limit"] = flat.get("download_rate_limit") or "20MiB/s"
        if flat.get("upload_rate_limit"):
            template["qbit_limits"]["upload_limit"] = flat.get("upload_rate_limit")
        template["seeding_requirements"]["min_seed_time_hours"] = flat.get("min_seed_time_hours") or 72
    if "target" in roles:
        template["qbit_limits"]["upload_limit"] = flat.get("upload_rate_limit") or "2MiB/s"
        if flat.get("download_rate_limit"):
            template["qbit_limits"]["download_limit"] = flat.get("download_rate_limit")
        template["seeding_requirements"]["min_ratio"] = flat.get("min_ratio") or 1.0
    for key in ("freeleech_required", "required_promotions", "forbidden_title_patterns", "forbidden_release_groups"):
        if key in flat:
            template["transfer_rules"][key] = flat[key]
    return template


def _site_policy_config_templates(matrix: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "ptcli.site_policy_config_templates",
        "config_path": 'config["PTCLI"]["SITE_POLICIES"]',
        "trackers": {str(item.get("tracker")): (item.get("policy_profile") or {}).get("template") for item in matrix if item.get("tracker")},
        "structured_trackers": {str(item.get("tracker")): (item.get("policy_profile") or {}).get("structured_template") for item in matrix if item.get("tracker")},
    }


def _site_policy_handoff(matrix: list[dict[str, Any]], policy_gap_summary: dict[str, Any], execution_readiness: dict[str, Any], report: dict[str, Any], request_context: dict[str, Any]) -> dict[str, Any]:
    templates = _site_policy_config_templates(matrix)
    blocked_trackers = _string_list(execution_readiness.get("blocked_trackers"))
    ready = bool(execution_readiness.get("ready")) and bool(report.get("ready"))
    missing_by_category = policy_gap_summary.get("missing_by_category") if isinstance(policy_gap_summary.get("missing_by_category"), dict) else {}
    rule_obligations = {str(item.get("tracker")): item.get("rule_obligations") for item in matrix if item.get("tracker")}
    tracker_items = []
    for item in matrix:
        tracker = str(item.get("tracker") or "")
        if not tracker:
            continue
        profile = item.get("policy_profile") if isinstance(item.get("policy_profile"), dict) else {}
        coverage = item.get("policy_coverage") if isinstance(item.get("policy_coverage"), dict) else {}
        readiness = item.get("execution_readiness") if isinstance(item.get("execution_readiness"), dict) else {}
        item_rule_obligations = item.get("rule_obligations") if isinstance(item.get("rule_obligations"), dict) else {}
        tracker_items.append(
            {
                "tracker": tracker,
                "roles": _string_list(item.get("roles")),
                "ready": bool(coverage.get("complete")) and bool(readiness.get("ready")),
                "config_path": profile.get("config_path"),
                "missing_fields": _string_list(coverage.get("missing_fields")),
                "disabled_automation": _string_list(coverage.get("disabled_automation")),
                "template": profile.get("template"),
                "rules_url": item.get("rules_url"),
                "rule_review_fingerprint": item.get("rule_review_fingerprint"),
                "rule_obligations": item_rule_obligations,
            }
        )
    next_step = _site_policy_next_step(ready, templates, tracker_items, missing_by_category, report, execution_readiness, request_context)
    return {
        "kind": "ptcli.site_policy_handoff",
        "ready": ready,
        "accepted_rules": bool(report.get("accept_rules")),
        "config_path": templates.get("config_path"),
        "blocked_trackers": blocked_trackers,
        "tracker_count": len(tracker_items),
        "items": tracker_items,
        "rule_obligations": rule_obligations,
        "config_templates": templates,
        "missing_by_category": missing_by_category,
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "blockers": _string_list(report.get("blockers")) + _string_list(execution_readiness.get("blockers")),
    }


def _site_policy_next_step(
    ready: bool,
    templates: dict[str, Any],
    tracker_items: list[dict[str, Any]],
    missing_by_category: dict[str, Any],
    report: dict[str, Any],
    execution_readiness: dict[str, Any],
    request_context: dict[str, Any],
) -> dict[str, Any]:
    policy_request = _site_policy_rerun_request(request_context, report)
    if ready:
        return {
            "tool": "readiness_bundle",
            "endpoint": "/v1/readiness/bundle",
            "method": "POST",
            "request": policy_request,
            "reason": "site_policy_ready",
        }
    return {
        "tool": "edit_config",
        "endpoint": None,
        "method": None,
        "request": {
            "config_path": templates.get("config_path"),
            "site_policy_templates": templates.get("trackers"),
            "structured_site_policy_templates": templates.get("structured_trackers"),
            "blocked_trackers": _string_list(execution_readiness.get("blocked_trackers")),
            "missing_by_category": missing_by_category,
        },
        "reason": "site_policy_incomplete",
        "after_edit": {
            "tool": "site_policies",
            "endpoint": "/v1/site-policies",
            "method": "POST",
            "request": policy_request,
        },
        "items": tracker_items,
    }


def _site_policy_rerun_request(request_context: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    request: dict[str, Any] = {"accept_rules": bool(report.get("accept_rules"))}
    roles = request_context.get("roles") if isinstance(request_context.get("roles"), dict) else {}
    trackers = _string_list(request_context.get("trackers"))
    source_trackers = [tracker for tracker in trackers if "source" in _string_list(roles.get(tracker))]
    target_trackers = [tracker for tracker in trackers if "target" in _string_list(roles.get(tracker))]
    if source_trackers:
        request["source_tracker"] = source_trackers[0]
    if target_trackers:
        request["target"] = ",".join(target_trackers)
    elif trackers:
        request["trackers"] = ",".join(trackers)
    if request_context.get("config"):
        request["config"] = request_context.get("config")
    return request


def _site_policy_profile_next_actions(tracker: str, roles: list[str], coverage: dict[str, Any]) -> list[str]:
    actions = _string_list(coverage.get("recommendations"))
    if not actions:
        actions.append(f"{tracker}: keep SITE_POLICIES current when site upload/download rules change.")
    if "unknown" in roles:
        actions.append(f"{tracker}: rerun site-policies with --from/--to so the profile can apply source/target requirements.")
    return list(dict.fromkeys(actions))


def _site_policy_item_execution_readiness(item: dict[str, Any]) -> dict[str, Any]:
    tracker = str(item.get("tracker") or "")
    roles = _string_list(item.get("roles")) or ["unknown"]
    coverage = item.get("policy_coverage") if isinstance(item.get("policy_coverage"), dict) else {}
    automation = item.get("automation") if isinstance(item.get("automation"), dict) else {}
    qbit_limits = item.get("qbit_limits") if isinstance(item.get("qbit_limits"), dict) else {}
    seeding = item.get("seeding_requirements") if isinstance(item.get("seeding_requirements"), dict) else {}
    transfer_rules = item.get("transfer_rules") if isinstance(item.get("transfer_rules"), dict) else {}
    blockers = _string_list(coverage.get("missing_fields")) + _string_list(coverage.get("disabled_automation"))
    role_status: dict[str, dict[str, Any]] = {}
    for role in roles:
        role_blockers = _site_policy_role_blockers(role, coverage, automation, qbit_limits, seeding)
        role_status[role] = {
            "ready": not role_blockers,
            "blockers": role_blockers,
            "can_download": role == "source" and automation.get("download") is True and not role_blockers,
            "can_upload": role == "target" and automation.get("upload") is True and not role_blockers,
            "can_retorrent": automation.get("retorrent") is True and not role_blockers,
        }
    return {
        "tracker": tracker,
        "roles": roles,
        "ready": bool(coverage.get("complete")),
        "manual_review_ready": item.get("manual_review_required") is not True or bool(item.get("rule_review_fingerprint")),
        "rules_url": item.get("rules_url"),
        "rule_review_fingerprint": item.get("rule_review_fingerprint"),
        "rule_obligations": item.get("rule_obligations") if isinstance(item.get("rule_obligations"), dict) else {},
        "blockers": blockers,
        "role_status": role_status,
        "qbit_limits": qbit_limits,
        "seeding_requirements": seeding,
        "transfer_rules": transfer_rules,
    }


def _site_policy_role_blockers(role: str, coverage: dict[str, Any], automation: dict[str, Any], qbit_limits: dict[str, Any], seeding: dict[str, Any]) -> list[str]:
    blockers = _string_list(coverage.get("missing_fields")) + _string_list(coverage.get("disabled_automation"))
    if role == "source":
        if automation.get("download") is not True:
            blockers.append("auto_download")
        if qbit_limits.get("download_limit") is None:
            blockers.append("download_rate_limit")
        if seeding.get("min_seed_time_hours") is None:
            blockers.append("min_seed_time_hours")
    elif role == "target":
        if automation.get("upload") is not True:
            blockers.append("auto_upload")
        if qbit_limits.get("upload_limit") is None:
            blockers.append("upload_rate_limit")
        if seeding.get("min_ratio") is None:
            blockers.append("min_ratio")
    elif role == "unknown":
        blockers.append("source_or_target_role")
    return list(dict.fromkeys(blockers))


def _site_policy_execution_readiness(matrix: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any]:
    by_tracker = {
        str(item.get("tracker")): item.get("execution_readiness")
        for item in matrix
        if item.get("tracker") and isinstance(item.get("execution_readiness"), dict)
    }
    ready_trackers = [tracker for tracker, readiness in by_tracker.items() if readiness and readiness.get("ready") is True]
    blocked_trackers = [tracker for tracker, readiness in by_tracker.items() if not readiness or readiness.get("ready") is not True]
    return {
        "ready": bool(report.get("ready")) and not blocked_trackers,
        "accepted_rules": bool(report.get("accept_rules")),
        "ready_trackers": ready_trackers,
        "blocked_trackers": blocked_trackers,
        "by_tracker": by_tracker,
        "blockers": _string_list(report.get("blockers")) + [
            f"{tracker}: {', '.join(_string_list((readiness or {}).get('blockers')))}"
            for tracker, readiness in by_tracker.items()
            if readiness and _string_list(readiness.get("blockers"))
        ],
    }


def _site_policy_execution_summary(
    matrix: list[dict[str, Any]],
    policy_gap_summary: dict[str, Any],
    execution_readiness: dict[str, Any],
    policy_handoff: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    items = [_site_policy_execution_summary_item(item) for item in matrix]
    by_role: dict[str, list[dict[str, Any]]] = {"source": [], "target": [], "unknown": []}
    for item in items:
        for role in _string_list(item.get("roles")) or ["unknown"]:
            by_role.setdefault(role, []).append(item)
    blockers = list(dict.fromkeys(_string_list(execution_readiness.get("blockers")) + _string_list(report.get("blockers")) + _site_policy_execution_item_blockers(items)))
    next_step = policy_handoff.get("next_step") if isinstance(policy_handoff.get("next_step"), dict) else {}
    return {
        "kind": "ptcli.policy_execution_summary",
        "ready": bool(execution_readiness.get("ready")),
        "accepted_rules": bool(report.get("accept_rules")),
        "tracker_count": len(items),
        "ready_trackers": _string_list(execution_readiness.get("ready_trackers")),
        "blocked_trackers": _string_list(execution_readiness.get("blocked_trackers")),
        "by_role": by_role,
        "items": items,
        "qbit_limit_plan": _site_policy_qbit_limit_plan(items),
        "seeding_plan": _site_policy_seeding_plan(items),
        "transfer_rule_plan": _site_policy_transfer_rule_plan(items),
        "missing_by_category": policy_gap_summary.get("missing_by_category") if isinstance(policy_gap_summary.get("missing_by_category"), dict) else {},
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "blockers": blockers,
        "next_actions": _site_policy_execution_summary_next_actions(blockers, next_step),
    }


def _site_policy_execution_handoff(policy_execution_summary: dict[str, Any], policy_handoff: dict[str, Any], policy_setup_summary: dict[str, Any], request_context: dict[str, Any]) -> dict[str, Any]:
    execution_next_step = policy_execution_summary.get("next_step")
    handoff_next_step = policy_handoff.get("next_step")
    next_step = execution_next_step if isinstance(execution_next_step, dict) else handoff_next_step if isinstance(handoff_next_step, dict) else {}
    blockers = list(dict.fromkeys(_string_list(policy_execution_summary.get("blockers")) + _string_list(policy_handoff.get("blockers")) + _string_list(policy_setup_summary.get("blockers"))))
    ready = bool(policy_execution_summary.get("ready")) and bool(policy_setup_summary.get("ready")) and not blockers
    qbit_plan = policy_execution_summary.get("qbit_limit_plan") if isinstance(policy_execution_summary.get("qbit_limit_plan"), dict) else {}
    seeding_plan = policy_execution_summary.get("seeding_plan") if isinstance(policy_execution_summary.get("seeding_plan"), dict) else {}
    transfer_rule_plan = policy_execution_summary.get("transfer_rule_plan") if isinstance(policy_execution_summary.get("transfer_rule_plan"), dict) else {}
    return {
        "kind": "ptcli.policy_execution_handoff",
        "ready": ready,
        "accepted_rules": bool(policy_execution_summary.get("accepted_rules")),
        "phase": "ready_for_live_preflight" if ready else "configure_site_policy",
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "next_step": next_step,
        "qbit": {
            "ready": qbit_plan.get("ready") is True,
            "source": qbit_plan.get("source") if isinstance(qbit_plan.get("source"), dict) else {},
            "target": qbit_plan.get("target") if isinstance(qbit_plan.get("target"), dict) else {},
            "missing": qbit_plan.get("missing") if isinstance(qbit_plan.get("missing"), list) else [],
        },
        "seeding": {
            "ready": seeding_plan.get("ready") is True,
            "by_tracker": seeding_plan.get("by_tracker") if isinstance(seeding_plan.get("by_tracker"), dict) else {},
            "missing": seeding_plan.get("missing") if isinstance(seeding_plan.get("missing"), list) else [],
        },
        "transfer_rules": {
            "ready": transfer_rule_plan.get("ready") is True,
            "by_tracker": transfer_rule_plan.get("by_tracker") if isinstance(transfer_rule_plan.get("by_tracker"), dict) else {},
            "manual_review_note": transfer_rule_plan.get("manual_review_note"),
        },
        "rule_obligations": policy_handoff.get("rule_obligations") if isinstance(policy_handoff.get("rule_obligations"), dict) else {},
        "config": {
            "path": policy_handoff.get("config_path"),
            "templates": (policy_handoff.get("config_templates") or {}).get("trackers") if isinstance(policy_handoff.get("config_templates"), dict) else {},
            "structured_templates": (policy_handoff.get("config_templates") or {}).get("structured_trackers") if isinstance(policy_handoff.get("config_templates"), dict) else {},
            "missing_by_category": policy_handoff.get("missing_by_category") if isinstance(policy_handoff.get("missing_by_category"), dict) else {},
        },
        "request": _site_policy_rerun_request(request_context, {"accept_rules": bool(policy_execution_summary.get("accepted_rules"))}),
        "continue_when": "policy_execution_handoff.ready=true; then run readiness_bundle or source_url_retorrent_preflight before live upload",
        "stop_when": [
            "policy_execution_handoff.ready=false",
            "policy_execution_handoff.qbit.missing is non-empty",
            "policy_execution_handoff.seeding.missing is non-empty",
            "rule_obligations.*.ready=false",
        ],
        "blockers": blockers,
        "next_actions": _site_policy_execution_handoff_next_actions(ready, next_step, blockers),
    }


def _site_policy_execution_handoff_next_actions(ready: bool, next_step: dict[str, Any], blockers: list[str]) -> list[str]:
    if ready:
        return ["Continue with policy_execution_handoff.next_step, then verify deployment and tracker credentials before creating live jobs."]
    if next_step.get("tool") == "edit_config":
        return ["Apply policy_execution_handoff.config.templates to PTCLI.SITE_POLICIES, replace rule_review_fingerprint placeholders after manual rule review, then rerun site_policies."]
    if blockers:
        return ["Resolve policy_execution_handoff.blockers before live automation."]
    return ["Inspect policy_execution_handoff before deciding the next policy step."]


def _site_policy_execution_summary_item(item: dict[str, Any]) -> dict[str, Any]:
    tracker = str(item.get("tracker") or "")
    readiness = item.get("execution_readiness") if isinstance(item.get("execution_readiness"), dict) else {}
    coverage = item.get("policy_coverage") if isinstance(item.get("policy_coverage"), dict) else {}
    qbit_limits = item.get("qbit_limits") if isinstance(item.get("qbit_limits"), dict) else {}
    seeding = item.get("seeding_requirements") if isinstance(item.get("seeding_requirements"), dict) else {}
    transfer_rules = item.get("transfer_rules") if isinstance(item.get("transfer_rules"), dict) else {}
    rule_obligations = item.get("rule_obligations") if isinstance(item.get("rule_obligations"), dict) else {}
    return {
        "tracker": tracker,
        "roles": _string_list(item.get("roles")) or ["unknown"],
        "ready": readiness.get("ready") is True and coverage.get("complete") is True and rule_obligations.get("ready") is True,
        "execution_ready": readiness.get("ready") is True,
        "policy_complete": coverage.get("complete") is True,
        "rule_obligations_ready": rule_obligations.get("ready") is True,
        "automation": item.get("automation") if isinstance(item.get("automation"), dict) else {},
        "qbit_limits": {
            "download_limit": qbit_limits.get("download_limit"),
            "download_limit_human": qbit_limits.get("download_limit_human"),
            "upload_limit": qbit_limits.get("upload_limit"),
            "upload_limit_human": qbit_limits.get("upload_limit_human"),
        },
        "seeding_requirements": {
            "min_seed_time_hours": seeding.get("min_seed_time_hours"),
            "min_ratio": seeding.get("min_ratio"),
            "freeleech_required": seeding.get("freeleech_required") is True,
        },
        "transfer_rules": transfer_rules,
        "missing_fields": _string_list(coverage.get("missing_fields")),
        "disabled_automation": _string_list(coverage.get("disabled_automation")),
        "role_status": readiness.get("role_status") if isinstance(readiness.get("role_status"), dict) else {},
        "blockers": list(dict.fromkeys(_string_list(readiness.get("blockers")) + _string_list(coverage.get("missing_fields")) + _string_list(coverage.get("disabled_automation")) + _string_list(rule_obligations.get("blockers")))),
    }


def _site_policy_execution_item_blockers(items: list[dict[str, Any]]) -> list[str]:
    blockers: list[str] = []
    for item in items:
        tracker = str(item.get("tracker") or "UNKNOWN")
        blockers.extend(f"{tracker}: {blocker}" for blocker in _string_list(item.get("blockers")))
    return blockers


def _site_policy_qbit_limit_plan(items: list[dict[str, Any]]) -> dict[str, Any]:
    source_limits = {item["tracker"]: item.get("qbit_limits") for item in items if "source" in _string_list(item.get("roles"))}
    target_limits = {item["tracker"]: item.get("qbit_limits") for item in items if "target" in _string_list(item.get("roles"))}
    missing = []
    for item in items:
        tracker = item.get("tracker")
        limits = item.get("qbit_limits") if isinstance(item.get("qbit_limits"), dict) else {}
        if "source" in _string_list(item.get("roles")) and limits.get("download_limit") is None:
            missing.append({"tracker": tracker, "field": "download_rate_limit"})
        if "target" in _string_list(item.get("roles")) and limits.get("upload_limit") is None:
            missing.append({"tracker": tracker, "field": "upload_rate_limit"})
    return {"ready": not missing, "source": source_limits, "target": target_limits, "missing": missing}


def _site_policy_seeding_plan(items: list[dict[str, Any]]) -> dict[str, Any]:
    missing = []
    by_tracker: dict[str, Any] = {}
    for item in items:
        tracker = str(item.get("tracker") or "")
        seeding = item.get("seeding_requirements") if isinstance(item.get("seeding_requirements"), dict) else {}
        by_tracker[tracker] = seeding
        if "source" in _string_list(item.get("roles")) and seeding.get("min_seed_time_hours") is None:
            missing.append({"tracker": tracker, "field": "min_seed_time_hours"})
        if "target" in _string_list(item.get("roles")) and seeding.get("min_ratio") is None:
            missing.append({"tracker": tracker, "field": "min_ratio"})
    return {"ready": not missing, "by_tracker": by_tracker, "missing": missing}


def _site_policy_transfer_rule_plan(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ready": True,
        "by_tracker": {str(item.get("tracker")): item.get("transfer_rules") for item in items if item.get("tracker")},
        "manual_review_note": "Transfer rules are local gates only; unresolved site-specific interpretation still requires rule_obligations/manual review.",
    }


def _site_policy_execution_summary_next_actions(blockers: list[str], next_step: dict[str, Any]) -> list[str]:
    if not blockers:
        return ["Policy execution summary is ready; continue with readiness_bundle or manual/source-url retorrent preflight."]
    if next_step.get("tool") == "edit_config":
        return ["Update PTCLI.SITE_POLICIES using policy_execution_summary.next_step.request, then rerun site_policies with accept_rules after manual rule review."]
    return ["Resolve policy_execution_summary.blockers before live automation."]


def _site_policy_setup_summary(
    matrix: list[dict[str, Any]],
    policy_gap_summary: dict[str, Any],
    execution_readiness: dict[str, Any],
    policy_handoff: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    config_path = str(policy_handoff.get("config_path") or 'config["PTCLI"]["SITE_POLICIES"]')
    missing_fingerprints = [_site_policy_setup_fingerprint_item(item) for item in matrix if _site_policy_missing_rule_review(item)]
    placeholder_fingerprints = [_site_policy_setup_fingerprint_item(item) for item in matrix if _site_policy_placeholder_rule_review(item)]
    missing_by_category = policy_gap_summary.get("missing_by_category") if isinstance(policy_gap_summary.get("missing_by_category"), dict) else {}
    next_step = policy_handoff.get("next_step") if isinstance(policy_handoff.get("next_step"), dict) else {}
    ready = bool(report.get("ready")) and bool(execution_readiness.get("ready")) and not missing_fingerprints and not placeholder_fingerprints
    return {
        "kind": "ptcli.site_policy_setup_summary",
        "ready": ready,
        "accepted_rules": bool(report.get("accept_rules")),
        "config_path": config_path,
        "tracker_count": len(matrix),
        "ready_trackers": _string_list(execution_readiness.get("ready_trackers")),
        "blocked_trackers": _string_list(execution_readiness.get("blocked_trackers")),
        "missing_fingerprint_count": len(missing_fingerprints),
        "missing_fingerprints": missing_fingerprints,
        "placeholder_fingerprint_count": len(placeholder_fingerprints),
        "placeholder_fingerprints": placeholder_fingerprints,
        "missing_by_category": missing_by_category,
        "copyable_templates": (policy_handoff.get("config_templates") or {}).get("trackers") if isinstance(policy_handoff.get("config_templates"), dict) else {},
        "ready_after_review": not missing_fingerprints and not placeholder_fingerprints and not _string_list(execution_readiness.get("blocked_trackers")),
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "blockers": _site_policy_setup_blockers(missing_fingerprints, placeholder_fingerprints, execution_readiness, report),
        "next_actions": _site_policy_setup_next_actions(missing_fingerprints, placeholder_fingerprints, next_step),
    }


def _site_policy_missing_rule_review(item: dict[str, Any]) -> bool:
    if item.get("manual_review_required") is not True:
        return False
    return not str(item.get("rule_review_fingerprint") or "").strip()


def _site_policy_placeholder_rule_review(item: dict[str, Any]) -> bool:
    value = str(item.get("rule_review_fingerprint") or "").strip()
    if not value:
        return False
    normalized = value.lower()
    return "yyyy" in normalized or normalized in {"manual-review", "manual-review-yyyy-mm-dd", "reviewed-YYYY-MM-DD".lower()}


def _site_policy_setup_fingerprint_item(item: dict[str, Any]) -> dict[str, Any]:
    profile = item.get("policy_profile") if isinstance(item.get("policy_profile"), dict) else {}
    template = profile.get("template") if isinstance(profile.get("template"), dict) else {}
    return {
        "tracker": item.get("tracker"),
        "roles": _string_list(item.get("roles")),
        "config_path": profile.get("config_path"),
        "rules_url": item.get("rules_url"),
        "current_value": item.get("rule_review_fingerprint") or "",
        "template_value": template.get("rule_review_fingerprint") or "manual-review-YYYY-MM-DD",
        "required_after": "manual site-rule review",
    }


def _site_policy_setup_blockers(
    missing_fingerprints: list[dict[str, Any]],
    placeholder_fingerprints: list[dict[str, Any]],
    execution_readiness: dict[str, Any],
    report: dict[str, Any],
) -> list[str]:
    blockers = _string_list(report.get("blockers")) + _string_list(execution_readiness.get("blockers"))
    blockers.extend(f"{item.get('tracker')}: rule_review_fingerprint is empty." for item in missing_fingerprints)
    blockers.extend(f"{item.get('tracker')}: rule_review_fingerprint still looks like a placeholder." for item in placeholder_fingerprints)
    return list(dict.fromkeys(blockers))


def _site_policy_setup_next_actions(missing_fingerprints: list[dict[str, Any]], placeholder_fingerprints: list[dict[str, Any]], next_step: dict[str, Any]) -> list[str]:
    if missing_fingerprints or placeholder_fingerprints:
        trackers = ", ".join(str(item.get("tracker")) for item in [*missing_fingerprints, *placeholder_fingerprints] if item.get("tracker"))
        return [
            f"Review the current tracker rules for {trackers}, then replace rule_review_fingerprint placeholders in PTCLI.SITE_POLICIES with a real audit marker.",
            "Rerun site_policies with accept_rules=true; continue only when policy_setup_summary.ready=true.",
        ]
    if next_step.get("tool") == "edit_config":
        return ["Apply policy_setup_summary.copyable_templates to config[\"PTCLI\"][\"SITE_POLICIES\"], then rerun site_policies."]
    return ["Policy setup is ready; continue with readiness_bundle or source_url_retorrent_preflight."]


def _site_policy_gap_summary(matrix: list[dict[str, Any]]) -> dict[str, Any]:
    by_role: dict[str, dict[str, Any]] = {}
    missing_by_category = {
        "rule_review": [],
        "rate_limits": [],
        "seeding_requirements": [],
        "automation": [],
        "role": [],
    }
    for item in matrix:
        tracker = str(item.get("tracker") or "")
        coverage = item.get("policy_coverage") if isinstance(item.get("policy_coverage"), dict) else {}
        missing_fields = _string_list(coverage.get("missing_fields"))
        disabled = _string_list(coverage.get("disabled_automation"))
        for role in _string_list(item.get("roles")) or ["unknown"]:
            role_summary = by_role.setdefault(role, {"trackers": [], "missing_fields": {}, "disabled_automation": {}, "recommendations": []})
            if tracker and tracker not in role_summary["trackers"]:
                role_summary["trackers"].append(tracker)
            if missing_fields:
                role_summary["missing_fields"][tracker] = missing_fields
            if disabled:
                role_summary["disabled_automation"][tracker] = disabled
            role_summary["recommendations"].extend(recommendation for recommendation in _string_list(coverage.get("recommendations")) if recommendation not in role_summary["recommendations"])
        for field in missing_fields:
            if field == "rule_review_fingerprint":
                missing_by_category["rule_review"].append({"tracker": tracker, "field": field})
            elif field in {"download_rate_limit", "upload_rate_limit"}:
                missing_by_category["rate_limits"].append({"tracker": tracker, "field": field})
            elif field in {"min_seed_time_hours", "min_ratio"}:
                missing_by_category["seeding_requirements"].append({"tracker": tracker, "field": field})
            elif field == "source_or_target_role":
                missing_by_category["role"].append({"tracker": tracker, "field": field})
        for automation in disabled:
            missing_by_category["automation"].append({"tracker": tracker, "field": automation})
    recommendations = list(dict.fromkeys(recommendation for item in matrix for recommendation in _string_list((item.get("policy_coverage") or {}).get("recommendations"))))
    return {
        "ready": all(bool((item.get("policy_coverage") or {}).get("complete")) for item in matrix),
        "tracker_count": len(matrix),
        "missing_total": sum(len(_string_list((item.get("policy_coverage") or {}).get("missing_fields"))) for item in matrix),
        "disabled_total": sum(len(_string_list((item.get("policy_coverage") or {}).get("disabled_automation"))) for item in matrix),
        "by_role": by_role,
        "missing_by_category": missing_by_category,
        "recommendations": recommendations,
    }


def _site_policy_agent_summary(matrix: list[dict[str, Any]], report: dict[str, Any], policy_gap_summary: dict[str, Any], execution_readiness: dict[str, Any], policy_execution_summary: dict[str, Any]) -> dict[str, Any]:
    missing_by_tracker = {
        str(item.get("tracker")): _string_list((item.get("policy_coverage") or {}).get("missing_fields"))
        for item in matrix
        if _string_list((item.get("policy_coverage") or {}).get("missing_fields"))
    }
    disabled_by_tracker = {
        str(item.get("tracker")): _string_list((item.get("policy_coverage") or {}).get("disabled_automation"))
        for item in matrix
        if _string_list((item.get("policy_coverage") or {}).get("disabled_automation"))
    }
    return {
        "ready": bool(report.get("ready")),
        "tracker_count": len(matrix),
        "trackers": [str(item.get("tracker")) for item in matrix if item.get("tracker")],
        "manual_review_required": [str(item.get("tracker")) for item in matrix if item.get("manual_review_required") is True],
        "auto_download_enabled": [str(item.get("tracker")) for item in matrix if (item.get("automation") or {}).get("download") is True],
        "auto_upload_enabled": [str(item.get("tracker")) for item in matrix if (item.get("automation") or {}).get("upload") is True],
        "retorrent_enabled": [str(item.get("tracker")) for item in matrix if (item.get("automation") or {}).get("retorrent") is True],
        "qbit_limits_present": [str(item.get("tracker")) for item in matrix if (item.get("qbit_limits") or {}).get("download_limit") is not None or (item.get("qbit_limits") or {}).get("upload_limit") is not None],
        "seeding_requirements_present": [
            str(item.get("tracker"))
            for item in matrix
            if (item.get("seeding_requirements") or {}).get("min_seed_time_hours") is not None or (item.get("seeding_requirements") or {}).get("min_ratio") is not None
        ],
        "policy_coverage_ready": all(bool((item.get("policy_coverage") or {}).get("complete")) for item in matrix),
        "execution_ready": bool(execution_readiness.get("ready")),
        "execution_ready_trackers": _string_list(execution_readiness.get("ready_trackers")),
        "execution_blocked_trackers": _string_list(execution_readiness.get("blocked_trackers")),
        "missing_policy_fields": missing_by_tracker,
        "disabled_automation": disabled_by_tracker,
        "policy_gap_summary": policy_gap_summary,
        "policy_execution_summary": policy_execution_summary,
        "policy_recommendations": [recommendation for item in matrix for recommendation in _string_list((item.get("policy_coverage") or {}).get("recommendations"))],
        "blockers": _string_list(report.get("blockers")),
        "next_actions": _string_list(report.get("next_actions")),
    }


def _append_common_options(argv: list[str], request: dict[str, Any]) -> None:
    _append_optional(argv, "--config", request.get("config"))
    _append_optional(argv, "--client", request.get("client"))
    _append_optional(argv, "--base-dir", request.get("base_dir"))


def _append_metadata_options(argv: list[str], request: dict[str, Any]) -> None:
    _append_optional(argv, "--imdb-id", request.get("imdb_id"))
    _append_optional(argv, "--tmdb-id", request.get("tmdb_id"))
    _append_optional(argv, "--tmdb-type", request.get("tmdb_type"))
    _append_optional(argv, "--douban-id", request.get("douban_id"))
    _append_optional(argv, "--douban-url", request.get("douban_url"))


def _append_execute_bool_options(argv: list[str], request: dict[str, Any]) -> None:
    bool_options = {
        "accept_rules": "--accept-rules",
        "confirm_upload": "--confirm-upload",
        "paused": "--paused",
        "uploaded_paused": "--uploaded-paused",
        "write_payload": "--write-payload",
        "export_target_torrent": "--export-target-torrent",
        "enrich_metadata": "--enrich-metadata",
        "fetch_ptgen": "--fetch-ptgen",
        "generate_bdinfo": "--generate-bdinfo",
        "generate_mediainfo": "--generate-mediainfo",
        "generate_screenshots": "--generate-screenshots",
        "upload_screenshots": "--upload-screenshots",
        "download_uploaded_torrent": "--download-uploaded-torrent",
        "inject_uploaded_torrent": "--inject-uploaded-torrent",
    }
    for key, option in bool_options.items():
        if request.get(key):
            argv.append(option)
    negative_bool_options = {
        "no_enrich_metadata": "--no-enrich-metadata",
        "no_fetch_ptgen": "--no-fetch-ptgen",
        "no_generate_bdinfo": "--no-generate-bdinfo",
        "no_generate_mediainfo": "--no-generate-mediainfo",
        "no_generate_screenshots": "--no-generate-screenshots",
        "no_upload_screenshots": "--no-upload-screenshots",
        "no_sanitize_target_torrent": "--no-sanitize-target-torrent",
    }
    for key, option in negative_bool_options.items():
        if request.get(key):
            argv.append(option)


def _append_optional(argv: list[str], option: str, value: Any) -> None:
    if value is None or value == "":
        return
    argv.extend([option, str(value)])


def _list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalized_request(request: dict[str, Any], source: dict[str, Any], target_trackers: str, *, execute: bool) -> dict[str, Any]:
    return {
        "source": source,
        "source_reference": source,
        "source_input": source.get("requested_source"),
        "source_url": source.get("details_url"),
        "target_trackers": target_trackers,
        "execute": execute,
        "accept_rules": bool(request.get("accept_rules")),
        "confirm_upload": bool(request.get("confirm_upload")),
        "client": request.get("client") or "default",
        "config": request.get("config"),
        "path": request.get("path") or request.get("content_path"),
        "save_path": request.get("save_path"),
        "qbit_category": request.get("qbit_category"),
        "qbit_tags": request.get("qbit_tags"),
        "qbit_upload_limit": request.get("qbit_upload_limit"),
        "qbit_download_limit": request.get("qbit_download_limit"),
        "uploaded_qbit_category": request.get("uploaded_qbit_category"),
        "uploaded_qbit_tags": request.get("uploaded_qbit_tags"),
        "uploaded_qbit_upload_limit": request.get("uploaded_qbit_upload_limit"),
        "uploaded_qbit_download_limit": request.get("uploaded_qbit_download_limit"),
        "material_options": _request_material_options(request),
        "policy_qbit_defaults": request.get("policy_qbit_defaults"),
        "policy_coverage": _request_policy_coverage(request, source, target_trackers),
    }


def _request_material_options(request: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    value_keys = (
        "metadata_file",
        "ptgen_description_file",
        "mediainfo_file",
        "bdinfo_file",
        "bdinfo_playlist",
        "image_host_file",
        "image_host",
        "screenshot_count",
        "imdb_id",
        "tmdb_id",
        "tmdb_type",
        "douban_id",
        "douban_url",
    )
    for key in value_keys:
        value = request.get(key)
        if value not in (None, ""):
            options[key] = value
    screenshot_files = _list_value(request.get("screenshot_file") or request.get("screenshot_files"))
    if screenshot_files:
        options["screenshot_files"] = [str(item) for item in screenshot_files]
    for key in ("enrich_metadata", "fetch_ptgen", "generate_bdinfo", "generate_mediainfo", "generate_screenshots", "upload_screenshots"):
        if key in request:
            options[key] = bool(request.get(key))
    return options


def _request_with_policy_qbit_defaults(request: dict[str, Any], source: dict[str, Any], target_trackers: str) -> dict[str, Any]:
    enriched = dict(request)
    if isinstance(enriched.get("policy_qbit_defaults"), dict):
        return enriched
    defaults: dict[str, Any] = {"applied": {}, "sources": {}, "request_overrides": {}, "errors": []}
    try:
        config = load_config(enriched.get("config"))
        source_tracker = str(source.get("tracker") or "")
        targets = parse_tracker_list(target_trackers)
        source_limits = qbit_limits_for_tracker(config, source_tracker, role="source") if source_tracker else {}
        _apply_policy_qbit_default(enriched, defaults, "qbit_upload_limit", source_limits.get("upload_limit"), source_tracker)
        _apply_policy_qbit_default(enriched, defaults, "qbit_download_limit", source_limits.get("download_limit"), source_tracker)
        if targets:
            target = targets[0]
            target_limits = qbit_limits_for_tracker(config, target, role="target")
            _apply_policy_qbit_default(enriched, defaults, "uploaded_qbit_upload_limit", target_limits.get("upload_limit"), target)
            _apply_policy_qbit_default(enriched, defaults, "uploaded_qbit_download_limit", target_limits.get("download_limit"), target)
    except Exception as exc:
        defaults["errors"].append(str(exc))
    enriched["policy_qbit_defaults"] = defaults
    return enriched


def _apply_policy_qbit_default(enriched: dict[str, Any], defaults: dict[str, Any], key: str, value: Any, tracker: str) -> None:
    if enriched.get(key) not in (None, ""):
        defaults["request_overrides"][key] = enriched.get(key)
        return
    if value is None:
        return
    enriched[key] = value
    defaults["applied"][key] = value
    defaults["sources"][key] = f"site_policy:{tracker}"


def _request_policy_coverage(request: dict[str, Any], source: dict[str, Any], target_trackers: str) -> dict[str, Any]:
    try:
        config = load_config(request.get("config"))
        source_tracker = str(source.get("tracker") or "")
        targets = parse_tracker_list(target_trackers)
        report = build_site_policy_report(config, [source_tracker, *targets], accept_rules=bool(request.get("accept_rules")))
        policies = report.get("site_policies") if isinstance(report.get("site_policies"), list) else []
        policies_by_tracker = {str(policy.get("tracker")): policy for policy in policies if isinstance(policy, dict) and policy.get("tracker")}
        source_policy = policies_by_tracker.get(source_tracker)
        target_policies = [policies_by_tracker.get(target) for target in targets]
        source_coverage = build_site_policy_coverage(source_policy, roles=["source"], accept_rules=bool(request.get("accept_rules"))) if isinstance(source_policy, dict) else None
        target_coverages = [build_site_policy_coverage(policy, roles=["target"], accept_rules=bool(request.get("accept_rules"))) for policy in target_policies if isinstance(policy, dict)]
        coverages = [coverage for coverage in [source_coverage, *target_coverages] if isinstance(coverage, dict)]
        return {
            "ready": bool(report.get("ready")) and bool(coverages) and all(bool(coverage.get("complete")) for coverage in coverages),
            "site_policy_ready": bool(report.get("ready")),
            "accept_rules": bool(request.get("accept_rules")),
            "source": source_coverage,
            "targets": target_coverages,
            "obligations": {
                "source": _policy_obligation_from_policy(source_policy, role="source", accept_rules=bool(request.get("accept_rules"))) if isinstance(source_policy, dict) else None,
                "targets": [_policy_obligation_from_policy(policy, role="target", accept_rules=bool(request.get("accept_rules"))) for policy in target_policies if isinstance(policy, dict)],
            },
            "missing_policy_fields": _policy_coverage_fields(coverages, "missing_fields"),
            "disabled_automation": _policy_coverage_fields(coverages, "disabled_automation"),
            "recommendations": [recommendation for coverage in coverages for recommendation in _string_list(coverage.get("recommendations"))],
            "blockers": _string_list(report.get("blockers")),
            "next_actions": _string_list(report.get("next_actions")),
        }
    except Exception as exc:
        return {
            "ready": None,
            "error": str(exc),
            "recommendations": ["Run deployment_check or provide a readable data/config.py before relying on policy coverage."],
        }


def _policy_obligation_from_policy(policy: dict[str, Any], *, role: str, accept_rules: bool = False) -> dict[str, Any]:
    qbit_limits = {
        "download_limit": policy.get("download_rate_limit"),
        "download_limit_human": policy.get("download_rate_limit_human"),
        "upload_limit": policy.get("upload_rate_limit"),
        "upload_limit_human": policy.get("upload_rate_limit_human"),
    }
    return {
        "tracker": policy.get("tracker"),
        "role": role,
        "rules_url": policy.get("rules_url"),
        "manual_review_required": policy.get("manual_review_required"),
        "rule_review_fingerprint": policy.get("rule_review_fingerprint"),
        "rule_obligations": build_rule_obligations(policy, roles=[role], accept_rules=accept_rules),
        "automation": policy.get("automation") if isinstance(policy.get("automation"), dict) else {},
        "qbit_limits": qbit_limits,
        "seeding_requirements": {
            "min_seed_time_hours": policy.get("min_seed_time_hours"),
            "min_ratio": policy.get("min_ratio"),
        },
        "transfer_rules": policy.get("transfer_rules") if isinstance(policy.get("transfer_rules"), dict) else {},
    }


def _policy_coverage_fields(coverages: list[dict[str, Any]], key: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for coverage in coverages:
        tracker = str(coverage.get("tracker") or "UNKNOWN")
        values = _string_list(coverage.get(key))
        if values:
            fields[tracker] = values
    return fields


def _service_result(kind: str, request: dict[str, Any], argv: list[str], result: dict[str, Any], started_at: float) -> dict[str, Any]:
    blockers = _result_blockers(result)
    next_actions = _string_list(result.get("next_actions"))
    summary_file = _result_summary_file(result)
    resume_state = _result_resume_state(result)
    next_command_argv = _result_next_command_argv(result)
    return {
        "kind": kind,
        "status": result.get("status", "ok"),
        "ok": result.get("status") not in {"blocked", "error"},
        "request": request,
        "command_argv": ["ptcli", *argv],
        "duplicate_check": _duplicate_check(result),
        "submit_if_clear_handoff": _submit_if_clear_handoff(request, _duplicate_check(result), kind=kind),
        "blockers": blockers,
        "next_actions": next_actions,
        "summary_file": summary_file,
        "resume_state": resume_state,
        "agent_summary": _agent_summary(result),
        "next_stage": _nested_value(result, "next_stage"),
        "next_command": _nested_value(result, "next_command"),
        "next_command_argv": next_command_argv,
        "automation_action": _nested_value(result, "automation_action"),
        "should_execute_next_command": _nested_value(result, "should_execute_next_command"),
        "elapsed_seconds": round(time.time() - started_at, 3),
        "result": result,
    }


def _duplicate_check(result: dict[str, Any]) -> dict[str, Any]:
    stage = _find_stage(result, "target-dupe-check")
    if not stage:
        return {"searched": False, "status": "unknown", "exists": None, "count": None, "dupes": []}
    stage_result = stage.get("result") if isinstance(stage.get("result"), dict) else {}
    dupes = stage_result.get("dupes") if isinstance(stage_result.get("dupes"), list) else []
    count = stage_result.get("count")
    if not isinstance(count, int):
        count = len(dupes)
    searched = bool(stage_result.get("searched") or stage.get("ok"))
    exists = count > 0 if searched else None
    status = "exists" if exists else "not_found" if searched else "unknown"
    return {
        "searched": searched,
        "status": status,
        "exists": exists,
        "count": count if searched else None,
        "dupes": dupes,
        "stage_ok": bool(stage.get("ok")),
        "message": stage.get("message"),
    }


def _find_stage(result: dict[str, Any], stage_name: str) -> dict[str, Any] | None:
    containers = [result]
    pipeline = result.get("pipeline")
    if isinstance(pipeline, dict):
        containers.append(pipeline)
    for container in containers:
        stages = container.get("stages")
        if not isinstance(stages, list):
            continue
        for stage in stages:
            if isinstance(stage, dict) and stage.get("stage") == stage_name:
                return stage
    return None


def _result_blockers(result: dict[str, Any]) -> list[str]:
    blockers = _string_list(result.get("blockers"))
    pipeline = result.get("pipeline")
    if isinstance(pipeline, dict):
        for blocker in _string_list(pipeline.get("blockers")):
            if blocker not in blockers:
                blockers.append(blocker)
    return blockers


def _agent_summary(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    candidate_summary = _agent_candidate_summary(payload)
    if candidate_summary:
        return candidate_summary
    material = _nested_dict(payload, "material_diagnostics")
    target_preflight = _nested_dict(payload, "target_preflight_diagnostics")
    resume_state = _nested_dict(payload, "resume_state")
    closure_status = _nested_dict(payload, "closure_status")
    target_upload_diagnostics = _nested_dict(payload, "target_upload_diagnostics")
    qbit_wait = _nested_dict(payload, "qbit_wait_diagnostics")
    duplicate_check = payload.get("duplicate_check") if isinstance(payload.get("duplicate_check"), dict) else _duplicate_check(payload)
    summary = {
        "status": _nested_value(payload, "status"),
        "ok": payload.get("ok") if isinstance(payload.get("ok"), bool) else None,
        "duplicate_check": duplicate_check,
        "metadata": _agent_metadata_summary(material),
        "materials": _agent_materials_summary(material),
        "target_preflight": _agent_target_preflight_summary(target_preflight, target_upload_diagnostics),
        "qbit": _agent_qbit_summary(closure_status, qbit_wait),
        "resume": _agent_resume_summary(resume_state, payload),
    }
    return summary if any(value for value in summary.values()) else None


def _agent_candidate_summary(payload: dict[str, Any]) -> dict[str, Any] | None:
    candidate_payload = _candidate_payload(payload)
    if not candidate_payload:
        return None
    digest = _candidate_digest_from_payload(candidate_payload) or {}
    push_items = digest.get("push_items") if isinstance(digest.get("push_items"), list) else []
    top_candidate = digest.get("top_candidate") if isinstance(digest.get("top_candidate"), dict) else {}
    top_policy_summary = top_candidate.get("policy_summary") if isinstance(top_candidate.get("policy_summary"), dict) else {}
    policy_coverage = top_policy_summary.get("policy_coverage") if isinstance(top_policy_summary.get("policy_coverage"), dict) else {}
    nested_result = candidate_payload.get("result") if isinstance(candidate_payload.get("result"), dict) else {}
    return {
        "type": "daily_candidates",
        "status": candidate_payload.get("status"),
        "ok": candidate_payload.get("ok") if isinstance(candidate_payload.get("ok"), bool) else None,
        "source_tracker": candidate_payload.get("source_tracker") or nested_result.get("source_tracker"),
        "target_trackers": candidate_payload.get("target_trackers") or nested_result.get("target_trackers"),
        "count": candidate_payload.get("count") or nested_result.get("count"),
        "ready_count": candidate_payload.get("ready_count") or nested_result.get("ready_count"),
        "recommendation": digest.get("recommendation"),
        "top_candidate": digest.get("top_candidate"),
        "top_submit_request": digest.get("top_submit_request"),
        "top_submit_job_endpoint": digest.get("top_submit_job_endpoint"),
        "top_submit_tool": digest.get("top_submit_tool"),
        "policy_coverage": policy_coverage or None,
        "policy_coverage_ready": policy_coverage.get("ready") if isinstance(policy_coverage.get("ready"), bool) else None,
        "push_count": len(push_items),
        "digest": digest,
        "blockers": _string_list(candidate_payload.get("blockers") or nested_result.get("blockers")),
        "next_actions": _string_list(candidate_payload.get("next_actions") or nested_result.get("next_actions")),
    }


def _agent_metadata_summary(material: dict[str, Any]) -> dict[str, Any]:
    metadata_fields = material.get("metadata_fields") if isinstance(material.get("metadata_fields"), dict) else {}
    description = material.get("description") if isinstance(material.get("description"), dict) else {}
    external_links = description.get("external_links") if isinstance(description.get("external_links"), dict) else {}
    external_id_readiness = description.get("external_id_readiness") if isinstance(description.get("external_id_readiness"), dict) else {}
    return {
        "ready": _metadata_ready(metadata_fields, external_id_readiness),
        "fields": metadata_fields,
        "imdb_ready": _field_ready(metadata_fields, "imdb") or _field_ready(metadata_fields, "imdb_id") or external_id_readiness.get("imdb"),
        "tmdb_ready": _field_ready(metadata_fields, "tmdb") or _field_ready(metadata_fields, "tmdb_id") or external_id_readiness.get("tmdb"),
        "douban_ready": _field_ready(metadata_fields, "douban") or _field_ready(metadata_fields, "douban_id") or external_id_readiness.get("douban"),
        "ptgen_description_ready": _field_ready(metadata_fields, "ptgen_description") or bool(description.get("has_ptgen_description")),
        "ptgen_description_length": _metadata_field_value(metadata_fields, "ptgen_description", "length") or description.get("ptgen_description_length"),
        "external_links": external_links,
        "missing": _string_list(description.get("external_id_missing")),
    }


def _agent_materials_summary(material: dict[str, Any]) -> dict[str, Any]:
    description = material.get("description") if isinstance(material.get("description"), dict) else {}
    critical_path = material.get("critical_path") if isinstance(material.get("critical_path"), dict) else {}
    image_host_urls = material.get("image_host_urls") if isinstance(material.get("image_host_urls"), dict) else {}
    live_gate = material.get("live_gate") if isinstance(material.get("live_gate"), dict) else {}
    return {
        "present": bool(material.get("present")),
        "ready_for_mteam_upload": material.get("ready_for_mteam_upload"),
        "critical_ready": material.get("critical_ready"),
        "critical_missing": _string_list(material.get("critical_missing")),
        "critical_path": {
            "ready": critical_path.get("ready"),
            "next_step": critical_path.get("next_step"),
            "missing": _string_list(critical_path.get("missing")),
        },
        "media_info_requirement": material.get("media_info_requirement"),
        "mediainfo_or_bdinfo_ready": description.get("has_mediainfo_or_bdinfo"),
        "screenshots_ready": description.get("has_screenshot_bbcode"),
        "screenshot_count": description.get("bbcode_image_count"),
        "image_host_urls": image_host_urls,
        "description_ready": description.get("ready"),
        "description_input_chain_ready": material.get("description_input_chain_ready"),
        "target_materials_ready": material.get("target_materials_ready"),
        "target_preparation_ready": material.get("target_preparation_ready"),
        "upload_material_blockers": _string_list(material.get("upload_material_blockers")),
        "live_gate": live_gate,
    }


def _agent_target_preflight_summary(target_preflight: dict[str, Any], target_upload_diagnostics: dict[str, Any]) -> dict[str, Any]:
    preflight = target_upload_diagnostics.get("preflight") if isinstance(target_upload_diagnostics.get("preflight"), dict) else {}
    source = target_preflight or preflight
    return {
        "ready": source.get("ready"),
        "target_preparation_ready": source.get("target_preparation_ready"),
        "materials_ready": source.get("materials_ready"),
        "metadata_ready": source.get("metadata_ready"),
        "assets_ready": source.get("assets_ready"),
        "description_ready": source.get("description_ready"),
        "payload_ready": source.get("payload_ready"),
        "materials_ready_required": source.get("materials_ready_required"),
        "missing": _string_list(source.get("missing")),
        "description_missing": _string_list(source.get("description_missing")),
        "blockers": _string_list(source.get("blockers")),
    }


def _agent_qbit_summary(closure_status: dict[str, Any], qbit_wait: dict[str, Any]) -> dict[str, Any]:
    source = closure_status.get("source") if isinstance(closure_status.get("source"), dict) else {}
    target = closure_status.get("target") if isinstance(closure_status.get("target"), dict) else {}
    return {
        "source": {
            "ready": source.get("ready"),
            "torrent_hash": source.get("torrent_hash"),
            "content_path": source.get("content_path"),
            "injection_verified": source.get("injection_verified"),
            "wait_complete": source.get("wait_complete"),
        },
        "target": {
            "ready": target.get("ready"),
            "uploaded_torrent_hash": target.get("uploaded_torrent_hash"),
            "injected_torrent_hash": target.get("injected_torrent_hash"),
            "injection_visible_in_client": target.get("injection_visible_in_client"),
            "injection_verified": target.get("injection_verified"),
            "uploaded_wait_evidence": target.get("uploaded_wait_evidence"),
        },
        "wait_diagnostics": qbit_wait,
    }


def _agent_resume_summary(resume_state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    materials = resume_state.get("materials") if isinstance(resume_state.get("materials"), dict) else {}
    return {
        "available": resume_state.get("resume_available"),
        "ready": resume_state.get("ready"),
        "next_stage": resume_state.get("next_stage") or _nested_value(payload, "next_stage"),
        "next_command": resume_state.get("next_command") or _nested_value(payload, "next_command"),
        "next_command_argv": _argv_list(resume_state.get("next_command_argv")) or _result_next_command_argv(payload),
        "materials_missing": _string_list(materials.get("target_materials_missing")) or _string_list(materials.get("target_preparation_missing")),
        "material_recovery_hints": materials.get("recovery_hints") if isinstance(materials.get("recovery_hints"), list) else [],
    }


def _candidate_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("kind") or "")
    if kind in {"ptcli.daily_candidates", "ptcli.service.daily_candidates"}:
        return payload
    nested = payload.get("result")
    if isinstance(nested, dict) and str(nested.get("kind") or "") in {"ptcli.daily_candidates", "ptcli.service.daily_candidates"}:
        return nested
    return payload if isinstance(payload.get("digest"), dict) and isinstance(payload.get("candidates"), list) else None


def _candidate_digest_from_payload(payload: Any) -> dict[str, Any] | None:
    candidate_payload = _candidate_payload(payload)
    if not candidate_payload:
        return None
    digest = candidate_payload.get("digest")
    if isinstance(digest, dict):
        return digest
    nested = candidate_payload.get("result")
    if isinstance(nested, dict) and isinstance(nested.get("digest"), dict):
        return nested["digest"]
    return None


def _nested_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = _nested_value(payload, key)
    return value if isinstance(value, dict) else {}


def _metadata_ready(metadata_fields: dict[str, Any], external_id_readiness: dict[str, Any]) -> bool | None:
    checks = [
        _field_ready(metadata_fields, "imdb") or _field_ready(metadata_fields, "imdb_id") or external_id_readiness.get("imdb"),
        _field_ready(metadata_fields, "tmdb") or _field_ready(metadata_fields, "tmdb_id") or external_id_readiness.get("tmdb"),
        _field_ready(metadata_fields, "douban") or _field_ready(metadata_fields, "douban_id") or external_id_readiness.get("douban"),
        _field_ready(metadata_fields, "ptgen_description"),
    ]
    boolean_checks = [check for check in checks if isinstance(check, bool)]
    return all(boolean_checks) if boolean_checks else None


def _field_ready(metadata_fields: dict[str, Any], key: str) -> bool | None:
    field = metadata_fields.get(key)
    if isinstance(field, dict) and isinstance(field.get("ready"), bool):
        return field["ready"]
    return None


def _metadata_field_value(metadata_fields: dict[str, Any], key: str, field_name: str) -> Any:
    field = metadata_fields.get(key)
    if isinstance(field, dict):
        return field.get(field_name)
    return None


def _resolve_job_dir(root: str | Path | None) -> Path:
    if root:
        return Path(root).expanduser()
    configured = os.environ.get("PTCLI_JOB_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "ptcli-jobs"


def _validate_job_id(job_id: str) -> None:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise ServiceError("Invalid job id.", status=HTTPStatus.BAD_REQUEST)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _resolve_max_concurrent_jobs(value: int | str | None = None) -> int:
    configured = value if value is not None else os.environ.get("PTCLI_MAX_CONCURRENT_JOBS")
    return _bounded_int(configured, default=DEFAULT_MAX_CONCURRENT_JOBS, minimum=1, maximum=16)


def _job_queue_summary(status_counts: dict[str, int], max_concurrent_jobs: int) -> dict[str, Any]:
    queued_count = int(status_counts.get("queued") or 0)
    running_count = int(status_counts.get("running") or 0)
    available_slots = max(0, max_concurrent_jobs - running_count)
    return {
        "max_concurrent_jobs": max_concurrent_jobs,
        "running_count": running_count,
        "queued_count": queued_count,
        "available_slots": available_slots,
        "backlog_count": max(0, queued_count - available_slots),
    }


def _timestamp(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _job_runtime(job: dict[str, Any]) -> dict[str, Any]:
    status = str(job.get("status") or "unknown")
    terminal = status not in {"queued", "running"}
    now = int(time.time())
    created_at = _timestamp(job.get("created_at"))
    updated_at = _timestamp(job.get("updated_at"))
    started_at = _timestamp(job.get("started_at"))
    completed_at = _timestamp(job.get("completed_at"))
    end_at = completed_at or now
    elapsed_seconds = max(0, end_at - created_at) if created_at is not None else None
    status_age_seconds = max(0, now - updated_at) if updated_at is not None and not terminal else None
    queued_until = started_at or completed_at or now
    queued_seconds = max(0, queued_until - created_at) if created_at is not None else None
    running_seconds = max(0, end_at - started_at) if started_at is not None else None
    job_id = job.get("job_id")
    return {
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "elapsed_seconds": elapsed_seconds,
        "status_age_seconds": status_age_seconds,
        "queued_seconds": queued_seconds,
        "running_seconds": running_seconds,
        "terminal": terminal,
        "should_poll": not terminal,
        "poll_after_seconds": DEFAULT_JOB_POLL_AFTER_SECONDS if not terminal else None,
        "status_endpoint": f"/v1/jobs/{job_id}" if job_id else None,
        "summary_endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None,
        "resume_endpoint": f"/v1/jobs/{job_id}/resume" if job_id else None,
    }


def _job_handoff(job: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    status = str(job.get("status") or "unknown")
    runtime = _job_runtime(job)
    agent_decision = _agent_decision(job)
    resume_plan = _job_resume_plan(job)
    resume_requirements = _job_resume_requirements(job, payload if isinstance(payload, dict) else None)
    resume_summary = _job_resume_summary(job)
    submit_if_clear = _job_submit_if_clear_handoff(job)
    candidate_submission_summary = _job_candidate_submission_summary(job, payload if isinstance(payload, dict) else None)
    candidate_execution = candidate_submission_summary.get("execution_handoff") if isinstance(candidate_submission_summary, dict) and isinstance(candidate_submission_summary.get("execution_handoff"), dict) else None
    closure_handoff = _job_closure_handoff(job, payload if isinstance(payload, dict) else None)
    next_step = closure_handoff.get("next_step") if isinstance(closure_handoff.get("next_step"), dict) else {}
    action = str(agent_decision.get("decision") or "inspect")
    recommended_tool = agent_decision.get("next_tool")
    recommended_endpoint = None
    recommended_method = None
    recommended_request = None
    continue_when = None
    stop_when: list[str] = []

    if runtime.get("should_poll"):
        action = "wait"
        recommended_tool = "get_job_status"
        recommended_endpoint = runtime.get("status_endpoint")
        recommended_method = "GET"
        continue_when = "status not in queued,running"
        stop_when = ["status in blocked,failed,cancelled"]
    elif isinstance(candidate_execution, dict):
        action = str(candidate_execution.get("state") or action)
        candidate_material_template = candidate_execution.get("material_input_template") if isinstance(candidate_execution.get("material_input_template"), dict) else {}
        candidate_material_dry_run_request = candidate_material_template.get("dry_run_request") if action == "prepare_materials" and isinstance(candidate_material_template.get("dry_run_request"), dict) else None
        recommended_tool = candidate_execution.get("recommended_tool")
        recommended_endpoint = candidate_execution.get("recommended_endpoint")
        recommended_method = candidate_execution.get("recommended_method")
        recommended_request = candidate_material_dry_run_request or candidate_execution.get("recommended_request")
        if candidate_material_dry_run_request:
            recommended_tool = "resume_job"
            recommended_endpoint = candidate_execution.get("recommended_endpoint") or runtime.get("resume_endpoint")
            recommended_method = "POST"
        continue_when = candidate_execution.get("continue_when")
        stop_when = _string_list(candidate_execution.get("stop_when"))
    elif action == "submit_if_clear" and isinstance(submit_if_clear, dict):
        recommended_tool = submit_if_clear.get("tool")
        recommended_endpoint = submit_if_clear.get("endpoint")
        recommended_method = submit_if_clear.get("method")
        recommended_request = submit_if_clear.get("request")
        continue_when = "job_id is returned"
        stop_when = ["duplicate_check.exists=true", "submit_if_clear_handoff.ready=false"]
    elif resume_plan.get("recommended") or resume_summary.get("recommended"):
        action = "resume"
        recommended_tool = "resume_job"
        recommended_endpoint = resume_plan.get("endpoint")
        recommended_method = "POST"
        recommended_request = resume_requirements.get("dry_run_request") or resume_requirements.get("request_template")
        continue_when = "resume_preview.ok=true; then call execute_request after user review"
        stop_when = ["resume_plan.allowed=false", "next_command_argv not allowlisted"]
    elif status == "complete":
        action = "done"
        recommended_tool = "get_job_summary"
        recommended_endpoint = runtime.get("summary_endpoint")
        recommended_method = "GET"
        continue_when = "report summary/evidence to user"
    elif status == "cancelled":
        action = "stop"
        recommended_tool = None
        stop_when = ["job.status=cancelled"]
    elif isinstance(next_step, dict) and next_step.get("tool"):
        recommended_tool = next_step.get("tool")
        recommended_endpoint = next_step.get("endpoint")
        recommended_method = next_step.get("method")
        recommended_request = next_step.get("request")
        continue_when = next_step.get("continue_when")
    else:
        recommended_tool = recommended_tool or agent_decision.get("recommended_tool")

    candidate_material_dry_run_request = _job_handoff_candidate_material_request(candidate_execution, "dry_run_request")
    candidate_material_execute_request = _job_handoff_candidate_material_request(candidate_execution, "execute_request")
    dry_run_request = candidate_material_dry_run_request or (resume_requirements.get("dry_run_request") if isinstance(resume_requirements.get("dry_run_request"), dict) else None)
    execute_request = candidate_material_execute_request or (resume_requirements.get("execute_request") if isinstance(resume_requirements.get("execute_request"), dict) else None)

    return {
        "kind": "ptcli.job_handoff",
        "job_id": job_id or None,
        "job_kind": job.get("kind"),
        "status": status,
        "terminal": bool(runtime.get("terminal")),
        "action": action,
        "recommended_tool": recommended_tool,
        "recommended_endpoint": recommended_endpoint,
        "recommended_method": recommended_method,
        "recommended_request": recommended_request,
        "continue_when": continue_when,
        "stop_when": stop_when,
        "status_endpoint": runtime.get("status_endpoint"),
        "summary_endpoint": runtime.get("summary_endpoint"),
        "resume_endpoint": runtime.get("resume_endpoint"),
        "poll_after_seconds": runtime.get("poll_after_seconds"),
        "should_poll": bool(runtime.get("should_poll")),
        "can_resume": bool(resume_plan.get("allowed")),
        "resume_recommended": bool(resume_plan.get("recommended")),
        "can_attempt_live": bool(agent_decision.get("can_attempt_live")),
        "summary_file": _job_summary_file(job),
        "resume_plan": resume_plan,
        "resume_requirements": resume_requirements,
        "resume_execution_handoff": _job_resume_execution_handoff(job, payload if isinstance(payload, dict) else None),
        "candidate_submission_execution": candidate_execution,
        "material_input_template": candidate_execution.get("material_input_template") if isinstance(candidate_execution, dict) else None,
        "dry_run_request": dry_run_request,
        "execute_request": execute_request,
        "agent_decision": agent_decision,
        "blockers": _string_list(job.get("blockers")),
        "next_actions": _job_handoff_next_actions(status, runtime, resume_plan, recommended_tool, job_id, _string_list(job.get("next_actions"))),
    }


def _job_handoff_candidate_material_request(candidate_execution: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    if not isinstance(candidate_execution, dict) or candidate_execution.get("state") != "prepare_materials":
        return None
    material_template = candidate_execution.get("material_input_template") if isinstance(candidate_execution.get("material_input_template"), dict) else {}
    request = material_template.get(key)
    return request if isinstance(request, dict) else None


def _job_handoff_next_actions(status: str, runtime: dict[str, Any], resume_plan: dict[str, Any], recommended_tool: Any, job_id: str, job_actions: list[str]) -> list[str]:
    actions: list[str] = []
    if runtime.get("should_poll"):
        actions.append(f"Poll /v1/jobs/{job_id} after {runtime.get('poll_after_seconds')} seconds.")
    elif resume_plan.get("recommended"):
        actions.append(f"Preview resume with POST /v1/jobs/{job_id}/resume dry_run=true, review command_argv, then execute when safe.")
    elif status == "complete":
        actions.append(f"Read /v1/jobs/{job_id}/summary and report closure_summary/evidence.")
    elif status == "cancelled":
        actions.append("Submit a new job if the work is still needed; cancelled jobs are terminal.")
    elif recommended_tool:
        actions.append(f"Call recommended_tool={recommended_tool} after reviewing blockers and site rules.")
    actions.extend(job_actions)
    return list(dict.fromkeys(action for action in actions if action))


def _job_recovery_handoff(job: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    status = str(job.get("status") or "unknown")
    runtime = _job_runtime(job)
    job_handoff = job.get("job_handoff") if isinstance(job.get("job_handoff"), dict) else {}
    resume_plan = _job_resume_plan(job)
    resume_requirements = _job_resume_requirements(job, payload if isinstance(payload, dict) else None)
    resume_execution = _job_resume_execution_handoff(job, payload if isinstance(payload, dict) else None)
    materials_handoff = _job_materials_handoff(job, payload if isinstance(payload, dict) else None)
    target_upload_handoff = _job_target_upload_handoff(job, payload if isinstance(payload, dict) else None)
    closure_handoff = _job_closure_handoff(job, payload if isinstance(payload, dict) else None)
    qbit_handoff = _job_qbit_handoff(job, payload if isinstance(payload, dict) else None)
    candidate_submission_summary = _job_candidate_submission_summary(job, payload if isinstance(payload, dict) else None)

    action, phase, reason = _job_recovery_decision(status, runtime, job_handoff, materials_handoff, target_upload_handoff, closure_handoff, resume_plan, candidate_submission_summary)
    next_step = _job_recovery_next_step(action, job_handoff, materials_handoff, target_upload_handoff, closure_handoff, resume_execution, runtime)
    recommended_request = next_step.get("request") if isinstance(next_step, dict) else None
    dry_run_request = _job_recovery_dry_run_request(action, materials_handoff, resume_execution, resume_requirements, recommended_request)
    execute_request = _job_recovery_execute_request(action, materials_handoff, resume_execution, resume_requirements)
    blockers = _job_recovery_blockers(job, materials_handoff, target_upload_handoff, closure_handoff, resume_execution, qbit_handoff)
    return {
        "kind": "ptcli.job_recovery_handoff",
        "job_id": job_id or None,
        "job_kind": job.get("kind"),
        "status": status,
        "terminal": bool(runtime.get("terminal")),
        "phase": phase,
        "action": action,
        "reason": reason,
        "ready": action in {"poll", "preview_resume", "execute_resume", "prepare_materials", "repair_target_payload", "repair_qbit", "read_summary"},
        "should_poll": bool(runtime.get("should_poll")),
        "should_resume": action in {"preview_resume", "execute_resume", "prepare_materials", "repair_target_payload", "repair_qbit"},
        "resume_preview_required": action in {"preview_resume", "prepare_materials", "repair_target_payload", "repair_qbit"},
        "recommended_tool": next_step.get("tool") if isinstance(next_step, dict) else None,
        "recommended_endpoint": next_step.get("endpoint") if isinstance(next_step, dict) else None,
        "recommended_method": next_step.get("method") if isinstance(next_step, dict) else None,
        "recommended_request": recommended_request,
        "dry_run_request": dry_run_request,
        "execute_request": execute_request,
        "status_endpoint": runtime.get("status_endpoint"),
        "summary_endpoint": runtime.get("summary_endpoint"),
        "resume_endpoint": runtime.get("resume_endpoint"),
        "poll_after_seconds": runtime.get("poll_after_seconds"),
        "gates": _job_recovery_gates(runtime, resume_plan, materials_handoff, target_upload_handoff, closure_handoff, qbit_handoff),
        "handoff_sources": {
            "job_handoff_action": job_handoff.get("action"),
            "resume_recommended": bool(resume_plan.get("recommended")),
            "materials_ready": materials_handoff.get("ready") if isinstance(materials_handoff, dict) else None,
            "target_upload_action": target_upload_handoff.get("action") if isinstance(target_upload_handoff, dict) else None,
            "closure_action": closure_handoff.get("action") if isinstance(closure_handoff, dict) else None,
            "candidate_submission_action": candidate_submission_summary.get("execution_state") if isinstance(candidate_submission_summary, dict) else None,
        },
        "read_fields": [
            "recovery_handoff",
            "job_handoff",
            "resume_execution_handoff",
            "resume_requirements",
            "materials_handoff",
            "target_upload_handoff",
            "closure_handoff",
            "qbit_handoff",
            "closure_summary",
        ],
        "continue_when": _job_recovery_continue_when(action),
        "stop_when": _job_recovery_stop_when(action),
        "blockers": blockers,
        "next_actions": _job_recovery_next_actions(action, next_step, blockers, runtime),
    }


def _job_recovery_decision(
    status: str,
    runtime: dict[str, Any],
    job_handoff: dict[str, Any],
    materials_handoff: dict[str, Any] | None,
    target_upload_handoff: dict[str, Any] | None,
    closure_handoff: dict[str, Any] | None,
    resume_plan: dict[str, Any],
    candidate_submission_summary: dict[str, Any] | None,
) -> tuple[str, str, str]:
    if runtime.get("should_poll"):
        return "poll", "runtime", "job_not_terminal"
    if status == "complete":
        return "read_summary", "complete", "job_complete"
    if status == "cancelled":
        return "stop", "cancelled", "job_cancelled"
    candidate_execution = candidate_submission_summary.get("execution_handoff") if isinstance(candidate_submission_summary, dict) and isinstance(candidate_submission_summary.get("execution_handoff"), dict) else None
    candidate_state = str(candidate_execution.get("state") or "") if isinstance(candidate_execution, dict) else ""
    if candidate_state in {"prepare_materials", "repair_target_payload", "repair_qbit", "resume"}:
        return candidate_state if candidate_state != "resume" else "preview_resume", "candidate_submission", f"candidate_submission.{candidate_state}"
    closure_action = str(closure_handoff.get("action") or "") if isinstance(closure_handoff, dict) else ""
    if closure_action in {"prepare_materials", "repair_target_payload", "repair_qbit"}:
        return closure_action, "closure", f"closure.{closure_action}"
    target_action = str(target_upload_handoff.get("action") or "") if isinstance(target_upload_handoff, dict) else ""
    if target_action in {"prepare_materials", "repair_target_payload", "collect_confirmations"}:
        return ("stop" if target_action == "collect_confirmations" else target_action), "target_upload", f"target_upload.{target_action}"
    if isinstance(materials_handoff, dict) and materials_handoff.get("ready") is False and isinstance(materials_handoff.get("resume_handoff"), dict) and materials_handoff["resume_handoff"].get("resume_recommended"):
        return "prepare_materials", "materials", "materials_resume_recommended"
    if resume_plan.get("recommended"):
        return "preview_resume", "resume", "resume_plan_recommended"
    if job_handoff.get("action") == "done":
        return "read_summary", "complete", "job_handoff_done"
    if job_handoff.get("action") in {"stop", "stop_duplicate", "collect_confirmations", "configure_policy"}:
        return "stop", "blocked", str(job_handoff.get("action"))
    return "inspect", "unknown", "no_recovery_action_selected"


def _job_recovery_next_step(
    action: str,
    job_handoff: dict[str, Any],
    materials_handoff: dict[str, Any] | None,
    target_upload_handoff: dict[str, Any] | None,
    closure_handoff: dict[str, Any] | None,
    resume_execution: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    if action == "poll":
        return {"tool": "get_job_status", "endpoint": runtime.get("status_endpoint"), "method": "GET", "request": None, "reason": "poll_running_job"}
    if action == "read_summary":
        return {"tool": "get_job_summary", "endpoint": runtime.get("summary_endpoint"), "method": "GET", "request": None, "reason": "read_terminal_summary"}
    if action == "prepare_materials" and isinstance(materials_handoff, dict):
        resume_handoff = materials_handoff.get("resume_handoff") if isinstance(materials_handoff.get("resume_handoff"), dict) else {}
        if resume_handoff.get("recommended_tool"):
            return {
                "tool": resume_handoff.get("recommended_tool"),
                "endpoint": resume_handoff.get("recommended_endpoint"),
                "method": resume_handoff.get("method"),
                "request": resume_handoff.get("recommended_request"),
                "reason": "materials_missing",
            }
    for handoff in (target_upload_handoff, closure_handoff):
        next_step = handoff.get("next_step") if isinstance(handoff, dict) and isinstance(handoff.get("next_step"), dict) else None
        if next_step and next_step.get("tool"):
            return next_step
    if action in {"preview_resume", "execute_resume", "repair_target_payload", "repair_qbit", "prepare_materials"}:
        request = resume_execution.get("dry_run_request") or resume_execution.get("recommended_request") or job_handoff.get("recommended_request")
        return {"tool": "resume_job", "endpoint": resume_execution.get("endpoint") or job_handoff.get("resume_endpoint"), "method": "POST", "request": request, "reason": action}
    return {
        "tool": job_handoff.get("recommended_tool"),
        "endpoint": job_handoff.get("recommended_endpoint"),
        "method": job_handoff.get("recommended_method"),
        "request": job_handoff.get("recommended_request"),
        "reason": action,
    }


def _job_recovery_dry_run_request(action: str, materials_handoff: dict[str, Any] | None, resume_execution: dict[str, Any], resume_requirements: dict[str, Any], recommended_request: Any) -> dict[str, Any] | None:
    if action == "prepare_materials" and isinstance(materials_handoff, dict):
        resume_handoff = materials_handoff.get("resume_handoff") if isinstance(materials_handoff.get("resume_handoff"), dict) else {}
        request = resume_handoff.get("dry_run_request")
        if isinstance(request, dict):
            return request
    for request in (resume_execution.get("dry_run_request"), resume_requirements.get("dry_run_request"), recommended_request):
        if isinstance(request, dict) and request.get("dry_run") is True:
            return request
    return None


def _job_recovery_execute_request(action: str, materials_handoff: dict[str, Any] | None, resume_execution: dict[str, Any], resume_requirements: dict[str, Any]) -> dict[str, Any] | None:
    if action == "prepare_materials" and isinstance(materials_handoff, dict):
        resume_handoff = materials_handoff.get("resume_handoff") if isinstance(materials_handoff.get("resume_handoff"), dict) else {}
        request = resume_handoff.get("execute_request")
        if isinstance(request, dict):
            return request
    for request in (resume_execution.get("execute_request"), resume_requirements.get("execute_request")):
        if isinstance(request, dict):
            return request
    return None


def _job_recovery_gates(
    runtime: dict[str, Any],
    resume_plan: dict[str, Any],
    materials_handoff: dict[str, Any] | None,
    target_upload_handoff: dict[str, Any] | None,
    closure_handoff: dict[str, Any] | None,
    qbit_handoff: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "terminal": bool(runtime.get("terminal")),
        "resume_available": bool(resume_plan.get("available")),
        "resume_allowed": bool(resume_plan.get("allowed")),
        "resume_recommended": bool(resume_plan.get("recommended")),
        "materials_ready": materials_handoff.get("ready") if isinstance(materials_handoff, dict) else None,
        "target_upload_ready": target_upload_handoff.get("ready_for_live_upload") if isinstance(target_upload_handoff, dict) else None,
        "uploaded_seeding_ready": target_upload_handoff.get("uploaded_seeding_ready") if isinstance(target_upload_handoff, dict) else None,
        "closure_complete": closure_handoff.get("complete") if isinstance(closure_handoff, dict) else None,
        "qbit_ready": qbit_handoff.get("ready") if isinstance(qbit_handoff, dict) else None,
    }


def _job_recovery_blockers(
    job: dict[str, Any],
    materials_handoff: dict[str, Any] | None,
    target_upload_handoff: dict[str, Any] | None,
    closure_handoff: dict[str, Any] | None,
    resume_execution: dict[str, Any],
    qbit_handoff: dict[str, Any] | None,
) -> list[str]:
    blockers = _string_list(job.get("blockers"))
    for handoff in (materials_handoff, target_upload_handoff, closure_handoff, resume_execution, qbit_handoff):
        if isinstance(handoff, dict):
            blockers.extend(_string_list(handoff.get("blockers")))
    return list(dict.fromkeys(str(item) for item in blockers if item))


def _job_recovery_continue_when(action: str) -> str | None:
    return {
        "poll": "job status leaves queued/running",
        "preview_resume": "resume dry_run returns ok=true and command_argv has been reviewed",
        "execute_resume": "resume execute_request creates a child job or completes the requested step",
        "prepare_materials": "materials_handoff.ready=true and target_upload_handoff.preflight.payload_ready=true",
        "repair_target_payload": "target_upload_handoff.ready_for_live_upload=true",
        "repair_qbit": "qbit_handoff.ready=true and closure_handoff.complete=true",
        "read_summary": "closure_summary.complete=true or blockers are reported to the user",
    }.get(action)


def _job_recovery_stop_when(action: str) -> list[str]:
    common = ["duplicate_check.exists=true", "site rule obligations are not ready", "resume preview contains unexpected ignored_overrides"]
    if action == "poll":
        return ["job status becomes blocked, failed, or cancelled"]
    if action == "read_summary":
        return ["closure_summary.blockers is non-empty"]
    if action == "stop":
        return ["current blockers require user action before automation continues"]
    return common


def _job_recovery_next_actions(action: str, next_step: dict[str, Any], blockers: list[str], runtime: dict[str, Any]) -> list[str]:
    if action == "poll":
        return [f"Poll recovery_handoff.status_endpoint after {runtime.get('poll_after_seconds')} seconds."]
    if action in {"preview_resume", "prepare_materials", "repair_target_payload", "repair_qbit"}:
        return ["Call recovery_handoff.dry_run_request first, review command_argv and site-rule gates, then use execute_request only after approval."]
    if action == "read_summary":
        return ["Read recovery_handoff.summary_endpoint and report closure_summary/evidence."]
    if blockers:
        return ["Resolve recovery_handoff.blockers before continuing automation."]
    if next_step.get("tool"):
        return [f"Call recovery_handoff.recommended_tool={next_step.get('tool')} after reviewing blockers."]
    return ["Inspect recovery_handoff.read_fields before taking the next automation step."]


def _job_lineage_summary(job: dict[str, Any], all_jobs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    all_jobs = all_jobs or [job]
    job_id = str(job.get("job_id") or "")
    by_id = {str(item.get("job_id")): item for item in all_jobs if isinstance(item, dict) and item.get("job_id")}
    parent_job_id = _job_parent_job_id(job)
    children = [item for item in all_jobs if _job_parent_job_id(item) == job_id]
    children.sort(key=lambda item: _timestamp(item.get("created_at")) or 0)
    chain = _job_lineage_chain(job, by_id)
    root_job_id = chain[0].get("job_id") if chain else job_id or None
    latest_child = _job_lineage_item(children[-1]) if children else None
    return {
        "kind": "ptcli.job_lineage",
        "job_id": job_id or None,
        "parent_job_id": parent_job_id,
        "root_job_id": root_job_id,
        "depth": max(0, len(chain) - 1),
        "is_resume_job": bool(parent_job_id or str(job.get("kind") or "") == "ptcli.resume"),
        "chain": chain,
        "child_count": len(children),
        "children": [_job_lineage_item(child) for child in children],
        "latest_child": latest_child,
        "has_active_child": any(child.get("status") in {"queued", "running"} for child in children),
        "terminal_child_count": sum(1 for child in children if child.get("status") not in {"queued", "running"}),
        "next_actions": _job_lineage_next_actions(job_id, children, latest_child),
    }


def _job_parent_job_id(job: dict[str, Any]) -> str | None:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    lineage = request.get("resume_lineage") if isinstance(request.get("resume_lineage"), dict) else {}
    context = request.get("resume_context") if isinstance(request.get("resume_context"), dict) else {}
    parent = request.get("parent_job_id") or lineage.get("parent_job_id") or context.get("parent_job_id")
    return str(parent) if parent else None


def _job_lineage_chain(job: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    chain = [_job_lineage_item(job)]
    seen = {str(job.get("job_id") or "")}
    current = job
    while True:
        parent_id = _job_parent_job_id(current)
        if not parent_id or parent_id in seen:
            break
        parent = by_id.get(parent_id)
        if not parent:
            chain.insert(0, {"job_id": parent_id, "missing": True})
            break
        chain.insert(0, _job_lineage_item(parent))
        seen.add(parent_id)
        current = parent
    return chain


def _job_lineage_item(job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    return {
        "job_id": job_id or None,
        "kind": job.get("kind"),
        "status": job.get("status"),
        "parent_job_id": _job_parent_job_id(job),
        "created_at": job.get("created_at"),
        "completed_at": job.get("completed_at"),
        "status_endpoint": f"/v1/jobs/{job_id}" if job_id else None,
        "summary_endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None,
        "resume_endpoint": f"/v1/jobs/{job_id}/resume" if job_id else None,
    }


def _job_lineage_next_actions(job_id: str, children: list[dict[str, Any]], latest_child: dict[str, Any] | None) -> list[str]:
    if not children:
        return []
    if any(child.get("status") in {"queued", "running"} for child in children):
        return [f"Poll active child jobs before resuming parent job {job_id} again."]
    if latest_child:
        return [f"Inspect latest_child.summary_endpoint before deciding whether parent job {job_id} still needs another resume."]
    return []


def _job_list_item(job: dict[str, Any], job_lineage: dict[str, Any] | None = None) -> dict[str, Any]:
    job_id = job.get("job_id")
    return {
        "job_id": job_id,
        "kind": job.get("kind"),
        "status": job.get("status"),
        "ok": job.get("status") == "complete",
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "completed_at": job.get("completed_at"),
        "runtime": _job_runtime(job),
        "blockers": _string_list(job.get("blockers")),
        "next_actions": _string_list(job.get("next_actions")),
        "interruption": job.get("interruption") if isinstance(job.get("interruption"), dict) else None,
        "cancellation": job.get("cancellation") if isinstance(job.get("cancellation"), dict) else None,
        "summary_file": _job_summary_file(job),
        "source_reference": _job_source_reference(job),
        "target_trackers": (job.get("request") or {}).get("target_trackers") if isinstance(job.get("request"), dict) else None,
        "duplicate_check": _job_duplicate_check(job),
        "submit_if_clear_handoff": _job_submit_if_clear_handoff(job),
        "policy_handoff": _job_policy_handoff(job),
        "qbit_plan": _job_qbit_plan(job),
        "qbit_limit_audit": _job_qbit_limit_audit(job),
        "qbit_handoff": _job_qbit_handoff(job),
        "qbit_enforcement_summary": _job_qbit_enforcement_summary(job),
        "materials_handoff": _job_materials_handoff(job),
        "target_upload_handoff": _job_target_upload_handoff(job),
        "closure_handoff": _job_closure_handoff(job),
        "closure_summary": _job_closure_summary(job),
        "manual_retorrent_handoff": _job_manual_retorrent_handoff(job),
        "agent_decision": job.get("agent_decision") if isinstance(job.get("agent_decision"), dict) else _agent_decision(job),
        "resume_plan": _job_resume_plan(job),
        "resume_requirements": _job_resume_requirements(job),
        "resume_execution_handoff": _job_resume_execution_handoff(job),
        "resume_lineage": _job_resume_lineage(job),
        "job_lineage": job_lineage or _job_lineage_summary(job),
        "resume_audit": _job_resume_audit(job),
        "resume_summary": _job_resume_summary(job),
        "material_resolution": _job_material_resolution(job),
        "candidate_submission": _job_candidate_submission(job),
        "check_submission": _job_check_submission(job),
        "candidate_batch_handoff": _job_candidate_batch_handoff(job),
        "candidate_submission_handoff": _job_candidate_submission_handoff(job),
        "candidate_submission_summary": _job_candidate_submission_summary(job),
        "job_handoff": _job_handoff(job),
        "recovery_handoff": _job_recovery_handoff(job),
        "status_endpoint": f"/v1/jobs/{job_id}" if job_id else None,
        "summary_endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None,
        "resume_endpoint": f"/v1/jobs/{job_id}/resume" if job_id else None,
    }


def _job_list_next_actions(jobs: list[dict[str, Any]], total: int, limit: int) -> list[str]:
    actions: list[str] = []
    if total > limit:
        actions.append("Increase limit or filter by status/kind to inspect additional jobs.")
    if any(job.get("status") in {"queued", "running"} for job in jobs):
        actions.append("Poll running jobs with jobs[].status_endpoint until they complete or block.")
    if any((job.get("resume_plan") or {}).get("recommended") for job in jobs if isinstance(job.get("resume_plan"), dict)):
        actions.append("Resume recommended blocked jobs with jobs[].resume_endpoint after reviewing blockers and site rules.")
    return actions


def _job_public_payload(job: dict[str, Any], job_lineage: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": job.get("status"),
        "ok": job.get("status") == "complete",
        "job_id": job.get("job_id"),
        "kind": job.get("kind"),
        "request": job.get("request"),
        "command_argv": job.get("command_argv"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "runtime": _job_runtime(job),
        "blockers": _string_list(job.get("blockers")),
        "next_actions": _string_list(job.get("next_actions")),
        "interruption": job.get("interruption") if isinstance(job.get("interruption"), dict) else None,
        "cancellation": job.get("cancellation") if isinstance(job.get("cancellation"), dict) else None,
        "duplicate_check": job.get("duplicate_check"),
        "submit_if_clear_handoff": _job_submit_if_clear_handoff(job),
        "summary_file": job.get("summary_file"),
        "resume_state": job.get("resume_state"),
        "agent_summary": job.get("agent_summary") if isinstance(job.get("agent_summary"), dict) else _agent_summary(job.get("result")),
        "agent_decision": job.get("agent_decision") if isinstance(job.get("agent_decision"), dict) else _agent_decision(job),
        "candidate_digest": _candidate_digest_from_payload(job.get("result")),
        "policy_coverage": _job_policy_coverage(job),
        "policy_handoff": _job_policy_handoff(job),
        "policy_qbit_defaults": _job_policy_qbit_defaults(job),
        "qbit_plan": _job_qbit_plan(job),
        "qbit_limit_audit": _job_qbit_limit_audit(job),
        "qbit_handoff": _job_qbit_handoff(job),
        "qbit_enforcement_summary": _job_qbit_enforcement_summary(job),
        "materials_handoff": _job_materials_handoff(job),
        "target_upload_handoff": _job_target_upload_handoff(job),
        "closure_handoff": _job_closure_handoff(job),
        "closure_summary": _job_closure_summary(job),
        "manual_retorrent_handoff": _job_manual_retorrent_handoff(job),
        "resume_plan": _job_resume_plan(job),
        "resume_requirements": _job_resume_requirements(job),
        "resume_execution_handoff": _job_resume_execution_handoff(job),
        "resume_lineage": _job_resume_lineage(job),
        "job_lineage": job_lineage or _job_lineage_summary(job),
        "resume_context": _job_resume_context(job),
        "resume_audit": _job_resume_audit(job),
        "resume_summary": _job_resume_summary(job),
        "material_resolution": _job_material_resolution(job),
        "candidate_submission": _job_candidate_submission(job),
        "check_submission": _job_check_submission(job),
        "candidate_batch_handoff": _job_candidate_batch_handoff(job),
        "candidate_submission_handoff": _job_candidate_submission_handoff(job),
        "candidate_submission_summary": _job_candidate_submission_summary(job),
        "source_reference": _job_source_reference(job),
        "workflow_context": _job_workflow_context(job),
        "job_handoff": _job_handoff(job),
        "recovery_handoff": _job_recovery_handoff(job),
        "result_status": _nested_value(job.get("result"), "status"),
        "next_stage": _nested_value(job.get("result"), "next_stage"),
        "next_command": _nested_value(job.get("result"), "next_command"),
        "next_command_argv": _result_next_command_argv(job.get("result")),
        "should_execute_next_command": _nested_value(job.get("result"), "should_execute_next_command"),
        "automation_action": _nested_value(job.get("result"), "automation_action"),
    }


def _submit_if_clear_handoff(request: dict[str, Any], duplicate_check: dict[str, Any], *, kind: str | None = None, job_id: str | None = None) -> dict[str, Any] | None:
    submit_request = _submit_if_clear_request(request)
    if not submit_request:
        return None
    searched = duplicate_check.get("searched") is True
    duplicate_exists = duplicate_check.get("exists") is True
    duplicate_clear = searched and not duplicate_exists
    ready = duplicate_clear
    blockers: list[str] = []
    if not searched:
        blockers.append("duplicate_check.not_searched")
    elif duplicate_exists:
        blockers.append("duplicate_check.exists")
    blockers.extend(_submit_if_clear_missing_confirmations(submit_request))
    ready = ready and not blockers
    return {
        "kind": "ptcli.submit_if_clear_handoff",
        "ready": ready,
        "source_kind": kind,
        "source_job_id": job_id,
        "duplicate_clear": duplicate_clear,
        "duplicate_check": duplicate_check,
        "tool": "source_url_retorrent_job",
        "endpoint": "/v1/jobs/retorrent/from-url",
        "method": "POST",
        "request": submit_request,
        "requires_before_call": ["duplicate_check.searched=true", "duplicate_check.exists=false", "accept_rules=true", "confirm_upload=true"],
        "blockers": list(dict.fromkeys(blockers)),
        "next_step": _submit_if_clear_next_step(ready, submit_request, duplicate_check, blockers),
        "next_actions": _submit_if_clear_next_actions(ready, duplicate_exists, blockers),
    }


def _submit_if_clear_request(request: dict[str, Any]) -> dict[str, Any] | None:
    source_url = request.get("source_url") or request.get("source")
    target = request.get("target_trackers") or request.get("target")
    if not source_url or not target:
        return None
    submit_request: dict[str, Any] = {
        "source_url": str(source_url),
        "target": target,
        "accept_rules": request.get("accept_rules") is True or _truthy(request.get("accept_rules")),
        "confirm_upload": request.get("confirm_upload") is True or _truthy(request.get("confirm_upload")),
    }
    for key in (
        "config",
        "base_dir",
        "client",
        "path",
        "content_path",
        "save_path",
        "source_torrent_file",
        "target_torrent_file",
        "uploaded_torrent_file",
        "uploaded_qbit_category",
        "uploaded_qbit_tags",
        "uploaded_qbit_upload_limit",
        "uploaded_qbit_download_limit",
        "qbit_category",
        "qbit_tags",
        "qbit_upload_limit",
        "qbit_download_limit",
        "metadata_file",
        "ptgen_description_file",
        "mediainfo_file",
        "bdinfo_file",
        "image_host_file",
        "image_host",
        "screenshot_file",
        "screenshot_files",
        "screenshot_count",
        "enrich_metadata",
        "fetch_ptgen",
        "generate_mediainfo",
        "generate_bdinfo",
        "generate_screenshots",
        "upload_screenshots",
        "imdb_id",
        "tmdb_id",
        "tmdb_type",
        "douban_id",
        "douban_url",
    ):
        if request.get(key) is not None:
            submit_request[key] = request[key]
    return submit_request


def _submit_if_clear_missing_confirmations(request: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if request.get("accept_rules") is not True:
        missing.append("accept_rules=true")
    if request.get("confirm_upload") is not True:
        missing.append("confirm_upload=true")
    return missing


def _submit_if_clear_next_step(ready: bool, request: dict[str, Any], duplicate_check: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    if ready:
        return {"tool": "source_url_retorrent_job", "endpoint": "/v1/jobs/retorrent/from-url", "method": "POST", "request": request, "reason": "duplicate_clear"}
    if duplicate_check.get("searched") is not True:
        return {"tool": "retorrent_check", "endpoint": "/v1/retorrent/check", "method": "POST", "request": request, "reason": "duplicate_check_required"}
    return {"tool": None, "endpoint": None, "method": None, "request": None, "reason": "submit_if_clear_blocked", "blockers": blockers}


def _submit_if_clear_next_actions(ready: bool, duplicate_exists: bool, blockers: list[str]) -> list[str]:
    if ready:
        return ["Submit submit_if_clear_handoff.request to source_url_retorrent_job."]
    if duplicate_exists:
        return ["Do not upload; target duplicate exists. Inspect duplicate_check.dupes."]
    if blockers:
        return ["Resolve submit_if_clear_handoff.blockers before creating a live retorrent job."]
    return ["Run retorrent_check before creating a live retorrent job."]


def _job_submit_if_clear_handoff(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    handoff = _submit_if_clear_handoff(request, _job_duplicate_check(job), kind=str(job.get("kind") or ""), job_id=str(job.get("job_id") or ""))
    return handoff


def _job_policy_coverage(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request")
    if isinstance(request, dict) and isinstance(request.get("policy_coverage"), dict):
        return request["policy_coverage"]
    return None


def _job_policy_handoff(job: dict[str, Any]) -> dict[str, Any] | None:
    coverage = _job_policy_coverage(job)
    qbit_defaults = _job_policy_qbit_defaults(job)
    qbit_plan = _job_qbit_plan(job)
    if not isinstance(coverage, dict) and not isinstance(qbit_defaults, dict):
        return None
    ready = coverage.get("ready") if isinstance(coverage, dict) and isinstance(coverage.get("ready"), bool) else None
    obligations = coverage.get("obligations") if isinstance(coverage, dict) and isinstance(coverage.get("obligations"), dict) else {}
    missing_fields = coverage.get("missing_policy_fields") if isinstance(coverage, dict) and isinstance(coverage.get("missing_policy_fields"), dict) else {}
    disabled_automation = coverage.get("disabled_automation") if isinstance(coverage, dict) and isinstance(coverage.get("disabled_automation"), dict) else {}
    blockers = _policy_handoff_blockers(coverage, missing_fields, disabled_automation)
    next_step = _policy_handoff_next_step(job, ready, blockers)
    return {
        "kind": "ptcli.policy_handoff",
        "ready": ready,
        "accepted_rules": coverage.get("accept_rules") if isinstance(coverage, dict) else None,
        "site_policy_ready": coverage.get("site_policy_ready") if isinstance(coverage, dict) else None,
        "source": obligations.get("source"),
        "targets": obligations.get("targets") if isinstance(obligations.get("targets"), list) else [],
        "missing_policy_fields": missing_fields,
        "disabled_automation": disabled_automation,
        "qbit_defaults": qbit_defaults,
        "qbit_plan": qbit_plan,
        "recommendations": _string_list(coverage.get("recommendations")) if isinstance(coverage, dict) else [],
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "blockers": blockers,
        "next_actions": _policy_handoff_next_actions(ready, blockers),
    }


def _policy_handoff_blockers(coverage: dict[str, Any] | None, missing_fields: dict[str, Any], disabled_automation: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if isinstance(coverage, dict):
        blockers.extend(_string_list(coverage.get("blockers")))
        for tracker, fields in missing_fields.items():
            blockers.extend(f"{tracker}.{field}" for field in _string_list(fields))
        for tracker, fields in disabled_automation.items():
            blockers.extend(f"{tracker}.{field}" for field in _string_list(fields))
    return list(dict.fromkeys(blockers))


def _policy_handoff_next_step(job: dict[str, Any], ready: bool | None, blockers: list[str]) -> dict[str, Any]:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    source = _job_source_reference(job)
    policy_request: dict[str, Any] = {"accept_rules": bool(request.get("accept_rules"))}
    if isinstance(source, dict) and source.get("tracker"):
        policy_request["source_tracker"] = source.get("tracker")
    if request.get("target_trackers"):
        policy_request["target"] = request.get("target_trackers")
    if request.get("config"):
        policy_request["config"] = request.get("config")
    if ready is False or blockers:
        return {"tool": "site_policies", "endpoint": "/v1/site-policies", "method": "POST", "request": policy_request, "reason": "policy_obligations_not_ready"}
    job_id = job.get("job_id")
    return {"tool": "get_job_summary", "endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None, "method": "GET", "request": None, "reason": "policy_obligations_ready"}


def _policy_handoff_next_actions(ready: bool | None, blockers: list[str]) -> list[str]:
    if ready is True and not blockers:
        return []
    if blockers:
        return ["Review policy_handoff.blockers and update PTCLI.SITE_POLICIES before live automation."]
    return ["Inspect policy_handoff obligations before live automation."]


def _job_policy_qbit_defaults(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request")
    if isinstance(request, dict) and isinstance(request.get("policy_qbit_defaults"), dict):
        return request["policy_qbit_defaults"]
    return None


def _job_qbit_plan(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request")
    if not isinstance(request, dict):
        return None
    defaults = request.get("policy_qbit_defaults") if isinstance(request.get("policy_qbit_defaults"), dict) else {}
    sources = defaults.get("sources") if isinstance(defaults.get("sources"), dict) else {}
    overrides = defaults.get("request_overrides") if isinstance(defaults.get("request_overrides"), dict) else {}
    return {
        "client": request.get("client") or "default",
        "source": {
            "category": request.get("qbit_category"),
            "tags": request.get("qbit_tags"),
            "upload_limit": request.get("qbit_upload_limit"),
            "download_limit": request.get("qbit_download_limit"),
            "upload_limit_source": _qbit_plan_value_source("qbit_upload_limit", sources, overrides),
            "download_limit_source": _qbit_plan_value_source("qbit_download_limit", sources, overrides),
        },
        "uploaded": {
            "category": request.get("uploaded_qbit_category"),
            "tags": request.get("uploaded_qbit_tags"),
            "upload_limit": request.get("uploaded_qbit_upload_limit"),
            "download_limit": request.get("uploaded_qbit_download_limit"),
            "upload_limit_source": _qbit_plan_value_source("uploaded_qbit_upload_limit", sources, overrides),
            "download_limit_source": _qbit_plan_value_source("uploaded_qbit_download_limit", sources, overrides),
        },
        "policy_defaults": defaults or None,
    }


def _job_qbit_limit_audit(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    qbit_plan = _job_qbit_plan(job)
    if not qbit_plan:
        return None
    payloads = [payload for payload in (summary_payload, job.get("result")) if isinstance(payload, dict)]
    source = _qbit_limit_role_audit("source", qbit_plan.get("source"), _qbit_limit_source_result(payloads))
    uploaded = _qbit_limit_role_audit("uploaded", qbit_plan.get("uploaded"), _qbit_limit_uploaded_result(payloads))
    blockers = [*source["blockers"], *uploaded["blockers"]]
    return {
        "ready": not blockers,
        "source": source,
        "uploaded": uploaded,
        "blockers": blockers,
        "next_actions": _qbit_limit_audit_next_actions(blockers),
    }


def _job_qbit_handoff(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    plan = _job_qbit_plan(job)
    if not isinstance(plan, dict):
        return None
    audit = _job_qbit_limit_audit(job, summary_payload)
    source_plan = plan.get("source") if isinstance(plan.get("source"), dict) else {}
    uploaded_plan = plan.get("uploaded") if isinstance(plan.get("uploaded"), dict) else {}
    source_audit = audit.get("source") if isinstance(audit, dict) and isinstance(audit.get("source"), dict) else {}
    uploaded_audit = audit.get("uploaded") if isinstance(audit, dict) and isinstance(audit.get("uploaded"), dict) else {}
    blockers = _string_list(audit.get("blockers")) if isinstance(audit, dict) else []
    enforcement_handoff = _qbit_enforcement_handoff(plan, audit, blockers)
    return {
        "kind": "ptcli.qbit_handoff",
        "client": plan.get("client"),
        "ready": bool(audit.get("ready")) if isinstance(audit, dict) else False,
        "source": _qbit_handoff_role("source", source_plan, source_audit),
        "uploaded": _qbit_handoff_role("uploaded", uploaded_plan, uploaded_audit),
        "enforcement_handoff": enforcement_handoff,
        "policy_defaults": plan.get("policy_defaults"),
        "blockers": blockers,
        "next_actions": _qbit_handoff_next_actions(blockers),
    }


def _job_qbit_enforcement_summary(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    handoff = _job_qbit_handoff(job, summary_payload)
    if not isinstance(handoff, dict):
        return None
    enforcement = handoff.get("enforcement_handoff") if isinstance(handoff.get("enforcement_handoff"), dict) else {}
    roles = enforcement.get("roles") if isinstance(enforcement.get("roles"), list) else []
    role_summaries = [_qbit_enforcement_summary_role(role) for role in roles if isinstance(role, dict)]
    expected_roles = [role["role"] for role in role_summaries if role.get("has_expected_limits")]
    applied_roles = [role["role"] for role in role_summaries if role.get("status") == "applied"]
    pending_roles = _string_list(enforcement.get("pending_roles"))
    mismatch_roles = _string_list(enforcement.get("mismatch_roles"))
    blockers = _string_list(enforcement.get("blockers")) or _string_list(handoff.get("blockers"))
    next_step = enforcement.get("next_step") if isinstance(enforcement.get("next_step"), dict) else {}
    return {
        "kind": "ptcli.qbit_enforcement_summary",
        "ready": enforcement.get("ready") is True,
        "status": enforcement.get("status") or "none",
        "client": handoff.get("client"),
        "expected_role_count": len(expected_roles),
        "applied_role_count": len(applied_roles),
        "pending_role_count": len(pending_roles),
        "mismatch_role_count": len(mismatch_roles),
        "expected_roles": expected_roles,
        "applied_roles": applied_roles,
        "pending_roles": pending_roles,
        "mismatch_roles": mismatch_roles,
        "roles": role_summaries,
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "blockers": blockers,
        "next_actions": _qbit_enforcement_summary_next_actions(enforcement.get("ready") is True, pending_roles, mismatch_roles, blockers),
    }


def _qbit_enforcement_summary_role(role: dict[str, Any]) -> dict[str, Any]:
    expected_limits = role.get("expected_limits") if isinstance(role.get("expected_limits"), dict) else {}
    observed_limits = role.get("observed_limits") if isinstance(role.get("observed_limits"), dict) else None
    return {
        "role": role.get("role"),
        "ready": role.get("ready") is True,
        "status": role.get("status") or "none",
        "has_expected_limits": bool(expected_limits),
        "expected_limits": expected_limits,
        "observed_limits": observed_limits,
        "evidence_present": role.get("evidence_present") is True,
        "requires_injection_evidence": role.get("requires_injection_evidence") is True,
        "requires_rate_limit_repair": role.get("requires_rate_limit_repair") is True,
        "upload_limit_source": role.get("upload_limit_source"),
        "download_limit_source": role.get("download_limit_source"),
        "blockers": _string_list(role.get("blockers")),
    }


def _qbit_enforcement_summary_next_actions(ready: bool, pending_roles: list[str], mismatch_roles: list[str], blockers: list[str]) -> list[str]:
    if ready:
        return ["qBittorrent category/tag/rate-limit enforcement is verified; continue with closure summary and seeding evidence."]
    if pending_roles:
        return [f"Resume or rerun qBittorrent injection for roles: {', '.join(pending_roles)}; then read qbit_enforcement_summary again."]
    if mismatch_roles:
        return [f"Repair qBittorrent rate limits for roles: {', '.join(mismatch_roles)}; then verify qbit_limit_audit."]
    if blockers:
        return ["Inspect qbit_enforcement_summary.blockers and qbit_handoff.enforcement_handoff before continuing."]
    return ["Inspect qbit_enforcement_summary before treating qBittorrent enforcement as complete."]


def _qbit_handoff_role(role: str, plan: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    expected = audit.get("expected") if isinstance(audit.get("expected"), dict) else {}
    observed = audit.get("observed") if isinstance(audit.get("observed"), dict) else None
    return {
        "role": role,
        "category": plan.get("category"),
        "tags": plan.get("tags"),
        "upload_limit": plan.get("upload_limit"),
        "download_limit": plan.get("download_limit"),
        "upload_limit_source": plan.get("upload_limit_source"),
        "download_limit_source": plan.get("download_limit_source"),
        "expected_limits": expected,
        "audit_status": audit.get("status") or "none",
        "audit_ready": bool(audit.get("ready")),
        "evidence_present": bool(audit.get("evidence_present")),
        "observed_limits": observed,
        "blockers": _string_list(audit.get("blockers")),
    }


def _qbit_handoff_next_actions(blockers: list[str]) -> list[str]:
    if not blockers:
        return []
    return ["Review qbit_handoff.source/uploaded blockers, then resume or rerun the affected qBittorrent injection step with the listed category, tags, and rate limits."]


def _qbit_enforcement_handoff(plan: dict[str, Any], audit: dict[str, Any] | None, blockers: list[str]) -> dict[str, Any]:
    roles = []
    for role in ("source", "uploaded"):
        role_plan = plan.get(role) if isinstance(plan.get(role), dict) else {}
        role_audit = audit.get(role) if isinstance(audit, dict) and isinstance(audit.get(role), dict) else {}
        roles.append(_qbit_enforcement_role(role, role_plan, role_audit))
    pending_roles = [role["role"] for role in roles if role.get("status") == "pending"]
    mismatch_roles = [role["role"] for role in roles if role.get("status") == "mismatch"]
    return {
        "kind": "ptcli.qbit_enforcement_handoff",
        "ready": not blockers,
        "status": "ready" if not blockers else "mismatch" if mismatch_roles else "pending" if pending_roles else "blocked",
        "roles": roles,
        "pending_roles": pending_roles,
        "mismatch_roles": mismatch_roles,
        "blockers": blockers,
        "next_step": _qbit_enforcement_next_step(blockers, pending_roles, mismatch_roles),
        "continue_when": "qbit_limit_audit.ready=true and source/uploaded role blockers are empty",
        "stop_when": "any qbit_handoff.enforcement_handoff.roles[].blockers remain after the injection or resume step",
    }


def _qbit_enforcement_role(role: str, plan: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    expected_limits = audit.get("expected") if isinstance(audit.get("expected"), dict) else {}
    observed_limits = audit.get("observed") if isinstance(audit.get("observed"), dict) else None
    status = audit.get("status") or "none"
    blockers = _string_list(audit.get("blockers"))
    return {
        "role": role,
        "ready": bool(audit.get("ready")),
        "status": status,
        "category": plan.get("category"),
        "tags": plan.get("tags"),
        "expected_limits": expected_limits,
        "observed_limits": observed_limits,
        "upload_limit_source": plan.get("upload_limit_source"),
        "download_limit_source": plan.get("download_limit_source"),
        "evidence_present": bool(audit.get("evidence_present")),
        "requires_injection_evidence": bool(expected_limits) and not bool(audit.get("evidence_present")),
        "requires_rate_limit_repair": status == "mismatch",
        "blockers": blockers,
        "requested_options": {
            "category": plan.get("category"),
            "tags": plan.get("tags"),
            "upload_limit": plan.get("upload_limit"),
            "download_limit": plan.get("download_limit"),
        },
    }


def _qbit_enforcement_next_step(blockers: list[str], pending_roles: list[str], mismatch_roles: list[str]) -> dict[str, Any]:
    if not blockers:
        return {"tool": "get_job_summary", "endpoint": None, "method": "GET", "request": None, "reason": "qbit_limits_enforced"}
    if pending_roles:
        return {"tool": "resume_job", "endpoint": "/v1/jobs/{job_id}/resume", "method": "POST", "request": {"dry_run": True}, "reason": "qbit_injection_evidence_missing", "roles": pending_roles}
    if mismatch_roles:
        return {"tool": "resume_job", "endpoint": "/v1/jobs/{job_id}/resume", "method": "POST", "request": {"dry_run": True}, "reason": "qbit_rate_limit_mismatch", "roles": mismatch_roles}
    return {"tool": "get_job_summary", "endpoint": None, "method": "GET", "request": None, "reason": "inspect_qbit_handoff", "blockers": blockers}


def _job_materials_handoff(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    result = summary_payload if isinstance(summary_payload, dict) else job.get("result") if isinstance(job.get("result"), dict) else {}
    agent_summary = _agent_summary(result) or (job.get("agent_summary") if isinstance(job.get("agent_summary"), dict) else {}) or {}
    metadata = agent_summary.get("metadata") if isinstance(agent_summary.get("metadata"), dict) else {}
    materials = agent_summary.get("materials") if isinstance(agent_summary.get("materials"), dict) else {}
    target_preflight = agent_summary.get("target_preflight") if isinstance(agent_summary.get("target_preflight"), dict) else {}
    if not metadata and not materials and not target_preflight:
        return None

    critical_missing = _string_list(materials.get("critical_missing"))
    upload_material_blockers = _string_list(materials.get("upload_material_blockers"))
    preflight_missing = _string_list(target_preflight.get("missing"))
    preflight_description_missing = _string_list(target_preflight.get("description_missing"))
    preflight_blockers = _string_list(target_preflight.get("blockers"))
    if not _materials_handoff_has_signal(metadata, materials, target_preflight, critical_missing, upload_material_blockers, preflight_missing, preflight_description_missing, preflight_blockers):
        return None
    blockers = list(dict.fromkeys(_string_list(metadata.get("missing")) + critical_missing + upload_material_blockers + preflight_missing + preflight_description_missing + preflight_blockers))
    recommended_inputs = _materials_handoff_recommended_inputs(request, metadata, materials, target_preflight, blockers)
    ready = _materials_handoff_ready(metadata, materials, target_preflight, blockers)
    material_plan = _materials_handoff_plan(request, metadata, materials, target_preflight, recommended_inputs)
    resume_handoff = _materials_handoff_resume_handoff(job, material_plan)
    return {
        "kind": "ptcli.materials_handoff",
        "ready": ready,
        "can_prepare_upload_payload": ready,
        "metadata": {
            "ready": metadata.get("ready"),
            "imdb_ready": metadata.get("imdb_ready"),
            "tmdb_ready": metadata.get("tmdb_ready"),
            "douban_ready": metadata.get("douban_ready"),
            "ptgen_description_ready": metadata.get("ptgen_description_ready"),
            "ptgen_description_length": metadata.get("ptgen_description_length"),
            "missing": _string_list(metadata.get("missing")),
            "external_links": metadata.get("external_links") if isinstance(metadata.get("external_links"), dict) else {},
        },
        "materials": {
            "ready_for_mteam_upload": materials.get("ready_for_mteam_upload"),
            "critical_ready": materials.get("critical_ready"),
            "critical_missing": critical_missing,
            "next_step": (materials.get("critical_path") or {}).get("next_step") if isinstance(materials.get("critical_path"), dict) else None,
            "media_info_requirement": materials.get("media_info_requirement"),
            "mediainfo_or_bdinfo_ready": materials.get("mediainfo_or_bdinfo_ready"),
            "screenshots_ready": materials.get("screenshots_ready"),
            "screenshot_count": materials.get("screenshot_count"),
            "image_host_urls": materials.get("image_host_urls") if isinstance(materials.get("image_host_urls"), dict) else {},
            "description_ready": materials.get("description_ready"),
            "upload_material_blockers": upload_material_blockers,
        },
        "target_preflight": {
            "ready": target_preflight.get("ready"),
            "payload_ready": target_preflight.get("payload_ready"),
            "materials_ready": target_preflight.get("materials_ready"),
            "metadata_ready": target_preflight.get("metadata_ready"),
            "assets_ready": target_preflight.get("assets_ready"),
            "description_ready": target_preflight.get("description_ready"),
            "materials_ready_required": target_preflight.get("materials_ready_required"),
            "missing": preflight_missing,
            "description_missing": preflight_description_missing,
            "blockers": preflight_blockers,
        },
        "material_plan": material_plan,
        "resume_request_template": _materials_handoff_resume_request_template(material_plan),
        "resume_handoff": resume_handoff,
        "recommended_inputs": recommended_inputs,
        "blockers": blockers,
        "next_actions": _materials_handoff_next_actions(ready, recommended_inputs, blockers),
    }


def _materials_handoff_has_signal(
    metadata: dict[str, Any],
    materials: dict[str, Any],
    target_preflight: dict[str, Any],
    critical_missing: list[str],
    upload_material_blockers: list[str],
    preflight_missing: list[str],
    preflight_description_missing: list[str],
    preflight_blockers: list[str],
) -> bool:
    metadata_signal = any(
        [
            bool(metadata.get("fields") if isinstance(metadata.get("fields"), dict) else {}),
            bool(metadata.get("missing")),
            bool(metadata.get("external_links") if isinstance(metadata.get("external_links"), dict) else {}),
            any(isinstance(metadata.get(key), bool) for key in ("ready", "imdb_ready", "tmdb_ready", "douban_ready")),
        ]
    )
    material_keys = ("ready_for_mteam_upload", "critical_ready", "mediainfo_or_bdinfo_ready", "screenshots_ready", "description_ready")
    preflight_keys = ("ready", "payload_ready", "materials_ready", "metadata_ready", "assets_ready", "description_ready")
    return any(
        [
            materials.get("present") is True,
            metadata_signal,
            any(isinstance(materials.get(key), bool) for key in material_keys),
            any(isinstance(target_preflight.get(key), bool) for key in preflight_keys),
            bool(critical_missing or upload_material_blockers or preflight_missing or preflight_description_missing or preflight_blockers),
        ]
    )


def _materials_handoff_ready(metadata: dict[str, Any], materials: dict[str, Any], target_preflight: dict[str, Any], blockers: list[str]) -> bool:
    readiness_values = [
        metadata.get("ready"),
        materials.get("ready_for_mteam_upload"),
        materials.get("critical_ready"),
        target_preflight.get("ready"),
        target_preflight.get("payload_ready"),
    ]
    if any(value is False for value in readiness_values):
        return False
    if blockers:
        return False
    return any(value is True for value in readiness_values)


def _materials_handoff_recommended_inputs(
    request: dict[str, Any],
    metadata: dict[str, Any],
    materials: dict[str, Any],
    target_preflight: dict[str, Any],
    blockers: list[str],
) -> list[dict[str, Any]]:
    inputs = _resume_recommended_inputs(request, metadata, materials, target_preflight)
    blocker_text = " ".join(blockers)
    if "image_host" in blocker_text or "screenshot_coverage" in blocker_text:
        inputs.append(
            _recommended_input(
                "image_host_file",
                ["image_host_file", "upload_screenshots", "image_host"],
                "hosted screenshot URLs are missing, partial, or stale",
                stage="materials-image-host",
                blocking_keys=["assets.image_host_uploads", "description.screenshot_coverage"],
                examples={"image_host_file": "/tmp/materials/image-host-uploads.json", "upload_screenshots": True, "image_host": "ptpimg"},
            )
        )
    return _dedupe_recommended_inputs(inputs)


def _recommended_input(
    key: str,
    accepted_keys: list[str],
    reason: str,
    *,
    stage: str,
    required: bool = False,
    blocking_keys: list[str] | None = None,
    examples: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": key,
        "accepted_keys": accepted_keys,
        "required": required,
        "reason": reason,
        "stage": stage,
        "resume_tool": "resume_job",
        "resume_endpoint_hint": "/v1/jobs/{job_id}/resume",
    }
    if blocking_keys:
        item["blocking_keys"] = blocking_keys
    if examples:
        item["examples"] = examples
    return item


def _dedupe_recommended_inputs(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in inputs:
        key = str(item.get("key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _materials_handoff_next_actions(ready: bool, recommended_inputs: list[dict[str, Any]], blockers: list[str]) -> list[str]:
    if ready:
        return []
    if recommended_inputs:
        keys = ", ".join(str(item.get("key")) for item in recommended_inputs if item.get("key"))
        return [f"Provide missing upload materials via resume_job overrides or rerun target package preparation with: {keys}."]
    if blockers:
        return ["Resolve materials_handoff.blockers, then rerun target package preparation or resume the blocked job."]
    return ["Inspect agent_summary.materials and target_preflight before attempting live upload."]


def _materials_handoff_plan(
    request: dict[str, Any],
    metadata: dict[str, Any],
    materials: dict[str, Any],
    target_preflight: dict[str, Any],
    recommended_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    by_key = {str(item.get("key")): item for item in recommended_inputs if isinstance(item, dict) and item.get("key")}
    items = [
        _material_plan_item(
            "source_content",
            "Source content path or qBittorrent save path",
            ready=bool(request.get("path") or request.get("content_path") or request.get("save_path")),
            recommended_input=by_key.get("path_or_save_path"),
            resume_overrides={"path": "/downloads/Example.Release", "save_path": "/downloads"},
        ),
        _material_plan_item(
            "metadata_ids",
            "IMDb/TMDb/Douban identifiers and metadata",
            ready=metadata.get("ready") is True or all(metadata.get(key) is True for key in ("imdb_ready", "tmdb_ready", "douban_ready")),
            recommended_input=by_key.get("metadata_file"),
            resume_overrides={"metadata_file": "/tmp/materials/metadata.json", "imdb_id": "tt1234567", "tmdb_id": "999", "tmdb_type": "movie", "douban_id": "1291546", "fetch_ptgen": True},
        ),
        _material_plan_item(
            "ptgen_description",
            "PTGen/Douban description for target upload",
            ready=metadata.get("ptgen_description_ready") is True and target_preflight.get("description_ready") is not False,
            recommended_input=by_key.get("ptgen_description_file"),
            resume_overrides={"ptgen_description_file": "/tmp/materials/ptgen-description.txt", "fetch_ptgen": True, "douban_url": "https://movie.douban.com/subject/1291546/"},
        ),
        _material_plan_item(
            "mediainfo_bdinfo",
            "MediaInfo or BDInfo text",
            ready=materials.get("mediainfo_or_bdinfo_ready") is not False,
            recommended_input=by_key.get("mediainfo_or_bdinfo"),
            resume_overrides={"mediainfo_file": "/tmp/materials/MI_FULL_00.txt", "bdinfo_file": "/tmp/materials/BDINFO.txt", "generate_mediainfo": True, "generate_bdinfo": True},
        ),
        _material_plan_item(
            "screenshots",
            "Video screenshots",
            ready=materials.get("screenshots_ready") is not False,
            recommended_input=by_key.get("screenshot_files"),
            resume_overrides={"screenshot_files": ["/tmp/materials/screen-01.png", "/tmp/materials/screen-02.png"], "generate_screenshots": True, "screenshot_count": "4"},
        ),
        _material_plan_item(
            "image_host",
            "Hosted screenshot URLs / image-host upload evidence",
            ready=by_key.get("image_host_file") is None,
            recommended_input=by_key.get("image_host_file"),
            resume_overrides={"image_host_file": "/tmp/materials/image-host-uploads.json", "upload_screenshots": True, "image_host": "ptpimg"},
        ),
        _material_plan_item(
            "target_package",
            "Reusable target upload package directory",
            ready=target_preflight.get("payload_ready") is not False,
            recommended_input=by_key.get("package_dir"),
            resume_overrides={"package_dir": "/tmp/target/U2-60635-to-MTEAM"},
        ),
    ]
    missing = [item["key"] for item in items if item["ready"] is False]
    next_item = next((item for item in items if item["ready"] is False), None)
    return {
        "kind": "ptcli.material_plan",
        "ready": not missing,
        "missing": missing,
        "next_item": next_item,
        "items": items,
    }


def _material_plan_item(
    key: str,
    label: str,
    *,
    ready: bool,
    recommended_input: dict[str, Any] | None,
    resume_overrides: dict[str, Any],
) -> dict[str, Any]:
    accepted_keys = _string_list(recommended_input.get("accepted_keys")) if isinstance(recommended_input, dict) else []
    return {
        "key": key,
        "label": label,
        "ready": ready,
        "stage": recommended_input.get("stage") if isinstance(recommended_input, dict) else None,
        "recommended_input_key": recommended_input.get("key") if isinstance(recommended_input, dict) else None,
        "accepted_keys": accepted_keys,
        "blocking_keys": _string_list(recommended_input.get("blocking_keys")) if isinstance(recommended_input, dict) else [],
        "next_step": "resume_job" if ready is False else None,
        "resume_overrides": {key: value for key, value in resume_overrides.items() if not accepted_keys or key in accepted_keys},
    }


def _materials_handoff_resume_request_template(material_plan: dict[str, Any]) -> dict[str, Any]:
    next_item = material_plan.get("next_item") if isinstance(material_plan.get("next_item"), dict) else None
    if not next_item:
        return {"dry_run": True}
    return {
        "dry_run": True,
        **(next_item.get("resume_overrides") if isinstance(next_item.get("resume_overrides"), dict) else {}),
    }


def _materials_handoff_resume_handoff(job: dict[str, Any], material_plan: dict[str, Any]) -> dict[str, Any]:
    job_id = job.get("job_id")
    endpoint = f"/v1/jobs/{job_id}/resume" if job_id else "/v1/jobs/{job_id}/resume"
    missing_items = [item for item in material_plan.get("items", []) if isinstance(item, dict) and item.get("ready") is False]
    combined_overrides: dict[str, Any] = {}
    staged_requests: list[dict[str, Any]] = []
    for item in missing_items:
        overrides = item.get("resume_overrides") if isinstance(item.get("resume_overrides"), dict) else {}
        combined_overrides.update(overrides)
        staged_requests.append(
            {
                "key": item.get("key"),
                "label": item.get("label"),
                "stage": item.get("stage"),
                "recommended_input_key": item.get("recommended_input_key"),
                "accepted_keys": _string_list(item.get("accepted_keys")),
                "blocking_keys": _string_list(item.get("blocking_keys")),
                "dry_run_request": _materials_handoff_resume_request(job_id, overrides, dry_run=True),
                "execute_request": _materials_handoff_resume_request(job_id, overrides, dry_run=False),
            }
        )
    dry_run_request = _materials_handoff_resume_request(job_id, combined_overrides, dry_run=True)
    execute_request = _materials_handoff_resume_request(job_id, combined_overrides, dry_run=False)
    next_item = material_plan.get("next_item") if isinstance(material_plan.get("next_item"), dict) else None
    return {
        "kind": "ptcli.materials_resume_handoff",
        "ready": bool(material_plan.get("ready")),
        "resume_recommended": bool(missing_items),
        "recommended_tool": "resume_job" if missing_items else None,
        "recommended_endpoint": endpoint if missing_items else None,
        "method": "POST" if missing_items else None,
        "next_item": next_item,
        "missing": _string_list(material_plan.get("missing")),
        "accepted_override_keys": sorted({key for item in missing_items for key in _string_list(item.get("accepted_keys"))}),
        "dry_run_request": dry_run_request if missing_items else None,
        "execute_request": execute_request if missing_items else None,
        "recommended_request": dry_run_request if missing_items else None,
        "staged_requests": staged_requests,
        "continue_when": "resume preview covers material_resolution.unresolved_recommended_inputs, then rerun without dry_run and inspect the resumed job summary." if missing_items else None,
        "stop_when": "resume_context.ignored_overrides is non-empty for required material keys or material_resolution.unresolved_recommended_inputs remains non-empty." if missing_items else None,
    }


def _materials_handoff_resume_request(job_id: Any, overrides: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    request: dict[str, Any] = {}
    if job_id:
        request["job_id"] = job_id
    request.update(overrides)
    if dry_run:
        request["dry_run"] = True
    return request


def _job_target_upload_handoff(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    result = summary_payload if isinstance(summary_payload, dict) else job.get("result") if isinstance(job.get("result"), dict) else {}
    agent_summary = _agent_summary(result) or (job.get("agent_summary") if isinstance(job.get("agent_summary"), dict) else {}) or {}
    target_preflight = agent_summary.get("target_preflight") if isinstance(agent_summary.get("target_preflight"), dict) else {}
    target_upload_diagnostics = _nested_dict(result, "target_upload_diagnostics")
    if not _target_upload_handoff_has_signal(target_preflight, target_upload_diagnostics):
        return None

    preflight = target_upload_diagnostics.get("preflight") if isinstance(target_upload_diagnostics.get("preflight"), dict) else {}
    completion = target_upload_diagnostics.get("completion") if isinstance(target_upload_diagnostics.get("completion"), dict) else {}
    payload_review = target_upload_diagnostics.get("payload_review") if isinstance(target_upload_diagnostics.get("payload_review"), dict) else {}
    materials_handoff = _job_materials_handoff(job, summary_payload)
    duplicate_check = _job_duplicate_check(job)
    duplicate_exists = duplicate_check.get("exists") is True
    missing_confirmations = _missing_live_confirmations(request)
    policy_coverage = _job_policy_coverage(job)
    policy_ready = policy_coverage.get("ready") if isinstance(policy_coverage, dict) and isinstance(policy_coverage.get("ready"), bool) else None
    preflight_ready = _first_bool(target_preflight.get("ready"), preflight.get("ready"), preflight.get("status") == "ready" if preflight else None)
    payload_ready = _first_bool(target_preflight.get("payload_ready"), preflight.get("payload_ready"))
    materials_ready = _first_bool(target_preflight.get("materials_ready"), materials_handoff.get("ready") if isinstance(materials_handoff, dict) else None)
    uploaded_seeding_ready = _first_bool(target_upload_diagnostics.get("ready_for_uploaded_seeding"), completion.get("ready_for_uploaded_seeding"))
    upload_blockers = list(
        dict.fromkeys(
            _string_list(target_preflight.get("blockers"))
            + _string_list(target_preflight.get("missing"))
            + _string_list(target_preflight.get("description_missing"))
            + _string_list(preflight.get("blockers"))
            + _string_list(preflight.get("missing"))
            + _string_list(preflight.get("description_missing"))
            + _string_list(payload_review.get("recovery_missing"))
        )
    )
    gate_blockers = _target_upload_gate_blockers(duplicate_exists, missing_confirmations, policy_ready, materials_handoff)
    can_upload_now = bool(preflight_ready and payload_ready and not upload_blockers and not gate_blockers)
    action = _target_upload_handoff_action(uploaded_seeding_ready, duplicate_exists, missing_confirmations, policy_ready, materials_handoff, can_upload_now, upload_blockers)
    resume_plan = _job_resume_plan(job)
    next_step = _target_upload_handoff_next_step(job, action, resume_plan, missing_confirmations, materials_handoff, upload_blockers)
    return {
        "kind": "ptcli.target_upload_handoff",
        "ready": bool(uploaded_seeding_ready),
        "action": action,
        "ready_for_live_upload": can_upload_now,
        "uploaded_seeding_ready": bool(uploaded_seeding_ready),
        "preflight": {
            "ready": preflight_ready,
            "payload_ready": payload_ready,
            "materials_ready": materials_ready,
            "metadata_ready": _first_bool(target_preflight.get("metadata_ready"), preflight.get("metadata_ready")),
            "assets_ready": _first_bool(target_preflight.get("assets_ready"), preflight.get("assets_ready")),
            "description_ready": _first_bool(target_preflight.get("description_ready"), preflight.get("description_ready")),
            "materials_ready_required": _first_bool(target_preflight.get("materials_ready_required"), preflight.get("materials_ready_required")),
            "missing": list(dict.fromkeys(_string_list(target_preflight.get("missing")) + _string_list(preflight.get("missing")))),
            "description_missing": list(dict.fromkeys(_string_list(target_preflight.get("description_missing")) + _string_list(preflight.get("description_missing")))),
            "blockers": list(dict.fromkeys(_string_list(target_preflight.get("blockers")) + _string_list(preflight.get("blockers")))),
        },
        "payload_review": {
            "present": bool(payload_review),
            "recovery_missing": _string_list(payload_review.get("recovery_missing")),
            "next_actions": _string_list(payload_review.get("next_actions")),
        },
        "duplicate_check": duplicate_check,
        "duplicate_clear": duplicate_check.get("searched") is True and not duplicate_exists,
        "missing_confirmations": missing_confirmations,
        "policy_coverage_ready": policy_ready,
        "materials_handoff": materials_handoff,
        "resume_plan": resume_plan,
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "summary_file": job.get("summary_file") or _job_summary_file(job),
        "blockers": list(dict.fromkeys(gate_blockers + upload_blockers)),
        "next_actions": _target_upload_handoff_next_actions(action, missing_confirmations, materials_handoff, upload_blockers),
    }


def _target_upload_handoff_has_signal(target_preflight: dict[str, Any], target_upload_diagnostics: dict[str, Any]) -> bool:
    if target_upload_diagnostics:
        return True
    keys = ("ready", "payload_ready", "materials_ready", "metadata_ready", "assets_ready", "description_ready", "materials_ready_required")
    return any(isinstance(target_preflight.get(key), bool) for key in keys) or any(_string_list(target_preflight.get(key)) for key in ("missing", "description_missing", "blockers"))


def _target_upload_gate_blockers(duplicate_exists: bool, missing_confirmations: list[str], policy_ready: bool | None, materials_handoff: dict[str, Any] | None) -> list[str]:
    blockers: list[str] = []
    if duplicate_exists:
        blockers.append("duplicate_check.exists")
    blockers.extend(f"missing_confirmation.{item}" for item in missing_confirmations)
    if policy_ready is False:
        blockers.append("policy_coverage.incomplete")
    if isinstance(materials_handoff, dict) and materials_handoff.get("ready") is False:
        blockers.append("materials_handoff.not_ready")
    return blockers


def _target_upload_handoff_action(
    uploaded_seeding_ready: bool | None,
    duplicate_exists: bool,
    missing_confirmations: list[str],
    policy_ready: bool | None,
    materials_handoff: dict[str, Any] | None,
    can_upload_now: bool,
    upload_blockers: list[str],
) -> str:
    if uploaded_seeding_ready is True:
        return "done"
    if duplicate_exists:
        return "stop_duplicate"
    if missing_confirmations:
        return "collect_confirmations"
    if policy_ready is False:
        return "configure_policy"
    if isinstance(materials_handoff, dict) and materials_handoff.get("ready") is False:
        return "prepare_materials"
    if upload_blockers:
        return "repair_target_payload"
    if can_upload_now:
        return "ready_for_upload"
    return "inspect"


def _target_upload_handoff_next_actions(action: str, missing_confirmations: list[str], materials_handoff: dict[str, Any] | None, upload_blockers: list[str]) -> list[str]:
    if action == "done":
        return []
    if action == "stop_duplicate":
        return ["Do not upload. Inspect duplicate_check.dupes or choose another target tracker."]
    if action == "collect_confirmations":
        return [f"Collect explicit confirmation for: {', '.join(missing_confirmations)}."]
    if action == "configure_policy":
        return ["Complete site policy coverage before attempting live target upload."]
    if action == "prepare_materials" and isinstance(materials_handoff, dict):
        return _string_list(materials_handoff.get("next_actions")) or ["Provide missing materials, then resume target package preparation."]
    if action == "repair_target_payload":
        return ["Repair target upload payload/preflight blockers, then resume target-upload or target package preparation."]
    if action == "ready_for_upload":
        return ["Target upload preflight is ready; execute only with explicit accept_rules and confirm_upload already captured."]
    if upload_blockers:
        return ["Inspect target_upload_handoff.blockers before attempting live upload."]
    return []


def _target_upload_handoff_next_step(
    job: dict[str, Any],
    action: str,
    resume_plan: dict[str, Any],
    missing_confirmations: list[str],
    materials_handoff: dict[str, Any] | None,
    upload_blockers: list[str],
) -> dict[str, Any]:
    job_id = job.get("job_id")
    resume_endpoint = resume_plan.get("endpoint") if isinstance(resume_plan.get("endpoint"), str) else f"/v1/jobs/{job_id}/resume" if job_id else None
    resume_allowed = bool(resume_plan.get("allowed"))
    base = {
        "action": action,
        "resume_allowed": resume_allowed,
        "resume_endpoint": resume_endpoint,
        "resume_subcommand": resume_plan.get("subcommand"),
    }
    if action == "done":
        return {**base, "tool": "get_job_summary", "endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None, "method": "GET", "request": None, "reason": "uploaded_target_seeding_ready"}
    if action == "stop_duplicate":
        return {**base, "tool": None, "endpoint": None, "method": None, "request": None, "reason": "target_duplicate_exists"}
    if action == "configure_policy":
        return {**base, "tool": "site_policies", "endpoint": "/v1/site-policies", "method": "POST", "request": _target_upload_policy_request(job), "reason": "policy_coverage_incomplete"}
    if action == "collect_confirmations":
        return {
            **base,
            "tool": "resume_job" if resume_allowed else None,
            "endpoint": resume_endpoint if resume_allowed else None,
            "method": "POST" if resume_allowed else None,
            "request": _target_upload_confirmation_overrides(missing_confirmations) if resume_allowed else None,
            "reason": "missing_required_confirmation",
        }
    if action == "prepare_materials":
        return {
            **base,
            "tool": "resume_job" if resume_allowed else None,
            "endpoint": resume_endpoint if resume_allowed else None,
            "method": "POST" if resume_allowed else None,
            "request": {},
            "reason": "materials_not_ready",
            "recommended_inputs": materials_handoff.get("recommended_inputs") if isinstance(materials_handoff, dict) else [],
        }
    if action == "repair_target_payload":
        return {
            **base,
            "tool": "resume_job" if resume_allowed else None,
            "endpoint": resume_endpoint if resume_allowed else None,
            "method": "POST" if resume_allowed else None,
            "request": {},
            "reason": "target_payload_or_preflight_blocked",
            "upload_blockers": upload_blockers,
        }
    if action == "ready_for_upload":
        return {
            **base,
            "tool": "resume_job" if resume_allowed else None,
            "endpoint": resume_endpoint if resume_allowed else None,
            "method": "POST" if resume_allowed else None,
            "request": {},
            "reason": "target_upload_preflight_ready",
        }
    return {**base, "tool": "get_job_summary", "endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None, "method": "GET", "request": None, "reason": "inspect_target_upload_state"}


def _target_upload_confirmation_overrides(missing_confirmations: list[str]) -> dict[str, bool]:
    overrides: dict[str, bool] = {}
    if "accept_rules=true" in missing_confirmations:
        overrides["accept_rules"] = True
    if "confirm_upload=true" in missing_confirmations:
        overrides["confirm_upload"] = True
    return overrides


def _target_upload_policy_request(job: dict[str, Any]) -> dict[str, Any]:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    policy_request: dict[str, Any] = {}
    source_reference = _job_source_reference(job)
    if isinstance(source_reference, dict) and source_reference.get("tracker"):
        policy_request["source_tracker"] = source_reference.get("tracker")
    target_trackers = request.get("target_trackers")
    if target_trackers:
        policy_request["target"] = target_trackers
    if request.get("accept_rules") is True:
        policy_request["accept_rules"] = True
    return policy_request


def _job_closure_handoff(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    result = summary_payload if isinstance(summary_payload, dict) else job.get("result") if isinstance(job.get("result"), dict) else {}
    status = str(job.get("status") or "unknown")
    job_id = job.get("job_id")
    duplicate_check = _job_duplicate_check(job)
    target_upload_handoff = _job_target_upload_handoff(job, summary_payload)
    manual_retorrent_handoff = _job_manual_retorrent_handoff(job, summary_payload)
    qbit_handoff = _job_qbit_handoff(job, summary_payload)
    closure_status = _nested_dict(result, "closure_status")
    closure = _nested_dict(result, "closure")
    evidence = _nested_dict(result, "evidence")
    agent_summary = _agent_summary(result) or (job.get("agent_summary") if isinstance(job.get("agent_summary"), dict) else {}) or {}
    qbit_summary = agent_summary.get("qbit") if isinstance(agent_summary.get("qbit"), dict) else {}
    source = closure_status.get("source") if isinstance(closure_status.get("source"), dict) else qbit_summary.get("source") if isinstance(qbit_summary.get("source"), dict) else {}
    target = closure_status.get("target") if isinstance(closure_status.get("target"), dict) else qbit_summary.get("target") if isinstance(qbit_summary.get("target"), dict) else {}
    blockers = list(dict.fromkeys(_string_list(job.get("blockers")) + _string_list(result.get("blockers")) + _closure_handoff_nested_blockers(target_upload_handoff, manual_retorrent_handoff, qbit_handoff)))
    complete = _closure_handoff_complete(status, result, closure, target_upload_handoff)
    action = _closure_handoff_action(status, complete, duplicate_check, target_upload_handoff, manual_retorrent_handoff, qbit_handoff, blockers)
    next_step = _closure_handoff_next_step(job, action, target_upload_handoff, manual_retorrent_handoff)
    source_ready = _first_bool(source.get("ready"), source.get("wait_complete"), closure.get("source_ready")) if source else closure.get("source_ready")
    target_ready = _first_bool(
        target.get("ready"),
        target.get("uploaded_wait_evidence"),
        target.get("injection_verified"),
        target_upload_handoff.get("uploaded_seeding_ready") if isinstance(target_upload_handoff, dict) else None,
        closure.get("target_ready"),
    )
    closure_checklist = _closure_checklist(source_ready, target_ready, complete, duplicate_check, manual_retorrent_handoff, target_upload_handoff, qbit_handoff)
    return {
        "kind": "ptcli.closure_handoff",
        "ready": complete,
        "complete": complete,
        "closure_checklist": closure_checklist,
        "action": action,
        "status": status,
        "source_reference": _job_source_reference(job),
        "target_trackers": request.get("target_trackers"),
        "source": {
            "ready": source_ready,
            "torrent_hash": source.get("torrent_hash"),
            "content_path": source.get("content_path"),
            "injection_verified": source.get("injection_verified"),
            "wait_complete": source.get("wait_complete"),
        },
        "target": {
            "ready": target_ready,
            "uploaded_torrent_hash": target.get("uploaded_torrent_hash"),
            "injected_torrent_hash": target.get("injected_torrent_hash"),
            "injection_visible_in_client": target.get("injection_visible_in_client"),
            "injection_verified": target.get("injection_verified"),
            "uploaded_wait_evidence": target.get("uploaded_wait_evidence"),
            "uploaded_seeding_ready": target_upload_handoff.get("uploaded_seeding_ready") if isinstance(target_upload_handoff, dict) else None,
        },
        "duplicate_check": duplicate_check,
        "target_upload_handoff": target_upload_handoff,
        "manual_retorrent_handoff": manual_retorrent_handoff,
        "qbit_handoff": qbit_handoff,
        "evidence": {
            "present": bool(evidence),
            "source_torrent": evidence.get("source_torrent") if isinstance(evidence.get("source_torrent"), dict) else None,
            "target_torrent": evidence.get("target_torrent") if isinstance(evidence.get("target_torrent"), dict) else None,
            "uploaded_torrent": evidence.get("uploaded_torrent") if isinstance(evidence.get("uploaded_torrent"), dict) else None,
            "source_torrent_path": evidence.get("source_torrent_path"),
            "target_torrent_file": evidence.get("target_torrent_file"),
            "uploaded_torrent_path": evidence.get("uploaded_torrent_path"),
        },
        "summary_file": job.get("summary_file") or _job_summary_file(job),
        "status_endpoint": f"/v1/jobs/{job_id}" if job_id else None,
        "summary_endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None,
        "resume_endpoint": f"/v1/jobs/{job_id}/resume" if job_id else None,
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "blockers": blockers,
        "next_actions": _closure_handoff_next_actions(action, blockers, next_step),
    }


def _job_closure_summary(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    closure_handoff = _job_closure_handoff(job, summary_payload)
    materials_handoff = _job_materials_handoff(job, summary_payload)
    policy_handoff = _job_policy_handoff(job)
    qbit_handoff = _job_qbit_handoff(job, summary_payload)
    duplicate_check = closure_handoff.get("duplicate_check") if isinstance(closure_handoff.get("duplicate_check"), dict) else {}
    source = closure_handoff.get("source") if isinstance(closure_handoff.get("source"), dict) else {}
    target = closure_handoff.get("target") if isinstance(closure_handoff.get("target"), dict) else {}
    evidence = closure_handoff.get("evidence") if isinstance(closure_handoff.get("evidence"), dict) else {}
    checklist = closure_handoff.get("closure_checklist") if isinstance(closure_handoff.get("closure_checklist"), dict) else {}
    gates = {
        "source_ready": source.get("ready") is True,
        "target_ready": target.get("ready") is True,
        "duplicate_clear": duplicate_check.get("searched") is True and duplicate_check.get("exists") is False,
        "policy_ready": policy_handoff.get("ready") is not False if isinstance(policy_handoff, dict) else None,
        "materials_ready": materials_handoff.get("ready") is True if isinstance(materials_handoff, dict) else None,
        "qbit_ready": qbit_handoff.get("ready") is True if isinstance(qbit_handoff, dict) else None,
        "uploaded_seeding_ready": target.get("uploaded_seeding_ready") is True or target.get("uploaded_wait_evidence") is True,
    }
    blockers = list(dict.fromkeys(_string_list(closure_handoff.get("blockers")) + _string_list(checklist.get("blockers"))))
    next_step = closure_handoff.get("next_step") if isinstance(closure_handoff.get("next_step"), dict) else {}
    complete = closure_handoff.get("complete") is True
    return {
        "kind": "ptcli.closure_summary",
        "complete": complete,
        "status": closure_handoff.get("status"),
        "action": closure_handoff.get("action"),
        "ready_for_report": complete and not blockers,
        "source_reference": closure_handoff.get("source_reference"),
        "target_trackers": closure_handoff.get("target_trackers"),
        "gates": gates,
        "source": {
            "ready": source.get("ready"),
            "torrent_hash": source.get("torrent_hash"),
            "content_path": source.get("content_path"),
            "wait_complete": source.get("wait_complete"),
        },
        "target": {
            "ready": target.get("ready"),
            "uploaded_torrent_hash": target.get("uploaded_torrent_hash"),
            "injected_torrent_hash": target.get("injected_torrent_hash"),
            "uploaded_seeding_ready": target.get("uploaded_seeding_ready"),
            "uploaded_wait_evidence": target.get("uploaded_wait_evidence"),
        },
        "duplicate_check": {
            "searched": duplicate_check.get("searched"),
            "exists": duplicate_check.get("exists"),
            "status": duplicate_check.get("status"),
            "count": duplicate_check.get("count"),
        },
        "materials": {
            "ready": materials_handoff.get("ready") if isinstance(materials_handoff, dict) else None,
            "blockers": _string_list(materials_handoff.get("blockers")) if isinstance(materials_handoff, dict) else [],
        },
        "policy": {
            "ready": policy_handoff.get("ready") if isinstance(policy_handoff, dict) else None,
            "blockers": _string_list(policy_handoff.get("blockers")) if isinstance(policy_handoff, dict) else [],
        },
        "qbit": {
            "ready": qbit_handoff.get("ready") if isinstance(qbit_handoff, dict) else None,
            "blockers": _string_list(qbit_handoff.get("blockers")) if isinstance(qbit_handoff, dict) else [],
        },
        "evidence": {
            "present": evidence.get("present") is True,
            "source_torrent_path": evidence.get("source_torrent_path"),
            "target_torrent_file": evidence.get("target_torrent_file"),
            "uploaded_torrent_path": evidence.get("uploaded_torrent_path"),
        },
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "blockers": blockers,
        "next_actions": _closure_summary_next_actions(complete, blockers, next_step),
    }


def _closure_summary_next_actions(complete: bool, blockers: list[str], next_step: dict[str, Any]) -> list[str]:
    if complete and not blockers:
        return []
    if next_step.get("tool"):
        return [f"Use closure_summary.next_step with {next_step['tool']} after reviewing blockers."]
    if blockers:
        return ["Resolve closure_summary.blockers before considering the retorrent complete."]
    return ["Inspect closure_handoff for detailed source, target, qBittorrent, and material evidence."]


def _closure_checklist(
    source_ready: Any,
    target_ready: Any,
    complete: bool,
    duplicate_check: dict[str, Any],
    manual_retorrent_handoff: dict[str, Any] | None,
    target_upload_handoff: dict[str, Any] | None,
    qbit_handoff: dict[str, Any] | None,
) -> dict[str, Any]:
    manual_checklist = manual_retorrent_handoff.get("live_checklist") if isinstance(manual_retorrent_handoff, dict) and isinstance(manual_retorrent_handoff.get("live_checklist"), dict) else None
    items = [
        _checklist_item("source_download_or_match", source_ready is True, required=True, blocker="source.not_ready"),
        _checklist_item("target_uploaded_and_seeding", target_ready is True, required=True, blocker="target.uploaded_seeding_not_ready"),
        _checklist_item("target_duplicate_clear", duplicate_check.get("searched") is True and duplicate_check.get("exists") is False, required=True, blocker="duplicate_check.not_clear"),
        _checklist_item("manual_live_gates", manual_checklist.get("ready") is True if isinstance(manual_checklist, dict) else None, required=True, blocker="manual_retorrent_handoff.live_checklist_not_ready"),
        _checklist_item("target_upload_handoff", target_upload_handoff.get("uploaded_seeding_ready") is True if isinstance(target_upload_handoff, dict) else None, required=True, blocker="target_upload_handoff.not_ready"),
        _checklist_item("qbit_handoff", qbit_handoff.get("ready") is True if isinstance(qbit_handoff, dict) else None, required=False, blocker="qbit_handoff.not_ready"),
    ]
    blockers = _checklist_blockers(items)
    return {
        "kind": "ptcli.closure_checklist",
        "ready": complete and not blockers,
        "items": items,
        "blockers": blockers,
        "next_actions": [] if complete and not blockers else ["Follow closure_handoff.next_step after reviewing closure_checklist.blockers."],
    }


def _closure_handoff_nested_blockers(*handoffs: dict[str, Any] | None) -> list[str]:
    blockers: list[str] = []
    for handoff in handoffs:
        if isinstance(handoff, dict):
            blockers.extend(_string_list(handoff.get("blockers")))
    return blockers


def _closure_handoff_complete(status: str, result: dict[str, Any], closure: dict[str, Any], target_upload_handoff: dict[str, Any] | None) -> bool:
    if closure.get("complete") is True:
        return True
    if result.get("status") == "complete" or result.get("ok") is True:
        if isinstance(target_upload_handoff, dict):
            return bool(target_upload_handoff.get("uploaded_seeding_ready") or target_upload_handoff.get("ready"))
        return status == "complete"
    return status == "complete" and (not isinstance(target_upload_handoff, dict) or bool(target_upload_handoff.get("uploaded_seeding_ready") or target_upload_handoff.get("ready")))


def _closure_handoff_action(
    status: str,
    complete: bool,
    duplicate_check: dict[str, Any],
    target_upload_handoff: dict[str, Any] | None,
    manual_retorrent_handoff: dict[str, Any] | None,
    qbit_handoff: dict[str, Any] | None,
    blockers: list[str],
) -> str:
    if complete:
        return "done"
    if status in {"queued", "running"}:
        return "wait"
    if duplicate_check.get("exists") is True:
        return "stop_duplicate"
    if isinstance(target_upload_handoff, dict) and target_upload_handoff.get("action") not in {None, "inspect", "done"}:
        return str(target_upload_handoff.get("action"))
    if isinstance(manual_retorrent_handoff, dict) and manual_retorrent_handoff.get("action") not in {None, "inspect", "done"}:
        return str(manual_retorrent_handoff.get("action"))
    if isinstance(qbit_handoff, dict) and qbit_handoff.get("ready") is False:
        return "repair_qbit"
    if blockers:
        return "resolve_blockers"
    return "inspect"


def _closure_handoff_next_step(
    job: dict[str, Any],
    action: str,
    target_upload_handoff: dict[str, Any] | None,
    manual_retorrent_handoff: dict[str, Any] | None,
) -> dict[str, Any]:
    job_id = job.get("job_id")
    if action == "done":
        return {"tool": "get_job_summary", "endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None, "method": "GET", "request": None, "reason": "retorrent_closure_complete"}
    if action == "wait":
        return {"tool": "get_job_status", "endpoint": f"/v1/jobs/{job_id}" if job_id else None, "method": "GET", "request": None, "reason": "job_still_running"}
    if action == "stop_duplicate":
        return {"tool": None, "endpoint": None, "method": None, "request": None, "reason": "target_duplicate_exists"}
    if isinstance(target_upload_handoff, dict) and isinstance(target_upload_handoff.get("next_step"), dict) and action == target_upload_handoff.get("action"):
        return target_upload_handoff["next_step"]
    if isinstance(manual_retorrent_handoff, dict) and action == manual_retorrent_handoff.get("action") and manual_retorrent_handoff.get("resume_endpoint"):
        return {
            "tool": "resume_job",
            "endpoint": manual_retorrent_handoff.get("resume_endpoint"),
            "method": "POST",
            "request": {},
            "reason": manual_retorrent_handoff.get("reason") or "manual_retorrent_resume_recommended",
        }
    if action == "repair_qbit":
        return {"tool": "resume_job", "endpoint": f"/v1/jobs/{job_id}/resume" if job_id else None, "method": "POST", "request": {}, "reason": "qbit_evidence_or_limits_not_ready"}
    return {"tool": "get_job_summary", "endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None, "method": "GET", "request": None, "reason": "inspect_closure_state"}


def _closure_handoff_next_actions(action: str, blockers: list[str], next_step: dict[str, Any]) -> list[str]:
    if action == "done":
        return []
    if action == "stop_duplicate":
        return ["Do not upload. Inspect closure_handoff.duplicate_check.dupes or choose another target tracker."]
    if next_step.get("tool"):
        return [f"Call {next_step['tool']} via closure_handoff.next_step after reviewing blockers and site rules."]
    if blockers:
        return ["Resolve closure_handoff.blockers before attempting live retorrent closure."]
    return ["Inspect closure_handoff.source, closure_handoff.target, and closure_handoff.evidence before taking live action."]


def _first_bool(*values: Any) -> bool | None:
    for value in values:
        if isinstance(value, bool):
            return value
    return None


def _qbit_limit_source_result(payloads: list[dict[str, Any]]) -> dict[str, Any] | None:
    for payload in payloads:
        stage = _stage_from_payload(payload, "inject-source")
        result = stage.get("result") if isinstance(stage, dict) else None
        if isinstance(result, dict):
            return result
    return None


def _qbit_limit_uploaded_result(payloads: list[dict[str, Any]]) -> dict[str, Any] | None:
    for payload in payloads:
        stage = _stage_from_payload(payload, "target-upload")
        result = stage.get("result") if isinstance(stage, dict) else None
        if isinstance(result, dict) and isinstance(result.get("injected_torrent"), dict):
            return result["injected_torrent"]
        direct = _nested_value(payload, "injected_torrent")
        if isinstance(direct, dict):
            return direct
    return None


def _stage_from_payload(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    stages = payload.get("stages")
    if not isinstance(stages, list):
        pipeline = payload.get("pipeline")
        stages = pipeline.get("stages") if isinstance(pipeline, dict) else None
    if not isinstance(stages, list):
        return None
    for stage in stages:
        if isinstance(stage, dict) and stage.get("stage") == name:
            return stage
    return None


def _qbit_limit_role_audit(role: str, plan: Any, result: dict[str, Any] | None) -> dict[str, Any]:
    plan = plan if isinstance(plan, dict) else {}
    expected = {
        key: value
        for key, value in {
            "upload_limit": plan.get("upload_limit"),
            "download_limit": plan.get("download_limit"),
        }.items()
        if value is not None
    }
    if not expected:
        return {
            "role": role,
            "status": "no_limits_requested",
            "ready": True,
            "expected": {},
            "observed": None,
            "evidence_present": isinstance(result, dict),
            "blockers": [],
        }
    if not isinstance(result, dict):
        blockers = [f"{role}.qbit_limit_evidence_missing"]
        return {
            "role": role,
            "status": "pending",
            "ready": False,
            "expected": expected,
            "observed": None,
            "evidence_present": False,
            "blockers": blockers,
        }

    rate_limits = result.get("rate_limits") if isinstance(result.get("rate_limits"), dict) else {}
    requested = rate_limits.get("requested") if isinstance(rate_limits.get("requested"), dict) else {}
    observed = {
        "upload_limit": result.get("upload_limit", requested.get("upload_limit")),
        "download_limit": result.get("download_limit", requested.get("download_limit")),
        "rate_limits": rate_limits or None,
    }
    mismatches = [key for key, value in expected.items() if observed.get(key) != value]
    missing_calls = _qbit_limit_missing_calls(expected, rate_limits)
    blockers = [f"{role}.{key}_mismatch" for key in mismatches] + [f"{role}.{item}" for item in missing_calls]
    applied = bool(rate_limits.get("applied")) and not blockers
    return {
        "role": role,
        "status": "applied" if applied else "mismatch",
        "ready": applied,
        "expected": expected,
        "observed": observed,
        "evidence_present": True,
        "blockers": blockers,
    }


def _qbit_limit_missing_calls(expected: dict[str, Any], rate_limits: dict[str, Any]) -> list[str]:
    calls = rate_limits.get("calls") if isinstance(rate_limits.get("calls"), list) else []
    methods = {str(call.get("method")) for call in calls if isinstance(call, dict)}
    missing: list[str] = []
    if "upload_limit" in expected and "torrents_set_upload_limit" not in methods:
        missing.append("upload_limit_call_missing")
    if "download_limit" in expected and "torrents_set_download_limit" not in methods:
        missing.append("download_limit_call_missing")
    return missing


def _qbit_limit_audit_next_actions(blockers: list[str]) -> list[str]:
    if not blockers:
        return []
    return ["Resume or rerun the qBittorrent injection step so configured per-site rate limits are applied and captured in job evidence."]


def _qbit_plan_value_source(key: str, sources: dict[str, Any], overrides: dict[str, Any]) -> str | None:
    if key in overrides:
        return "request"
    if key in sources:
        return str(sources[key])
    return None


def _job_resume_context(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request")
    if isinstance(request, dict) and isinstance(request.get("resume_context"), dict):
        return request["resume_context"]
    return None


def _job_material_resolution(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request")
    if isinstance(request, dict) and isinstance(request.get("material_resolution"), dict):
        return request["material_resolution"]
    resume_context = _job_resume_context(job)
    if isinstance(resume_context, dict) and isinstance(resume_context.get("material_resolution"), dict):
        return resume_context["material_resolution"]
    return None


def _job_resume_lineage(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request")
    if isinstance(request, dict) and isinstance(request.get("resume_lineage"), dict):
        return request["resume_lineage"]
    return None


def _job_resume_audit(job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    lineage = _job_resume_lineage(job)
    context = _job_resume_context(job)
    material_resolution = _job_material_resolution(job)
    plan = _job_resume_plan(job)
    requirements = _job_resume_requirements(job)
    applied_overrides = _resume_audit_override_list(request, context, "applied_overrides")
    ignored_overrides = _resume_audit_override_list(request, context, "ignored_overrides")
    is_resume_job = bool(lineage or context or str(job.get("kind") or "") == "ptcli.resume")
    parent_job_id = request.get("parent_job_id") or (lineage or {}).get("parent_job_id") or (context or {}).get("parent_job_id")
    dry_run_request = requirements.get("dry_run_request") if isinstance(requirements.get("dry_run_request"), dict) else None
    execute_request = requirements.get("execute_request") if isinstance(requirements.get("execute_request"), dict) else None
    return {
        "kind": "ptcli.resume_audit",
        "job_id": job_id or None,
        "is_resume_job": is_resume_job,
        "parent_job_id": parent_job_id,
        "parent_status": request.get("parent_status") or (lineage or {}).get("parent_status") or (context or {}).get("parent_status"),
        "parent_kind": request.get("parent_kind") or (lineage or {}).get("parent_kind") or (context or {}).get("parent_kind"),
        "child_status": job.get("status"),
        "resume_available": bool(plan.get("available")),
        "resume_allowed": bool(plan.get("allowed")),
        "resume_recommended": bool(plan.get("recommended")),
        "resume_endpoint": plan.get("endpoint"),
        "summary_endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None,
        "next_subcommand": plan.get("subcommand") or (lineage or {}).get("next_subcommand"),
        "next_command_argv": plan.get("next_command_argv") or request.get("next_command_argv"),
        "original_next_command_argv": request.get("original_next_command_argv"),
        "applied_override_keys": [item["key"] for item in applied_overrides],
        "ignored_override_keys": [item["key"] for item in ignored_overrides],
        "applied_overrides": applied_overrides,
        "ignored_overrides": ignored_overrides,
        "material_resolution": material_resolution,
        "covered_recommended_inputs": _string_list((material_resolution or {}).get("covered_recommended_inputs")) if isinstance(material_resolution, dict) else [],
        "unresolved_recommended_inputs": (material_resolution or {}).get("unresolved_recommended_inputs") if isinstance(material_resolution, dict) else [],
        "dry_run_request": dry_run_request,
        "execute_request": execute_request,
        "next_step": _resume_audit_next_step(job, plan, dry_run_request, execute_request),
        "next_actions": _resume_audit_next_actions(job, plan, material_resolution),
    }


def _string_keyed_overrides(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict) and item.get("key")]


def _resume_audit_override_list(request: dict[str, Any], context: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    if isinstance(request.get(key), list):
        return _string_keyed_overrides(request.get(key))
    if isinstance(context, dict) and isinstance(context.get(key), list):
        return _string_keyed_overrides(context.get(key))
    return []


def _resume_audit_next_step(job: dict[str, Any], plan: dict[str, Any], dry_run_request: dict[str, Any] | None, execute_request: dict[str, Any] | None) -> dict[str, Any]:
    if plan.get("recommended") and plan.get("allowed"):
        return {
            "tool": "resume_job",
            "endpoint": plan.get("endpoint"),
            "method": "POST",
            "request": dry_run_request or execute_request or {"job_id": job.get("job_id")},
            "reason": "resume_recommended",
        }
    if job.get("status") in {"queued", "running"}:
        return {"tool": "get_job_status", "endpoint": f"/v1/jobs/{job.get('job_id')}", "method": "GET", "request": None, "reason": "job_running"}
    return {"tool": "get_job_summary", "endpoint": f"/v1/jobs/{job.get('job_id')}/summary", "method": "GET", "request": None, "reason": "inspect_resume_audit"}


def _resume_audit_next_actions(job: dict[str, Any], plan: dict[str, Any], material_resolution: dict[str, Any] | None) -> list[str]:
    if plan.get("recommended") and plan.get("allowed"):
        return ["Preview resume with resume_audit.dry_run_request, then execute resume_audit.execute_request after reviewing command_argv and blockers."]
    if isinstance(material_resolution, dict) and material_resolution.get("unresolved_recommended_inputs"):
        return ["Provide unresolved material inputs before executing resume."]
    if job.get("status") in {"queued", "running"}:
        return ["Poll status_endpoint before attempting resume."]
    return ["Inspect resume_audit and closure_handoff before taking the next action."]


def _job_resume_summary(job: dict[str, Any]) -> dict[str, Any]:
    plan = _job_resume_plan(job)
    requirements = _job_resume_requirements(job)
    material_resolution = _job_material_resolution(job)
    dry_run_request = requirements.get("dry_run_request") if isinstance(requirements.get("dry_run_request"), dict) else None
    execute_request = requirements.get("execute_request") if isinstance(requirements.get("execute_request"), dict) else None
    recommended_inputs = requirements.get("recommended_inputs") if isinstance(requirements.get("recommended_inputs"), list) else []
    unresolved_inputs = material_resolution.get("unresolved_recommended_inputs") if isinstance(material_resolution, dict) and isinstance(material_resolution.get("unresolved_recommended_inputs"), list) else []
    next_step = _resume_summary_next_step(job, plan, dry_run_request, execute_request, unresolved_inputs)
    blockers = _resume_summary_blockers(job, plan, requirements, unresolved_inputs)
    return {
        "kind": "ptcli.resume_summary",
        "available": bool(plan.get("available")),
        "allowed": bool(plan.get("allowed")),
        "recommended": bool(plan.get("recommended")),
        "status": plan.get("status"),
        "subcommand": plan.get("subcommand"),
        "endpoint": plan.get("endpoint"),
        "method": plan.get("method"),
        "next_command_argv": plan.get("next_command_argv"),
        "blocker": plan.get("blocker"),
        "missing_confirmations": _string_list(requirements.get("missing_confirmations")),
        "required_overrides": _string_list(requirements.get("required_overrides")),
        "suggested_overrides": requirements.get("suggested_overrides") if isinstance(requirements.get("suggested_overrides"), dict) else {},
        "recommended_input_keys": [item.get("key") for item in recommended_inputs if isinstance(item, dict) and item.get("key")],
        "unresolved_recommended_inputs": unresolved_inputs,
        "covered_recommended_inputs": _string_list(material_resolution.get("covered_recommended_inputs")) if isinstance(material_resolution, dict) else [],
        "dry_run_request": dry_run_request,
        "execute_request": execute_request,
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "blockers": blockers,
        "next_actions": _resume_summary_next_actions(plan, next_step, blockers, unresolved_inputs),
    }


def _job_resume_execution_handoff(job: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    requirements = _job_resume_requirements(job, payload if isinstance(payload, dict) else None)
    summary = _job_resume_summary(job)
    plan = _job_resume_plan(job)
    dry_run_request = requirements.get("dry_run_request") if isinstance(requirements.get("dry_run_request"), dict) else None
    execute_request = requirements.get("execute_request") if isinstance(requirements.get("execute_request"), dict) else None
    unresolved_inputs = summary.get("unresolved_recommended_inputs") if isinstance(summary.get("unresolved_recommended_inputs"), list) else []
    blockers = _resume_execution_handoff_blockers(plan, requirements, summary, unresolved_inputs)
    ready = bool(plan.get("recommended")) and bool(plan.get("allowed")) and not blockers
    return {
        "kind": "ptcli.resume_execution_handoff",
        "ready": ready,
        "status": plan.get("status"),
        "subcommand": plan.get("subcommand"),
        "endpoint": plan.get("endpoint"),
        "method": "POST",
        "preview_required": True,
        "dry_run_request": dry_run_request,
        "execute_request": execute_request,
        "recommended_request": dry_run_request or execute_request,
        "allowed_overrides": requirements.get("allowed_overrides") if isinstance(requirements.get("allowed_overrides"), dict) else {},
        "required_overrides": _string_list(requirements.get("required_overrides")),
        "suggested_overrides": requirements.get("suggested_overrides") if isinstance(requirements.get("suggested_overrides"), dict) else {},
        "recommended_inputs": requirements.get("recommended_inputs") if isinstance(requirements.get("recommended_inputs"), list) else [],
        "unresolved_recommended_inputs": unresolved_inputs,
        "safety_gates": {
            "resume_available": bool(plan.get("available")),
            "resume_allowed": bool(plan.get("allowed")),
            "resume_recommended": bool(plan.get("recommended")),
            "next_command_allowlisted": bool(plan.get("allowed")),
            "missing_confirmations": _string_list(requirements.get("missing_confirmations")),
            "unknown_overrides_ignored": True,
            "live_upload": _resume_current_flags(plan.get("next_command_argv") if isinstance(plan.get("next_command_argv"), list) else None).get("has_confirm_upload"),
        },
        "continue_when": "dry_run_request returns resume_preview.ok=true and user has reviewed command_argv, applied_overrides, ignored_overrides, and site-rule confirmations",
        "execute_when": "preview_required satisfied and execute_request has only allowlisted overrides required for this resume",
        "stop_when": [
            "resume_plan.allowed=false",
            "resume_preview.ok=false",
            "resume_preview.ignored_overrides is non-empty and user has not approved continuing",
            "resume_execution_handoff.blockers is non-empty",
        ],
        "blockers": blockers,
        "next_actions": _resume_execution_handoff_next_actions(ready, dry_run_request, execute_request, blockers),
    }


def _resume_execution_handoff_blockers(plan: dict[str, Any], requirements: dict[str, Any], summary: dict[str, Any], unresolved_inputs: list[Any]) -> list[str]:
    blockers: list[str] = []
    if plan.get("available") is not True:
        blockers.append(plan.get("blocker") or "resume.next_command_unavailable")
    elif plan.get("allowed") is not True:
        blockers.append(plan.get("blocker") or "resume.next_command_not_allowlisted")
    if plan.get("recommended") is not True:
        blockers.append("resume.not_recommended_for_current_status")
    blockers.extend(_string_list(requirements.get("missing_confirmations")))
    blockers.extend([f"resume.unresolved_input.{item.get('key')}" for item in unresolved_inputs if isinstance(item, dict) and item.get("key")])
    blockers.extend(_string_list(summary.get("blockers")))
    return list(dict.fromkeys(str(item) for item in blockers if item))


def _resume_execution_handoff_next_actions(ready: bool, dry_run_request: dict[str, Any] | None, execute_request: dict[str, Any] | None, blockers: list[str]) -> list[str]:
    if ready and dry_run_request and execute_request:
        return ["Call resume_execution_handoff.dry_run_request first, review the preview, then call execute_request only after user approval."]
    if ready and execute_request:
        return ["Call resume_execution_handoff.execute_request after reviewing site rules and blockers."]
    if blockers:
        return ["Resolve resume_execution_handoff.blockers before attempting resume."]
    return ["Inspect resume_execution_handoff before taking the next resume action."]


def _resume_summary_next_step(
    job: dict[str, Any],
    plan: dict[str, Any],
    dry_run_request: dict[str, Any] | None,
    execute_request: dict[str, Any] | None,
    unresolved_inputs: list[Any],
) -> dict[str, Any]:
    if job.get("status") in {"queued", "running"}:
        return {"tool": "get_job_status", "endpoint": f"/v1/jobs/{job.get('job_id')}", "method": "GET", "request": None, "reason": "job_running"}
    if plan.get("recommended") and plan.get("allowed"):
        return {
            "tool": "resume_job",
            "endpoint": plan.get("endpoint"),
            "method": "POST",
            "request": dry_run_request or execute_request or {"job_id": job.get("job_id")},
            "reason": "preview_resume_before_execute" if dry_run_request else "execute_resume",
        }
    if unresolved_inputs:
        return {"tool": "get_job_summary", "endpoint": f"/v1/jobs/{job.get('job_id')}/summary", "method": "GET", "request": None, "reason": "provide_recommended_inputs"}
    return {"tool": "get_job_summary", "endpoint": f"/v1/jobs/{job.get('job_id')}/summary", "method": "GET", "request": None, "reason": "inspect_resume_summary"}


def _resume_summary_blockers(job: dict[str, Any], plan: dict[str, Any], requirements: dict[str, Any], unresolved_inputs: list[Any]) -> list[str]:
    blockers: list[str] = []
    if plan.get("available") is not True:
        blockers.append(plan.get("blocker") or "resume.next_command_unavailable")
    elif plan.get("allowed") is not True:
        blockers.append(plan.get("blocker") or "resume.next_command_not_allowlisted")
    if job.get("status") in {"queued", "running"}:
        blockers.append(f"resume.job_still_{job.get('status')}")
    blockers.extend(_string_list(requirements.get("missing_confirmations")))
    blockers.extend([f"resume.unresolved_input.{item.get('key')}" for item in unresolved_inputs if isinstance(item, dict) and item.get("key")])
    return list(dict.fromkeys(str(item) for item in blockers if item))


def _resume_summary_next_actions(plan: dict[str, Any], next_step: dict[str, Any], blockers: list[str], unresolved_inputs: list[Any]) -> list[str]:
    request = next_step.get("request") if isinstance(next_step.get("request"), dict) else {}
    if plan.get("recommended") and plan.get("allowed") and not unresolved_inputs and request.get("dry_run") is True:
        return ["Call resume_summary.next_step to dry-run the patched command, review command_argv, then call resume_summary.execute_request without dry_run when ready."]
    if plan.get("recommended") and plan.get("allowed"):
        return ["Call resume_summary.next_step after reviewing blockers, confirmations, and site rules."]
    if unresolved_inputs:
        return ["Provide resume_summary.unresolved_recommended_inputs before executing resume."]
    if blockers:
        return ["Resolve resume_summary.blockers before attempting resume."]
    return ["Inspect resume_summary and closure_summary before taking the next action."]


def _job_candidate_submission(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request")
    if isinstance(request, dict) and isinstance(request.get("candidate_submission"), dict):
        return request["candidate_submission"]
    return None


def _job_check_submission(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request")
    if isinstance(request, dict) and isinstance(request.get("check_submission"), dict):
        return request["check_submission"]
    return None


def _job_candidate_submission_handoff(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    submission = _job_candidate_submission(job)
    if not submission:
        return None
    job_id = job.get("job_id")
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    manual_handoff = _job_manual_retorrent_handoff(job, summary_payload)
    policy_execution_handoff = submission.get("policy_execution_handoff") if isinstance(submission.get("policy_execution_handoff"), dict) else {}
    closure_summary = _job_closure_summary(job, summary_payload)
    execution_handoff = _candidate_submission_execution_handoff(job, manual_handoff if isinstance(manual_handoff, dict) else {}, closure_summary, policy_execution_handoff)
    return {
        "kind": "ptcli.candidate_submission_handoff",
        "candidate_job_id": submission.get("candidate_job_id"),
        "candidate_rank": submission.get("candidate_rank"),
        "candidate_source_id": submission.get("candidate_source_id"),
        "candidate_title": submission.get("candidate_title"),
        "candidate_summary_text": submission.get("candidate_summary_text"),
        "candidate_digest_kind": submission.get("candidate_digest_kind"),
        "inherited_request": submission.get("inherited_request") if isinstance(submission.get("inherited_request"), dict) else {},
        "submitted_overrides": submission.get("submitted_overrides") if isinstance(submission.get("submitted_overrides"), dict) else {},
        "material_options": submission.get("material_options") if isinstance(submission.get("material_options"), dict) else {},
        "qbit_overrides": submission.get("qbit_overrides") if isinstance(submission.get("qbit_overrides"), dict) else {},
        "policy_execution_handoff": policy_execution_handoff,
        "retorrent_job_id": job_id,
        "retorrent_status": job.get("status"),
        "source_reference": _job_source_reference(job),
        "target_trackers": request.get("target_trackers"),
        "manual_retorrent_handoff": manual_handoff,
        "action": manual_handoff.get("action") if isinstance(manual_handoff, dict) else None,
        "execution_state": execution_handoff.get("state"),
        "execution_handoff": execution_handoff,
        "status_endpoint": f"/v1/jobs/{job_id}" if job_id else None,
        "summary_endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None,
        "parent_status_endpoint": f"/v1/jobs/{submission.get('candidate_job_id')}" if submission.get("candidate_job_id") else None,
        "parent_summary_endpoint": f"/v1/jobs/{submission.get('candidate_job_id')}/summary" if submission.get("candidate_job_id") else None,
        "next_actions": _candidate_submission_handoff_next_actions(manual_handoff),
    }


def _job_candidate_submission_summary(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    submission = _job_candidate_submission(job)
    if not submission:
        return None
    handoff = _job_candidate_submission_handoff(job, summary_payload)
    manual_handoff = handoff.get("manual_retorrent_handoff") if isinstance(handoff, dict) and isinstance(handoff.get("manual_retorrent_handoff"), dict) else {}
    closure_summary = _job_closure_summary(job, summary_payload)
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    submitted_overrides = submission.get("submitted_overrides") if isinstance(submission.get("submitted_overrides"), dict) else {}
    material_options = submission.get("material_options") if isinstance(submission.get("material_options"), dict) else {}
    qbit_overrides = submission.get("qbit_overrides") if isinstance(submission.get("qbit_overrides"), dict) else {}
    policy_execution_handoff = submission.get("policy_execution_handoff") if isinstance(submission.get("policy_execution_handoff"), dict) else {}
    next_step = closure_summary.get("next_step") if isinstance(closure_summary.get("next_step"), dict) else {}
    execution_handoff = handoff.get("execution_handoff") if isinstance(handoff, dict) and isinstance(handoff.get("execution_handoff"), dict) else {}
    blockers = list(
        dict.fromkeys(
            _string_list(closure_summary.get("blockers"))
            + _string_list(manual_handoff.get("blockers"))
            + _string_list(job.get("blockers"))
        )
    )
    return {
        "kind": "ptcli.candidate_submission_summary",
        "candidate_job_id": submission.get("candidate_job_id"),
        "retorrent_job_id": job.get("job_id"),
        "retorrent_status": job.get("status"),
        "candidate_rank": submission.get("candidate_rank"),
        "candidate_source_id": submission.get("candidate_source_id"),
        "candidate_title": submission.get("candidate_title"),
        "source_reference": _job_source_reference(job),
        "target_trackers": request.get("target_trackers"),
        "inherited_request": submission.get("inherited_request") if isinstance(submission.get("inherited_request"), dict) else {},
        "submitted_override_keys": sorted(submitted_overrides),
        "material_option_keys": sorted(material_options),
        "qbit_override_keys": sorted(qbit_overrides),
        "policy_execution_handoff": policy_execution_handoff,
        "policy_execution_ready": policy_execution_handoff.get("ready") if policy_execution_handoff else None,
        "manual_action": manual_handoff.get("action"),
        "closure_action": closure_summary.get("action"),
        "closure_complete": closure_summary.get("complete") is True,
        "execution_state": execution_handoff.get("state"),
        "execution_handoff": execution_handoff,
        "can_attempt_live": manual_handoff.get("can_attempt_live"),
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_request": next_step.get("request"),
        "status_endpoint": f"/v1/jobs/{job.get('job_id')}" if job.get("job_id") else None,
        "summary_endpoint": f"/v1/jobs/{job.get('job_id')}/summary" if job.get("job_id") else None,
        "parent_summary_endpoint": f"/v1/jobs/{submission.get('candidate_job_id')}/summary" if submission.get("candidate_job_id") else None,
        "blockers": blockers,
        "next_actions": _candidate_submission_summary_next_actions(closure_summary, manual_handoff, next_step, blockers),
    }


def _candidate_submission_execution_handoff(job: dict[str, Any], manual_handoff: dict[str, Any], closure_summary: dict[str, Any], policy_execution_handoff: dict[str, Any]) -> dict[str, Any]:
    status = str(job.get("status") or "unknown")
    blockers = list(dict.fromkeys(_string_list(closure_summary.get("blockers")) + _string_list(manual_handoff.get("blockers")) + _string_list(job.get("blockers"))))
    closure_action = str(closure_summary.get("action") or "")
    manual_action = str(manual_handoff.get("action") or "")
    next_step = closure_summary.get("next_step") if isinstance(closure_summary.get("next_step"), dict) else {}
    materials_handoff = manual_handoff.get("materials_handoff") if isinstance(manual_handoff.get("materials_handoff"), dict) else None
    material_input_template = _candidate_submission_material_input_template(materials_handoff)
    policy_ready = policy_execution_handoff.get("ready") if isinstance(policy_execution_handoff.get("ready"), bool) else None
    state, reason = _candidate_submission_execution_state(status, closure_summary, manual_handoff, policy_ready, blockers)
    recommended_tool = next_step.get("tool")
    recommended_endpoint = next_step.get("endpoint")
    recommended_request = next_step.get("request")
    return {
        "kind": "ptcli.candidate_submission_execution_handoff",
        "state": state,
        "reason": reason,
        "status": status,
        "ready_for_live": state in {"ready_for_live_upload", "resume"} and not blockers,
        "should_poll": state == "wait",
        "should_resume": state in {"resume", "prepare_materials", "repair_target_payload", "repair_qbit"} and recommended_tool == "resume_job",
        "should_stop": state in {"stop_duplicate", "collect_confirmations", "configure_policy", "cancelled"},
        "manual_action": manual_action or None,
        "closure_action": closure_action or None,
        "policy_execution_ready": policy_ready,
        "recommended_tool": recommended_tool,
        "recommended_endpoint": recommended_endpoint,
        "recommended_method": next_step.get("method"),
        "recommended_request": recommended_request,
        "material_input_template": material_input_template,
        "continue_when": _candidate_submission_execution_continue_when(state),
        "stop_when": _candidate_submission_execution_stop_when(state),
        "blockers": blockers,
        "next_actions": _candidate_submission_execution_next_actions(state, next_step, blockers, material_input_template),
    }


def _candidate_submission_material_input_template(materials_handoff: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(materials_handoff, dict):
        return None
    recommended_inputs = materials_handoff.get("recommended_inputs") if isinstance(materials_handoff.get("recommended_inputs"), list) else []
    material_plan = materials_handoff.get("material_plan") if isinstance(materials_handoff.get("material_plan"), dict) else {}
    resume_handoff = materials_handoff.get("resume_handoff") if isinstance(materials_handoff.get("resume_handoff"), dict) else {}
    next_item = material_plan.get("next_item") if isinstance(material_plan.get("next_item"), dict) else None
    return {
        "kind": "ptcli.candidate_submission_material_input_template",
        "ready": materials_handoff.get("ready") is True,
        "recommended_input_keys": [item.get("key") for item in recommended_inputs if isinstance(item, dict) and item.get("key")],
        "recommended_inputs": recommended_inputs,
        "missing": _string_list(material_plan.get("missing")),
        "next_item": next_item,
        "accepted_override_keys": _string_list(resume_handoff.get("accepted_override_keys")),
        "resume_request_template": materials_handoff.get("resume_request_template") if isinstance(materials_handoff.get("resume_request_template"), dict) else {},
        "dry_run_request": resume_handoff.get("dry_run_request") if isinstance(resume_handoff.get("dry_run_request"), dict) else None,
        "execute_request": resume_handoff.get("execute_request") if isinstance(resume_handoff.get("execute_request"), dict) else None,
        "staged_requests": resume_handoff.get("staged_requests") if isinstance(resume_handoff.get("staged_requests"), list) else [],
        "examples_by_key": {
            str(item.get("key")): item.get("examples")
            for item in recommended_inputs
            if isinstance(item, dict) and item.get("key") and isinstance(item.get("examples"), dict)
        },
        "continue_when": resume_handoff.get("continue_when"),
        "stop_when": resume_handoff.get("stop_when"),
    }


def _candidate_submission_execution_state(status: str, closure_summary: dict[str, Any], manual_handoff: dict[str, Any], policy_ready: bool | None, blockers: list[str]) -> tuple[str, str]:
    closure_action = str(closure_summary.get("action") or "")
    manual_action = str(manual_handoff.get("action") or "")
    if status in {"queued", "running"} or closure_action == "wait" or manual_action == "poll":
        return "wait", "submitted_retorrent_job_still_running"
    if closure_summary.get("complete") is True and not blockers:
        return "complete", "retorrent_closure_complete"
    if closure_action == "stop_duplicate" or manual_action == "stop_duplicate":
        return "stop_duplicate", "target_duplicate_exists"
    if manual_action == "collect_confirmations" or closure_action == "collect_confirmations":
        return "collect_confirmations", "missing_required_confirmation"
    if policy_ready is False or manual_action == "configure_policy" or closure_action == "configure_policy":
        return "configure_policy", "policy_coverage_or_execution_incomplete"
    if closure_action in {"prepare_materials", "repair_target_payload", "repair_qbit", "ready_for_upload"}:
        return "ready_for_live_upload" if closure_action == "ready_for_upload" else closure_action, closure_action
    if manual_action == "resume" or closure_summary.get("recommended_tool") == "resume_job":
        return "resume", "resume_submitted_retorrent_job"
    if status == "cancelled":
        return "cancelled", "job_cancelled"
    if blockers:
        return "blocked", "blocked_by_runtime_or_gate"
    if status == "complete":
        return "complete", "retorrent_job_complete"
    return "inspect", "unknown_candidate_submission_state"


def _candidate_submission_execution_continue_when(state: str) -> str | None:
    if state == "wait":
        return "job status is no longer queued/running"
    if state in {"resume", "prepare_materials", "repair_target_payload", "repair_qbit"}:
        return "recommended_request reviewed, site rules accepted, and resume dry-run is acceptable"
    if state == "ready_for_live_upload":
        return "accept_rules=true, confirm_upload=true, duplicate_check clear, policy_execution_ready=true, and target payload ready"
    if state == "complete":
        return "closure_summary.complete=true and blockers=[]"
    return None


def _candidate_submission_execution_stop_when(state: str) -> list[str]:
    if state == "stop_duplicate":
        return ["duplicate_check.exists=true"]
    if state == "collect_confirmations":
        return ["accept_rules or confirm_upload is missing"]
    if state == "configure_policy":
        return ["policy_execution_handoff.ready=false", "rule obligations or site policy coverage incomplete"]
    if state == "cancelled":
        return ["job.status=cancelled"]
    if state == "blocked":
        return ["candidate_submission_summary.blockers is not empty and recommended_tool is null"]
    return []


def _candidate_submission_execution_next_actions(state: str, next_step: dict[str, Any], blockers: list[str], material_input_template: dict[str, Any] | None) -> list[str]:
    if state == "complete":
        return []
    if state == "wait":
        return ["Poll candidate_submission_handoff.status_endpoint until the submitted retorrent job is terminal."]
    if state == "stop_duplicate":
        return ["Stop. Do not upload; report duplicate_check.dupes or choose another target."]
    if state == "collect_confirmations":
        return ["Ask the user for explicit accept_rules/confirm_upload confirmation before resuming."]
    if state == "configure_policy":
        return ["Call site_policies or update PTCLI.SITE_POLICIES before attempting live work."]
    if state == "prepare_materials" and isinstance(material_input_template, dict):
        keys = ", ".join(_string_list(material_input_template.get("recommended_input_keys")))
        return [f"Use candidate_submission_summary.execution_handoff.material_input_template to provide material overrides: {keys}."]
    if next_step.get("tool"):
        return [f"Use candidate_submission_summary.execution_handoff.recommended_request with {next_step['tool']} after reviewing blockers and site rules."]
    if blockers:
        return ["Resolve candidate_submission_summary.execution_handoff.blockers before resuming the submitted retorrent job."]
    return ["Inspect candidate_submission_summary.execution_handoff and closure_summary before taking live action."]


def _candidate_submission_summary_next_actions(closure_summary: dict[str, Any], manual_handoff: dict[str, Any], next_step: dict[str, Any], blockers: list[str]) -> list[str]:
    if closure_summary.get("complete") is True and not blockers:
        return []
    if next_step.get("tool"):
        return [f"Use candidate_submission_summary.next_step with {next_step['tool']} after reviewing blockers and confirmations."]
    actions = _string_list(manual_handoff.get("next_actions")) or _string_list(closure_summary.get("next_actions"))
    if actions:
        return actions
    if blockers:
        return ["Resolve candidate_submission_summary.blockers before resuming the submitted retorrent job."]
    return ["Poll candidate_submission_summary.status_endpoint, then read candidate_submission_summary.summary_endpoint."]


def _candidate_submission_handoff_next_actions(manual_handoff: dict[str, Any] | None) -> list[str]:
    if not isinstance(manual_handoff, dict):
        return ["Poll the retorrent job status endpoint, then inspect manual_retorrent_handoff."]
    action = manual_handoff.get("action")
    if action == "poll":
        return ["Poll status_endpoint until manual_retorrent_handoff.action changes."]
    if action == "resume":
        return _string_list(manual_handoff.get("next_actions")) or ["Resume the retorrent job through manual_retorrent_handoff.resume_endpoint."]
    return _string_list(manual_handoff.get("next_actions"))


def _job_source_reference(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request")
    if not isinstance(request, dict):
        return None
    if isinstance(request.get("source_reference"), dict):
        return request["source_reference"]
    if isinstance(request.get("parent_source_reference"), dict):
        return request["parent_source_reference"]
    if isinstance(request.get("source"), dict):
        return request["source"]
    return None


def _job_manual_retorrent_handoff(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    if not _is_manual_retorrent_job(job, request):
        return None
    status = str(job.get("status") or "unknown")
    duplicate_check = _job_duplicate_check(job)
    duplicate_searched = duplicate_check.get("searched") is True
    duplicate_exists = duplicate_check.get("exists") is True
    duplicate_clear = duplicate_searched and duplicate_exists is False
    missing_confirmations = _missing_live_confirmations(request)
    policy_coverage = _job_policy_coverage(job)
    policy_ready = policy_coverage.get("ready") if isinstance(policy_coverage, dict) and isinstance(policy_coverage.get("ready"), bool) else None
    qbit_limit_audit = _job_qbit_limit_audit(job, summary_payload)
    qbit_limit_ready = qbit_limit_audit.get("ready") if isinstance(qbit_limit_audit, dict) and isinstance(qbit_limit_audit.get("ready"), bool) else None
    materials_handoff = _job_materials_handoff(job, summary_payload)
    target_upload_handoff = _job_target_upload_handoff(job, summary_payload)
    resume_plan = _job_resume_plan(job)
    runtime = _job_runtime(job)
    blockers = _string_list(job.get("blockers"))
    next_actions = _string_list(job.get("next_actions"))
    action, reason = _manual_retorrent_handoff_action(
        status=status,
        duplicate_exists=duplicate_exists,
        missing_confirmations=missing_confirmations,
        policy_ready=policy_ready,
        resume_plan=resume_plan,
        blockers=blockers,
    )
    can_attempt_live = bool(duplicate_clear and not missing_confirmations and policy_ready is not False and status not in {"queued", "running", "cancelled"})
    can_resume = bool(resume_plan.get("recommended")) and not duplicate_exists and not missing_confirmations and policy_ready is not False
    live_checklist = _manual_retorrent_live_checklist(
        job,
        duplicate_clear=duplicate_clear,
        missing_confirmations=missing_confirmations,
        policy_ready=policy_ready,
        qbit_limit_ready=qbit_limit_ready,
        materials_handoff=materials_handoff,
        target_upload_handoff=target_upload_handoff,
    )
    return {
        "kind": "ptcli.manual_retorrent_handoff",
        "ready": status == "complete",
        "action": action,
        "reason": reason,
        "live_ready": live_checklist["ready"],
        "live_checklist": live_checklist,
        "status": status,
        "source_reference": _job_source_reference(job),
        "target_trackers": request.get("target_trackers"),
        "duplicate_check": duplicate_check,
        "duplicate_clear": duplicate_clear,
        "missing_confirmations": missing_confirmations,
        "policy_coverage_ready": policy_ready,
        "qbit_limit_audit_ready": qbit_limit_ready,
        "materials_handoff": materials_handoff,
        "target_upload_handoff": target_upload_handoff,
        "can_attempt_live": can_attempt_live,
        "can_resume": can_resume,
        "should_poll": bool(runtime.get("should_poll")),
        "status_endpoint": runtime.get("status_endpoint"),
        "summary_endpoint": runtime.get("summary_endpoint"),
        "resume_endpoint": runtime.get("resume_endpoint") if can_resume else None,
        "resume_plan": resume_plan,
        "summary_file": job.get("summary_file") or _job_summary_file(job),
        "blockers": blockers,
        "next_actions": _manual_retorrent_handoff_next_actions(action, next_actions, missing_confirmations, resume_plan),
    }


def _manual_retorrent_live_checklist(
    job: dict[str, Any],
    *,
    duplicate_clear: bool,
    missing_confirmations: list[str],
    policy_ready: bool | None,
    qbit_limit_ready: bool | None,
    materials_handoff: dict[str, Any] | None,
    target_upload_handoff: dict[str, Any] | None,
) -> dict[str, Any]:
    source_reference = _job_source_reference(job)
    source_resolved = bool(source_reference.get("tracker") and (source_reference.get("source_id") or source_reference.get("source_url")))
    materials_ready = materials_handoff.get("ready") if isinstance(materials_handoff, dict) else None
    target_payload_ready = target_upload_handoff.get("ready_for_live_upload") if isinstance(target_upload_handoff, dict) else None
    uploaded_seeding_ready = target_upload_handoff.get("uploaded_seeding_ready") if isinstance(target_upload_handoff, dict) else None
    items = [
        _checklist_item("source_reference", source_resolved, required=True, blocker="source_reference.unresolved", evidence=source_reference),
        _checklist_item("target_duplicate_clear", duplicate_clear, required=True, blocker="duplicate_check.not_clear"),
        _checklist_item("confirmations", not missing_confirmations, required=True, blocker="confirmations.missing", missing=missing_confirmations),
        _checklist_item("site_policy", policy_ready is True, required=True, blocker="policy_coverage.not_ready", state="unknown" if policy_ready is None else "ready" if policy_ready else "blocked"),
        _checklist_item("materials", materials_ready is True if materials_ready is not None else None, required=False, blocker="materials_handoff.not_ready"),
        _checklist_item("target_upload_payload", target_payload_ready is True if target_payload_ready is not None else None, required=False, blocker="target_upload_handoff.not_ready"),
        _checklist_item("uploaded_seeding", uploaded_seeding_ready is True if uploaded_seeding_ready is not None else None, required=False, blocker="target_upload_handoff.uploaded_seeding_not_ready"),
        _checklist_item("qbit_limit_audit", qbit_limit_ready is True if qbit_limit_ready is not None else None, required=False, blocker="qbit_limit_audit.not_ready"),
    ]
    blockers = _checklist_blockers(items)
    return {
        "kind": "ptcli.manual_retorrent_live_checklist",
        "ready": not blockers,
        "items": items,
        "blockers": blockers,
        "next_actions": [] if not blockers else ["Resolve manual_retorrent_handoff.live_checklist.blockers before attempting live upload."],
    }


def _checklist_item(key: str, ready: bool | None, *, required: bool, blocker: str, **extra: Any) -> dict[str, Any]:
    item = {
        "key": key,
        "ready": ready,
        "required": required,
        "blocker": blocker if ready is not True else None,
    }
    item.update({name: value for name, value in extra.items() if value not in (None, [], {})})
    return item


def _checklist_blockers(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["blocker"]) for item in items if item.get("required") is True and item.get("ready") is not True and item.get("blocker")]


def _is_manual_retorrent_job(job: dict[str, Any], request: dict[str, Any]) -> bool:
    kind = str(job.get("kind") or "")
    mode = str(request.get("mode") or "")
    return kind in {"ptcli.manual_retorrent", "ptcli.source_url_retorrent", "ptcli.candidate_retorrent"} or mode in {"manual_retorrent", "source_url_retorrent", "candidate_retorrent"}


def _manual_retorrent_handoff_action(
    *,
    status: str,
    duplicate_exists: bool,
    missing_confirmations: list[str],
    policy_ready: bool | None,
    resume_plan: dict[str, Any],
    blockers: list[str],
) -> tuple[str, str | None]:
    if status in {"queued", "running"}:
        return "poll", "job_running"
    if duplicate_exists:
        return "stop_duplicate", "target_duplicate_exists"
    if missing_confirmations:
        return "collect_confirmations", "missing_required_confirmation"
    if policy_ready is False:
        return "configure_policy", "policy_coverage_incomplete"
    if status == "complete":
        return "done", None
    if status == "cancelled":
        return "stop", "job_cancelled"
    if resume_plan.get("recommended"):
        return "resume", None
    if blockers:
        return "resolve_blockers", "blocked_by_runtime_or_gate"
    return "inspect", "unknown_state"


def _manual_retorrent_handoff_next_actions(action: str, next_actions: list[str], missing_confirmations: list[str], resume_plan: dict[str, Any]) -> list[str]:
    if action == "poll":
        return ["Poll status_endpoint until status is no longer queued or running."]
    if action == "stop_duplicate":
        return ["Do not upload. Inspect duplicate_check.dupes or choose another target tracker."]
    if action == "collect_confirmations":
        return [f"Collect explicit confirmation for: {', '.join(missing_confirmations)}."]
    if action == "resume" and resume_plan.get("endpoint"):
        return [f"Call resume_endpoint {resume_plan['endpoint']} after reviewing blockers and site rules."]
    if next_actions:
        return next_actions
    return []


def _job_workflow_context(job: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    result = payload if isinstance(payload, dict) else job.get("result") if isinstance(job.get("result"), dict) else {}
    agent_summary = _agent_summary(result) or (job.get("agent_summary") if isinstance(job.get("agent_summary"), dict) else {}) or {}
    metadata = agent_summary.get("metadata") if isinstance(agent_summary.get("metadata"), dict) else {}
    materials = agent_summary.get("materials") if isinstance(agent_summary.get("materials"), dict) else {}
    target_preflight = agent_summary.get("target_preflight") if isinstance(agent_summary.get("target_preflight"), dict) else {}
    qbit = agent_summary.get("qbit") if isinstance(agent_summary.get("qbit"), dict) else {}
    qbit_source = qbit.get("source") if isinstance(qbit.get("source"), dict) else {}
    qbit_target = qbit.get("target") if isinstance(qbit.get("target"), dict) else {}
    duplicate_check = _job_duplicate_check(job)
    policy_coverage = _job_policy_coverage(job)
    resume_state = job.get("resume_state") if isinstance(job.get("resume_state"), dict) else _result_resume_state(result)
    next_command_argv = _result_next_command_argv(result)
    missing_confirmations = _missing_live_confirmations(request)
    duplicate_exists = duplicate_check.get("exists") is True
    duplicate_searched = duplicate_check.get("searched") is True
    policy_ready = policy_coverage.get("ready") if isinstance(policy_coverage, dict) else None
    resume_plan = _job_resume_plan(job)
    return {
        "workflow": job.get("kind"),
        "mode": request.get("mode"),
        "status": job.get("status"),
        "source_reference": _job_source_reference(job),
        "candidate_submission": _job_candidate_submission(job),
        "target_trackers": request.get("target_trackers"),
        "resume_lineage": _job_resume_lineage(job),
        "summary_file": job.get("summary_file"),
        "blockers": _string_list(job.get("blockers")),
        "next_actions": _string_list(job.get("next_actions")),
        "next_command_argv": next_command_argv,
        "gates": {
            "source_resolved": _job_source_reference(job) is not None,
            "duplicate_check": {
                "searched": duplicate_searched,
                "exists": duplicate_check.get("exists"),
                "status": duplicate_check.get("status"),
                "count": duplicate_check.get("count"),
                "clear": duplicate_searched and not duplicate_exists,
            },
            "policy_coverage_ready": policy_ready,
            "confirmations_ready": not missing_confirmations,
            "resume_available": bool(resume_plan.get("available")),
            "resume_allowed": bool(resume_plan.get("allowed")),
            "resume_recommended": bool(resume_plan.get("recommended")),
            "materials_ready": materials.get("ready_for_mteam_upload"),
            "target_preflight_ready": target_preflight.get("ready"),
            "qbit_source_ready": qbit_source.get("ready"),
            "qbit_target_ready": qbit_target.get("ready"),
            "uploaded_seeding_evidence": qbit_target.get("uploaded_wait_evidence"),
        },
        "metadata": {
            "ready": metadata.get("ready"),
            "imdb_ready": metadata.get("imdb_ready"),
            "tmdb_ready": metadata.get("tmdb_ready"),
            "douban_ready": metadata.get("douban_ready"),
            "ptgen_description_ready": metadata.get("ptgen_description_ready"),
            "missing": _string_list(metadata.get("missing")),
        },
        "materials": {
            "ready_for_mteam_upload": materials.get("ready_for_mteam_upload"),
            "critical_ready": materials.get("critical_ready"),
            "critical_missing": _string_list(materials.get("critical_missing")),
            "next_step": (materials.get("critical_path") or {}).get("next_step") if isinstance(materials.get("critical_path"), dict) else None,
            "mediainfo_or_bdinfo_ready": materials.get("mediainfo_or_bdinfo_ready"),
            "screenshots_ready": materials.get("screenshots_ready"),
            "screenshot_count": materials.get("screenshot_count"),
            "description_ready": materials.get("description_ready"),
            "upload_material_blockers": _string_list(materials.get("upload_material_blockers")),
        },
        "target_preflight": {
            "ready": target_preflight.get("ready"),
            "materials_ready": target_preflight.get("materials_ready"),
            "metadata_ready": target_preflight.get("metadata_ready"),
            "assets_ready": target_preflight.get("assets_ready"),
            "description_ready": target_preflight.get("description_ready"),
            "payload_ready": target_preflight.get("payload_ready"),
            "missing": _string_list(target_preflight.get("missing")),
            "description_missing": _string_list(target_preflight.get("description_missing")),
            "blockers": _string_list(target_preflight.get("blockers")),
        },
        "qbit": {
            "source": qbit_source,
            "target": qbit_target,
            "wait_diagnostics": qbit.get("wait_diagnostics") if isinstance(qbit.get("wait_diagnostics"), dict) else {},
        },
        "required_confirmations_missing": missing_confirmations,
        "policy_coverage": policy_coverage,
        "policy_handoff": _job_policy_handoff(job),
        "policy_qbit_defaults": _job_policy_qbit_defaults(job),
        "qbit_plan": _job_qbit_plan(job),
        "qbit_limit_audit": _job_qbit_limit_audit(job, payload if isinstance(payload, dict) else None),
        "qbit_handoff": _job_qbit_handoff(job, payload if isinstance(payload, dict) else None),
        "qbit_enforcement_summary": _job_qbit_enforcement_summary(job, payload if isinstance(payload, dict) else None),
        "materials_handoff": _job_materials_handoff(job, payload if isinstance(payload, dict) else None),
        "target_upload_handoff": _job_target_upload_handoff(job, payload if isinstance(payload, dict) else None),
        "manual_retorrent_handoff": _job_manual_retorrent_handoff(job, payload if isinstance(payload, dict) else None),
        "candidate_submission_handoff": _job_candidate_submission_handoff(job, payload if isinstance(payload, dict) else None),
        "candidate_submission_summary": _job_candidate_submission_summary(job, payload if isinstance(payload, dict) else None),
        "resume_plan": resume_plan,
        "resume_requirements": _job_resume_requirements(job, payload if isinstance(payload, dict) else None),
        "resume_execution_handoff": _job_resume_execution_handoff(job, payload if isinstance(payload, dict) else None),
        "recovery_handoff": _job_recovery_handoff(job, payload if isinstance(payload, dict) else None),
        "resume_state": resume_state,
        "resume_context": _job_resume_context(job),
        "resume_summary": _job_resume_summary(job),
        "material_resolution": _job_material_resolution(job),
    }


def _job_resume_requirements(job: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    result = payload if isinstance(payload, dict) else job.get("result") if isinstance(job.get("result"), dict) else {}
    plan = _job_resume_plan(job)
    argv = plan.get("next_command_argv") if isinstance(plan.get("next_command_argv"), list) else None
    agent_summary = _agent_summary(result) or (job.get("agent_summary") if isinstance(job.get("agent_summary"), dict) else {}) or {}
    metadata = agent_summary.get("metadata") if isinstance(agent_summary.get("metadata"), dict) else {}
    materials = agent_summary.get("materials") if isinstance(agent_summary.get("materials"), dict) else {}
    target_preflight = agent_summary.get("target_preflight") if isinstance(agent_summary.get("target_preflight"), dict) else {}
    missing_confirmations = _missing_live_confirmations(request)
    suggested_overrides: dict[str, Any] = {}
    required_overrides: list[str] = []
    if "accept_rules=true" in missing_confirmations:
        suggested_overrides["accept_rules"] = True
        required_overrides.append("accept_rules")
    if "confirm_upload=true" in missing_confirmations:
        suggested_overrides["confirm_upload"] = True
        required_overrides.append("confirm_upload")

    recommended_inputs = _resume_recommended_inputs(request, metadata, materials, target_preflight)
    request_template = _resume_request_template(plan, suggested_overrides)
    return {
        "kind": "ptcli.resume_requirements",
        "can_call_resume": bool(plan.get("allowed")),
        "resume_recommended": bool(plan.get("recommended")),
        "subcommand": plan.get("subcommand"),
        "endpoint": plan.get("endpoint"),
        "method": "POST",
        "missing_confirmations": missing_confirmations,
        "required_overrides": required_overrides,
        "suggested_overrides": suggested_overrides,
        "request_template": request_template,
        "dry_run_request": {**request_template, "dry_run": True},
        "execute_request": request_template,
        "recommended_inputs": recommended_inputs,
        "allowed_overrides": {
            "boolean": sorted(RESUME_BOOLEAN_FLAG_OVERRIDES),
            "value": sorted(RESUME_VALUE_FLAG_OVERRIDES),
            "repeatable": sorted(RESUME_REPEATABLE_FLAG_OVERRIDES),
        },
        "current_flags": _resume_current_flags(argv),
        "ignored_override_policy": "Unknown override keys are ignored and reported in resume_context.ignored_overrides.",
        "blocker": plan.get("blocker"),
    }


def _resume_request_template(plan: dict[str, Any], suggested_overrides: dict[str, Any]) -> dict[str, Any]:
    template: dict[str, Any] = {}
    if plan.get("parent_job_id"):
        template["job_id"] = plan.get("parent_job_id")
    template.update(suggested_overrides)
    return template


def _resume_recommended_inputs(
    request: dict[str, Any],
    metadata: dict[str, Any],
    materials: dict[str, Any],
    target_preflight: dict[str, Any],
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    critical_missing = _string_list(materials.get("critical_missing"))
    upload_material_blockers = _string_list(materials.get("upload_material_blockers"))
    target_missing = _string_list(target_preflight.get("missing"))
    target_description_missing = _string_list(target_preflight.get("description_missing"))
    target_blockers = _string_list(target_preflight.get("blockers"))
    all_missing = set(_string_list(metadata.get("missing")) + critical_missing + upload_material_blockers + target_missing + target_description_missing + target_blockers)
    if not request.get("path") and not request.get("save_path"):
        inputs.append(
            _recommended_input(
                "path_or_save_path",
                ["path", "content_path", "save_path"],
                "content path helps resume without rediscovering qBittorrent state",
                stage="source-content",
                examples={"path": "/downloads/Example.Movie.2024", "save_path": "/downloads"},
            )
        )
    metadata_missing_keys = {"metadata.imdb", "metadata.tmdb", "metadata.douban", "materials.metadata.imdb", "materials.metadata.tmdb", "materials.metadata.douban"}
    if metadata.get("imdb_ready") is False or metadata.get("tmdb_ready") is False or metadata.get("douban_ready") is False or any(key in all_missing for key in metadata_missing_keys):
        inputs.append(
            _recommended_input(
                "metadata_file",
                ["metadata_file", "imdb_id", "tmdb_id", "tmdb_type", "douban_id", "douban_url", "enrich_metadata", "fetch_ptgen"],
                "IMDb/TMDb/豆瓣 metadata is incomplete; provide IDs/file or let ptcli enrich it before target package preparation",
                stage="materials-metadata",
                blocking_keys=sorted(key for key in metadata_missing_keys if key in all_missing),
                examples={"metadata_file": "/tmp/materials/metadata.json", "imdb_id": "tt1234567", "tmdb_id": "999", "tmdb_type": "movie", "douban_id": "1291546", "fetch_ptgen": True},
            )
        )
    description_missing_keys = {"description.content", "materials.description.ptgen_description", "materials.description.external_ids.tmdb", "materials.description.external_ids.imdb", "materials.description.external_ids.douban"}
    if metadata.get("ptgen_description_ready") is False or target_preflight.get("description_ready") is False or any(key in all_missing for key in description_missing_keys):
        inputs.append(
            _recommended_input(
                "ptgen_description_file",
                ["ptgen_description_file", "metadata_file", "fetch_ptgen", "enrich_metadata", "douban_id", "douban_url"],
                "target description/PTGen content is incomplete",
                stage="materials-description",
                blocking_keys=sorted(key for key in description_missing_keys if key in all_missing),
                examples={"ptgen_description_file": "/tmp/materials/ptgen-description.txt", "fetch_ptgen": True, "douban_url": "https://movie.douban.com/subject/1291546/"},
            )
        )
    if materials.get("mediainfo_or_bdinfo_ready") is False:
        inputs.append(
            _recommended_input(
                "mediainfo_or_bdinfo",
                ["mediainfo_file", "bdinfo_file", "generate_mediainfo", "generate_bdinfo"],
                "MediaInfo or BDInfo evidence is missing",
                stage="materials-media-info",
                blocking_keys=["materials.description.mediainfo_or_bdinfo"],
                examples={"mediainfo_file": "/tmp/materials/MI_FULL_00.txt", "bdinfo_file": "/tmp/materials/BDINFO.txt", "generate_mediainfo": True},
            )
        )
    if materials.get("screenshots_ready") is False:
        inputs.append(
            _recommended_input(
                "screenshot_files",
                ["screenshot_files", "screenshot_file", "generate_screenshots", "screenshot_count"],
                "screenshots are missing or insufficient",
                stage="materials-screenshots",
                blocking_keys=["materials.description.screenshot_bbcode"],
                examples={"screenshot_files": ["/tmp/materials/screen-01.png", "/tmp/materials/screen-02.png"], "generate_screenshots": True, "screenshot_count": "4"},
            )
        )
    image_host_missing_keys = {"assets.image_host_uploads", "materials.assets.image_host_uploads", "description.screenshot_coverage", "materials.description.screenshot_coverage"}
    if any(key in all_missing for key in image_host_missing_keys):
        inputs.append(
            _recommended_input(
                "image_host_file",
                ["image_host_file", "upload_screenshots", "image_host"],
                "hosted screenshot URLs are missing, partial, or stale",
                stage="materials-image-host",
                blocking_keys=sorted(key for key in image_host_missing_keys if key in all_missing),
                examples={"image_host_file": "/tmp/materials/image-host-uploads.json", "upload_screenshots": True, "image_host": "ptpimg"},
            )
        )
    if target_preflight.get("payload_ready") is False and request.get("package_dir"):
        inputs.append(
            _recommended_input(
                "package_dir",
                ["package_dir"],
                "reuse or replace the target upload package directory",
                stage="target-package",
                examples={"package_dir": "/tmp/target/U2-60635-to-MTEAM"},
            )
        )
    return inputs


def _resume_current_flags(argv: list[Any] | None) -> dict[str, Any]:
    argv = [str(item) for item in argv or []]
    return {
        "has_accept_rules": "--accept-rules" in argv,
        "has_confirm_upload": "--confirm-upload" in argv,
        "has_path": "--path" in argv,
        "has_save_path": "--save-path" in argv,
        "has_package_dir": "--package-dir" in argv,
        "has_screenshot_files": "--screenshot-file" in argv,
        "has_uploaded_torrent_file": "--uploaded-torrent-file" in argv,
    }


def _job_resume_plan(job: dict[str, Any]) -> dict[str, Any]:
    job_id = job.get("job_id")
    status = str(job.get("status") or "unknown")
    argv = _resume_argv_from_job(job)
    allowed, reason = _resume_command_allowed(argv)
    available = bool(argv)
    recommended = status in {"blocked", "failed"} and allowed
    endpoint = f"/v1/jobs/{job_id}/resume" if job_id else None
    if status in {"queued", "running"}:
        recommended = False
        if available:
            reason = f"Job is still {status}; wait before resuming."
    return {
        "available": available,
        "allowed": bool(allowed),
        "recommended": recommended,
        "endpoint": endpoint,
        "method": "POST",
        "status": status,
        "subcommand": _ptcli_subcommand(argv or []),
        "next_command_argv": argv,
        "blocker": reason,
        "parent_job_id": job_id,
    }


def _resume_preview(parent: dict[str, Any], request: dict[str, Any], argv: list[str] | None, *, allowed: bool, reason: str | None) -> dict[str, Any]:
    parent_job_id = str(parent.get("job_id") or "")
    blockers = [] if allowed else [reason or "No executable resume command is available."]
    payload = {
        "kind": "ptcli.resume_preview",
        "status": "ok" if allowed else "blocked",
        "ok": bool(allowed),
        "dry_run": True,
        "mutates_state": False,
        "live_upload": "--confirm-upload" in (argv or []),
        "parent_job_id": parent_job_id,
        "parent_status": parent.get("status"),
        "parent_kind": parent.get("kind"),
        "command_argv": argv or [],
        "original_next_command_argv": request.get("original_next_command_argv"),
        "resume_allowed": bool(allowed),
        "resume_blocker": reason,
        "resume_overrides": request.get("resume_overrides"),
        "applied_overrides": request.get("applied_overrides"),
        "ignored_overrides": request.get("ignored_overrides"),
        "material_resolution": request.get("material_resolution"),
        "resume_context": request.get("resume_context"),
        "resume_lineage": request.get("resume_lineage"),
        "resume_audit": _resume_audit_from_request(parent, request, argv, allowed=allowed, reason=reason),
        "resume_plan": _job_resume_plan(parent),
        "resume_requirements": _job_resume_requirements(parent),
        "source_reference": _job_source_reference(parent),
        "workflow_context": _job_workflow_context(parent),
        "blockers": blockers,
        "next_actions": _resume_preview_next_actions(allowed, blockers, parent_job_id),
    }
    payload["agent_decision"] = {
        "decision": "resume_preview" if allowed else "blocked",
        "should_resume": bool(allowed),
        "dry_run": True,
        "mutates_state": False,
        "resume_allowed": bool(allowed),
        "resume_blocker": reason,
        "command_argv": argv or [],
        "blockers": blockers,
        "next_actions": payload["next_actions"],
    }
    return payload


def _resume_audit_from_request(parent: dict[str, Any], request: dict[str, Any], argv: list[str] | None, *, allowed: bool, reason: str | None) -> dict[str, Any]:
    preview_job = {
        "job_id": parent.get("job_id"),
        "kind": parent.get("kind"),
        "status": parent.get("status"),
        "request": {
            **request,
            "resume_lineage": request.get("resume_lineage"),
            "resume_context": request.get("resume_context"),
        },
        "result": {"next_command_argv": argv},
    }
    audit = _job_resume_audit(preview_job)
    audit.update(
        {
            "kind": "ptcli.resume_preview_audit",
            "dry_run": True,
            "resume_allowed": bool(allowed),
            "resume_blocker": reason,
            "next_command_argv": argv or [],
        }
    )
    return audit


def _resume_preview_next_actions(allowed: bool, blockers: list[str], parent_job_id: str) -> list[str]:
    if allowed:
        return [f"Review command_argv, then call /v1/jobs/{parent_job_id}/resume without dry_run when ready."]
    if blockers:
        return ["Resolve resume_preview.blockers before executing resume."]
    return ["Inspect resume_preview before executing resume."]


def _resume_lineage(parent: dict[str, Any], *, parent_job_id: str, argv: list[str] | None, allowed: bool, reason: str | None) -> dict[str, Any]:
    parent_workflow_context = _job_workflow_context(parent)
    parent_duplicate_check = _job_duplicate_check(parent)
    return {
        "parent_job_id": parent_job_id,
        "parent_kind": parent.get("kind"),
        "parent_status": parent.get("status"),
        "parent_summary_file": _job_summary_file(parent),
        "parent_source_reference": _job_source_reference(parent),
        "parent_target_trackers": (parent.get("request") or {}).get("target_trackers") if isinstance(parent.get("request"), dict) else None,
        "parent_duplicate_check": parent_duplicate_check,
        "parent_workflow_context": {
            "workflow": parent_workflow_context.get("workflow"),
            "mode": parent_workflow_context.get("mode"),
            "status": parent_workflow_context.get("status"),
            "source_reference": parent_workflow_context.get("source_reference"),
            "target_trackers": parent_workflow_context.get("target_trackers"),
            "summary_file": parent_workflow_context.get("summary_file"),
            "gates": parent_workflow_context.get("gates"),
        },
        "parent_materials_handoff": _job_materials_handoff(parent),
        "inherited_policy": {
            "policy_coverage": _job_policy_coverage(parent),
            "policy_qbit_defaults": _job_policy_qbit_defaults(parent),
        },
        "resume_allowed": allowed,
        "resume_blocker": reason,
        "next_command_argv": argv,
        "next_subcommand": _ptcli_subcommand(argv or []),
    }


def _resume_context(parent: dict[str, Any], *, parent_job_id: str, argv: list[str] | None, allowed: bool, reason: str | None) -> dict[str, Any]:
    lineage = _resume_lineage(parent, parent_job_id=parent_job_id, argv=argv, allowed=allowed, reason=reason)
    return {
        "parent_job_id": parent_job_id,
        "parent_kind": parent.get("kind"),
        "parent_status": parent.get("status"),
        "resume_allowed": allowed,
        "resume_blocker": reason,
        "next_command_argv": argv,
        "parent_summary_file": _job_summary_file(parent),
        "parent_source_reference": lineage.get("parent_source_reference"),
        "parent_workflow_context": lineage.get("parent_workflow_context"),
        "parent_materials_handoff": lineage.get("parent_materials_handoff"),
        "parent_next_actions": _string_list(parent.get("next_actions")),
        "inherited_policy": lineage.get("inherited_policy"),
    }


def _resume_material_resolution(parent: dict[str, Any], override_result: dict[str, Any]) -> dict[str, Any] | None:
    materials_handoff = _job_materials_handoff(parent)
    if not isinstance(materials_handoff, dict):
        return None
    recommended_inputs = materials_handoff.get("recommended_inputs") if isinstance(materials_handoff.get("recommended_inputs"), list) else []
    applied_keys = [
        str(item.get("key"))
        for item in override_result.get("applied", [])
        if isinstance(item, dict) and item.get("key")
    ]
    ignored_keys = [
        str(item.get("key"))
        for item in override_result.get("ignored", [])
        if isinstance(item, dict) and item.get("key")
    ]
    covered: list[str] = []
    unresolved: list[dict[str, Any]] = []
    for item in recommended_inputs:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        accepted_keys = [str(value) for value in _string_list(item.get("accepted_keys"))]
        if key in applied_keys or any(accepted_key in applied_keys for accepted_key in accepted_keys):
            covered.append(key)
        else:
            unresolved.append(item)
    return {
        "kind": "ptcli.resume_material_resolution",
        "ready_before_resume": materials_handoff.get("ready"),
        "parent_materials_handoff": materials_handoff,
        "recommended_inputs": recommended_inputs,
        "applied_override_keys": applied_keys,
        "ignored_override_keys": ignored_keys,
        "covered_recommended_inputs": covered,
        "unresolved_recommended_inputs": unresolved,
        "blockers_before_resume": _string_list(materials_handoff.get("blockers")),
        "next_actions": _resume_material_resolution_next_actions(unresolved, applied_keys, ignored_keys),
    }


def _resume_material_resolution_next_actions(unresolved: list[dict[str, Any]], applied_keys: list[str], ignored_keys: list[str]) -> list[str]:
    if unresolved:
        keys = ", ".join(str(item.get("key")) for item in unresolved if isinstance(item, dict) and item.get("key"))
        return [f"Resume was accepted, but these recommended material inputs were not provided yet: {keys}."]
    if applied_keys:
        return ["Resume command includes material overrides; poll the resumed job and inspect materials_handoff/target_preflight for validation."]
    if ignored_keys:
        return ["Resume ignored some provided material overrides; inspect ignored_override_keys and retry with allowed override names."]
    return []


def _agent_candidate_decision(
    job: dict[str, Any],
    result: dict[str, Any],
    request: dict[str, Any],
    status: str,
    blockers: list[str],
    next_actions: list[str],
) -> dict[str, Any] | None:
    if str(job.get("kind") or "") != "ptcli.daily_candidates" and not _candidate_payload(result):
        return None
    digest = _candidate_digest_from_payload(result) or {}
    top_submit_request = digest.get("top_submit_request") if isinstance(digest.get("top_submit_request"), dict) else None
    top_candidate = digest.get("top_candidate") if isinstance(digest.get("top_candidate"), dict) else {}
    top_policy_summary = top_candidate.get("policy_summary") if isinstance(top_candidate.get("policy_summary"), dict) else {}
    policy_coverage = top_policy_summary.get("policy_coverage") if isinstance(top_policy_summary.get("policy_coverage"), dict) else {}
    policy_coverage_ready = policy_coverage.get("ready") if isinstance(policy_coverage.get("ready"), bool) else None
    ready_count = int(digest.get("ready_count") or 0)
    if status in {"queued", "running"}:
        decision = "wait"
        stop_reason = None
        recommended_action = "Poll get_job_status until the daily candidate job is complete."
    elif top_submit_request and ready_count > 0 and policy_coverage_ready is False:
        decision = "configure_policy"
        stop_reason = "policy_coverage_incomplete"
        recommendations = _string_list(policy_coverage.get("recommendations"))
        recommended_action = recommendations[0] if recommendations else "Complete policy_coverage missing fields before submitting the top candidate."
    elif top_submit_request and ready_count > 0:
        decision = "submit_candidate_when_confirmed"
        stop_reason = None
        recommended_action = "Review digest.top_candidate and site rules, add confirm_upload=true plus a save_path or path, then submit digest.top_submit_request to source_url_retorrent_job."
    elif blockers or digest.get("recommendation") == "resolve_blockers":
        decision = "blocked"
        stop_reason = "candidate_blockers"
        recommended_action = next_actions[0] if next_actions else "Resolve digest.blockers before submitting any candidate."
    else:
        decision = "inspect"
        stop_reason = "no_ready_candidate"
        recommended_action = "Inspect digest.push_items and rerun later or adjust source/target filters."
    return {
        "workflow": job.get("kind"),
        "status": status,
        "decision": decision,
        "recommended_action": recommended_action,
        "stop_reason": stop_reason,
        "candidate_digest": digest,
        "top_submit_request": top_submit_request,
        "top_submit_job_endpoint": digest.get("top_submit_job_endpoint"),
        "top_submit_tool": digest.get("top_submit_tool"),
        "ready_count": ready_count,
        "policy_coverage": policy_coverage or None,
        "policy_coverage_ready": policy_coverage_ready,
        "runtime": _job_runtime(job),
        "missing_confirmations": _missing_live_confirmations(top_submit_request or request),
        "can_submit_job": bool(top_submit_request and ready_count > 0 and policy_coverage_ready is not False),
        "should_poll": status in {"queued", "running"},
        "should_resume": False,
        "resume_available": False,
        "summary_file": job.get("summary_file"),
        "blocker_count": len(blockers),
    }


def _agent_decision(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    status = str(job.get("status") or "unknown")
    blockers = _string_list(job.get("blockers"))
    next_actions = _string_list(job.get("next_actions"))
    candidate_decision = _agent_candidate_decision(job, result, request, status, blockers, next_actions)
    if candidate_decision:
        return candidate_decision
    duplicate_check = _job_duplicate_check(job)
    next_command_argv = _result_next_command_argv(result)
    resume_context = _job_resume_context(job)
    resume_lineage = _job_resume_lineage(job)
    material_resolution = _job_material_resolution(job)
    source_reference = _job_source_reference(job)
    workflow_context = _job_workflow_context(job)
    manual_retorrent_handoff = _job_manual_retorrent_handoff(job)
    materials_handoff = _job_materials_handoff(job)
    target_upload_handoff = _job_target_upload_handoff(job)
    closure_handoff = _job_closure_handoff(job)
    candidate_submission_handoff = _job_candidate_submission_handoff(job)
    candidate_submission_summary = _job_candidate_submission_summary(job)
    candidate_submission_execution = candidate_submission_summary.get("execution_handoff") if isinstance(candidate_submission_summary, dict) and isinstance(candidate_submission_summary.get("execution_handoff"), dict) else None
    resume_plan = _job_resume_plan(job)
    resume_summary = _job_resume_summary(job)
    submit_if_clear_handoff = _job_submit_if_clear_handoff(job)
    missing_confirmations = _missing_live_confirmations(request)
    policy_coverage = _job_policy_coverage(job) if request.get("execute") is True or request.get("execute_if_no_duplicate") is True or request.get("mode") == "manual_retorrent" else None
    policy_handoff = _job_policy_handoff(job) if policy_coverage is not None else None
    policy_qbit_defaults = _job_policy_qbit_defaults(job) if request.get("execute") is True or request.get("execute_if_no_duplicate") is True or request.get("mode") == "manual_retorrent" else None
    qbit_plan = _job_qbit_plan(job) if policy_qbit_defaults is not None else None
    qbit_limit_audit = _job_qbit_limit_audit(job) if qbit_plan is not None else None
    qbit_handoff = _job_qbit_handoff(job) if qbit_plan is not None else None
    qbit_enforcement_summary = _job_qbit_enforcement_summary(job) if qbit_plan is not None else None
    policy_coverage_ready = policy_coverage.get("ready") if isinstance(policy_coverage, dict) and isinstance(policy_coverage.get("ready"), bool) else None
    policy_coverage_incomplete = policy_coverage_ready is False
    duplicate_exists = duplicate_check.get("exists") is True
    resume_available = bool(resume_plan.get("available"))
    resume_requirements = _job_resume_requirements(job)
    should_poll = status in {"queued", "running"}
    should_resume = bool(resume_plan.get("recommended")) and not duplicate_exists and not missing_confirmations and not policy_coverage_incomplete
    can_attempt_live = not duplicate_exists and not missing_confirmations and not policy_coverage_incomplete and status not in {"queued", "running", "cancelled"}

    if should_poll:
        decision = "wait"
        stop_reason = None
        recommended_action = "Poll get_job_status until the job is no longer queued or running."
    elif duplicate_exists:
        decision = "stop"
        stop_reason = "target_duplicate_exists"
        recommended_action = "Do not upload. Inspect duplicate_check.dupes or choose a different target."
    elif missing_confirmations:
        decision = "ask_confirmation"
        stop_reason = "missing_required_confirmation"
        recommended_action = f"Collect explicit confirmation for: {', '.join(missing_confirmations)}."
    elif _is_retorrent_check_job(job) and isinstance(submit_if_clear_handoff, dict) and submit_if_clear_handoff.get("ready"):
        decision = "submit_if_clear"
        stop_reason = None
        recommended_action = "Target duplicate check is clear; submit submit_if_clear_handoff.request to source_url_retorrent_job after reviewing site rules."
    elif status == "complete":
        decision = "done"
        stop_reason = None
        recommended_action = "Retorrent workflow is complete; inspect summary_file and seeding evidence."
    elif status == "cancelled":
        decision = "stop"
        stop_reason = "job_cancelled"
        recommended_action = "Submit a new job if the work is still needed; cancelled jobs are terminal."
    elif policy_coverage_incomplete:
        decision = "configure_policy"
        stop_reason = "policy_coverage_incomplete"
        recommendations = _string_list(policy_coverage.get("recommendations")) if isinstance(policy_coverage, dict) else []
        recommended_action = recommendations[0] if recommendations else "Complete policy_coverage missing fields before attempting live retorrent automation."
    elif should_resume:
        decision = "resume"
        stop_reason = None
        recommended_action = "Call resume_job or run the allowlisted next_command_argv."
    elif blockers:
        decision = "blocked"
        stop_reason = "blocked_by_runtime_or_gate"
        recommended_action = next_actions[0] if next_actions else "Inspect blockers and resolve the missing runtime, credential, rule, metadata, or qBittorrent evidence."
    else:
        decision = "inspect"
        stop_reason = "unknown_state"
        recommended_action = "Inspect result, summary_file, and logs before taking live action."

    return {
        "workflow": job.get("kind"),
        "status": status,
        "decision": decision,
        "recommended_action": recommended_action,
        "stop_reason": stop_reason,
        "duplicate_check": duplicate_check,
        "submit_if_clear_handoff": submit_if_clear_handoff,
        "missing_confirmations": missing_confirmations,
        "policy_coverage": policy_coverage,
        "policy_handoff": policy_handoff,
        "policy_qbit_defaults": policy_qbit_defaults,
        "qbit_plan": qbit_plan,
        "qbit_limit_audit": qbit_limit_audit,
        "qbit_handoff": qbit_handoff,
        "qbit_enforcement_summary": qbit_enforcement_summary,
        "materials_handoff": materials_handoff,
        "target_upload_handoff": target_upload_handoff,
        "closure_handoff": closure_handoff,
        "policy_coverage_ready": policy_coverage_ready,
        "runtime": _job_runtime(job),
        "cancellation": job.get("cancellation") if isinstance(job.get("cancellation"), dict) else None,
        "can_attempt_live": can_attempt_live,
        "should_poll": should_poll,
        "should_resume": should_resume,
        "resume_available": resume_available,
        "resume_plan": resume_plan,
        "resume_requirements": resume_requirements,
        "resume_summary": resume_summary,
        "resume_lineage": resume_lineage,
        "resume_context": resume_context,
        "material_resolution": material_resolution,
        "candidate_submission_execution": candidate_submission_execution,
        "material_input_template": candidate_submission_execution.get("material_input_template") if isinstance(candidate_submission_execution, dict) else None,
        "candidate_submission": _job_candidate_submission(job),
        "check_submission": _job_check_submission(job),
        "candidate_submission_summary": candidate_submission_summary,
        "source_reference": source_reference,
        "workflow_context": workflow_context,
        "manual_retorrent_handoff": manual_retorrent_handoff,
        "candidate_submission_handoff": candidate_submission_handoff,
        "next_command_argv": next_command_argv,
        "summary_file": job.get("summary_file"),
        "blocker_count": len(blockers),
    }


def _job_duplicate_check(job: dict[str, Any]) -> dict[str, Any]:
    duplicate_check = job.get("duplicate_check")
    if isinstance(duplicate_check, dict):
        return duplicate_check
    result = job.get("result")
    if isinstance(result, dict):
        nested_duplicate_check = result.get("duplicate_check")
        if isinstance(nested_duplicate_check, dict):
            return nested_duplicate_check
        return _duplicate_check(result)
    return {"searched": False, "status": "unknown", "exists": None, "count": None, "dupes": []}


def _is_retorrent_check_job(job: dict[str, Any]) -> bool:
    kind = str(job.get("kind") or "")
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    return kind in {"ptcli.retorrent_check"} or (kind == "ptcli.retorrent" and request.get("execute") is not True)


def _missing_live_confirmations(request: dict[str, Any]) -> list[str]:
    if request.get("execute") is not True and request.get("execute_if_no_duplicate") is not True and request.get("mode") != "manual_retorrent":
        return []
    missing: list[str] = []
    if request.get("accept_rules") is not True:
        missing.append("accept_rules=true")
    if request.get("confirm_upload") is not True:
        missing.append("confirm_upload=true")
    return missing


def _job_status_from_result(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "")
    if status == "error":
        return "failed"
    if status == "blocked":
        return "blocked"
    if result.get("ok") is False and _result_blockers(result):
        return "blocked"
    return "complete"


def _result_summary_file(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    summary_file = result.get("summary_file")
    if summary_file:
        return str(summary_file)
    nested = result.get("result")
    if isinstance(nested, dict) and nested.get("summary_file"):
        return str(nested["summary_file"])
    pipeline = result.get("pipeline")
    if isinstance(pipeline, dict) and pipeline.get("summary_file"):
        return str(pipeline["summary_file"])
    return None


def _job_summary_file(job: dict[str, Any]) -> str | None:
    summary_file = job.get("summary_file")
    if summary_file:
        return str(summary_file)
    return _result_summary_file(job.get("result"))


def _result_resume_state(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    resume_state = result.get("resume_state")
    if isinstance(resume_state, dict):
        return resume_state
    nested = result.get("result")
    if isinstance(nested, dict) and isinstance(nested.get("resume_state"), dict):
        return nested["resume_state"]
    pipeline = result.get("pipeline")
    if isinstance(pipeline, dict) and isinstance(pipeline.get("resume_state"), dict):
        return pipeline["resume_state"]
    return None


def _result_next_command_argv(result: Any) -> list[str] | None:
    if not isinstance(result, dict):
        return None
    for candidate in (
        result.get("next_command_argv"),
        (result.get("resume_state") or {}).get("next_command_argv") if isinstance(result.get("resume_state"), dict) else None,
    ):
        argv = _argv_list(candidate)
        if argv:
            return argv
    nested = result.get("result")
    if isinstance(nested, dict):
        argv = _result_next_command_argv(nested)
        if argv:
            return argv
    pipeline = result.get("pipeline")
    if isinstance(pipeline, dict):
        argv = _result_next_command_argv(pipeline)
        if argv:
            return argv
    return None


def _resume_argv_from_job(job: dict[str, Any]) -> list[str] | None:
    result = job.get("result")
    argv = _result_next_command_argv(result)
    if argv:
        return argv
    resume_state = job.get("resume_state")
    if isinstance(resume_state, dict):
        return _argv_list(resume_state.get("next_command_argv"))
    return None


def _apply_resume_overrides(argv: list[str] | None, overrides: dict[str, Any]) -> dict[str, Any]:
    provided = {key: value for key, value in overrides.items() if key not in {"job_id", "dry_run"}} if isinstance(overrides, dict) else {}
    patched = list(argv or [])
    applied: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    if not patched:
        return {"argv": None, "provided": provided, "applied": applied, "ignored": [{"key": key, "reason": "no_resume_command"} for key in provided]}

    for key, value in provided.items():
        if key in RESUME_BOOLEAN_FLAG_OVERRIDES:
            flag = RESUME_BOOLEAN_FLAG_OVERRIDES[key]
            if _truthy(value):
                patched = _argv_ensure_boolean_flag(patched, flag)
                applied.append({"key": key, "flag": flag, "value": True})
            else:
                ignored.append({"key": key, "flag": flag, "reason": "boolean override must be true to add a live-action flag"})
        elif key in RESUME_VALUE_FLAG_OVERRIDES:
            flag = RESUME_VALUE_FLAG_OVERRIDES[key]
            if value in (None, ""):
                ignored.append({"key": key, "flag": flag, "reason": "empty value"})
            else:
                patched = _argv_set_value_flag(patched, flag, str(value))
                applied.append({"key": key, "flag": flag, "value": str(value)})
        elif key in RESUME_REPEATABLE_FLAG_OVERRIDES:
            flag = RESUME_REPEATABLE_FLAG_OVERRIDES[key]
            values = _list_value(value)
            if not values:
                ignored.append({"key": key, "flag": flag, "reason": "empty repeatable value"})
            else:
                patched = _argv_set_repeatable_flag(patched, flag, [str(item) for item in values])
                applied.append({"key": key, "flag": flag, "value": [str(item) for item in values]})
        else:
            ignored.append({"key": key, "reason": "override is not allowlisted for resume commands"})
    return {"argv": patched, "provided": provided, "applied": applied, "ignored": ignored}


def _argv_ensure_boolean_flag(argv: list[str], flag: str) -> list[str]:
    return argv if flag in argv else [*argv, flag]


def _argv_set_value_flag(argv: list[str], flag: str, value: str) -> list[str]:
    cleaned = _argv_without_flag(argv, flag, has_value=True)
    return [*cleaned, flag, value]


def _argv_set_repeatable_flag(argv: list[str], flag: str, values: list[str]) -> list[str]:
    cleaned = _argv_without_flag(argv, flag, has_value=True)
    for value in values:
        cleaned.extend([flag, value])
    return cleaned


def _argv_without_flag(argv: list[str], flag: str, *, has_value: bool) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == flag:
            index += 2 if has_value and index + 1 < len(argv) else 1
            continue
        result.append(item)
        index += 1
    return result


def _argv_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    argv = [str(item) for item in value if str(item)]
    return argv or None


def _resume_command_allowed(argv: list[str] | None) -> tuple[bool, str | None]:
    if not argv:
        return False, "No executable resume command is available for this job."
    if any("\x00" in item for item in argv):
        return False, "Resume command contains invalid null bytes."
    subcommand = _ptcli_subcommand(argv)
    if not subcommand:
        return False, "Resume command is not a recognized ptcli invocation."
    if subcommand not in RESUME_COMMAND_ALLOWLIST:
        return False, f"Resume command subcommand is not allowlisted: {subcommand}"
    return True, None


def _ptcli_subcommand(argv: list[str]) -> str | None:
    if len(argv) >= 2 and Path(argv[0]).name == "ptcli":
        return argv[1]
    if len(argv) >= 3 and Path(argv[1]).name == "ptcli.py":
        return argv[2]
    return None


def _run_resume_command(argv: list[str], *, parent_job_id: str) -> dict[str, Any]:
    started_at = time.time()
    completed = subprocess.run(argv, cwd=os.getcwd(), capture_output=True, text=True)
    parsed_stdout = _parse_json_stdout(completed.stdout)
    status = "ok" if completed.returncode == 0 else "blocked"
    blockers = [] if completed.returncode == 0 else [f"Resume command exited with code {completed.returncode}."]
    if isinstance(parsed_stdout, dict):
        blockers.extend(_result_blockers(parsed_stdout))
    if completed.stderr.strip():
        blockers.append(completed.stderr.strip())
    return {
        "kind": "ptcli.service.resume",
        "status": status,
        "ok": completed.returncode == 0,
        "parent_job_id": parent_job_id,
        "command_argv": argv,
        "returncode": completed.returncode,
        "stdout_json": parsed_stdout,
        "stdout": completed.stdout if parsed_stdout is None else None,
        "stderr": completed.stderr,
        "blockers": blockers,
        "next_actions": _string_list(parsed_stdout.get("next_actions")) if isinstance(parsed_stdout, dict) else [],
        "summary_file": _result_summary_file(parsed_stdout) if isinstance(parsed_stdout, dict) else None,
        "resume_state": _result_resume_state(parsed_stdout) if isinstance(parsed_stdout, dict) else None,
        "next_command_argv": _result_next_command_argv(parsed_stdout) if isinstance(parsed_stdout, dict) else None,
        "elapsed_seconds": round(time.time() - started_at, 3),
        "result": parsed_stdout,
    }


def _parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _nested_value(payload: Any, key: str) -> Any:
    if not isinstance(payload, dict):
        return None
    if key in payload:
        return payload[key]
    nested = payload.get("result")
    if isinstance(nested, dict) and key in nested:
        return nested[key]
    pipeline = payload.get("pipeline")
    if isinstance(pipeline, dict) and key in pipeline:
        return pipeline[key]
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]


def health_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "ptcli",
        "api": "v1",
        "time": int(time.time()),
    }


def deployment_check_payload(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a local deployment readiness report without touching trackers or qBittorrent."""
    request = request or {}
    base_dir = Path(str(request.get("base_dir") or os.environ.get("PTCLI_BASE_DIR") or os.getcwd())).expanduser()
    config_path = _deployment_path(request.get("config") or os.environ.get("PTCLI_CONFIG") or "data/config.py", base_dir)
    cookies_dir = _deployment_path(request.get("cookies_dir") or "data/cookies", base_dir)
    tmp_dir = Path(os.environ.get("TMPDIR") or str(base_dir / "tmp")).expanduser()
    job_dir = _resolve_job_dir(request.get("job_dir") or os.environ.get("PTCLI_JOB_DIR"))
    max_concurrent_jobs = _resolve_max_concurrent_jobs(request.get("max_concurrent_jobs"))
    downloads_path = Path(str(request.get("downloads_path") or os.environ.get("PTCLI_DOWNLOADS_PATH") or "/downloads")).expanduser()
    client = str(request.get("client") or "default")
    compose_path = _deployment_path(request.get("compose_file") or os.environ.get("PTCLI_COMPOSE_FILE") or "docker-compose.yml", base_dir)

    runtime_check = build_runtime_dependency_check()
    checks: list[dict[str, Any]] = [
        {
            "name": "runtime.ptcli_dependencies",
            "ok": bool(runtime_check.get("ok")),
            "message": runtime_check.get("message"),
            "details": runtime_check,
        },
        _deployment_file_check("config", config_path, required=True),
        _deployment_dir_check("cookies_dir", cookies_dir, required=True),
        _deployment_dir_check("tmp_dir", tmp_dir, required=True, writable=True),
        _deployment_dir_check("job_dir", job_dir, required=True, writable=True),
        _deployment_dir_check("downloads_path", downloads_path, required=True),
        _deployment_api_token_check(),
    ]

    config: dict[str, Any] | None = None
    qbit: dict[str, Any] = {"configured": False, "client": client, "connectivity_checked": False}
    if checks[1]["ok"]:
        try:
            config = load_config(str(config_path))
            checks.append({"name": "config.load", "ok": True, "message": f"Config loaded: {config_path}"})
        except Exception as exc:
            checks.append({"name": "config.load", "ok": False, "message": f"Config could not be loaded: {exc}"})
    if config is not None:
        try:
            resolved_client, qbit_config = resolve_client_config(config, client)
            qbit = {
                "configured": True,
                "client": resolved_client,
                "torrent_client": qbit_config.get("torrent_client"),
                "qbit_url": qbit_config.get("qbit_url"),
                "qbit_port": qbit_config.get("qbit_port"),
                "qui_proxy_configured": bool(qbit_config.get("qui_proxy_url")),
                "connectivity_checked": False,
            }
            checks.append({"name": "qbit.config", "ok": True, "message": f"qBittorrent client config is present: {resolved_client}", "details": qbit})
        except Exception as exc:
            checks.append({"name": "qbit.config", "ok": False, "message": f"qBittorrent client config is not ready: {exc}", "details": qbit})

    daily_candidate_plan = _deployment_daily_candidate_plan(request)
    docker_compose = _deployment_docker_compose_summary(compose_path)
    checks.append(
        {
            "name": "automation.daily_candidates",
            "ok": bool(daily_candidate_plan.get("configured")) and not bool(daily_candidate_plan.get("blockers")),
            "blocking": False,
            "message": _deployment_daily_candidate_message(daily_candidate_plan),
            "details": daily_candidate_plan,
        }
    )
    checks.append(
        {
            "name": "docker.compose_daily_schedule",
            "ok": bool(docker_compose.get("daily_scheduler_service_ready") or docker_compose.get("daily_schedule_service_ready")),
            "blocking": False,
            "message": _deployment_docker_compose_message(docker_compose),
            "path": str(compose_path),
            "details": docker_compose,
        }
    )
    checks.append(
        {
            "name": "docker.compose_ptcli_api",
            "ok": bool(docker_compose.get("ptcli_api_service_ready")),
            "blocking": False,
            "message": _deployment_docker_compose_api_message(docker_compose),
            "path": str(compose_path),
            "details": docker_compose,
        }
    )

    blockers = [str(check.get("message")) for check in checks if check.get("ok") is False and check.get("blocking", True) is not False]
    warnings = [str(check.get("message")) for check in checks if check.get("ok") is False and check.get("blocking") is False]
    next_actions = _deployment_next_actions(checks)
    ready = not blockers
    paths = {
        "base_dir": str(base_dir),
        "config": str(config_path),
        "cookies_dir": str(cookies_dir),
        "tmp_dir": str(tmp_dir),
        "job_dir": str(job_dir),
        "downloads_path": str(downloads_path),
        "compose_file": str(compose_path),
    }
    mounts = _deployment_mount_summary(checks)
    agent_summary = _deployment_agent_summary(ready, checks, paths, mounts, qbit, daily_candidate_plan, docker_compose)
    deployment_handoff = _deployment_runtime_handoff(agent_summary, paths, qbit, daily_candidate_plan, docker_compose, blockers, warnings)
    return {
        "kind": "ptcli.deployment_check",
        "status": "ok" if ready else "blocked",
        "ok": ready,
        "ready": ready,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "next_actions": next_actions,
        "paths": paths,
        "mounts": mounts,
        "queue": {"max_concurrent_jobs": max_concurrent_jobs},
        "qbit": qbit,
        "daily_candidates": daily_candidate_plan,
        "docker_compose": docker_compose,
        "deployment_handoff": deployment_handoff,
        "agent_summary": agent_summary,
        "agent_handoff": _deployment_agent_handoff(agent_summary, paths, qbit, daily_candidate_plan, docker_compose, blockers, warnings),
        "connectivity_checked": False,
    }


def _deployment_path(path: Any, base_dir: Path) -> Path:
    candidate = Path(str(path)).expanduser()
    return candidate if candidate.is_absolute() else base_dir / candidate


def _deployment_file_check(name: str, path: Path, *, required: bool) -> dict[str, Any]:
    exists = path.is_file()
    ok = exists or not required
    return {
        "name": f"path.{name}",
        "ok": ok,
        "blocking": required,
        "path": str(path),
        "message": f"{name} file is present: {path}" if exists else f"{name} file is missing: {path}",
    }


def _deployment_dir_check(name: str, path: Path, *, required: bool, writable: bool = False) -> dict[str, Any]:
    exists = path.is_dir()
    is_writable = os.access(path, os.W_OK) if exists else False
    ok = (exists or not required) and (not writable or is_writable)
    if not exists:
        message = f"{name} directory is missing: {path}"
    elif writable and not is_writable:
        message = f"{name} directory is not writable: {path}"
    else:
        message = f"{name} directory is ready: {path}"
    return {
        "name": f"path.{name}",
        "ok": ok,
        "blocking": required,
        "path": str(path),
        "exists": exists,
        "writable": is_writable,
        "message": message,
    }


def _deployment_api_token_check() -> dict[str, Any]:
    configured = bool(os.environ.get("PTCLI_API_TOKEN"))
    return {
        "name": "security.api_token",
        "ok": configured,
        "blocking": False,
        "configured": configured,
        "message": "PTCLI_API_TOKEN is configured." if configured else "PTCLI_API_TOKEN is not configured; keep the API bound to localhost or set a token before exposing it.",
    }


def _deployment_daily_candidate_plan(request: dict[str, Any]) -> dict[str, Any]:
    schedule_request: dict[str, Any] = {}
    if "schedules" in request:
        schedule_request["schedules"] = request.get("schedules")
    elif "daily_candidate_schedules" in request:
        schedule_request["schedules"] = request.get("daily_candidate_schedules")
    try:
        plan = daily_candidate_schedule_payload(schedule_request)
    except ServiceError as exc:
        return {
            "configured": False,
            "status": "blocked",
            "ok": False,
            "source": "request" if schedule_request else "env",
            "env": DAILY_CANDIDATE_SCHEDULE_ENV,
            "count": 0,
            "schedules": [],
            "blockers": [str(exc)],
            "next_actions": [f"Fix {DAILY_CANDIDATE_SCHEDULE_ENV} JSON or POST valid schedules to /v1/candidates/daily/schedule."],
        }
    return {
        "configured": bool(plan.get("count")),
        "status": plan.get("status"),
        "ok": bool(plan.get("ok")),
        "source": plan.get("source"),
        "env": plan.get("env"),
        "count": plan.get("count", 0),
        "schedules": plan.get("schedules", []),
        "blockers": _string_list(plan.get("blockers")),
        "next_actions": _string_list(plan.get("next_actions")),
    }


def _deployment_daily_candidate_message(plan: dict[str, Any]) -> str:
    if plan.get("configured") and not plan.get("blockers"):
        return f"Daily candidate schedules are configured: {plan.get('count', 0)}."
    if plan.get("configured"):
        return f"Daily candidate schedules need attention: {', '.join(_string_list(plan.get('blockers')))}"
    return f"No daily candidate schedules configured; set {DAILY_CANDIDATE_SCHEDULE_ENV} when daily push jobs are needed."


def _deployment_docker_compose_summary(compose_path: Path) -> dict[str, Any]:
    exists = compose_path.is_file()
    text = ""
    if exists:
        try:
            text = compose_path.read_text(encoding="utf-8")
        except OSError as exc:
            return {
                "present": True,
                "readable": False,
                "path": str(compose_path),
                "error": str(exc),
                "ptcli_api_service": False,
                "daily_schedule_service": False,
                "daily_scheduler_service": False,
                "daily_profile": False,
                "daily_schedule_command": False,
                "daily_scheduler_command": False,
                "daily_schedule_service_ready": False,
                "daily_scheduler_service_ready": False,
                "ptcli_api_service_ready": False,
            }
    scheduler_command = 'command: ["daily-scheduler", "--summary-output-dir", "/Upload-Assistant/tmp/daily-candidates", "--write-notification", "--json"]'
    schedule_command = 'command: ["daily-schedule", "--write-summary", "--summary-output-dir", "/Upload-Assistant/tmp/daily-candidates", "--write-notification", "--json"]'
    return {
        "present": exists,
        "readable": exists,
        "path": str(compose_path),
        "ptcli_api_service": "ptcli-api:" in text,
        "ptcli_api_command": 'command: ["serve", "--host", "0.0.0.0", "--port", "8080"]' in text,
        "ptcli_api_healthcheck": "healthcheck:" in text and "http://127.0.0.1:8080/health" in text,
        "ptcli_api_localhost_port": '"127.0.0.1:8080:8080"' in text or "'127.0.0.1:8080:8080'" in text,
        "ptcli_api_token_env": "PTCLI_API_TOKEN=${PTCLI_API_TOKEN:-}" in text,
        "ptcli_public_base_url_env": "PTCLI_PUBLIC_BASE_URL=${PTCLI_PUBLIC_BASE_URL:-" in text,
        "ptcli_job_dir_env": "PTCLI_JOB_DIR=/Upload-Assistant/tmp/ptcli-jobs" in text,
        "host_gateway": "host.docker.internal:host-gateway" in text,
        "downloads_mount": ":/downloads/" in text or ":/downloads:" in text,
        "config_mount": ":/Upload-Assistant/data/config.py:" in text,
        "cookies_mount": ":/Upload-Assistant/data/cookies/" in text or ":/Upload-Assistant/data/cookies:" in text,
        "tmp_mount": ":/Upload-Assistant/tmp/" in text or ":/Upload-Assistant/tmp:" in text,
        "daily_schedule_service": "ptcli-daily-schedule:" in text,
        "daily_scheduler_service": "ptcli-daily-scheduler:" in text,
        "daily_profile": "- daily" in text,
        "daily_schedule_command": schedule_command in text,
        "daily_scheduler_command": scheduler_command in text,
        "ptcli_api_service_ready": all(
            (
                exists,
                "ptcli-api:" in text,
                'command: ["serve", "--host", "0.0.0.0", "--port", "8080"]' in text,
                "healthcheck:" in text,
                "http://127.0.0.1:8080/health" in text,
                ("\"127.0.0.1:8080:8080\"" in text or "'127.0.0.1:8080:8080'" in text),
                "PTCLI_API_TOKEN=${PTCLI_API_TOKEN:-}" in text,
                "PTCLI_JOB_DIR=/Upload-Assistant/tmp/ptcli-jobs" in text,
                "host.docker.internal:host-gateway" in text,
                (":/downloads/" in text or ":/downloads:" in text),
                ":/Upload-Assistant/data/config.py:" in text,
                (":/Upload-Assistant/data/cookies/" in text or ":/Upload-Assistant/data/cookies:" in text),
                (":/Upload-Assistant/tmp/" in text or ":/Upload-Assistant/tmp:" in text),
            )
        ),
        "daily_schedule_service_ready": all(
            (
                exists,
                "ptcli-api:" in text,
                "ptcli-daily-schedule:" in text,
                "- daily" in text,
                schedule_command in text,
            )
        ),
        "daily_scheduler_service_ready": all(
            (
                exists,
                "ptcli-api:" in text,
                "ptcli-daily-scheduler:" in text,
                "- daily" in text,
                scheduler_command in text,
            )
        ),
    }


def _deployment_docker_compose_api_message(summary: dict[str, Any]) -> str:
    path = summary.get("path")
    if summary.get("ptcli_api_service_ready"):
        return f"Docker Compose ptcli-api service is configured for local AI access: {path}"
    if not summary.get("present"):
        return f"docker-compose.yml is not present at {path}; skip this warning if not using Docker Compose."
    if not summary.get("readable"):
        return f"docker-compose.yml could not be read at {path}: {summary.get('error')}"
    missing = [
        name
        for name, ready in (
            ("ptcli-api service", summary.get("ptcli_api_service")),
            ("serve command", summary.get("ptcli_api_command")),
            ("healthcheck", summary.get("ptcli_api_healthcheck")),
            ("127.0.0.1 API port binding", summary.get("ptcli_api_localhost_port")),
            ("PTCLI_API_TOKEN env", summary.get("ptcli_api_token_env")),
            ("PTCLI_JOB_DIR env", summary.get("ptcli_job_dir_env")),
            ("host.docker.internal host-gateway", summary.get("host_gateway")),
            ("/downloads mount", summary.get("downloads_mount")),
            ("data/config.py mount", summary.get("config_mount")),
            ("data/cookies mount", summary.get("cookies_mount")),
            ("tmp mount", summary.get("tmp_mount")),
        )
        if not ready
    ]
    return f"Docker Compose ptcli-api service is incomplete at {path}: {', '.join(missing)}."


def _deployment_docker_compose_message(summary: dict[str, Any]) -> str:
    path = summary.get("path")
    if summary.get("daily_scheduler_service_ready"):
        return f"Docker Compose daily scheduler service is configured: {path}"
    if summary.get("daily_schedule_service_ready"):
        return f"Docker Compose one-shot daily schedule service is configured: {path}"
    if not summary.get("present"):
        return f"docker-compose.yml is not present at {path}; skip this warning if not using Docker Compose."
    if not summary.get("readable"):
        return f"docker-compose.yml could not be read at {path}: {summary.get('error')}"
    missing = [
        name
        for name, ready in (
            ("ptcli-api service", summary.get("ptcli_api_service")),
            ("ptcli-daily-scheduler service", summary.get("daily_scheduler_service")),
            ("daily profile", summary.get("daily_profile")),
            ("daily-scheduler command", summary.get("daily_scheduler_command")),
        )
        if not ready
    ]
    return f"Docker Compose daily scheduler service is incomplete at {path}: {', '.join(missing)}."


def _deployment_mount_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    mount_names = {"path.config", "path.cookies_dir", "path.tmp_dir", "path.job_dir", "path.downloads_path"}
    required: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for check in checks:
        if check.get("name") not in mount_names:
            continue
        item = {
            "name": str(check.get("name")).removeprefix("path."),
            "path": check.get("path"),
            "ok": bool(check.get("ok")),
            "exists": check.get("exists", bool(check.get("ok"))),
            "writable": check.get("writable"),
            "message": check.get("message"),
        }
        required.append(item)
        if not item["ok"]:
            missing.append(item)
    return {
        "required": required,
        "missing": missing,
        "ready": not missing,
    }


def _deployment_agent_summary(
    ready: bool,
    checks: list[dict[str, Any]],
    paths: dict[str, str],
    mounts: dict[str, Any],
    qbit: dict[str, Any],
    daily_candidate_plan: dict[str, Any],
    docker_compose: dict[str, Any],
) -> dict[str, Any]:
    check_by_name = {str(check.get("name")): check for check in checks}
    api_token_check = check_by_name.get("security.api_token", {})
    blocking_failures = [str(check.get("name")) for check in checks if check.get("ok") is False and check.get("blocking", True) is not False]
    warning_failures = [str(check.get("name")) for check in checks if check.get("ok") is False and check.get("blocking") is False]
    return {
        "ready_for_ai": ready,
        "ready_for_manual_retorrent": ready and bool(qbit.get("configured")),
        "ready_for_daily_candidates": ready and bool(daily_candidate_plan.get("configured")) and not bool(daily_candidate_plan.get("blockers")),
        "manual_workflow_ready": ready and bool(qbit.get("configured")),
        "daily_workflow_ready": ready and bool(daily_candidate_plan.get("configured")) and not bool(daily_candidate_plan.get("blockers")),
        "compose_deployable": bool(docker_compose.get("ptcli_api_service_ready")),
        "api_local_only": bool(docker_compose.get("ptcli_api_localhost_port")),
        "api_auth_recommended": not bool(api_token_check.get("configured")),
        "api_token_configured": bool(api_token_check.get("configured")),
        "qbit_configured": bool(qbit.get("configured")),
        "daily_candidates_configured": bool(daily_candidate_plan.get("configured")),
        "docker_compose_api_ready": bool(docker_compose.get("ptcli_api_service_ready")),
        "docker_compose_daily_ready": bool(docker_compose.get("daily_scheduler_service_ready") or docker_compose.get("daily_schedule_service_ready")),
        "missing_mounts": mounts.get("missing", []),
        "blocking_checks": blocking_failures,
        "warning_checks": warning_failures,
        "configured_paths": paths,
        "qbit_client": qbit.get("client"),
        "daily_candidate_schedule_count": daily_candidate_plan.get("count", 0),
    }


def _deployment_runtime_handoff(
    agent_summary: dict[str, Any],
    paths: dict[str, str],
    qbit: dict[str, Any],
    daily_candidate_plan: dict[str, Any],
    docker_compose: dict[str, Any],
    blockers: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    api_base_url = os.environ.get("PTCLI_PUBLIC_BASE_URL") or "http://127.0.0.1:8080"
    manual_ready = bool(agent_summary.get("manual_workflow_ready"))
    daily_ready = bool(agent_summary.get("daily_workflow_ready"))
    compose_ready = bool(agent_summary.get("compose_deployable"))
    return {
        "kind": "ptcli.deployment_runtime_handoff",
        "ready": bool(agent_summary.get("ready_for_ai")),
        "compose_deployable": compose_ready,
        "api": {
            "base_url": api_base_url,
            "health": f"{api_base_url.rstrip('/')}/health",
            "openapi": f"{api_base_url.rstrip('/')}/openapi.json",
            "tools": f"{api_base_url.rstrip('/')}/v1/tools",
            "agent_manifest": f"{api_base_url.rstrip('/')}/.well-known/ptcli-agent.json",
            "localhost_bound": bool(docker_compose.get("ptcli_api_localhost_port")),
            "token_configured": bool(agent_summary.get("api_token_configured")),
            "auth_header": "Authorization: Bearer <PTCLI_API_TOKEN>",
            "auth_recommended": bool(agent_summary.get("api_auth_recommended")),
        },
        "manual_retorrent": {
            "ready": manual_ready,
            "tool": "source_url_check_and_submit" if manual_ready else "readiness_bundle",
            "endpoint": "/v1/jobs/retorrent/from-url/check-and-submit" if manual_ready else "/v1/readiness/bundle",
            "minimum_request": {
                "source_url": "https://u2.dmhy.org/details.php?id=60635",
                "target": "MTEAM",
                "accept_rules": True,
                "confirm_upload": True,
                "save_path": paths.get("downloads_path") or "/downloads",
            },
            "blocked_by": [] if manual_ready else _deployment_handoff_blockers(agent_summary, blockers, require_daily=False),
        },
        "daily_candidates": {
            "ready": daily_ready,
            "tool": "daily_candidates_schedule_job" if daily_ready else "deployment_check",
            "endpoint": "/v1/jobs/candidates/daily/schedule" if daily_ready else "/v1/deployment/check",
            "schedule_count": daily_candidate_plan.get("count", 0),
            "blocked_by": [] if daily_ready else _deployment_handoff_blockers(agent_summary, blockers, require_daily=True),
        },
        "qbit": {
            "configured": bool(qbit.get("configured")),
            "client": qbit.get("client"),
            "url": qbit.get("qbit_url"),
            "connectivity_checked": bool(qbit.get("connectivity_checked")),
        },
        "next_step": _deployment_runtime_next_step(manual_ready, daily_ready, compose_ready, blockers, warnings),
        "warnings": warnings,
    }


def _deployment_runtime_next_step(manual_ready: bool, daily_ready: bool, compose_ready: bool, blockers: list[str], warnings: list[str]) -> dict[str, Any]:
    if blockers:
        return {"action": "fix_deployment", "tool": "deployment_check", "endpoint": "/v1/deployment/check", "blockers": blockers}
    if not compose_ready:
        return {"action": "fix_compose", "tool": "deployment_check", "endpoint": "/v1/deployment/check", "warnings": warnings}
    if manual_ready:
        return {"action": "run_manual_preflight", "tool": "source_url_check_and_submit", "endpoint": "/v1/jobs/retorrent/from-url/check-and-submit"}
    if daily_ready:
        return {"action": "run_daily_candidates", "tool": "daily_candidates_schedule_job", "endpoint": "/v1/jobs/candidates/daily/schedule"}
    return {"action": "configure_workflows", "tool": "readiness_bundle", "endpoint": "/v1/readiness/bundle", "warnings": warnings}


def _deployment_agent_handoff(
    agent_summary: dict[str, Any],
    paths: dict[str, str],
    qbit: dict[str, Any],
    daily_candidate_plan: dict[str, Any],
    docker_compose: dict[str, Any],
    blockers: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    manual_ready = bool(agent_summary.get("ready_for_manual_retorrent"))
    daily_ready = bool(agent_summary.get("ready_for_daily_candidates"))
    return {
        "kind": "ptcli.deployment_agent_handoff",
        "ready": bool(agent_summary.get("ready_for_ai")),
        "recommended_first_step": "site_policies" if agent_summary.get("ready_for_ai") else "fix_deployment",
        "manual_retorrent": {
            "ready": manual_ready,
            "tool": "source_url_retorrent_job",
            "endpoint": "/v1/jobs/retorrent/from-url",
            "minimum_request": {
                "source_url": "https://u2.dmhy.org/details.php?id=60635",
                "target": "MTEAM",
                "accept_rules": True,
                "confirm_upload": True,
                "save_path": paths.get("downloads_path") or "/downloads",
            },
            "required_confirmations": ["accept_rules=true", "confirm_upload=true", "rules reviewed for source and target"],
            "blocked_by": [] if manual_ready else _deployment_handoff_blockers(agent_summary, blockers, require_daily=False),
        },
        "daily_candidates": {
            "ready": daily_ready,
            "tool": "daily_candidates_schedule_job",
            "endpoint": "/v1/jobs/candidates/daily/schedule",
            "configured_schedule_count": daily_candidate_plan.get("count", 0),
            "submit_handoff": "Read schedule_digest.submission_handoff, then POST approved items to /v1/jobs/candidates/{candidate_job_id}/submit.",
            "required_confirmations": ["accept_rules=true in schedule", "confirm_upload=true when submitting a selected candidate", "save_path or path when submitting"],
            "blocked_by": [] if daily_ready else _deployment_handoff_blockers(agent_summary, blockers, require_daily=True),
        },
        "qbit": {
            "configured": bool(qbit.get("configured")),
            "client": qbit.get("client"),
            "url": qbit.get("qbit_url"),
            "connectivity_checked": bool(qbit.get("connectivity_checked")),
        },
        "docker_compose": {
            "api_ready": bool(docker_compose.get("ptcli_api_service_ready")),
            "daily_schedule_ready": bool(docker_compose.get("daily_scheduler_service_ready") or docker_compose.get("daily_schedule_service_ready")),
            "daily_scheduler_ready": bool(docker_compose.get("daily_scheduler_service_ready")),
            "compose_file": docker_compose.get("path"),
            "api_service": {
                "service": bool(docker_compose.get("ptcli_api_service")),
                "serve_command": bool(docker_compose.get("ptcli_api_command")),
                "healthcheck": bool(docker_compose.get("ptcli_api_healthcheck")),
                "localhost_port": bool(docker_compose.get("ptcli_api_localhost_port")),
                "api_token_env": bool(docker_compose.get("ptcli_api_token_env")),
                "job_dir_env": bool(docker_compose.get("ptcli_job_dir_env")),
                "host_gateway": bool(docker_compose.get("host_gateway")),
                "downloads_mount": bool(docker_compose.get("downloads_mount")),
                "config_mount": bool(docker_compose.get("config_mount")),
                "cookies_mount": bool(docker_compose.get("cookies_mount")),
                "tmp_mount": bool(docker_compose.get("tmp_mount")),
            },
        },
        "safety": {
            "api_token_configured": bool(agent_summary.get("api_token_configured")),
            "live_upload_requires": ["accept_rules=true", "confirm_upload=true", "non-duplicate target", "ready site policy gate"],
            "warnings": warnings,
        },
        "next_tools": ["deployment_check", "site_policies", "source_url_retorrent_job", "daily_candidates_schedule_job", "get_job_status", "get_job_summary"],
    }


def _deployment_handoff_blockers(agent_summary: dict[str, Any], blockers: list[str], *, require_daily: bool) -> list[str]:
    items = list(blockers)
    if agent_summary.get("qbit_configured") is not True:
        items.append("qBittorrent is not configured.")
    if require_daily and agent_summary.get("daily_candidates_configured") is not True:
        items.append(f"No daily candidate schedules configured. Set {DAILY_CANDIDATE_SCHEDULE_ENV}.")
    return list(dict.fromkeys(item for item in items if item))


def _deployment_next_actions(checks: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for check in checks:
        if check.get("ok") is not False:
            continue
        name = str(check.get("name") or "")
        path = check.get("path")
        if name == "path.config":
            actions.append(f"Mount or create data/config.py at {path}.")
        elif name == "path.cookies_dir":
            actions.append(f"Mount data/cookies at {path} and add source tracker cookie files such as U2.txt or CHD.txt.")
        elif name in {"path.tmp_dir", "path.job_dir"}:
            actions.append(f"Create the writable directory {path} or fix its permissions.")
        elif name == "path.downloads_path":
            actions.append(f"Mount the qBittorrent download path at {path}, matching the paths used by qBittorrent.")
        elif name == "qbit.config":
            actions.append("Configure DEFAULT.default_torrent_client and TORRENT_CLIENTS.<client> for qBittorrent in data/config.py.")
        elif name == "runtime.ptcli_dependencies":
            actions.append("Install focused ptcli dependencies from requirements-ptcli.txt or rebuild the ptcli Docker image.")
        elif name == "security.api_token":
            actions.append("Set PTCLI_API_TOKEN before exposing ptcli-api outside localhost.")
        elif name == "automation.daily_candidates":
            actions.extend(_string_list((check.get("details") or {}).get("next_actions")))
        elif name == "docker.compose_daily_schedule":
            actions.append("Add or update the ptcli-daily-schedule service in docker-compose.yml, or run ptcli daily-schedule manually if not using Docker Compose.")
    return actions


def tools_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "tools": _agent_tool_schemas(),
        "example": {
            "source": "https://u2.dmhy.org/details.php?id=60635",
            "target": "MTEAM",
            "execute": True,
            "accept_rules": True,
            "confirm_upload": True,
            "save_path": "/downloads",
            "uploaded_qbit_category": "MTEAM",
            "uploaded_qbit_tags": "retorrent",
            "uploaded_qbit_upload_limit": "2MiB/s",
        },
    }


def agent_run_preview_payload(request: dict[str, Any] | None = None) -> dict[str, Any]:
    request = request if isinstance(request, dict) else {}
    source_url = str(request.get("source_url") or request.get("source") or "").strip()
    source_tracker = str(request.get("source_tracker") or "").strip().upper()
    target = request.get("target") or request.get("target_trackers") or "MTEAM"
    target_trackers = _preview_target_trackers(target)
    workflow_name = str(request.get("workflow") or "source_url_retorrent")
    workflows = {workflow["name"]: workflow for workflow in _agent_default_workflows()}
    workflow = workflows.get(workflow_name) or workflows["source_url_retorrent"]
    accept_rules = _truthy(request.get("accept_rules"))
    confirm_upload = _truthy(request.get("confirm_upload"))
    request_template = _agent_preview_request_template(str(workflow.get("name") or ""), source_url, source_tracker, target_trackers, accept_rules, confirm_upload, request)
    blockers = _agent_preview_blockers(str(workflow.get("name") or ""), source_url, source_tracker, target_trackers, accept_rules, confirm_upload, request)
    steps = _agent_preview_steps(workflow, request_template)
    one_call_handoff = _agent_preview_one_call_handoff(workflow, request_template)
    one_call_ready = not blockers and one_call_handoff.get("ready")
    closure_contract = _agent_closure_contract()
    return {
        "status": "ok" if not blockers else "blocked",
        "ok": not blockers,
        "kind": "ptcli.agent_run_preview",
        "dry_run": True,
        "mutates_state": False,
        "live_upload": False,
        "workflow": workflow.get("name"),
        "tool": workflow.get("tool"),
        "request": {
            "source_url": source_url or None,
            "source_tracker": source_tracker or None,
            "target": target_trackers,
            "accept_rules": accept_rules,
            "confirm_upload": confirm_upload,
            "limit": request.get("limit"),
            "save_path": request.get("save_path"),
            "uploaded_qbit_category": request.get("uploaded_qbit_category"),
            "uploaded_qbit_tags": request.get("uploaded_qbit_tags"),
        },
        "request_template": request_template,
        "closure_contract": closure_contract,
        "closure_handoff_examples": _agent_preview_closure_examples(),
        "one_call_handoff": one_call_handoff,
        "steps": steps,
        "next_step": one_call_handoff.get("next_step") if one_call_ready else steps[0] if steps else None,
        "recommended_tool": one_call_handoff.get("tool") if one_call_ready else steps[0].get("tool") if steps else None,
        "recommended_endpoint": one_call_handoff.get("endpoint") if one_call_ready else steps[0].get("endpoint") if steps else None,
        "recommended_request": one_call_handoff.get("request") if one_call_ready else steps[0].get("request") if steps else None,
        "blockers": blockers,
        "next_actions": _agent_preview_next_actions(str(workflow.get("name") or ""), blockers, closure_contract),
    }


def _preview_target_trackers(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    return [item.strip().upper() for item in str(value or "MTEAM").split(",") if item.strip()]


def _agent_preview_request_template(workflow: str, source_url: str, source_tracker: str, target_trackers: list[str], accept_rules: bool, confirm_upload: bool, request: dict[str, Any]) -> dict[str, Any]:
    target: str | list[str] = target_trackers[0] if len(target_trackers) == 1 else target_trackers
    if workflow == "daily_candidates":
        template: dict[str, Any] = {
            "source_tracker": source_tracker or "<source tracker code>",
            "target": target,
            "limit": _agent_preview_limit(request.get("limit")),
            "accept_rules": accept_rules,
        }
    else:
        template = {
            "source_url": source_url or "<source tracker details URL>",
            "target": target,
            "accept_rules": accept_rules,
            "confirm_upload": confirm_upload,
        }
    for key in ("save_path", "path", "uploaded_qbit_category", "uploaded_qbit_tags", "uploaded_qbit_upload_limit", "uploaded_qbit_download_limit"):
        if request.get(key) is not None:
            template[key] = request[key]
    if workflow == "daily_candidates":
        template["submission_overrides"] = _agent_preview_candidate_submission_overrides(confirm_upload, request)
    return template


def _agent_preview_candidate_submission_overrides(confirm_upload: bool, request: dict[str, Any]) -> dict[str, Any]:
    overrides: dict[str, Any] = {"confirm_upload": confirm_upload}
    for key in ("save_path", "path", "uploaded_qbit_category", "uploaded_qbit_tags", "uploaded_qbit_upload_limit", "uploaded_qbit_download_limit"):
        if request.get(key) is not None:
            overrides[key] = request[key]
    return overrides


def _agent_preview_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_CANDIDATE_LIMIT
    return max(1, min(parsed, DEFAULT_CANDIDATE_LIMIT))


def _agent_preview_blockers(workflow: str, source_url: str, source_tracker: str, target_trackers: list[str], accept_rules: bool, confirm_upload: bool, request: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if workflow == "daily_candidates":
        if not source_tracker:
            blockers.append("source_tracker is required before creating daily candidate jobs.")
    elif not source_url:
        blockers.append("source_url is required before submitting source_url_retorrent_job.")
    if not target_trackers:
        blockers.append("target is required before submitting retorrent automation.")
    if not accept_rules:
        blockers.append("accept_rules=true is required before live retorrent automation.")
    if workflow == "source_url_retorrent" and not confirm_upload:
        blockers.append("confirm_upload=true is required before live target upload.")
    if workflow == "daily_candidates" and not confirm_upload:
        blockers.append("confirm_upload=true will be required before submitting an approved candidate.")
    if workflow == "daily_candidates" and not (request.get("save_path") or request.get("path")):
        blockers.append("save_path or path will be required before submitting an approved candidate.")
    return blockers


def _agent_preview_steps(workflow: dict[str, Any], request_template: dict[str, Any]) -> list[dict[str, Any]]:
    endpoints = {tool["name"]: tool.get("path") for tool in _agent_tool_schemas()}
    methods = {tool["name"]: tool.get("method") for tool in _agent_tool_schemas()}
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(workflow.get("runbook") if isinstance(workflow.get("runbook"), list) else [], start=1):
        if not isinstance(step, dict):
            continue
        tool = step.get("tool")
        preview_step = {
            "index": index,
            "step": step.get("step"),
            "tool": tool,
            "endpoint": endpoints.get(tool),
            "method": methods.get(tool),
            "request": _agent_preview_step_request(str(tool or ""), request_template),
            "read": step.get("read"),
            "continue_when": step.get("continue_when"),
            "repeat_when": step.get("repeat_when"),
            "stop_when": step.get("stop_when"),
            "complete_when": step.get("complete_when"),
            "resume_with": step.get("resume_with"),
        }
        steps.append({key: value for key, value in preview_step.items() if value is not None})
    return steps


def _agent_preview_one_call_handoff(workflow: dict[str, Any], request_template: dict[str, Any]) -> dict[str, Any]:
    one_call = workflow.get("one_call") if isinstance(workflow.get("one_call"), dict) else {}
    tool = str(one_call.get("tool") or "")
    if not tool:
        return {"ready": False, "tool": None, "endpoint": None, "method": None, "request": None, "blockers": ["workflow has no one_call tool"]}
    endpoints = {item["name"]: item.get("path") for item in _agent_tool_schemas()}
    methods = {item["name"]: item.get("method") for item in _agent_tool_schemas()}
    request = _agent_preview_step_request(tool, request_template)
    blockers = [] if request else ["one_call request template is unavailable"]
    return {
        "ready": not blockers,
        "tool": tool,
        "endpoint": endpoints.get(tool) or one_call.get("endpoint"),
        "method": methods.get(tool) or one_call.get("method"),
        "request": request,
        "continue_when": one_call.get("continue_when"),
        "stop_when": one_call.get("stop_when"),
        "then_follow": one_call.get("then_follow"),
        "next_step": {
            "tool": tool,
            "endpoint": endpoints.get(tool) or one_call.get("endpoint"),
            "method": methods.get(tool) or one_call.get("method"),
            "request": request,
            "reason": "one_call_check_duplicates_then_submit_job",
        }
        if not blockers
        else None,
        "blockers": blockers,
    }


def _agent_preview_step_request(tool: str, request_template: dict[str, Any]) -> dict[str, Any] | None:
    if tool == "readiness_bundle":
        return request_template
    if tool == "source_url_retorrent_preflight":
        return request_template
    if tool == "site_policies":
        return {"source_url": request_template.get("source_url"), "target": request_template.get("target"), "accept_rules": request_template.get("accept_rules")}
    if tool == "retorrent_check":
        return {"source": request_template.get("source_url"), "source_url": request_template.get("source_url"), "target": request_template.get("target"), "accept_rules": request_template.get("accept_rules")}
    if tool == "source_url_retorrent_job":
        return request_template
    if tool == "source_url_check_and_submit":
        return request_template
    if tool == "get_job_status":
        return {"job_id": "<job_id from source_url_retorrent_job>"}
    if tool == "get_job_summary":
        return {"job_id": "<job_id from source_url_retorrent_job>"}
    if tool == "resume_job":
        return {"job_id": "<job_id from closure_handoff.next_step>", "overrides": "<allowlisted overrides only>"}
    if tool == "daily_candidates_schedule_job":
        return {"schedules": [request_template]}
    if tool == "submit_daily_candidate_job":
        return {"job_id": "<candidate_job_id from schedule_digest.submission_handoff.items[]>", "rank": 1, "overrides": request_template.get("submission_overrides")}
    return None


def _agent_preview_closure_examples() -> dict[str, Any]:
    return {
        "complete": {"closure_handoff": {"complete": True, "action": "done", "recommended_tool": "get_job_summary", "next_step": {"tool": "get_job_summary", "method": "GET"}}},
        "resume": {"closure_handoff": {"complete": False, "action": "prepare_materials", "recommended_tool": "resume_job", "next_step": {"tool": "resume_job", "method": "POST", "request": {}}}},
        "stop": {"closure_handoff": {"complete": False, "action": "stop_duplicate", "recommended_tool": None, "next_step": {"tool": None, "method": None}}},
    }


def _agent_preview_next_actions(workflow: str, blockers: list[str], closure_contract: dict[str, Any]) -> list[str]:
    if blockers:
        return ["Resolve preview blockers before submitting live-capable jobs. This preview does not contact trackers or qBittorrent."]
    if workflow == "daily_candidates":
        return [f"Create daily candidate schedule jobs, read schedule_digest.submission_handoff, submit one approved candidate, then follow {closure_contract['next_step_source']} until {closure_contract['complete_when']}."]
    return [f"Submit one_call_handoff.request to source_url_check_and_submit, poll get_job_status when job_id is returned, then follow {closure_contract['next_step_source']} until {closure_contract['complete_when']}."]


def _agent_tool_schemas() -> list[dict[str, Any]]:
    retorrent_request_schema = _retorrent_tool_request_schema()
    manual_retorrent_request_schema = _manual_retorrent_tool_request_schema()
    source_url_retorrent_request_schema = _source_url_retorrent_tool_request_schema()
    source_url_check_submit_request_schema = _source_url_retorrent_tool_request_schema()
    candidate_request_schema = _daily_candidate_tool_request_schema()
    candidate_schedule_request_schema = _daily_candidate_schedule_tool_request_schema()
    candidate_submit_request_schema = _candidate_submit_tool_request_schema()
    retorrent_check_submit_request_schema = _retorrent_check_submit_tool_request_schema()
    sites_request_schema = _sites_tool_request_schema()
    qbit_inspect_request_schema = _qbit_inspect_tool_request_schema()
    qbit_match_request_schema = _qbit_match_tool_request_schema()
    qbit_export_request_schema = _qbit_export_tool_request_schema()
    qbit_inject_request_schema = _qbit_inject_tool_request_schema()
    qbit_wait_request_schema = _qbit_wait_tool_request_schema()
    site_policy_request_schema = _site_policy_tool_request_schema()
    readiness_bundle_request_schema = _readiness_bundle_tool_request_schema()
    agent_run_preview_request_schema = _agent_run_preview_tool_request_schema()
    job_id_schema = _job_id_tool_request_schema()
    job_resume_schema = _job_resume_tool_request_schema()
    job_cancel_schema = _job_cancel_tool_request_schema()
    job_list_schema = _job_list_tool_request_schema()
    return [
        {
            "name": "agent_run_preview",
            "method": "POST",
            "path": "/v1/agent/run-preview",
            "description": "Return a no-network, no-mutation walkthrough of the OpenClaw/Hermes source-url retorrent workflow, including request templates and closure_handoff handling.",
            "input_schema": agent_run_preview_request_schema,
            "response_contract": _agent_run_preview_response_contract(),
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "source_url_retorrent_preflight",
            "method": "POST",
            "path": "/v1/retorrent/source-url/preflight",
            "description": "Resolve a user-provided source URL and summarize deployment, policy, confirmation, material, and job-template readiness before creating any live-capable retorrent job. This endpoint never contacts trackers or qBittorrent.",
            "input_schema": source_url_retorrent_request_schema,
            "response_contract": _source_url_preflight_response_contract(),
            "workflow_hints": {"submit_with": "source_url_retorrent_job", "readiness_source": "readiness_bundle", "duplicate_check_with": "retorrent_check"},
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "retorrent_check",
            "method": "POST",
            "path": "/v1/retorrent/check",
            "description": "Resolve a source tracker URL, fetch source metadata, and check whether the target already has duplicates. This endpoint never uploads.",
            "input_schema": _without_execute_fields(retorrent_request_schema),
            "response_contract": _sync_response_contract(),
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "retorrent_execute_if_no_duplicate",
            "method": "POST",
            "path": "/v1/retorrent",
            "description": "Run the live retorrent closure when execute=true. The existing rule and duplicate gates block unsafe uploads.",
            "input_schema": retorrent_request_schema,
            "response_contract": _sync_response_contract(),
            "safety": _live_upload_safety_contract(),
        },
        {
            "name": "retorrent_check_job",
            "method": "POST",
            "path": "/v1/jobs/retorrent/check",
            "description": "Create an asynchronous duplicate-check job and return a job_id for polling.",
            "input_schema": _without_execute_fields(retorrent_request_schema),
            "response_contract": _job_response_contract(),
            "workflow_hints": {"poll_with": "get_job_status", "summary_with": "get_job_summary", "resume_with": "resume_job"},
            "safety": {"mutates_state": True, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "submit_checked_retorrent_job",
            "method": "POST",
            "path": "/v1/jobs/retorrent/check/{job_id}/submit",
            "description": "Create a live retorrent job from a completed duplicate-check job only when submit_if_clear_handoff.ready=true. Source and target identity are inherited from the check job; request body accepts execution overrides such as confirmations, paths, qBittorrent rate limits, and material files.",
            "input_schema": retorrent_check_submit_request_schema,
            "response_contract": _job_response_contract(),
            "workflow_hints": {"requires": "submit_if_clear_handoff.ready=true", "poll_with": "get_job_status", "summary_with": "get_job_summary", "resume_with": "resume_job"},
            "safety": _live_upload_safety_contract(),
        },
        {
            "name": "retorrent_job",
            "method": "POST",
            "path": "/v1/jobs/retorrent",
            "description": "Create an asynchronous retorrent job and return a job_id for polling long-running download, material, upload, and qBittorrent steps.",
            "input_schema": retorrent_request_schema,
            "response_contract": _job_response_contract(),
            "workflow_hints": {"poll_with": "get_job_status", "summary_with": "get_job_summary", "resume_with": "resume_job"},
            "safety": _live_upload_safety_contract(),
        },
        {
            "name": "manual_retorrent_job",
            "method": "POST",
            "path": "/v1/jobs/retorrent/submit",
            "description": "Submit the primary AI workflow: source tracker URL plus target tracker. It creates a retorrent job that checks duplicates and only proceeds when rule, duplicate, and confirmation gates allow.",
            "input_schema": manual_retorrent_request_schema,
            "response_contract": _job_response_contract(),
            "workflow_hints": {"poll_with": "get_job_status", "summary_with": "get_job_summary", "resume_with": "resume_job"},
            "safety": _live_upload_safety_contract(),
        },
        {
            "name": "source_url_retorrent_job",
            "method": "POST",
            "path": "/v1/jobs/retorrent/from-url",
            "description": "Recommended AI entrypoint for a user-provided source tracker details URL plus target tracker. It infers the source tracker and torrent id, checks duplicates, and only proceeds when rule, duplicate, and confirmation gates allow.",
            "input_schema": source_url_retorrent_request_schema,
            "response_contract": _job_response_contract(),
            "workflow_hints": {"poll_with": "get_job_status", "summary_with": "get_job_summary", "resume_with": "resume_job"},
            "safety": _live_upload_safety_contract(),
        },
        {
            "name": "source_url_check_and_submit",
            "method": "POST",
            "path": "/v1/jobs/retorrent/from-url/check-and-submit",
            "description": "One-call AI-safe manual workflow: run target duplicate check now, stop if a duplicate exists, otherwise create a live source-url retorrent job and return its job_id. Live upload still requires accept_rules=true and confirm_upload=true.",
            "input_schema": source_url_check_submit_request_schema,
            "response_contract": _source_url_check_and_submit_response_contract(),
            "workflow_hints": {"checks_with": "retorrent_check", "poll_with": "get_job_status", "summary_with": "get_job_summary", "resume_with": "resume_job"},
            "safety": _live_upload_safety_contract(),
        },
        {
            "name": "daily_candidates",
            "method": "POST",
            "path": "/v1/candidates/daily",
            "description": "Return up to 10 ranked source/target retorrent candidates with metadata availability, duplicate status, policy blockers, risk signals, push_payload, and an executable retorrent request template.",
            "input_schema": candidate_request_schema,
            "response_contract": _candidate_response_contract(),
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "daily_candidates_job",
            "method": "POST",
            "path": "/v1/jobs/candidates/daily",
            "description": "Create an asynchronous daily-candidate discovery job and return a job_id for polling.",
            "input_schema": candidate_request_schema,
            "response_contract": _daily_candidate_job_response_contract(),
            "workflow_hints": {"poll_with": "get_job_status", "summary_with": "get_job_summary"},
            "safety": {"mutates_state": True, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "submit_daily_candidate_job",
            "method": "POST",
            "path": "/v1/jobs/candidates/{job_id}/submit",
            "description": "Create a live retorrent job from a ranked item in a completed daily-candidates job. The candidate source/target identity is inherited from the digest; this endpoint only accepts execution overrides such as confirm_upload, save_path, qBittorrent tags, rate limits, and material files.",
            "input_schema": candidate_submit_request_schema,
            "response_contract": _job_response_contract(),
            "workflow_hints": {"poll_with": "get_job_status", "summary_with": "get_job_summary", "resume_with": "resume_job"},
            "safety": _live_upload_safety_contract(),
        },
        {
            "name": "daily_candidates_schedule",
            "method": "POST",
            "path": "/v1/candidates/daily/schedule",
            "description": "Validate and normalize daily candidate schedules into job requests that external cron, Docker, OpenClaw, or Hermes can run. This endpoint does not contact trackers or upload.",
            "input_schema": candidate_schedule_request_schema,
            "response_contract": {
                "required_fields": ["status", "ok", "count", "schedules", "blockers", "next_actions"],
                "schedule_fields": ["name", "enabled", "schedule", "job_endpoint", "job_request", "push_contract"],
            },
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "daily_candidates_schedule_job",
            "method": "POST",
            "path": "/v1/jobs/candidates/daily/schedule",
            "description": "Create one daily-candidate discovery job per enabled schedule entry and return job_ids for polling. This only scans candidates and never uploads.",
            "input_schema": candidate_schedule_request_schema,
            "response_contract": {
                "required_fields": ["status", "ok", "job_count", "jobs", "skipped", "schedule_digest", "notification_payload", "delivery_handoff", "agent_decision", "blockers", "next_actions"],
                "job_fields": ["schedule_name", "job_id", "status_endpoint", "summary_endpoint", "job_request", "candidate_digest", "agent_decision"],
                "digest_fields": ["items", "push_items", "push_payload", "approval_queue", "top_safe_candidates", "top_submit_requests", "submission_handoff", "target_count", "selected_count", "ready_count", "shortfall_count", "target_met", "ready_job_count", "submit_request_count", "pending_job_count", "blocked_job_count"],
                "push_payload_fields": ["title", "summary", "message", "format", "target_count", "selected_count", "shortfall_count", "target_met", "items", "top_item", "approval_queue", "top_safe_candidates", "decision_summary", "submission_ready", "recommended_action"],
                "notification_fields": ["title", "summary", "message", "status", "ready", "submission_ready", "counts", "top_item", "items", "approval_queue", "top_safe_candidates", "submit_items", "submission_handoff", "execution_summary", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "next_actions"],
                "delivery_handoff_fields": ["ready", "publish_ready", "submission_ready", "target_met", "status", "recommended_tool", "recommended_endpoint", "recommended_request", "counts", "notification_payload", "approval_queue", "top_safe_candidates", "submission_handoff", "execution_summary", "top_submit_requests", "publish_contract", "continue_when", "stop_when", "blockers", "next_actions"],
                "approval_queue_fields": ["ready", "safe_count", "guarded_count", "blocked_count", "pending_job_count", "recommended_count", "items", "top_safe_candidates", "submit_tool", "submit_endpoint_template", "requires_confirmation", "continue_when", "stop_when", "blockers", "next_actions"],
                "approval_queue_item_fields": ["schedule_name", "candidate_job_id", "rank", "source_tracker", "target_trackers", "source_id", "source_url", "title", "score", "risk_level", "policy_risk_level", "execution_priority", "duplicate_clear", "metadata", "policy_risk_summary", "submit_tool", "submit_endpoint", "request_template", "source_url_retorrent_request", "requires_confirmation", "after_submit"],
                "submission_handoff_fields": ["ready", "submit_tool", "submit_endpoint_template", "required_overrides", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "approval_queue", "top_safe_candidates", "execution_summary", "items"],
                "execution_summary_fields": ["ready", "submit_count", "blocked_count", "counts", "recommended_tool", "recommended_endpoint", "recommended_request", "post_submit_flow", "actions", "items", "blockers", "next_actions"],
                "submission_item_fields": ["candidate_job_id", "submit_tool", "submit_endpoint", "selector", "request_template", "identity_inherited_from_candidate", "policy_execution", "required_overrides", "allowed_overrides", "after_submit"],
                "after_submit_fields": ["read_fields", "poll_with", "summary_with", "stop_when", "resume_when", "material_resume_request"],
            },
            "workflow_hints": {"poll_with": "get_job_status", "summary_with": "get_job_summary"},
            "safety": {"mutates_state": True, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "site_profiles",
            "method": "POST",
            "path": "/v1/sites",
            "description": "Return Chinese PT tracker adapter capabilities, credential requirements, policy profiles, and supported source/target flow matrix. This endpoint never contacts trackers or qBittorrent.",
            "input_schema": sites_request_schema,
            "response_contract": _sites_response_contract(),
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "qbit_inspect",
            "method": "POST",
            "path": "/v1/qbit/inspect",
            "description": "Read qBittorrent torrent state by optional hash/limit. This endpoint is read-only and returns hash/path/progress/category/tag evidence for AI decisions.",
            "input_schema": qbit_inspect_request_schema,
            "response_contract": _qbit_inspect_response_contract(),
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "qbit_match",
            "method": "POST",
            "path": "/v1/qbit/match",
            "description": "Find qBittorrent torrents matching a seedbox content path. This endpoint is read-only and helps AI decide whether existing local content can be reused for retorrent workflows.",
            "input_schema": qbit_match_request_schema,
            "response_contract": _qbit_match_response_contract(),
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "qbit_export_target_torrent",
            "method": "POST",
            "path": "/v1/qbit/export",
            "description": "Export a .torrent from qBittorrent by hash and create an MTEAM-safe target upload candidate. This reads qBittorrent state and writes files to output_dir, but does not add torrents, upload, or change rate limits.",
            "input_schema": qbit_export_request_schema,
            "response_contract": _qbit_export_response_contract(),
            "safety": {"mutates_state": True, "mutates_qbittorrent": False, "writes_files": True, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "qbit_inject_torrent",
            "method": "POST",
            "path": "/v1/qbit/inject",
            "description": "Add a local .torrent file to qBittorrent with explicit save path, optional category/tags/rate limits, and return verified hash/path evidence. This mutates qBittorrent but never uploads to a tracker.",
            "input_schema": qbit_inject_request_schema,
            "response_contract": _qbit_inject_response_contract(),
            "safety": {"mutates_state": True, "mutates_qbittorrent": True, "writes_files": False, "live_upload": False, "requires_confirmation": ["torrent_file and save_path must be explicit", "site rules and rate limits must already be reviewed for live workflows"]},
        },
        {
            "name": "qbit_wait_complete",
            "method": "POST",
            "path": "/v1/qbit/wait",
            "description": "Wait for a qBittorrent torrent to complete by hash or content path and return completion evidence for source download or uploaded-seeding closure.",
            "input_schema": qbit_wait_request_schema,
            "response_contract": _qbit_wait_response_contract(),
            "safety": {"mutates_state": False, "mutates_qbittorrent": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "list_jobs",
            "method": "GET",
            "path": "/v1/jobs",
            "description": "List recent ptcli jobs with short AI-readable status, blockers, resume plan, lineage, and status/summary/resume endpoints. This endpoint never runs work.",
            "input_schema": job_list_schema,
            "response_contract": _job_list_response_contract(),
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "get_job_status",
            "method": "GET",
            "path": "/v1/jobs/{job_id}",
            "description": "Return short AI-readable job status, blockers, next actions, duplicate check, summary file, and resume state.",
            "input_schema": job_id_schema,
            "response_contract": _job_response_contract(),
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "get_job_summary",
            "method": "GET",
            "path": "/v1/jobs/{job_id}/summary",
            "description": "Return the job result and parsed summary-file payload when available.",
            "input_schema": job_id_schema,
            "response_contract": {"required_fields": ["status", "ok", "job_id", "summary_file", "summary", "agent_summary", "agent_decision", "candidate_digest", "submit_if_clear_handoff", "policy_coverage", "policy_handoff", "policy_qbit_defaults", "qbit_plan", "qbit_limit_audit", "qbit_handoff", "qbit_enforcement_summary", "materials_handoff", "target_upload_handoff", "closure_handoff", "closure_summary", "manual_retorrent_handoff", "candidate_batch_handoff", "candidate_submission_handoff", "candidate_submission_summary", "runtime", "resume_plan", "resume_requirements", "resume_execution_handoff", "recovery_handoff", "resume_lineage", "job_lineage", "resume_context", "resume_audit", "resume_summary", "material_resolution", "candidate_submission", "check_submission", "source_reference", "workflow_context", "job_handoff", "result", "blockers", "next_actions"]},
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "resume_job",
            "method": "POST",
            "path": "/v1/jobs/{job_id}/resume",
            "description": "Create a follow-up job from the allowlisted next_command_argv generated by a blocked or failed job. Optional allowlisted overrides can add missing confirmations, paths, qBittorrent limits, or material files before the resume job is queued.",
            "input_schema": job_resume_schema,
            "response_contract": _job_response_contract(),
            "workflow_hints": {"poll_with": "get_job_status", "summary_with": "get_job_summary"},
            "safety": {"mutates_state": True, "live_upload": "inherits_from_resume_command", "requires_confirmation": ["existing allowlisted next_command_argv", "unknown overrides are ignored"]},
        },
        {
            "name": "cancel_job",
            "method": "POST",
            "path": "/v1/jobs/{job_id}/cancel",
            "description": "Cancel a queued job before it starts. Running jobs are not force-stopped and return 409 so live tracker/qBittorrent work is not interrupted unsafely.",
            "input_schema": job_cancel_schema,
            "response_contract": _job_response_contract(),
            "safety": {"mutates_state": True, "live_upload": False, "requires_confirmation": ["job must still be queued"]},
        },
        {
            "name": "agent_manifest",
            "method": "GET",
            "path": "/.well-known/ptcli-agent.json",
            "description": "Return an OpenClaw/Hermes-friendly skill manifest with OpenAPI, tool, auth, safety, and workflow metadata.",
            "input_schema": {"type": "object", "required": [], "properties": {}},
            "response_contract": {"required_fields": ["schema_version", "base_url", "auth", "discovery", "safety", "closure_contract", "tools", "default_workflows"]},
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "deployment_check",
            "method": "GET",
            "path": "/v1/deployment/check",
            "description": "Check local ptcli deployment readiness: runtime imports, config mount, cookies/tmp/job/download paths, API token warning, and qBittorrent config presence. This does not contact trackers or qBittorrent.",
            "input_schema": {
                "type": "object",
                "required": [],
                "properties": {
                    "config": {"type": "string"},
                    "base_dir": {"type": "string"},
                    "cookies_dir": {"type": "string"},
                    "job_dir": {"type": "string"},
                    "max_concurrent_jobs": {"type": "integer", "default": DEFAULT_MAX_CONCURRENT_JOBS},
                    "downloads_path": {"type": "string"},
                    "compose_file": {"type": "string"},
                    "client": {"type": "string", "default": "default"},
                },
            },
            "response_contract": {
                "required_fields": ["status", "ok", "ready", "checks", "blockers", "warnings", "next_actions", "paths", "mounts", "queue", "qbit", "daily_candidates", "docker_compose", "deployment_handoff", "agent_summary", "agent_handoff"],
                "status_values": ["ok", "blocked"],
                "agent_summary_fields": ["ready_for_ai", "ready_for_manual_retorrent", "ready_for_daily_candidates", "manual_workflow_ready", "daily_workflow_ready", "compose_deployable", "api_local_only", "api_auth_recommended", "missing_mounts", "qbit_configured", "daily_candidates_configured", "docker_compose_api_ready", "docker_compose_daily_ready"],
                "deployment_handoff_fields": ["ready", "compose_deployable", "api", "manual_retorrent", "daily_candidates", "qbit", "next_step", "warnings"],
                "agent_handoff_fields": ["ready", "recommended_first_step", "manual_retorrent", "daily_candidates", "qbit", "docker_compose", "safety", "next_tools"],
                "docker_compose_fields": ["ptcli_api_service_ready", "ptcli_api_service", "ptcli_api_command", "ptcli_api_healthcheck", "ptcli_api_localhost_port", "ptcli_api_token_env", "ptcli_job_dir_env", "host_gateway", "downloads_mount", "config_mount", "cookies_mount", "tmp_mount", "daily_schedule_service_ready", "daily_scheduler_service_ready"],
            },
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "readiness_bundle",
            "method": "POST",
            "path": "/v1/readiness/bundle",
            "description": "Aggregate deployment, site-policy, daily-schedule, and live doctor handoff signals before an AI agent attempts retorrent automation. This endpoint never contacts trackers or qBittorrent.",
            "input_schema": readiness_bundle_request_schema,
            "response_contract": _readiness_bundle_response_contract(),
            "workflow_hints": {"manual_live_entrypoint": "source_url_retorrent_job", "daily_entrypoint": "daily_candidates_schedule_job", "policy_check": "site_policies"},
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "site_policies",
            "method": "POST",
            "path": "/v1/site-policies",
            "description": "Return the configured Chinese PT site policy matrix: automation gates, qBittorrent rate limits, seeding requirements, rule URLs, and manual review blockers. This does not contact trackers.",
            "input_schema": site_policy_request_schema,
            "response_contract": {
                "required_fields": ["status", "ok", "ready", "policy_matrix", "rule_obligations", "config_templates", "qbit_limits", "policy_gap_summary", "execution_readiness", "policy_execution_summary", "policy_setup_summary", "policy_execution_handoff", "policy_handoff", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers", "next_actions", "agent_summary"],
                "policy_fields": [
                    "tracker",
                    "roles",
                    "rules_url",
                    "automation",
                    "qbit_limits",
                    "seeding_requirements",
                    "transfer_rules",
                    "rule_obligations",
                    "policy_profile",
                    "manual_review_required",
                    "rule_review_fingerprint",
                    "policy_coverage",
                    "execution_readiness",
                ],
                "policy_profile_fields": ["config_path", "required_fields", "optional_fields", "accepted_config_shapes", "missing_fields", "disabled_automation", "template", "flat_template", "structured_template", "current_values", "next_actions"],
                "config_template_fields": ["config_path", "trackers", "structured_trackers"],
                "gap_summary_fields": ["ready", "missing_total", "disabled_total", "by_role", "missing_by_category", "recommendations"],
                "rule_obligation_fields": ["ready", "accepted_rules", "rules_url", "manual_review_required", "rule_review_fingerprint", "missing_fields", "missing_confirmations", "scopes", "required_confirmations", "blockers"],
                "rule_obligation_scope_fields": ["role", "scope", "action", "ready", "rules_url", "review_fingerprint", "required_confirmations", "missing_fields", "missing_confirmations", "blockers"],
                "execution_readiness_fields": ["ready", "accepted_rules", "ready_trackers", "blocked_trackers", "by_tracker", "blockers"],
                "policy_execution_summary_fields": ["ready", "accepted_rules", "ready_trackers", "blocked_trackers", "by_role", "items", "qbit_limit_plan", "seeding_plan", "transfer_rule_plan", "missing_by_category", "next_step", "recommended_tool", "blockers", "next_actions"],
                "policy_setup_summary_fields": ["ready", "accepted_rules", "config_path", "ready_trackers", "blocked_trackers", "missing_fingerprints", "placeholder_fingerprints", "copyable_templates", "next_step", "recommended_tool", "blockers", "next_actions"],
                "policy_execution_handoff_fields": ["ready", "accepted_rules", "phase", "recommended_tool", "recommended_endpoint", "recommended_request", "next_step", "qbit", "seeding", "transfer_rules", "rule_obligations", "config", "request", "continue_when", "stop_when", "blockers", "next_actions"],
                "policy_handoff_fields": ["ready", "config_path", "blocked_trackers", "items", "config_templates", "missing_by_category", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers", "rule_obligations"],
            },
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
    ]


def _retorrent_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["source", "target"],
        "properties": {
            "source": {"type": "string", "description": "Source tracker details URL or torrent id. URL inference supports known Chinese PT trackers."},
            "source_tracker": {"type": "string", "description": "Optional tracker code when source is only an id, e.g. U2 or CHD."},
            "target": {"type": ["string", "array"], "description": "Target tracker code(s), currently live upload closure is MTEAM-focused."},
            "execute": {"type": "boolean", "description": "Set true for live retorrent execution; omit or false for check-only behavior."},
            "accept_rules": {"type": "boolean", "description": "Required for source download and live upload automation."},
            "confirm_upload": {"type": "boolean", "description": "Required before live target upload."},
            "path": {"type": "string", "description": "Existing completed content path on the seedbox."},
            "save_path": {"type": "string", "description": "qBittorrent save path for source torrent download/injection."},
            "uploaded_save_path": {"type": "string", "description": "qBittorrent save path for uploaded target torrent injection."},
            "qbit_category": {"type": "string"},
            "qbit_tags": {"type": "string"},
            "uploaded_qbit_category": {"type": "string"},
            "uploaded_qbit_tags": {"type": "string"},
            "qbit_upload_limit": {"type": ["string", "integer"], "description": "Source torrent upload limit, e.g. 500KiB/s. If omitted, PTCLI.SITE_POLICIES may provide a default."},
            "qbit_download_limit": {"type": ["string", "integer"], "description": "Source torrent download limit, e.g. 20MiB/s. If omitted, PTCLI.SITE_POLICIES may provide a default."},
            "uploaded_qbit_upload_limit": {"type": ["string", "integer"], "description": "Uploaded target torrent upload limit, e.g. 2MiB/s. If omitted, PTCLI.SITE_POLICIES may provide a default."},
            "uploaded_qbit_download_limit": {"type": ["string", "integer"], "description": "Uploaded target torrent download limit. If omitted, PTCLI.SITE_POLICIES may provide a default."},
            "metadata_file": {"type": "string"},
            "ptgen_description_file": {"type": "string"},
            "mediainfo_file": {"type": "string", "description": "Existing MediaInfo report file to include in the target upload package."},
            "bdinfo_file": {"type": "string", "description": "Existing BDInfo report file to include in the target upload package."},
            "image_host_file": {"type": "string", "description": "JSON file containing hosted screenshot URLs and local screenshot evidence."},
            "image_host": {"type": "string", "description": "Optional image host selector used when upload_screenshots=true."},
            "screenshot_file": {"type": "string", "description": "Single existing screenshot file; use screenshot_files for multiple screenshots."},
            "screenshot_files": {"type": "array", "items": {"type": "string"}, "description": "Existing local screenshot files to include in the target upload package."},
            "screenshot_count": {"type": ["string", "integer"], "description": "Screenshot count to generate when generate_screenshots=true."},
            "enrich_metadata": {"type": "boolean", "description": "Enrich source metadata before preparing the target package."},
            "fetch_ptgen": {"type": "boolean", "description": "Fetch PTGen/Douban description metadata before preparing the target package."},
            "generate_mediainfo": {"type": "boolean", "description": "Generate MediaInfo from the resolved local content path before preparing the target package."},
            "generate_bdinfo": {"type": "boolean", "description": "Generate BDInfo for Blu-ray content before preparing the target package."},
            "generate_screenshots": {"type": "boolean", "description": "Generate video screenshots from the resolved local content path before preparing the target package."},
            "upload_screenshots": {"type": "boolean", "description": "Upload local/generated screenshots to the configured image host before preparing the target package."},
            "imdb_id": {"type": "string"},
            "tmdb_id": {"type": "string"},
            "tmdb_type": {"type": "string", "enum": ["movie", "tv"]},
            "douban_id": {"type": "string"},
            "douban_url": {"type": "string"},
        },
    }


def _manual_retorrent_tool_request_schema() -> dict[str, Any]:
    schema = json.loads(json.dumps(_retorrent_tool_request_schema()))
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("execute", None)
        properties["execute_if_no_duplicate"] = {
            "type": "boolean",
            "default": True,
            "description": "This endpoint treats the request as execute-if-clear; duplicate, rule, and confirmation gates still block unsafe work.",
        }
    return schema


def _source_url_retorrent_tool_request_schema() -> dict[str, Any]:
    schema = json.loads(json.dumps(_manual_retorrent_tool_request_schema()))
    schema["required"] = ["source_url", "target"]
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties["source_url"] = {
            "type": "string",
            "description": "User-provided source tracker details or download URL. The service infers tracker and torrent id from this URL.",
        }
        properties["source"]["description"] = "Alias for source_url; source_url is preferred for this tool."
    return schema


def _agent_run_preview_tool_request_schema() -> dict[str, Any]:
    schema = json.loads(json.dumps(_source_url_retorrent_tool_request_schema()))
    schema["required"] = []
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties["workflow"] = {"type": "string", "default": "source_url_retorrent", "enum": ["source_url_retorrent", "daily_candidates"], "description": "Agent workflow to preview without running live work."}
        properties["source_tracker"] = {"type": "string", "description": "Required for workflow=daily_candidates, e.g. U2 or CHD."}
        properties["limit"] = {"type": "integer", "default": DEFAULT_CANDIDATE_LIMIT, "maximum": DEFAULT_CANDIDATE_LIMIT}
    return schema


def _daily_candidate_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["source_tracker", "target"],
        "properties": {
            "source_tracker": {"type": "string", "description": "Source tracker code, e.g. U2 or CHD."},
            "target": {"type": ["string", "array"], "description": "Target tracker code(s), currently MTEAM duplicate checks are supported."},
            "limit": {"type": "integer", "default": DEFAULT_CANDIDATE_LIMIT, "maximum": DEFAULT_CANDIDATE_LIMIT},
            "accept_rules": {"type": "boolean", "description": "Whether rule obligations have been manually reviewed for executable candidate templates."},
            "check_dupes": {"type": "boolean", "default": True},
            "base_dir": {"type": "string"},
            "config": {"type": "string"},
        },
    }


def _daily_candidate_schedule_tool_request_schema() -> dict[str, Any]:
    candidate_properties = _daily_candidate_tool_request_schema()["properties"]
    return {
        "type": "object",
        "required": [],
        "properties": {
            "schedules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["source_tracker", "target"],
                    "properties": {
                        **candidate_properties,
                        "name": {"type": "string"},
                        "enabled": {"type": "boolean", "default": True},
                        "time": {"type": "string", "default": "09:00"},
                        "timezone": {"type": "string", "default": "Asia/Shanghai"},
                    },
                },
            }
        },
    }


def _candidate_submit_tool_request_schema() -> dict[str, Any]:
    execution_properties = _source_url_retorrent_tool_request_schema()["properties"]
    return {
        "type": "object",
        "required": ["job_id"],
        "properties": {
            "job_id": {"type": "string", "description": "Completed daily_candidates_job id that contains candidate_digest.push_items."},
            "rank": {"type": "integer", "default": 1, "minimum": 1, "maximum": DEFAULT_CANDIDATE_LIMIT, "description": "Rank in candidate_digest.push_items to submit."},
            "source_id": {"type": "string", "description": "Optional source torrent id selector. When supplied, it is matched against candidate_digest.push_items[].source_id."},
            "overrides": {
                "type": "object",
                "description": "Optional execution overrides. Source/target identity is ignored here and inherited from the selected candidate.",
                "properties": {key: value for key, value in execution_properties.items() if key not in {"source", "source_url", "source_tracker", "target"}},
            },
            **{key: value for key, value in execution_properties.items() if key not in {"source", "source_url", "source_tracker", "target"}},
        },
    }


def _retorrent_check_submit_tool_request_schema() -> dict[str, Any]:
    execution_properties = _source_url_retorrent_tool_request_schema()["properties"]
    inherited_keys = {"source", "source_url", "source_tracker", "target"}
    override_properties = {key: value for key, value in execution_properties.items() if key not in inherited_keys}
    return {
        "type": "object",
        "required": ["job_id"],
        "properties": {
            "job_id": {"type": "string", "description": "Completed retorrent_check_job id whose submit_if_clear_handoff.ready must be true."},
            "overrides": {
                "type": "object",
                "description": "Optional execution overrides. Source/target identity is inherited from the completed duplicate-check job.",
                "properties": override_properties,
            },
            **override_properties,
        },
    }


def _sites_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [],
        "properties": {
            "trackers": {"type": ["string", "array"], "description": "Optional comma-separated tracker codes or array. Defaults to the full Chinese PT allowlist."},
            "source_tracker": {"type": "string", "description": "Optional source tracker code when asking for a source/target flow profile."},
            "target": {"type": ["string", "array"], "description": "Optional target tracker code(s), e.g. MTEAM."},
            "accept_rules": {"type": "boolean", "description": "Whether manual rule review obligations are acknowledged for policy profile readiness."},
            "config": {"type": "string", "description": "Path to data/config.py inside the container."},
        },
    }


def _qbit_inspect_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [],
        "properties": {
            "client": {"type": "string", "default": "default"},
            "config": {"type": "string", "description": "Path to data/config.py inside the container."},
            "hash": {"type": "string", "description": "Optional torrent hash/infohash filter."},
            "torrent_hash": {"type": "string", "description": "Alias for hash."},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
        },
    }


def _qbit_match_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["path"],
        "properties": {
            "client": {"type": "string", "default": "default"},
            "config": {"type": "string", "description": "Path to data/config.py inside the container."},
            "path": {"type": "string", "description": "Seedbox content path to match against qBittorrent torrents."},
            "content_path": {"type": "string", "description": "Alias for path."},
        },
    }


def _qbit_export_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["hash"],
        "properties": {
            "client": {"type": "string", "default": "default"},
            "config": {"type": "string", "description": "Path to data/config.py inside the container."},
            "hash": {"type": "string", "description": "qBittorrent torrent hash/infohash to export."},
            "torrent_hash": {"type": "string", "description": "Alias for hash."},
            "output_dir": {"type": "string", "default": "./tmp/exported"},
            "target_torrent_output_dir": {"type": "string", "description": "Alias for output_dir."},
            "sanitize_for": {"type": "string", "enum": ["MTEAM"], "default": "MTEAM"},
            "target": {"type": "string", "description": "Alias for sanitize_for; currently MTEAM only."},
        },
    }


def _qbit_inject_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["torrent_file", "save_path"],
        "properties": {
            "client": {"type": "string", "default": "default"},
            "config": {"type": "string", "description": "Path to data/config.py inside the container."},
            "torrent_file": {"type": "string", "description": "Local .torrent file path visible to the ptcli service/container."},
            "torrent_path": {"type": "string", "description": "Alias for torrent_file."},
            "save_path": {"type": "string", "description": "qBittorrent save path for this torrent."},
            "category": {"type": "string", "description": "Optional qBittorrent category, e.g. MTEAM."},
            "tags": {"type": ["string", "array"], "description": "Optional comma-separated string or array of qBittorrent tags."},
            "upload_limit": {"type": ["string", "integer"], "description": "Optional upload limit, e.g. 2MiB/s or bytes per second."},
            "download_limit": {"type": ["string", "integer"], "description": "Optional download limit, e.g. 20MiB/s or bytes per second."},
            "paused": {"type": "boolean", "default": False},
            "skip_checking": {"type": "boolean", "default": False},
            "verify_timeout": {"type": "number", "default": 5.0, "minimum": 0},
            "verify_interval": {"type": "number", "default": 0.5, "exclusiveMinimum": 0},
        },
    }


def _qbit_wait_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [],
        "properties": {
            "client": {"type": "string", "default": "default"},
            "config": {"type": "string", "description": "Path to data/config.py inside the container."},
            "hash": {"type": "string", "description": "qBittorrent torrent hash/infohash to wait for. Either hash or path is required."},
            "torrent_hash": {"type": "string", "description": "Alias for hash."},
            "infohash": {"type": "string", "description": "Alias for hash."},
            "path": {"type": "string", "description": "Seedbox content/save path to wait for when hash is not known. Either hash or path is required."},
            "content_path": {"type": "string", "description": "Alias for path."},
            "timeout": {"type": "number", "default": 3600.0, "minimum": 0},
            "interval": {"type": "number", "default": 30.0, "exclusiveMinimum": 0},
        },
    }


def _site_policy_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [],
        "properties": {
            "trackers": {"type": ["string", "array"], "description": "Comma-separated tracker codes or an array, e.g. U2,MTEAM."},
            "source_tracker": {"type": "string", "description": "Optional source tracker code when asking for a source/target pair."},
            "target": {"type": ["string", "array"], "description": "Optional target tracker code(s) when asking for a source/target pair."},
            "accept_rules": {"type": "boolean", "description": "Whether manual rule review obligations are acknowledged for this policy read."},
            "config": {"type": "string", "description": "Path to data/config.py inside the container."},
        },
    }


def _readiness_bundle_tool_request_schema() -> dict[str, Any]:
    properties = {
        **_source_url_retorrent_tool_request_schema()["properties"],
        **_daily_candidate_schedule_tool_request_schema()["properties"],
        "source_id": {"type": "string", "description": "Optional source torrent id when source_tracker is provided instead of a full source_url."},
        "cookies_dir": {"type": "string"},
        "job_dir": {"type": "string"},
        "downloads_path": {"type": "string"},
        "compose_file": {"type": "string"},
        "max_concurrent_jobs": {"type": "integer", "default": DEFAULT_MAX_CONCURRENT_JOBS},
    }
    return {
        "type": "object",
        "required": [],
        "properties": properties,
    }


def _job_id_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["job_id"],
        "properties": {"job_id": {"type": "string", "description": "32-character ptcli job id returned by a job creation endpoint."}},
    }


def _job_resume_tool_request_schema() -> dict[str, Any]:
    schema = json.loads(json.dumps(_job_id_tool_request_schema()))
    properties = schema["properties"]
    properties["dry_run"] = {"type": "boolean", "description": "Preview the patched allowlisted resume command without creating a child job or executing it."}
    for key in RESUME_BOOLEAN_FLAG_OVERRIDES:
        properties[key] = {"type": "boolean", "description": f"Optional resume override for {RESUME_BOOLEAN_FLAG_OVERRIDES[key]}; only true values are applied."}
    for key in RESUME_VALUE_FLAG_OVERRIDES:
        properties[key] = {"type": "string", "description": f"Optional resume override for {RESUME_VALUE_FLAG_OVERRIDES[key]}."}
    properties["screenshot_file"] = {"type": "string", "description": "Optional single screenshot file override; use screenshot_files for multiple files."}
    properties["screenshot_files"] = {"type": "array", "items": {"type": "string"}, "description": "Optional repeatable --screenshot-file override."}
    return schema


def _job_cancel_tool_request_schema() -> dict[str, Any]:
    schema = json.loads(json.dumps(_job_id_tool_request_schema()))
    schema["properties"]["reason"] = {"type": "string", "description": "Optional audit reason for cancelling a queued job."}
    return schema


def _job_list_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [],
        "properties": {
            "status": {"type": "string", "enum": JOB_STATUS_VALUES},
            "kind": {"type": "string"},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
        },
    }


def _without_execute_fields(schema: dict[str, Any]) -> dict[str, Any]:
    copy = json.loads(json.dumps(schema))
    properties = copy.get("properties")
    if isinstance(properties, dict):
        for key in ("execute", "confirm_upload"):
            properties.pop(key, None)
    return copy


def _sync_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "request", "duplicate_check", "submit_if_clear_handoff", "blockers", "next_actions", "command_argv", "result"],
        "status_values": ["ok", "blocked", "error", "complete"],
        "blocked_fields": ["blockers", "next_actions"],
        "submit_if_clear_handoff_fields": ["ready", "duplicate_clear", "tool", "endpoint", "method", "request", "requires_before_call", "next_step", "blockers", "next_actions"],
    }


def _agent_run_preview_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "kind", "dry_run", "mutates_state", "live_upload", "workflow", "tool", "request", "request_template", "closure_contract", "closure_handoff_examples", "one_call_handoff", "steps", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers", "next_actions"],
        "workflows": ["source_url_retorrent", "daily_candidates"],
        "one_call_fields": ["ready", "tool", "endpoint", "method", "request", "continue_when", "stop_when", "then_follow", "next_step", "blockers"],
        "step_fields": ["index", "step", "tool", "endpoint", "method", "request", "read", "continue_when", "repeat_when", "stop_when", "complete_when", "resume_with"],
        "closure_examples": ["complete", "resume", "stop"],
        "safety": ["does_not_contact_trackers", "does_not_contact_qbittorrent", "does_not_create_jobs", "does_not_upload"],
    }


def _source_url_preflight_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "ready", "dry_run", "mutates_state", "live_upload", "request", "source_reference", "target_trackers", "ready_to_create_job", "ready_for_live_upload", "duplicate_check", "duplicate_check_handoff", "readiness_bundle", "policy_execution_summary", "policy_execution_handoff", "job_template", "job_creation_handoff", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "agent_decision", "blockers", "warnings", "next_actions", "safety"],
        "next_step_fields": ["tool", "endpoint", "method", "request", "reason", "blockers"],
        "policy_execution_handoff_fields": ["ready", "phase", "qbit", "seeding", "transfer_rules", "rule_obligations", "config", "next_step", "blockers", "next_actions"],
        "duplicate_check_fields": ["searched", "status", "exists", "count", "dupes", "reason", "ready_to_check", "next_tool", "next_endpoint", "next_request", "continue_when", "stop_when"],
        "duplicate_handoff_fields": ["ready", "tool", "endpoint", "method", "request", "read", "continue_when", "stop_when", "then_tool", "then_endpoint", "then_request"],
        "job_template_fields": ["tool", "endpoint", "request"],
        "job_creation_handoff_fields": ["ready_after_duplicate_clear", "tool", "endpoint", "method", "request", "requires_before_call"],
        "safety": ["does_not_create_job", "does_not_contact_trackers", "does_not_contact_qbittorrent", "live_job_requires_accept_rules_confirm_upload_policy_and_duplicate_clear"],
    }


def _source_url_check_and_submit_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "mutates_state", "live_upload", "check_result", "duplicate_check", "submit_if_clear_handoff", "job_id", "submitted_job", "status_endpoint", "summary_endpoint", "agent_summary", "blockers", "next_actions"],
        "status_values": ["ok", "blocked"],
        "duplicate_check_fields": ["searched", "exists", "count", "dupes"],
        "submit_if_clear_handoff_fields": ["ready", "duplicate_clear", "request", "requires_before_call", "blockers", "next_step"],
        "agent_summary_fields": ["ready", "duplicate_searched", "duplicate_exists", "duplicate_count", "submit_ready", "job_id", "job_status", "blocker_count"],
        "safety": ["runs_duplicate_check_before_job_creation", "stops_when_duplicate_exists", "live_upload_requires_accept_rules_and_confirm_upload"],
    }


def _readiness_bundle_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "ready", "request", "deployment", "site_policies", "daily_schedule", "live_verification", "live_readiness", "live_test_handoff", "seedbox_live_validation_handoff", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "agent_decision", "blockers", "warnings", "next_actions"],
        "live_verification_fields": ["ready", "connectivity_checked", "checks", "credential_requirements", "flow_check", "materials", "blockers", "warnings", "next_actions"],
        "live_readiness_fields": ["ready_for_ai", "ready_for_manual_retorrent", "ready_for_daily_candidates", "source", "target_trackers", "site_policy_ready", "policy_execution_summary", "policy_setup_summary", "policy_execution_handoff", "live_verification_ready", "credential_requirements", "doctor_template", "manual_job_template", "blockers", "warnings", "next_actions"],
        "live_test_handoff_fields": ["ready", "doctor_ready", "manual_job_ready", "preflight_checklist", "execution_plan", "doctor_template", "manual_job_template", "policy_execution_summary", "policy_execution_handoff", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "after_doctor", "blockers", "warnings"],
        "seedbox_live_validation_handoff_fields": ["ready", "phase", "connectivity_checked", "preflight_ready", "preflight_checklist", "execution_plan", "docker_compose", "qbit", "site_policy", "credentials", "doctor", "manual_job", "validation_plan", "post_submit_handoff", "evidence_contract", "recommended_tool", "recommended_endpoint", "recommended_request", "next_step", "continue_when", "stop_when", "blockers", "warnings", "next_actions"],
        "seedbox_live_validation_plan_fields": ["ready", "first_step", "steps", "required_order", "read_first"],
        "seedbox_post_submit_handoff_fields": ["ready", "submit_tool", "submit_endpoint", "submit_request", "poll_tool", "poll_until", "resume_tool", "resume_when", "finish_tool", "complete_when", "stop_when"],
        "seedbox_live_evidence_contract_fields": ["final_read", "complete_when", "required_fields", "audit_notes"],
        "agent_decision_fields": ["decision", "recommended_action", "runbook_ref", "next_tool", "can_create_manual_job", "can_run_daily_candidates", "should_fix_deployment", "next_actions"],
        "safety": ["non_live", "does_not_contact_trackers", "does_not_contact_qbittorrent", "live_upload_still_requires_accept_rules_and_confirm_upload"],
    }


def _sites_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "ready", "sites", "capability_matrix", "adapter_profiles", "policy_matrix", "policy_execution_summary", "extension_plan", "extension_handoff", "flow_matrix", "agent_summary", "blockers", "next_actions"],
        "capability_fields": ["tracker", "capabilities", "adapter_profile", "policy_profile", "execution_readiness", "ready_for_source", "ready_for_mteam_target_flow", "ready_as_target"],
        "adapter_profile_fields": ["tracker", "source_info", "source_info_adapter", "source_download", "source_download_adapter", "target_upload", "target_upload_adapter", "credential_requirements", "mteam_source_flow", "full_live_closure_to_mteam", "implemented_roles", "extension_notes", "extension_checklist"],
        "policy_profile_fields": ["config_path", "required_fields", "optional_fields", "accepted_config_shapes", "missing_fields", "template", "flat_template", "structured_template", "current_values", "next_actions"],
        "extension_plan_fields": ["ready", "trackers", "ready_sources", "ready_targets", "reference_sources_to_mteam", "items", "next_item", "blockers", "next_actions"],
        "extension_item_fields": ["tracker", "source_ready", "target_ready", "full_live_closure_to_mteam", "has_reference_flow", "implemented_roles", "missing_components", "checklist", "blockers", "next_action"],
        "extension_handoff_fields": ["ready", "phase", "recommended_next_tracker", "reference_flow", "implementation_order", "tracker_steps", "endpoint_sequence", "validation_sequence", "continue_when", "stop_when", "blockers", "next_actions"],
        "agent_summary_fields": ["ready", "site_count", "source_info_count", "source_download_count", "target_upload_count", "full_live_closure_to_mteam_count", "policy_profile_count", "reference_flow_count", "extension_ready", "extension_blocker_count", "recommended_next_tool", "blocker_count"],
        "safety": ["does_not_contact_trackers", "does_not_contact_qbittorrent", "does_not_upload"],
    }


def _qbit_inspect_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "read_only", "client", "request", "count", "torrents", "agent_summary", "blockers", "next_actions"],
        "torrent_fields": ["name", "hash", "save_path", "content_path", "size", "progress", "state", "category", "tags", "tracker"],
        "agent_summary_fields": ["ready", "read_only", "client", "query_hash", "torrent_count", "complete_count", "incomplete_count", "hashes"],
        "safety": ["read_only", "does_not_add_torrents", "does_not_export_torrents", "does_not_change_rate_limits"],
    }


def _qbit_match_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "read_only", "client", "request", "path", "count", "matched", "matches", "agent_summary", "blockers", "next_actions"],
        "match_fields": ["name", "hash", "save_path", "content_path", "size", "progress", "state", "category", "tags", "tracker"],
        "agent_summary_fields": ["ready", "read_only", "client", "path", "matched", "matched_count", "complete_count", "hashes", "blocker_count"],
        "safety": ["read_only", "does_not_add_torrents", "does_not_export_torrents", "does_not_change_rate_limits"],
    }


def _qbit_export_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "read_only_client", "mutates_filesystem", "client", "request", "hash", "exported_path", "target_torrent_file", "candidate", "evidence", "target_upload_handoff", "agent_summary", "blockers", "next_actions"],
        "evidence_fields": ["exported", "candidate", "candidate_mteam_safe"],
        "target_upload_handoff_fields": ["ready", "target", "target_torrent_file", "request_fields", "requires_before_upload", "next_step", "blockers"],
        "agent_summary_fields": ["ready", "client", "hash", "target", "target_torrent_file", "mteam_safe", "recommended_next_tool"],
        "safety": ["does_not_add_torrents", "does_not_upload", "does_not_change_rate_limits", "writes_exported_torrent_files"],
    }


def _qbit_inject_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "mutates_qbittorrent", "live_upload", "client", "request", "torrent_path", "hash", "save_path", "category", "tags", "upload_limit", "download_limit", "rate_limits", "paused", "skip_checking", "visible_in_client", "verified_in_client", "client_verification", "client_matches", "agent_summary", "blockers", "next_actions"],
        "rate_limit_fields": ["requested", "applied", "skipped", "calls"],
        "client_verification_fields": ["visible", "hash_matched", "save_path_matched", "category_matched", "tags_matched", "requested", "observed"],
        "agent_summary_fields": ["ready", "mutates_qbittorrent", "client", "hash", "torrent_file", "save_path", "category", "tags", "visible_in_client", "verified_in_client", "rate_limits_requested", "rate_limits_applied", "blocker_count"],
        "safety": ["adds_torrent_to_qbittorrent", "may_change_qbittorrent_rate_limits", "does_not_upload"],
    }


def _qbit_wait_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "read_only", "client", "request", "complete", "query", "matched_count", "completion_verification", "matches", "agent_summary", "blockers", "next_actions"],
        "completion_verification_fields": ["matched_count", "complete_count", "seeding_state_count", "all_matches_complete", "any_complete", "requested_hash_matched", "requested_content_path_matched", "observed_hashes", "observed_content_paths", "observed_save_paths", "observed_states", "observed_progress"],
        "match_fields": ["name", "hash", "save_path", "content_path", "size", "progress", "state", "category", "tags", "tracker"],
        "agent_summary_fields": ["ready", "read_only", "client", "hash", "path", "complete", "matched_count", "complete_count", "observed_hashes", "observed_content_paths", "blocker_count"],
        "safety": ["read_only", "does_not_add_torrents", "does_not_upload", "does_not_change_rate_limits"],
    }


def _job_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "job_id", "kind", "request", "command_argv", "blockers", "next_actions", "interruption", "cancellation", "runtime", "summary_file", "resume_state", "agent_summary", "agent_decision", "candidate_digest", "submit_if_clear_handoff", "policy_coverage", "policy_handoff", "policy_qbit_defaults", "qbit_plan", "qbit_limit_audit", "qbit_handoff", "qbit_enforcement_summary", "materials_handoff", "target_upload_handoff", "closure_handoff", "closure_summary", "manual_retorrent_handoff", "candidate_batch_handoff", "candidate_submission_handoff", "candidate_submission_summary", "resume_plan", "resume_requirements", "resume_execution_handoff", "recovery_handoff", "resume_lineage", "job_lineage", "resume_context", "resume_audit", "resume_summary", "material_resolution", "candidate_submission", "check_submission", "source_reference", "workflow_context", "job_handoff"],
        "status_values": JOB_STATUS_VALUES,
        "blocked_fields": ["blockers", "next_actions", "interruption", "cancellation", "runtime", "resume_state", "resume_plan", "resume_requirements", "next_command_argv", "agent_decision"],
        "running_fields": ["runtime.should_poll", "runtime.poll_after_seconds", "runtime.status_endpoint", "agent_decision.should_poll"],
        "cancel_fields": ["cancellation", "agent_decision.stop_reason", "runtime.terminal"],
        "job_handoff_fields": ["action", "recommended_tool", "recommended_endpoint", "recommended_method", "recommended_request", "dry_run_request", "execute_request", "resume_execution_handoff", "candidate_submission_execution", "material_input_template", "continue_when", "stop_when", "status_endpoint", "summary_endpoint", "resume_endpoint", "poll_after_seconds", "should_poll", "can_resume", "resume_recommended", "can_attempt_live", "blockers", "next_actions"],
        "recovery_handoff_fields": ["phase", "action", "reason", "ready", "should_poll", "should_resume", "resume_preview_required", "recommended_tool", "recommended_endpoint", "recommended_method", "recommended_request", "dry_run_request", "execute_request", "status_endpoint", "summary_endpoint", "resume_endpoint", "poll_after_seconds", "gates", "handoff_sources", "read_fields", "continue_when", "stop_when", "blockers", "next_actions"],
        "request_fields": ["policy_coverage", "policy_qbit_defaults", "qbit_plan", "material_options", "qbit_upload_limit", "qbit_download_limit", "uploaded_qbit_upload_limit", "uploaded_qbit_download_limit", "qbit_category", "qbit_tags", "uploaded_qbit_category", "uploaded_qbit_tags"],
        "material_option_fields": ["metadata_file", "ptgen_description_file", "mediainfo_file", "bdinfo_file", "image_host_file", "screenshot_files", "enrich_metadata", "fetch_ptgen", "generate_mediainfo", "generate_bdinfo", "generate_screenshots", "upload_screenshots"],
        "resume_requirement_fields": ["can_call_resume", "resume_recommended", "subcommand", "missing_confirmations", "required_overrides", "suggested_overrides", "request_template", "dry_run_request", "execute_request", "recommended_inputs", "allowed_overrides", "current_flags"],
        "resume_execution_handoff_fields": ["ready", "status", "subcommand", "endpoint", "method", "preview_required", "dry_run_request", "execute_request", "recommended_request", "allowed_overrides", "required_overrides", "suggested_overrides", "recommended_inputs", "unresolved_recommended_inputs", "safety_gates", "continue_when", "execute_when", "stop_when", "blockers", "next_actions"],
        "resume_preview_fields": ["dry_run", "mutates_state", "live_upload", "command_argv", "original_next_command_argv", "applied_overrides", "ignored_overrides", "material_resolution", "resume_context", "resume_lineage", "resume_plan", "resume_requirements", "agent_decision"],
        "resume_audit_fields": ["is_resume_job", "parent_job_id", "parent_status", "parent_kind", "child_status", "resume_available", "resume_allowed", "resume_recommended", "next_subcommand", "next_command_argv", "applied_override_keys", "ignored_override_keys", "covered_recommended_inputs", "unresolved_recommended_inputs", "dry_run_request", "execute_request", "next_step", "next_actions"],
        "job_lineage_fields": ["job_id", "parent_job_id", "root_job_id", "depth", "is_resume_job", "chain", "child_count", "children", "latest_child", "has_active_child", "terminal_child_count", "next_actions"],
        "resume_summary_fields": ["available", "allowed", "recommended", "status", "subcommand", "missing_confirmations", "recommended_input_keys", "unresolved_recommended_inputs", "dry_run_request", "execute_request", "next_step", "recommended_tool", "blockers", "next_actions"],
        "recommended_input_fields": ["key", "accepted_keys", "required", "reason", "stage", "resume_tool", "resume_endpoint_hint", "blocking_keys", "examples"],
        "materials_handoff_fields": ["ready", "can_prepare_upload_payload", "metadata", "materials", "target_preflight", "material_plan", "resume_request_template", "resume_handoff", "recommended_inputs", "blockers", "next_actions"],
        "material_plan_fields": ["ready", "missing", "next_item", "items"],
        "material_plan_item_fields": ["key", "label", "ready", "stage", "recommended_input_key", "accepted_keys", "blocking_keys", "next_step", "resume_overrides"],
        "materials_resume_handoff_fields": ["ready", "resume_recommended", "recommended_tool", "recommended_endpoint", "method", "next_item", "missing", "accepted_override_keys", "dry_run_request", "execute_request", "recommended_request", "staged_requests", "continue_when", "stop_when"],
        "material_resolution_fields": ["ready_before_resume", "recommended_inputs", "applied_override_keys", "covered_recommended_inputs", "unresolved_recommended_inputs", "blockers_before_resume"],
        "qbit_handoff_fields": ["ready", "source", "uploaded", "enforcement_handoff", "policy_defaults", "blockers", "next_actions"],
        "qbit_enforcement_handoff_fields": ["ready", "status", "roles", "pending_roles", "mismatch_roles", "blockers", "next_step", "continue_when", "stop_when"],
        "qbit_enforcement_summary_fields": ["ready", "status", "client", "expected_role_count", "applied_role_count", "pending_role_count", "mismatch_role_count", "expected_roles", "applied_roles", "pending_roles", "mismatch_roles", "roles", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers", "next_actions"],
        "qbit_enforcement_role_fields": ["role", "ready", "status", "category", "tags", "expected_limits", "observed_limits", "upload_limit_source", "download_limit_source", "evidence_present", "requires_injection_evidence", "requires_rate_limit_repair", "blockers", "requested_options"],
        "target_upload_handoff_fields": ["action", "ready_for_live_upload", "uploaded_seeding_ready", "preflight", "duplicate_clear", "missing_confirmations", "policy_coverage_ready", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers", "next_actions"],
        "policy_handoff_fields": ["ready", "accepted_rules", "site_policy_ready", "source", "targets", "missing_policy_fields", "disabled_automation", "qbit_defaults", "qbit_plan", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers", "next_actions"],
        "manual_retorrent_handoff_fields": ["action", "live_ready", "live_checklist", "duplicate_clear", "missing_confirmations", "policy_coverage_ready", "can_attempt_live", "can_resume", "resume_plan", "blockers", "next_actions"],
        "candidate_batch_handoff_fields": ["ready", "candidate_job_id", "status", "submit_count", "submit_tool", "submit_endpoint", "submit_endpoint_template", "required_overrides", "allowed_selector_fields", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "items", "blockers", "next_actions"],
        "candidate_batch_item_fields": ["candidate_job_id", "submit_tool", "submit_endpoint", "selector", "request_template", "identity_inherited_from_candidate", "policy_execution", "required_overrides", "allowed_overrides", "after_submit"],
        "candidate_submission_handoff_fields": ["candidate_job_id", "candidate_rank", "candidate_source_id", "inherited_request", "submitted_overrides", "material_options", "qbit_overrides", "policy_execution_handoff", "execution_state", "execution_handoff", "retorrent_job_id", "manual_retorrent_handoff", "status_endpoint", "summary_endpoint", "parent_status_endpoint", "parent_summary_endpoint", "next_actions"],
        "candidate_submission_summary_fields": ["candidate_job_id", "retorrent_job_id", "candidate_rank", "candidate_source_id", "submitted_override_keys", "material_option_keys", "qbit_override_keys", "policy_execution_handoff", "policy_execution_ready", "execution_state", "execution_handoff", "manual_action", "closure_action", "closure_complete", "next_step", "recommended_tool", "blockers", "next_actions"],
        "agent_candidate_submission_fields": ["candidate_submission", "candidate_submission_summary", "candidate_submission_handoff", "candidate_submission_execution", "material_input_template"],
        "candidate_submission_execution_handoff_fields": ["state", "reason", "status", "ready_for_live", "should_poll", "should_resume", "should_stop", "manual_action", "closure_action", "policy_execution_ready", "recommended_tool", "recommended_endpoint", "recommended_method", "recommended_request", "material_input_template", "continue_when", "stop_when", "blockers", "next_actions"],
        "candidate_submission_material_input_template_fields": ["ready", "recommended_input_keys", "recommended_inputs", "missing", "next_item", "accepted_override_keys", "resume_request_template", "dry_run_request", "execute_request", "staged_requests", "examples_by_key", "continue_when", "stop_when"],
        "check_submission_fields": ["check_job_id", "check_status", "check_kind", "check_summary_file", "duplicate_check", "inherited_request", "submitted_overrides", "material_options", "qbit_overrides"],
        "submit_if_clear_handoff_fields": ["ready", "duplicate_clear", "tool", "endpoint", "method", "request", "requires_before_call", "next_step", "blockers", "next_actions"],
        "closure_handoff_fields": ["action", "complete", "closure_checklist", "source", "target", "evidence", "duplicate_check", "target_upload_handoff", "qbit_handoff", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers", "next_actions"],
        "closure_summary_fields": ["complete", "ready_for_report", "action", "gates", "source", "target", "duplicate_check", "materials", "policy", "qbit", "evidence", "next_step", "recommended_tool", "blockers", "next_actions"],
    }


def _daily_candidate_job_response_contract() -> dict[str, Any]:
    contract = _job_response_contract()
    candidate_contract = _candidate_response_contract()
    contract.update(
        {
            "result_fields": ["ranking", "digest", "candidates", "ready_count"],
            "digest_fields": candidate_contract["digest_fields"],
            "candidate_fields": candidate_contract["candidate_fields"],
            "push_item_fields": candidate_contract["push_item_fields"],
            "push_payload_fields": candidate_contract["push_payload_fields"],
            "approval_queue_fields": candidate_contract["approval_queue_fields"],
            "approval_queue_item_fields": candidate_contract["approval_queue_item_fields"],
            "policy_summary_fields": candidate_contract["policy_summary_fields"],
            "policy_risk_summary_fields": candidate_contract["policy_risk_summary_fields"],
            "policy_execution_handoff_fields": candidate_contract["policy_execution_handoff_fields"],
            "policy_coverage_fields": candidate_contract["policy_coverage_fields"],
            "rule_fields": candidate_contract["rule_fields"],
            "rule_fingerprint_status_fields": candidate_contract["rule_fingerprint_status_fields"],
        }
    )
    return contract


def _job_list_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "count", "total", "limit", "filters", "status_counts", "queue", "jobs", "next_actions"],
        "job_fields": ["job_id", "kind", "status", "blockers", "next_actions", "interruption", "cancellation", "runtime", "summary_file", "candidate_submission", "check_submission", "candidate_batch_handoff", "candidate_submission_handoff", "candidate_submission_summary", "source_reference", "duplicate_check", "submit_if_clear_handoff", "policy_handoff", "qbit_plan", "qbit_limit_audit", "qbit_handoff", "qbit_enforcement_summary", "materials_handoff", "target_upload_handoff", "closure_handoff", "manual_retorrent_handoff", "agent_decision", "resume_plan", "resume_requirements", "resume_execution_handoff", "recovery_handoff", "resume_lineage", "job_lineage", "resume_summary", "material_resolution", "job_handoff", "status_endpoint", "summary_endpoint", "resume_endpoint"],
        "filters": ["status", "kind", "limit"],
        "queue_fields": ["max_concurrent_jobs", "running_count", "queued_count", "available_slots", "backlog_count"],
    }


def _candidate_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "target_count", "scan_count", "count", "ready_count", "shortfall_count", "target_met", "target_summary", "site_policy", "ranking", "digest", "candidates", "blockers", "next_actions"],
        "digest_fields": [
            "recommendation",
            "recommended_action",
            "target_count",
            "scan_count",
            "selected_count",
            "shortfall_count",
            "target_met",
            "target_summary",
            "push_title",
            "push_summary",
            "push_payload",
            "approval_queue",
            "top_safe_candidates",
            "push_count",
            "ready_count",
            "review_count",
            "blocked_count",
            "top_candidate",
            "top_submit_request",
            "top_submit_job_endpoint",
            "top_submit_tool",
            "push_items",
            "decision_summary",
            "blockers",
            "next_actions",
        ],
        "push_payload_fields": [
            "title",
            "summary",
            "message",
            "format",
            "target_count",
            "scan_count",
            "item_count",
            "ready_count",
            "blocked_count",
            "shortfall_count",
            "target_met",
            "target_summary",
            "recommended_action",
            "decision_summary",
            "policy_risk_summary",
            "approval_queue",
            "top_safe_candidates",
            "top_item",
            "items",
            "blockers",
            "next_actions",
        ],
        "candidate_fields": [
            "status",
            "source",
            "source_info",
            "duplicate_check",
            "source_policy",
            "target_policies",
            "policy_summary",
            "policy_risk_summary",
            "policy_coverage",
            "policy_execution_handoff",
            "ranking",
            "decision_summary",
            "audit_summary",
            "recommendation",
            "blockers",
            "agent_workflow",
            "submit_request",
            "submit_job_endpoint",
            "submit_tool",
            "execute_request",
            "execute_job_endpoint",
        ],
        "push_item_fields": [
            "rank",
            "summary_text",
            "source_tracker",
            "source_id",
            "source_url",
            "title",
            "size",
            "published_at",
            "promotion",
            "metadata",
            "duplicate_status",
            "duplicate_count",
            "score",
            "tier",
            "decision_summary",
            "audit_summary",
            "policy_summary",
            "policy_risk_summary",
            "policy_execution_handoff",
            "blockers",
            "next_actions",
            "can_submit",
            "action_label",
            "action_endpoint",
            "submit_request",
            "submit_job_endpoint",
            "submit_tool",
        ],
        "policy_summary_fields": ["manual_review_ready", "automation", "policy_coverage", "policy_execution_handoff", "policy_risk_summary", "qbit_limits", "seeding_requirements", "transfer_rules", "rules"],
        "policy_risk_summary_fields": ["ready", "risk_level", "execution_priority", "qbit_limit_ready", "seeding_ready", "rule_obligations_ready", "manual_review_ready", "strict_transfer_rule_count", "strict_transfer_rules", "qbit_missing", "seeding_missing", "blockers", "next_actions"],
        "approval_queue_fields": ["ready", "safe_count", "guarded_count", "blocked_count", "recommended_count", "items", "top_safe_candidates", "guarded_source_ids", "blocked_source_ids", "submit_tool", "submit_endpoint", "requires_confirmation", "continue_when", "stop_when", "next_actions"],
        "approval_queue_item_fields": ["rank", "source_tracker", "source_id", "source_url", "title", "score", "risk_level", "policy_risk_level", "execution_priority", "duplicate_clear", "metadata", "policy_risk_summary", "submit_tool", "submit_endpoint", "request", "requires_confirmation"],
        "policy_execution_handoff_fields": ["ready", "accepted_rules", "phase", "qbit", "seeding", "transfer_rules", "rule_obligations", "missing_by_category", "continue_when", "stop_when", "blockers", "next_actions"],
        "policy_coverage_fields": ["ready", "rule_obligations_ready", "source", "targets", "missing_policy_fields", "disabled_automation", "recommendations"],
        "rule_fields": ["source_rules_url", "target_rules_urls", "source_fingerprint", "target_fingerprints", "fingerprint_status"],
        "rule_fingerprint_status_fields": ["tracker", "manual_review_required", "ready", "missing", "placeholder", "fingerprint"],
        "ranking": {"score_range": "0-100", "tiers": ["ready", "review", "blocked"], "sort": "ready-first, then descending score"},
    }


def _live_upload_safety_contract() -> dict[str, Any]:
    return {
        "mutates_state": True,
        "live_upload": True,
        "requires_confirmation": ["accept_rules=true", "confirm_upload=true"],
        "must_stop_when": ["duplicate_check.exists=true", "rule gate not ready", "site policy not ready", "uploaded target torrent cannot be injected for seeding"],
    }


def agent_manifest_payload(*, base_url: str | None = None) -> dict[str, Any]:
    public_base_url = (base_url or os.environ.get("PTCLI_PUBLIC_BASE_URL") or "http://127.0.0.1:8080").rstrip("/")
    tools = tools_payload()["tools"]
    return {
        "schema_version": "ptcli.agent_manifest.v1",
        "name": "ptcli-retorrent",
        "display_name": "PTCLI Retorrent Assistant",
        "description": "AI-callable Chinese PT retorrent, upload, qBittorrent, and daily-candidate automation service.",
        "audience": ["OpenClaw", "Hermes", "OpenAPI-compatible agents"],
        "base_url": public_base_url,
        "auth": {
            "type": "bearer",
            "header": "Authorization",
            "format": "Bearer <PTCLI_API_TOKEN>",
            "required_when": "PTCLI_API_TOKEN is configured on the service.",
            "env": "PTCLI_API_TOKEN",
        },
        "discovery": {
            "health": f"{public_base_url}/health",
            "deployment_check": f"{public_base_url}/v1/deployment/check",
            "source_url_preflight": f"{public_base_url}/v1/retorrent/source-url/preflight",
            "readiness_bundle": f"{public_base_url}/v1/readiness/bundle",
            "openapi": f"{public_base_url}/openapi.json",
            "tools": f"{public_base_url}/v1/tools",
            "manifest": f"{public_base_url}/.well-known/ptcli-agent.json",
        },
        "safety": {
            "live_upload_requires": ["accept_rules=true", "confirm_upload=true"],
            "never_skip": ["site rule gates", "target duplicate check", "uploaded torrent injection/seeding evidence"],
            "blocked_contract": "When rules, duplicate checks, qBittorrent evidence, or required confirmations are missing, APIs return status=blocked with blockers and next_actions.",
        },
        "closure_contract": _agent_closure_contract(),
        "default_workflows": _agent_default_workflows(),
        "tools": tools,
        "tool_count": len(tools),
        "openclaw": {
            "skill_id": "ptcli-retorrent",
            "entrypoint": f"{public_base_url}/openapi.json",
            "manifest_url": f"{public_base_url}/v1/openclaw/skill.json",
        },
        "hermes": {
            "skill_id": "ptcli-retorrent",
            "entrypoint": f"{public_base_url}/openapi.json",
            "manifest_url": f"{public_base_url}/v1/hermes/skill.json",
        },
    }


def _agent_default_workflows() -> list[dict[str, Any]]:
    return [
        {
            "name": "source_url_retorrent",
            "tool": "source_url_check_and_submit",
            "fallback_tool": "source_url_retorrent_job",
            "description": "Recommended flow when a user sends one source tracker link and a target tracker. The service infers tracker/torrent id, checks duplicates, then proceeds only when rules and confirmations allow.",
            "required_fields": ["source_url", "target"],
            "recommended_fields": ["save_path", "accept_rules", "confirm_upload", "uploaded_qbit_category", "uploaded_qbit_tags"],
            "one_call": {
                "tool": "source_url_check_and_submit",
                "endpoint": "/v1/jobs/retorrent/from-url/check-and-submit",
                "method": "POST",
                "continue_when": "ok=true and job_id is present",
                "stop_when": ["duplicate_check.exists=true", "submit_if_clear_handoff.ready=false", "status=blocked"],
                "then_follow": "poll",
            },
            "runbook": [
                {
                    "step": "preflight",
                    "tool": "source_url_retorrent_preflight",
                    "read": ["ready_to_create_job", "source_reference", "target_trackers", "policy_execution_summary", "policy_execution_handoff", "duplicate_check", "duplicate_check_handoff", "job_creation_handoff.request", "next_step", "blockers"],
                    "continue_when": "ready_to_create_job=true",
                    "stop_when": ["source_reference.error is present", "policy_execution_handoff.ready=false", "policy_execution_summary.ready=false", "accept_rules or confirm_upload missing", "deployment.ready=false"],
                },
                {
                    "step": "readiness_bundle",
                    "tool": "readiness_bundle",
                    "read": ["live_readiness.ready_for_manual_retorrent", "live_readiness.policy_execution_summary", "live_readiness.policy_execution_handoff", "live_readiness.blockers", "live_readiness.manual_job_template.request"],
                    "continue_when": "live_readiness.ready_for_manual_retorrent=true",
                    "stop_when": ["deployment.ready=false", "live_readiness.policy_execution_handoff.ready=false", "policy_execution_summary.ready=false", "accept_rules or confirm_upload missing"],
                },
                {
                    "step": "policy_audit",
                    "tool": "site_policies",
                    "read": ["ready", "policy_execution_summary.ready", "policy_execution_handoff.ready", "policy_execution_handoff.qbit", "policy_execution_handoff.seeding", "policy_execution_handoff.transfer_rules", "policy_execution_handoff.rule_obligations", "policy_gap_summary", "execution_readiness", "agent_summary.policy_recommendations"],
                    "continue_when": "ready=true and policy_execution_handoff.ready=true",
                    "stop_when": ["missing rule_review_fingerprint", "missing qBittorrent rate limits", "missing seeding requirements", "automation disabled"],
                },
                {
                    "step": "duplicate_check",
                    "tool": "retorrent_check",
                    "request_from": "source_url_retorrent_preflight.duplicate_check_handoff.request",
                    "read": ["duplicate_check.searched", "duplicate_check.exists", "duplicate_check.count", "duplicate_check.dupes", "blockers", "next_actions"],
                    "continue_when": "duplicate_check.searched=true and duplicate_check.exists=false",
                    "stop_when": ["duplicate_check.exists=true", "status=blocked", "status=error"],
                },
                {
                    "step": "submit_job",
                    "tool": "source_url_retorrent_job",
                    "request_from": "source_url_retorrent_preflight.job_creation_handoff.request after duplicate_check.exists=false",
                    "read": ["job_id", "status", "job_handoff", "runtime.status_endpoint", "agent_decision", "closure_summary", "closure_handoff", "materials_handoff", "target_upload_handoff", "workflow_context"],
                    "continue_when": "job_id is present",
                    "stop_when": ["job_handoff.action=stop", "closure_handoff.action=stop_duplicate", "closure_handoff.action=configure_policy", "closure_handoff.action=collect_confirmations"],
                },
                {
                    "step": "poll",
                    "tool": "get_job_status",
                    "read": ["status", "job_handoff.action", "job_handoff.should_poll", "job_handoff.poll_after_seconds", "job_handoff.recommended_tool", "job_handoff.recommended_endpoint", "job_handoff.recommended_request", "runtime.should_poll", "agent_decision", "closure_summary", "closure_handoff", "blockers", "next_actions"],
                    "continue_when": "job_handoff.action!=wait and status not in queued,running",
                    "repeat_when": "job_handoff.action=wait and job_handoff.should_poll=true",
                    "stop_when": ["job_handoff.action=stop", "status=blocked", "status=failed", "status=cancelled"],
                },
                {
                    "step": "closure_decision",
                    "tool": "get_job_summary",
                    "read": ["job_handoff.action", "job_handoff.recommended_tool", "job_handoff.recommended_endpoint", "job_handoff.recommended_request", "job_handoff.blockers", "closure_summary.complete", "closure_summary.action", "closure_summary.next_step", "closure_summary.recommended_tool", "closure_summary.blockers", "closure_summary.gates", "closure_summary.source", "closure_summary.target", "closure_handoff", "summary", "evidence", "resume_plan", "resume_requirements", "candidate_submission"],
                    "complete_when": "job_handoff.action=done and closure_summary.complete=true and closure_summary.blockers=[]",
                    "resume_with": "job_handoff when job_handoff.action=resume; pass only job_handoff.recommended_request plus allowlisted overrides for confirmations, paths, qBittorrent limits, or material files",
                    "stop_when": ["job_handoff.action=stop", "closure_summary.action=stop_duplicate", "closure_summary.action=collect_confirmations without explicit user confirmation", "closure_summary.action=configure_policy", "closure_summary.action=resolve_blockers and recommended_tool is null"],
                },
            ],
        },
        {
            "name": "manual_retorrent",
            "tool": "manual_retorrent_job",
            "description": "Create the primary source URL plus target tracker job. It checks duplicates and only proceeds when rule, duplicate, and confirmation gates allow.",
            "required_fields": ["source", "target"],
            "recommended_fields": ["save_path", "accept_rules", "confirm_upload", "uploaded_qbit_category", "uploaded_qbit_tags"],
            "runbook_ref": "source_url_retorrent",
        },
        {
            "name": "daily_candidates",
            "tool": "daily_candidates_schedule_job",
            "description": "Find up to 10 ranked source/target retorrent candidates, then submit approved candidates through the inherited-identity handoff.",
            "required_fields": ["source_tracker", "target"],
            "read_result": ["schedule_digest", "candidate_digest", "digest", "candidates", "ready_count"],
            "runbook": [
                {
                    "step": "preflight",
                    "tool": "readiness_bundle",
                    "read": ["live_readiness.ready_for_daily_candidates", "daily_schedule.schedules", "agent_decision"],
                    "continue_when": "live_readiness.ready_for_daily_candidates=true",
                    "stop_when": ["deployment.ready=false", "daily schedule missing", "site policy not ready"],
                },
                {
                    "step": "create_candidate_jobs",
                    "tool": "daily_candidates_schedule_job",
                    "read": ["schedule_digest.items", "schedule_digest.push_items", "schedule_digest.submission_handoff"],
                    "continue_when": "schedule_digest.pending_job_count=0 and schedule_digest.submission_handoff.ready=true",
                    "repeat_when": "schedule_digest.pending_job_count>0",
                },
                {
                    "step": "submit_approved_candidate",
                    "tool": "submit_daily_candidate_job",
                    "request_from": "schedule_digest.submission_handoff.items[].request_template after human rule review",
                    "read": ["job_id", "job_handoff", "candidate_submission", "agent_decision", "closure_handoff", "qbit_plan"],
                    "stop_when": ["candidate can_submit=false", "confirm_upload missing", "save_path/path missing"],
                    "then_follow": "source_url_retorrent.poll via job_handoff",
                },
            ],
        },
        {
            "name": "resume_blocked_job",
            "tool": "resume_job",
            "description": "Resume a blocked/failed job using allowlisted next_command_argv emitted by ptcli summaries.",
            "required_fields": ["job_id"],
            "runbook": [
                {
                    "step": "inspect",
                    "tool": "get_job_status",
                    "read": ["job_handoff", "closure_handoff", "resume_plan", "resume_requirements", "resume_state", "next_command_argv", "blockers", "next_actions"],
                    "continue_when": "job_handoff.action=resume and resume_plan.allowed=true",
                    "stop_when": ["job_handoff.action=done", "job_handoff.action=stop", "resume_plan.allowed=false", "next_command_argv not allowlisted"],
                },
                {
                    "step": "resume",
                    "tool": "resume_job",
                    "read": ["job_id", "resume_requirements", "resume_lineage", "resume_context.applied_overrides", "runtime.status_endpoint"],
                    "then_follow": "source_url_retorrent.poll",
                },
            ],
        },
    ]


def _agent_closure_contract() -> dict[str, Any]:
    return {
        "primary_field": "closure_handoff",
        "control_field": "job_handoff",
        "complete_when": "closure_handoff.complete=true",
        "never_treat_complete_when": ["closure_handoff.action!=done", "closure_handoff.target.uploaded_seeding_ready=false", "closure_handoff.blockers is not empty"],
        "next_step_source": "job_handoff when present, otherwise closure_handoff.next_step",
        "recommended_call_fields": ["job_handoff.recommended_tool", "job_handoff.recommended_endpoint", "job_handoff.recommended_request", "recommended_tool", "recommended_endpoint", "recommended_request"],
        "actions": {
            "done": "Read get_job_summary and report the completed source/target/qBittorrent evidence.",
            "wait": "Poll get_job_status after runtime.poll_after_seconds.",
            "stop_duplicate": "Do not upload; report duplicate_check.dupes to the user.",
            "collect_confirmations": "Ask the user for explicit accept_rules/confirm_upload confirmation before resume.",
            "configure_policy": "Call site_policies or update PTCLI.SITE_POLICIES before live work.",
            "prepare_materials": "Use closure_handoff.next_step or resume_job with allowlisted material overrides.",
            "repair_target_payload": "Use closure_handoff.next_step after fixing target upload payload blockers.",
            "repair_qbit": "Use closure_handoff.next_step after reviewing qBittorrent evidence and rate-limit blockers.",
            "resolve_blockers": "Stop and report closure_handoff.blockers unless next_step provides a safe tool call.",
            "inspect": "Read get_job_summary and report uncertainty; do not attempt live upload.",
        },
    }


def openapi_payload(*, require_auth: bool | None = None) -> dict[str, Any]:
    token_security = [{"bearerAuth": []}] if (os.environ.get("PTCLI_API_TOKEN") if require_auth is None else require_auth) else []
    request_schema = {
        "type": "object",
        "required": ["source", "target"],
        "properties": {
            "source": {"type": "string", "description": "Source tracker details/download URL, or a torrent id when source_tracker is supplied."},
            "source_tracker": {"type": "string", "description": "Optional source tracker code. Usually inferred from source URL."},
            "target": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}], "description": "Target tracker code(s), currently full live closure is MTEAM-focused."},
            "execute": {"type": "boolean", "description": "When true, run retorrent --execute. When false or omitted, only check source metadata and target duplicates."},
            "accept_rules": {"type": "boolean", "description": "Required for live source download/upload automation."},
            "confirm_upload": {"type": "boolean", "description": "Required before live target upload."},
            "path": {"type": "string", "description": "Existing content path on the seedbox."},
            "save_path": {"type": "string", "description": "qBittorrent save path for source torrent injection when content is not already local."},
            "qbit_upload_limit": {"oneOf": [{"type": "string"}, {"type": "integer"}], "description": "Optional source qBittorrent upload limit, e.g. 500KiB/s or bytes/sec."},
            "qbit_download_limit": {"oneOf": [{"type": "string"}, {"type": "integer"}], "description": "Optional source qBittorrent download limit, e.g. 20MiB/s or bytes/sec."},
            "uploaded_qbit_upload_limit": {"oneOf": [{"type": "string"}, {"type": "integer"}], "description": "Optional uploaded target torrent qBittorrent upload limit."},
            "uploaded_qbit_download_limit": {"oneOf": [{"type": "string"}, {"type": "integer"}], "description": "Optional uploaded target torrent qBittorrent download limit."},
            "client": {"type": "string", "default": "default"},
            "config": {"type": "string", "description": "Path to data/config.py inside the container."},
            "metadata_file": {"type": "string"},
            "ptgen_description_file": {"type": "string"},
            "imdb_id": {"type": "string"},
            "tmdb_id": {"type": "string"},
            "tmdb_type": {"type": "string", "enum": ["movie", "tv"]},
            "douban_id": {"type": "string"},
            "douban_url": {"type": "string"},
        },
    }
    manual_request_schema = json.loads(json.dumps(request_schema))
    manual_properties = manual_request_schema.get("properties")
    if isinstance(manual_properties, dict):
        manual_properties.pop("execute", None)
        manual_properties["execute_if_no_duplicate"] = {
            "type": "boolean",
            "default": True,
            "description": "This endpoint treats the request as execute-if-clear; duplicate, rule, and confirmation gates still block unsafe work.",
        }
    source_url_request_schema = json.loads(json.dumps(manual_request_schema))
    source_url_request_schema["required"] = ["source_url", "target"]
    source_url_properties = source_url_request_schema.get("properties")
    if isinstance(source_url_properties, dict):
        source_url_properties["source_url"] = {
            "type": "string",
            "description": "User-provided source tracker details/download URL. The service infers source_tracker and torrent id from this URL.",
        }
        source_url_properties["source"]["description"] = "Alias for source_url; source_url is preferred for this endpoint."
    source_url_check_submit_request_schema = json.loads(json.dumps(source_url_request_schema))
    resume_request_schema = _job_resume_tool_request_schema()
    resume_request_schema["required"] = []
    resume_request_schema["properties"].pop("job_id", None)
    response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "request": {"type": "object"},
            "duplicate_check": {"type": "object"},
            "submit_if_clear_handoff": {"type": ["object", "null"]},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
            "command_argv": {"type": "array", "items": {"type": "string"}},
            "result": {"type": "object"},
        },
    }
    job_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": JOB_STATUS_VALUES},
            "ok": {"type": "boolean"},
            "job_id": {"type": "string"},
            "kind": {"type": "string"},
            "request": {"type": "object"},
            "policy_coverage": {"type": ["object", "null"]},
            "policy_handoff": {"type": ["object", "null"]},
            "command_argv": {"type": "array", "items": {"type": "string"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
            "interruption": {"type": ["object", "null"]},
            "cancellation": {"type": ["object", "null"]},
            "runtime": {"type": "object"},
            "duplicate_check": {"type": "object"},
            "submit_if_clear_handoff": {"type": ["object", "null"]},
            "summary_file": {"type": ["string", "null"]},
            "resume_state": {"type": ["object", "null"]},
            "agent_summary": {"type": ["object", "null"]},
            "agent_decision": {"type": ["object", "null"]},
            "candidate_digest": {"type": ["object", "null"]},
            "policy_qbit_defaults": {"type": ["object", "null"]},
            "qbit_plan": {"type": ["object", "null"]},
            "qbit_limit_audit": {"type": ["object", "null"]},
            "qbit_handoff": {"type": ["object", "null"]},
            "qbit_enforcement_summary": {"type": ["object", "null"]},
            "materials_handoff": {"type": ["object", "null"]},
            "target_upload_handoff": {"type": ["object", "null"]},
            "closure_handoff": {"type": ["object", "null"]},
            "closure_summary": {"type": "object"},
            "manual_retorrent_handoff": {"type": ["object", "null"]},
            "candidate_submission_handoff": {"type": ["object", "null"]},
            "candidate_submission_summary": {"type": ["object", "null"]},
            "resume_plan": {"type": "object"},
            "resume_requirements": {"type": "object"},
            "resume_lineage": {"type": ["object", "null"]},
            "job_lineage": {"type": "object"},
            "resume_context": {"type": ["object", "null"]},
            "resume_audit": {"type": "object"},
            "resume_summary": {"type": "object"},
            "material_resolution": {"type": ["object", "null"]},
            "candidate_submission": {"type": ["object", "null"]},
            "check_submission": {"type": ["object", "null"]},
            "source_reference": {"type": ["object", "null"]},
            "workflow_context": {"type": ["object", "null"]},
            "next_command_argv": {"type": ["array", "null"], "items": {"type": "string"}},
        },
    }
    source_url_check_submit_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok", "blocked"]},
            "ok": {"type": "boolean"},
            "mutates_state": {"type": "boolean"},
            "live_upload": {"type": "boolean"},
            "check_result": {"type": "object"},
            "duplicate_check": {"type": "object"},
            "submit_if_clear_handoff": {"type": ["object", "null"]},
            "job_id": {"type": ["string", "null"]},
            "submitted_job": {"type": ["object", "null"]},
            "status_endpoint": {"type": ["string", "null"]},
            "summary_endpoint": {"type": ["string", "null"]},
            "agent_summary": {"type": "object"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    job_summary_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "job_id": {"type": "string"},
            "kind": {"type": "string"},
            "summary_file": {"type": ["string", "null"]},
            "summary": {"type": ["object", "null"]},
            "agent_summary": {"type": ["object", "null"]},
            "agent_decision": {"type": ["object", "null"]},
            "candidate_digest": {"type": ["object", "null"]},
            "submit_if_clear_handoff": {"type": ["object", "null"]},
            "policy_coverage": {"type": ["object", "null"]},
            "policy_handoff": {"type": ["object", "null"]},
            "policy_qbit_defaults": {"type": ["object", "null"]},
            "qbit_plan": {"type": ["object", "null"]},
            "qbit_limit_audit": {"type": ["object", "null"]},
            "qbit_handoff": {"type": ["object", "null"]},
            "qbit_enforcement_summary": {"type": ["object", "null"]},
            "materials_handoff": {"type": ["object", "null"]},
            "target_upload_handoff": {"type": ["object", "null"]},
            "closure_handoff": {"type": ["object", "null"]},
            "closure_summary": {"type": "object"},
            "manual_retorrent_handoff": {"type": ["object", "null"]},
            "candidate_submission_handoff": {"type": ["object", "null"]},
            "candidate_submission_summary": {"type": ["object", "null"]},
            "runtime": {"type": "object"},
            "resume_plan": {"type": "object"},
            "resume_requirements": {"type": "object"},
            "resume_lineage": {"type": ["object", "null"]},
            "job_lineage": {"type": "object"},
            "resume_context": {"type": ["object", "null"]},
            "resume_audit": {"type": "object"},
            "resume_summary": {"type": "object"},
            "material_resolution": {"type": ["object", "null"]},
            "candidate_submission": {"type": ["object", "null"]},
            "check_submission": {"type": ["object", "null"]},
            "source_reference": {"type": ["object", "null"]},
            "workflow_context": {"type": ["object", "null"]},
            "result": {"type": ["object", "null"]},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
            "cancellation": {"type": ["object", "null"]},
        },
    }
    job_list_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "count": {"type": "integer"},
            "total": {"type": "integer"},
            "limit": {"type": "integer"},
            "filters": {"type": "object"},
            "status_counts": {"type": "object"},
            "queue": {"type": "object"},
            "jobs": {"type": "array", "items": {"type": "object"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    candidate_request_schema = {
        "type": "object",
        "required": ["source_tracker", "target"],
        "properties": {
            "source_tracker": {"type": "string", "description": "Source tracker code, e.g. U2 or CHD."},
            "target": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}], "description": "Target tracker code(s), currently MTEAM duplicate checks are supported."},
            "limit": {"type": "integer", "default": DEFAULT_CANDIDATE_LIMIT, "maximum": DEFAULT_CANDIDATE_LIMIT},
            "config": {"type": "string"},
            "base_dir": {"type": "string"},
            "accept_rules": {"type": "boolean", "description": "Whether rule obligations have been manually reviewed for candidate execution templates."},
            "check_dupes": {"type": "boolean", "default": True},
        },
    }
    candidate_schedule_request_schema = _daily_candidate_schedule_tool_request_schema()
    agent_run_preview_request_schema = _agent_run_preview_tool_request_schema()
    candidate_submit_request_schema = _candidate_submit_tool_request_schema()
    retorrent_check_submit_request_schema = _retorrent_check_submit_tool_request_schema()
    sites_request_schema = _sites_tool_request_schema()
    qbit_inspect_request_schema = _qbit_inspect_tool_request_schema()
    qbit_match_request_schema = _qbit_match_tool_request_schema()
    qbit_export_request_schema = _qbit_export_tool_request_schema()
    qbit_inject_request_schema = _qbit_inject_tool_request_schema()
    qbit_wait_request_schema = _qbit_wait_tool_request_schema()
    candidate_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "request": {"type": "object"},
            "count": {"type": "integer"},
            "ready_count": {"type": "integer"},
            "site_policy": {"type": "object"},
            "ranking": {"type": "object"},
            "digest": {"type": "object"},
            "candidates": {"type": "array", "items": {"type": "object"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    candidate_schedule_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "source": {"type": "string"},
            "env": {"type": "string"},
            "count": {"type": "integer"},
            "schedules": {"type": "array", "items": {"type": "object"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    candidate_schedule_jobs_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "plan": {"type": "object"},
            "job_count": {"type": "integer"},
            "jobs": {"type": "array", "items": {"type": "object"}},
            "skipped": {"type": "array", "items": {"type": "object"}},
            "schedule_digest": {"type": "object"},
            "notification_payload": {"type": "object"},
            "delivery_handoff": {"type": "object"},
            "agent_decision": {"type": "object"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    site_policy_request_schema = _site_policy_tool_request_schema()
    sites_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "ready": {"type": "boolean"},
            "sites": {"type": "array", "items": {"type": "string"}},
            "all_sites": {"type": "array", "items": {"type": "string"}},
            "capability_matrix": {"type": "array", "items": {"type": "object"}},
            "adapter_profiles": {"type": "object"},
            "policy_matrix": {"type": "array", "items": {"type": "object"}},
            "policy_gap_summary": {"type": "object"},
            "policy_execution_summary": {"type": "object"},
            "extension_plan": {"type": "object"},
            "extension_handoff": {"type": "object"},
            "flow_matrix": {"type": "array", "items": {"type": "object"}},
            "reference_flows": {"type": "array", "items": {"type": "object"}},
            "agent_summary": {"type": "object"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    qbit_inspect_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "read_only": {"type": "boolean"},
            "client": {"type": "string"},
            "request": {"type": "object"},
            "count": {"type": "integer"},
            "torrents": {"type": "array", "items": {"type": "object"}},
            "agent_summary": {"type": "object"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    qbit_match_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "read_only": {"type": "boolean"},
            "client": {"type": "string"},
            "request": {"type": "object"},
            "path": {"type": "string"},
            "count": {"type": "integer"},
            "matched": {"type": "boolean"},
            "matches": {"type": "array", "items": {"type": "object"}},
            "agent_summary": {"type": "object"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    qbit_export_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "read_only_client": {"type": "boolean"},
            "mutates_filesystem": {"type": "boolean"},
            "client": {"type": "string"},
            "request": {"type": "object"},
            "hash": {"type": "string"},
            "exported_path": {"type": "string"},
            "path": {"type": "string"},
            "target_torrent_file": {"type": "string"},
            "candidate": {"type": ["object", "null"]},
            "evidence": {"type": "object"},
            "target_upload_handoff": {"type": "object"},
            "agent_summary": {"type": "object"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    qbit_inject_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "mutates_qbittorrent": {"type": "boolean"},
            "live_upload": {"type": "boolean"},
            "client": {"type": "string"},
            "request": {"type": "object"},
            "torrent_path": {"type": "string"},
            "hash": {"type": "string"},
            "save_path": {"type": "string"},
            "category": {"type": ["string", "null"]},
            "tags": {"type": ["string", "null"]},
            "upload_limit": {"type": ["integer", "null"]},
            "download_limit": {"type": ["integer", "null"]},
            "rate_limits": {"type": "object"},
            "paused": {"type": "boolean"},
            "skip_checking": {"type": "boolean"},
            "visible_in_client": {"type": "boolean"},
            "verified_in_client": {"type": "boolean"},
            "client_verification": {"type": "object"},
            "client_matches": {"type": "array", "items": {"type": "object"}},
            "agent_summary": {"type": "object"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    qbit_wait_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "read_only": {"type": "boolean"},
            "client": {"type": "string"},
            "request": {"type": "object"},
            "complete": {"type": "boolean"},
            "query": {"type": "object"},
            "matched_count": {"type": "integer"},
            "completion_verification": {"type": "object"},
            "matches": {"type": "array", "items": {"type": "object"}},
            "agent_summary": {"type": "object"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    site_policy_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
            "ready": {"type": "boolean"},
            "request": {"type": "object"},
            "policy_matrix": {"type": "array", "items": {"type": "object"}},
            "config_templates": {"type": "object"},
            "site_policies": {"type": "array", "items": {"type": "object"}},
            "qbit_limits": {"type": "object"},
            "policy_gap_summary": {"type": "object"},
            "execution_readiness": {"type": "object"},
            "policy_execution_summary": {"type": "object"},
            "policy_setup_summary": {"type": "object"},
            "policy_execution_handoff": {"type": "object"},
            "policy_handoff": {"type": "object"},
            "next_step": {"type": "object"},
            "recommended_tool": {"type": ["string", "null"]},
            "recommended_endpoint": {"type": ["string", "null"]},
            "recommended_request": {"type": ["object", "null"]},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
            "agent_summary": {"type": "object"},
        },
    }
    manifest_response_schema = {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string"},
            "name": {"type": "string"},
            "base_url": {"type": "string"},
            "auth": {"type": "object"},
            "discovery": {"type": "object"},
            "safety": {"type": "object"},
            "closure_contract": {"type": "object"},
            "default_workflows": {"type": "array", "items": {"type": "object"}},
            "tools": {"type": "array", "items": {"type": "object"}},
        },
    }
    agent_run_preview_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok", "blocked"]},
            "ok": {"type": "boolean"},
            "kind": {"type": "string"},
            "dry_run": {"type": "boolean"},
            "mutates_state": {"type": "boolean"},
            "live_upload": {"type": "boolean"},
            "workflow": {"type": "string"},
            "tool": {"type": "string"},
            "request": {"type": "object"},
            "request_template": {"type": "object"},
            "closure_contract": {"type": "object"},
            "closure_handoff_examples": {"type": "object"},
            "one_call_handoff": {"type": "object"},
            "steps": {"type": "array", "items": {"type": "object"}},
            "next_step": {"type": ["object", "null"]},
            "recommended_tool": {"type": ["string", "null"]},
            "recommended_endpoint": {"type": ["string", "null"]},
            "recommended_request": {"type": ["object", "null"]},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    source_url_preflight_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok", "blocked"]},
            "ok": {"type": "boolean"},
            "ready": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
            "mutates_state": {"type": "boolean"},
            "live_upload": {"type": "boolean"},
            "request": {"type": "object"},
            "source_reference": {"type": ["object", "null"]},
            "target_trackers": {"type": ["string", "null"]},
            "ready_to_create_job": {"type": "boolean"},
            "ready_for_live_upload": {"type": "boolean"},
            "duplicate_check": {"type": "object"},
            "duplicate_check_handoff": {"type": "object"},
            "readiness_bundle": {"type": "object"},
            "policy_execution_summary": {"type": ["object", "null"]},
            "policy_execution_handoff": {"type": ["object", "null"]},
            "job_template": {"type": ["object", "null"]},
            "job_creation_handoff": {"type": "object"},
            "next_step": {"type": "object"},
            "recommended_tool": {"type": ["string", "null"]},
            "recommended_endpoint": {"type": ["string", "null"]},
            "recommended_request": {"type": ["object", "null"]},
            "agent_decision": {"type": "object"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
            "safety": {"type": "object"},
        },
    }
    deployment_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok", "blocked"]},
            "ok": {"type": "boolean"},
            "ready": {"type": "boolean"},
            "checks": {"type": "array", "items": {"type": "object"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
            "paths": {"type": "object"},
            "mounts": {"type": "object"},
            "queue": {"type": "object"},
            "qbit": {"type": "object"},
            "daily_candidates": {"type": "object"},
            "docker_compose": {"type": "object"},
            "deployment_handoff": {"type": "object"},
            "agent_summary": {"type": "object"},
            "agent_handoff": {"type": "object"},
        },
    }
    readiness_bundle_response_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok", "blocked"]},
            "ok": {"type": "boolean"},
            "ready": {"type": "boolean"},
            "request": {"type": "object"},
            "deployment": {"type": "object"},
            "site_policies": {"type": ["object", "null"]},
            "daily_schedule": {"type": "object"},
            "live_verification": {"type": "object"},
            "live_readiness": {"type": "object"},
            "live_test_handoff": {"type": "object"},
            "seedbox_live_validation_handoff": {"type": "object"},
            "next_step": {"type": "object"},
            "recommended_tool": {"type": ["string", "null"]},
            "recommended_endpoint": {"type": ["string", "null"]},
            "recommended_request": {"type": ["object", "null"]},
            "agent_decision": {"type": "object"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "ptcli Retorrent API", "version": "1.0.0"},
        "paths": {
            "/health": {
                "get": {
                    "operationId": "health",
                    "responses": {"200": {"description": "Service is alive."}},
                }
            },
            "/v1/tools": {
                "get": {
                    "operationId": "listPtcliTools",
                    "responses": {"200": {"description": "AI-readable tool list."}},
                }
            },
            "/.well-known/ptcli-agent.json": {
                "get": {
                    "operationId": "getPtcliAgentManifest",
                    "responses": {"200": {"description": "OpenClaw/Hermes-friendly agent manifest.", "content": {"application/json": {"schema": manifest_response_schema}}}},
                }
            },
            "/v1/agent-manifest": {
                "get": {
                    "operationId": "getPtcliAgentManifestV1",
                    "responses": {"200": {"description": "OpenClaw/Hermes-friendly agent manifest.", "content": {"application/json": {"schema": manifest_response_schema}}}},
                }
            },
            "/v1/openclaw/skill.json": {
                "get": {
                    "operationId": "getPtcliOpenClawSkill",
                    "responses": {"200": {"description": "OpenClaw-compatible ptcli skill manifest.", "content": {"application/json": {"schema": manifest_response_schema}}}},
                }
            },
            "/v1/hermes/skill.json": {
                "get": {
                    "operationId": "getPtcliHermesSkill",
                    "responses": {"200": {"description": "Hermes-compatible ptcli skill manifest.", "content": {"application/json": {"schema": manifest_response_schema}}}},
                }
            },
            "/v1/agent/run-preview": {
                "post": {
                    "operationId": "previewPtcliAgentRun",
                    "security": token_security,
                    "requestBody": {"required": False, "content": {"application/json": {"schema": agent_run_preview_request_schema}}},
                    "responses": {"200": {"description": "No-network AI workflow preview for source-url retorrent automation.", "content": {"application/json": {"schema": agent_run_preview_response_schema}}}},
                }
            },
            "/v1/retorrent/source-url/preflight": {
                "post": {
                    "operationId": "preflightSourceUrlRetorrent",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": source_url_request_schema}}},
                    "responses": {"200": {"description": "No-mutation source URL retorrent preflight for AI job creation decisions.", "content": {"application/json": {"schema": source_url_preflight_response_schema}}}},
                }
            },
            "/v1/deployment/check": {
                "get": {
                    "operationId": "checkPtcliDeployment",
                    "security": token_security,
                    "parameters": [
                        {"name": "config", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "base_dir", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "downloads_path", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "compose_file", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "client", "in": "query", "required": False, "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "Local deployment readiness report.", "content": {"application/json": {"schema": deployment_response_schema}}}},
                }
            },
            "/v1/readiness/bundle": {
                "get": {
                    "operationId": "getPtcliReadinessBundle",
                    "security": token_security,
                    "parameters": [
                        {"name": "source_url", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "source_tracker", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "source_id", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "target", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "accept_rules", "in": "query", "required": False, "schema": {"type": "boolean"}},
                        {"name": "confirm_upload", "in": "query", "required": False, "schema": {"type": "boolean"}},
                    ],
                    "responses": {"200": {"description": "AI readiness bundle.", "content": {"application/json": {"schema": readiness_bundle_response_schema}}}},
                },
                "post": {
                    "operationId": "createPtcliReadinessBundle",
                    "security": token_security,
                    "requestBody": {"required": False, "content": {"application/json": {"schema": _readiness_bundle_tool_request_schema()}}},
                    "responses": {"200": {"description": "AI readiness bundle.", "content": {"application/json": {"schema": readiness_bundle_response_schema}}}},
                },
            },
            "/v1/sites": {
                "get": {
                    "operationId": "listPtcliSiteProfiles",
                    "security": token_security,
                    "parameters": [
                        {"name": "trackers", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "source_tracker", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "target", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "accept_rules", "in": "query", "required": False, "schema": {"type": "boolean"}},
                    ],
                    "responses": {"200": {"description": "Chinese PT site adapter and policy profiles.", "content": {"application/json": {"schema": sites_response_schema}}}},
                },
                "post": {
                    "operationId": "createPtcliSiteProfiles",
                    "security": token_security,
                    "requestBody": {"required": False, "content": {"application/json": {"schema": sites_request_schema}}},
                    "responses": {"200": {"description": "Chinese PT site adapter and policy profiles.", "content": {"application/json": {"schema": sites_response_schema}}}},
                },
            },
            "/v1/qbit/inspect": {
                "get": {
                    "operationId": "inspectQbittorrent",
                    "security": token_security,
                    "parameters": [
                        {"name": "client", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "hash", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}},
                    ],
                    "responses": {"200": {"description": "Read-only qBittorrent torrent state.", "content": {"application/json": {"schema": qbit_inspect_response_schema}}}},
                },
                "post": {
                    "operationId": "inspectQbittorrentPost",
                    "security": token_security,
                    "requestBody": {"required": False, "content": {"application/json": {"schema": qbit_inspect_request_schema}}},
                    "responses": {"200": {"description": "Read-only qBittorrent torrent state.", "content": {"application/json": {"schema": qbit_inspect_response_schema}}}},
                },
            },
            "/v1/qbit/match": {
                "get": {
                    "operationId": "matchQbittorrentPath",
                    "security": token_security,
                    "parameters": [
                        {"name": "client", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "Read-only qBittorrent content path match.", "content": {"application/json": {"schema": qbit_match_response_schema}}}},
                },
                "post": {
                    "operationId": "matchQbittorrentPathPost",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": qbit_match_request_schema}}},
                    "responses": {"200": {"description": "Read-only qBittorrent content path match.", "content": {"application/json": {"schema": qbit_match_response_schema}}}},
                },
            },
            "/v1/qbit/export": {
                "get": {
                    "operationId": "exportQbittorrentTargetTorrent",
                    "security": token_security,
                    "parameters": [
                        {"name": "client", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "hash", "in": "query", "required": True, "schema": {"type": "string"}},
                        {"name": "output_dir", "in": "query", "required": False, "schema": {"type": "string", "default": "./tmp/exported"}},
                        {"name": "sanitize_for", "in": "query", "required": False, "schema": {"type": "string", "enum": ["MTEAM"], "default": "MTEAM"}},
                    ],
                    "responses": {"200": {"description": "Exported qBittorrent torrent and MTEAM-safe target candidate.", "content": {"application/json": {"schema": qbit_export_response_schema}}}},
                },
                "post": {
                    "operationId": "exportQbittorrentTargetTorrentPost",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": qbit_export_request_schema}}},
                    "responses": {"200": {"description": "Exported qBittorrent torrent and MTEAM-safe target candidate.", "content": {"application/json": {"schema": qbit_export_response_schema}}}},
                },
            },
            "/v1/qbit/inject": {
                "post": {
                    "operationId": "injectQbittorrentTorrent",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": qbit_inject_request_schema}}},
                    "responses": {"200": {"description": "Add a local .torrent to qBittorrent and verify hash/path/category/tag/rate-limit evidence.", "content": {"application/json": {"schema": qbit_inject_response_schema}}}},
                },
            },
            "/v1/qbit/wait": {
                "post": {
                    "operationId": "waitQbittorrentComplete",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": qbit_wait_request_schema}}},
                    "responses": {"200": {"description": "Wait for qBittorrent completion by hash or content path.", "content": {"application/json": {"schema": qbit_wait_response_schema}}}},
                },
            },
            "/v1/site-policies": {
                "get": {
                    "operationId": "getPtcliSitePolicies",
                    "security": token_security,
                    "parameters": [
                        {"name": "trackers", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "source_tracker", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "target", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "accept_rules", "in": "query", "required": False, "schema": {"type": "boolean"}},
                        {"name": "config", "in": "query", "required": False, "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "Chinese PT site policy matrix.", "content": {"application/json": {"schema": site_policy_response_schema}}}},
                },
                "post": {
                    "operationId": "postPtcliSitePolicies",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": site_policy_request_schema}}},
                    "responses": {"200": {"description": "Chinese PT site policy matrix.", "content": {"application/json": {"schema": site_policy_response_schema}}}},
                },
            },
            "/v1/retorrent/check": {
                "post": {
                    "operationId": "checkRetorrentDuplicate",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": request_schema}}},
                    "responses": {"200": {"description": "Duplicate-check result.", "content": {"application/json": {"schema": response_schema}}}},
                }
            },
            "/v1/retorrent": {
                "post": {
                    "operationId": "retorrentExecuteIfNoDuplicate",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": request_schema}}},
                    "responses": {"200": {"description": "Retorrent result or check result.", "content": {"application/json": {"schema": response_schema}}}},
                }
            },
            "/v1/jobs/retorrent/check": {
                "post": {
                    "operationId": "createRetorrentCheckJob",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": request_schema}}},
                    "responses": {"200": {"description": "Queued duplicate-check job.", "content": {"application/json": {"schema": job_response_schema}}}},
                }
            },
            "/v1/jobs/retorrent/check/{job_id}/submit": {
                "post": {
                    "operationId": "submitCheckedRetorrentJob",
                    "security": token_security,
                    "parameters": [{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "requestBody": {"required": False, "content": {"application/json": {"schema": retorrent_check_submit_request_schema}}},
                    "responses": {"200": {"description": "Queued live retorrent job from a completed clear duplicate-check job.", "content": {"application/json": {"schema": job_response_schema}}}},
                }
            },
            "/v1/jobs/retorrent": {
                "post": {
                    "operationId": "createRetorrentJob",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": request_schema}}},
                    "responses": {"200": {"description": "Queued retorrent job.", "content": {"application/json": {"schema": job_response_schema}}}},
                }
            },
            "/v1/jobs/retorrent/submit": {
                "post": {
                    "operationId": "submitManualRetorrentJob",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": manual_request_schema}}},
                    "responses": {
                        "200": {
                            "description": "Queued manual retorrent job that checks duplicates and executes only when gates allow.",
                            "content": {"application/json": {"schema": job_response_schema}},
                        }
                    },
                }
            },
            "/v1/jobs/retorrent/from-url": {
                "post": {
                    "operationId": "submitSourceUrlRetorrentJob",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": source_url_request_schema}}},
                    "responses": {
                        "200": {
                            "description": "Queued source-URL retorrent job that infers tracker/torrent id and executes only when gates allow.",
                            "content": {"application/json": {"schema": job_response_schema}},
                        }
                    },
                }
            },
            "/v1/jobs/retorrent/from-url/check-and-submit": {
                "post": {
                    "operationId": "checkAndSubmitSourceUrlRetorrentJob",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": source_url_check_submit_request_schema}}},
                    "responses": {
                        "200": {
                            "description": "Runs duplicate check first, then creates a source-URL retorrent job only when clear.",
                            "content": {"application/json": {"schema": source_url_check_submit_response_schema}},
                        }
                    },
                }
            },
            "/v1/candidates/daily": {
                "post": {
                    "operationId": "dailyRetorrentCandidates",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": candidate_request_schema}}},
                    "responses": {"200": {"description": "Daily retorrent candidates.", "content": {"application/json": {"schema": candidate_response_schema}}}},
                }
            },
            "/v1/candidates/daily/schedule": {
                "get": {
                    "operationId": "dailyRetorrentCandidateScheduleFromEnv",
                    "security": token_security,
                    "responses": {"200": {"description": "Daily candidate schedule plan from environment.", "content": {"application/json": {"schema": candidate_schedule_response_schema}}}},
                },
                "post": {
                    "operationId": "dailyRetorrentCandidateSchedule",
                    "security": token_security,
                    "requestBody": {"required": False, "content": {"application/json": {"schema": candidate_schedule_request_schema}}},
                    "responses": {"200": {"description": "Normalized daily candidate schedule plan.", "content": {"application/json": {"schema": candidate_schedule_response_schema}}}},
                },
            },
            "/v1/jobs/candidates/daily": {
                "post": {
                    "operationId": "createDailyRetorrentCandidateJob",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": candidate_request_schema}}},
                    "responses": {"200": {"description": "Queued daily-candidate discovery job.", "content": {"application/json": {"schema": job_response_schema}}}},
                }
            },
            "/v1/jobs/candidates/{job_id}/submit": {
                "post": {
                    "operationId": "submitDailyRetorrentCandidateJob",
                    "security": token_security,
                    "parameters": [{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "requestBody": {"required": True, "content": {"application/json": {"schema": candidate_submit_request_schema}}},
                    "responses": {"200": {"description": "Queued retorrent job from a selected daily candidate.", "content": {"application/json": {"schema": job_response_schema}}}},
                }
            },
            "/v1/jobs/candidates/daily/schedule": {
                "post": {
                    "operationId": "createDailyRetorrentCandidateScheduleJobs",
                    "security": token_security,
                    "requestBody": {"required": False, "content": {"application/json": {"schema": candidate_schedule_request_schema}}},
                    "responses": {"200": {"description": "Queued daily-candidate jobs for each enabled schedule.", "content": {"application/json": {"schema": candidate_schedule_jobs_response_schema}}}},
                }
            },
            "/v1/jobs": {
                "get": {
                    "operationId": "listPtcliJobs",
                    "security": token_security,
                    "parameters": [
                        {"name": "status", "in": "query", "required": False, "schema": {"type": "string", "enum": JOB_STATUS_VALUES}},
                        {"name": "kind", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}},
                    ],
                    "responses": {"200": {"description": "Recent ptcli jobs with AI-readable status and resume endpoints.", "content": {"application/json": {"schema": job_list_response_schema}}}},
                }
            },
            "/v1/jobs/{job_id}": {
                "get": {
                    "operationId": "getPtcliJob",
                    "security": token_security,
                    "parameters": [{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Job status.", "content": {"application/json": {"schema": job_response_schema}}}},
                }
            },
            "/v1/jobs/{job_id}/summary": {
                "get": {
                    "operationId": "getPtcliJobSummary",
                    "security": token_security,
                    "parameters": [{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Job summary and summary-file payload when available.", "content": {"application/json": {"schema": job_summary_response_schema}}}},
                }
            },
            "/v1/jobs/{job_id}/resume": {
                "post": {
                    "operationId": "resumePtcliJob",
                    "security": token_security,
                    "parameters": [{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "requestBody": {"required": False, "content": {"application/json": {"schema": resume_request_schema}}},
                    "responses": {"202": {"description": "Queued resume job.", "content": {"application/json": {"schema": job_response_schema}}}},
                }
            },
            "/v1/jobs/{job_id}/cancel": {
                "post": {
                    "operationId": "cancelPtcliJob",
                    "security": token_security,
                    "parameters": [{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "requestBody": {"required": False, "content": {"application/json": {"schema": {"type": "object", "properties": {"reason": {"type": "string"}}}}}},
                    "responses": {
                        "200": {"description": "Cancelled queued job.", "content": {"application/json": {"schema": job_response_schema}}},
                        "409": {"description": "Job is already running or terminal and cannot be cancelled safely."},
                    },
                }
            },
        },
        "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
    }
