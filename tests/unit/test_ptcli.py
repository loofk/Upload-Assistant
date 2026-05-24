import argparse
import asyncio
import json
import shlex
from pathlib import Path

import pytest
from torf import Torrent

import src.ptcli.cli as ptcli_cli
import src.ptcli.source as ptcli_source
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


class IncompleteQbitClient(FakeQbitClient):
    def torrents_info(self, **kwargs):
        if kwargs.get("torrent_hashes"):
            return [{"name": "One", "hash": kwargs["torrent_hashes"], "content_path": "/downloads/One", "progress": 0.5}]
        return [{"name": "One", "hash": "a" * 40, "content_path": "/downloads/One", "progress": 0.5}]


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


def test_normalize_tracker_aliases() -> None:
    assert normalize_tracker("m-team") == "MTEAM"
    assert normalize_tracker("pterclub") == "PTER"


def test_parse_tracker_list_deduplicates() -> None:
    assert parse_tracker_list("mteam, M-TEAM, tjupt") == ["MTEAM", "TJUPT"]


def test_help_surfaces_short_live_closure_commands() -> None:
    help_text = build_parser().format_help()

    assert "Common live closure commands:" in help_text
    assert "ptcli retorrent --from U2 --source-id 60635 --to MTEAM --execute" in help_text
    assert "ptcli pipeline --from U2 --source-id 60635 --to MTEAM --save-path /downloads" in help_text
    assert "ptcli doctor --from U2 --source-id 60635 --to MTEAM" in help_text
    assert "--target-torrent-file ./tmp/exported/mteam.torrent" in help_text


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
    assert '"blockers"' in out


