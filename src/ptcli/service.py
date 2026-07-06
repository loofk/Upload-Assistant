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
from src.ptcli.cli import build_parser, pipeline_payload, retorrent_payload
from src.ptcli.config import load_config, resolve_client_config
from src.ptcli.credentials import build_flow_check
from src.ptcli.doctor import build_runtime_dependency_check
from src.ptcli.mainland import parse_tracker_list
from src.ptcli.policies import build_site_policy_coverage, build_site_policy_report, qbit_limits_for_tracker
from src.ptcli.source import resolve_source_reference

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
    "screenshot_count": "--screenshot-count",
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
        return _job_public_payload(self._read(job_id))

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
            recovered.append(_job_list_item(job))
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
                jobs.append(_job_list_item(job))
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
            "policy_coverage": _job_policy_coverage(job),
            "policy_qbit_defaults": _job_policy_qbit_defaults(job),
            "qbit_plan": _job_qbit_plan(job),
            "qbit_limit_audit": _job_qbit_limit_audit(job, summary_payload),
            "qbit_handoff": _job_qbit_handoff(job, summary_payload),
            "materials_handoff": _job_materials_handoff(job, summary_payload),
            "target_upload_handoff": _job_target_upload_handoff(job, summary_payload),
            "closure_handoff": _job_closure_handoff(job, summary_payload),
            "manual_retorrent_handoff": _job_manual_retorrent_handoff(job, summary_payload),
            "runtime": _job_runtime(job),
            "resume_plan": _job_resume_plan(job),
            "resume_requirements": _job_resume_requirements(job, summary_payload),
            "resume_lineage": _job_resume_lineage(job),
            "resume_context": _job_resume_context(job),
            "material_resolution": _job_material_resolution(job),
            "candidate_submission": _job_candidate_submission(job),
            "candidate_submission_handoff": _job_candidate_submission_handoff(job, summary_payload),
            "source_reference": _job_source_reference(job),
            "workflow_context": _job_workflow_context(job, summary_payload),
            "result": job.get("result"),
            "blockers": _string_list(job.get("blockers")),
            "next_actions": _string_list(job.get("next_actions")),
            "cancellation": job.get("cancellation") if isinstance(job.get("cancellation"), dict) else None,
        }

    def resume(self, job_id: str, request_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        parent = self._read(job_id)
        status = str(parent.get("status") or "")
        if status in {"queued", "running"}:
            raise ServiceError(f"Job {job_id} is still {status}; wait before resuming.", status=HTTPStatus.CONFLICT)
        original_argv = _resume_argv_from_job(parent)
        override_result = _apply_resume_overrides(original_argv, request_overrides or {})
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
                "/v1/site-policies": site_policies_payload,
                "/v1/readiness/bundle": readiness_bundle_payload,
                "/v1/candidates/daily": lambda payload: asyncio.run(daily_candidates(payload)),
                "/v1/candidates/daily/schedule": daily_candidate_schedule_payload,
                "/v1/jobs/retorrent/check": lambda payload: create_retorrent_check_job(job_store, payload),
                "/v1/jobs/retorrent": lambda payload: create_retorrent_job(job_store, payload),
                "/v1/jobs/retorrent/submit": lambda payload: create_manual_retorrent_job(job_store, payload),
                "/v1/jobs/retorrent/from-url": lambda payload: create_source_url_retorrent_job(job_store, payload),
                "/v1/jobs/candidates/daily": lambda payload: create_daily_candidates_job(job_store, payload),
                "/v1/jobs/candidates/daily/schedule": lambda payload: create_daily_candidate_schedule_jobs(job_store, payload),
            }
            if path.startswith("/v1/jobs/candidates/") and path.endswith("/submit"):
                try:
                    self._send_json(HTTPStatus.OK, self._candidate_submit(path, self._read_json()))
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
    effective_request = {**submit_request, **_candidate_submit_overrides(request)}
    effective_request["candidate_submission"] = {
        "candidate_job_id": candidate_job_id,
        "candidate_rank": candidate_item.get("rank"),
        "candidate_source_id": candidate_item.get("source_id"),
        "candidate_title": candidate_item.get("title"),
        "candidate_summary_text": candidate_item.get("summary_text"),
        "candidate_digest_kind": digest.get("kind"),
    }
    return _create_ai_retorrent_job(job_store, effective_request, kind="ptcli.candidate_retorrent", mode="candidate_retorrent")


def _create_ai_retorrent_job(job_store: JobStore, request: dict[str, Any], *, kind: str, mode: str) -> dict[str, Any]:
    effective_request = {**request, "execute": True, "execute_if_no_duplicate": True, "manual_retorrent": True}
    source = _resolve_request_source(effective_request)
    target_trackers = _target_trackers(effective_request)
    effective_request = _request_with_policy_qbit_defaults(effective_request, source, target_trackers)
    _, normalized_request, argv = _retorrent_execute_args(effective_request)
    normalized_request = {**normalized_request, "mode": mode, "execute_if_no_duplicate": True}
    if isinstance(effective_request.get("candidate_submission"), dict):
        normalized_request["candidate_submission"] = effective_request["candidate_submission"]
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
        "ready_count": result.get("ready_count", 0),
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


