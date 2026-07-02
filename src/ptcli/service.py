"""Small JSON HTTP service for AI-driven ptcli automation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from src.ptcli.cli import build_parser, pipeline_payload, retorrent_payload
from src.ptcli.source import resolve_source_reference

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
DEFAULT_SERVICE_PORT = 8080


class ServiceError(Exception):
    """Error that should be returned as a structured API response."""

    def __init__(self, message: str, *, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


def run_service(host: str, port: int, *, api_token: str | None = None) -> None:
    """Run the local ptcli JSON API service."""
    handler = _handler_class(api_token)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"ptcli service listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _handler_class(api_token: str | None) -> type[BaseHTTPRequestHandler]:
    class PtcliServiceHandler(BaseHTTPRequestHandler):
        server_version = "ptcli-service/1"

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send_json(HTTPStatus.OK, health_payload())
                return
            if self.path == "/openapi.json":
                self._send_json(HTTPStatus.OK, openapi_payload(require_auth=bool(api_token)))
                return
            if self.path == "/v1/tools":
                self._send_json(HTTPStatus.OK, tools_payload())
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Endpoint not found."})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"status": "error", "message": "Unauthorized."})
                return
            handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
                "/v1/retorrent/check": lambda payload: asyncio.run(retorrent_check(payload)),
                "/v1/retorrent": lambda payload: asyncio.run(retorrent(payload)),
            }
            handler = handlers.get(self.path)
            if handler is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Endpoint not found."})
                return
            try:
                self._send_json(HTTPStatus.OK, handler(self._read_json()))
            except ServiceError as exc:
                self._send_json(exc.status, {"status": "error", "message": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "message": str(exc)})

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
    return {
        "kind": kind,
        "status": result.get("status", "ok"),
        "ok": result.get("status") not in {"blocked", "error"},
        "request": request,
        "command_argv": ["ptcli", *argv],
        "duplicate_check": _duplicate_check(result),
        "blockers": blockers,
        "next_actions": next_actions,
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
        },
        "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
    }
