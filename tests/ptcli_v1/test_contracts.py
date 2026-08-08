from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from src.ptcli.contracts import (
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
from src.ptcli.service import (
    JobStore,
    agent_manifest_payload,
    openapi_payload,
    public_deployment_check_payload,
    public_goal_progress_payload,
    public_readiness_bundle_payload,
    tools_payload,
)


def test_public_contracts_stay_within_ai_response_budgets() -> None:
    root = Path(tempfile.mkdtemp(prefix="ptcli-contract-"))
    store = JobStore(root, run_inline=True, recover_interrupted=False, compact_create_results=True)
    created = store.create(
        "ptcli.test",
        {"source": "U2", "target": "MTEAM"},
        ["ptcli", "sites"],
        lambda: {
            "status": "ok",
            "ok": True,
            "summary": {"message": "mock closure complete"},
            "evidence": {"torrent_hash": "a" * 40, "content_path": "/downloads/mock"},
            "next_actions": [],
        },
    )

    status = store.get_compact(created["job_id"])
    summary = store.summary_compact(created["job_id"])
    listing = store.list_compact()
    tools = tools_payload()
    manifest = agent_manifest_payload()

    assert json_size_bytes(status) < JOB_STATUS_BUDGET_BYTES
    assert json_size_bytes(summary) < JOB_SUMMARY_BUDGET_BYTES
    assert json_size_bytes(listing) < JOB_LIST_BUDGET_BYTES
    assert json_size_bytes(tools) < TOOLS_BUDGET_BYTES
    assert json_size_bytes(manifest) < AGENT_MANIFEST_BUDGET_BYTES
    assert tools["count"] <= 15
    assert tools["count"] == manifest["tool_count"]


def test_compact_job_poll_is_fast_and_has_stable_short_path() -> None:
    root = Path(tempfile.mkdtemp(prefix="ptcli-poll-"))
    store = JobStore(root, run_inline=True, recover_interrupted=False, compact_create_results=True)
    created = store.create("ptcli.test", {}, ["ptcli", "sites"], lambda: {"status": "blocked", "blockers": ["manual review required"], "next_actions": ["review rules"]})

    durations: list[float] = []
    for _ in range(30):
        started = time.perf_counter()
        payload = store.get_compact(created["job_id"])
        durations.append(time.perf_counter() - started)

    assert sorted(durations)[28] < 0.2
    assert {
        "schema_version",
        "status",
        "ok",
        "job_id",
        "blockers",
        "next_actions",
        "summary",
        "resume_state",
        "duplicate_check",
        "links",
        "evidence",
    }.issubset(payload)
    assert payload["status"] == "blocked"
    assert payload["links"]["summary"].endswith("/summary")


def test_openapi_job_operations_use_compact_envelope() -> None:
    document = openapi_payload(require_auth=True)
    paths = document["paths"]
    status_schema = paths["/v1/jobs/{job_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    summary_schema = paths["/v1/jobs/{job_id}/summary"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]

    for schema in (status_schema, summary_schema):
        assert {"schema_version", "status", "ok", "blockers", "next_actions", "links", "evidence"}.issubset(schema["required"])
        assert "job_handoff" not in schema["properties"]
        assert "live_validation_completion_audit" not in schema["properties"]
    for path in (
        "/v1/jobs/retorrent/check",
        "/v1/jobs/retorrent",
        "/v1/jobs/retorrent/from-url",
        "/v1/jobs/materials/prepare",
        "/v1/jobs/candidates/daily",
    ):
        assert "202" in paths[path]["post"]["responses"]
    assert {"200", "202"}.issubset(paths["/v1/jobs/retorrent/from-url/check-and-submit"]["post"]["responses"])
    assert "202" in paths["/v1/jobs/{job_id}/resume"]["post"]["responses"]
    for path in ("/v1/deployment/check", "/v1/readiness/bundle", "/v1/goal/progress"):
        schema = paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert {"schema_version", "status", "ok", "blockers", "next_actions", "links", "evidence"}.issubset(schema["required"])
        assert any(parameter.get("name") == "view" for parameter in paths[path]["get"].get("parameters", []))


def test_operational_endpoints_default_to_bounded_seedbox_handoff(tmp_path) -> None:
    cookies = tmp_path / "cookies"
    downloads = tmp_path / "downloads"
    jobs = tmp_path / "jobs"
    for path in (cookies, downloads, jobs):
        path.mkdir()
    request = {
        "base_dir": str(Path.cwd()),
        "config": "data/example-config.py",
        "cookies_dir": str(cookies),
        "downloads_path": str(downloads),
        "job_dir": str(jobs),
        "source_url": "https://u2.dmhy.org/details.php?id=60635",
        "target": "MTEAM",
    }

    deployment = public_deployment_check_payload(request)
    readiness = public_readiness_bundle_payload(request)
    progress = public_goal_progress_payload(request)

    assert json_size_bytes(deployment) < DEPLOYMENT_BUDGET_BYTES
    assert json_size_bytes(readiness) < READINESS_BUDGET_BYTES
    assert json_size_bytes(progress) < GOAL_PROGRESS_BUDGET_BYTES
    assert deployment["links"]["detail"].endswith("view=detail")
    assert readiness["operator_package"]["safe_to_auto_execute"] is False
    assert {item["source_tracker"] for item in readiness["operator_package"]["reference_flows"]} == {"U2", "CHD"}
    assert readiness["operator_package"]["completion_contract"]["read"] == "get_job_summary.completion"
    assert readiness["operator_package"]["completion_contract"]["status"] == "not_evaluated"
    assert progress["next_call"]["tool"] == "site_policy_rule_review"


def test_compact_live_job_summary_exposes_final_completion_audit(tmp_path) -> None:
    store = JobStore(tmp_path, run_inline=True, recover_interrupted=False, compact_create_results=True)
    created = store.create(
        "ptcli.source_url_retorrent",
        {"source_url": "https://u2.dmhy.org/details.php?id=60635", "target": "MTEAM", "confirm_upload": True},
        ["ptcli", "retorrent"],
        lambda: {
            "status": "ok",
            "ok": True,
            "live_validation_completion_audit": {
                "report_allowed": True,
                "verdict": "report_complete",
                "failed_checks": [],
                "missing_evidence": [],
                "blockers": [],
                "source": {"torrent_hash": "1" * 40, "path": "/downloads/source"},
                "target": {"torrent_hash": "a" * 40, "seeding": True},
                "duplicate_check": {"searched": True, "exists": False},
                "qbit": {"limits_applied": True},
            },
        },
    )

    summary = store.summary_compact(created["job_id"])

    assert summary["completion"]["status"] == "verified"
    assert summary["completion"]["report_allowed"] is True
    assert summary["completion"]["source"]["torrent_hash"] == "1" * 40
    assert summary["completion"]["target"]["seeding"] is True


def test_compact_live_job_cannot_report_complete_without_final_evidence(tmp_path) -> None:
    store = JobStore(tmp_path, run_inline=True, recover_interrupted=False, compact_create_results=True)
    created = store.create(
        "ptcli.source_url_retorrent",
        {"source_url": "https://ptchdbits.co/details.php?id=12345", "target": "MTEAM", "confirm_upload": True},
        ["ptcli", "retorrent"],
        lambda: {"status": "ok", "ok": True, "summary": {"message": "runner returned without target seeding evidence"}},
    )

    summary = store.summary_compact(created["job_id"])

    assert summary["status"] == "complete"
    assert summary["completion"]["required"] is True
    assert summary["completion"]["report_allowed"] is False
    assert summary["completion"]["status"] != "verified"


def test_static_manifest_is_valid_compact_json() -> None:
    expected = agent_manifest_payload(base_url="http://127.0.0.1:8080")
    for relative in ("ai/openclaw/ptcli.skill.json", "ai/hermes/ptcli.skill.json"):
        payload = json.loads(Path(relative).read_text(encoding="utf-8"))
        assert payload == expected
        assert len(payload["tools"]) <= 15