def site_policies_payload(request: dict[str, Any]) -> dict[str, Any]:
    """Return a tracker policy matrix for AI-safe automation decisions."""
    context = _site_policy_request_context(request)
    config = load_config(context.get("config"))
    report = build_site_policy_report(config, context["trackers"], accept_rules=bool(context.get("accept_rules")))
    roles = context.get("roles") if isinstance(context.get("roles"), dict) else {}
    matrix = [_site_policy_matrix_item(policy, roles=_string_list(roles.get(str(policy.get("tracker"))))) for policy in report.get("site_policies", []) if isinstance(policy, dict)]
    policy_gap_summary = _site_policy_gap_summary(matrix)
    execution_readiness = _site_policy_execution_readiness(matrix, report)
    policy_handoff = _site_policy_handoff(matrix, policy_gap_summary, execution_readiness, report, context)
    return {
        "kind": "ptcli.site_policies",
        "status": report.get("status", "ok"),
        "ok": bool(report.get("ready")),
        "ready": bool(report.get("ready")),
        "request": context,
        "policy_matrix": matrix,
        "config_templates": _site_policy_config_templates(matrix),
        "site_policies": report.get("site_policies", []),
        "qbit_limits": report.get("qbit_limits", {}),
        "policy_gap_summary": policy_gap_summary,
        "execution_readiness": execution_readiness,
        "policy_handoff": policy_handoff,
        "next_step": policy_handoff.get("next_step"),
        "recommended_tool": policy_handoff.get("recommended_tool"),
        "recommended_endpoint": policy_handoff.get("recommended_endpoint"),
        "recommended_request": policy_handoff.get("recommended_request"),
        "blockers": _string_list(report.get("blockers")),
        "next_actions": _string_list(report.get("next_actions")),
        "agent_summary": _site_policy_agent_summary(matrix, report, policy_gap_summary, execution_readiness),
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
    if source is None:
        blockers.append("source_url or source/source_id with source_tracker is required for manual live readiness.")
    elif source.get("error"):
        blockers.append(f"Source could not be resolved: {source.get('error')}")
    if not target_trackers:
        blockers.append("target is required for manual live readiness.")
    if site_policies and (site_policies.get("ready") is not True or (site_policies.get("execution_readiness") or {}).get("ready") is not True):
        blockers.extend(_string_list(site_policies.get("blockers")))
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
    return {
        "kind": "ptcli.live_test_handoff",
        "ready": bool(live_readiness.get("ready_for_manual_retorrent")),
        "doctor_ready": bool(doctor_template),
        "manual_job_ready": bool(manual_job_template) and bool(live_readiness.get("ready_for_manual_retorrent")),
        "connectivity_checked": bool(live_verification.get("connectivity_checked")),
        "doctor_template": doctor_template,
        "manual_job_template": manual_job_template,
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
        policy_handoff = site_policies.get("policy_handoff") if isinstance(site_policies.get("policy_handoff"), dict) else {}
        policy_step = policy_handoff.get("next_step") if isinstance(policy_handoff.get("next_step"), dict) else {}
        return {
            "tool": policy_step.get("tool") or "site_policies",
            "endpoint": policy_step.get("endpoint") or "/v1/site-policies",
            "method": policy_step.get("method") or "POST",
            "request": policy_step.get("request") or site_policies.get("request"),
            "reason": "site_policy_not_ready",
            "blockers": _string_list(site_policies.get("blockers")) or _string_list((site_policies.get("agent_summary") or {}).get("policy_recommendations")),
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
    for job in jobs:
        digest = job.get("candidate_digest") if isinstance(job.get("candidate_digest"), dict) else {}
        decision = job.get("agent_decision") if isinstance(job.get("agent_decision"), dict) else {}
        request = job.get("job_request") if isinstance(job.get("job_request"), dict) else {}
        top_candidate = digest.get("top_candidate") if isinstance(digest.get("top_candidate"), dict) else {}
        top_submit_request = digest.get("top_submit_request") if isinstance(digest.get("top_submit_request"), dict) else None
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
            "ready_count": int(digest.get("ready_count") or decision.get("ready_count") or 0),
            "top_candidate": top_candidate or None,
            "top_submit_request": top_submit_request,
            "top_submit_job_endpoint": digest.get("top_submit_job_endpoint"),
            "top_submit_tool": digest.get("top_submit_tool"),
            "agent_decision": decision.get("decision"),
            "can_submit_job": bool(decision.get("can_submit_job")),
            "missing_confirmations": _string_list(decision.get("missing_confirmations")),
            "push_count": len(digest.get("push_items")) if isinstance(digest.get("push_items"), list) else 0,
            "blockers": _string_list(decision.get("blockers") or digest.get("blockers")),
            "next_actions": _string_list(digest.get("next_actions")),
        }
        items.append(item)
        digest_push_items = digest.get("push_items") if isinstance(digest.get("push_items"), list) else []
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
            submission_source = {
                **(digest_push_items[0] if digest_push_items and isinstance(digest_push_items[0], dict) else {}),
                **top_candidate,
            }
            submission_items.append(_daily_candidate_schedule_submission_item(job, request, submission_source, item))
    submission_handoff = _daily_candidate_schedule_submission_handoff(submission_items, blockers)
    push_payload = _daily_candidate_schedule_push_payload(push_items, items, submission_handoff, blockers)
    return {
        "kind": "ptcli.daily_candidate_schedule_digest",
        "job_count": len(jobs),
        "ready_job_count": sum(1 for item in items if item.get("ready_count", 0) > 0),
        "pending_job_count": sum(1 for item in items if item.get("status") in {"queued", "running"}),
        "blocked_job_count": sum(1 for item in items if item.get("status") in {"blocked", "failed"} or item.get("blockers")),
        "skipped_count": len(skipped),
        "push_count": len(push_items),
        "push_payload": push_payload,
        "submit_request_count": len(top_submit_requests),
        "items": items,
        "push_items": push_items,
        "top_submit_requests": top_submit_requests,
        "submission_handoff": submission_handoff,
        "skipped": skipped,
        "blockers": blockers,
    }


def _daily_candidate_schedule_push_payload(push_items: list[dict[str, Any]], items: list[dict[str, Any]], submission_handoff: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    ready_items = [item for item in push_items if item.get("can_submit") is True]
    blocked_items = [item for item in push_items if item.get("can_submit") is not True]
    pending_count = sum(1 for item in items if item.get("status") in {"queued", "running"})
    title = "Daily PT candidate schedule"
    summary = f"{len(push_items)} candidate(s) across {len(items)} schedule job(s): {len(ready_items)} ready, {len(blocked_items)} blocked/review, {pending_count} pending."
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
        "item_count": len(push_items),
        "ready_count": len(ready_items),
        "blocked_count": len(blocked_items),
        "pending_job_count": pending_count,
        "submission_ready": bool(submission_handoff.get("ready")),
        "recommended_action": recommended_action,
        "top_item": ready_items[0] if ready_items else push_items[0] if push_items else None,
        "items": push_items,
        "blockers": blockers,
        "next_actions": [recommended_action],
    }


def _daily_candidate_schedule_notification_payload(schedule_digest: dict[str, Any], agent_decision: dict[str, Any]) -> dict[str, Any]:
    push_payload = schedule_digest.get("push_payload") if isinstance(schedule_digest.get("push_payload"), dict) else {}
    submission_handoff = schedule_digest.get("submission_handoff") if isinstance(schedule_digest.get("submission_handoff"), dict) else {}
    top_item = push_payload.get("top_item") if isinstance(push_payload.get("top_item"), dict) else None
    items = push_payload.get("items") if isinstance(push_payload.get("items"), list) else []
    ready_items = [item for item in items if isinstance(item, dict) and item.get("can_submit") is True]
    blocked_items = [item for item in items if isinstance(item, dict) and item.get("can_submit") is not True]
    pending_count = int(schedule_digest.get("pending_job_count") or 0)
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
            "candidates": schedule_digest.get("push_count", 0),
            "ready_candidates": ready_count,
            "blocked_candidates": len(blocked_items),
            "submit_requests": schedule_digest.get("submit_request_count", 0),
        },
        "top_item": top_item,
        "items": items,
        "submission_handoff": submission_handoff,
        "next_step": submission_handoff.get("next_step"),
        "recommended_tool": submission_handoff.get("recommended_tool"),
        "recommended_endpoint": submission_handoff.get("recommended_endpoint"),
        "recommended_request": submission_handoff.get("recommended_request"),
        "submit_items": submit_items,
        "top_submit_requests": schedule_digest.get("top_submit_requests", []),
        "blockers": _string_list(schedule_digest.get("blockers")),
        "next_actions": _daily_candidate_notification_next_actions(agent_decision, submission_handoff, pending_count),
    }


