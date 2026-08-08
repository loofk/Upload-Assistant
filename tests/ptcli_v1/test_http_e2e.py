from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

import pytest

from src.ptcli.service import JobStore, _handler_class
from src.ptcli.source import resolve_source_reference


@contextmanager
def _server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    store = JobStore(tmp_path / "jobs", run_inline=True, recover_interrupted=False, compact_create_results=True)

    async def fake_check_and_submit(job_store: JobStore, request: dict) -> dict:
        source = resolve_source_reference(str(request.get("source_url") or request.get("source")))
        if request.get("duplicate") is True:
            return {
                "status": "blocked",
                "ok": False,
                "duplicate_check": {"searched": True, "exists": True, "dupes": [{"id": "999", "name": "existing"}]},
                "blockers": ["Target duplicate exists."],
                "next_actions": ["Stop without uploading."],
            }
        blockers = []
        if request.get("accept_rules") is not True:
            blockers.append("accept_rules=true is required.")
        if request.get("confirm_upload") is not True:
            blockers.append("confirm_upload=true is required.")
        if blockers:
            return {"status": "blocked", "ok": False, "blockers": blockers, "next_actions": ["Collect explicit confirmation."]}

        tracker = source["tracker"]
        source_hash = ("1" if tracker == "U2" else "2") * 40
        target_hash = ("a" if tracker == "U2" else "b") * 40
        return job_store.create(
            "ptcli.source_url_retorrent",
            request,
            ["ptcli", "retorrent", "--from", tracker, "--source-id", source["source_id"], "--to", "MTEAM", "--json"],
            lambda: {
                "status": "ok",
                "ok": True,
                "summary": {"source_tracker": tracker, "target_tracker": "MTEAM", "closure_complete": True},
                "evidence": {
                    "source": {"torrent_hash": source_hash, "content_path": f"/downloads/{tracker}-{source['source_id']}"},
                    "target": {"uploaded_torrent_hash": target_hash, "injected_torrent_hash": target_hash, "seeding": True},
                },
                "duplicate_check": {"searched": True, "exists": False, "dupes": []},
                "blockers": [],
                "next_actions": [],
            },
        )

    monkeypatch.setattr("src.ptcli.service.create_source_url_check_and_submit_job", fake_check_and_submit)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_class("test-token", store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(base_url: str, path: str, payload: dict, *, token: str | None = "test-token") -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib_request.Request(f"{base_url}{path}", data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get(base_url: str, path: str, *, token: str | None = "test-token") -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib_request.Request(f"{base_url}{path}", headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.mark.parametrize(
    ("source_url", "tracker"),
    [
        ("https://u2.dmhy.org/details.php?id=60635", "U2"),
        ("https://ptchdbits.co/details.php?id=12345", "CHD"),
    ],
)
def test_mocked_source_to_mteam_http_closure(tmp_path, monkeypatch, source_url: str, tracker: str) -> None:
    with _server(tmp_path, monkeypatch) as base_url:
        status, created = _post(
            base_url,
            "/v1/jobs/retorrent/from-url/check-and-submit",
            {"source_url": source_url, "target": "MTEAM", "accept_rules": True, "confirm_upload": True},
        )
        assert status == 202, json.dumps(created, ensure_ascii=False)
        assert created["status"] == "complete"
        with urllib_request.urlopen(
            urllib_request.Request(f"{base_url}{created['links']['summary']}", headers={"Authorization": "Bearer test-token"}),
            timeout=3,
        ) as response:
            summary = json.loads(response.read())

    assert summary["status"] == "complete"
    assert summary["summary"]["source_tracker"] == tracker
    assert summary["evidence"]["source"]["torrent_hash"]
    assert summary["evidence"]["target"]["seeding"] is True


def test_http_duplicate_and_confirmation_gates_are_not_bypassable(tmp_path, monkeypatch) -> None:
    source_url = "https://u2.dmhy.org/details.php?id=60635"
    with _server(tmp_path, monkeypatch) as base_url:
        unauthorized_status, unauthorized = _post(base_url, "/v1/jobs/retorrent/from-url/check-and-submit", {"source_url": source_url, "target": "MTEAM"}, token=None)
        blocked_status, blocked = _post(base_url, "/v1/jobs/retorrent/from-url/check-and-submit", {"source_url": source_url, "target": "MTEAM", "accept_rules": False, "confirm_upload": False})
        duplicate_status, duplicate = _post(base_url, "/v1/jobs/retorrent/from-url/check-and-submit", {"source_url": source_url, "target": "MTEAM", "accept_rules": True, "confirm_upload": True, "duplicate": True})

    assert unauthorized_status == 401
    assert unauthorized["schema_version"] == 1
    assert blocked_status == 200
    assert blocked["status"] == "blocked"
    assert {"accept_rules=true is required.", "confirm_upload=true is required."}.issubset(blocked["blockers"])
    assert duplicate_status == 200
    assert duplicate["duplicate_check"]["exists"] is True
    assert duplicate["job_id"] is None


def test_http_operational_contracts_are_compact_by_default_and_detail_is_opt_in(tmp_path, monkeypatch) -> None:
    source_url = "https://u2.dmhy.org/details.php?id=60635"
    with _server(tmp_path, monkeypatch) as base_url:
        goal_status, goal = _get(base_url, "/v1/goal/progress")
        goal_detail_status, goal_detail = _get(base_url, "/v1/goal/progress?view=detail")
        readiness_status, readiness = _post(base_url, "/v1/readiness/bundle", {"source_url": source_url, "target": "MTEAM"})
        readiness_detail_status, readiness_detail = _post(base_url, "/v1/readiness/bundle", {"source_url": source_url, "target": "MTEAM", "view": "detail"})

    assert goal_status == goal_detail_status == 200
    assert goal["kind"] == "ptcli.goal_progress.compact"
    assert "goal_distance_report" not in goal
    assert "goal_distance_report" in goal_detail
    assert readiness_status == readiness_detail_status == 200
    assert readiness["kind"] == "ptcli.readiness_bundle.compact"
    assert readiness["operator_package"]["safe_to_auto_execute"] is False
    assert readiness["operator_package"]["selected_flow"]["source_tracker"] == "U2"
    assert "live_execution_package" not in readiness
    assert "live_execution_package" in readiness_detail
