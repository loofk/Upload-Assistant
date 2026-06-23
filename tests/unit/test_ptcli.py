import argparse
import asyncio
import json
import shlex
import subprocess
from pathlib import Path

import pytest
from torf import Torrent

import src.ptcli.cli as ptcli_cli
import src.ptcli.doctor as ptcli_doctor
import src.ptcli.metadata as ptcli_metadata
import src.ptcli.source as ptcli_source
import src.ptcli.target as ptcli_target
from src.ptcli.cli import _with_captured_stdout, build_parser, build_plan, main
from src.ptcli.config import resolve_client_config
from src.ptcli.credentials import build_flow_check
from src.ptcli.doctor import build_doctor_check
from src.ptcli.mainland import normalize_tracker, parse_tracker_list
from src.ptcli.materials import find_primary_media_file, generate_bdinfo_material, generate_mediainfo_material, generate_screenshot_materials, upload_screenshot_image_hosts
from src.ptcli.metadata import enrich_source_metadata, load_metadata_overrides, normalize_metadata_overrides
from src.ptcli.qbit import QbitReadOnlyService, match_torrents, summarize_torrent
from src.ptcli.rules import build_rule_check
from src.ptcli.source import create_source_meta, extract_torrent_id, source_info_from_tuple
from src.ptcli.target import (
    build_mteam_description_draft,
    build_mteam_field_mapping,
    build_mteam_materials_manifest,
    build_mteam_meta_draft,
    build_mteam_prepare_preview,
    build_mteam_rule_review,
    build_mteam_upload_gate,
    build_mteam_upload_preflight,
    create_mteam_upload_torrent_candidate,
    extract_mteam_uploaded_torrent_id,
    load_mteam_prepare_package,
    mteam_upload_torrent_candidate_summary,
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


def write_valid_torrent(torrent_path: Path, content_path: Path) -> str:
    content_path.parent.mkdir(parents=True, exist_ok=True)
    content_path.write_bytes(b"content")
    torrent = Torrent(path=str(content_path), trackers=["https://source.example/passkey/announce"])
    torrent.generate()
    torrent_path.parent.mkdir(parents=True, exist_ok=True)
    torrent.write(str(torrent_path), overwrite=True)
    return str(torrent.infohash)


def mteam_ready_stages() -> list[dict]:
    return [
        {"stage": "rule-check", "ok": True, "result": build_rule_check("U2", ["MTEAM"], accept_rules=True)},
        {"stage": "match", "ok": True, "result": {"count": 1, "matches": [{"content_path": "/downloads/Example", "hash": "a" * 40}]}},
        {"stage": "target-dupe-check", "ok": True, "result": {"searched": True, "count": 0, "dupes": []}},
    ]


def write_material_ready_mteam_package(source_info: dict, tmp_path: Path, content_path: str = "/downloads/Example", output_dir: str | None = None) -> dict:
    ready_source = {
        **source_info,
        "tmdb_id": source_info.get("tmdb_id") or 999,
        "douban_id": source_info.get("douban_id") or "1291546",
        "douban_url": source_info.get("douban_url") or "https://movie.douban.com/subject/1291546/",
        "ptgen_description": source_info.get("ptgen_description") or "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    material_dir = tmp_path / "material-fixtures" / str(ready_source.get("torrent_id", "unknown"))
    material_dir.mkdir(parents=True, exist_ok=True)
    mediainfo = material_dir / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    screenshot = material_dir / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_host_file = material_dir / "image-host-uploads.json"
    image_host_file.write_text(
        json.dumps({"items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}]}),
        encoding="utf-8",
    )
    return write_mteam_prepare_package(
        ready_source,
        ["MTEAM"],
        mteam_ready_stages(),
        content_path,
        output_dir or str(tmp_path),
        accept_rules=True,
        material_files={"mediainfo_file": str(mediainfo), "screenshot_files": [str(screenshot)], "image_host_file": str(image_host_file)},
    )


def patch_pipeline_live_material_stages(monkeypatch) -> None:
    def fake_pipeline_material_prerequisite_check(_config, _args):
        return {"name": "material.prerequisites", "ok": True, "message": "Material prerequisites are ready.", "checks": [], "blockers": []}

    async def fake_enrich_source_metadata(_config, source_info, *, overrides=None, fetch_ptgen=False, base_dir=None):
        _ = (overrides, base_dir)
        enriched = {
            **source_info,
            "tmdb_id": source_info.get("tmdb_id") or 999,
            "douban_id": source_info.get("douban_id") or "1291546",
            "douban_url": source_info.get("douban_url") or "https://movie.douban.com/subject/1291546/",
            "ptgen_description": source_info.get("ptgen_description") or "◎译　　名　示例电影\n◎简　　介　示例简介",
        }
        return {"status": "enriched", "ready": True, "source_info": enriched, "missing": [], "blockers": [], "sources": ["ptgen"] if fetch_ptgen else ["override"]}

    async def fake_generate_mediainfo_material(_content_path, output_dir):
        output_path = Path(output_dir) / "MI_FULL_00.txt"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("General\nComplete name : Name.mkv\n", encoding="utf-8")
        return {"status": "generated", "mediainfo_file": str(output_path), "blockers": []}

    async def fake_generate_screenshot_materials(_content_path, output_dir, count):
        files = []
        for index in range(1, int(count or 1) + 1):
            output_path = Path(output_dir) / f"screenshot-{index:02d}.png"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"png")
            files.append(str(output_path))
        return {"status": "generated", "screenshot_files": files, "count": len(files), "blockers": []}

    async def fake_upload_screenshot_image_hosts(_config, screenshot_files, output_dir, image_host=None):
        output_path = Path(output_dir) / "image-host-uploads.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "uploaded",
            "host": image_host,
            "count": len(screenshot_files),
            "items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"} for _ in screenshot_files],
            "blockers": [],
        }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return {**payload, "image_host_file": str(output_path)}

    monkeypatch.setattr(ptcli_cli, "_pipeline_material_prerequisite_check", fake_pipeline_material_prerequisite_check)
    monkeypatch.setattr(ptcli_cli, "enrich_source_metadata", fake_enrich_source_metadata)
    monkeypatch.setattr(ptcli_cli, "generate_mediainfo_material", fake_generate_mediainfo_material)
    monkeypatch.setattr(ptcli_cli, "generate_screenshot_materials", fake_generate_screenshot_materials)
    monkeypatch.setattr(ptcli_cli, "upload_screenshot_image_hosts", fake_upload_screenshot_image_hosts)


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
    assert payload["capabilities"]["U2"]["source_info_adapter"] == "generic_details_cookie"
    assert payload["capabilities"]["U2"]["source_download"] is True
    assert payload["capabilities"]["U2"]["source_download_adapter"] == "nexusphp_passkey"
    assert payload["capabilities"]["U2"]["credential_requirements"] == ["TRACKERS.U2.passkey", "data/cookies/U2.txt"]
    assert payload["capabilities"]["U2"]["full_live_closure_to_mteam"] is True
    assert payload["capabilities"]["HDS"]["source_info"] is True
    assert payload["capabilities"]["HDS"]["source_info_adapter"] == "generic_details_cookie"
    assert payload["capabilities"]["HDS"]["source_download"] is True
    assert payload["capabilities"]["HDS"]["source_download_adapter"] == "cookie_download"
    assert payload["capabilities"]["HDS"]["credential_requirements"] == ["data/cookies/HDS.txt"]
    assert payload["capabilities"]["HDS"]["full_live_closure_to_mteam"] is True
    assert payload["capabilities"]["TTG"]["source_download_adapter"] == "ttg_passkey"
    assert payload["capabilities"]["MTEAM"]["source_info_adapter"] == "mteam_api"
    assert payload["capabilities"]["MTEAM"]["source_download_adapter"] == "mteam_api"
    assert payload["capabilities"]["MTEAM"]["credential_requirements"] == ["TRACKERS.MTEAM.api_key"]
    assert payload["capabilities"]["MTEAM"]["target_upload"] is True
    assert "U2" in payload["full_live_closure_sources"]
    assert "HDS" in payload["full_live_closure_sources"]
    u2_flow = next(flow for flow in payload["flows"] if flow["source_tracker"] == "U2")
    assert u2_flow["target_tracker"] == "MTEAM"
    assert u2_flow["full_live_closure"] is True
    hds_flow = next(flow for flow in payload["flows"] if flow["source_tracker"] == "HDS")
    assert hds_flow["full_live_closure"] is True


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
    assert '"stage": "retorrent-resume-uploaded-torrent"' in out
    assert "retorrent --from U2 --source-id 123 --to MTEAM --execute --accept-rules --confirm-upload --package-dir ./tmp/target/U2-123-to-MTEAM --uploaded-torrent-file ./tmp/uploaded/MTEAM-<id>.torrent" in out
    assert "pipeline --from U2 --source-id 123 --to MTEAM --package-dir ./tmp/target/U2-123-to-MTEAM --upload-target --uploaded-torrent-file ./tmp/uploaded/MTEAM-<id>.torrent" in out
    assert '"stage": "retorrent-resume-uploaded-torrent-download"' in out
    assert "retorrent --from U2 --source-id 123 --to MTEAM --execute --accept-rules --confirm-upload --package-dir ./tmp/target/U2-123-to-MTEAM --uploaded-torrent-id <id>" in out
    assert "--uploaded-torrent-id <id> --download-uploaded-torrent --inject-uploaded-torrent" in out
    assert '"stage": "doctor-live"' in out
    assert "--target-execute --confirm-upload" in out
    assert "--package-dir ./tmp/target/U2-123-to-MTEAM" in out
    assert "--target-torrent-file ./tmp/exported/mteam.torrent" in out
    assert '"stage": "retorrent-execute"' in out
    assert "--execute --accept-rules --confirm-upload" in out
    assert "--download-uploaded-torrent" in out
    assert "--inject-uploaded-torrent" in out
    assert "--wait-uploaded-complete" in out
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
    plan_commands = ptcli_cli.build_plan_commands("U2", "60635", ["MTEAM"], "/downloads/Example")
    commands = {command["stage"]: command["command"] for command in plan_commands}
    command_argv = {command["stage"]: command["argv"] for command in plan_commands}

    resume_target = commands["resume-target-package"]
    assert "--target-execute --confirm-upload" in resume_target
    assert "--download-uploaded-torrent" in resume_target
    assert "--inject-uploaded-torrent" in resume_target
    assert '--uploaded-save-path "/downloads/Example"' in resume_target
    assert "--wait-uploaded-complete" in resume_target
    assert "--write-summary" in resume_target
    assert "--uploaded-qbit-category MTEAM" in resume_target
    assert "--uploaded-qbit-tags retorrent" in resume_target
    assert command_argv["resume-target-package"][:3] == ["python3", "ptcli.py", "pipeline"]
    assert "/downloads/Example" in command_argv["resume-target-package"]

    doctor_live = commands["doctor-live"]
    assert "--download-uploaded-torrent" in doctor_live
    assert "--inject-uploaded-torrent" in doctor_live
    assert "--wait-uploaded-complete" in doctor_live
    assert "--connect-qbit" in doctor_live
    assert "--probe-source" in doctor_live
    assert "--probe-target" in doctor_live
    assert command_argv["doctor-live"][:3] == ["python3", "ptcli.py", "doctor"]

    resume_uploaded = commands["resume-uploaded-torrent"]
    assert "--uploaded-torrent-file ./tmp/uploaded/MTEAM-<id>.torrent" in resume_uploaded
    assert "--upload-target" in resume_uploaded
    assert "--inject-uploaded-torrent" not in resume_uploaded
    assert '--uploaded-save-path "/downloads/Example"' in resume_uploaded
    assert "--wait-uploaded-complete" not in resume_uploaded
    assert "--uploaded-qbit-category MTEAM" in resume_uploaded
    assert command_argv["resume-uploaded-torrent"][:3] == ["python3", "ptcli.py", "pipeline"]
    assert "--upload-target" in command_argv["resume-uploaded-torrent"]
    assert "./tmp/uploaded/MTEAM-<id>.torrent" in command_argv["resume-uploaded-torrent"]
    assert "/downloads/Example" in command_argv["resume-uploaded-torrent"]
    assert "--uploaded-qbit-tags retorrent" in resume_uploaded

    resume_uploaded_download = commands["resume-uploaded-torrent-download"]
    assert "--uploaded-torrent-id <id>" in resume_uploaded_download
    assert "--upload-target" in resume_uploaded_download
    assert "--uploaded-output-dir ./tmp/uploaded" in resume_uploaded_download
    assert "--download-uploaded-torrent" not in resume_uploaded_download
    assert "--inject-uploaded-torrent" not in resume_uploaded_download
    assert '--uploaded-save-path "/downloads/Example"' in resume_uploaded_download
    assert "--wait-uploaded-complete" not in resume_uploaded_download
    assert "--uploaded-qbit-category MTEAM" in resume_uploaded_download
    assert "--uploaded-qbit-tags retorrent" in resume_uploaded_download
    assert command_argv["resume-uploaded-torrent-download"][:3] == ["python3", "ptcli.py", "pipeline"]
    assert "--upload-target" in command_argv["resume-uploaded-torrent-download"]
    assert "/downloads/Example" in command_argv["resume-uploaded-torrent-download"]

    retorrent_resume_uploaded = commands["retorrent-resume-uploaded-torrent"]
    assert "--package-dir ./tmp/target/U2-60635-to-MTEAM" in retorrent_resume_uploaded
    assert "--uploaded-torrent-file ./tmp/uploaded/MTEAM-<id>.torrent" in retorrent_resume_uploaded
    assert "--inject-uploaded-torrent" in retorrent_resume_uploaded
    assert '--uploaded-save-path "/downloads/Example"' in retorrent_resume_uploaded
    assert "--write-summary" in retorrent_resume_uploaded
    assert command_argv["retorrent-resume-uploaded-torrent"][:3] == ["python3", "ptcli.py", "retorrent"]
    assert "./tmp/uploaded/MTEAM-<id>.torrent" in command_argv["retorrent-resume-uploaded-torrent"]

    retorrent_resume_download = commands["retorrent-resume-uploaded-torrent-download"]
    assert "--uploaded-torrent-id <id>" in retorrent_resume_download
    assert "--download-uploaded-torrent" in retorrent_resume_download
    assert "--inject-uploaded-torrent" in retorrent_resume_download
    assert '--uploaded-save-path "/downloads/Example"' in retorrent_resume_download
    assert command_argv["retorrent-resume-uploaded-torrent-download"][:3] == ["python3", "ptcli.py", "retorrent"]

    retorrent_execute = commands["retorrent-execute"]
    assert "--fetch-ptgen" in retorrent_execute
    assert "--generate-mediainfo" in retorrent_execute
    assert "--generate-screenshots" in retorrent_execute
    assert "--upload-screenshots" in retorrent_execute
    assert "--download-uploaded-torrent" in retorrent_execute
    assert "--inject-uploaded-torrent" in retorrent_execute
    assert "--wait-uploaded-complete" in retorrent_execute
    assert command_argv["retorrent-execute"][:3] == ["python3", "ptcli.py", "retorrent"]


def test_retorrent_plan_commands_preserve_runtime_context() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "retorrent",
            "--config",
            "/etc/ua/config.py",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--path",
            "/downloads/Example",
            "--client",
            "seedbox",
            "--base-dir",
            "/srv/Upload-Assistant",
            "--dry-run",
            "--json",
        ]
    )

    plan = build_plan(args)
    command_argv = {command["stage"]: command["argv"] for command in plan.commands}
    commands = {command["stage"]: command["command"] for command in plan.commands}

    assert "--config /etc/ua/config.py" in commands["source-info"]
    assert "--base-dir /srv/Upload-Assistant" in commands["source-info"]
    assert "/etc/ua/config.py" in command_argv["source-download"]
    assert "/srv/Upload-Assistant" in command_argv["source-download"]
    assert "--client seedbox" in commands["resume-source-torrent"]
    assert "--config /etc/ua/config.py" in commands["resume-target-package"]
    assert "--client seedbox" in commands["resume-target-package"]
    assert "--base-dir /srv/Upload-Assistant" in commands["resume-target-package"]
    assert "--client seedbox" in commands["doctor-live"]
    assert "--base-dir /srv/Upload-Assistant" in commands["doctor-live"]
    assert "--config /etc/ua/config.py" in commands["retorrent-execute"]
    assert "--client seedbox" in commands["retorrent-execute"]
    assert "--base-dir /srv/Upload-Assistant" in commands["retorrent-execute"]
    assert "--fetch-ptgen" in command_argv["retorrent-execute"]
    assert "--generate-mediainfo" in command_argv["retorrent-execute"]
    assert "--generate-screenshots" in command_argv["retorrent-execute"]
    assert "--upload-screenshots" in command_argv["retorrent-execute"]
    assert "--client seedbox" in commands["match"]
    assert "/etc/ua/config.py" in command_argv["match"]
    assert "--base-dir" not in command_argv["match"]


def test_resume_target_package_reuses_existing_material_manifest_assets() -> None:
    payload = {
        "source_tracker": "U2",
        "source_torrent_id": "60635",
        "target_trackers": ["MTEAM"],
        "client": "default",
        "path": "/downloads/Example",
        "requested_actions": {"prepare_target": True, "generate_screenshots": True, "upload_screenshots": True},
        "effective_actions": {"prepare_target": True, "generate_screenshots": True, "upload_screenshots": True, "fetch_ptgen": True},
        "material_options": {"screenshot_count": 3, "image_host": "ptpimg"},
        "output_options": {"target_output_dir": "./tmp/target"},
    }
    artifacts = {
        "target_package_dir": "./tmp/target/U2-60635-to-MTEAM",
        "target_materials_ready": False,
        "target_preparation_ready": False,
        "target_materials": {
            "assets": {
                "mediainfo": {"ready": True, "path": "/tmp/materials/MI_FULL_00.txt"},
                "screenshots": {"ready": True, "count": 2, "paths": ["/tmp/materials/screen-1.png", "/tmp/materials/screen-2.png"]},
                "image_hosts": {"ready": True, "count": 2, "path": "/tmp/materials/image-host-uploads.json"},
            }
        },
    }

    commands = {command["stage"]: command for command in ptcli_cli._run_summary_resume_commands(payload, artifacts)}
    argv = commands["resume-target-package"]["argv"]
    command = commands["resume-target-package"]["command"]

    assert "--mediainfo-file /tmp/materials/MI_FULL_00.txt" in command
    assert argv.count("--screenshot-file") == 2
    assert "/tmp/materials/screen-1.png" in argv
    assert "/tmp/materials/screen-2.png" in argv
    assert "--image-host-file /tmp/materials/image-host-uploads.json" in command
    assert "--fetch-ptgen" in argv
    assert "--upload-screenshots" in argv
    assert "--image-host" in argv


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
                    "ready": True,
                    "mode": "downloaded",
                    "torrent_hash": "a" * 40,
                    "source_torrent_path": "/tmp/U2-60635.torrent",
                    "torrent_file_evidence": True,
                    "source_save_path": "/downloads",
                    "source_qbit_category": "SOURCE",
                    "source_qbit_tags": "source-tag",
                    "source_paused": True,
                    "hash_consistent": True,
                    "injected_torrent_hash": "a" * 40,
                    "qbit_closure": {"injection": {"visible_in_client": True}},
                    "injection_verified": True,
                    "source_wait_evidence": True,
                    "content_path": "/downloads/Name",
                },
                "target": {
                    "ready": True,
                    "prepared": True,
                    "uploaded": True,
                    "downloaded": True,
                    "injected": True,
                    "seeding": True,
                    "materials_ready": True,
                    "uploaded_torrent_id": "999",
                    "uploaded_torrent_hash": "b" * 40,
                    "uploaded_torrent_file_evidence": True,
                    "injected_torrent_hash": "b" * 40,
                    "qbit_closure": {"injection": {"visible_in_client": True}},
                    "injection_verified": True,
                    "uploaded_torrent_path": "/tmp/MTEAM-999.torrent",
                    "uploaded_save_path": "/downloads/Name",
                    "uploaded_qbit_category": "MTEAM",
                    "uploaded_qbit_tags": "retorrent",
                    "uploaded_paused": True,
                    "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                    "hash_consistent": True,
                    "duplicate_clean": True,
                    "rule_obligations": rule_obligations,
                    "preparation_audit": {
                        "ready": True,
                        "materials_ready": True,
                        "metadata_ready": True,
                        "assets_ready": True,
                        "description_ready": True,
                        "payload_ready": True,
                        "missing": [],
                        "description": {
                            "path": "/tmp/MTEAM-description.txt",
                            "exists": True,
                            "char_length": 1000,
                            "expected_length": 1000,
                            "has_ptgen_description": True,
                            "has_external_ids": True,
                            "has_mediainfo_or_bdinfo": True,
                            "has_screenshot_bbcode": True,
                            "bbcode_image_count": 1,
                        },
                        "payload": {
                            "torrent_file": {"path": "/tmp/mteam.torrent", "mteam_safe": True, "metadata_readable": True, "source_flag": "MTEAM"},
                            "materials_ready_required": True,
                            "payload_checks_ready": True,
                            "description_checks_ready": True,
                        },
                    },
                    "uploaded_wait_evidence": True,
                },
            },
            "summary": {"ready": True, "complete": True, "status": "complete"},
            "material_diagnostics": {
                "present": True,
                "ready_for_mteam_upload": True,
                "target_materials_ready": True,
                "target_preparation_ready": True,
                "critical_ready": True,
                "critical_missing": [],
                "critical_path": {"ready": True, "next_step": None, "missing": []},
                "image_host_urls": {"img_urls": ["https://img.example/thumb.png"]},
            },
            "target_preflight_diagnostics": {
                "present": True,
                "status": "ready",
                "ready": True,
                "target_preparation_ready": True,
                "materials_ready": True,
                "description_ready": True,
                "payload_ready": True,
                "materials_ready_required": True,
            },
            "closure_audit": {"ready": True, "missing": [], "items": [{"name": "source.ready", "ok": True}]},
            "summary_file": str(tmp_path / "summary" / "ptcli-run-summary.json"),
            "output_options": {
                "source_output_dir": "./tmp/source",
                "target_output_dir": "./tmp/target",
                "target_torrent_output_dir": "./tmp/exported",
                "uploaded_output_dir": str(tmp_path / "uploaded"),
                "summary_output_dir": str(tmp_path / "summary"),
            },
            "wait_options": {
                "source": {"timeout": 7200.0, "interval": 45.0},
                "uploaded": {"timeout": 900.0, "interval": 20.0},
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
            "--wait-timeout",
            "7200",
            "--wait-interval",
            "45",
            "--enrich-metadata",
            "--fetch-ptgen",
            "--tmdb-id",
            "999",
            "--douban-id",
            "1291546",
            "--target-torrent-file",
            str(torrent_file),
            "--mediainfo-file",
            str(tmp_path / "MEDIAINFO.txt"),
            "--generate-mediainfo",
            "--generate-screenshots",
            "--screenshot-count",
            "2",
            "--screenshot-file",
            str(tmp_path / "screen-1.png"),
            "--upload-screenshots",
            "--image-host",
            "ptpimg",
            "--image-host-file",
            str(tmp_path / "image-host.json"),
            "--uploaded-output-dir",
            str(tmp_path / "uploaded"),
            "--inject-uploaded-torrent",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--uploaded-wait-timeout",
            "900",
            "--uploaded-wait-interval",
            "20",
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
    assert payload["wait_options"] == {
        "source": {"timeout": 7200.0, "interval": 45.0},
        "uploaded": {"timeout": 900.0, "interval": 20.0},
    }
    assert payload["qbit_wait_diagnostics"] == {}
    assert payload["qbit_wait_mismatch"] is False
    assert payload["qbit_wait_mismatches"] == []
    assert payload["qbit_wait_retry_hints"] == {}
    assert payload["automation_action"] == "complete"
    assert payload["automation_reason"] == "Summary is complete and no follow-up command is required."
    assert payload["automation_exit_code"] == 0
    assert payload["should_execute_next_command"] is False
    assert payload["next_actions"] == ["Retorrent closure is complete; verify the target tracker page and qBittorrent seeding state."]
    assert payload["ready"] is True
    assert payload["closure_status"]["complete"] is True
    assert payload["closure_status"]["target"]["ready"] is True
    assert payload["closure_status"]["target"]["rule_obligations_ready"] is True
    assert payload["closure_review"]["complete"] is True
    assert payload["closure_review"]["missing"] == []
    assert payload["closure_review"]["checks"]["source.ready"] is True
    assert payload["closure_review"]["checks"]["target.uploaded_wait_evidence"] is True
    assert payload["closure_review"]["target"]["uploaded_torrent_hash"] == "b" * 40
    assert payload["closure_review"]["target"]["uploaded_torrent_file"] == "/tmp/MTEAM-999.torrent"
    assert payload["closure_review"]["source"]["torrent_file_evidence"] is True
    assert payload["closure_review"]["source"]["save_path"] == "/downloads"
    assert payload["closure_review"]["source"]["content_path"] == "/downloads/Name"
    assert payload["closure_review"]["target"]["materials_ready"] is True
    assert payload["closure_review"]["target"]["metadata_ready"] is True
    assert payload["closure_review"]["target"]["assets_ready"] is True
    assert payload["closure_review"]["target"]["description_ready"] is True
    assert payload["closure_review"]["target"]["preparation_ready"] is True
    assert payload["closure_review"]["target"]["description"]["has_ptgen_description"] is True
    assert payload["closure_review"]["target"]["description"]["has_external_ids"] is True
    assert payload["closure_review"]["target"]["description"]["has_mediainfo_or_bdinfo"] is True
    assert payload["closure_review"]["target"]["description"]["has_screenshot_bbcode"] is True
    assert payload["closure"]["source"]["complete"] is True
    assert payload["closure"]["target"]["injected"] is True
    assert payload["closure_audit"]["ready"] is True
    assert payload["closure_audit"]["missing"] == []
    assert payload["evidence"]["source"]["mode"] == "downloaded"
    assert payload["evidence"]["target"]["uploaded_torrent_hash"] == "b" * 40
    assert payload["summary"]["status"] == "complete"
    assert payload["material_diagnostics"]["ready_for_mteam_upload"] is True
    assert payload["material_diagnostics"]["critical_path"]["ready"] is True
    assert payload["material_diagnostics"]["image_host_urls"]["img_urls"] == ["https://img.example/thumb.png"]
    assert payload["target_preflight_diagnostics"]["ready"] is True
    assert payload["target_preflight_diagnostics"]["materials_ready"] is True
    assert payload["target_preflight_diagnostics"]["description_ready"] is True
    assert payload["target_preflight_diagnostics"]["payload_ready"] is True
    assert payload["completion_matrix"]["domains"]["materials"]["ready"] is True
    assert payload["completion_matrix"]["domains"]["materials"]["evidence"]["ready_for_mteam_upload"] is True
    assert payload["completion_matrix"]["domains"]["target_upload"]["ready"] is True
    assert payload["completion_matrix"]["domains"]["target_upload"]["evidence"]["ready_for_uploaded_seeding"] is True
    assert payload["completion_matrix"]["domains"]["qbit_wait"]["ready"] is True
    assert payload["completion_matrix"]["ready"] is True
    assert payload["completion_next_stages"] == []
    assert payload["readiness_summary"]["status"] == "complete"
    assert payload["readiness_summary"]["ready"] is True
    assert payload["readiness_summary"]["complete"] is True
    assert payload["readiness_summary"]["completion_ready"] is True
    assert payload["readiness_summary"]["missing_domains"] == []
    assert payload["readiness_summary"]["blockers"] == []
    assert payload["readiness_summary"]["flow_ready"] is None
    assert payload["readiness_summary"]["source_ready"] is True
    assert payload["readiness_summary"]["materials_ready"] is True
    assert payload["readiness_summary"]["rules_ready"] is True
    assert payload["readiness_summary"]["target_upload_ready"] is True
    assert payload["readiness_summary"]["qbit_wait_ready"] is True
    assert payload["readiness_summary"]["ready_for_mteam_upload"] is True
    assert payload["readiness_summary"]["material_critical_ready"] is True
    assert payload["readiness_summary"]["target_preflight_ready"] is True
    assert payload["readiness_summary"]["target_preflight_materials_ready"] is True
    assert payload["readiness_summary"]["target_preflight_description_ready"] is True
    assert payload["readiness_summary"]["target_preflight_payload_ready"] is True
    assert payload["readiness_summary"]["ready_for_uploaded_seeding"] is True
    assert payload["readiness_summary"]["qbit_wait_mismatch"] is False
    assert payload["readiness_summary"]["qbit_wait_mismatches"] == []
    assert payload["readiness_summary"]["next_stage"] is None
    assert payload["readiness_summary"]["next_command"] is None
    assert payload["readiness_summary"]["next_command_argv"] == []
    assert payload["readiness_summary"]["automation_action"] == "complete"
    assert payload["readiness_summary"]["should_execute_next_command"] is False
    assert payload["readiness_summary"]["automation_exit_code"] == 0
    assert payload["summary_file"].endswith("ptcli-run-summary.json")
    assert payload["readiness_summary"]["summary_file"] == payload["summary_file"]
    assert payload["automation_handoff"]["json"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", payload["summary_file"], "--json"]
    assert payload["automation_handoff"]["run_next_command"]["command"] == shlex.join(["python3", "ptcli.py", "summary-check", "--summary-file", payload["summary_file"], "--run-next-command"])
    assert payload["artifacts"] == {
        "source_torrent_hash": "a" * 40,
        "source_torrent_file": "/tmp/U2-60635.torrent",
        "source_torrent_file_evidence": True,
        "source_save_path": "/downloads",
        "source_qbit_category": "SOURCE",
        "source_qbit_tags": "source-tag",
        "source_paused": True,
        "source_hash_consistent": True,
        "source_injected_torrent_hash": "a" * 40,
        "source_injection_visible_in_client": True,
        "source_injection_verified": True,
        "source_wait_evidence": True,
        "uploaded_torrent_id": "999",
        "uploaded_torrent_hash": "b" * 40,
        "uploaded_torrent_file_evidence": True,
        "injected_torrent_hash": "b" * 40,
        "injection_visible_in_client": True,
        "injection_verified": True,
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
        "target_preparation_audit": {
            "ready": True,
            "materials_ready": True,
            "metadata_ready": True,
            "assets_ready": True,
            "description_ready": True,
            "payload_ready": True,
            "missing": [],
            "description": {
                "path": "/tmp/MTEAM-description.txt",
                "exists": True,
                "char_length": 1000,
                "expected_length": 1000,
                "has_ptgen_description": True,
                "has_external_ids": True,
                "has_mediainfo_or_bdinfo": True,
                "has_screenshot_bbcode": True,
                "bbcode_image_count": 1,
            },
            "payload": {
                "torrent_file": {"path": "/tmp/mteam.torrent", "mteam_safe": True, "metadata_readable": True, "source_flag": "MTEAM"},
                "materials_ready_required": True,
                "payload_checks_ready": True,
                "description_checks_ready": True,
            },
        },
        "target_preparation_ready": True,
        "target_preparation_missing": [],
        "target_preflight_gates": {
            "present": True,
            "status": "ready",
            "ready": True,
            "blockers": [],
            "target_preparation_ready": True,
            "materials_ready": True,
            "metadata_ready": True,
            "assets_ready": True,
            "description_ready": True,
            "payload_ready": True,
            "payload_checks_ready": True,
            "description_checks_ready": True,
            "materials_ready_required": True,
            "torrent_file": {"path": "/tmp/mteam.torrent", "mteam_safe": True, "metadata_readable": True, "source_flag": "MTEAM"},
        },
    }
    assert payload["resume_commands"] == [{"stage": "resume-uploaded-torrent-download", "command": "python3 ptcli.py target-upload --uploaded-torrent-id 999"}]
    assert payload["candidate_command_count"] == 1
    assert payload["runnable_command_count"] == 1
    assert payload["next_command_ready"] is False
    assert payload["next_command_placeholder"] is False
    assert payload["next_command_run_allowed"] is False
    assert payload["candidate_commands"] == [
        {
            "stage": "resume-uploaded-torrent-download",
            "command": "python3 ptcli.py target-upload --uploaded-torrent-id 999",
            "argv": ["python3", "ptcli.py", "target-upload", "--uploaded-torrent-id", "999"],
            "source": "resume_commands",
            "subcommand": "target-upload",
            "run_allowed": True,
            "run_blocker": None,
            "placeholder": False,
        }
    ]
    assert payload["first_runnable_stage"] == "resume-uploaded-torrent-download"
    assert payload["first_runnable_command"] == "python3 ptcli.py target-upload --uploaded-torrent-id 999"
    assert payload["first_runnable_command_argv"] == ["python3", "ptcli.py", "target-upload", "--uploaded-torrent-id", "999"]
    assert payload["first_runnable_command_source"] == "resume_commands"
    assert payload["first_runnable_command_subcommand"] == "target-upload"
    assert payload["rejected_command_count"] == 0
    assert payload["rejected_command_blockers"] == []
    assert payload["resume_state"]["complete"] is True
    assert payload["resume_state"]["pipeline_complete"] is True
    assert payload["resume_state"]["next_stage"] is None
    assert payload["resume_state"]["next_command"] is None
    assert payload["resume_state"]["artifacts"]["source_torrent_file"] is True
    assert payload["resume_state"]["artifacts"]["source_torrent_file_evidence"] is True
    assert payload["resume_state"]["artifacts"]["source_torrent_hash"] is True
    assert payload["resume_state"]["artifacts"]["source_save_path"] is True
    assert payload["resume_state"]["artifacts"]["source_qbit_category"] is True
    assert payload["resume_state"]["artifacts"]["source_qbit_tags"] is True
    assert payload["resume_state"]["artifacts"]["source_paused"] is True
    assert payload["resume_state"]["artifacts"]["source_hash_consistent"] is True
    assert payload["resume_state"]["artifacts"]["source_injected_torrent_hash"] is True
    assert payload["resume_state"]["artifacts"]["source_injection_visible_in_client"] is True
    assert payload["resume_state"]["artifacts"]["source_injection_verified"] is True
    assert payload["resume_state"]["artifacts"]["source_wait_evidence"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_torrent_id"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_torrent_file"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_torrent_file_evidence"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_torrent_hash"] is True
    assert payload["resume_state"]["artifacts"]["injected_torrent_hash"] is True
    assert payload["resume_state"]["artifacts"]["injection_visible_in_client"] is True
    assert payload["resume_state"]["artifacts"]["injection_verified"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_save_path"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_qbit_category"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_qbit_tags"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_paused"] is True
    assert payload["resume_state"]["artifacts"]["uploaded_wait_evidence"] is True
    assert payload["resume_state"]["artifacts"]["target_hash_consistent"] is True
    assert payload["resume_state"]["artifacts"]["target_duplicate_clean"] is True
    assert payload["resume_state"]["artifacts"]["target_rule_obligations"] is True
    assert payload["resume_state"]["artifacts"]["target_preparation_ready"] is True
    assert payload["resume_state"]["artifacts"]["target_preflight_gates_ready"] is True
    assert payload["resume_state"]["artifacts"]["target_preflight_materials_ready"] is True
    assert payload["resume_state"]["artifacts"]["target_preflight_description_ready"] is True
    assert payload["resume_state"]["artifacts"]["target_preflight_payload_ready"] is True
    assert payload["resume_state"]["artifacts"]["target_preflight_torrent_safe"] is True
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
    assert pipeline_args.uploaded_save_path == "/downloads"
    assert pipeline_args.wait_timeout == 7200.0
    assert pipeline_args.wait_interval == 45.0
    assert pipeline_args.enrich_metadata is True
    assert pipeline_args.fetch_ptgen is True
    assert pipeline_args.tmdb_id == "999"
    assert pipeline_args.douban_id == "1291546"
    assert pipeline_args.uploaded_wait_timeout == 900.0
    assert pipeline_args.uploaded_wait_interval == 20.0
    assert pipeline_args.target_torrent_file == str(torrent_file)
    assert pipeline_args.sanitize_target_torrent is True
    assert pipeline_args.mediainfo_file == str(tmp_path / "MEDIAINFO.txt")
    assert pipeline_args.generate_bdinfo is True
    assert pipeline_args.generate_mediainfo is True
    assert pipeline_args.generate_screenshots is True
    assert pipeline_args.screenshot_count == 2
    assert pipeline_args.screenshot_file == [str(tmp_path / "screen-1.png")]
    assert pipeline_args.upload_screenshots is True
    assert pipeline_args.image_host == "ptpimg"
    assert pipeline_args.image_host_file == str(tmp_path / "image-host.json")


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
    assert pipeline_args.uploaded_save_path == "/downloads/Name"
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
async def test_retorrent_execute_reuses_package_for_uploaded_torrent_file_resume(monkeypatch, tmp_path) -> None:
    package_dir = tmp_path / "target" / "U2-60635-to-MTEAM"
    uploaded_torrent = tmp_path / "uploaded" / "MTEAM-999.torrent"
    captured_args = {}

    async def fake_pipeline_payload(args):
        captured_args["args"] = args
        return {
            "ready": True,
            "closure": {"complete": True, "blockers": []},
            "artifacts": {
                "target_package_dir": str(package_dir),
                "uploaded_torrent_file": str(uploaded_torrent),
                "uploaded_torrent_file_evidence": True,
                "source_wait_evidence": True,
                "uploaded_torrent_hash": "b" * 40,
                "injected_torrent_hash": "b" * 40,
                "injection_visible_in_client": True,
                "injection_verified": True,
                "uploaded_wait_evidence": True,
            },
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
            "--package-dir",
            str(package_dir),
            "--uploaded-torrent-file",
            str(uploaded_torrent),
            "--uploaded-save-path",
            "/downloads/Name",
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    pipeline_args = captured_args["args"]
    assert payload["status"] == "complete"
    assert pipeline_args.package_dir == str(package_dir)
    assert pipeline_args.prepare_target is False
    assert pipeline_args.check_dupes is False
    assert pipeline_args.target_execute is False
    assert pipeline_args.export_target_torrent is False
    assert pipeline_args.sanitize_target_torrent is False
    assert pipeline_args.download_source is False
    assert pipeline_args.inject_source is False
    assert pipeline_args.wait_complete is False
    assert pipeline_args.uploaded_torrent_file == str(uploaded_torrent)
    assert pipeline_args.inject_uploaded_torrent is True
    assert pipeline_args.wait_uploaded_complete is True
    assert pipeline_args.uploaded_save_path == "/downloads/Name"
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
    assert captured_args["args"].uploaded_save_path == "/downloads/Name"
    assert captured_args["args"].uploaded_output_dir == "./tmp/uploaded"
    assert captured_args["args"].enrich_metadata is True
    assert captured_args["args"].fetch_ptgen is True
    assert captured_args["args"].generate_bdinfo is True
    assert captured_args["args"].generate_mediainfo is True
    assert captured_args["args"].generate_screenshots is True
    assert captured_args["args"].upload_screenshots is True
    assert payload["output_options"]["uploaded_output_dir"] == "./tmp/uploaded"


@pytest.mark.asyncio
async def test_retorrent_execute_can_disable_default_material_chain(monkeypatch) -> None:
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
            "--no-enrich-metadata",
            "--no-fetch-ptgen",
            "--no-generate-bdinfo",
            "--no-generate-mediainfo",
            "--no-generate-screenshots",
            "--no-upload-screenshots",
            "--json",
        ]
    )

    payload = await ptcli_cli.retorrent_payload(args)

    assert payload["ready"] is True
    assert captured_args["args"].enrich_metadata is False
    assert captured_args["args"].fetch_ptgen is False
    assert captured_args["args"].generate_bdinfo is False
    assert captured_args["args"].generate_mediainfo is False
    assert captured_args["args"].generate_screenshots is False
    assert captured_args["args"].upload_screenshots is False


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
            "resume_commands": [
                {
                    "stage": "resume-uploaded-torrent",
                    "command": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent",
                    "argv": ["python3", "ptcli.py", "target-upload", "--uploaded-torrent-file", "/tmp/MTEAM-999.torrent"],
                }
            ],
            "resume_state": {
                "complete": False,
                "resume_available": True,
                "next_stage": "resume-uploaded-torrent",
                "next_command": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent",
                "next_command_argv": ["python3", "ptcli.py", "target-upload", "--uploaded-torrent-file", "/tmp/MTEAM-999.torrent"],
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
    assert payload["resume_state"]["next_command_argv"] == ["python3", "ptcli.py", "target-upload", "--uploaded-torrent-file", "/tmp/MTEAM-999.torrent"]
    assert payload["next_stage"] == "resume-uploaded-torrent"
    assert payload["next_command"] == "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"
    assert payload["next_command_argv"] == ["python3", "ptcli.py", "target-upload", "--uploaded-torrent-file", "/tmp/MTEAM-999.torrent"]
    assert payload["resume_state"]["blockers"] == ["target.injected", "pipeline did not report ready."]
    assert payload["completion_matrix"]["ready"] is False
    assert "target_upload" in payload["completion_matrix"]["missing_domains"]
    assert payload["completion_matrix"]["domains"]["target_upload"]["ready"] is False
    assert "resume-uploaded-torrent" in payload["completion_next_stages"]
    assert "resume-uploaded-torrent-download" in payload["completion_next_stages"]
    assert payload["candidate_command_count"] == 1
    assert payload["runnable_command_count"] == 1
    assert payload["next_command_ready"] is True
    assert payload["next_command_placeholder"] is False
    assert payload["next_command_run_allowed"] is True
    assert payload["automation_action"] == "run_next_command"
    assert payload["automation_reason"] == "Next generated ptcli command is ready to run for stage resume-uploaded-torrent."
    assert payload["automation_exit_code"] == 1
    assert payload["should_execute_next_command"] is True
    assert payload["readiness_summary"]["status"] == "blocked"
    assert payload["readiness_summary"]["ready"] is False
    assert payload["readiness_summary"]["complete"] is False
    assert payload["readiness_summary"]["completion_ready"] is False
    assert "target_upload" in payload["readiness_summary"]["missing_domains"]
    assert payload["readiness_summary"]["blockers"] == ["target.injected", "pipeline did not report ready."]
    assert payload["readiness_summary"]["target_upload_ready"] is False
    assert payload["readiness_summary"]["qbit_wait_ready"] is True
    assert payload["readiness_summary"]["qbit_wait_mismatch"] is False
    assert payload["readiness_summary"]["next_stage"] == "resume-uploaded-torrent"
    assert payload["readiness_summary"]["next_command"] == "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"
    assert payload["readiness_summary"]["next_command_argv"] == ["python3", "ptcli.py", "target-upload", "--uploaded-torrent-file", "/tmp/MTEAM-999.torrent"]
    assert payload["readiness_summary"]["automation_action"] == "run_next_command"
    assert payload["readiness_summary"]["should_execute_next_command"] is True
    assert payload["readiness_summary"]["automation_exit_code"] == 1


def test_target_upload_automation_requires_complete_audit_artifacts() -> None:
    fields = ptcli_cli._target_upload_automation_fields(
        {"ready": True, "blockers": []},
        {
            "next_stage": None,
            "next_command": None,
            "artifacts": {
                "uploaded_torrent_hash": True,
                "injected_torrent_hash": False,
                "injection_visible_in_client": False,
                "injection_verified": False,
                "target_hash_consistent": True,
                "target_duplicate_clean": True,
                "target_rule_obligations": True,
                "target_preparation_ready": True,
                "uploaded_wait_evidence": False,
            },
        },
        {
            "next_command_placeholder": False,
            "next_command_ready": False,
            "next_command_run_allowed": False,
        },
    )

    assert fields["automation_action"] == "resolve_blockers"
    assert fields["automation_exit_code"] == 1
    assert fields["should_execute_next_command"] is False
    assert fields["automation_reason"] == "Resolve blockers before automation can continue: missing audit artifact: injected_torrent_hash"


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


def test_retorrent_resume_state_infers_pipeline_complete_from_closure() -> None:
    resume_state = ptcli_cli._retorrent_execute_resume_state(
        {"closure": {"complete": True, "blockers": []}},
        {},
        [],
        [],
    )

    assert resume_state["complete"] is True
    assert resume_state["pipeline_complete"] is True


def test_retorrent_execute_blockers_require_uploaded_wait_evidence() -> None:
    pipeline_result = {"status": "ok", "ready": True, "summary": {"blockers": []}}
    closure = {"complete": True, "blockers": []}

    blockers = ptcli_cli._retorrent_execute_blockers(
        pipeline_result,
        closure,
        True,
        {
            "source_wait_evidence": True,
            "uploaded_wait_evidence": False,
            "uploaded_torrent_file_evidence": True,
            "uploaded_torrent_hash": "b" * 40,
            "injected_torrent_hash": "b" * 40,
            "injection_visible_in_client": True,
            "injection_verified": True,
        },
    )

    assert blockers == ["target.uploaded_wait_evidence"]


def test_retorrent_execute_blockers_require_source_wait_evidence() -> None:
    pipeline_result = {"status": "ok", "ready": True, "summary": {"blockers": []}}
    closure = {"complete": True, "blockers": []}

    blockers = ptcli_cli._retorrent_execute_blockers(
        pipeline_result,
        closure,
        True,
        {"uploaded_wait_evidence": True, "uploaded_torrent_file_evidence": True, "uploaded_torrent_hash": "b" * 40, "injected_torrent_hash": "b" * 40, "injection_visible_in_client": True, "injection_verified": True},
    )

    assert blockers == ["source.wait_evidence"]


def test_retorrent_execute_blockers_require_uploaded_injection_artifacts() -> None:
    pipeline_result = {"status": "ok", "ready": True, "summary": {"blockers": []}}
    closure = {"complete": True, "blockers": []}

    blockers = ptcli_cli._retorrent_execute_blockers(
        pipeline_result,
        closure,
        True,
        {"source_wait_evidence": True, "uploaded_wait_evidence": True, "uploaded_torrent_file_evidence": True},
    )

    assert blockers == ["target.uploaded_torrent_hash", "target.injected_torrent_hash", "target.injection_visible_in_client", "target.injection_verified"]


def test_retorrent_execute_blockers_require_closure_audit_ready() -> None:
    pipeline_result = {
        "status": "ok",
        "ready": True,
        "summary": {"blockers": []},
        "closure_audit": {"ready": False, "missing": ["target.uploaded_wait_evidence"]},
    }
    closure = {"complete": True, "blockers": []}

    blockers = ptcli_cli._retorrent_execute_blockers(
        pipeline_result,
        closure,
        True,
        {
            "source_wait_evidence": True,
            "uploaded_wait_evidence": True,
            "uploaded_torrent_file_evidence": True,
            "uploaded_torrent_hash": "b" * 40,
            "injected_torrent_hash": "b" * 40,
            "injection_visible_in_client": True,
            "injection_verified": True,
        },
    )

    assert blockers == ["target.uploaded_wait_evidence"]


def test_retorrent_next_actions_explain_target_preparation_ready() -> None:
    actions = ptcli_cli._retorrent_execute_next_actions({}, ["target_preparation_ready"])

    assert actions == [
        "Regenerate the MTEAM target package after completing IMDb/TMDb/Douban metadata, PTGen/Douban description, MediaInfo/BDInfo, screenshot, and image-host materials."
    ]


def test_retorrent_next_actions_expand_missing_target_materials() -> None:
    actions = ptcli_cli._retorrent_execute_next_actions(
        {
            "artifacts": {
                "target_preparation_missing": [
                    "metadata.tmdb",
                    "metadata.ptgen_description",
                    "assets.mediainfo_or_bdinfo",
                    "assets.screenshots",
                    "assets.image_host_uploads",
                    "description.content",
                ]
            }
        },
        ["target.preparation_ready"],
    )

    assert actions[0] == "Regenerate the MTEAM target package after completing IMDb/TMDb/Douban metadata, PTGen/Douban description, MediaInfo/BDInfo, screenshot, and image-host materials."
    assert any("IMDb/TMDb/Douban metadata" in action for action in actions)
    assert any("PTGen/Douban description" in action for action in actions)
    assert any("MediaInfo/BDInfo" in action for action in actions)
    assert any("screenshot" in action for action in actions)
    assert any("image host" in action for action in actions)
    assert any("Regenerate the MTEAM description" in action for action in actions)


def test_retorrent_next_actions_expand_closure_review_materials_ready() -> None:
    actions = ptcli_cli._retorrent_execute_next_actions(
        {
            "closure_review": {
                "target": {
                    "preparation_missing": ["description.external_ids", "description.screenshot_bbcode"],
                    "description": {"missing": ["description.mediainfo_or_bdinfo"]},
                }
            }
        },
        ["target.materials_ready"],
    )

    assert any("IMDb/TMDb/Douban metadata" in action for action in actions)
    assert any("screenshot" in action for action in actions)
    assert any("MediaInfo/BDInfo" in action for action in actions)


def test_target_preparation_missing_next_actions_explain_specific_external_ids() -> None:
    actions = ptcli_cli._target_preparation_missing_next_actions(
        ["description.external_ids.tmdb", "description.external_ids.douban", "metadata.imdb"]
    )

    assert any("--tmdb-id" in action for action in actions)
    assert any("--douban-id/--douban-url" in action for action in actions)
    assert any("--imdb-id" in action for action in actions)


def test_target_preparation_missing_next_actions_include_ptgen_for_generic_external_ids() -> None:
    actions = ptcli_cli._target_preparation_missing_next_actions(["description.external_ids"])

    assert actions == [
        "Fetch or supply IMDb/TMDb/Douban metadata with --enrich-metadata, --fetch-ptgen, --metadata-file, --imdb-id, --tmdb-id, or --douban-id, then rerun resume-target-package."
    ]


def test_target_preparation_recovery_hints_explain_specific_external_ids() -> None:
    hints = ptcli_cli._target_preparation_recovery_hints(
        ["description.external_ids.tmdb", "description.external_ids.douban", "metadata.imdb"]
    )
    by_key = {hint["key"]: hint for hint in hints}

    assert by_key["metadata.tmdb_id"]["command_flags"] == ["--enrich-metadata"]
    assert by_key["metadata.tmdb_id"]["existing_file_options"] == ["--metadata-file", "--tmdb-id"]
    assert by_key["metadata.douban"]["command_flags"] == ["--enrich-metadata", "--fetch-ptgen"]
    assert by_key["metadata.douban"]["existing_file_options"] == ["--metadata-file", "--douban-id", "--douban-url"]
    assert by_key["metadata.imdb_id"]["command_flags"] == ["--enrich-metadata"]
    assert by_key["metadata.imdb_id"]["existing_file_options"] == ["--metadata-file", "--imdb-id"]


def test_target_preparation_recovery_hints_include_ptgen_for_generic_external_ids() -> None:
    hints = ptcli_cli._target_preparation_recovery_hints(["description.external_ids"])

    assert hints[0]["key"] == "metadata.external_ids"
    assert hints[0]["command_flags"] == ["--enrich-metadata", "--fetch-ptgen"]
    assert hints[0]["existing_file_options"] == ["--metadata-file", "--imdb-id", "--tmdb-id", "--douban-id", "--douban-url"]


def test_target_package_material_auto_flags_include_specific_external_ids() -> None:
    flags = ptcli_cli._target_package_material_auto_flags(
        {"target_preparation_missing": ["description.external_ids.tmdb", "description.external_ids.douban"]}
    )

    assert "--enrich-metadata" in flags
    assert "--fetch-ptgen" in flags


def test_target_package_material_auto_flags_include_ptgen_for_generic_external_ids() -> None:
    flags = ptcli_cli._target_package_material_auto_flags({"target_preparation_missing": ["description.external_ids"]})

    assert "--enrich-metadata" in flags
    assert "--fetch-ptgen" in flags


def test_target_package_material_auto_flags_include_screenshot_coverage() -> None:
    flags = ptcli_cli._target_package_material_auto_flags({"target_preparation_missing": ["description.screenshot_coverage"]})

    assert "--upload-screenshots" in flags


def test_target_package_material_auto_flags_include_payload_review_completeness() -> None:
    flags = ptcli_cli._target_package_material_auto_flags(
        {
            "target_payload_review": {
                "description": {
                    "completeness": {
                        "recovery_missing": [
                            "description.ptgen_description",
                            "description.mediainfo_or_bdinfo",
                            "description.screenshot_coverage",
                        ]
                    }
                }
            }
        }
    )

    assert "--enrich-metadata" in flags
    assert "--fetch-ptgen" in flags
    assert "--generate-mediainfo" in flags
    assert "--upload-screenshots" in flags


def test_target_package_material_auto_flags_include_preparation_payload_review_completeness() -> None:
    flags = ptcli_cli._target_package_material_auto_flags(
        {
            "target_preparation_audit": {
                "payload_review": {
                    "description": {
                        "completeness": {
                            "recovery_missing": [
                                "description.external_ids",
                                "description.screenshot_bbcode",
                            ]
                        }
                    }
                }
            }
        }
    )

    assert "--enrich-metadata" in flags
    assert "--fetch-ptgen" in flags
    assert "--prepare-target" in flags


def test_material_recovery_resume_command_covers_screenshot_coverage() -> None:
    hints = ptcli_cli._target_preparation_recovery_hints(["description.screenshot_coverage"])
    commands = [
        {
            "stage": "resume-target-package",
            "command": "python3 ptcli.py pipeline --prepare-target --upload-screenshots --image-host ptpimg",
            "argv": ["python3", "ptcli.py", "pipeline", "--prepare-target", "--upload-screenshots", "--image-host", "ptpimg"],
        }
    ]

    enriched = ptcli_cli._attach_material_recovery_resume_commands(hints, commands)

    assert enriched[0]["key"] == "assets.image_host_uploads"
    assert enriched[0]["resume_command_available"] is True
    assert enriched[0]["resume_command_stage"] == "resume-target-package"
    assert enriched[0]["required_command_flags"] == ["--upload-screenshots"]
    assert enriched[0]["missing_command_flags"] == []
    assert "--upload-screenshots" in enriched[0]["resume_command_argv"]


def test_material_recovery_resume_command_reports_missing_flags() -> None:
    hints = ptcli_cli._target_preparation_recovery_hints(["metadata.ptgen_description"])
    commands = [
        {
            "stage": "resume-target-package",
            "command": "python3 ptcli.py pipeline --prepare-target --enrich-metadata",
            "argv": ["python3", "ptcli.py", "pipeline", "--prepare-target", "--enrich-metadata"],
        }
    ]

    enriched = ptcli_cli._attach_material_recovery_resume_commands(hints, commands)

    assert enriched[0]["key"] == "metadata.ptgen_description"
    assert enriched[0]["required_command_flags"] == ["--enrich-metadata", "--fetch-ptgen"]
    assert enriched[0]["missing_command_flags"] == ["--fetch-ptgen"]
    assert enriched[0]["resume_command_available"] is False
    assert enriched[0]["resume_command"] is None
    assert enriched[0]["resume_command_argv"] == []


def test_run_summary_resume_state_uses_payload_review_description_completeness() -> None:
    resume_commands = [
        {
            "stage": "resume-target-package",
            "command": "python3 ptcli.py pipeline --prepare-target --enrich-metadata --fetch-ptgen --upload-screenshots",
            "argv": ["python3", "ptcli.py", "pipeline", "--prepare-target", "--enrich-metadata", "--fetch-ptgen", "--upload-screenshots"],
        }
    ]
    resume_state = ptcli_cli._run_summary_resume_state(
        {"closure": {"complete": False, "blockers": ["target.materials_ready"]}},
        {
            "target_payload_review": {
                "description": {
                    "completeness": {
                        "recovery_missing": [
                            "description.ptgen_description",
                            "description.screenshot_coverage",
                        ]
                    }
                }
            }
        },
        resume_commands,
    )

    recovery_by_key = {hint["key"]: hint for hint in resume_state["materials"]["recovery_hints"]}
    assert recovery_by_key["metadata.ptgen_description"]["resume_command_available"] is True
    assert recovery_by_key["assets.image_host_uploads"]["resume_command_available"] is True
    assert resume_state["materials"]["next_actions"] == [
        "Fetch PTGen/Douban description with --fetch-ptgen or supply metadata containing ptgen_description, then rerun resume-target-package.",
        "Upload screenshots to an image host with --upload-screenshots/--image-host or provide --image-host-file, then rerun resume-target-package.",
    ]


def test_readiness_material_recovery_summary_exposes_actionable_commands() -> None:
    resume_state = {
        "materials": {
            "target_materials_missing": ["assets.image_host_uploads"],
            "target_preparation_missing": ["description.screenshot_coverage"],
            "next_actions": ["Upload screenshots to an image host with --upload-screenshots/--image-host or provide --image-host-file, then rerun resume-target-package."],
            "recovery_hints": [
                {
                    "key": "assets.image_host_uploads",
                    "resume_stage": "resume-target-package",
                    "reason": "Upload screenshots to an image host or provide existing image-host upload evidence before regenerating the MTEAM package.",
                    "command_flags": ["--upload-screenshots", "--image-host"],
                    "existing_file_options": ["--image-host-file"],
                    "required_command_flags": ["--upload-screenshots"],
                    "missing_command_flags": [],
                    "resume_command_available": True,
                    "resume_command_stage": "resume-target-package",
                    "resume_command": "python3 ptcli.py pipeline --prepare-target --upload-screenshots --image-host ptpimg",
                    "resume_command_argv": ["python3", "ptcli.py", "pipeline", "--prepare-target", "--upload-screenshots", "--image-host", "ptpimg"],
                }
            ],
        }
    }

    recovery = ptcli_cli._readiness_material_recovery_summary(resume_state)

    assert recovery["present"] is True
    assert recovery["target_materials_missing"] == ["assets.image_host_uploads"]
    assert recovery["target_preparation_missing"] == ["description.screenshot_coverage"]
    assert recovery["next_actions"] == ["Upload screenshots to an image host with --upload-screenshots/--image-host or provide --image-host-file, then rerun resume-target-package."]
    assert recovery["hint_count"] == 1
    assert recovery["keys"] == ["assets.image_host_uploads"]
    assert recovery["required_flags"] == ["--upload-screenshots"]
    assert recovery["missing_flags"] == []
    assert recovery["existing_file_options"] == ["--image-host-file"]
    assert recovery["first_command"] == "python3 ptcli.py pipeline --prepare-target --upload-screenshots --image-host ptpimg"
    assert recovery["first_command_argv"] == ["python3", "ptcli.py", "pipeline", "--prepare-target", "--upload-screenshots", "--image-host", "ptpimg"]
    assert recovery["command_coverage"] == {
        "ready": True,
        "hint_count": 1,
        "available_count": 1,
        "missing_count": 0,
        "first_uncovered_key": None,
        "first_uncovered_missing_flags": [],
        "uncovered_keys": [],
    }
    assert recovery["completion_command"] is None
    assert recovery["completion_command_argv"] == []


def test_summary_candidate_commands_include_material_recovery_completion() -> None:
    payload = {
        "resume_commands": [
            {
                "stage": "resume-target-package",
                "command": "python3 ptcli.py pipeline --prepare-target",
                "argv": ["python3", "ptcli.py", "pipeline", "--prepare-target"],
            }
        ],
        "resume_state": {
            "next_stage": "resume-target-package",
            "next_command": "python3 ptcli.py pipeline --prepare-target",
            "next_command_argv": ["python3", "ptcli.py", "pipeline", "--prepare-target"],
            "materials": {
                "recovery_hints": [
                    {
                        "key": "metadata.ptgen_description",
                        "required_command_flags": ["--enrich-metadata", "--fetch-ptgen"],
                        "missing_command_flags": ["--enrich-metadata", "--fetch-ptgen"],
                        "resume_command_available": False,
                    }
                ]
            },
        },
    }

    candidates = ptcli_cli._summary_candidate_commands(payload)

    assert candidates[0]["source"] == "resume_commands"
    assert candidates[0]["run_allowed"] is False
    assert candidates[0]["run_blocker"] == "material recovery command does not cover metadata.ptgen_description; missing flags: --enrich-metadata,--fetch-ptgen"
    assert candidates[1]["source"] == "material_recovery_completion"
    assert candidates[1]["run_allowed"] is True
    assert candidates[1]["argv"] == ["python3", "ptcli.py", "pipeline", "--prepare-target", "--enrich-metadata", "--fetch-ptgen"]


def test_readiness_uploaded_followup_summary_exposes_target_seeding_state() -> None:
    uploaded_hash = "a" * 40
    retry_hash = "b" * 40
    resume_state = {
        "uploaded_followup": {
            "ready": False,
            "ready_for_uploaded_seeding": False,
            "missing": ["injected_torrent_hash", "uploaded_wait_evidence"],
            "blockers": ["uploaded MTEAM torrent injection is not verified in qBittorrent", "qBittorrent has not reported the uploaded MTEAM torrent as complete"],
            "next_actions": ["Inject the uploaded MTEAM torrent into qBittorrent.", "Wait for qBittorrent to report the uploaded MTEAM torrent as complete."],
            "uploaded_torrent_id": "999",
            "uploaded_torrent_hash": uploaded_hash,
            "injected_torrent_hash": None,
            "uploaded_torrent_file": "/tmp/MTEAM-999.torrent",
            "uploaded_torrent_file_evidence": {
                "path": "/tmp/MTEAM-999.torrent",
                "exists": True,
                "is_file": True,
                "size_bytes": 1234,
                "sha1": "c" * 40,
                "torrent_hash": uploaded_hash,
                "metadata_readable": True,
            },
            "uploaded_save_path": "/downloads/Example",
            "uploaded_wait_query": {"torrent_hash": uploaded_hash, "content_path": "/downloads/Example", "timeout": 42.0, "interval": 3.0},
            "qbit_wait_mismatch": True,
            "qbit_wait_mismatches": ["uploaded.requested_hash"],
            "wait_retry": {
                "retry_recommended": True,
                "suggested_torrent_hash": retry_hash,
                "suggested_content_path": "/downloads/Other",
                "suggested_save_path": "/downloads",
            },
            "gates": {"downloaded": True, "injection_verified": False, "uploaded_wait_evidence": False},
        }
    }

    followup = ptcli_cli._readiness_uploaded_followup_summary(resume_state)

    assert followup["present"] is True
    assert followup["ready"] is False
    assert followup["ready_for_uploaded_seeding"] is False
    assert followup["missing"] == ["injected_torrent_hash", "uploaded_wait_evidence"]
    assert followup["blockers"] == ["uploaded MTEAM torrent injection is not verified in qBittorrent", "qBittorrent has not reported the uploaded MTEAM torrent as complete"]
    assert followup["next_actions"] == ["Inject the uploaded MTEAM torrent into qBittorrent.", "Wait for qBittorrent to report the uploaded MTEAM torrent as complete."]
    assert followup["uploaded_torrent_id"] == "999"
    assert followup["uploaded_torrent_hash"] == uploaded_hash
    assert followup["injected_torrent_hash"] is None
    assert followup["uploaded_torrent_file"] == "/tmp/MTEAM-999.torrent"
    assert followup["uploaded_torrent_file_evidence"]["torrent_hash"] == uploaded_hash
    assert followup["uploaded_save_path"] == "/downloads/Example"
    assert followup["uploaded_wait_query"] == {"torrent_hash": uploaded_hash, "content_path": "/downloads/Example", "timeout": 42.0, "interval": 3.0}
    assert followup["qbit_wait_mismatch"] is True
    assert followup["qbit_wait_mismatches"] == ["uploaded.requested_hash"]
    assert followup["wait_retry"]["suggested_torrent_hash"] == retry_hash
    assert followup["gates"] == {"downloaded": True, "injection_verified": False, "uploaded_wait_evidence": False}


def test_readiness_shell_fields_export_uploaded_followup_state() -> None:
    uploaded_hash = "a" * 40
    retry_hash = "b" * 40
    fields = ptcli_cli._summary_check_readiness_shell_fields(
        {
            "uploaded_followup": {
                "present": True,
                "ready": False,
                "ready_for_uploaded_seeding": False,
                "missing": ["injected_torrent_hash", "uploaded_wait_evidence"],
                "blockers": ["uploaded MTEAM torrent injection is not verified in qBittorrent", "qBittorrent has not reported the uploaded MTEAM torrent as complete"],
                "next_actions": ["Inject the uploaded MTEAM torrent into qBittorrent.", "Wait for qBittorrent to report the uploaded MTEAM torrent as complete."],
                "uploaded_torrent_id": "999",
                "uploaded_torrent_hash": uploaded_hash,
                "injected_torrent_hash": None,
                "uploaded_torrent_file": "/tmp/MTEAM-999.torrent",
                "uploaded_save_path": "/downloads/Example",
                "uploaded_wait_query": {"torrent_hash": uploaded_hash, "content_path": "/downloads/Example", "timeout": 42.0, "interval": 3.0},
                "qbit_wait_mismatch": True,
                "qbit_wait_mismatches": ["uploaded.requested_hash"],
                "wait_retry": {
                    "retry_recommended": True,
                    "suggested_torrent_hash": retry_hash,
                    "suggested_content_path": "/downloads/Other",
                    "suggested_save_path": "/downloads",
                },
            }
        }
    )

    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_PRESENT"] == "1"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_READY"] == "0"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_READY_FOR_SEEDING"] == "0"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_MISSING"] == "injected_torrent_hash,uploaded_wait_evidence"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_BLOCKERS"] == "uploaded MTEAM torrent injection is not verified in qBittorrent|qBittorrent has not reported the uploaded MTEAM torrent as complete"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_NEXT_ACTIONS"] == "Inject the uploaded MTEAM torrent into qBittorrent. | Wait for qBittorrent to report the uploaded MTEAM torrent as complete."
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_TORRENT_ID"] == "999"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_TORRENT_HASH"] == uploaded_hash
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_INJECTED_HASH"] is None
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_TORRENT_FILE"] == "/tmp/MTEAM-999.torrent"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_SAVE_PATH"] == "/downloads/Example"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_QUERY_HASH"] == uploaded_hash
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_QUERY_CONTENT_PATH"] == "/downloads/Example"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_QUERY_TIMEOUT"] == 42.0
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_QUERY_INTERVAL"] == 3.0
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_MISMATCH"] == "1"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_MISMATCHES"] == "uploaded.requested_hash"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_RETRY_RECOMMENDED"] == "1"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_HASH"] == retry_hash
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_CONTENT_PATH"] == "/downloads/Other"
    assert fields["PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_SAVE_PATH"] == "/downloads"


def test_retorrent_execute_blockers_require_qbit_wait_match() -> None:
    pipeline_result = {
        "status": "ok",
        "ready": True,
        "summary": {"blockers": []},
        "evidence": {
            "target": {
                "qbit_closure": {
                    "wait": {
                        "complete": True,
                        "completion_verification": {
                            "matched_count": 1,
                            "complete_count": 1,
                            "any_complete": True,
                            "requested_hash_matched": True,
                            "requested_content_path_matched": False,
                        },
                    }
                }
            }
        },
    }
    closure = {"complete": True, "blockers": []}

    blockers = ptcli_cli._retorrent_execute_blockers(
        pipeline_result,
        closure,
        True,
        {
            "source_wait_evidence": True,
            "uploaded_wait_evidence": True,
            "uploaded_torrent_file_evidence": True,
            "uploaded_torrent_hash": "b" * 40,
            "injected_torrent_hash": "b" * 40,
            "injection_visible_in_client": True,
            "injection_verified": True,
        },
    )

    assert blockers == ["qBittorrent wait mismatch: uploaded.requested_content_path"]


def test_retorrent_execute_artifacts_preserve_target_payload_review() -> None:
    payload_review = {
        "present": True,
        "description": {
            "has_ptgen_description": True,
            "external_id_readiness": {"imdb": True, "tmdb": True, "douban": True},
            "screenshot_coverage": {
                "ready": True,
                "expected_urls": ["https://img.example/thumb.png"],
                "description_urls": ["https://img.example/thumb.png"],
                "missing_urls": [],
            },
        },
        "materials": {"image_host_urls": ["https://img.example/thumb.png"]},
    }

    artifacts = ptcli_cli._retorrent_execute_artifacts({"artifacts": {}}, {"target": {"payload_review": payload_review}}, {"target": {}})

    assert artifacts["target_payload_review"] == payload_review


def test_retorrent_execute_artifacts_derive_target_preflight_gates() -> None:
    preparation_audit = {
        "ready": True,
        "materials_ready": True,
        "metadata_ready": True,
        "assets_ready": True,
        "description_ready": True,
        "payload_ready": True,
        "missing": [],
        "payload": {
            "materials_ready_required": True,
            "payload_checks_ready": True,
            "description_checks_ready": True,
            "torrent_file": {"path": "/tmp/mteam.torrent", "mteam_safe": True, "metadata_readable": True, "source_flag": "MTEAM"},
        },
    }

    artifacts = ptcli_cli._retorrent_execute_artifacts({"artifacts": {"target_preparation_audit": preparation_audit}}, {}, {})
    diagnostics = ptcli_cli._summary_target_preflight_diagnostics({"artifacts": artifacts})

    assert artifacts["target_preflight_gates"]["ready"] is True
    assert diagnostics["ready"] is True
    assert diagnostics["target_preparation_ready"] is True
    assert diagnostics["materials_ready"] is True
    assert diagnostics["description_ready"] is True
    assert diagnostics["payload_ready"] is True
    assert diagnostics["materials_ready_required"] is True
    assert diagnostics["torrent_file"]["mteam_safe"] is True


@pytest.mark.asyncio
async def test_retorrent_execute_blocks_when_pipeline_closure_audit_is_incomplete(monkeypatch, tmp_path) -> None:
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")

    async def fake_pipeline_payload(_args):
        return {
            "status": "ok",
            "ready": True,
            "closure": {"complete": True, "blockers": []},
            "closure_audit": {"ready": False, "missing": ["target.uploaded_wait_evidence"]},
            "evidence": {
                "source": {"source_wait_evidence": True},
                "target": {
                    "uploaded_torrent_hash": "b" * 40,
                    "uploaded_torrent_file_evidence": True,
                    "injected_torrent_hash": "b" * 40,
                    "qbit_closure": {"injection": {"visible_in_client": True}},
                    "injection_verified": True,
                    "uploaded_wait_evidence": True,
                },
            },
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

    assert payload["status"] == "blocked"
    assert payload["complete"] is False
    assert payload["blockers"] == ["target.uploaded_wait_evidence"]


@pytest.mark.asyncio
async def test_retorrent_execute_blocks_when_pipeline_qbit_wait_mismatches(monkeypatch, tmp_path) -> None:
    torrent_file = tmp_path / "target.torrent"
    torrent_file.write_bytes(b"d4:infod")

    async def fake_pipeline_payload(_args):
        return {
            "status": "ok",
            "ready": True,
            "closure": {"complete": True, "blockers": []},
            "evidence": {
                "source": {"source_wait_evidence": True},
                "target": {
                    "uploaded_torrent_hash": "b" * 40,
                    "uploaded_torrent_file_evidence": True,
                    "injected_torrent_hash": "b" * 40,
                    "injection_verified": True,
                    "uploaded_wait_evidence": True,
                    "qbit_closure": {
                        "injection": {"visible_in_client": True},
                        "wait": {
                            "complete": True,
                            "completion_verification": {
                                "matched_count": 1,
                                "complete_count": 1,
                                "any_complete": True,
                                "requested_hash_matched": False,
                                "requested_content_path_matched": True,
                                "observed_hashes": ["f" * 40],
                            },
                        }
                    },
                },
            },
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

    assert payload["status"] == "blocked"
    assert payload["complete"] is False
    assert payload["qbit_wait_mismatch"] is True
    assert payload["blockers"] == ["qBittorrent wait mismatch: uploaded.requested_hash"]
    assert payload["next_actions"][0].startswith("Resolve the uploaded qBittorrent wait mismatch")
    assert payload["qbit_wait_retry_hints"]["uploaded"]["retry_recommended"] is True
    assert payload["automation_action"] == "resolve_qbit_wait_mismatch"
    assert payload["automation_reason"].startswith("qBittorrent wait evidence mismatched the requested torrent/content: uploaded.requested_hash.")
    assert payload["automation_exit_code"] == 1
    assert payload["should_execute_next_command"] is False


def test_retorrent_execute_blockers_require_source_injection_artifacts_for_downloaded_mode() -> None:
    pipeline_result = {"status": "ok", "ready": True, "summary": {"blockers": []}, "evidence": {"source": {"mode": "downloaded"}}}
    closure = {"complete": True, "blockers": []}

    blockers = ptcli_cli._retorrent_execute_blockers(
        pipeline_result,
        closure,
        True,
        {
            "source_wait_evidence": True,
            "source_torrent_file_evidence": True,
            "uploaded_wait_evidence": True,
            "uploaded_torrent_file_evidence": True,
            "uploaded_torrent_hash": "b" * 40,
            "injected_torrent_hash": "b" * 40,
            "injection_visible_in_client": True,
            "injection_verified": True,
        },
    )

    assert blockers == ["source.torrent_hash", "source.injected_torrent_hash", "source.injection_visible_in_client", "source.injection_verified"]


def test_retorrent_execute_blockers_require_torrent_file_evidence() -> None:
    pipeline_result = {"status": "ok", "ready": True, "summary": {"blockers": []}, "evidence": {"source": {"mode": "downloaded"}}}
    closure = {"complete": True, "blockers": []}

    blockers = ptcli_cli._retorrent_execute_blockers(
        pipeline_result,
        closure,
        True,
        {
            "source_wait_evidence": True,
            "source_torrent_hash": "a" * 40,
            "source_injected_torrent_hash": "a" * 40,
            "source_injection_visible_in_client": True,
            "source_injection_verified": True,
            "uploaded_wait_evidence": True,
            "uploaded_torrent_hash": "b" * 40,
            "injected_torrent_hash": "b" * 40,
            "injection_visible_in_client": True,
            "injection_verified": True,
        },
    )

    assert blockers == ["source.torrent_file_evidence", "target.uploaded_torrent_file_evidence"]


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
    assert f"hash={'f' * 40}" in actions[0]
    assert "path=/downloads/Other" in actions[0]
    assert f"hash={'a' * 40}" in actions[1]
    assert "path=/downloads/Wrong" in actions[1]
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
    assert set(ptcli_source.COOKIE_DOWNLOAD_URLS) == {"HDS"}
    assert ptcli_source.source_download_adapter("U2") == "nexusphp_passkey"
    assert ptcli_source.source_download_adapter("HDS") == "cookie_download"
    assert ptcli_source.source_download_adapter("TTG") == "ttg_passkey"
    assert ptcli_source.source_download_adapter("MTEAM") == "mteam_api"
    assert ptcli_source.source_download_adapter("unsupported") is None
    assert ptcli_source.source_credential_requirements("HDS") == ["data/cookies/HDS.txt"]


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
async def test_reference_source_info_parses_plain_external_ids(monkeypatch, tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "CHD.txt").write_text("uid=1;", encoding="utf-8")
    html = """
    <html>
      <head><title>CHD Reference</title></head>
      <body>
        <h1>CHD.Reference.2024.1080p.BluRay-GROUP</h1>
        <table>
          <tr><td>IMDb</td><td>tt7654321</td></tr>
          <tr><td>TMDb ID</td><td>98765</td></tr>
          <tr><td>豆瓣</td><td>3541415</td></tr>
        </table>
        <div class="torrent-description">Info Hash: 1234567890ABCDEF1234567890ABCDEF12345678</div>
      </body>
    </html>
    """

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
            assert url == "https://ptchdbits.co/details.php?id=2468"
            return FakeResponse()

    monkeypatch.setattr(ptcli_source.httpx, "AsyncClient", FakeClient)

    info = await ptcli_source.fetch_source_info({}, "CHD", "2468", base_dir=str(tmp_path))

    assert info.tracker == "CHD"
    assert info.torrent_id == "2468"
    assert info.imdb_id == 7654321
    assert info.tmdb_id == 98765
    assert info.douban_id == "3541415"
    assert info.douban_url == "https://movie.douban.com/subject/3541415/"
    assert info.torrenthash == "1234567890abcdef1234567890abcdef12345678"


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


@pytest.mark.asyncio
async def test_hds_source_download_uses_cookie_download(monkeypatch, tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "HDS.txt").write_text("uid=1; pass=secret;", encoding="utf-8")

    class FakeResponse:
        content = b"d4:infod"

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
            assert url == "https://hd-space.org/download.php?id=456"
            return FakeResponse()

    monkeypatch.setattr(ptcli_source.httpx, "AsyncClient", FakeClient)

    path = await ptcli_source.download_source_torrent({}, "HDS", "456", str(tmp_path / "out"), base_dir=str(tmp_path))

    assert path == tmp_path / "out" / "HDS-456.torrent"
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
    expected_torrent_path = tmp_path / "source-out" / "U2-60635.torrent"
    source_hash = write_valid_torrent(expected_torrent_path, tmp_path / "source-content" / "Name.mkv")

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (_config, base_dir)
        return source_info_from_tuple(tracker, extract_torrent_id(source_id), (1, 2, "Name", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, base_dir)
        assert source_id == source_url
        assert output_dir == "source-out"
        return expected_torrent_path

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
    assert payload["source_torrent"]["metadata_readable"] is True
    assert payload["source_torrent"]["torrent_hash"] == source_hash
    assert len(payload["source_torrent"]["sha1"]) == 40
    assert payload["source_torrent_verification"]["expected_hash"] == source_hash
    assert payload["source_torrent_verification"]["actual_hash"] == source_hash
    assert payload["source_torrent_verification"]["verified"] is True
    assert payload["qbit_handoff"]["ready"] is False
    assert payload["qbit_handoff"]["blockers"] == ["--save-path is required before the generated qBittorrent source injection handoff can run."]
    assert payload["next_stage"] == "resume-source-torrent"
    assert payload["next_command_ready"] is False
    assert payload["next_command_placeholder"] is True
    assert payload["should_execute_next_command"] is False
    assert payload["next_command_run_allowed"] is False
    assert payload["next_command_run_blocker"] == "next command contains placeholders"
    assert payload["automation_action"] == "fill_command_placeholders"
    assert payload["automation_reason"] == "Next command contains placeholders and requires manual values before execution."
    assert payload["automation_exit_code"] == 1
    assert payload["candidate_command_count"] == 1
    assert payload["runnable_command_count"] == 0
    assert payload["recommended_commands"][0]["stage"] == "resume-source-torrent"
    assert payload["recommended_commands"][0]["argv"][:3] == ["python3", "ptcli.py", "pipeline"]
    assert "--source-torrent-file" in payload["next_command_argv"]
    assert payload["source_torrent"]["path"] in payload["next_command_argv"]
    assert "--save-path" in payload["next_command_argv"]
    assert "<save-path>" in payload["next_command_argv"]
    assert "--write-summary" in payload["next_command_argv"]


def test_source_download_generates_ready_qbit_handoff(monkeypatch, capsys, tmp_path) -> None:
    source_url = "https://u2.dmhy.org/details.php?id=60635&hit=1"
    expected_torrent_path = tmp_path / "source-out" / "U2-60635.torrent"
    source_hash = write_valid_torrent(expected_torrent_path, tmp_path / "source-content" / "Name.mkv")

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (_config, base_dir)
        return source_info_from_tuple(tracker, extract_torrent_id(source_id), (1, 2, "Name", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, base_dir)
        assert source_id == source_url
        assert output_dir == "source-out"
        return expected_torrent_path

    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {})
    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)

    code = main(
        [
            "source-download",
            "--config",
            "/etc/ua/config.py",
            "--tracker",
            "U2",
            "--source-id",
            source_url,
            "--to",
            "MTEAM",
            "--output-dir",
            "source-out",
            "--client",
            "box",
            "--base-dir",
            "/srv/Upload-Assistant",
            "--save-path",
            "/downloads",
            "--qbit-category",
            "SOURCE",
            "--qbit-tags",
            "retorrent",
            "--paused",
            "--wait-timeout",
            "900",
            "--wait-interval",
            "10",
            "--accept-rules",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["qbit_handoff"]["ready"] is True
    assert payload["qbit_handoff"]["blockers"] == []
    assert payload["next_command_ready"] is True
    assert payload["next_command_placeholder"] is False
    assert payload["should_execute_next_command"] is True
    assert payload["next_command_run_allowed"] is True
    assert payload["next_command_run_blocker"] is None
    assert payload["automation_action"] == "run_next_command"
    assert payload["automation_reason"] == "Next generated ptcli command is ready to run for stage resume-source-torrent."
    assert payload["automation_exit_code"] == 1
    assert payload["candidate_command_count"] == 1
    assert payload["runnable_command_count"] == 1
    assert payload["next_command_argv"] == [
        "python3",
        "ptcli.py",
        "pipeline",
        "--from",
        "U2",
        "--source-id",
        "60635",
        "--to",
        "MTEAM",
        "--source-torrent-file",
        payload["source_torrent"]["path"],
        "--inject-source",
        "--save-path",
        "/downloads",
        "--wait-complete",
        "--accept-rules",
        "--write-summary",
        "--json",
        "--config",
        "/etc/ua/config.py",
        "--client",
        "box",
        "--base-dir",
        "/srv/Upload-Assistant",
        "--qbit-category",
        "SOURCE",
        "--qbit-tags",
        "retorrent",
        "--paused",
        "--wait-timeout",
        "900",
        "--wait-interval",
        "10",
    ]


def test_source_download_blocks_unreadable_torrent_metadata(monkeypatch, capsys, tmp_path) -> None:
    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (_config, base_dir)
        return source_info_from_tuple(tracker, extract_torrent_id(source_id), (1, 2, "Name", "a" * 40, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (_config, tracker, source_id, output_dir, base_dir)
        torrent_path = tmp_path / "source-out" / "U2-60635.torrent"
        torrent_path.parent.mkdir(parents=True, exist_ok=True)
        torrent_path.write_bytes(b"d4:infod")
        return torrent_path

    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {})
    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)

    code = main(["source-download", "--tracker", "U2", "--source-id", "60635", "--to", "MTEAM", "--output-dir", "source-out", "--accept-rules", "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["source_torrent"]["metadata_readable"] is False
    assert payload["source_torrent_verification"]["blockers"] == ["source torrent metadata is not readable"]
    assert payload["blockers"] == ["source-torrent-verify: source torrent metadata is not readable"]
    assert payload["automation_action"] == "resolve_blockers"
    assert payload["automation_reason"] == "Resolve blockers before automation can continue: source-torrent-verify: source torrent metadata is not readable"
    assert payload["automation_exit_code"] == 1
    assert payload["should_execute_next_command"] is False
    assert payload["candidate_command_count"] == 1


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


def test_source_torrent_verify_blocks_missing_downloaded_infohash() -> None:
    stage = ptcli_cli._source_torrent_verify_stage({"result": {"path": "/tmp/U2-60635.torrent", "metadata_readable": True}}, "a" * 40)

    assert stage["ok"] is False
    assert stage["message"] == "Downloaded source torrent infohash is unavailable for source tracker metadata verification."
    assert stage["result"] == {
        "verified": False,
        "expected_hash": "a" * 40,
        "actual_hash": None,
        "blockers": [f"source torrent infohash unavailable: expected {'a' * 40}"],
    }


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


def test_pipeline_exit_code_requires_complete_live_closure() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--upload-target", "--target-execute", "--json"])

    assert ptcli_cli._pipeline_exit_code(args, {"status": "ok", "ready": True, "complete": False}) == 1
    assert ptcli_cli._pipeline_exit_code(args, {"status": "ok", "ready": True, "complete": True, "closure_audit": {"ready": True, "missing": []}}) == 0


def test_pipeline_exit_code_requires_live_closure_audit_ready() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--from", "U2", "--source-id", "60635", "--to", "MTEAM", "--upload-target", "--target-execute", "--json"])

    assert ptcli_cli._pipeline_exit_code(args, {"status": "ok", "ready": True, "complete": True, "closure_audit": {"ready": False, "missing": ["source.wait_evidence"]}}) == 1


def test_pipeline_run_summary_blocks_live_closure_audit_missing() -> None:
    blockers: list[str] = []
    closure_audit = {"ready": False, "missing": ["source.wait_evidence", "target.uploaded_wait_evidence"]}

    ptcli_cli._extend_unique_string(blockers, ptcli_cli._closure_audit_blockers(closure_audit))

    assert blockers == ["closure audit missing: source.wait_evidence", "closure audit missing: target.uploaded_wait_evidence"]


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
        "resume-source-download": "python3 ptcli.py pipeline --download-source --inject-source --save-path /downloads",
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
    source_download = ptcli_cli._resume_next_command(["source-download: temporary tracker error"], {"resume-source-download": commands["resume-source-download"]})

    assert source_hash["stage"] == "resume-source-torrent"
    assert source_wait["stage"] == "resume-source-torrent"
    assert source_download["stage"] == "resume-source-download"
    assert target_hash["stage"] == "resume-uploaded-torrent"
    assert target_wait["stage"] == "resume-uploaded-torrent"
    assert uploaded_wait["stage"] == "resume-uploaded-torrent"
    assert downloaded_missing["stage"] == "resume-uploaded-torrent-download"
    assert generic_target["stage"] == "resume-target-upload"


def test_completion_matrix_preferred_stages_map_domains_to_resume_commands() -> None:
    matrix = {"missing_domains": ["materials", "target_upload", "qbit_wait", "rules", "source"]}

    pipeline_stages = ptcli_cli._completion_matrix_preferred_stages(matrix, kind="ptcli.pipeline.run_summary")
    target_upload_stages = ptcli_cli._completion_matrix_preferred_stages(matrix, kind="ptcli.target_upload.summary")
    readiness_stages = ptcli_cli._readiness_summary_preferred_stages({"missing_domains": matrix["missing_domains"]}, kind="ptcli.pipeline.run_summary")

    assert pipeline_stages[:4] == ("resume-target-package", "resume-uploaded-torrent", "resume-uploaded-torrent-download", "resume-target-upload")
    assert "resume-source-torrent" in pipeline_stages
    assert target_upload_stages[:4] == ("resume-target-package", "resume-uploaded-torrent", "resume-uploaded-torrent-download", "target-upload-retry")
    assert "resume-source-download" in target_upload_stages
    assert readiness_stages == pipeline_stages


def test_target_package_resume_args_reuse_generated_material_artifacts() -> None:
    args = ptcli_cli._target_package_material_resume_args(
        {"generate_mediainfo": True, "generate_screenshots": True, "upload_screenshots": True},
        {"generate_bdinfo": True},
        {"image_host": "ptpimg", "screenshot_count": 3},
        {
            "material_generation": {
                "bdinfo": {"bdinfo_file": "/tmp/materials/BD_FULL_00.txt"},
                "mediainfo": {"mediainfo_file": "/tmp/materials/MI_FULL_00.txt"},
                "screenshots": {"screenshot_files": ["/tmp/materials/screenshot-01.png", "/tmp/materials/screenshot-02.png"]},
                "image_host": {"image_host_file": "/tmp/materials/image-host-uploads.json"},
            }
        },
    )

    assert "--bdinfo-file" in args
    assert "/tmp/materials/BD_FULL_00.txt" in args
    assert "--mediainfo-file" in args
    assert "/tmp/materials/MI_FULL_00.txt" in args
    assert args.count("--screenshot-file") == 2
    assert "/tmp/materials/screenshot-01.png" in args
    assert "/tmp/materials/screenshot-02.png" in args
    assert "--image-host-file" in args
    assert "/tmp/materials/image-host-uploads.json" in args
    assert "--generate-bdinfo" in args
    assert "--generate-mediainfo" in args
    assert "--generate-screenshots" in args
    assert "--upload-screenshots" in args
    assert "--image-host" in args
    assert "ptpimg" in args


def test_target_package_resume_args_reuse_generated_metadata_ids() -> None:
    args = ptcli_cli._target_package_material_resume_args(
        {"enrich_metadata": True},
        {},
        {},
        {
            "material_generation": {
                "metadata": {
                    "imdb_id": 1234567,
                    "tmdb_id": 999,
                    "douban_id": "1291546",
                    "douban_url": "https://movie.douban.com/subject/1291546/",
                }
            }
        },
    )

    assert "--enrich-metadata" in args
    assert "--imdb-id" in args
    assert "1234567" in args
    assert "--tmdb-id" in args
    assert "999" in args
    assert "--douban-id" in args
    assert "1291546" in args
    assert "--douban-url" in args
    assert "https://movie.douban.com/subject/1291546/" in args


def test_target_package_resume_args_reuse_package_metadata_ids() -> None:
    args = ptcli_cli._target_package_material_resume_args(
        {},
        {"fetch_ptgen": True},
        {"tmdb_id": "111"},
        {
            "target_materials": {
                "metadata": {
                    "imdb_id": 7654321,
                    "tmdb_id": 999,
                    "douban_id": "3541415",
                    "douban_url": "https://movie.douban.com/subject/3541415/",
                }
            }
        },
    )

    assert "--fetch-ptgen" in args
    assert args[args.index("--imdb-id") + 1] == "7654321"
    assert args[args.index("--tmdb-id") + 1] == "111"
    assert args[args.index("--douban-id") + 1] == "3541415"
    assert args[args.index("--douban-url") + 1] == "https://movie.douban.com/subject/3541415/"


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


def test_run_summary_resume_commands_use_source_qbit_wait_retry_hint() -> None:
    payload = {
        "source_tracker": "U2",
        "source_torrent_id": "60635",
        "target_trackers": ["MTEAM"],
        "client": "default",
    }
    artifacts = {
        "source_torrent_file": "/tmp/U2-60635.torrent",
        "source_save_path": "/verified/source",
        "qbit_wait_retry_hints": {
            "source": {
                "retry_recommended": True,
                "suggested_torrent_hash": "f" * 40,
                "suggested_content_path": "/downloads/ObservedSource",
                "suggested_save_path": "/downloads",
            }
        },
    }

    commands = {command["stage"]: command for command in ptcli_cli._run_summary_resume_commands(payload, artifacts)}

    assert "--save-path /downloads/ObservedSource" in commands["resume-source-torrent"]["command"]
    assert "/downloads/ObservedSource" in commands["resume-source-torrent"]["argv"]
    assert "/verified/source" not in commands["resume-source-torrent"]["argv"]


def test_run_summary_resume_commands_include_source_download_retry_without_torrent_file() -> None:
    payload = {
        "source_tracker": "U2",
        "source_torrent_id": "60635",
        "target_trackers": ["MTEAM"],
        "config": "/tmp/config.py",
        "base_dir": "/tmp/base",
        "client": "seedbox",
        "source_save_path": "/downloads/source",
        "qbit_options": {"source": {"category": "SOURCE", "tags": "source-tag", "paused": True}},
        "output_options": {"source_output_dir": "/tmp/source", "summary_output_dir": "/tmp/summary"},
        "wait_options": {"source": {"timeout": 7200.0, "interval": 45.0}},
        "requested_actions": {"download_source": False, "inject_source": False, "wait_complete": False},
        "effective_actions": {"live_target_upload": True, "download_source": True, "inject_source": True, "wait_complete": True},
    }

    commands = {command["stage"]: command for command in ptcli_cli._run_summary_resume_commands(payload, {})}

    assert "resume-source-download" in commands
    command = commands["resume-source-download"]["command"]
    argv = commands["resume-source-download"]["argv"]
    assert argv[:3] == ["python3", "ptcli.py", "pipeline"]
    assert "--download-source" in argv
    assert "--output-dir /tmp/source" in command
    assert "--inject-source" in argv
    assert "--save-path /downloads/source" in command
    assert "--qbit-category SOURCE" in command
    assert "--qbit-tags source-tag" in command
    assert "--paused" in argv
    assert "--wait-complete" in argv
    assert "--wait-timeout 7200" in command
    assert "--wait-interval 45" in command
    assert "--config /tmp/config.py" in command
    assert "--base-dir /tmp/base" in command
    assert "--client seedbox" in command
    assert "--summary-output-dir /tmp/summary" in command


def test_run_summary_resume_commands_include_target_torrent_export_retry() -> None:
    payload = {
        "source_tracker": "U2",
        "source_torrent_id": "60635",
        "target_trackers": ["MTEAM"],
        "path": "/downloads/Name",
        "client": "seedbox",
        "output_options": {"target_torrent_output_dir": "/tmp/exported", "uploaded_output_dir": "/tmp/uploaded", "summary_output_dir": "/tmp/summary"},
        "wait_options": {"uploaded": {"timeout": 900.0, "interval": 20.0}},
        "qbit_options": {"uploaded": {"category": "MTEAM", "tags": "retorrent", "paused": True}},
        "requested_actions": {"prepare_target": True, "upload_target": True, "target_execute": True},
        "effective_actions": {"live_target_upload": True, "prepare_target": True, "upload_target": True, "target_execute": True, "target_torrent_export": True},
        "closure": {"complete": False, "blockers": ["target.uploaded"]},
        "blockers": ["target.uploaded"],
    }
    artifacts = {
        "target_package_dir": "/tmp/package",
        "target_materials_ready": True,
        "target_preparation_ready": True,
        "uploaded_save_path": "/downloads/Name",
    }

    commands = {command["stage"]: command for command in ptcli_cli._run_summary_resume_commands(payload, artifacts)}
    resume_state = ptcli_cli._run_summary_resume_state(payload, artifacts, list(commands.values()))

    assert "resume-target-torrent" in commands
    command = commands["resume-target-torrent"]["command"]
    argv = commands["resume-target-torrent"]["argv"]
    assert resume_state["next_stage"] == "resume-target-torrent"
    assert resume_state["next_command"] == command
    assert argv[:3] == ["python3", "ptcli.py", "pipeline"]
    assert "--package-dir /tmp/package" in command
    assert "--path /downloads/Name" in command
    assert "--check-dupes" in argv
    assert "--upload-target" in argv
    assert "--export-target-torrent" in argv
    assert "--target-torrent-output-dir /tmp/exported" in command
    assert "--target-execute" in argv
    assert "--confirm-upload" in argv
    assert "--download-uploaded-torrent" in argv
    assert "--uploaded-output-dir /tmp/uploaded" in command
    assert "--inject-uploaded-torrent" in argv
    assert "--uploaded-save-path /downloads/Name" in command
    assert "--uploaded-qbit-category MTEAM" in command
    assert "--uploaded-qbit-tags retorrent" in command
    assert "--uploaded-paused" in argv
    assert "--wait-uploaded-complete" in argv
    assert "--uploaded-wait-timeout 900" in command
    assert "--uploaded-wait-interval 20" in command
    assert "--summary-output-dir /tmp/summary" in command


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
                "evidence": {"source": {"mode": "matched"}, "target": {"mode": "live_upload"}},
                "closure_audit": {"ready": True, "missing": [], "items": [{"name": "target.uploaded_wait_evidence", "scope": "target", "ok": True}]},
                "resume_state": {
                    "next_stage": None,
                    "next_command": None,
                    "available_stages": [],
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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
    assert payload["automation_reason"] == "Summary is complete and no follow-up command is required."
    assert payload["automation_handoff"]["json"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_file), "--json"]
    assert payload["automation_handoff"]["print_next_argv"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_file), "--print-next-argv"]
    assert payload["automation_handoff"]["print_first_runnable_command"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_file), "--print-first-runnable-command"]
    assert payload["automation_handoff"]["print_first_runnable_argv"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_file), "--print-first-runnable-argv"]
    assert payload["automation_handoff"]["run_next_command"]["command"] == shlex.join(["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_file), "--run-next-command"])
    assert payload["automation_handoff"]["run_first_runnable_command"]["command"] == shlex.join(["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_file), "--run-first-runnable-command"])
    assert payload["next_command_ready"] is False
    assert payload["next_command_run_allowed"] is False
    assert payload["next_command_subcommand"] is None
    assert payload["next_command_run_blocker"] == "next command is missing or unparsable"
    assert payload["should_execute_next_command"] is False
    assert payload["automation_exit_code"] == 0
    assert payload["complete"] is True
    assert payload["live_safe_to_attempt"] is True
    assert payload["missing_artifacts"] == []
    assert payload["missing_closure_audit"] == []
    assert payload["closure_modes"] == {"source": "matched", "target": "live_upload"}
    assert payload["source_mode"] == "matched"
    assert payload["target_mode"] == "live_upload"


def test_summary_check_exposes_material_diagnostics(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["material-prerequisite-check: Material prerequisites have blockers."],
                "artifacts": {
                    "material_generation": {
                        "prerequisites": {
                            "ok": False,
                            "skipped": False,
                            "message": "Material prerequisites have blockers.",
                            "checks": [{"name": "assets.image_host", "ok": False, "message": "--upload-screenshots requires --image-host."}],
                            "blockers": ["--upload-screenshots requires --image-host."],
                        },
                        "metadata": {
                            "ok": True,
                            "ready": True,
                            "missing": [],
                            "imdb_id": 1234567,
                            "tmdb_id": 999,
                            "douban_id": "1291546",
                            "ptgen_description_length": 42,
                        },
                        "image_host": {
                            "ok": False,
                            "skipped": True,
                            "message": "Skipped because material-prerequisite-check did not pass.",
                            "blockers": [],
                        },
                    },
                    "target_materials": {
                        "ready": False,
                        "assets": {
                            "disc_structure": {"ready": True, "path": "/downloads/Disc/BDMV", "bdmv": True, "type": "BDMV"},
                            "image_hosts": {
                                "ready": True,
                                "count": 1,
                                "items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}],
                            },
                        },
                        "missing": ["assets.bdinfo_for_disc", "assets.image_host_uploads"],
                    },
                    "target_materials_ready": False,
                    "target_materials_missing": ["assets.bdinfo_for_disc", "assets.image_host_uploads"],
                    "target_materials_warnings": ["BDMV disc content requires --bdinfo-file.", "Image-host uploads are missing."],
                    "target_preparation_missing": ["assets.bdinfo_for_disc", "assets.image_host_uploads"],
                },
                "resume_state": {
                    "next_stage": "resume-target-package",
                    "next_command": "python3 ptcli.py pipeline --prepare-target",
                    "next_command_argv": ["python3", "ptcli.py", "pipeline", "--prepare-target"],
                    "available_stages": ["resume-target-package"],
                    "artifacts": {"target_materials_ready": False},
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    diagnostics = payload["material_diagnostics"]
    assert diagnostics["present"] is True
    assert diagnostics["generation_ready"] is False
    assert diagnostics["target_materials_ready"] is False
    assert diagnostics["target_materials_missing"] == ["assets.bdinfo_for_disc", "assets.image_host_uploads"]
    assert diagnostics["target_preparation_missing"] == ["assets.bdinfo_for_disc", "assets.image_host_uploads"]
    assert diagnostics["ready_for_mteam_upload"] is False
    assert diagnostics["upload_material_gates"] == {"critical_ready": False, "target_materials_ready": False, "target_preparation_ready": False}
    assert "critical material missing: assets.bdinfo_for_disc" in diagnostics["upload_material_blockers"]
    assert "target materials are not ready" in diagnostics["upload_material_blockers"]
    assert "target preparation is not ready" in diagnostics["upload_material_blockers"]
    assert diagnostics["critical_ready"] is False
    assert diagnostics["critical_missing"] == ["assets.bdinfo_for_disc", "assets.image_host_uploads"]
    assert diagnostics["critical_domains"]["media_info"] == {"ready": False, "missing": ["assets.bdinfo_for_disc"]}
    assert diagnostics["critical_domains"]["image_host"] == {"ready": False, "missing": ["assets.image_host_uploads"]}
    assert diagnostics["critical_domains"]["metadata"]["ready"] is True
    assert diagnostics["critical_domains"]["screenshots"]["ready"] is True
    assert diagnostics["critical_domains"]["description"]["ready"] is True
    assert diagnostics["critical_path"]["ready"] is False
    assert diagnostics["critical_path"]["next_step"] == "media_info"
    assert diagnostics["critical_path"]["missing"] == ["assets.bdinfo_for_disc", "assets.image_host_uploads"]
    assert [step["name"] for step in diagnostics["critical_path"]["steps"]] == [
        "metadata",
        "media_info",
        "screenshots",
        "image_host",
        "description",
        "target_materials",
        "target_preparation",
    ]
    assert diagnostics["critical_path"]["steps"][1]["label"] == "MediaInfo/BDInfo"
    assert diagnostics["critical_path"]["steps"][1]["ready"] is False
    assert diagnostics["critical_path"]["steps"][3]["ready"] is False
    assert diagnostics["disc_structure"]["type"] == "BDMV"
    assert diagnostics["disc_structure"]["bdmv"] is True
    assert diagnostics["bdinfo_required"] is True
    assert diagnostics["media_info_requirement"] == "bdinfo"
    assert diagnostics["sections"]["prerequisites"]["ok"] is False
    assert diagnostics["sections"]["prerequisites"]["checks"][0]["name"] == "assets.image_host"
    assert diagnostics["sections"]["metadata"]["ptgen_description_length"] == 42
    assert diagnostics["image_host_urls"]["raw_urls"] == ["https://img.example/raw.png"]
    assert diagnostics["image_host_urls"]["img_urls"] == ["https://img.example/thumb.png"]
    assert diagnostics["image_host_urls"]["web_urls"] == ["https://img.example/page"]
    assert diagnostics["image_host_urls"]["item_count"] == 1
    assert diagnostics["image_host_urls"]["valid_count"] == 1
    assert diagnostics["image_host_urls"]["invalid_count"] == 0
    assert diagnostics["blockers"] == ["prerequisites: --upload-screenshots requires --image-host.", "BDMV disc content requires --bdinfo-file.", "Image-host uploads are missing."]
    code = main(["summary-check", "--summary-file", str(summary_file), "--print-shell"])
    assert code == 0
    out = capsys.readouterr().out
    assert "export PTCLI_READY_FOR_MTEAM_UPLOAD=0\n" in out
    assert "export PTCLI_MATERIAL_UPLOAD_BLOCKERS=" in out
    assert "target materials are not ready" in out
    assert "target preparation is not ready" in out
    assert "export PTCLI_MATERIAL_CRITICAL_READY=0\n" in out
    assert "export PTCLI_MATERIAL_CRITICAL_MISSING=assets.bdinfo_for_disc,assets.image_host_uploads\n" in out
    assert "export PTCLI_MATERIAL_CRITICAL_MEDIA_INFO_READY=0\n" in out
    assert "export PTCLI_MATERIAL_CRITICAL_MEDIA_INFO_MISSING=assets.bdinfo_for_disc\n" in out
    assert "export PTCLI_MATERIAL_CRITICAL_IMAGE_HOST_READY=0\n" in out
    assert "export PTCLI_MATERIAL_CRITICAL_IMAGE_HOST_MISSING=assets.image_host_uploads\n" in out
    assert "export PTCLI_MATERIAL_CRITICAL_PATH_READY=0\n" in out
    assert "export PTCLI_MATERIAL_CRITICAL_PATH_NEXT_STEP=media_info\n" in out
    assert "export PTCLI_MATERIAL_CRITICAL_PATH_MISSING=assets.bdinfo_for_disc,assets.image_host_uploads\n" in out
    assert "export PTCLI_MATERIAL_BDINFO_REQUIRED=1\n" in out
    assert "export PTCLI_MATERIAL_MEDIA_INFO_REQUIREMENT=bdinfo\n" in out
    assert "export PTCLI_MATERIAL_IMAGE_HOST_ITEM_COUNT=1\n" in out
    assert "export PTCLI_MATERIAL_IMAGE_HOST_VALID_COUNT=1\n" in out
    assert "export PTCLI_MATERIAL_IMAGE_HOST_INVALID_COUNT=0\n" in out


def test_summary_material_diagnostics_recovers_metadata_readiness_from_target_package() -> None:
    readiness = {
        "imdb_id": {"ready": True, "required": True, "source": "source"},
        "tmdb_id": {"ready": False, "required": True, "source": None},
        "douban_id": {"ready": True, "required": True, "source": "ptgen"},
        "douban_url": {"ready": True, "required": True, "source": "ptgen"},
        "ptgen_description": {"ready": True, "required": True, "source": "ptgen"},
    }
    diagnostics = ptcli_cli._summary_material_diagnostics(
        {
            "artifacts": {
                "target_materials": {
                    "ready": False,
                    "metadata_ready": False,
                    "metadata": {
                        "imdb_id": 1234567,
                        "tmdb_id": None,
                        "douban_id": "1291546",
                        "douban_url": "https://movie.douban.com/subject/1291546/",
                        "ptgen_description_length": 42,
                        "enrichment_status": "enriched",
                        "enrichment_ready": False,
                        "sources": ["source", "ptgen"],
                        "applied": {"douban_url": "https://movie.douban.com/subject/1291546/"},
                        "readiness": readiness,
                        "missing": ["tmdb_id"],
                        "blockers": ["TMDb enrichment returned no TMDb id."],
                        "readiness_blockers": ["Missing metadata after enrichment: tmdb_id"],
                    },
                    "missing": ["metadata.tmdb"],
                },
                "target_materials_ready": False,
            }
        }
    )

    metadata = diagnostics["sections"]["metadata"]
    assert metadata["ok"] is False
    assert metadata["status"] == "enriched"
    assert metadata["sources"] == ["source", "ptgen"]
    assert metadata["applied"] == {"douban_url": "https://movie.douban.com/subject/1291546/"}
    assert metadata["readiness"] == readiness
    assert metadata["missing"] == ["tmdb_id"]
    assert metadata["readiness_blockers"] == ["Missing metadata after enrichment: tmdb_id"]
    assert diagnostics["metadata_fields"] == {
        "imdb_id": {"ready": True, "required": True, "source": "source", "value": 1234567},
        "tmdb_id": {"ready": False, "required": True, "source": None, "value": None},
        "douban_id": {"ready": True, "required": True, "source": "ptgen", "value": "1291546"},
        "douban_url": {"ready": True, "required": True, "source": "ptgen", "value": "https://movie.douban.com/subject/1291546/"},
        "ptgen_description": {"ready": True, "required": True, "source": "ptgen", "length": 42},
    }
    assert diagnostics["blockers"] == ["metadata: TMDb enrichment returned no TMDb id.", "metadata: Missing metadata after enrichment: tmdb_id"]

    shell_fields = ptcli_cli._summary_check_material_shell_fields(diagnostics)
    assert json.loads(shell_fields["PTCLI_MATERIAL_METADATA_READINESS"]) == readiness
    assert shell_fields["PTCLI_MATERIAL_METADATA_SOURCES"] == "source,ptgen"
    assert shell_fields["PTCLI_MATERIAL_METADATA_APPLIED_KEYS"] == "douban_url"
    assert shell_fields["PTCLI_MATERIAL_METADATA_READINESS_BLOCKERS"] == "Missing metadata after enrichment: tmdb_id"
    assert shell_fields["PTCLI_MATERIAL_METADATA_IMDB_READY"] == "1"
    assert shell_fields["PTCLI_MATERIAL_METADATA_IMDB_SOURCE"] == "source"
    assert shell_fields["PTCLI_MATERIAL_METADATA_TMDB_READY"] == "0"
    assert shell_fields["PTCLI_MATERIAL_METADATA_TMDB_SOURCE"] is None
    assert shell_fields["PTCLI_MATERIAL_METADATA_DOUBAN_ID_READY"] == "1"
    assert shell_fields["PTCLI_MATERIAL_METADATA_DOUBAN_ID_SOURCE"] == "ptgen"
    assert shell_fields["PTCLI_MATERIAL_METADATA_DOUBAN_URL_READY"] == "1"
    assert shell_fields["PTCLI_MATERIAL_METADATA_DOUBAN_URL_SOURCE"] == "ptgen"
    assert shell_fields["PTCLI_MATERIAL_METADATA_PTGEN_READY"] == "1"
    assert shell_fields["PTCLI_MATERIAL_METADATA_PTGEN_REQUIRED"] == "1"
    assert shell_fields["PTCLI_MATERIAL_METADATA_PTGEN_SOURCE"] == "ptgen"


def test_summary_material_diagnostics_exposes_invalid_image_host_url_counts() -> None:
    diagnostics = ptcli_cli._summary_material_diagnostics(
        {
            "artifacts": {
                "target_materials": {
                    "ready": False,
                    "assets": {
                        "image_hosts": {
                            "ready": False,
                            "count": 1,
                            "valid_count": 0,
                            "invalid_count": 1,
                            "items": [{"local_file": "/tmp/screen-1.png"}],
                        }
                    },
                    "missing": ["assets.image_host_uploads"],
                },
                "target_materials_ready": False,
                "target_materials_missing": ["assets.image_host_uploads"],
            }
        }
    )

    assert diagnostics["image_host_urls"]["raw_urls"] == []
    assert diagnostics["image_host_urls"]["img_urls"] == []
    assert diagnostics["image_host_urls"]["web_urls"] == []
    assert diagnostics["image_host_urls"]["item_count"] == 1
    assert diagnostics["image_host_urls"]["valid_count"] == 0
    assert diagnostics["image_host_urls"]["invalid_count"] == 1
    shell_fields = ptcli_cli._summary_check_material_shell_fields(diagnostics)
    assert shell_fields["PTCLI_MATERIAL_IMAGE_HOST_ITEM_COUNT"] == 1
    assert shell_fields["PTCLI_MATERIAL_IMAGE_HOST_VALID_COUNT"] == 0
    assert shell_fields["PTCLI_MATERIAL_IMAGE_HOST_INVALID_COUNT"] == 1


def test_summary_material_diagnostics_exposes_material_file_evidence(tmp_path) -> None:
    mediainfo = tmp_path / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_host = tmp_path / "image-host-uploads.json"
    image_host.write_text(json.dumps({"items": [{"img_url": "https://img.example/1.png"}]}), encoding="utf-8")

    diagnostics = ptcli_cli._summary_material_diagnostics(
        {
            "artifacts": {
                "material_generation": {
                    "mediainfo": {
                        "ok": True,
                        "status": "generated",
                        "mediainfo_file": str(mediainfo),
                        "mediainfo_file_evidence": ptcli_cli._material_file_evidence(str(mediainfo)),
                    },
                    "screenshots": {
                        "ok": True,
                        "status": "generated",
                        "screenshot_files": [str(screenshot)],
                        "screenshot_files_evidence": ptcli_cli._material_file_evidence([str(screenshot)]),
                        "count": 1,
                    },
                    "image_host": {
                        "ok": True,
                        "status": "uploaded",
                        "image_host_file": str(image_host),
                        "image_host_file_evidence": ptcli_cli._material_file_evidence(str(image_host)),
                    },
                }
            }
        }
    )

    mediainfo_evidence = diagnostics["sections"]["mediainfo"]["mediainfo_file_evidence"]
    assert mediainfo_evidence["exists"] is True
    assert mediainfo_evidence["is_file"] is True
    assert mediainfo_evidence["size_bytes"] == len(mediainfo.read_bytes())
    assert len(mediainfo_evidence["sha1"]) == 40
    screenshot_evidence = diagnostics["sections"]["screenshots"]["screenshot_files_evidence"]
    assert screenshot_evidence[0]["exists"] is True
    assert screenshot_evidence[0]["is_file"] is True
    assert len(screenshot_evidence[0]["sha1"]) == 40
    image_host_evidence = diagnostics["sections"]["image_host"]["image_host_file_evidence"]
    assert image_host_evidence["exists"] is True
    assert len(image_host_evidence["sha1"]) == 40

    shell_fields = ptcli_cli._summary_check_material_shell_fields(diagnostics)
    assert shell_fields["PTCLI_MATERIAL_MEDIAINFO_FILE"] == str(mediainfo)
    assert shell_fields["PTCLI_MATERIAL_MEDIAINFO_FILE_EXISTS"] == "1"
    assert shell_fields["PTCLI_MATERIAL_MEDIAINFO_FILE_SHA1"] == mediainfo_evidence["sha1"]
    assert shell_fields["PTCLI_MATERIAL_SCREENSHOTS_FILES_EXIST"] == "1"
    assert shell_fields["PTCLI_MATERIAL_SCREENSHOTS_SHA1S"] == screenshot_evidence[0]["sha1"]
    assert shell_fields["PTCLI_MATERIAL_IMAGE_HOST_FILE"] == str(image_host)
    assert shell_fields["PTCLI_MATERIAL_IMAGE_HOST_FILE_EXISTS"] == "1"
    assert shell_fields["PTCLI_MATERIAL_IMAGE_HOST_FILE_SHA1"] == image_host_evidence["sha1"]


def test_summary_material_diagnostics_marks_bdinfo_optional_for_file_content() -> None:
    diagnostics = ptcli_cli._summary_material_diagnostics(
        {
            "artifacts": {
                "target_materials": {
                    "ready": False,
                    "assets": {"disc_structure": {"ready": False, "path": "/downloads/Movie.mkv", "bdmv": False, "type": None}},
                    "missing": ["assets.mediainfo_or_bdinfo"],
                },
                "target_materials_ready": False,
                "target_materials_missing": ["assets.mediainfo_or_bdinfo"],
            }
        }
    )

    assert diagnostics["bdinfo_required"] is False
    assert diagnostics["media_info_requirement"] == "mediainfo_or_bdinfo"
    shell_fields = ptcli_cli._summary_check_material_shell_fields(diagnostics)
    assert shell_fields["PTCLI_MATERIAL_BDINFO_REQUIRED"] == "0"
    assert shell_fields["PTCLI_MATERIAL_MEDIA_INFO_REQUIREMENT"] == "mediainfo_or_bdinfo"


def test_summary_material_diagnostics_exposes_ready_for_mteam_upload() -> None:
    diagnostics = ptcli_cli._summary_material_diagnostics(
        {
            "artifacts": {
                "target_materials": {
                    "ready": True,
                    "assets": {"disc_structure": {"ready": False, "path": "/downloads/Movie.mkv", "bdmv": False, "type": None}},
                    "missing": [],
                },
                "target_materials_ready": True,
                "target_preparation_ready": True,
                "target_preparation_audit": {
                    "description_ready": True,
                    "description": {
                        "has_ptgen_description": True,
                        "has_external_ids": True,
                        "has_mediainfo_or_bdinfo": True,
                        "has_screenshot_bbcode": True,
                        "missing": [],
                    },
                },
            }
        }
    )

    assert diagnostics["ready_for_mteam_upload"] is True
    assert diagnostics["upload_material_gates"] == {"critical_ready": True, "target_materials_ready": True, "target_preparation_ready": True}
    assert diagnostics["upload_material_blockers"] == []
    assert diagnostics["critical_path"]["ready"] is True
    assert diagnostics["critical_path"]["next_step"] is None
    assert diagnostics["critical_path"]["missing"] == []
    shell_fields = ptcli_cli._summary_check_material_shell_fields(diagnostics)
    assert shell_fields["PTCLI_READY_FOR_MTEAM_UPLOAD"] == "1"
    assert shell_fields["PTCLI_MATERIAL_UPLOAD_BLOCKERS"] == ""
    assert shell_fields["PTCLI_MATERIAL_CRITICAL_PATH_READY"] == "1"
    assert shell_fields["PTCLI_MATERIAL_CRITICAL_PATH_NEXT_STEP"] is None
    assert json.loads(shell_fields["PTCLI_MATERIAL_UPLOAD_GATES"]) == {
        "critical_ready": True,
        "target_materials_ready": True,
        "target_preparation_ready": True,
    }
    matrix = ptcli_cli._summary_completion_matrix(
        flow_diagnostics={},
        material_diagnostics=diagnostics,
        target_upload_diagnostics={},
        closure_review={},
        closure_status={},
        qbit_wait_mismatches=[],
    )
    assert matrix["domains"]["materials"]["evidence"]["ready_for_mteam_upload"] is True


def test_summary_material_diagnostics_exposes_description_external_id_readiness() -> None:
    diagnostics = ptcli_cli._summary_material_diagnostics(
        {
            "artifacts": {
                "target_preparation_audit": {
                    "description_ready": False,
                    "description": {
                        "has_external_ids": False,
                        "external_id_readiness": {"imdb": True, "tmdb": False, "douban": True},
                        "external_id_missing": ["tmdb"],
                        "external_links": {
                            "imdb": "https://www.imdb.com/title/tt1234567",
                            "tmdb": None,
                            "douban": "https://movie.douban.com/subject/1291546/",
                        },
                        "missing": ["materials.description.external_ids.tmdb"],
                    },
                },
                "target_preparation_ready": False,
            }
        }
    )

    description = diagnostics["description"]
    assert description["has_external_ids"] is False
    assert description["external_id_readiness"] == {"imdb": True, "tmdb": False, "douban": True}
    assert description["external_id_missing"] == ["tmdb"]
    assert description["completeness"] == {
        "ready": False,
        "missing": ["ptgen_description", "external_ids", "mediainfo_or_bdinfo", "screenshot_bbcode", "screenshot_coverage"],
        "recovery_missing": ["description.ptgen_description", "description.external_ids", "description.mediainfo_or_bdinfo", "description.screenshot_bbcode", "description.screenshot_coverage"],
        "next_actions": [
            "Fetch PTGen/Douban description with --fetch-ptgen or supply metadata containing ptgen_description, then rerun resume-target-package.",
            "Fetch or supply IMDb/TMDb/Douban metadata with --enrich-metadata, --fetch-ptgen, --metadata-file, --imdb-id, --tmdb-id, or --douban-id, then rerun resume-target-package.",
            "Generate or provide MediaInfo/BDInfo with --generate-mediainfo, --mediainfo-file, --generate-bdinfo, or --bdinfo-file, then rerun resume-target-package.",
            "Regenerate the MTEAM description after screenshot and image-host materials are ready.",
            "Upload screenshots to an image host with --upload-screenshots/--image-host or provide --image-host-file, then rerun resume-target-package.",
        ],
        "checks": [
            {"name": "ptgen_description", "ready": None},
            {"name": "external_ids", "ready": False},
            {"name": "mediainfo_or_bdinfo", "ready": None},
            {"name": "screenshot_bbcode", "ready": None},
            {"name": "screenshot_coverage", "ready": None},
        ],
    }
    shell_fields = ptcli_cli._summary_check_material_shell_fields(diagnostics)
    assert shell_fields["PTCLI_MATERIAL_DESCRIPTION_COMPLETE"] == "0"
    assert shell_fields["PTCLI_MATERIAL_DESCRIPTION_COMPLETENESS_MISSING"] == "ptgen_description,external_ids,mediainfo_or_bdinfo,screenshot_bbcode,screenshot_coverage"
    assert shell_fields["PTCLI_MATERIAL_DESCRIPTION_COMPLETENESS_RECOVERY_MISSING"] == "description.ptgen_description,description.external_ids,description.mediainfo_or_bdinfo,description.screenshot_bbcode,description.screenshot_coverage"
    assert "Fetch PTGen/Douban description" in shell_fields["PTCLI_MATERIAL_DESCRIPTION_COMPLETENESS_NEXT_ACTIONS"]
    assert "Upload screenshots to an image host" in shell_fields["PTCLI_MATERIAL_DESCRIPTION_COMPLETENESS_NEXT_ACTIONS"]
    assert json.loads(shell_fields["PTCLI_MATERIAL_DESCRIPTION_EXTERNAL_ID_READINESS"]) == {"imdb": True, "tmdb": False, "douban": True}
    assert shell_fields["PTCLI_MATERIAL_DESCRIPTION_EXTERNAL_ID_MISSING"] == "tmdb"
    assert shell_fields["PTCLI_MATERIAL_DESCRIPTION_HAS_IMDB"] == "1"
    assert shell_fields["PTCLI_MATERIAL_DESCRIPTION_HAS_TMDB"] == "0"
    assert shell_fields["PTCLI_MATERIAL_DESCRIPTION_HAS_DOUBAN"] == "1"


def test_resume_material_shell_fields_expose_description_external_id_readiness() -> None:
    resume_state = {
        "materials": {
            "closure": {
                "description": {
                    "ready": False,
                    "has_external_ids": False,
                    "external_id_readiness": {"imdb": True, "tmdb": False, "douban": True},
                    "external_id_missing": ["tmdb"],
                    "external_links": {
                        "imdb": "https://www.imdb.com/title/tt1234567",
                        "tmdb": None,
                        "douban": "https://movie.douban.com/subject/1291546/",
                    },
                }
            }
        }
    }

    shell_fields = ptcli_cli._summary_check_resume_material_shell_fields(resume_state)

    assert json.loads(shell_fields["PTCLI_RESUME_MATERIAL_DESCRIPTION_EXTERNAL_ID_READINESS"]) == {"imdb": True, "tmdb": False, "douban": True}
    assert shell_fields["PTCLI_RESUME_MATERIAL_DESCRIPTION_EXTERNAL_ID_MISSING"] == "tmdb"
    assert shell_fields["PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_IMDB"] == "1"
    assert shell_fields["PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_TMDB"] == "0"
    assert shell_fields["PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_DOUBAN"] == "1"
    assert shell_fields["PTCLI_RESUME_MATERIAL_DESCRIPTION_IMDB_LINK"] == "https://www.imdb.com/title/tt1234567"
    assert shell_fields["PTCLI_RESUME_MATERIAL_DESCRIPTION_TMDB_LINK"] is None
    assert shell_fields["PTCLI_RESUME_MATERIAL_DESCRIPTION_DOUBAN_LINK"] == "https://movie.douban.com/subject/1291546/"


def test_summary_check_blocks_missing_pipeline_closure_audit(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": True,
                "complete": True,
                "blockers": [],
                "closure_audit": {
                    "ready": False,
                    "missing": ["target.uploaded_wait_evidence"],
                    "items": [{"name": "target.uploaded_wait_evidence", "scope": "target", "ok": False}],
                },
                "resume_commands": [{"stage": "resume-uploaded-torrent", "command": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"}],
                "resume_state": {
                    "next_stage": None,
                    "next_command": None,
                    "available_stages": ["resume-uploaded-torrent"],
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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
    assert payload["missing_artifacts"] == []
    assert payload["missing_closure_audit"] == ["target.uploaded_wait_evidence"]
    assert "closure audit missing: target.uploaded_wait_evidence" in payload["blockers"]
    assert payload["next_stage"] == "resume-uploaded-torrent"
    assert payload["next_command"] == "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"
    assert payload["automation_action"] == "run_next_command"
    assert payload["next_command_source"] == "resume_commands"
    assert payload["next_command_subcommand"] == "target-upload"
    assert payload["next_command_run_allowed"] is True
    assert payload["next_command_run_blocker"] is None


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
                        "target_preparation_ready": True,
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
    assert payload["next_command_source"] == "resume_state"
    assert "source_wait_evidence" in payload["missing_artifacts"]
    assert "uploaded_torrent_hash" in payload["missing_artifacts"]
    assert "injected_torrent_hash" in payload["missing_artifacts"]
    assert "injection_visible_in_client" in payload["missing_artifacts"]
    assert "injection_verified" in payload["missing_artifacts"]
    assert "target_rule_obligations" in payload["missing_artifacts"]
    assert "uploaded_wait_evidence" in payload["missing_artifacts"]
    assert "missing audit artifact: source_wait_evidence" in payload["blockers"]
    assert "missing audit artifact: uploaded_torrent_hash" in payload["blockers"]
    assert "missing audit artifact: injected_torrent_hash" in payload["blockers"]
    assert "missing audit artifact: injection_visible_in_client" in payload["blockers"]
    assert "missing audit artifact: injection_verified" in payload["blockers"]
    assert "missing audit artifact: target_rule_obligations" in payload["blockers"]
    assert "missing audit artifact: uploaded_wait_evidence" in payload["blockers"]


def test_summary_check_prefers_target_package_for_preparation_audit(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": True,
                "complete": True,
                "blockers": [],
                "evidence": {"source": {"mode": "matched"}, "target": {"mode": "live_upload"}},
                "closure_audit": {"ready": True, "missing": [], "items": []},
                "resume_commands": [
                    {"stage": "resume-target-package", "command": "python3 ptcli.py pipeline --prepare-target", "argv": ["python3", "ptcli.py", "pipeline", "--prepare-target"]},
                    {"stage": "resume-target-upload", "command": "python3 ptcli.py pipeline --upload-target", "argv": ["python3", "ptcli.py", "pipeline", "--upload-target"]},
                ],
                "resume_state": {
                    "next_stage": None,
                    "next_command": None,
                    "available_stages": ["resume-target-package", "resume-target-upload"],
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": False,
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
    assert "target_preparation_ready" in payload["missing_artifacts"]
    assert "missing audit artifact: target_preparation_ready" in payload["blockers"]
    assert payload["next_stage"] == "resume-target-package"
    assert payload["next_command"] == "python3 ptcli.py pipeline --prepare-target"
    assert payload["next_command_argv"] == ["python3", "ptcli.py", "pipeline", "--prepare-target"]
    assert "materials" in payload["readiness_summary"]["missing_domains"]
    assert payload["readiness_summary"]["materials_ready"] is False
    assert payload["readiness_summary"]["next_stage"] == "resume-target-package"
    assert payload["readiness_summary"]["next_command"] == "python3 ptcli.py pipeline --prepare-target"
    assert payload["automation_action"] == "run_next_command"


def test_summary_check_prefers_source_resume_for_source_visibility_artifact(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": True,
                "complete": True,
                "blockers": [],
                "evidence": {"source": {"mode": "downloaded"}},
                "resume_commands": [
                    {"stage": "resume-source-torrent", "command": "python3 ptcli.py pipeline --source-torrent-file /tmp/U2-60635.torrent"},
                    {"stage": "resume-uploaded-torrent", "command": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"},
                ],
                "resume_state": {
                    "available_stages": ["resume-source-torrent", "resume-uploaded-torrent"],
                    "artifacts": {
                        "source_torrent_hash": True,
                        "source_injected_torrent_hash": True,
                        "source_injection_visible_in_client": False,
                        "source_injection_verified": True,
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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
    assert payload["next_stage"] == "resume-source-torrent"
    assert payload["next_command"] == "python3 ptcli.py pipeline --source-torrent-file /tmp/U2-60635.torrent"
    assert "source_injection_visible_in_client" in payload["missing_artifacts"]


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
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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


def test_summary_check_prefers_uploaded_resume_for_target_visibility_artifact(tmp_path, capsys) -> None:
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
                    "available_stages": ["target-upload-retry", "resume-uploaded-torrent"],
                    "artifacts": {
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": False,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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
    assert payload["next_stage"] == "resume-uploaded-torrent"
    assert payload["next_command"] == "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"
    assert "injection_visible_in_client" in payload["missing_artifacts"]


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
                                "query": {"torrent_hash": "b" * 40, "content_path": "/downloads/Expected", "save_path": "/downloads", "timeout": 3600, "interval": 30},
                                "matched_count": 1,
                                "completion_verification": {
                                    "matched_count": 1,
                                    "complete_count": 1,
                                    "any_complete": True,
                                    "all_matches_complete": True,
                                    "seeding_state_count": 1,
                                    "requested_hash_matched": False,
                                    "requested_content_path_matched": None,
                                    "observed_hashes": ["f" * 40, "e" * 40],
                                    "observed_content_paths": ["/downloads/Other", "/downloads/Second"],
                                    "observed_save_paths": ["/downloads", "/downloads2"],
                                    "observed_states": ["uploading", "stalledUP"],
                                    "observed_progress": [1.0, 0.5],
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
                        "target_preparation_ready": True,
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
    assert f"source suggested retry values: hash={'f' * 40}, path=/downloads/Other, save_path=/downloads." in payload["automation_reason"]
    assert payload["next_stage"] == "resume-source-torrent"
    assert payload["next_command"] == "python3 ptcli.py pipeline --source-torrent-file /tmp/U2-60635.torrent"
    assert payload["next_command_ready"] is True
    assert payload["should_execute_next_command"] is False
    assert payload["qbit_wait_mismatch"] is True
    assert payload["qbit_wait_mismatches"] == ["source.requested_hash"]
    diagnostics = payload["qbit_wait_diagnostics"]["source"]
    assert diagnostics["request_mismatch"] is True
    assert diagnostics["requested_hash"] == "b" * 40
    assert diagnostics["requested_content_path"] == "/downloads/Expected"
    assert diagnostics["requested_save_path"] == "/downloads"
    assert diagnostics["requested_timeout"] == 3600
    assert diagnostics["requested_interval"] == 30
    assert diagnostics["any_complete"] is True
    assert diagnostics["all_matches_complete"] is True
    assert diagnostics["seeding_state_count"] == 1
    assert diagnostics["complete_count"] == 1
    assert diagnostics["requested_hash_matched"] is False
    assert diagnostics["observed_hashes"] == ["f" * 40, "e" * 40]
    assert diagnostics["observed_content_paths"] == ["/downloads/Other", "/downloads/Second"]
    assert diagnostics["observed_states"] == ["uploading", "stalledUP"]
    assert diagnostics["observed_progress"] == [1.0, 0.5]
    retry_hint = payload["qbit_wait_retry_hints"]["source"]
    assert retry_hint["retry_recommended"] is True
    assert retry_hint["suggested_torrent_hash"] == "f" * 40
    assert retry_hint["suggested_content_path"] == "/downloads/Other"
    assert retry_hint["suggested_save_path"] == "/downloads"
    assert retry_hint["observed_candidate_count"] == 2
    assert retry_hint["observed_candidates"] == [
        {"hash": "f" * 40, "content_path": "/downloads/Other", "save_path": "/downloads", "state": "uploading", "progress": 1.0},
        {"hash": "e" * 40, "content_path": "/downloads/Second", "save_path": "/downloads2", "state": "stalledUP", "progress": 0.5},
    ]
    assert retry_hint["reason"] == "source qBittorrent wait matched a different torrent/content than requested_hash."


def test_summary_check_blocks_complete_pipeline_qbit_wait_mismatch(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": True,
                "complete": True,
                "blockers": [],
                "evidence": {
                    "target": {
                        "qbit_closure": {
                            "wait": {
                                "complete": True,
                                "matched_count": 1,
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
                        }
                    }
                },
                "resume_commands": [{"stage": "resume-uploaded-torrent", "command": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"}],
                "resume_state": {
                    "available_stages": ["resume-uploaded-torrent"],
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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
    assert payload["automation_action"] == "resolve_qbit_wait_mismatch"
    assert payload["qbit_wait_mismatches"] == ["uploaded.requested_content_path"]
    assert "qBittorrent wait mismatch: uploaded.requested_content_path" in payload["blockers"]
    assert payload["next_stage"] == "resume-uploaded-torrent"


def test_summary_check_blocks_ready_target_upload_qbit_wait_mismatch(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-target-upload-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.target_upload.summary",
                "summary": {
                    "ready": True,
                    "mode": "resumed_uploaded_torrent",
                    "blockers": [],
                    "uploaded_wait": {
                        "complete": True,
                        "matched_count": 1,
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
                    },
                },
                "recommended_commands": [{"stage": "resume-uploaded-torrent", "command": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent"}],
                "resume_state": {
                    "available_stages": ["resume-uploaded-torrent"],
                    "artifacts": {
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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
    assert payload["automation_action"] == "resolve_qbit_wait_mismatch"
    assert payload["qbit_wait_mismatches"] == ["uploaded.requested_hash"]
    assert payload["target_mode"] == "resumed_uploaded_torrent"
    assert "qBittorrent wait mismatch: uploaded.requested_hash" in payload["blockers"]
    assert payload["next_stage"] == "resume-uploaded-torrent"


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
    assert payload["artifacts"]["qbit_wait_mismatches"] == ["source.requested_hash", "uploaded.requested_content_path"]
    assert payload["artifacts"]["qbit_wait_retry_hints"]["source"]["suggested_torrent_hash"] == "f" * 40
    assert payload["artifacts"]["qbit_wait_retry_hints"]["source"]["suggested_content_path"] == "/downloads/Example"
    assert payload["artifacts"]["qbit_wait_retry_hints"]["uploaded"]["suggested_torrent_hash"] == "b" * 40
    assert payload["artifacts"]["qbit_wait_retry_hints"]["uploaded"]["suggested_content_path"] == "/downloads/Other"
    assert payload["artifacts"]["qbit_wait_retry_hints"]["uploaded"]["suggested_save_path"] == "/downloads"


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
                "summary": {"source": {"mode": "downloaded"}, "target": {"mode": "prepared"}},
                "flow_check": {
                    "ready": True,
                    "source_tracker": "U2",
                    "source_torrent_id": "60635",
                    "target_trackers": ["MTEAM"],
                    "source_capability": {
                        "tracker": "U2",
                        "source_info_adapter": "generic_details_cookie",
                        "source_download_adapter": "nexusphp_passkey",
                        "credential_requirements": ["TRACKERS.U2.passkey", "data/cookies/U2.txt"],
                    },
                    "target_capabilities": [{"tracker": "MTEAM", "target_upload_adapter": "mteam_api", "credential_requirements": ["TRACKERS.MTEAM.api_key"]}],
                    "credential_requirements": ["TRACKERS.U2.passkey", "data/cookies/U2.txt", "TRACKERS.MTEAM.api_key"],
                },
                "resume_commands": [
                    {
                        "stage": "resume-target-upload",
                        "command": "python3 ptcli.py pipeline --upload-target",
                        "argv": ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"],
                    },
                    {"stage": "inspect-client", "command": "python3 ptcli.py inspect --client default --json"},
                ],
                "resume_state": {
                    "next_stage": None,
                    "next_command": None,
                    "available_stages": ["resume-target-upload"],
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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
    assert payload["automation_reason"] == "Next generated ptcli command is ready to run for stage resume-target-upload."
    assert payload["next_command_source"] == "resume_commands"
    assert payload["next_command_ready"] is True
    assert payload["next_command_run_allowed"] is True
    assert payload["next_command_subcommand"] == "pipeline"
    assert payload["next_command_run_blocker"] is None
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
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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


def test_summary_check_print_next_argv_outputs_safe_command_argv(tmp_path, capsys) -> None:
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
                    },
                    {"stage": "inspect-client", "command": "python3 ptcli.py inspect --client default --json"},
                ],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "source_wait_evidence": True,
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
                        "uploaded_wait_evidence": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-next-argv"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"]


def test_summary_check_print_next_argv_is_quiet_when_complete(tmp_path, capsys) -> None:
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
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
                        "uploaded_wait_evidence": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-next-argv"])

    assert code == 0
    assert capsys.readouterr().out == ""


def test_summary_check_print_next_argv_fails_without_safe_argv(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "resume_commands": [{"stage": "resume-target-upload", "command": "python3 ptcli.py inspect --client default --json"}],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-next-argv"])

    assert code == 1
    assert capsys.readouterr().out == ""


def test_summary_check_print_first_runnable_command_outputs_allowlisted_candidate(tmp_path, capsys) -> None:
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
                    {"stage": "inspect-client", "command": "python3 ptcli.py inspect --client default --json"},
                    {
                        "stage": "resume-target-upload",
                        "command": "python3 ptcli.py pipeline --upload-target",
                        "argv": ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"],
                    },
                ],
                "resume_state": {
                    "next_stage": "inspect-client",
                    "next_command": "python3 ptcli.py inspect --client default --json",
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-first-runnable-command"])

    assert code == 0
    assert capsys.readouterr().out == "python3 ptcli.py pipeline --upload-target\n"


def test_summary_check_print_first_runnable_argv_outputs_allowlisted_candidate(tmp_path, capsys) -> None:
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
                    {"stage": "inspect-client", "command": "python3 ptcli.py inspect --client default --json"},
                    {
                        "stage": "resume-target-upload",
                        "command": "python3 ptcli.py pipeline --upload-target",
                        "argv": ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"],
                    },
                ],
                "resume_state": {
                    "next_stage": "inspect-client",
                    "next_command": "python3 ptcli.py inspect --client default --json",
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-first-runnable-argv"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"]


def test_summary_check_print_first_runnable_argv_fails_without_runnable_candidate(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "resume_commands": [{"stage": "inspect-client", "command": "python3 ptcli.py inspect --client default --json"}],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-first-runnable-argv"])

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
                "summary": {"source": {"mode": "downloaded"}, "target": {"mode": "prepared"}},
                "flow_check": {
                    "ready": True,
                    "source_tracker": "U2",
                    "source_torrent_id": "60635",
                    "target_trackers": ["MTEAM"],
                    "source_capability": {
                        "tracker": "U2",
                        "source_info_adapter": "generic_details_cookie",
                        "source_download_adapter": "nexusphp_passkey",
                        "credential_requirements": ["TRACKERS.U2.passkey", "data/cookies/U2.txt"],
                    },
                    "target_capabilities": [{"tracker": "MTEAM", "target_upload_adapter": "mteam_api", "credential_requirements": ["TRACKERS.MTEAM.api_key"]}],
                    "credential_requirements": ["TRACKERS.U2.passkey", "data/cookies/U2.txt", "TRACKERS.MTEAM.api_key"],
                },
                "artifacts": {
                    "material_generation": {
                        "prerequisites": {"ok": True, "blockers": []},
                        "metadata": {
                            "ok": True,
                            "missing": [],
                            "sources": ["source", "tmdb_api", "ptgen"],
                            "applied": {"tmdb_id": 999, "douban_url": "https://movie.douban.com/subject/1291546/"},
                            "readiness": {
                                "imdb_id": {"ready": True, "required": True, "source": "source"},
                                "tmdb_id": {"ready": True, "required": True, "source": "tmdb_api"},
                                "douban_id": {"ready": True, "required": True, "source": "ptgen"},
                                "douban_url": {"ready": True, "required": True, "source": "ptgen"},
                                "ptgen_description": {"ready": True, "required": True, "source": "ptgen"},
                            },
                            "imdb_id": 1234567,
                            "tmdb_id": 999,
                            "douban_id": "1291546",
                            "douban_url": "https://movie.douban.com/subject/1291546/",
                            "ptgen_description_length": 42,
                        },
                        "mediainfo": {"ok": True, "mediainfo_file": "/tmp/MI_FULL_00.txt"},
                        "screenshots": {"ok": True, "count": 3, "screenshot_files": ["/tmp/s1.png", "/tmp/s2.png", "/tmp/s3.png"]},
                        "image_host": {
                            "ok": True,
                            "host": "ptpimg",
                            "count": 3,
                            "image_host_file": "/tmp/image-host-uploads.json",
                            "items": [
                                {"raw_url": "https://img.example/raw-1.png", "img_url": "https://img.example/thumb-1.png", "web_url": "https://img.example/page-1"},
                                {"raw_url": "https://img.example/raw-2.png", "img_url": "https://img.example/thumb-2.png", "web_url": "https://img.example/page-2"},
                            ],
                        },
                    },
                    "target_materials": {"ready": True, "assets": {"disc_structure": {"ready": True, "path": "/downloads/Disc/BDMV", "bdmv": True, "type": "BDMV"}}, "missing": []},
                    "target_materials_ready": True,
                    "target_preparation_ready": True,
                    "target_preparation_audit": {
                        "description_ready": True,
                        "description": {
                            "path": "/tmp/with space/mteam-description-draft.txt",
                            "exists": True,
                            "char_length": 4096,
                            "expected_length": 4096,
                            "has_ptgen_description": True,
                            "has_external_ids": True,
                            "external_links": {
                                "imdb": "https://www.imdb.com/title/tt1234567",
                                "tmdb": "https://www.themoviedb.org/movie/999",
                                "douban": "https://movie.douban.com/subject/1291546/",
                            },
                            "has_mediainfo_or_bdinfo": True,
                            "media_info": {"has_excerpt": True, "source": "/tmp/MI_FULL_00.txt", "length": 34},
                            "has_screenshot_bbcode": True,
                            "bbcode_image_count": 3,
                            "bbcode_image_urls": ["https://img.example/thumb-1.png", "https://img.example/thumb-2.png"],
                            "screenshot_coverage": {
                                "ready": False,
                                "expected_urls": ["https://img.example/thumb-1.png", "https://img.example/thumb-2.png", "https://img.example/thumb-3.png"],
                                "description_urls": ["https://img.example/thumb-1.png", "https://img.example/thumb-2.png"],
                                "missing_urls": ["https://img.example/thumb-3.png"],
                            },
                        },
                    },
                    "target_materials_missing": [],
                    "target_preparation_missing": [],
                },
                "resume_commands": [
                    {
                        "stage": "resume-target-upload",
                        "command": "python3 ptcli.py pipeline --upload-target",
                        "argv": ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"],
                    },
                    {"stage": "inspect-client", "command": "python3 ptcli.py inspect --client default --json"},
                ],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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
    assert "export PTCLI_AUTOMATION_REASON='Next generated ptcli command is ready to run for stage resume-target-upload.'\n" in out
    assert "export PTCLI_AUTOMATION_EXIT_CODE=1\n" in out
    assert "export PTCLI_BLOCKERS=target.uploaded\n" in out
    assert "export PTCLI_READINESS_STATUS=blocked\n" in out
    assert "export PTCLI_READINESS_READY=0\n" in out
    assert "export PTCLI_READINESS_COMPLETE=0\n" in out
    assert "export PTCLI_READINESS_BLOCKERS=target.uploaded\n" in out
    assert "export PTCLI_READINESS_NEXT_STAGE=resume-target-upload\n" in out
    assert "export PTCLI_READINESS_NEXT_COMMAND='python3 ptcli.py pipeline --upload-target'\n" in out
    assert 'export PTCLI_READINESS_NEXT_COMMAND_ARGV=\'["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"]\'\n' in out
    assert "export PTCLI_READINESS_AUTOMATION_ACTION=run_next_command\n" in out
    assert "export PTCLI_READINESS_SHOULD_EXECUTE_NEXT_COMMAND=1\n" in out
    assert "export PTCLI_READINESS_AUTOMATION_EXIT_CODE=1\n" in out
    assert "export PTCLI_READINESS_FLOW_READY=1\n" in out
    assert "export PTCLI_READINESS_MATERIALS_READY=1\n" in out
    assert "export PTCLI_READINESS_READY_FOR_MTEAM_UPLOAD=1\n" in out
    assert "export PTCLI_READINESS_QBIT_WAIT_MISMATCH=0\n" in out
    assert "export PTCLI_MISSING_ARTIFACTS=''\n" in out
    assert "export PTCLI_MISSING_CLOSURE_AUDIT=''\n" in out
    assert "export PTCLI_FLOW_READY=1\n" in out
    assert "export PTCLI_FLOW_SOURCE_TRACKER=U2\n" in out
    assert "export PTCLI_FLOW_SOURCE_ID=60635\n" in out
    assert "export PTCLI_FLOW_TARGET_TRACKERS=MTEAM\n" in out
    assert "export PTCLI_CREDENTIAL_REQUIREMENTS=TRACKERS.U2.passkey,data/cookies/U2.txt,TRACKERS.MTEAM.api_key\n" in out
    assert "export PTCLI_MATERIAL_PRESENT=1\n" in out
    assert "export PTCLI_MATERIAL_GENERATION_READY=1\n" in out
    assert "export PTCLI_TARGET_MATERIALS_READY=1\n" in out
    assert "export PTCLI_TARGET_PREPARATION_READY=1\n" in out
    assert "export PTCLI_TARGET_MATERIALS_MISSING=''\n" in out
    assert "export PTCLI_TARGET_PREPARATION_MISSING=''\n" in out
    assert "export PTCLI_MATERIAL_BLOCKERS=''\n" in out
    assert "export PTCLI_MATERIAL_DISC_TYPE=BDMV\n" in out
    assert "export PTCLI_MATERIAL_DISC_BDMV=1\n" in out
    assert "export PTCLI_MATERIAL_DISC_PATH=/downloads/Disc/BDMV\n" in out
    assert "export PTCLI_MATERIAL_PREREQUISITES_OK=1\n" in out
    assert "export PTCLI_MATERIAL_METADATA_OK=1\n" in out
    assert "export PTCLI_MATERIAL_METADATA_SOURCES=source,tmdb_api,ptgen\n" in out
    assert "export PTCLI_MATERIAL_METADATA_APPLIED_KEYS=douban_url,tmdb_id\n" in out
    assert "export PTCLI_MATERIAL_METADATA_BLOCKERS=''\n" in out
    assert "export PTCLI_MATERIAL_METADATA_READINESS_BLOCKERS=''\n" in out
    assert "export PTCLI_MATERIAL_METADATA_BLOCKER_COUNT=0\n" in out
    assert "export PTCLI_MATERIAL_METADATA_READINESS_BLOCKER_COUNT=0\n" in out
    assert "export PTCLI_MATERIAL_METADATA_IMDB_ID=1234567\n" in out
    assert "export PTCLI_MATERIAL_METADATA_TMDB_ID=999\n" in out
    assert "export PTCLI_MATERIAL_METADATA_DOUBAN_ID=1291546\n" in out
    assert "export PTCLI_MATERIAL_METADATA_DOUBAN_URL=https://movie.douban.com/subject/1291546/\n" in out
    assert "export PTCLI_MATERIAL_PTGEN_DESCRIPTION_LENGTH=42\n" in out
    assert "export PTCLI_MATERIAL_METADATA_IMDB_READY=1\n" in out
    assert "export PTCLI_MATERIAL_METADATA_IMDB_SOURCE=source\n" in out
    assert "export PTCLI_MATERIAL_METADATA_TMDB_READY=1\n" in out
    assert "export PTCLI_MATERIAL_METADATA_TMDB_SOURCE=tmdb_api\n" in out
    assert "export PTCLI_MATERIAL_METADATA_DOUBAN_ID_READY=1\n" in out
    assert "export PTCLI_MATERIAL_METADATA_DOUBAN_ID_SOURCE=ptgen\n" in out
    assert "export PTCLI_MATERIAL_METADATA_DOUBAN_URL_READY=1\n" in out
    assert "export PTCLI_MATERIAL_METADATA_DOUBAN_URL_SOURCE=ptgen\n" in out
    assert "export PTCLI_MATERIAL_METADATA_PTGEN_READY=1\n" in out
    assert "export PTCLI_MATERIAL_METADATA_PTGEN_REQUIRED=1\n" in out
    assert "export PTCLI_MATERIAL_METADATA_PTGEN_SOURCE=ptgen\n" in out
    assert "export PTCLI_MATERIAL_MEDIAINFO_OK=1\n" in out
    assert "export PTCLI_MATERIAL_SCREENSHOTS_OK=1\n" in out
    assert "export PTCLI_MATERIAL_SCREENSHOTS_COUNT=3\n" in out
    assert "export PTCLI_MATERIAL_IMAGE_HOST_OK=1\n" in out
    assert "export PTCLI_MATERIAL_IMAGE_HOST_HOST=ptpimg\n" in out
    assert "export PTCLI_MATERIAL_IMAGE_HOST_COUNT=3\n" in out
    assert "export PTCLI_MATERIAL_IMAGE_HOST_RAW_URLS=https://img.example/raw-1.png,https://img.example/raw-2.png\n" in out
    assert "export PTCLI_MATERIAL_IMAGE_HOST_IMG_URLS=https://img.example/thumb-1.png,https://img.example/thumb-2.png\n" in out
    assert "export PTCLI_MATERIAL_IMAGE_HOST_WEB_URLS=https://img.example/page-1,https://img.example/page-2\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_READY=1\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_COMPLETE=0\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_COMPLETENESS_MISSING=screenshot_coverage\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_COMPLETENESS_RECOVERY_MISSING=description.screenshot_coverage\n" in out
    assert "PTCLI_MATERIAL_DESCRIPTION_COMPLETENESS_NEXT_ACTIONS='Upload screenshots to an image host" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_PATH='/tmp/with space/mteam-description-draft.txt'\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_LENGTH=4096\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_EXPECTED_LENGTH=4096\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_HAS_PTGEN=1\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_HAS_EXTERNAL_IDS=1\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_IMDB_LINK=https://www.imdb.com/title/tt1234567\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_TMDB_LINK=https://www.themoviedb.org/movie/999\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_DOUBAN_LINK=https://movie.douban.com/subject/1291546/\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_HAS_MEDIAINFO_OR_BDINFO=1\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_MEDIAINFO_SOURCE=/tmp/MI_FULL_00.txt\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_MEDIAINFO_LENGTH=34\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_MEDIAINFO_HAS_EXCERPT=1\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_HAS_SCREENSHOTS=1\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_IMAGE_COUNT=3\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_IMAGE_URLS=https://img.example/thumb-1.png,https://img.example/thumb-2.png\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_READY=0\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_EXPECTED_URLS=https://img.example/thumb-1.png,https://img.example/thumb-2.png,https://img.example/thumb-3.png\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_DESCRIPTION_URLS=https://img.example/thumb-1.png,https://img.example/thumb-2.png\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_MISSING_URLS=https://img.example/thumb-3.png\n" in out
    assert "export PTCLI_MATERIAL_DESCRIPTION_MISSING=''\n" in out
    assert "export PTCLI_SHOULD_EXECUTE_NEXT_COMMAND=1\n" in out
    assert "export PTCLI_QBIT_WAIT_MISMATCH=0\n" in out
    assert "export PTCLI_QBIT_WAIT_MISMATCHES=''\n" in out
    assert "export PTCLI_CLOSURE_STATUS_COMPLETE=0\n" in out
    assert "export PTCLI_CLOSURE_STATUS_READY=0\n" in out
    assert "export PTCLI_CLOSURE_STATUS_PIPELINE_STATUS=blocked\n" in out
    assert "export PTCLI_CLOSURE_STATUS_PIPELINE_BLOCKERS=target.uploaded\n" in out
    assert "export PTCLI_CLOSURE_STATUS_CLOSURE_COMPLETE=0\n" in out
    assert "export PTCLI_CLOSURE_STATUS_AUDIT_READY=0\n" in out
    assert "export PTCLI_CLOSURE_STATUS_QBIT_WAIT_MISMATCH=0\n" in out
    assert "export PTCLI_CLOSURE_SOURCE_READY=0\n" in out
    assert "export PTCLI_CLOSURE_TARGET_READY=0\n" in out
    assert "export PTCLI_SOURCE_MODE=downloaded\n" in out
    assert "export PTCLI_TARGET_MODE=prepared\n" in out
    assert "export PTCLI_NEXT_COMMAND='python3 ptcli.py pipeline --upload-target'\n" in out
    assert "export PTCLI_NEXT_COMMAND_SOURCE=resume_commands\n" in out
    assert "export PTCLI_NEXT_COMMAND_SUBCOMMAND=pipeline\n" in out
    assert "export PTCLI_NEXT_COMMAND_RUN_ALLOWED=1\n" in out
    assert "export PTCLI_NEXT_COMMAND_RUN_BLOCKER=''\n" in out
    assert "export PTCLI_CANDIDATE_COMMAND_COUNT=2\n" in out
    assert "export PTCLI_RUNNABLE_COMMAND_COUNT=1\n" in out
    assert "export PTCLI_FIRST_RUNNABLE_STAGE=resume-target-upload\n" in out
    assert "export PTCLI_FIRST_RUNNABLE_COMMAND='python3 ptcli.py pipeline --upload-target'\n" in out
    assert 'export PTCLI_FIRST_RUNNABLE_COMMAND_ARGV=\'["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"]\'\n' in out
    assert "export PTCLI_FIRST_RUNNABLE_COMMAND_SOURCE=resume_commands\n" in out
    assert "export PTCLI_FIRST_RUNNABLE_COMMAND_SUBCOMMAND=pipeline\n" in out
    assert "export PTCLI_REJECTED_COMMAND_COUNT=1\n" in out
    assert "export PTCLI_REJECTED_COMMAND_BLOCKERS='ptcli subcommand inspect is not in the summary-check auto-run allowlist'\n" in out
    assert "export PTCLI_FIRST_REJECTED_STAGE=inspect-client\n" in out
    assert "export PTCLI_FIRST_REJECTED_COMMAND='python3 ptcli.py inspect --client default --json'\n" in out
    assert "export PTCLI_FIRST_REJECTED_COMMAND_SOURCE=resume_commands\n" in out
    assert "export PTCLI_FIRST_REJECTED_COMMAND_SUBCOMMAND=inspect\n" in out
    assert "export PTCLI_FIRST_REJECTED_COMMAND_BLOCKER='ptcli subcommand inspect is not in the summary-check auto-run allowlist'\n" in out


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
                                "query": {"torrent_hash": "b" * 40, "content_path": "/downloads/Expected", "save_path": "/downloads", "timeout": 3600, "interval": 30},
                                "completion_verification": {
                                    "matched_count": 1,
                                    "complete_count": 1,
                                    "any_complete": True,
                                    "requested_hash_matched": False,
                                    "requested_content_path_matched": None,
                                    "observed_hashes": ["f" * 40, "e" * 40],
                                    "observed_content_paths": ["/downloads/Other", "/downloads/Second"],
                                    "observed_save_paths": ["/downloads", "/downloads2"],
                                    "observed_states": ["uploading", "stalledUP"],
                                    "observed_progress": [1.0, 0.5],
                                },
                                "blockers": ["qBittorrent matched torrents, but none matched requested hash bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb."],
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
    assert f"export PTCLI_AUTOMATION_REASON='qBittorrent wait evidence mismatched the requested torrent/content: source.requested_hash. source suggested retry values: hash={'f' * 40}, path=/downloads/Other, save_path=/downloads.'\n" in out
    assert "export PTCLI_SHOULD_EXECUTE_NEXT_COMMAND=0\n" in out
    assert "export PTCLI_BLOCKERS='source.wait_evidence,qBittorrent wait mismatch: source.requested_hash'\n" in out
    assert "export PTCLI_MISSING_ARTIFACTS=source_wait_evidence\n" in out
    assert "export PTCLI_FLOW_READY=''\n" in out
    assert "export PTCLI_CREDENTIAL_REQUIREMENTS=''\n" in out
    assert "export PTCLI_QBIT_WAIT_MISMATCH=1\n" in out
    assert "export PTCLI_QBIT_WAIT_MISMATCHES=source.requested_hash\n" in out
    assert "export PTCLI_CLOSURE_STATUS_QBIT_WAIT_MISMATCH=1\n" in out
    assert "export PTCLI_CLOSURE_STATUS_QBIT_WAIT_MISMATCHES=source.requested_hash\n" in out
    assert f"export PTCLI_QBIT_WAIT_SOURCE_REQUESTED_HASH={'b' * 40}\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_REQUESTED_CONTENT_PATH=/downloads/Expected\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_REQUESTED_SAVE_PATH=/downloads\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_REQUESTED_TIMEOUT=3600\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_REQUESTED_INTERVAL=30\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_REQUESTED_HASH_MATCHED=0\n" in out
    assert f"export PTCLI_QBIT_WAIT_SOURCE_OBSERVED_HASHES={'f' * 40},{'e' * 40}\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_OBSERVED_CONTENT_PATHS=/downloads/Other,/downloads/Second\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_OBSERVED_SAVE_PATHS=/downloads,/downloads2\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_OBSERVED_STATES=uploading,stalledUP\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_OBSERVED_PROGRESS=1.0,0.5\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_ANY_COMPLETE=1\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_RETRY_RECOMMENDED=1\n" in out
    assert f"export PTCLI_QBIT_WAIT_SOURCE_SUGGESTED_HASH={'f' * 40}\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_SUGGESTED_CONTENT_PATH=/downloads/Other\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_SUGGESTED_SAVE_PATH=/downloads\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_OBSERVED_CANDIDATE_COUNT=2\n" in out
    assert f"export PTCLI_QBIT_WAIT_SOURCE_FIRST_CANDIDATE_HASH={'f' * 40}\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_FIRST_CANDIDATE_CONTENT_PATH=/downloads/Other\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_FIRST_CANDIDATE_SAVE_PATH=/downloads\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_FIRST_CANDIDATE_STATE=uploading\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_FIRST_CANDIDATE_PROGRESS=1.0\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_RETRY_REASON='source qBittorrent wait matched a different torrent/content than requested_hash.'\n" in out
    assert "export PTCLI_QBIT_WAIT_SOURCE_RETRY_ACTION='Resolve the source qBittorrent wait mismatch before rerunning:" in out
    assert f"Suggested retry values from qBittorrent: hash={'f' * 40}, path=/downloads/Other, save_path=/downloads." in out


def test_summary_check_print_shell_exposes_resume_material_fields(tmp_path, capsys) -> None:
    summary_file = tmp_path / "summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "kind": "ptcli.pipeline.run_summary",
                "schema_version": 1,
                "summary_file": str(summary_file),
                "status": "blocked",
                "ready": False,
                "complete": False,
                "blockers": ["target.materials_ready"],
                "resume_commands": [{"stage": "resume-target-package", "command": "python3 ptcli.py pipeline --prepare-target", "argv": ["python3", "ptcli.py", "pipeline", "--prepare-target"]}],
                "resume_state": {
                    "complete": False,
                    "resume_available": True,
                    "next_stage": "resume-target-package",
                    "next_command": "python3 ptcli.py pipeline --prepare-target",
                    "next_command_argv": ["python3", "ptcli.py", "pipeline", "--prepare-target"],
                    "available_stages": ["resume-target-package"],
                    "artifacts": {"target_materials_ready": False, "target_preparation_ready": False},
                    "materials": {
                        "target_materials_ready": False,
                        "target_preparation_ready": False,
                        "target_materials_missing": ["metadata.ptgen_description", "assets.image_host_uploads"],
                        "target_preparation_missing": ["description.content"],
                        "next_actions": [
                            "Fetch PTGen/Douban description with --fetch-ptgen or supply metadata containing ptgen_description, then rerun resume-target-package.",
                            "Upload screenshots to an image host with --upload-screenshots/--image-host or provide --image-host-file, then rerun resume-target-package.",
                        ],
                        "recovery_hints": [
                            {
                                "key": "metadata.ptgen_description",
                                "resume_stage": "resume-target-package",
                                "reason": "Fetch PTGen/Douban description before regenerating the MTEAM package.",
                                "command_flags": ["--enrich-metadata", "--fetch-ptgen"],
                                "existing_file_options": ["--metadata-file"],
                                "required_command_flags": ["--enrich-metadata", "--fetch-ptgen"],
                                "missing_command_flags": ["--enrich-metadata", "--fetch-ptgen"],
                                "resume_command_available": False,
                                "resume_command_stage": None,
                                "resume_command": None,
                                "resume_command_argv": [],
                            },
                            {
                                "key": "assets.image_host_uploads",
                                "resume_stage": "resume-target-package",
                                "reason": "Upload screenshots to an image host or provide existing image-host upload evidence before regenerating the MTEAM package.",
                                "command_flags": ["--upload-screenshots", "--image-host"],
                                "existing_file_options": ["--image-host-file"],
                                "required_command_flags": ["--upload-screenshots"],
                                "missing_command_flags": ["--upload-screenshots"],
                                "resume_command_available": False,
                                "resume_command_stage": None,
                                "resume_command": None,
                                "resume_command_argv": [],
                            },
                        ],
                    },
                    "blockers": ["target.materials_ready"],
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-shell"])

    assert code == 0
    out = capsys.readouterr().out
    assert "export PTCLI_AUTOMATION_ACTION=complete_material_recovery_command\n" in out
    assert "material recovery command does not cover metadata.ptgen_description" in out
    assert "export PTCLI_SHOULD_EXECUTE_NEXT_COMMAND=0\n" in out
    assert "export PTCLI_NEXT_COMMAND_RUN_ALLOWED=0\n" in out
    assert "export PTCLI_NEXT_COMMAND_RUN_BLOCKER='material recovery command does not cover metadata.ptgen_description; missing flags: --enrich-metadata,--fetch-ptgen'\n" in out
    assert "export PTCLI_CANDIDATE_COMMAND_COUNT=2\n" in out
    assert "export PTCLI_RUNNABLE_COMMAND_COUNT=1\n" in out
    assert "export PTCLI_FIRST_RUNNABLE_COMMAND='python3 ptcli.py pipeline --prepare-target --enrich-metadata --fetch-ptgen --upload-screenshots'\n" in out
    assert 'export PTCLI_FIRST_RUNNABLE_COMMAND_ARGV=\'["python3", "ptcli.py", "pipeline", "--prepare-target", "--enrich-metadata", "--fetch-ptgen", "--upload-screenshots"]\'\n' in out
    assert "export PTCLI_FIRST_RUNNABLE_COMMAND_SOURCE=material_recovery_completion\n" in out
    assert "export PTCLI_FIRST_REJECTED_COMMAND='python3 ptcli.py pipeline --prepare-target'\n" in out
    assert "export PTCLI_RESUME_MATERIALS_PRESENT=1\n" in out
    assert "export PTCLI_RESUME_TARGET_MATERIALS_READY=0\n" in out
    assert "export PTCLI_RESUME_TARGET_PREPARATION_READY=0\n" in out
    assert "export PTCLI_RESUME_TARGET_MATERIALS_MISSING=metadata.ptgen_description,assets.image_host_uploads\n" in out
    assert "export PTCLI_RESUME_TARGET_PREPARATION_MISSING=description.content\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_REQUIRED_FLAGS=--enrich-metadata,--fetch-ptgen,--upload-screenshots\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_EXISTING_FILE_OPTIONS=--metadata-file,--image-host-file\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_MISSING_FLAGS=--enrich-metadata,--fetch-ptgen,--upload-screenshots\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_COMPLETION_COMMAND='python3 ptcli.py pipeline --prepare-target --enrich-metadata --fetch-ptgen --upload-screenshots'\n" in out
    assert 'export PTCLI_RESUME_MATERIAL_RECOVERY_COMPLETION_COMMAND_ARGV=\'["python3", "ptcli.py", "pipeline", "--prepare-target", "--enrich-metadata", "--fetch-ptgen", "--upload-screenshots"]\'\n' in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_COMMAND_COVERAGE_READY=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_COMMAND_COVERAGE_AVAILABLE=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_COMMAND_COVERAGE_MISSING=2\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_FIRST_UNCOVERED_KEY=metadata.ptgen_description\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_FIRST_UNCOVERED_FLAGS=--enrich-metadata,--fetch-ptgen\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_PRESENT=1\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_TARGET_MATERIALS_MISSING=metadata.ptgen_description,assets.image_host_uploads\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_TARGET_PREPARATION_MISSING=description.content\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_HINT_COUNT=2\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_KEYS=metadata.ptgen_description,assets.image_host_uploads\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_REQUIRED_FLAGS=--enrich-metadata,--fetch-ptgen,--upload-screenshots\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_MISSING_FLAGS=--enrich-metadata,--fetch-ptgen,--upload-screenshots\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_EXISTING_FILE_OPTIONS=--metadata-file,--image-host-file\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_COMPLETION_COMMAND='python3 ptcli.py pipeline --prepare-target --enrich-metadata --fetch-ptgen --upload-screenshots'\n" in out
    assert 'export PTCLI_READINESS_MATERIAL_RECOVERY_COMPLETION_COMMAND_ARGV=\'["python3", "ptcli.py", "pipeline", "--prepare-target", "--enrich-metadata", "--fetch-ptgen", "--upload-screenshots"]\'\n' in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_COMMAND_COVERAGE_READY=0\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_COMMAND_COVERAGE_AVAILABLE=0\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_COMMAND_COVERAGE_MISSING=2\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_FIRST_UNCOVERED_KEY=metadata.ptgen_description\n" in out
    assert "export PTCLI_READINESS_MATERIAL_RECOVERY_FIRST_UNCOVERED_FLAGS=--enrich-metadata,--fetch-ptgen\n" in out
    assert "PTCLI_READINESS_MATERIAL_RECOVERY_NEXT_ACTIONS='Fetch PTGen/Douban description" in out
    assert "PTCLI_RESUME_MATERIAL_NEXT_ACTIONS='Fetch PTGen/Douban description" in out
    assert "Upload screenshots to an image host" in out


def _write_material_recovery_summary(tmp_path: Path) -> Path:
    summary_file = tmp_path / "summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "kind": "ptcli.pipeline.run_summary",
                "schema_version": 1,
                "summary_file": str(summary_file),
                "status": "blocked",
                "ready": False,
                "complete": False,
                "blockers": ["target.materials_ready"],
                "resume_commands": [
                    {
                        "stage": "resume-target-package",
                        "command": "python3 ptcli.py pipeline --prepare-target",
                        "argv": ["python3", "ptcli.py", "pipeline", "--prepare-target"],
                    }
                ],
                "resume_state": {
                    "complete": False,
                    "resume_available": True,
                    "next_stage": "resume-target-package",
                    "next_command": "python3 ptcli.py pipeline --prepare-target",
                    "next_command_argv": ["python3", "ptcli.py", "pipeline", "--prepare-target"],
                    "available_stages": ["resume-target-package"],
                    "materials": {
                        "target_materials_missing": ["assets.image_host_uploads"],
                        "target_preparation_missing": ["description.screenshot_coverage"],
                        "recovery_hints": [
                            {
                                "key": "assets.image_host_uploads",
                                "resume_stage": "resume-target-package",
                                "reason": "Upload screenshots to an image host or provide existing image-host upload evidence before regenerating the MTEAM package.",
                                "command_flags": ["--upload-screenshots", "--image-host"],
                                "existing_file_options": ["--image-host-file"],
                                "required_command_flags": ["--upload-screenshots"],
                                "missing_command_flags": [],
                                "resume_command_available": True,
                                "resume_command_stage": "resume-target-package",
                                "resume_command": "python3 ptcli.py pipeline --prepare-target --upload-screenshots --image-host ptpimg",
                                "resume_command_argv": ["python3", "ptcli.py", "pipeline", "--prepare-target", "--upload-screenshots", "--image-host", "ptpimg"],
                            }
                        ],
                    },
                    "blockers": ["target.materials_ready"],
                },
            }
        ),
        encoding="utf-8",
    )
    return summary_file


def test_summary_check_promotes_material_recovery_command(tmp_path, capsys) -> None:
    summary_file = _write_material_recovery_summary(tmp_path)

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_stage"] == "resume-target-package"
    assert payload["next_command"] == "python3 ptcli.py pipeline --prepare-target --upload-screenshots --image-host ptpimg"
    assert payload["next_command_argv"] == ["python3", "ptcli.py", "pipeline", "--prepare-target", "--upload-screenshots", "--image-host", "ptpimg"]
    assert payload["next_command_source"] == "material_recovery"
    assert payload["next_command_run_allowed"] is True
    assert payload["automation_action"] == "run_next_command"
    assert payload["readiness_summary"]["material_recovery"]["first_command"] == "python3 ptcli.py pipeline --prepare-target --upload-screenshots --image-host ptpimg"


def test_summary_check_print_next_argv_uses_material_recovery_command(tmp_path, capsys) -> None:
    summary_file = _write_material_recovery_summary(tmp_path)

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-next-argv"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == ["python3", "ptcli.py", "pipeline", "--prepare-target", "--upload-screenshots", "--image-host", "ptpimg"]


def test_summary_check_run_next_command_uses_material_recovery_command(tmp_path, monkeypatch, capsys) -> None:
    summary_file = _write_material_recovery_summary(tmp_path)
    calls = []

    def fake_run(argv, check):
        calls.append((argv, check))
        return argparse.Namespace(returncode=11)

    monkeypatch.setattr(ptcli_cli.subprocess, "run", fake_run)

    code = main(["summary-check", "--summary-file", str(summary_file), "--run-next-command"])

    assert code == 11
    assert calls == [([ptcli_cli.sys.executable, str(ptcli_cli._ptcli_script_path()), "pipeline", "--prepare-target", "--upload-screenshots", "--image-host", "ptpimg"], False)]
    assert capsys.readouterr().out == ""


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
                        "target_preparation_ready": True,
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
    assert calls == [([ptcli_cli.sys.executable, str(ptcli_cli._ptcli_script_path()), "pipeline", "--upload-target", "--package-dir", "/tmp/with space"], False)]
    assert capsys.readouterr().out == ""


def test_summary_check_run_first_runnable_command_executes_allowlisted_candidate(tmp_path, monkeypatch, capsys) -> None:
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
                    {"stage": "inspect-client", "command": "python3 ptcli.py inspect --client default --json"},
                    {
                        "stage": "resume-target-upload",
                        "command": "python3 ptcli.py pipeline --upload-target",
                        "argv": ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"],
                    },
                ],
                "resume_state": {
                    "next_stage": "inspect-client",
                    "next_command": "python3 ptcli.py inspect --client default --json",
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_run(argv, check):
        calls.append((argv, check))
        return argparse.Namespace(returncode=9)

    monkeypatch.setattr(ptcli_cli.subprocess, "run", fake_run)

    code = main(["summary-check", "--summary-file", str(summary_file), "--run-first-runnable-command"])

    assert code == 9
    assert calls == [([ptcli_cli.sys.executable, str(ptcli_cli._ptcli_script_path()), "pipeline", "--upload-target", "--package-dir", "/tmp/with space"], False)]
    assert capsys.readouterr().out == ""


def test_summary_check_run_first_runnable_command_fails_without_candidate(tmp_path, monkeypatch, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "resume_commands": [{"stage": "inspect-client", "command": "python3 ptcli.py inspect --client default --json"}],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    def fail_run(*_args, **_kwargs):
        pytest.fail("unexpected subprocess call")

    monkeypatch.setattr(ptcli_cli.subprocess, "run", fail_run)

    code = main(["summary-check", "--summary-file", str(summary_file), "--run-first-runnable-command"])

    assert code == 1
    assert capsys.readouterr().out == ""


def test_summary_check_exposes_structured_next_command_argv(tmp_path, capsys) -> None:
    flow_check = {
        "ready": True,
        "source_tracker": "U2",
        "source_torrent_id": "60635",
        "target_trackers": ["MTEAM"],
        "source_capability": {
            "tracker": "U2",
            "source_info_adapter": "generic_details_cookie",
            "source_download_adapter": "nexusphp_passkey",
            "credential_requirements": ["TRACKERS.U2.passkey", "data/cookies/U2.txt"],
        },
        "target_capabilities": [{"tracker": "MTEAM", "target_upload_adapter": "mteam_api", "credential_requirements": ["TRACKERS.MTEAM.api_key"]}],
        "credential_requirements": ["TRACKERS.U2.passkey", "data/cookies/U2.txt", "TRACKERS.MTEAM.api_key"],
    }
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "flow_check": flow_check,
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
                        "target_preparation_ready": True,
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
    assert payload["next_command_source"] == "resume_commands"
    assert payload["next_command_subcommand"] == "pipeline"
    assert payload["next_command_run_allowed"] is True
    assert payload["next_command_run_blocker"] is None
    assert payload["candidate_command_count"] == 1
    assert payload["runnable_command_count"] == 1
    assert payload["first_runnable_stage"] == "resume-target-upload"
    assert payload["first_runnable_command"] == "python3 ptcli.py pipeline --upload-target"
    assert payload["first_runnable_command_argv"] == ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"]
    assert payload["readiness_summary"]["status"] == "blocked"
    assert payload["readiness_summary"]["ready"] is False
    assert payload["readiness_summary"]["complete"] is False
    assert payload["readiness_summary"]["blockers"] == ["target.uploaded"]
    assert payload["readiness_summary"]["next_stage"] == "resume-target-upload"
    assert payload["readiness_summary"]["next_command"] == "python3 ptcli.py pipeline --upload-target"
    assert payload["readiness_summary"]["next_command_argv"] == ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"]
    assert payload["readiness_summary"]["automation_action"] == "run_next_command"
    assert payload["readiness_summary"]["should_execute_next_command"] is True
    assert payload["readiness_summary"]["automation_exit_code"] == 1
    assert payload["readiness_summary"]["flow_ready"] is True
    assert payload["readiness_summary"]["qbit_wait_mismatch"] is False
    assert payload["first_runnable_command_source"] == "resume_commands"
    assert payload["first_runnable_command_subcommand"] == "pipeline"
    assert payload["rejected_command_count"] == 0
    assert payload["rejected_command_blockers"] == []
    assert payload["first_rejected_stage"] is None
    assert payload["candidate_commands"] == [
        {
            "stage": "resume-target-upload",
            "command": "python3 ptcli.py pipeline --upload-target",
            "argv": ["python3", "ptcli.py", "pipeline", "--upload-target", "--package-dir", "/tmp/with space"],
            "source": "resume_commands",
            "subcommand": "pipeline",
            "run_allowed": True,
            "run_blocker": None,
            "placeholder": False,
        }
    ]
    assert payload["flow_diagnostics"]["present"] is True
    assert payload["flow_diagnostics"]["source_capability"]["source_download_adapter"] == "nexusphp_passkey"
    assert payload["credential_requirements"] == ["TRACKERS.U2.passkey", "data/cookies/U2.txt", "TRACKERS.MTEAM.api_key"]


def test_summary_check_exposes_unsupported_next_command_metadata(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "resume_commands": [{"stage": "resume-target-upload", "command": "python3 ptcli.py inspect --client default --json"}],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["automation_action"] == "unsupported_next_command"
    assert payload["next_command_ready"] is True
    assert payload["next_command_run_allowed"] is False
    assert payload["next_command_subcommand"] == "inspect"
    assert payload["next_command_run_blocker"] == "ptcli subcommand inspect is not in the summary-check auto-run allowlist"
    assert payload["candidate_command_count"] == 1
    assert payload["runnable_command_count"] == 0
    assert payload["candidate_commands"][0]["stage"] == "resume-target-upload"
    assert payload["candidate_commands"][0]["source"] == "resume_commands"
    assert payload["candidate_commands"][0]["subcommand"] == "inspect"
    assert payload["candidate_commands"][0]["run_allowed"] is False
    assert payload["candidate_commands"][0]["run_blocker"] == "ptcli subcommand inspect is not in the summary-check auto-run allowlist"
    assert payload["should_execute_next_command"] is False
    assert payload["first_runnable_stage"] is None
    assert payload["first_runnable_command"] is None
    assert payload["first_runnable_command_argv"] is None
    assert payload["first_runnable_command_source"] is None
    assert payload["first_runnable_command_subcommand"] is None
    assert payload["rejected_command_count"] == 1
    assert payload["rejected_command_blockers"] == ["ptcli subcommand inspect is not in the summary-check auto-run allowlist"]
    assert payload["first_rejected_stage"] == "resume-target-upload"
    assert payload["first_rejected_command"] == "python3 ptcli.py inspect --client default --json"
    assert payload["first_rejected_command_source"] == "resume_commands"
    assert payload["first_rejected_command_subcommand"] == "inspect"
    assert payload["first_rejected_command_blocker"] == "ptcli subcommand inspect is not in the summary-check auto-run allowlist"
    assert payload["automation_reason"] == "Next command is present but is not allowed for automatic execution: ptcli subcommand inspect is not in the summary-check auto-run allowlist."


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
                        "target_preparation_ready": True,
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


def test_summary_check_run_next_command_rejects_non_resume_ptcli_command(tmp_path, monkeypatch, capsys) -> None:
    summary_file = tmp_path / "ptcli-run-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.pipeline.run_summary",
                "ready": False,
                "complete": False,
                "blockers": ["target.uploaded"],
                "resume_commands": [{"stage": "resume-target-upload", "command": "python3 ptcli.py inspect --client default --json"}],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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


def test_summary_check_marks_placeholder_next_command_not_ready(tmp_path, capsys) -> None:
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
                        "stage": "resume-uploaded-torrent-download",
                        "command": "python3 ptcli.py target-upload --uploaded-torrent-id <id>",
                        "argv": ["python3", "ptcli.py", "target-upload", "--uploaded-torrent-id", "<id>"],
                    }
                ],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["automation_action"] == "fill_command_placeholders"
    assert payload["next_command_ready"] is False
    assert payload["next_command_placeholder"] is True
    assert payload["should_execute_next_command"] is False


def test_summary_check_run_next_command_rejects_placeholder_argv(tmp_path, monkeypatch, capsys) -> None:
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
                        "stage": "resume-uploaded-torrent-download",
                        "command": "python3 ptcli.py target-upload --uploaded-torrent-id <id>",
                        "argv": ["python3", "ptcli.py", "target-upload", "--uploaded-torrent-id", "<id>"],
                    }
                ],
                "resume_state": {
                    "artifacts": {
                        "source_hash_consistent": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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
    assert code == 1
    assert captured.out == ""
    assert captured.err == ""


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
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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
                        "uploaded_torrent_hash": True,
                        "injected_torrent_hash": True,
                        "injection_visible_in_client": True,
                        "injection_verified": True,
                        "target_hash_consistent": True,
                        "target_duplicate_clean": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
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


def test_summary_check_blocks_incomplete_target_upload_followup(tmp_path, capsys) -> None:
    summary_file = tmp_path / "ptcli-target-upload-summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ptcli.target_upload.summary",
                "summary": {
                    "ready": True,
                    "uploaded": True,
                    "uploaded_torrent_id": "999",
                    "blockers": [],
                    "completion_review": {
                        "complete": False,
                        "missing": ["uploaded_torrent_file", "injection_verified", "uploaded_wait_complete"],
                        "checks": {
                            "uploaded": True,
                            "uploaded_torrent_id": True,
                            "uploaded_torrent_file": False,
                            "injection_verified": False,
                            "uploaded_wait_complete": False,
                        },
                    },
                },
                "recommended_commands": [
                    {
                        "stage": "resume-uploaded-torrent-download",
                        "command": "python3 ptcli.py target-upload --uploaded-torrent-id 999 --download-uploaded-torrent --inject-uploaded-torrent",
                        "argv": [
                            "python3",
                            "ptcli.py",
                            "target-upload",
                            "--uploaded-torrent-id",
                            "999",
                            "--download-uploaded-torrent",
                            "--inject-uploaded-torrent",
                        ],
                    }
                ],
                "resume_state": {
                    "ready": True,
                    "resume_available": True,
                    "next_stage": "resume-uploaded-torrent-download",
                    "next_command": "python3 ptcli.py target-upload --uploaded-torrent-id 999 --download-uploaded-torrent --inject-uploaded-torrent",
                    "next_command_argv": [
                        "python3",
                        "ptcli.py",
                        "target-upload",
                        "--uploaded-torrent-id",
                        "999",
                        "--download-uploaded-torrent",
                        "--inject-uploaded-torrent",
                    ],
                    "artifacts": {
                        "uploaded_torrent_id": True,
                        "uploaded_torrent_file": False,
                        "injection_verified": False,
                        "uploaded_wait_evidence": False,
                    },
                    "uploaded_followup": {
                        "ready": False,
                        "ready_for_uploaded_seeding": False,
                        "missing": ["downloaded", "injection_verified", "uploaded_wait_evidence"],
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
    assert payload["ready"] is True
    assert payload["complete"] is False
    assert payload["live_safe_to_attempt"] is False
    assert payload["next_stage"] == "resume-uploaded-torrent-download"
    assert payload["next_command_argv"][:5] == ["python3", "ptcli.py", "target-upload", "--uploaded-torrent-id", "999"]
    assert payload["blockers"] == ["target upload follow-up incomplete: uploaded_torrent_file, injection_verified, uploaded_wait_complete."]


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
                        "target_preparation_ready": True,
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
                        "target_preparation_ready": True,
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
                "mode": "live_upload",
                "target_mode": "live_upload",
                "ready": True,
                "live_safe_to_attempt": True,
                "failed_check_names": [],
                "artifacts": {
                    "target_preflight_gates": {
                        "present": True,
                        "source": "doctor",
                        "status": "ready",
                        "ready": True,
                        "blockers": [],
                        "target_preparation_ready": True,
                        "materials_ready": True,
                        "metadata_ready": True,
                        "assets_ready": True,
                        "description_ready": True,
                        "payload_ready": True,
                        "payload_checks_ready": True,
                        "description_checks_ready": True,
                        "materials_ready_required": True,
                        "torrent_file": {
                            "path": "/tmp/exported/mteam.torrent",
                            "mteam_safe": True,
                            "metadata_readable": True,
                            "source_flag": "MTEAM",
                        },
                    },
                    "target_preparation_audit": {
                        "ready": True,
                        "materials_ready": True,
                        "metadata_ready": True,
                        "assets_ready": True,
                        "description_ready": True,
                        "payload_ready": True,
                        "missing": [],
                        "description": {
                            "has_ptgen_description": True,
                            "has_external_ids": True,
                            "has_mediainfo_or_bdinfo": True,
                            "has_screenshot_bbcode": True,
                            "missing": [],
                        },
                    },
                    "target_preparation_ready": True,
                    "target_preparation_missing": [],
                    "target_materials_ready": True,
                },
                "resume_state": {
                    "next_stage": "pipeline-live",
                    "next_command": "python3 ptcli.py pipeline --target-execute",
                    "available_stages": ["pipeline-live"],
                    "artifacts": {
                        "flow_check_ready": True,
                        "rule_check_ready": True,
                        "rules_acknowledged": True,
                        "live_upload_confirmation": True,
                        "target_rule_obligations": True,
                        "target_preparation_ready": True,
                        "target_package_preflight_ready": True,
                        "download_uploaded_torrent": True,
                        "inject_uploaded_torrent": True,
                        "effective_uploaded_save_path": True,
                        "wait_uploaded_complete": True,
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
    assert payload["target_mode"] == "live_upload"
    assert payload["closure_modes"]["target"] == "live_upload"
    preflight = payload["target_preflight_diagnostics"]
    assert preflight["present"] is True
    assert preflight["source"] == "doctor"
    assert preflight["status"] == "ready"
    assert preflight["ready"] is True
    assert preflight["materials_ready"] is True
    assert preflight["description_ready"] is True
    assert preflight["payload_ready"] is True
    assert preflight["torrent_file"]["path"] == "/tmp/exported/mteam.torrent"
    assert preflight["torrent_file"]["source_flag"] == "MTEAM"
    assert payload["material_diagnostics"]["present"] is True
    assert payload["material_diagnostics"]["critical_path"]["ready"] is True

    code = main(["summary-check", "--summary-file", str(summary_file), "--print-shell"])

    assert code == 0
    out = capsys.readouterr().out
    assert "export PTCLI_TARGET_PREFLIGHT_PRESENT=1\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_SOURCE=doctor\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_READY=1\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_MATERIALS_READY=1\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_DESCRIPTION_READY=1\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_PAYLOAD_READY=1\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_TORRENT_PATH=/tmp/exported/mteam.torrent\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_TORRENT_MTEAM_SAFE=1\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_TORRENT_SOURCE_FLAG=MTEAM\n" in out
    assert "export PTCLI_MATERIAL_CRITICAL_PATH_READY=1\n" in out


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
    assert payload["automation_handoff"]["json"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_file), "--json"]
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
    assert payload["automation_handoff"]["print_shell"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_file), "--print-shell"]
    assert "ptcli.pipeline.run_summary" in payload["supported_kinds"]
    assert "Unsupported ptcli summary kind: not.ptcli" in payload["blockers"]


def test_summary_check_missing_file_includes_automation_handoff(tmp_path, capsys) -> None:
    summary_file = tmp_path / "missing-summary.json"

    code = main(["summary-check", "--summary-file", str(summary_file), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["automation_action"] == "provide_summary"
    assert payload["automation_handoff"]["json"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_file), "--json"]
    assert payload["automation_handoff"]["print_first_runnable_argv"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_file), "--print-first-runnable-argv"]
    assert payload["automation_handoff"]["run_next_command"]["command"] == shlex.join(["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_file), "--run-next-command"])


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


def test_target_upload_result_requires_uploaded_torrent_visibility_evidence() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "verified_in_client": True},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is False
    assert "injected_torrent: qBittorrent did not list the injected torrent after add." in ptcli_cli._target_upload_result_blockers(payload)


def test_source_inject_result_requires_visibility_evidence() -> None:
    result = {"hash": "a" * 40, "verified_in_client": True}

    assert ptcli_cli._source_inject_result_blockers(result) == ["qBittorrent did not list the injected source torrent after add."]
    assert ptcli_cli._injected_torrent_verified(result) is False


def test_target_upload_result_requires_uploaded_torrent_client_metadata_match() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {
            "hash": "a" * 40,
            "visible_in_client": True, "verified_in_client": True,
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
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is True


def test_target_upload_result_requires_uploaded_torrent_hash_consistency() -> None:
    payload = {
        "status": "uploaded",
        "submitted_torrent_hash": "a" * 40,
        "uploaded_torrent_hash": "a" * 40,
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "torrent_hash": "b" * 40},
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is False


def test_target_upload_result_checks_uploaded_wait_match_hash_consistency() -> None:
    payload = {
        "status": "uploaded",
        "uploaded_torrent_hash": "a" * 40,
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "torrent_hash": "a" * 40},
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
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
        {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
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


def test_target_upload_summary_blocks_missing_rule_obligations() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
        "uploaded_wait": {"complete": True, "matches": [{"hash": "a" * 40}]},
    }

    summary = ptcli_cli._target_upload_summary(
        payload,
        {
            "status": "ready",
            "blockers": [],
            "rule_obligation_review": {"ready": False, "missing": ["source_download_and_retorrent", "mteam_upload_and_seed"]},
        },
    )

    assert summary["ready"] is False
    assert summary["rule_obligations"]["ready"] is False
    assert "target rule obligations are not ready: missing source_download_and_retorrent, mteam_upload_and_seed." in summary["blockers"]


def test_target_upload_resume_state_requires_ready_rule_obligations() -> None:
    resume_state = ptcli_cli._target_upload_resume_state(
        {"ready": True, "blockers": []},
        {"target_rule_obligations": {"ready": False, "missing": ["mteam_upload_and_seed"]}},
        [],
    )

    assert resume_state["artifacts"]["target_rule_obligations"] is False


def test_target_upload_summary_surfaces_downloaded_torrent_file_evidence_blockers() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "exists": False},
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
    }

    summary = ptcli_cli._target_upload_summary(payload, {"status": "ready", "blockers": [], "rule_obligation_review": {"ready": True, "blockers": []}})

    assert summary["ready"] is False
    assert "downloaded_torrent: target torrent file does not exist on disk." in summary["blockers"]


def test_target_upload_result_requires_downloaded_torrent_file_evidence_when_available() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "exists": False},
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is False


def test_uploaded_torrent_followup_requires_readable_metadata(tmp_path) -> None:
    uploaded_torrent = tmp_path / "MTEAM-999.torrent"
    uploaded_torrent.write_bytes(b"d4:infod")
    payload = ptcli_cli._with_downloaded_torrent_file_evidence(
        {
            "status": "uploaded",
            "downloaded_torrent": {"path": str(uploaded_torrent)},
            "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
        }
    )

    summary = ptcli_cli._target_upload_summary(payload, {"status": "ready", "blockers": [], "rule_obligation_review": {"ready": True, "blockers": []}})

    assert payload["downloaded_torrent"]["metadata_readable"] is False
    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True) is False
    assert "downloaded_torrent: target torrent metadata is not readable." in summary["blockers"]


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
            "visible_in_client": True, "verified_in_client": True,
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
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
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
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
        "uploaded_wait": {"complete": True, "matches": [{"hash": "a" * 40}]},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True, wait_uploaded_complete=True) is True


def test_target_upload_result_requires_uploaded_wait_match_evidence() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
        "uploaded_wait": {"complete": True, "matches": []},
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True, wait_uploaded_complete=True) is False


def test_target_upload_result_rejects_uploaded_wait_query_mismatch_without_verification() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
        "uploaded_wait": {
            "complete": True,
            "query": {"torrent_hash": "a" * 40, "content_path": "/downloads/Name"},
            "matches": [{"hash": "b" * 40, "content_path": "/downloads/Other"}],
        },
    }

    assert ptcli_cli._target_upload_result_ready(payload, execute=True, download_uploaded=True, inject_uploaded=True, wait_uploaded_complete=True) is False
    assert ptcli_cli._target_upload_result_blockers(payload) == [
        "uploaded_torrent_hash: inconsistent target torrent hashes (injected_torrent=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, uploaded_wait_query=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, uploaded_wait_match=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb)",
        "uploaded_wait: qBittorrent completion wait matched torrents, but not the requested hash.",
        "uploaded_wait: qBittorrent completion wait matched torrents, but not the requested content path.",
    ]


def test_wait_result_completed_rejects_query_path_mismatch_without_verification() -> None:
    wait_result = {
        "complete": True,
        "query": {"torrent_hash": "a" * 40, "content_path": "/downloads/Name"},
        "matches": [{"hash": "a" * 40, "content_path": "/downloads/Other"}],
    }

    assert ptcli_cli._wait_result_completed(wait_result) is False
    assert ptcli_cli._wait_completion_verification_blockers(wait_result) == ["qBittorrent completion wait matched torrents, but not the requested content path."]


def test_target_upload_summary_requires_uploaded_wait_match_evidence() -> None:
    payload = {
        "status": "uploaded",
        "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
        "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
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
            "injected_torrent": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True},
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


def test_target_upload_next_command_retries_after_completed_wait_when_hash_inconsistent() -> None:
    next_command = ptcli_cli._target_upload_next_command(
        {
            "ready": False,
            "downloaded": True,
            "injected": True,
            "seeding_verified": True,
            "hash_consistent": False,
            "duplicate_clean": True,
            "rule_obligations": {"ready": True},
            "uploaded_wait": {"complete": True, "matches": [{"hash": "a" * 40, "content_path": "/downloads/Example"}]},
            "blockers": ["uploaded_torrent_hash: inconsistent target torrent hashes"],
        },
        {
            "resume-uploaded-torrent": "python3 ptcli.py target-upload --uploaded-torrent-file /tmp/MTEAM-999.torrent",
            "target-upload-retry": "python3 ptcli.py target-upload --execute",
        },
    )

    assert next_command["stage"] == "target-upload-retry"


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
    flow_check = {
        "ready": True,
        "source_tracker": "U2",
        "source_torrent_id": "60635",
        "target_trackers": ["MTEAM"],
        "source_capability": {
            "tracker": "U2",
            "source_info_adapter": "generic_details_cookie",
            "source_download_adapter": "nexusphp_passkey",
            "credential_requirements": ["TRACKERS.U2.passkey", "data/cookies/U2.txt"],
        },
        "target_capabilities": [{"tracker": "MTEAM", "target_upload_adapter": "mteam_api", "credential_requirements": ["TRACKERS.MTEAM.api_key"]}],
        "credential_requirements": ["TRACKERS.U2.passkey", "data/cookies/U2.txt", "TRACKERS.MTEAM.api_key"],
        "checks": [],
    }
    stages = [
        {"stage": "flow-check", "ok": True, "result": flow_check},
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
    assert ptcli_cli._pipeline_flow_check_summary(stages) == {
        "ready": True,
        "source_tracker": "U2",
        "source_torrent_id": "60635",
        "target_trackers": ["MTEAM"],
        "source_capability": flow_check["source_capability"],
        "target_capabilities": flow_check["target_capabilities"],
        "credential_requirements": flow_check["credential_requirements"],
    }
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is True
    assert closure["blockers"] == []
    assert closure["source"]["ready"] is True
    assert closure["source"]["matched"] is True


def test_pipeline_closure_uses_downloaded_uploaded_torrent_hash_when_followup_incomplete() -> None:
    uploaded_hash = "b" * 40
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": False,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "torrent_hash": uploaded_hash, "exists": True, "size_bytes": 128, "metadata_readable": True},
                "injected_torrent": {"status": "blocked", "blockers": ["uploaded save path could not be inferred."]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)
    audit = ptcli_cli._pipeline_closure_audit(closure, evidence)

    assert closure["complete"] is False
    assert "target.injected" in closure["blockers"]
    assert closure["target"]["downloaded"] is True
    assert closure["target"]["uploaded_torrent_hash"] == uploaded_hash
    assert evidence["target"]["uploaded_torrent_hash"] == uploaded_hash
    assert "target.uploaded_torrent_hash" not in audit["missing"]


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
                "injected_torrent": {"hash": "c" * 40, "visible_in_client": True, "verified_in_client": True},
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


def test_pipeline_closure_rejects_match_when_source_wait_failed() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": False, "error": "qBittorrent task did not complete with matched source torrent evidence.", "result": {"complete": False, "matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {"stage": "source-content-verify", "ok": True, "result": {"verified": True, "expected_hash": "a" * 40, "matched_hashes": ["a" * 40]}},
        {"stage": "target-prepare", "ok": True, "result": {"rule_review": mteam_clean_rule_review()}},
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent"},
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is False
    assert closure["blockers"] == ["source.ready"]
    assert closure["source"]["matched"] is True
    assert closure["source"]["content_verified"] is True
    assert closure["source"]["ready"] is False


def test_pipeline_closure_preserves_torrent_file_evidence() -> None:
    source_torrent = {"path": "/tmp/U2-60635.torrent", "exists": True, "size_bytes": 8, "sha1": "c" * 40}
    uploaded_torrent = {"path": "/tmp/MTEAM-999.torrent", "exists": True, "size_bytes": 9, "sha1": "d" * 40}
    stages = [
        {"stage": "source-download", "ok": True, "result": source_torrent},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True}},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)

    assert closure["source"]["source_torrent"] == source_torrent
    assert closure["target"]["uploaded_torrent"] == uploaded_torrent
    assert evidence["source"]["source_torrent"] == source_torrent
    assert evidence["source"]["torrent_file_evidence"] is False
    assert evidence["target"]["uploaded_torrent"] == uploaded_torrent
    assert evidence["target"]["uploaded_torrent_file_evidence"] is False


def test_pipeline_evidence_marks_complete_torrent_file_evidence() -> None:
    source_torrent = {"path": "/tmp/U2-60635.torrent", "exists": True, "size_bytes": 8, "sha1": "c" * 40, "torrent_hash": "a" * 40, "metadata_readable": True}
    uploaded_torrent = {"path": "/tmp/MTEAM-999.torrent", "exists": True, "size_bytes": 9, "sha1": "d" * 40, "torrent_hash": "b" * 40, "metadata_readable": True}
    closure = {
        "source": {
            "ready": True,
            "downloaded": True,
            "source_torrent": source_torrent,
        },
        "target": {
            "prepared": True,
            "uploaded": True,
            "downloaded": True,
            "injected": True,
            "seeding": True,
            "uploaded_torrent": uploaded_torrent,
        },
    }

    evidence = ptcli_cli._pipeline_evidence(closure)

    assert evidence["source"]["torrent_file_evidence"] is True
    assert evidence["target"]["uploaded_torrent_file_evidence"] is True


def test_pipeline_closure_requires_existing_source_torrent_file_evidence() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "result": {"path": "/tmp/U2-60635.torrent", "exists": False}},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True}},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True}},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": False},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)

    assert closure["complete"] is False
    assert closure["blockers"] == ["target.injected"]
    assert closure["target"]["injected"] is False
    assert closure["target"]["injection_verified"] is False
    assert closure["target"]["injected_torrent_hash"] == "b" * 40
    assert closure["target"]["uploaded_torrent_hash"] == "b" * 40
    assert evidence["target"]["qbit_closure"]["injection"]["visible_in_client"] is True
    assert evidence["target"]["qbit_closure"]["injection"]["verified_in_client"] is False


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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True}},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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


def test_pipeline_closure_requires_target_materials_when_package_exists() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {
            "stage": "target-prepare",
            "ok": True,
            "result": {
                "package_dir": "/tmp/U2-60635-to-MTEAM",
                "rule_review": mteam_clean_rule_review(),
                "materials": {
                    "ready": False,
                    "metadata": {"imdb_id": None, "tmdb_id": None, "douban_id": None},
                    "assets": {"screenshots": {"ready": False}, "image_hosts": {"ready": False}},
                    "checks": {
                        "metadata": [{"name": "imdb_id", "ok": False}, {"name": "tmdb_id", "ok": False}, {"name": "douban_id", "ok": False}],
                        "assets": [{"name": "screenshots", "ok": False}, {"name": "image_hosts", "ok": False}],
                    },
                },
            },
        },
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "hash": "b" * 40},
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "query": {"torrent_hash": "b" * 40}, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")

    assert closure["complete"] is False
    assert "target.materials_ready" in closure["blockers"]
    assert closure["target"]["materials_ready"] is False
    assert closure["target"]["preparation_audit"]["missing"] == [
        "metadata.imdb_id",
        "metadata.tmdb_id",
        "metadata.douban_id",
        "assets.screenshots",
        "assets.image_hosts",
        "description.content",
        "payload.preflight",
    ]


def test_pipeline_closure_requires_target_preparation_when_materials_exist() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "skipped": True},
        {"stage": "inject-source", "ok": True, "skipped": True},
        {"stage": "wait-complete", "ok": True, "skipped": True},
        {"stage": "match", "ok": True, "result": {"matches": [{"content_path": "/downloads/Name", "hash": "a" * 40}]}},
        {
            "stage": "target-prepare",
            "ok": True,
            "result": {
                "rule_review": mteam_clean_rule_review(),
                "materials": {
                    "ready": True,
                    "metadata": {"imdb_id": "1234567", "tmdb_id": 999, "douban_id": "1291546"},
                    "assets": {"screenshots": {"ready": True}, "image_hosts": {"ready": True}},
                    "checks": {
                        "metadata": [{"name": "imdb_id", "ok": True}, {"name": "tmdb_id", "ok": True}, {"name": "douban_id", "ok": True}],
                        "assets": [{"name": "screenshots", "ok": True}, {"name": "image_hosts", "ok": True}],
                    },
                },
            },
        },
        {
            "stage": "target-upload",
            "ok": True,
            "result": {
                "status": "uploaded",
                "fresh_duplicate_check": {"searched": True, "count": 0, "dupes": []},
                "uploaded_torrent_hash": "b" * 40,
                "downloaded_torrent": {"path": "/tmp/MTEAM-999.torrent", "hash": "b" * 40},
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
                "uploaded_wait": {"complete": True, "query": {"torrent_hash": "b" * 40}, "matches": [{"hash": "b" * 40}]},
            },
        },
    ]

    closure = ptcli_cli._pipeline_closure(stages, "/downloads/Name", "a" * 40, "/tmp/target.torrent")
    evidence = ptcli_cli._pipeline_evidence(closure)
    audit = ptcli_cli._pipeline_closure_audit(closure, evidence)

    assert closure["complete"] is True
    assert "target.materials_ready" not in closure["blockers"]
    assert closure["target"]["materials_ready"] is True
    assert closure["target"]["preparation_audit"]["missing"] == ["description.content", "payload.preflight"]
    assert audit["ready"] is False
    assert "target.preparation_ready" in audit["missing"]


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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True}},
        {"stage": "wait-complete", "ok": True, "result": {"complete": True, "query": {"torrent_hash": "b" * 40}, "matches": [{"hash": "b" * 40}]}},
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
                "injected_torrent": {"hash": "c" * 40, "visible_in_client": True, "verified_in_client": True},
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
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True}},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
                "visible_in_client": True,
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
                "visible_in_client": True,
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
    assert evidence["source"]["qbit_closure"]["injection"]["visible_in_client"] is True
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
    assert evidence["target"]["qbit_closure"]["injection"]["visible_in_client"] is True
    assert evidence["target"]["qbit_closure"]["wait"]["complete"] is True
    assert evidence["target"]["qbit_closure"]["wait"]["query"]["torrent_hash"] == "b" * 40


def test_pipeline_closure_audit_requires_injection_visibility() -> None:
    closure = {
        "source": {
            "ready": True,
            "downloaded": True,
            "injected": True,
            "injection_verified": True,
            "injected_torrent": {"hash": "a" * 40, "verified_in_client": False, "visible_in_client": False},
            "injected_torrent_hash": "a" * 40,
            "hash_consistent": True,
            "torrent_hash": "a" * 40,
            "source_wait": {"complete": True, "matches": [{"hash": "a" * 40}]},
        },
        "target": {
            "prepared": True,
            "uploaded": True,
            "downloaded": True,
            "injected": True,
            "injection_verified": True,
            "injected_torrent": {"hash": "b" * 40, "verified_in_client": False, "visible_in_client": False},
            "seeding": True,
            "hash_consistent": True,
            "duplicate_clean": True,
            "rule_obligations": {"ready": True},
            "uploaded_torrent_hash": "b" * 40,
            "injected_torrent_hash": "b" * 40,
            "uploaded_wait": {"complete": True, "matches": [{"hash": "b" * 40}]},
        },
    }
    evidence = ptcli_cli._pipeline_evidence(closure)

    audit = ptcli_cli._pipeline_closure_audit(closure, evidence)

    assert audit["ready"] is False
    assert "source.injection_visible_in_client" in audit["missing"]
    assert "target.injection_visible_in_client" in audit["missing"]


def test_pipeline_evidence_reports_resume_sources() -> None:
    stages = [
        {"stage": "source-download", "ok": True, "result": {"path": "/tmp/U2-60635.torrent", "reused": True}},
        {"stage": "inject-source", "ok": True, "result": {"hash": "a" * 40, "visible_in_client": True, "verified_in_client": True}},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
    assert evidence["target"]["mode"] == "resumed_uploaded_torrent"
    assert summary["resume"]["used"] is True
    assert summary["target"]["mode"] == "resumed_uploaded_torrent"


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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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
                "injected_torrent": {"hash": "b" * 40, "visible_in_client": True, "verified_in_client": True},
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


def test_source_info_from_tuple_extracts_external_ids_from_description() -> None:
    description = """
    IMDb: https://www.imdb.com/title/tt7654321/
    TMDb ID: 98765
    豆瓣：https://movie.douban.com/subject/3541415/
    """

    info = source_info_from_tuple("CHD", "2468", (None, None, "Release Name", "b" * 40, description), {})

    assert info.imdb_id == 7654321
    assert info.tmdb_id == 98765
    assert info.douban_id == "3541415"
    assert info.douban_url == "https://movie.douban.com/subject/3541415/"


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
    assert payload["source_capability"] == {
        "tracker": "U2",
        "source_info_adapter": "generic_details_cookie",
        "source_download_adapter": "nexusphp_passkey",
        "credential_requirements": ["TRACKERS.U2.passkey", "data/cookies/U2.txt"],
    }
    assert payload["target_capabilities"] == [
        {
            "tracker": "MTEAM",
            "target_upload_adapter": "mteam_api",
            "credential_requirements": ["TRACKERS.MTEAM.api_key"],
        }
    ]
    assert payload["credential_requirements"] == ["TRACKERS.U2.passkey", "data/cookies/U2.txt", "TRACKERS.MTEAM.api_key"]


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


def test_flow_check_ready_for_hds_to_mteam_uses_cookie_only(tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "HDS.txt").write_text("uid=1;", encoding="utf-8")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {
            "HDS": {},
            "MTEAM": {"api_key": "mteam-api"},
        },
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }

    payload = build_flow_check(config, "HDS", "123", "MTEAM", "default", base_dir=str(tmp_path))

    assert payload["ready"] is True
    assert not any(check["name"] == "HDS.passkey" for check in payload["checks"])
    assert any(check["name"] == "HDS.cookie" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "reference_flow" and check["ok"] is True for check in payload["checks"])
    assert payload["source_capability"]["source_download_adapter"] == "cookie_download"
    assert payload["credential_requirements"] == ["data/cookies/HDS.txt", "TRACKERS.MTEAM.api_key"]


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
    package = write_material_ready_mteam_package(source_info, tmp_path, content_path=str(content_path), output_dir=str(tmp_path / "target"))

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
    assert any(check["name"] == "target_materials" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "wait_uploaded_complete" and check["ok"] is True for check in payload["checks"])
    assert any(check["name"] == "rule_obligations" and check["ok"] is True for check in payload["checks"])
    args = build_parser().parse_args(
        [
            "doctor",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--path",
            str(content_path),
            "--package-dir",
            package["package_dir"],
            "--target-torrent-file",
            target_torrent,
            "--accept-rules",
            "--target-execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            str(content_path),
            "--wait-uploaded-complete",
            "--write-summary",
            "--json",
        ]
    )
    summary = ptcli_cli._doctor_summary_payload(payload, args, str(tmp_path / "ptcli-doctor-summary.json"))
    audit = summary["target_preparation_audit"]
    assert audit["ready"] is True
    assert audit["description"]["has_ptgen_description"] is True
    assert audit["description"]["has_external_ids"] is True
    assert audit["description"]["external_links"]["imdb"] == "https://www.imdb.com/title/tt1234567"
    assert audit["description"]["external_links"]["tmdb"] == "https://www.themoviedb.org/movie/999"
    assert audit["description"]["external_links"]["douban"] == "https://movie.douban.com/subject/1291546/"
    assert audit["description"]["has_mediainfo_or_bdinfo"] is True
    assert audit["description"]["has_screenshot_bbcode"] is True
    assert audit["description"]["bbcode_image_count"] == 1
    assert summary["artifacts"]["target_preparation_ready"] is True
    assert summary["artifacts"]["target_materials_ready"] is True
    assert summary["artifacts"]["target_preparation_missing"] == []
    material_diagnostics = summary["material_diagnostics"]
    assert material_diagnostics["present"] is True
    assert material_diagnostics["ready_for_mteam_upload"] is True
    assert material_diagnostics["critical_path"]["ready"] is True
    assert material_diagnostics["critical_path"]["next_step"] is None
    gates = summary["artifacts"]["target_preflight_gates"]
    assert gates["present"] is True
    assert gates["status"] == "ready"
    assert gates["ready"] is True
    assert gates["target_preparation_ready"] is True
    assert gates["materials_ready"] is True
    assert gates["metadata_ready"] is True
    assert gates["assets_ready"] is True
    assert gates["description_ready"] is True
    assert gates["payload_ready"] is True
    assert gates["payload_checks_ready"] is True
    assert gates["description_checks_ready"] is True
    assert gates["materials_ready_required"] is True
    assert gates["torrent_file"]["mteam_safe"] is True
    assert gates["torrent_file"]["metadata_readable"] is True
    assert gates["torrent_file"]["source_flag"] == "MTEAM"
    assert summary["resume_state"]["artifacts"]["target_preparation_ready"] is True
    assert summary["resume_state"]["artifacts"]["target_preflight_gates_ready"] is True
    assert summary["resume_state"]["artifacts"]["target_preflight_materials_ready"] is True
    assert summary["resume_state"]["artifacts"]["target_preflight_description_ready"] is True
    assert summary["resume_state"]["artifacts"]["target_preflight_payload_ready"] is True
    assert summary["resume_state"]["artifacts"]["target_preflight_torrent_safe"] is True


def test_doctor_preflight_gates_expose_blocked_materials(tmp_path) -> None:
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
    material_dir = tmp_path / "materials"
    material_dir.mkdir()
    mediainfo = material_dir / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Name.mkv\n", encoding="utf-8")
    screenshot = material_dir / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_host_file = material_dir / "image-host-uploads.json"
    image_host_file.write_text(
        json.dumps({"items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}]}),
        encoding="utf-8",
    )
    package = write_mteam_prepare_package(
        source_info,
        ["MTEAM"],
        mteam_ready_stages(),
        str(content_path),
        str(tmp_path / "target"),
        accept_rules=True,
        material_files={"mediainfo_file": str(mediainfo), "screenshot_files": [str(screenshot)], "image_host_file": str(image_host_file)},
    )

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
    args = build_parser().parse_args(
        [
            "doctor",
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--path",
            str(content_path),
            "--package-dir",
            package["package_dir"],
            "--target-torrent-file",
            target_torrent,
            "--accept-rules",
            "--target-execute",
            "--confirm-upload",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            str(content_path),
            "--wait-uploaded-complete",
            "--write-summary",
            "--json",
        ]
    )

    summary = ptcli_cli._doctor_summary_payload(payload, args, str(tmp_path / "ptcli-doctor-summary.json"))

    assert payload["ready"] is False
    assert payload["live_safe_to_attempt"] is False
    gates = summary["artifacts"]["target_preflight_gates"]
    assert gates["present"] is True
    assert gates["status"] == "blocked"
    assert gates["ready"] is False
    assert gates["target_preparation_ready"] is False
    assert gates["materials_ready"] is False
    assert gates["metadata_ready"] is False
    assert gates["assets_ready"] is True
    assert gates["description_ready"] is False
    assert gates["payload_ready"] is False
    assert gates["payload_checks_ready"] is True
    assert gates["description_checks_ready"] is False
    assert gates["materials_ready_required"] is True
    assert gates["torrent_file"]["mteam_safe"] is True
    assert gates["torrent_file"]["metadata_readable"] is True
    assert gates["torrent_file"]["source_flag"] == "MTEAM"
    assert summary["material_diagnostics"]["present"] is True
    assert summary["material_diagnostics"]["ready_for_mteam_upload"] is False
    assert summary["material_diagnostics"]["critical_path"]["ready"] is False
    assert summary["material_diagnostics"]["critical_path"]["next_step"] == "metadata"
    assert "metadata.ptgen_description" in summary["material_diagnostics"]["critical_path"]["missing"]
    assert "description.content" in summary["material_diagnostics"]["critical_path"]["missing"]
    commands = {command["stage"]: command for command in summary["recommended_commands"]}
    assert "resume-target-package" in commands
    assert summary["resume_state"]["next_stage"] == "resume-target-package"
    assert summary["resume_state"]["next_command"] == commands["resume-target-package"]["command"]
    assert "--prepare-target" in commands["resume-target-package"]["argv"]
    assert "--fetch-ptgen" in commands["resume-target-package"]["argv"]
    assert "--target-output-dir" in commands["resume-target-package"]["argv"]
    assert "--mediainfo-file" in commands["resume-target-package"]["argv"]
    assert str(mediainfo) in commands["resume-target-package"]["argv"]
    assert "--screenshot-file" in commands["resume-target-package"]["argv"]
    assert str(screenshot) in commands["resume-target-package"]["argv"]
    assert "--image-host-file" in commands["resume-target-package"]["argv"]
    assert str(image_host_file) in commands["resume-target-package"]["argv"]
    assert str(content_path) in commands["resume-target-package"]["argv"]
    assert summary["resume_state"]["artifacts"]["target_preflight_gates_ready"] is False
    assert summary["resume_state"]["artifacts"]["target_preflight_materials_ready"] is False
    assert summary["resume_state"]["artifacts"]["target_preflight_description_ready"] is False
    assert summary["resume_state"]["artifacts"]["target_preflight_payload_ready"] is False
    assert summary["resume_state"]["artifacts"]["target_preflight_torrent_safe"] is True


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
    package = write_material_ready_mteam_package(source_info, tmp_path, content_path=str(content_path), output_dir=str(tmp_path / "target"))

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
    package = write_material_ready_mteam_package(source_info, tmp_path, content_path=str(content_path), output_dir=str(tmp_path / "target"))

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


def test_doctor_resume_commands_use_effective_uploaded_save_path(tmp_path) -> None:
    content_path = tmp_path / "downloads" / "Name"
    content_path.mkdir(parents=True)
    package_dir = tmp_path / "target" / "U2-60635-to-MTEAM"
    package_dir.mkdir(parents=True)
    args = argparse.Namespace(
        source_tracker="U2",
        source_id="60635",
        target_trackers="MTEAM",
        config=None,
        base_dir=None,
        content_path=None,
        source_torrent_file=None,
        package_dir=str(package_dir),
        target_torrent_file=None,
        uploaded_torrent_id="999",
        uploaded_torrent_file=None,
        uploaded_save_path=None,
        uploaded_qbit_category="MTEAM",
        uploaded_qbit_tags="retorrent",
        uploaded_paused=False,
        uploaded_wait_timeout=42.0,
        uploaded_wait_interval=3.0,
        client="default",
        accept_rules=True,
        target_execute=True,
        confirm_upload=True,
        download_uploaded_torrent=True,
        inject_uploaded_torrent=True,
        wait_uploaded_complete=True,
        write_summary=True,
        summary_output_dir=None,
        connect_qbit=False,
        probe_source=False,
        probe_target=False,
        check_runtime=False,
    )
    artifacts = {"effective_uploaded_save_path": ptcli_cli._path_artifact(str(content_path))}

    commands = {command["stage"]: command for command in ptcli_cli._doctor_recommended_commands({"live_safe_to_attempt": True}, args, artifacts)}
    resume_command = commands["resume-uploaded-torrent-download"]["command"]
    resume_argv = commands["resume-uploaded-torrent-download"]["argv"]

    assert f"--uploaded-save-path {shlex.quote(str(content_path))}" in resume_command
    assert str(content_path) in resume_argv


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


def test_doctor_surfaces_material_gate_checks(tmp_path) -> None:
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
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    stages = [
        {"stage": "rule-check", "ok": True, "result": build_rule_check("U2", ["MTEAM"], accept_rules=True)},
        {"stage": "match", "ok": True, "result": {"count": 1, "matches": [{"content_path": str(content_path), "hash": "a" * 40}]}},
        {"stage": "target-dupe-check", "ok": True, "result": {"searched": True, "count": 0, "dupes": []}},
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

    checks = {check["name"]: check for check in payload["checks"]}

    assert payload["ready"] is False
    assert payload["live_safe_to_attempt"] is False
    assert checks["target_materials"]["ok"] is False
    assert checks["target_materials_metadata_tmdb"]["ok"] is False
    assert checks["target_materials_metadata_douban"]["ok"] is False
    assert checks["target_materials_assets_mediainfo_or_bdinfo"]["ok"] is False
    assert checks["target_materials_assets_screenshots"]["ok"] is False
    assert checks["target_materials_assets_image_host_uploads"]["ok"] is False
    assert any("Fix target_materials" in action for action in payload["next_actions"])


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
    package = write_material_ready_mteam_package(source_info, tmp_path, content_path=str(content_path), output_dir=str(tmp_path / "target"))

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


def test_doctor_resume_files_still_require_target_materials(tmp_path) -> None:
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

    checks = {check["name"]: check for check in payload["checks"]}

    assert payload["ready"] is False
    assert payload["live_safe_to_attempt"] is False
    assert checks["uploaded_torrent_file"]["ok"] is True
    assert checks["download_uploaded_torrent"]["ok"] is True
    assert checks["target_materials"]["ok"] is False
    assert checks["target_materials_metadata_tmdb"]["ok"] is False
    assert checks["target_materials_assets_image_host_uploads"]["ok"] is False


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
    assert any(item["module"] == "src.trackers.COMMON" and item["available"] is True for item in runtime_check["internal_imports"])
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


def test_doctor_runtime_check_blocks_failed_internal_import(monkeypatch, tmp_path) -> None:
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    original_import_module = ptcli_doctor.importlib.import_module

    def fake_import_module(module: str):
        if module == "src.trackers.COMMON":
            raise ImportError("missing focused ptgen dependency")
        return original_import_module(module)

    monkeypatch.setattr(ptcli_doctor.importlib, "import_module", fake_import_module)

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
    ptgen_import = next(item for item in runtime_check["internal_imports"] if item["name"] == "ptgen_adapter")
    assert runtime_check["ok"] is False
    assert "ptgen_adapter" in runtime_check["message"]
    assert ptgen_import["available"] is False
    assert "missing focused ptgen dependency" in ptgen_import["error"]


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
    package = write_material_ready_mteam_package(source_info, tmp_path, content_path=str(content_path), output_dir=str(tmp_path / "target"))
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
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}, "seedbox": {"torrent_client": "qbit"}},
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
    package = write_material_ready_mteam_package(source_info, tmp_path, content_path=str(content_path), output_dir=str(tmp_path / "target"))
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    code = main(
        [
            "doctor",
            "--config",
            str(tmp_path / "custom-config.py"),
            "--from",
            "U2",
            "--source-id",
            "60635",
            "--to",
            "MTEAM",
            "--client",
            "seedbox",
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
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--uploaded-paused",
            "--wait-uploaded-complete",
            "--uploaded-wait-timeout",
            "42",
            "--uploaded-wait-interval",
            "3",
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
    command_argv = {command["stage"]: command["argv"] for command in summary_payload["recommended_commands"]}
    assert summary_payload["schema_version"] == 1
    assert summary_payload["kind"] == "ptcli.doctor.live_readiness"
    assert summary_payload["automation_handoff"]["json"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(tmp_path / "summary" / "ptcli-doctor-summary.json"), "--json"]
    assert summary_payload["automation_handoff"]["print_next_argv"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(tmp_path / "summary" / "ptcli-doctor-summary.json"), "--print-next-argv"]
    assert summary_payload["automation_handoff"]["run_next_command"]["command"] == shlex.join(["python3", "ptcli.py", "summary-check", "--summary-file", str(tmp_path / "summary" / "ptcli-doctor-summary.json"), "--run-next-command"])
    assert summary_payload["mode"] == "live_upload"
    assert summary_payload["target_mode"] == "live_upload"
    assert summary_payload["artifacts"]["content_path"]["exists"] is True
    assert summary_payload["artifacts"]["package_dir"]["is_dir"] is True
    assert summary_payload["artifacts"]["target_torrent_file"]["is_file"] is True
    assert summary_payload["artifacts"]["flow_check_ready"] is True
    assert summary_payload["artifacts"]["rule_check_ready"] is True
    assert summary_payload["artifacts"]["rules_acknowledged"] is True
    assert summary_payload["artifacts"]["live_upload_confirmation"] is True
    assert summary_payload["artifacts"]["rule_obligations"]["ready"] is True
    assert summary_payload["artifacts"]["target_rule_obligations"]["ready"] is True
    assert summary_payload["artifacts"]["target_package_preflight_ready"] is True
    gates = summary_payload["artifacts"]["target_preflight_gates"]
    assert gates["present"] is True
    assert gates["status"] == "ready"
    assert gates["ready"] is True
    assert gates["target_preparation_ready"] is True
    assert gates["materials_ready"] is True
    assert gates["metadata_ready"] is True
    assert gates["assets_ready"] is True
    assert gates["description_ready"] is True
    assert gates["payload_ready"] is True
    assert gates["payload_checks_ready"] is True
    assert gates["description_checks_ready"] is True
    assert gates["materials_ready_required"] is True
    assert gates["torrent_file"]["mteam_safe"] is True
    assert gates["torrent_file"]["metadata_readable"] is True
    assert gates["torrent_file"]["source_flag"] == "MTEAM"
    assert summary_payload["artifacts"]["download_uploaded_torrent"] is True
    assert summary_payload["artifacts"]["inject_uploaded_torrent"] is True
    assert summary_payload["artifacts"]["wait_uploaded_complete"] is True
    assert summary_payload["compliance"]["ready"] is True
    assert summary_payload["compliance"]["site_specific_rules_encoded"] is False
    assert summary_payload["failed_check_names"] == []
    assert "--connect-qbit" in commands["doctor-live-probes"]
    assert "--probe-source" in commands["doctor-live-probes"]
    assert "--probe-target" in commands["doctor-live-probes"]
    assert "--target-execute --confirm-upload" in commands["pipeline-live"]
    assert str(content_path) in commands["pipeline-live"]
    assert str(target_torrent) in commands["pipeline-live"]
    assert "--uploaded-qbit-category MTEAM" in commands["pipeline-live"]
    assert "--uploaded-qbit-tags retorrent" in commands["pipeline-live"]
    assert "--uploaded-paused" in commands["pipeline-live"]
    assert "--uploaded-wait-timeout 42" in commands["pipeline-live"]
    assert "--uploaded-wait-interval 3" in commands["pipeline-live"]
    assert "--uploaded-wait-timeout 42" in commands["doctor-retry"]
    assert "--uploaded-wait-interval 3" in commands["doctor-retry"]
    assert command_argv["doctor-retry"][:3] == ["python3", "ptcli.py", "doctor"]
    assert command_argv["doctor-live-probes"][:3] == ["python3", "ptcli.py", "doctor"]
    assert command_argv["pipeline-live"][:3] == ["python3", "ptcli.py", "pipeline"]
    assert str(tmp_path / "custom-config.py") in command_argv["pipeline-live"]
    assert "seedbox" in command_argv["pipeline-live"]
    assert str(tmp_path) in command_argv["pipeline-live"]
    assert str(tmp_path / "summary") in command_argv["pipeline-live"]
    assert str(content_path) in command_argv["pipeline-live"]
    assert str(target_torrent) in command_argv["pipeline-live"]
    assert "MTEAM" in command_argv["pipeline-live"]
    assert "retorrent" in command_argv["pipeline-live"]
    assert "--uploaded-paused" in command_argv["pipeline-live"]
    assert summary_payload["resume_state"]["ready"] is True
    assert summary_payload["resume_state"]["live_safe_to_attempt"] is True
    assert summary_payload["resume_state"]["resume_available"] is True
    assert summary_payload["resume_state"]["next_stage"] == "pipeline-live"
    assert summary_payload["resume_state"]["next_command"] == commands["pipeline-live"]
    assert summary_payload["resume_state"]["next_command_argv"] == command_argv["pipeline-live"]
    assert summary_payload["resume_state"]["artifacts"]["content_path"] is True
    assert summary_payload["resume_state"]["artifacts"]["package_dir"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_torrent_file"] is True
    assert summary_payload["resume_state"]["artifacts"]["flow_check_ready"] is True
    assert summary_payload["resume_state"]["artifacts"]["rule_check_ready"] is True
    assert summary_payload["resume_state"]["artifacts"]["rules_acknowledged"] is True
    assert summary_payload["resume_state"]["artifacts"]["live_upload_confirmation"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_rule_obligations"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_package_preflight_ready"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_preflight_gates_ready"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_preflight_materials_ready"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_preflight_description_ready"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_preflight_payload_ready"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_preflight_torrent_safe"] is True
    assert summary_payload["resume_state"]["artifacts"]["download_uploaded_torrent"] is True
    assert summary_payload["resume_state"]["artifacts"]["inject_uploaded_torrent"] is True
    assert summary_payload["resume_state"]["artifacts"]["wait_uploaded_complete"] is True


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
    package = write_material_ready_mteam_package(source_info, tmp_path, content_path=str(content_path), output_dir=str(tmp_path / "target"))
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    code = main(
        [
            "doctor",
            "--config",
            str(tmp_path / "custom-config.py"),
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
            "--uploaded-wait-timeout",
            "42",
            "--uploaded-wait-interval",
            "3",
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
    command_argv = {command["stage"]: command["argv"] for command in summary_payload["recommended_commands"]}
    assert summary_payload["inputs"]["uploaded_torrent_id"] == "999"
    assert summary_payload["mode"] == "resumed_uploaded_id"
    assert summary_payload["target_mode"] == "resumed_uploaded_id"
    assert summary_payload["artifacts"]["uploaded_torrent_id"] == "999"
    assert summary_payload["artifacts"]["download_uploaded_torrent"] is True
    assert summary_payload["artifacts"]["inject_uploaded_torrent"] is True
    assert summary_payload["artifacts"]["wait_uploaded_complete"] is True
    assert "--connect-qbit" in commands["doctor-live-probes"]
    assert "--probe-source" in commands["doctor-live-probes"]
    assert "--probe-target" in commands["doctor-live-probes"]
    assert "pipeline-live" not in commands
    assert "--uploaded-torrent-id 999" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-save-path" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-category MTEAM" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-tags retorrent" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-paused" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-wait-timeout 42" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-wait-interval 3" in commands["resume-uploaded-torrent-download"]
    assert summary_payload["resume_state"]["live_safe_to_attempt"] is True
    assert summary_payload["resume_state"]["next_stage"] == "resume-uploaded-torrent-download"
    assert summary_payload["resume_state"]["next_command"] == commands["resume-uploaded-torrent-download"]
    assert summary_payload["resume_state"]["next_command_argv"] == command_argv["resume-uploaded-torrent-download"]
    assert command_argv["resume-uploaded-torrent-download"][:3] == ["python3", "ptcli.py", "target-upload"]
    assert str(tmp_path / "custom-config.py") in command_argv["resume-uploaded-torrent-download"]
    assert str(tmp_path / "summary") in command_argv["resume-uploaded-torrent-download"]
    assert "999" in summary_payload["resume_state"]["next_command_argv"]
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_id"] is True
    assert summary_payload["resume_state"]["artifacts"]["download_uploaded_torrent"] is True
    assert summary_payload["resume_state"]["artifacts"]["inject_uploaded_torrent"] is True
    assert summary_payload["resume_state"]["artifacts"]["wait_uploaded_complete"] is True


def test_doctor_uploaded_torrent_file_resume_is_live_safe(monkeypatch, tmp_path, capsys) -> None:
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
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")
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
    package = write_material_ready_mteam_package(source_info, tmp_path, content_path=str(content_path), output_dir=str(tmp_path / "target"))
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    code = main(
        [
            "doctor",
            "--config",
            str(tmp_path / "custom-config.py"),
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
            "--uploaded-torrent-file",
            str(uploaded_torrent),
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            str(content_path),
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
            "--uploaded-paused",
            "--wait-uploaded-complete",
            "--uploaded-wait-timeout",
            "42",
            "--uploaded-wait-interval",
            "3",
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
    command_argv = {command["stage"]: command["argv"] for command in summary_payload["recommended_commands"]}
    assert summary_payload["inputs"]["uploaded_torrent_file"] == str(uploaded_torrent)
    assert summary_payload["mode"] == "resumed_uploaded_torrent"
    assert summary_payload["target_mode"] == "resumed_uploaded_torrent"
    assert summary_payload["artifacts"]["uploaded_torrent_file"]["path"] == str(uploaded_torrent)
    assert summary_payload["artifacts"]["uploaded_torrent_file"]["is_file"] is True
    assert summary_payload["artifacts"]["download_uploaded_torrent"] is True
    assert summary_payload["artifacts"]["inject_uploaded_torrent"] is True
    assert summary_payload["artifacts"]["wait_uploaded_complete"] is True
    assert "pipeline-live" not in commands
    assert "--uploaded-torrent-file" in commands["resume-uploaded-torrent"]
    assert str(uploaded_torrent) in commands["resume-uploaded-torrent"]
    assert "--uploaded-save-path" in commands["resume-uploaded-torrent"]
    assert "--uploaded-qbit-category MTEAM" in commands["resume-uploaded-torrent"]
    assert "--uploaded-qbit-tags retorrent" in commands["resume-uploaded-torrent"]
    assert "--uploaded-paused" in commands["resume-uploaded-torrent"]
    assert "--uploaded-wait-timeout 42" in commands["resume-uploaded-torrent"]
    assert "--uploaded-wait-interval 3" in commands["resume-uploaded-torrent"]
    assert summary_payload["resume_state"]["live_safe_to_attempt"] is True
    assert summary_payload["resume_state"]["next_stage"] == "resume-uploaded-torrent"
    assert summary_payload["resume_state"]["next_command"] == commands["resume-uploaded-torrent"]
    assert summary_payload["resume_state"]["next_command_argv"] == command_argv["resume-uploaded-torrent"]
    assert command_argv["resume-uploaded-torrent"][:3] == ["python3", "ptcli.py", "target-upload"]
    assert str(tmp_path / "custom-config.py") in command_argv["resume-uploaded-torrent"]
    assert str(tmp_path / "summary") in command_argv["resume-uploaded-torrent"]
    assert str(uploaded_torrent) in command_argv["resume-uploaded-torrent"]
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_file"] is True
    assert summary_payload["resume_state"]["artifacts"]["download_uploaded_torrent"] is True
    assert summary_payload["resume_state"]["artifacts"]["inject_uploaded_torrent"] is True
    assert summary_payload["resume_state"]["artifacts"]["wait_uploaded_complete"] is True


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
    result_payload = json.loads(out)
    assert result_payload["summary_file"] == str(summary_path)
    assert result_payload["automation_handoff"]["json"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_path), "--json"]
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["kind"] == "ptcli.doctor.live_readiness"
    assert payload["summary_file"] == str(summary_path)
    assert payload["mode"] == "readiness_check"
    assert payload["target_mode"] == "readiness_check"
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
    source_torrent = tmp_path / "source-out" / "U2-60635.torrent"
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        return source_torrent

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
    assert download_stage["result"]["metadata_readable"] is True
    assert download_stage["result"]["torrent_hash"] == source_hash
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
    source_torrent = tmp_path / "source-out" / "U2-60635.torrent"
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        return source_torrent

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
    source_torrent = tmp_path / "source-out" / "U2-60635.torrent"
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        return source_torrent

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name)
        return {
            "client": "qbittorrent",
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
            "hash": source_hash,
            "visible_in_client": True, "verified_in_client": True,
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
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", source_hash, "desc"), {})

    async def fake_download_source_torrent(*_args, **_kwargs):
        raise AssertionError("source download must not run when --source-torrent-file is provided")

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, category, tags, paused)
        return {"client": "qbittorrent", "torrent_path": torrent_path, "save_path": save_path, "hash": source_hash, "visible_in_client": True, "verified_in_client": True}

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
    assert download_stage["result"]["metadata_readable"] is True
    assert download_stage["result"]["torrent_hash"] == source_hash
    assert len(download_stage["result"]["sha1"]) == 40
    assert inject_stage["ok"] is True
    assert inject_stage["result"]["torrent_path"] == str(source_torrent)
    assert payload["requested_actions"]["source_torrent_file"] is True
    assert payload["requested_actions"]["download_source"] is False


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
    source_torrent = tmp_path / "source-out" / "U2-60635.torrent"
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        return source_torrent

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, torrent_path, save_path, category, tags, paused)
        return {"client": "qbittorrent", "hash": source_hash, "verified_in_client": False}

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
    source_torrent = tmp_path / "source-out" / "U2-60635.torrent"
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        return source_torrent

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, torrent_path, category, tags, paused)
        return {"client": "qbittorrent", "save_path": save_path, "visible_in_client": True, "verified_in_client": True}

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
    source_torrent = tmp_path / "source-out" / "U2-60635.torrent"
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        return source_torrent

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, torrent_path, category, tags, paused)
        return {"client": "qbittorrent", "save_path": save_path, "hash": source_hash, "visible_in_client": True, "verified_in_client": True}

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
    source_torrent = tmp_path / "source-out" / "U2-60635.torrent"
    metadata_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")
    injected_hash = metadata_hash

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", metadata_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        return source_torrent

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, torrent_path, category, tags, paused)
        return {"client": "qbittorrent", "save_path": save_path, "hash": injected_hash, "visible_in_client": True, "verified_in_client": True}

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
        _ = (tracker, source_id, output_dir, base_dir)
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
        _ = (tracker, source_id, output_dir, base_dir)
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
        _ = (tracker, source_id, output_dir, base_dir)
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
        _ = (tracker, source_id, output_dir, base_dir)
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
        return {"client": client_name, "hash": wrong_hash, "save_path": save_path, "visible_in_client": True, "verified_in_client": True}

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
    source_torrent = tmp_path / "source-out" / "U2-60635.torrent"
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        return source_torrent

    async def fake_inject_source_with_config(config, client_name, torrent_path, save_path, category, tags, paused):
        _ = (config, client_name, torrent_path, category, tags, paused)
        return {"client": "qbittorrent", "save_path": save_path, "hash": source_hash, "visible_in_client": True, "verified_in_client": True}

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, content_path, torrent_hash, timeout, interval)
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": "/downloads/Name", "hash": source_hash}]}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": source_hash}]}

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
    mediainfo = tmp_path / "MEDIAINFO.txt"
    mediainfo.write_text("General\nComplete name : Name.mkv\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_hosts = tmp_path / "image-host.json"
    image_hosts.write_text(json.dumps({"items": [{"raw_url": "https://img.example/raw.png"}]}), encoding="utf-8")
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
            "--mediainfo-file",
            str(mediainfo),
            "--screenshot-file",
            str(screenshot),
            "--image-host-file",
            str(image_hosts),
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
    assert target_stage["result"]["materials"]["assets"]["mediainfo"]["ready"] is True
    assert target_stage["result"]["materials"]["assets"]["screenshots"]["count"] == 1
    assert target_stage["result"]["materials"]["assets"]["image_hosts"]["count"] == 1
    assert payload["evidence"]["target"]["materials"]["assets"]["mediainfo"]["ready"] is True
    assert payload["evidence"]["target"]["materials"]["assets"]["screenshots"]["count"] == 1
    assert payload["evidence"]["target"]["materials"]["assets"]["image_hosts"]["count"] == 1
    assert payload["evidence"]["target"]["materials_ready"] is False
    assert payload["artifacts"]["target_materials_ready"] is False
    assert payload["resume_state"]["artifacts"]["target_materials_ready"] is False
    assert any("rules_acknowledged" in blocker for blocker in target_stage["result"]["blockers"])
    assert any("duplicate_check" in blocker for blocker in target_stage["result"]["blockers"])


@pytest.mark.asyncio
async def test_pipeline_generate_mediainfo_material_before_prepare_target(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {"imdb_id": 1234567, "tmdb_id": 999, "douban_id": "1291546"})

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_generate_mediainfo_material(_content_path, output_dir):
        output_path = Path(output_dir) / "MI_FULL_00.txt"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("General\nComplete name : Name.mkv\n", encoding="utf-8")
        return {"status": "generated", "mediainfo_file": str(output_path), "blockers": []}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "generate_mediainfo_material", fake_generate_mediainfo_material)
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
            "--path",
            "/downloads/Name",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--generate-mediainfo",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    material_stage = next(stage for stage in payload["stages"] if stage["stage"] == "materials-mediainfo")
    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert material_stage["ok"] is True
    assert material_stage["result"]["mediainfo_file"].endswith("MI_FULL_00.txt")
    assert target_stage["result"]["materials"]["assets"]["mediainfo"]["ready"] is True
    assert target_stage["result"]["materials"]["assets"]["mediainfo"]["path"].endswith("MI_FULL_00.txt")


@pytest.mark.asyncio
async def test_pipeline_generate_bdinfo_material_before_prepare_target(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {"imdb_id": 1234567, "tmdb_id": 999, "douban_id": "1291546"})

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_generate_bdinfo_material(_content_path, output_dir, base_dir=None, playlist=None):
        _ = (base_dir, playlist)
        output_path = Path(output_dir) / "BD_FULL_00.txt"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("DISC INFO:\nDisc Title: Name\n", encoding="utf-8")
        return {"status": "generated", "bdinfo_file": str(output_path), "playlist": "00001.mpls", "blockers": []}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "generate_bdinfo_material", fake_generate_bdinfo_material)
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
            "--path",
            "/downloads/Disc",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--generate-bdinfo",
            "--bdinfo-playlist",
            "00001.mpls",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    material_stage = next(stage for stage in payload["stages"] if stage["stage"] == "materials-bdinfo")
    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert material_stage["ok"] is True
    assert material_stage["result"]["bdinfo_file"].endswith("BD_FULL_00.txt")
    assert target_stage["result"]["materials"]["assets"]["bdinfo"]["ready"] is True
    assert target_stage["result"]["materials"]["assets"]["bdinfo"]["path"].endswith("BD_FULL_00.txt")


@pytest.mark.asyncio
async def test_pipeline_enrich_metadata_before_prepare_target(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, None, "Name", "a" * 40, "desc"), {})

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    metadata_file = tmp_path / "metadata.json"
    metadata_file.write_text(json.dumps({"tmdb_id": 999, "douban": "https://movie.douban.com/subject/1291546/"}), encoding="utf-8")
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
            "--path",
            "/downloads/Name",
            "--enrich-metadata",
            "--metadata-file",
            str(metadata_file),
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    enrichment_stage = next(stage for stage in payload["stages"] if stage["stage"] == "metadata-enrich")
    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert enrichment_stage["ok"] is True
    assert enrichment_stage["result"]["tmdb_id"] == 999
    assert enrichment_stage["result"]["douban_id"] == "1291546"
    assert target_stage["result"]["metadata"]["tmdb_id"] == 999
    assert target_stage["result"]["materials"]["metadata"]["douban_url"] == "https://movie.douban.com/subject/1291546/"


@pytest.mark.asyncio
async def test_pipeline_generate_screenshot_materials_before_prepare_target(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {"imdb_id": 1234567, "tmdb_id": 999, "douban_id": "1291546"})

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_generate_screenshot_materials(_content_path, output_dir, count):
        files = []
        for index in range(1, count + 1):
            output_path = Path(output_dir) / f"screenshot-{index:02d}.png"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"png")
            files.append(str(output_path))
        return {"status": "generated", "screenshot_files": files, "count": len(files), "blockers": []}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "generate_screenshot_materials", fake_generate_screenshot_materials)
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
            "--path",
            "/downloads/Name",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--generate-screenshots",
            "--screenshot-count",
            "2",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    material_stage = next(stage for stage in payload["stages"] if stage["stage"] == "materials-screenshots")
    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert material_stage["ok"] is True
    assert material_stage["result"]["count"] == 2
    assert target_stage["result"]["materials"]["assets"]["screenshots"]["ready"] is True
    assert target_stage["result"]["materials"]["assets"]["screenshots"]["count"] == 2


@pytest.mark.asyncio
async def test_pipeline_upload_screenshots_before_prepare_target(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent", "img_host_1": "ptpimg", "ptpimg_api": "ptpimg-key"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {"imdb_id": 1234567, "tmdb_id": 999, "douban_id": "1291546"})

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_upload_screenshot_image_hosts(_config, screenshot_files, output_dir, image_host=None):
        output_path = Path(output_dir) / "image-host-uploads.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "uploaded",
            "host": image_host,
            "count": len(screenshot_files),
            "items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}],
            "blockers": [],
        }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return {**payload, "image_host_file": str(output_path)}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_screenshot_image_hosts", fake_upload_screenshot_image_hosts)
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
            "--path",
            "/downloads/Name",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--screenshot-file",
            str(screenshot),
            "--upload-screenshots",
            "--image-host",
            "ptpimg",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    image_host_stage = next(stage for stage in payload["stages"] if stage["stage"] == "materials-image-host")
    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert image_host_stage["ok"] is True
    assert image_host_stage["result"]["image_host_file"].endswith("image-host-uploads.json")
    assert target_stage["result"]["materials"]["assets"]["image_hosts"]["ready"] is True
    assert target_stage["result"]["materials"]["assets"]["image_hosts"]["count"] == 1


@pytest.mark.asyncio
async def test_pipeline_material_prerequisite_check_blocks_missing_image_host(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", "a" * 40, "desc"), {"imdb_id": 1234567, "tmdb_id": 999, "douban_id": "1291546"})

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_upload_screenshot_image_hosts(*_args, **_kwargs):
        raise AssertionError("image-host upload must not run without image-host prerequisites")

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_screenshot_image_hosts", fake_upload_screenshot_image_hosts)
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
            "--path",
            "/downloads/Name",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--screenshot-file",
            str(screenshot),
            "--upload-screenshots",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    prerequisite_stage = next(stage for stage in payload["stages"] if stage["stage"] == "material-prerequisite-check")
    image_host_stage = next(stage for stage in payload["stages"] if stage["stage"] == "materials-image-host")
    assert prerequisite_stage["ok"] is False
    assert "--upload-screenshots requires --image-host" in prerequisite_stage["result"]["blockers"][0]
    assert image_host_stage["ok"] is False
    assert payload["artifacts"]["material_generation"]["prerequisites"]["ok"] is False
    assert payload["artifacts"]["material_generation"]["prerequisites"]["blockers"] == prerequisite_stage["result"]["blockers"]
    assert payload["artifacts"]["material_generation"]["image_host"]["skipped"] is True
    assert any(blocker.startswith("material-prerequisite-check:") for blocker in payload["blockers"])
    assert "Fix the metadata/material prerequisites" in payload["next_actions"][0]


@pytest.mark.asyncio
async def test_pipeline_target_execute_defaults_to_generating_materials(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent", "img_host_1": "ptpimg", "ptpimg_api": "ptpimg-key"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (base_dir, source_id)
        return source_info_from_tuple(
            tracker,
            "60635",
            (1234567, 999, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"),
            {"douban_id": "1291546"},
        )

    async def fake_enrich_source_metadata(_config, source_info, *, overrides=None, fetch_ptgen=False, base_dir=None):
        _ = (overrides, base_dir)
        assert fetch_ptgen is True
        enriched = {
            **source_info,
            "tmdb_id": source_info.get("tmdb_id") or 999,
            "douban_id": "1291546",
            "douban_url": "https://movie.douban.com/subject/1291546/",
            "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
        }
        return {"status": "enriched", "ready": True, "source_info": enriched, "missing": [], "blockers": [], "sources": ["ptgen"]}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_generate_mediainfo_material(_content_path, output_dir):
        output_path = Path(output_dir) / "MI_FULL_00.txt"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("General\nComplete name : Name.mkv\n", encoding="utf-8")
        return {"status": "generated", "mediainfo_file": str(output_path), "blockers": []}

    async def fake_generate_screenshot_materials(_content_path, output_dir, count):
        output_path = Path(output_dir) / "screenshot-01.png"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"png")
        return {"status": "generated", "screenshot_files": [str(output_path)], "count": count, "blockers": []}

    async def fake_upload_screenshot_image_hosts(_config, screenshot_files, output_dir, image_host=None):
        output_path = Path(output_dir) / "image-host-uploads.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "uploaded",
            "host": image_host,
            "count": len(screenshot_files),
            "items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}],
            "blockers": [],
        }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return {**payload, "image_host_file": str(output_path)}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "enrich_source_metadata", fake_enrich_source_metadata)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "generate_mediainfo_material", fake_generate_mediainfo_material)
    monkeypatch.setattr(ptcli_cli, "generate_screenshot_materials", fake_generate_screenshot_materials)
    monkeypatch.setattr(ptcli_cli, "upload_screenshot_image_hosts", fake_upload_screenshot_image_hosts)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")
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
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--wait-uploaded-complete",
            "--uploaded-save-path",
            "/downloads/Name",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    assert payload["requested_actions"]["generate_mediainfo"] is False
    assert payload["requested_actions"]["generate_bdinfo"] is False
    assert payload["requested_actions"]["generate_screenshots"] is False
    assert payload["requested_actions"]["upload_screenshots"] is False
    assert payload["requested_actions"]["enrich_metadata"] is False
    assert payload["requested_actions"]["fetch_ptgen"] is False
    assert payload["effective_actions"]["enrich_metadata"] is True
    assert payload["effective_actions"]["fetch_ptgen"] is True
    assert payload["effective_actions"]["generate_bdinfo"] is True
    assert payload["effective_actions"]["generate_mediainfo"] is True
    assert payload["effective_actions"]["generate_screenshots"] is True
    assert payload["effective_actions"]["upload_screenshots"] is True
    bdinfo_stage = next(stage for stage in payload["stages"] if stage["stage"] == "materials-bdinfo")
    assert bdinfo_stage["ok"] is True
    assert bdinfo_stage["skipped"] is True
    assert next(stage for stage in payload["stages"] if stage["stage"] == "materials-mediainfo")["ok"] is True
    assert next(stage for stage in payload["stages"] if stage["stage"] == "materials-screenshots")["ok"] is True
    assert next(stage for stage in payload["stages"] if stage["stage"] == "materials-image-host")["ok"] is True
    target_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-prepare")
    assert target_stage["result"]["materials"]["ready"] is True


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
async def test_pipeline_promotes_matched_qbit_hash_when_source_hash_is_missing(monkeypatch, tmp_path) -> None:
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
        return source_info_from_tuple(tracker, source_id, (1, 2, "Name", None, "desc"), {})

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
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    verify_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-content-verify")
    assert verify_stage["ok"] is True
    assert verify_stage["result"]["expected_hash"] is None
    assert verify_stage["result"]["matched_hashes"] == ["b" * 40]
    assert payload["source_torrent_hash"] == "b" * 40
    assert payload["closure"]["source"]["torrent_hash"] == "b" * 40
    assert payload["evidence"]["source"]["torrent_hash"] == "b" * 40
    assert payload["evidence"]["source"]["mode"] == "matched"
    assert payload["closure_audit"]["ready"] is False
    assert next(item for item in payload["closure_audit"]["items"] if item["name"] == "source.hash_consistent")["ok"] is True


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
async def test_pipeline_prepare_target_gate_uses_dupe_check_and_rules_ack(monkeypatch, tmp_path, capsys) -> None:
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
    metadata_file = tmp_path / "metadata.json"
    await asyncio.to_thread(metadata_file.write_text, json.dumps({"tmdb_id": 999, "douban_id": "1291546"}), encoding="utf-8")
    mediainfo = tmp_path / "MI_FULL_00.txt"
    await asyncio.to_thread(mediainfo.write_text, "General\nComplete name : Name.mkv\n", encoding="utf-8")
    bdinfo = tmp_path / "BD_FULL_00.txt"
    await asyncio.to_thread(bdinfo.write_text, "DISC INFO:\nDisc Title: Name\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    await asyncio.to_thread(screenshot.write_bytes, b"png")
    image_host_file = tmp_path / "image-host-uploads.json"
    await asyncio.to_thread(
        image_host_file.write_text,
        json.dumps([{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}]),
        encoding="utf-8",
    )
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
            "--enrich-metadata",
            "--metadata-file",
            str(metadata_file),
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--mediainfo-file",
            str(mediainfo),
            "--bdinfo-file",
            str(bdinfo),
            "--generate-bdinfo",
            "--generate-mediainfo",
            "--screenshot-file",
            str(screenshot),
            "--generate-screenshots",
            "--screenshot-count",
            "2",
            "--image-host-file",
            str(image_host_file),
            "--upload-screenshots",
            "--image-host",
            "ptpimg",
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
    assert summary_payload["closure"]["blockers"] == ["target.uploaded", "target.downloaded", "target.injected", "target.materials_ready"]
    assert summary_payload["requested_actions"] == payload["requested_actions"]
    assert summary_payload["effective_actions"] == payload["effective_actions"]
    assert payload["flow_check"]["source_capability"]["source_download_adapter"] == "nexusphp_passkey"
    assert payload["flow_check"]["credential_requirements"] == ["TRACKERS.U2.passkey", "data/cookies/U2.txt", "TRACKERS.MTEAM.api_key"]
    assert summary_payload["flow_check"] == payload["flow_check"]
    assert summary_payload["summary"]["ready"] is True
    assert summary_payload["summary"]["flow"] == payload["flow_check"]
    assert summary_payload["summary"]["requested_source_id"] == source_url
    assert summary_payload["summary"]["source_torrent_id"] == "60635"
    assert summary_payload["summary"]["target"]["ready"] is False
    assert summary_payload["summary"]["gates"]["rule_check"]["rules_acknowledged"] is True
    assert summary_payload["summary"]["gates"]["duplicate_check"]["ok"] is True
    assert summary_payload["summary"]["gates"]["upload_gate"]["ready"] is True
    assert summary_payload["schema_version"] == 1
    assert summary_payload["kind"] == "ptcli.pipeline.run_summary"
    assert summary_payload["summary_file"] == str(summary_path)
    assert summary_payload["automation_handoff"]["json"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_path), "--json"]
    assert summary_payload["automation_handoff"]["print_shell"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_path), "--print-shell"]
    assert summary_payload["automation_handoff"]["run_next_command"]["command"] == shlex.join(["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_path), "--run-next-command"])
    assert summary_payload["stages"] == payload["stages"]
    assert summary_payload["artifacts"]["summary_file"] == str(summary_path)
    assert summary_payload["artifacts"]["target_package_dir"] == target_stage["result"]["package_dir"]
    assert summary_payload["artifacts"]["target_package_files"] == target_stage["result"]["files"]
    assert summary_payload["artifacts"]["target_materials"]["ready"] is False
    assert summary_payload["artifacts"]["target_materials_ready"] is False
    assert "metadata.ptgen_description" in summary_payload["artifacts"]["target_materials_missing"]
    assert summary_payload["artifacts"]["target_materials_warnings"]
    assert "metadata.ptgen_description" in summary_payload["artifacts"]["target_preparation_missing"]
    material_generation = summary_payload["artifacts"]["material_generation"]
    assert material_generation["prerequisites"]["ok"] is True
    assert material_generation["metadata"]["ok"] is True
    assert material_generation["metadata"]["missing"] == []
    assert material_generation["metadata"]["ptgen_description_length"] == 0
    assert material_generation["metadata"]["readiness"]["imdb_id"] == {"ready": True, "required": True, "source": "source"}
    assert material_generation["metadata"]["readiness"]["tmdb_id"] == {"ready": True, "required": True, "source": "source"}
    assert material_generation["metadata"]["readiness"]["douban_id"] == {"ready": True, "required": True, "source": "overrides"}
    assert material_generation["metadata"]["readiness"]["douban_url"] == {"ready": True, "required": True, "source": "overrides"}
    assert material_generation["metadata"]["readiness"]["ptgen_description"] == {"ready": False, "required": False, "source": None}
    assert material_generation["bdinfo"]["skipped"] is True
    assert material_generation["mediainfo"]["skipped"] is True
    assert material_generation["screenshots"]["skipped"] is True
    assert material_generation["image_host"]["skipped"] is True
    assert summary_payload["status"] == "blocked"
    assert any("target.materials.metadata.ptgen_description" in blocker for blocker in summary_payload["blockers"])
    assert any("PTGen/Douban description" in action for action in summary_payload["next_actions"])
    assert summary_payload["material_options"]["metadata_file"] == str(metadata_file)
    assert summary_payload["material_options"]["mediainfo_file"] == str(mediainfo)
    assert summary_payload["material_options"]["bdinfo_file"] == str(bdinfo)
    assert summary_payload["material_options"]["screenshot_files"] == [str(screenshot)]
    assert summary_payload["material_options"]["screenshot_count"] == 2
    assert summary_payload["material_options"]["image_host"] == "ptpimg"
    assert summary_payload["material_options"]["image_host_file"] == str(image_host_file)
    assert summary_payload["resume_state"]["complete"] is False
    assert summary_payload["resume_state"]["resume_available"] is True
    assert summary_payload["resume_state"]["next_stage"] == "resume-target-package"
    assert summary_payload["resume_state"]["artifacts"]["target_package_dir"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_materials_ready"] is False
    assert summary_payload["resume_state"]["artifacts"]["target_torrent_file"] is False
    assert summary_payload["resume_state"]["materials"]["target_materials_ready"] is False
    assert summary_payload["resume_state"]["materials"]["target_preparation_ready"] is False
    assert "metadata.ptgen_description" in summary_payload["resume_state"]["materials"]["target_materials_missing"]
    assert "metadata.ptgen_description" in summary_payload["resume_state"]["materials"]["target_preparation_missing"]
    recovery_hints = summary_payload["resume_state"]["materials"]["recovery_hints"]
    recovery_by_key = {hint["key"]: hint for hint in recovery_hints}
    assert recovery_by_key["metadata.ptgen_description"]["command_flags"] == ["--enrich-metadata", "--fetch-ptgen"]
    assert recovery_by_key["metadata.ptgen_description"]["existing_file_options"] == ["--metadata-file"]
    assert recovery_by_key["metadata.ptgen_description"]["resume_command_available"] is True
    assert recovery_by_key["metadata.ptgen_description"]["resume_command_stage"] == "resume-target-package"
    assert "--fetch-ptgen" in recovery_by_key["metadata.ptgen_description"]["resume_command_argv"]
    assert recovery_by_key["description.content"]["command_flags"] == ["--prepare-target"]
    assert recovery_by_key["description.content"]["resume_command_available"] is True
    assert "--prepare-target" in recovery_by_key["description.content"]["resume_command_argv"]
    assert all(hint["resume_stage"] == "resume-target-package" for hint in recovery_hints)
    material_closure = summary_payload["resume_state"]["materials"]["closure"]
    assert material_closure["ready"] is False
    assert material_closure["critical_ready"] is False
    assert material_closure["critical_missing"] == ["metadata.ptgen_description", "description.ptgen_description", "description.content"]
    assert material_closure["critical_domains"]["metadata"] == {"ready": False, "missing": ["metadata.ptgen_description", "description.ptgen_description"]}
    assert material_closure["critical_domains"]["description"] == {"ready": False, "missing": ["description.content"]}
    assert material_closure["critical_domains"]["media_info"]["ready"] is True
    assert material_closure["critical_domains"]["screenshots"]["ready"] is True
    assert material_closure["critical_domains"]["image_host"]["ready"] is True
    assert material_closure["critical_path"]["ready"] is False
    assert material_closure["critical_path"]["next_step"] == "metadata"
    assert material_closure["critical_path"]["missing"] == [
        "metadata.ptgen_description",
        "description.ptgen_description",
        "description.content",
        "materials.description.ptgen_description",
        "payload.preflight",
    ]
    assert [step["name"] for step in material_closure["critical_path"]["steps"]] == [
        "metadata",
        "media_info",
        "screenshots",
        "image_host",
        "description",
        "target_materials",
        "target_preparation",
    ]
    assert material_closure["metadata"]["ready"] is False
    assert material_closure["metadata"]["imdb_id"] == 1234567
    assert material_closure["metadata"]["tmdb_id"] == 2
    assert material_closure["metadata"]["douban_id"] == "1291546"
    assert material_closure["metadata"]["ptgen_description_length"] == 0
    assert material_closure["metadata"]["readiness"]["ptgen_description"]["ready"] is False
    assert material_closure["metadata"]["readiness"]["ptgen_description"]["required"] is False
    assert "metadata.ptgen_description" in material_closure["metadata"]["missing"]
    assert material_closure["mediainfo"] == {"ready": True, "generated": False, "missing": [], "path": str(mediainfo)}
    assert material_closure["bdinfo"] == {"ready": True, "required": False, "generated": False, "missing": [], "path": str(bdinfo)}
    assert material_closure["screenshots"]["ready"] is True
    assert material_closure["screenshots"]["count"] == 1
    assert material_closure["image_host"]["ready"] is True
    assert material_closure["image_host"]["count"] == 1
    assert material_closure["image_host"]["urls"] == {
        "raw_urls": ["https://img.example/raw.png"],
        "img_urls": ["https://img.example/thumb.png"],
        "web_urls": ["https://img.example/page"],
        "item_count": 1,
        "valid_count": 1,
        "invalid_count": 0,
    }
    assert material_closure["description"]["ready"] is False
    assert material_closure["description"]["has_ptgen_description"] is False
    assert material_closure["description"]["has_external_ids"] is True
    assert material_closure["description"]["external_id_readiness"] == {"imdb": True, "tmdb": True, "douban": True}
    assert material_closure["description"]["external_id_missing"] == []
    assert material_closure["description"]["external_links"] == {
        "imdb": "https://www.imdb.com/title/tt1234567",
        "tmdb": "https://www.themoviedb.org/movie/2",
        "douban": "https://movie.douban.com/subject/1291546/",
    }
    assert material_closure["description"]["has_mediainfo_or_bdinfo"] is True
    assert material_closure["description"]["has_screenshot_bbcode"] is True
    assert material_closure["description"]["bbcode_image_urls"] == ["https://img.example/thumb.png"]
    assert any("PTGen/Douban description" in action for action in summary_payload["resume_state"]["materials"]["next_actions"])
    code = main(["summary-check", "--summary-file", str(summary_path), "--print-shell"])
    assert code == 0
    out = capsys.readouterr().out
    assert "export PTCLI_RESUME_MATERIAL_CLOSURE_READY=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_READY=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_MISSING=metadata.ptgen_description,description.ptgen_description,description.content\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_METADATA_READY=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_METADATA_MISSING=metadata.ptgen_description,description.ptgen_description\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_MEDIA_INFO_READY=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_SCREENSHOTS_READY=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_IMAGE_HOST_READY=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_DESCRIPTION_READY=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_DESCRIPTION_MISSING=description.content\n" in out
    assert "export PTCLI_RESUME_MATERIAL_METADATA_READY=0\n" in out
    assert "PTCLI_RESUME_MATERIAL_METADATA_READINESS=" in out
    assert "ptgen_description" in out
    assert "export PTCLI_RESUME_MATERIAL_METADATA_IMDB_ID=1234567\n" in out
    assert "export PTCLI_RESUME_MATERIAL_METADATA_TMDB_ID=2\n" in out
    assert "export PTCLI_RESUME_MATERIAL_METADATA_DOUBAN_ID=1291546\n" in out
    assert "export PTCLI_RESUME_MATERIAL_PTGEN_DESCRIPTION_LENGTH=0\n" in out
    assert f"export PTCLI_RESUME_MATERIAL_MEDIAINFO_FILE={mediainfo}\n" in out
    assert f"export PTCLI_RESUME_MATERIAL_BDINFO_FILE={bdinfo}\n" in out
    assert "export PTCLI_RESUME_MATERIAL_SCREENSHOTS_READY=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_READY=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_ITEM_COUNT=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_VALID_COUNT=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_INVALID_COUNT=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_IMG_URLS=https://img.example/thumb.png\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_WEB_URLS=https://img.example/page\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_PTGEN=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_EXTERNAL_IDS=1\n" in out
    assert "PTCLI_RESUME_MATERIAL_DESCRIPTION_EXTERNAL_ID_READINESS=" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_EXTERNAL_ID_MISSING=''\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_IMDB=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_TMDB=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_DOUBAN=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_IMDB_LINK=https://www.imdb.com/title/tt1234567\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_TMDB_LINK=https://www.themoviedb.org/movie/2\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_DOUBAN_LINK=https://movie.douban.com/subject/1291546/\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_IMAGE_URLS=https://img.example/thumb.png\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_HINT_COUNT=2\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_KEYS=metadata.ptgen_description,description.content\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_COMMAND_AVAILABLE=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_COMMAND_STAGES=resume-target-package,resume-target-package\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_MISSING_FLAGS=''\n" in out
    assert "PTCLI_RESUME_MATERIAL_FIRST_RECOVERY_COMMAND='python3 ptcli.py pipeline" in out
    assert "--fetch-ptgen" in out
    resume_commands = {command["stage"]: command["command"] for command in summary_payload["resume_commands"]}
    assert "resume-target-package" in resume_commands
    assert "--prepare-target" in resume_commands["resume-target-package"]
    assert "--fetch-ptgen" in resume_commands["resume-target-package"]
    assert "--check-dupes" in resume_commands["resume-target-package"]
    assert "--target-output-dir" in resume_commands["resume-target-package"]
    assert f"--metadata-file {shlex.quote(str(metadata_file))}" in resume_commands["resume-target-package"]
    assert f"--mediainfo-file {shlex.quote(str(mediainfo))}" in resume_commands["resume-target-package"]
    assert f"--bdinfo-file {shlex.quote(str(bdinfo))}" in resume_commands["resume-target-package"]
    assert "--generate-bdinfo" in resume_commands["resume-target-package"]
    assert "--generate-mediainfo" in resume_commands["resume-target-package"]
    assert f"--screenshot-file {shlex.quote(str(screenshot))}" in resume_commands["resume-target-package"]
    assert "--generate-screenshots" in resume_commands["resume-target-package"]
    assert "--screenshot-count 2" in resume_commands["resume-target-package"]
    assert f"--image-host-file {shlex.quote(str(image_host_file))}" in resume_commands["resume-target-package"]
    assert "--upload-screenshots" in resume_commands["resume-target-package"]
    assert "--image-host ptpimg" in resume_commands["resume-target-package"]
    assert "resume-target-upload" not in resume_commands
    assert summary_payload["next_actions"]


@pytest.mark.asyncio
async def test_pipeline_summary_recovers_missing_image_host_uploads(monkeypatch, tmp_path, capsys) -> None:
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
        return source_info_from_tuple(
            tracker,
            source_id,
            (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"),
            {"douban_id": "1291546", "douban_url": "https://movie.douban.com/subject/1291546/"},
        )

    async def fake_enrich_source_metadata(_config, source_info, *, overrides=None, fetch_ptgen=False, base_dir=None):
        _ = (overrides, fetch_ptgen, base_dir)
        enriched = {**source_info, "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介"}
        return {"status": "enriched", "ready": True, "source_info": enriched, "applied": {"ptgen_description": {"length": 21}}, "missing": [], "sources": ["ptgen"], "blockers": []}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "enrich_source_metadata", fake_enrich_source_metadata)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    mediainfo = tmp_path / "MI_FULL_00.txt"
    await asyncio.to_thread(mediainfo.write_text, "General\nComplete name : Name.mkv\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    await asyncio.to_thread(screenshot.write_bytes, b"png")
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
            "--enrich-metadata",
            "--fetch-ptgen",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--mediainfo-file",
            str(mediainfo),
            "--screenshot-file",
            str(screenshot),
            "--accept-rules",
            "--write-summary",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    summary_path = Path(payload["summary_file"])
    summary_payload = json.loads(await asyncio.to_thread(summary_path.read_text, encoding="utf-8"))
    assert summary_payload["status"] == "blocked"
    assert summary_payload["artifacts"]["target_materials_ready"] is False
    assert "assets.image_host_uploads" in summary_payload["artifacts"]["target_materials_missing"]
    assert "description.content" in summary_payload["artifacts"]["target_preparation_missing"]
    material_closure = summary_payload["resume_state"]["materials"]["closure"]
    assert material_closure["critical_ready"] is False
    assert material_closure["critical_domains"]["metadata"]["ready"] is True
    assert material_closure["critical_domains"]["media_info"]["ready"] is True
    assert material_closure["critical_domains"]["screenshots"]["ready"] is True
    assert material_closure["critical_domains"]["image_host"] == {"ready": False, "missing": ["assets.image_host_uploads"]}
    assert material_closure["critical_domains"]["description"] == {"ready": False, "missing": ["description.screenshot_bbcode", "description.content"]}
    assert material_closure["screenshots"]["ready"] is True
    assert material_closure["screenshots"]["count"] == 1
    assert material_closure["screenshots"]["files"] == [str(screenshot)]
    assert material_closure["image_host"]["ready"] is False
    assert material_closure["image_host"]["count"] == 0
    assert material_closure["image_host"]["urls"] == {"raw_urls": [], "img_urls": [], "web_urls": [], "item_count": 0, "valid_count": 0, "invalid_count": 0}
    assert material_closure["description"]["has_ptgen_description"] is True
    assert material_closure["description"]["has_external_ids"] is True
    assert material_closure["description"]["has_mediainfo_or_bdinfo"] is True
    assert material_closure["description"]["has_screenshot_bbcode"] is False
    assert material_closure["description"]["screenshot_coverage"]["ready"] is True
    recovery_by_key = {hint["key"]: hint for hint in summary_payload["resume_state"]["materials"]["recovery_hints"]}
    assert recovery_by_key["assets.image_host_uploads"]["command_flags"] == ["--upload-screenshots", "--image-host"]
    assert recovery_by_key["assets.image_host_uploads"]["existing_file_options"] == ["--image-host-file"]
    assert recovery_by_key["assets.image_host_uploads"]["resume_command_available"] is True
    assert "--upload-screenshots" in recovery_by_key["assets.image_host_uploads"]["resume_command_argv"]
    assert "--screenshot-file" in recovery_by_key["assets.image_host_uploads"]["resume_command_argv"]

    code = main(["summary-check", "--summary-file", str(summary_path), "--print-shell"])

    assert code == 0
    out = capsys.readouterr().out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_IMAGE_HOST_READY=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_IMAGE_HOST_MISSING=assets.image_host_uploads\n" in out
    assert "export PTCLI_RESUME_MATERIAL_SCREENSHOTS_READY=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_SCREENSHOTS_COUNT=1\n" in out
    assert f"export PTCLI_RESUME_MATERIAL_SCREENSHOTS_FILES={screenshot}\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_READY=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_COUNT=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_ITEM_COUNT=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_VALID_COUNT=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_INVALID_COUNT=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_SCREENSHOTS=0\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_READY=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_RECOVERY_KEYS=assets.image_host_uploads,description.screenshot_bbcode,description.content\n" in out
    assert "--upload-screenshots" in out


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
    patch_pipeline_live_material_stages(monkeypatch)
    uploaded_hash: str | None = None

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        nonlocal uploaded_hash
        uploaded_path = tmp_path / "MTEAM-999.torrent"
        uploaded_hash = write_valid_torrent(uploaded_path, tmp_path / "uploaded-content" / "MTEAM-999.mkv")
        return {
            "status": "uploaded",
            "uploaded_torrent_hash": uploaded_hash,
            "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_path)},
        }

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        injected_hash = uploaded_hash if "MTEAM" in str(torrent_path) and uploaded_hash else "a" * 40
        return {
            "hash": injected_hash,
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
            "visible_in_client": True, "verified_in_client": True,
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
    payload_review = payload["evidence"]["target"]["payload_review"]
    assert payload_review["present"] is True
    assert payload_review["description"]["external_id_readiness"] == {"imdb": True, "tmdb": True, "douban": True}
    assert payload_review["description"]["has_ptgen_description"] is True
    assert payload_review["description"]["has_mediainfo_or_bdinfo"] is True
    assert payload_review["description"]["has_screenshot_bbcode"] is True
    assert payload_review["description"]["screenshot_coverage"]["ready"] is True
    assert payload_review["materials"]["image_host_urls"] == ["https://img.example/thumb.png"]


@pytest.mark.asyncio
async def test_pipeline_closure_complete_for_full_retorrent_flow(monkeypatch, tmp_path, capsys) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"U2": {"passkey": "u2-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "U2.txt").write_text("uid=1;", encoding="utf-8")
    torrent_file = make_mteam_safe_torrent(tmp_path, "target")
    source_torrent = tmp_path / "source-out" / "U2-60635.torrent"
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")
    mediainfo = tmp_path / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Name.mkv\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_host_file = tmp_path / "image-host-uploads.json"
    image_host_file.write_text(json.dumps({"items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}]}), encoding="utf-8")
    uploaded_hash: str | None = None
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(
            tracker,
            source_id,
            (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", source_hash, "desc"),
            {"douban_id": "1291546"},
        )

    async def fake_enrich_source_metadata(_config, source_info, *, overrides=None, fetch_ptgen=False, base_dir=None):
        _ = (overrides, fetch_ptgen, base_dir)
        enriched = {**source_info, "douban_id": "1291546", "douban_url": "https://movie.douban.com/subject/1291546/", "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介"}
        return {"status": "enriched", "ready": True, "source_info": enriched, "applied": {"ptgen_description": {"length": len(enriched["ptgen_description"])}}, "missing": [], "sources": ["ptgen"], "blockers": []}

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        return source_torrent

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        return {"hash": source_hash, "torrent_path": torrent_path, "save_path": save_path, "category": category, "tags": tags, "paused": paused, "visible_in_client": True, "verified_in_client": True}

    wait_calls = []

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = config
        wait_calls.append({"client_name": client_name, "content_path": content_path, "torrent_hash": torrent_hash, "timeout": timeout, "interval": interval})
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": content_path or "/downloads/Name", "hash": torrent_hash or source_hash}]}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": source_hash}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        nonlocal uploaded_hash
        uploaded_path = tmp_path / "MTEAM-999.torrent"
        uploaded_hash = write_valid_torrent(uploaded_path, tmp_path / "uploaded-content" / "MTEAM-999.mkv")
        return {"status": "uploaded", "uploaded_torrent_hash": uploaded_hash, "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_path)}}

    async def fake_inject_uploaded_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        assert uploaded_hash is not None
        return {"hash": uploaded_hash, "torrent_path": torrent_path, "save_path": save_path, "category": category, "tags": tags, "paused": paused, "visible_in_client": True, "verified_in_client": True}

    calls = {"inject": 0}

    async def fake_inject_router(*args):
        calls["inject"] += 1
        if calls["inject"] == 1:
            return await fake_inject_source_with_config(*args)
        return await fake_inject_uploaded_with_config(*args)

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "enrich_source_metadata", fake_enrich_source_metadata)
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
            "--wait-timeout",
            "7200",
            "--wait-interval",
            "45",
            "--enrich-metadata",
            "--fetch-ptgen",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--mediainfo-file",
            str(mediainfo),
            "--screenshot-file",
            str(screenshot),
            "--image-host-file",
            str(image_host_file),
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
            "--uploaded-wait-timeout",
            "900",
            "--uploaded-wait-interval",
            "20",
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
    assert payload["evidence"]["source"]["torrent_file_evidence"] is True
    assert payload["evidence"]["target"]["uploaded_torrent_file_evidence"] is True
    assert payload["evidence"]["target"]["materials_ready"] is True
    assert payload["evidence"]["target"]["materials"]["assets"]["image_hosts"]["count"] == 1
    target_audit = payload["evidence"]["target"]["preparation_audit"]
    assert target_audit["ready"] is True
    assert target_audit["description"]["has_ptgen_description"] is True
    assert target_audit["description"]["has_external_ids"] is True
    assert target_audit["description"]["has_mediainfo_or_bdinfo"] is True
    assert target_audit["description"]["has_screenshot_bbcode"] is True
    assert target_audit["description"]["bbcode_image_count"] == 1
    assert target_audit["description"]["missing"] == []
    assert target_audit["payload"]["description_checks_ready"] is True
    assert target_audit["payload_review"]["present"] is True
    assert target_audit["payload_review"]["description"]["external_id_readiness"] == {"imdb": True, "tmdb": True, "douban": True}
    assert target_audit["payload_review"]["description"]["screenshot_coverage"]["ready"] is True
    assert payload["evidence"]["target"]["payload_review"]["description"]["bbcode_image_urls"] == ["https://img.example/thumb.png"]
    assert payload["evidence"]["target"]["payload_review"]["materials"]["image_host_urls"] == ["https://img.example/thumb.png"]
    assert payload["closure_audit"]["ready"] is True
    assert payload["closure_audit"]["missing"] == []
    audit_items = {item["name"]: item for item in payload["closure_audit"]["items"]}
    assert audit_items["source.injected_torrent_hash"]["ok"] is True
    assert audit_items["target.uploaded_wait_evidence"]["ok"] is True
    assert payload["closure"]["target"]["uploaded_torrent_hash"] == uploaded_hash
    assert payload["closure"]["target"]["seeding"] is True
    assert payload["closure"]["target"]["uploaded_wait"]["complete"] is True
    assert wait_calls[0]["timeout"] == 7200.0
    assert wait_calls[0]["interval"] == 45.0
    assert wait_calls[-1]["torrent_hash"] == uploaded_hash
    assert wait_calls[-1]["content_path"] == "/downloads"
    assert wait_calls[-1]["timeout"] == 900.0
    assert wait_calls[-1]["interval"] == 20.0
    assert payload["automation_handoff"]["json"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", payload["summary_file"], "--json"]
    assert payload["automation_handoff"]["run_next_command"]["command"] == shlex.join(["python3", "ptcli.py", "summary-check", "--summary-file", payload["summary_file"], "--run-next-command"])
    summary_payload = json.loads(await asyncio.to_thread(Path(payload["summary_file"]).read_text, encoding="utf-8"))
    assert summary_payload["complete"] is True
    assert summary_payload["closure_audit"]["ready"] is True
    assert summary_payload["closure_audit"]["missing"] == []
    assert summary_payload["closure_status"]["complete"] is True
    assert summary_payload["closure_status"]["closure_complete"] is True
    assert summary_payload["closure_status"]["pipeline_status"] == "ok"
    assert summary_payload["closure_status"]["pipeline_blockers"] == summary_payload["blockers"]
    assert summary_payload["closure_status"]["source"]["ready"] is True
    assert summary_payload["closure_status"]["source"]["hash_consistent"] is True
    assert summary_payload["closure_status"]["target"]["ready"] is True
    assert summary_payload["closure_status"]["target"]["rule_obligations_ready"] is True
    assert summary_payload["closure_status"]["target"]["uploaded_wait_evidence"] is True
    assert summary_payload["closure_review"]["complete"] is True
    assert summary_payload["closure_review"]["missing"] == []
    assert summary_payload["closure_review"]["source"]["torrent_hash"] == source_hash
    assert summary_payload["closure_review"]["source"]["torrent_file_evidence"] is True
    assert summary_payload["closure_review"]["source"]["torrent_file_artifact"]["exists"] is True
    assert summary_payload["closure_review"]["source"]["torrent_file_artifact"]["is_file"] is True
    assert summary_payload["closure_review"]["source"]["torrent_file_artifact"]["torrent_hash"] == source_hash
    assert summary_payload["closure_review"]["source"]["torrent_file_artifact"]["metadata_readable"] is True
    assert summary_payload["closure_review"]["source"]["save_path"] == "/downloads"
    assert summary_payload["closure_review"]["source"]["qbit_category"] == "SOURCE"
    assert summary_payload["closure_review"]["source"]["qbit_tags"] == "source-tag"
    assert summary_payload["closure_review"]["source"]["paused"] is True
    assert summary_payload["closure_review"]["source"]["injection_visible_in_client"] is True
    assert summary_payload["closure_review"]["target"]["uploaded_torrent_hash"] == uploaded_hash
    assert summary_payload["closure_review"]["target"]["uploaded_torrent_file"].endswith("MTEAM-999.torrent")
    assert summary_payload["closure_review"]["target"]["materials_ready"] is True
    assert summary_payload["closure_review"]["target"]["metadata_ready"] is True
    assert summary_payload["closure_review"]["target"]["assets_ready"] is True
    assert summary_payload["closure_review"]["target"]["description_ready"] is True
    assert summary_payload["closure_review"]["target"]["preparation_ready"] is True
    assert summary_payload["closure_review"]["target"]["preparation_missing"] == []
    assert summary_payload["closure_review"]["target"]["description"]["has_ptgen_description"] is True
    assert summary_payload["closure_review"]["target"]["description"]["ptgen_description_length"] == 21
    assert summary_payload["closure_review"]["target"]["description"]["has_external_ids"] is True
    assert summary_payload["closure_review"]["target"]["description"]["external_links"]["imdb"] == "https://www.imdb.com/title/tt1234567"
    assert summary_payload["closure_review"]["target"]["description"]["external_links"]["tmdb"] == "https://www.themoviedb.org/movie/2"
    assert summary_payload["closure_review"]["target"]["description"]["external_links"]["douban"] == "https://movie.douban.com/subject/1291546/"
    assert summary_payload["closure_review"]["target"]["description"]["has_mediainfo_or_bdinfo"] is True
    assert summary_payload["closure_review"]["target"]["description"]["has_screenshot_bbcode"] is True
    assert summary_payload["closure_review"]["target"]["description"]["bbcode_image_count"] == 1
    assert summary_payload["closure_review"]["target"]["description"]["bbcode_image_urls"] == ["https://img.example/thumb.png"]
    assert summary_payload["evidence"]["target"]["payload_review"]["description"]["external_links"]["douban"] == "https://movie.douban.com/subject/1291546/"
    assert summary_payload["evidence"]["target"]["payload_review"]["description"]["screenshot_coverage"] == {
        "ready": True,
        "expected_urls": ["https://img.example/thumb.png"],
        "description_urls": ["https://img.example/thumb.png"],
        "missing_urls": [],
    }
    assert summary_payload["summary"]["closure_audit"]["ready"] is True
    assert summary_payload["config"] == str(tmp_path / "config.py")
    assert summary_payload["base_dir"] == str(tmp_path)
    assert summary_payload["client"] == "default"
    assert summary_payload["qbit_options"] == {
        "source": {"category": "SOURCE", "tags": "source-tag", "paused": True},
        "uploaded": {"category": "MTEAM", "tags": "retorrent", "paused": True},
    }
    assert summary_payload["wait_options"] == {
        "source": {"timeout": 7200.0, "interval": 45.0},
        "uploaded": {"timeout": 900.0, "interval": 20.0},
    }
    assert summary_payload["artifacts"]["source_torrent_file"].endswith("U2-60635.torrent")
    assert summary_payload["artifacts"]["target_preparation_ready"] is True
    assert summary_payload["artifacts"]["target_preparation_audit"]["description"]["has_screenshot_bbcode"] is True
    assert summary_payload["artifacts"]["target_payload_review"]["description"]["has_ptgen_description"] is True
    assert summary_payload["artifacts"]["target_payload_review"]["materials"]["image_host_urls"] == ["https://img.example/thumb.png"]
    assert summary_payload["material_diagnostics"]["present"] is True
    assert summary_payload["material_diagnostics"]["ready_for_mteam_upload"] is True
    assert summary_payload["material_diagnostics"]["critical_ready"] is True
    assert summary_payload["material_diagnostics"]["critical_missing"] == []
    assert summary_payload["material_diagnostics"]["critical_path"]["ready"] is True
    assert summary_payload["material_diagnostics"]["critical_path"]["next_step"] is None
    assert summary_payload["material_diagnostics"]["image_host_urls"]["img_urls"] == ["https://img.example/thumb.png"]
    assert summary_payload["target_preflight_diagnostics"]["ready"] is True
    assert summary_payload["target_preflight_diagnostics"]["target_preparation_ready"] is True
    assert summary_payload["target_preflight_diagnostics"]["materials_ready"] is True
    assert summary_payload["target_preflight_diagnostics"]["description_ready"] is True
    assert summary_payload["target_preflight_diagnostics"]["payload_ready"] is True
    assert summary_payload["target_preflight_diagnostics"]["materials_ready_required"] is True
    material_closure = summary_payload["resume_state"]["materials"]["closure"]
    assert material_closure["ready"] is True
    assert material_closure["critical_ready"] is True
    assert material_closure["critical_missing"] == []
    assert material_closure["critical_path"]["ready"] is True
    assert material_closure["critical_path"]["next_step"] is None
    assert material_closure["metadata"]["ready"] is True
    assert material_closure["metadata"]["ptgen_description_length"] == 21
    assert material_closure["mediainfo"]["ready"] is True
    assert material_closure["screenshots"]["ready"] is True
    assert material_closure["image_host"]["ready"] is True
    assert material_closure["image_host"]["urls"]["img_urls"] == ["https://img.example/thumb.png"]
    assert material_closure["description"]["ready"] is True
    assert material_closure["description"]["external_id_readiness"] == {"imdb": True, "tmdb": True, "douban": True}
    assert material_closure["description"]["bbcode_image_urls"] == ["https://img.example/thumb.png"]
    assert summary_payload["artifacts"]["source_torrent_file_evidence"] is True
    assert summary_payload["artifacts"]["source_torrent_file_artifact"]["path"].endswith("U2-60635.torrent")
    assert summary_payload["artifacts"]["source_torrent_file_artifact"]["exists"] is True
    assert summary_payload["artifacts"]["source_torrent_file_artifact"]["is_file"] is True
    assert summary_payload["artifacts"]["source_torrent_file_artifact"]["size_bytes"] > 0
    assert len(summary_payload["artifacts"]["source_torrent_file_artifact"]["sha1"]) == 40
    assert summary_payload["artifacts"]["source_torrent_file_artifact"]["torrent_hash"] == source_hash
    assert summary_payload["artifacts"]["source_torrent_file_artifact"]["metadata_readable"] is True
    assert summary_payload["artifacts"]["source_torrent_hash"] == source_hash
    assert summary_payload["artifacts"]["source_save_path"] == "/downloads"
    assert summary_payload["artifacts"]["source_qbit_category"] == "SOURCE"
    assert summary_payload["artifacts"]["source_qbit_tags"] == "source-tag"

    code = main(["summary-check", "--summary-file", str(payload["summary_file"]), "--print-shell"])

    assert code == 0
    out = capsys.readouterr().out
    assert "export PTCLI_CLOSURE_REVIEW_COMPLETE=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_MISSING=''\n" in out
    assert "export PTCLI_COMPLETION_MATRIX_READY=1\n" in out
    assert "export PTCLI_COMPLETION_MATRIX_MISSING_DOMAINS=''\n" in out
    assert "export PTCLI_COMPLETION_FLOW_READY=1\n" in out
    assert "export PTCLI_COMPLETION_SOURCE_READY=1\n" in out
    assert "export PTCLI_COMPLETION_MATERIALS_READY=1\n" in out
    assert "export PTCLI_READY_FOR_MTEAM_UPLOAD=1\n" in out
    assert "export PTCLI_MATERIAL_CRITICAL_READY=1\n" in out
    assert "export PTCLI_MATERIAL_CRITICAL_PATH_READY=1\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_READY=1\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_MATERIALS_READY=1\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_DESCRIPTION_READY=1\n" in out
    assert "export PTCLI_TARGET_PREFLIGHT_PAYLOAD_READY=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CLOSURE_READY=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_CRITICAL_READY=1\n" in out
    assert "export PTCLI_RESUME_MATERIAL_IMAGE_HOST_IMG_URLS=https://img.example/thumb.png\n" in out
    assert "export PTCLI_RESUME_MATERIAL_DESCRIPTION_IMAGE_URLS=https://img.example/thumb.png\n" in out
    assert "export PTCLI_COMPLETION_RULES_READY=1\n" in out
    assert "export PTCLI_COMPLETION_TARGET_UPLOAD_READY=1\n" in out
    assert "export PTCLI_COMPLETION_QBIT_WAIT_READY=1\n" in out
    assert f"export PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_HASH={source_hash}\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_FILE_EVIDENCE=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_EXISTS=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_IS_FILE=1\n" in out
    assert f"export PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_INFOHASH={source_hash}\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_METADATA_READABLE=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_SOURCE_SAVE_PATH=/downloads\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_SOURCE_QBIT_CATEGORY=SOURCE\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_SOURCE_QBIT_TAGS=source-tag\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_SOURCE_PAUSED=1\n" in out
    assert f"export PTCLI_CLOSURE_REVIEW_UPLOADED_TORRENT_HASH={uploaded_hash}\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_TARGET_UPLOADED_WAIT_EVIDENCE=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_TARGET_MATERIALS_READY=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_TARGET_METADATA_READY=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_TARGET_ASSETS_READY=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_TARGET_DESCRIPTION_READY=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_DESCRIPTION_HAS_PTGEN=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_DESCRIPTION_PTGEN_LENGTH=21\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_DESCRIPTION_HAS_EXTERNAL_IDS=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_DESCRIPTION_IMDB_LINK=https://www.imdb.com/title/tt1234567\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_DESCRIPTION_TMDB_LINK=https://www.themoviedb.org/movie/2\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_DESCRIPTION_DOUBAN_LINK=https://movie.douban.com/subject/1291546/\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_DESCRIPTION_HAS_MEDIAINFO_OR_BDINFO=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_DESCRIPTION_HAS_SCREENSHOTS=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_DESCRIPTION_IMAGE_URLS=https://img.example/thumb.png\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_READY=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_TORRENT_HASH=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_INJECTED_HASH=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_INJECTION_VISIBLE=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_INJECTION_VERIFIED=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_WAIT=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_CHECK_TARGET_PREPARATION=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_CHECK_TARGET_UPLOADED_WAIT=1\n" in out
    assert "export PTCLI_CLOSURE_REVIEW_CHECK_TARGET_RULES=1\n" in out
    assert summary_payload["artifacts"]["source_paused"] is True
    assert summary_payload["artifacts"]["source_hash_consistent"] is True
    assert summary_payload["artifacts"]["source_injected_torrent_hash"] == source_hash
    assert summary_payload["artifacts"]["source_injection_visible_in_client"] is True
    assert summary_payload["artifacts"]["source_injection_verified"] is True
    assert summary_payload["artifacts"]["target_torrent_file"] == str(torrent_file)
    assert summary_payload["artifacts"]["target_package_dir"]
    assert summary_payload["artifacts"]["uploaded_torrent_file"] == str(tmp_path / "MTEAM-999.torrent")
    assert summary_payload["artifacts"]["uploaded_torrent_file_evidence"] is True
    assert summary_payload["artifacts"]["uploaded_torrent_id"] == "999"
    assert summary_payload["artifacts"]["uploaded_torrent_hash"] == uploaded_hash
    assert summary_payload["artifacts"]["injected_torrent_hash"] == uploaded_hash
    assert summary_payload["artifacts"]["injection_visible_in_client"] is True
    assert summary_payload["artifacts"]["injection_verified"] is True
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
        "source_injected_torrent_hash": True,
        "source_injection_visible_in_client": True,
        "source_injection_verified": True,
        "source_wait_evidence": True,
        "target_package_dir": True,
        "target_materials_ready": True,
        "target_torrent_file": True,
        "uploaded_torrent_id": True,
        "uploaded_torrent_file": True,
        "uploaded_torrent_hash": True,
        "injected_torrent_hash": True,
        "injection_visible_in_client": True,
        "injection_verified": True,
        "uploaded_save_path": True,
        "uploaded_qbit_category": True,
        "uploaded_qbit_tags": True,
        "uploaded_paused": True,
        "uploaded_wait_evidence": True,
        "target_hash_consistent": True,
        "target_duplicate_clean": True,
        "target_rule_obligations": True,
        "target_preparation_ready": True,
    }
    assert summary_payload["artifacts"]["source_torrent_file"] in resume_commands["resume-source-torrent"]
    assert resume_argv["resume-source-torrent"][:3] == ["python3", "ptcli.py", "pipeline"]
    assert "--source-torrent-file" in resume_argv["resume-source-torrent"]
    assert "--client default" in resume_commands["resume-source-torrent"]
    assert "--qbit-category SOURCE" in resume_commands["resume-source-torrent"]
    assert "--qbit-tags source-tag" in resume_commands["resume-source-torrent"]
    assert "--paused" in resume_commands["resume-source-torrent"]
    assert "--wait-timeout 7200" in resume_commands["resume-source-torrent"]
    assert "--wait-interval 45" in resume_commands["resume-source-torrent"]
    assert config_arg in resume_commands["resume-source-torrent"]
    assert base_dir_arg in resume_commands["resume-source-torrent"]
    assert summary_output_arg in resume_commands["resume-source-torrent"]
    assert str(torrent_file) in resume_commands["resume-target-upload"]
    assert str(tmp_path / "MTEAM-999.torrent") in resume_commands["resume-uploaded-torrent"]
    assert resume_argv["resume-target-upload"][:3] == ["python3", "ptcli.py", "pipeline"]
    assert resume_argv["resume-uploaded-torrent"][:3] == ["python3", "ptcli.py", "pipeline"]
    assert "--upload-target" in resume_argv["resume-uploaded-torrent"]
    assert str(tmp_path / "MTEAM-999.torrent") in resume_argv["resume-uploaded-torrent"]
    assert "--inject-uploaded-torrent" in resume_argv["resume-uploaded-torrent"]
    assert "--wait-uploaded-complete" in resume_argv["resume-uploaded-torrent"]
    assert shlex.quote(summary_payload["artifacts"]["target_package_dir"]) in resume_commands["resume-target-upload"]
    assert summary_payload["artifacts"]["uploaded_save_path"] == "/downloads"
    assert "--uploaded-save-path /downloads" in resume_commands["resume-target-upload"]
    assert "--uploaded-qbit-category MTEAM" in resume_commands["resume-target-upload"]
    assert "--uploaded-qbit-tags retorrent" in resume_commands["resume-target-upload"]
    assert "--uploaded-paused" in resume_commands["resume-target-upload"]
    assert "--uploaded-wait-timeout 900" in resume_commands["resume-target-upload"]
    assert "--uploaded-wait-interval 20" in resume_commands["resume-target-upload"]
    assert config_arg in resume_commands["resume-target-upload"]
    assert base_dir_arg in resume_commands["resume-target-upload"]
    assert summary_output_arg in resume_commands["resume-target-upload"]
    assert "--client default" in resume_commands["resume-uploaded-torrent"]
    assert "--uploaded-save-path /downloads" in resume_commands["resume-uploaded-torrent"]
    assert "--uploaded-qbit-category MTEAM" in resume_commands["resume-uploaded-torrent"]
    assert "--uploaded-qbit-tags retorrent" in resume_commands["resume-uploaded-torrent"]
    assert "--uploaded-paused" in resume_commands["resume-uploaded-torrent"]
    assert "--uploaded-wait-timeout 900" in resume_commands["resume-uploaded-torrent"]
    assert "--uploaded-wait-interval 20" in resume_commands["resume-uploaded-torrent"]
    assert config_arg in resume_commands["resume-uploaded-torrent"]
    assert base_dir_arg in resume_commands["resume-uploaded-torrent"]
    assert summary_output_arg in resume_commands["resume-uploaded-torrent"]
    assert any(stage["stage"] == "target-upload" and stage["ok"] is True for stage in summary_payload["stages"])


@pytest.mark.asyncio
async def test_pipeline_closure_complete_for_chd_to_mteam_nexus_flow(monkeypatch, tmp_path) -> None:
    config = {
        "DEFAULT": {"default_torrent_client": "qbittorrent"},
        "TRACKERS": {"CHD": {"passkey": "chd-passkey"}, "MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    cookies_dir = tmp_path / "data" / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "CHD.txt").write_text("uid=1;", encoding="utf-8")
    source_torrent = tmp_path / "source-out" / "CHD-2468.torrent"
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")
    target_torrent = make_mteam_safe_torrent(tmp_path, "target")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    patch_pipeline_live_material_stages(monkeypatch)
    uploaded_hash: str | None = None

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(
            tracker,
            source_id,
            (1234567, 2, "CHD.Reference.2024.1080p.BluRay-GROUP", source_hash, "desc"),
            {"douban_id": "1291546"},
        )

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (output_dir, base_dir)
        assert tracker == "CHD"
        assert source_id == "2468"
        return source_torrent

    async def fake_search_mteam_duplicates(_config, source_info):
        assert source_info["tracker"] == "CHD"
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {
            "client": client_name,
            "complete": True,
            "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval},
            "matches": [{"content_path": content_path or "/downloads/CHD.Reference.2024", "hash": torrent_hash or source_hash}],
        }

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": source_hash}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        nonlocal uploaded_hash
        uploaded_path = tmp_path / "MTEAM-2468.torrent"
        uploaded_hash = write_valid_torrent(uploaded_path, tmp_path / "uploaded-content" / "MTEAM-2468.mkv")
        return {"status": "uploaded", "uploaded_torrent_hash": uploaded_hash, "downloaded_torrent": {"torrent_id": "2468", "path": str(uploaded_path)}}

    calls = {"inject": 0}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        calls["inject"] += 1
        injected_hash = uploaded_hash if calls["inject"] > 1 and uploaded_hash is not None else source_hash
        return {
            "hash": injected_hash,
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
            "visible_in_client": True,
            "verified_in_client": True,
        }

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "download_source_torrent", fake_download_source_torrent)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "upload_mteam_from_package", fake_upload_mteam_from_package)
    monkeypatch.setattr(ptcli_cli, "_inject_source_with_config", fake_inject_source_with_config)
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "--from",
            "CHD",
            "--source-id",
            "2468",
            "--to",
            "MTEAM",
            "--base-dir",
            str(tmp_path),
            "--download-source",
            "--inject-source",
            "--save-path",
            "/downloads/CHD.Reference.2024",
            "--wait-complete",
            "--check-dupes",
            "--prepare-target",
            "--target-output-dir",
            str(tmp_path / "target"),
            "--accept-rules",
            "--upload-target",
            "--target-torrent-file",
            target_torrent,
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

    assert payload["status"] == "ok"
    assert payload["ready"] is True
    assert payload["complete"] is True
    assert payload["closure"]["complete"] is True
    assert payload["closure"]["blockers"] == []
    assert payload["closure"]["source"]["downloaded"] is True
    assert payload["closure"]["source"]["injected"] is True
    assert payload["closure"]["source"]["complete"] is True
    assert payload["closure"]["source"]["torrent_hash"] == source_hash
    assert payload["closure"]["target"]["uploaded_torrent_hash"] == uploaded_hash
    assert payload["closure"]["target"]["seeding"] is True
    assert payload["evidence"]["target"]["materials_ready"] is True
    assert payload["evidence"]["target"]["preparation_audit"]["description"]["has_ptgen_description"] is True
    assert payload["evidence"]["target"]["preparation_audit"]["description"]["has_external_ids"] is True
    assert payload["evidence"]["target"]["preparation_audit"]["description"]["has_screenshot_bbcode"] is True
    assert calls["inject"] == 2
    summary_payload = json.loads(await asyncio.to_thread(Path(payload["summary_file"]).read_text, encoding="utf-8"))
    assert summary_payload["source_tracker"] == "CHD"
    assert summary_payload["input_source_id"] == "2468"
    assert summary_payload["source_torrent_id"] == "2468"
    assert summary_payload["closure_review"]["complete"] is True
    assert summary_payload["closure_review"]["source"]["torrent_hash"] == source_hash
    assert summary_payload["closure_review"]["target"]["materials_ready"] is True
    assert summary_payload["closure_review"]["target"]["metadata_ready"] is True
    assert summary_payload["closure_review"]["target"]["assets_ready"] is True
    assert summary_payload["closure_review"]["target"]["description_ready"] is True
    assert summary_payload["closure_review"]["target"]["uploaded_torrent_hash"] == uploaded_hash
    assert summary_payload["artifacts"]["source_torrent_file"].endswith("CHD-2468.torrent")
    assert summary_payload["artifacts"]["target_rule_obligations"]["ready"] is True
    assert summary_payload["resume_state"]["complete"] is True
    resume_commands = {command["stage"]: command["command"] for command in summary_payload["resume_commands"]}
    assert "--from CHD" in resume_commands["resume-source-torrent"]
    assert "--source-id 2468" in resume_commands["resume-source-torrent"]
    assert "--from CHD" in resume_commands["resume-target-upload"]
    assert "--source-id 2468" in resume_commands["resume-target-upload"]
    diagnostics = ptcli_cli._summary_check_diagnostics(summary_payload)
    assert diagnostics["completion_matrix"]["ready"] is True
    assert diagnostics["completion_matrix"]["domains"]["materials"]["evidence"]["ready_for_mteam_upload"] is True
    assert diagnostics["completion_matrix"]["domains"]["target_upload"]["evidence"]["ready_for_uploaded_seeding"] is True


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
    patch_pipeline_live_material_stages(monkeypatch)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", "a" * 40, "desc"), {})

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {
            "client": client_name,
            "complete": True,
            "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval},
            "matches": [{"content_path": content_path, "hash": torrent_hash or "a" * 40}],
        }

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        return {"status": "uploaded", "uploaded_torrent_id": "999"}

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "search_mteam_duplicates", fake_search_mteam_duplicates)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
    monkeypatch.setattr(ptcli_cli, "_wait_complete_with_config", fake_wait_complete_with_config)
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
            "--wait-uploaded-complete",
            "--uploaded-wait-timeout",
            "900",
            "--uploaded-wait-interval",
            "20",
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
    assert payload["resume_state"]["next_command_argv"][:3] == ["python3", "ptcli.py", "pipeline"]
    summary_payload = json.loads(await asyncio.to_thread(Path(payload["summary_file"]).read_text, encoding="utf-8"))
    assert summary_payload["output_options"]["uploaded_output_dir"] == uploaded_output_dir
    assert summary_payload["artifacts"]["uploaded_torrent_id"] == "999"
    assert "uploaded_torrent_file" not in summary_payload["artifacts"]
    resume_commands = {command["stage"]: command["command"] for command in summary_payload["resume_commands"]}
    resume_argv = {command["stage"]: command["argv"] for command in summary_payload["resume_commands"]}
    assert summary_payload["resume_state"]["resume_available"] is True
    assert summary_payload["resume_state"]["next_stage"] == "resume-uploaded-torrent-download"
    assert summary_payload["resume_state"]["next_command"] == resume_commands["resume-uploaded-torrent-download"]
    assert summary_payload["resume_state"]["next_command_argv"] == resume_argv["resume-uploaded-torrent-download"]
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_id"] is True
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_file"] is False
    assert "--uploaded-torrent-id 999" in resume_commands["resume-uploaded-torrent-download"]
    assert resume_argv["resume-uploaded-torrent-download"][:3] == ["python3", "ptcli.py", "pipeline"]
    assert "--upload-target" in resume_argv["resume-uploaded-torrent-download"]
    assert "999" in resume_argv["resume-uploaded-torrent-download"]
    assert "--download-uploaded-torrent" in resume_commands["resume-uploaded-torrent-download"]
    assert f"--uploaded-output-dir {shlex.quote(uploaded_output_dir)}" in resume_commands["resume-uploaded-torrent-download"]
    assert "--inject-uploaded-torrent" in resume_commands["resume-uploaded-torrent-download"]
    assert "--uploaded-save-path /downloads/Name" in resume_commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-category MTEAM" in resume_commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-tags retorrent" in resume_commands["resume-uploaded-torrent-download"]
    assert "--uploaded-paused" in resume_commands["resume-uploaded-torrent-download"]
    assert "--wait-uploaded-complete" in resume_commands["resume-uploaded-torrent-download"]
    assert "--uploaded-wait-timeout 900" in resume_commands["resume-uploaded-torrent-download"]
    assert "--uploaded-wait-interval 20" in resume_commands["resume-uploaded-torrent-download"]
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
    source_torrent = tmp_path / "source-out" / "U2-60635.torrent"
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    patch_pipeline_live_material_stages(monkeypatch)
    uploaded_hash: str | None = None

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        return source_torrent

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        injected_hash = uploaded_hash if "MTEAM" in str(torrent_path) and uploaded_hash else source_hash
        return {
            "hash": injected_hash,
            "torrent_path": torrent_path,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "paused": paused,
            "visible_in_client": True, "verified_in_client": True,
        }

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, content_path, torrent_hash, timeout, interval)
        matched_hash = uploaded_hash if torrent_hash == uploaded_hash else source_hash
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": "/downloads/Name", "hash": matched_hash}]}

    async def fake_search_mteam_duplicates(_config, source_info):
        return {"searched": True, "query": {"imdb": f"tt{source_info['imdb_id']}"}, "count": 0, "dupes": []}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": source_hash}]}

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        nonlocal uploaded_hash
        uploaded_path = tmp_path / "MTEAM-999.torrent"
        uploaded_hash = write_valid_torrent(uploaded_path, tmp_path / "uploaded-content" / "MTEAM-999.mkv")
        return {
            "status": "uploaded",
            "uploaded_torrent_hash": uploaded_hash,
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
    uploaded_path = tmp_path / "MTEAM-999.torrent"
    uploaded_hash = write_valid_torrent(uploaded_path, tmp_path / "uploaded-content" / "MTEAM-999.mkv")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    patch_pipeline_live_material_stages(monkeypatch)

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
        return {"status": "uploaded", "torrent_file": torrent_file, "uploaded_torrent_hash": uploaded_hash, "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_path)}}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        _ = (torrent_path, category, tags, paused)
        return {"hash": uploaded_hash, "save_path": save_path, "visible_in_client": True, "verified_in_client": True}

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
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_material_ready_mteam_package(source_info, tmp_path, output_dir=str(tmp_path / "target"))
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
        "TRACKERS": {"MTEAM": {"api_key": "mteam-api"}},
        "TORRENT_CLIENTS": {"qbittorrent": {"torrent_client": "qbit"}},
    }
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_material_ready_mteam_package(source_info, tmp_path, output_dir=str(tmp_path / "target"))
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")
    uploaded_hash = str(Torrent.read(str(uploaded_torrent), validate=False).infohash)
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = (_config, tracker, source_id, base_dir)
        raise AssertionError("uploaded torrent package resume must not fetch source metadata")

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        raise AssertionError("live upload must not run when --uploaded-torrent-file is provided")

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        return {"hash": uploaded_hash, "torrent_path": torrent_path, "save_path": save_path, "category": category, "tags": tags, "paused": paused, "visible_in_client": True, "verified_in_client": True}

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
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    flow_stage = next(stage for stage in payload["stages"] if stage["stage"] == "flow-check")
    source_info_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-info")
    match_stage = next(stage for stage in payload["stages"] if stage["stage"] == "match")
    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert payload["path"] == "/downloads/Example"
    assert flow_stage["ok"] is True
    assert flow_stage.get("skipped") is True
    assert source_info_stage["ok"] is True
    assert source_info_stage.get("skipped") is True
    assert source_info_stage["result"]["torrent_id"] == "60635"
    assert match_stage["ok"] is True
    assert match_stage.get("skipped") is not True
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["downloaded_torrent"]["reused"] is True
    assert upload_stage["result"]["downloaded_torrent"]["path"] == str(uploaded_torrent)
    assert upload_stage["result"]["uploaded_torrent_hash"] == uploaded_hash
    assert upload_stage["result"]["uploaded_wait"]["complete"] is True
    assert payload["closure"]["complete"] is True
    assert payload["closure"]["blockers"] == []
    assert payload["closure"]["target"]["uploaded"] is True
    assert payload["closure"]["target"]["injected"] is True
    assert payload["closure"]["target"]["seeding"] is True
    assert payload["closure"]["target"]["duplicate_clean"] is True
    assert payload["closure"]["target"]["fresh_duplicate_check"]["source"] == "target_package_upload_gate"
    assert payload["evidence"]["resume"]["target_package"] is True
    assert payload["evidence"]["resume"]["uploaded_torrent_file"] is True
    assert payload["evidence"]["target"]["mode"] == "resumed_uploaded_torrent"
    assert payload["evidence"]["target"]["duplicate_clean"] is True
    assert payload["requested_actions"]["uploaded_torrent_file"] is True
    assert payload["requested_actions"]["uploaded_torrent_id"] is False
    assert payload["effective_actions"]["inject_uploaded_torrent"] is True
    assert payload["effective_actions"]["wait_uploaded_complete"] is True
    assert payload["summary"]["resume"]["used"] is True


@pytest.mark.asyncio
async def test_pipeline_uploaded_torrent_file_resume_auto_waits_when_inject_requested(monkeypatch, tmp_path) -> None:
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
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_material_ready_mteam_package(source_info, tmp_path, output_dir=str(tmp_path / "target"))
    uploaded_torrent = make_mteam_safe_torrent(tmp_path, "uploaded-resume")
    uploaded_hash = str(Torrent.read(str(uploaded_torrent), validate=False).infohash)
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, source_info["name"], "a" * 40, "desc"), {})

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        return {"hash": uploaded_hash, "torrent_path": torrent_path, "save_path": save_path, "category": category, "tags": tags, "paused": paused, "visible_in_client": True, "verified_in_client": True}

    async def fake_wait_complete_with_config(_config, client_name, content_path, torrent_hash, timeout, interval):
        return {
            "client": client_name,
            "complete": True,
            "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval},
            "matches": [{"hash": torrent_hash, "content_path": content_path}],
        }

    monkeypatch.setattr(ptcli_cli, "fetch_source_info", fake_fetch_source_info)
    monkeypatch.setattr(ptcli_cli, "_match_with_config", fake_match_with_config)
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
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    upload_stage = next(stage for stage in payload["stages"] if stage["stage"] == "target-upload")
    assert upload_stage["ok"] is True
    assert upload_stage["result"]["uploaded_wait"]["complete"] is True
    assert payload["closure"]["complete"] is True
    assert payload["effective_actions"]["inject_uploaded_torrent"] is True
    assert payload["effective_actions"]["wait_uploaded_complete"] is True


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
    uploaded_hash = write_valid_torrent(uploaded_torrent, tmp_path / "uploaded-content" / "MTEAM-999.mkv")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, source_info["name"], "a" * 40, "desc"), {})

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        raise AssertionError("live upload must not run when --uploaded-torrent-id is provided")

    async def fake_download_mteam_uploaded_torrent(_config, torrent_id, output_dir):
        _ = output_dir
        assert torrent_id == "999"
        return {"status": "uploaded", "uploaded_torrent_id": torrent_id, "uploaded_torrent_hash": uploaded_hash, "downloaded_torrent": {"torrent_id": torrent_id, "path": str(uploaded_torrent)}}

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": "a" * 40}]}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        return {"hash": uploaded_hash, "torrent_path": torrent_path, "save_path": save_path, "category": category, "tags": tags, "paused": paused, "visible_in_client": True, "verified_in_client": True}

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
            "--uploaded-qbit-category",
            "MTEAM",
            "--uploaded-qbit-tags",
            "retorrent",
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
    assert payload["evidence"]["target"]["mode"] == "resumed_uploaded_id"
    assert payload["summary"]["target"]["mode"] == "resumed_uploaded_id"
    assert payload["requested_actions"]["uploaded_torrent_id"] is True
    assert payload["requested_actions"]["uploaded_torrent_file"] is False
    assert payload["effective_actions"]["download_uploaded_torrent"] is True
    assert payload["effective_actions"]["inject_uploaded_torrent"] is True
    assert payload["effective_actions"]["wait_uploaded_complete"] is True


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
    uploaded_hash = write_valid_torrent(uploaded_torrent, tmp_path / "uploaded-content" / "MTEAM-999.mkv")
    torrent_file.write_bytes(b"d4:infod")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    patch_pipeline_live_material_stages(monkeypatch)

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
        return {"status": "uploaded", "torrent_file": torrent_file_arg, "uploaded_torrent_hash": uploaded_hash, "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_torrent)}}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        _ = (torrent_path, category, tags, paused)
        return {"hash": uploaded_hash, "save_path": save_path, "visible_in_client": True, "verified_in_client": True}

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
    assert payload["evidence"]["target"]["mode"] == "live_upload"
    assert payload["summary"]["target"]["mode"] == "live_upload"


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
async def test_pipeline_target_execute_does_not_fallback_to_match_after_wait_failure(monkeypatch, tmp_path) -> None:
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
        return {
            "client": client_name,
            "complete": False,
            "query": {"torrent_hash": torrent_hash, "content_path": content_path, "timeout": timeout, "interval": interval},
            "matches": [{"hash": source_hash, "content_path": content_path}],
            "blockers": ["qBittorrent matched the torrent but did not report it as complete before timeout."],
        }

    async def fake_match_with_config(_config, _client_name, content_path):
        return {"client": "qbittorrent", "path": content_path, "count": 1, "matches": [{"content_path": content_path, "hash": source_hash}]}

    async def fake_target_upload_with_config(*_args, **_kwargs):
        raise AssertionError("live target upload must not run after source completion wait fails")

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
    assert payload["closure"]["source"]["matched"] is True
    assert payload["closure"]["source"]["ready"] is False
    assert "wait-complete: qBittorrent matched the torrent but did not report it as complete before timeout." in payload["blockers"]
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
    raw_torrent = tmp_path / "raw.mteam-upload.torrent"
    sanitized_torrent = tmp_path / "exported" / "raw.mteam-upload.torrent"
    uploaded_path = tmp_path / "MTEAM-999.torrent"
    uploaded_hash = write_valid_torrent(uploaded_path, tmp_path / "uploaded-content" / "MTEAM-999.mkv")
    raw_torrent.write_bytes(b"d4:infod")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    patch_pipeline_live_material_stages(monkeypatch)

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
        return {"status": "uploaded", "torrent_file": torrent_file, "uploaded_torrent_hash": uploaded_hash, "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_path)}}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        _ = (torrent_path, category, tags, paused)
        return {"hash": uploaded_hash, "save_path": save_path, "visible_in_client": True, "verified_in_client": True}

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
    uploaded_hash = write_valid_torrent(uploaded_torrent, tmp_path / "uploaded-content" / "MTEAM-999.mkv")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    patch_pipeline_live_material_stages(monkeypatch)

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
        return {"status": "uploaded", "torrent_file": torrent_file, "uploaded_torrent_hash": uploaded_hash, "downloaded_torrent": {"torrent_id": "999", "path": str(uploaded_torrent)}}

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        _ = (torrent_path, category, tags, paused)
        return {"hash": uploaded_hash, "save_path": save_path, "visible_in_client": True, "verified_in_client": True}

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
    source_hash = write_valid_torrent(source_torrent, tmp_path / "source-content" / "Name.mkv")
    uploaded_hash = write_valid_torrent(uploaded_torrent, tmp_path / "uploaded-content" / "MTEAM-999.mkv")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: config)
    patch_pipeline_live_material_stages(monkeypatch)

    async def fake_fetch_source_info(_config, tracker, source_id, base_dir=None):
        _ = base_dir
        return source_info_from_tuple(tracker, source_id, (1234567, 2, "Name.2024.1080p.WEB-DL-GROUP", source_hash, "desc"), {})

    async def fake_download_source_torrent(_config, tracker, source_id, output_dir, base_dir=None):
        _ = (tracker, source_id, output_dir, base_dir)
        return source_torrent

    async def fake_inject_source_with_config(_config, _client_name, torrent_path, save_path, category, tags, paused):
        _ = (category, tags, paused)
        if torrent_path == str(source_torrent):
            return {"hash": source_hash, "torrent_path": torrent_path, "save_path": save_path, "visible_in_client": True, "verified_in_client": True}
        return {"hash": uploaded_hash, "torrent_path": torrent_path, "save_path": save_path, "visible_in_client": True, "verified_in_client": True}

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
        "ptgen_description": "[img]https://poster.example/poster.jpg[/img]\n◎译　　名　示例电影\n◎简　　介　示例简介",
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
    assert "[b]Movie information[/b]" in description
    assert "[b]External links[/b]" in description
    assert "IMDb: https://www.imdb.com/title/tt1234567" in description
    assert "TMDb: https://www.themoviedb.org/movie/999" in description
    assert "Douban: https://movie.douban.com/subject/1291546/" in description
    assert "[img]https://poster.example/poster.jpg[/img]" in description
    assert "![](https://poster.example/poster.jpg)" not in description
    assert "◎简　　介　示例简介" in description
    assert "Source tracker: U2" in description
    assert gate["ready"] is True
    assert gate["blockers"] == []


def test_mteam_description_draft_includes_material_screenshots(tmp_path) -> None:
    image_host_file = tmp_path / "image-host-uploads.json"
    image_host_file.write_text(
        json.dumps({"items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}]}),
        encoding="utf-8",
    )
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    mediainfo = tmp_path / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "torrenthash": "a" * 40,
    }
    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example")
    materials = build_mteam_materials_manifest(
        preview,
        source_info,
        "/downloads/Example",
        material_files={"mediainfo_file": str(mediainfo), "screenshot_files": [str(screenshot)], "image_host_file": str(image_host_file)},
    )

    description = build_mteam_description_draft(preview["meta_draft"], source_info, materials=materials)

    assert "[b]Media materials[/b]" in description
    assert "MediaInfo: ready" in description
    assert "[b]MediaInfo[/b]" in description
    assert "Complete name : Example.mkv" in description
    assert "[url=https://img.example/page][img]https://img.example/thumb.png[/img][/url]" in description


def test_mteam_description_draft_accepts_url_only_image_host_items(tmp_path) -> None:
    image_host_file = tmp_path / "image-host-uploads.json"
    image_host_file.write_text(json.dumps([{"url": "https://img.example/screen-1.png"}]), encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    mediainfo = tmp_path / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "torrenthash": "a" * 40,
    }
    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example")
    materials = build_mteam_materials_manifest(
        preview,
        source_info,
        "/downloads/Example",
        material_files={"mediainfo_file": str(mediainfo), "screenshot_files": [str(screenshot)], "image_host_file": str(image_host_file)},
    )

    description = build_mteam_description_draft(preview["meta_draft"], source_info, materials=materials)

    assert "[url=https://img.example/screen-1.png][img]https://img.example/screen-1.png[/img][/url]" in description


def test_mteam_description_draft_uses_tmdb_tv_link_for_series() -> None:
    source_info = {
        "tracker": "CHD",
        "torrent_id": "12345",
        "name": "Example.Show.S01.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 7654321,
        "tmdb_id": 321,
        "douban_id": "26752088",
        "torrenthash": "b" * 40,
        "ptgen_description": "◎译　　名　示例剧集\n◎简　　介　示例简介",
    }
    meta_draft = build_mteam_meta_draft(source_info, "/downloads/Example.Show.S01")

    description = build_mteam_description_draft(meta_draft, source_info)

    assert meta_draft["category"] == "TV"
    assert "TMDb: https://www.themoviedb.org/tv/321" in description


def test_mteam_materials_manifest_tracks_metadata_and_missing_assets() -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "metadata_enrichment": {
            "status": "enriched",
            "ready": False,
            "sources": ["source", "overrides"],
            "applied": {"douban_url": "https://movie.douban.com/subject/1291546/"},
            "missing": [],
            "readiness": {
                "imdb_id": {"ready": True, "required": True, "source": "source"},
                "tmdb_id": {"ready": True, "required": True, "source": "source"},
                "douban_id": {"ready": True, "required": True, "source": "source"},
                "douban_url": {"ready": True, "required": True, "source": "overrides"},
                "ptgen_description": {"ready": False, "required": True, "source": None},
            },
            "blockers": [],
            "readiness_blockers": ["PTGen/Douban description is missing after enrichment."],
        },
    }
    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example")

    materials = build_mteam_materials_manifest(preview, source_info, "/downloads/Example")

    metadata_checks = {check["name"]: check for check in materials["checks"]["metadata"]}
    asset_checks = {check["name"]: check for check in materials["checks"]["assets"]}
    assert metadata_checks["imdb"]["ok"] is True
    assert metadata_checks["tmdb"]["ok"] is True
    assert metadata_checks["douban"]["ok"] is True
    assert metadata_checks["ptgen_description"]["ok"] is False
    assert asset_checks["mediainfo_or_bdinfo"]["ok"] is False
    assert asset_checks["screenshots"]["ok"] is False
    assert asset_checks["image_host_uploads"]["ok"] is False
    assert materials["metadata"]["source_description_available"] is True
    assert materials["metadata"]["ptgen_description_length"] == 0
    assert materials["metadata"]["enrichment_status"] == "enriched"
    assert materials["metadata"]["enrichment_ready"] is False
    assert materials["metadata"]["sources"] == ["source", "overrides"]
    assert materials["metadata"]["applied"] == {"douban_url": "https://movie.douban.com/subject/1291546/"}
    assert materials["metadata"]["readiness"]["ptgen_description"] == {"ready": False, "required": True, "source": None}
    assert materials["metadata"]["readiness_blockers"] == ["PTGen/Douban description is missing after enrichment."]
    assert materials["ready"] is False
    assert materials["missing"] == ["metadata.ptgen_description", "assets.mediainfo_or_bdinfo", "assets.screenshots", "assets.image_host_uploads"]
    assert "Fetch or supply IMDb/TMDb/Douban metadata" in materials["next_actions"][0]
    assert "Generate MediaInfo or BDInfo" in materials["next_actions"][1]


def test_mteam_materials_manifest_accepts_ptgen_description() -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example")

    materials = build_mteam_materials_manifest(preview, source_info, "/downloads/Example")

    metadata_checks = {check["name"]: check for check in materials["checks"]["metadata"]}
    assert metadata_checks["ptgen_description"]["ok"] is True
    assert materials["metadata"]["ptgen_description_length"] == len(source_info["ptgen_description"])


def test_mteam_materials_manifest_records_existing_material_files(tmp_path) -> None:
    mediainfo = tmp_path / "MEDIAINFO.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_hosts = tmp_path / "image-host.json"
    image_hosts.write_text(json.dumps({"items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png"}]}), encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example")

    materials = build_mteam_materials_manifest(
        preview,
        source_info,
        "/downloads/Example",
        material_files={
            "mediainfo_file": str(mediainfo),
            "screenshot_files": [str(screenshot)],
            "image_host_file": str(image_hosts),
        },
    )

    asset_checks = {check["name"]: check for check in materials["checks"]["assets"]}
    assert asset_checks["mediainfo_or_bdinfo"]["ok"] is True
    assert asset_checks["screenshots"]["ok"] is True
    assert asset_checks["image_host_uploads"]["ok"] is True
    assert materials["assets"]["mediainfo"]["sha1"]
    assert materials["assets"]["screenshots"]["count"] == 1
    assert materials["assets"]["image_hosts"]["count"] == 1
    assert materials["ready"] is True
    assert materials["missing"] == []


def test_mteam_materials_manifest_requires_usable_image_host_urls(tmp_path) -> None:
    mediainfo = tmp_path / "MEDIAINFO.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_hosts = tmp_path / "image-host.json"
    image_hosts.write_text(json.dumps({"items": [{"local_file": str(screenshot)}]}), encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example")

    materials = build_mteam_materials_manifest(
        preview,
        source_info,
        "/downloads/Example",
        material_files={
            "mediainfo_file": str(mediainfo),
            "screenshot_files": [str(screenshot)],
            "image_host_file": str(image_hosts),
        },
    )

    asset_checks = {check["name"]: check for check in materials["checks"]["assets"]}
    assert asset_checks["image_host_uploads"]["ok"] is False
    assert asset_checks["image_host_uploads"]["message"] == "Screenshot image-host upload results are missing usable image URLs."
    assert materials["assets"]["image_hosts"]["count"] == 1
    assert materials["assets"]["image_hosts"]["valid_count"] == 0
    assert materials["assets"]["image_hosts"]["invalid_count"] == 1
    assert materials["ready"] is False


def test_mteam_materials_manifest_rejects_non_web_image_host_urls(tmp_path) -> None:
    mediainfo = tmp_path / "MEDIAINFO.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_hosts = tmp_path / "image-host.json"
    image_hosts.write_text(json.dumps({"items": [{"raw_url": str(screenshot), "img_url": "ftp://img.example/screen.png"}]}), encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example")

    materials = build_mteam_materials_manifest(
        preview,
        source_info,
        "/downloads/Example",
        material_files={
            "mediainfo_file": str(mediainfo),
            "screenshot_files": [str(screenshot)],
            "image_host_file": str(image_hosts),
        },
    )
    description = build_mteam_description_draft(preview["meta_draft"], source_info, materials=materials)

    asset_checks = {check["name"]: check for check in materials["checks"]["assets"]}
    assert asset_checks["image_host_uploads"]["ok"] is False
    assert materials["assets"]["image_hosts"]["valid_count"] == 0
    assert materials["assets"]["image_hosts"]["invalid_count"] == 1
    assert "[img]" not in description
    assert materials["ready"] is False


def test_mteam_description_uses_img_url_when_raw_url_is_missing(tmp_path) -> None:
    image_hosts = tmp_path / "image-host.json"
    image_hosts.write_text(json.dumps({"items": [{"img_url": "https://img.example/thumb.png"}]}), encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example")
    materials = build_mteam_materials_manifest(preview, source_info, "/downloads/Example", material_files={"image_host_file": str(image_hosts)})

    description = build_mteam_description_draft(preview["meta_draft"], source_info, materials=materials)

    assert "[url=https://img.example/thumb.png][img]https://img.example/thumb.png[/img][/url]" in description


def test_mteam_materials_manifest_requires_bdinfo_for_bdmv_content(tmp_path) -> None:
    content = tmp_path / "Disc"
    (content / "BDMV" / "STREAM").mkdir(parents=True)
    mediainfo = tmp_path / "MEDIAINFO.txt"
    mediainfo.write_text("General\nComplete name : 00001.m2ts\n", encoding="utf-8")
    bdinfo = tmp_path / "BD_FULL_00.txt"
    bdinfo.write_text("DISC INFO:\nDisc Title: Example\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_hosts = tmp_path / "image-host.json"
    image_hosts.write_text(json.dumps({"items": [{"raw_url": "https://img.example/raw.png"}]}), encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.BluRay.COMPLETE-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], mteam_ready_stages(), str(content))

    missing_bdinfo = build_mteam_materials_manifest(
        preview,
        source_info,
        str(content),
        material_files={
            "mediainfo_file": str(mediainfo),
            "screenshot_files": [str(screenshot)],
            "image_host_file": str(image_hosts),
        },
    )

    missing_checks = {check["name"]: check for check in missing_bdinfo["checks"]["assets"]}
    assert missing_checks["mediainfo_or_bdinfo"]["ok"] is True
    assert missing_checks["bdinfo_for_disc"]["ok"] is False
    assert missing_bdinfo["assets"]["disc_structure"]["type"] == "BDMV"
    assert missing_bdinfo["ready"] is False
    assert missing_bdinfo["missing"] == ["assets.bdinfo_for_disc"]
    assert any("Provide a BDInfo text file with --bdinfo-file" in action for action in missing_bdinfo["next_actions"])

    ready = build_mteam_materials_manifest(
        preview,
        source_info,
        str(content),
        material_files={
            "bdinfo_file": str(bdinfo),
            "screenshot_files": [str(screenshot)],
            "image_host_file": str(image_hosts),
        },
    )

    ready_checks = {check["name"]: check for check in ready["checks"]["assets"]}
    assert ready_checks["bdinfo_for_disc"]["ok"] is True
    assert ready["assets"]["bdinfo"]["ready"] is True
    assert ready["ready"] is True
    assert ready["missing"] == []


def test_find_primary_media_file_prefers_largest_supported_video(tmp_path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    small = content / "small.mkv"
    small.write_bytes(b"1")
    larger = content / "larger.m2ts"
    larger.write_bytes(b"123")
    hidden_dir = content / ".hidden"
    hidden_dir.mkdir()
    hidden = hidden_dir / "huge.mkv"
    hidden.write_bytes(b"123456")

    assert find_primary_media_file(str(content)) == larger


@pytest.mark.asyncio
async def test_generate_mediainfo_material_writes_files_and_evidence(tmp_path) -> None:
    media_file = tmp_path / "Name.mkv"
    media_file.write_bytes(b"video")

    def fake_parse(path, output, full=False):
        if output == "JSON":
            return json.dumps({"media": {"track": [{"@type": "General"}]}})
        suffix = "full" if full else "summary"
        return f"General\nComplete name : {path}\nKind : {suffix}\n"

    result = await generate_mediainfo_material(str(media_file), str(tmp_path / "materials"), parser=fake_parse)
    full_text = await asyncio.to_thread(Path(result["mediainfo_file"]).read_text, encoding="utf-8")
    json_text = await asyncio.to_thread(Path(result["mediainfo_json_file"]).read_text, encoding="utf-8")

    assert result["status"] == "generated"
    assert result["mediainfo_file"].endswith("MI_FULL_00.txt")
    assert full_text.count(str(media_file)) == 0
    assert await asyncio.to_thread(Path(result["mediainfo_summary_file"]).exists)
    assert json.loads(json_text)["media"]["track"][0]["@type"] == "General"
    assert result["sha1"]
    assert result["size_bytes"] > 0


@pytest.mark.asyncio
async def test_generate_bdinfo_material_writes_full_file_and_evidence(tmp_path) -> None:
    content = tmp_path / "Disc"
    playlist_dir = content / "BDMV" / "PLAYLIST"
    playlist_dir.mkdir(parents=True)
    (playlist_dir / "00001.mpls").write_bytes(b"mpls")

    def fake_runner(command):
        output_dir = Path(command[-1])
        (output_dir / "BDINFO.00001.txt").write_text("DISC INFO:\nDisc Title: Example\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = await generate_bdinfo_material(str(content), str(tmp_path / "materials"), playlist="00001.mpls", runner=fake_runner, bdinfo_binary="/usr/bin/bdinfo")

    assert result["status"] == "generated"
    assert result["playlist"] == "00001.mpls"
    assert result["bdinfo_file"].endswith("BD_FULL_00.txt")
    generated_text = await asyncio.to_thread(Path(result["bdinfo_file"]).read_text, encoding="utf-8")
    assert generated_text.startswith("DISC INFO:")
    assert result["sha1"]
    assert result["command"][:3] == ["/usr/bin/bdinfo", str(content / "BDMV"), "-m"]


@pytest.mark.asyncio
async def test_generate_screenshot_materials_writes_files_and_evidence(tmp_path) -> None:
    media_file = tmp_path / "Name.mkv"
    media_file.write_bytes(b"video")

    def fake_parse(_path, output, full=False):
        _ = full
        assert output == "JSON"
        return json.dumps({"media": {"track": [{"@type": "General", "Duration": "120000"}]}})

    def fake_runner(command):
        output_path = Path(command[-1])
        output_path.write_bytes(b"png")
        return argparse.Namespace(returncode=0, stderr="")

    result = await generate_screenshot_materials(str(media_file), str(tmp_path / "screens"), count=2, parser=fake_parse, runner=fake_runner, ffmpeg_binary="/usr/bin/ffmpeg")

    assert result["status"] == "generated"
    assert result["count"] == 2
    assert len(result["screenshot_files"]) == 2
    assert result["files"][0]["sha1"]
    assert result["files"][0]["timestamp_seconds"] > 0


@pytest.mark.asyncio
async def test_upload_screenshot_image_hosts_writes_upload_json(tmp_path) -> None:
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")

    async def fake_uploader(args):
        image, host, _config, _meta = args
        return {
            "status": "success",
            "img_url": f"https://{host}/thumb/{Path(image).name}",
            "raw_url": f"https://{host}/raw/{Path(image).name}",
            "web_url": f"https://{host}/page/{Path(image).name}",
        }

    result = await upload_screenshot_image_hosts({"DEFAULT": {"img_host_1": "ptpimg"}}, [str(screenshot)], str(tmp_path / "materials"), uploader=fake_uploader)

    assert result["status"] == "uploaded"
    assert result["host"] == "ptpimg"
    assert result["count"] == 1
    assert result["items"][0]["raw_url"] == "https://ptpimg/raw/screen-1.png"
    assert await asyncio.to_thread(Path(result["image_host_file"]).exists)


@pytest.mark.asyncio
async def test_upload_screenshot_image_hosts_blocks_legacy_imgbox_without_extra_dependency(tmp_path) -> None:
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")

    result = await upload_screenshot_image_hosts({"DEFAULT": {"img_host_1": "imgbox"}}, [str(screenshot)], str(tmp_path / "materials"))

    assert result["status"] == "blocked"
    assert result["host"] == "imgbox"
    assert result["count"] == 0
    assert "pyimgbox" in result["blockers"][0]
    assert await asyncio.to_thread(Path(result["image_host_file"]).exists)


def test_normalize_metadata_overrides_accepts_urls_ids_and_ptgen_description(tmp_path) -> None:
    metadata_file = tmp_path / "metadata.json"
    metadata_file.write_text(
        json.dumps(
            {
                "imdb": "tt1234567",
                "tmdb": "999",
                "douban": "https://movie.douban.com/subject/1291546/",
                "ptgen_description": "◎译　　名　示例电影\r\n◎简　　介　示例简介",
            }
        ),
        encoding="utf-8",
    )

    overrides = load_metadata_overrides(str(metadata_file))

    assert overrides == {
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    assert normalize_metadata_overrides({"douban_id": "1291546"})["douban_url"] == "https://movie.douban.com/subject/1291546/"
    assert normalize_metadata_overrides({"ptgen": {"description": "◎片　　名　嵌套示例"}})["ptgen_description"] == "◎片　　名　嵌套示例"


def test_normalize_metadata_overrides_extracts_ids_from_ptgen_description() -> None:
    overrides = normalize_metadata_overrides(
        {
            "ptgen_description": "\n".join(
                [
                    "IMDb: https://www.imdb.com/title/tt1234567/",
                    "TMDb: https://www.themoviedb.org/movie/999",
                    "豆瓣: https://movie.douban.com/subject/1291546/",
                    "◎译　　名　示例电影",
                ]
            )
        }
    )

    assert overrides["imdb_id"] == 1234567
    assert overrides["tmdb_id"] == 999
    assert overrides["douban_id"] == "1291546"
    assert overrides["douban_url"] == "https://movie.douban.com/subject/1291546/"
    assert "◎译　　名　示例电影" in overrides["ptgen_description"]


def test_normalize_metadata_overrides_prefers_explicit_ids_over_ptgen_text() -> None:
    overrides = normalize_metadata_overrides(
        {
            "imdb_id": "tt7654321",
            "tmdb_id": 111,
            "douban_id": "26752088",
            "ptgen_description": "IMDb: tt1234567\nTMDb: https://www.themoviedb.org/tv/999\nDouban: https://movie.douban.com/subject/1291546/",
        }
    )

    assert overrides["imdb_id"] == 7654321
    assert overrides["tmdb_id"] == 111
    assert overrides["douban_id"] == "26752088"
    assert overrides["douban_url"] == "https://movie.douban.com/subject/26752088/"


@pytest.mark.asyncio
async def test_enrich_source_metadata_applies_overrides_without_clobbering_existing() -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "name": "Name",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "douban_id": None,
        "douban_url": None,
    }

    result = await enrich_source_metadata({}, source_info, overrides={"imdb_id": 7654321, "tmdb_id": 999, "douban_id": "1291546"})

    assert result["source_info"]["imdb_id"] == 1234567
    assert result["source_info"]["tmdb_id"] == 999
    assert result["source_info"]["douban_url"] == "https://movie.douban.com/subject/1291546/"
    assert result["applied"]["tmdb_id"] == 999
    assert "imdb_id" not in result["applied"]
    assert result["readiness"]["imdb_id"] == {"ready": True, "required": True, "source": "source"}
    assert result["readiness"]["tmdb_id"] == {"ready": True, "required": True, "source": "overrides"}
    assert result["readiness"]["douban_id"] == {"ready": True, "required": True, "source": "overrides"}
    assert result["readiness"]["douban_url"] == {"ready": True, "required": True, "source": "overrides"}
    assert result["readiness"]["ptgen_description"] == {"ready": False, "required": False, "source": None}


@pytest.mark.asyncio
async def test_enrich_source_metadata_accepts_ptgen_description_override_when_required() -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "name": "Name",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
    }

    result = await enrich_source_metadata({}, source_info, overrides={"ptgen_description": "◎译　　名　示例电影"}, fetch_ptgen=True)

    assert result["ready"] is True
    assert result["source_info"]["ptgen_description"] == "◎译　　名　示例电影"
    assert result["applied"]["ptgen_description"] == "◎译　　名　示例电影"
    assert result["sources"] == ["overrides"]
    assert result["readiness"]["ptgen_description"] == {"ready": True, "required": True, "source": "overrides"}


@pytest.mark.asyncio
async def test_enrich_source_metadata_derives_douban_id_from_existing_url() -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "name": "Name",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "douban_id": None,
        "douban_url": "https://movie.douban.com/subject/1291546/",
    }

    result = await enrich_source_metadata({}, source_info)

    assert result["ready"] is True
    assert result["source_info"]["douban_id"] == "1291546"
    assert result["applied"]["douban_id"] == "1291546"
    assert result["readiness"]["douban_id"] == {"ready": True, "required": True, "source": "source"}


@pytest.mark.asyncio
async def test_enrich_source_metadata_fetches_tmdb_without_legacy_tmdb_manager(monkeypatch) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "name": "Name",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
    }

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"movie_results": [{"id": 999}], "tv_results": []}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            assert kwargs["timeout"] == 30.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url, params):
            assert url == "https://api.themoviedb.org/3/find/tt1234567"
            assert params == {"api_key": "tmdb-key", "external_source": "imdb_id"}
            return FakeResponse()

    monkeypatch.setattr(ptcli_metadata.httpx, "AsyncClient", FakeClient)

    result = await enrich_source_metadata({"DEFAULT": {"tmdb_api": "tmdb-key"}}, source_info)

    assert result["ready"] is True
    assert result["source_info"]["tmdb_id"] == 999
    assert result["sources"] == ["tmdb_api"]
    assert result["applied"]["tmdb_id"] == 999
    assert result["readiness"]["tmdb_id"] == {"ready": True, "required": True, "source": "tmdb_api"}


@pytest.mark.asyncio
async def test_enrich_source_metadata_fetches_imdb_from_tmdb_external_ids(monkeypatch) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "imdb_id": None,
        "tmdb_id": 999,
        "name": "Name",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
    }

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"imdb_id": "tt1234567"}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            assert kwargs["timeout"] == 30.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url, params):
            assert url == "https://api.themoviedb.org/3/movie/999/external_ids"
            assert params == {"api_key": "tmdb-key"}
            return FakeResponse()

    monkeypatch.setattr(ptcli_metadata.httpx, "AsyncClient", FakeClient)

    result = await enrich_source_metadata({"DEFAULT": {"tmdb_api": "tmdb-key"}}, source_info)

    assert result["ready"] is True
    assert result["source_info"]["imdb_id"] == 1234567
    assert result["sources"] == ["tmdb_api"]
    assert result["applied"]["imdb_id"] == 1234567
    assert result["readiness"]["imdb_id"] == {"ready": True, "required": True, "source": "tmdb_api"}


@pytest.mark.asyncio
async def test_enrich_source_metadata_fetches_imdb_from_tmdb_tv_external_ids(monkeypatch) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "imdb_id": None,
        "tmdb_id": 999,
        "name": "Series Name",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
    }
    requested_urls = []

    class FakeResponse:
        def __init__(self, payload, status_code=200) -> None:
            self._payload = payload
            self.status_code = status_code

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            assert kwargs["timeout"] == 30.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url, params):
            assert params == {"api_key": "tmdb-key"}
            requested_urls.append(url)
            if "/movie/" in url:
                return FakeResponse({}, status_code=404)
            return FakeResponse({"imdb_id": "tt7654321"})

    monkeypatch.setattr(ptcli_metadata.httpx, "AsyncClient", FakeClient)

    result = await enrich_source_metadata({"DEFAULT": {"tmdb_api": "tmdb-key"}}, source_info)

    assert requested_urls == [
        "https://api.themoviedb.org/3/movie/999/external_ids",
        "https://api.themoviedb.org/3/tv/999/external_ids",
    ]
    assert result["ready"] is True
    assert result["source_info"]["imdb_id"] == 7654321
    assert result["applied"]["imdb_id"] == 7654321


@pytest.mark.asyncio
async def test_pipeline_metadata_enrichment_stage_blocks_missing_ready_metadata(monkeypatch) -> None:
    args = argparse.Namespace(
        metadata_file=None,
        imdb_id=None,
        tmdb_id=None,
        douban_id=None,
        douban_url=None,
        fetch_ptgen=True,
        base_dir=None,
    )
    source_stage = {
        "stage": "source-info",
        "ok": True,
        "result": {
            "tracker": "U2",
            "torrent_id": "60635",
            "imdb_id": 1234567,
            "tmdb_id": None,
            "douban_id": None,
            "douban_url": None,
            "name": "Name",
            "torrenthash": "a" * 40,
        },
    }

    async def fake_enrich_source_metadata(_config, source_info, *, overrides=None, fetch_ptgen=False, base_dir=None):
        _ = (overrides, fetch_ptgen, base_dir)
        return {
            "status": "unchanged",
            "ready": False,
            "source_info": source_info,
            "applied": {},
            "missing": ["tmdb_id", "douban_id", "douban_url"],
            "sources": [],
            "blockers": [],
        }

    monkeypatch.setattr(ptcli_cli, "enrich_source_metadata", fake_enrich_source_metadata)

    stage = await ptcli_cli._pipeline_metadata_enrichment_stage({}, args, source_stage)

    enrichment = stage["result"]["metadata_enrichment"]
    assert stage["ok"] is False
    assert "Missing metadata after enrichment" in enrichment["readiness_blockers"][0]
    assert "PTGen/Douban description is missing" in enrichment["readiness_blockers"][1]
    assert "tmdb_id" in enrichment["missing"]


def test_pipeline_stage_blockers_include_metadata_enrichment_readiness() -> None:
    blockers = ptcli_cli._pipeline_stage_blockers(
        [
            {
                "stage": "metadata-enrich",
                "ok": False,
                "message": "Metadata enrichment completed with blockers.",
                "result": {
                    "metadata_enrichment": {
                        "readiness_blockers": [
                            "Missing metadata after enrichment: tmdb_id, douban_id, douban_url",
                            "PTGen/Douban description is missing after enrichment.",
                        ],
                    },
                },
            }
        ]
    )
    actions = [ptcli_cli._pipeline_stage_blocker_next_action(blocker) for blocker in blockers]

    assert "metadata-enrich: Missing metadata after enrichment: tmdb_id, douban_id, douban_url" in blockers
    assert "metadata-enrich: PTGen/Douban description is missing after enrichment." in blockers
    assert actions.count("Fetch or supply IMDb/TMDb/Douban metadata and PTGen/Douban description, then rerun target preparation.") == len(actions)


def test_material_generation_artifacts_keep_metadata_blockers_separate() -> None:
    artifacts = ptcli_cli._material_generation_artifacts(
        [
            {
                "stage": "metadata-enrich",
                "ok": False,
                "message": "Metadata enrichment completed with blockers.",
                "result": {
                    "imdb_id": 1234567,
                    "tmdb_id": None,
                    "douban_id": None,
                    "douban_url": None,
                    "metadata_enrichment": {
                        "status": "unchanged",
                        "ready": False,
                        "applied": {},
                        "missing": ["tmdb_id", "douban_id", "douban_url"],
                        "readiness": {"tmdb_id": {"ready": False, "required": True, "source": None}},
                        "sources": [],
                        "blockers": ["TMDb enrichment requires DEFAULT.tmdb_api."],
                        "readiness_blockers": ["Missing metadata after enrichment: tmdb_id, douban_id, douban_url"],
                    },
                },
            }
        ]
    )

    metadata = artifacts["metadata"]
    assert metadata["ok"] is False
    assert metadata["blockers"] == ["TMDb enrichment requires DEFAULT.tmdb_api."]
    assert metadata["readiness_blockers"] == ["Missing metadata after enrichment: tmdb_id, douban_id, douban_url"]
    assert metadata["all_blockers"] == ["TMDb enrichment requires DEFAULT.tmdb_api.", "Missing metadata after enrichment: tmdb_id, douban_id, douban_url"]


def test_material_generation_artifacts_include_file_evidence(tmp_path) -> None:
    mediainfo = tmp_path / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_host = tmp_path / "image-host-uploads.json"
    image_host.write_text(json.dumps({"items": [{"img_url": "https://img.example/1.png"}]}), encoding="utf-8")

    artifacts = ptcli_cli._material_generation_artifacts(
        [
            {"stage": "materials-mediainfo", "ok": True, "result": {"status": "generated", "mediainfo_file": str(mediainfo)}},
            {"stage": "materials-screenshots", "ok": True, "result": {"status": "generated", "screenshot_files": [str(screenshot)], "count": 1}},
            {"stage": "materials-image-host", "ok": True, "result": {"status": "uploaded", "image_host_file": str(image_host)}},
        ]
    )

    mediainfo_evidence = artifacts["mediainfo"]["mediainfo_file_evidence"]
    assert mediainfo_evidence["path"] == str(mediainfo)
    assert mediainfo_evidence["exists"] is True
    assert mediainfo_evidence["is_file"] is True
    assert mediainfo_evidence["size_bytes"] == len(mediainfo.read_bytes())
    assert len(mediainfo_evidence["sha1"]) == 40
    screenshot_evidence = artifacts["screenshots"]["screenshot_files_evidence"]
    assert screenshot_evidence[0]["path"] == str(screenshot)
    assert screenshot_evidence[0]["size_bytes"] == len(screenshot.read_bytes())
    assert len(screenshot_evidence[0]["sha1"]) == 40
    image_host_evidence = artifacts["image_host"]["image_host_file_evidence"]
    assert image_host_evidence["path"] == str(image_host)
    assert image_host_evidence["exists"] is True
    assert len(image_host_evidence["sha1"]) == 40


def test_pipeline_stage_blocker_next_action_explains_bdmv_bdinfo_requirement() -> None:
    action = ptcli_cli._pipeline_stage_blocker_next_action("target.materials.assets.bdinfo_for_disc: BDMV disc content requires --bdinfo-file for MTEAM target preparation.")

    assert action == "Provide BDInfo for BDMV disc content with --bdinfo-file or --generate-bdinfo, then rerun resume-target-package."


def test_pipeline_stage_blocker_next_action_uses_specific_material_recovery() -> None:
    cases = {
        "target.materials.metadata.tmdb: TMDb id is missing.": "Fetch TMDb metadata with --enrich-metadata or supply it with --metadata-file/--tmdb-id, then rerun resume-target-package.",
        "target.materials.metadata.douban: Douban id/url is missing.": "Fetch Douban metadata with --fetch-ptgen or supply it with --metadata-file/--douban-id/--douban-url, then rerun resume-target-package.",
        "target.materials.metadata.ptgen_description: PTGen/Douban description text is missing.": "Fetch PTGen/Douban description with --fetch-ptgen or supply metadata containing ptgen_description, then rerun resume-target-package.",
        "target.materials.assets.mediainfo_or_bdinfo: MediaInfo/BDInfo has not been generated.": "Generate or provide MediaInfo/BDInfo with --generate-mediainfo, --mediainfo-file, --generate-bdinfo, or --bdinfo-file, then rerun resume-target-package.",
        "target.materials.assets.screenshots: Screenshots are missing.": "Generate or provide screenshots with --generate-screenshots or --screenshot-file, then rerun resume-target-package.",
        "target.materials.assets.image_host_uploads: Image host uploads are missing.": "Upload screenshots to an image host with --upload-screenshots/--image-host or provide --image-host-file, then rerun resume-target-package.",
        "target.materials.description.screenshot_coverage: Description has fewer hosted screenshots than local screenshots.": "Upload screenshots to an image host with --upload-screenshots/--image-host or provide --image-host-file, then rerun resume-target-package.",
    }

    for blocker, expected in cases.items():
        assert ptcli_cli._pipeline_stage_blocker_next_action(blocker) == expected


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
    assert preview["metadata"]["tracker"] is None
    assert preview["metadata"]["torrent_id"] is None
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
    assert package["files"]["materials"].endswith("mteam-materials.json")
    assert package["files"]["description_draft"].endswith("mteam-description-draft.txt")
    assert package["files"]["rule_review"].endswith("mteam-rule-review.json")
    assert package["files"]["upload_gate"].endswith("mteam-upload-gate.json")
    assert package["files"]["manifest"].endswith("mteam-package-manifest.json")
    assert package["package_manifest"]["schema_version"] == 1
    assert package["package_manifest"]["kind"] == "ptcli.mteam.prepare_package"
    assert package["package_manifest"]["source"]["tracker"] == "U2"
    assert package["package_manifest"]["source"]["torrent_id"] == "60635"
    assert package["metadata"]["tracker"] == "U2"
    assert package["metadata"]["torrent_id"] == "60635"
    assert package["package_manifest"]["files"]["preview"]["sha1"]
    assert package["package_manifest"]["files"]["materials"]["sha1"]
    assert package["materials"]["kind"] == "ptcli.mteam.materials"
    assert package["materials"]["assets"]["mediainfo"]["ready"] is False
    assert package["materials"]["assets"]["screenshots"]["ready"] is False
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
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-materials.json").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-description-draft.txt").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-rule-review.json").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-package-manifest.json").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-field-mapping.json").read_text(encoding="utf-8").strip().startswith("{")
    loaded = load_mteam_prepare_package(package["package_dir"])
    assert loaded["files"]["manifest"].endswith("mteam-package-manifest.json")
    assert loaded["files"]["materials"].endswith("mteam-materials.json")
    assert loaded["materials"]["kind"] == "ptcli.mteam.materials"
    assert loaded["package_manifest"]["ready"] is package["package_manifest"]["ready"]
    assert loaded["package_manifest"]["source"]["tracker"] == "U2"
    assert loaded["preview"]["metadata"]["torrent_id"] == "60635"


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


def test_mteam_upload_torrent_candidate_summary_verifies_metadata_not_suffix_only(tmp_path) -> None:
    content = tmp_path / "Example.mkv"
    content.write_bytes(b"content")
    unsafe_torrent = tmp_path / "unsafe.mteam-upload.torrent"
    torrent = Torrent(path=str(content), trackers=["https://source.example/passkey/announce"], comment="private comment")
    torrent.generate()
    torrent.write(str(unsafe_torrent), overwrite=True)
    safe_torrent = make_mteam_safe_torrent(tmp_path, "safe")

    unsafe_summary = mteam_upload_torrent_candidate_summary(str(unsafe_torrent))
    safe_summary = mteam_upload_torrent_candidate_summary(str(safe_torrent))

    assert unsafe_summary["has_mteam_upload_suffix"] is True
    assert unsafe_summary["metadata_readable"] is True
    assert unsafe_summary["mteam_safe"] is False
    assert safe_summary["has_mteam_upload_suffix"] is True
    assert safe_summary["metadata_readable"] is True
    assert safe_summary["mteam_safe"] is True


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
    material_dir = tmp_path / "materials"
    material_dir.mkdir()
    mediainfo = material_dir / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    screenshot = material_dir / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_host_file = material_dir / "image-host-uploads.json"
    image_host_file.write_text(
        json.dumps({"items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}]}),
        encoding="utf-8",
    )
    package = write_mteam_prepare_package(
        source_info,
        ["MTEAM"],
        mteam_ready_stages(),
        "/downloads/Example",
        str(tmp_path),
        accept_rules=True,
        material_files={"mediainfo_file": str(mediainfo), "screenshot_files": [str(screenshot)], "image_host_file": str(image_host_file)},
    )
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
    assert torrent_summary["extra_top_level_fields"] == []
    assert torrent_summary["announce_list_present"] is False
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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


def test_mteam_upload_preflight_blocks_extra_torrent_metadata(tmp_path) -> None:
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
    safe_torrent = Path(make_mteam_safe_torrent(tmp_path, "unsafe-extra"))
    torrent = Torrent.read(str(safe_torrent), validate=False)
    torrent.metainfo["announce-list"] = [["https://source.example/passkey/announce"]]
    torrent.metainfo["publisher-url"] = "https://source.example/details.php?id=1"
    torrent.write(str(safe_torrent), overwrite=True)

    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(safe_torrent))

    torrent_summary = preflight["upload_payload"]["torrent_file"]
    assert preflight["status"] == "blocked"
    assert torrent_summary["mteam_safe"] is False
    assert torrent_summary["announce_list_present"] is True
    assert torrent_summary["extra_top_level_fields"] == ["announce-list", "publisher-url"]
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], torrent_file=str(torrent_file), write_payload=True)

    assert preflight["status"] == "ready"
    assert preflight["files"]["upload_payload"].endswith("mteam-upload-payload.json")
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-upload-payload.json").exists()
    assert preflight["upload_payload"]["description_file"]["exists"] is True
    assert preflight["upload_payload"]["description_file"]["char_length"] == preflight["upload_payload"]["form_fields"]["descr"]["length"]
    description_checks = [check for check in preflight["upload_payload"]["material_checks"] if check["name"].startswith("payload.description_")]
    assert all(check["ok"] for check in description_checks)
    assert preflight["upload_payload"]["materials_ready_required"] is False


def test_mteam_upload_preflight_execute_requires_ready_materials(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(torrent_file))

    assert preflight["status"] == "blocked"
    assert preflight["upload_payload"]["materials_ready_required"] is True
    assert any("materials.assets.mediainfo_or_bdinfo" in blocker for blocker in preflight["upload_payload"]["blockers"])


def test_target_upload_summary_diagnostics_expose_blocked_preflight(tmp_path) -> None:
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
    material_dir = tmp_path / "target-upload-materials"
    material_dir.mkdir()
    mediainfo = material_dir / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Example.Movie.2024.mkv\n", encoding="utf-8")
    screenshot = material_dir / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_host_file = material_dir / "image-host-uploads.json"
    image_host_file.write_text(json.dumps({"items": [{"img_url": "https://img.example/screen-1.png", "web_url": "https://img.example/view/screen-1"}]}), encoding="utf-8")
    package = write_mteam_prepare_package(
        source_info,
        ["MTEAM"],
        mteam_ready_stages(),
        "/downloads/Example",
        str(tmp_path),
        accept_rules=True,
        material_files={
            "mediainfo_file": str(mediainfo),
            "screenshot_files": [str(screenshot)],
            "image_host_file": str(image_host_file),
        },
    )
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")
    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(torrent_file))
    args = build_parser().parse_args(["target-upload", "--package-dir", package["package_dir"], "--torrent-file", str(torrent_file), "--execute", "--confirm-upload"])
    summary_file = ptcli_cli._write_target_upload_summary(preflight, preflight, args, package["package_dir"])
    summary_payload = json.loads(Path(summary_file).read_text(encoding="utf-8"))

    diagnostics = ptcli_cli._summary_check_diagnostics(summary_payload)

    preflight_diagnostics = diagnostics["target_upload_diagnostics"]["preflight"]
    material_diagnostics = diagnostics["material_diagnostics"]
    assert preflight_diagnostics["status"] == "blocked"
    assert preflight_diagnostics["ready"] is False
    assert "materials.metadata.tmdb" in preflight_diagnostics["missing"]
    assert "materials.assets.mediainfo_or_bdinfo" not in preflight_diagnostics["missing"]
    assert "description.content" in preflight_diagnostics["missing"]
    assert "materials.description.external_ids.tmdb" in preflight_diagnostics["description_missing"]
    assert "materials.description.mediainfo_or_bdinfo" not in preflight_diagnostics["description_missing"]
    assert preflight_diagnostics["target_preparation_ready"] is False
    assert preflight_diagnostics["materials_ready"] is False
    assert preflight_diagnostics["metadata_ready"] is False
    assert preflight_diagnostics["assets_ready"] is True
    assert preflight_diagnostics["description_ready"] is False
    assert preflight_diagnostics["payload_ready"] is False
    assert preflight_diagnostics["payload_checks_ready"] is True
    assert preflight_diagnostics["description_checks_ready"] is False
    assert preflight_diagnostics["materials_ready_required"] is True
    assert preflight_diagnostics["torrent_file"]["mteam_safe"] is True
    assert preflight_diagnostics["torrent_file"]["metadata_readable"] is True
    assert preflight_diagnostics["blockers"]
    assert summary_payload["material_diagnostics"]["present"] is True
    assert material_diagnostics["present"] is True
    assert material_diagnostics["ready_for_mteam_upload"] is False
    assert material_diagnostics["critical_path"]["ready"] is False
    assert material_diagnostics["critical_path"]["next_step"] == "metadata"
    assert "metadata.tmdb" in material_diagnostics["critical_path"]["missing"]
    assert "assets.mediainfo_or_bdinfo" not in material_diagnostics["critical_path"]["missing"]
    assert "description.content" in material_diagnostics["critical_path"]["missing"]
    payload_review = diagnostics["target_upload_diagnostics"]["payload_review"]
    payload_completeness = payload_review["description"]["completeness"]
    assert payload_completeness["ready"] is False
    assert payload_completeness["missing"] == ["ptgen_description", "external_ids"]
    assert payload_completeness["recovery_missing"] == ["description.ptgen_description", "description.external_ids"]
    assert any("Fetch PTGen/Douban description" in action for action in payload_completeness["next_actions"])
    assert any("IMDb/TMDb/Douban metadata" in action for action in payload_completeness["next_actions"])
    commands = {command["stage"]: command for command in summary_payload["recommended_commands"]}
    assert "resume-target-package" in commands
    assert summary_payload["resume_state"]["next_stage"] == "resume-target-package"
    assert summary_payload["resume_state"]["next_command"] == commands["resume-target-package"]["command"]
    assert "--prepare-target" in commands["resume-target-package"]["argv"]
    assert "--enrich-metadata" in commands["resume-target-package"]["argv"]
    assert "--mediainfo-file" in commands["resume-target-package"]["argv"]
    assert str(mediainfo) in commands["resume-target-package"]["argv"]
    assert "--screenshot-file" in commands["resume-target-package"]["argv"]
    assert str(screenshot) in commands["resume-target-package"]["argv"]
    assert "--image-host-file" in commands["resume-target-package"]["argv"]
    assert str(image_host_file) in commands["resume-target-package"]["argv"]
    assert "/downloads/Example" in commands["resume-target-package"]["argv"]
    shell_fields = ptcli_cli._summary_check_target_upload_shell_fields(diagnostics["target_upload_diagnostics"])
    assert shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_READY"] == "0"
    assert "materials.metadata.tmdb" in shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_MISSING"]
    assert "description.content" in shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_MISSING"]
    assert "materials.description.external_ids.tmdb" in shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_DESCRIPTION_MISSING"]
    assert shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_MATERIALS_READY"] == "0"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_METADATA_READY"] == "0"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_ASSETS_READY"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_DESCRIPTION_READY"] == "0"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_PAYLOAD_READY"] == "0"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_PAYLOAD_CHECKS_READY"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_DESCRIPTION_CHECKS_READY"] == "0"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_MATERIALS_READY_REQUIRED"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_TORRENT_MTEAM_SAFE"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PREFLIGHT_TORRENT_METADATA_READABLE"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETE"] == "0"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETENESS_MISSING"] == "ptgen_description,external_ids"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETENESS_RECOVERY_MISSING"] == "description.ptgen_description,description.external_ids"
    assert "Fetch PTGen/Douban description" in shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETENESS_NEXT_ACTIONS"]
    assert "IMDb/TMDb/Douban metadata" in shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETENESS_NEXT_ACTIONS"]


def test_mteam_upload_preflight_execute_requires_materials_even_when_none_supplied(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": None,
        "tmdb_id": None,
        "douban_id": None,
        "douban_url": None,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_mteam_prepare_package(source_info, ["MTEAM"], mteam_ready_stages(), "/downloads/Example", str(tmp_path), accept_rules=True)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(torrent_file))

    assert preflight["status"] == "blocked"
    assert preflight["upload_payload"]["materials_ready_required"] is True
    blockers = preflight["upload_payload"]["blockers"]
    assert any("materials.metadata.imdb" in blocker for blocker in blockers)
    assert any("materials.metadata.tmdb" in blocker for blocker in blockers)
    assert any("materials.metadata.douban" in blocker for blocker in blockers)
    assert any("materials.metadata.ptgen_description" in blocker for blocker in blockers)
    assert any("materials.assets.screenshots" in blocker for blocker in blockers)
    assert any("materials.assets.image_host_uploads" in blocker for blocker in blockers)


def test_mteam_upload_preflight_exposes_missing_description_external_id(tmp_path) -> None:
    mediainfo = tmp_path / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_host_file = tmp_path / "image-host-uploads.json"
    image_host_file.write_text(json.dumps({"items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}]}), encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": None,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    package = write_mteam_prepare_package(
        source_info,
        ["MTEAM"],
        mteam_ready_stages(),
        "/downloads/Example",
        str(tmp_path),
        accept_rules=True,
        material_files={"mediainfo_file": str(mediainfo), "screenshot_files": [str(screenshot)], "image_host_file": str(image_host_file)},
    )
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(torrent_file))

    assert preflight["status"] == "blocked"
    content = preflight["upload_payload"]["description_file"]["content"]
    assert content["external_id_readiness"] == {"imdb": True, "tmdb": False, "douban": True}
    assert content["external_id_missing"] == ["tmdb"]
    blockers = preflight["upload_payload"]["blockers"]
    assert any("materials.description.external_ids.tmdb" in blocker for blocker in blockers)
    assert not any("materials.description.external_ids.imdb" in blocker for blocker in blockers)
    assert not any("materials.description.external_ids.douban" in blocker for blocker in blockers)
    audit = ptcli_cli._target_preparation_audit(package, str(torrent_file))
    assert audit["description"]["external_id_readiness"] == {"imdb": True, "tmdb": False, "douban": True}
    assert audit["description"]["external_id_missing"] == ["tmdb"]


def test_mteam_upload_preflight_execute_accepts_ready_materials(tmp_path) -> None:
    mediainfo = tmp_path / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_host_file = tmp_path / "image-host-uploads.json"
    image_host_file.write_text(json.dumps({"items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}]}), encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    package = write_mteam_prepare_package(
        source_info,
        ["MTEAM"],
        mteam_ready_stages(),
        "/downloads/Example",
        str(tmp_path),
        accept_rules=True,
        material_files={"mediainfo_file": str(mediainfo), "screenshot_files": [str(screenshot)], "image_host_file": str(image_host_file)},
    )
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(torrent_file))

    assert preflight["status"] == "ready"
    assert preflight["upload_payload"]["form_fields"]["mediainfo"]["length"] == len(mediainfo.read_text(encoding="utf-8"))
    assert preflight["upload_payload"]["description_file"]["content"]["has_ptgen_description"] is True
    assert preflight["upload_payload"]["description_file"]["content"]["has_screenshot_bbcode"] is True
    assert preflight["upload_payload"]["description_file"]["content"]["bbcode_image_urls"] == ["https://img.example/thumb.png"]
    assert preflight["upload_payload"]["description_file"]["content"]["has_mediainfo_or_bdinfo"] is True
    assert preflight["upload_payload"]["description_file"]["content"]["external_links"]["imdb"] == "https://www.imdb.com/title/tt1234567"
    assert preflight["upload_payload"]["description_file"]["content"]["external_links"]["tmdb"] == "https://www.themoviedb.org/movie/999"
    assert preflight["upload_payload"]["description_file"]["content"]["external_links"]["douban"] == "https://movie.douban.com/subject/1291546/"
    assert preflight["upload_payload"]["description_file"]["content"]["external_id_readiness"] == {"imdb": True, "tmdb": True, "douban": True}
    assert preflight["upload_payload"]["description_file"]["content"]["external_id_missing"] == []
    review = preflight["upload_payload"]["review"]
    assert review["description"]["external_links"]["imdb"] == "https://www.imdb.com/title/tt1234567"
    assert review["description"]["external_links"]["tmdb"] == "https://www.themoviedb.org/movie/999"
    assert review["description"]["external_links"]["douban"] == "https://movie.douban.com/subject/1291546/"
    assert review["description"]["external_id_readiness"] == {"imdb": True, "tmdb": True, "douban": True}
    assert review["description"]["external_id_missing"] == []
    assert review["description"]["bbcode_image_count"] == 1
    assert review["description"]["bbcode_image_urls"] == ["https://img.example/thumb.png"]
    assert review["description"]["screenshot_coverage"] == {
        "ready": True,
        "expected_urls": ["https://img.example/thumb.png"],
        "description_urls": ["https://img.example/thumb.png"],
        "missing_urls": [],
    }
    assert review["materials"]["mediainfo_or_bdinfo_source"] == str(mediainfo)
    assert review["materials"]["mediainfo_or_bdinfo_length"] == len(mediainfo.read_text(encoding="utf-8"))
    assert review["materials"]["screenshot_file_count"] == 1
    assert review["materials"]["image_host_count"] == 1
    assert review["materials"]["image_host_urls"] == ["https://img.example/thumb.png"]
    assert review["form"]["name"] == source_info["name"]
    coverage_check = next(check for check in preflight["upload_payload"]["material_checks"] if check["name"] == "materials.description.screenshot_coverage")
    assert coverage_check["ok"] is True
    assert coverage_check["expected_urls"] == ["https://img.example/thumb.png"]
    assert coverage_check["description_urls"] == ["https://img.example/thumb.png"]
    assert coverage_check["missing_urls"] == []
    assert all(check["ok"] for check in preflight["upload_payload"]["material_checks"])
    audit = ptcli_cli._target_preparation_audit(package, str(torrent_file))
    assert audit["description"]["external_id_readiness"] == {"imdb": True, "tmdb": True, "douban": True}
    assert audit["description"]["external_id_missing"] == []
    assert audit["description"]["media_info"] == {
        "has_excerpt": True,
        "source": str(mediainfo),
        "length": len(mediainfo.read_text(encoding="utf-8")),
    }


def test_mteam_upload_preflight_execute_blocks_missing_image_host_urls_in_description(tmp_path) -> None:
    mediainfo = tmp_path / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    screenshots = []
    for index in (1, 2):
        screenshot = tmp_path / f"screen-{index}.png"
        screenshot.write_bytes(b"png")
        screenshots.append(str(screenshot))
    image_host_file = tmp_path / "image-host-uploads.json"
    image_host_file.write_text(
        json.dumps(
            {
                "items": [
                    {"raw_url": "https://img.example/raw-1.png", "img_url": "https://img.example/thumb-1.png", "web_url": "https://img.example/page-1"},
                    {"raw_url": "https://img.example/raw-2.png", "img_url": "https://img.example/thumb-2.png", "web_url": "https://img.example/page-2"},
                ]
            }
        ),
        encoding="utf-8",
    )
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    package = write_mteam_prepare_package(
        source_info,
        ["MTEAM"],
        mteam_ready_stages(),
        "/downloads/Example",
        str(tmp_path),
        accept_rules=True,
        material_files={"mediainfo_file": str(mediainfo), "screenshot_files": screenshots, "image_host_file": str(image_host_file)},
    )
    package_from_disk = load_mteam_prepare_package(package["package_dir"])
    description_path = Path(package_from_disk["files"]["description_draft"])
    description_text = description_path.read_text(encoding="utf-8")
    description_path.write_text(description_text.replace("[url=https://img.example/page-2][img]https://img.example/thumb-2.png[/img][/url]", ""), encoding="utf-8")
    package_from_disk["description_length"] = len(description_path.read_text(encoding="utf-8"))
    manifest_path = Path(package_from_disk["files"]["manifest"])
    manifest_path.write_text(json.dumps(package_from_disk, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(torrent_file))

    assert preflight["status"] == "blocked"
    coverage_check = next(check for check in preflight["upload_payload"]["material_checks"] if check["name"] == "materials.description.screenshot_coverage")
    assert coverage_check["ok"] is False
    assert coverage_check["expected_urls"] == ["https://img.example/thumb-1.png", "https://img.example/thumb-2.png"]
    assert coverage_check["description_urls"] == ["https://img.example/thumb-1.png"]
    assert coverage_check["missing_urls"] == ["https://img.example/thumb-2.png"]
    blockers = preflight["upload_payload"]["blockers"]
    assert any("materials.description.screenshot_coverage" in blocker for blocker in blockers)
    review = preflight["upload_payload"]["review"]
    assert review["description"]["screenshot_coverage"] == {
        "ready": False,
        "expected_urls": ["https://img.example/thumb-1.png", "https://img.example/thumb-2.png"],
        "description_urls": ["https://img.example/thumb-1.png"],
        "missing_urls": ["https://img.example/thumb-2.png"],
    }
    assert review["materials"]["image_host_urls"] == ["https://img.example/thumb-1.png", "https://img.example/thumb-2.png"]
    audit = ptcli_cli._target_preparation_audit(package_from_disk, str(torrent_file))
    assert audit["description_ready"] is False
    assert "materials.description.screenshot_coverage" in audit["description"]["missing"]
    assert audit["description"]["screenshot_coverage"] == {
        "ready": False,
        "expected_urls": ["https://img.example/thumb-1.png", "https://img.example/thumb-2.png"],
        "description_urls": ["https://img.example/thumb-1.png"],
        "missing_urls": ["https://img.example/thumb-2.png"],
    }


def test_mteam_upload_preflight_execute_blocks_stale_description_materials(tmp_path) -> None:
    mediainfo = tmp_path / "MI_FULL_00.txt"
    mediainfo.write_text("General\nComplete name : Example.mkv\n", encoding="utf-8")
    screenshot = tmp_path / "screen-1.png"
    screenshot.write_bytes(b"png")
    image_host_file = tmp_path / "image-host-uploads.json"
    image_host_file.write_text(json.dumps({"items": [{"raw_url": "https://img.example/raw.png", "img_url": "https://img.example/thumb.png", "web_url": "https://img.example/page"}]}), encoding="utf-8")
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "tmdb_id": 999,
        "douban_id": "1291546",
        "douban_url": "https://movie.douban.com/subject/1291546/",
        "torrenthash": "a" * 40,
        "description_length": 100,
        "ptgen_description": "◎译　　名　示例电影\n◎简　　介　示例简介",
    }
    package = write_mteam_prepare_package(
        source_info,
        ["MTEAM"],
        mteam_ready_stages(),
        "/downloads/Example",
        str(tmp_path),
        accept_rules=True,
        material_files={"mediainfo_file": str(mediainfo), "screenshot_files": [str(screenshot)], "image_host_file": str(image_host_file)},
    )
    package_from_disk = load_mteam_prepare_package(package["package_dir"])
    description_path = Path(package_from_disk["files"]["description_draft"])
    stale_description = "\n".join(
        [
            "[b]Retorrent review draft[/b]",
            "[b]IMDb[/b]: 1234567",
            "[b]TMDb[/b]: 999",
            "[b]Douban[/b]: https://movie.douban.com/subject/1291546/",
            "[b]Movie information[/b]",
            "PTGen/Douban description: missing",
        ]
    )
    description_path.write_text(stale_description, encoding="utf-8")
    package_from_disk["description_length"] = len(stale_description)
    manifest_path = Path(package_from_disk["files"]["manifest"])
    manifest_path.write_text(json.dumps(package_from_disk, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")

    preflight = build_mteam_upload_preflight(package["package_dir"], execute=True, torrent_file=str(torrent_file))

    assert preflight["status"] == "blocked"
    blockers = preflight["upload_payload"]["blockers"]
    assert any("materials.description.ptgen_description" in blocker for blocker in blockers)
    assert any("materials.description.mediainfo_or_bdinfo" in blocker for blocker in blockers)
    assert any("materials.description.screenshot_bbcode" in blocker for blocker in blockers)
    audit = ptcli_cli._target_preparation_audit(package_from_disk, str(torrent_file))
    assert "materials.description.ptgen_description" in audit["description"]["missing"]
    assert "materials.description.mediainfo_or_bdinfo" in audit["description"]["missing"]
    assert "materials.description.screenshot_bbcode" in audit["description"]["missing"]


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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
async def test_target_upload_injects_downloaded_torrent(monkeypatch, tmp_path, capsys) -> None:
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
    package = write_material_ready_mteam_package(source_info, tmp_path)
    torrent_file = make_mteam_safe_torrent(tmp_path, "upload")
    monkeypatch.setattr(ptcli_cli, "load_config", lambda _path: {"TRACKERS": {"MTEAM": {"api_key": "fake"}}})
    uploaded_path = tmp_path / "MTEAM-999.torrent"
    uploaded_hash = write_valid_torrent(uploaded_path, tmp_path / "uploaded-content" / "MTEAM-999.mkv")

    async def fake_upload_mteam_from_package(*_args, **_kwargs):
        return {
            "status": "uploaded",
            "uploaded_torrent_hash": uploaded_hash,
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
            "visible_in_client": True, "verified_in_client": True,
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
            "--uploaded-wait-timeout",
            "42",
            "--uploaded-wait-interval",
            "3",
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
    assert result["downloaded_torrent"]["size_bytes"] > 0
    assert result["downloaded_torrent"]["metadata_readable"] is True
    assert len(result["downloaded_torrent"]["sha1"]) == 40
    assert result["injected_torrent"]["save_path"] == "/downloads/Example"
    assert result["injected_torrent"]["category"] == "MTEAM"
    assert result["injected_torrent"]["tags"] == "retorrent"
    assert result["uploaded_wait"]["complete"] is True
    assert result["uploaded_wait"]["query"]["torrent_hash"] == uploaded_hash
    assert result["uploaded_wait"]["query"]["content_path"] == "/downloads/Example"
    assert result["uploaded_wait"]["query"]["timeout"] == 42.0
    assert result["uploaded_wait"]["query"]["interval"] == 3.0
    assert result["qbit_wait_mismatch"] is False
    assert result["qbit_wait_mismatches"] == []
    assert result["qbit_wait_diagnostics"]["uploaded"]["complete"] is True
    assert result["summary"]["ready"] is True
    assert result["resume_state"]["ready"] is True
    assert result["resume_state"]["next_stage"] is None
    assert result["next_command"] is None
    assert result["automation_action"] == "complete"
    assert result["automation_reason"] == "Summary is complete and no follow-up command is required."
    assert result["automation_exit_code"] == 0
    assert result["should_execute_next_command"] is False
    assert result["candidate_command_count"] == 4
    assert result["runnable_command_count"] == 2
    assert result["recommended_commands"][0]["stage"] == "target-upload-retry"
    assert result["first_runnable_stage"] == "target-upload-retry"
    summary_path = Path(result["summary_file"])
    assert summary_path == tmp_path / "summary" / "ptcli-target-upload-summary.json"
    assert result["automation_handoff"]["json"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_path), "--json"]
    assert result["automation_handoff"]["run_next_command"]["command"] == shlex.join(["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_path), "--run-next-command"])
    summary_payload = json.loads(await asyncio.to_thread(summary_path.read_text, encoding="utf-8"))
    assert summary_payload["schema_version"] == 1
    assert summary_payload["kind"] == "ptcli.target_upload.summary"
    assert summary_payload["summary_file"] == str(summary_path)
    assert summary_payload["automation_handoff"]["json"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_path), "--json"]
    assert summary_payload["automation_handoff"]["print_next_command"]["argv"] == ["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_path), "--print-next-command"]
    assert summary_payload["automation_handoff"]["run_next_command"]["command"] == shlex.join(["python3", "ptcli.py", "summary-check", "--summary-file", str(summary_path), "--run-next-command"])
    assert summary_payload["client"] == "default"
    assert summary_payload["qbit_options"] == {"uploaded": {"category": "MTEAM", "tags": "retorrent", "paused": True}}
    assert summary_payload["output_options"] == {"uploaded_output_dir": None, "summary_output_dir": str(tmp_path / "summary")}
    assert summary_payload["wait_options"] == {"uploaded": {"timeout": 42.0, "interval": 3.0}}
    assert summary_payload["summary"]["mode"] == "live_upload"
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
    assert summary_payload["summary"]["uploaded_torrent"]["size_bytes"] > 0
    assert summary_payload["summary"]["uploaded_torrent"]["metadata_readable"] is True
    assert len(summary_payload["summary"]["uploaded_torrent"]["sha1"]) == 40
    assert summary_payload["summary"]["uploaded_wait"]["complete"] is True
    assert summary_payload["summary"]["qbit_closure"]["injection"]["save_path"] == "/downloads/Example"
    assert summary_payload["summary"]["qbit_closure"]["injection"]["category"] == "MTEAM"
    assert summary_payload["summary"]["qbit_closure"]["injection"]["tags"] == "retorrent"
    assert summary_payload["summary"]["qbit_closure"]["wait"]["complete"] is True
    assert summary_payload["summary"]["qbit_closure"]["wait"]["query"]["torrent_hash"] == uploaded_hash
    completion_review = summary_payload["summary"]["completion_review"]
    assert completion_review["complete"] is True
    assert completion_review["missing"] == []
    assert completion_review["checks"]["uploaded_torrent_file"] is True
    assert completion_review["checks"]["injection_verified"] is True
    assert completion_review["checks"]["uploaded_wait_complete"] is True
    assert completion_review["uploaded_torrent_id"] == "999"
    assert completion_review["uploaded_torrent_hash"] == uploaded_hash
    assert completion_review["uploaded_torrent_path"] == str(tmp_path / "MTEAM-999.torrent")
    assert completion_review["injected_torrent_hash"] == uploaded_hash
    assert completion_review["uploaded_save_path"] == "/downloads/Example"
    assert completion_review["uploaded_wait_query"]["torrent_hash"] == uploaded_hash
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
    assert summary_payload["artifacts"]["uploaded_torrent_file"]["size_bytes"] > 0
    assert summary_payload["artifacts"]["uploaded_torrent_file"]["metadata_readable"] is True
    assert summary_payload["artifacts"]["uploaded_torrent_file"]["torrent_hash"] == uploaded_hash
    assert len(summary_payload["artifacts"]["uploaded_torrent_file"]["sha1"]) == 40
    assert summary_payload["artifacts"]["uploaded_torrent_hash"] == uploaded_hash
    assert summary_payload["artifacts"]["injected_torrent_hash"] == uploaded_hash
    assert summary_payload["artifacts"]["injection_visible_in_client"] is True
    assert summary_payload["artifacts"]["injection_verified"] is True
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
    assert summary_payload["resume_state"]["artifacts"]["uploaded_torrent_hash"] is True
    assert summary_payload["resume_state"]["artifacts"]["injected_torrent_hash"] is True
    assert summary_payload["resume_state"]["artifacts"]["injection_visible_in_client"] is True
    assert summary_payload["resume_state"]["artifacts"]["injection_verified"] is True
    assert summary_payload["resume_state"]["artifacts"]["uploaded_wait_evidence"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_hash_consistent"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_duplicate_clean"] is True
    assert summary_payload["resume_state"]["artifacts"]["target_rule_obligations"] is True
    uploaded_followup = summary_payload["resume_state"]["uploaded_followup"]
    assert uploaded_followup["ready"] is True
    assert uploaded_followup["ready_for_uploaded_seeding"] is True
    assert uploaded_followup["gates"]["uploaded"] is True
    assert uploaded_followup["gates"]["downloaded"] is True
    assert uploaded_followup["gates"]["injection_verified"] is True
    assert uploaded_followup["gates"]["uploaded_wait_evidence"] is True
    assert uploaded_followup["blockers"] == []
    assert uploaded_followup["missing"] == []
    assert uploaded_followup["uploaded"] is True
    assert uploaded_followup["downloaded"] is True
    assert uploaded_followup["injection_verified"] is True
    assert uploaded_followup["uploaded_wait_evidence"] is True
    assert uploaded_followup["uploaded_torrent_hash"] == uploaded_hash
    assert uploaded_followup["injected_torrent_hash"] == uploaded_hash
    assert uploaded_followup["uploaded_torrent_file"] == str(tmp_path / "MTEAM-999.torrent")
    assert uploaded_followup["uploaded_torrent_file_evidence"]["path"] == str(tmp_path / "MTEAM-999.torrent")
    assert uploaded_followup["uploaded_torrent_file_evidence"]["exists"] is True
    assert uploaded_followup["uploaded_torrent_file_evidence"]["is_file"] is True
    assert uploaded_followup["uploaded_torrent_file_evidence"]["size_bytes"] > 0
    assert uploaded_followup["uploaded_torrent_file_evidence"]["metadata_readable"] is True
    assert uploaded_followup["uploaded_torrent_file_evidence"]["torrent_hash"] == uploaded_hash
    assert len(uploaded_followup["uploaded_torrent_file_evidence"]["sha1"]) == 40
    assert uploaded_followup["uploaded_save_path"] == "/downloads/Example"
    assert uploaded_followup["uploaded_wait_query"] == {"torrent_hash": uploaded_hash, "content_path": "/downloads/Example", "timeout": 42.0, "interval": 3.0}
    diagnostics = ptcli_cli._summary_check_diagnostics(summary_payload)
    assert diagnostics["target_upload_diagnostics"]["ready_for_uploaded_seeding"] is True
    preflight_diagnostics = diagnostics["target_upload_diagnostics"]["preflight"]
    assert preflight_diagnostics["status"] == "ready"
    assert preflight_diagnostics["ready"] is True
    assert preflight_diagnostics["blockers"] == []
    assert preflight_diagnostics["target_preparation_ready"] is True
    assert preflight_diagnostics["materials_ready"] is True
    assert preflight_diagnostics["metadata_ready"] is True
    assert preflight_diagnostics["assets_ready"] is True
    assert preflight_diagnostics["description_ready"] is True
    assert preflight_diagnostics["payload_ready"] is True
    assert preflight_diagnostics["payload_checks_ready"] is True
    assert preflight_diagnostics["description_checks_ready"] is True
    assert preflight_diagnostics["materials_ready_required"] is True
    assert preflight_diagnostics["torrent_file"]["mteam_safe"] is True
    assert preflight_diagnostics["torrent_file"]["metadata_readable"] is True
    assert preflight_diagnostics["torrent_file"]["source_flag"] == "MTEAM"
    payload_review = diagnostics["target_upload_diagnostics"]["payload_review"]
    assert payload_review["present"] is True
    assert payload_review["description"]["external_id_readiness"] == {"imdb": True, "tmdb": True, "douban": True}
    assert payload_review["description"]["external_id_missing"] == []
    assert payload_review["description"]["external_links"] == {
        "imdb": "https://www.imdb.com/title/tt1234567",
        "tmdb": "https://www.themoviedb.org/movie/999",
        "douban": "https://movie.douban.com/subject/1291546/",
    }
    assert payload_review["description"]["has_ptgen_description"] is True
    assert payload_review["description"]["has_mediainfo_or_bdinfo"] is True
    assert payload_review["description"]["has_screenshot_bbcode"] is True
    assert payload_review["description"]["bbcode_image_urls"] == ["https://img.example/thumb.png"]
    assert payload_review["description"]["screenshot_coverage"] == {
        "ready": True,
        "expected_urls": ["https://img.example/thumb.png"],
        "description_urls": ["https://img.example/thumb.png"],
        "missing_urls": [],
    }
    assert payload_review["description"]["completeness"] == {
        "ready": True,
        "missing": [],
        "recovery_missing": [],
        "next_actions": [],
        "checks": [
            {"name": "ptgen_description", "ready": True},
            {"name": "external_ids", "ready": True},
            {"name": "mediainfo_or_bdinfo", "ready": True},
            {"name": "screenshot_bbcode", "ready": True},
            {"name": "screenshot_coverage", "ready": True},
        ],
    }
    assert payload_review["materials"]["image_host_urls"] == ["https://img.example/thumb.png"]
    shell_fields = ptcli_cli._summary_check_target_upload_shell_fields(diagnostics["target_upload_diagnostics"])
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_REVIEW_PRESENT"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETE"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETENESS_MISSING"] == ""
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETENESS_RECOVERY_MISSING"] == ""
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETENESS_NEXT_ACTIONS"] == ""
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_PTGEN"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_EXTERNAL_IDS"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_EXTERNAL_ID_MISSING"] == ""
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_IMDB"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_TMDB"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_DOUBAN"] == "1"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_IMAGE_URLS"] == "https://img.example/thumb.png"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_IMAGE_HOST_URLS"] == "https://img.example/thumb.png"
    assert shell_fields["PTCLI_TARGET_UPLOAD_PAYLOAD_SCREENSHOT_COVERAGE_READY"] == "1"
    assert diagnostics["completion_matrix"]["domains"]["target_upload"]["evidence"]["ready_for_uploaded_seeding"] is True
    commands = {command["stage"]: command["command"] for command in summary_payload["recommended_commands"]}
    command_argv = {command["stage"]: command["argv"] for command in summary_payload["recommended_commands"]}
    assert "target-upload-retry" in commands
    assert command_argv["target-upload-retry"][:3] == ["python3", "ptcli.py", "target-upload"]
    assert str(tmp_path / "MTEAM-999.torrent") in commands["resume-uploaded-torrent"]
    assert str(tmp_path / "MTEAM-999.torrent") in command_argv["resume-uploaded-torrent"]
    assert "--client default" in commands["resume-uploaded-torrent"]
    assert "--config config.py" in commands["resume-uploaded-torrent"]
    assert "config.py" in command_argv["resume-uploaded-torrent"]
    assert "--uploaded-save-path /downloads/Example" in commands["resume-uploaded-torrent"]
    assert "--uploaded-qbit-category MTEAM" in commands["resume-uploaded-torrent"]
    assert "--uploaded-qbit-tags retorrent" in commands["resume-uploaded-torrent"]
    assert "--uploaded-paused" in commands["resume-uploaded-torrent"]
    assert "--uploaded-wait-timeout 42" in commands["resume-uploaded-torrent"]
    assert "--uploaded-wait-interval 3" in commands["resume-uploaded-torrent"]
    assert f"--summary-output-dir {shlex.quote(str(tmp_path / 'summary'))}" in commands["resume-uploaded-torrent"]
    assert str(tmp_path / "MTEAM-999.torrent") in commands["retorrent-resume-uploaded-torrent"]
    assert "--package-dir" in commands["retorrent-resume-uploaded-torrent"]
    assert "--from U2" in commands["retorrent-resume-uploaded-torrent"]
    assert "--source-id 60635" in commands["retorrent-resume-uploaded-torrent"]
    assert "--client default" in commands["retorrent-resume-uploaded-torrent"]
    assert "--config config.py" in commands["retorrent-resume-uploaded-torrent"]
    assert "--uploaded-save-path /downloads/Example" in commands["retorrent-resume-uploaded-torrent"]
    assert "--uploaded-wait-timeout 42" in commands["retorrent-resume-uploaded-torrent"]
    assert "--uploaded-wait-interval 3" in commands["retorrent-resume-uploaded-torrent"]
    assert f"--summary-output-dir {shlex.quote(str(tmp_path / 'summary'))}" in commands["retorrent-resume-uploaded-torrent"]
    assert command_argv["retorrent-resume-uploaded-torrent"][:3] == ["python3", "ptcli.py", "retorrent"]
    assert command_argv["verify-seeding"] == ["python3", "ptcli.py", "inspect", "--client", "default", "--json"]
    assert commands["verify-seeding"].startswith("python3 ptcli.py inspect")

    code = main(["summary-check", "--summary-file", str(summary_path), "--print-shell"])

    assert code == 0
    out = capsys.readouterr().out
    assert "export PTCLI_TARGET_UPLOAD_PRESENT=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_MODE=live_upload\n" in out
    assert "export PTCLI_TARGET_UPLOAD_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_UPLOADED=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_COMPLETE=1\n" in out
    assert "export PTCLI_COMPLETION_MATRIX_READY=1\n" in out
    assert "export PTCLI_COMPLETION_MATRIX_MISSING_DOMAINS=''\n" in out
    assert "export PTCLI_COMPLETION_RULES_READY=1\n" in out
    assert "export PTCLI_COMPLETION_TARGET_UPLOAD_READY=1\n" in out
    assert "export PTCLI_COMPLETION_TARGET_UPLOAD_READY_FOR_UPLOADED_SEEDING=1\n" in out
    assert "export PTCLI_COMPLETION_QBIT_WAIT_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_MISSING=''\n" in out
    assert "export PTCLI_TARGET_UPLOAD_TORRENT_ID=999\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_READY=1\n" in out
    assert "export PTCLI_READY_FOR_UPLOADED_SEEDING=1\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_BLOCKERS=''\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_GATES=" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_MISSING=''\n" in out
    assert f"export PTCLI_UPLOADED_FOLLOWUP_TORRENT_HASH={uploaded_hash}\n" in out
    assert f"export PTCLI_UPLOADED_FOLLOWUP_INJECTED_HASH={uploaded_hash}\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_TORRENT_EXISTS=1\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_TORRENT_IS_FILE=1\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_TORRENT_METADATA_READABLE=1\n" in out
    assert f"export PTCLI_UPLOADED_FOLLOWUP_TORRENT_INFOHASH={uploaded_hash}\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_SAVE_PATH=/downloads/Example\n" in out
    assert f"export PTCLI_TARGET_UPLOAD_TORRENT_HASH={uploaded_hash}\n" in out
    assert f"export PTCLI_TARGET_UPLOAD_TORRENT_PATH={str(tmp_path / 'MTEAM-999.torrent')}\n" in out
    assert f"export PTCLI_TARGET_UPLOAD_INJECTED_HASH={uploaded_hash}\n" in out
    assert "export PTCLI_TARGET_UPLOAD_SAVE_PATH=/downloads/Example\n" in out
    assert f"export PTCLI_TARGET_UPLOAD_WAIT_QUERY_HASH={uploaded_hash}\n" in out
    assert "export PTCLI_TARGET_UPLOAD_WAIT_QUERY_CONTENT_PATH=/downloads/Example\n" in out
    assert "export PTCLI_TARGET_UPLOAD_WAIT_QUERY_TIMEOUT=42.0\n" in out
    assert "export PTCLI_TARGET_UPLOAD_WAIT_QUERY_INTERVAL=3.0\n" in out
    assert f"export PTCLI_UPLOADED_FOLLOWUP_WAIT_QUERY_HASH={uploaded_hash}\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_WAIT_QUERY_CONTENT_PATH=/downloads/Example\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_WAIT_QUERY_TIMEOUT=42.0\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_WAIT_QUERY_INTERVAL=3.0\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_STATUS=ready\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_BLOCKERS=''\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_PREPARATION_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_MATERIALS_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_METADATA_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_ASSETS_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_DESCRIPTION_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_PAYLOAD_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_PAYLOAD_CHECKS_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_DESCRIPTION_CHECKS_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_MATERIALS_READY_REQUIRED=1\n" in out
    assert f"export PTCLI_TARGET_UPLOAD_PREFLIGHT_TORRENT_PATH={torrent_file}\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_TORRENT_MTEAM_SAFE=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_TORRENT_METADATA_READABLE=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_PREFLIGHT_TORRENT_SOURCE_FLAG=MTEAM\n" in out
    assert "export PTCLI_TARGET_UPLOAD_CHECK_PREPARATION_READY=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_CHECK_TORRENT_FILE=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_CHECK_INJECTION_VERIFIED=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_CHECK_WAIT_COMPLETE=1\n" in out
    assert "export PTCLI_TARGET_UPLOAD_CHECK_RULES_READY=1\n" in out


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
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
    command_argv = {command["stage"]: command["argv"] for command in summary_payload["recommended_commands"]}
    assert summary_payload["resume_state"]["next_command"] == commands["resume-uploaded-torrent"]
    assert summary_payload["resume_state"]["next_command_argv"] == command_argv["resume-uploaded-torrent"]
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
            "--config",
            str(tmp_path / "custom-config.py"),
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
            "--wait-uploaded-complete",
            "--uploaded-wait-timeout",
            "900",
            "--uploaded-wait-interval",
            "20",
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
    assert summary_payload["summary"]["mode"] == "live_upload"
    assert summary_payload["summary"]["uploaded_torrent_id"] == "999"
    assert summary_payload["summary"]["completion_review"]["complete"] is False
    assert summary_payload["summary"]["completion_review"]["uploaded_torrent_id"] == "999"
    assert "uploaded_torrent_file" in summary_payload["summary"]["completion_review"]["missing"]
    assert "injection_verified" in summary_payload["summary"]["completion_review"]["missing"]
    assert "uploaded_wait_complete" in summary_payload["summary"]["completion_review"]["missing"]
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
    assert f"--config {shlex.quote(str(tmp_path / 'custom-config.py'))}" in commands["resume-uploaded-torrent-download"]
    assert command_argv["resume-uploaded-torrent-download"][:3] == ["python3", "ptcli.py", "target-upload"]
    assert str(tmp_path / "custom-config.py") in command_argv["resume-uploaded-torrent-download"]
    assert "999" in command_argv["resume-uploaded-torrent-download"]
    assert "--download-uploaded-torrent" in commands["resume-uploaded-torrent-download"]
    assert "--inject-uploaded-torrent" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-save-path /mnt/seedbox/Example" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-category MTEAM" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-qbit-tags retorrent" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-paused" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-wait-timeout 900" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-wait-interval 20" in commands["resume-uploaded-torrent-download"]
    assert f"--summary-output-dir {shlex.quote(str(tmp_path / 'summary'))}" in commands["resume-uploaded-torrent-download"]
    assert "--uploaded-wait-timeout 900" in commands["target-upload-retry"]
    assert "--uploaded-wait-interval 20" in commands["target-upload-retry"]
    assert "--uploaded-torrent-id 999" in commands["retorrent-resume-uploaded-torrent-download"]
    assert f"--config {shlex.quote(str(tmp_path / 'custom-config.py'))}" in commands["retorrent-resume-uploaded-torrent-download"]
    assert "--download-uploaded-torrent" in commands["retorrent-resume-uploaded-torrent-download"]
    assert "--from U2" in commands["retorrent-resume-uploaded-torrent-download"]
    assert "--source-id 60635" in commands["retorrent-resume-uploaded-torrent-download"]
    assert "--uploaded-save-path /mnt/seedbox/Example" in commands["retorrent-resume-uploaded-torrent-download"]
    assert "--uploaded-wait-timeout 900" in commands["retorrent-resume-uploaded-torrent-download"]
    assert "--uploaded-wait-interval 20" in commands["retorrent-resume-uploaded-torrent-download"]
    assert f"--summary-output-dir {shlex.quote(str(tmp_path / 'summary'))}" in commands["retorrent-resume-uploaded-torrent-download"]
    assert command_argv["retorrent-resume-uploaded-torrent-download"][:3] == ["python3", "ptcli.py", "retorrent"]


def test_target_upload_summary_exposes_target_preparation_audit(tmp_path) -> None:
    source_info = {
        "tracker": "U2",
        "torrent_id": "60635",
        "name": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "imdb_id": 1234567,
        "torrenthash": "a" * 40,
        "description_length": 100,
    }
    package = write_material_ready_mteam_package(source_info, tmp_path)
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
            "/downloads/Example",
            "--wait-uploaded-complete",
            "--write-summary",
            "--json",
        ]
    )

    summary_file = ptcli_cli._write_target_upload_summary({"status": "uploaded", "uploaded_torrent_id": "999"}, preflight, args, package["package_dir"])

    summary_payload = json.loads(Path(summary_file).read_text(encoding="utf-8"))
    audit = summary_payload["summary"]["target_preparation_audit"]
    assert audit["ready"] is True
    assert audit["description"]["has_ptgen_description"] is True
    assert audit["description"]["has_external_ids"] is True
    assert audit["description"]["has_mediainfo_or_bdinfo"] is True
    assert audit["description"]["has_screenshot_bbcode"] is True
    assert audit["description"]["bbcode_image_count"] == 1
    assert summary_payload["summary"]["target_preparation_ready"] is True
    assert summary_payload["artifacts"]["target_preparation_ready"] is True
    assert summary_payload["material_diagnostics"]["present"] is True
    assert summary_payload["material_diagnostics"]["ready_for_mteam_upload"] is True
    assert summary_payload["material_diagnostics"]["critical_path"]["ready"] is True
    assert summary_payload["material_diagnostics"]["critical_path"]["next_step"] is None
    assert summary_payload["resume_state"]["artifacts"]["target_preparation_ready"] is True


def test_target_upload_retry_command_uses_inferred_uploaded_save_path(tmp_path) -> None:
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
            "--write-summary",
            "--json",
        ]
    )

    summary_file = ptcli_cli._write_target_upload_summary({"status": "blocked", "uploaded_torrent_id": "999"}, preflight, args, package["package_dir"])

    summary_payload = json.loads(Path(summary_file).read_text(encoding="utf-8"))
    commands = {command["stage"]: command["command"] for command in summary_payload["recommended_commands"]}
    command_argv = {command["stage"]: command["argv"] for command in summary_payload["recommended_commands"]}
    assert summary_payload["artifacts"]["uploaded_save_path"]["path"] == "/downloads/Example"
    assert "--uploaded-save-path /downloads/Example" in commands["target-upload-retry"]
    assert "/downloads/Example" in command_argv["target-upload-retry"]


def test_target_upload_retorrent_resume_uses_package_source_identity_after_rename(tmp_path) -> None:
    source_info = {
        "tracker": "u2",
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
    renamed_package_dir = tmp_path / "portable-mteam-package"
    Path(package["package_dir"]).rename(renamed_package_dir)
    preflight = build_mteam_upload_preflight(str(renamed_package_dir), execute=True, torrent_file=str(make_mteam_safe_torrent(tmp_path, "upload")))
    parser = build_parser()
    args = parser.parse_args(
        [
            "target-upload",
            "--package-dir",
            str(renamed_package_dir),
            "--uploaded-torrent-id",
            "999",
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--uploaded-save-path",
            "/downloads/Example",
            "--wait-uploaded-complete",
            "--json",
        ]
    )

    summary_file = ptcli_cli._write_target_upload_summary({"status": "uploaded", "uploaded_torrent_id": "999"}, preflight, args, str(renamed_package_dir))

    summary_payload = json.loads(Path(summary_file).read_text(encoding="utf-8"))
    commands = {command["stage"]: command["command"] for command in summary_payload["recommended_commands"]}
    command_argv = {command["stage"]: command["argv"] for command in summary_payload["recommended_commands"]}
    assert "--from U2" in commands["retorrent-resume-uploaded-torrent-download"]
    assert "--source-id 60635" in commands["retorrent-resume-uploaded-torrent-download"]
    assert str(renamed_package_dir) in command_argv["retorrent-resume-uploaded-torrent-download"]


def test_target_upload_summary_exposes_uploaded_wait_mismatch(tmp_path, capsys) -> None:
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
    followup = summary_payload["resume_state"]["uploaded_followup"]
    assert followup["qbit_wait_mismatch"] is True
    assert followup["qbit_wait_mismatches"] == ["uploaded.requested_content_path"]
    assert followup["wait_retry"]["retry_recommended"] is True
    assert followup["wait_retry"]["suggested_torrent_hash"] == "b" * 40
    assert followup["wait_retry"]["suggested_content_path"] == "/downloads/Other"
    assert followup["wait_retry"]["suggested_save_path"] == "/downloads"
    commands = {command["stage"]: command["command"] for command in summary_payload["recommended_commands"]}
    command_argv = {command["stage"]: command["argv"] for command in summary_payload["recommended_commands"]}
    assert "--uploaded-save-path /downloads/Other" in commands["resume-uploaded-torrent"]
    assert "/downloads/Other" in command_argv["resume-uploaded-torrent"]
    assert "--uploaded-save-path /downloads/Other" in commands["retorrent-resume-uploaded-torrent"]
    assert "/downloads/Other" in command_argv["retorrent-resume-uploaded-torrent"]
    diagnostics = summary_payload["qbit_wait_diagnostics"]["uploaded"]
    assert diagnostics["complete"] is True
    assert diagnostics["request_mismatch"] is True
    assert diagnostics["requested_hash"] == "b" * 40
    assert diagnostics["requested_content_path"] == "/downloads/Example"
    assert diagnostics["requested_hash_matched"] is True
    assert diagnostics["requested_content_path_matched"] is False
    assert diagnostics["observed_hashes"] == ["b" * 40]
    assert diagnostics["observed_content_paths"] == ["/downloads/Other"]
    code = main(["summary-check", "--summary-file", str(summary_file), "--print-shell"])
    assert code == 0
    out = capsys.readouterr().out
    assert "export PTCLI_UPLOADED_FOLLOWUP_WAIT_MISMATCH=1\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_WAIT_MISMATCHES=uploaded.requested_content_path\n" in out
    assert f"export PTCLI_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_HASH={'b' * 40}\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_CONTENT_PATH=/downloads/Other\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_SAVE_PATH=/downloads\n" in out
    assert "export PTCLI_QBIT_WAIT_UPLOADED_RETRY_ACTION='Resolve the uploaded qBittorrent wait mismatch before rerunning:" in out
    assert f"Suggested retry values from qBittorrent: hash={'b' * 40}, path=/downloads/Other, save_path=/downloads." in out


def test_target_upload_summary_retries_when_uploaded_hash_is_inconsistent_after_wait(tmp_path) -> None:
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
    uploaded_hash = str(Torrent.read(uploaded_torrent, validate=False).infohash)
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
        "status": "uploaded",
        "uploaded_torrent_id": "999",
        "downloaded_torrent": {"path": str(uploaded_torrent), "torrent_hash": uploaded_hash, "metadata_readable": True},
        "uploaded_torrent_hash": "b" * 40,
        "injected_torrent": {"hash": uploaded_hash, "visible_in_client": True, "verified_in_client": True},
        "uploaded_wait": {
            "complete": True,
            "query": {"torrent_hash": uploaded_hash, "content_path": "/downloads/Example"},
            "matches": [{"hash": uploaded_hash, "content_path": "/downloads/Example"}],
            "completion_verification": {
                "matched_count": 1,
                "complete_count": 1,
                "any_complete": True,
                "requested_hash_matched": True,
                "requested_content_path_matched": True,
            },
        },
    }

    summary_file = ptcli_cli._write_target_upload_summary(result, preflight, args, package["package_dir"])

    summary_payload = json.loads(Path(summary_file).read_text(encoding="utf-8"))
    assert summary_payload["summary"]["ready"] is False
    assert summary_payload["summary"]["hash_consistent"] is False
    assert summary_payload["resume_state"]["uploaded_followup"]["uploaded_wait_evidence"] is True
    assert summary_payload["resume_state"]["uploaded_followup"]["hash_consistent"] is False
    assert summary_payload["resume_state"]["next_stage"] == "target-upload-retry"
    commands = {command["stage"]: command["command"] for command in summary_payload["recommended_commands"]}
    assert summary_payload["resume_state"]["next_command"] == commands["target-upload-retry"]


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
    uploaded_hash = write_valid_torrent(uploaded_path, tmp_path / "uploaded-content" / "MTEAM-999.mkv")

    async def fake_download_mteam_uploaded_torrent(_config, torrent_id, output_dir):
        assert torrent_id == "999"
        assert output_dir == "uploaded"
        return {"status": "uploaded", "uploaded_torrent_id": torrent_id, "uploaded_torrent_hash": uploaded_hash, "downloaded_torrent": {"torrent_id": torrent_id, "path": str(uploaded_path)}}

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
    summary_payload = json.loads((Path(package["package_dir"]) / "ptcli-target-upload-summary.json").read_text(encoding="utf-8"))
    assert summary_payload["output_options"] == {"uploaded_output_dir": "uploaded", "summary_output_dir": None}
    assert summary_payload["wait_options"] == {"uploaded": {"timeout": 600.0, "interval": 15.0}}
    assert summary_payload["summary"]["mode"] == "resumed_uploaded_id"


@pytest.mark.asyncio
async def test_target_upload_download_only_records_uploaded_torrent_file_evidence(monkeypatch, tmp_path, capsys) -> None:
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
    assert result["downloaded_torrent"]["hash"] == Torrent.read(uploaded_path, validate=False).infohash
    assert result["downloaded_torrent"]["torrent_hash"] == Torrent.read(uploaded_path, validate=False).infohash
    assert result["summary"]["uploaded_torrent"]["exists"] is True
    assert result["summary"]["uploaded_torrent_hash"] == result["downloaded_torrent"]["torrent_hash"]
    assert result["summary"]["mode"] == "resumed_uploaded_id"
    assert result["resume_state"]["ready"] is True
    assert result["resume_state"]["uploaded_followup"]["ready"] is False
    assert result["resume_state"]["uploaded_followup"]["ready_for_uploaded_seeding"] is False
    assert result["resume_state"]["uploaded_followup"]["downloaded"] is True
    assert result["resume_state"]["uploaded_followup"]["uploaded_torrent_hash"] == result["downloaded_torrent"]["torrent_hash"]
    assert result["resume_state"]["uploaded_followup"]["missing"] == ["injected_torrent_hash", "injection_verified", "uploaded_wait_evidence"]
    assert result["resume_state"]["uploaded_followup"]["gates"]["downloaded"] is True
    assert result["resume_state"]["uploaded_followup"]["gates"]["injection_verified"] is False
    assert result["resume_state"]["uploaded_followup"]["gates"]["uploaded_wait_evidence"] is False
    assert "uploaded MTEAM torrent injection is not verified in qBittorrent" in result["resume_state"]["uploaded_followup"]["blockers"]
    assert "qBittorrent has not reported the uploaded MTEAM torrent as complete" in result["resume_state"]["uploaded_followup"]["blockers"]
    assert any("Inject the uploaded MTEAM torrent" in action for action in result["resume_state"]["uploaded_followup"]["next_actions"])
    assert any("Wait for qBittorrent" in action for action in result["resume_state"]["uploaded_followup"]["next_actions"])
    assert result["resume_state"]["next_stage"] == "resume-uploaded-torrent"
    assert result["next_command"] == result["resume_state"]["next_command"]
    assert "--uploaded-torrent-file" in result["next_command_argv"]
    assert str(uploaded_path) in result["next_command_argv"]
    assert result["next_command_ready"] is True
    assert result["next_command_run_allowed"] is True
    assert result["automation_action"] == "run_next_command"
    assert result["automation_reason"] == "Next generated ptcli command is ready to run for stage resume-uploaded-torrent."
    assert result["automation_exit_code"] == 1
    assert result["should_execute_next_command"] is True
    assert result["candidate_command_count"] == 5
    assert result["runnable_command_count"] == 3
    summary_path = Path(result["summary_file"])

    code = main(["summary-check", "--summary-file", str(summary_path), "--print-shell"])

    assert code == 0
    out = capsys.readouterr().out
    assert "export PTCLI_UPLOADED_FOLLOWUP_DOWNLOADED=1\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_INJECTION_VERIFIED=0\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_WAIT_EVIDENCE=0\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_NEXT_ACTION_COUNT=2\n" in out
    assert "export PTCLI_UPLOADED_FOLLOWUP_FIRST_NEXT_ACTION='Inject the uploaded MTEAM torrent into qBittorrent with the correct save path.'\n" in out
    assert "Inject the uploaded MTEAM torrent into qBittorrent with the correct save path." in out
    assert "Wait for qBittorrent to report the uploaded MTEAM torrent as matched and complete." in out


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
            "visible_in_client": True, "verified_in_client": True,
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
    assert result["summary"]["mode"] == "resumed_uploaded_torrent"
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
    assert result["summary"]["duplicate_clean"] is True
    assert result["summary"]["fresh_duplicate_check"]["source"] == "target_package_upload_gate"


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
            "visible_in_client": True, "verified_in_client": True,
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
    assert evidence["hash"] == torrent.infohash
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
    assert result["visible_in_client"] is True
    assert result["verified_in_client"] is False
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
    assert result["visible_in_client"] is True
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

    assert result["verified_in_client"] is True
    assert result["visible_in_client"] is True
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
    assert result["visible_in_client"] is True
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

    assert result["visible_in_client"] is True
    assert result["verified_in_client"] is False
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