def _daily_candidate_notification_next_actions(agent_decision: dict[str, Any], submission_handoff: dict[str, Any], pending_count: int) -> list[str]:
    actions = _string_list(agent_decision.get("recommended_action"))
    if submission_handoff.get("ready"):
        actions.append("Review notification_payload.submit_items, then POST an approved item request_template to submit_endpoint with explicit user confirmation.")
    elif pending_count:
        actions.append("Poll notification_payload.items[].status_endpoint until pending jobs finish, then refresh the daily schedule summary.")
    elif not actions:
        actions.append("Inspect notification_payload.items and rerun the daily schedule later.")
    return list(dict.fromkeys(actions))


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
            "read_fields": ["candidate_submission_handoff", "manual_retorrent_handoff", "materials_handoff", "agent_decision", "status_endpoint", "summary_endpoint"],
            "poll_with": "get_job_status",
            "summary_with": "get_job_summary",
            "stop_when": ["manual_retorrent_handoff.action=stop_duplicate", "manual_retorrent_handoff.action=collect_confirmations", "manual_retorrent_handoff.action=configure_policy"],
            "resume_when": "manual_retorrent_handoff.action=resume and resume_plan.allowed=true",
        },
        "status_endpoint": job.get("status_endpoint"),
        "summary_endpoint": job.get("summary_endpoint"),
    }


def _daily_candidate_schedule_submission_handoff(submission_items: list[dict[str, Any]], blockers: list[str]) -> dict[str, Any]:
    next_step = _daily_candidate_submission_next_step(submission_items, blockers)
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
        "items": submission_items,
        "blockers": blockers,
    }


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