def test_retorrent_plan_marks_non_reference_flow_blocker(capsys) -> None:
    code = main(["retorrent", "--from", "MTEAM", "--source-id", "123", "--to", "TJUPT", "--dry-run", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert "not one of the first reference flows" in out


def test_retorrent_plan_accepts_reference_flow_without_reference_blocker(capsys) -> None:
    code = main(["retorrent", "--from", "U2", "--source-id", "https://u2.dmhy.org/details.php?id=123", "--to", "MTEAM", "--dry-run", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert "not one of the first reference flows" not in out
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
    assert "--uploaded-save-path" not in resume_target
    assert "--download-uploaded-torrent" not in resume_target
    assert "--inject-uploaded-torrent" not in resume_target
    assert "--wait-uploaded-complete" not in resume_target
    assert "--uploaded-qbit-category MTEAM" in resume_target
    assert "--uploaded-qbit-tags retorrent" in resume_target

    resume_uploaded = commands["resume-uploaded-torrent"]
    assert "--uploaded-torrent-file ./tmp/uploaded/MTEAM-<id>.torrent" in resume_uploaded
    assert "--inject-uploaded-torrent" in resume_uploaded
    assert "--uploaded-save-path" not in resume_uploaded
    assert "--wait-uploaded-complete" in resume_uploaded
    assert "--uploaded-qbit-category MTEAM" in resume_uploaded
    assert "--uploaded-qbit-tags retorrent" in resume_uploaded


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
        return {
            "ready": True,
            "closure": {"complete": True, "blockers": [], "source": {"complete": True}, "target": {"uploaded": True, "injected": True}},
            "evidence": {
                "complete": True,
                "source": {"mode": "downloaded", "torrent_hash": "a" * 40, "content_path": "/downloads/Name"},
                "target": {"ready": True, "uploaded_torrent_hash": "b" * 40},
            },
            "summary": {"ready": True, "complete": True, "status": "complete"},
            "summary_file": str(tmp_path / "summary" / "ptcli-run-summary.json"),
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
    assert payload["next_actions"] == ["Retorrent closure is complete; verify the target tracker page and qBittorrent seeding state."]
    assert payload["ready"] is True
    assert payload["closure"]["source"]["complete"] is True
    assert payload["closure"]["target"]["injected"] is True
    assert payload["evidence"]["source"]["mode"] == "downloaded"
    assert payload["evidence"]["target"]["uploaded_torrent_hash"] == "b" * 40
    assert payload["summary"]["status"] == "complete"
    assert payload["summary_file"].endswith("ptcli-run-summary.json")
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
    assert pipeline_args.write_summary is True
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
    assert payload["next_actions"] == ["Inject the generated target torrent into qBittorrent with --inject-uploaded-torrent and a valid uploaded save path."]


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


def test_rule_check_command_ready_for_reference_flow_with_ack(capsys) -> None:
    code = main(["rule-check", "--from", "CHD", "--to", "MTEAM", "--accept-rules", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"ready": true' in out
    assert '"source_tracker": "CHD"' in out
    assert '"target_trackers": [' in out
    payload = json.loads(out)
    assert payload["rule_obligations"] == [
        {
            "tracker": "CHD",
            "role": "source",
            "action": "download_and_retorrent",
            "rules_url": "https://ptchdbits.co/rules.php",
            "acknowledged": True,
            "message": "CHD source download_and_retorrent rules have been acknowledged.",
        },
        {
            "tracker": "MTEAM",
            "role": "target",
            "action": "upload_and_seed",
            "rules_url": "https://kp.m-team.cc/rules",
            "acknowledged": True,
            "message": "MTEAM target upload_and_seed rules have been acknowledged.",
        },
    ]


def test_source_download_requires_target_rule_context(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {})

    code = main(["source-download", "--tracker", "U2", "--source-id", "60635", "--output-dir", "./tmp/source", "--accept-rules", "--json"])

    assert code == 1
    out = capsys.readouterr().out
    assert '"status": "blocked"' in out
    assert "--to is required" in out


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


def test_source_download_runs_after_rule_gate(monkeypatch, capsys, tmp_path) -> None:
    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, base_dir)
        torrent_path = tmp_path / output_dir / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {})
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
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["rule_check"]["ready"] is True
    assert payload["path"] == payload["source_torrent"]["path"]
    assert payload["source_torrent"]["path"].endswith("source-out/U2-60635.torrent")
    assert payload["source_torrent"]["exists"] is True
    assert payload["source_torrent"]["size_bytes"] == len(b"d4:infod")
    assert len(payload["source_torrent"]["sha1"]) == 40


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


def test_pipeline_next_actions_reports_closure_blockers() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--prepare-target", "--json"])
    actions = ptcli_cli._pipeline_next_actions(args, [], {"complete": False, "blockers": ["source.ready", "target.prepared", "target.uploaded", "target.downloaded"]})

    assert any("--source-torrent-file" in action for action in actions)
    assert any("--package-dir" in action for action in actions)
    assert any("--upload-target --target-execute --confirm-upload" in action for action in actions)
    assert any("--uploaded-torrent-file" in action for action in actions)


def test_pipeline_next_actions_reports_completed_closure() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--upload-target", "--json"])
    actions = ptcli_cli._pipeline_next_actions(args, [], {"complete": True, "blockers": []})

    assert actions == ["Retorrent closure is complete; verify the target tracker page and qBittorrent seeding state."]


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


def test_target_upload_result_accepts_completed_uploaded_torrent_injection() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is True


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


def test_target_upload_result_accepts_completed_uploaded_torrent_wait() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
        "uploaded_wait": {"complete": True, "matches": [{"hash": "a" * 40}]},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True, wait_uploaded_complete=True) is True


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
            "uploaded_torrent_hash": "b" * 40,
            "injected_torrent_hash": "b" * 40,
            "uploaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "exists": True, "size_bytes": 8, "sha1": "d" * 40},
            "uploaded_torrent_path": "/tmp/MTEAM-999.torrent",
        },
    }

    evidence = ptcli_cli._pipeline_evidence(closure)

    assert evidence["complete"] is True
    assert evidence["source"]["mode"] == "downloaded"
    assert evidence["source"]["torrent_hash"] == "a" * 40
    assert evidence["source"]["source_torrent"]["sha1"] == "c" * 40
    assert evidence["target"]["ready"] is True
    assert evidence["target"]["uploaded_torrent_hash"] == "b" * 40
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
        {"stage": "rule-check", "ok": True, "result": {"ready": True, "checks": [{"name": "rules_acknowledged", "ok": True, "message": "ok"}]}},
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
    assert summary["resume"]["used"] is False
    assert summary["source"]["mode"] == "matched"


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
        {"stage": "target-prepare", "ok": True, "result": {}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is True
    assert closure["blockers"] == []
    assert closure["source"]["ready"] is True
    assert closure["source"]["matched"] is True


def test_pipeline_closure_preserves_torrent_file_evidence() -> None:
    source_torrent = {"path": "/tmp/U2-60635.torrent", "exists": True, "size_bytes": 8, "sha1": "c" * 40}
    uploaded_torrent = {"path": "/tmp/MTEAM-999.torrent", "exists": True, "size_bytes": 9, "sha1": "d" * 40}
    stages = [
        {"stage": "source-download", "ok": True, "result": source_torrent},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "verified_in_client": True}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": True}},
        {"stage": "match", "ok": True, "result": {"matches": []}},
        {"stage": "target-prepare", "ok": True, "result": {}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
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


def test_pipeline_closure_requires_target_injection_client_verification() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "target-prepare", "ok": True, "result": {}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
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


def test_pipeline_closure_requires_uploaded_torrent_completion_when_waited() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "target-prepare", "ok": True, "result": {}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
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


def test_pipeline_closure_requires_source_injection_client_verification() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "result": {"torrent_path": "/tmp/U2-60635.torrent"}},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "verified_in_client": False}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": True}},
        {"stage": "match", "ok": True, "result": {"matches": []}},
        {"stage": "target-prepare", "ok": True, "result": {}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
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


def test_pipeline_evidence_reports_source_injection_verification() -> None:
    closure = {
        "complete": True,
        "blockers": [],
        "source": {
            "ready": True,
            "downloaded": True,
            "injected": True,
            "injection_verified": True,
            "injected_torrent_hash": "a" * 40,
            "complete": True,
            "matched": False,
            "torrent_hash": "a" * 40,
            "content_path": "/downloads/Name",
        },
        "target": {
            "prepared": True,
            "uploaded": True,
            "downloaded": True,
            "injected": True,
            "injection_verified": True,
            "torrent_file": "/tmp/mteam.torrent",
            "uploaded_torrent_hash": "b" * 40,
            "injected_torrent_hash": "b" * 40,
            "uploaded_torrent_path": "/tmp/MTEAM-999.torrent",
        },
    }

    evidence = ptcli_cli._pipeline_evidence(closure)

    assert evidence["source"]["injection_verified"] is True
    assert evidence["source"]["injected_torrent_hash"] == "a" * 40
    assert "source_torrent_path" in evidence["source"]
    assert "source_wait" in evidence["source"]
    assert evidence["target"]["injection_verified"] is True
    assert evidence["target"]["injected_torrent_hash"] == "b" * 40


def test_pipeline_evidence_reports_resume_sources() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "result": {"path": "/tmp/U2-60635.torrent", "reused": True}},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "verified_in_client": True}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": True}},
        {"stage": "match", "ok": True, "result": {"matches": []}},
        {"stage": "target-prepare", "ok": True, "result": {"package_dir": "/tmp/package", "reused": True}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "reused": True},
                "injected_torrent": {"hash": "b" * 40, "verified_in_client": True},
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
        {"stage": "target-prepare", "ok": True, "result": {}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40},
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
        {"stage": "target-prepare", "ok": True, "result": {}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40},
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
async def test_fetch_source_info_closes_tracker_session(monkeypatch) -> None:
    closed = {"value": False}

    class FakeSession:
        async def aclose(self):
            closed["value"] = True

    class FakeTracker:
        def __init__(self, config):
            self.config = config
            self.session = FakeSession()

        async def get_info_from_torrent_id(self, torrent_id, meta=None):
            _ = meta
            return (1234567, 2, f"Name-{torrent_id}", "a" * 40, "desc")

    monkeypatch.setitem(ptcli_source.SOURCE_TRACKER_CLASSES, "U2", FakeTracker)

    info = await ptcli_source.fetch_source_info({}, "U2", "60635")

    assert info.name == "Name-60635"
    assert closed["value"] is True


@pytest.mark.asyncio
async def test_fetch_source_info_closes_tracker_session_on_error(monkeypatch) -> None:
    closed = {"value": False}

    class FakeSession:
        async def aclose(self):
            closed["value"] = True

    class FakeTracker:
        def __init__(self, config):
            self.config = config
            self.session = FakeSession()

        async def get_info_from_torrent_id(self, torrent_id, meta=None):
            _ = (torrent_id, meta)
            raise RuntimeError("metadata failed")

    monkeypatch.setitem(ptcli_source.SOURCE_TRACKER_CLASSES, "U2", FakeTracker)

    with pytest.raises(RuntimeError, match="metadata failed"):
        await ptcli_source.fetch_source_info({}, "U2", "60635")

    assert closed["value"] is True


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
    assert payload["source_torrent_id"] == "60635"


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


def test_doctor_command_writes_summary_json(monkeypatch, tmp_path, capsys) -> None:
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
            "60635",
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
    assert payload["flow_check"]["ready"] is True
    assert payload["live_safe_to_attempt"] is False
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
    assert payload["stages"][1]["stage"] == "source-info"
    assert payload["stages"][1]["ok"] is False
    assert payload["stages"][1]["error"] == "source unavailable"


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
    assert payload["stages"][1]["ok"] is False
    assert "no usable identifiers" in payload["stages"][1]["error"]


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
        return {"client": "qbittorrent", "save_path": save_path}

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
        return {"client": "qbittorrent", "save_path": save_path, "hash": injected_hash}

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
        return {"client": "qbittorrent", "save_path": save_path}

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, content_path, torrent_hash, timeout, interval)
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": "/downloads/Name"}]}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path}]}

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
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path}]}

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
            "--write-summary",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert target_stage["result"]["upload_gate"]["ready"] is True
    assert target_stage["result"]["upload_gate"]["dupe_count"] == 0
    summary_path = Path(payload["summary_file"])
    assert summary_path == Path(target_stage["result"]["package_dir"]) / "ptcli-run-summary.json"
    summary_payload = json.loads(await asyncio.to_thread(summary_path.read_text, encoding="utf-8"))
    assert summary_payload["closure"] == payload["closure"]
    assert summary_payload["closure"]["complete"] is False
    assert summary_payload["closure"]["blockers"] == ["target.uploaded", "target.downloaded", "target.injected"]
    assert summary_payload["requested_actions"] == payload["requested_actions"]
    assert summary_payload["effective_actions"] == payload["effective_actions"]
    assert summary_payload["summary"]["ready"] is True
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
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        return {
            "status": "uploaded",
            "downloaded_torrent": {"torrent_id": "999", "path": str(tmp_path / "MTEAM-999.torrent")},
        }

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        return {
            "hash": uploaded_hash,
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
    assert payload["closure"]["blockers"] == []
    assert payload["closure"]["source"]["ready"] is True
    assert payload["closure"]["source"]["matched"] is True
    assert payload["closure"]["source"]["content_path"] == "/downloads/Name"
    assert payload["closure"]["target"]["prepared"] is True
    assert payload["closure"]["target"]["uploaded"] is True
    assert payload["closure"]["target"]["downloaded"] is True
    assert payload["closure"]["target"]["injected"] is True
    assert payload["closure"]["target"]["uploaded_torrent_hash"] == uploaded_hash


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
        return {"hash": "a" * 40, "torrent_path": torrent_path, "save_path": save_path, "category": category, "tags": tags, "paused": paused}

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
        return {"status": "uploaded", "downloaded_torrent": {"torrent_id": "999", "path": str(tmp_path / "MTEAM-999.torrent")}}

    async def fake_inject_uploaded_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        return {"hash": uploaded_hash, "torrent_path": torrent_path, "save_path": save_path, "category": category, "tags": tags, "paused": paused}

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
    assert summary_payload["artifacts"]["source_torrent_file"].endswith("U2-60635.torrent")
    assert summary_payload["artifacts"]["target_torrent_file"] == str(torrent_file)
    assert summary_payload["artifacts"]["target_package_dir"]
    assert summary_payload["artifacts"]["uploaded_torrent_file"] == str(tmp_path / "MTEAM-999.torrent")
    resume_commands = {command["stage"]: command["command"] for command in summary_payload["resume_commands"]}
    assert summary_payload["artifacts"]["source_torrent_file"] in resume_commands["resume-source-torrent"]
    assert str(torrent_file) in resume_commands["resume-target-upload"]
    assert str(tmp_path / "MTEAM-999.torrent") in resume_commands["resume-uploaded-torrent"]
    assert shlex.quote(summary_payload["artifacts"]["target_package_dir"]) in resume_commands["resume-target-upload"]
    assert "--uploaded-save-path /downloads" in resume_commands["resume-target-upload"]
    assert "--uploaded-save-path /downloads" in resume_commands["resume-uploaded-torrent"]
    assert any(stage["stage"] == "target-upload" and stage["ok"] is True for stage in summary_payload["stages"])


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
        return {
            "hash": uploaded_hash,
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
        }

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, content_path, torrent_hash, timeout, interval)
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": "/downloads/Name"}]}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        return {
            "status": "uploaded",
            "downloaded_torrent": {"torrent_id": "999", "path": str(tmp_path / "MTEAM-999.torrent")},
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
        return {"status": "uploaded", "torrent_file": torrent_file, "downloaded_torrent": {"torrent_id": "999", "path": str(tmp_path / "MTEAM-999.torrent")}}

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
    uploaded_hash = "b" * 40
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

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
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

    match_stage = next(stage for stage in payload["stages"] if stage["stage"] == "match")
    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert payload["path"] == "/downloads/Example"
    assert match_stage["ok"] is True
    assert match_stage.get("skipped") is not True
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["downloaded_torrent"]["reused"] is True
    assert upload_stage["result"]["downloaded_torrent"]["path"] == str(uploaded_torrent)
    assert upload_stage["result"]["uploaded_torrent_hash"] == uploaded_hash
    assert payload["closure"]["target"]["uploaded"] is True
    assert payload["closure"]["target"]["injected"] is True
    assert payload["evidence"]["resume"]["target_package"] is True
    assert payload["evidence"]["resume"]["uploaded_torrent_file"] is True
    assert payload["summary"]["resume"]["used"] is True


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
        return {"status": "uploaded", "torrent_file": torrent_file, "downloaded_torrent": {"torrent_id": "999", "path": str(tmp_path / "MTEAM-999.torrent")}}

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
        "site_specific_rules_encoded": False,
        "message": "Manual source/target rule review has been acknowledged.",
    }
    assert review["rule_obligations"] == [
        {
            "tracker": "U2",
            "role": "source",
            "action": "download_and_retorrent",
            "rules_url": "https://u2.dmhy.org/rules.php",
            "acknowledged": True,
            "message": "U2 source download_and_retorrent rules have been acknowledged.",
        },
        {
            "tracker": "MTEAM",
            "role": "target",
            "action": "upload_and_seed",
            "rules_url": "https://kp.m-team.cc/rules",
            "acknowledged": True,
            "message": "MTEAM target upload_and_seed rules have been acknowledged.",
        },
    ]


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
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-prepare-preview.json").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-description-draft.txt").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-rule-review.json").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-field-mapping.json").read_text(encoding="utf-8").strip().startswith("{")


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
    assert preflight["upload_payload"]["form_fields"]["name"] == source_info["name"]
    assert preflight["upload_payload"]["torrent_file"]["sha1"]
    assert preflight["next_actions"] == ["Review the package manually, then rerun with --execute --confirm-upload and the reviewed target torrent file when ready."]


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
    assert extract_mteam_uploaded_torrent_id({"response": {"message": "ok"}}) is None


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
            "--wait-uploaded-complete",
            "--write-summary",
            "--client",
            "default",
            "--json",
        ]
    )

    result = await ptcli_cli.target_upload_payload(args)

    assert result["status"] == "uploaded"
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
    assert summary_path == Path(package["package_dir"]) / "ptcli-target-upload-summary.json"
    summary_payload = json.loads(await asyncio.to_thread(summary_path.read_text, encoding="utf-8"))
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
    assert summary_payload["summary"]["rule_obligations"]["ready"] is True


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

    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
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
    assert result["summary"]["uploaded_torrent_path"] == str(uploaded_torrent)
    assert result["summary"]["injected_torrent_hash"] == uploaded_hash


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
    )

    assert result["save_path"] == "/downloads"
    assert result["hash"] == torrent.infohash
    assert result["verified_in_client"] is True
    assert result["verification_attempts"] == 1
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
async def test_qbit_service_waits_for_completed_match() -> None:
    service = QbitReadOnlyService({}, qbit_client=FakeQbitClient())

    result = await service.wait_for_completion(content_path="/downloads/One", timeout=0, interval=0.1)

    assert result["complete"] is True
    assert result["query"]["mode"] == "content_path"
    assert result["query"]["content_path"] == "/downloads/One"
    assert result["matched_count"] == 1
    assert result["matches"][0]["hash"] == "a" * 40


@pytest.mark.asyncio
async def test_qbit_service_wait_reports_hash_query() -> None:
    service = QbitReadOnlyService({}, qbit_client=FakeQbitClient())

    result = await service.wait_for_completion(torrent_hash="b" * 40, timeout=0, interval=0.1)

    assert result["complete"] is True
    assert result["query"]["mode"] == "hash"
    assert result["query"]["torrent_hash"] == "b" * 40
    assert result["matched_count"] == 1
    assert result["matches"][0]["hash"] == "b" * 40


@pytest.mark.asyncio
async def test_qbit_service_wait_reports_incomplete_blockers() -> None:
    service = QbitReadOnlyService({}, qbit_client=IncompleteQbitClient())

    result = await service.wait_for_completion(content_path="/downloads/One", timeout=0, interval=0.1)

    assert result["complete"] is False
    assert result["matched_count"] == 1
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
