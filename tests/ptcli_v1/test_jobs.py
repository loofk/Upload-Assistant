from __future__ import annotations

import json
import threading
import time

from src.ptcli.service import JobStore


def _wait_status(store: JobStore, job_id: str, expected: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    payload = store.get_compact(job_id)
    while payload["status"] != expected and time.monotonic() < deadline:
        time.sleep(0.01)
        payload = store.get_compact(job_id)
    return payload


def test_fixed_worker_queue_preserves_max_concurrency(tmp_path) -> None:
    store = JobStore(tmp_path, run_inline=False, recover_interrupted=False, max_concurrent_jobs=1, compact_create_results=True)
    release_first = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()

    def first_runner() -> dict:
        first_started.set()
        assert release_first.wait(2)
        return {"status": "ok", "ok": True}

    def second_runner() -> dict:
        second_started.set()
        return {"status": "ok", "ok": True}

    first = store.create("ptcli.test", {}, ["ptcli", "first"], first_runner)
    assert first_started.wait(1)
    second = store.create("ptcli.test", {}, ["ptcli", "second"], second_runner)
    assert store.get_compact(first["job_id"])["status"] == "running"
    assert store.get_compact(second["job_id"])["status"] == "queued"
    assert not second_started.is_set()

    release_first.set()
    assert _wait_status(store, first["job_id"], "complete")["status"] == "complete"
    assert _wait_status(store, second["job_id"], "complete")["status"] == "complete"
    assert second_started.is_set()


def test_restart_marks_interrupted_jobs_blocked_and_keeps_resume_link(tmp_path) -> None:
    job_id = "a" * 32
    (tmp_path / f"{job_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "kind": "ptcli.source_url_retorrent",
                "status": "running",
                "ok": False,
                "request": {"source_url": "https://u2.dmhy.org/details.php?id=60635", "target": "MTEAM"},
                "command_argv": ["ptcli", "retorrent", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--json"],
                "created_at": 1,
                "updated_at": 1,
                "blockers": [],
                "next_actions": [],
            }
        ),
        encoding="utf-8",
    )

    store = JobStore(tmp_path, run_inline=True, recover_interrupted=True, compact_create_results=True)
    recovered = store.get_compact(job_id)

    assert recovered["status"] == "blocked"
    assert "service restarted" in recovered["blockers"][0]
    assert recovered["links"]["resume"].endswith("/resume")


def test_corrupt_job_file_does_not_break_compact_listing(tmp_path) -> None:
    store = JobStore(tmp_path, run_inline=True, recover_interrupted=False, compact_create_results=True)
    created = store.create("ptcli.test", {}, ["ptcli", "sites"], lambda: {"status": "ok", "ok": True})
    (tmp_path / f"{'b' * 32}.json").write_text("{broken", encoding="utf-8")

    listing = store.list_compact()

    assert listing["ok"] is True
    assert [job["job_id"] for job in listing["jobs"]] == [created["job_id"]]
