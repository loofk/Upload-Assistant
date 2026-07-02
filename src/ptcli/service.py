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
from urllib.parse import urlparse

from src.ptcli.candidates import DEFAULT_CANDIDATE_LIMIT, build_daily_candidates
from src.ptcli.cli import build_parser, pipeline_payload, retorrent_payload
from src.ptcli.config import load_config
from src.ptcli.source import resolve_source_reference

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
DEFAULT_SERVICE_PORT = 8080
JOB_SCHEMA_VERSION = 1
JOB_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
RESUME_COMMAND_ALLOWLIST = {"pipeline", "target-upload", "doctor", "summary-check", "retorrent"}


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
                    "duplicate_check": result.get("duplicate_check") if isinstance(result.get("duplicate_check"), dict) else None,
                    "result": result,
                }
            )
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
            path = urlparse(self.path).path
            if path == "/health":
                self._send_json(HTTPStatus.OK, health_payload())
                return
            if path == "/openapi.json":
                self._send_json(HTTPStatus.OK, openapi_payload(require_auth=bool(api_token)))
                return
            if path == "/v1/tools":
                self._send_json(HTTPStatus.OK, tools_payload())
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
    _append_optional(argv, "--uploaded-qbit-category", request.get("uploaded_qbit_category"))
    _append_optional(argv, "--uploaded-qbit-tags", request.get("uploaded_qbit_tags"))
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
        "result_status": _nested_value(job.get("result"), "status"),
        "next_stage": _nested_value(job.get("result"), "next_stage"),
        "next_command": _nested_value(job.get("result"), "next_command"),
        "next_command_argv": _result_next_command_argv(job.get("result")),
        "should_execute_next_command": _nested_value(job.get("result"), "should_execute_next_command"),
        "automation_action": _nested_value(job.get("result"), "automation_action"),
    }


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


def tools_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "tools": [
            {
                "name": "retorrent_check",
                "method": "POST",
                "path": "/v1/retorrent/check",
                "description": "Resolve a source tracker URL, fetch source metadata, and check whether the MTEAM target already has duplicates. This endpoint never uploads.",
            },
            {
                "name": "retorrent_execute_if_no_duplicate",
                "method": "POST",
                "path": "/v1/retorrent",
                "description": "Run the live retorrent closure when execute=true. The existing rule and duplicate gates block unsafe uploads.",
            },
            {
                "name": "retorrent_check_job",
                "method": "POST",
                "path": "/v1/jobs/retorrent/check",
                "description": "Create an asynchronous duplicate-check job and return a job_id for polling.",
            },
            {
                "name": "retorrent_job",
                "method": "POST",
                "path": "/v1/jobs/retorrent",
                "description": "Create an asynchronous retorrent job and return a job_id for polling long-running download, material, upload, and qBittorrent steps.",
            },
            {
                "name": "daily_candidates",
                "method": "POST",
                "path": "/v1/candidates/daily",
                "description": "Return up to 10 source/target retorrent candidates with metadata availability, duplicate status, blockers, and an executable retorrent request template.",
            },
            {
                "name": "daily_candidates_job",
                "method": "POST",
                "path": "/v1/jobs/candidates/daily",
                "description": "Create an asynchronous daily-candidate discovery job and return a job_id for polling.",
            },
            {
                "name": "get_job_status",
                "method": "GET",
                "path": "/v1/jobs/{job_id}",
                "description": "Return short AI-readable job status, blockers, next actions, duplicate check, summary file, and resume state.",
            },
            {
                "name": "resume_job",
                "method": "POST",
                "path": "/v1/jobs/{job_id}/resume",
                "description": "Create a follow-up job from the allowlisted next_command_argv generated by a blocked or failed job.",
            },
        ],
        "example": {
            "source": "https://u2.dmhy.org/details.php?id=60635",
            "target": "MTEAM",
            "execute": True,
            "accept_rules": True,
            "confirm_upload": True,
            "save_path": "/downloads",
            "uploaded_qbit_category": "MTEAM",
            "uploaded_qbit_tags": "retorrent",
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
            "next_command_argv": {"type": ["array", "null"], "items": {"type": "string"}},
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
            "candidates": {"type": "array", "items": {"type": "object"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
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
                    "responses": {"200": {"description": "Job summary and summary-file payload when available."}},
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
