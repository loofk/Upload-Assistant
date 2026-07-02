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
from src.ptcli.doctor import build_runtime_dependency_check
from src.ptcli.source import resolve_source_reference

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
DEFAULT_SERVICE_PORT = 8080
JOB_SCHEMA_VERSION = 1
JOB_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
RESUME_COMMAND_ALLOWLIST = {"pipeline", "target-upload", "doctor", "summary-check", "retorrent"}
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

    def __init__(self, root: str | Path | None = None, *, run_inline: bool = False) -> None:
        self.root = _resolve_job_dir(root)
        self.run_inline = run_inline
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

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
            thread = threading.Thread(target=self._run, args=(job_id, runner), daemon=True)
            thread.start()
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        return _job_public_payload(self._read(job_id))

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
            "result": job.get("result"),
            "blockers": _string_list(job.get("blockers")),
            "next_actions": _string_list(job.get("next_actions")),
        }

    def resume(self, job_id: str) -> dict[str, Any]:
        parent = self._read(job_id)
        status = str(parent.get("status") or "")
        if status in {"queued", "running"}:
            raise ServiceError(f"Job {job_id} is still {status}; wait before resuming.", status=HTTPStatus.CONFLICT)
        argv = _resume_argv_from_job(parent)
        allowed, reason = _resume_command_allowed(argv)
        request = {
            "parent_job_id": job_id,
            "parent_status": parent.get("status"),
            "next_command_argv": argv,
            "resume_allowed": allowed,
            "resume_blocker": reason,
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

    def _run(self, job_id: str, runner: Callable[[], dict[str, Any]]) -> None:
        job = self._read(job_id)
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


def run_service(host: str, port: int, *, api_token: str | None = None, job_dir: str | None = None) -> None:
    """Run the local ptcli JSON API service."""
    job_store = JobStore(job_dir)
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
                "/v1/candidates/daily": lambda payload: asyncio.run(daily_candidates(payload)),
                "/v1/jobs/retorrent/check": lambda payload: create_retorrent_check_job(job_store, payload),
                "/v1/jobs/retorrent": lambda payload: create_retorrent_job(job_store, payload),
                "/v1/jobs/retorrent/submit": lambda payload: create_manual_retorrent_job(job_store, payload),
                "/v1/jobs/candidates/daily": lambda payload: create_daily_candidates_job(job_store, payload),
            }
            if path.startswith("/v1/jobs/") and path.endswith("/resume"):
                try:
                    self._send_json(HTTPStatus.ACCEPTED, self._job_resume(path))
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

        def _job_resume(self, path: str) -> dict[str, Any]:
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["v1", "jobs"] and parts[3] == "resume":
                return job_store.resume(parts[2])
            raise ServiceError("Job resume endpoint not found.", status=HTTPStatus.NOT_FOUND)

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
    effective_request = {**request, "execute": True, "execute_if_no_duplicate": True, "manual_retorrent": True}
    _, normalized_request, argv = _retorrent_execute_args(effective_request)
    normalized_request = {**normalized_request, "mode": "manual_retorrent", "execute_if_no_duplicate": True}
    return job_store.create(
        "ptcli.manual_retorrent",
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
        "candidates": result.get("candidates", []),
        "count": result.get("count", 0),
        "ready_count": result.get("ready_count", 0),
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
        "target_trackers": target_trackers,
        "execute": execute,
        "accept_rules": bool(request.get("accept_rules")),
        "confirm_upload": bool(request.get("confirm_upload")),
        "client": request.get("client") or "default",
        "config": request.get("config"),
        "path": request.get("path") or request.get("content_path"),
        "save_path": request.get("save_path"),
    }


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
        "blockers": _string_list(job.get("blockers")),
        "next_actions": _string_list(job.get("next_actions")),
        "duplicate_check": job.get("duplicate_check"),
        "summary_file": job.get("summary_file"),
        "resume_state": job.get("resume_state"),
        "agent_summary": job.get("agent_summary") if isinstance(job.get("agent_summary"), dict) else _agent_summary(job.get("result")),
        "agent_decision": job.get("agent_decision") if isinstance(job.get("agent_decision"), dict) else _agent_decision(job),
        "result_status": _nested_value(job.get("result"), "status"),
        "next_stage": _nested_value(job.get("result"), "next_stage"),
        "next_command": _nested_value(job.get("result"), "next_command"),
        "next_command_argv": _result_next_command_argv(job.get("result")),
        "should_execute_next_command": _nested_value(job.get("result"), "should_execute_next_command"),
        "automation_action": _nested_value(job.get("result"), "automation_action"),
    }