def _site_policy_matrix_item(policy: dict[str, Any], *, roles: list[str] | None = None) -> dict[str, Any]:
    policy_roles = roles or ["unknown"]
    item = {
        "tracker": policy.get("tracker"),
        "roles": policy_roles,
        "rules_url": policy.get("rules_url"),
        "manual_review_required": policy.get("manual_review_required"),
        "rule_review_fingerprint": policy.get("rule_review_fingerprint"),
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
    item["policy_coverage"] = build_site_policy_coverage(policy, roles=policy_roles)
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
        "missing_fields": _string_list(coverage.get("missing_fields")),
        "disabled_automation": _string_list(coverage.get("disabled_automation")),
        "template": _site_policy_config_template(item),
        "current_values": {
            "rules_url": item.get("rules_url"),
            "automation": item.get("automation"),
            "qbit_limits": item.get("qbit_limits"),
            "seeding_requirements": item.get("seeding_requirements"),
            "transfer_rules": item.get("transfer_rules"),
            "rule_review_fingerprint": item.get("rule_review_fingerprint"),
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


def _site_policy_config_templates(matrix: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "ptcli.site_policy_config_templates",
        "config_path": 'config["PTCLI"]["SITE_POLICIES"]',
        "trackers": {str(item.get("tracker")): (item.get("policy_profile") or {}).get("template") for item in matrix if item.get("tracker")},
    }


def _site_policy_handoff(matrix: list[dict[str, Any]], policy_gap_summary: dict[str, Any], execution_readiness: dict[str, Any], report: dict[str, Any], request_context: dict[str, Any]) -> dict[str, Any]:
    templates = _site_policy_config_templates(matrix)
    blocked_trackers = _string_list(execution_readiness.get("blocked_trackers"))
    ready = bool(execution_readiness.get("ready")) and bool(report.get("ready"))
    missing_by_category = policy_gap_summary.get("missing_by_category") if isinstance(policy_gap_summary.get("missing_by_category"), dict) else {}
    tracker_items = []
    for item in matrix:
        tracker = str(item.get("tracker") or "")
        if not tracker:
            continue
        profile = item.get("policy_profile") if isinstance(item.get("policy_profile"), dict) else {}
        coverage = item.get("policy_coverage") if isinstance(item.get("policy_coverage"), dict) else {}
        readiness = item.get("execution_readiness") if isinstance(item.get("execution_readiness"), dict) else {}
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


def _site_policy_agent_summary(matrix: list[dict[str, Any]], report: dict[str, Any], policy_gap_summary: dict[str, Any], execution_readiness: dict[str, Any]) -> dict[str, Any]:
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
        "policy_qbit_defaults": request.get("policy_qbit_defaults"),
        "policy_coverage": _request_policy_coverage(request, source, target_trackers),
    }


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
        source_coverage = build_site_policy_coverage(source_policy, roles=["source"]) if isinstance(source_policy, dict) else None
        target_coverages = [build_site_policy_coverage(policy, roles=["target"]) for policy in target_policies if isinstance(policy, dict)]
        coverages = [coverage for coverage in [source_coverage, *target_coverages] if isinstance(coverage, dict)]
        return {
            "ready": bool(report.get("ready")) and bool(coverages) and all(bool(coverage.get("complete")) for coverage in coverages),
            "site_policy_ready": bool(report.get("ready")),
            "accept_rules": bool(request.get("accept_rules")),
            "source": source_coverage,
            "targets": target_coverages,
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


def _job_list_item(job: dict[str, Any]) -> dict[str, Any]:
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
        "qbit_plan": _job_qbit_plan(job),
        "qbit_limit_audit": _job_qbit_limit_audit(job),
        "qbit_handoff": _job_qbit_handoff(job),
        "materials_handoff": _job_materials_handoff(job),
        "target_upload_handoff": _job_target_upload_handoff(job),
        "closure_handoff": _job_closure_handoff(job),
        "manual_retorrent_handoff": _job_manual_retorrent_handoff(job),
        "agent_decision": job.get("agent_decision") if isinstance(job.get("agent_decision"), dict) else _agent_decision(job),
        "resume_plan": _job_resume_plan(job),
        "resume_requirements": _job_resume_requirements(job),
        "resume_lineage": _job_resume_lineage(job),
        "material_resolution": _job_material_resolution(job),
        "candidate_submission": _job_candidate_submission(job),
        "candidate_submission_handoff": _job_candidate_submission_handoff(job),
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


def _job_public_payload(job: dict[str, Any]) -> dict[str, Any]:
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
        "summary_file": job.get("summary_file"),
        "resume_state": job.get("resume_state"),
        "agent_summary": job.get("agent_summary") if isinstance(job.get("agent_summary"), dict) else _agent_summary(job.get("result")),
        "agent_decision": job.get("agent_decision") if isinstance(job.get("agent_decision"), dict) else _agent_decision(job),
        "candidate_digest": _candidate_digest_from_payload(job.get("result")),
        "policy_coverage": _job_policy_coverage(job),
        "policy_qbit_defaults": _job_policy_qbit_defaults(job),
        "qbit_plan": _job_qbit_plan(job),
        "qbit_limit_audit": _job_qbit_limit_audit(job),
        "qbit_handoff": _job_qbit_handoff(job),
        "materials_handoff": _job_materials_handoff(job),
        "target_upload_handoff": _job_target_upload_handoff(job),
        "closure_handoff": _job_closure_handoff(job),
        "manual_retorrent_handoff": _job_manual_retorrent_handoff(job),
        "resume_plan": _job_resume_plan(job),
        "resume_requirements": _job_resume_requirements(job),
        "resume_lineage": _job_resume_lineage(job),
        "resume_context": _job_resume_context(job),
        "material_resolution": _job_material_resolution(job),
        "candidate_submission": _job_candidate_submission(job),
        "candidate_submission_handoff": _job_candidate_submission_handoff(job),
        "source_reference": _job_source_reference(job),
        "workflow_context": _job_workflow_context(job),
        "result_status": _nested_value(job.get("result"), "status"),
        "next_stage": _nested_value(job.get("result"), "next_stage"),
        "next_command": _nested_value(job.get("result"), "next_command"),
        "next_command_argv": _result_next_command_argv(job.get("result")),
        "should_execute_next_command": _nested_value(job.get("result"), "should_execute_next_command"),
        "automation_action": _nested_value(job.get("result"), "automation_action"),
    }


def _job_policy_coverage(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request")
    if isinstance(request, dict) and isinstance(request.get("policy_coverage"), dict):
        return request["policy_coverage"]
    return None


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
    return {
        "kind": "ptcli.qbit_handoff",
        "client": plan.get("client"),
        "ready": bool(audit.get("ready")) if isinstance(audit, dict) else False,
        "source": _qbit_handoff_role("source", source_plan, source_audit),
        "uploaded": _qbit_handoff_role("uploaded", uploaded_plan, uploaded_audit),
        "policy_defaults": plan.get("policy_defaults"),
        "blockers": blockers,
        "next_actions": _qbit_handoff_next_actions(blockers),
    }


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
        inputs.append({"key": "image_host_file", "accepted_keys": ["image_host_file"], "required": False, "reason": "hosted screenshot URLs are missing or stale"})
    return _dedupe_recommended_inputs(inputs)


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
    return {
        "kind": "ptcli.closure_handoff",
        "ready": complete,
        "complete": complete,
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


def _job_candidate_submission(job: dict[str, Any]) -> dict[str, Any] | None:
    request = job.get("request")
    if isinstance(request, dict) and isinstance(request.get("candidate_submission"), dict):
        return request["candidate_submission"]
    return None


def _job_candidate_submission_handoff(job: dict[str, Any], summary_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    submission = _job_candidate_submission(job)
    if not submission:
        return None
    job_id = job.get("job_id")
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    manual_handoff = _job_manual_retorrent_handoff(job, summary_payload)
    return {
        "kind": "ptcli.candidate_submission_handoff",
        "candidate_job_id": submission.get("candidate_job_id"),
        "candidate_rank": submission.get("candidate_rank"),
        "candidate_source_id": submission.get("candidate_source_id"),
        "candidate_title": submission.get("candidate_title"),
        "candidate_summary_text": submission.get("candidate_summary_text"),
        "candidate_digest_kind": submission.get("candidate_digest_kind"),
        "retorrent_job_id": job_id,
        "retorrent_status": job.get("status"),
        "source_reference": _job_source_reference(job),
        "target_trackers": request.get("target_trackers"),
        "manual_retorrent_handoff": manual_handoff,
        "action": manual_handoff.get("action") if isinstance(manual_handoff, dict) else None,
        "status_endpoint": f"/v1/jobs/{job_id}" if job_id else None,
        "summary_endpoint": f"/v1/jobs/{job_id}/summary" if job_id else None,
        "parent_status_endpoint": f"/v1/jobs/{submission.get('candidate_job_id')}" if submission.get("candidate_job_id") else None,
        "parent_summary_endpoint": f"/v1/jobs/{submission.get('candidate_job_id')}/summary" if submission.get("candidate_job_id") else None,
        "next_actions": _candidate_submission_handoff_next_actions(manual_handoff),
    }


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
    return {
        "kind": "ptcli.manual_retorrent_handoff",
        "ready": status == "complete",
        "action": action,
        "reason": reason,
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
        "policy_qbit_defaults": _job_policy_qbit_defaults(job),
        "qbit_plan": _job_qbit_plan(job),
        "qbit_limit_audit": _job_qbit_limit_audit(job, payload if isinstance(payload, dict) else None),
        "qbit_handoff": _job_qbit_handoff(job, payload if isinstance(payload, dict) else None),
        "materials_handoff": _job_materials_handoff(job, payload if isinstance(payload, dict) else None),
        "target_upload_handoff": _job_target_upload_handoff(job, payload if isinstance(payload, dict) else None),
        "manual_retorrent_handoff": _job_manual_retorrent_handoff(job, payload if isinstance(payload, dict) else None),
        "candidate_submission_handoff": _job_candidate_submission_handoff(job, payload if isinstance(payload, dict) else None),
        "resume_plan": resume_plan,
        "resume_requirements": _job_resume_requirements(job, payload if isinstance(payload, dict) else None),
        "resume_state": resume_state,
        "resume_context": _job_resume_context(job),
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


def _resume_recommended_inputs(
    request: dict[str, Any],
    metadata: dict[str, Any],
    materials: dict[str, Any],
    target_preflight: dict[str, Any],
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    if not request.get("path") and not request.get("save_path"):
        inputs.append({"key": "path_or_save_path", "accepted_keys": ["path", "save_path"], "required": False, "reason": "content path helps resume without rediscovering qBittorrent state"})
    if metadata.get("tmdb_ready") is False or "metadata.tmdb" in _string_list(materials.get("critical_missing")):
        inputs.append({"key": "metadata_file", "accepted_keys": ["metadata_file"], "required": False, "reason": "TMDb/IMDb/豆瓣 metadata is incomplete"})
    if metadata.get("ptgen_description_ready") is False or "description.content" in _string_list(materials.get("critical_missing")) or target_preflight.get("description_ready") is False:
        inputs.append({"key": "ptgen_description_file", "accepted_keys": ["ptgen_description_file"], "required": False, "reason": "target description is incomplete"})
    if materials.get("mediainfo_or_bdinfo_ready") is False:
        inputs.append({"key": "mediainfo_or_bdinfo", "accepted_keys": ["mediainfo_file", "bdinfo_file"], "required": False, "reason": "MediaInfo or BDInfo evidence is missing"})
    if materials.get("screenshots_ready") is False:
        inputs.append({"key": "screenshot_files", "accepted_keys": ["screenshot_files", "screenshot_file"], "required": False, "reason": "screenshots are missing or insufficient"})
    if target_preflight.get("payload_ready") is False and request.get("package_dir"):
        inputs.append({"key": "package_dir", "accepted_keys": ["package_dir"], "required": False, "reason": "reuse or replace the target upload package directory"})
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
    resume_plan = _job_resume_plan(job)
    missing_confirmations = _missing_live_confirmations(request)
    policy_coverage = _job_policy_coverage(job) if request.get("execute") is True or request.get("execute_if_no_duplicate") is True or request.get("mode") == "manual_retorrent" else None
    policy_qbit_defaults = _job_policy_qbit_defaults(job) if request.get("execute") is True or request.get("execute_if_no_duplicate") is True or request.get("mode") == "manual_retorrent" else None
    qbit_plan = _job_qbit_plan(job) if policy_qbit_defaults is not None else None
    qbit_limit_audit = _job_qbit_limit_audit(job) if qbit_plan is not None else None
    qbit_handoff = _job_qbit_handoff(job) if qbit_plan is not None else None
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
        "missing_confirmations": missing_confirmations,
        "policy_coverage": policy_coverage,
        "policy_qbit_defaults": policy_qbit_defaults,
        "qbit_plan": qbit_plan,
        "qbit_limit_audit": qbit_limit_audit,
        "qbit_handoff": qbit_handoff,
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
        "resume_lineage": resume_lineage,
        "resume_context": resume_context,
        "material_resolution": material_resolution,
        "candidate_submission": _job_candidate_submission(job),
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
    provided = {key: value for key, value in overrides.items() if key != "job_id"} if isinstance(overrides, dict) else {}
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
            "ok": bool(docker_compose.get("daily_schedule_service_ready")),
            "blocking": False,
            "message": _deployment_docker_compose_message(docker_compose),
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
                "daily_profile": False,
                "daily_schedule_command": False,
                "daily_schedule_service_ready": False,
            }
    return {
        "present": exists,
        "readable": exists,
        "path": str(compose_path),
        "ptcli_api_service": "ptcli-api:" in text,
        "daily_schedule_service": "ptcli-daily-schedule:" in text,
        "daily_profile": "- daily" in text,
        "daily_schedule_command": 'command: ["daily-schedule", "--write-summary", "--summary-output-dir", "/Upload-Assistant/tmp/daily-candidates", "--json"]' in text,
        "daily_schedule_service_ready": all(
            (
                exists,
                "ptcli-api:" in text,
                "ptcli-daily-schedule:" in text,
                "- daily" in text,
                'command: ["daily-schedule", "--write-summary", "--summary-output-dir", "/Upload-Assistant/tmp/daily-candidates", "--json"]' in text,
            )
        ),
    }


def _deployment_docker_compose_message(summary: dict[str, Any]) -> str:
    path = summary.get("path")
    if summary.get("daily_schedule_service_ready"):
        return f"Docker Compose daily schedule service is configured: {path}"
    if not summary.get("present"):
        return f"docker-compose.yml is not present at {path}; skip this warning if not using Docker Compose."
    if not summary.get("readable"):
        return f"docker-compose.yml could not be read at {path}: {summary.get('error')}"
    missing = [
        name
        for name, ready in (
            ("ptcli-api service", summary.get("ptcli_api_service")),
            ("ptcli-daily-schedule service", summary.get("daily_schedule_service")),
            ("daily profile", summary.get("daily_profile")),
            ("daily-schedule summary command", summary.get("daily_schedule_command")),
        )
        if not ready
    ]
    return f"Docker Compose daily schedule service is incomplete at {path}: {', '.join(missing)}."


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
        "api_token_configured": bool(api_token_check.get("configured")),
        "qbit_configured": bool(qbit.get("configured")),
        "daily_candidates_configured": bool(daily_candidate_plan.get("configured")),
        "docker_compose_daily_ready": bool(docker_compose.get("daily_schedule_service_ready")),
        "missing_mounts": mounts.get("missing", []),
        "blocking_checks": blocking_failures,
        "warning_checks": warning_failures,
        "configured_paths": paths,
        "qbit_client": qbit.get("client"),
        "daily_candidate_schedule_count": daily_candidate_plan.get("count", 0),
    }


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
            "daily_schedule_ready": bool(docker_compose.get("daily_schedule_service_ready")),
            "compose_file": docker_compose.get("path"),
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
    target = request.get("target") or request.get("target_trackers") or "MTEAM"
    target_trackers = _preview_target_trackers(target)
    workflow_name = str(request.get("workflow") or "source_url_retorrent")
    workflows = {workflow["name"]: workflow for workflow in _agent_default_workflows()}
    workflow = workflows.get(workflow_name) or workflows["source_url_retorrent"]
    accept_rules = _truthy(request.get("accept_rules"))
    confirm_upload = _truthy(request.get("confirm_upload"))
    request_template = _agent_preview_request_template(source_url, target_trackers, accept_rules, confirm_upload, request)
    blockers = _agent_preview_blockers(source_url, target_trackers, accept_rules, confirm_upload)
    steps = _agent_preview_steps(workflow, request_template)
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
            "target": target_trackers,
            "accept_rules": accept_rules,
            "confirm_upload": confirm_upload,
            "save_path": request.get("save_path"),
            "uploaded_qbit_category": request.get("uploaded_qbit_category"),
            "uploaded_qbit_tags": request.get("uploaded_qbit_tags"),
        },
        "request_template": request_template,
        "closure_contract": closure_contract,
        "closure_handoff_examples": _agent_preview_closure_examples(),
        "steps": steps,
        "next_step": steps[0] if steps else None,
        "recommended_tool": steps[0].get("tool") if steps else None,
        "recommended_endpoint": steps[0].get("endpoint") if steps else None,
        "recommended_request": steps[0].get("request") if steps else None,
        "blockers": blockers,
        "next_actions": _agent_preview_next_actions(blockers, closure_contract),
    }


def _preview_target_trackers(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    return [item.strip().upper() for item in str(value or "MTEAM").split(",") if item.strip()]


def _agent_preview_request_template(source_url: str, target_trackers: list[str], accept_rules: bool, confirm_upload: bool, request: dict[str, Any]) -> dict[str, Any]:
    template: dict[str, Any] = {
        "source_url": source_url or "<source tracker details URL>",
        "target": target_trackers[0] if len(target_trackers) == 1 else target_trackers,
        "accept_rules": accept_rules,
        "confirm_upload": confirm_upload,
    }
    for key in ("save_path", "path", "uploaded_qbit_category", "uploaded_qbit_tags", "uploaded_qbit_upload_limit", "uploaded_qbit_download_limit"):
        if request.get(key) is not None:
            template[key] = request[key]
    return template


def _agent_preview_blockers(source_url: str, target_trackers: list[str], accept_rules: bool, confirm_upload: bool) -> list[str]:
    blockers: list[str] = []
    if not source_url:
        blockers.append("source_url is required before submitting source_url_retorrent_job.")
    if not target_trackers:
        blockers.append("target is required before submitting source_url_retorrent_job.")
    if not accept_rules:
        blockers.append("accept_rules=true is required before live retorrent automation.")
    if not confirm_upload:
        blockers.append("confirm_upload=true is required before live target upload.")
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


def _agent_preview_step_request(tool: str, request_template: dict[str, Any]) -> dict[str, Any] | None:
    if tool == "readiness_bundle":
        return request_template
    if tool == "site_policies":
        return {"source_url": request_template.get("source_url"), "target": request_template.get("target"), "accept_rules": request_template.get("accept_rules")}
    if tool == "source_url_retorrent_job":
        return request_template
    if tool == "get_job_status":
        return {"job_id": "<job_id from source_url_retorrent_job>"}
    if tool == "get_job_summary":
        return {"job_id": "<job_id from source_url_retorrent_job>"}
    if tool == "resume_job":
        return {"job_id": "<job_id from closure_handoff.next_step>", "overrides": "<allowlisted overrides only>"}
    return None


def _agent_preview_closure_examples() -> dict[str, Any]:
    return {
        "complete": {"closure_handoff": {"complete": True, "action": "done", "recommended_tool": "get_job_summary", "next_step": {"tool": "get_job_summary", "method": "GET"}}},
        "resume": {"closure_handoff": {"complete": False, "action": "prepare_materials", "recommended_tool": "resume_job", "next_step": {"tool": "resume_job", "method": "POST", "request": {}}}},
        "stop": {"closure_handoff": {"complete": False, "action": "stop_duplicate", "recommended_tool": None, "next_step": {"tool": None, "method": None}}},
    }


def _agent_preview_next_actions(blockers: list[str], closure_contract: dict[str, Any]) -> list[str]:
    if blockers:
        return ["Resolve preview blockers before submitting source_url_retorrent_job. This preview does not contact trackers or qBittorrent."]
    return [f"Submit request_template to source_url_retorrent_job, poll get_job_status, then follow {closure_contract['next_step_source']} until {closure_contract['complete_when']}."]


def _agent_tool_schemas() -> list[dict[str, Any]]:
    retorrent_request_schema = _retorrent_tool_request_schema()
    manual_retorrent_request_schema = _manual_retorrent_tool_request_schema()
    source_url_retorrent_request_schema = _source_url_retorrent_tool_request_schema()
    candidate_request_schema = _daily_candidate_tool_request_schema()
    candidate_schedule_request_schema = _daily_candidate_schedule_tool_request_schema()
    candidate_submit_request_schema = _candidate_submit_tool_request_schema()
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
                "required_fields": ["status", "ok", "job_count", "jobs", "skipped", "schedule_digest", "notification_payload", "agent_decision", "blockers", "next_actions"],
                "job_fields": ["schedule_name", "job_id", "status_endpoint", "summary_endpoint", "job_request", "candidate_digest", "agent_decision"],
                "digest_fields": ["items", "push_items", "push_payload", "top_submit_requests", "submission_handoff", "ready_job_count", "submit_request_count", "pending_job_count", "blocked_job_count"],
                "push_payload_fields": ["title", "summary", "message", "format", "items", "top_item", "decision_summary", "submission_ready", "recommended_action"],
                "notification_fields": ["title", "summary", "message", "status", "ready", "submission_ready", "counts", "top_item", "items", "submit_items", "submission_handoff", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "next_actions"],
                "submission_handoff_fields": ["ready", "submit_tool", "submit_endpoint_template", "required_overrides", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "items"],
            },
            "workflow_hints": {"poll_with": "get_job_status", "summary_with": "get_job_summary"},
            "safety": {"mutates_state": True, "live_upload": False, "requires_confirmation": []},
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
            "response_contract": {"required_fields": ["status", "ok", "job_id", "summary_file", "summary", "agent_summary", "agent_decision", "candidate_digest", "policy_coverage", "policy_qbit_defaults", "qbit_plan", "qbit_limit_audit", "qbit_handoff", "materials_handoff", "target_upload_handoff", "closure_handoff", "manual_retorrent_handoff", "candidate_submission_handoff", "runtime", "resume_plan", "resume_requirements", "resume_lineage", "resume_context", "material_resolution", "candidate_submission", "source_reference", "workflow_context", "result", "blockers", "next_actions"]},
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
                "required_fields": ["status", "ok", "ready", "checks", "blockers", "warnings", "next_actions", "paths", "mounts", "queue", "qbit", "daily_candidates", "docker_compose", "agent_summary", "agent_handoff"],
                "status_values": ["ok", "blocked"],
                "agent_summary_fields": ["ready_for_ai", "ready_for_manual_retorrent", "ready_for_daily_candidates", "missing_mounts", "qbit_configured", "daily_candidates_configured", "docker_compose_daily_ready"],
                "agent_handoff_fields": ["ready", "recommended_first_step", "manual_retorrent", "daily_candidates", "qbit", "docker_compose", "safety", "next_tools"],
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
                "required_fields": ["status", "ok", "ready", "policy_matrix", "config_templates", "qbit_limits", "policy_gap_summary", "execution_readiness", "policy_handoff", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers", "next_actions", "agent_summary"],
                "policy_fields": [
                    "tracker",
                    "roles",
                    "rules_url",
                    "automation",
                    "qbit_limits",
                    "seeding_requirements",
                    "transfer_rules",
                    "policy_profile",
                    "manual_review_required",
                    "rule_review_fingerprint",
                    "policy_coverage",
                    "execution_readiness",
                ],
                "policy_profile_fields": ["config_path", "required_fields", "optional_fields", "missing_fields", "disabled_automation", "template", "current_values", "next_actions"],
                "gap_summary_fields": ["ready", "missing_total", "disabled_total", "by_role", "missing_by_category", "recommendations"],
                "execution_readiness_fields": ["ready", "accepted_rules", "ready_trackers", "blocked_trackers", "by_tracker", "blockers"],
                "policy_handoff_fields": ["ready", "config_path", "blocked_trackers", "items", "config_templates", "missing_by_category", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers"],
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
        properties["workflow"] = {"type": "string", "default": "source_url_retorrent", "enum": ["source_url_retorrent"], "description": "Agent workflow to preview without running live work."}
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
        "required_fields": ["status", "ok", "request", "duplicate_check", "blockers", "next_actions", "command_argv", "result"],
        "status_values": ["ok", "blocked", "error", "complete"],
        "blocked_fields": ["blockers", "next_actions"],
    }


def _agent_run_preview_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "kind", "dry_run", "mutates_state", "live_upload", "workflow", "tool", "request", "request_template", "closure_contract", "closure_handoff_examples", "steps", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers", "next_actions"],
        "step_fields": ["index", "step", "tool", "endpoint", "method", "request", "read", "continue_when", "repeat_when", "stop_when", "complete_when", "resume_with"],
        "closure_examples": ["complete", "resume", "stop"],
        "safety": ["does_not_contact_trackers", "does_not_contact_qbittorrent", "does_not_create_jobs", "does_not_upload"],
    }


def _readiness_bundle_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "ready", "request", "deployment", "site_policies", "daily_schedule", "live_verification", "live_readiness", "live_test_handoff", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "agent_decision", "blockers", "warnings", "next_actions"],
        "live_verification_fields": ["ready", "connectivity_checked", "checks", "credential_requirements", "flow_check", "materials", "blockers", "warnings", "next_actions"],
        "live_readiness_fields": ["ready_for_ai", "ready_for_manual_retorrent", "ready_for_daily_candidates", "source", "target_trackers", "site_policy_ready", "live_verification_ready", "credential_requirements", "doctor_template", "manual_job_template", "blockers", "warnings", "next_actions"],
        "live_test_handoff_fields": ["ready", "doctor_ready", "manual_job_ready", "doctor_template", "manual_job_template", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "after_doctor", "blockers", "warnings"],
        "agent_decision_fields": ["decision", "recommended_action", "runbook_ref", "next_tool", "can_create_manual_job", "can_run_daily_candidates", "should_fix_deployment", "next_actions"],
        "safety": ["non_live", "does_not_contact_trackers", "does_not_contact_qbittorrent", "live_upload_still_requires_accept_rules_and_confirm_upload"],
    }


