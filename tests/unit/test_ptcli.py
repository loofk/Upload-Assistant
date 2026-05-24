import pytest

import src.ptcli.cli as ptcli_cli
from src.ptcli.cli import _with_captured_stdout, build_parser, build_plan, main
from src.ptcli.config import resolve_client_config
from src.ptcli.credentials import build_flow_check
from src.ptcli.mainland import normalize_tracker, parse_tracker_list
from src.ptcli.qbit import QbitReadOnlyService, match_torrents, summarize_torrent
from src.ptcli.source import create_source_meta, extract_torrent_id, source_info_from_tuple
from src.ptcli.target import build_mteam_field_mapping, build_mteam_meta_draft, build_mteam_prepare_preview, search_mteam_duplicates, write_mteam_prepare_package


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


def test_normalize_tracker_aliases() -> None:
    assert normalize_tracker("m-team") == "MTEAM"
    assert normalize_tracker("pterclub") == "PTER"


def test_parse_tracker_list_deduplicates() -> None:
    assert parse_tracker_list("mteam, M-TEAM, tjupt") == ["MTEAM", "TJUPT"]


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


def test_rules_command_outputs_profile_json(capsys) -> None:
    code = main(["rules", "--trackers", "MTEAM,TJUPT", "--json"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"tracker": "MTEAM"' in out
    assert '"tracker": "TJUPT"' in out
    assert '"review_required": true' in out


def test_json_capture_moves_stdout_to_logs() -> None:
    def noisy_payload():
        print("noisy tracker log")
        return {"status": "ok"}

    payload = _with_captured_stdout(noisy_payload, json_output=True)

    assert payload == {"status": "ok", "logs": ["noisy tracker log"]}


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

    assert payload["ready"] is False
    assert payload["stages"][1]["stage"] == "source-info"
    assert payload["stages"][1]["ok"] is False
    assert payload["stages"][1]["error"] == "source unavailable"


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
        return tmp_path / output_dir / "U2-60635.torrent"

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
            "--output-dir",
            "source-out",
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    download_stage = next(stage for stage in payload["stages"] if stage["stage"] == "source-download")
    assert download_stage["ok"] is True
    assert download_stage["result"]["path"].endswith("source-out/U2-60635.torrent")


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
        _ = (config, client_name, torrent_path, category, tags, paused)
        return {"client": "qbittorrent", "save_path": save_path}

    async def fake_wait_complete_with_config(config, client_name, content_path, torrent_hash, timeout, interval):
        _ = (config, client_name, torrent_hash, timeout, interval)
        return {"client": "qbittorrent", "complete": True, "matches": [{"content_path": content_path}]}

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
            "--json",
        ]
    )

    payload = await ptcli_cli.pipeline_payload(args)

    wait_stage = next(stage for stage in payload["stages"] if stage["stage"] == "wait-complete")
    assert wait_stage["ok"] is True
    assert wait_stage["result"]["complete"] is True
    assert wait_stage["result"]["matches"][0]["content_path"] == "/downloads"


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
    assert target_stage["ok"] is True
    assert target_stage["result"]["target_tracker"] == "MTEAM"
    assert target_stage["result"]["verified_content"] is True
    assert target_stage["result"]["metadata"]["name"] == "Name"
    assert target_stage["result"]["files"]["preview"].endswith("mteam-prepare-preview.json")


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
    stages = [{"stage": "match", "ok": True, "result": {"count": 1}}]

    preview = build_mteam_prepare_preview(source_info, ["MTEAM"], stages, "/downloads/Example.Movie.2024")

    assert preview["blockers"] == []
    assert preview["verified_content"] is True
    assert preview["meta_draft"]["type"] == "REMUX"
    assert preview["field_mapping"]["category"] == 439
    assert preview["field_mapping"]["standard"] == 6


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

    package = write_mteam_prepare_package(source_info, ["MTEAM"], [{"stage": "match", "ok": True, "result": {"count": 1}}], "/downloads/Example", str(tmp_path))

    assert package["package_dir"].endswith("U2-60635-to-MTEAM")
    assert package["files"]["preview"].endswith("mteam-prepare-preview.json")
    assert package["files"]["meta_draft"].endswith("mteam-meta-draft.json")
    assert package["files"]["field_mapping"].endswith("mteam-field-mapping.json")
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-prepare-preview.json").exists()
    assert (tmp_path / "U2-60635-to-MTEAM" / "mteam-field-mapping.json").read_text(encoding="utf-8").strip().startswith("{")


@pytest.mark.asyncio
async def test_qbit_service_adds_torrent_file_with_fake_client(tmp_path) -> None:
    fake_client = FakeQbitClient()
    torrent_path = tmp_path / "source.torrent"
    torrent_path.write_bytes(b"d4:infod")
    service = QbitReadOnlyService({}, qbit_client=fake_client)

    result = await service.add_torrent_file(
        torrent_path=str(torrent_path),
        save_path="/downloads",
        category="pt",
        tags="U2",
        paused=True,
    )

    assert result["save_path"] == "/downloads"
    assert result["category"] == "pt"
    assert result["tags"] == "U2"
    assert fake_client.added_kwargs["save_path"] == "/downloads"
    assert fake_client.added_kwargs["category"] == "pt"
    assert fake_client.added_kwargs["tags"] == "U2"
    assert fake_client.added_kwargs["paused"] is True


@pytest.mark.asyncio
async def test_qbit_service_waits_for_completed_match() -> None:
    service = QbitReadOnlyService({}, qbit_client=FakeQbitClient())

    result = await service.wait_for_completion(content_path="/downloads/One", timeout=0, interval=0.1)

    assert result["complete"] is True
    assert result["matches"][0]["hash"] == "a" * 40


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