def _agent_decision(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    status = str(job.get("status") or "unknown")
    blockers = _string_list(job.get("blockers"))
    next_actions = _string_list(job.get("next_actions"))
    duplicate_check = _job_duplicate_check(job)
    next_command_argv = _result_next_command_argv(result)
    resume_state = job.get("resume_state") if isinstance(job.get("resume_state"), dict) else _result_resume_state(result)
    missing_confirmations = _missing_live_confirmations(request)
    duplicate_exists = duplicate_check.get("exists") is True
    resume_available = bool(next_command_argv or (isinstance(resume_state, dict) and resume_state.get("resume_available") is True))
    should_poll = status in {"queued", "running"}
    should_resume = status in {"blocked", "failed"} and resume_available and not duplicate_exists and not missing_confirmations
    can_attempt_live = not duplicate_exists and not missing_confirmations and status not in {"queued", "running"}

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
        "can_attempt_live": can_attempt_live,
        "should_poll": should_poll,
        "should_resume": should_resume,
        "resume_available": resume_available,
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
    downloads_path = Path(str(request.get("downloads_path") or os.environ.get("PTCLI_DOWNLOADS_PATH") or "/downloads")).expanduser()
    client = str(request.get("client") or "default")

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

    blockers = [str(check.get("message")) for check in checks if check.get("ok") is False and check.get("blocking", True) is not False]
    warnings = [str(check.get("message")) for check in checks if check.get("ok") is False and check.get("blocking") is False]
    next_actions = _deployment_next_actions(checks)
    ready = not blockers
    return {
        "kind": "ptcli.deployment_check",
        "status": "ok" if ready else "blocked",
        "ok": ready,
        "ready": ready,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "next_actions": next_actions,
        "paths": {
            "base_dir": str(base_dir),
            "config": str(config_path),
            "cookies_dir": str(cookies_dir),
            "tmp_dir": str(tmp_dir),
            "job_dir": str(job_dir),
            "downloads_path": str(downloads_path),
        },
        "qbit": qbit,
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


def _agent_tool_schemas() -> list[dict[str, Any]]:
    retorrent_request_schema = _retorrent_tool_request_schema()
    manual_retorrent_request_schema = _manual_retorrent_tool_request_schema()
    candidate_request_schema = _daily_candidate_tool_request_schema()
    job_id_schema = _job_id_tool_request_schema()
    return [
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
            "name": "daily_candidates",
            "method": "POST",
            "path": "/v1/candidates/daily",
            "description": "Return up to 10 ranked source/target retorrent candidates with metadata availability, duplicate status, policy blockers, risk signals, and an executable retorrent request template.",
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
            "response_contract": _job_response_contract(),
            "workflow_hints": {"poll_with": "get_job_status", "summary_with": "get_job_summary"},
            "safety": {"mutates_state": True, "live_upload": False, "requires_confirmation": []},
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
            "response_contract": {"required_fields": ["status", "ok", "job_id", "summary_file", "summary", "agent_summary", "agent_decision", "result", "blockers", "next_actions"]},
            "safety": {"mutates_state": False, "live_upload": False, "requires_confirmation": []},
        },
        {
            "name": "resume_job",
            "method": "POST",
            "path": "/v1/jobs/{job_id}/resume",
            "description": "Create a follow-up job from the allowlisted next_command_argv generated by a blocked or failed job.",
            "input_schema": job_id_schema,
            "response_contract": _job_response_contract(),
            "workflow_hints": {"poll_with": "get_job_status", "summary_with": "get_job_summary"},
            "safety": {"mutates_state": True, "live_upload": "inherits_from_resume_command", "requires_confirmation": ["existing allowlisted next_command_argv"]},
        },
        {
            "name": "agent_manifest",
            "method": "GET",
            "path": "/.well-known/ptcli-agent.json",
            "description": "Return an OpenClaw/Hermes-friendly skill manifest with OpenAPI, tool, auth, safety, and workflow metadata.",
            "input_schema": {"type": "object", "required": [], "properties": {}},
            "response_contract": {"required_fields": ["schema_version", "base_url", "auth", "discovery", "safety", "tools", "default_workflows"]},
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
                    "downloads_path": {"type": "string"},
                    "client": {"type": "string", "default": "default"},
                },
            },
            "response_contract": {
                "required_fields": ["status", "ok", "ready", "checks", "blockers", "warnings", "next_actions", "paths", "qbit"],
                "status_values": ["ok", "blocked"],
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
            "qbit_upload_limit": {"type": ["string", "integer"], "description": "Source torrent upload limit, e.g. 500KiB/s."},
            "qbit_download_limit": {"type": ["string", "integer"], "description": "Source torrent download limit, e.g. 20MiB/s."},
            "uploaded_qbit_upload_limit": {"type": ["string", "integer"], "description": "Uploaded target torrent upload limit, e.g. 2MiB/s."},
            "uploaded_qbit_download_limit": {"type": ["string", "integer"]},
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


def _job_id_tool_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["job_id"],
        "properties": {"job_id": {"type": "string", "description": "32-character ptcli job id returned by a job creation endpoint."}},
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


def _job_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "job_id", "kind", "request", "command_argv", "blockers", "next_actions", "summary_file", "resume_state", "agent_summary", "agent_decision"],
        "status_values": ["queued", "running", "blocked", "failed", "complete"],
        "blocked_fields": ["blockers", "next_actions", "resume_state", "next_command_argv", "agent_decision"],
    }