def _job_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "job_id", "kind", "request", "command_argv", "blockers", "next_actions", "interruption", "cancellation", "runtime", "summary_file", "resume_state", "agent_summary", "agent_decision", "candidate_digest", "policy_coverage", "policy_qbit_defaults", "qbit_plan", "qbit_limit_audit", "qbit_handoff", "materials_handoff", "target_upload_handoff", "closure_handoff", "manual_retorrent_handoff", "candidate_submission_handoff", "resume_plan", "resume_requirements", "resume_lineage", "resume_context", "material_resolution", "candidate_submission", "source_reference", "workflow_context"],
        "status_values": JOB_STATUS_VALUES,
        "blocked_fields": ["blockers", "next_actions", "interruption", "cancellation", "runtime", "resume_state", "resume_plan", "resume_requirements", "next_command_argv", "agent_decision"],
        "running_fields": ["runtime.should_poll", "runtime.poll_after_seconds", "runtime.status_endpoint", "agent_decision.should_poll"],
        "cancel_fields": ["cancellation", "agent_decision.stop_reason", "runtime.terminal"],
        "request_fields": ["policy_coverage", "policy_qbit_defaults", "qbit_plan", "qbit_upload_limit", "qbit_download_limit", "uploaded_qbit_upload_limit", "uploaded_qbit_download_limit", "qbit_category", "qbit_tags", "uploaded_qbit_category", "uploaded_qbit_tags"],
        "resume_requirement_fields": ["can_call_resume", "resume_recommended", "subcommand", "missing_confirmations", "required_overrides", "suggested_overrides", "recommended_inputs", "allowed_overrides", "current_flags"],
        "material_resolution_fields": ["ready_before_resume", "recommended_inputs", "applied_override_keys", "covered_recommended_inputs", "unresolved_recommended_inputs", "blockers_before_resume"],
        "target_upload_handoff_fields": ["action", "ready_for_live_upload", "uploaded_seeding_ready", "preflight", "duplicate_clear", "missing_confirmations", "policy_coverage_ready", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers", "next_actions"],
        "closure_handoff_fields": ["action", "complete", "source", "target", "evidence", "duplicate_check", "target_upload_handoff", "qbit_handoff", "next_step", "recommended_tool", "recommended_endpoint", "recommended_request", "blockers", "next_actions"],
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
        }
    )
    return contract


