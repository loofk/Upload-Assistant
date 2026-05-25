import argparse
import asyncio
import json
import shlex
from pathlib import Path

import pytest
from torf import Torrent

import src.ptcli.cli as ptcli_cli
import src.ptcli.doctor as ptcli_doctor
import src.ptcli.source as ptcli_source
import src.ptcli.target as ptcli_target
from src.ptcli.cli import _with_captured_stdout, build_parser, build_plan, main
from src.ptcli.config import resolve_client_config
from src.ptcli.credentials import build_flow_check
from src.ptcli.doctor import build_doctor_check
from src.ptcli.mainland import normalize_tracker, parse_tracker_list
from src.ptcli.qbit import QbitReadOnlyService, match_torrents, summarize_torrent
from src.ptcli.rules import build_rule_check
from src.ptcli.source import create_source_meta, extract_torrent_id, source_info_from_tuple
from src.ptcli.target import (
    build_mteam_description_draft,
    build_mteam_field_mapping,
    build_mteam_meta_draft,
    build_mteam_prepare_preview,
    build_mteam_rule_review,
    build_mteam_upload_gate,
    build_mteam_upload_preflight,
    create_mteam_upload_torrent_candidate,
    extract_mteam_uploaded_torrent_id,
    load_mteam_prepare_package,
    search_mteam_duplicates,
    upload_mteam_from_package,
    write_mteam_prepare_package,
)


class FakeQbitClient:
    def __init__(self) -> None:
        self.logged_in = False
        self.added_kwargs = None

    def auth_log_in(self) -> None:
        self.logged_in = True

    def torrents_info(self, **kwargs):
        if kwargs.get("torrent_hashes"):
            return [{"name": "One", "hash": kwargs["torrent_hashes"], "content_path": "/downloads/One", "progress": 1.0}]
        return [{"name": "One", "hash": "a" * 40, "content_path": "/downloads/One", "progress": 1.0}]

    def torrents_export(self, torrent_hash: str) -> bytes:
        return f"torrent:{torrent_hash}".encode()

    def torrents_add(self, **kwargs):
        self.added_kwargs = kwargs


class TaggedQbitClient(FakeQbitClient):
    def torrents_info(self, **kwargs):
        torrent_hash = kwargs.get("torrent_hashes") or "a" * 40
        return [
            {
                "name": "One",
                "hash": torrent_hash,
                "save_path": "/downloads",
                "content_path": "/downloads/One",
                "progress": 1.0,
                "category": "MTEAM",
                "tags": "retorrent,uploaded",
            }
        ]


class IncompleteQbitClient(FakeQbitClient):
    def torrents_info(self, **kwargs):
        if kwargs.get("torrent_hashes"):
            return [{"name": "One", "hash": kwargs["torrent_hashes"], "content_path": "/downloads/One", "progress": 0.5}]
        return [{"name": "One", "hash": "a" * 40, "content_path": "/downloads/One", "progress": 0.5}]


class SeedingStateQbitClient(FakeQbitClient):
    def torrents_info(self, **kwargs):
        torrent_hash = kwargs.get("torrent_hashes") or "a" * 40
        return [{"name": "One", "hash": torrent_hash, "content_path": "/downloads/One", "progress": 1.0, "state": "uploading"}]


class EmptyQbitClient(FakeQbitClient):
    def torrents_info(self, **kwargs):
        _ = kwargs
        return []


class DelayedQbitClient(FakeQbitClient):
    def __init__(self, visible_after: int = 2) -> None:
        super().__init__()
        self.visible_after = visible_after
        self.info_calls = 0

    def torrents_info(self, **kwargs):
        self.info_calls += 1
        if kwargs.get("torrent_hashes") and self.info_calls >= self.visible_after:
            return [{"name": "One", "hash": kwargs["torrent_hashes"], "content_path": "/downloads/One", "progress": 1.0}]
        return []


class DelayedTaggedQbitClient(FakeQbitClient):
    def __init__(self, metadata_after: int = 3) -> None:
        super().__init__()
        self.metadata_after = metadata_after
        self.info_calls = 0

    def torrents_info(self, **kwargs):
        self.info_calls += 1
        torrent_hash = kwargs.get("torrent_hashes") or "a" * 40
        payload = {
            "name": "One",
            "hash": torrent_hash,
            "save_path": "/downloads",
            "content_path": "/downloads/One",
            "progress": 1.0,
        }
        if self.info_calls >= self.metadata_after:
            payload.update({"category": "MTEAM", "tags": "retorrent,uploaded"})
        return [payload]


class WrongHashQbitClient(FakeQbitClient):
    def torrents_info(self, **kwargs):
        if kwargs.get("torrent_hashes"):
            return [{"name": "Wrong", "hash": "f" * 40, "save_path": "/downloads", "content_path": "/downloads/Wrong", "progress": 1.0}]
        return []


class WrongPathQbitClient(FakeQbitClient):
    def torrents_info(self, **kwargs):
        _ = kwargs
        return [{"name": "One.2024", "hash": "a" * 40, "save_path": "/other", "content_path": "/other/One.2024", "progress": 1.0}]


def make_mteam_safe_torrent(tmp_path, name: str = "upload") -> str:
    content = tmp_path / f"{name}.mkv"
    content.write_bytes(b"content")
    source_torrent = tmp_path / f"{name}.source.torrent"
    torrent = Torrent(path=str(content), trackers=["https://source.example/passkey/announce"], comment="private comment")
    torrent.generate()
    torrent.write(str(source_torrent), overwrite=True)
    return create_mteam_upload_torrent_candidate(str(source_torrent), str(tmp_path / "exported"))["path"]


def mteam_ready_stages() -> list[dict]:
    return [
        {"stage": "rule-check", "ok": True, "result": build_rule_check("U2", ["MTEAM"], accept_rules=True)},
        {"stage": "match", "ok": True, "result": {"count": 1, "matches": [{"content_path": "/downloads/Example", "hash": "a" * 40}]}},
        {"stage": "target-dupe-check", "ok": True, "result": {"searched": True, "count": 0, "dupes": []}},
    ]


def mteam_clean_rule_review() -> dict:
    rule_check = build_rule_check("U2", ["MTEAM"], accept_rules=True)
    return {
        "rule_check_ready": True,
        "blockers": [],
        "rule_obligations": rule_check["rule_obligations"],
    }


def test_normalize_tracker_aliases() -> None:
    assert normalize_tracker("m-team") == "MTEAM"
    assert normalize_tracker("pterclub") == "PTER"


def test_parse_tracker_list_deduplicates() -> None:
    assert parse_tracker_list("mteam, M-TEAM, tjupt") == ["MTEAM", "TJUPT"]


def test_sites_json_exposes_capability_matrix(capsys) -> None:
    code = main(["sites", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert "MTEAM" in payload["sites"]
    assert payload["capabilities"]["U2"]["source_info"] is True
    assert payload["capabilities"]["U2"]["source_download"] is True
    assert payload["capabilities"]["U2"]["full_live_closure_to_mteam"] is True
    assert payload["capabilities"]["HDS"]["source_info"] is True
    assert payload["capabilities"]["HDS"]["source_download"] is False
    assert payload["capabilities"]["HDS"]["full_live_closure_to_mteam"] is False
    assert payload["capabilities"]["MTEAM"]["target_upload"] is True
    assert "U2" in payload["full_live_closure_sources"]
    assert "HDS" not in payload["full_live_closure_sources"]
    u2_flow = next(flow for flow in payload["flows"] if flow["source_tracker"] == "U2")
    assert u2_flow["target_tracker"] == "MTEAM"
    assert u2_flow["full_live_closure"] is True


def test_help_surfaces_short_live_closure_commands() -> None:
    help_text = build_parser().format_help()

    assert "Common live closure commands:" in help_text
    assert "ptcli retorrent --from U2 --source-id 60635 --to MTEAM --execute" in help_text
    assert "ptcli pipeline --from U2 --source-id 60635 --to MTEAM --save-path /downloads" in help_text
    assert "ptcli doctor --from U2 --source-id 60635 --to MTEAM" in help_text
    assert "--target-torrent-file ./tmp/exported/mteam.torrent" in help_text


def test_help_points_to_capability_matrix() -> None:
    parser = build_parser()
    subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction)).choices

    source_info_help = subparsers["source-info"].format_help()
    assert "ptcli sites" in source_info_help
    assert "--json" in source_info_help
    assert "source_download capability" in subparsers["source-download"].format_help()
    assert "enabled ptcli retorrent flow" in subparsers["flow-check"].format_help()
    assert "full live closure sources" in subparsers["retorrent"].format_help()
    assert "full live closure pipeline" in subparsers["retorrent"].format_help()


def test_pipeline_help_describes_live_closure_defaults() -> None:
    parser = build_parser()
    pipeline_parser = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction)).choices["pipeline"]
    help_text = pipeline_parser.format_help()

    assert "automatically fills in the live closure defaults" in help_text
    assert "download/inject/wait" in help_text
    assert "uploaded MTEAM torrent download/inject/wait" in help_text


def test_retorrent_plan_requires_supported_trackers() -> None:
    parser = build_parser()
    args = parser.parse_args(["retorrent", "--from", "PTP", "--source-id", "123", "--to", "MTEAM", "--dry-run"])

    try:
        build_plan(args)
    except ValueError as exc:
        assert "Unsupported tracker" in str(exc)
    else:
        raise AssertionError("unsupported tracker should fail")


def test_non_dry_run_requires_rules_ack() -> None:
    parser = build_parser()
    args = parser.parse_args(["retorrent", "--from", "MTEAM", "--source-id", "123", "--to", "TJUPT"])

    try:
        build_plan(args)
    except ValueError as exc:
        assert "--accept-rules" in str(exc)
    else:
        raise AssertionError("non-dry-run without rules ack should fail")