def _candidate_response_contract() -> dict[str, Any]:
    return {
        "required_fields": ["status", "ok", "count", "ready_count", "site_policy", "ranking", "candidates", "blockers", "next_actions"],
        "candidate_fields": [
            "status",
            "source",
            "source_info",
            "duplicate_check",
            "source_policy",
            "target_policies",
            "ranking",
            "recommendation",
            "blockers",
            "agent_workflow",
            "submit_request",
            "submit_job_endpoint",
            "submit_tool",
            "execute_request",
            "execute_job_endpoint",
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
            "openapi": f"{public_base_url}/openapi.json",
            "tools": f"{public_base_url}/v1/tools",
            "manifest": f"{public_base_url}/.well-known/ptcli-agent.json",
        },
        "safety": {
            "live_upload_requires": ["accept_rules=true", "confirm_upload=true"],
            "never_skip": ["site rule gates", "target duplicate check", "uploaded torrent injection/seeding evidence"],
            "blocked_contract": "When rules, duplicate checks, qBittorrent evidence, or required confirmations are missing, APIs return status=blocked with blockers and next_actions.",
        },
        "default_workflows": [
            {
                "name": "manual_retorrent",
                "tool": "manual_retorrent_job",
                "description": "Create the primary source URL plus target tracker job. It checks duplicates and only proceeds when rule, duplicate, and confirmation gates allow.",
                "required_fields": ["source", "target"],
                "recommended_fields": ["save_path", "accept_rules", "confirm_upload", "uploaded_qbit_category", "uploaded_qbit_tags"],
            },
            {
                "name": "daily_candidates",
                "tool": "daily_candidates_job",
                "description": "Find up to 10 ranked source/target retorrent candidates with duplicate checks, policy blockers, risk signals, and executable request templates.",
                "required_fields": ["source_tracker", "target"],
            },
            {
                "name": "resume_blocked_job",
                "tool": "resume_job",
                "description": "Resume a blocked/failed job using allowlisted next_command_argv emitted by ptcli summaries.",
                "required_fields": ["job_id"],
            },
        ],
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
            "status": {"type": "string", "enum": ["queued", "running", "blocked", "failed", "complete"]},
            "ok": {"type": "boolean"},
            "job_id": {"type": "string"},
            "kind": {"type": "string"},
            "request": {"type": "object"},
            "command_argv": {"type": "array", "items": {"type": "string"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
            "duplicate_check": {"type": "object"},
            "summary_file": {"type": ["string", "null"]},
            "resume_state": {"type": ["object", "null"]},
            "agent_summary": {"type": ["object", "null"]},
            "agent_decision": {"type": ["object", "null"]},
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
            "result": {"type": ["object", "null"]},
            "blockers": {"type": "array", "items": {"type": "string"}},
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
            "candidates": {"type": "array", "items": {"type": "object"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next_actions": {"type": "array", "items": {"type": "string"}},
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
            "tools": {"type": "array", "items": {"type": "object"}},
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
            "qbit": {"type": "object"},
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
            "/v1/deployment/check": {
                "get": {
                    "operationId": "checkPtcliDeployment",
                    "security": token_security,
                    "parameters": [
                        {"name": "config", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "base_dir", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "downloads_path", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "client", "in": "query", "required": False, "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "Local deployment readiness report.", "content": {"application/json": {"schema": deployment_response_schema}}}},
                }
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
            "/v1/candidates/daily": {
                "post": {
                    "operationId": "dailyRetorrentCandidates",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": candidate_request_schema}}},
                    "responses": {"200": {"description": "Daily retorrent candidates.", "content": {"application/json": {"schema": candidate_response_schema}}}},
                }
            },
            "/v1/jobs/candidates/daily": {
                "post": {
                    "operationId": "createDailyRetorrentCandidateJob",
                    "security": token_security,
                    "requestBody": {"required": True, "content": {"application/json": {"schema": candidate_request_schema}}},
                    "responses": {"200": {"description": "Queued daily-candidate discovery job.", "content": {"application/json": {"schema": job_response_schema}}}},
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
                    "responses": {"202": {"description": "Queued resume job.", "content": {"application/json": {"schema": job_response_schema}}}},
                }
            },
        },
        "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
    }