def _job_list_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "count", "total", "limit", "filters", "status_counts", "queue", "jobs", "next_actions"],
        "job_fields": ["job_id", "kind", "status", "blockers", "next_actions", "interruption", "cancellation", "runtime", "summary_file", "candidate_submission", "candidate_submission_handoff", "source_reference", "duplicate_check", "qbit_plan", "qbit_limit_audit", "qbit_handoff", "materials_handoff", "target_upload_handoff", "closure_handoff", "manual_retorrent_handoff", "agent_decision", "resume_plan", "resume_requirements", "resume_lineage", "material_resolution", "status_endpoint", "summary_endpoint", "resume_endpoint"],
        "filters": ["status", "kind", "limit"],
        "queue_fields": ["max_concurrent_jobs", "running_count", "queued_count", "available_slots", "backlog_count"],
    }


def _candidate_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "count", "ready_count", "site_policy", "ranking", "digest", "candidates", "blockers", "next_actions"],
        "digest_fields": [
            "recommendation",
            "recommended_action",
            "push_title",
            "push_summary",
            "push_payload",
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
            "item_count",
            "ready_count",
            "blocked_count",
            "recommended_action",
            "decision_summary",
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
            "policy_coverage",
            "ranking",
            "decision_summary",
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
            "policy_summary",
            "blockers",
            "next_actions",
            "can_submit",
            "action_label",
            "action_endpoint",
            "submit_request",
            "submit_job_endpoint",
            "submit_tool",
        ],
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
            "tool": "source_url_retorrent_job",
            "description": "Recommended flow when a user sends one source tracker link and a target tracker. The service infers tracker/torrent id, checks duplicates, then proceeds only when rules and confirmations allow.",
            "required_fields": ["source_url", "target"],
            "recommended_fields": ["save_path", "accept_rules", "confirm_upload", "uploaded_qbit_category", "uploaded_qbit_tags"],
            "runbook": [
                {
                    "step": "preflight",
                    "tool": "readiness_bundle",
                    "read": ["live_readiness.ready_for_manual_retorrent", "live_readiness.blockers", "live_readiness.manual_job_template.request"],
                    "continue_when": "live_readiness.ready_for_manual_retorrent=true",
                    "stop_when": ["deployment.ready=false", "site policy not ready", "accept_rules or confirm_upload missing"],
                },
                {
                    "step": "policy_audit",
                    "tool": "site_policies",
                    "read": ["ready", "policy_gap_summary", "execution_readiness", "agent_summary.policy_recommendations"],
                    "continue_when": "ready=true and execution_readiness.ready=true",
                    "stop_when": ["missing rule_review_fingerprint", "missing qBittorrent rate limits", "missing seeding requirements", "automation disabled"],
                },
                {
                    "step": "submit_job",
                    "tool": "source_url_retorrent_job",
                    "request_from": "readiness_bundle.live_readiness.manual_job_template.request",
                    "read": ["job_id", "status", "runtime.status_endpoint", "agent_decision", "closure_handoff", "materials_handoff", "target_upload_handoff", "workflow_context"],
                    "continue_when": "job_id is present",
                    "stop_when": ["closure_handoff.action=stop_duplicate", "closure_handoff.action=configure_policy", "closure_handoff.action=collect_confirmations"],
                },
                {
                    "step": "poll",
                    "tool": "get_job_status",
                    "read": ["status", "runtime.should_poll", "runtime.poll_after_seconds", "agent_decision", "closure_handoff", "blockers", "next_actions"],
                    "continue_when": "status not in queued,running",
                    "repeat_when": "status in queued,running and runtime.should_poll=true",
                    "stop_when": ["status=blocked", "status=failed", "status=cancelled"],
                },
                {
                    "step": "closure_decision",
                    "tool": "get_job_summary",
                    "read": ["closure_handoff.complete", "closure_handoff.action", "closure_handoff.next_step", "closure_handoff.recommended_tool", "closure_handoff.blockers", "closure_handoff.source", "closure_handoff.target", "summary", "evidence", "resume_plan", "resume_requirements", "candidate_submission"],
                    "complete_when": "closure_handoff.complete=true",
                    "resume_with": "closure_handoff.recommended_tool when closure_handoff.next_step.method is not null; pass only closure_handoff.recommended_request plus allowlisted overrides for confirmations, paths, qBittorrent limits, or material files",
                    "stop_when": ["closure_handoff.action=stop_duplicate", "closure_handoff.action=collect_confirmations without explicit user confirmation", "closure_handoff.action=configure_policy", "closure_handoff.action=resolve_blockers and recommended_tool is null"],
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
                    "read": ["job_id", "candidate_submission", "agent_decision", "closure_handoff", "qbit_plan"],
                    "stop_when": ["candidate can_submit=false", "confirm_upload missing", "save_path/path missing"],
                    "then_follow": "source_url_retorrent.poll",
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
                    "read": ["closure_handoff", "resume_plan", "resume_requirements", "resume_state", "next_command_argv", "blockers", "next_actions"],
                    "continue_when": "closure_handoff.next_step.tool=resume_job and resume_plan.allowed=true",
                    "stop_when": ["closure_handoff.complete=true", "resume_plan.allowed=false", "next_command_argv not allowlisted"],
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
        "complete_when": "closure_handoff.complete=true",
        "never_treat_complete_when": ["closure_handoff.action!=done", "closure_handoff.target.uploaded_seeding_ready=false", "closure_handoff.blockers is not empty"],
        "next_step_source": "closure_handoff.next_step",
        "recommended_call_fields": ["recommended_tool", "recommended_endpoint", "recommended_request"],
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
            "command_argv": {"type": "array", "items": {"type": "string"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
            "interruption": {"type": ["object", "null"]},
            "cancellation": {"type": ["object", "null"]},
            "runtime": {"type": "object"},
            "duplicate_check": {"type": "object"},
            "summary_file": {"type": ["string", "null"]},
            "resume_state": {"type": ["object", "null"]},
            "agent_summary": {"type": ["object", "null"]},
            "agent_decision": {"type": ["object", "null"]},
            "candidate_digest": {"type": ["object", "null"]},
            "policy_qbit_defaults": {"type": ["object", "null"]},
            "qbit_plan": {"type": ["object", "null"]},
            "qbit_limit_audit": {"type": ["object", "null"]},
            "qbit_handoff": {"type": ["object", "null"]},
            "materials_handoff": {"type": ["object", "null"]},
            "target_upload_handoff": {"type": ["object", "null"]},
            "closure_handoff": {"type": ["object", "null"]},
            "manual_retorrent_handoff": {"type": ["object", "null"]},
            "candidate_submission_handoff": {"type": ["object", "null"]},
            "resume_plan": {"type": "object"},
            "resume_requirements": {"type": "object"},
            "resume_lineage": {"type": ["object", "null"]},
            "resume_context": {"type": ["object", "null"]},
            "material_resolution": {"type": ["object", "null"]},
            "candidate_submission": {"type": ["object", "null"]},
            "source_reference": {"type": ["object", "null"]},
            "workflow_context": {"type": ["object", "null"]},
            "next_command_argv": {"type": ["array", "null"], "items": {"type": "string"}},
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
            "policy_coverage": {"type": ["object", "null"]},
            "policy_qbit_defaults": {"type": ["object", "null"]},
            "qbit_plan": {"type": ["object", "null"]},
            "qbit_limit_audit": {"type": ["object", "null"]},
            "qbit_handoff": {"type": ["object", "null"]},
            "materials_handoff": {"type": ["object", "null"]},
            "target_upload_handoff": {"type": ["object", "null"]},
            "closure_handoff": {"type": ["object", "null"]},
            "manual_retorrent_handoff": {"type": ["object", "null"]},
            "candidate_submission_handoff": {"type": ["object", "null"]},
            "runtime": {"type": "object"},
            "resume_plan": {"type": "object"},
            "resume_requirements": {"type": "object"},
            "resume_lineage": {"type": ["object", "null"]},
            "resume_context": {"type": ["object", "null"]},
            "material_resolution": {"type": ["object", "null"]},
            "candidate_submission": {"type": ["object", "null"]},
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
            "agent_decision": {"type": "object"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
        },
    }
    site_policy_request_schema = _site_policy_tool_request_schema()
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
            "steps": {"type": "array", "items": {"type": "object"}},
            "next_step": {"type": ["object", "null"]},
            "recommended_tool": {"type": ["string", "null"]},
            "recommended_endpoint": {"type": ["string", "null"]},
            "recommended_request": {"type": ["object", "null"]},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
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