def test_retorrent_plan_json_exit_ok(capsys) -> None:
    code = main(["retorrent", "--from", "MTEAM", "--source-id", "123", "--to", "TJUPT,CHD", "--dry-run", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"source_tracker": "MTEAM"' in out
    assert '"TJUPT"' in out
    assert '"CHD"' in out
    assert '"rule_profiles"' in out
    assert '"rule_check"' in out
    assert '"rule_obligations"' in out
    assert '"flow_profiles"' in out
    assert '"capability"' in out
    assert '"blockers"' in out


def test_retorrent_plan_marks_non_reference_flow_blocker(capsys) -> None:
    code = main(["retorrent", "--from", "MTEAM", "--source-id", "123", "--to", "TJUPT", "--dry-run", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "not enabled for ptcli retorrent flow execution" in out
    assert payload["plan"]["capability"]["full_live_closure"] is False


def test_retorrent_plan_accepts_reference_flow_without_reference_blocker(capsys) -> None:
    source_url = "https://u2.dmhy.org/details.php?id=123"
    code = main(["retorrent", "--from", "U2", "--source-id", source_url, "--to", "MTEAM", "--dry-run", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["plan"]["requested_source_id"] == source_url
    assert payload["plan"]["source_torrent_id"] == "123"
    assert "not enabled for ptcli retorrent flow execution" not in out
    assert payload["plan"]["capability"]["full_live_closure"] is True
    assert '"source_torrent_id": "123"' in out
    assert '"source_kind": "nexusphp"' in out
    assert '"target_kind": "mteam_api"' in out
    assert '"stage": "source-info"' in out
    assert "source-download" in out
    assert '"stage": "resume-source-torrent"' in out
    assert "--source-torrent-file" in out
    assert '"stage": "resume-target-package"' in out
    assert "--package-dir" in out
    assert '"stage": "resume-uploaded-torrent"' in out
    assert "--uploaded-torrent-file" in out
    assert '"stage": "resume-uploaded-torrent-download"' in out
    assert "--uploaded-torrent-id <id>" in out
    assert "--download-uploaded-torrent" in out
    assert '"stage": "doctor-live"' in out
    assert "--target-execute --confirm-upload" in out
    assert "--package-dir ./tmp/target/U2-123-to-MTEAM" in out
    assert "--target-torrent-file ./tmp/exported/mteam.torrent" in out
    assert '"stage": "retorrent-execute"' in out
    assert "--execute --accept-rules --confirm-upload" in out
    assert "--uploaded-qbit-category MTEAM" in out
    assert "--write-summary" in out


def test_retorrent_plan_includes_rule_check_obligations() -> None:
    parser = build_parser()
    args = parser.parse_args(["retorrent", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--dry-run"])

    plan = build_plan(args)

    assert plan.rule_check["ready"] is False
    assert plan.rule_check["source_tracker"] == "U2"
    assert plan.rule_check["target_trackers"] == ["MTEAM"]
    assert [obligation["action"] for obligation in plan.rule_check["rule_obligations"]] == ["download_and_retorrent", "upload_and_seed"]
    assert any(check["name"] == "site_rule_obligations_acknowledged" and check["ok"] is False for check in plan.rule_check["checks"])


def test_retorrent_plan_marks_rule_check_ready_with_ack() -> None:
    parser = build_parser()
    args = parser.parse_args(["retorrent", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--dry-run", "--accept-rules"])

    plan = build_plan(args)

    assert plan.rule_check["ready"] is True
    assert all(obligation["acknowledged"] is True for obligation in plan.rule_check["rule_obligations"])


def test_retorrent_plan_resume_commands_keep_live_closure_flags() -> None:
    commands = {command["stage"]: command["command"] for command in ptcli_cli.build_plan_commands("U2", "60635", ["MTEAM"], "/downloads/Example")}

    resume_target = commands["resume-target-package"]
    assert "--target-execute --confirm-upload" in resume_target
    assert "--download-uploaded-torrent" in resume_target
    assert "--inject-uploaded-torrent" in resume_target
    assert '--uploaded-save-path "/downloads/Example"' in resume_target
    assert "--wait-uploaded-complete" in resume_target
    assert "--write-summary" in resume_target
    assert "--uploaded-qbit-category MTEAM" in resume_target
    assert "--uploaded-qbit-tags retorrent" in resume_target

    doctor_live = commands["doctor-live"]
    assert "--download-uploaded-torrent" in doctor_live
    assert "--inject-uploaded-torrent" in doctor_live
    assert "--wait-uploaded-complete" in doctor_live
    assert "--connect-qbit" in doctor_live
    assert "--probe-source" in doctor_live
    assert "--probe-target" in doctor_live

    resume_uploaded = commands["resume-uploaded-torrent"]
    assert "--uploaded-torrent-file ./tmp/uploaded/MTEAM-<id>.torrent" in resume_uploaded
    assert "--inject-uploaded-torrent" in resume_uploaded
    assert "--uploaded-save-path" not in resume_uploaded
    assert "--wait-uploaded-complete" in resume_uploaded
    assert "--uploaded-qbit-category MTEAM" in resume_uploaded
    assert "--uploaded-qbit-tags retorrent" in resume_uploaded

    resume_uploaded_download = commands["resume-uploaded-torrent-download"]
    assert "--uploaded-torrent-id <id>" in resume_uploaded_download
    assert "--download-uploaded-torrent" in resume_uploaded_download
    assert "--inject-uploaded-torrent" in resume_uploaded_download
    assert "--wait-uploaded-complete" in resume_uploaded_download
    assert "--uploaded-qbit-category MTEAM" in resume_uploaded_download
    assert "--uploaded-qbit-tags retorrent" in resume_uploaded_download


def test_retorrent_execute_blocked_returns_nonzero(capsys, tmp_path) -> None:
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")

    code = main(
        [
            "retorrent",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--execute",
            "--accept-rules",
            "--save-path",
            "/downloads",
            "--target-torrent-file",
            str(torrent_file),
            "--json",
        ]
    )

    assert code == 1
    out = capsys.readouterr().out
    assert '"status": "blocked"' in out
    assert "--confirm-upload" in out


@pytest.mark.asyncio
async def test_retorrent_execute_runs_reference_pipeline(monkeypatch, tmp_path) -> None:
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")
    captured_args = {}

    async def fake_pipeline_payload(args):
        captured_args["args"] = args
        rule_obligations = {"ready": True, "count": 2, "missing": []}
        return {
            "ready": True,
            "closure": {"complete": True, "blockers": [], "source": {"complete": True}, "target": {"uploaded": True, "injected": True}},
            "evidence": {
                "complete": True,
                "source": {
                    "mode": "downloaded",
                    "torrent_hash": "a" * 40,
                    "source_torrent_path": "/tmp/U2-60635.torrent",
                    "source_save_path": "/downloads",
                    "source_qbit_category": "SOURCE",
                    "source_qbit_tags": "source-tag",
                    "source_paused": True,
                    "hash_consistent": True,
                    "source_wait_evidence": True,
                    "content_path": "/downloads/Name",
                },
                "target": {
                    "ready": True,
                    "uploaded_torrent_id": "999",
                    "uploaded_torrent_hash": "b" * 40,
                    "uploaded_torrent_path": "/tmp/MTEAM-999.torrent",
                    "uploaded_save_path": "/downloads/Name",
                    "uploaded_qbit_category": "MTEAM",
                    "uploaded_qbit_tags": "retorrent",
                    "uploaded_paused": True,
                    "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                    "hash_consistent": True,
                    "duplicate_clean": True,
                    "rule_obligations": rule_obligations,
                    "uploaded_wait_evidence": True,
                },
            },
            "summary": {"ready": True, "complete": True, "status": "complete"},
            "summary_file": str(tmp_path / "summary" / "ptcli-run-summary.json"),
            "output_options": {
                "source_output_dir": "./tmp/source",
                "target_output_dir": "./tmp/target",
                "target_torrent_output_dir": "./tmp/exported",
                "uploaded_output_dir": str(tmp_path / "uploaded"),
                "summary_output_dir": str(tmp_path / "summary"),
            },
            "artifacts": {},
            "resume_commands": [{"stage": "resume-uploaded-torrent-download", "command": "python3 ptcli.py target-upload --uploaded-torrent-id 999"}],
            "resume_state": {
                "complete": True,
                "resume_available": True,
                "next_stage": None,
                "next_command": None,
                "available_stages": ["resume-uploaded-torrent-download"],
            },
            "next_actions": ["Retorrent closure is complete; verify the target tracker page and qBittorrent seeding state."],
            "stages": [{"stage": "target-upload", "ok": True}],
        }

    monkeypatch.setattr(ptcli_cli, "pipeline_payload", fake_pipeline_payload)
    parser = build_parser()
    args = parser.parse_args(
        [
            "retorrent",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--config",
            "data/config.py",
            "--base-dir",
            str(tmp_path),
            "--execute",
            "--accept-rules",
            "--confirm-upload",
            "--save-path",
            "/downloads",
            "--target-torrent-file",
            str(torrent_file),
            "--uploaded-output-dir",
            str(tmp_path / "uploaded"),
            "--inject-uploaded-torrent",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--write-summary",
            "--summary-output-dir",
            str(tmp_path / "summary"),
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    pipeline_args = captured_args["args"]
    assert payload["status"] == "complete"
    assert payload["complete"] is True
    assert payload["blockers"] == []
    assert payload["config"] == "data/config.py"
    assert payload["base_dir"] == str(tmp_path)
    assert payload["client"] == "default"
    assert payload["output_options"]["uploaded_output_dir"] == str(tmp_path / "uploaded")
    assert payload["output_options"]["summary_output_dir"] == str(tmp_path / "summary")
    assert payload["qbit_wait_diagnostics"] == {}
    assert payload["qbit_wait_mismatch"] is False
    assert payload["qbit_wait_mismatches"] == []
    assert payload["next_actions"] == ["Retorrent closure is complete; verify the target tracker page and qBittorrent seeding state."]
    assert payload["ready"] is True
    assert payload["closure"]["source"]["complete"] is True
    assert payload["closure"]["target"]["injected"] is True
    assert payload["evidence"]["source"]["mode"] == "downloaded"
    assert payload["evidence"]["target"]["uploaded_torrent_hash"] == "b" * 40
    assert payload["summary"]["status"] == "complete"
    assert payload["summary_file"].endswith("ptcli-run-summary.json")
    assert payload["artifacts"] == {
        "source_torrent_hash": "a" * 40,
        "source_torrent_file": "/tmp/U2-60635.torrent",
        "source_save_path": "/downloads",
        "source_qbit_category": "SOURCE",
        "source_qbit_tags": "source-tag",
        "source_paused": True,
        "source_hash_consistent": True,
        "source_wait_evidence": True,
        "uploaded_torrent_id": "999",
        "uploaded_torrent_hash": "b" * 40,
        "uploaded_torrent_path": "/tmp/MTEAM-999.torrent",
        "uploaded_torrent_file": "/tmp/MTEAM-999.torrent",
        "uploaded_save_path": "/downloads/Name",
        "uploaded_qbit_category": "MTEAM",
        "uploaded_qbit_tags": "retorrent",
        "uploaded_paused": True,
        "uploaded_wait_evidence": True,
        "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
        "target_hash_consistent": True,
        "target_duplicate_clean": True,
        "target_rule_obligations": {"ready": True, "count": 2, "missing": []},
    }
    assert payload["resume_commands"] == [{"stage": "resume-uploaded-torrent-download", "command": "python3 ptcli.py target-upload --uploaded-torrent-id 999"}]
    assert payload["resume_state"]["complete"] is True
    assert payload["resume_state"]["pipeline_complete"] is True
    assert payload["resume_state"]["next_stage"] is None
    assert payload["resume_state"]["next_command"] is None
    assert payload["resume_state"]["artifacts"]["source_torrent_file"] is True
    assert payload["resume_state"]["artifacts"]["source_torrent_hash"] is True
    assert payload["resume_state"]["artifacts"]["source_save_path"] is True
    assert payload["resume_state"]["artifacts"]["source_qbit_category"] is True
    assert payload["resume_state"]["artifacts"]["source_qbit_tags"] is True
    assert payload["resume_state"]["artifacts"]["source_paused"] is True
    assert payload["resume_state"]["artifacts"]["source_hash_consistent"] is True
    assert payload["resume_state"]["artifacts"]["source_wait_evidence"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_torrent_id"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_torrent_file"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_torrent_hash"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_save_path"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_qbit_category"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_qbit_tags"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_paused"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_wait_evidence"] is True
    assert payload["resume_state"]["artifacts"]["target_hash_consistent"] is True
    assert payload["resume_state"]["artifacts"]["target_duplicate_clean"] is True
    assert payload["resume_state"]["artifacts"]["target_rule_obligations"] is True
    assert pipeline_args.download_source is True
    assert pipeline_args.inject_source is True
    assert pipeline_args.wait_complete is True
    assert pipeline_args.check_dupes is True
    assert pipeline_args.prepare_target is True
    assert pipeline_args.upload_target is True
    assert pipeline_args.target_execute is True
    assert pipeline_args.download_uploaded_torrent is True
    assert pipeline_args.inject_uploaded_torrent is True
    assert pipeline_args.wait_uploaded_complete is True
    assert pipeline_args.check_runtime is True
    assert pipeline_args.write_summary is True
    assert pipeline_args.config == "data/config.py"
    assert pipeline_args.base_dir == str(tmp_path)
    assert pipeline_args.client == "default"
    assert pipeline_args.summary_output_dir == str(tmp_path / "summary")
    assert pipeline_args.save_path == "/downloads"
    assert pipeline_args.target_torrent_file == str(torrent_file)
    assert pipeline_args.sanitize_target_torrent is True


@pytest.mark.asyncio
async def test_retorrent_execute_enables_uploaded_torrent_followup_by_default(monkeypatch, tmp_path) -> None:
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")
    captured_args = {}

    async def fake_pipeline_payload(args):
        captured_args["args"] = args
        return {
            "ready": True,
            "closure": {"complete": True, "blockers": [], "target": {"downloaded": True, "injected": True}},
            "stages": [{"stage": "target-upload", "ok": True}],
        }

    monkeypatch.setattr(ptcli_cli, "pipeline_payload", fake_pipeline_payload)
    parser = build_parser()
    args = parser.parse_args(
        [
            "retorrent",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--execute",
            "--accept-rules",
            "--confirm-upload",
            "--path",
            "/downloads/Name",
            "--target-torrent-file",
            str(torrent_file),
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    pipeline_args = captured_args["args"]
    assert payload["ready"] is True
    assert pipeline_args.download_uploaded_torrent is True
    assert pipeline_args.inject_uploaded_torrent is True
    assert pipeline_args.wait_uploaded_complete is True
    assert pipeline_args.uploaded_save_path is None
    assert pipeline_args.content_path == "/downloads/Name"
    assert pipeline_args.write_summary is True


@pytest.mark.asyncio
async def test_retorrent_execute_reuses_source_torrent_file(monkeypatch, tmp_path) -> None:
    source_torrent = tmp_path / "U2-60635.torrent"
    source_torrent.write_bytes(b"d4:infod")
    captured_args = {}

    async def fake_pipeline_payload(args):
        captured_args["args"] = args
        return {
            "ready": True,
            "closure": {"complete": True, "blockers": []},
            "stages": [{"stage": "target-upload", "ok": True}],
        }

    monkeypatch.setattr(ptcli_cli, "pipeline_payload", fake_pipeline_payload)
    parser = build_parser()
    args = parser.parse_args(
        [
            "retorrent",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--execute",
            "--accept-rules",
            "--confirm-upload",
            "--source-torrent-file",
            str(source_torrent),
            "--save-path",
            "/downloads",
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    pipeline_args = captured_args["args"]
    assert payload["ready"] is True
    assert pipeline_args.download_source is False
    assert pipeline_args.source_torrent_file == str(source_torrent)
    assert pipeline_args.inject_source is True
    assert pipeline_args.wait_complete is True


@pytest.mark.asyncio
async def test_retorrent_execute_reuses_uploaded_torrent_id(monkeypatch) -> None:
    captured_args = {}

    async def fake_pipeline_payload(args):
        captured_args["args"] = args
        return {
            "ready": True,
            "closure": {"complete": True, "blockers": [], "target": {"downloaded": True, "injected": True}},
            "artifacts": {"uploaded_torrent_id": "999"},
            "stages": [{"stage": "target-upload", "ok": True}],
        }

    monkeypatch.setattr(ptcli_cli, "pipeline_payload", fake_pipeline_payload)
    parser = build_parser()
    args = parser.parse_args(
        [
            "retorrent",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--execute",
            "--accept-rules",
            "--confirm-upload",
            "--path",
            "/downloads/Name",
            "--uploaded-torrent-id",
            "999",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    pipeline_args = captured_args["args"]
    assert payload["ready"] is True
    assert pipeline_args.uploaded_torrent_id == "999"
    assert pipeline_args.download_uploaded_torrent is True
    assert pipeline_args.inject_uploaded_torrent is True
    assert pipeline_args.wait_uploaded_complete is True
    assert pipeline_args.uploaded_qbit_category == "MTEAM"
    assert pipeline_args.uploaded_qbit_tags == "retorrent"


@pytest.mark.asyncio
async def test_retorrent_execute_defaults_to_export_target_torrent(monkeypatch) -> None:
    captured_args = {}

    async def fake_pipeline_payload(args):
        captured_args["args"] = args
        return {"ready": True, "closure": {"complete": True, "blockers": []}, "evidence": {"complete": True}, "stages": []}

    monkeypatch.setattr(ptcli_cli, "pipeline_payload", fake_pipeline_payload)
    parser = build_parser()
    args = parser.parse_args(
        [
            "retorrent",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--execute",
            "--accept-rules",
            "--confirm-upload",
            "--path",
            "/downloads/Name",
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    assert payload["ready"] is True
    assert captured_args["args"].export_target_torrent is True
    assert captured_args["args"].target_torrent_file is None


@pytest.mark.asyncio
async def test_retorrent_execute_can_disable_target_torrent_sanitizing(monkeypatch, tmp_path) -> None:
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")
    captured_args = {}

    async def fake_pipeline_payload(args):
        captured_args["args"] = args
        return {"ready": True, "closure": {"complete": True, "blockers": []}, "stages": [{"stage": "target-upload", "ok": True}]}

    monkeypatch.setattr(ptcli_cli, "pipeline_payload", fake_pipeline_payload)
    parser = build_parser()
    args = parser.parse_args(
        [
            "retorrent",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--execute",
            "--accept-rules",
            "--confirm-upload",
            "--save-path",
            "/downloads",
            "--target-torrent-file",
            str(torrent_file),
            "--no-sanitize-target-torrent",
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    assert payload["ready"] is True
    assert captured_args["args"].sanitize_target_torrent is False


@pytest.mark.asyncio
async def test_retorrent_execute_blocks_when_pipeline_closure_is_incomplete(monkeypatch, tmp_path) -> None:
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")

    async def fake_pipeline_payload(_args):
        return {
            "ready": False,
            "closure": {"complete": False, "blockers": ["target.injected"]},
            "next_actions": ["Inject the generated target torrent into qBittorrent with --inject-uploaded-torrent and a valid uploaded save path."],
            "resume_commands": [{"stage": "resume-uploaded-torrent", "command": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"}],
            "resume_state": {
                "complete": False,
                "resume_available": True,
                "next_stage": "resume-uploaded-torrent",
                "next_command": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent",
                "available_stages": ["resume-uploaded-torrent"],
            },
            "stages": [{"stage": "target-upload", "ok": False}],
        }

    monkeypatch.setattr(ptcli_cli, "pipeline_payload", fake_pipeline_payload)
    parser = build_parser()
    args = parser.parse_args(
        [
            "retorrent",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--execute",
            "--accept-rules",
            "--confirm-upload",
            "--path",
            "/downloads/Name",
            "--target-torrent-file",
            str(torrent_file),
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    assert payload["status"] == "blocked"
    assert payload["complete"] is False
    assert payload["ready"] is False
    assert payload["blockers"] == ["target.injected", "pipeline did not report ready."]
    assert "Inject the generated target torrent into qBittorrent with --inject-uploaded-torrent and a valid uploaded save path." in payload["next_actions"]
    assert "Inspect the pipeline blockers and resume from the first incomplete stage." in payload["next_actions"]
    assert payload["resume_state"]["complete"] is False
    assert payload["resume_state"]["pipeline_complete"] is False
    assert payload["resume_state"]["next_stage"] == "resume-uploaded-torrent"
    assert payload["resume_state"]["next_command"] == "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"
    assert payload["resume_state"]["blockers"] == ["target.injected", "pipeline did not report ready."]


def test_retorrent_execute_blockers_promote_pipeline_stage_details() -> None:
    pipeline_result = {
        "status": "blocked",
        "blockers": ["target-upload: uploaded_wait: torrent hash missing"],
        "summary": {
            "blockers": [
                "target-upload: uploaded_wait: torrent hash missing",
                "target-upload: Target upload stage did not complete every requested upload follow-up.",
            ]
        },
    }
    blockers = ptcli_cli._retorrent_execute_blockers(pipeline_result, {"complete": False, "blockers": ["target.seeding"]}, False)

    assert blockers == [
        "target.seeding",
        "target-upload: uploaded_wait: torrent hash missing",
        "target-upload: Target upload stage did not complete every requested upload follow-up.",
        "pipeline did not report ready.",
        "pipeline status is blocked.",
    ]


def test_retorrent_execute_blockers_require_uploaded_wait_evidence() -> None:
    pipeline_result = {"status": "ok", "ready": True, "summary": {"blockers": []}}
    closure = {"complete": True, "blockers": []}

    blockers = ptcli_cli._retorrent_execute_blockers(
        pipeline_result,
        closure,
        True,
        {"source_wait_evidence": True, "uploaded_wait_evidence": False},
    )

    assert blockers == ["target.uploaded_wait_evidence"]


def test_retorrent_execute_blockers_require_source_wait_evidence() -> None:
    pipeline_result = {"status": "ok", "ready": True, "summary": {"blockers": []}}
    closure = {"complete": True, "blockers": []}

    blockers = ptcli_cli._retorrent_execute_blockers(
        pipeline_result,
        closure,
        True,
        {"uploaded_wait_evidence": True},
    )

    assert blockers == ["source.wait_evidence"]


def test_retorrent_execute_next_actions_explain_wait_evidence_blockers() -> None:
    actions = ptcli_cli._retorrent_execute_next_actions(
        {"next_actions": ["Retorrent closure is complete; verify the target tracker page and qBittorrent seeding state."]},
        ["source.wait_evidence", "target.uploaded_wait_evidence"],
    )

    assert any("--wait-complete" in action for action in actions)
    assert any("--wait-uploaded-complete" in action for action in actions)
    assert all(not action.startswith("Retorrent closure is complete;") for action in actions)


def test_retorrent_execute_next_actions_surface_qbit_wait_mismatches() -> None:
    actions = ptcli_cli._retorrent_execute_next_actions(
        {
            "evidence": {
                "source": {
                    "qbit_closure": {
                        "wait": {
                            "complete": False,
                            "completion_verification": {
                                "complete_count": 1,
                                "any_complete": True,
                                "requested_hash_matched": False,
                                "requested_content_path_matched": None,
                                "observed_hashes": ["f" * 40],
                                "observed_content_paths": ["/downloads/Other"],
                            },
                        }
                    }
                },
                "target": {
                    "qbit_closure": {
                        "wait": {
                            "complete": False,
                            "completion_verification": {
                                "complete_count": 1,
                                "any_complete": True,
                                "requested_hash_matched": True,
                                "requested_content_path_matched": False,
                                "observed_hashes": ["a" * 40],
                                "observed_content_paths": ["/downloads/Wrong"],
                            },
                        }
                    }
                },
            }
        },
        ["source.wait_evidence", "target.uploaded_wait_evidence"],
    )

    assert actions[0].startswith("Resolve the source qBittorrent wait mismatch")
    assert actions[1].startswith("Resolve the uploaded qBittorrent wait mismatch")
    assert any("--wait-complete" in action for action in actions)
    assert any("--wait-uploaded-complete" in action for action in actions)


@pytest.mark.asyncio
async def test_retorrent_execute_exposes_qbit_wait_mismatch_diagnostics(monkeypatch, tmp_path) -> None:
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")

    async def fake_pipeline_payload(_args):
        return {
            "ready": False,
            "status": "blocked",
            "closure": {"complete": False, "blockers": ["source.wait_evidence"]},
            "evidence": {
                "source": {
                    "qbit_closure": {
                        "wait": {
                            "complete": False,
                            "completion_verification": {
                                "complete_count": 1,
                                "any_complete": True,
                                "requested_hash_matched": False,
                                "requested_content_path_matched": None,
                                "observed_hashes": ["f" * 40],
                                "observed_content_paths": ["/downloads/Other"],
                                "observed_save_paths": ["/downloads"],
                            },
                            "blockers": ["qBittorrent matched torrents, but none matched requested hash."],
                        }
                    }
                }
            },
            "next_actions": ["Re-run the source qBittorrent completion wait with --wait-complete."],
            "stages": [{"stage": "source-qbit-wait", "ok": False}],
        }

    monkeypatch.setattr(ptcli_cli, "pipeline_payload", fake_pipeline_payload)
    parser = build_parser()
    args = parser.parse_args(
        [
            "retorrent",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--execute",
            "--accept-rules",
            "--confirm-upload",
            "--save-path",
            "/downloads",
            "--target-torrent-file",
            str(torrent_file),
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    assert payload["status"] == "blocked"
    assert payload["qbit_wait_mismatch"] is True
    assert payload["qbit_wait_mismatches"] == ["source.requested_hash"]
    assert payload["qbit_wait_diagnostics"]["source"]["request_mismatch"] is True
    assert payload["qbit_wait_diagnostics"]["source"]["observed_hashes"] == ["f" * 40]
    assert payload["next_actions"][0].startswith("Resolve the source qBittorrent wait mismatch")


@pytest.mark.asyncio
async def test_retorrent_execute_blocks_without_live_confirmation(tmp_path) -> None:
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")
    parser = build_parser()
    args = parser.parse_args(
        [
            "retorrent",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--execute",
            "--accept-rules",
            "--save-path",
            "/downloads",
            "--target-torrent-file",
            str(torrent_file),
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    assert payload["status"] == "blocked"
    assert any("--confirm-upload" in blocker for blocker in payload["blockers"])


@pytest.mark.asyncio
async def test_retorrent_execute_can_use_target_torrent_export(monkeypatch, tmp_path) -> None:
    captured_args = {}

    async def fake_pipeline_payload(args):
        captured_args["args"] = args
        return {"ready": True, "closure": {"complete": True, "blockers": []}, "target_torrent_file": str(tmp_path / "exported.torrent"), "stages": []}

    monkeypatch.setattr(ptcli_cli, "pipeline_payload", fake_pipeline_payload)
    parser = build_parser()
    args = parser.parse_args(
        [
            "retorrent",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--execute",
            "--accept-rules",
            "--confirm-upload",
            "--save-path",
            "/downloads",
            "--export-target-torrent",
            "--target-torrent-output-dir",
            str(tmp_path / "exported"),
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    assert payload["ready"] is True
    assert captured_args["args"].export_target_torrent is True
    assert captured_args["args"].target_torrent_file is None
    assert captured_args["args"].target_torrent_output_dir == str(tmp_path / "exported")


def test_rules_command_outputs_profile_json(capsys) -> None:
    code = main(["rules", "--trackers", "MTEAM,TJUPT", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"tracker": "MTEAM"' in out
    assert '"tracker": "TJUPT"' in out
    assert '"review_required": true' in out
    assert '"automation_status": "enabled"' in out


def test_rule_check_command_requires_ack_for_ready(capsys) -> None:
    code = main(["rule-check", "--from", "U2", "--to", "MTEAM", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"ready": false' in out
    assert '"rules_acknowledged"' in out
    assert '"reference_flow_enabled"' in out
    assert '"rule_obligations"' in out
    assert '"action": "download_and_retorrent"' in out
    assert '"action": "upload_and_seed"' in out
    assert '"site_rule_obligations_acknowledged"' in out
    assert '"site_specific_rules_encoded": false' in out
    payload = json.loads(out)
    assert payload["manual_review"]["required"] is True
    assert payload["manual_review"]["acknowledged"] is False
    assert payload["manual_review"]["obligation_count"] == 2
    assert payload["manual_review"]["rules_urls"] == ["https://kp.m-team.cc/rules", "https://u2.dmhy.org/rules.php"]
    assert len(payload["manual_review"]["required_confirmations"]) == 2
    assert all(item["required_confirmations"] for item in payload["manual_review"]["required_confirmations"])


def test_rule_check_blocks_unsupported_tracker_as_scope_result(capsys) -> None:
    code = main(["rule-check", "--from", "PTP", "--to", "MTEAM", "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["command"] == "rule-check"
    assert payload["source_tracker"] == "PTP"
    assert payload["target_trackers"] == ["MTEAM"]
    assert payload["blockers"] == ["Unsupported tracker(s) for focused CLI scope: PTP"]


def test_rule_check_command_ready_for_reference_flow_with_ack(capsys) -> None:
    code = main(["rule-check", "--from", "CHD", "--to", "MTEAM", "--accept-rules", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"ready": true' in out
    assert '"source_tracker": "CHD"' in out
    assert '"target_trackers": [' in out
    payload = json.loads(out)
    assert [(obligation["tracker"], obligation["role"], obligation["action"]) for obligation in payload["rule_obligations"]] == [
        ("CHD", "source", "download_and_retorrent"),
        ("MTEAM", "target", "upload_and_seed"),
    ]
    assert all(obligation["acknowledged"] is True for obligation in payload["rule_obligations"])
    assert all(obligation["rules_url"] for obligation in payload["rule_obligations"])
    assert all(len(obligation["review_fingerprint"]) == 64 for obligation in payload["rule_obligations"])
    assert all(obligation["acknowledgement_evidence"]["review_fingerprint"] == obligation["review_fingerprint"] for obligation in payload["rule_obligations"])
    assert all(obligation["review_scope"]["required_confirmations"] for obligation in payload["rule_obligations"])
    assert all("adapter_preflight_required" in obligation["review_scope"]["encoded_checks"] for obligation in payload["rule_obligations"])
    assert payload["manual_review"]["required"] is True
    assert payload["manual_review"]["acknowledged"] is True
    assert payload["manual_review"]["source_tracker"] == "CHD"
    assert payload["manual_review"]["target_trackers"] == ["MTEAM"]
    assert payload["manual_review"]["obligation_count"] == 2
    assert payload["manual_review"]["acknowledged_count"] == 2
    assert payload["manual_review"]["rules_urls"] == ["https://kp.m-team.cc/rules", "https://ptchdbits.co/rules.php"]
    assert payload["manual_review"]["required_confirmations"] == [
        {
            "tracker": obligation["tracker"],
            "role": obligation["role"],
            "action": obligation["action"],
            "rules_url": obligation["rules_url"],
            "review_fingerprint": obligation["review_fingerprint"],
            "required_confirmations": obligation["review_scope"]["required_confirmations"],
        }
        for obligation in payload["rule_obligations"]
    ]
    assert payload["manual_review"]["acknowledgement_evidence"] == [obligation["acknowledgement_evidence"] for obligation in payload["rule_obligations"]]
    assert payload["manual_review"]["review_fingerprints"] == [obligation["review_fingerprint"] for obligation in payload["rule_obligations"]]
    assert payload["manual_review"]["site_specific_rules_encoded"] is False
    assert payload["automation_scope"] == {
        "site_specific_rules_encoded": False,
        "concrete_policy_checks": "tracker_adapters",
        "reference_flow_enabled": True,
        "source_adapter_enabled": True,
        "target_adapter_enabled": True,
    }


def test_rule_check_accepts_enabled_chinese_nexus_source_with_ack(capsys) -> None:
    code = main(["rule-check", "--from", "PTER", "--to", "MTEAM", "--accept-rules", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["source_tracker"] == "PTER"
    assert payload["automation_scope"]["reference_flow_enabled"] is True
    assert payload["automation_scope"]["source_adapter_enabled"] is True
    assert payload["automation_scope"]["target_adapter_enabled"] is True
    assert payload["manual_review"]["site_specific_rules_encoded"] is False


def test_source_download_requires_target_rule_context(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {})

    source_url = "https://u2.dmhy.org/details.php?id=60635&hit=1"
    code = main(["source-download", "--tracker", "U2", "--source-id", source_url, "--output-dir", "./tmp/source", "--accept-rules", "--json"])

    assert code == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["requested_source_id"] == source_url
    assert payload["input_source_id"] == source_url
    assert payload["source_torrent_id"] == "60635"
    assert '"status": "blocked"' in out
    assert "--to is required" in out


def test_source_info_exposes_normalized_source_id(monkeypatch, capsys) -> None:
    source_url = "https://u2.dmhy.org/details.php?id=60635&hit=1"

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (_config, base_dir)
        return source_info_from_tuple(tracker, extract_torrent_id(source_id), (1, 2, "Name", "a" * 40, "desc"), {})

    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {})
    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)

    code = main(["source-info", "--tracker", "U2", "--source-id", source_url, "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tracker"] == "U2"
    assert payload["requested_source_id"] == source_url
    assert payload["input_source_id"] == source_url
    assert payload["source_torrent_id"] == "60635"
    assert payload["source"]["torrent_id"] == "60635"


def test_source_info_uses_enabled_chinese_source_adapter(monkeypatch, capsys) -> None:
    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (_config, base_dir)
        return source_info_from_tuple(tracker, extract_torrent_id(source_id), (7, 8, "PTER.Name.2024", "b" * 40, "desc"), {})

    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {})
    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)

    code = main(["source-info", "--tracker", "PTER", "--source-id", "123", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tracker"] == "PTER"
    assert payload["source_torrent_id"] == "123"
    assert payload["source"]["name"] == "PTER.Name.2024"


def test_source_module_registers_enabled_chinese_source_adapters() -> None:
    assert set(ptcli_source.SOURCE_TRACKER_CLASSES) == set()
    assert set(ptcli_source.MTEAM_API_TRACKERS) == {"MTEAM"}
    for tracker in ["AUDIENCES", "CHD", "HDSKY", "HHAN", "PTER", "TJUPT", "U2"]:
        assert tracker in ptcli_source.NEXUS_DOWNLOAD_BASE_URLS
    for tracker in ["AUDIENCES", "CHD", "HDS", "HDSKY", "HHAN", "OB", "PTER", "TJUPT", "TTG", "U2"]:
        assert tracker in ptcli_source.GENERIC_DETAILS_BASE_URLS
    assert set(ptcli_source.DIRECT_DOWNLOAD_TRACKER_CLASSES) == set()
    assert set(ptcli_source.TTG_DOWNLOAD_BASE_URLS) == {"TTG"}


@pytest.mark.asyncio
async def test_ptcli_cookie_loader_reads_netscape_and_header_cookies(tmp_path) -> None:
    cookiefile = tmp_path / "cookies.txt"
    cookiefile.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".example.test\tTRUE\t/\tFALSE\t1893456000\tuid\t123",
                "#HttpOnly_.example.test\tTRUE\t/\tFALSE\t1893456000\tpass\tsecret",
                "cf_clearance=token; locale=zh_CN;",
            ]
        ),
        encoding="utf-8",
    )

    cookies = await ptcli_source._load_cookie_file(cookiefile)

    assert cookies == {
        "uid": "123",
        "pass": "secret",
        "cf_clearance": "token",
        "locale": "zh_CN",
    }


@pytest.mark.asyncio
async def test_generic_source_info_parses_hds_details(monkeypatch, tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "HDS.txt").write_text("uid=1; pass=secret;", encoding="utf-8")
    html = """
    <html>
      <head><title>Example.Movie.2024.1080p - HD-Space</title></head>
      <body>
        <h1>Example.Movie.2024.1080p.BluRay-GROUP</h1>
        <a href="https://www.imdb.com/title/tt1234567/">IMDb</a>
        <a href="https://www.themoviedb.org/movie/76543">TMDb</a>
        <a href="https://movie.douban.com/subject/1291546/">Douban</a>
        <div class="torrent-description">Info hash: ABCDEF1234567890ABCDEF1234567890ABCDEF12</div>
      </body>
    </html>
    """

    class FakeResponse:
        text = html

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            assert kwargs["cookies"] == {"uid": "1", "pass": "secret"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url):
            assert url == "https://hd-space.org/index.php?page=torrent-details&id=456"
            return FakeResponse()

    monkeypatch.setattr(ptcli_source.httpx, "AsyncClient", FakeClient)

    info = await ptcli_source.fetch_source_info({}, "HDS", "456", base_dir=str(tmp_path))

    assert info.tracker == "HDS"
    assert info.torrent_id == "456"
    assert info.imdb_id == 1234567
    assert info.tmdb_id == 76543
    assert info.name == "Example.Movie.2024.1080p.BluRay-GROUP"
    assert info.torrenthash == "abcdef1234567890abcdef1234567890abcdef12"
    assert info.douban_id == "1291546"
    assert info.douban_url == "https://movie.douban.com/subject/1291546/"
    assert info.description_length > 0


@pytest.mark.asyncio
async def test_nexus_source_download_uses_ptcli_cookie_loader(monkeypatch, tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text(".u2.dmhy.org\tTRUE\t/\tFALSE\t1893456000\tuid\t1\n", encoding="utf-8")

    class FakeResponse:
        content = b"d4:infod"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            assert kwargs["cookies"] == {"uid": "1"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url):
            assert url == "https://u2.dmhy.org/download.php?id=60635&passkey=u2-passkey"
            return FakeResponse()

    monkeypatch.setattr(ptcli_source.httpx, "AsyncClient", FakeClient)

    path = await ptcli_source.download_source_torrent({"TRACKERS": {"U2": {"passkey": "u2-passkey"}}}, "U2", "60635", str(tmp_path / "out"), base_dir=str(tmp_path))

    assert path == tmp_path / "out" / "U2-60635.torrent"
    assert path.read_bytes() == b"d4:infod"


@pytest.mark.asyncio
async def test_reference_source_info_uses_ptcli_generic_details(monkeypatch, tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    html = """
    <html>
      <head><title>Ignored Title</title></head>
      <body>
        <h1>U2.Reference.2024.1080p.BluRay-GROUP</h1>
        <a href="https://www.imdb.com/title/tt7654321/">IMDb</a>
        <a href="https://www.themoviedb.org/tv/98765">TMDb</a>
        <a href="https://movie.douban.com/subject/3541415/">Douban</a>
        <td class="embedded">Torrent hash: 1234567890ABCDEF1234567890ABCDEF12345678</td>
      </body>
    </html>
    """

    class ExplodingTracker:
        def __init__(self, config):
            self.config = config

        async def get_info_from_torrent_id(self, torrent_id, meta=None):
            _ = (torrent_id, meta)
            raise AssertionError("U2 reference metadata should use ptcli generic details parsing")

    class FakeResponse:
        text = html

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url):
            assert url == "https://u2.dmhy.org/details.php?id=60635"
            return FakeResponse()

    monkeypatch.setitem(ptcli_source.SOURCE_TRACKER_CLASSES, "U2", ExplodingTracker)
    monkeypatch.setattr(ptcli_source.httpx, "AsyncClient", FakeClient)

    info = await ptcli_source.fetch_source_info({}, "U2", "60635", base_dir=str(tmp_path))

    assert info.tracker == "U2"
    assert info.torrent_id == "60635"
    assert info.imdb_id == 7654321
    assert info.tmdb_id == 98765
    assert info.name == "U2.Reference.2024.1080p.BluRay-GROUP"
    assert info.torrenthash == "1234567890abcdef1234567890abcdef12345678"
    assert info.douban_id == "3541415"


@pytest.mark.asyncio
async def test_enabled_nexus_source_info_uses_ptcli_generic_details(monkeypatch, tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "PTER.txt").write_text("uid=1;", encoding="utf-8")
    html = """
    <html>
      <head><title>PTER Reference</title></head>
      <body>
        <h1>PTER.Reference.2024.1080p.BluRay-GROUP</h1>
        <a href="https://www.imdb.com/title/tt1234567/">IMDb</a>
        <div class="torrent-description">Torrent hash: 1234567890ABCDEF1234567890ABCDEF12345678</div>
      </body>
    </html>
    """

    class ExplodingTracker:
        def __init__(self, config):
            self.config = config

        async def get_info_from_torrent_id(self, torrent_id, meta=None):
            _ = (torrent_id, meta)
            raise AssertionError("NexusPHP source metadata should use ptcli generic details parsing")

    class FakeResponse:
        text = html

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url):
            assert url == "https://pterclub.com/details.php?id=123"
            return FakeResponse()

    monkeypatch.setitem(ptcli_source.SOURCE_TRACKER_CLASSES, "PTER", ExplodingTracker)
    monkeypatch.setattr(ptcli_source.httpx, "AsyncClient", FakeClient)

    info = await ptcli_source.fetch_source_info({}, "PTER", "123", base_dir=str(tmp_path))

    assert info.tracker == "PTER"
    assert info.name == "PTER.Reference.2024.1080p.BluRay-GROUP"
    assert info.imdb_id == 1234567
    assert info.torrenthash == "1234567890abcdef1234567890abcdef12345678"


@pytest.mark.asyncio
async def test_ttg_source_download_uses_ptcli_native_downloader(monkeypatch, tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "TTG.txt").write_text("uid=1;", encoding="utf-8")

    class FakeResponse:
        content = b"d4:infod"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            assert kwargs["cookies"] == {"uid": "1"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url):
            assert url == "https://totheglory.im/dl/789/ttg-passkey"
            return FakeResponse()

    assert "TTG" not in ptcli_source.DIRECT_DOWNLOAD_TRACKER_CLASSES
    monkeypatch.setattr(ptcli_source.httpx, "AsyncClient", FakeClient)

    path = await ptcli_source.download_source_torrent(
        {"TRACKERS": {"TTG": {"announce_url": "https://totheglory.im/announce/ttg-passkey"}}},
        "TTG",
        "789",
        str(tmp_path / "out"),
        base_dir=str(tmp_path),
    )

    assert path == tmp_path / "out" / "TTG-789.torrent"
    assert path.read_bytes() == b"d4:infod"


def test_source_info_blocks_unsupported_tracker_before_fetch(monkeypatch, capsys) -> None:
    async def fake_fetch_source_info(*_args, **_kwargs):
        raise AssertionError("unsupported tracker must not reach source fetch")

    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: (_ for _ in ()).throw(AssertionError("unsupported tracker must not read config")))
    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)

    code = main(["source-info", "--tracker", "PTP", "--source-id", "123", "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["tracker"] == "PTP"
    assert payload["blockers"] == ["Unsupported tracker(s) for focused CLI scope: PTP"]


def test_source_download_requires_rule_ack(monkeypatch, capsys) -> None:
    async def fake_download_source_torrent(*_args, **_kwargs):
        raise AssertionError("download must not run without rule acknowledgement")

    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {})
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)

    code = main(["source-download", "--tracker", "U2", "--source-id", "60635", "--to", "MTEAM", "--output-dir", "./tmp/source", "--json"])

    assert code == 1
    out = capsys.readouterr().out
    assert '"status": "blocked"' in out
    assert "rules_acknowledged" in out


def test_source_download_blocks_unsupported_tracker_before_config(monkeypatch, capsys) -> None:
    async def fake_download_source_torrent(*_args, **_kwargs):
        raise AssertionError("unsupported tracker must not reach source download")

    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: (_ for _ in ()).throw(AssertionError("unsupported tracker must not read config")))
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)

    code = main(["source-download", "--tracker", "PTP", "--source-id", "123", "--to", "MTEAM", "--output-dir", "/tmp/out", "--accept-rules", "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["command"] == "source-download"
    assert payload["tracker"] == "PTP"
    assert payload["source_tracker"] == "PTP"
    assert payload["source_torrent_id"] == "123"
    assert payload["target_trackers"] == ["MTEAM"]
    assert payload["blockers"] == ["Unsupported tracker(s) for focused CLI scope: PTP"]


def test_source_download_runs_after_rule_gate(monkeypatch, capsys, tmp_path) -> None:
    source_url = "https://u2.dmhy.org/details.php?id=60635&hit=1"

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (_config, base_dir)
        return source_info_from_tuple(tracker, extract_torrent_id(source_id), (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, base_dir)
        assert source_id == source_url
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {})
    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)

    code = main(
        [
            "source-download",
            "--tracker",
            "U2",
            "--source-id",
            source_url,
            "--to",
            "MTEAM",
            "--output-dir",
            "source-out",
            "--accept-rules",
            "--json",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["requested_source_id"] == source_url
    assert payload["input_source_id"] == source_url
    assert payload["source_torrent_id"] == "60635"
    assert payload["source"]["torrent_id"] == "60635"
    assert payload["rule_check"]["ready"] is True
    assert payload["path"] == payload["source_torrent"]["path"]
    assert payload["source_torrent"]["path"].endswith("source-out/U2-60635.torrent")
    assert payload["source_torrent"]["exists"] is True
    assert payload["source_torrent"]["size_bytes"] == len(b"d4:infod")
    assert len(payload["source_torrent"]["sha1"]) == 40
    assert payload["source_torrent_verification"]["expected_hash"] == "a" * 40
    assert payload["source_torrent_verification"]["actual_hash"] is None
    assert payload["source_torrent_verification"]["verified"] is False


def test_source_download_verifies_downloaded_torrent_hash(monkeypatch, capsys, tmp_path) -> None:
    content = tmp_path / "source-content.mkv"
    content.write_bytes(b"content")
    torrent_path = tmp_path / "source-out" / "U2-60635.torrent"
    torrent_path.parent.mkdir(parents=True, exist_ok=True)
    torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
    torrent.generate()
    torrent.write(str(torrent_path), overwrite=True)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (_config, base_dir)
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", torrent.infohash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (_config, tracker, source_id, output_dir, base_dir)
        return torrent_path

    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {})
    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)

    code = main(
        [
            "source-download",
            "--tracker",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--output-dir",
            "source-out",
            "--accept-rules",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["source_torrent"]["torrent_hash"] == torrent.infohash
    assert payload["source_torrent_verification"] == {
        "verified": True,
        "expected_hash": torrent.infohash,
        "actual_hash": torrent.infohash,
        "message": "Source torrent hash matched source metadata.",
    }


def test_source_download_blocks_downloaded_torrent_hash_mismatch(monkeypatch, capsys, tmp_path) -> None:
    content = tmp_path / "source-content.mkv"
    content.write_bytes(b"content")
    torrent_path = tmp_path / "source-out" / "U2-60635.torrent"
    torrent_path.parent.mkdir(parents=True, exist_ok=True)
    torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
    torrent.generate()
    torrent.write(str(torrent_path), overwrite=True)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (_config, base_dir)
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (_config, tracker, source_id, output_dir, base_dir)
        return torrent_path

    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {})
    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)

    code = main(
        [
            "source-download",
            "--tracker",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--output-dir",
            "source-out",
            "--accept-rules",
            "--json",
        ]
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["source_torrent_verification"]["verified"] is False
    assert payload["source_torrent_verification"]["expected_hash"] == "a" * 40
    assert payload["source_torrent_verification"]["actual_hash"] == torrent.infohash
    assert payload["blockers"] == [f"source-torrent-verify: source torrent hash mismatch: expected {'a' * 40}, got {torrent.infohash}"]


def test_json_capture_moves_stdout_to_logs() -> None:
    def noisy_payload():
        print("noisy tracker log")
        return {"status": "ok"}

    payload = _with_captured_stdout(noisy_payload, json_output=True)

    assert payload == {"status": "ok", "logs": ["noisy tracker log"]}


def test_pipeline_exit_code_keeps_preview_zero_when_not_ready() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--json"])

    assert ptcli_cli._pipeline_exit_code(args, {"status": "ok", "ready": False}) == 0


def test_pipeline_exit_code_returns_nonzero_for_failed_action() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--upload-target", "--json"])

    assert ptcli_cli._pipeline_exit_code(args, {"status": "ok", "ready": False}) == 1


def test_pipeline_exit_code_returns_zero_for_ready_action() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--download-source", "--json"])

    assert ptcli_cli._pipeline_exit_code(args, {"status": "ok", "ready": True}) == 0


def test_pipeline_next_actions_reports_stage_blockers() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--inject-source", "--json"])
    actions = ptcli_cli._pipeline_next_actions(args, ["inject-source: --save-path is required when --inject-source is used."], {"complete": False})

    assert actions == ["Provide a qBittorrent save path with --save-path when using --inject-source."]


def test_pipeline_next_actions_explain_source_stage_blockers() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--download-source", "--json"])
    actions = ptcli_cli._pipeline_next_actions(
        args,
        [
            "source-download: Skipped because flow-check, source-info, or executable rule-check did not pass.",
            "inject-source: qBittorrent source torrent injection was not verified.",
            "wait-complete: qBittorrent task did not complete before timeout.",
        ],
        {"complete": False},
    )

    assert any("--download-source" in action and "rule-check" in action for action in actions)
    assert any("--inject-source --save-path" in action for action in actions)
    assert any("--wait-complete" in action and "--path" in action for action in actions)


def test_pipeline_next_actions_explain_target_upload_followup_blockers() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--upload-target", "--json"])
    actions = ptcli_cli._pipeline_next_actions(
        args,
        [
            "target-upload: Target upload stage did not complete every requested upload follow-up.",
            "target-upload: injected_torrent: qBittorrent refused torrent",
            "target-upload: uploaded_wait: torrent hash missing",
        ],
        {"complete": False},
    )

    assert any("Inspect target-upload follow-up blockers" in action for action in actions)
    assert any("--inject-uploaded-torrent" in action and "--uploaded-save-path" in action for action in actions)
    assert any("--wait-uploaded-complete" in action for action in actions)


def test_resume_next_command_uses_stage_blocker_details() -> None:
    commands = {
        "resume-source-torrent": "python3 ptcli.py pipeline --source-torrent-file /tmp/U2-60635.torrent",
        "resume-target-upload": "python3 ptcli.py pipeline --upload-target",
        "resume-uploaded-torrent": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent",
        "resume-uploaded-torrent-download": "python3 ptcli.py target-upload --uploaded-torrent-id 999",
    }

    source_hash = ptcli_cli._resume_next_command(["source.hash_consistent"], commands)
    source_wait = ptcli_cli._resume_next_command(["source.wait_evidence"], commands)
    target_hash = ptcli_cli._resume_next_command(["target.hash_consistent"], commands)
    target_wait = ptcli_cli._resume_next_command(["target.uploaded_wait_evidence"], commands)
    uploaded_wait = ptcli_cli._resume_next_command(["target-upload: uploaded_wait: torrent hash missing"], commands)
    downloaded_missing = ptcli_cli._resume_next_command(["target-upload: downloaded_torrent: target torrent file does not exist on disk."], commands)
    generic_target = ptcli_cli._resume_next_command(["target-upload: MTEAM upload failed."], commands)

    assert source_hash["stage"] == "resume-source-torrent"
    assert source_wait["stage"] == "resume-source-torrent"
    assert target_hash["stage"] == "resume-uploaded-torrent"
    assert target_wait["stage"] == "resume-uploaded-torrent"
    assert uploaded_wait["stage"] == "resume-uploaded-torrent"
    assert downloaded_missing["stage"] == "resume-uploaded-torrent-download"
    assert generic_target["stage"] == "resume-target-upload"


def test_run_summary_resume_commands_prefer_artifact_save_paths() -> None:
    payload = {
        "source_tracker": "U2",
        "source_torrent_id": "60635",
        "target_trackers": ["MTEAM"],
        "client": "default",
        "qbit_options": {
            "source": {"category": "SOURCE"},
            "uploaded": {"category": "MTEAM"},
        },
    }
    artifacts = {
        "source_torrent_file": "/tmp/U2-60635.torrent",
        "source_save_path": "/verified/source",
        "target_package_dir": "/tmp/package",
        "target_torrent_file": "/tmp/target.torrent",
        "uploaded_torrent_file": "/tmp/MTEAM-999.torrent",
        "uploaded_save_path": "/verified/uploaded",
    }

    commands = {command["stage"]: command["command"] for command in ptcli_cli._run_summary_resume_commands(payload, artifacts)}

    assert "--save-path /verified/source" in commands["resume-source-torrent"]
    assert "--uploaded-save-path /verified/uploaded" in commands["resume-target-upload"]
    assert "--uploaded-save-path /verified/uploaded" in commands["resume-uploaded-torrent"]


def test_pipeline_next_actions_reports_closure_blockers() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--prepare-target", "--json"])
    actions = ptcli_cli._pipeline_next_actions(
        args,
        [],
        {
            "complete": False,
            "blockers": [
                "source.ready",
                "source.hash_consistent",
                "target.prepared",
                "target.uploaded",
                "target.downloaded",
                "target.hash_consistent",
                "target.duplicate_clean",
                "target.rule_obligations",
            ],
        },
    )

    assert any("--source-torrent-file" in action for action in actions)
    assert any("different source hash" in action for action in actions)
    assert any("--package-dir" in action for action in actions)
    assert any("--upload-target --target-execute --confirm-upload" in action for action in actions)
    assert any("--uploaded-torrent-file" in action for action in actions)
    assert any("different uploaded hash" in action for action in actions)
    assert any("fresh MTEAM duplicate check" in action for action in actions)
    assert any("source download/retorrent" in action for action in actions)


def test_pipeline_next_actions_reports_completed_closure() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--upload-target", "--json"])
    actions = ptcli_cli._pipeline_next_actions(args, [], {"complete": True, "blockers": []})

    assert actions == ["Retorrent closure is complete; verify the target tracker page and qBittorrent seeding state."]


def test_summary_check_reports_pipeline_completion(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": True,
                "complete": True,
                "blockers": [],
                "resume_state": {
                    "next_stage": None,
                    "next_command": None,
                    "available_stages": [],
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "uploaded_wait_evidence": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["schema_version_ok"] is True
    assert payload["kind_supported"] is True
    assert payload["automation_action"] == "complete"
    assert payload["next_command_ready"] is False
    assert payload["should_execute_next_command"] is False
    assert payload["automation_exit_code"] == 0
    assert payload["complete"] is True
    assert payload["live_safe_to_attempt"] is True
    assert payload["missing_artifacts"] == []


def test_summary_check_blocks_missing_pipeline_audit_artifact(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": True,
                "complete": True,
                "blockers": [],
                "resume_state": {
                    "next_stage": "resume-target-upload",
                    "next_command": "python3 ptcli.py pipeline --upload-target",
                    "available_stages": ["resume-target-upload"],
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["next_stage"] == "resume-target-upload"
    assert "source_wait_evidence" in payload["missing_artifacts"]
    assert "target_rule_obligations" in payload["missing_artifacts"]
    assert "uploaded_wait_evidence" in payload["missing_artifacts"]
    assert "missing audit artifact: source_wait_evidence" in payload["blockers"]
    assert "missing audit artifact: target_rule_obligations" in payload["blockers"]
    assert "missing audit artifact: uploaded_wait_evidence" in payload["blockers"]


def test_summary_check_prefers_uploaded_resume_for_uploaded_wait_artifact(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": True,
                "complete": True,
                "blockers": [],
                "resume_commands": [
                    {"stage": "resume-source-torrent", "command": "python3 ptcli.py pipeline --source-torrent-file /tmp/U2-60635.torrent"},
                    {"stage": "resume-uploaded-torrent", "command": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"},
                ],
                "resume_state": {
                    "next_stage": None,
                    "next_command": None,
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_stage"] == "resume-uploaded-torrent"
    assert payload["next_command"] == "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"


def test_summary_check_reports_qbit_wait_request_mismatch(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["source.wait_evidence"],
                "resume_commands": [{"stage": "resume-source-torrent", "command": "python3 ptcli.py pipeline --source-torrent-file /tmp/U2-60635.torrent"}],
                "evidence": {
                    "source": {
                        "qbit_closure": {
                            "wait": {
                                "complete": False,
                                "matched_count": 1,
                                "completion_verification": {
                                    "matched_count": 1,
                                    "complete_count": 1,
                                    "any_complete": True,
                                    "requested_hash_matched": False,
                                    "requested_content_path_matched": None,
                                    "observed_hashes": ["f" * 40],
                                    "observed_content_paths": ["/downloads/Other"],
                                    "observed_save_paths": ["/downloads"],
                                },
                                "blockers": ["qBittorrent matched torrents, but none matched requested hash bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb."],
                            }
                        }
                    }
                },
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": False,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "uploaded_wait_evidence": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["automation_action"] == "resolve_qbit_wait_mismatch"
    assert payload["next_stage"] == "resume-source-torrent"
    assert payload["next_command"] == "python3 ptcli.py pipeline --source-torrent-file /tmp/U2-60635.torrent"
    assert payload["next_command_ready"] is True
    assert payload["should_execute_next_command"] is False
    assert payload["qbit_wait_mismatch"] is True
    assert payload["qbit_wait_mismatches"] == ["source.requested_hash"]
    diagnostics = payload["qbit_wait_diagnostics"]["source"]
    assert diagnostics["request_mismatch"] is True
    assert diagnostics["any_complete"] is True
    assert diagnostics["complete_count"] == 1
    assert diagnostics["requested_hash_matched"] is False
    assert diagnostics["observed_hashes"] == ["f" * 40]
    assert diagnostics["observed_content_paths"] == ["/downloads/Other"]


def test_run_summary_exposes_qbit_wait_request_mismatch(tmp_path) -> None:
    summary_file = ptcli_cli._write_run_summary(
        {
            "status": "blocked",
            "ready": False,
            "complete": False,
            "blockers": ["source.wait_evidence"],
            "evidence": {
                "source": {
                    "source_wait": {
                        "complete": True,
                        "completion_verification": {
                            "matched_count": 1,
                            "complete_count": 1,
                            "any_complete": True,
                            "requested_hash_matched": False,
                            "requested_content_path_matched": True,
                            "observed_hashes": ["f" * 40],
                            "observed_content_paths": ["/downloads/Example"],
                            "observed_save_paths": ["/downloads"],
                        },
                    }
                },
                "target": {
                    "uploaded_wait": {
                        "complete": True,
                        "completion_verification": {
                            "matched_count": 1,
                            "complete_count": 1,
                            "any_complete": True,
                            "requested_hash_matched": True,
                            "requested_content_path_matched": False,
                            "observed_hashes": ["b" * 40],
                            "observed_content_paths": ["/downloads/Other"],
                            "observed_save_paths": ["/downloads"],
                        },
                    }
                },
            },
            "stages": [],
        },
        str(tmp_path),
    )

    payload = json.loads(Path(summary_file).read_text(encoding="utf-8"))
    assert payload["qbit_wait_mismatch"] is True
    assert payload["qbit_wait_mismatches"] == ["source.requested_hash", "uploaded.requested_content_path"]
    assert payload["qbit_wait_diagnostics"]["source"]["observed_hashes"] == ["f" * 40]
    assert payload["qbit_wait_diagnostics"]["uploaded"]["observed_content_paths"] == ["/downloads/Other"]


def test_summary_check_falls_back_to_pipeline_resume_command(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "resume_commands": [
                    {
                        "stage": "resume-target-upload",
                        "command": "python3 ptcli.py pipeline --upload-target",
                        "argv": ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"],
                    }
                ],
                "resume_state": {
                    "next_stage": None,
                    "next_command": None,
                    "available_stages": ["resume-target-upload"],
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "uploaded_wait_evidence": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["next_stage"] == "resume-target-upload"
    assert payload["next_command"] == "python3 ptcli.py pipeline --upload-target"
    assert payload["automation_action"] == "run_next_command"
    assert payload["next_command_ready"] is True
    assert payload["should_execute_next_command"] is True
    assert payload["automation_exit_code"] == 1


def test_summary_check_print_next_command_outputs_only_command(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "resume_commands": [{"stage": "resume-target-upload", "command": "python3 ptcli.py pipeline --upload-target"}],
                "resume_state": {
                    "next_stage": None,
                    "next_command": None,
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "uploaded_wait_evidence": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-next-command"])

    assert code == 0
    assert capsys.readouterr().out == "python3 ptcli.py pipeline --upload-target\n"


def test_summary_check_print_next_command_is_quiet_when_complete(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": True,
                "complete": True,
                "blockers": [],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "uploaded_wait_evidence": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-next-command"])

    assert code == 0
    assert capsys.readouterr().out == ""


def test_summary_check_print_next_command_fails_without_resumable_command(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": True,
                "complete": True,
                "blockers": [],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-next-command"])

    assert code == 1
    assert capsys.readouterr().out == ""


def test_summary_check_print_shell_exports_automation_state(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "resume_commands": [
                    {
                        "stage": "resume-target-upload",
                        "command": "python3 ptcli.py pipeline --upload-target",
                        "argv": ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"],
                    }
                ],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-shell"])

    assert code == 0
    out = capsys.readouterr().out
    assert "export PTCLI_SUMMARY_STATUS=blocked\n" in out
    assert "export PTCLI_AUTOMATION_ACTION=run_next_command\n" in out
    assert "export PTCLI_AUTOMATION_EXIT_CODE=1\n" in out
    assert "export PTCLI_SHOULD_EXECUTE_NEXT_COMMAND=1\n" in out
    assert "export PTCLI_QBIT_WAIT_MISMATCH=0\n" in out
    assert "export PTCLI_QBIT_WAIT_MISMATCHES=''\n" in out
    assert "export PTCLI_NEXT_COMMAND='python3 ptcli.py pipeline --upload-target'\n" in out


def test_summary_check_print_shell_exports_qbit_wait_mismatch(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["source.wait_evidence"],
                "resume_commands": [{"stage": "resume-source-torrent", "command": "python3 ptcli.py pipeline --source-torrent-file /tmp/U2-60635.torrent"}],
                "evidence": {
                    "source": {
                        "qbit_closure": {
                            "wait": {
                                "complete": False,
                                "completion_verification": {
                                    "requested_hash_matched": False,
                                    "requested_content_path_matched": None,
                                },
                            }
                        }
                    }
                },
                "resume_state": {"artifacts": {"source_hash_consistent": True, "source_wait_evidence": False}},
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-shell"])

    assert code == 0
    out = capsys.readouterr().out
    assert "export PTCLI_AUTOMATION_ACTION=resolve_qbit_wait_mismatch\n" in out
    assert "export PTCLI_SHOULD_EXECUTE_NEXT_COMMAND=0\n" in out
    assert "export PTCLI_QBIT_WAIT_MISMATCH=1\n" in out
    assert "export PTCLI_QBIT_WAIT_MISMATCHES=source.requested_hash\n" in out


def test_summary_check_run_next_command_executes_ptcli_argv(tmp_path, monkeypatch, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "resume_commands": [
                    {
                        "stage": "resume-target-upload",
                        "command": "python3 ptcli.py pipeline --upload-target",
                        "argv": ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"],
                    }
                ],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_run(argv, check):
        calls.append((argv, check))
        return argparse.Namespace(returncode=7)

    monkeypatch.setattr(ptcli_cli.subprocess, "run", fake_run)

    code = main(["summary-check", "--summary-file", str(summary_file), "--run-next-command"])

    assert code == 7
    assert calls == [([ptcli_cli.sys.executable, "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"], False)]
    assert capsys.readouterr().out == ""


def test_summary_check_exposes_structured_next_command_argv(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "resume_commands": [
                    {
                        "stage": "resume-target-upload",
                        "command": "python3 ptcli.py pipeline --upload-target",
                        "argv": ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"],
                    }
                ],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["automation_action"] == "run_next_command"
    assert payload["next_stage"] == "resume-target-upload"
    assert payload["next_command"] == "python3 ptcli.py pipeline --upload-target"
    assert payload["next_command_argv"] == ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"]


def test_summary_check_run_next_command_rejects_non_ptcli_command(tmp_path, monkeypatch, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "resume_commands": [{"stage": "resume-target-upload", "command": "sh -c 'echo unsafe'"}],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    def fail_run(*_args, **_kwargs):
        pytest.fail("unexpected subprocess call")

    monkeypatch.setattr(ptcli_cli.subprocess, "run", fail_run)

    code = main(["summary-check", "--summary-file", str(summary_file), "--run-next-command"])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "Refusing to run unsupported summary next_command" in captured.err


def test_summary_check_run_next_command_is_noop_when_complete(tmp_path, monkeypatch, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": True,
                "complete": True,
                "blockers": [],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "uploaded_wait_evidence": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    def fail_run(*_args, **_kwargs):
        pytest.fail("unexpected subprocess call")

    monkeypatch.setattr(ptcli_cli.subprocess, "run", fail_run)

    code = main(["summary-check", "--summary-file", str(summary_file), "--run-next-command"])

    assert code == 0
    assert capsys.readouterr().out == ""


def test_summary_check_reports_target_upload_completion(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-target-upload-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.target_upload.summary",
                "summary": {"ready": True, "blockers": []},
                "resume_state": {
                    "next_stage": None,
                    "next_command": None,
                    "available_stages": ["verify-seeding"],
                    "artifacts": {
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "uploaded_wait_evidence": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["ready"] is True
    assert payload["available_stages"] == ["verify-seeding"]


def test_summary_check_prefers_uploaded_resume_for_target_upload_wait_artifact(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-target-upload-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.target_upload.summary",
                "summary": {"ready": True, "blockers": []},
                "recommended_commands": [
                    {"stage": "target-upload-retry", "command": "python3 ptcli.py target-upload --execute"},
                    {"stage": "resume-uploaded-torrent", "command": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"},
                ],
                "resume_state": {
                    "next_stage": None,
                    "next_command": None,
                    "artifacts": {
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_stage"] == "resume-uploaded-torrent"
    assert payload["next_command"] == "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"


def test_summary_check_falls_back_to_target_upload_recommended_command(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-target-upload-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.target_upload.summary",
                "summary": {"ready": False, "blockers": ["uploaded_wait: torrent hash missing"]},
                "recommended_commands": [{"stage": "resume-uploaded-torrent", "command": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"}],
                "resume_state": {
                    "next_stage": None,
                    "next_command": None,
                    "available_stages": ["resume-uploaded-torrent"],
                    "artifacts": {
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_stage"] == "resume-uploaded-torrent"
    assert payload["next_command"] == "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"


def test_summary_check_reports_doctor_live_safety(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-doctor-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.doctor.live_readiness",
                "ready": True,
                "live_safe_to_attempt": True,
                "failed_check_names": [],
                "resume_state": {
                    "next_stage": "pipeline-live",
                    "next_command": "python3 ptcli.py pipeline --target-execute",
                    "available_stages": ["pipeline-live"],
                    "artifacts": {
                        "flow_check_ready": True,
                        "rule_check_ready": True,
                        "rules_acknowledged": True,
                        "target_rule_obligations": True,
                        "target_package_preflight_ready": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["live_safe_to_attempt"] is True
    assert payload["next_stage"] == "pipeline-live"


def test_summary_check_blocks_unsupported_schema_version(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "ptcli.pipeline.run_summary",
                "ready": True,
                "complete": True,
                "resume_state": {"artifacts": {}},
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["schema_version"] == 2
    assert payload["expected_schema_version"] == 1
    assert payload["schema_version_ok"] is False
    assert payload["kind_supported"] is True
    assert payload["automation_action"] == "replace_summary"
    assert payload["should_execute_next_command"] is False
    assert any("Unsupported summary schema_version" in blocker for blocker in payload["blockers"])


def test_summary_check_blocks_unknown_kind_with_supported_kinds(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-unknown-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "not.ptcli",
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["schema_version_ok"] is True
    assert payload["kind_supported"] is False
    assert "ptcli.pipeline.run_summary" in payload["supported_kinds"]
    assert "Unsupported ptcli summary kind: not.ptcli" in payload["blockers"]


def test_target_upload_result_requires_requested_uploaded_torrent_injection() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"status": "blocked", "blockers": ["qBittorrent refused torrent"]},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is False


def test_target_upload_result_requires_uploaded_torrent_client_verification() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": False},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is False


def test_target_upload_result_requires_uploaded_torrent_client_metadata_match() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {
            "hash": "a" * 40,
            "verified_in_client": True,
            "client_verification": {
                "visible": True,
                "save_path_matched": True,
                "category_matched": False,
                "tags_matched": True,
            },
        },
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is False


def test_target_upload_result_accepts_completed_uploaded_torrent_injection() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is True


def test_target_upload_result_requires_uploaded_torrent_hash_consistency() -> None:
    payload = {
        "status": "uploaded",
        "submitted_torrent_hash": "a" * 40,
        "uploaded_torrent_hash": "a" * 40,
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "torrent_hash": "b" * 40},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is False


def test_target_upload_result_checks_uploaded_wait_match_hash_consistency() -> None:
    payload = {
        "status": "uploaded",
        "uploaded_torrent_hash": "a" * 40,
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "torrent_hash": "a" * 40},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
        "uploaded_wait": {
            "complete": True,
            "query": {"torrent_hash": "a" * 40, "content_path": "/downloads/Name"},
            "matches": [{"hash": "b" * 40, "content_path": "/downloads/Name"}],
        },
    }

    blockers = ptcli_cli._uploaded_torrent_hash_consistency_blockers(payload)

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True, wait_uploaded_complete=True) is False
    assert blockers
    assert f"uploaded_wait_query={'a' * 40}" in blockers[0]
    assert f"uploaded_wait_match={'b' * 40}" in blockers[0]


def test_uploaded_injection_preserves_upload_response_hash_for_consistency_check() -> None:
    payload = ptcli_cli._with_uploaded_injection(
        {
            "status": "uploaded",
            "uploaded_torrent_hash": "a" * 40,
            "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "torrent_hash": "a" * 40},
        },
        {"hash": "b" * 40, "verified_in_client": True},
    )

    blockers = ptcli_cli._uploaded_torrent_hash_consistency_blockers(payload)

    assert payload["uploaded_torrent_hash"] == "a" * 40
    assert blockers
    assert f"upload_response={'a' * 40}" in blockers[0]
    assert f"injected_torrent={'b' * 40}" in blockers[0]


def test_target_upload_result_requires_uploaded_torrent_completion_when_requested() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
        "uploaded_wait": {"complete": False, "matches": []},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True, wait_uploaded_complete=True) is False


def test_target_upload_summary_surfaces_followup_blockers() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"status": "blocked", "blockers": ["qBittorrent refused torrent"]},
        "uploaded_wait": {"complete": False, "blockers": ["torrent hash missing"]},
    }

    summary = ptcli_cli._target_upload_summary(payload, {"status": "ready", "blockers": [], "rule_obligation_review": {"ready": True, "blockers": []}})

    assert summary["ready"] is False
    assert summary["rule_obligations"]["ready"] is True
    assert "injected_torrent: qBittorrent refused torrent" in summary["blockers"]
    assert "uploaded_wait: torrent hash missing" in summary["blockers"]
    assert "uploaded_wait: qBittorrent did not report the uploaded target torrent as complete." in summary["blockers"]


def test_target_upload_summary_surfaces_downloaded_torrent_file_evidence_blockers() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "exists": False},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
    }

    summary = ptcli_cli._target_upload_summary(payload, {"status": "ready", "blockers": [], "rule_obligation_review": {"ready": True, "blockers": []}})

    assert summary["ready"] is False
    assert "downloaded_torrent: target torrent file does not exist on disk." in summary["blockers"]


def test_target_upload_result_requires_downloaded_torrent_file_evidence_when_available() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "exists": False},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is False


def test_target_upload_summary_surfaces_client_metadata_mismatch() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {
            "client": "qbittorrent",
            "hash": "a" * 40,
            "save_path": "/downloads/Name",
            "category": "MTEAM",
            "tags": "retorrent",
            "verified_in_client": True,
            "verification_attempts": 3,
            "client_verification": {
                "visible": True,
                "save_path_matched": False,
                "category_matched": True,
                "tags_matched": False,
            },
        },
    }

    summary = ptcli_cli._target_upload_summary(payload, {"status": "ready", "blockers": [], "rule_obligation_review": {"ready": True, "blockers": []}})

    assert summary["ready"] is False
    assert summary["qbit_closure"]["injection"]["client"] == "qbittorrent"
    assert summary["qbit_closure"]["injection"]["hash"] == "a" * 40
    assert summary["qbit_closure"]["injection"]["save_path"] == "/downloads/Name"
    assert summary["qbit_closure"]["injection"]["category"] == "MTEAM"
    assert summary["qbit_closure"]["injection"]["tags"] == "retorrent"
    assert summary["qbit_closure"]["injection"]["verified_in_client"] is True
    assert summary["qbit_closure"]["injection"]["verification_attempts"] == 3
    assert summary["qbit_closure"]["injection"]["client_verification"]["save_path_matched"] is False
    assert "injected_torrent: qBittorrent did not report the requested save path for the injected torrent." in summary["blockers"]
    assert "injected_torrent: qBittorrent did not report the requested tags for the injected torrent." in summary["blockers"]


def test_target_upload_summary_surfaces_uploaded_torrent_hash_mismatch() -> None:
    payload = {
        "status": "uploaded",
        "submitted_torrent_hash": "a" * 40,
        "uploaded_torrent_hash": "a" * 40,
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "torrent_hash": "b" * 40},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
        "uploaded_wait": {"complete": True, "query": {"torrent_hash": "a" * 40}, "matches": [{"hash": "a" * 40}]},
    }

    summary = ptcli_cli._target_upload_summary(payload, {"status": "ready", "blockers": [], "rule_obligation_review": {"ready": True, "blockers": []}})

    assert summary["ready"] is False
    assert "uploaded_torrent_hash: inconsistent target torrent hashes" in summary["blockers"][0]
    assert f"submitted_torrent={'a' * 40}" in summary["blockers"][0]
    assert f"downloaded_torrent={'b' * 40}" in summary["blockers"][0]


def test_target_upload_result_accepts_completed_uploaded_torrent_wait() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
        "uploaded_wait": {"complete": True, "matches": [{"hash": "a" * 40}]},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True, wait_uploaded_complete=True) is True


def test_target_upload_result_requires_uploaded_wait_match_evidence() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
        "uploaded_wait": {"complete": True, "matches": []},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True, wait_uploaded_complete=True) is False


def test_target_upload_summary_requires_uploaded_wait_match_evidence() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
        "uploaded_wait": {"complete": True, "matches": []},
    }

    summary = ptcli_cli._target_upload_summary(payload, {"status": "ready", "blockers": [], "rule_obligation_review": {"ready": True, "blockers": []}})

    assert summary["ready"] is False
    assert summary["seeding_verified"] is False
    assert "uploaded_wait: qBittorrent completion wait did not include matched torrent evidence." in summary["blockers"]


def test_wait_result_completed_rejects_request_mismatch() -> None:
    assert ptcli_cli._wait_result_completed(
        {
            "complete": True,
            "matches": [{"hash": "a" * 40}],
            "completion_verification": {"matched_count": 1, "complete_count": 1, "any_complete": True, "requested_hash_matched": False},
        }
    ) is False
    assert ptcli_cli._wait_result_completed(
        {
            "complete": True,
            "matches": [{"hash": "a" * 40, "content_path": "/downloads/Other"}],
            "completion_verification": {"matched_count": 1, "complete_count": 1, "any_complete": True, "requested_content_path_matched": False},
        }
    ) is False


def test_wait_complete_blockers_report_request_mismatch() -> None:
    blockers = ptcli_cli._wait_complete_result_blockers(
        {
            "complete": True,
            "matches": [{"hash": "a" * 40}],
            "completion_verification": {"matched_count": 1, "complete_count": 1, "any_complete": True, "requested_hash_matched": False},
        }
    )

    assert blockers == ["qBittorrent completion wait matched torrents, but not the requested hash."]


def test_target_upload_blockers_report_uploaded_wait_request_mismatch() -> None:
    summary = ptcli_cli._target_upload_summary(
        {
            "status": "uploaded",
            "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
            "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
            "uploaded_wait": {
                "complete": True,
                "matches": [{"hash": "a" * 40, "content_path": "/downloads/Other"}],
                "completion_verification": {"matched_count": 1, "complete_count": 1, "any_complete": True, "requested_content_path_matched": False},
            },
        },
        {"status": "ready", "blockers": [], "rule_obligation_review": {"ready": True, "blockers": []}},
    )

    assert summary["ready"] is False
    assert "uploaded_wait: qBittorrent completion wait matched torrents, but not the requested content path." in summary["blockers"]


def test_target_upload_next_command_requires_uploaded_wait_match_evidence() -> None:
    next_command = ptcli_cli._target_upload_next_command(
        {
            "injected": True,
            "seeding_verified": False,
            "uploaded_wait": {"complete": True, "matches": []},
            "blockers": ["uploaded_wait: qBittorrent completion wait did not include matched torrent evidence."],
        },
        {"resume-uploaded-torrent": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"},
    )

    assert next_command["stage"] == "resume-uploaded-torrent"


def test_pipeline_evidence_summarizes_closure_for_automation() -> None:
    closure = {
        "complete": True,
        "blockers": [],
        "source": {
            "ready": True,
            "downloaded": True,
            "injected": True,
            "complete": True,
            "matched": False,
            "torrent_hash": "a" * 40,
            "content_path": "/downloads/Name",
            "source_torrent": {"path": "/tmp/U2-60635.torrent", "exists": True, "size_bytes": 8, "sha1": "c" * 40},
        },
        "target": {
            "prepared": True,
            "uploaded": True,
            "downloaded": True,
            "injected": True,
            "injection_verified": True,
            "seeding": True,
            "torrent_file": "/tmp/mteam.torrent",
            "uploaded_torrent_id": "999",
            "uploaded_torrent_hash": "b" * 40,
            "injected_torrent_hash": "b" * 40,
            "uploaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "exists": True, "size_bytes": 8, "sha1": "d" * 40},
            "uploaded_torrent_path": "/tmp/MTEAM-999.torrent",
            "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
        },
    }

    evidence = ptcli_cli._pipeline_evidence(closure)

    assert evidence["complete"] is True
    assert evidence["source"]["mode"] == "downloaded"
    assert evidence["source"]["torrent_hash"] == "a" * 40
    assert evidence["source"]["source_torrent"]["sha1"] == "c" * 40
    assert evidence["target"]["ready"] is True
    assert evidence["target"]["uploaded_torrent_id"] == "999"
    assert evidence["target"]["uploaded_torrent_hash"] == "b" * 40
    assert evidence["target"]["fresh_duplicate_check"] == {"searched": True, "count": 0, "dupes": []}
    assert evidence["target"]["injection_verified"] is True
    assert evidence["target"]["injected_torrent_hash"] == "b" * 40
    assert evidence["target"]["uploaded_torrent"]["sha1"] == "d" * 40


def test_pipeline_stage_blockers_include_target_upload_followup_details() -> None:
    stages = [
        {
            "stage": "target-upload",
            "ok": False,
            "error": "Target upload stage did not complete every requested upload follow-up.",
            "result": {
                "status": "uploaded",
                "injected_torrent": {"status": "blocked", "blockers": ["qBittorrent refused torrent"]},
            },
        }
    ]

    blockers = ptcli_cli._pipeline_stage_blockers(stages)

    assert "target-upload: Target upload stage did not complete every requested upload follow-up." in blockers
    assert "target-upload: injected_torrent: qBittorrent refused torrent" in blockers


def test_pipeline_stage_blockers_include_source_followup_details() -> None:
    stages = [
        {
            "stage": "inject-source",
            "ok": False,
            "error": "qBittorrent source torrent injection was not verified.",
            "result": {"hash": "a" * 40, "verified_in_client": False},
        },
        {
            "stage": "wait-complete",
            "ok": False,
            "error": "qBittorrent task did not complete before timeout.",
            "result": {"complete": False, "blockers": ["no matching torrent"]},
        },
    ]

    blockers = ptcli_cli._pipeline_stage_blockers(stages)

    assert "inject-source: qBittorrent source torrent injection was not verified." in blockers
    assert "inject-source: qBittorrent did not verify the injected source torrent in the client list." in blockers
    assert "wait-complete: no matching torrent" in blockers
    assert "wait-complete: qBittorrent did not report the source torrent as complete." in blockers


def test_pipeline_run_summary_reports_stage_statuses_for_automation() -> None:
    stages = [
        {"stage": "flow-check", "ok": True, "result": {"ready": True, "checks": []}},
        {
            "stage": "rule-check",
            "ok": True,
            "result": {
                "ready": True,
                "checks": [{"name": "rules_acknowledged", "ok": True, "message": "ok"}],
                "manual_review": {"required": True, "acknowledged": True},
                "automation_scope": {"site_specific_rules_encoded": False, "concrete_policy_checks": "tracker_adapters"},
                "rule_obligations": [
                    {"tracker": "U2", "role": "source", "action": "download_and_retorrent", "rules_url": "https://u2.dmhy.org/rules.php", "acknowledged": True},
                    {"tracker": "MTEAM", "role": "target", "action": "upload_and_seed", "rules_url": "https://kp.m-team.cc/rules", "acknowledged": True},
                ],
            },
        },
        {"stage": "source-download", "ok": True, "skipped": True, "message": "--download-source not provided; source download skipped."},
        {"stage": "target-dupe-check", "ok": True, "result": {"searched": True, "count": 0, "dupes": []}},
        {
            "stage": "target-prepare",
            "ok": True,
            "result": {
                "upload_gate": {"ready": True, "dupe_count": 0, "blockers": []},
                "rule_review": {
                    "rule_check_ready": True,
                    "blockers": [],
                    "rule_obligations": [
                        {"tracker": "U2", "role": "source", "action": "download_and_retorrent", "rules_url": "https://u2.dmhy.org/rules.php", "acknowledged": True},
                        {"tracker": "MTEAM", "role": "target", "action": "upload_and_seed", "rules_url": "https://kp.m-team.cc/rules", "acknowledged": True},
                    ],
                },
            },
        },
        {"stage": "target-upload", "ok": False, "error": "Target upload stage did not complete every requested upload follow-up."},
    ]
    closure = {
        "complete": False,
        "blockers": ["target.injected"],
        "source": {"ready": True, "matched": True, "content_path": "/downloads/Name"},
        "target": {"prepared": True, "uploaded": True, "downloaded": True, "injected": False},
    }
    evidence = ptcli_cli._pipeline_evidence(closure)

    summary = ptcli_cli._pipeline_run_summary(stages, False, ["target-upload: failed"], closure, evidence)

    assert summary["status"] == "blocked"
    assert summary["complete"] is False
    assert summary["blockers"] == ["target-upload: failed"]
    assert summary["failed_stages"] == ["target-upload"]
    assert summary["completed_stages"] == ["flow-check", "rule-check", "target-dupe-check", "target-prepare"]
    assert summary["skipped_stages"] == ["source-download"]
    assert summary["stage_statuses"][-1]["message"] == "Target upload stage did not complete every requested upload follow-up."
    assert summary["gates"]["rule_check"]["rules_acknowledged"] is True
    assert summary["gates"]["duplicate_check"]["ok"] is True
    assert summary["gates"]["upload_gate"]["ready"] is True
    assert summary["gates"]["rule_review"]["rule_obligations"]["ready"] is True
    assert summary["gates"]["rule_review"]["rule_obligations"]["source_acknowledged"] is True
    assert summary["gates"]["rule_review"]["rule_obligations"]["mteam_acknowledged"] is True
    assert summary["compliance"]["ready"] is True
    assert summary["compliance"]["rules_acknowledged"] is True
    assert summary["compliance"]["site_specific_rules_encoded"] is False
    assert summary["compliance"]["policy_checks"] == "tracker_adapters"
    assert summary["compliance"]["rule_obligations"]["acknowledged_count"] == 2
    assert "requires manual source/target rule review" in summary["compliance"]["disclaimer"]
    assert summary["resume"]["used"] is False
    assert summary["source"]["mode"] == "matched"


def test_pipeline_compliance_summary_blocks_missing_rule_check() -> None:
    summary = ptcli_cli._pipeline_compliance_summary([])

    assert summary["ready"] is False
    assert summary["rules_acknowledged"] is False
    assert summary["site_specific_rules_encoded"] is False
    assert summary["policy_checks"] == "missing_rule_check"
    assert summary["blockers"] == ["rule-check stage did not produce compliance evidence."]


def test_pipeline_gate_summary_reports_missing_rule_obligations() -> None:
    summary = ptcli_cli._pipeline_gate_summary(
        [
            {"stage": "target-prepare", "ok": True, "result": {"rule_review": {"rule_check_ready": True, "blockers": [], "rule_obligations": []}}},
        ]
    )

    obligations = summary["rule_review"]["rule_obligations"]
    assert obligations["ready"] is False
    assert obligations["missing"] == ["source_download_and_retorrent", "mteam_upload_and_seed"]
    assert obligations["source_acknowledged"] is False
    assert obligations["mteam_acknowledged"] is False


def test_pipeline_closure_accepts_existing_qbit_match_as_source_ready() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is True
    assert closure["blockers"] == []
    assert closure["source"]["ready"] is True
    assert closure["source"]["matched"] is True


def test_pipeline_closure_rejects_unverified_existing_qbit_match() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "b" * 40}]}},
        {
            "stage": "source-content-verify",
            "ok": False,
            "message": "Matched qBittorrent content does not include the source tracker torrent hash.",
            "result": {"verified": False, "expected_hash": "a" * 40, "matched_hashes": ["b" * 40]},
        },
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "c" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "c" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "c" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)

    assert closure["complete"] is False
    assert closure["blockers"] == ["source.ready"]
    assert closure["source"]["ready"] is False
    assert closure["source"]["matched"] is True
    assert closure["source"]["content_verified"] is False
    assert evidence["source"]["content_verified"] is False
    assert evidence["source"]["content_verification"]["matched_hashes"] == ["b" * 40]


def test_pipeline_closure_preserves_torrent_file_evidence() -> None:
    source_torrent = {"path": "/tmp/U2-60635.torrent", "exists": True, "size_bytes": 8, "sha1": "c" * 40}
    uploaded_torrent = {"path": "/tmp/MTEAM-999.torrent", "exists": True, "size_bytes": 9, "sha1": "d" * 40}
    stages = [
        {"stage": "source-download", "ok": True, "result": source_torrent},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "verified_in_client": True}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": True, "matches": [{"hash": "a" * 40, "content_path": "/downloads/Name"}]}},
        {"stage": "match", "ok": True, "result": {"matches": []}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": uploaded_torrent,
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)

    assert closure["source"]["source_torrent"] == source_torrent
    assert closure["target"]["uploaded_torrent"] == uploaded_torrent
    assert evidence["source"]["source_torrent"] == source_torrent
    assert evidence["target"]["uploaded_torrent"] == uploaded_torrent


def test_pipeline_closure_requires_existing_source_torrent_file_evidence() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "result": {"path": "/tmp/U2-60635.torrent", "exists": False}},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "verified_in_client": True}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": True}},
        {"stage": "match", "ok": True, "result": {"matches": []}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is False
    assert closure["source"]["downloaded"] is False
    assert "source.ready" in closure["blockers"]


def test_pipeline_closure_requires_existing_uploaded_torrent_file_evidence() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "result": {"path": "/tmp/U2-60635.torrent"}},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "verified_in_client": True}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": True}},
        {"stage": "match", "ok": True, "result": {"matches": []}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "exists": False},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)

    assert closure["complete"] is False
    assert closure["target"]["downloaded"] is False
    assert "target.downloaded" in closure["blockers"]
    assert evidence["target"]["downloaded"] is False


def test_pipeline_closure_requires_target_injection_client_verification() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": False},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is False
    assert closure["blockers"] == ["target.injected"]
    assert closure["target"]["injected"] is False
    assert closure["target"]["injection_verified"] is False
    assert closure["target"]["injected_torrent_hash"] == "b" * 40
    assert closure["target"]["uploaded_torrent_hash"] == "b" * 40


def test_pipeline_closure_rejects_target_injection_without_client_visibility() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is False
    assert closure["blockers"] == ["target.injected"]
    assert closure["target"]["injected"] is False
    assert closure["target"]["injection_verified"] is False


def test_pipeline_closure_accepts_structured_client_visibility() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "hash": "b" * 40},
                "injected_torrent": {"hash": "b" * 40, "client_verification": {"visible": True, "hash_matched": True}},
                "uploaded_wait": {"complete": True, "query": {"torrent_hash": "b" * 40}, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["target"]["injected"] is True
    assert "target.injected" not in closure["blockers"]


def test_pipeline_closure_requires_uploaded_torrent_completion_when_waited() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": False, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is False
    assert closure["blockers"] == ["target.seeding"]
    assert closure["target"]["injected"] is True
    assert closure["target"]["seeding"] is False
    assert closure["target"]["uploaded_wait"]["complete"] is False


def test_pipeline_closure_requires_source_wait_match_evidence() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "result": {"torrent_path": "/tmp/U2-60635.torrent"}},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "verified_in_client": True}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": True, "matches": []}},
        {"stage": "match", "ok": True, "result": {"matches": []}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is False
    assert closure["source"]["complete"] is False
    assert "source.ready" in closure["blockers"]


def test_pipeline_closure_requires_uploaded_wait_match_evidence() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": []},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)

    assert closure["complete"] is False
    assert closure["target"]["seeding"] is False
    assert "target.seeding" in closure["blockers"]
    assert evidence["target"]["seeding"] is False


def test_pipeline_closure_requires_uploaded_torrent_hash_consistency() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "hash": "c" * 40},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "query": {"torrent_hash": "b" * 40}, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)

    assert closure["complete"] is False
    assert closure["blockers"] == ["target.hash_consistent"]
    assert closure["target"]["hash_consistent"] is False
    assert evidence["target"]["hash_consistent"] is False
    assert evidence["target"]["seeding"] is True


def test_pipeline_closure_requires_clean_target_duplicate_check() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 1, "dupes": [{"name": "Existing"}]},
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "hash": "b" * 40},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "query": {"torrent_hash": "b" * 40}, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)

    assert closure["complete"] is False
    assert closure["blockers"] == ["target.duplicate_clean"]
    assert closure["target"]["duplicate_clean"] is False
    assert evidence["target"]["duplicate_clean"] is False
    assert evidence["target"]["fresh_duplicate_check"]["count"] == 1


def test_pipeline_closure_requires_target_rule_obligations() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {
            "stage": "target-prepare",
            "ok": True,
            "result": {"rule_review": {"rule_check_ready": True, "blockers": [], "rule_obligations": []}},
        },
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "hash": "b" * 40},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "query": {"torrent_hash": "b" * 40}, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)

    assert closure["complete"] is False
    assert closure["blockers"] == ["target.rule_obligations"]
    assert closure["target"]["rule_obligations"]["ready"] is False
    assert evidence["target"]["rule_obligations"]["missing"] == ["source_download_and_retorrent", "mteam_upload_and_seed"]


def test_pipeline_closure_requires_source_injection_client_verification() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "result": {"torrent_path": "/tmp/U2-60635.torrent"}},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "verified_in_client": False}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": True}},
        {"stage": "match", "ok": True, "result": {"matches": []}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is False
    assert closure["blockers"] == ["source.ready"]
    assert closure["source"]["ready"] is False
    assert closure["source"]["injected"] is False
    assert closure["source"]["injection_verified"] is False
    assert closure["source"]["injected_torrent_hash"] == "a" * 40


def test_pipeline_closure_reports_source_hash_inconsistency() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "result": {"torrent_hash": "a" * 40}},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "verified_in_client": True}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": True, "query": {"torrent_hash": "a" * 40}, "matches": [{"hash": "b" * 40}]}},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "source-content-verify", "ok": True, "result": {"verified": True, "matched_hashes": ["a" * 40]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "c" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "hash": "c" * 40},
                "injected_torrent": {"hash": "c" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "query": {"torrent_hash": "c" * 40}, "matches": [{"hash": "c" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)

    assert closure["complete"] is False
    assert closure["blockers"] == ["source.hash_consistent"]
    assert closure["source"]["hash_consistent"] is False
    assert evidence["source"]["hash_consistent"] is False


def test_pipeline_closure_requires_source_wait_completion() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "result": {"torrent_path": "/tmp/U2-60635.torrent"}},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "verified_in_client": True}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": False, "matches": []}},
        {"stage": "match", "ok": True, "result": {"matches": []}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is False
    assert closure["blockers"] == ["source.ready"]
    assert closure["source"]["downloaded"] is True
    assert closure["source"]["injected"] is True
    assert closure["source"]["complete"] is False
    assert closure["source"]["source_wait"]["complete"] is False


def test_pipeline_evidence_reports_source_injection_verification() -> None:
    closure = {
        "complete": True,
        "blockers": [],
        "source": {
            "ready": True,
            "downloaded": True,
            "injected": True,
            "injection_verified": True,
            "injected_torrent": {
                "client": "qbittorrent",
                "hash": "a" * 40,
                "save_path": "/downloads",
                "verified_in_client": True,
                "verification_attempts": 2,
            },
            "injected_torrent_hash": "a" * 40,
            "complete": True,
            "matched": False,
            "torrent_hash": "a" * 40,
            "content_path": "/downloads/Name",
            "source_wait": {"client": "qbittorrent", "complete": True, "query": {"torrent_hash": "a" * 40}, "matches": [{"hash": "a" * 40}]},
        },
        "target": {
            "prepared": True,
            "uploaded": True,
            "downloaded": True,
            "injected": True,
            "injection_verified": True,
            "injected_torrent": {
                "client": "qbittorrent",
                "hash": "b" * 40,
                "save_path": "/downloads/Name",
                "category": "MTEAM",
                "tags": "retorrent",
                "verified_in_client": True,
                "verification_attempts": 3,
            },
            "torrent_file": "/tmp/mteam.torrent",
            "uploaded_torrent_hash": "b" * 40,
            "injected_torrent_hash": "b" * 40,
            "uploaded_wait": {"client": "qbittorrent", "complete": True, "query": {"torrent_hash": "b" * 40}, "matches": [{"hash": "b" * 40}]},
            "uploaded_torrent_path": "/tmp/MTEAM-999.torrent",
        },
    }

    evidence = ptcli_cli._pipeline_evidence(closure)

    assert evidence["source"]["injection_verified"] is True
    assert evidence["source"]["downloaded"] is True
    assert evidence["source"]["injected"] is True
    assert evidence["source"]["complete"] is True
    assert evidence["source"]["matched"] is False
    assert evidence["source"]["injected_torrent_hash"] == "a" * 40
    assert "source_torrent_path" in evidence["source"]
    assert "source_wait" in evidence["source"]
    assert evidence["source"]["qbit_closure"]["injection"]["hash"] == "a" * 40
    assert evidence["source"]["qbit_closure"]["injection"]["save_path"] == "/downloads"
    assert evidence["source"]["qbit_closure"]["injection"]["verification_attempts"] == 2
    assert evidence["source"]["qbit_closure"]["wait"]["complete"] is True
    assert evidence["source"]["qbit_closure"]["wait"]["query"]["torrent_hash"] == "a" * 40
    assert evidence["target"]["injection_verified"] is True
    assert evidence["target"]["prepared"] is True
    assert evidence["target"]["uploaded"] is True
    assert evidence["target"]["downloaded"] is True
    assert evidence["target"]["injected"] is True
    assert evidence["target"]["seeding"] is True
    assert evidence["target"]["seeding_verified"] is True
    assert evidence["target"]["injected_torrent_hash"] == "b" * 40
    assert evidence["target"]["qbit_closure"]["injection"]["hash"] == "b" * 40
    assert evidence["target"]["qbit_closure"]["injection"]["category"] == "MTEAM"
    assert evidence["target"]["qbit_closure"]["injection"]["tags"] == "retorrent"
    assert evidence["target"]["qbit_closure"]["wait"]["complete"] is True
    assert evidence["target"]["qbit_closure"]["wait"]["query"]["torrent_hash"] == "b" * 40


def test_pipeline_evidence_reports_resume_sources() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "result": {"path": "/tmp/U2-60635.torrent", "reused": True}},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "verified_in_client": True}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": True, "matches": [{"hash": "a" * 40, "content_path": "/downloads/Name"}]}},
        {"stage": "match", "ok": True, "result": {"matches": []}},
        {"stage": "target-prepare", "ok": True, "result": {"package_dir": "/tmp/package", "reused": True, "rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "reused": True},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, None)
    evidence = ptcli_cli._pipeline_evidence(closure)
    summary = ptcli_cli._pipeline_run_summary(stages, True, [], closure, evidence)

    assert closure["complete"] is True
    assert evidence["resume"] == {"used": True, "source_torrent_file": True, "target_package": True, "uploaded_torrent_file": True}
    assert evidence["source"]["mode"] == "resumed_torrent"
    assert evidence["source"]["source_torrent_reused"] is True
    assert evidence["target"]["package_reused"] is True
    assert evidence["target"]["uploaded_torrent_reused"] is True
    assert summary["resume"]["used"] is True


def test_pipeline_closure_blocks_existing_path_without_qbit_match() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": []}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", None, "/tmp/target.torrent")

    assert closure["complete"] is False
    assert closure["blockers"] == ["source.ready"]
    assert closure["source"]["ready"] is False
    assert closure["source"]["matched"] is False


def test_pipeline_closure_rejects_empty_qbit_match_evidence() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{}]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", None, "/tmp/target.torrent")

    assert closure["complete"] is False
    assert closure["blockers"] == ["source.ready"]
    assert closure["source"]["matched"] is False


def test_resolve_default_qbit_client() -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit", "qbit_url": "http://127.0.0.1"}},
    }

    name, client_config = resolve_client_config(config, "default")

    assert name == "qbittorrent"
    assert client_config["torrent_client"] == "qbit"


def test_resolve_rejects_non_qbit_client() -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "rtorrent"},
        "TORRENT_CLIENTS": {"rtorrent": {"torrent_client": "rtorrent"}},
    }

    try:
        resolve_client_config(config, "default")
    except ValueError as exc:
        assert "qBittorrent only" in str(exc)
    else:
        raise AssertionError("non-qbit client should fail")


def test_summarize_torrent_from_dict() -> None:
    summary = summarize_torrent(
        {
            "name": "Movie.2024.1080p",
            "hash": "ABC123",
            "save_path": "/downloads",
            "content_path": "/downloads/Movie.2024.1080p",
            "size": "42",
            "progress": "1",
            "state": "uploading",
            "category": "pt",
            "tags": "mteam",
            "tracker": "https://tracker.example/announce",
        }
    )

    assert summary.name == "Movie.2024.1080p"
    assert summary.hash == "ABC123"
    assert summary.size == 42
    assert summary.progress == 1.0


def test_match_torrents_by_content_path() -> None:
    first = summarize_torrent({"name": "Movie.2024", "hash": "A", "content_path": "/downloads/Movie.2024"})
    second = summarize_torrent({"name": "Other.2024", "hash": "B", "content_path": "/downloads/Other.2024"})

    matches = match_torrents([first, second], "/downloads/Movie.2024")

    assert [match.hash for match in matches] == ["A"]


def test_match_torrents_avoids_loose_substring_false_positive() -> None:
    false_positive = summarize_torrent({"name": "Someone.2024", "hash": "A", "content_path": "/downloads/Someone.2024"})
    release_match = summarize_torrent({"name": "One.2024.1080p", "hash": "B", "content_path": "/downloads/One.2024.1080p"})

    matches = match_torrents([false_positive, release_match], "/downloads/One")

    assert [match.hash for match in matches] == ["B"]


def test_inspect_reports_missing_config_as_json(capsys) -> None:
    code = main(["inspect", "--config", "/missing/config.py", "--json"])

    assert code == 2
    out = capsys.readouterr().out
    assert '"status": "error"' in out
    assert "Config file not found" in out


def test_extract_torrent_id_from_supported_inputs() -> None:
    assert extract_torrent_id("12345") == "12345"
    assert extract_torrent_id("https://u2.dmhy.org/details.php?id=60635&hit=1") == "60635"
    assert extract_torrent_id("https://kp.m-team.cc/details/111") == "111"


def test_source_info_from_tuple_includes_meta_side_effects() -> None:
    meta = create_source_meta()
    meta["douban_id"] = "1291546"
    meta["douban_url"] = "https://movie.douban.com/subject/1291546/"

    info = source_info_from_tuple("U2", "60635", (1234567, 98765, "Release Name", "a" * 40, "desc"), meta)

    assert info.tracker == "U2"
    assert info.torrent_id == "60635"
    assert info.imdb_id == 1234567
    assert info.tmdb_id == 98765
    assert info.name == "Release Name"
    assert info.description_length == 4
    assert info.douban_id == "1291546"


@pytest.mark.asyncio
async def test_mteam_source_info_uses_ptcli_api_client(monkeypatch) -> None:
    closed = {"value": False}

    class FakeMTeamApiClient:
        def __init__(self, config):
            assert config["TRACKERS"]["MTEAM"]["api_key"] == "fake"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed["value"] = True

        async def torrent_detail(self, torrent_id):
            assert torrent_id == "60635"
            return {
                "imdb": "https://www.imdb.com/title/tt1234567/",
                "tmdb": "https://www.themoviedb.org/movie/76543",
                "douban": "https://movie.douban.com/subject/1291546/",
                "name": "MTEAM.Name.2024",
                "hash": "a" * 40,
                "descr": "desc",
            }

    monkeypatch.setattr(ptcli_source, "MTeamApiClient", FakeMTeamApiClient)

    info = await ptcli_source.fetch_source_info({"TRACKERS": {"MTEAM": {"api_key": "fake"}}}, "MTEAM", "60635")

    assert info.name == "MTEAM.Name.2024"
    assert info.imdb_id == 1234567
    assert info.tmdb_id == 76543
    assert info.douban_id == "1291546"
    assert info.description_length == 4
    assert closed["value"] is True


@pytest.mark.asyncio
async def test_mteam_source_info_closes_api_client_on_error(monkeypatch) -> None:
    closed = {"value": False}

    class FakeMTeamApiClient:
        def __init__(self, config):
            _ = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed["value"] = True

        async def torrent_detail(self, torrent_id):
            _ = torrent_id
            raise RuntimeError("metadata failed")

    monkeypatch.setattr(ptcli_source, "MTeamApiClient", FakeMTeamApiClient)

    with pytest.raises(RuntimeError, match="metadata failed"):
        await ptcli_source.fetch_source_info({"TRACKERS": {"MTEAM": {"api_key": "fake"}}}, "MTEAM", "60635")

    assert closed["value"] is True


@pytest.mark.asyncio
async def test_mteam_source_download_uses_ptcli_api_client(monkeypatch, tmp_path) -> None:
    class FakeMTeamApiClient:
        def __init__(self, config):
            assert config["TRACKERS"]["MTEAM"]["api_key"] == "fake"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def download_torrent(self, torrent_id, destination):
            assert torrent_id == "60635"
            await asyncio.to_thread(Path(destination).write_bytes, b"d4:infod")

    monkeypatch.setattr(ptcli_source, "MTeamApiClient", FakeMTeamApiClient)

    path = await ptcli_source.download_source_torrent({"TRACKERS": {"MTEAM": {"api_key": "fake"}}}, "MTEAM", "60635", str(tmp_path))

    assert path == tmp_path / "MTEAM-60635.torrent"
    assert await asyncio.to_thread(path.read_bytes) == b"d4:infod"


def test_flow_check_ready_for_u2_to_mteam(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {
            "U2": {"passkey": "u2-passkey"},
            "MTEAM": {"api_key": "mteam-api"},
        },
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }

    payload = build_flow_check(config, "U2", "https://u2.dmhy.org/details.php?id=60635", "MTEAM", "default", base_dir=str(tmp_path))

    assert payload["ready"] is True
    assert payload["requested_source_id"] == "https://u2.dmhy.org/details.php?id=60635"
    assert payload["input_source_id"] == "https://u2.dmhy.org/details.php?id=60635"
    assert payload["source_torrent_id"] == "60635"


def test_flow_check_ready_for_enabled_chinese_nexus_source(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "PTER.txt").write_text("uid=1;", encoding="utf-8")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {
            "PTER": {"passkey": "pter-passkey"},
            "MTEAM": {"api_key": "mteam-api"},
        },
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }

    payload = build_flow_check(config, "PTER", "123", "MTEAM", "default", base_dir=str(tmp_path))

    assert payload["ready"] is True
    assert payload["source_tracker"] == "PTER"
    assert any(check["name"] == "PTER.passkey" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "PTER.cookie" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "reference_flow" and check["ok"] is True for check in payload["checks"])


def test_flow_check_ready_for_ttg_to_mteam_uses_announce_url(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "TTG.txt").write_text("uid=1;", encoding="utf-8")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {
            "TTG": {"announce_url": "https://totheglory.im/announce/passkey"},
            "MTEAM": {"api_key": "mteam-api"},
        },
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }

    payload = build_flow_check(config, "TTG", "123", "MTEAM", "default", base_dir=str(tmp_path))

    assert payload["ready"] is True
    assert any(check["name"] == "TTG.passkey" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "TTG.cookie" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "reference_flow" and check["ok"] is True for check in payload["checks"])


def test_flow_check_reports_missing_cookie(tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {
            "CHD": {"passkey": "chd-passkey"},
            "MTEAM": {"api_key": "mteam-api"},
        },
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }

    payload = build_flow_check(config, "CHD", "123", "MTEAM", "default", base_dir=str(tmp_path))

    assert payload["ready"] is False
    assert any(check["name"] == "CHD.cookie" and check["ok"] is False for check in payload["checks"])


def test_flow_check_blocks_unsupported_tracker_before_config(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: (_ for _ in ()).throw(AssertionError("unsupported tracker must not read config")))

    code = main(["flow-check", "--from", "PTP", "--source-id", "123", "--to", "MTEAM", "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["command"] == "flow-check"
    assert payload["source_tracker"] == "PTP"
    assert payload["source_torrent_id"] == "123"
    assert payload["target_trackers"] == ["MTEAM"]
    assert payload["blockers"] == ["Unsupported tracker(s) for focused CLI scope: PTP"]


def test_doctor_reports_ready_live_checklist(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    content_path = tmp_path / "downloads" / "Name"
    content_path.mkdir(parents=True)
    target_torrent = make_mteam_safe_torrent(tmp_path, "target")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, str(content_path), str(tmp_path / "target"), accept_rules=True)

    payload = build_doctor_check(
        config,
        source_tracker="U2",
        source_id="60635",
        target_trackers="MTEAM",
        client="default",
        base_dir=str(tmp_path),
        content_path=str(content_path),
        package_dir=package["package_dir"],
        target_torrent_file=target_torrent,
        accept_rules=True,
        target_execute=True,
        confirm_upload=True,
        download_uploaded_torrent=True,
        inject_uploaded_torrent=True,
        uploaded_save_path=str(content_path),
        wait_uploaded_complete=True,
    )

    assert payload["ready"] is True
    assert payload["live_safe_to_attempt"] is True
    assert payload["package_preflight"]["status"] == "ready"
    assert payload["compliance"]["ready"] is True
    assert payload["compliance"]["rules_acknowledged"] is True
    assert payload["compliance"]["site_specific_rules_encoded"] is False
    assert payload["compliance"]["rule_obligations"]["acknowledged_count"] == 2
    assert payload["compliance"]["target_rule_obligation_review"]["ready"] is True
    assert any(check["name"] == "wait_uploaded_complete" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "rule_obligations" and check["ok"] is True for check in payload["checks"])


def test_doctor_auto_enables_uploaded_torrent_followup_for_live_closure(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    content_path = tmp_path / "downloads" / "Name"
    content_path.mkdir(parents=True)
    target_torrent = make_mteam_safe_torrent(tmp_path, "target")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), str(content_path), str(tmp_path / "target"), accept_rules=True)

    payload = build_doctor_check(
        config,
        source_tracker="U2",
        source_id="60635",
        target_trackers="MTEAM",
        client="default",
        base_dir=str(tmp_path),
        content_path=str(content_path),
        package_dir=package["package_dir"],
        target_torrent_file=target_torrent,
        accept_rules=True,
        target_execute=True,
        confirm_upload=True,
    )

    assert payload["ready"] is True
    assert payload["live_safe_to_attempt"] is True
    assert any(check["name"] == "download_uploaded_torrent" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "inject_uploaded_torrent" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "wait_uploaded_complete" and check["ok"] is True for check in payload["checks"])


def test_doctor_infers_uploaded_save_path_from_package_content(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    content_path = tmp_path / "downloads" / "Name"
    content_path.mkdir(parents=True)
    target_torrent = make_mteam_safe_torrent(tmp_path, "target")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), str(content_path), str(tmp_path / "target"), accept_rules=True)

    payload = build_doctor_check(
        config,
        source_tracker="U2",
        source_id="60635",
        target_trackers="MTEAM",
        client="default",
        base_dir=str(tmp_path),
        package_dir=package["package_dir"],
        target_torrent_file=target_torrent,
        accept_rules=True,
        target_execute=True,
        confirm_upload=True,
    )

    assert payload["ready"] is True
    assert payload["live_safe_to_attempt"] is True
    assert payload["effective_uploaded_save_path"] == str(content_path)
    assert any(check["name"] == "uploaded_save_path" and check["ok"] is True and str(content_path) in check["message"] for check in payload["checks"])


def test_doctor_summary_artifacts_include_effective_uploaded_save_path(tmp_path) -> None:
    content_path = tmp_path / "downloads" / "Name"
    content_path.mkdir(parents=True)
    args = argparse.Namespace(
        content_path=None,
        source_torrent_file=None,
        package_dir=None,
        target_torrent_file=None,
        uploaded_torrent_id=None,
        uploaded_torrent_file=None,
    )

    artifacts = ptcli_cli._doctor_summary_artifacts(args, {}, str(content_path))
    resume_state = ptcli_cli._doctor_resume_state(
        {"ready": True, "live_safe_to_attempt": True},
        artifacts,
        [],
        [{"stage": "doctor-live-probes", "command": "python3 ptcli.py doctor --connect-qbit"}],
    )

    assert artifacts["effective_uploaded_save_path"]["path"] == str(content_path)
    assert artifacts["effective_uploaded_save_path"]["exists"] is True
    assert artifacts["effective_uploaded_save_path"]["is_dir"] is True
    assert resume_state["artifacts"]["effective_uploaded_save_path"] is True


def test_doctor_blocks_live_upload_without_rule_obligations(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    content_path = tmp_path / "downloads" / "Name"
    content_path.mkdir(parents=True)
    target_torrent = make_mteam_safe_torrent(tmp_path, "target")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    legacy_stages = [
        {"stage": "rule-check", "ok": True, "result": {"ready": True, "source_tracker": "U2", "target_trackers": ["MTEAM"], "checks": []}},
        {"stage": "match", "ok": True, "result": {"count": 1, "matches": [{"content_path": str(content_path), "hash": "a" * 40}]}},
        {"stage": "target-dupe-check", "ok": True, "result": {"searched": True, "count": 0, "dupes": []}},
    ]
    package = write_mteam_prepare_package(source_info, ["MTEAM"], legacy_stages, str(content_path), str(tmp_path / "target"), accept_rules=True)

    payload = build_doctor_check(
        config,
        source_tracker="U2",
        source_id="60635",
        target_trackers="MTEAM",
        client="default",
        base_dir=str(tmp_path),
        content_path=str(content_path),
        package_dir=package["package_dir"],
        target_torrent_file=target_torrent,
        accept_rules=True,
        target_execute=True,
        confirm_upload=True,
        download_uploaded_torrent=True,
        inject_uploaded_torrent=True,
        uploaded_save_path=str(content_path),
        wait_uploaded_complete=True,
    )

    rule_obligations = next(check for check in payload["checks"] if check["name"] == "rule_obligations")
    assert payload["ready"] is False
    assert payload["live_safe_to_attempt"] is False
    assert rule_obligations["ok"] is False
    assert "Rule obligations are missing" in rule_obligations["message"]


def test_doctor_accepts_resume_files_for_live_closure(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    content_path = tmp_path / "downloads" / "Name"
    content_path.mkdir(parents=True)
    source_torrent = tmp_path / "U2-60635.torrent"
    source_torrent.write_bytes(b"d4:infod")
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), str(content_path), str(tmp_path / "target"), accept_rules=True)

    payload = build_doctor_check(
        config,
        source_tracker="U2",
        source_id="60635",
        target_trackers="MTEAM",
        client="default",
        base_dir=str(tmp_path),
        content_path=str(content_path),
        source_torrent_file=str(source_torrent),
        package_dir=package["package_dir"],
        uploaded_torrent_file=str(uploaded_torrent),
        accept_rules=True,
        target_execute=True,
        confirm_upload=True,
        inject_uploaded_torrent=True,
        uploaded_save_path=str(content_path),
        wait_uploaded_complete=True,
    )

    assert payload["ready"] is True
    assert payload["live_safe_to_attempt"] is True
    assert any(check["name"] == "source_torrent_file" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "uploaded_torrent_file" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "download_uploaded_torrent" and check["ok"] is True and "already available" in check["message"] for check in payload["checks"])


def test_doctor_surfaces_duplicate_package_blocker(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    content_path = tmp_path / "downloads" / "Name"
    content_path.mkdir(parents=True)
    target_torrent = make_mteam_safe_torrent(tmp_path, "target")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = [
        {"stage": "rule-check", "ok": True, "result": {"ready": True}},
        {"stage": "match", "ok": True, "result": {"count": 1, "matches": [{"content_path": str(content_path), "hash": "a" * 40}]}},
        {"stage": "target-dupe-check", "ok": True, "result": {"searched": True, "count": 1, "dupes": [{"name": "Existing"}]}},
    ]
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, str(content_path), str(tmp_path / "target"), accept_rules=True)

    payload = build_doctor_check(
        config,
        source_tracker="U2",
        source_id="60635",
        target_trackers="MTEAM",
        client="default",
        base_dir=str(tmp_path),
        content_path=str(content_path),
        package_dir=package["package_dir"],
        target_torrent_file=target_torrent,
        accept_rules=True,
        target_execute=True,
        confirm_upload=True,
        download_uploaded_torrent=True,
        inject_uploaded_torrent=True,
        uploaded_save_path=str(content_path),
        wait_uploaded_complete=True,
    )

    target_package = next(check for check in payload["checks"] if check["name"] == "target_package")
    assert payload["ready"] is False
    assert target_package["ok"] is False
    assert "duplicate_check" in target_package["message"]
    assert "found 1" in target_package["message"]


def test_doctor_reports_blockers_for_missing_package(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }

    payload = build_doctor_check(
        config,
        source_tracker="U2",
        source_id="60635",
        target_trackers="MTEAM",
        client="default",
        base_dir=str(tmp_path),
        accept_rules=False,
        target_execute=True,
        confirm_upload=False,
    )

    assert payload["ready"] is False
    assert payload["live_safe_to_attempt"] is False
    assert any(check["name"] == "rules_acknowledged" and check["ok"] is False for check in payload["checks"])
    assert any(check["name"] == "target_package" and check["ok"] is False for check in payload["checks"])


def test_doctor_runtime_check_reports_ptcli_dependencies(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }

    payload = build_doctor_check(
        config,
        source_tracker="U2",
        source_id="60635",
        target_trackers="MTEAM",
        client="default",
        base_dir=str(tmp_path),
        check_runtime=True,
    )

    runtime_check = next(check for check in payload["checks"] if check["name"] == "runtime.ptcli_dependencies")
    assert runtime_check["ok"] is True
    assert any(item["module"] == "qbittorrentapi" and item["available"] is True for item in runtime_check["required"])
    assert runtime_check["legacy_optional"]["message"] == "Legacy Web UI/Discord/client dependencies are not required for ptcli."


def test_doctor_runtime_check_blocks_missing_ptcli_dependency(monkeypatch, tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    original_find_spec = ptcli_doctor.importlib.util.find_spec

    def fake_find_spec(module: str):
        if module == "qbittorrentapi":
            return None
        return original_find_spec(module)

    monkeypatch.setattr(ptcli_doctor.importlib.util, "find_spec", fake_find_spec)

    payload = build_doctor_check(
        config,
        source_tracker="U2",
        source_id="60635",
        target_trackers="MTEAM",
        client="default",
        base_dir=str(tmp_path),
        check_runtime=True,
    )

    runtime_check = next(check for check in payload["checks"] if check["name"] == "runtime.ptcli_dependencies")
    assert runtime_check["ok"] is False
    assert "qbittorrent-api" in runtime_check["message"]


def test_doctor_runtime_check_blocks_live_safe_when_requested(monkeypatch, tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    content_path = tmp_path / "downloads" / "Name"
    content_path.mkdir(parents=True)
    target_torrent = make_mteam_safe_torrent(tmp_path, "target")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), str(content_path), str(tmp_path / "target"), accept_rules=True)
    original_find_spec = ptcli_doctor.importlib.util.find_spec

    def fake_find_spec(module: str):
        if module == "qbittorrentapi":
            return None
        return original_find_spec(module)

    monkeypatch.setattr(ptcli_doctor.importlib.util, "find_spec", fake_find_spec)

    payload = build_doctor_check(
        config,
        source_tracker="U2",
        source_id="60635",
        target_trackers="MTEAM",
        client="default",
        base_dir=str(tmp_path),
        content_path=str(content_path),
        package_dir=package["package_dir"],
        target_torrent_file=target_torrent,
        accept_rules=True,
        target_execute=True,
        confirm_upload=True,
        download_uploaded_torrent=True,
        inject_uploaded_torrent=True,
        uploaded_save_path=str(content_path),
        wait_uploaded_complete=True,
        check_runtime=True,
    )

    assert payload["ready"] is False
    assert payload["live_safe_to_attempt"] is False
    assert any(check["name"] == "runtime.ptcli_dependencies" and check["ok"] is False for check in payload["checks"])


def test_doctor_command_outputs_json(monkeypatch, tmp_path, capsys) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    code = main(["doctor", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"flow_check"' in out
    assert '"live_safe_to_attempt"' in out


def test_doctor_blocks_unsupported_tracker_before_config(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: (_ for _ in ()).throw(AssertionError("unsupported tracker must not read config")))

    code = main(["doctor", "--from", "PTP", "--source-id", "123", "--to", "MTEAM", "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["command"] == "doctor"
    assert payload["source_tracker"] == "PTP"
    assert payload["source_torrent_id"] == "123"
    assert payload["target_trackers"] == ["MTEAM"]
    assert payload["ready"] is False
    assert payload["live_safe_to_attempt"] is False
    assert payload["checks"] == [
        {
            "name": "tracker_scope",
            "ok": False,
            "message": "Unsupported tracker(s) for focused CLI scope: PTP",
        }
    ]


def test_doctor_target_execute_not_live_safe_returns_nonzero(monkeypatch, tmp_path, capsys) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    code = main(["doctor", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--target-execute", "--json"])

    assert code == 1
    out = capsys.readouterr().out
    assert '"live_safe_to_attempt": false' in out
    assert '"live_upload_confirmation"' in out


def test_doctor_target_execute_live_safe_returns_zero(monkeypatch, tmp_path, capsys) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    content_path = tmp_path / "downloads" / "Name"
    content_path.mkdir(parents=True)
    target_torrent = make_mteam_safe_torrent(tmp_path, "target")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), str(content_path), str(tmp_path / "target"), accept_rules=True)
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    code = main(
        [
            "doctor",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            str(content_path),
            "--package-dir",
            package["package_dir"],
            "--target-torrent-file",
            str(target_torrent),
            "--accept-rules",
            "--target-execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            str(content_path),
            "--wait-uploaded-complete",
            "--write-summary",
            "--summary-output-dir",
            str(tmp_path / "summary"),
            "--json",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert '"ready": true' in out
    assert '"live_safe_to_attempt": true' in out
    summary_payload = json.loads((tmp_path / "summary" / "ptcli-doctor-summary.json").read_text(encoding="utf-8"))
    commands = {command["stage"]: command["command"] for command in summary_payload["recommended_commands"]}
    assert summary_payload["schema_version"] == 1
    assert summary_payload["kind"] == "ptcli.doctor.live_readiness"
    assert summary_payload["artifacts"]["content_path"]["exists"] is True
    assert summary_payload["artifacts"]["package_dir"]["is_dir"] is True
    assert summary_payload["artifacts"]["target_torrent_file"]["is_file"] is True
    assert summary_payload["artifacts"]["flow_check_ready"] is True
    assert summary_payload["artifacts"]["rule_check_ready"] is True
    assert summary_payload["artifacts"]["rules_acknowledged"] is True
    assert summary_payload["artifacts"]["rule_obligations"]["ready"] is True
    assert summary_payload["artifacts"]["target_rule_obligations"]["ready"] is True
    assert summary_payload["artifacts"]["target_package_preflight_ready"] is True
    assert summary_payload["compliance"]["ready"] is True
    assert summary_payload["compliance"]["site_specific_rules_encoded"] is False
    assert summary_payload["failed_check_names"] == []
    assert "--connect-qbit" in commands["doctor-live-probes"]
    assert "--probe-source" in commands["doctor-live-probes"]
    assert "--probe-target" in commands["doctor-live-probes"]
    assert "--target-execute --confirm-upload" in commands["pipeline-live"]
    assert str(content_path) in commands["pipeline-live"]
    assert str(target_torrent) in commands["pipeline-live"]
    assert summary_payload["resume_state"]["ready"] is True
    assert summary_payload["resume_state"]["live_safe_to_attempt"] is True
    assert summary_payload["resume_state"]["resume_available"] is True
    assert summary_payload["resume_state"]["next_stage"] == "pipeline-live"
    assert summary_payload["resume_state"]["next_command"] == commands["pipeline-live"]
    assert summary_payload["resume_state"]["artifacts"]["content_path"] is True
    assert summary_payload["resume_state"]["artifacts"]["package_dir"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_torrent_file"] is True
    assert summary_payload["resume_state"]["artifacts"]["flow_check_ready"] is True
    assert summary_payload["resume_state"]["artifacts"]["rule_check_ready"] is True
    assert summary_payload["resume_state"]["artifacts"]["rules_acknowledged"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_rule_obligations"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_package_preflight_ready"] is True


def test_doctor_uploaded_torrent_id_resume_is_live_safe(monkeypatch, tmp_path, capsys) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    content_path = tmp_path / "downloads" / "Name"
    content_path.mkdir(parents=True)
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), str(content_path), str(tmp_path / "target"), accept_rules=True)
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    code = main(
        [
            "doctor",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            str(content_path),
            "--package-dir",
            package["package_dir"],
            "--accept-rules",
            "--target-execute",
            "--confirm-upload",
            "--uploaded-torrent-id",
            "999",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            str(content_path),
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--uploaded-paused",
            "--wait-uploaded-complete",
            "--write-summary",
            "--summary-output-dir",
            str(tmp_path / "summary"),
            "--json",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert '"live_safe_to_attempt": true' in out
    summary_payload = json.loads((tmp_path / "summary" / "ptcli-doctor-summary.json").read_text(encoding="utf-8"))
    commands = {command["stage"]: command["command"] for command in summary_payload["recommended_commands"]}
    assert summary_payload["inputs"]["uploaded_torrent_id"] == "999"
    assert summary_payload["artifacts"]["uploaded_torrent_id"] == "999"
    assert "--connect-qbit" in commands["doctor-live-probes"]
    assert "--probe-source" in commands["doctor-live-probes"]
    assert "--probe-target" in commands["doctor-live-probes"]
    assert "pipeline-live" not in commands
    assert "--uploaded-torrent-id 999" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-save-path" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-category MTEAM" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-tags retorrent" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-paused" in commands["resume-uploaded-torrent-download"]
    assert summary_payload["resume_state"]["live_safe_to_attempt"] is True
    assert summary_payload["resume_state"]["next_stage"] == "resume-uploaded-torrent-download"
    assert summary_payload["resume_state"]["next_command"] == commands["resume-uploaded-torrent-download"]
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_id"] is True


def test_doctor_command_writes_summary_json(monkeypatch, tmp_path, capsys) -> None:
    source_url = "https://u2.dmhy.org/details.php?id=60635&hit=1"
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    summary_dir = tmp_path / "doctor-summary"
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    code = main(
        [
            "doctor",
            "--from",
            "U2",
            "--source-id",
            source_url,
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--write-summary",
            "--summary-output-dir",
            str(summary_dir),
            "--json",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert '"summary_file"' in out
    summary_path = summary_dir / "ptcli-doctor-summary.json"
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["kind"] == "ptcli.doctor.live_readiness"
    assert payload["summary_file"] == str(summary_path)
    assert payload["requested_source_id"] == source_url
    assert payload["input_source_id"] == source_url
    assert payload["source_torrent_id"] == "60635"
    assert payload["flow_check"]["ready"] is True
    assert payload["flow_check"]["requested_source_id"] == source_url
    assert payload["flow_check"]["source_torrent_id"] == "60635"
    assert payload["live_safe_to_attempt"] is False
    assert "target_package" in payload["failed_check_names"]
    assert payload["recommended_commands"][0]["stage"] == "doctor-retry"
    assert payload["resume_state"]["ready"] is False
    assert payload["resume_state"]["live_safe_to_attempt"] is False
    assert payload["resume_state"]["resume_available"] is False
    assert payload["resume_state"]["next_stage"] == "doctor-retry"
    assert payload["resume_state"]["next_command"] == payload["recommended_commands"][0]["command"]
    assert "target_package" in payload["resume_state"]["failed_check_names"]
    assert isinstance(payload["checks"], list)


def test_doctor_command_can_probe_qbit_connection(monkeypatch, tmp_path, capsys) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_qbit_connection_check(_config, client_name):
        return {"name": "qbit.connection", "ok": True, "message": f"connected: {client_name}"}

    monkeypatch.setattr(ptcli_cli, "_qbit_connection_check", fake_qbit_connection_check)

    code = main(["doctor", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--connect-qbit", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"name": "qbit.connection"' in out
    assert "connected: default" in out


def test_doctor_command_can_check_runtime(monkeypatch, tmp_path, capsys) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    code = main(["doctor", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--check-runtime", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    runtime_check = next(check for check in payload["checks"] if check["name"] == "runtime.ptcli_dependencies")
    assert runtime_check["ok"] is True
    assert payload["ready"] is False


def test_doctor_command_can_probe_source_and_target(monkeypatch, tmp_path, capsys) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_source_connection_check(_config, source_tracker, source_id, base_dir):
        _ = base_dir
        return {
            "check": {"name": "source.connection", "ok": True, "message": f"source ok: {source_tracker} {source_id}"},
            "source": {"tracker": source_tracker, "torrent_id": source_id, "imdb_id": 1234567},
        }

    async def fake_target_connection_check(_config, target_trackers, source_info):
        return {"name": "target.connection", "ok": True, "message": f"target ok: {target_trackers} tt{source_info['imdb_id']}"}

    monkeypatch.setattr(ptcli_cli, "_source_connection_check", fake_source_connection_check)
    monkeypatch.setattr(ptcli_cli, "_target_connection_check", fake_target_connection_check)

    code = main(["doctor", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--probe-source", "--probe-target", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"name": "source.connection"' in out
    assert '"name": "target.connection"' in out
    assert "target ok: MTEAM tt1234567" in out


@pytest.mark.asyncio
async def test_qbit_connection_check_reports_failure() -> None:
    payload = await ptcli_cli._qbit_connection_check({"TORRENT_CLIENTS": {}}, "default")

    assert payload["name"] == "qbit.connection"
    assert payload["ok"] is False
    assert "DEFAULT" in payload["message"] or "Torrent client" in payload["message"]


@pytest.mark.asyncio
async def test_target_connection_check_requires_source_info() -> None:
    payload = await ptcli_cli._target_connection_check({}, "MTEAM", None)

    assert payload["name"] == "target.connection"
    assert payload["ok"] is False
    assert "Source metadata" in payload["message"]


@pytest.mark.asyncio
async def test_pipeline_skips_match_without_path(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--json"])

    payload = await ptcli_cli.pipeline_payload(args)

    assert payload["ready"] is True
    assert any(stage["stage"] == "source-download" and stage["skipped"] is True for stage in payload["stages"])
    match_stage = next(stage for stage in payload["stages"] if stage["stage"] == "match")
    assert match_stage["skipped"] is True
    assert payload["requested_actions"]["download_source"] is False
    assert payload["requested_actions"]["inject_source"] is False
    assert payload["requested_actions"]["wait_complete"] is False
    assert payload["effective_actions"]["live_target_upload"] is False
    assert payload["effective_actions"]["download_source"] is False
    assert payload["effective_actions"]["inject_source"] is False
    assert payload["effective_actions"]["wait_complete"] is False
    assert payload["summary"]["requested_actions"] == payload["requested_actions"]
    assert payload["summary"]["effective_actions"] == payload["effective_actions"]


@pytest.mark.asyncio
async def test_pipeline_keeps_source_info_error(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, _tracker, _source_id, base_dir=None):
        _ = base_dir
        raise ValueError("source unavailable")

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--json"])

    payload = await ptcli_cli.pipeline_payload(args)

    assert payload["status"] == "ok"
    assert payload["ready"] is False
    assert payload["blockers"] == []
    source_info_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-info")
    assert source_info_stage["ok"] is False
    assert source_info_stage["error"] == "source unavailable"


@pytest.mark.asyncio
async def test_pipeline_action_failure_reports_top_level_blockers(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--inject-source", "--json"])

    payload = await ptcli_cli.pipeline_payload(args)

    assert payload["status"] == "blocked"
    assert payload["ready"] is False
    assert payload["blockers"] == ["inject-source: --save-path is required when --inject-source is used."]


@pytest.mark.asyncio
async def test_pipeline_rejects_empty_source_info(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (None, None, None, None, None), {})

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--json"])

    payload = await ptcli_cli.pipeline_payload(args)

    assert payload["ready"] is False
    source_info_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-info")
    assert source_info_stage["ok"] is False
    assert "no usable identifiers" in source_info_stage["error"]


@pytest.mark.asyncio
async def test_pipeline_download_source_skips_when_source_info_fails(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (None, None, None, None, None), {})

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    parser = build_parser()
    args = parser.parse_args(
        ["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--download-source", "--json"]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    download_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-download")
    assert download_stage["ok"] is False
    assert download_stage["skipped"] is True


@pytest.mark.asyncio
async def test_pipeline_download_source_requires_rule_check_ready(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(*_args, **_kwargs):
        raise AssertionError("source torrent download must not run before executable rule check is ready")

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    parser = build_parser()
    args = parser.parse_args(
        ["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--download-source", "--json"]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    download_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-download")
    assert download_stage["ok"] is False
    assert download_stage["skipped"] is True
    assert "executable rule-check" in download_stage["message"]
    assert payload["blockers"] == [f"source-download: {download_stage['message']}"]


@pytest.mark.asyncio
async def test_pipeline_download_source_runs_after_prereqs(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--accept-rules",
            "--output-dir",
            "source-out",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    download_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-download")
    assert download_stage["ok"] is True
    assert download_stage["result"]["path"].endswith("source-out/U2-60635.torrent")
    assert download_stage["result"]["exists"] is True
    assert download_stage["result"]["size_bytes"] == len(b"d4:infod")
    assert len(download_stage["result"]["sha1"]) == 40


@pytest.mark.asyncio
async def test_pipeline_inject_source_requires_save_path(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    parser = build_parser()
    args = parser.parse_args(
        ["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--download-source", "--inject-source", "--json"]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    inject_stage = next(stage for stage in payload["stages"] if stage["stage"] == "inject-source")
    assert inject_stage["ok"] is False
    assert "--save-path is required" in inject_stage["message"]


@pytest.mark.asyncio
async def test_pipeline_inject_source_runs_after_download(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name)
        return {
            "client": "qbittorrent",
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
            "verified_in_client": True,
        }

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads",
            "--qbit-category",
            "pt",
            "--qbit-tags",
            "U2",
            "--paused",
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    inject_stage = next(stage for stage in payload["stages"] if stage["stage"] == "inject-source")
    assert inject_stage["ok"] is True
    assert inject_stage["result"]["save_path"] == "/downloads"
    assert inject_stage["result"]["category"] == "pt"
    assert inject_stage["result"]["tags"] == "U2"
    assert inject_stage["result"]["paused"] is True


@pytest.mark.asyncio
async def test_pipeline_inject_source_reuses_existing_source_torrent(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    source_torrent = tmp_path / "U2-60635.torrent"
    source_torrent.write_bytes(b"d4:infod")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(*_args, **_kwargs):
        raise AssertionError("source download must not run when --source-torrent-file is provided")

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, category, tags, paused)
        return {"client": "qbittorrent", "torrent_path": torrent_path, "save_path": save_path, "hash": "a" * 40, "verified_in_client": True}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--source-torrent-file",
            str(source_torrent),
            "--inject-source",
            "--save-path",
            "/downloads",
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    download_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-download")
    inject_stage = next(stage for stage in payload["stages"] if stage["stage"] == "inject-source")
    assert download_stage["ok"] is True
    assert download_stage["result"]["reused"] is True
    assert download_stage["result"]["path"] == str(source_torrent)
    assert download_stage["result"]["exists"] is True
    assert download_stage["result"]["size_bytes"] == len(b"d4:infod")
    assert len(download_stage["result"]["sha1"]) == 40
    assert inject_stage["ok"] is True
    assert inject_stage["result"]["torrent_path"] == str(source_torrent)


@pytest.mark.asyncio
async def test_pipeline_inject_source_requires_client_verification(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, torrent_path, save_path, category, tags, paused)
        return {"client": "qbittorrent", "hash": "a" * 40, "verified_in_client": False}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads",
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    inject_stage = next(stage for stage in payload["stages"] if stage["stage"] == "inject-source")
    assert inject_stage["ok"] is False
    assert "not verified" in inject_stage["error"]
    assert any("injected source torrent" in blocker for blocker in payload["blockers"])
    assert any("--inject-source --save-path" in action for action in payload["next_actions"])


@pytest.mark.asyncio
async def test_pipeline_wait_complete_runs_after_inject(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    source_hash = "b" * 40

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, torrent_path, category, tags, paused)
        return {"client": "qbittorrent", "save_path": save_path, "verified_in_client": True}

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, timeout, interval)
        assert torrent_hash == source_hash
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": content_path, "hash": torrent_hash}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads",
            "--wait-complete",
            "--wait-timeout",
            "1",
            "--wait-interval",
            "0.1",
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    wait_stage = next(stage for stage in payload["stages"] if stage["stage"] == "wait-complete")
    source_download_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-download")
    assert wait_stage["ok"] is True
    assert wait_stage["result"]["complete"] is True
    assert wait_stage["result"]["matches"][0]["content_path"] == "/downloads"
    assert payload["source_torrent_hash"] == source_hash
    assert payload["evidence"]["source"]["source_torrent_path"] == source_download_stage["result"]["path"]
    assert source_download_stage["result"]["exists"] is True
    assert len(source_download_stage["result"]["sha1"]) == 40
    assert payload["evidence"]["source"]["source_wait"]["complete"] is True
    assert payload["summary"]["source"]["source_wait"]["matches"][0]["hash"] == source_hash


@pytest.mark.asyncio
async def test_pipeline_wait_complete_requires_matched_source_evidence(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    source_hash = "b" * 40

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, torrent_path, category, tags, paused)
        return {"client": "qbittorrent", "save_path": save_path, "hash": source_hash, "verified_in_client": True}

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, timeout, interval)
        assert torrent_hash == source_hash
        return {
            "client": "qbittorrent",
            "complete": True,
            "matches": [{"content_path": "/downloads/Other", "hash": source_hash}],
            "completion_verification": {
                "matched_count": 1,
                "complete_count": 1,
                "any_complete": True,
                "requested_hash_matched": True,
                "requested_content_path_matched": False,
            },
            "query": {"torrent_hash": torrent_hash, "content_path": content_path},
        }

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads",
            "--wait-complete",
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    wait_stage = next(stage for stage in payload["stages"] if stage["stage"] == "wait-complete")
    assert wait_stage["ok"] is False
    assert wait_stage["error"] == "qBittorrent task did not complete with matched source torrent evidence."
    assert payload["closure"]["source"]["complete"] is False
    assert payload["evidence"]["source"]["source_wait_evidence"] is False
    assert "wait-complete: qBittorrent task did not complete with matched source torrent evidence." in payload["blockers"]


@pytest.mark.asyncio
async def test_pipeline_wait_complete_prefers_injected_hash(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    metadata_hash = "a" * 40
    injected_hash = "c" * 40

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", metadata_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, torrent_path, category, tags, paused)
        return {"client": "qbittorrent", "save_path": save_path, "hash": injected_hash, "verified_in_client": True}

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, content_path, timeout, interval)
        assert torrent_hash == injected_hash
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": "/downloads/Name", "hash": torrent_hash}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads",
            "--wait-complete",
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    wait_stage = next(stage for stage in payload["stages"] if stage["stage"] == "wait-complete")
    assert wait_stage["ok"] is True
    assert payload["source_torrent_hash"] == injected_hash


@pytest.mark.asyncio
async def test_pipeline_wait_complete_uses_hash_from_real_injected_torrent(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    fake_client = FakeQbitClient()
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", None, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        content = tmp_path / "source-content.mkv"
        content.write_bytes(b"content")
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
        torrent.generate()
        torrent.write(str(torrent_path), overwrite=True)
        return torrent_path

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name)
        service = QbitReadOnlyService({}, qbit_client=fake_client)
        return await service.add_torrent_file(torrent_path, save_path, category=category, tags=tags, paused=paused)

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, content_path, timeout, interval)
        assert torrent_hash
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": "/downloads/Name", "hash": torrent_hash}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads",
            "--wait-complete",
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    assert payload["source_torrent_hash"]
    assert fake_client.added_kwargs["save_path"] == "/downloads"
    assert next(stage for stage in payload["stages"] if stage["stage"] == "wait-complete")["ok"] is True


@pytest.mark.asyncio
async def test_pipeline_wait_complete_uses_hash_from_downloaded_source_torrent(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    expected_hash = None

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", None, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        nonlocal expected_hash
        _ = (tracker, source_id, base_dir)
        content = tmp_path / "source-content.mkv"
        content.write_bytes(b"content")
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
        torrent.generate()
        torrent.write(str(torrent_path), overwrite=True)
        expected_hash = torrent.infohash
        return torrent_path

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, timeout, interval)
        assert content_path == "/downloads/Name"
        assert torrent_hash == expected_hash
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": content_path, "hash": torrent_hash}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--path",
            "/downloads/Name",
            "--wait-complete",
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    source_download_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-download")
    verify_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-torrent-verify")
    wait_stage = next(stage for stage in payload["stages"] if stage["stage"] == "wait-complete")
    assert source_download_stage["result"]["torrent_hash"] == expected_hash
    assert verify_stage["ok"] is True
    assert verify_stage["result"]["actual_hash"] == expected_hash
    assert payload["source_torrent_hash"] == expected_hash
    assert wait_stage["ok"] is True
    assert wait_stage["result"]["matches"][0]["hash"] == expected_hash


@pytest.mark.asyncio
async def test_pipeline_blocks_source_injection_when_downloaded_torrent_hash_mismatches_metadata(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        content = tmp_path / "source-content.mkv"
        content.write_bytes(b"content")
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
        torrent.generate()
        torrent.write(str(torrent_path), overwrite=True)
        assert str(torrent.infohash) != "a" * 40
        return torrent_path

    async def fake_inject_source_with_config(*_args, **_kwargs):
        raise AssertionError("source injection must not run when source torrent hash verification fails")

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads",
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    verify_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-torrent-verify")
    inject_stage = next(stage for stage in payload["stages"] if stage["stage"] == "inject-source")
    assert verify_stage["ok"] is False
    assert verify_stage["result"]["expected_hash"] == "a" * 40
    assert "source torrent hash mismatch" in verify_stage["result"]["blockers"][0]
    assert inject_stage["ok"] is False
    assert inject_stage["skipped"] is True
    assert payload["blockers"] == [
        "source-torrent-verify: Downloaded source torrent infohash does not match source tracker metadata.",
        f"source-torrent-verify: {verify_stage['result']['blockers'][0]}",
        "inject-source: Skipped because source torrent hash verification failed.",
    ]


@pytest.mark.asyncio
async def test_pipeline_blocks_source_injection_hash_mismatch(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    source_hash = None
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", None, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        nonlocal source_hash
        _ = (tracker, source_id, base_dir)
        content = tmp_path / "source-content.mkv"
        content.write_bytes(b"content")
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
        torrent.generate()
        torrent.write(str(torrent_path), overwrite=True)
        source_hash = str(torrent.infohash)
        return torrent_path

    async def fake_inject_source_with_config(_config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (torrent_path, category, tags, paused)
        wrong_hash = "f" * 40 if source_hash != "f" * 40 else "e" * 40
        return {"client": client_name, "hash": wrong_hash, "save_path": save_path, "verified_in_client": True}

    async def fake_wait_complete_with_config(*_args, **_kwargs):
        raise AssertionError("source wait must not run when injected source hash mismatches downloaded source torrent")

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads",
            "--wait-complete",
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    inject_stage = next(stage for stage in payload["stages"] if stage["stage"] == "inject-source")
    wait_stage = next(stage for stage in payload["stages"] if stage["stage"] == "wait-complete")
    assert inject_stage["ok"] is False
    assert inject_stage["error"] == "Injected source torrent infohash does not match downloaded source torrent."
    assert inject_stage["result"]["expected_hash"] == source_hash
    assert inject_stage["result"]["hash_matched"] is False
    assert "injected source torrent hash mismatch" in inject_stage["result"]["blockers"][0]
    assert wait_stage["ok"] is False
    assert wait_stage["skipped"] is True
    assert "inject-source: Injected source torrent infohash does not match downloaded source torrent." in payload["blockers"]


@pytest.mark.asyncio
async def test_pipeline_infers_content_path_from_completed_qbit_match(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, torrent_path, category, tags, paused)
        return {"client": "qbittorrent", "save_path": save_path, "verified_in_client": True}

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, content_path, torrent_hash, timeout, interval)
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads",
            "--wait-complete",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    assert payload["path"] == "/downloads/Name"
    assert payload["requested_path"] is None
    match_stage = next(stage for stage in payload["stages"] if stage["stage"] == "match")
    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert match_stage["result"]["path"] == "/downloads/Name"
    assert target_stage["ok"] is True
    assert target_stage["result"]["content_path"] == "/downloads/Name"


@pytest.mark.asyncio
async def test_pipeline_prepare_target_preview(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert target_stage["ok"] is False
    assert target_stage["result"]["target_tracker"] == "MTEAM"
    assert target_stage["result"]["verified_content"] is True
    assert target_stage["result"]["metadata"]["name"] == "Name"
    assert target_stage["result"]["files"]["preview"].endswith("mteam-prepare-preview.json")
    assert any("rules_acknowledged" in blocker for blocker in target_stage["result"]["blockers"])
    assert any("duplicate_check" in blocker for blocker in target_stage["result"]["blockers"])


@pytest.mark.asyncio
async def test_pipeline_prepare_target_blocks_mismatched_existing_qbit_content(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "b" * 40}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    verify_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-content-verify")
    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert verify_stage["ok"] is False
    assert verify_stage["result"]["expected_hash"] == "a" * 40
    assert verify_stage["result"]["matched_hashes"] == ["b" * 40]
    assert target_stage["ok"] is False
    assert target_stage["result"]["verified_content"] is False
    assert "source-content-verify: Matched qBittorrent content does not include the source tracker torrent hash." in payload["blockers"]


@pytest.mark.asyncio
async def test_pipeline_prepare_target_blocks_unhashed_existing_qbit_content(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    verify_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-content-verify")
    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert verify_stage["ok"] is False
    assert verify_stage["result"]["expected_hash"] == "a" * 40
    assert verify_stage["result"]["matched_hashes"] == []
    assert target_stage["ok"] is False
    assert target_stage["result"]["verified_content"] is False
    assert "source-content-verify: Matched qBittorrent content did not expose torrent hash evidence for source verification." in payload["blockers"]


@pytest.mark.asyncio
async def test_pipeline_check_dupes_runs_after_source_info(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name", "a" * 40, "desc"), {})

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 1, "dupes": [{"name": "Name"}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--check-dupes", "--json"])

    payload = await ptcli_cli.pipeline_payload(args)

    dupe_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-dupe-check")
    assert dupe_stage["ok"] is True
    assert dupe_stage["result"]["count"] == 1


@pytest.mark.asyncio
async def test_pipeline_prepare_target_gate_uses_dupe_check_and_rules_ack(monkeypatch, tmp_path) -> None:
    source_url = "https://u2.dmhy.org/details.php?id=60635&hit=1"
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            source_url,
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--write-summary",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    assert payload["requested_source_id"] == source_url
    assert payload["input_source_id"] == source_url
    assert payload["source_torrent_id"] == "60635"
    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert target_stage["result"]["upload_gate"]["ready"] is True
    assert target_stage["result"]["upload_gate"]["dupe_count"] == 0
    summary_path = Path(payload["summary_file"])
    assert summary_path == Path(target_stage["result"]["package_dir"]) / "ptcli-run-summary.json"
    summary_payload = json.loads(await asyncio.to_thread(summary_path.read_text, encoding="utf-8"))
    assert summary_payload["closure"] == payload["closure"]
    assert summary_payload["complete"] is False
    assert summary_payload["requested_source_id"] == source_url
    assert summary_payload["input_source_id"] == source_url
    assert summary_payload["source_torrent_id"] == "60635"
    assert summary_payload["closure"]["complete"] is False
    assert summary_payload["closure"]["blockers"] == ["target.uploaded", "target.downloaded", "target.injected"]
    assert summary_payload["requested_actions"] == payload["requested_actions"]
    assert summary_payload["effective_actions"] == payload["effective_actions"]
    assert summary_payload["summary"]["ready"] is True
    assert summary_payload["summary"]["requested_source_id"] == source_url
    assert summary_payload["summary"]["source_torrent_id"] == "60635"
    assert summary_payload["summary"]["target"]["ready"] is False
    assert summary_payload["summary"]["gates"]["rule_check"]["rules_acknowledged"] is True
    assert summary_payload["summary"]["gates"]["duplicate_check"]["ok"] is True
    assert summary_payload["summary"]["gates"]["upload_gate"]["ready"] is True
    assert summary_payload["schema_version"] == 1
    assert summary_payload["kind"] == "ptcli.pipeline.run_summary"
    assert summary_payload["summary_file"] == str(summary_path)
    assert summary_payload["stages"] == payload["stages"]
    assert summary_payload["artifacts"]["summary_file"] == str(summary_path)
    assert summary_payload["artifacts"]["target_package_dir"] == target_stage["result"]["package_dir"]
    assert summary_payload["artifacts"]["target_package_files"] == target_stage["result"]["files"]
    assert summary_payload["resume_state"]["complete"] is False
    assert summary_payload["resume_state"]["resume_available"] is False
    assert summary_payload["resume_state"]["next_stage"] is None
    assert summary_payload["resume_state"]["artifacts"]["target_package_dir"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_torrent_file"] is False
    resume_commands = {command["stage"]: command["command"] for command in summary_payload["resume_commands"]}
    assert "resume-target-upload" not in resume_commands
    assert summary_payload["next_actions"]


@pytest.mark.asyncio
async def test_pipeline_can_orchestrate_target_upload_and_qbit_inject(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    uploaded_hash = "d" * 40

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        uploaded_path = tmp_path / "MTEAM-999.torrent"
        uploaded_path.write_bytes(b"d4:infod")
        return {
            "status": "uploaded",
            "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_path)},
        }

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        injected_hash = uploaded_hash if "MTEAM" in str(torrent_path) else "a" * 40
        return {
            "hash": injected_hash,
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
            "verified_in_client": True,
        }

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {"client": client_name, "complete": True, "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval}, "matches": [{"hash": torrent_hash, "content_path": content_path}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--upload-target",
            "--target-torrent-file",
            str(torrent_file),
            "--target-execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            "/downloads/Name",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["status"] == "uploaded"
    assert upload_stage["result"]["uploaded_torrent_hash"] == uploaded_hash
    assert upload_stage["result"]["downloaded_torrent"]["hash"] == uploaded_hash
    assert upload_stage["result"]["injected_torrent"]["save_path"] == "/downloads/Name"
    assert upload_stage["result"]["injected_torrent"]["category"] == "MTEAM"
    assert payload["closure"]["complete"] is True
    assert payload["complete"] is True
    assert payload["closure"]["blockers"] == []
    assert payload["closure"]["source"]["ready"] is True
    assert payload["closure"]["source"]["matched"] is True
    assert payload["closure"]["source"]["content_path"] == "/downloads/Name"
    assert payload["closure"]["target"]["prepared"] is True
    assert payload["closure"]["target"]["uploaded"] is True
    assert payload["closure"]["target"]["downloaded"] is True
    assert payload["closure"]["target"]["injected"] is True
    assert payload["closure"]["target"]["uploaded_torrent_id"] == "999"
    assert payload["closure"]["target"]["uploaded_torrent_hash"] == uploaded_hash
    assert payload["closure"]["target"]["fresh_duplicate_check"] == {"searched": True, "query": {"imdb": "tt1234567"}, "count": 0, "dupes": []}
    assert payload["evidence"]["target"]["uploaded_torrent_id"] == "999"
    assert payload["evidence"]["target"]["fresh_duplicate_check"]["searched"] is True


@pytest.mark.asyncio
async def test_pipeline_closure_complete_for_full_retorrent_flow(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")
    uploaded_hash = "d" * 40
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        return {"hash": "a" * 40, "torrent_path": torrent_path, "save_path": save_path, "category": category, "tags": tags, "paused": paused, "verified_in_client": True}

    wait_calls = []

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, timeout, interval)
        wait_calls.append({"client_name": client_name, "content_path": content_path, "torrent_hash": torrent_hash})
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": content_path or "/downloads/Name", "hash": torrent_hash or "a" * 40}]}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        uploaded_path = tmp_path / "MTEAM-999.torrent"
        uploaded_path.write_bytes(b"d4:infod")
        return {"status": "uploaded", "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_path)}}

    async def fake_inject_uploaded_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        return {"hash": uploaded_hash, "torrent_path": torrent_path, "save_path": save_path, "category": category, "tags": tags, "paused": paused, "verified_in_client": True}

    calls = {"inject": 0}

    async def fake_inject_router(*args):
        calls["inject"] += 1
        if calls["inject"] == 1:
            return await fake_inject_source_with_config(*args)
        return await fake_inject_uploaded_with_config(*args)

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_router)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--config",
            str(tmp_path / "config.py"),
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads",
            "--qbit-category",
            "SOURCE",
            "--qbit-tags",
            "source-tag",
            "--paused",
            "--wait-complete",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--upload-target",
            "--target-torrent-file",
            str(torrent_file),
            "--target-execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--uploaded-paused",
            "--wait-uploaded-complete",
            "--write-summary",
            "--summary-output-dir",
            str(tmp_path / "summary"),
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    assert payload["closure"]["complete"] is True
    assert payload["closure"]["blockers"] == []
    assert payload["closure"]["source"]["downloaded"] is True
    assert payload["closure"]["source"]["injected"] is True
    assert payload["closure"]["source"]["complete"] is True
    assert payload["closure"]["target"]["uploaded_torrent_hash"] == uploaded_hash
    assert payload["closure"]["target"]["seeding"] is True
    assert payload["closure"]["target"]["uploaded_wait"]["complete"] is True
    assert wait_calls[-1]["torrent_hash"] == uploaded_hash
    assert wait_calls[-1]["content_path"] == "/downloads"
    summary_payload = json.loads(await asyncio.to_thread(Path(payload["summary_file"]).read_text, encoding="utf-8"))
    assert summary_payload["complete"] is True
    assert summary_payload["config"] == str(tmp_path / "config.py")
    assert summary_payload["base_dir"] == str(tmp_path)
    assert summary_payload["client"] == "default"
    assert summary_payload["qbit_options"] == {
        "source": {"category": "SOURCE", "tags": "source-tag", "paused": True},
        "uploaded": {"category": "MTEAM", "tags": "retorrent", "paused": True},
    }
    assert summary_payload["artifacts"]["source_torrent_file"].endswith("U2-60635.torrent")
    assert summary_payload["artifacts"]["source_torrent_hash"] == "a" * 40
    assert summary_payload["artifacts"]["source_save_path"] == "/downloads"
    assert summary_payload["artifacts"]["source_qbit_category"] == "SOURCE"
    assert summary_payload["artifacts"]["source_qbit_tags"] == "source-tag"
    assert summary_payload["artifacts"]["source_paused"] is True
    assert summary_payload["artifacts"]["source_hash_consistent"] is True
    assert summary_payload["artifacts"]["target_torrent_file"] == str(torrent_file)
    assert summary_payload["artifacts"]["target_package_dir"]
    assert summary_payload["artifacts"]["uploaded_torrent_file"] == str(tmp_path / "MTEAM-999.torrent")
    assert summary_payload["artifacts"]["uploaded_torrent_id"] == "999"
    assert summary_payload["artifacts"]["uploaded_torrent_hash"] == uploaded_hash
    assert summary_payload["artifacts"]["uploaded_qbit_category"] == "MTEAM"
    assert summary_payload["artifacts"]["uploaded_qbit_tags"] == "retorrent"
    assert summary_payload["artifacts"]["uploaded_paused"] is True
    assert summary_payload["artifacts"]["fresh_duplicate_check"] == {"searched": True, "query": {"imdb": "tt1234567"}, "count": 0, "dupes": []}
    assert summary_payload["artifacts"]["target_hash_consistent"] is True
    assert summary_payload["artifacts"]["target_duplicate_clean"] is True
    assert summary_payload["artifacts"]["target_rule_obligations"]["ready"] is True
    resume_commands = {command["stage"]: command["command"] for command in summary_payload["resume_commands"]}
    resume_argv = {command["stage"]: command["argv"] for command in summary_payload["resume_commands"]}
    summary_output_dir = str(tmp_path / "summary")
    summary_output_arg = f"--summary-output-dir {shlex.quote(summary_output_dir)}"
    config_arg = f"--config {shlex.quote(str(tmp_path / 'config.py'))}"
    base_dir_arg = f"--base-dir {shlex.quote(str(tmp_path))}"
    assert summary_payload["resume_state"]["complete"] is True
    assert summary_payload["resume_state"]["resume_available"] is True
    assert summary_payload["resume_state"]["next_stage"] is None
    assert summary_payload["resume_state"]["next_command"] is None
    assert summary_payload["resume_state"]["artifacts"] == {
        "source_torrent_file": True,
        "source_torrent_hash": True,
        "source_save_path": True,
        "source_qbit_category": True,
        "source_qbit_tags": True,
        "source_paused": True,
        "source_hash_consistent": True,
        "source_wait_evidence": True,
        "target_package_dir": True,
        "target_torrent_file": True,
        "uploaded_torrent_id": True,
        "uploaded_torrent_file": True,
        "uploaded_save_path": True,
        "uploaded_qbit_category": True,
        "uploaded_qbit_tags": True,
        "uploaded_paused": True,
        "uploaded_wait_evidence": True,
        "target_hash_consistent": True,
        "target_duplicate_clean": True,
        "target_rule_obligations": True,
    }
    assert summary_payload["artifacts"]["source_torrent_file"] in resume_commands["resume-source-torrent"]
    assert resume_argv["resume-source-torrent"][:3] == ["python3", "ptcli.py", "pipeline"]
    assert "--source-torrent-file" in resume_argv["resume-source-torrent"]
    assert "--client default" in resume_commands["resume-source-torrent"]
    assert "--qbit-category SOURCE" in resume_commands["resume-source-torrent"]
    assert "--qbit-tags source-tag" in resume_commands["resume-source-torrent"]
    assert "--paused" in resume_commands["resume-source-torrent"]
    assert config_arg in resume_commands["resume-source-torrent"]
    assert base_dir_arg in resume_commands["resume-source-torrent"]
    assert summary_output_arg in resume_commands["resume-source-torrent"]
    assert str(torrent_file) in resume_commands["resume-target-upload"]
    assert str(tmp_path / "MTEAM-999.torrent") in resume_commands["resume-uploaded-torrent"]
    assert resume_argv["resume-target-upload"][:3] == ["python3", "ptcli.py", "pipeline"]
    assert resume_argv["resume-uploaded-torrent"][:3] == ["python3", "ptcli.py", "target-upload"]
    assert str(tmp_path / "MTEAM-999.torrent") in resume_argv["resume-uploaded-torrent"]
    assert shlex.quote(summary_payload["artifacts"]["target_package_dir"]) in resume_commands["resume-target-upload"]
    assert summary_payload["artifacts"]["uploaded_save_path"] == "/downloads"
    assert "--uploaded-save-path /downloads" in resume_commands["resume-target-upload"]
    assert "--uploaded-qbit-category MTEAM" in resume_commands["resume-target-upload"]
    assert "--uploaded-qbit-tags retorrent" in resume_commands["resume-target-upload"]
    assert "--uploaded-paused" in resume_commands["resume-target-upload"]
    assert config_arg in resume_commands["resume-target-upload"]
    assert base_dir_arg in resume_commands["resume-target-upload"]
    assert summary_output_arg in resume_commands["resume-target-upload"]
    assert "--client default" in resume_commands["resume-uploaded-torrent"]
    assert "--uploaded-save-path /downloads" in resume_commands["resume-uploaded-torrent"]
    assert "--uploaded-qbit-category MTEAM" in resume_commands["resume-uploaded-torrent"]
    assert "--uploaded-qbit-tags retorrent" in resume_commands["resume-uploaded-torrent"]
    assert "--uploaded-paused" in resume_commands["resume-uploaded-torrent"]
    assert config_arg in resume_commands["resume-uploaded-torrent"]
    assert base_dir_arg not in resume_commands["resume-uploaded-torrent"]
    assert summary_output_arg in resume_commands["resume-uploaded-torrent"]
    assert any(stage["stage"] == "target-upload" and stage["ok"] is True for stage in summary_payload["stages"])


@pytest.mark.asyncio
async def test_pipeline_summary_recommends_uploaded_id_resume_when_download_missing(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        return {"status": "uploaded", "uploaded_torrent_id": "999"}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    uploaded_output_dir = str(tmp_path / "uploaded")
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--upload-target",
            "--target-torrent-file",
            str(torrent_file),
            "--target-execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--uploaded-output-dir",
            uploaded_output_dir,
            "--inject-uploaded-torrent",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--uploaded-paused",
            "--write-summary",
            "--summary-output-dir",
            str(tmp_path / "summary"),
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    assert payload["status"] == "blocked"
    assert payload["artifacts"]["uploaded_torrent_id"] == "999"
    assert payload["output_options"]["uploaded_output_dir"] == uploaded_output_dir
    payload_resume_commands = {command["stage"]: command["command"] for command in payload["resume_commands"]}
    assert "--uploaded-torrent-id 999" in payload_resume_commands["resume-uploaded-torrent-download"]
    assert f"--uploaded-output-dir {shlex.quote(uploaded_output_dir)}" in payload_resume_commands["resume-uploaded-torrent-download"]
    assert payload["resume_state"]["complete"] is False
    assert payload["resume_state"]["next_stage"] == "resume-uploaded-torrent-download"
    assert payload["resume_state"]["next_command"] == payload_resume_commands["resume-uploaded-torrent-download"]
    summary_payload = json.loads(await asyncio.to_thread(Path(payload["summary_file"]).read_text, encoding="utf-8"))
    assert summary_payload["output_options"]["uploaded_output_dir"] == uploaded_output_dir
    assert summary_payload["artifacts"]["uploaded_torrent_id"] == "999"
    assert "uploaded_torrent_file" not in summary_payload["artifacts"]
    resume_commands = {command["stage"]: command["command"] for command in summary_payload["resume_commands"]}
    resume_argv = {command["stage"]: command["argv"] for command in summary_payload["resume_commands"]}
    assert summary_payload["resume_state"]["resume_available"] is True
    assert summary_payload["resume_state"]["next_stage"] == "resume-uploaded-torrent-download"
    assert summary_payload["resume_state"]["next_command"] == resume_commands["resume-uploaded-torrent-download"]
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_id"] is True
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_file"] is False
    assert "--uploaded-torrent-id 999" in resume_commands["resume-uploaded-torrent-download"]
    assert resume_argv["resume-uploaded-torrent-download"][:3] == ["python3", "ptcli.py", "target-upload"]
    assert "999" in resume_argv["resume-uploaded-torrent-download"]
    assert "--download-uploaded-torrent" in resume_commands["resume-uploaded-torrent-download"]
    assert f"--uploaded-output-dir {shlex.quote(uploaded_output_dir)}" in resume_commands["resume-uploaded-torrent-download"]
    assert "--inject-uploaded-torrent" in resume_commands["resume-uploaded-torrent-download"]
    assert "--uploaded-save-path /downloads/Name" in resume_commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-category MTEAM" in resume_commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-tags retorrent" in resume_commands["resume-uploaded-torrent-download"]
    assert "--uploaded-paused" in resume_commands["resume-uploaded-torrent-download"]
    assert f"--summary-output-dir {shlex.quote(str(tmp_path / 'summary'))}" in resume_commands["resume-uploaded-torrent-download"]


@pytest.mark.asyncio
async def test_pipeline_reuses_inferred_path_for_uploaded_torrent_inject(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    uploaded_hash = "e" * 40

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        injected_hash = uploaded_hash if "MTEAM" in str(torrent_path) else "a" * 40
        return {
            "hash": injected_hash,
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
            "verified_in_client": True,
        }

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, content_path, torrent_hash, timeout, interval)
        matched_hash = uploaded_hash if torrent_hash == uploaded_hash else "a" * 40
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": "/downloads/Name", "hash": matched_hash}]}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        uploaded_path = tmp_path / "MTEAM-999.torrent"
        uploaded_path.write_bytes(b"d4:infod")
        return {
            "status": "uploaded",
            "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_path)},
        }

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads",
            "--wait-complete",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--upload-target",
            "--target-torrent-file",
            str(torrent_file),
            "--target-execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["uploaded_torrent_hash"] == uploaded_hash
    assert upload_stage["result"]["injected_torrent"]["save_path"] == "/downloads/Name"
    assert upload_stage["result"]["injected_torrent"]["category"] == "MTEAM"


@pytest.mark.asyncio
async def test_pipeline_exports_matched_torrent_for_target_upload(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    exported_torrent = tmp_path / "exported" / ("a" * 40 + ".torrent")
    sanitized_torrent = tmp_path / "exported" / ("a" * 40 + ".mteam-upload.torrent")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_export_hash_with_config(_config, client_name, torrent_hash, output_dir):
        _ = output_dir
        exported_torrent.parent.mkdir(parents=True, exist_ok=True)
        exported_torrent.write_bytes(b"d4:infod")
        return {"client": client_name, "hash": torrent_hash, "path": str(exported_torrent)}

    async def fake_sanitize_target_torrent_with_config(torrent_file, output_dir):
        assert torrent_file == str(exported_torrent)
        sanitized_torrent.write_bytes(b"d4:infod")
        return {"source_path": torrent_file, "path": str(sanitized_torrent), "output_dir": output_dir}

    async def fake_upload_mteam_from_package(_config, _package_dir, torrent_file, **_kwargs):
        uploaded_path = tmp_path / "MTEAM-999.torrent"
        uploaded_path.write_bytes(b"d4:infod")
        return {"status": "uploaded", "torrent_file": torrent_file, "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_path)}}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        _ = (torrent_path, category, tags, paused)
        return {"hash": "b" * 40, "save_path": save_path, "verified_in_client": True}

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {"client": client_name, "complete": True, "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval}, "matches": [{"hash": torrent_hash, "content_path": content_path}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "_export_hash_with_config", fake_export_hash_with_config)
    monkeypatch.setattr(ptcli_cli, "_sanitize_target_torrent_with_config", fake_sanitize_target_torrent_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--upload-target",
            "--export-target-torrent",
            "--target-torrent-output-dir",
            str(tmp_path / "exported"),
            "--target-execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    export_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-torrent-export")
    sanitize_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-torrent-sanitize")
    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert export_stage["ok"] is True
    assert sanitize_stage["ok"] is True
    assert payload["target_torrent_file"] == str(sanitized_torrent)
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["torrent_file"] == str(sanitized_torrent)
    assert upload_stage["result"]["injected_torrent"]["save_path"] == "/downloads/Name"


@pytest.mark.asyncio
async def test_pipeline_reuses_existing_target_package_for_upload(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path / "target"), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "resume-upload")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, source_info["name"], "a" * 40, "desc"), {})

    async def fake_upload_mteam_from_package(_config, package_dir, target_torrent_file, **kwargs):
        assert package_dir == package["package_dir"]
        assert target_torrent_file == str(torrent_file)
        assert kwargs["execute"] is False
        return {"status": "ready", "package_dir": package_dir, "torrent_file": target_torrent_file}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--package-dir",
            package["package_dir"],
            "--upload-target",
            "--target-torrent-file",
            str(torrent_file),
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    prepare_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert prepare_stage["ok"] is True
    assert prepare_stage["result"]["reused"] is True
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["status"] == "ready"


@pytest.mark.asyncio
async def test_pipeline_reuses_uploaded_torrent_file_for_target_injection(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path / "target"), accept_rules=True)
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")
    uploaded_hash = str(Torrent.read(str(uploaded_torrent), validate=False).infohash)
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, source_info["name"], "a" * 40, "desc"), {})

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        raise AssertionError("live upload must not run when --uploaded-torrent-file is provided")

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        return {"hash": uploaded_hash, "torrent_path": torrent_path, "save_path": save_path, "category": category, "tags": tags, "paused": paused, "verified_in_client": True}

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {
            "client": client_name,
            "complete": True,
            "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval},
            "matches": [{"hash": torrent_hash, "content_path": content_path}],
        }

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--package-dir",
            package["package_dir"],
            "--upload-target",
            "--uploaded-torrent-file",
            str(uploaded_torrent),
            "--inject-uploaded-torrent",
            "--wait-uploaded-complete",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    match_stage = next(stage for stage in payload["stages"] if stage["stage"] == "match")
    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert payload["path"] == "/downloads/Example"
    assert match_stage["ok"] is True
    assert match_stage.get("skipped") is not True
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["downloaded_torrent"]["reused"] is True
    assert upload_stage["result"]["downloaded_torrent"]["path"] == str(uploaded_torrent)
    assert upload_stage["result"]["uploaded_torrent_hash"] == uploaded_hash
    assert upload_stage["result"]["uploaded_wait"]["complete"] is True
    assert payload["closure"]["target"]["uploaded"] is True
    assert payload["closure"]["target"]["injected"] is True
    assert payload["closure"]["target"]["seeding"] is True
    assert payload["evidence"]["resume"]["target_package"] is True
    assert payload["evidence"]["resume"]["uploaded_torrent_file"] is True
    assert payload["summary"]["resume"]["used"] is True


@pytest.mark.asyncio
async def test_pipeline_uploaded_torrent_injection_requires_completion_wait(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path / "target"), accept_rules=True)
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, source_info["name"], "a" * 40, "desc"), {})

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_inject_source_with_config(*_args, **_kwargs):
        raise AssertionError("pipeline must not inject uploaded torrent without --wait-uploaded-complete")

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--package-dir",
            package["package_dir"],
            "--upload-target",
            "--uploaded-torrent-file",
            str(uploaded_torrent),
            "--inject-uploaded-torrent",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert upload_stage["ok"] is False
    assert upload_stage["skipped"] is True
    assert "--wait-uploaded-complete is required" in upload_stage["message"]
    assert "target-upload: --wait-uploaded-complete is required" in payload["blockers"][0]


@pytest.mark.asyncio
async def test_pipeline_reuses_uploaded_torrent_id_for_target_injection(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path / "target"), accept_rules=True)
    uploaded_torrent = tmp_path / "uploaded" / "MTEAM-999.torrent"
    uploaded_hash = "b" * 40
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, source_info["name"], "a" * 40, "desc"), {})

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        raise AssertionError("live upload must not run when --uploaded-torrent-id is provided")

    async def fake_download_mteam_uploaded_torrent(_config, torrent_id, output_dir):
        _ = output_dir
        assert torrent_id == "999"
        uploaded_torrent.parent.mkdir(parents=True, exist_ok=True)
        uploaded_torrent.write_bytes(b"d4:infod")
        return {"status": "uploaded", "uploaded_torrent_id": torrent_id, "downloaded_torrent": {"torrent_id": torrent_id, "path": str(uploaded_torrent)}}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        return {"hash": uploaded_hash, "torrent_path": torrent_path, "save_path": save_path, "category": category, "tags": tags, "paused": paused, "verified_in_client": True}

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {
            "client": client_name,
            "complete": True,
            "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval},
            "matches": [{"hash": torrent_hash, "content_path": content_path}],
        }

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    monkeypatch.setattr(ptcli_cli, "download_mteam_uploaded_torrent", fake_download_mteam_uploaded_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--package-dir",
            package["package_dir"],
            "--upload-target",
            "--uploaded-torrent-id",
            "999",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--wait-uploaded-complete",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    export_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-torrent-export")
    sanitize_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-torrent-sanitize")
    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert export_stage["skipped"] is True
    assert sanitize_stage["skipped"] is True
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["uploaded_torrent_id"] == "999"
    assert upload_stage["result"]["downloaded_torrent"]["path"] == str(uploaded_torrent)
    assert upload_stage["result"]["injected_torrent"]["save_path"] == "/downloads/Example"
    assert upload_stage["result"]["injected_torrent"]["category"] == "MTEAM"
    assert upload_stage["result"]["uploaded_wait"]["complete"] is True
    assert payload["closure"]["target"]["uploaded"] is True
    assert payload["closure"]["target"]["downloaded"] is True
    assert payload["closure"]["target"]["injected"] is True
    assert payload["closure"]["target"]["seeding"] is True
    assert payload["artifacts"]["uploaded_torrent_id"] == "999"


@pytest.mark.asyncio
async def test_pipeline_target_execute_enables_uploaded_torrent_followup(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    torrent_file = tmp_path / "target.torrent"
    sanitized_torrent = tmp_path / "exported" / "target.mteam-upload.torrent"
    uploaded_torrent = tmp_path / "uploaded" / "MTEAM-999.torrent"
    uploaded_hash = "b" * 40
    torrent_file.write_bytes(b"d4:infod")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_sanitize_target_torrent_with_config(torrent_file_arg, output_dir):
        assert torrent_file_arg == str(torrent_file)
        sanitized_torrent.parent.mkdir(parents=True, exist_ok=True)
        sanitized_torrent.write_bytes(b"d4:infod")
        return {"source_path": torrent_file_arg, "path": str(sanitized_torrent), "output_dir": output_dir}

    async def fake_upload_mteam_from_package(_config, _package_dir, torrent_file_arg, **kwargs):
        assert torrent_file_arg == str(sanitized_torrent)
        assert kwargs["download_uploaded"] is True
        uploaded_torrent.parent.mkdir(parents=True, exist_ok=True)
        uploaded_torrent.write_bytes(b"d4:infod")
        return {"status": "uploaded", "torrent_file": torrent_file_arg, "uploaded_torrent_hash": uploaded_hash, "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_torrent)}}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        _ = (torrent_path, category, tags, paused)
        return {"hash": uploaded_hash, "save_path": save_path, "verified_in_client": True}

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {"client": client_name, "complete": True, "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval}, "matches": [{"hash": torrent_hash, "content_path": content_path}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "_sanitize_target_torrent_with_config", fake_sanitize_target_torrent_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--upload-target",
            "--target-torrent-file",
            str(torrent_file),
            "--target-execute",
            "--confirm-upload",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["downloaded_torrent"]["path"] == str(uploaded_torrent)
    assert upload_stage["result"]["injected_torrent"]["save_path"] == "/downloads/Name"
    assert upload_stage["result"]["uploaded_wait"]["complete"] is True
    assert payload["closure"]["complete"] is True
    assert payload["closure"]["target"]["downloaded"] is True
    assert payload["closure"]["target"]["injected"] is True
    assert payload["closure"]["target"]["seeding"] is True


@pytest.mark.asyncio
async def test_pipeline_target_execute_blocks_missing_runtime_dependency(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    source_hash = "a" * 40
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 2,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": source_hash,
        "description_length": 4,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Name", str(tmp_path / "target"), accept_rules=True)
    target_torrent = tmp_path / "target.mteam-upload.torrent"
    target_torrent.write_bytes(b"d4:infod")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    original_find_spec = ptcli_doctor.importlib.util.find_spec

    def fake_find_spec(module):
        if module == "qbittorrentapi":
            return None
        return original_find_spec(module)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (base_dir, source_id)
        return source_info_from_tuple(tracker, "60635", (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", source_hash, "desc"), {})

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {"client": client_name, "complete": True, "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval}, "matches": [{"hash": source_hash, "content_path": content_path}]}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": source_hash}]}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_target_upload_with_config(*_args, **_kwargs):
        raise AssertionError("live target upload must not run when runtime dependencies are missing")

    monkeypatch.setattr(ptcli_doctor.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_target_upload_with_config", fake_target_upload_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--package-dir",
            package["package_dir"],
            "--check-dupes",
            "--accept-rules",
            "--upload-target",
            "--target-torrent-file",
            str(target_torrent),
            "--target-execute",
            "--confirm-upload",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    runtime_stage = next(stage for stage in payload["stages"] if stage["stage"] == "runtime-check")
    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert runtime_stage["ok"] is False
    assert "qbittorrent-api" in runtime_stage["result"]["message"]
    assert upload_stage["ok"] is False
    assert upload_stage["skipped"] is True
    assert "focused ptcli runtime dependencies are not ready" in upload_stage["message"]
    assert any(blocker.startswith("runtime-check: Missing PTCLI runtime dependencies") for blocker in payload["blockers"])
    assert "Install the focused ptcli runtime dependencies" in payload["next_actions"][0]
    assert payload["effective_actions"]["check_runtime"] is True


@pytest.mark.asyncio
async def test_pipeline_target_execute_requires_current_source_ready_evidence(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    source_hash = "a" * 40
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 2,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": source_hash,
        "description_length": 4,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Name", str(tmp_path / "target"), accept_rules=True)
    target_torrent = tmp_path / "target.mteam-upload.torrent"
    target_torrent.write_bytes(b"d4:infod")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (base_dir, source_id)
        return source_info_from_tuple(tracker, "60635", (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", source_hash, "desc"), {})

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {"client": client_name, "complete": False, "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval}, "matches": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 0, "matches": []}

    async def fake_target_upload_with_config(*_args, **_kwargs):
        raise AssertionError("live target upload must not run before current source content is verified")

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "_target_upload_with_config", fake_target_upload_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--package-dir",
            package["package_dir"],
            "--accept-rules",
            "--upload-target",
            "--target-torrent-file",
            str(target_torrent),
            "--target-execute",
            "--confirm-upload",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert upload_stage["ok"] is False
    assert upload_stage["skipped"] is True
    assert "current pipeline run did not verify complete source qBittorrent content" in upload_stage["message"]
    assert "target-upload: Skipped because current pipeline run did not verify complete source qBittorrent content before live target upload." in payload["blockers"]


@pytest.mark.asyncio
async def test_pipeline_target_execute_requires_current_duplicate_check(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    source_hash = "a" * 40
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 2,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": source_hash,
        "description_length": 4,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Name", str(tmp_path / "target"), accept_rules=True)
    target_torrent = tmp_path / "target.mteam-upload.torrent"
    target_torrent.write_bytes(b"d4:infod")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (base_dir, source_id)
        return source_info_from_tuple(tracker, "60635", (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", source_hash, "desc"), {})

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {"client": client_name, "complete": True, "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval}, "matches": [{"hash": source_hash, "content_path": content_path}]}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": source_hash}]}

    async def fake_target_upload_with_config(*_args, **_kwargs):
        raise AssertionError("live target upload must not run before current duplicate check")

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "_target_upload_with_config", fake_target_upload_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--package-dir",
            package["package_dir"],
            "--accept-rules",
            "--upload-target",
            "--target-torrent-file",
            str(target_torrent),
            "--target-execute",
            "--confirm-upload",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    dupe_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-dupe-check")
    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert dupe_stage["skipped"] is True
    assert upload_stage["ok"] is False
    assert upload_stage["skipped"] is True
    assert "current pipeline run did not complete a clean MTEAM duplicate check" in upload_stage["message"]
    assert "target-upload: Skipped because current pipeline run did not complete a clean MTEAM duplicate check before live target upload." in payload["blockers"]


@pytest.mark.asyncio
async def test_pipeline_target_execute_rechecks_fresh_duplicates_before_upload(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    source_hash = "a" * 40
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Name.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 2,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": source_hash,
        "description_length": 4,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Name", str(tmp_path / "target"), accept_rules=True)
    target_torrent = make_mteam_safe_torrent(tmp_path, "target")
    dupe_calls = []
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (base_dir, source_id)
        return source_info_from_tuple(tracker, "60635", (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", source_hash, "desc"), {})

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {"client": client_name, "complete": True, "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval}, "matches": [{"hash": source_hash, "content_path": content_path}]}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": source_hash}]}

    async def fake_search_mteam_duplicates(_config, dupe_source_info):
        dupe_calls.append(dupe_source_info)
        if len(dupe_calls) == 1:
            return {"searched": True, "query": {"imdb": "tt1234567"}, "count": 0, "dupes": []}
        return {"searched": True, "query": {"imdb": "tt1234567"}, "count": 1, "dupes": [{"name": "Existing"}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        raise AssertionError("live upload must not run when fresh duplicate check finds matches")

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--package-dir",
            package["package_dir"],
            "--check-dupes",
            "--accept-rules",
            "--upload-target",
            "--target-torrent-file",
            str(target_torrent),
            "--target-execute",
            "--confirm-upload",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert len(dupe_calls) == 2
    assert upload_stage["ok"] is False
    assert upload_stage["result"]["fresh_duplicate_check"]["count"] == 1
    assert "target-upload: Target upload stage did not complete every requested upload follow-up." in payload["blockers"]
    assert any("fresh_duplicate_check" in blocker for blocker in payload["blockers"])


@pytest.mark.asyncio
async def test_pipeline_sanitizes_manual_target_torrent_for_upload(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    raw_torrent = tmp_path / "raw.torrent"
    sanitized_torrent = tmp_path / "exported" / "raw.mteam-upload.torrent"
    raw_torrent.write_bytes(b"d4:infod")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_sanitize_target_torrent_with_config(torrent_file, output_dir):
        assert torrent_file == str(raw_torrent)
        assert output_dir == str(tmp_path / "exported")
        sanitized_torrent.parent.mkdir(parents=True, exist_ok=True)
        sanitized_torrent.write_bytes(b"d4:infod")
        return {"source_path": torrent_file, "path": str(sanitized_torrent), "announce": "https://fake.tracker", "source_flag": "MTEAM", "removed_fields": ["announce-list"]}

    async def fake_upload_mteam_from_package(_config, _package_dir, torrent_file, **_kwargs):
        uploaded_path = tmp_path / "MTEAM-999.torrent"
        uploaded_path.write_bytes(b"d4:infod")
        return {"status": "uploaded", "torrent_file": torrent_file, "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_path)}}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        _ = (torrent_path, category, tags, paused)
        return {"hash": "b" * 40, "save_path": save_path, "verified_in_client": True}

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {"client": client_name, "complete": True, "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval}, "matches": [{"hash": torrent_hash, "content_path": content_path}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "_sanitize_target_torrent_with_config", fake_sanitize_target_torrent_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--upload-target",
            "--target-torrent-file",
            str(raw_torrent),
            "--target-torrent-output-dir",
            str(tmp_path / "exported"),
            "--sanitize-target-torrent",
            "--target-execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    sanitize_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-torrent-sanitize")
    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert sanitize_stage["ok"] is True
    assert sanitize_stage["result"]["path"] == str(sanitized_torrent)
    assert payload["target_torrent_file"] == str(sanitized_torrent)
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["torrent_file"] == str(sanitized_torrent)
    assert upload_stage["result"]["injected_torrent"]["save_path"] == "/downloads/Name"


@pytest.mark.asyncio
async def test_pipeline_target_execute_auto_exports_and_sanitizes_target_torrent(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    exported_torrent = tmp_path / "exported" / "matched.torrent"
    sanitized_torrent = tmp_path / "exported" / "matched.mteam-upload.torrent"
    uploaded_torrent = tmp_path / "uploaded" / "MTEAM-999.torrent"
    uploaded_hash = "b" * 40
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_export_hash_with_config(_config, client_name, torrent_hash, output_dir):
        assert client_name == "default"
        assert torrent_hash == "a" * 40
        assert output_dir == str(tmp_path / "exported")
        exported_torrent.parent.mkdir(parents=True, exist_ok=True)
        exported_torrent.write_bytes(b"d4:infod")
        return {"client": client_name, "hash": torrent_hash, "path": str(exported_torrent)}

    async def fake_sanitize_target_torrent_with_config(torrent_file, output_dir):
        assert torrent_file == str(exported_torrent)
        assert output_dir == str(tmp_path / "exported")
        sanitized_torrent.write_bytes(b"d4:infod")
        return {"source_path": torrent_file, "path": str(sanitized_torrent), "announce": "https://fake.tracker", "source_flag": "MTEAM", "removed_fields": ["announce-list"]}

    async def fake_upload_mteam_from_package(_config, _package_dir, torrent_file, **_kwargs):
        assert torrent_file == str(sanitized_torrent)
        uploaded_torrent.parent.mkdir(parents=True, exist_ok=True)
        uploaded_torrent.write_bytes(b"d4:infod")
        return {"status": "uploaded", "torrent_file": torrent_file, "uploaded_torrent_hash": uploaded_hash, "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_torrent)}}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        _ = (torrent_path, category, tags, paused)
        return {"hash": uploaded_hash, "save_path": save_path, "verified_in_client": True}

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {"client": client_name, "complete": True, "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval}, "matches": [{"hash": torrent_hash, "content_path": content_path}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "_export_hash_with_config", fake_export_hash_with_config)
    monkeypatch.setattr(ptcli_cli, "_sanitize_target_torrent_with_config", fake_sanitize_target_torrent_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--path",
            "/downloads/Name",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--target-torrent-output-dir",
            str(tmp_path / "exported"),
            "--accept-rules",
            "--upload-target",
            "--target-execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--wait-uploaded-complete",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    export_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-torrent-export")
    sanitize_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-torrent-sanitize")
    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert export_stage["ok"] is True
    assert export_stage["result"]["path"] == str(exported_torrent)
    assert sanitize_stage["ok"] is True
    assert sanitize_stage["result"]["path"] == str(sanitized_torrent)
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["torrent_file"] == str(sanitized_torrent)
    assert payload["target_torrent_file"] == str(sanitized_torrent)
    assert payload["closure"]["complete"] is True
    assert payload["closure"]["target"]["seeding"] is True


@pytest.mark.asyncio
async def test_pipeline_target_execute_auto_downloads_injects_and_waits_source(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    source_torrent = tmp_path / "source" / "U2-60635.torrent"
    exported_torrent = tmp_path / "exported" / "matched.torrent"
    sanitized_torrent = tmp_path / "exported" / "matched.mteam-upload.torrent"
    uploaded_torrent = tmp_path / "uploaded" / "MTEAM-999.torrent"
    source_hash = "a" * 40
    uploaded_hash = "b" * 40
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        source_torrent.parent.mkdir(parents=True, exist_ok=True)
        source_torrent.write_bytes(b"d4:infod")
        return source_torrent

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        _ = (category, tags, paused)
        if torrent_path == str(source_torrent):
            return {"hash": source_hash, "torrent_path": torrent_path, "save_path": save_path, "verified_in_client": True}
        return {"hash": uploaded_hash, "torrent_path": torrent_path, "save_path": save_path, "verified_in_client": True}

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (timeout, interval)
        if torrent_hash == source_hash:
            return {"client": client_name, "complete": True, "query": {"torrent_hash": torrent_hash, "content_path": content_path}, "matches": [{"hash": torrent_hash, "content_path": "/downloads/Name"}]}
        return {"client": client_name, "complete": True, "query": {"torrent_hash": torrent_hash, "content_path": content_path}, "matches": [{"hash": torrent_hash, "content_path": content_path}]}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        assert content_path == "/downloads/Name"
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": source_hash}]}

    async def fake_export_hash_with_config(_config, _client_name, torrent_hash, output_dir):
        assert torrent_hash == source_hash
        exported_torrent.parent.mkdir(parents=True, exist_ok=True)
        exported_torrent.write_bytes(b"d4:infod")
        return {"hash": torrent_hash, "path": str(exported_torrent), "output_dir": output_dir}

    async def fake_sanitize_target_torrent_with_config(torrent_file, output_dir):
        assert torrent_file == str(exported_torrent)
        sanitized_torrent.write_bytes(b"d4:infod")
        return {"source_path": torrent_file, "path": str(sanitized_torrent), "output_dir": output_dir}

    async def fake_upload_mteam_from_package(_config, _package_dir, torrent_file, **_kwargs):
        assert torrent_file == str(sanitized_torrent)
        uploaded_torrent.parent.mkdir(parents=True, exist_ok=True)
        uploaded_torrent.write_bytes(b"d4:infod")
        return {"status": "uploaded", "torrent_file": torrent_file, "uploaded_torrent_hash": uploaded_hash, "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_torrent)}}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "_export_hash_with_config", fake_export_hash_with_config)
    monkeypatch.setattr(ptcli_cli, "_sanitize_target_torrent_with_config", fake_sanitize_target_torrent_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--save-path",
            "/downloads",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--target-torrent-output-dir",
            str(tmp_path / "exported"),
            "--accept-rules",
            "--upload-target",
            "--target-execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--wait-uploaded-complete",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    source_download = next(stage for stage in payload["stages"] if stage["stage"] == "source-download")
    inject_source = next(stage for stage in payload["stages"] if stage["stage"] == "inject-source")
    wait_complete = next(stage for stage in payload["stages"] if stage["stage"] == "wait-complete")
    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert source_download["ok"] is True
    assert source_download.get("skipped") is not True
    assert inject_source["ok"] is True
    assert inject_source["result"]["save_path"] == "/downloads"
    assert wait_complete["ok"] is True
    assert payload["path"] == "/downloads/Name"
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["injected_torrent"]["save_path"] == "/downloads/Name"
    assert payload["closure"]["complete"] is True
    assert payload["closure"]["source"]["complete"] is True
    assert payload["closure"]["target"]["seeding"] is True
    assert payload["requested_actions"]["download_source"] is False
    assert payload["requested_actions"]["inject_source"] is False
    assert payload["requested_actions"]["wait_complete"] is False
    assert payload["effective_actions"]["live_target_upload"] is True
    assert payload["effective_actions"]["download_source"] is True
    assert payload["effective_actions"]["inject_source"] is True
    assert payload["effective_actions"]["wait_complete"] is True
    assert payload["effective_actions"]["target_torrent_export"] is True
    assert payload["effective_actions"]["target_torrent_sanitize"] is True
    assert payload["effective_actions"]["download_uploaded_torrent"] is True
    assert payload["effective_actions"]["inject_uploaded_torrent"] is True
    assert payload["effective_actions"]["wait_uploaded_complete"] is True
    assert payload["summary"]["effective_actions"] == payload["effective_actions"]


@pytest.mark.asyncio
async def test_pipeline_upload_target_requires_prepare_target(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name", "a" * 40, "desc"), {})

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--base-dir", str(tmp_path), "--upload-target", "--target-torrent-file", "x.torrent", "--json"])

    payload = await ptcli_cli.pipeline_payload(args)

    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert upload_stage["ok"] is False
    assert "target-prepare" in upload_stage["message"]


def test_mteam_prepare_preview_blocks_missing_source() -> None:
    preview = build_mteam_prepare_preview(None, ["MTEAM"], [], None)

    assert preview["blockers"]
    assert "Source metadata is not available." in preview["blockers"]


@pytest.mark.asyncio
async def test_mteam_duplicate_search_requires_imdb() -> None:
    result = await search_mteam_duplicates({}, {"name": "No IMDb"})

    assert result["searched"] is False
    assert "IMDb" in result["reason"]


@pytest.mark.asyncio
async def test_mteam_duplicate_search_uses_ptcli_api_client(monkeypatch) -> None:
    closed = {"value": False}

    class FakeMTeamApiClient:
        def __init__(self, config):
            assert config["TRACKERS"]["MTEAM"]["api_key"] == "fake"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed["value"] = True

        async def search_by_imdb(self, imdb):
            assert imdb == "tt1234567"
            return {
                "data": [
                    {
                        "id": 999,
                        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
                        "size": 123,
                        "numfiles": "1",
                        "standard": 1,
                        "source": 8,
                        "smallDescr": "Example",
                    }
                ]
            }

    monkeypatch.setattr(ptcli_target, "MTeamApiClient", FakeMTeamApiClient)

    result = await search_mteam_duplicates(
        {"TRACKERS": {"MTEAM": {"api_key": "fake"}}},
        {"name": "Example.Movie.2024.1080p.WEB-DL-GROUP", "imdb_id": 1234567, "content_path": "/downloads/Example"},
    )

    assert closed["value"] is True
    assert result["searched"] is True
    assert result["count"] == 1
    assert result["dupes"][0]["id"] == 999
    assert result["dupes"][0]["res"] == "1080p"
    assert result["dupes"][0]["type"] == "WEBDL"


@pytest.mark.asyncio
async def test_mteam_uploaded_torrent_download_uses_ptcli_api_client(monkeypatch, tmp_path) -> None:
    class FakeMTeamApiClient:
        def __init__(self, config):
            assert config["TRACKERS"]["MTEAM"]["api_key"] == "fake"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def download_torrent(self, torrent_id, destination):
            assert torrent_id == "999"
            await asyncio.to_thread(Path(destination).write_bytes, b"d4:infod")

    monkeypatch.setattr(ptcli_target, "MTeamApiClient", FakeMTeamApiClient)

    result = await ptcli_target.download_mteam_uploaded_torrent({"TRACKERS": {"MTEAM": {"api_key": "fake"}}}, "999", str(tmp_path))

    assert result["uploaded_torrent_id"] == "999"
    assert await asyncio.to_thread(Path(result["downloaded_torrent"]["path"]).read_bytes) == b"d4:infod"


def test_mteam_meta_draft_and_field_mapping_from_name() -> None:
    source_info = {
        "name": "Example.Movie.2024.1080p.WEB-DL.DDP5.1.H.265-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
    }

    meta_draft = build_mteam_meta_draft(source_info, "/downloads/Example.Movie.2024")
    mapping = build_mteam_field_mapping(meta_draft)

    assert meta_draft["category"] == "MOVIE"
    assert meta_draft["type"] == "WEBDL"
    assert meta_draft["resolution"] == "1080p"
    assert mapping["name"] == source_info["name"]
    assert mapping["category"] == 419
    assert mapping["standard"] == 1
    assert mapping["imdb"] == "https://www.imdb.com/title/tt1234567"
    assert mapping["douban"] == "https://movie.douban.com/subject/1291546/"


def test_mteam_meta_draft_normalizes_tt_prefixed_imdb() -> None:
    source_info = {
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": "tt1234567",
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }

    meta_draft = build_mteam_meta_draft(source_info, "/downloads/Example.Movie.2024")
    mapping = build_mteam_field_mapping(meta_draft)

    assert meta_draft["imdb_id"] == 1234567
    assert meta_draft["imdb"] == "1234567"
    assert mapping["imdb"] == "https://www.imdb.com/title/tt1234567"


def test_mteam_description_draft_and_upload_gate() -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "torrenthash": "a" * 40,
    }
    meta_draft = build_mteam_meta_draft(source_info, "/downloads/Example")
    description = build_mteam_description_draft(meta_draft, source_info)
    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example")
    gate = build_mteam_upload_gate(
        preview,
        mteam_ready_stages(),
        accept_rules=True,
    )

    assert "Retorrent review draft" in description
    assert "Source tracker: U2" in description
    assert gate["ready"] is True
    assert gate["blockers"] == []


def test_mteam_upload_gate_surfaces_duplicate_blocker() -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "torrenthash": "a" * 40,
    }
    stages = [
        {"stage": "rule-check", "ok": True, "result": {"ready": True}},
        {"stage": "match", "ok": True, "result": {"count": 1, "matches": [{"content_path": "/downloads/Example", "hash": "a" * 40}]}},
        {"stage": "target-dupe-check", "ok": True, "result": {"searched": True, "count": 2, "dupes": [{"name": "Existing"}]}},
    ]

    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], stages, "/downloads/Example")
    gate = build_mteam_upload_gate(preview, stages, accept_rules=True)

    assert gate["ready"] is False
    assert gate["dupe_count"] == 2
    assert any("duplicate search found 2" in blocker for blocker in gate["blockers"])


def test_mteam_rule_review_records_rule_gate() -> None:
    review = build_mteam_rule_review(mteam_ready_stages(), accept_rules=True)

    assert review["rules_acknowledged"] is True
    assert review["rule_check_ready"] is True
    assert review["blockers"] == []


def test_mteam_rule_review_records_site_rule_obligations() -> None:
    stages = [{"stage": "rule-check", "ok": True, "result": build_rule_check("U2", ["MTEAM"], accept_rules=True)}]

    review = build_mteam_rule_review(stages, accept_rules=True)

    assert review["manual_review"] == {
        "required": True,
        "acknowledged": True,
        "source_tracker": "U2",
        "target_trackers": ["MTEAM"],
        "obligation_count": 2,
        "rules_urls": ["https://kp.m-team.cc/rules", "https://u2.dmhy.org/rules.php"],
        "required_confirmations": [
            {
                "tracker": obligation["tracker"],
                "role": obligation["role"],
                "action": obligation["action"],
                "rules_url": obligation["rules_url"],
                "review_fingerprint": obligation["review_fingerprint"],
                "required_confirmations": obligation["review_scope"]["required_confirmations"],
            }
            for obligation in review["rule_obligations"]
        ],
        "acknowledgement_evidence": [obligation["acknowledgement_evidence"] for obligation in review["rule_obligations"]],
        "review_fingerprints": [obligation["review_fingerprint"] for obligation in review["rule_obligations"]],
        "site_specific_rules_encoded": False,
        "message": "Manual source/target rule review has been acknowledged.",
    }
    assert [(obligation["tracker"], obligation["role"], obligation["action"]) for obligation in review["rule_obligations"]] == [
        ("U2", "source", "download_and_retorrent"),
        ("MTEAM", "target", "upload_and_seed"),
    ]
    assert all(obligation["acknowledged"] is True for obligation in review["rule_obligations"])
    assert all(len(obligation["review_fingerprint"]) == 64 for obligation in review["rule_obligations"])
    assert all(obligation["acknowledgement_evidence"]["review_fingerprint"] == obligation["review_fingerprint"] for obligation in review["rule_obligations"])
    assert all(obligation["acknowledgement_evidence"]["site_specific_rules_encoded"] is False for obligation in review["rule_obligations"])
    assert all(obligation["review_scope"]["rules_url"] == obligation["rules_url"] for obligation in review["rule_obligations"])
    assert all(obligation["review_scope"]["required_confirmations"] for obligation in review["rule_obligations"])


def test_mteam_rule_review_blocks_without_ack() -> None:
    review = build_mteam_rule_review(mteam_ready_stages(), accept_rules=False)

    assert review["rules_acknowledged"] is False
    assert review["manual_review"]["acknowledged"] is False
    assert review["manual_review"]["site_specific_rules_encoded"] is False
    assert any("rules_acknowledged" in blocker for blocker in review["blockers"])


def test_mteam_prepare_preview_contains_package_fields() -> None:
    source_info = {
        "name": "Example.Movie.2024.2160p.BluRay.REMUX.HEVC-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()

    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], stages, "/downloads/Example.Movie.2024")

    assert preview["blockers"] == []
    assert preview["verified_content"] is True
    assert preview["meta_draft"]["type"] == "REMUX"
    assert preview["field_mapping"]["category"] == 439
    assert preview["field_mapping"]["standard"] == 6


def test_mteam_prepare_preview_rejects_empty_qbit_match_evidence() -> None:
    source_info = {
        "name": "Example.Movie.2024.2160p.BluRay.REMUX.HEVC-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = [
        {"stage": "rule-check", "ok": True, "result": {"ready": True}},
        {"stage": "match", "ok": True, "result": {"count": 1, "matches": [{}]}},
        {"stage": "target-dupe-check", "ok": True, "result": {"searched": True, "count": 0, "dupes": []}},
    ]

    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], stages, "/downloads/Example.Movie.2024")
    gate = build_mteam_upload_gate(preview, stages, accept_rules=True)

    assert preview["verified_content"] is False
    assert gate["ready"] is False
    assert any(check["name"] == "verified_content" and check["ok"] is False for check in gate["checks"])


def test_write_mteam_prepare_package_creates_auditable_files(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }

    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path))

    assert package["package_dir"].endswith("U2-60635-to-MTEAM")
    assert package["files"]["preview"].endswith("mteam-prepare-preview.json")
    assert package["files"]["meta_draft"].endswith("mteam-meta-draft.json")
    assert package["files"]["field_mapping"].endswith("mteam-field-mapping.json")
    assert package["files"]["description_draft"].endswith("mteam-description-draft.txt")
    assert package["files"]["rule_review"].endswith("mteam-rule-review.json")
    assert package["files"]["upload_gate"].endswith("mteam-upload-gate.json")
    assert package["files"]["manifest"].endswith("mteam-package-manifest.json")
    assert package["package_manifest"]["schema_version"] == 1
    assert package["package_manifest"]["kind"] == "ptcli.mteam.prepare_package"
    assert package["package_manifest"]["files"]["preview"]["sha1"]
    assert package["package_manifest"]["rule_obligations"]["count"] == 2
    manifest_commands = {command["stage"]: command["command"] for command in package["package_manifest"]["commands"]}
    assert "--package-dir" in manifest_commands["target-upload-preflight"]
    assert shlex.quote(package["package_dir"]) in manifest_commands["target-upload-preflight"]
    assert "--write-payload" in manifest_commands["target-upload-preflight"]
    assert "--execute --confirm-upload" in manifest_commands["target-upload-live"]
    assert "--download-uploaded-torrent" in manifest_commands["target-upload-live"]
    assert "--inject-uploaded-torrent" in manifest_commands["target-upload-live"]
    assert "--uploaded-save-path /downloads/Example" in manifest_commands["target-upload-live"]
    assert "--wait-uploaded-complete" in manifest_commands["target-upload-live"]
    assert "--uploaded-torrent-id '<id>'" in manifest_commands["resume-uploaded-torrent-id"]
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-prepare-preview.json").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-description-draft.txt").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-rule-review.json").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-package-manifest.json").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-field-mapping.json").read_text(encoding="utf-8").strip().startswith("{")
    loaded = load_mteam_prepare_package(package["package_dir"])
    assert loaded["files"]["manifest"].endswith("mteam-package-manifest.json")
    assert loaded["package_manifest"]["ready"] is package["package_manifest"]["ready"]


def test_create_mteam_upload_torrent_candidate_sanitizes_export(tmp_path) -> None:
    content = tmp_path / "Example.mkv"
    content.write_bytes(b"content")
    source_torrent = tmp_path / "source.torrent"
    torrent = Torrent(path=str(content), trackers=["https://source.example/passkey/announce"], comment="private comment")
    torrent.generate()
    torrent.metainfo["url-list"] = ["https://source.example/file"]
    torrent.write(str(source_torrent), overwrite=True)

    result = create_mteam_upload_torrent_candidate(str(source_torrent), str(tmp_path / "exported"))

    sanitized = Torrent.read(result["path"], validate=False)
    assert result["path"].endswith(".mteam-upload.torrent")
    assert sanitized.metainfo["announce"] == "https://fake.tracker"
    assert sanitized.metainfo["comment"] == ""
    assert sanitized.metainfo["info"]["source"] == "MTEAM"
    assert "url-list" not in sanitized.metainfo


def test_mteam_upload_preflight_reads_ready_package(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], torrent_file=str(torrent_file))

    assert preflight["status"] == "ready"
    assert preflight["dry_run"] is True
    assert preflight["upload_gate"]["ready"] is True
    assert preflight["rule_review"]["rule_check_ready"] is True
    assert preflight["rule_obligation_review"]["ready"] is True
    assert preflight["package_manifest"]["kind"] == "ptcli.mteam.prepare_package"
    assert preflight["package_manifest"]["files"]["description_draft"]["size_bytes"] > 0
    assert preflight["upload_payload"]["form_fields"]["name"] == source_info["name"]
    assert all(check["ok"] for check in preflight["upload_payload"]["field_checks"])
    assert preflight["upload_payload"]["torrent_file"]["sha1"]
    assert len(preflight["upload_payload"]["torrent_file"]["torrent_hash"]) == 40
    assert preflight["upload_payload"]["torrent_file"]["infohash"] == preflight["upload_payload"]["torrent_file"]["torrent_hash"]
    assert preflight["next_actions"] == ["Review the package manually, then rerun with --execute --confirm-upload and the reviewed target torrent file when ready."]


def test_mteam_upload_preflight_blocks_tampered_package_file(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")
    description_path = Path(package["files"]["description_draft"])
    description_path.write_text(description_path.read_text(encoding="utf-8") + "\nmanual edit after manifest\n", encoding="utf-8")

    preflight = build_mteam_upload_preflight(package["package_dir"], torrent_file=str(torrent_file))

    assert preflight["status"] == "blocked"
    assert "MTEAM package file integrity mismatch for description_draft: size_bytes changed." in preflight["blockers"]


def test_mteam_upload_preflight_blocks_missing_package_manifest(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")
    Path(package["files"]["manifest"]).unlink()

    preflight = build_mteam_upload_preflight(package["package_dir"], torrent_file=str(torrent_file))

    assert preflight["status"] == "blocked"
    assert "MTEAM package manifest is missing; regenerate the package before upload." in preflight["blockers"]


def test_mteam_upload_preflight_blocks_execute_without_rule_obligations(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    legacy_stages = [
        {"stage": "rule-check", "ok": True, "result": {"ready": True, "source_tracker": "U2", "target_trackers": ["MTEAM"], "checks": []}},
        {"stage": "match", "ok": True, "result": {"count": 1, "matches": [{"content_path": "/downloads/Example", "hash": "a" * 40}]}},
        {"stage": "target-dupe-check", "ok": True, "result": {"searched": True, "count": 0, "dupes": []}},
    ]
    package = write_mteam_prepare_package(source_info, ["MTEAM"], legacy_stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preview = build_mteam_upload_preflight(package["package_dir"], torrent_file=str(torrent_file))
    execute = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(torrent_file))

    assert preview["status"] == "ready"
    assert preview["rule_obligation_review"]["ready"] is False
    assert execute["status"] == "blocked"
    assert any("Rule obligations are missing" in blocker for blocker in execute["blockers"])


def test_mteam_upload_preflight_blocks_execute_without_rule_review_scope(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    rule_review_path = Path(package["files"]["rule_review"])
    rule_review = json.loads(rule_review_path.read_text(encoding="utf-8"))
    for obligation in rule_review["rule_obligations"]:
        obligation.pop("review_scope", None)
    rule_review_path.write_text(json.dumps(rule_review), encoding="utf-8")
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(torrent_file))

    assert preflight["status"] == "blocked"
    assert any("manual review scope" in blocker for blocker in preflight["blockers"])


def test_mteam_upload_preflight_blocks_rule_review_blockers(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    rule_review_path = Path(package["files"]["rule_review"])
    rule_review = json.loads(rule_review_path.read_text(encoding="utf-8"))
    rule_review["blockers"] = ["Injected rule blocker for preflight coverage."]
    rule_review_path.write_text(json.dumps(rule_review), encoding="utf-8")
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], torrent_file=str(torrent_file))

    assert preflight["status"] == "blocked"
    assert preflight["rule_review"]["blockers"]
    assert "MTEAM rule review has blockers." in preflight["blockers"]


def test_mteam_upload_preflight_reports_duplicate_gate_blocker(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = [
        {"stage": "rule-check", "ok": True, "result": {"ready": True}},
        {"stage": "match", "ok": True, "result": {"count": 1, "matches": [{"content_path": "/downloads/Example", "hash": "a" * 40}]}},
        {"stage": "target-dupe-check", "ok": True, "result": {"searched": True, "count": 1, "dupes": [{"name": "Existing"}]}},
    ]
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], torrent_file=str(torrent_file))

    assert any("duplicate_check" in blocker and "found 1" in blocker for blocker in package["blockers"])
    assert preflight["status"] == "blocked"
    assert any("duplicate_check" in blocker and "found 1" in blocker for blocker in preflight["blockers"])


def test_mteam_upload_preflight_reports_torrent_safety_metadata(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    content = tmp_path / "Example.mkv"
    content.write_bytes(b"content")
    source_torrent = tmp_path / "source.torrent"
    torrent = Torrent(path=str(content), trackers=["https://source.example/passkey/announce"], comment="private comment")
    torrent.generate()
    torrent.write(str(source_torrent), overwrite=True)
    candidate = create_mteam_upload_torrent_candidate(str(source_torrent), str(tmp_path / "exported"))

    preflight = build_mteam_upload_preflight(package["package_dir"], torrent_file=candidate["path"])

    torrent_summary = preflight["upload_payload"]["torrent_file"]
    assert torrent_summary["metadata_readable"] is True
    assert len(torrent_summary["torrent_hash"]) == 40
    assert torrent_summary["infohash"] == torrent_summary["torrent_hash"]
    assert torrent_summary["announce"] == "https://fake.tracker"
    assert torrent_summary["source_flag"] == "MTEAM"
    assert torrent_summary["comment_length"] == 0
    assert torrent_summary["mteam_safe"] is True


def test_mteam_upload_preflight_blocks_unsafe_target_torrent(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    content = tmp_path / "Example.mkv"
    content.write_bytes(b"content")
    unsafe_torrent = tmp_path / "unsafe.torrent"
    torrent = Torrent(path=str(content), trackers=["https://source.example/passkey/announce"], comment="private comment")
    torrent.generate()
    torrent.write(str(unsafe_torrent), overwrite=True)

    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(unsafe_torrent))

    assert preflight["status"] == "blocked"
    assert preflight["upload_payload"]["torrent_file"]["metadata_readable"] is True
    assert preflight["upload_payload"]["torrent_file"]["mteam_safe"] is False
    assert any("--sanitize-target-torrent" in blocker for blocker in preflight["blockers"])


def test_mteam_upload_preflight_allows_execute_when_payload_is_ready(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(torrent_file))

    assert preflight["status"] == "ready"
    assert preflight["dry_run"] is False
    assert preflight["rule_obligation_review"]["ready"] is True


@pytest.mark.asyncio
async def test_mteam_live_upload_requires_confirmation(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    result = await upload_mteam_from_package({}, package["package_dir"], str(torrent_file), execute=True)

    assert result["status"] == "blocked"
    assert "confirm-upload" in result["blockers"][0]


@pytest.mark.asyncio
async def test_mteam_live_upload_blocks_unsafe_torrent_before_uploader(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    unsafe_torrent = tmp_path / "unsafe.torrent"
    unsafe_torrent.write_bytes(b"d4:infod")

    async def fake_uploader(_config, _package_dir, _torrent_file_path):
        raise AssertionError("unsafe torrent must not reach live uploader")

    result = await upload_mteam_from_package(
        {"TRACKERS": {"MTEAM": {"api_key": "fake"}}},
        package["package_dir"],
        str(unsafe_torrent),
        execute=True,
        confirm_upload=True,
        uploader=fake_uploader,
    )

    assert result["status"] == "blocked"
    assert "metadata is not readable" in " ".join(result["blockers"])


@pytest.mark.asyncio
async def test_mteam_live_upload_uses_injected_uploader(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    async def fake_uploader(_config, package_dir, torrent_file_path):
        assert package_dir == package["package_dir"]
        assert torrent_file_path == str(torrent_file)
        return {"submitted": True, "response": {"id": "999"}}

    result = await upload_mteam_from_package(
        {"TRACKERS": {"MTEAM": {"api_key": "fake"}}},
        package["package_dir"],
        str(torrent_file),
        execute=True,
        confirm_upload=True,
        uploader=fake_uploader,
    )

    assert result["status"] == "uploaded"
    assert result["upload_result"]["response"]["id"] == "999"
    assert result["uploaded_torrent_id"] == "999"
    assert len(result["submitted_torrent_hash"]) == 40


@pytest.mark.asyncio
async def test_mteam_live_upload_can_download_uploaded_torrent(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    async def fake_uploader(_config, _package_dir, _torrent_file_path):
        return {"submitted": True, "response": {"torrentId": "999"}}

    async def fake_downloader(_config, torrent_id, output_dir):
        path = tmp_path / output_dir / f"MTEAM-{torrent_id}.torrent"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"d4:infod")
        return str(path)

    result = await upload_mteam_from_package(
        {"TRACKERS": {"MTEAM": {"api_key": "fake"}}},
        package["package_dir"],
        str(torrent_file),
        execute=True,
        confirm_upload=True,
        download_uploaded=True,
        uploaded_output_dir="uploaded",
        uploader=fake_uploader,
        downloader=fake_downloader,
    )

    assert result["status"] == "uploaded"
    assert result["downloaded_torrent"]["torrent_id"] == "999"
    assert result["downloaded_torrent"]["path"].endswith("MTEAM-999.torrent")


@pytest.mark.asyncio
async def test_mteam_live_upload_reports_missing_uploaded_torrent_id(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    async def fake_uploader(_config, _package_dir, _torrent_file_path):
        return {"submitted": True, "response": {"message": "ok"}}

    result = await upload_mteam_from_package(
        {"TRACKERS": {"MTEAM": {"api_key": "fake"}}},
        package["package_dir"],
        str(torrent_file),
        execute=True,
        confirm_upload=True,
        download_uploaded=True,
        uploader=fake_uploader,
    )

    assert result["status"] == "uploaded-needs-review"
    assert "torrent id" in result["blockers"][0]


def test_extract_mteam_uploaded_torrent_id_accepts_known_keys() -> None:
    assert extract_mteam_uploaded_torrent_id({"response": {"id": 1}}) == "1"
    assert extract_mteam_uploaded_torrent_id({"response": {"torrentId": "2"}}) == "2"
    assert extract_mteam_uploaded_torrent_id({"response": {"torrent_id": "3"}}) == "3"
    assert extract_mteam_uploaded_torrent_id({"response": {"data": {"id": 4}}}) == "4"
    assert extract_mteam_uploaded_torrent_id({"response": {"data": "5"}}) == "5"
    assert extract_mteam_uploaded_torrent_id({"response": 6}) == "6"
    assert extract_mteam_uploaded_torrent_id({"response": "7"}) == "7"
    assert extract_mteam_uploaded_torrent_id({"response": {"message": "ok"}}) is None


def test_mteam_upload_response_summary_preserves_scalar_torrent_id() -> None:
    assert ptcli_target._summarize_mteam_upload_response("999") == {"raw_type": "str", "id": "999"}
    assert ptcli_target._summarize_mteam_upload_response({"data": {"torrentId": "1000"}, "message": "ok"}) == {"message": "ok", "id": "1000"}


def test_mteam_upload_payload_summary_blocks_missing_torrent(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path))
    from_disk = build_mteam_upload_preflight(package["package_dir"])

    assert from_disk["status"] == "blocked"
    assert "torrent file is required" in from_disk["upload_payload"]["blockers"][0]


def test_mteam_upload_payload_summary_blocks_invalid_optional_urls(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": "1291546",
        "douban_url": "https://example.com/not-douban",
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], torrent_file=str(torrent_file))

    assert preflight["status"] == "blocked"
    douban_check = next(check for check in preflight["upload_payload"]["field_checks"] if check["name"] == "payload.douban")
    assert douban_check["ok"] is False
    assert any("payload.douban" in blocker for blocker in preflight["upload_payload"]["blockers"])


def test_mteam_upload_payload_summary_blocks_unknown_standard(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], torrent_file=str(torrent_file))

    assert preflight["status"] == "blocked"
    standard_check = next(check for check in preflight["upload_payload"]["field_checks"] if check["name"] == "payload.standard")
    assert standard_check["ok"] is False
    assert any("payload.standard" in blocker for blocker in preflight["upload_payload"]["blockers"])


def test_mteam_upload_payload_summary_writes_payload_file(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], torrent_file=str(torrent_file), write_payload=True)

    assert preflight["status"] == "ready"
    assert preflight["files"]["upload_payload"].endswith("mteam-upload-payload.json")
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-upload-payload.json").exists()
    assert preflight["upload_payload"]["description_file"]["exists"] is True
    assert preflight["upload_payload"]["description_file"]["char_length"] == preflight["upload_payload"]["form_fields"]["descr"]["length"]
    assert all(check["ok"] for check in preflight["upload_payload"]["material_checks"])


def test_mteam_upload_payload_summary_blocks_missing_description_file(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    package_from_disk = load_mteam_prepare_package(package["package_dir"])
    Path(package_from_disk["files"]["description_draft"]).unlink()
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    payload = ptcli_target.build_mteam_upload_payload_summary(package_from_disk, torrent_file=str(torrent_file))

    assert payload["description_file"]["exists"] is False
    assert any(check["name"] == "payload.description_file" and check["ok"] is False for check in payload["material_checks"])
    assert any("payload.description_file" in blocker for blocker in payload["blockers"])


def test_target_upload_command_outputs_preflight_json(tmp_path, capsys) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path))
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    code = main(["target-upload", "--package-dir", package["package_dir"], "--torrent-file", str(torrent_file), "--write-payload", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"target_tracker": "MTEAM"' in out
    assert '"upload_gate"' in out
    assert '"upload_payload"' in out


def test_target_upload_execute_requires_confirmation_before_config_load(tmp_path, capsys) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    code = main(["target-upload", "--config", "/missing/config.py", "--package-dir", package["package_dir"], "--torrent-file", str(torrent_file), "--execute", "--json"])

    assert code == 1
    out = capsys.readouterr().out
    assert "confirm-upload" in out
    assert "Config file not found" not in out


def test_target_upload_execute_requires_uploaded_torrent_followup_before_config_load(tmp_path, capsys) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    code = main(["target-upload", "--config", "/missing/config.py", "--package-dir", package["package_dir"], "--torrent-file", str(torrent_file), "--execute", "--confirm-upload", "--json"])

    assert code == 1
    out = capsys.readouterr().out
    assert "download-uploaded-torrent" in out
    assert "inject-uploaded-torrent" in out
    assert "wait-uploaded-complete" in out
    assert "Config file not found" not in out


def test_target_upload_uploaded_torrent_followup_requires_save_path_without_package_content(tmp_path, capsys) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), None, str(tmp_path), accept_rules=True)
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")

    code = main(
        [
            "target-upload",
            "--package-dir",
            package["package_dir"],
            "--uploaded-torrent-file",
            str(uploaded_torrent),
            "--inject-uploaded-torrent",
            "--json",
        ]
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "uploaded-save-path" in out


@pytest.mark.asyncio
async def test_target_upload_injects_downloaded_torrent(monkeypatch, tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {"TRACKERS": {"MTEAM": {"api_key": "fake"}}})
    uploaded_hash = "f" * 40

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        uploaded_path = tmp_path / "MTEAM-999.torrent"
        uploaded_path.write_bytes(b"d4:infod")
        return {
            "status": "uploaded",
            "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_path)},
        }

    async def fake_search_mteam_duplicates(_config, source_info):
        assert source_info["imdb_id"] == 1234567
        assert source_info["content_path"] == "/downloads/Example"
        return {"searched": True, "query": {"imdb": "tt1234567"}, "count": 0, "dupes": []}

    async def fake_inject_source_with_config(_config, client_name, torrent_path, save_path, category, tags, paused):
        return {
            "client": client_name,
            "hash": uploaded_hash,
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
            "verified_in_client": True,
        }

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {
            "client": client_name,
            "complete": True,
            "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval},
            "matches": [{"hash": torrent_hash, "content_path": content_path}],
        }

    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--config",
            "config.py",
            "--package-dir",
            package["package_dir"],
            "--torrent-file",
            str(torrent_file),
            "--execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--uploaded-paused",
            "--wait-uploaded-complete",
            "--write-summary",
            "--summary-output-dir",
            str(tmp_path / "summary"),
            "--client",
            "default",
            "--json",
        ]
    )

    result = await ptcli_cli.target_upload_payload(args)

    assert result["status"] == "uploaded"
    assert result["fresh_duplicate_check"]["searched"] is True
    assert result["fresh_duplicate_check"]["count"] == 0
    assert result["uploaded_torrent_hash"] == uploaded_hash
    assert result["downloaded_torrent"]["hash"] == uploaded_hash
    assert result["downloaded_torrent"]["exists"] is True
    assert result["downloaded_torrent"]["size_bytes"] == len(b"d4:infod")
    assert len(result["downloaded_torrent"]["sha1"]) == 40
    assert result["injected_torrent"]["save_path"] == "/downloads/Example"
    assert result["injected_torrent"]["category"] == "MTEAM"
    assert result["injected_torrent"]["tags"] == "retorrent"
    assert result["uploaded_wait"]["complete"] is True
    assert result["uploaded_wait"]["query"]["torrent_hash"] == uploaded_hash
    assert result["uploaded_wait"]["query"]["content_path"] == "/downloads/Example"
    summary_path = Path(result["summary_file"])
    assert summary_path == tmp_path / "summary" / "ptcli-target-upload-summary.json"
    summary_payload = json.loads(await asyncio.to_thread(summary_path.read_text, encoding="utf-8"))
    assert summary_payload["schema_version"] == 1
    assert summary_payload["kind"] == "ptcli.target_upload.summary"
    assert summary_payload["summary_file"] == str(summary_path)
    assert summary_payload["client"] == "default"
    assert summary_payload["qbit_options"] == {"uploaded": {"category": "MTEAM", "tags": "retorrent", "paused": True}}
    assert summary_payload["summary"]["uploaded"] is True
    assert summary_payload["summary"]["injected"] is True
    assert summary_payload["summary"]["injection_verified"] is True
    assert summary_payload["summary"]["seeding_verified"] is True
    assert summary_payload["summary"]["uploaded_torrent_hash"] == uploaded_hash
    assert summary_payload["summary"]["injected_torrent_hash"] == uploaded_hash
    assert summary_payload["summary"]["uploaded_save_path"] == "/downloads/Example"
    assert summary_payload["summary"]["uploaded_torrent_path"] == str(tmp_path / "MTEAM-999.torrent")
    assert summary_payload["summary"]["uploaded_torrent"]["path"] == str(tmp_path / "MTEAM-999.torrent")
    assert summary_payload["summary"]["uploaded_torrent"]["exists"] is True
    assert summary_payload["summary"]["uploaded_torrent"]["size_bytes"] == len(b"d4:infod")
    assert len(summary_payload["summary"]["uploaded_torrent"]["sha1"]) == 40
    assert summary_payload["summary"]["uploaded_wait"]["complete"] is True
    assert summary_payload["summary"]["qbit_closure"]["injection"]["save_path"] == "/downloads/Example"
    assert summary_payload["summary"]["qbit_closure"]["injection"]["category"] == "MTEAM"
    assert summary_payload["summary"]["qbit_closure"]["injection"]["tags"] == "retorrent"
    assert summary_payload["summary"]["qbit_closure"]["wait"]["complete"] is True
    assert summary_payload["summary"]["qbit_closure"]["wait"]["query"]["torrent_hash"] == uploaded_hash
    assert summary_payload["qbit_wait_mismatch"] is False
    assert summary_payload["qbit_wait_mismatches"] == []
    assert summary_payload["qbit_wait_diagnostics"]["uploaded"]["complete"] is True
    assert summary_payload["qbit_wait_diagnostics"]["uploaded"]["requested_hash_matched"] is None
    assert summary_payload["summary"]["fresh_duplicate_check"] == {"searched": True, "query": {"imdb": "tt1234567"}, "count": 0, "dupes": []}
    assert summary_payload["summary"]["hash_consistent"] is True
    assert summary_payload["summary"]["duplicate_clean"] is True
    assert summary_payload["summary"]["rule_obligations"]["ready"] is True
    assert summary_payload["artifacts"]["package_dir"]["is_dir"] is True
    assert summary_payload["artifacts"]["package_content_path"]["path"] == "/downloads/Example"
    assert summary_payload["artifacts"]["package_content_path"]["exists"] is False
    assert summary_payload["artifacts"]["target_torrent_file"]["is_file"] is True
    assert summary_payload["artifacts"]["uploaded_torrent_file"]["path"] == str(tmp_path / "MTEAM-999.torrent")
    assert summary_payload["artifacts"]["uploaded_torrent_file"]["is_file"] is True
    assert summary_payload["artifacts"]["fresh_duplicate_check"] == {"searched": True, "query": {"imdb": "tt1234567"}, "count": 0, "dupes": []}
    assert summary_payload["artifacts"]["uploaded_wait_evidence"] is True
    assert summary_payload["artifacts"]["target_hash_consistent"] is True
    assert summary_payload["artifacts"]["target_duplicate_clean"] is True
    assert summary_payload["artifacts"]["target_rule_obligations"]["ready"] is True
    assert summary_payload["resume_state"]["ready"] is True
    assert summary_payload["resume_state"]["next_stage"] is None
    assert summary_payload["resume_state"]["next_command"] is None
    assert summary_payload["resume_state"]["artifacts"]["package_content_path"] is True
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_file"] is True
    assert summary_payload["resume_state"]["artifacts"]["uploaded_wait_evidence"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_hash_consistent"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_duplicate_clean"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_rule_obligations"] is True
    commands = {command["stage"]: command["command"] for command in summary_payload["recommended_commands"]}
    command_argv = {command["stage"]: command["argv"] for command in summary_payload["recommended_commands"]}
    assert "target-upload-retry" in commands
    assert command_argv["target-upload-retry"][:3] == ["python3", "ptcli.py", "target-upload"]
    assert str(tmp_path / "MTEAM-999.torrent") in commands["resume-uploaded-torrent"]
    assert str(tmp_path / "MTEAM-999.torrent") in command_argv["resume-uploaded-torrent"]
    assert "--client default" in commands["resume-uploaded-torrent"]
    assert "--uploaded-save-path /downloads/Example" in commands["resume-uploaded-torrent"]
    assert "--uploaded-qbit-category MTEAM" in commands["resume-uploaded-torrent"]
    assert "--uploaded-qbit-tags retorrent" in commands["resume-uploaded-torrent"]
    assert "--uploaded-paused" in commands["resume-uploaded-torrent"]
    assert f"--summary-output-dir {shlex.quote(str(tmp_path / 'summary'))}" in commands["resume-uploaded-torrent"]
    assert command_argv["verify-seeding"] == ["python3", "ptcli.py", "inspect", "--client", "default", "--json"]
    assert commands["verify-seeding"].startswith("python3 ptcli.py inspect")


@pytest.mark.asyncio
async def test_target_upload_execute_blocks_on_fresh_duplicate_check(monkeypatch, tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {"TRACKERS": {"MTEAM": {"api_key": "fake"}}})

    async def fake_search_mteam_duplicates(_config, dupe_source_info):
        assert dupe_source_info["imdb_id"] == 1234567
        return {"searched": True, "query": {"imdb": "tt1234567"}, "count": 1, "dupes": [{"name": "Existing"}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        raise AssertionError("live upload must not run when fresh MTEAM duplicate check finds matches")

    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--config",
            "config.py",
            "--package-dir",
            package["package_dir"],
            "--torrent-file",
            str(torrent_file),
            "--execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--wait-uploaded-complete",
            "--json",
        ]
    )

    result = await ptcli_cli.target_upload_payload(args)

    assert result["status"] == "blocked"
    assert result["fresh_duplicate_check"]["count"] == 1
    assert any("fresh_duplicate_check" in blocker for blocker in result["blockers"])


@pytest.mark.asyncio
async def test_target_upload_recovery_blocks_missing_runtime_dependency(monkeypatch, tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")
    original_find_spec = ptcli_doctor.importlib.util.find_spec

    def fake_find_spec(module):
        if module == "qbittorrentapi":
            return None
        return original_find_spec(module)

    async def fake_inject_source_with_config(*_args, **_kwargs):
        raise AssertionError("qBittorrent injection must not run when runtime dependencies are missing")

    monkeypatch.setattr(ptcli_doctor.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--package-dir",
            package["package_dir"],
            "--uploaded-torrent-file",
            str(uploaded_torrent),
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            "/downloads/Example",
            "--wait-uploaded-complete",
            "--write-summary",
            "--json",
        ]
    )

    result = await ptcli_cli.target_upload_payload(args)

    assert result["status"] == "blocked"
    assert result["runtime_check"]["ok"] is False
    assert "runtime-check: Missing PTCLI runtime dependencies" in result["blockers"][0]
    summary_payload = json.loads((Path(package["package_dir"]) / "ptcli-target-upload-summary.json").read_text(encoding="utf-8"))
    assert summary_payload["preflight"]["runtime_check"]["ok"] is False
    assert summary_payload["summary"]["ready"] is False
    assert summary_payload["resume_state"]["ready"] is False
    assert summary_payload["resume_state"]["next_stage"] == "resume-uploaded-torrent"
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_file"] is True
    commands = {command["stage"]: command["command"] for command in summary_payload["recommended_commands"]}
    assert summary_payload["resume_state"]["next_command"] == commands["resume-uploaded-torrent"]
    assert "--inject-uploaded-torrent" in commands["target-upload-retry"]


def test_target_upload_summary_recommends_uploaded_id_resume(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")
    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(torrent_file))
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--package-dir",
            package["package_dir"],
            "--torrent-file",
            str(torrent_file),
            "--execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            "/mnt/seedbox/Example",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--uploaded-paused",
            "--write-summary",
            "--summary-output-dir",
            str(tmp_path / "summary"),
            "--client",
            "default",
            "--json",
        ]
    )

    summary_file = ptcli_cli._write_target_upload_summary({"status": "uploaded", "uploaded_torrent_id": "999"}, preflight, args, package["package_dir"])

    summary_payload = json.loads(Path(summary_file).read_text(encoding="utf-8"))
    assert summary_payload["summary"]["uploaded_torrent_id"] == "999"
    assert summary_payload["artifacts"]["uploaded_torrent_id"] == "999"
    commands = {command["stage"]: command["command"] for command in summary_payload["recommended_commands"]}
    assert summary_payload["resume_state"]["resume_available"] is True
    assert summary_payload["resume_state"]["next_stage"] == "resume-uploaded-torrent-download"
    assert summary_payload["resume_state"]["next_command"] == commands["resume-uploaded-torrent-download"]
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_id"] is True
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_file"] is False
    assert summary_payload["artifacts"]["uploaded_save_path"]["path"] == "/mnt/seedbox/Example"
    command_argv = {command["stage"]: command["argv"] for command in summary_payload["recommended_commands"]}
    assert "--uploaded-torrent-id 999" in commands["resume-uploaded-torrent-download"]
    assert command_argv["resume-uploaded-torrent-download"][:3] == ["python3", "ptcli.py", "target-upload"]
    assert "999" in command_argv["resume-uploaded-torrent-download"]
    assert "--download-uploaded-torrent" in commands["resume-uploaded-torrent-download"]
    assert "--inject-uploaded-torrent" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-save-path /mnt/seedbox/Example" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-category MTEAM" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-tags retorrent" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-paused" in commands["resume-uploaded-torrent-download"]
    assert f"--summary-output-dir {shlex.quote(str(tmp_path / 'summary'))}" in commands["resume-uploaded-torrent-download"]


def test_target_upload_summary_exposes_uploaded_wait_mismatch(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")
    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(uploaded_torrent))
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--package-dir",
            package["package_dir"],
            "--uploaded-torrent-file",
            str(uploaded_torrent),
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            "/downloads/Example",
            "--wait-uploaded-complete",
            "--write-summary",
            "--json",
        ]
    )
    result = {
        "status": "blocked",
        "uploaded_wait": {
            "complete": True,
            "query": {"torrent_hash": "b" * 40, "content_path": "/downloads/Example"},
            "matches": [{"hash": "b" * 40, "content_path": "/downloads/Other"}],
            "completion_verification": {
                "matched_count": 1,
                "complete_count": 1,
                "any_complete": True,
                "requested_hash_matched": True,
                "requested_content_path_matched": False,
                "observed_hashes": ["b" * 40],
                "observed_content_paths": ["/downloads/Other"],
                "observed_save_paths": ["/downloads"],
            },
        },
    }

    summary_file = ptcli_cli._write_target_upload_summary(result, preflight, args, package["package_dir"])

    summary_payload = json.loads(Path(summary_file).read_text(encoding="utf-8"))
    assert summary_payload["qbit_wait_mismatch"] is True
    assert summary_payload["qbit_wait_mismatches"] == ["uploaded.requested_content_path"]
    diagnostics = summary_payload["qbit_wait_diagnostics"]["uploaded"]
    assert diagnostics["complete"] is True
    assert diagnostics["request_mismatch"] is True
    assert diagnostics["requested_hash_matched"] is True
    assert diagnostics["requested_content_path_matched"] is False
    assert diagnostics["observed_hashes"] == ["b" * 40]
    assert diagnostics["observed_content_paths"] == ["/downloads/Other"]


@pytest.mark.asyncio
async def test_target_upload_downloads_uploaded_torrent_by_id(monkeypatch, tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {"TRACKERS": {"MTEAM": {"api_key": "fake"}}})
    uploaded_path = tmp_path / "uploaded" / "MTEAM-999.torrent"
    uploaded_hash = "e" * 40

    async def fake_download_mteam_uploaded_torrent(_config, torrent_id, output_dir):
        assert torrent_id == "999"
        assert output_dir == "uploaded"
        uploaded_path.parent.mkdir(parents=True, exist_ok=True)
        uploaded_path.write_bytes(b"d4:infod")
        return {"status": "uploaded", "uploaded_torrent_id": torrent_id, "downloaded_torrent": {"torrent_id": torrent_id, "path": str(uploaded_path)}}

    async def fake_inject_source_with_config(_config, client_name, torrent_path, save_path, category, tags, paused):
        return {
            "client": client_name,
            "hash": uploaded_hash,
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
        }

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {
            "client": client_name,
            "complete": True,
            "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval},
            "matches": [{"hash": torrent_hash, "content_path": content_path}],
        }

    monkeypatch.setattr(ptcli_cli, "download_mteam_uploaded_torrent", fake_download_mteam_uploaded_torrent)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--config",
            "config.py",
            "--package-dir",
            package["package_dir"],
            "--uploaded-torrent-id",
            "999",
            "--download-uploaded-torrent",
            "--uploaded-output-dir",
            "uploaded",
            "--inject-uploaded-torrent",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--uploaded-paused",
            "--wait-uploaded-complete",
            "--write-summary",
            "--client",
            "default",
            "--json",
        ]
    )

    result = await ptcli_cli.target_upload_payload(args)

    assert result["status"] == "uploaded"
    assert result["uploaded_torrent_id"] == "999"
    assert result["downloaded_torrent"]["path"] == str(uploaded_path)
    assert result["downloaded_torrent"]["exists"] is True
    assert result["injected_torrent"]["save_path"] == "/downloads/Example"
    assert result["injected_torrent"]["category"] == "MTEAM"
    assert result["injected_torrent"]["tags"] == "retorrent"
    assert result["injected_torrent"]["paused"] is True
    assert result["uploaded_wait"]["complete"] is True


@pytest.mark.asyncio
async def test_target_upload_download_only_records_uploaded_torrent_file_evidence(monkeypatch, tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    uploaded_path = make_mteam_safe_torrent(tmp_path, "uploaded-download-only")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {"TRACKERS": {"MTEAM": {"api_key": "fake"}}})

    async def fake_download_mteam_uploaded_torrent(_config, torrent_id, output_dir):
        assert torrent_id == "999"
        _ = output_dir
        return {"status": "uploaded", "uploaded_torrent_id": torrent_id, "downloaded_torrent": {"torrent_id": torrent_id, "path": uploaded_path}}

    monkeypatch.setattr(ptcli_cli, "download_mteam_uploaded_torrent", fake_download_mteam_uploaded_torrent)
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--config",
            "config.py",
            "--package-dir",
            package["package_dir"],
            "--uploaded-torrent-id",
            "999",
            "--download-uploaded-torrent",
            "--write-summary",
            "--json",
        ]
    )

    result = await ptcli_cli.target_upload_payload(args)

    assert result["status"] == "uploaded"
    assert result["downloaded_torrent"]["exists"] is True
    assert result["downloaded_torrent"]["size_bytes"] > 0
    assert len(result["downloaded_torrent"]["sha1"]) == 40
    assert result["downloaded_torrent"]["torrent_hash"] == Torrent.read(uploaded_path, validate=False).infohash
    assert result["summary"]["uploaded_torrent"]["exists"] is True
    assert result["summary"]["uploaded_torrent_hash"] == result["downloaded_torrent"]["torrent_hash"]


@pytest.mark.asyncio
async def test_target_upload_reuses_uploaded_torrent_file(monkeypatch, tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")
    uploaded_hash = "f" * 40
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {"TRACKERS": {"MTEAM": {"api_key": "fake"}}})

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        raise AssertionError("live upload must not run when --uploaded-torrent-file is provided")

    async def fake_inject_source_with_config(_config, client_name, torrent_path, save_path, category, tags, paused):
        return {
            "client": client_name,
            "hash": uploaded_hash,
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
            "verified_in_client": True,
        }

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {
            "client": client_name,
            "complete": True,
            "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval},
            "matches": [{"hash": torrent_hash, "content_path": content_path}],
        }

    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--config",
            "config.py",
            "--package-dir",
            package["package_dir"],
            "--uploaded-torrent-file",
            str(uploaded_torrent),
            "--inject-uploaded-torrent",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--wait-uploaded-complete",
            "--write-summary",
            "--client",
            "default",
            "--json",
        ]
    )

    result = await ptcli_cli.target_upload_payload(args)

    assert result["status"] == "uploaded"
    assert result["downloaded_torrent"]["reused"] is True
    assert result["downloaded_torrent"]["path"] == str(uploaded_torrent)
    assert result["downloaded_torrent"]["exists"] is True
    assert result["downloaded_torrent"]["size_bytes"] > 0
    assert len(result["downloaded_torrent"]["sha1"]) == 40
    assert result["uploaded_torrent_hash"] == uploaded_hash
    assert result["injected_torrent"]["save_path"] == "/downloads/Example"
    assert result["summary"]["uploaded_save_path"] == "/downloads/Example"
    assert result["summary"]["downloaded"] is True
    assert result["summary"]["uploaded_torrent"]["path"] == str(uploaded_torrent)
    assert result["summary"]["uploaded_torrent"]["exists"] is True
    assert result["summary"]["uploaded_torrent"]["size_bytes"] > 0
    assert len(result["summary"]["uploaded_torrent"]["sha1"]) == 40
    assert result["summary"]["injected"] is True
    assert result["summary"]["injection_verified"] is True
    assert result["summary"]["seeding_verified"] is True
    assert result["summary"]["uploaded_torrent_path"] == str(uploaded_torrent)
    assert result["summary"]["injected_torrent_hash"] == uploaded_hash


@pytest.mark.asyncio
async def test_target_upload_uploaded_torrent_injection_requires_completion_wait(monkeypatch, tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")

    async def fake_inject_source_with_config(*_args, **_kwargs):
        raise AssertionError("uploaded torrent injection must not run without --wait-uploaded-complete")

    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--package-dir",
            package["package_dir"],
            "--uploaded-torrent-file",
            str(uploaded_torrent),
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            "/downloads/Example",
            "--json",
        ]
    )

    result = await ptcli_cli.target_upload_payload(args)

    assert result["status"] == "blocked"
    assert any("--wait-uploaded-complete is required" in blocker for blocker in result["blockers"])


@pytest.mark.asyncio
async def test_target_upload_wait_uses_hash_from_reused_uploaded_torrent_file(monkeypatch, tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")
    expected_hash = Torrent.read(str(uploaded_torrent), validate=False).infohash
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {"TRACKERS": {"MTEAM": {"api_key": "fake"}}})

    async def fake_inject_source_with_config(_config, client_name, torrent_path, save_path, category, tags, paused):
        return {
            "client": client_name,
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
            "verified_in_client": True,
        }

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {
            "client": client_name,
            "complete": True,
            "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval},
            "matches": [{"hash": torrent_hash, "content_path": content_path}],
        }

    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--config",
            "config.py",
            "--package-dir",
            package["package_dir"],
            "--uploaded-torrent-file",
            str(uploaded_torrent),
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            "/downloads/Example",
            "--wait-uploaded-complete",
            "--write-summary",
            "--client",
            "default",
            "--json",
        ]
    )

    result = await ptcli_cli.target_upload_payload(args)

    assert result["uploaded_torrent_hash"] == expected_hash
    assert result["downloaded_torrent"]["hash"] == expected_hash
    assert result["uploaded_wait"]["complete"] is True
    assert result["uploaded_wait"]["query"]["torrent_hash"] == expected_hash
    assert result["summary"]["uploaded_torrent_hash"] == expected_hash
    assert result["summary"]["injected_torrent_hash"] is None
    assert result["summary"]["seeding_verified"] is True


@pytest.mark.asyncio
async def test_target_upload_inject_requires_download_flag(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = mteam_ready_stages()
    package = write_mteam_prepare_package(source_info, ["MTEAM"], stages, "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--package-dir",
            package["package_dir"],
            "--torrent-file",
            str(torrent_file),
            "--execute",
            "--confirm-upload",
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            "/downloads/Example",
            "--json",
        ]
    )

    result = await ptcli_cli.target_upload_payload(args)

    assert result["status"] == "blocked"
    assert any("requires --download-uploaded-torrent" in blocker for blocker in result["blockers"])


@pytest.mark.asyncio
async def test_target_upload_execute_requires_uploaded_completion_wait(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--package-dir",
            package["package_dir"],
            "--torrent-file",
            str(torrent_file),
            "--execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            "/downloads/Example",
            "--json",
        ]
    )

    result = await ptcli_cli.target_upload_payload(args)

    assert result["status"] == "blocked"
    assert any("--wait-uploaded-complete is required" in blocker for blocker in result["blockers"])


def test_torrent_file_evidence_includes_infohash(tmp_path) -> None:
    content = tmp_path / "source.mkv"
    content.write_bytes(b"content")
    torrent_path = tmp_path / "source.torrent"
    torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
    torrent.generate()
    torrent.write(str(torrent_path), overwrite=True)

    evidence = ptcli_cli._torrent_file_evidence(torrent_path)

    assert evidence["exists"] is True
    assert evidence["size_bytes"] > 0
    assert len(evidence["sha1"]) == 40
    assert evidence["torrent_hash"] == torrent.infohash
    assert evidence["infohash"] == torrent.infohash


@pytest.mark.asyncio
async def test_qbit_service_adds_torrent_file_with_fake_client(tmp_path) -> None:
    fake_client = FakeQbitClient()
    content = tmp_path / "source.mkv"
    content.write_bytes(b"content")
    torrent_path = tmp_path / "source.torrent"
    torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
    torrent.generate()
    torrent.write(str(torrent_path), overwrite=True)
    service = QbitReadOnlyService({}, qbit_client=fake_client)

    result = await service.add_torrent_file(
        torrent_path=str(torrent_path),
        save_path="/downloads",
        category="pt",
        tags="U2",
        paused=True,
        verify_timeout=0,
    )

    assert result["save_path"] == "/downloads"
    assert result["hash"] == torrent.infohash
    assert result["verified_in_client"] is True
    assert result["verification_attempts"] == 1
    assert result["client_verification"]["visible"] is True
    assert result["client_verification"]["hash_matched"] is True
    assert result["client_verification"]["save_path_matched"] is True
    assert result["client_verification"]["category_matched"] is False
    assert result["client_verification"]["tags_matched"] is False
    assert result["client_verification"]["requested"] == {"hash": torrent.infohash, "save_path": "/downloads", "category": "pt", "tags": "U2"}
    assert result["client_matches"][0]["hash"] == torrent.infohash
    assert result["category"] == "pt"
    assert result["tags"] == "U2"
    assert fake_client.added_kwargs["save_path"] == "/downloads"
    assert fake_client.added_kwargs["category"] == "pt"
    assert fake_client.added_kwargs["tags"] == "U2"
    assert fake_client.added_kwargs["paused"] is True


@pytest.mark.asyncio
async def test_qbit_service_waits_for_added_torrent_visibility(tmp_path) -> None:
    fake_client = DelayedQbitClient(visible_after=3)
    content = tmp_path / "source.mkv"
    content.write_bytes(b"content")
    torrent_path = tmp_path / "source.torrent"
    torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
    torrent.generate()
    torrent.write(str(torrent_path), overwrite=True)
    service = QbitReadOnlyService({}, qbit_client=fake_client)

    result = await service.add_torrent_file(
        torrent_path=str(torrent_path),
        save_path="/downloads",
        verify_timeout=1.0,
        verify_interval=0.001,
    )

    assert result["verified_in_client"] is True
    assert result["verification_attempts"] == 3
    assert result["client_matches"][0]["hash"] == torrent.infohash


@pytest.mark.asyncio
async def test_qbit_service_reports_client_verification_for_requested_metadata(tmp_path) -> None:
    content = tmp_path / "source.mkv"
    content.write_bytes(b"content")
    torrent_path = tmp_path / "source.torrent"
    torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
    torrent.generate()
    torrent.write(str(torrent_path), overwrite=True)
    service = QbitReadOnlyService({}, qbit_client=TaggedQbitClient())

    result = await service.add_torrent_file(
        torrent_path=str(torrent_path),
        save_path="/downloads",
        category="MTEAM",
        tags="retorrent",
    )

    assert result["client_verification"]["visible"] is True
    assert result["client_verification"]["hash_matched"] is True
    assert result["client_verification"]["save_path_matched"] is True
    assert result["client_verification"]["category_matched"] is True
    assert result["client_verification"]["tags_matched"] is True


@pytest.mark.asyncio
async def test_qbit_service_waits_for_requested_metadata_verification(tmp_path) -> None:
    fake_client = DelayedTaggedQbitClient(metadata_after=3)
    content = tmp_path / "source.mkv"
    content.write_bytes(b"content")
    torrent_path = tmp_path / "source.torrent"
    torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
    torrent.generate()
    torrent.write(str(torrent_path), overwrite=True)
    service = QbitReadOnlyService({}, qbit_client=fake_client)

    result = await service.add_torrent_file(
        torrent_path=str(torrent_path),
        save_path="/downloads",
        category="MTEAM",
        tags="retorrent",
        verify_timeout=1.0,
        verify_interval=0.001,
    )

    assert result["verified_in_client"] is True
    assert result["verification_attempts"] == 3
    assert result["client_verification"]["visible"] is True
    assert result["client_verification"]["hash_matched"] is True
    assert result["client_verification"]["save_path_matched"] is True
    assert result["client_verification"]["category_matched"] is True
    assert result["client_verification"]["tags_matched"] is True


@pytest.mark.asyncio
async def test_qbit_service_rejects_visible_wrong_hash_after_add(tmp_path) -> None:
    content = tmp_path / "source.mkv"
    content.write_bytes(b"content")
    torrent_path = tmp_path / "source.torrent"
    torrent = Torrent(path=str(content), trackers=["https://source.example/announce"])
    torrent.generate()
    torrent.write(str(torrent_path), overwrite=True)
    service = QbitReadOnlyService({}, qbit_client=WrongHashQbitClient())

    result = await service.add_torrent_file(
        torrent_path=str(torrent_path),
        save_path="/downloads",
        verify_timeout=0,
    )

    assert result["verified_in_client"] is True
    assert result["client_verification"]["visible"] is True
    assert result["client_verification"]["hash_matched"] is False
    assert result["client_verification"]["requested"]["hash"] == torrent.infohash
    assert result["client_matches"][0]["hash"] == "f" * 40
    assert "expected infohash" in ptcli_cli._client_verification_blockers(result["client_verification"])[0]


@pytest.mark.asyncio
async def test_qbit_service_waits_for_completed_match() -> None:
    service = QbitReadOnlyService({}, qbit_client=FakeQbitClient())

    result = await service.wait_for_completion(content_path="/downloads/One", timeout=0, interval=0.1)

    assert result["complete"] is True
    assert result["query"]["mode"] == "content_path"
    assert result["query"]["content_path"] == "/downloads/One"
    assert result["matched_count"] == 1
    assert result["completion_verification"]["matched_count"] == 1
    assert result["completion_verification"]["complete_count"] == 1
    assert result["completion_verification"]["all_matches_complete"] is True
    assert result["completion_verification"]["requested_hash_matched"] is None
    assert result["completion_verification"]["requested_content_path_matched"] is True
    assert result["completion_verification"]["observed_hashes"] == ["a" * 40]
    assert result["completion_verification"]["observed_content_paths"] == ["/downloads/One"]
    assert result["matches"][0]["hash"] == "a" * 40


@pytest.mark.asyncio
async def test_qbit_service_wait_reports_seeding_state_summary() -> None:
    service = QbitReadOnlyService({}, qbit_client=SeedingStateQbitClient())

    result = await service.wait_for_completion(torrent_hash="b" * 40, timeout=0, interval=0.1)

    assert result["complete"] is True
    assert result["completion_verification"]["seeding_state_count"] == 1
    assert result["completion_verification"]["requested_hash_matched"] is True
    assert result["completion_verification"]["requested_content_path_matched"] is None
    assert result["completion_verification"]["observed_hashes"] == ["b" * 40]
    assert result["completion_verification"]["observed_content_paths"] == ["/downloads/One"]
    assert result["completion_verification"]["observed_states"] == ["uploading"]
    assert result["completion_verification"]["observed_progress"] == [1.0]


@pytest.mark.asyncio
async def test_qbit_service_wait_reports_hash_query() -> None:
    service = QbitReadOnlyService({}, qbit_client=FakeQbitClient())

    result = await service.wait_for_completion(torrent_hash="b" * 40, timeout=0, interval=0.1)

    assert result["complete"] is True
    assert result["query"]["mode"] == "hash"
    assert result["query"]["torrent_hash"] == "b" * 40
    assert result["matched_count"] == 1
    assert result["matches"][0]["hash"] == "b" * 40
    assert result["completion_verification"]["requested_hash_matched"] is True


@pytest.mark.asyncio
async def test_qbit_service_wait_rejects_wrong_hash_match() -> None:
    service = QbitReadOnlyService({}, qbit_client=WrongHashQbitClient())

    result = await service.wait_for_completion(torrent_hash="b" * 40, timeout=0, interval=0.1)

    assert result["complete"] is False
    assert result["completion_verification"]["complete_count"] == 1
    assert result["completion_verification"]["any_complete"] is True
    assert result["completion_verification"]["requested_hash_matched"] is False
    assert result["blockers"] == [f"qBittorrent matched torrents, but none matched requested hash {'b' * 40}."]


@pytest.mark.asyncio
async def test_qbit_service_wait_rejects_wrong_path_match() -> None:
    service = QbitReadOnlyService({}, qbit_client=WrongPathQbitClient())

    result = await service.wait_for_completion(content_path="/downloads/One", timeout=0, interval=0.1)

    assert result["complete"] is False
    assert result["completion_verification"]["complete_count"] == 1
    assert result["completion_verification"]["any_complete"] is True
    assert result["completion_verification"]["requested_content_path_matched"] is False
    assert result["blockers"] == ["qBittorrent matched torrents, but none matched requested path /downloads/One."]


@pytest.mark.asyncio
async def test_qbit_service_wait_reports_incomplete_blockers() -> None:
    service = QbitReadOnlyService({}, qbit_client=IncompleteQbitClient())

    result = await service.wait_for_completion(content_path="/downloads/One", timeout=0, interval=0.1)

    assert result["complete"] is False
    assert result["matched_count"] == 1
    assert result["completion_verification"]["complete_count"] == 0
    assert result["completion_verification"]["any_complete"] is False
    assert result["completion_verification"]["observed_hashes"] == ["a" * 40]
    assert result["completion_verification"]["observed_content_paths"] == ["/downloads/One"]
    assert result["blockers"] == ["qBittorrent matched the torrent but did not report it as complete before timeout."]


@pytest.mark.asyncio
async def test_qbit_service_wait_reports_missing_match_blockers() -> None:
    service = QbitReadOnlyService({}, qbit_client=EmptyQbitClient())

    result = await service.wait_for_completion(torrent_hash="b" * 40, timeout=0, interval=0.1)

    assert result["complete"] is False
    assert result["matched_count"] == 0
    assert result["blockers"] == [f"No qBittorrent torrent matched hash {'b' * 40}."]


@pytest.mark.asyncio
async def test_qbit_service_lists_with_fake_client() -> None:
    service = QbitReadOnlyService({}, qbit_client=FakeQbitClient())

    torrents = await service.list_torrents(torrent_hash="b" * 40)

    assert len(torrents) == 1
    assert torrents[0].hash == "b" * 40


@pytest.mark.asyncio
async def test_qbit_service_exports_with_fake_client(tmp_path) -> None:
    service = QbitReadOnlyService({}, qbit_client=FakeQbitClient())

    path = await service.export_torrent("A" * 40, str(tmp_path))

    assert path.name == f"{'a' * 40}.torrent"
    assert path.read_bytes() == f"torrent:{'a' * 40}".encode()


@pytest.mark.asyncio
async def test_qbit_service_rejects_bad_export_hash(tmp_path) -> None:
    service = QbitReadOnlyService({}, qbit_client=FakeQbitClient())

    with pytest.raises(ValueError, match="hex"):
        await service.export_torrent("../not-a-hash", str(tmp_path))
