"""Dedicated CLI surface for focused PT retorrent automation."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.ptcli.config import load_config, resolve_client_config
from src.ptcli.credentials import build_flow_check
from src.ptcli.doctor import build_doctor_check, extend_doctor_check
from src.ptcli.flows import flow_profiles_to_dicts, get_flow_profiles
from src.ptcli.mainland import CHINESE_PT_TRACKERS, normalize_tracker, parse_tracker_list, unsupported_trackers
from src.ptcli.qbit import QbitReadOnlyService, match_torrents, summaries_to_dicts
from src.ptcli.rules import build_rule_check, get_rule_profiles, rule_profiles_to_dicts
from src.ptcli.source import download_source_torrent, extract_torrent_id, fetch_source_info, source_info_has_signal
from src.ptcli.target import (
    build_mteam_upload_preflight,
    create_mteam_upload_torrent_candidate,
    search_mteam_duplicates,
    upload_mteam_from_package,
    write_mteam_prepare_package,
)


@dataclass(frozen=True)
class RetorrentPlan:
    source_tracker: str
    source_torrent_id: str
    target_trackers: list[str]
    content_path: str | None
    client: str
    dry_run: bool
    accept_rules: bool
    flow_profiles: list[dict[str, Any]]
    rule_profiles: list[dict[str, Any]]
    blockers: list[str]
    commands: list[dict[str, str]]
    steps: list[str]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ptcli",
        description="Focused CLI for compliant mainland/CN PT retorrent workflows.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    sites = subparsers.add_parser("sites", help="List supported tracker codes for the focused CLI.")
    sites.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    rules = subparsers.add_parser("rules", help="Show rule review profiles for supported trackers.")
    rules.add_argument("--trackers", help="Optional comma-separated tracker codes. Defaults to all supported trackers.")
    rules.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    rule_check = subparsers.add_parser("rule-check", help="Run executable rule gates for a source/target workflow.")
    rule_check.add_argument("--from", dest="source_tracker", required=True, help="Source tracker code.")
    rule_check.add_argument("--to", dest="target_trackers", required=True, help="Target tracker codes, comma-separated.")
    rule_check.add_argument("--accept-rules", action="store_true", help="Acknowledge that source and target tracker rules have been manually reviewed.")
    rule_check.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    inspect = subparsers.add_parser("inspect", help="Read qBittorrent torrent state without modifying anything.")
    inspect.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    inspect.add_argument("--client", default="default", help="Configured qBittorrent client name, defaults to config default.")
    inspect.add_argument("--hash", dest="torrent_hash", help="Optional torrent hash to inspect.")
    inspect.add_argument("--limit", type=int, default=20, help="Maximum number of torrents to return when listing.")
    inspect.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    match = subparsers.add_parser("match", help="Find qBittorrent torrents matching a local seedbox path.")
    match.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    match.add_argument("--client", default="default", help="Configured qBittorrent client name, defaults to config default.")
    match.add_argument("--path", required=True, help="Local content path to match against qBittorrent.")
    match.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    export = subparsers.add_parser("export", help="Export an existing .torrent from qBittorrent without modifying the client.")
    export.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    export.add_argument("--client", default="default", help="Configured qBittorrent client name, defaults to config default.")
    export.add_argument("--hash", dest="torrent_hash", required=True, help="Torrent hash to export.")
    export.add_argument("--output-dir", required=True, help="Directory where the .torrent file will be written.")
    export.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    source_info = subparsers.add_parser("source-info", help="Fetch source tracker metadata by torrent id/details URL.")
    source_info.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    source_info.add_argument("--tracker", required=True, help="Source tracker code, initially U2, CHD, or MTEAM.")
    source_info.add_argument("--source-id", required=True, help="Source tracker torrent id or details URL.")
    source_info.add_argument("--base-dir", help="Project/base directory used for cookies, defaults to current directory.")
    source_info.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    source_download = subparsers.add_parser("source-download", help="Download a source .torrent to an output directory.")
    source_download.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    source_download.add_argument("--tracker", required=True, help="Source tracker code, initially U2, CHD, or MTEAM.")
    source_download.add_argument("--source-id", required=True, help="Source tracker torrent id or details URL.")
    source_download.add_argument("--to", dest="target_trackers", help="Target tracker codes for executable source-download rule gates.")
    source_download.add_argument("--output-dir", required=True, help="Directory where the source .torrent file will be written.")
    source_download.add_argument("--base-dir", help="Project/base directory used for cookies, defaults to current directory.")
    source_download.add_argument("--accept-rules", action="store_true", help="Acknowledge that source and target tracker rules have been manually reviewed.")
    source_download.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    flow_check = subparsers.add_parser("flow-check", help="Check local config readiness for a reference retorrent flow.")
    flow_check.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    flow_check.add_argument("--from", dest="source_tracker", required=True, help="Source tracker code.")
    flow_check.add_argument("--source-id", required=True, help="Source tracker torrent id or details URL.")
    flow_check.add_argument("--to", dest="target_trackers", required=True, help="Target tracker codes, comma-separated.")
    flow_check.add_argument("--client", default="default", help="Configured qBittorrent client name, defaults to config default.")
    flow_check.add_argument("--base-dir", help="Project/base directory used for cookies, defaults to current directory.")
    flow_check.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    doctor = subparsers.add_parser("doctor", help="Run a live-readiness checklist before executing a retorrent workflow.")
    doctor.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    doctor.add_argument("--from", dest="source_tracker", required=True, help="Source tracker code.")
    doctor.add_argument("--source-id", required=True, help="Source tracker torrent id or details URL.")
    doctor.add_argument("--to", dest="target_trackers", required=True, help="Target tracker codes, comma-separated.")
    doctor.add_argument("--client", default="default", help="Configured qBittorrent client name, defaults to config default.")
    doctor.add_argument("--base-dir", help="Project/base directory used for cookies, defaults to current directory.")
    doctor.add_argument("--path", dest="content_path", help="Existing local content path on the seedbox.")
    doctor.add_argument("--package-dir", help="Directory created by pipeline --prepare-target.")
    doctor.add_argument("--target-torrent-file", help="MTEAM .torrent file intended for target upload.")
    doctor.add_argument("--accept-rules", action="store_true", help="Acknowledge that source and target tracker rules have been manually reviewed.")
    doctor.add_argument("--target-execute", action="store_true", help="Check readiness for a live target upload.")
    doctor.add_argument("--confirm-upload", action="store_true", help="Confirm manual rule review and live upload intent.")
    doctor.add_argument("--download-uploaded-torrent", action="store_true", help="Check follow-up download of the generated MTEAM torrent file.")
    doctor.add_argument("--inject-uploaded-torrent", action="store_true", help="Check follow-up qBittorrent injection after target upload.")
    doctor.add_argument("--uploaded-save-path", help="qBittorrent save path required by --inject-uploaded-torrent.")
    doctor.add_argument("--wait-uploaded-complete", action="store_true", help="Check qBittorrent completion wait after uploaded target torrent injection.")
    doctor.add_argument("--connect-qbit", action="store_true", help="Probe qBittorrent connectivity by listing one torrent.")
    doctor.add_argument("--probe-source", action="store_true", help="Probe source tracker metadata lookup with the configured credentials/cookies.")
    doctor.add_argument("--probe-target", action="store_true", help="Probe MTEAM target duplicate-search API with the source metadata signal.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    pipeline = subparsers.add_parser("pipeline", help="Run a read-only dry-run pipeline: flow-check, source-info, and optional qBittorrent match.")
    pipeline.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    pipeline.add_argument("--from", dest="source_tracker", required=True, help="Source tracker code.")
    pipeline.add_argument("--source-id", required=True, help="Source tracker torrent id or details URL.")
    pipeline.add_argument("--to", dest="target_trackers", required=True, help="Target tracker codes, comma-separated.")
    pipeline.add_argument("--path", dest="content_path", help="Existing local content path on the seedbox.")
    pipeline.add_argument("--client", default="default", help="Configured qBittorrent client name, defaults to config default.")
    pipeline.add_argument("--base-dir", help="Project/base directory used for cookies, defaults to current directory.")
    pipeline.add_argument("--download-source", action="store_true", help="Download the source .torrent only after earlier pipeline stages pass.")
    pipeline.add_argument("--output-dir", default="./tmp/source", help="Directory for --download-source output.")
    pipeline.add_argument("--inject-source", action="store_true", help="Add the downloaded source .torrent to qBittorrent after download succeeds.")
    pipeline.add_argument("--save-path", help="qBittorrent save path required by --inject-source.")
    pipeline.add_argument("--qbit-category", help="Optional qBittorrent category for --inject-source.")
    pipeline.add_argument("--qbit-tags", help="Optional qBittorrent tags for --inject-source.")
    pipeline.add_argument("--paused", action="store_true", help="Add injected source torrent paused.")
    pipeline.add_argument("--wait-complete", action="store_true", help="Wait for qBittorrent task completion after injection or by --path.")
    pipeline.add_argument("--wait-timeout", type=float, default=3600.0, help="Seconds to wait with --wait-complete.")
    pipeline.add_argument("--wait-interval", type=float, default=30.0, help="Polling interval seconds for --wait-complete.")
    pipeline.add_argument("--prepare-target", action="store_true", help="Build a dry-run target preparation preview after prior stages.")
    pipeline.add_argument("--target-output-dir", default="./tmp/target", help="Directory for --prepare-target review package files.")
    pipeline.add_argument("--check-dupes", action="store_true", help="Run target duplicate search after source metadata is available.")
    pipeline.add_argument("--accept-rules", action="store_true", help="Acknowledge that source and target tracker rules have been manually reviewed.")
    pipeline.add_argument("--upload-target", action="store_true", help="Run the target upload stage after --prepare-target succeeds.")
    pipeline.add_argument("--target-torrent-file", help="MTEAM .torrent file used by --upload-target.")
    pipeline.add_argument("--export-target-torrent", action="store_true", help="Export the matched qBittorrent .torrent as a target upload candidate when --target-torrent-file is not provided.")
    pipeline.add_argument("--target-torrent-output-dir", default="./tmp/exported", help="Directory for --export-target-torrent output.")
    pipeline.add_argument("--sanitize-target-torrent", action="store_true", help="Create a cleaned MTEAM upload candidate from --target-torrent-file before upload.")
    pipeline.add_argument("--target-execute", action="store_true", help="Submit the target upload when every gate passes.")
    pipeline.add_argument("--confirm-upload", action="store_true", help="Required with --target-execute to confirm manual rule review and live upload intent.")
    pipeline.add_argument("--write-payload", action="store_true", help="Write mteam-upload-payload.json during target upload preflight.")
    pipeline.add_argument("--download-uploaded-torrent", action="store_true", help="After target upload succeeds, download the generated MTEAM torrent file.")
    pipeline.add_argument("--uploaded-output-dir", help="Directory for --download-uploaded-torrent. Defaults to the package directory.")
    pipeline.add_argument("--inject-uploaded-torrent", action="store_true", help="Add the downloaded target torrent to qBittorrent after upload.")
    pipeline.add_argument("--uploaded-save-path", help="qBittorrent save path required by --inject-uploaded-torrent.")
    pipeline.add_argument("--uploaded-qbit-category", help="Optional qBittorrent category for --inject-uploaded-torrent.")
    pipeline.add_argument("--uploaded-qbit-tags", help="Optional qBittorrent tags for --inject-uploaded-torrent.")
    pipeline.add_argument("--uploaded-paused", action="store_true", help="Add uploaded target torrent to qBittorrent paused.")
    pipeline.add_argument("--wait-uploaded-complete", action="store_true", help="Wait for the injected target torrent to become complete in qBittorrent.")
    pipeline.add_argument("--uploaded-wait-timeout", type=float, default=600.0, help="Seconds to wait with --wait-uploaded-complete.")
    pipeline.add_argument("--uploaded-wait-interval", type=float, default=15.0, help="Polling interval seconds for --wait-uploaded-complete.")
    pipeline.add_argument("--write-summary", action="store_true", help="Write ptcli-run-summary.json for audit and automation handoff.")
    pipeline.add_argument("--summary-output-dir", help="Directory for --write-summary. Defaults to target package or ./tmp/retorrent-runs.")
    pipeline.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    target_upload = subparsers.add_parser("target-upload", help="Preflight a prepared target package before live upload.")
    target_upload.add_argument("--config", help="Path to config.py, defaults to data/config.py. Required for --execute.")
    target_upload.add_argument("--package-dir", required=True, help="Directory created by pipeline --prepare-target.")
    target_upload.add_argument("--torrent-file", help="MTEAM .torrent file to include in the upload payload summary.")
    target_upload.add_argument("--write-payload", action="store_true", help="Write mteam-upload-payload.json into the package directory.")
    target_upload.add_argument("--execute", action="store_true", help="Submit the prepared payload to MTEAM after every gate passes.")
    target_upload.add_argument("--confirm-upload", action="store_true", help="Required with --execute to confirm manual rule review and live upload intent.")
    target_upload.add_argument("--download-uploaded-torrent", action="store_true", help="After live upload succeeds, download the generated MTEAM torrent file.")
    target_upload.add_argument("--uploaded-output-dir", help="Directory for --download-uploaded-torrent. Defaults to the package directory.")
    target_upload.add_argument("--inject-uploaded-torrent", action="store_true", help="Add the downloaded target torrent to qBittorrent after upload.")
    target_upload.add_argument("--uploaded-save-path", help="qBittorrent save path required by --inject-uploaded-torrent.")
    target_upload.add_argument("--uploaded-qbit-category", help="Optional qBittorrent category for --inject-uploaded-torrent.")
    target_upload.add_argument("--uploaded-qbit-tags", help="Optional qBittorrent tags for --inject-uploaded-torrent.")
    target_upload.add_argument("--uploaded-paused", action="store_true", help="Add uploaded target torrent to qBittorrent paused.")
    target_upload.add_argument("--wait-uploaded-complete", action="store_true", help="Wait for the injected target torrent to become complete in qBittorrent.")
    target_upload.add_argument("--uploaded-wait-timeout", type=float, default=600.0, help="Seconds to wait with --wait-uploaded-complete.")
    target_upload.add_argument("--uploaded-wait-interval", type=float, default=15.0, help="Polling interval seconds for --wait-uploaded-complete.")
    target_upload.add_argument("--write-summary", action="store_true", help="Write ptcli-target-upload-summary.json for audit and automation handoff.")
    target_upload.add_argument("--summary-output-dir", help="Directory for --write-summary. Defaults to --package-dir.")
    target_upload.add_argument("--client", default="default", help="Configured qBittorrent client name for --inject-uploaded-torrent.")
    target_upload.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    retorrent = subparsers.add_parser("retorrent", help="Plan or execute a retorrent workflow between supported trackers.")
    retorrent.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    retorrent.add_argument("--from", dest="source_tracker", required=True, help="Source tracker code, for example MTEAM.")
    retorrent.add_argument("--source-id", required=True, help="Source tracker torrent id or details URL.")
    retorrent.add_argument("--to", dest="target_trackers", required=True, help="Target tracker codes, comma-separated.")
    retorrent.add_argument("--path", dest="content_path", help="Existing local content path on the seedbox.")
    retorrent.add_argument("--client", default="default", help="Configured torrent client name, defaults to config default.")
    retorrent.add_argument("--base-dir", help="Project/base directory used for cookies, defaults to current directory.")
    retorrent.add_argument("--execute", action="store_true", help="Run the full reference pipeline after every gate passes.")
    retorrent.add_argument("--dry-run", action="store_true", help="Plan only; do not download, upload, or inject torrents.")
    retorrent.add_argument("--output-dir", default="./tmp/source", help="Directory for downloaded source .torrent files.")
    retorrent.add_argument("--save-path", help="qBittorrent save path for source injection. Required for --execute without --path.")
    retorrent.add_argument("--qbit-category", help="Optional qBittorrent category for source injection.")
    retorrent.add_argument("--qbit-tags", help="Optional qBittorrent tags for source injection.")
    retorrent.add_argument("--paused", action="store_true", help="Add injected source torrent paused.")
    retorrent.add_argument("--wait-timeout", type=float, default=3600.0, help="Seconds to wait for qBittorrent completion during --execute.")
    retorrent.add_argument("--wait-interval", type=float, default=30.0, help="Polling interval seconds during --execute.")
    retorrent.add_argument("--target-output-dir", default="./tmp/target", help="Directory for MTEAM target preparation package.")
    retorrent.add_argument("--target-torrent-file", help="MTEAM .torrent file used by the live upload stage.")
    retorrent.add_argument("--export-target-torrent", action="store_true", help="Export the matched qBittorrent .torrent as the target upload candidate if --target-torrent-file is omitted.")
    retorrent.add_argument("--target-torrent-output-dir", default="./tmp/exported", help="Directory for --export-target-torrent output.")
    retorrent.add_argument("--no-sanitize-target-torrent", dest="sanitize_target_torrent", action="store_false", default=True, help="Use the provided --target-torrent-file as-is instead of creating a cleaned MTEAM upload candidate.")
    retorrent.add_argument("--write-payload", action="store_true", help="Write mteam-upload-payload.json during upload preflight.")
    retorrent.add_argument("--confirm-upload", action="store_true", help="Required with --execute to confirm manual rule review and live upload intent.")
    retorrent.add_argument("--download-uploaded-torrent", action="store_true", help="After target upload succeeds, download the generated MTEAM torrent file. Enabled automatically by --execute.")
    retorrent.add_argument("--uploaded-output-dir", help="Directory for --download-uploaded-torrent. Defaults to the package directory.")
    retorrent.add_argument("--inject-uploaded-torrent", action="store_true", help="Add the downloaded MTEAM torrent to qBittorrent after upload. Enabled automatically by --execute.")
    retorrent.add_argument("--uploaded-save-path", help="qBittorrent save path for uploaded target torrent injection.")
    retorrent.add_argument("--uploaded-qbit-category", help="Optional qBittorrent category for uploaded target torrent injection.")
    retorrent.add_argument("--uploaded-qbit-tags", help="Optional qBittorrent tags for uploaded target torrent injection.")
    retorrent.add_argument("--uploaded-paused", action="store_true", help="Add uploaded target torrent paused.")
    retorrent.add_argument("--uploaded-wait-timeout", type=float, default=600.0, help="Seconds to wait for the uploaded target torrent to become complete during --execute; uploaded completion wait is enabled automatically.")
    retorrent.add_argument("--uploaded-wait-interval", type=float, default=15.0, help="Polling interval seconds for uploaded target torrent completion during --execute; uploaded completion wait is enabled automatically.")
    retorrent.add_argument("--write-summary", action="store_true", help="Write ptcli-run-summary.json during --execute for audit and automation handoff.")
    retorrent.add_argument("--summary-output-dir", help="Directory for --write-summary. Defaults to target package or ./tmp/retorrent-runs.")
    retorrent.add_argument(
        "--accept-rules",
        action="store_true",
        help="Confirm you have checked and will follow every source/target tracker rule.",
    )
    retorrent.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    return parser


async def inspect_qbit(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    client_name, client_config = resolve_client_config(config, args.client)
    service = QbitReadOnlyService(client_config)
    torrents = await service.list_torrents(torrent_hash=args.torrent_hash, limit=args.limit)
    return {
        "status": "ok",
        "client": client_name,
        "count": len(torrents),
        "torrents": summaries_to_dicts(torrents),
    }


async def match_qbit(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    client_name, client_config = resolve_client_config(config, args.client)
    service = QbitReadOnlyService(client_config)
    torrents = await service.list_torrents()
    matches = match_torrents(torrents, args.path)
    return {
        "status": "ok",
        "client": client_name,
        "path": args.path,
        "count": len(matches),
        "matches": summaries_to_dicts(matches),
    }


async def export_qbit(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    client_name, client_config = resolve_client_config(config, args.client)
    service = QbitReadOnlyService(client_config)
    output_path = await service.export_torrent(args.torrent_hash, args.output_dir)
    return {
        "status": "ok",
        "client": client_name,
        "hash": args.torrent_hash.strip().lower(),
        "path": str(output_path),
    }


async def source_info(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    info = await fetch_source_info(config, args.tracker, args.source_id, base_dir=args.base_dir)
    return {
        "status": "ok",
        "source": info.to_dict(),
    }


async def source_download(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    source_tracker = normalize_tracker(args.tracker)
    if not args.target_trackers:
        return {
            "status": "blocked",
            "tracker": source_tracker,
            "blockers": ["--to is required so source-download can run source/target rule gates before downloading."],
        }
    target_trackers = parse_tracker_list(args.target_trackers)
    rule_check = build_rule_check(source_tracker, target_trackers, accept_rules=args.accept_rules)
    if not rule_check.get("ready"):
        return {
            "status": "blocked",
            "tracker": source_tracker,
            "target_trackers": target_trackers,
            "rule_check": rule_check,
            "blockers": _rule_check_blockers(rule_check),
        }
    output_path = await download_source_torrent(config, source_tracker, args.source_id, args.output_dir, base_dir=args.base_dir)
    return {
        "status": "ok",
        "tracker": source_tracker,
        "target_trackers": target_trackers,
        "rule_check": rule_check,
        "path": str(output_path),
    }


def build_plan(args: argparse.Namespace) -> RetorrentPlan:
    source_tracker = normalize_tracker(args.source_tracker)
    source_torrent_id = extract_torrent_id(args.source_id)
    target_trackers = parse_tracker_list(args.target_trackers)
    invalid = unsupported_trackers([source_tracker, *target_trackers])
    if invalid:
        raise ValueError(f"Unsupported tracker(s) for focused CLI scope: {', '.join(invalid)}")
    if source_tracker in target_trackers:
        raise ValueError("Source tracker cannot also be a target tracker.")
    if not args.dry_run and not args.accept_rules:
        raise ValueError("Non-dry-run retorrent requires --accept-rules.")

    rule_profiles = get_rule_profiles([source_tracker, *target_trackers])
    blockers: list[str] = []
    if not args.dry_run and any(profile.review_required for profile in rule_profiles) and not args.accept_rules:
        blockers.append("Rule review acknowledgement is required before any non-dry-run action.")
    if any(profile.automation_status != "enabled" for profile in rule_profiles):
        blockers.append("Tracker rule profiles are in planning mode; upload/download automation is not enabled yet.")
    flow_profiles = get_flow_profiles(source_tracker, target_trackers)
    if not flow_profiles:
        blockers.append("This source/target combination is not one of the first reference flows yet.")

    steps = [
        "validate source and target tracker scope",
        "load tracker credentials and per-site rule profile",
        "fetch source torrent metadata without bypassing site restrictions",
        "locate or verify matching content in qBittorrent",
        "prepare target-specific metadata, description, screenshots, and torrent",
        "run target tracker dupe and rule checks",
        "upload only to targets that pass validation",
        "inject uploaded torrents into qBittorrent for seeding",
    ]
    commands = build_plan_commands(source_tracker, source_torrent_id, target_trackers, args.content_path)

    return RetorrentPlan(
        source_tracker=source_tracker,
        source_torrent_id=source_torrent_id,
        target_trackers=target_trackers,
        content_path=args.content_path,
        client=args.client,
        dry_run=bool(args.dry_run),
        accept_rules=bool(args.accept_rules),
        flow_profiles=flow_profiles_to_dicts(flow_profiles),
        rule_profiles=rule_profiles_to_dicts(rule_profiles),
        blockers=blockers,
        commands=commands,
        steps=steps,
    )


async def retorrent_payload(args: argparse.Namespace) -> dict[str, Any]:
    plan = build_plan(args)
    plan_payload = asdict(plan)
    if not args.execute:
        return {"status": "ok", "plan": plan_payload}
    if args.dry_run:
        return {"status": "blocked", "plan": plan_payload, "blockers": ["--execute cannot be combined with --dry-run."]}
    if plan.blockers:
        return {"status": "blocked", "plan": plan_payload, "blockers": plan.blockers}
    if not args.confirm_upload:
        return {"status": "blocked", "plan": plan_payload, "blockers": ["--confirm-upload is required with retorrent --execute."]}
    if not args.content_path and not args.save_path:
        return {"status": "blocked", "plan": plan_payload, "blockers": ["--path or --save-path is required with retorrent --execute."]}

    pipeline_args = _pipeline_args_from_retorrent(args)
    pipeline_result = await pipeline_payload(pipeline_args)
    closure = pipeline_result.get("closure") if isinstance(pipeline_result.get("closure"), dict) else None
    evidence = pipeline_result.get("evidence") if isinstance(pipeline_result.get("evidence"), dict) else None
    summary = pipeline_result.get("summary") if isinstance(pipeline_result.get("summary"), dict) else None
    ready = bool(pipeline_result.get("ready"))
    blockers = _retorrent_execute_blockers(pipeline_result, closure, ready)
    next_actions = _retorrent_execute_next_actions(pipeline_result, blockers)
    return {
        "status": "complete" if not blockers else "blocked",
        "plan": plan_payload,
        "pipeline": pipeline_result,
        "closure": closure,
        "evidence": evidence,
        "summary": summary,
        "summary_file": pipeline_result.get("summary_file"),
        "ready": ready,
        "complete": not blockers,
        "blockers": blockers,
        "next_actions": next_actions,
    }


def _retorrent_execute_blockers(pipeline_result: dict[str, Any], closure: dict[str, Any] | None, ready: bool) -> list[str]:
    blockers: list[str] = []
    if closure is None:
        blockers.append("pipeline did not return a closure summary.")
    else:
        closure_blockers = closure.get("blockers")
        if isinstance(closure_blockers, list):
            blockers.extend(str(blocker) for blocker in closure_blockers)
        if closure.get("complete") is not True and not blockers:
            blockers.append("retorrent closure did not complete.")
    if not ready:
        blockers.append("pipeline did not report ready.")
    if pipeline_result.get("status") not in {None, "ok", "complete"}:
        blockers.append(f"pipeline status is {pipeline_result.get('status')}.")
    return blockers


def _retorrent_execute_next_actions(pipeline_result: dict[str, Any], blockers: list[str]) -> list[str]:
    if not blockers:
        return ["Retorrent closure is complete; verify the target tracker page and qBittorrent seeding state."]
    pipeline_actions = pipeline_result.get("next_actions")
    if isinstance(pipeline_actions, list) and pipeline_actions:
        return [str(action) for action in pipeline_actions]
    return [f"Fix {blocker}" for blocker in blockers]


def _pipeline_args_from_retorrent(args: argparse.Namespace) -> argparse.Namespace:
    needs_source_download = not bool(args.content_path)
    return argparse.Namespace(
        command="pipeline",
        config=args.config,
        source_tracker=args.source_tracker,
        source_id=args.source_id,
        target_trackers=args.target_trackers,
        content_path=args.content_path,
        client=args.client,
        base_dir=args.base_dir,
        download_source=needs_source_download,
        output_dir=args.output_dir,
        inject_source=needs_source_download,
        save_path=args.save_path,
        qbit_category=args.qbit_category,
        qbit_tags=args.qbit_tags,
        paused=args.paused,
        wait_complete=needs_source_download or bool(args.content_path),
        wait_timeout=args.wait_timeout,
        wait_interval=args.wait_interval,
        prepare_target=True,
        target_output_dir=args.target_output_dir,
        check_dupes=True,
        accept_rules=args.accept_rules,
        upload_target=True,
        target_torrent_file=args.target_torrent_file,
        export_target_torrent=args.export_target_torrent or not bool(args.target_torrent_file),
        target_torrent_output_dir=args.target_torrent_output_dir,
        sanitize_target_torrent=args.sanitize_target_torrent,
        target_execute=True,
        confirm_upload=args.confirm_upload,
        write_payload=args.write_payload,
        download_uploaded_torrent=True,
        uploaded_output_dir=args.uploaded_output_dir,
        inject_uploaded_torrent=True,
        uploaded_save_path=args.uploaded_save_path,
        uploaded_qbit_category=args.uploaded_qbit_category,
        uploaded_qbit_tags=args.uploaded_qbit_tags,
        uploaded_paused=args.uploaded_paused,
        wait_uploaded_complete=True,
        uploaded_wait_timeout=args.uploaded_wait_timeout,
        uploaded_wait_interval=args.uploaded_wait_interval,
        write_summary=args.write_summary,
        summary_output_dir=args.summary_output_dir,
        json=getattr(args, "json", False),
    )


def flow_check_payload(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    return build_flow_check(config, args.source_tracker, args.source_id, args.target_trackers, args.client, base_dir=args.base_dir)


async def doctor_payload(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    payload = build_doctor_check(
        config,
        source_tracker=args.source_tracker,
        source_id=args.source_id,
        target_trackers=args.target_trackers,
        client=args.client,
        base_dir=args.base_dir,
        content_path=args.content_path,
        package_dir=args.package_dir,
        target_torrent_file=args.target_torrent_file,
        accept_rules=args.accept_rules,
        target_execute=args.target_execute,
        confirm_upload=args.confirm_upload,
        download_uploaded_torrent=args.download_uploaded_torrent,
        inject_uploaded_torrent=args.inject_uploaded_torrent,
        uploaded_save_path=args.uploaded_save_path,
        wait_uploaded_complete=args.wait_uploaded_complete,
    )
    live_checks = []
    if args.connect_qbit:
        live_checks.append(await _qbit_connection_check(config, args.client))
    source_probe_info = None
    if args.probe_source or args.probe_target:
        source_probe = await _source_connection_check(config, args.source_tracker, args.source_id, args.base_dir)
        live_checks.append(source_probe["check"])
        source_probe_info = source_probe.get("source")
    if args.probe_target:
        live_checks.append(await _target_connection_check(config, args.target_trackers, source_probe_info))
    if live_checks:
        payload = extend_doctor_check(payload, live_checks, target_execute=args.target_execute)
    return payload


async def target_upload_payload(args: argparse.Namespace) -> dict[str, Any]:
    if not args.execute:
        preflight = build_mteam_upload_preflight(args.package_dir, execute=False, torrent_file=args.torrent_file, write_payload=args.write_payload)
        return _maybe_write_target_upload_summary(args, preflight, preflight)
    if not args.torrent_file:
        preflight = build_mteam_upload_preflight(args.package_dir, execute=True, torrent_file=args.torrent_file, write_payload=args.write_payload)
        return _maybe_write_target_upload_summary(args, preflight, preflight)
    preflight = build_mteam_upload_preflight(args.package_dir, execute=True, torrent_file=args.torrent_file, write_payload=args.write_payload)
    blockers = [*preflight["blockers"], *_target_upload_execute_blockers(args)]
    if blockers:
        blocked = {**preflight, "status": "blocked", "dry_run": False, "blockers": blockers}
        return _maybe_write_target_upload_summary(args, blocked, preflight)
    config = load_config(args.config)
    result = await upload_mteam_from_package(
        config,
        args.package_dir,
        args.torrent_file,
        execute=args.execute,
        confirm_upload=args.confirm_upload,
        write_payload=args.write_payload,
        download_uploaded=args.download_uploaded_torrent,
        uploaded_output_dir=args.uploaded_output_dir,
    )
    if args.inject_uploaded_torrent and result.get("status") == "uploaded" and isinstance(result.get("downloaded_torrent"), dict):
        downloaded_path = str(result["downloaded_torrent"]["path"])
        inject_result = await _inject_source_with_config(
            config,
            args.client,
            downloaded_path,
            args.uploaded_save_path,
            args.uploaded_qbit_category,
            args.uploaded_qbit_tags,
            args.uploaded_paused,
        )
        injected_payload = _with_uploaded_injection(result, inject_result)
        if args.wait_uploaded_complete:
            waited_payload = await _with_uploaded_wait(config, args, injected_payload, args.uploaded_save_path)
            return _maybe_write_target_upload_summary(args, waited_payload, preflight)
        return _maybe_write_target_upload_summary(args, injected_payload, preflight)
    return _maybe_write_target_upload_summary(args, result, preflight)


def _target_upload_execute_blockers(args: argparse.Namespace) -> list[str]:
    blockers: list[str] = []
    if not args.confirm_upload:
        blockers.append("MTEAM live upload requires --confirm-upload.")
    if not args.download_uploaded_torrent:
        blockers.append("target-upload --execute requires --download-uploaded-torrent so the generated MTEAM torrent can be seeded.")
    if not args.inject_uploaded_torrent:
        blockers.append("--inject-uploaded-torrent is required with target-upload --execute for full live retorrent closure.")
    elif not args.download_uploaded_torrent:
        blockers.append("--inject-uploaded-torrent requires --download-uploaded-torrent.")
    elif not args.uploaded_save_path:
        blockers.append("--uploaded-save-path is required with --inject-uploaded-torrent.")
    if args.wait_uploaded_complete and not args.inject_uploaded_torrent:
        blockers.append("--wait-uploaded-complete requires --inject-uploaded-torrent.")
    return blockers


def _maybe_write_target_upload_summary(args: argparse.Namespace, result: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    if not getattr(args, "write_summary", False):
        return result
    summary_file = _write_target_upload_summary(result, preflight, args.summary_output_dir or args.package_dir)
    summary = _target_upload_summary(result, preflight)
    return {**result, "summary": summary, "summary_file": summary_file}


def _write_target_upload_summary(result: dict[str, Any], preflight: dict[str, Any], output_dir: str) -> str:
    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / "ptcli-target-upload-summary.json"
    payload = {
        "summary": _target_upload_summary(result, preflight),
        "preflight": preflight,
        "result": result,
    }
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(destination)


def _target_upload_summary(result: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    injected_torrent = result.get("injected_torrent")
    uploaded_wait = result.get("uploaded_wait")
    blockers = _target_upload_result_blockers(result)
    return {
        "status": result.get("status"),
        "ready": result.get("status") in {"ready", "uploaded"} and not blockers,
        "uploaded": result.get("status") == "uploaded",
        "downloaded": isinstance(result.get("downloaded_torrent"), dict),
        "injected": _injected_torrent_verified(injected_torrent),
        "seeding_verified": isinstance(uploaded_wait, dict) and bool(uploaded_wait.get("complete")),
        "uploaded_torrent_hash": result.get("uploaded_torrent_hash") or _torrent_hash_from_result(injected_torrent),
        "blockers": blockers,
        "preflight_status": preflight.get("status"),
        "preflight_blockers": preflight.get("blockers", []),
    }


async def pipeline_payload(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    source_tracker = normalize_tracker(args.source_tracker)
    source_torrent_id = extract_torrent_id(args.source_id)
    target_trackers = parse_tracker_list(args.target_trackers)
    effective_content_path = args.content_path
    effective_target_torrent_file = args.target_torrent_file
    effective_source_torrent_hash: str | None = None

    stages: list[dict[str, Any]] = []
    flow_check_result = build_flow_check(config, source_tracker, source_torrent_id, ",".join(target_trackers), args.client, base_dir=args.base_dir)
    stages.append({"stage": "flow-check", "ok": bool(flow_check_result.get("ready")), "result": flow_check_result})

    source_info_result = await _pipeline_stage(
        "source-info",
        lambda: fetch_source_info(config, source_tracker, source_torrent_id, base_dir=args.base_dir),
        lambda info: info.to_dict(),
        validate=lambda info: source_info_has_signal(info),
        invalid_message="Source metadata lookup returned no usable identifiers, name, hash, description, or Douban data.",
    )
    stages.append(source_info_result)
    effective_source_torrent_hash = _source_torrent_hash_from_stage(source_info_result)
    rule_check_result = build_rule_check(source_tracker, target_trackers, accept_rules=args.accept_rules)
    stages.append({"stage": "rule-check", "ok": True, "result": rule_check_result})

    if args.download_source:
        if _required_stages_ok(stages, {"flow-check", "source-info"}) and _rule_check_ready(stages):
            source_download_result = await _pipeline_stage(
                "source-download",
                lambda: download_source_torrent(config, source_tracker, source_torrent_id, args.output_dir, base_dir=args.base_dir),
                lambda path: {"path": str(path)},
            )
            stages.append(source_download_result)
        else:
            stages.append(
                {
                    "stage": "source-download",
                    "ok": False,
                    "skipped": True,
                    "message": "Skipped because flow-check, source-info, or executable rule-check did not pass.",
                }
            )
    else:
        stages.append({"stage": "source-download", "ok": True, "skipped": True, "message": "--download-source not provided; source download skipped."})

    if args.inject_source:
        source_download_stage = _find_stage(stages, "source-download")
        if not args.save_path:
            stages.append({"stage": "inject-source", "ok": False, "skipped": True, "message": "--save-path is required when --inject-source is used."})
        elif not source_download_stage or not source_download_stage.get("ok") or source_download_stage.get("skipped"):
            stages.append({"stage": "inject-source", "ok": False, "skipped": True, "message": "Skipped because source-download did not complete successfully."})
        else:
            torrent_path = str(source_download_stage.get("result", {}).get("path", ""))
            inject_result = await _pipeline_stage(
                "inject-source",
                lambda: _inject_source_with_config(config, args.client, torrent_path, args.save_path, args.qbit_category, args.qbit_tags, args.paused),
                lambda payload: payload,
                validate=_injected_torrent_verified,
                invalid_message="qBittorrent source torrent injection was not verified.",
            )
            stages.append(inject_result)
            effective_source_torrent_hash = _torrent_hash_from_result(inject_result.get("result")) or effective_source_torrent_hash
    else:
        stages.append({"stage": "inject-source", "ok": True, "skipped": True, "message": "--inject-source not provided; qBittorrent injection skipped."})

    if args.wait_complete:
        inject_stage = _find_stage(stages, "inject-source")
        if inject_stage and inject_stage.get("ok") and not inject_stage.get("skipped"):
            wait_path = args.content_path or args.save_path
            wait_result = await _pipeline_stage(
                "wait-complete",
                lambda: _wait_complete_with_config(config, args.client, content_path=wait_path, torrent_hash=effective_source_torrent_hash, timeout=args.wait_timeout, interval=args.wait_interval),
                lambda payload: payload,
                validate=lambda payload: bool(payload.get("complete")),
                invalid_message="qBittorrent task did not complete before timeout.",
            )
            stages.append(wait_result)
            effective_content_path = effective_content_path or _content_path_from_stage(wait_result)
            effective_source_torrent_hash = _torrent_hash_from_stage(wait_result) or effective_source_torrent_hash
        elif args.content_path:
            wait_result = await _pipeline_stage(
                "wait-complete",
                lambda: _wait_complete_with_config(config, args.client, content_path=args.content_path, torrent_hash=effective_source_torrent_hash, timeout=args.wait_timeout, interval=args.wait_interval),
                lambda payload: payload,
                validate=lambda payload: bool(payload.get("complete")),
                invalid_message="qBittorrent task did not complete before timeout.",
            )
            stages.append(wait_result)
            effective_content_path = effective_content_path or _content_path_from_stage(wait_result)
            effective_source_torrent_hash = _torrent_hash_from_stage(wait_result) or effective_source_torrent_hash
        else:
            stages.append({"stage": "wait-complete", "ok": False, "skipped": True, "message": "--wait-complete requires successful injection or --path."})
    else:
        stages.append({"stage": "wait-complete", "ok": True, "skipped": True, "message": "--wait-complete not provided; qBittorrent wait skipped."})

    if effective_content_path:
        match_result = await _pipeline_stage(
            "match",
            lambda: _match_with_config(config, args.client, str(effective_content_path)),
            lambda payload: payload,
        )
        stages.append(match_result)
    else:
        stages.append({"stage": "match", "ok": True, "skipped": True, "message": "--path not provided; qBittorrent match skipped."})

    if args.check_dupes:
        source_stage = _find_stage(stages, "source-info")
        source_result = source_stage.get("result") if source_stage and source_stage.get("ok") else None
        if "MTEAM" not in target_trackers:
            stages.append({"stage": "target-dupe-check", "ok": False, "skipped": True, "message": "Only MTEAM duplicate search is enabled in this stage."})
        else:
            dupe_result = await _pipeline_stage(
                "target-dupe-check",
                lambda: search_mteam_duplicates(config, source_result if isinstance(source_result, dict) else None),
                lambda payload: payload,
                validate=lambda payload: bool(payload.get("searched")),
                invalid_message="MTEAM duplicate search could not run.",
            )
            stages.append(dupe_result)
    else:
        stages.append({"stage": "target-dupe-check", "ok": True, "skipped": True, "message": "--check-dupes not provided; target duplicate search skipped."})

    if args.prepare_target:
        source_stage = _find_stage(stages, "source-info")
        source_result = source_stage.get("result") if source_stage and source_stage.get("ok") else None
        target_prepare = write_mteam_prepare_package(
            source_result if isinstance(source_result, dict) else None,
            target_trackers,
            stages,
            effective_content_path,
            args.target_output_dir,
            accept_rules=args.accept_rules,
        )
        stages.append({"stage": "target-prepare", "ok": not target_prepare["blockers"], "result": target_prepare})
    else:
        stages.append({"stage": "target-prepare", "ok": True, "skipped": True, "message": "--prepare-target not provided; target preparation skipped."})

    if args.export_target_torrent:
        match_stage = _find_stage(stages, "match")
        torrent_hash = _torrent_hash_from_stage(match_stage) if match_stage else None
        if effective_target_torrent_file:
            stages.append({"stage": "target-torrent-export", "ok": True, "skipped": True, "message": "--target-torrent-file provided; qBittorrent export skipped."})
        elif not torrent_hash:
            stages.append({"stage": "target-torrent-export", "ok": False, "skipped": True, "message": "No matched qBittorrent hash is available for export."})
        else:
            export_stage = await _pipeline_stage(
                "target-torrent-export",
                lambda: _export_hash_with_config(config, args.client, torrent_hash, args.target_torrent_output_dir),
                lambda payload: payload,
            )
            stages.append(export_stage)
            if export_stage.get("ok") and isinstance(export_stage.get("result"), dict):
                effective_target_torrent_file = str(export_stage["result"].get("path") or effective_target_torrent_file or "")
    else:
        stages.append({"stage": "target-torrent-export", "ok": True, "skipped": True, "message": "--export-target-torrent not provided; target torrent export skipped."})

    if args.sanitize_target_torrent:
        if not effective_target_torrent_file:
            stages.append({"stage": "target-torrent-sanitize", "ok": False, "skipped": True, "message": "No target torrent file is available to sanitize."})
        elif str(effective_target_torrent_file).endswith(".mteam-upload.torrent"):
            stages.append({"stage": "target-torrent-sanitize", "ok": True, "skipped": True, "message": "Target torrent file is already a MTEAM upload candidate."})
        else:
            sanitize_stage = await _pipeline_stage(
                "target-torrent-sanitize",
                lambda: _sanitize_target_torrent_with_config(effective_target_torrent_file, args.target_torrent_output_dir),
                lambda payload: payload,
            )
            stages.append(sanitize_stage)
            if sanitize_stage.get("ok") and isinstance(sanitize_stage.get("result"), dict):
                effective_target_torrent_file = str(sanitize_stage["result"].get("path") or effective_target_torrent_file or "")
    else:
        stages.append({"stage": "target-torrent-sanitize", "ok": True, "skipped": True, "message": "--sanitize-target-torrent not provided; target torrent sanitizing skipped."})

    if args.upload_target:
        target_prepare_stage = _find_stage(stages, "target-prepare")
        if not target_prepare_stage or not target_prepare_stage.get("ok") or target_prepare_stage.get("skipped"):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "Skipped because target-prepare did not complete successfully."})
        elif not effective_target_torrent_file:
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "--target-torrent-file or --export-target-torrent is required when --upload-target is used."})
        elif args.target_execute and not args.download_uploaded_torrent:
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "pipeline --target-execute requires --download-uploaded-torrent so the generated target torrent can be seeded."})
        elif args.target_execute and not args.inject_uploaded_torrent:
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "pipeline --target-execute requires --inject-uploaded-torrent for full live retorrent closure."})
        elif args.inject_uploaded_torrent and not args.download_uploaded_torrent:
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "--inject-uploaded-torrent requires --download-uploaded-torrent."})
        elif args.inject_uploaded_torrent and not (args.uploaded_save_path or effective_content_path):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "--uploaded-save-path or an inferred completed content path is required with --inject-uploaded-torrent."})
        elif args.wait_uploaded_complete and not args.inject_uploaded_torrent:
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "--wait-uploaded-complete requires --inject-uploaded-torrent."})
        else:
            package_dir = str(target_prepare_stage.get("result", {}).get("package_dir", ""))
            upload_stage = await _pipeline_stage(
                "target-upload",
                lambda: _target_upload_with_config(config, args, package_dir, effective_content_path, effective_target_torrent_file),
                lambda payload: payload,
                validate=lambda payload: _target_upload_result_ready(
                    payload,
                    execute=args.target_execute,
                    download_uploaded=args.download_uploaded_torrent,
                    inject_uploaded=args.inject_uploaded_torrent,
                    wait_uploaded_complete=args.wait_uploaded_complete,
                ),
                invalid_message="Target upload stage did not complete every requested upload follow-up.",
            )
            stages.append(upload_stage)
    else:
        stages.append({"stage": "target-upload", "ok": True, "skipped": True, "message": "--upload-target not provided; target upload skipped."})

    ready = all(stage.get("ok", False) for stage in stages)
    blockers = _pipeline_stage_blockers(stages) if _pipeline_has_action(args) and not ready else []
    closure = _pipeline_closure(stages, effective_content_path, effective_source_torrent_hash, effective_target_torrent_file)
    evidence = _pipeline_evidence(closure)
    summary = _pipeline_run_summary(stages, ready, blockers, closure, evidence)
    payload = {
        "status": "blocked" if blockers else "ok",
        "source_tracker": source_tracker,
        "source_torrent_id": source_torrent_id,
        "source_torrent_hash": effective_source_torrent_hash,
        "target_trackers": target_trackers,
        "path": effective_content_path,
        "requested_path": args.content_path,
        "target_torrent_file": effective_target_torrent_file,
        "ready": ready,
        "blockers": blockers,
        "closure": closure,
        "evidence": evidence,
        "summary": summary,
        "next_actions": _pipeline_next_actions(args, blockers, closure),
        "stages": stages,
    }
    if getattr(args, "write_summary", False):
        summary_file = _write_run_summary(payload, args.summary_output_dir)
        payload["summary_file"] = summary_file
        summary["summary_file"] = summary_file
    return payload


async def _pipeline_stage(stage: str, operation: Any, serialize: Any, validate: Any | None = None, invalid_message: str | None = None) -> dict[str, Any]:
    try:
        result = await operation()
    except Exception as exc:
        return {"stage": stage, "ok": False, "error": str(exc)}
    if validate is not None and not validate(result):
        return {"stage": stage, "ok": False, "error": invalid_message or "Stage result did not pass validation.", "result": serialize(result)}
    return {"stage": stage, "ok": True, "result": serialize(result)}


def _required_stages_ok(stages: list[dict[str, Any]], required_stage_names: set[str]) -> bool:
    stage_status = {str(stage.get("stage")): bool(stage.get("ok")) for stage in stages}
    return all(stage_status.get(stage_name, False) for stage_name in required_stage_names)


def _rule_check_ready(stages: list[dict[str, Any]]) -> bool:
    stage = _find_stage(stages, "rule-check")
    if not stage or not stage.get("ok"):
        return False
    result = stage.get("result")
    return isinstance(result, dict) and bool(result.get("ready"))


def _pipeline_stage_blockers(stages: list[dict[str, Any]]) -> list[str]:
    blockers: list[str] = []
    for stage in stages:
        if stage.get("ok"):
            continue
        stage_name = str(stage.get("stage") or "unknown")
        reason = stage.get("error") or stage.get("message") or "stage did not complete."
        blockers.append(f"{stage_name}: {reason}")
        blockers.extend(f"{stage_name}: {detail}" for detail in _stage_result_blockers(stage_name, stage.get("result")))
    return blockers


def _stage_result_blockers(stage_name: str, result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []
    if stage_name == "target-upload":
        return _target_upload_result_blockers(result)
    if stage_name == "inject-source":
        return _source_inject_result_blockers(result)
    if stage_name == "wait-complete":
        return _wait_complete_result_blockers(result)
    return _string_list(result.get("blockers"))


def _source_inject_result_blockers(result: dict[str, Any]) -> list[str]:
    blockers = _string_list(result.get("blockers"))
    if result.get("verified_in_client") is False:
        _append_unique_string(blockers, "qBittorrent did not verify the injected source torrent in the client list.")
    return blockers


def _wait_complete_result_blockers(result: dict[str, Any]) -> list[str]:
    blockers = _string_list(result.get("blockers"))
    if result.get("complete") is False:
        _append_unique_string(blockers, "qBittorrent did not report the source torrent as complete.")
    return blockers


def _target_upload_result_blockers(result: dict[str, Any]) -> list[str]:
    blockers = _string_list(result.get("blockers"))
    _extend_unique_string(blockers, _nested_blockers(result.get("downloaded_torrent"), "downloaded_torrent"))
    _extend_unique_string(blockers, _nested_blockers(result.get("injected_torrent"), "injected_torrent"))
    _extend_unique_string(blockers, _nested_blockers(result.get("uploaded_wait"), "uploaded_wait"))
    uploaded_wait = result.get("uploaded_wait")
    if isinstance(uploaded_wait, dict) and uploaded_wait.get("complete") is False:
        _append_unique_string(blockers, "uploaded_wait: qBittorrent did not report the uploaded target torrent as complete.")
    return blockers


def _nested_blockers(payload: Any, label: str) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return [f"{label}: {blocker}" for blocker in _string_list(payload.get("blockers"))]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _extend_unique_string(items: list[str], additions: list[str]) -> None:
    for item in additions:
        _append_unique_string(items, item)


def _append_unique_string(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _pipeline_run_summary(stages: list[dict[str, Any]], ready: bool, blockers: list[str], closure: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    stage_statuses = [_pipeline_stage_status(stage) for stage in stages]
    return {
        "ready": ready,
        "complete": bool(closure.get("complete")),
        "status": "complete" if ready and closure.get("complete") else "blocked" if blockers or closure.get("blockers") else "incomplete",
        "blockers": blockers or (closure.get("blockers") if isinstance(closure.get("blockers"), list) else []),
        "stage_statuses": stage_statuses,
        "failed_stages": [stage["stage"] for stage in stage_statuses if not stage["ok"]],
        "completed_stages": [stage["stage"] for stage in stage_statuses if stage["ok"] and not stage["skipped"]],
        "skipped_stages": [stage["stage"] for stage in stage_statuses if stage["skipped"]],
        "gates": _pipeline_gate_summary(stages),
        "source": evidence.get("source", {}),
        "target": evidence.get("target", {}),
    }


def _pipeline_stage_status(stage: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": str(stage.get("stage") or "unknown"),
        "ok": bool(stage.get("ok")),
        "skipped": bool(stage.get("skipped")),
        "message": stage.get("error") or stage.get("message"),
    }


def _pipeline_gate_summary(stages: list[dict[str, Any]]) -> dict[str, Any]:
    flow_stage = _find_stage(stages, "flow-check")
    rule_stage = _find_stage(stages, "rule-check")
    dupe_stage = _find_stage(stages, "target-dupe-check")
    target_prepare_stage = _find_stage(stages, "target-prepare")
    flow_result = flow_stage.get("result") if isinstance(flow_stage, dict) else None
    rule_result = rule_stage.get("result") if isinstance(rule_stage, dict) else None
    dupe_result = dupe_stage.get("result") if isinstance(dupe_stage, dict) else None
    target_prepare_result = target_prepare_stage.get("result") if isinstance(target_prepare_stage, dict) else None
    upload_gate = target_prepare_result.get("upload_gate") if isinstance(target_prepare_result, dict) else None
    rule_review = target_prepare_result.get("rule_review") if isinstance(target_prepare_result, dict) else None
    return {
        "flow_check": {
            "ready": bool(flow_result.get("ready")) if isinstance(flow_result, dict) else False,
            "blockers": _failed_check_messages(flow_result.get("checks")) if isinstance(flow_result, dict) else [],
        },
        "rule_check": {
            "ready": bool(rule_result.get("ready")) if isinstance(rule_result, dict) else False,
            "rules_acknowledged": _check_ok(rule_result.get("checks"), "rules_acknowledged") if isinstance(rule_result, dict) else False,
            "blockers": _failed_check_messages(rule_result.get("checks")) if isinstance(rule_result, dict) else [],
        },
        "duplicate_check": {
            "searched": bool(dupe_result.get("searched")) if isinstance(dupe_result, dict) else False,
            "count": int(dupe_result.get("count", 0) or 0) if isinstance(dupe_result, dict) else 0,
            "ok": bool(dupe_result.get("searched")) and int(dupe_result.get("count", 0) or 0) == 0 if isinstance(dupe_result, dict) else False,
        },
        "upload_gate": {
            "ready": bool(upload_gate.get("ready")) if isinstance(upload_gate, dict) else False,
            "dupe_count": upload_gate.get("dupe_count") if isinstance(upload_gate, dict) else None,
            "blockers": upload_gate.get("blockers", []) if isinstance(upload_gate, dict) else [],
        },
        "rule_review": {
            "ready": bool(rule_review.get("rule_check_ready")) if isinstance(rule_review, dict) else False,
            "blockers": rule_review.get("blockers", []) if isinstance(rule_review, dict) else [],
        },
    }


def _failed_check_messages(checks: Any) -> list[str]:
    if not isinstance(checks, list):
        return []
    return [
        f"{check.get('name', 'check')}: {check.get('message', 'check failed')}"
        for check in checks
        if isinstance(check, dict) and not check.get("ok")
    ]


def _check_ok(checks: Any, name: str) -> bool:
    if not isinstance(checks, list):
        return False
    return any(isinstance(check, dict) and check.get("name") == name and bool(check.get("ok")) for check in checks)


def _write_run_summary(payload: dict[str, Any], output_dir: str | None) -> str:
    destination_dir = _run_summary_dir(payload, output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / "ptcli-run-summary.json"
    summary_payload = {
        "status": payload.get("status"),
        "source_tracker": payload.get("source_tracker"),
        "source_torrent_id": payload.get("source_torrent_id"),
        "target_trackers": payload.get("target_trackers"),
        "path": payload.get("path"),
        "target_torrent_file": payload.get("target_torrent_file"),
        "ready": payload.get("ready"),
        "blockers": payload.get("blockers", []),
        "summary": payload.get("summary"),
        "evidence": payload.get("evidence"),
        "next_actions": payload.get("next_actions", []),
    }
    destination.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(destination)


def _run_summary_dir(payload: dict[str, Any], output_dir: str | None) -> Path:
    if output_dir:
        return Path(output_dir).expanduser()
    package_dir = _target_package_dir_from_stages(payload.get("stages"))
    if package_dir:
        return Path(package_dir).expanduser()
    return Path("./tmp/retorrent-runs").expanduser()


def _target_package_dir_from_stages(stages: Any) -> str | None:
    if not isinstance(stages, list):
        return None
    target_prepare = _find_stage(stages, "target-prepare")
    result = target_prepare.get("result") if isinstance(target_prepare, dict) else None
    if isinstance(result, dict) and result.get("package_dir"):
        return str(result["package_dir"])
    return None


def _pipeline_next_actions(args: argparse.Namespace, blockers: list[str], closure: dict[str, Any]) -> list[str]:
    if blockers:
        actions: list[str] = []
        for blocker in blockers:
            _append_unique_string(actions, _pipeline_stage_blocker_next_action(blocker))
        return actions
    if bool(closure.get("complete")):
        return ["Retorrent closure is complete; verify the target tracker page and qBittorrent seeding state."]
    closure_blockers = closure.get("blockers") if isinstance(closure.get("blockers"), list) else []
    if not _pipeline_has_action(args):
        return ["Provide --path for already completed content, or run with --download-source --inject-source --save-path and --wait-complete before target upload."]
    actions = [_pipeline_closure_next_action(str(blocker)) for blocker in closure_blockers]
    return [action for action in actions if action]


def _pipeline_stage_blocker_next_action(blocker: str) -> str:
    if blocker.startswith("source-download:"):
        return "Fix source torrent download prerequisites, then re-run with --download-source after flow-check, source-info, and rule-check pass."
    if blocker.startswith("inject-source: --save-path"):
        return "Provide a qBittorrent save path with --save-path when using --inject-source."
    if blocker.startswith("inject-source:"):
        return "Fix qBittorrent source torrent injection, then re-run with --download-source --inject-source --save-path."
    if blocker.startswith("wait-complete:"):
        return "Wait for the source torrent to finish in qBittorrent, then re-run with --wait-complete or provide --path for already completed content."
    if blocker.startswith("target-upload: downloaded_torrent:"):
        return "Fix target torrent download from MTEAM, then re-run target upload follow-up with --download-uploaded-torrent and --inject-uploaded-torrent."
    if blocker.startswith("target-upload: injected_torrent:"):
        return "Fix qBittorrent target torrent injection, then re-run with --inject-uploaded-torrent and a valid --uploaded-save-path."
    if blocker.startswith("target-upload: uploaded_wait:"):
        return "Verify the uploaded target torrent is in qBittorrent, then re-run with --wait-uploaded-complete or inspect qBittorrent by the uploaded torrent hash."
    if blocker.startswith("target-upload: Target upload stage did not complete every requested upload follow-up."):
        return "Inspect target-upload follow-up blockers, then retry the failed MTEAM torrent download, qBittorrent injection, or uploaded completion wait."
    if "target-prepare" in blocker and ("rules_acknowledged" in blocker or "rule" in blocker):
        return "Review the tracker rules, then re-run with --accept-rules only if the transfer complies with the source and target site rules."
    if "target-prepare" in blocker and "duplicate_check" in blocker:
        return "Run target duplicate checking with --check-dupes and stop if MTEAM reports an existing torrent."
    return f"Fix {blocker}"


def _pipeline_closure_next_action(blocker: str) -> str:
    mapping = {
        "source.ready": "Complete the source side: use --path for existing qBittorrent content, or use --download-source --inject-source --save-path and --wait-complete.",
        "target.prepared": "Prepare the target package with --check-dupes --prepare-target --target-output-dir after source content is verified.",
        "target.uploaded": "Run the target upload with --upload-target --target-execute --confirm-upload after the package and torrent candidate are ready.",
        "target.downloaded": "Download the generated target torrent with --download-uploaded-torrent after live upload succeeds.",
        "target.injected": "Inject the generated target torrent into qBittorrent with --inject-uploaded-torrent and a valid uploaded save path.",
        "target.seeding": "Wait for the injected target torrent to become complete in qBittorrent with --wait-uploaded-complete.",
    }
    return mapping.get(blocker, f"Resolve closure blocker: {blocker}")


def _find_stage(stages: list[dict[str, Any]], stage_name: str) -> dict[str, Any] | None:
    for stage in stages:
        if stage.get("stage") == stage_name:
            return stage
    return None


def _pipeline_closure(stages: list[dict[str, Any]], content_path: str | None, source_torrent_hash: str | None, target_torrent_file: str | None) -> dict[str, Any]:
    source_download = _find_stage(stages, "source-download")
    inject_source = _find_stage(stages, "inject-source")
    wait_complete = _find_stage(stages, "wait-complete")
    match = _find_stage(stages, "match")
    target_prepare = _find_stage(stages, "target-prepare")
    target_upload = _find_stage(stages, "target-upload")
    target_upload_result = target_upload.get("result") if target_upload and isinstance(target_upload.get("result"), dict) else {}
    downloaded_torrent = target_upload_result.get("downloaded_torrent") if isinstance(target_upload_result, dict) else None
    injected_torrent = target_upload_result.get("injected_torrent") if isinstance(target_upload_result, dict) else None
    uploaded_wait = target_upload_result.get("uploaded_wait") if isinstance(target_upload_result, dict) else None
    source_downloaded = _stage_completed(source_download)
    source_injected = _source_injection_verified(inject_source)
    source_complete = _stage_completed(wait_complete)
    source_matched = _match_stage_has_match(match)
    target_injected = _injected_torrent_verified(injected_torrent)
    target_seeding = target_injected and (not isinstance(uploaded_wait, dict) or bool(uploaded_wait.get("complete")))
    injected_target_hash = _torrent_hash_from_result(injected_torrent)
    uploaded_target_hash = target_upload_result.get("uploaded_torrent_hash") if isinstance(target_upload_result, dict) else None
    source = {
        "ready": (source_downloaded and source_injected and source_complete) or source_matched,
        "downloaded": source_downloaded,
        "injected": source_injected,
        "injection_verified": source_injected,
        "injected_torrent_hash": _torrent_hash_from_stage(inject_source),
        "complete": source_complete,
        "matched": source_matched,
        "torrent_hash": source_torrent_hash,
        "content_path": content_path,
    }
    target = {
        "prepared": _stage_completed(target_prepare),
        "uploaded": isinstance(target_upload_result, dict) and target_upload_result.get("status") == "uploaded",
        "downloaded": isinstance(downloaded_torrent, dict),
        "injected": target_injected,
        "injection_verified": target_injected,
        "seeding": target_seeding,
        "uploaded_wait": uploaded_wait if isinstance(uploaded_wait, dict) else None,
        "torrent_file": target_torrent_file,
        "uploaded_torrent_hash": uploaded_target_hash or injected_target_hash,
        "injected_torrent_hash": injected_target_hash,
        "uploaded_torrent_path": downloaded_torrent.get("path") if isinstance(downloaded_torrent, dict) else None,
    }
    blockers = _closure_blockers(source, target)
    return {
        "complete": not blockers,
        "blockers": blockers,
        "source": source,
        "target": target,
    }


def _closure_blockers(source: dict[str, Any], target: dict[str, Any]) -> list[str]:
    checks = [
        ("source.ready", source.get("ready")),
        ("target.prepared", target.get("prepared")),
        ("target.uploaded", target.get("uploaded")),
        ("target.downloaded", target.get("downloaded")),
        ("target.injected", target.get("injected")),
    ]
    blockers = [name for name, ok in checks if not ok]
    if target.get("injected") and not target.get("seeding"):
        blockers.append("target.seeding")
    return blockers


def _pipeline_evidence(closure: dict[str, Any]) -> dict[str, Any]:
    source = closure.get("source") if isinstance(closure.get("source"), dict) else {}
    target = closure.get("target") if isinstance(closure.get("target"), dict) else {}
    return {
        "complete": bool(closure.get("complete")),
        "blockers": closure.get("blockers") if isinstance(closure.get("blockers"), list) else [],
        "source": {
            "ready": bool(source.get("ready")),
            "mode": "downloaded" if source.get("downloaded") and source.get("injected") and source.get("complete") else "matched" if source.get("matched") else "missing",
            "torrent_hash": source.get("torrent_hash"),
            "injected_torrent_hash": source.get("injected_torrent_hash"),
            "injection_verified": bool(source.get("injection_verified")),
            "content_path": source.get("content_path"),
        },
        "target": {
            "ready": bool(target.get("prepared") and target.get("uploaded") and target.get("downloaded") and target.get("injected") and target.get("seeding")),
            "torrent_file": target.get("torrent_file"),
            "uploaded_torrent_hash": target.get("uploaded_torrent_hash"),
            "injected_torrent_hash": target.get("injected_torrent_hash"),
            "injection_verified": bool(target.get("injection_verified")),
            "seeding_verified": bool(target.get("seeding")),
            "uploaded_wait": target.get("uploaded_wait"),
            "uploaded_torrent_path": target.get("uploaded_torrent_path"),
        },
    }


def _injected_torrent_verified(injected_torrent: Any) -> bool:
    if not isinstance(injected_torrent, dict) or injected_torrent.get("blockers"):
        return False
    if "verified_in_client" in injected_torrent:
        return bool(injected_torrent.get("verified_in_client"))
    return True


def _source_injection_verified(stage: dict[str, Any] | None) -> bool:
    if not _stage_completed(stage):
        return False
    return _injected_torrent_verified(stage.get("result"))


def _stage_completed(stage: dict[str, Any] | None) -> bool:
    return bool(stage and stage.get("ok") and not stage.get("skipped"))


def _match_stage_has_match(stage: dict[str, Any] | None) -> bool:
    if not _stage_completed(stage):
        return False
    result = stage.get("result")
    if not isinstance(result, dict):
        return False
    matches = result.get("matches")
    return isinstance(matches, list) and any(_match_has_evidence(match) for match in matches)


def _match_has_evidence(match: Any) -> bool:
    if not isinstance(match, dict):
        return False
    return bool(_normalize_torrent_hash(match.get("hash") or match.get("torrent_hash") or match.get("torrenthash")) or match.get("content_path"))


def _content_path_from_stage(stage: dict[str, Any]) -> str | None:
    if not stage.get("ok"):
        return None
    result = stage.get("result")
    if not isinstance(result, dict):
        return None
    matches = result.get("matches")
    if not isinstance(matches, list):
        return None
    for match in matches:
        if isinstance(match, dict) and match.get("content_path"):
            return str(match["content_path"])
    return None


def _source_torrent_hash_from_stage(stage: dict[str, Any] | None) -> str | None:
    if not stage or not stage.get("ok"):
        return None
    result = stage.get("result")
    if not isinstance(result, dict):
        return None
    return _normalize_torrent_hash(result.get("torrenthash") or result.get("torrent_hash") or result.get("hash"))


def _torrent_hash_from_stage(stage: dict[str, Any] | None) -> str | None:
    if not stage or not stage.get("ok"):
        return None
    result = stage.get("result")
    if not isinstance(result, dict):
        return None
    direct_hash = _normalize_torrent_hash(result.get("torrenthash") or result.get("torrent_hash") or result.get("hash"))
    if direct_hash:
        return direct_hash
    matches = result.get("matches")
    if not isinstance(matches, list):
        return None
    for match in matches:
        if isinstance(match, dict):
            torrent_hash = _normalize_torrent_hash(match.get("hash") or match.get("torrent_hash") or match.get("torrenthash"))
            if torrent_hash:
                return torrent_hash
    return None


def _torrent_hash_from_result(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    return _normalize_torrent_hash(result.get("hash") or result.get("torrent_hash") or result.get("torrenthash"))


def _normalize_torrent_hash(value: Any) -> str | None:
    if value is None:
        return None
    torrent_hash = str(value).strip().lower()
    if len(torrent_hash) not in {32, 40}:
        return None
    if any(char not in "0123456789abcdef" for char in torrent_hash):
        return None
    return torrent_hash


async def _match_with_config(config: dict[str, Any], client_name: str, content_path: str) -> dict[str, Any]:
    resolved_client_name, client_config = resolve_client_config(config, client_name)
    service = QbitReadOnlyService(client_config)
    torrents = await service.list_torrents()
    matches = match_torrents(torrents, content_path)
    return {
        "client": resolved_client_name,
        "path": content_path,
        "count": len(matches),
        "matches": summaries_to_dicts(matches),
    }


async def _export_hash_with_config(config: dict[str, Any], client_name: str, torrent_hash: str, output_dir: str) -> dict[str, Any]:
    resolved_client_name, client_config = resolve_client_config(config, client_name)
    service = QbitReadOnlyService(client_config)
    output_path = await service.export_torrent(torrent_hash, output_dir)
    candidate = await asyncio.to_thread(create_mteam_upload_torrent_candidate, str(output_path), output_dir)
    return {
        "client": resolved_client_name,
        "hash": torrent_hash.strip().lower(),
        "exported_path": str(output_path),
        "path": candidate["path"],
        "candidate": candidate,
    }


async def _sanitize_target_torrent_with_config(torrent_file: str, output_dir: str) -> dict[str, Any]:
    return await asyncio.to_thread(create_mteam_upload_torrent_candidate, torrent_file, output_dir)


async def _inject_source_with_config(
    config: dict[str, Any],
    client_name: str,
    torrent_path: str,
    save_path: str,
    category: str | None,
    tags: str | None,
    paused: bool,
) -> dict[str, Any]:
    resolved_client_name, client_config = resolve_client_config(config, client_name)
    service = QbitReadOnlyService(client_config)
    result = await service.add_torrent_file(
        torrent_path=torrent_path,
        save_path=save_path,
        category=category,
        tags=tags,
        paused=paused,
        skip_checking=False,
    )
    return {
        "client": resolved_client_name,
        **result,
    }


async def _target_upload_with_config(
    config: dict[str, Any],
    args: argparse.Namespace,
    package_dir: str,
    inferred_content_path: str | None = None,
    target_torrent_file: str | None = None,
) -> dict[str, Any]:
    result = await upload_mteam_from_package(
        config,
        package_dir,
        target_torrent_file or args.target_torrent_file,
        execute=args.target_execute,
        confirm_upload=args.confirm_upload,
        write_payload=args.write_payload,
        download_uploaded=args.download_uploaded_torrent,
        uploaded_output_dir=args.uploaded_output_dir,
    )
    if args.inject_uploaded_torrent and result.get("status") == "uploaded" and isinstance(result.get("downloaded_torrent"), dict):
        downloaded_path = str(result["downloaded_torrent"]["path"])
        uploaded_save_path = args.uploaded_save_path or inferred_content_path
        if not uploaded_save_path:
            return {**result, "injected_torrent": {"status": "blocked", "blockers": ["uploaded save path could not be inferred."]}}
        inject_result = await _inject_source_with_config(
            config,
            args.client,
            downloaded_path,
            uploaded_save_path,
            args.uploaded_qbit_category,
            args.uploaded_qbit_tags,
            args.uploaded_paused,
        )
        injected_payload = _with_uploaded_injection(result, inject_result)
        if args.wait_uploaded_complete:
            return await _with_uploaded_wait(config, args, injected_payload, uploaded_save_path)
        return injected_payload
    return result


async def _wait_complete_with_config(
    config: dict[str, Any],
    client_name: str,
    content_path: str | None,
    torrent_hash: str | None,
    timeout: float,
    interval: float,
) -> dict[str, Any]:
    resolved_client_name, client_config = resolve_client_config(config, client_name)
    service = QbitReadOnlyService(client_config)
    result = await service.wait_for_completion(torrent_hash=torrent_hash, content_path=content_path, timeout=timeout, interval=interval)
    return {
        "client": resolved_client_name,
        **result,
    }


def _with_uploaded_injection(result: dict[str, Any], inject_result: dict[str, Any]) -> dict[str, Any]:
    uploaded_torrent_hash = _torrent_hash_from_result(inject_result)
    payload = {**result, "injected_torrent": inject_result}
    if uploaded_torrent_hash:
        payload["uploaded_torrent_hash"] = uploaded_torrent_hash
        downloaded_torrent = payload.get("downloaded_torrent")
        if isinstance(downloaded_torrent, dict):
            payload["downloaded_torrent"] = {**downloaded_torrent, "hash": uploaded_torrent_hash}
    return payload


async def _with_uploaded_wait(config: dict[str, Any], args: argparse.Namespace, result: dict[str, Any], uploaded_save_path: str | None) -> dict[str, Any]:
    uploaded_torrent_hash = _torrent_hash_from_result(result.get("injected_torrent")) or _normalize_torrent_hash(result.get("uploaded_torrent_hash"))
    if not uploaded_torrent_hash:
        return {**result, "uploaded_wait": {"complete": False, "blockers": ["uploaded torrent hash is unavailable for qBittorrent completion wait."]}}
    wait_result = await _wait_complete_with_config(
        config,
        args.client,
        content_path=uploaded_save_path,
        torrent_hash=uploaded_torrent_hash,
        timeout=args.uploaded_wait_timeout,
        interval=args.uploaded_wait_interval,
    )
    return {**result, "uploaded_wait": wait_result}


async def _qbit_connection_check(config: dict[str, Any], client_name: str) -> dict[str, Any]:
    try:
        resolved_client_name, client_config = resolve_client_config(config, client_name)
        service = QbitReadOnlyService(client_config)
        torrents = await service.list_torrents(limit=1)
    except Exception as exc:
        return {"name": "qbit.connection", "ok": False, "message": str(exc)}
    return {
        "name": "qbit.connection",
        "ok": True,
        "message": f"qBittorrent connection ok: {resolved_client_name}, sample_count={len(torrents)}",
    }


async def _source_connection_check(config: dict[str, Any], source_tracker: str, source_id: str, base_dir: str | None) -> dict[str, Any]:
    try:
        source_info = await fetch_source_info(config, source_tracker, source_id, base_dir=base_dir)
    except Exception as exc:
        return {"check": {"name": "source.connection", "ok": False, "message": str(exc)}, "source": None}
    ok = source_info_has_signal(source_info)
    source_payload = source_info.to_dict()
    return {
        "check": {
            "name": "source.connection",
            "ok": ok,
            "message": f"Source metadata probe ok: {source_payload.get('tracker')} #{source_payload.get('torrent_id')}" if ok else "Source metadata probe returned no usable signal.",
        },
        "source": source_payload,
    }


async def _target_connection_check(config: dict[str, Any], target_trackers_raw: str, source_info: dict[str, Any] | None) -> dict[str, Any]:
    target_trackers = parse_tracker_list(target_trackers_raw)
    if "MTEAM" not in target_trackers:
        return {"name": "target.connection", "ok": False, "message": "Only MTEAM target API probe is implemented."}
    if not source_info:
        return {"name": "target.connection", "ok": False, "message": "Source metadata is required before probing MTEAM target API."}
    try:
        result = await search_mteam_duplicates(config, source_info)
    except Exception as exc:
        return {"name": "target.connection", "ok": False, "message": str(exc)}
    searched = bool(result.get("searched"))
    return {
        "name": "target.connection",
        "ok": searched,
        "message": f"MTEAM duplicate-search probe ok, dupes={result.get('count', 0)}" if searched else str(result.get("reason") or "MTEAM duplicate-search probe did not run."),
    }


def build_plan_commands(source_tracker: str, source_torrent_id: str, target_trackers: list[str], content_path: str | None) -> list[dict[str, str]]:
    target_trackers_arg = ",".join(target_trackers)
    retorrent_path_arg = f"--path {json.dumps(content_path, ensure_ascii=False)}" if content_path else '--save-path "/downloads"'
    doctor_path_arg = f"--path {json.dumps(content_path, ensure_ascii=False)}" if content_path else '--uploaded-save-path "/downloads"'
    commands = [
        {
            "stage": "source-info",
            "command": f"python3 ptcli.py source-info --tracker {source_tracker} --source-id {source_torrent_id} --json",
        },
        {
            "stage": "source-download",
            "command": f"python3 ptcli.py source-download --tracker {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --output-dir ./tmp/source --accept-rules --json",
        },
        {
            "stage": "rules",
            "command": f"python3 ptcli.py rules --trackers {source_tracker},{target_trackers_arg} --json",
        },
        {
            "stage": "doctor-live",
            "command": f"python3 ptcli.py doctor --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} {doctor_path_arg} --accept-rules --target-execute --confirm-upload --download-uploaded-torrent --inject-uploaded-torrent --wait-uploaded-complete --json",
        },
        {
            "stage": "retorrent-execute",
            "command": f"python3 ptcli.py retorrent --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --execute --accept-rules --confirm-upload {retorrent_path_arg} --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json",
        },
    ]
    if content_path:
        commands.append(
            {
                "stage": "match",
                "command": f"python3 ptcli.py match --path {json.dumps(content_path, ensure_ascii=False)} --json",
            }
        )
    return commands


def build_rules_payload(args: argparse.Namespace) -> dict[str, Any]:
    trackers = sorted(CHINESE_PT_TRACKERS)
    if args.trackers:
        trackers = parse_tracker_list(args.trackers)
        invalid = unsupported_trackers(trackers)
        if invalid:
            raise ValueError(f"Unsupported tracker(s) for focused CLI scope: {', '.join(invalid)}")
    return {
        "status": "ok",
        "rules": rule_profiles_to_dicts(get_rule_profiles(trackers)),
    }


def build_rule_check_payload(args: argparse.Namespace) -> dict[str, Any]:
    source_tracker = normalize_tracker(args.source_tracker)
    target_trackers = parse_tracker_list(args.target_trackers)
    invalid = unsupported_trackers([source_tracker, *target_trackers])
    if invalid:
        raise ValueError(f"Unsupported tracker(s) for focused CLI scope: {', '.join(invalid)}")
    return build_rule_check(source_tracker, target_trackers, accept_rules=args.accept_rules)


def _print_payload(payload: dict[str, Any], json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return

    if payload["status"] == "ok" and "sites" in payload:
        print("Supported trackers:")
        for tracker in payload["sites"]:
            print(f"  {tracker}")
        return

    if payload["status"] == "ok" and "rules" in payload:
        print("Rule profiles:")
        for profile in payload["rules"]:
            print(f"  {profile['tracker']}: {profile['automation_status']} ({profile['rules_url']})")
        return

    if payload["status"] in {"ok", "complete", "blocked"} and "plan" in payload and "pipeline" in payload:
        print(f"Status: {payload['status']}")
        print(f"Ready: {payload.get('ready')}")
        if payload.get("blockers"):
            print("Blockers:")
            for blocker in payload["blockers"]:
                print(f"  - {blocker}")
        closure = payload.get("closure")
        if isinstance(closure, dict):
            print(f"Closure complete: {closure.get('complete')}")
        return

    if payload["status"] == "ok" and "plan" in payload:
        plan = payload["plan"]
        print(f"Source: {plan['source_tracker']} #{plan['source_torrent_id']}")
        print(f"Targets: {', '.join(plan['target_trackers'])}")
        print(f"Client: {plan['client']}")
        print(f"Dry run: {plan['dry_run']}")
        if plan["blockers"]:
            print("Blockers:")
            for blocker in plan["blockers"]:
                print(f"  - {blocker}")
        if plan["commands"]:
            print("Commands:")
            for command in plan["commands"]:
                print(f"  - {command['stage']}: {command['command']}")
        print("Steps:")
        for step in plan["steps"]:
            print(f"  - {step}")
        return

    if payload["status"] == "ok" and "torrents" in payload:
        print(f"Client: {payload['client']}")
        print(f"Torrents: {payload['count']}")
        for torrent in payload["torrents"]:
            print(f"  {torrent['hash']}  {torrent['name']}")
        return

    if payload["status"] == "ok" and "matches" in payload:
        print(f"Client: {payload['client']}")
        print(f"Path: {payload['path']}")
        print(f"Matches: {payload['count']}")
        for torrent in payload["matches"]:
            print(f"  {torrent['hash']}  {torrent['name']}")
        return

    if payload["status"] == "ok" and "hash" in payload and "path" in payload:
        print(f"Client: {payload['client']}")
        print(f"Exported: {payload['hash']}")
        print(f"Path: {payload['path']}")
        return

    if payload["status"] == "ok" and "source" in payload:
        source = payload["source"]
        print(f"Source: {source['tracker']} #{source['torrent_id']}")
        print(f"Name: {source['name'] or ''}")
        print(f"IMDb: {source['imdb_id'] or ''}")
        print(f"TMDb: {source['tmdb_id'] or ''}")
        print(f"Douban: {source['douban_id'] or ''}")
        return

    if payload["status"] == "ok" and "ready" in payload:
        print(f"Ready: {payload['ready']}")
        for check in payload["checks"]:
            marker = "ok" if check["ok"] else "missing"
            print(f"  [{marker}] {check['name']}: {check['message']}")
        return

    if payload["status"] in {"ok", "blocked"} and "stages" in payload:
        print(f"Ready: {payload['ready']}")
        if payload.get("blockers"):
            print("Blockers:")
            for blocker in payload["blockers"]:
                print(f"  - {blocker}")
        for stage in payload["stages"]:
            marker = "ok" if stage.get("ok") else "error"
            suffix = " (skipped)" if stage.get("skipped") else ""
            print(f"  [{marker}] {stage['stage']}{suffix}")
        return

    if payload.get("target_tracker") == "MTEAM" and "upload_gate" in payload:
        print(f"Target: {payload['target_tracker']}")
        print(f"Status: {payload['status']}")
        print(f"Dry run: {payload['dry_run']}")
        if payload["blockers"]:
            print("Blockers:")
            for blocker in payload["blockers"]:
                print(f"  - {blocker}")
        return

    if payload["status"] == "ok" and "tracker" in payload and "path" in payload:
        print(f"Tracker: {payload['tracker']}")
        print(f"Path: {payload['path']}")
        return

    print(payload.get("message", "Unknown error"), file=sys.stderr)


def _with_captured_stdout(factory: Any, json_output: bool) -> dict[str, Any]:
    if not json_output:
        return factory()

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        payload = factory()
    logs = [line for line in buffer.getvalue().splitlines() if line]
    if logs:
        payload = {**payload, "logs": logs}
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    json_output = bool(getattr(args, "json", False))

    try:
        if args.command == "sites":
            _print_payload({"status": "ok", "sites": sorted(CHINESE_PT_TRACKERS)}, json_output)
            return 0

        if args.command == "rules":
            _print_payload(build_rules_payload(args), json_output)
            return 0

        if args.command == "rule-check":
            _print_payload(build_rule_check_payload(args), json_output)
            return 0

        if args.command == "retorrent":
            payload = _with_captured_stdout(lambda: asyncio.run(retorrent_payload(args)), json_output)
            _print_payload(payload, json_output)
            return _retorrent_exit_code(args, payload)

        if args.command == "inspect":
            payload = _with_captured_stdout(lambda: asyncio.run(inspect_qbit(args)), json_output)
            _print_payload(payload, json_output)
            return 0

        if args.command == "match":
            payload = _with_captured_stdout(lambda: asyncio.run(match_qbit(args)), json_output)
            _print_payload(payload, json_output)
            return 0

        if args.command == "export":
            payload = _with_captured_stdout(lambda: asyncio.run(export_qbit(args)), json_output)
            _print_payload(payload, json_output)
            return 0

        if args.command == "source-info":
            payload = _with_captured_stdout(lambda: asyncio.run(source_info(args)), json_output)
            _print_payload(payload, json_output)
            return 0

        if args.command == "source-download":
            payload = _with_captured_stdout(lambda: asyncio.run(source_download(args)), json_output)
            _print_payload(payload, json_output)
            return _source_download_exit_code(payload)

        if args.command == "flow-check":
            _print_payload(_with_captured_stdout(lambda: flow_check_payload(args), json_output), json_output)
            return 0

        if args.command == "doctor":
            payload = _with_captured_stdout(lambda: asyncio.run(doctor_payload(args)), json_output)
            _print_payload(payload, json_output)
            return 0

        if args.command == "pipeline":
            payload = _with_captured_stdout(lambda: asyncio.run(pipeline_payload(args)), json_output)
            _print_payload(payload, json_output)
            return _pipeline_exit_code(args, payload)

        if args.command == "target-upload":
            payload = _with_captured_stdout(lambda: asyncio.run(target_upload_payload(args)), json_output)
            _print_payload(payload, json_output)
            return _target_upload_exit_code(args, payload)

        parser.error(f"Unknown command: {args.command}")
        return 2
    except Exception as exc:
        _print_payload({"status": "error", "message": str(exc)}, json_output)
        return 2


def _retorrent_exit_code(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    if not getattr(args, "execute", False):
        return 0
    if payload.get("status") in {"complete", "ok"}:
        return 0
    return 1


def _source_download_exit_code(payload: dict[str, Any]) -> int:
    if payload.get("status") == "ok" and not payload.get("blockers"):
        return 0
    return 1


def _rule_check_blockers(rule_check: dict[str, Any]) -> list[str]:
    checks = rule_check.get("checks")
    if not isinstance(checks, list):
        return ["Executable rule check did not pass."]
    blockers = [f"{check.get('name', 'rule_check')}: {check.get('message', 'Executable rule check did not pass.')}" for check in checks if isinstance(check, dict) and not check.get("ok")]
    return blockers or ["Executable rule check did not pass."]


def _target_upload_result_ready(payload: dict[str, Any], *, execute: bool, download_uploaded: bool, inject_uploaded: bool, wait_uploaded_complete: bool = False) -> bool:
    if payload.get("status") not in {"ready", "uploaded"}:
        return False
    if payload.get("blockers"):
        return False
    if execute and payload.get("status") != "uploaded":
        return False
    if download_uploaded and not isinstance(payload.get("downloaded_torrent"), dict):
        return False
    if inject_uploaded:
        injected_torrent = payload.get("injected_torrent")
        if not _injected_torrent_verified(injected_torrent):
            return False
    if wait_uploaded_complete:
        uploaded_wait = payload.get("uploaded_wait")
        return isinstance(uploaded_wait, dict) and bool(uploaded_wait.get("complete"))
    return True


def _pipeline_exit_code(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    if not _pipeline_has_action(args):
        return 0
    if payload.get("status") == "ok" and payload.get("ready") is True:
        return 0
    return 1


def _pipeline_has_action(args: argparse.Namespace) -> bool:
    action_names = (
        "download_source",
        "inject_source",
        "wait_complete",
        "check_dupes",
        "prepare_target",
        "export_target_torrent",
        "sanitize_target_torrent",
        "upload_target",
        "target_execute",
        "write_payload",
        "download_uploaded_torrent",
        "inject_uploaded_torrent",
        "wait_uploaded_complete",
    )
    return any(bool(getattr(args, name, False)) for name in action_names)


def _target_upload_exit_code(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    if not getattr(args, "execute", False):
        return 0
    if _target_upload_result_ready(
        payload,
        execute=True,
        download_uploaded=bool(getattr(args, "download_uploaded_torrent", False)),
        inject_uploaded=bool(getattr(args, "inject_uploaded_torrent", False)),
        wait_uploaded_complete=bool(getattr(args, "wait_uploaded_complete", False)),
    ):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
