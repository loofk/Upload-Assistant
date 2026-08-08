#!/usr/bin/env python3
"""Verify the focused PTCLI v1 LOCAL_READY contract and Docker image."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ptcli.contracts import (  # noqa: E402
    AGENT_MANIFEST_BUDGET_BYTES,
    DEPLOYMENT_BUDGET_BYTES,
    GOAL_PROGRESS_BUDGET_BYTES,
    JOB_LIST_BUDGET_BYTES,
    JOB_STATUS_BUDGET_BYTES,
    JOB_SUMMARY_BUDGET_BYTES,
    READINESS_BUDGET_BYTES,
    TOOLS_BUDGET_BYTES,
    json_size_bytes,
)
from src.ptcli.service import (  # noqa: E402
    JobStore,
    agent_manifest_payload,
    openapi_payload,
    public_deployment_check_payload,
    public_goal_progress_payload,
    public_readiness_bundle_payload,
    tools_payload,
)

DEFAULT_IMAGE = "upload-assistant:ptcli-local-ready"
DEFAULT_REPORT = ROOT / "tmp" / "ptcli-local-ready.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-docker", action="store_true")
    parser.add_argument("--level", choices=("local", "seedbox-handoff"), default="local")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    checks = [*_contract_checks(), _command_check("docker_compose_config", ["docker", "compose", "config", "-q"])]
    if not args.skip_docker:
        checks.extend(_docker_checks(args.image, skip_build=args.skip_build))
    report = {
        "schema_version": 1,
        "kind": f"ptcli.{args.level.replace('-', '_')}_ready_report",
        "readiness_level": args.level,
        "status": "ready" if all(check["ok"] for check in checks) else "blocked",
        "ok": all(check["ok"] for check in checks),
        "commit": _git_sha(),
        "generated_at": int(time.time()),
        "checks": checks,
        "blockers": [check["message"] for check in checks if not check["ok"]],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def _contract_checks() -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="ptcli-local-ready-") as raw_root:
        store = JobStore(raw_root, run_inline=True, recover_interrupted=False, compact_create_results=True)
        job = store.create(
            "ptcli.verify",
            {"source": "U2", "target": "MTEAM"},
            ["ptcli", "sites"],
            lambda: {"status": "ok", "ok": True, "summary": {"closure_complete": True}, "evidence": {"torrent_hash": "a" * 40}},
        )
        measurements: dict[str, tuple[dict[str, Any], int]] = {
            "job_status": (store.get_compact(job["job_id"]), JOB_STATUS_BUDGET_BYTES),
            "job_summary": (store.summary_compact(job["job_id"]), JOB_SUMMARY_BUDGET_BYTES),
            "job_list": (store.list_compact(), JOB_LIST_BUDGET_BYTES),
            "tools": (tools_payload(), TOOLS_BUDGET_BYTES),
            "agent_manifest": (agent_manifest_payload(), AGENT_MANIFEST_BUDGET_BYTES),
        }
        runtime_root = Path(raw_root) / "runtime"
        cookies_dir = runtime_root / "cookies"
        downloads_dir = runtime_root / "downloads"
        cookies_dir.mkdir(parents=True)
        downloads_dir.mkdir(parents=True)
        operational_request = {
            "base_dir": str(ROOT),
            "config": "data/example-config.py",
            "cookies_dir": str(cookies_dir),
            "job_dir": str(Path(raw_root) / "jobs"),
            "downloads_path": str(downloads_dir),
            "source_url": "https://u2.dmhy.org/details.php?id=60635",
            "target": "MTEAM",
        }
        measurements.update(
            {
                "deployment": (public_deployment_check_payload(operational_request), DEPLOYMENT_BUDGET_BYTES),
                "readiness": (public_readiness_bundle_payload(operational_request), READINESS_BUDGET_BYTES),
                "goal_progress": (public_goal_progress_payload(operational_request), GOAL_PROGRESS_BUDGET_BYTES),
            }
        )
        checks: list[dict[str, Any]] = []
        for name, (payload, budget) in measurements.items():
            size = json_size_bytes(payload)
            checks.append({"name": f"contract_size.{name}", "ok": size < budget, "size_bytes": size, "budget_bytes": budget, "message": f"{name}: {size}/{budget} bytes"})

        durations: list[float] = []
        for _ in range(30):
            started = time.perf_counter()
            store.get_compact(job["job_id"])
            durations.append(time.perf_counter() - started)
        p95_ms = sorted(durations)[28] * 1000
        checks.append({"name": "latency.job_status_p95", "ok": p95_ms < 200, "p95_ms": round(p95_ms, 3), "budget_ms": 200, "message": f"job status p95: {p95_ms:.3f} ms"})

        tool_count = int(tools_payload()["count"])
        checks.append({"name": "contract.tool_count", "ok": tool_count <= 15, "count": tool_count, "budget": 15, "message": f"tool count: {tool_count}/15"})
        handoff = measurements["readiness"][0].get("operator_package", {})
        reference_sources = {item.get("source_tracker") for item in handoff.get("reference_flows", []) if isinstance(item, dict)}
        handoff_ok = handoff.get("safe_to_auto_execute") is False and {"U2", "CHD"}.issubset(reference_sources) and handoff.get("completion_contract", {}).get("required_values", {}).get("report_allowed") is True
        checks.append({"name": "contract.seedbox_handoff", "ok": handoff_ok, "handoff_id": handoff.get("handoff_id"), "message": "Seedbox handoff is bounded, non-executing, and covers U2/CHD -> MTEAM."})
        openapi = openapi_payload(require_auth=True)
        required_paths = {"/health", "/openapi.json", "/v1/tools", "/v1/jobs", "/v1/jobs/{job_id}", "/v1/jobs/{job_id}/summary", "/v1/jobs/{job_id}/resume"}
        missing_paths = sorted(required_paths.difference(openapi.get("paths", {})))
        checks.append({"name": "contract.openapi_core_paths", "ok": not missing_paths, "missing": missing_paths, "message": "OpenAPI core paths present." if not missing_paths else f"OpenAPI missing: {', '.join(missing_paths)}"})

        brief = public_goal_progress_payload({"config": "/definitely/missing/config.py"})
        brief_ok = brief.get("status") == "blocked" and json_size_bytes(brief) < 8 * 1024
        checks.append({"name": "contract.goal_progress_brief", "ok": brief_ok, "size_bytes": json_size_bytes(brief), "message": "Missing config returns a bounded structured blocker."})
        return checks


def _docker_checks(image: str, *, skip_build: bool) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if not skip_build:
        build = _command_check("docker_build", ["docker", "build", "-f", "Dockerfile.ptcli", "-t", image, "."], timeout=900)
        checks.append(build)
        if not build["ok"]:
            return checks

    with tempfile.TemporaryDirectory(prefix="ptcli-container-") as raw_root:
        root = Path(raw_root)
        (root / "cookies").mkdir()
        (root / "downloads").mkdir()
        (root / "tmp").mkdir()
        config = root / "config.py"
        config.write_text("config = {'DEFAULT': {}, 'TRACKERS': {}, 'TORRENT_CLIENTS': {}}\n", encoding="utf-8")
        command = [
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            "127.0.0.1::8080",
            "-e",
            "PTCLI_API_TOKEN=local-ready-token",
            "-v",
            f"{config}:/Upload-Assistant/data/config.py:ro",
            "-v",
            f"{root / 'cookies'}:/Upload-Assistant/data/cookies:ro",
            "-v",
            f"{root / 'downloads'}:/downloads:rw",
            "-v",
            f"{root / 'tmp'}:/Upload-Assistant/tmp:rw",
            image,
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
        ]
        started = _run(command, timeout=30)
        if started.returncode != 0:
            checks.append({"name": "docker_run", "ok": False, "message": _output(started)})
            return checks
        container_id = started.stdout.strip()
        try:
            port_result = _run(["docker", "port", container_id, "8080/tcp"], timeout=10)
            if port_result.returncode != 0:
                checks.append({"name": "docker_port", "ok": False, "message": _output(port_result)})
                return checks
            port = port_result.stdout.strip().rsplit(":", 1)[-1]
            base_url = f"http://127.0.0.1:{port}"
            checks.extend(_wait_for_container_contract(base_url))
        finally:
            _run(["docker", "stop", container_id], timeout=30)
    return checks


def _wait_for_container_contract(base_url: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 30
    health_status = 0
    health_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        health_status, health_payload = _http_json(f"{base_url}/health")
        if health_status == 200:
            break
        time.sleep(0.25)
    checks = [{"name": "docker.health", "ok": health_status == 200 and health_payload.get("status") == "ok", "http_status": health_status, "message": "Container health endpoint is ready."}]
    for name, path in (("openapi", "/openapi.json"), ("tools", "/v1/tools"), ("manifest", "/.well-known/ptcli-agent.json")):
        status, payload = _http_json(f"{base_url}{path}")
        checks.append({"name": f"docker.{name}", "ok": status == 200 and isinstance(payload, dict), "http_status": status, "size_bytes": json_size_bytes(payload), "message": f"Container {path} returned HTTP {status}."})
    unauthorized, unauthorized_payload = _http_json(f"{base_url}/v1/jobs")
    authorized, authorized_payload = _http_json(f"{base_url}/v1/jobs", token="local-ready-token")
    checks.append({"name": "docker.auth_unauthorized", "ok": unauthorized == 401 and unauthorized_payload.get("schema_version") == 1, "http_status": unauthorized, "message": "Protected endpoint rejects missing token."})
    checks.append({"name": "docker.auth_authorized", "ok": authorized == 200 and authorized_payload.get("ok") is True, "http_status": authorized, "message": "Protected endpoint accepts bearer token."})
    operational_paths = (
        ("deployment", "/v1/deployment/check", DEPLOYMENT_BUDGET_BYTES),
        ("readiness", "/v1/readiness/bundle?source_url=https%3A%2F%2Fu2.dmhy.org%2Fdetails.php%3Fid%3D60635&target=MTEAM", READINESS_BUDGET_BYTES),
        ("goal_progress", "/v1/goal/progress", GOAL_PROGRESS_BUDGET_BYTES),
    )
    for name, path, budget in operational_paths:
        status, payload = _http_json(f"{base_url}{path}", token="local-ready-token")
        size = json_size_bytes(payload)
        compact_ok = status == 200 and payload.get("schema_version") == 1 and size < budget and str(payload.get("kind") or "").endswith(".compact")
        checks.append({"name": f"docker.{name}_compact", "ok": compact_ok, "http_status": status, "size_bytes": size, "budget_bytes": budget, "message": f"Container {path} returned the compact v1 contract."})
    readiness_payload = _http_json(f"{base_url}{operational_paths[1][1]}", token="local-ready-token")[1]
    handoff = readiness_payload.get("operator_package") if isinstance(readiness_payload.get("operator_package"), dict) else {}
    checks.append({"name": "docker.seedbox_handoff_safety", "ok": handoff.get("safe_to_auto_execute") is False and handoff.get("requires_operator_submission") is True, "message": "Container seedbox handoff cannot silently execute live upload."})
    return checks


def _http_json(url: str, *, token: str | None = None) -> tuple[int, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib_request.Request(url, headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib_error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except json.JSONDecodeError:
            return exc.code, {}
    except (OSError, json.JSONDecodeError):
        return 0, {}


def _command_check(name: str, command: list[str], *, timeout: int = 60) -> dict[str, Any]:
    result = _run(command, timeout=timeout)
    return {"name": name, "ok": result.returncode == 0, "command": command, "message": _output(result) or f"{name} passed."}


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or result.stderr or "").strip()[-4000:]


def _git_sha() -> str | None:
    result = _run(["git", "rev-parse", "HEAD"], timeout=10)
    return result.stdout.strip() if result.returncode == 0 else None


if __name__ == "__main__":
    raise SystemExit(main())
