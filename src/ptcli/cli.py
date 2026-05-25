"""Dedicated CLI surface for focused PT retorrent automation."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import shlex
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from torf import Torrent

from src.ptcli.config import load_config, resolve_client_config
from src.ptcli.credentials import build_flow_check
from src.ptcli.doctor import build_doctor_check, build_runtime_dependency_check, extend_doctor_check
from src.ptcli.flows import NEXUSPHP_MTEAM_SOURCE_TRACKERS, flow_profiles_to_dicts, get_flow_profiles
from src.ptcli.mainland import CHINESE_PT_TRACKERS, normalize_tracker, parse_tracker_list, unsupported_trackers
from src.ptcli.qbit import QbitReadOnlyService, match_torrents, summaries_to_dicts
from src.ptcli.rules import build_rule_check, get_rule_profiles, rule_profiles_to_dicts
from src.ptcli.source import (
    DIRECT_DOWNLOAD_TRACKER_CLASSES,
    GENERIC_DETAILS_BASE_URLS,
    NEXUS_DOWNLOAD_BASE_URLS,
    SOURCE_TRACKER_CLASSES,
    download_source_torrent,
    extract_torrent_id,
    fetch_source_info,
    source_info_has_signal,
)
from src.ptcli.target import (
    build_mteam_upload_preflight,
    create_mteam_upload_torrent_candidate,
    download_mteam_uploaded_torrent,
    load_mteam_prepare_package,
    search_mteam_duplicates,
    upload_mteam_from_package,
    write_mteam_prepare_package,
)


@dataclass(frozen=True)
class RetorrentPlan:
    source_tracker: str
    requested_source_id: str
    source_torrent_id: str
    target_trackers: list[str]
    content_path: str | None
    client: str
    dry_run: bool
    accept_rules: bool
    flow_profiles: list[dict[str, Any]]
    rule_profiles: list[dict[str, Any]]
    rule_check: dict[str, Any]
    capability: dict[str, Any]
    blockers: list[str]
    commands: list[dict[str, str]]
    steps: list[str]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ptcli",
        description="Focused CLI for compliant mainland/CN PT retorrent workflows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Common live closure commands:\n"
            "  ptcli retorrent --from U2 --source-id 60635 --to MTEAM --execute --accept-rules --confirm-upload --save-path /downloads --json\n"
            "  ptcli pipeline --from U2 --source-id 60635 --to MTEAM --save-path /downloads --check-dupes --prepare-target --accept-rules --upload-target --target-execute --confirm-upload --json\n"
            "  ptcli doctor --from U2 --source-id 60635 --to MTEAM --path /downloads/movie --package-dir ./tmp/target/U2-60635-to-MTEAM --target-torrent-file ./tmp/exported/mteam.torrent --target-execute --confirm-upload --accept-rules --json"
        ),
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
    source_info.add_argument("--tracker", required=True, help="Supported source tracker code; inspect ptcli sites --json for current capabilities.")
    source_info.add_argument("--source-id", required=True, help="Source tracker torrent id or details URL.")
    source_info.add_argument("--base-dir", help="Project/base directory used for cookies, defaults to current directory.")
    source_info.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    source_download = subparsers.add_parser("source-download", help="Download a source .torrent to an output directory.")
    source_download.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    source_download.add_argument("--tracker", required=True, help="Source tracker code with source_download capability; inspect ptcli sites --json.")
    source_download.add_argument("--source-id", required=True, help="Source tracker torrent id or details URL.")
    source_download.add_argument("--to", dest="target_trackers", help="Target tracker codes for executable source-download rule gates.")
    source_download.add_argument("--output-dir", required=True, help="Directory where the source .torrent file will be written.")
    source_download.add_argument("--base-dir", help="Project/base directory used for cookies, defaults to current directory.")
    source_download.add_argument("--accept-rules", action="store_true", help="Acknowledge that source and target tracker rules have been manually reviewed.")
    source_download.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    flow_check = subparsers.add_parser(
        "flow-check",
        help="Check local config readiness for an enabled ptcli retorrent flow.",
        description="Check local config readiness for an enabled ptcli retorrent flow.",
    )
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
    doctor.add_argument("--source-torrent-file", help="Existing source .torrent file intended for qBittorrent injection.")
    doctor.add_argument("--package-dir", help="Directory created by pipeline --prepare-target.")
    doctor.add_argument("--target-torrent-file", help="MTEAM .torrent file intended for target upload.")
    doctor.add_argument("--accept-rules", action="store_true", help="Acknowledge that source and target tracker rules have been manually reviewed.")
    doctor.add_argument("--target-execute", action="store_true", help="Check readiness for a live target upload.")
    doctor.add_argument("--confirm-upload", action="store_true", help="Confirm manual rule review and live upload intent.")
    doctor.add_argument("--download-uploaded-torrent", action="store_true", help="Check follow-up download of the generated MTEAM torrent file.")
    doctor.add_argument("--uploaded-torrent-id", help="Existing MTEAM torrent id to download and inject without re-submitting the upload.")
    doctor.add_argument("--uploaded-torrent-file", help="Existing uploaded MTEAM .torrent file intended for qBittorrent injection.")
    doctor.add_argument("--inject-uploaded-torrent", action="store_true", help="Check follow-up qBittorrent injection after target upload.")
    doctor.add_argument("--uploaded-save-path", help="qBittorrent save path required by --inject-uploaded-torrent.")
    doctor.add_argument("--uploaded-qbit-category", help="Optional qBittorrent category for uploaded target torrent injection.")
    doctor.add_argument("--uploaded-qbit-tags", help="Optional qBittorrent tags for uploaded target torrent injection.")
    doctor.add_argument("--uploaded-paused", action="store_true", help="Add uploaded target torrent to qBittorrent paused.")
    doctor.add_argument("--wait-uploaded-complete", action="store_true", help="Check qBittorrent completion wait after uploaded target torrent injection.")
    doctor.add_argument("--connect-qbit", action="store_true", help="Probe qBittorrent connectivity by listing one torrent.")
    doctor.add_argument("--probe-source", action="store_true", help="Probe source tracker metadata lookup with the configured credentials/cookies.")
    doctor.add_argument("--probe-target", action="store_true", help="Probe MTEAM target duplicate-search API with the source metadata signal.")
    doctor.add_argument("--check-runtime", action="store_true", help="Verify focused ptcli runtime dependencies are importable without requiring legacy Web UI/Discord dependencies.")
    doctor.add_argument("--write-summary", action="store_true", help="Write ptcli-doctor-summary.json for live-readiness audit handoff.")
    doctor.add_argument("--summary-output-dir", help="Directory for --write-summary. Defaults to --package-dir or ./tmp/retorrent-runs.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    pipeline = subparsers.add_parser(
        "pipeline",
        help="Run staged retorrent checks; with --target-execute it auto-runs the live closure follow-ups.",
        description=(
            "Run the staged PT retorrent pipeline. Plain runs are dry/read-only unless action flags are supplied. "
            "When --upload-target --target-execute is used, the pipeline automatically fills in the live closure defaults: "
            "source download/inject/wait when needed, target torrent export/sanitize when needed, and uploaded MTEAM torrent download/inject/wait."
        ),
    )
    pipeline.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    pipeline.add_argument("--from", dest="source_tracker", required=True, help="Source tracker code.")
    pipeline.add_argument("--source-id", required=True, help="Source tracker torrent id or details URL.")
    pipeline.add_argument("--to", dest="target_trackers", required=True, help="Target tracker codes, comma-separated.")
    pipeline.add_argument("--path", dest="content_path", help="Existing local content path on the seedbox.")
    pipeline.add_argument("--client", default="default", help="Configured qBittorrent client name, defaults to config default.")
    pipeline.add_argument("--base-dir", help="Project/base directory used for cookies, defaults to current directory.")
    pipeline.add_argument("--download-source", action="store_true", help="Download the source .torrent only after earlier pipeline stages pass.")
    pipeline.add_argument("--output-dir", default="./tmp/source", help="Directory for --download-source output.")
    pipeline.add_argument("--source-torrent-file", help="Reuse an existing source .torrent file instead of downloading it again.")
    pipeline.add_argument("--inject-source", action="store_true", help="Add the downloaded source .torrent to qBittorrent after download succeeds.")
    pipeline.add_argument("--save-path", help="qBittorrent save path required by --inject-source.")
    pipeline.add_argument("--qbit-category", help="Optional qBittorrent category for --inject-source.")
    pipeline.add_argument("--qbit-tags", help="Optional qBittorrent tags for --inject-source.")
    pipeline.add_argument("--paused", action="store_true", help="Add injected source torrent paused.")
    pipeline.add_argument("--wait-complete", action="store_true", help="Wait for qBittorrent task completion after injection or by --path.")
    pipeline.add_argument("--wait-timeout", type=float, default=3600.0, help="Seconds to wait with --wait-complete.")
    pipeline.add_argument("--wait-interval", type=float, default=30.0, help="Polling interval seconds for --wait-complete.")
    pipeline.add_argument("--prepare-target", action="store_true", help="Build a dry-run target preparation preview after prior stages.")
    pipeline.add_argument("--package-dir", help="Reuse an existing MTEAM package created by pipeline --prepare-target.")
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
    pipeline.add_argument("--uploaded-torrent-id", help="Existing MTEAM torrent id to download and inject without re-submitting the upload.")
    pipeline.add_argument("--uploaded-torrent-file", help="Reuse an already downloaded MTEAM uploaded .torrent for qBittorrent injection.")
    pipeline.add_argument("--inject-uploaded-torrent", action="store_true", help="Add the downloaded target torrent to qBittorrent after upload.")
    pipeline.add_argument("--uploaded-save-path", help="qBittorrent save path required by --inject-uploaded-torrent.")
    pipeline.add_argument("--uploaded-qbit-category", help="Optional qBittorrent category for --inject-uploaded-torrent.")
    pipeline.add_argument("--uploaded-qbit-tags", help="Optional qBittorrent tags for --inject-uploaded-torrent.")
    pipeline.add_argument("--uploaded-paused", action="store_true", help="Add uploaded target torrent to qBittorrent paused.")
    pipeline.add_argument("--wait-uploaded-complete", action="store_true", help="Wait for the injected target torrent to become complete in qBittorrent.")
    pipeline.add_argument("--uploaded-wait-timeout", type=float, default=600.0, help="Seconds to wait with --wait-uploaded-complete.")
    pipeline.add_argument("--uploaded-wait-interval", type=float, default=15.0, help="Polling interval seconds for --wait-uploaded-complete.")
    pipeline.add_argument("--check-runtime", action="store_true", help="Verify focused ptcli runtime dependencies before action stages. Enabled automatically by --target-execute.")
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
    target_upload.add_argument("--uploaded-torrent-id", help="Existing MTEAM torrent id to download and inject without re-submitting the upload.")
    target_upload.add_argument("--uploaded-torrent-file", help="Reuse an already downloaded MTEAM uploaded .torrent for qBittorrent injection.")
    target_upload.add_argument("--inject-uploaded-torrent", action="store_true", help="Add the downloaded target torrent to qBittorrent after upload.")
    target_upload.add_argument("--uploaded-save-path", help="qBittorrent save path for --inject-uploaded-torrent; defaults to the package content path when available.")
    target_upload.add_argument("--uploaded-qbit-category", help="Optional qBittorrent category for --inject-uploaded-torrent.")
    target_upload.add_argument("--uploaded-qbit-tags", help="Optional qBittorrent tags for --inject-uploaded-torrent.")
    target_upload.add_argument("--uploaded-paused", action="store_true", help="Add uploaded target torrent to qBittorrent paused.")
    target_upload.add_argument("--wait-uploaded-complete", action="store_true", help="Wait for the injected target torrent to become complete in qBittorrent.")
    target_upload.add_argument("--uploaded-wait-timeout", type=float, default=600.0, help="Seconds to wait with --wait-uploaded-complete.")
    target_upload.add_argument("--uploaded-wait-interval", type=float, default=15.0, help="Polling interval seconds for --wait-uploaded-complete.")
    target_upload.add_argument("--check-runtime", action="store_true", help="Verify focused ptcli runtime dependencies before live upload or qBittorrent injection. Enabled automatically by --execute and --inject-uploaded-torrent.")
    target_upload.add_argument("--write-summary", action="store_true", help="Write ptcli-target-upload-summary.json for audit and automation handoff.")
    target_upload.add_argument("--summary-output-dir", help="Directory for --write-summary. Defaults to --package-dir.")
    target_upload.add_argument("--client", default="default", help="Configured qBittorrent client name for --inject-uploaded-torrent.")
    target_upload.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    retorrent = subparsers.add_parser("retorrent", help="Plan or execute a retorrent workflow between supported trackers.")
    retorrent.add_argument("--config", help="Path to config.py, defaults to data/config.py.")
    retorrent.add_argument("--from", dest="source_tracker", required=True, help="Source tracker code; inspect ptcli sites --json for full live closure sources.")
    retorrent.add_argument("--source-id", required=True, help="Source tracker torrent id or details URL.")
    retorrent.add_argument("--to", dest="target_trackers", required=True, help="Target tracker codes, comma-separated.")
    retorrent.add_argument("--path", dest="content_path", help="Existing local content path on the seedbox.")
    retorrent.add_argument("--client", default="default", help="Configured torrent client name, defaults to config default.")
    retorrent.add_argument("--base-dir", help="Project/base directory used for cookies, defaults to current directory.")
    retorrent.add_argument("--execute", action="store_true", help="Run the full live closure pipeline after every gate passes.")
    retorrent.add_argument("--dry-run", action="store_true", help="Plan only; do not download, upload, or inject torrents.")
    retorrent.add_argument("--output-dir", default="./tmp/source", help="Directory for downloaded source .torrent files.")
    retorrent.add_argument("--source-torrent-file", help="Reuse an existing source .torrent file during --execute instead of downloading it again.")
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
    retorrent.add_argument("--uploaded-torrent-id", help="Existing MTEAM torrent id to download and inject during --execute without re-submitting the upload.")
    retorrent.add_argument("--uploaded-torrent-file", help="Reuse an already downloaded MTEAM uploaded .torrent during --execute.")
    retorrent.add_argument("--inject-uploaded-torrent", action="store_true", help="Add the downloaded MTEAM torrent to qBittorrent after upload. Enabled automatically by --execute.")
    retorrent.add_argument("--uploaded-save-path", help="qBittorrent save path for uploaded target torrent injection.")
    retorrent.add_argument("--uploaded-qbit-category", help="Optional qBittorrent category for uploaded target torrent injection.")
    retorrent.add_argument("--uploaded-qbit-tags", help="Optional qBittorrent tags for uploaded target torrent injection.")
    retorrent.add_argument("--uploaded-paused", action="store_true", help="Add uploaded target torrent paused.")
    retorrent.add_argument("--uploaded-wait-timeout", type=float, default=600.0, help="Seconds to wait for the uploaded target torrent to become complete during --execute; uploaded completion wait is enabled automatically.")
    retorrent.add_argument("--uploaded-wait-interval", type=float, default=15.0, help="Polling interval seconds for uploaded target torrent completion during --execute; uploaded completion wait is enabled automatically.")
    retorrent.add_argument("--check-runtime", action="store_true", help="Verify focused ptcli runtime dependencies before action stages. Enabled automatically by --execute.")
    retorrent.add_argument("--write-summary", action="store_true", help="Write ptcli-run-summary.json during --execute for audit and automation handoff. Enabled by default for --execute.")
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
    tracker = normalize_tracker(args.tracker)
    scope_block = _tracker_scope_block_payload("source-info", tracker, source_id=args.source_id)
    if scope_block:
        return {"tracker": tracker, **scope_block}
    config = load_config(args.config)
    info = await fetch_source_info(config, tracker, args.source_id, base_dir=args.base_dir)
    source = info.to_dict()
    source_id_context = {
        "requested_source_id": args.source_id,
        "input_source_id": args.source_id,
        "source_torrent_id": source["torrent_id"],
    }
    return {
        "status": "ok",
        "tracker": source["tracker"],
        **source_id_context,
        "source": source,
    }


async def source_download(args: argparse.Namespace) -> dict[str, Any]:
    source_tracker = normalize_tracker(args.tracker)
    source_id_context = {
        "requested_source_id": args.source_id,
        "input_source_id": args.source_id,
        "source_torrent_id": extract_torrent_id(args.source_id),
    }
    target_trackers = parse_tracker_list(args.target_trackers) if args.target_trackers else []
    source_scope_block = _tracker_scope_block_payload("source-download", source_tracker, target_trackers, source_id=args.source_id)
    if source_scope_block:
        return {"tracker": source_tracker, **source_scope_block}
    if not args.target_trackers:
        return {
            "status": "blocked",
            "tracker": source_tracker,
            **source_id_context,
            "blockers": ["--to is required so source-download can run source/target rule gates before downloading."],
        }
    rule_check = build_rule_check(source_tracker, target_trackers, accept_rules=args.accept_rules)
    if not rule_check.get("ready"):
        return {
            "status": "blocked",
            "tracker": source_tracker,
            **source_id_context,
            "target_trackers": target_trackers,
            "rule_check": rule_check,
            "blockers": _rule_check_blockers(rule_check),
        }
    config = load_config(args.config)
    info = await fetch_source_info(config, source_tracker, args.source_id, base_dir=args.base_dir)
    source = info.to_dict()
    if not source_info_has_signal(info):
        return {
            "status": "blocked",
            "tracker": source_tracker,
            **source_id_context,
            "target_trackers": target_trackers,
            "rule_check": rule_check,
            "source": source,
            "blockers": ["source-info: Source metadata lookup returned no usable identifiers, name, hash, description, or Douban data."],
        }
    output_path = await download_source_torrent(config, source_tracker, args.source_id, args.output_dir, base_dir=args.base_dir)
    source_torrent = _torrent_file_evidence(output_path)
    source_torrent_verification = _source_torrent_verify_stage({"result": source_torrent}, source.get("torrenthash"))
    if not source_torrent_verification.get("ok"):
        verification = source_torrent_verification.get("result", {})
        verification_blockers = verification.get("blockers") if isinstance(verification, dict) else None
        blockers = [f"source-torrent-verify: {blocker}" for blocker in verification_blockers] if isinstance(verification_blockers, list) else ["source-torrent-verify: Downloaded source torrent infohash does not match source tracker metadata."]
        return {
            "status": "blocked",
            "tracker": source_tracker,
            **source_id_context,
            "target_trackers": target_trackers,
            "rule_check": rule_check,
            "source": source,
            "source_torrent": source_torrent,
            "source_torrent_verification": verification,
            "path": source_torrent["path"],
            "blockers": blockers,
        }
    return {
        "status": "ok",
        "tracker": source_tracker,
        **source_id_context,
        "target_trackers": target_trackers,
        "rule_check": rule_check,
        "source": source,
        "source_torrent": source_torrent,
        "source_torrent_verification": source_torrent_verification["result"],
        "path": source_torrent["path"],
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
    rule_check = build_rule_check(source_tracker, target_trackers, accept_rules=args.accept_rules)
    blockers: list[str] = []
    if not args.dry_run and any(profile.review_required for profile in rule_profiles) and not args.accept_rules:
        blockers.append("Rule review acknowledgement is required before any non-dry-run action.")
    if any(profile.automation_status != "enabled" for profile in rule_profiles):
        blockers.append("Tracker rule profiles are in planning mode; upload/download automation is not enabled yet.")
    flow_profiles = get_flow_profiles(source_tracker, target_trackers)
    if not flow_profiles:
        blockers.append("This source/target combination is not enabled for ptcli retorrent flow execution.")
    capability = _retorrent_plan_capability(source_tracker, target_trackers)
    if flow_profiles and not capability["full_live_closure"]:
        blockers.append("This source/target combination has partial ptcli support but is not eligible for full live closure.")

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
        requested_source_id=args.source_id,
        source_torrent_id=source_torrent_id,
        target_trackers=target_trackers,
        content_path=args.content_path,
        client=args.client,
        dry_run=bool(args.dry_run),
        accept_rules=bool(args.accept_rules),
        flow_profiles=flow_profiles_to_dicts(flow_profiles),
        rule_profiles=rule_profiles_to_dicts(rule_profiles),
        rule_check=rule_check,
        capability=capability,
        blockers=blockers,
        commands=commands,
        steps=steps,
    )


def _retorrent_plan_capability(source_tracker: str, target_trackers: list[str]) -> dict[str, Any]:
    sites_payload = build_sites_payload()
    capabilities = sites_payload["capabilities"]
    source_capability = capabilities.get(source_tracker, {})
    full_live_sources = sites_payload["full_live_closure_sources"]
    return {
        "source_tracker": source_tracker,
        "target_trackers": target_trackers,
        "source_info": bool(source_capability.get("source_info")),
        "source_download": bool(source_capability.get("source_download")),
        "mteam_source_flow": bool(source_capability.get("mteam_source_flow")),
        "target_upload": target_trackers == ["MTEAM"],
        "full_live_closure": target_trackers == ["MTEAM"] and source_tracker in full_live_sources,
    }


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
    artifacts = _retorrent_execute_artifacts(pipeline_result, evidence, closure)
    return {
        "status": "complete" if not blockers else "blocked",
        "plan": plan_payload,
        "pipeline": pipeline_result,
        "closure": closure,
        "evidence": evidence,
        "summary": summary,
        "summary_file": pipeline_result.get("summary_file"),
        "artifacts": artifacts,
        "resume_commands": pipeline_result.get("resume_commands", []),
        "ready": ready,
        "complete": not blockers,
        "blockers": blockers,
        "next_actions": next_actions,
    }


def _retorrent_execute_artifacts(pipeline_result: dict[str, Any], evidence: dict[str, Any] | None, closure: dict[str, Any] | None) -> dict[str, Any]:
    artifacts = pipeline_result.get("artifacts")
    merged = dict(artifacts) if isinstance(artifacts, dict) else {}
    evidence_target = evidence.get("target") if isinstance(evidence, dict) and isinstance(evidence.get("target"), dict) else {}
    closure_target = closure.get("target") if isinstance(closure, dict) and isinstance(closure.get("target"), dict) else {}
    for key in ("uploaded_torrent_id", "uploaded_torrent_hash", "uploaded_torrent_path", "fresh_duplicate_check"):
        if merged.get(key):
            continue
        value = evidence_target.get(key) or closure_target.get(key)
        if value:
            merged[key] = value
    if not merged.get("uploaded_torrent_file") and merged.get("uploaded_torrent_path"):
        merged["uploaded_torrent_file"] = merged["uploaded_torrent_path"]
    return merged


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
    needs_source_download = not bool(args.content_path or args.source_torrent_file)
    needs_source_injection = not bool(args.content_path)
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
        source_torrent_file=args.source_torrent_file,
        inject_source=needs_source_injection,
        save_path=args.save_path,
        qbit_category=args.qbit_category,
        qbit_tags=args.qbit_tags,
        paused=args.paused,
        wait_complete=needs_source_injection or bool(args.content_path),
        wait_timeout=args.wait_timeout,
        wait_interval=args.wait_interval,
        prepare_target=True,
        package_dir=None,
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
        uploaded_torrent_id=args.uploaded_torrent_id,
        uploaded_torrent_file=args.uploaded_torrent_file,
        inject_uploaded_torrent=True,
        uploaded_save_path=args.uploaded_save_path,
        uploaded_qbit_category=args.uploaded_qbit_category,
        uploaded_qbit_tags=args.uploaded_qbit_tags,
        uploaded_paused=args.uploaded_paused,
        wait_uploaded_complete=True,
        uploaded_wait_timeout=args.uploaded_wait_timeout,
        uploaded_wait_interval=args.uploaded_wait_interval,
        check_runtime=True,
        write_summary=True,
        summary_output_dir=args.summary_output_dir,
        json=getattr(args, "json", False),
    )


def build_sites_payload() -> dict[str, Any]:
    sites = sorted(CHINESE_PT_TRACKERS)
    source_info_trackers = sorted((set(SOURCE_TRACKER_CLASSES) | set(GENERIC_DETAILS_BASE_URLS)) & set(CHINESE_PT_TRACKERS))
    source_download_trackers = sorted((set(NEXUS_DOWNLOAD_BASE_URLS) | set(DIRECT_DOWNLOAD_TRACKER_CLASSES)) & set(CHINESE_PT_TRACKERS))
    mteam_flow_sources = sorted(NEXUSPHP_MTEAM_SOURCE_TRACKERS & set(CHINESE_PT_TRACKERS))
    full_live_sources = sorted(set(source_download_trackers) & set(mteam_flow_sources))
    target_upload_trackers = ["MTEAM"] if "MTEAM" in CHINESE_PT_TRACKERS else []
    capabilities = {
        tracker: {
            "source_info": tracker in source_info_trackers,
            "source_download": tracker in source_download_trackers,
            "mteam_source_flow": tracker in mteam_flow_sources,
            "full_live_closure_to_mteam": tracker in full_live_sources,
            "target_upload": tracker in target_upload_trackers,
        }
        for tracker in sites
    }
    flows = [
        {
            "source_tracker": source_tracker,
            "target_tracker": "MTEAM",
            "full_live_closure": source_tracker in full_live_sources,
            "requires": ["source-info", "source-download", "qBittorrent inject/wait", "MTEAM duplicate check", "MTEAM upload", "uploaded torrent inject/wait"],
        }
        for source_tracker in mteam_flow_sources
    ]
    return {
        "status": "ok",
        "sites": sites,
        "capabilities": capabilities,
        "source_info_trackers": source_info_trackers,
        "source_download_trackers": source_download_trackers,
        "target_upload_trackers": target_upload_trackers,
        "mteam_flow_sources": mteam_flow_sources,
        "full_live_closure_sources": full_live_sources,
        "flows": flows,
    }


def flow_check_payload(args: argparse.Namespace) -> dict[str, Any]:
    source_tracker = normalize_tracker(args.source_tracker)
    target_trackers = parse_tracker_list(args.target_trackers)
    scope_block = _tracker_scope_block_payload("flow-check", source_tracker, target_trackers, source_id=args.source_id)
    if scope_block:
        return scope_block
    config = load_config(args.config)
    return build_flow_check(config, source_tracker, args.source_id, ",".join(target_trackers), args.client, base_dir=args.base_dir)


async def doctor_payload(args: argparse.Namespace) -> dict[str, Any]:
    source_tracker = normalize_tracker(args.source_tracker)
    target_trackers = parse_tracker_list(args.target_trackers)
    scope_block = _tracker_scope_block_payload("doctor", source_tracker, target_trackers, source_id=args.source_id)
    if scope_block:
        return {
            **scope_block,
            "ready": False,
            "live_safe_to_attempt": False,
            "checks": [
                {
                    "name": "tracker_scope",
                    "ok": False,
                    "message": scope_block["blockers"][0],
                }
            ],
        }
    config = load_config(args.config)
    payload = build_doctor_check(
        config,
        source_tracker=source_tracker,
        source_id=args.source_id,
        target_trackers=",".join(target_trackers),
        client=args.client,
        base_dir=args.base_dir,
        content_path=args.content_path,
        source_torrent_file=args.source_torrent_file,
        package_dir=args.package_dir,
        target_torrent_file=args.target_torrent_file,
        accept_rules=args.accept_rules,
        target_execute=args.target_execute,
        confirm_upload=args.confirm_upload,
        download_uploaded_torrent=args.download_uploaded_torrent,
        uploaded_torrent_id=args.uploaded_torrent_id,
        uploaded_torrent_file=args.uploaded_torrent_file,
        inject_uploaded_torrent=args.inject_uploaded_torrent,
        uploaded_save_path=args.uploaded_save_path,
        wait_uploaded_complete=args.wait_uploaded_complete,
        check_runtime=args.check_runtime,
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
    if getattr(args, "write_summary", False):
        summary_file = _write_doctor_summary(payload, args, args.summary_output_dir or args.package_dir)
        payload = {**payload, "summary_file": summary_file}
    return payload


async def target_upload_payload(args: argparse.Namespace) -> dict[str, Any]:
    preflight_torrent_file = args.torrent_file or args.uploaded_torrent_file
    runtime_check = _target_upload_runtime_check(args)
    if args.uploaded_torrent_id:
        preflight = _target_upload_preflight_with_runtime(_target_upload_recovery_preflight(args, preflight_torrent_file), runtime_check)
        inferred_uploaded_save_path = _uploaded_save_path_from_preflight(args, preflight)
        blockers = [*preflight["blockers"], *_uploaded_torrent_id_reuse_blockers(args, inferred_uploaded_save_path=inferred_uploaded_save_path)]
        if blockers:
            blocked = {**preflight, "status": "blocked", "dry_run": False, "uploaded_torrent_id": args.uploaded_torrent_id, "blockers": blockers}
            return _maybe_write_target_upload_summary(args, blocked, preflight)
        config = load_config(args.config)
        output_dir = args.uploaded_output_dir or args.package_dir
        result = await download_mteam_uploaded_torrent(config, args.uploaded_torrent_id, output_dir)
        result = await _apply_uploaded_torrent_followup(config, args, result, inferred_uploaded_save_path)
        return _maybe_write_target_upload_summary(args, result, preflight)
    if args.uploaded_torrent_file:
        preflight = _target_upload_preflight_with_runtime(build_mteam_upload_preflight(args.package_dir, execute=False, torrent_file=preflight_torrent_file, write_payload=args.write_payload), runtime_check)
        inferred_uploaded_save_path = _uploaded_save_path_from_preflight(args, preflight)
        blockers = [*preflight["blockers"], *_uploaded_torrent_reuse_blockers(args, inferred_uploaded_save_path=inferred_uploaded_save_path)]
        if blockers:
            blocked = {**preflight, "status": "blocked", "dry_run": False, "blockers": blockers}
            return _maybe_write_target_upload_summary(args, blocked, preflight)
        config = load_config(args.config)
        result = await _existing_uploaded_torrent_payload(args.uploaded_torrent_file)
        result = await _apply_uploaded_torrent_followup(config, args, result, inferred_uploaded_save_path)
        return _maybe_write_target_upload_summary(args, result, preflight)
    if not args.execute:
        preflight = _target_upload_preflight_with_runtime(build_mteam_upload_preflight(args.package_dir, execute=False, torrent_file=args.torrent_file, write_payload=args.write_payload), runtime_check)
        return _maybe_write_target_upload_summary(args, preflight, preflight)
    if not args.torrent_file:
        preflight = _target_upload_preflight_with_runtime(build_mteam_upload_preflight(args.package_dir, execute=True, torrent_file=args.torrent_file, write_payload=args.write_payload), runtime_check)
        return _maybe_write_target_upload_summary(args, preflight, preflight)
    preflight = _target_upload_preflight_with_runtime(build_mteam_upload_preflight(args.package_dir, execute=True, torrent_file=args.torrent_file, write_payload=args.write_payload), runtime_check)
    inferred_uploaded_save_path = _uploaded_save_path_from_preflight(args, preflight)
    blockers = [*preflight["blockers"], *_target_upload_execute_blockers(args, inferred_uploaded_save_path=inferred_uploaded_save_path)]
    if blockers:
        blocked = {**preflight, "status": "blocked", "dry_run": False, "blockers": blockers}
        return _maybe_write_target_upload_summary(args, blocked, preflight)
    config = load_config(args.config)
    fresh_dupe_check = await _fresh_mteam_dupe_check_for_target_upload(config, preflight)
    dupe_blockers = _fresh_mteam_dupe_check_blockers(fresh_dupe_check)
    if dupe_blockers:
        blocked = {**preflight, "status": "blocked", "dry_run": False, "fresh_duplicate_check": fresh_dupe_check, "blockers": [*blockers, *dupe_blockers]}
        return _maybe_write_target_upload_summary(args, blocked, preflight)
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
    result = {**result, "fresh_duplicate_check": fresh_dupe_check}
    result = await _apply_uploaded_torrent_followup(config, args, result, inferred_uploaded_save_path)
    return _maybe_write_target_upload_summary(args, result, preflight)


def _target_upload_recovery_preflight(args: argparse.Namespace, torrent_file: str | None) -> dict[str, Any]:
    preflight = build_mteam_upload_preflight(args.package_dir, execute=False, torrent_file=torrent_file, write_payload=args.write_payload)
    blockers = [blocker for blocker in _string_list(preflight.get("blockers")) if blocker != "MTEAM upload torrent file is required."]
    payload_summary = preflight.get("upload_payload")
    if isinstance(payload_summary, dict):
        payload_summary = {
            **payload_summary,
            "blockers": [blocker for blocker in _string_list(payload_summary.get("blockers")) if blocker != "MTEAM upload torrent file is required."],
        }
    return {
        **preflight,
        "status": "blocked" if blockers else "ready",
        "blockers": blockers,
        "upload_payload": payload_summary if isinstance(payload_summary, dict) else preflight.get("upload_payload"),
    }


def _target_upload_runtime_check(args: argparse.Namespace) -> dict[str, Any] | None:
    if not bool(getattr(args, "check_runtime", False) or args.execute or args.inject_uploaded_torrent):
        return None
    return build_runtime_dependency_check()


def _target_upload_preflight_with_runtime(preflight: dict[str, Any], runtime_check: dict[str, Any] | None) -> dict[str, Any]:
    if runtime_check is None:
        return preflight
    runtime_blockers = [] if runtime_check.get("ok") else [f"runtime-check: {runtime_check.get('message') or 'PTCLI runtime dependencies are not ready.'}"]
    blockers = [*_string_list(preflight.get("blockers")), *runtime_blockers]
    return {
        **preflight,
        "status": "blocked" if blockers else preflight.get("status", "ready"),
        "runtime_check": runtime_check,
        "blockers": blockers,
    }


def _uploaded_save_path_from_preflight(args: argparse.Namespace, preflight: dict[str, Any]) -> str | None:
    return args.uploaded_save_path or _mteam_package_content_path(preflight)


def _mteam_package_content_path(preflight: dict[str, Any]) -> str | None:
    content_path = preflight.get("content_path")
    if content_path:
        return str(content_path)
    preview = preflight.get("preview")
    if not isinstance(preview, dict):
        return None
    preview_content_path = preview.get("content_path")
    return str(preview_content_path) if preview_content_path else None


def _target_upload_execute_blockers(args: argparse.Namespace, *, inferred_uploaded_save_path: str | None = None) -> list[str]:
    blockers: list[str] = []
    if not args.confirm_upload:
        blockers.append("MTEAM live upload requires --confirm-upload.")
    if not args.download_uploaded_torrent:
        blockers.append("target-upload --execute requires --download-uploaded-torrent so the generated MTEAM torrent can be seeded.")
    if not args.inject_uploaded_torrent:
        blockers.append("--inject-uploaded-torrent is required with target-upload --execute for full live retorrent closure.")
    elif not args.download_uploaded_torrent:
        blockers.append("--inject-uploaded-torrent requires --download-uploaded-torrent.")
    elif not inferred_uploaded_save_path:
        blockers.append("--uploaded-save-path is required with --inject-uploaded-torrent when the MTEAM package has no content path.")
    if args.wait_uploaded_complete and not args.inject_uploaded_torrent:
        blockers.append("--wait-uploaded-complete requires --inject-uploaded-torrent.")
    return blockers


async def _fresh_mteam_dupe_check_for_target_upload(config: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    return await search_mteam_duplicates(config, _source_info_from_mteam_preflight(preflight))


def _fresh_mteam_dupe_check_blockers(dupe_check: dict[str, Any]) -> list[str]:
    if not dupe_check.get("searched"):
        reason = dupe_check.get("reason") or "MTEAM duplicate search did not run."
        return [f"fresh_duplicate_check: {reason}"]
    dupe_count = int(dupe_check.get("count", 0) or 0)
    if dupe_count:
        return [f"fresh_duplicate_check: MTEAM duplicate search found {dupe_count} possible existing torrent(s)."]
    return []


def _source_info_from_mteam_preflight(preflight: dict[str, Any]) -> dict[str, Any] | None:
    package_manifest = preflight.get("package_manifest")
    manifest_source = package_manifest.get("source") if isinstance(package_manifest, dict) else None
    preview = preflight.get("preview")
    preview_metadata = preview.get("metadata") if isinstance(preview, dict) else None
    meta_draft = preflight.get("meta_draft")
    if not isinstance(meta_draft, dict):
        meta_draft = preview.get("meta_draft") if isinstance(preview, dict) else {}
    if not isinstance(meta_draft, dict):
        meta_draft = {}
    source = manifest_source if isinstance(manifest_source, dict) else preview_metadata if isinstance(preview_metadata, dict) else {}
    if not isinstance(source, dict):
        return None
    content_path = preflight.get("content_path") or (preview.get("content_path") if isinstance(preview, dict) else None)
    return {
        "tracker": source.get("tracker"),
        "torrent_id": source.get("torrent_id"),
        "name": source.get("name") or meta_draft.get("name"),
        "imdb_id": source.get("imdb_id") or meta_draft.get("imdb_id"),
        "tmdb_id": source.get("tmdb_id") or meta_draft.get("tmdb_id"),
        "douban_id": source.get("douban_id") or meta_draft.get("douban_id"),
        "douban_url": source.get("douban_url") or meta_draft.get("douban_url"),
        "torrenthash": source.get("torrenthash") or meta_draft.get("torrenthash"),
        "description_length": source.get("description_length"),
        "content_path": content_path,
    }


def _uploaded_torrent_reuse_blockers(args: argparse.Namespace, *, inferred_uploaded_save_path: str | None = None) -> list[str]:
    blockers: list[str] = []
    if args.wait_uploaded_complete and not args.inject_uploaded_torrent:
        blockers.append("--wait-uploaded-complete requires --inject-uploaded-torrent.")
    if args.inject_uploaded_torrent and not inferred_uploaded_save_path:
        blockers.append("--uploaded-save-path is required with --inject-uploaded-torrent when the MTEAM package has no content path.")
    return blockers


def _uploaded_torrent_id_reuse_blockers(args: argparse.Namespace, *, inferred_uploaded_save_path: str | None = None) -> list[str]:
    blockers: list[str] = []
    if not args.download_uploaded_torrent:
        blockers.append("--download-uploaded-torrent is required with --uploaded-torrent-id.")
    blockers.extend(_uploaded_torrent_reuse_blockers(args, inferred_uploaded_save_path=inferred_uploaded_save_path))
    return blockers


def _maybe_write_target_upload_summary(args: argparse.Namespace, result: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    if not getattr(args, "write_summary", False):
        return result
    summary_file = _write_target_upload_summary(result, preflight, args, args.summary_output_dir or args.package_dir)
    summary = _target_upload_summary(result, preflight)
    return {**result, "summary": summary, "summary_file": summary_file}


def _write_target_upload_summary(result: dict[str, Any], preflight: dict[str, Any], args: argparse.Namespace, output_dir: str) -> str:
    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / "ptcli-target-upload-summary.json"
    summary = _target_upload_summary(result, preflight)
    artifacts = _target_upload_summary_artifacts(result, preflight, args, str(destination))
    recommended_commands = _target_upload_recommended_commands(summary, args, artifacts)
    payload = {
        "schema_version": 1,
        "kind": "ptcli.target_upload.summary",
        "summary_file": str(destination),
        "client": args.client,
        "qbit_options": _target_upload_qbit_options(args),
        "summary": _target_upload_summary(result, preflight),
        "artifacts": artifacts,
        "recommended_commands": recommended_commands,
        "resume_state": _target_upload_resume_state(summary, artifacts, recommended_commands),
        "preflight": preflight,
        "result": result,
    }
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(destination)


def _target_upload_summary_artifacts(result: dict[str, Any], preflight: dict[str, Any], args: argparse.Namespace, summary_file: str) -> dict[str, Any]:
    downloaded_torrent = result.get("downloaded_torrent")
    uploaded_torrent_path = downloaded_torrent.get("path") if isinstance(downloaded_torrent, dict) else args.uploaded_torrent_file
    return {
        "summary_file": summary_file,
        "package_dir": _path_artifact(args.package_dir),
        "target_torrent_file": _path_artifact(args.torrent_file),
        "uploaded_torrent_id": _uploaded_torrent_id_from_result(result) or args.uploaded_torrent_id,
        "uploaded_torrent_file": _path_artifact(uploaded_torrent_path),
        "uploaded_save_path": _path_artifact(_uploaded_save_path_from_result(result) or _mteam_package_content_path(preflight) or args.uploaded_save_path),
    }


def _target_upload_recommended_commands(summary: dict[str, Any], args: argparse.Namespace, artifacts: dict[str, Any]) -> list[dict[str, str]]:
    commands = [
        {
            "stage": "target-upload-retry",
            "command": _target_upload_retry_command(args),
        }
    ]
    package_artifact = artifacts.get("package_dir")
    uploaded_torrent_artifact = artifacts.get("uploaded_torrent_file")
    uploaded_torrent_id = artifacts.get("uploaded_torrent_id")
    uploaded_save_path_artifact = artifacts.get("uploaded_save_path")
    if isinstance(package_artifact, dict) and uploaded_torrent_id and not (isinstance(uploaded_torrent_artifact, dict) and uploaded_torrent_artifact.get("path")):
        download_args = [
            "target-upload",
            "--package-dir",
            str(package_artifact.get("path") or args.package_dir),
            "--client",
            args.client,
            "--uploaded-torrent-id",
            str(uploaded_torrent_id),
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--wait-uploaded-complete",
            "--write-summary",
            "--json",
        ]
        if args.uploaded_output_dir:
            download_args.extend(["--uploaded-output-dir", args.uploaded_output_dir])
        if isinstance(uploaded_save_path_artifact, dict) and uploaded_save_path_artifact.get("path"):
            download_args.extend(["--uploaded-save-path", str(uploaded_save_path_artifact["path"])])
        if args.uploaded_qbit_category:
            download_args.extend(["--uploaded-qbit-category", args.uploaded_qbit_category])
        if args.uploaded_qbit_tags:
            download_args.extend(["--uploaded-qbit-tags", args.uploaded_qbit_tags])
        if args.uploaded_paused:
            download_args.append("--uploaded-paused")
        commands.append({"stage": "resume-uploaded-torrent-download", "command": _ptcli_command(download_args)})
    if isinstance(package_artifact, dict) and isinstance(uploaded_torrent_artifact, dict) and uploaded_torrent_artifact.get("path"):
        resume_args = [
            "target-upload",
            "--package-dir",
            str(package_artifact.get("path") or args.package_dir),
            "--client",
            args.client,
            "--uploaded-torrent-file",
            str(uploaded_torrent_artifact["path"]),
            "--inject-uploaded-torrent",
            "--wait-uploaded-complete",
            "--write-summary",
            "--json",
        ]
        if isinstance(uploaded_save_path_artifact, dict) and uploaded_save_path_artifact.get("path"):
            resume_args.extend(["--uploaded-save-path", str(uploaded_save_path_artifact["path"])])
        if args.uploaded_qbit_category:
            resume_args.extend(["--uploaded-qbit-category", args.uploaded_qbit_category])
        if args.uploaded_qbit_tags:
            resume_args.extend(["--uploaded-qbit-tags", args.uploaded_qbit_tags])
        if args.uploaded_paused:
            resume_args.append("--uploaded-paused")
        commands.append({"stage": "resume-uploaded-torrent", "command": _ptcli_command(resume_args)})
    if summary.get("ready"):
        commands.append({"stage": "verify-seeding", "command": _ptcli_command(["inspect", "--client", args.client, "--json"])})
    return commands


def _target_upload_resume_state(summary: dict[str, Any], artifacts: dict[str, Any], recommended_commands: list[dict[str, str]]) -> dict[str, Any]:
    commands_by_stage = {str(command.get("stage")): str(command.get("command")) for command in recommended_commands if isinstance(command, dict)}
    resume_available = any(stage.startswith("resume-") for stage in commands_by_stage)
    next_command = _target_upload_next_command(summary, commands_by_stage) if resume_available else {"stage": None, "command": None}
    return {
        "ready": bool(summary.get("ready")),
        "resume_available": resume_available,
        "next_stage": next_command.get("stage"),
        "next_command": next_command.get("command"),
        "available_stages": [str(command.get("stage")) for command in recommended_commands if isinstance(command, dict)],
        "artifacts": {
            "package_dir": bool(_path_artifact_exists(artifacts.get("package_dir"))),
            "target_torrent_file": bool(_path_artifact_exists(artifacts.get("target_torrent_file"))),
            "uploaded_torrent_id": bool(artifacts.get("uploaded_torrent_id")),
            "uploaded_torrent_file": bool(_path_artifact_exists(artifacts.get("uploaded_torrent_file"))),
            "uploaded_save_path": bool(_path_artifact_exists(artifacts.get("uploaded_save_path"))),
        },
        "blockers": _string_list(summary.get("blockers")),
    }


def _target_upload_next_command(summary: dict[str, Any], commands_by_stage: dict[str, str]) -> dict[str, str | None]:
    uploaded_wait = summary.get("uploaded_wait")
    uploaded_wait_complete = isinstance(uploaded_wait, dict) and bool(uploaded_wait.get("complete"))
    if summary.get("injected") and (summary.get("seeding_verified") or uploaded_wait_complete):
        return {"stage": None, "command": None}
    blockers = _string_list(summary.get("blockers"))
    blocker_text = "\n".join(blockers)
    preferred_stages: list[str] = []
    if "downloaded_torrent" in blocker_text or not summary.get("downloaded"):
        preferred_stages.append("resume-uploaded-torrent-download")
    if "injected_torrent" in blocker_text or "uploaded_wait" in blocker_text or not summary.get("injected") or not summary.get("seeding_verified"):
        preferred_stages.append("resume-uploaded-torrent")
    preferred_stages.extend(["resume-uploaded-torrent-download", "resume-uploaded-torrent", "target-upload-retry"])
    for stage in preferred_stages:
        command = commands_by_stage.get(stage)
        if command:
            return {"stage": stage, "command": command}
    return {"stage": None, "command": None}


def _path_artifact_exists(artifact: Any) -> bool:
    return isinstance(artifact, dict) and bool(artifact.get("path")) and artifact.get("exists") is not False


def _target_upload_retry_command(args: argparse.Namespace) -> str:
    retry_args = [
        "target-upload",
        "--package-dir",
        args.package_dir,
        "--write-summary",
        "--json",
    ]
    for option, value in (
        ("--config", args.config),
        ("--torrent-file", args.torrent_file),
        ("--uploaded-torrent-id", args.uploaded_torrent_id),
        ("--uploaded-torrent-file", args.uploaded_torrent_file),
        ("--uploaded-output-dir", args.uploaded_output_dir),
        ("--uploaded-save-path", args.uploaded_save_path),
        ("--uploaded-qbit-category", args.uploaded_qbit_category),
        ("--uploaded-qbit-tags", args.uploaded_qbit_tags),
        ("--summary-output-dir", args.summary_output_dir),
        ("--client", args.client),
    ):
        if value:
            retry_args.extend([option, value])
    for option, enabled in (
        ("--write-payload", args.write_payload),
        ("--execute", args.execute),
        ("--confirm-upload", args.confirm_upload),
        ("--download-uploaded-torrent", args.download_uploaded_torrent),
        ("--inject-uploaded-torrent", args.inject_uploaded_torrent),
        ("--uploaded-paused", args.uploaded_paused),
        ("--wait-uploaded-complete", args.wait_uploaded_complete),
        ("--check-runtime", getattr(args, "check_runtime", False)),
    ):
        if enabled:
            retry_args.append(option)
    return _ptcli_command(retry_args)


def _target_upload_qbit_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "uploaded": {
            "category": args.uploaded_qbit_category,
            "tags": args.uploaded_qbit_tags,
            "paused": bool(args.uploaded_paused),
        },
    }


def _write_doctor_summary(payload: dict[str, Any], args: argparse.Namespace, output_dir: str | None) -> str:
    destination_dir = Path(output_dir).expanduser() if output_dir else Path("./tmp/retorrent-runs").expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / "ptcli-doctor-summary.json"
    summary_payload = _doctor_summary_payload(payload, args, str(destination))
    destination.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(destination)


def _doctor_summary_payload(payload: dict[str, Any], args: argparse.Namespace, summary_file: str) -> dict[str, Any]:
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    failed_checks = [check for check in checks if isinstance(check, dict) and not check.get("ok")]
    artifacts = _doctor_summary_artifacts(args)
    recommended_commands = _doctor_recommended_commands(payload, args, artifacts)
    return {
        "schema_version": 1,
        "kind": "ptcli.doctor.live_readiness",
        "summary_file": summary_file,
        "status": payload.get("status"),
        "ready": payload.get("ready"),
        "live_safe_to_attempt": payload.get("live_safe_to_attempt"),
        "source_tracker": normalize_tracker(args.source_tracker),
        "requested_source_id": args.source_id,
        "input_source_id": args.source_id,
        "source_torrent_id": extract_torrent_id(args.source_id),
        "target_trackers": parse_tracker_list(args.target_trackers),
        "client": args.client,
        "inputs": _doctor_summary_inputs(args),
        "artifacts": artifacts,
        "failed_checks": failed_checks,
        "failed_check_names": [str(check.get("name")) for check in failed_checks if isinstance(check, dict)],
        "effective_uploaded_save_path": payload.get("effective_uploaded_save_path"),
        "next_actions": payload.get("next_actions", []),
        "recommended_commands": recommended_commands,
        "resume_state": _doctor_resume_state(payload, artifacts, failed_checks, recommended_commands),
        "checks": checks,
        "flow_check": payload.get("flow_check"),
        "rule_check": payload.get("rule_check"),
        "compliance": payload.get("compliance"),
        "package_preflight": payload.get("package_preflight"),
    }


def _doctor_resume_state(payload: dict[str, Any], artifacts: dict[str, Any], failed_checks: list[Any], recommended_commands: list[dict[str, str]]) -> dict[str, Any]:
    commands_by_stage = {str(command.get("stage")): str(command.get("command")) for command in recommended_commands if isinstance(command, dict)}
    next_command = _doctor_next_command(payload, commands_by_stage)
    return {
        "ready": bool(payload.get("ready")),
        "live_safe_to_attempt": bool(payload.get("live_safe_to_attempt")),
        "resume_available": any(stage != "doctor-retry" for stage in commands_by_stage),
        "next_stage": next_command.get("stage"),
        "next_command": next_command.get("command"),
        "available_stages": [str(command.get("stage")) for command in recommended_commands if isinstance(command, dict)],
        "artifacts": {
            "content_path": bool(_path_artifact_exists(artifacts.get("content_path"))),
            "source_torrent_file": bool(_path_artifact_exists(artifacts.get("source_torrent_file"))),
            "package_dir": bool(_path_artifact_exists(artifacts.get("package_dir"))),
            "target_torrent_file": bool(_path_artifact_exists(artifacts.get("target_torrent_file"))),
            "uploaded_torrent_id": bool(artifacts.get("uploaded_torrent_id")),
            "uploaded_torrent_file": bool(_path_artifact_exists(artifacts.get("uploaded_torrent_file"))),
        },
        "failed_check_names": [str(check.get("name")) for check in failed_checks if isinstance(check, dict)],
    }


def _doctor_next_command(payload: dict[str, Any], commands_by_stage: dict[str, str]) -> dict[str, str | None]:
    preferred_stages = ["resume-uploaded-torrent-download", "pipeline-live", "doctor-live-probes"] if payload.get("live_safe_to_attempt") else ["doctor-retry"]
    for stage in preferred_stages:
        command = commands_by_stage.get(stage)
        if command:
            return {"stage": stage, "command": command}
    return {"stage": None, "command": None}


def _doctor_summary_inputs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "content_path": args.content_path,
        "source_torrent_file": args.source_torrent_file,
        "package_dir": args.package_dir,
        "target_torrent_file": args.target_torrent_file,
        "uploaded_torrent_id": args.uploaded_torrent_id,
        "uploaded_torrent_file": args.uploaded_torrent_file,
        "accept_rules": bool(args.accept_rules),
        "target_execute": bool(args.target_execute),
        "confirm_upload": bool(args.confirm_upload),
        "download_uploaded_torrent": bool(args.download_uploaded_torrent),
        "inject_uploaded_torrent": bool(args.inject_uploaded_torrent),
        "uploaded_save_path": args.uploaded_save_path,
        "uploaded_qbit_category": args.uploaded_qbit_category,
        "uploaded_qbit_tags": args.uploaded_qbit_tags,
        "uploaded_paused": bool(args.uploaded_paused),
        "wait_uploaded_complete": bool(args.wait_uploaded_complete),
        "connect_qbit": bool(args.connect_qbit),
        "probe_source": bool(args.probe_source),
        "probe_target": bool(args.probe_target),
        "check_runtime": bool(args.check_runtime),
    }


def _doctor_summary_artifacts(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "content_path": _path_artifact(args.content_path),
        "source_torrent_file": _path_artifact(args.source_torrent_file),
        "package_dir": _path_artifact(args.package_dir),
        "target_torrent_file": _path_artifact(args.target_torrent_file),
        "uploaded_torrent_id": args.uploaded_torrent_id,
        "uploaded_torrent_file": _path_artifact(args.uploaded_torrent_file),
    }


def _path_artifact(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    resolved = Path(path).expanduser()
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "is_file": resolved.is_file(),
        "is_dir": resolved.is_dir(),
    }


def _doctor_recommended_commands(payload: dict[str, Any], args: argparse.Namespace, artifacts: dict[str, Any]) -> list[dict[str, str]]:
    commands = [
        {
            "stage": "doctor-retry",
            "command": _doctor_retry_command(args),
        }
    ]
    if not payload.get("live_safe_to_attempt"):
        return commands

    commands.append(
        {
            "stage": "doctor-live-probes",
            "command": _doctor_retry_command(args, force_probes=True),
        }
    )

    if args.uploaded_torrent_id and args.package_dir:
        uploaded_resume_args = [
            "target-upload",
            "--package-dir",
            args.package_dir,
            "--client",
            args.client,
            "--uploaded-torrent-id",
            args.uploaded_torrent_id,
            "--download-uploaded-torrent",
            "--inject-uploaded-torrent",
            "--wait-uploaded-complete",
            "--write-summary",
            "--json",
        ]
        if args.uploaded_save_path:
            uploaded_resume_args.extend(["--uploaded-save-path", args.uploaded_save_path])
        if args.uploaded_qbit_category:
            uploaded_resume_args.extend(["--uploaded-qbit-category", args.uploaded_qbit_category])
        if args.uploaded_qbit_tags:
            uploaded_resume_args.extend(["--uploaded-qbit-tags", args.uploaded_qbit_tags])
        if args.uploaded_paused:
            uploaded_resume_args.append("--uploaded-paused")
        commands.append({"stage": "resume-uploaded-torrent-download", "command": _ptcli_command(uploaded_resume_args)})
        return commands

    source_tracker = normalize_tracker(args.source_tracker)
    source_torrent_id = extract_torrent_id(args.source_id)
    target_trackers_arg = ",".join(parse_tracker_list(args.target_trackers))
    pipeline_args = [
        "pipeline",
        "--from",
        source_tracker,
        "--source-id",
        source_torrent_id,
        "--to",
        target_trackers_arg,
        "--accept-rules",
        "--upload-target",
        "--target-execute",
        "--confirm-upload",
        "--download-uploaded-torrent",
        "--inject-uploaded-torrent",
        "--wait-uploaded-complete",
        "--write-summary",
        "--json",
    ]
    _extend_command_path(pipeline_args, "--path", artifacts.get("content_path"))
    _extend_command_path(pipeline_args, "--source-torrent-file", artifacts.get("source_torrent_file"))
    _extend_command_path(pipeline_args, "--package-dir", artifacts.get("package_dir"))
    _extend_command_path(pipeline_args, "--target-torrent-file", artifacts.get("target_torrent_file"))
    if args.uploaded_save_path:
        pipeline_args.extend(["--uploaded-save-path", args.uploaded_save_path])
    commands.append({"stage": "pipeline-live", "command": _ptcli_command(pipeline_args)})
    return commands


def _doctor_retry_command(args: argparse.Namespace, *, force_probes: bool = False) -> str:
    retry_args = [
        "doctor",
        "--from",
        normalize_tracker(args.source_tracker),
        "--source-id",
        extract_torrent_id(args.source_id),
        "--to",
        ",".join(parse_tracker_list(args.target_trackers)),
        "--client",
        args.client,
        "--write-summary",
        "--json",
    ]
    if args.base_dir:
        retry_args.extend(["--base-dir", args.base_dir])
    for option, value in (
        ("--path", args.content_path),
        ("--source-torrent-file", args.source_torrent_file),
        ("--package-dir", args.package_dir),
        ("--target-torrent-file", args.target_torrent_file),
        ("--uploaded-torrent-id", args.uploaded_torrent_id),
        ("--uploaded-torrent-file", args.uploaded_torrent_file),
        ("--uploaded-save-path", args.uploaded_save_path),
        ("--uploaded-qbit-category", args.uploaded_qbit_category),
        ("--uploaded-qbit-tags", args.uploaded_qbit_tags),
    ):
        if value:
            retry_args.extend([option, value])
    for option, enabled in (
        ("--accept-rules", args.accept_rules),
        ("--target-execute", args.target_execute),
        ("--confirm-upload", args.confirm_upload),
        ("--download-uploaded-torrent", args.download_uploaded_torrent),
        ("--inject-uploaded-torrent", args.inject_uploaded_torrent),
        ("--wait-uploaded-complete", args.wait_uploaded_complete),
        ("--uploaded-paused", args.uploaded_paused),
        ("--connect-qbit", force_probes or args.connect_qbit),
        ("--probe-source", force_probes or args.probe_source),
        ("--probe-target", force_probes or args.probe_target),
        ("--check-runtime", args.check_runtime),
    ):
        if enabled:
            retry_args.append(option)
    return _ptcli_command(retry_args)


def _extend_command_path(command: list[str], option: str, artifact: Any) -> None:
    if isinstance(artifact, dict) and artifact.get("path"):
        command.extend([option, str(artifact["path"])])


def _target_upload_summary(result: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    downloaded_torrent = result.get("downloaded_torrent")
    injected_torrent = result.get("injected_torrent")
    uploaded_wait = result.get("uploaded_wait")
    blockers = _target_upload_result_blockers(result)
    uploaded_torrent_hash = _uploaded_torrent_hash_from_result(result)
    qbit_closure = {
        "injection": _qbit_injection_evidence(injected_torrent),
        "wait": _qbit_wait_evidence(uploaded_wait),
    }
    return {
        "status": result.get("status"),
        "ready": result.get("status") in {"ready", "uploaded"} and not blockers,
        "uploaded": result.get("status") == "uploaded",
        "uploaded_torrent_id": _uploaded_torrent_id_from_result(result),
        "downloaded": isinstance(downloaded_torrent, dict),
        "uploaded_torrent": downloaded_torrent if isinstance(downloaded_torrent, dict) else None,
        "uploaded_torrent_path": downloaded_torrent.get("path") if isinstance(downloaded_torrent, dict) else None,
        "injected": _injected_torrent_verified(injected_torrent),
        "injection_verified": _injected_torrent_verified(injected_torrent),
        "injected_torrent_hash": _torrent_hash_from_result(injected_torrent),
        "uploaded_save_path": _uploaded_save_path_from_result(result),
        "seeding_verified": isinstance(uploaded_wait, dict) and bool(uploaded_wait.get("complete")),
        "uploaded_torrent_hash": uploaded_torrent_hash,
        "uploaded_wait": uploaded_wait if isinstance(uploaded_wait, dict) else None,
        "qbit_closure": qbit_closure,
        "blockers": blockers,
        "preflight_status": preflight.get("status"),
        "preflight_blockers": preflight.get("blockers", []),
        "rule_obligations": preflight.get("rule_obligation_review", {}),
    }


def _qbit_injection_evidence(injected_torrent: Any) -> dict[str, Any] | None:
    if not isinstance(injected_torrent, dict):
        return None
    keys = (
        "client",
        "hash",
        "torrent_hash",
        "torrent_path",
        "save_path",
        "category",
        "tags",
        "paused",
        "skip_checking",
        "verified_in_client",
        "verification_attempts",
        "client_verification",
        "client_matches",
        "blockers",
    )
    return {key: injected_torrent[key] for key in keys if key in injected_torrent}


def _qbit_wait_evidence(wait_result: Any) -> dict[str, Any] | None:
    if not isinstance(wait_result, dict):
        return None
    keys = (
        "client",
        "complete",
        "query",
        "matches",
        "attempts",
        "elapsed_seconds",
        "blockers",
    )
    return {key: wait_result[key] for key in keys if key in wait_result}


def _uploaded_save_path_from_result(result: dict[str, Any]) -> str | None:
    injected_torrent = result.get("injected_torrent")
    if isinstance(injected_torrent, dict) and injected_torrent.get("save_path"):
        return str(injected_torrent["save_path"])
    uploaded_wait = result.get("uploaded_wait")
    if isinstance(uploaded_wait, dict):
        query = uploaded_wait.get("query")
        if isinstance(query, dict) and query.get("content_path"):
            return str(query["content_path"])
    return None


def _uploaded_torrent_id_from_result(result: dict[str, Any]) -> str | None:
    if result.get("uploaded_torrent_id"):
        return str(result["uploaded_torrent_id"])
    downloaded_torrent = result.get("downloaded_torrent")
    if isinstance(downloaded_torrent, dict) and downloaded_torrent.get("torrent_id"):
        return str(downloaded_torrent["torrent_id"])
    return None


async def pipeline_payload(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    source_tracker = normalize_tracker(args.source_tracker)
    source_torrent_id = extract_torrent_id(args.source_id)
    target_trackers = parse_tracker_list(args.target_trackers)
    effective_content_path = args.content_path
    effective_target_torrent_file = args.target_torrent_file
    effective_source_torrent_hash: str | None = None
    requested_actions = _pipeline_requested_actions(args)
    live_target_upload = bool(args.upload_target and args.target_execute)
    runtime_check_requested = bool(getattr(args, "check_runtime", False) or live_target_upload)
    source_download_requested = bool(args.download_source or (live_target_upload and not args.content_path and not args.source_torrent_file))
    source_injection_requested = bool(args.inject_source or (live_target_upload and not args.content_path))
    source_wait_requested = bool(args.wait_complete or (live_target_upload and (source_injection_requested or args.content_path)))
    if live_target_upload:
        if not args.uploaded_torrent_file:
            args.download_uploaded_torrent = True
        args.inject_uploaded_torrent = True
        args.wait_uploaded_complete = True

    stages: list[dict[str, Any]] = []
    if runtime_check_requested:
        runtime_check = build_runtime_dependency_check()
        stages.append({"stage": "runtime-check", "ok": bool(runtime_check.get("ok")), "message": runtime_check.get("message"), "result": runtime_check})
    else:
        stages.append({"stage": "runtime-check", "ok": True, "skipped": True, "message": "--check-runtime not provided; runtime dependency check skipped."})

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

    if args.source_torrent_file:
        if _runtime_check_ready(stages) and _required_stages_ok(stages, {"flow-check", "source-info"}) and _rule_check_ready(stages):
            source_download_result = await _pipeline_stage(
                "source-download",
                lambda: _existing_source_torrent_payload(args.source_torrent_file),
                lambda payload: payload,
            )
            stages.append(source_download_result)
            source_verify_stage = _source_torrent_verify_stage(source_download_result, effective_source_torrent_hash)
            stages.append(source_verify_stage)
            if source_verify_stage.get("ok"):
                effective_source_torrent_hash = _torrent_hash_from_stage(source_download_result) or effective_source_torrent_hash
        else:
            stages.append(
                {
                    "stage": "source-download",
                    "ok": False,
                    "skipped": True,
                    "message": "Skipped because runtime-check, flow-check, source-info, or executable rule-check did not pass.",
                }
            )
    elif source_download_requested:
        if _runtime_check_ready(stages) and _required_stages_ok(stages, {"flow-check", "source-info"}) and _rule_check_ready(stages):
            source_download_result = await _pipeline_stage(
                "source-download",
                lambda: download_source_torrent(config, source_tracker, source_torrent_id, args.output_dir, base_dir=args.base_dir),
                lambda path: _torrent_file_evidence(path),
            )
            stages.append(source_download_result)
            source_verify_stage = _source_torrent_verify_stage(source_download_result, effective_source_torrent_hash)
            stages.append(source_verify_stage)
            if source_verify_stage.get("ok"):
                effective_source_torrent_hash = _torrent_hash_from_stage(source_download_result) or effective_source_torrent_hash
        else:
            stages.append(
                {
                    "stage": "source-download",
                    "ok": False,
                    "skipped": True,
                    "message": "Skipped because runtime-check, flow-check, source-info, or executable rule-check did not pass.",
                }
            )
    else:
        stages.append({"stage": "source-download", "ok": True, "skipped": True, "message": "--download-source not provided; source download skipped."})

    if source_injection_requested:
        source_download_stage = _find_stage(stages, "source-download")
        source_verify_stage = _find_stage(stages, "source-torrent-verify")
        if not args.save_path:
            stages.append({"stage": "inject-source", "ok": False, "skipped": True, "message": "--save-path is required when --inject-source is used."})
        elif not source_download_stage or not source_download_stage.get("ok") or source_download_stage.get("skipped"):
            stages.append({"stage": "inject-source", "ok": False, "skipped": True, "message": "Skipped because source-download did not complete successfully."})
        elif source_verify_stage and not source_verify_stage.get("ok"):
            stages.append({"stage": "inject-source", "ok": False, "skipped": True, "message": "Skipped because source torrent hash verification failed."})
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

    if source_wait_requested:
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

    if args.package_dir and not effective_content_path:
        effective_content_path = _content_path_from_existing_target_package(args.package_dir)

    if effective_content_path:
        match_result = await _pipeline_stage(
            "match",
            lambda: _match_with_config(config, args.client, str(effective_content_path)),
            lambda payload: payload,
        )
        stages.append(match_result)
        stages.append(_source_content_verify_stage(match_result, effective_source_torrent_hash))
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
    elif args.package_dir:
        target_prepare = _load_existing_target_prepare_package(args.package_dir)
        stages.append({"stage": "target-prepare", "ok": not target_prepare["blockers"], "result": target_prepare})
    else:
        stages.append({"stage": "target-prepare", "ok": True, "skipped": True, "message": "--prepare-target not provided; target preparation skipped."})

    export_target_torrent = args.export_target_torrent or bool(args.upload_target and not effective_target_torrent_file and not args.uploaded_torrent_file and not args.uploaded_torrent_id)
    if export_target_torrent:
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

    sanitize_target_torrent = args.sanitize_target_torrent or bool(args.upload_target and args.target_execute and effective_target_torrent_file and not args.uploaded_torrent_file and not args.uploaded_torrent_id)
    if sanitize_target_torrent:
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

    effective_actions = _pipeline_effective_actions(
        args,
        live_target_upload=live_target_upload,
        runtime_check=runtime_check_requested,
        source_download=source_download_requested,
        source_injection=source_injection_requested,
        source_wait=source_wait_requested,
        target_torrent_export=export_target_torrent,
        target_torrent_sanitize=sanitize_target_torrent,
    )

    if args.upload_target:
        target_prepare_stage = _find_stage(stages, "target-prepare")
        if not target_prepare_stage or not target_prepare_stage.get("ok") or target_prepare_stage.get("skipped"):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "Skipped because target-prepare did not complete successfully."})
        elif not effective_target_torrent_file and not args.uploaded_torrent_file and not args.uploaded_torrent_id:
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "--target-torrent-file or --export-target-torrent is required when --upload-target is used."})
        elif args.target_execute and not args.uploaded_torrent_file and not args.uploaded_torrent_id and not args.download_uploaded_torrent:
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "pipeline --target-execute requires --download-uploaded-torrent so the generated target torrent can be seeded."})
        elif args.target_execute and not args.inject_uploaded_torrent:
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "pipeline --target-execute requires --inject-uploaded-torrent for full live retorrent closure."})
        elif args.inject_uploaded_torrent and not (args.download_uploaded_torrent or args.uploaded_torrent_file or args.uploaded_torrent_id):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "--inject-uploaded-torrent requires --download-uploaded-torrent, --uploaded-torrent-id, or --uploaded-torrent-file."})
        elif args.inject_uploaded_torrent and not (args.uploaded_save_path or effective_content_path):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "--uploaded-save-path or an inferred completed content path is required with --inject-uploaded-torrent."})
        elif args.wait_uploaded_complete and not args.inject_uploaded_torrent:
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "--wait-uploaded-complete requires --inject-uploaded-torrent."})
        elif args.target_execute and not _source_ready_for_live_target_upload(stages):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "Skipped because current pipeline run did not verify complete source qBittorrent content before live target upload."})
        elif args.target_execute and not (args.uploaded_torrent_file or args.uploaded_torrent_id) and not _target_duplicate_ready_for_live_upload(stages):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "Skipped because current pipeline run did not complete a clean MTEAM duplicate check before live target upload."})
        elif args.target_execute and not _runtime_check_ready(stages):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "Skipped because focused ptcli runtime dependencies are not ready for live target upload."})
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
    summary["requested_actions"] = requested_actions
    summary["effective_actions"] = effective_actions
    summary["requested_source_id"] = args.source_id
    summary["input_source_id"] = args.source_id
    summary["source_torrent_id"] = source_torrent_id
    payload = {
        "status": "blocked" if blockers else "ok",
        "source_tracker": source_tracker,
        "requested_source_id": args.source_id,
        "input_source_id": args.source_id,
        "source_torrent_id": source_torrent_id,
        "source_torrent_hash": effective_source_torrent_hash,
        "target_trackers": target_trackers,
        "client": args.client,
        "qbit_options": _pipeline_qbit_options(args),
        "path": effective_content_path,
        "requested_path": args.content_path,
        "target_torrent_file": effective_target_torrent_file,
        "ready": ready,
        "complete": bool(closure.get("complete")),
        "blockers": blockers,
        "requested_actions": requested_actions,
        "effective_actions": effective_actions,
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
    artifacts = _run_summary_artifacts(payload, str(payload.get("summary_file") or ""))
    payload["artifacts"] = artifacts
    resume_commands = _run_summary_resume_commands(payload, artifacts)
    payload["resume_commands"] = resume_commands
    payload["resume_state"] = _run_summary_resume_state(payload, artifacts, resume_commands)
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


def _runtime_check_ready(stages: list[dict[str, Any]]) -> bool:
    stage = _find_stage(stages, "runtime-check")
    return True if stage is None or stage.get("skipped") else bool(stage.get("ok"))


def _source_ready_for_live_target_upload(stages: list[dict[str, Any]]) -> bool:
    source_download = _find_stage(stages, "source-download")
    inject_source = _find_stage(stages, "inject-source")
    wait_complete = _find_stage(stages, "wait-complete")
    match = _find_stage(stages, "match")
    source_content_verify = _find_stage(stages, "source-content-verify")
    source_downloaded_flow_ready = _stage_completed(source_download) and _source_injection_verified(inject_source) and _stage_completed(wait_complete)
    existing_content_ready = _match_stage_has_match(match) and _source_content_verified(source_content_verify)
    return source_downloaded_flow_ready or existing_content_ready


def _target_duplicate_ready_for_live_upload(stages: list[dict[str, Any]]) -> bool:
    dupe_stage = _find_stage(stages, "target-dupe-check")
    if not _stage_completed(dupe_stage):
        return False
    result = dupe_stage.get("result")
    if not isinstance(result, dict):
        return False
    return bool(result.get("searched")) and int(result.get("count", 0) or 0) == 0


def _load_existing_target_prepare_package(package_dir: str) -> dict[str, Any]:
    try:
        package = load_mteam_prepare_package(package_dir)
    except Exception as exc:
        return {
            "target_tracker": "MTEAM",
            "package_dir": package_dir,
            "blockers": [str(exc)],
        }
    blockers = _existing_target_prepare_blockers(package)
    return {
        **package,
        "blockers": blockers,
        "reused": True,
    }


def _content_path_from_existing_target_package(package_dir: str) -> str | None:
    try:
        package = load_mteam_prepare_package(package_dir)
    except Exception:
        return None
    preview = package.get("preview")
    if not isinstance(preview, dict):
        return None
    content_path = preview.get("content_path")
    return str(content_path) if content_path else None


def _existing_target_prepare_blockers(package: dict[str, Any]) -> list[str]:
    blockers = _string_list(package.get("blockers"))
    rule_review = package.get("rule_review")
    if isinstance(rule_review, dict):
        _extend_unique_string(blockers, _string_list(rule_review.get("blockers")))
    upload_gate = package.get("upload_gate")
    if isinstance(upload_gate, dict):
        _extend_unique_string(blockers, _string_list(upload_gate.get("blockers")))
        if upload_gate.get("ready") is False:
            _append_unique_string(blockers, "MTEAM upload gate is not ready.")
    return blockers


async def _existing_source_torrent_payload(source_torrent_file: str) -> dict[str, Any]:
    return await asyncio.to_thread(_validate_existing_torrent_file, source_torrent_file, "Source")


async def _existing_uploaded_torrent_payload(uploaded_torrent_file: str) -> dict[str, Any]:
    torrent = await asyncio.to_thread(_validate_existing_torrent_file, uploaded_torrent_file, "Uploaded target")
    return {
        "status": "uploaded",
        "downloaded_torrent": torrent,
        "uploaded_torrent_file": torrent["path"],
    }


def _validate_existing_torrent_file(torrent_file: str, label: str) -> dict[str, Any]:
    path = Path(torrent_file).expanduser()
    if not path.exists():
        raise ValueError(f"{label} torrent file not found: {path}")
    if not path.is_file():
        raise ValueError(f"{label} torrent path is not a file: {path}")
    with path.open("rb") as torrent_file:
        if torrent_file.read(1) != b"d":
            raise ValueError(f"{label} torrent file does not look like a .torrent file.")
    return {
        **_torrent_file_evidence(path),
        "reused": True,
    }


def _torrent_file_evidence(torrent_file: str | Path) -> dict[str, Any]:
    path = Path(torrent_file).expanduser()
    payload: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
    }
    if path.is_file():
        data = path.read_bytes()
        payload.update(
            {
                "size_bytes": len(data),
                "sha1": hashlib.sha1(data).hexdigest(),
            }
        )
        infohash = _torrent_file_infohash(path)
        if infohash:
            payload["torrent_hash"] = infohash
            payload["infohash"] = infohash
    return payload


def _torrent_file_infohash(torrent_file: Path) -> str | None:
    try:
        return str(Torrent.read(str(torrent_file), validate=False).infohash)
    except Exception:
        return None


def _source_torrent_verify_stage(source_download_stage: dict[str, Any], expected_hash: str | None) -> dict[str, Any]:
    result = source_download_stage.get("result") if isinstance(source_download_stage, dict) else None
    actual_hash = _torrent_hash_from_result(result)
    expected = _normalize_torrent_hash(expected_hash)
    if expected and actual_hash and expected != actual_hash:
        return {
            "stage": "source-torrent-verify",
            "ok": False,
            "message": "Downloaded source torrent infohash does not match source tracker metadata.",
            "result": {
                "verified": False,
                "expected_hash": expected,
                "actual_hash": actual_hash,
                "blockers": [f"source torrent hash mismatch: expected {expected}, got {actual_hash}"],
            },
        }
    return {
        "stage": "source-torrent-verify",
        "ok": True,
        "result": {
            "verified": bool(expected and actual_hash),
            "expected_hash": expected,
            "actual_hash": actual_hash,
            "message": "Source torrent hash matched source metadata." if expected and actual_hash else "Source torrent hash verification skipped because one side did not expose an infohash.",
        },
    }


def _source_content_verify_stage(match_stage: dict[str, Any], expected_hash: str | None) -> dict[str, Any]:
    result = match_stage.get("result") if isinstance(match_stage, dict) else None
    expected = _normalize_torrent_hash(expected_hash)
    match_hashes = _match_hashes(result)
    has_match_evidence = _result_has_match_evidence(result)
    if expected and has_match_evidence and not match_hashes:
        return {
            "stage": "source-content-verify",
            "ok": False,
            "message": "Matched qBittorrent content did not expose torrent hash evidence for source verification.",
            "result": {
                "verified": False,
                "expected_hash": expected,
                "matched_hashes": [],
                "blockers": [f"source content hash unavailable: expected {expected}, but qBittorrent match did not expose a hash"],
            },
        }
    if expected and match_hashes and expected not in match_hashes:
        return {
            "stage": "source-content-verify",
            "ok": False,
            "message": "Matched qBittorrent content does not include the source tracker torrent hash.",
            "result": {
                "verified": False,
                "expected_hash": expected,
                "matched_hashes": match_hashes,
                "blockers": [f"source content hash mismatch: expected {expected}, matched {', '.join(match_hashes)}"],
            },
        }
    return {
        "stage": "source-content-verify",
        "ok": True,
        "result": {
            "verified": bool(expected and match_hashes),
            "expected_hash": expected,
            "matched_hashes": match_hashes,
            "message": "Matched qBittorrent content includes the source torrent hash." if expected and match_hashes else "Source content hash verification skipped because source hash or match hash evidence is unavailable.",
        },
    }


def _result_has_match_evidence(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    matches = result.get("matches")
    return isinstance(matches, list) and any(_match_has_evidence(match) for match in matches)


def _match_hashes(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []
    matches = result.get("matches")
    if not isinstance(matches, list):
        return []
    hashes: list[str] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        torrent_hash = _normalize_torrent_hash(match.get("hash") or match.get("torrent_hash") or match.get("torrenthash"))
        if torrent_hash and torrent_hash not in hashes:
            hashes.append(torrent_hash)
    return hashes


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
    _extend_unique_string(blockers, _client_verification_blockers(result.get("client_verification")))
    return blockers


def _wait_complete_result_blockers(result: dict[str, Any]) -> list[str]:
    blockers = _string_list(result.get("blockers"))
    if result.get("complete") is False:
        _append_unique_string(blockers, "qBittorrent did not report the source torrent as complete.")
    return blockers


def _target_upload_result_blockers(result: dict[str, Any]) -> list[str]:
    blockers = _string_list(result.get("blockers"))
    _extend_unique_string(blockers, _nested_blockers(result.get("downloaded_torrent"), "downloaded_torrent"))
    _extend_unique_string(blockers, _downloaded_torrent_file_blockers(result.get("downloaded_torrent")))
    _extend_unique_string(blockers, _nested_blockers(result.get("injected_torrent"), "injected_torrent"))
    _extend_unique_string(blockers, _uploaded_torrent_hash_consistency_blockers(result))
    injected_torrent = result.get("injected_torrent")
    if isinstance(injected_torrent, dict):
        _extend_unique_string(blockers, [f"injected_torrent: {blocker}" for blocker in _client_verification_blockers(injected_torrent.get("client_verification"))])
    _extend_unique_string(blockers, _nested_blockers(result.get("uploaded_wait"), "uploaded_wait"))
    uploaded_wait = result.get("uploaded_wait")
    if isinstance(uploaded_wait, dict) and uploaded_wait.get("complete") is False:
        _append_unique_string(blockers, "uploaded_wait: qBittorrent did not report the uploaded target torrent as complete.")
    return blockers


def _downloaded_torrent_file_blockers(downloaded_torrent: Any) -> list[str]:
    if not isinstance(downloaded_torrent, dict):
        return []
    blockers: list[str] = []
    if not downloaded_torrent.get("path"):
        blockers.append("downloaded_torrent: target torrent file path is missing.")
    if downloaded_torrent.get("exists") is False:
        blockers.append("downloaded_torrent: target torrent file does not exist on disk.")
    if downloaded_torrent.get("size_bytes") == 0:
        blockers.append("downloaded_torrent: target torrent file is empty.")
    if downloaded_torrent.get("metadata_readable") is False:
        blockers.append("downloaded_torrent: target torrent metadata is not readable.")
    return blockers


def _nested_blockers(payload: Any, label: str) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return [f"{label}: {blocker}" for blocker in _string_list(payload.get("blockers"))]


def _client_verification_blockers(client_verification: Any) -> list[str]:
    if not isinstance(client_verification, dict):
        return []
    checks = {
        "visible": "qBittorrent did not list the injected torrent after add.",
        "save_path_matched": "qBittorrent did not report the requested save path for the injected torrent.",
        "category_matched": "qBittorrent did not report the requested category for the injected torrent.",
        "tags_matched": "qBittorrent did not report the requested tags for the injected torrent.",
    }
    return [message for key, message in checks.items() if client_verification.get(key) is False]


def _uploaded_torrent_hash_consistency_blockers(result: dict[str, Any]) -> list[str]:
    hashes = _uploaded_torrent_hash_evidence(result)
    if len(set(hashes.values())) <= 1:
        return []
    evidence = ", ".join(f"{label}={torrent_hash}" for label, torrent_hash in hashes.items())
    return [f"uploaded_torrent_hash: inconsistent target torrent hashes ({evidence})"]


def _uploaded_torrent_hash_evidence(result: dict[str, Any]) -> dict[str, str]:
    evidence: dict[str, str] = {}
    submitted_hash = _normalize_torrent_hash(result.get("submitted_torrent_hash"))
    if submitted_hash:
        evidence["submitted_torrent"] = submitted_hash
    response_hash = _normalize_torrent_hash(result.get("uploaded_torrent_hash"))
    if response_hash:
        evidence["upload_response"] = response_hash
    downloaded_hash = _torrent_hash_from_result(result.get("downloaded_torrent"))
    if downloaded_hash:
        evidence["downloaded_torrent"] = downloaded_hash
    injected_hash = _torrent_hash_from_result(result.get("injected_torrent"))
    if injected_hash:
        evidence["injected_torrent"] = injected_hash
    uploaded_wait = result.get("uploaded_wait")
    if isinstance(uploaded_wait, dict):
        query = uploaded_wait.get("query")
        if isinstance(query, dict):
            wait_hash = _normalize_torrent_hash(query.get("torrent_hash"))
            if wait_hash:
                evidence["uploaded_wait"] = wait_hash
    return evidence


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
        "compliance": _pipeline_compliance_summary(stages),
        "resume": evidence.get("resume", {}),
        "source": evidence.get("source", {}),
        "target": evidence.get("target", {}),
    }


def _pipeline_requested_actions(args: argparse.Namespace) -> dict[str, bool]:
    return {
        "download_source": bool(args.download_source),
        "inject_source": bool(args.inject_source),
        "wait_complete": bool(args.wait_complete),
        "check_dupes": bool(args.check_dupes),
        "prepare_target": bool(args.prepare_target),
        "target_torrent_export": bool(args.export_target_torrent),
        "target_torrent_sanitize": bool(args.sanitize_target_torrent),
        "upload_target": bool(args.upload_target),
        "target_execute": bool(args.target_execute),
        "check_runtime": bool(getattr(args, "check_runtime", False)),
        "download_uploaded_torrent": bool(args.download_uploaded_torrent),
        "uploaded_torrent_id": bool(args.uploaded_torrent_id),
        "inject_uploaded_torrent": bool(args.inject_uploaded_torrent),
        "wait_uploaded_complete": bool(args.wait_uploaded_complete),
        "write_summary": bool(args.write_summary),
    }


def _pipeline_effective_actions(
    args: argparse.Namespace,
    *,
    live_target_upload: bool,
    runtime_check: bool,
    source_download: bool,
    source_injection: bool,
    source_wait: bool,
    target_torrent_export: bool,
    target_torrent_sanitize: bool,
) -> dict[str, bool]:
    return {
        "live_target_upload": live_target_upload,
        "check_runtime": runtime_check,
        "download_source": source_download,
        "inject_source": source_injection,
        "wait_complete": source_wait,
        "check_dupes": bool(args.check_dupes),
        "prepare_target": bool(args.prepare_target),
        "target_torrent_export": target_torrent_export,
        "target_torrent_sanitize": target_torrent_sanitize,
        "upload_target": bool(args.upload_target),
        "target_execute": bool(args.target_execute),
        "download_uploaded_torrent": bool(args.download_uploaded_torrent),
        "inject_uploaded_torrent": bool(args.inject_uploaded_torrent),
        "wait_uploaded_complete": bool(args.wait_uploaded_complete),
        "write_summary": bool(args.write_summary),
    }


def _pipeline_qbit_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "source": {
            "category": args.qbit_category,
            "tags": args.qbit_tags,
            "paused": bool(args.paused),
        },
        "uploaded": {
            "category": args.uploaded_qbit_category,
            "tags": args.uploaded_qbit_tags,
            "paused": bool(args.uploaded_paused),
        },
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
            "rule_obligations": _rule_obligation_summary(rule_review),
        },
    }


def _pipeline_compliance_summary(stages: list[dict[str, Any]]) -> dict[str, Any]:
    rule_stage = _find_stage(stages, "rule-check")
    rule_result = rule_stage.get("result") if isinstance(rule_stage, dict) else None
    target_prepare_stage = _find_stage(stages, "target-prepare")
    target_prepare_result = target_prepare_stage.get("result") if isinstance(target_prepare_stage, dict) else None
    rule_review = target_prepare_result.get("rule_review") if isinstance(target_prepare_result, dict) else None
    if not isinstance(rule_result, dict):
        return {
            "ready": False,
            "rules_acknowledged": False,
            "site_specific_rules_encoded": False,
            "policy_checks": "missing_rule_check",
            "manual_review": {"required": True, "acknowledged": False},
            "rule_obligations": {"ready": False, "count": 0, "acknowledged_count": 0, "items": []},
            "blockers": ["rule-check stage did not produce compliance evidence."],
        }

    automation_scope = rule_result.get("automation_scope") if isinstance(rule_result.get("automation_scope"), dict) else {}
    obligations = _rule_obligations_from_result(rule_result, rule_review)
    acknowledged = [obligation for obligation in obligations if obligation.get("acknowledged") is True]
    blockers = _failed_check_messages(rule_result.get("checks"))
    if isinstance(rule_review, dict):
        _extend_unique_string(blockers, _string_list(rule_review.get("blockers")))
    return {
        "ready": bool(rule_result.get("ready")) and not blockers,
        "rules_acknowledged": _check_ok(rule_result.get("checks"), "rules_acknowledged"),
        "site_specific_rules_encoded": bool(automation_scope.get("site_specific_rules_encoded")),
        "policy_checks": automation_scope.get("concrete_policy_checks") or "unknown",
        "manual_review": rule_result.get("manual_review") if isinstance(rule_result.get("manual_review"), dict) else {"required": True, "acknowledged": False},
        "automation_scope": automation_scope,
        "rule_obligations": {
            "ready": bool(obligations) and len(acknowledged) == len(obligations),
            "count": len(obligations),
            "acknowledged_count": len(acknowledged),
            "items": obligations,
        },
        "blockers": blockers,
        "disclaimer": "Site-specific tracker rules are not fully encoded; this workflow requires manual source/target rule review before live automation.",
    }


def _rule_obligations_from_result(rule_result: dict[str, Any], rule_review: Any) -> list[dict[str, Any]]:
    obligations = rule_result.get("rule_obligations")
    if not isinstance(obligations, list) and isinstance(rule_review, dict):
        obligations = rule_review.get("rule_obligations")
    if not isinstance(obligations, list):
        return []
    return [obligation for obligation in obligations if isinstance(obligation, dict)]


def _rule_obligation_summary(rule_review: Any) -> dict[str, Any]:
    obligations = rule_review.get("rule_obligations") if isinstance(rule_review, dict) else None
    if not isinstance(obligations, list):
        return {
            "ready": False,
            "count": 0,
            "source_acknowledged": False,
            "mteam_acknowledged": False,
            "missing": ["source_download_and_retorrent", "mteam_upload_and_seed"],
        }
    source_acknowledged = _rule_obligation_acknowledged(obligations, role="source", action="download_and_retorrent")
    mteam_acknowledged = _rule_obligation_acknowledged(obligations, role="target", action="upload_and_seed", tracker="MTEAM")
    missing = []
    if not source_acknowledged:
        missing.append("source_download_and_retorrent")
    if not mteam_acknowledged:
        missing.append("mteam_upload_and_seed")
    return {
        "ready": not missing,
        "count": len([obligation for obligation in obligations if isinstance(obligation, dict)]),
        "source_acknowledged": source_acknowledged,
        "mteam_acknowledged": mteam_acknowledged,
        "missing": missing,
    }


def _rule_obligation_acknowledged(obligations: list[Any], *, role: str, action: str, tracker: str | None = None) -> bool:
    return any(
        isinstance(obligation, dict)
        and obligation.get("role") == role
        and obligation.get("action") == action
        and (tracker is None or obligation.get("tracker") == tracker)
        and bool(obligation.get("rules_url"))
        and obligation.get("acknowledged") is True
        for obligation in obligations
    )


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
    artifacts = _run_summary_artifacts(payload, str(destination))
    resume_commands = _run_summary_resume_commands(payload, artifacts)
    summary_payload = {
        "schema_version": 1,
        "kind": "ptcli.pipeline.run_summary",
        "summary_file": str(destination),
        "status": payload.get("status"),
        "source_tracker": payload.get("source_tracker"),
        "requested_source_id": payload.get("requested_source_id"),
        "input_source_id": payload.get("input_source_id"),
        "source_torrent_id": payload.get("source_torrent_id"),
        "target_trackers": payload.get("target_trackers"),
        "client": payload.get("client"),
        "qbit_options": payload.get("qbit_options"),
        "path": payload.get("path"),
        "target_torrent_file": payload.get("target_torrent_file"),
        "ready": payload.get("ready"),
        "complete": payload.get("complete"),
        "blockers": payload.get("blockers", []),
        "requested_actions": payload.get("requested_actions", {}),
        "effective_actions": payload.get("effective_actions", {}),
        "closure": payload.get("closure"),
        "summary": payload.get("summary"),
        "evidence": payload.get("evidence"),
        "next_actions": payload.get("next_actions", []),
        "artifacts": artifacts,
        "resume_commands": resume_commands,
        "resume_state": _run_summary_resume_state(payload, artifacts, resume_commands),
        "stages": payload.get("stages", []),
    }
    destination.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(destination)


def _run_summary_artifacts(payload: dict[str, Any], summary_file: str) -> dict[str, Any]:
    stages = payload.get("stages")
    source_download = _find_stage(stages, "source-download") if isinstance(stages, list) else None
    target_prepare = _find_stage(stages, "target-prepare") if isinstance(stages, list) else None
    target_upload = _find_stage(stages, "target-upload") if isinstance(stages, list) else None

    artifacts: dict[str, Any] = {
        "summary_file": summary_file,
        "target_torrent_file": payload.get("target_torrent_file"),
    }
    if isinstance(source_download, dict):
        source_result = source_download.get("result")
        if isinstance(source_result, dict):
            artifacts["source_torrent_file"] = source_result.get("path")
    if isinstance(target_prepare, dict):
        prepare_result = target_prepare.get("result")
        if isinstance(prepare_result, dict):
            artifacts["target_package_dir"] = prepare_result.get("package_dir")
            artifacts["target_package_files"] = prepare_result.get("files")
    if isinstance(target_upload, dict):
        upload_result = target_upload.get("result")
        if isinstance(upload_result, dict):
            artifacts["uploaded_torrent_id"] = _uploaded_torrent_id_from_result(upload_result)
            artifacts["uploaded_torrent_hash"] = _uploaded_torrent_hash_from_result(upload_result)
            fresh_duplicate_check = upload_result.get("fresh_duplicate_check")
            if isinstance(fresh_duplicate_check, dict):
                artifacts["fresh_duplicate_check"] = fresh_duplicate_check
            downloaded_torrent = upload_result.get("downloaded_torrent")
            if isinstance(downloaded_torrent, dict):
                artifacts["uploaded_torrent_file"] = downloaded_torrent.get("path")
    if "fresh_duplicate_check" not in artifacts and isinstance(stages, list):
        target_dupe_check = _find_stage(stages, "target-dupe-check")
        dupe_result = target_dupe_check.get("result") if isinstance(target_dupe_check, dict) else None
        if isinstance(dupe_result, dict):
            artifacts["fresh_duplicate_check"] = dupe_result
    return artifacts


def _run_summary_resume_commands(payload: dict[str, Any], artifacts: dict[str, Any]) -> list[dict[str, str]]:
    source_tracker = str(payload.get("source_tracker") or "")
    source_torrent_id = str(payload.get("source_torrent_id") or "")
    target_trackers = payload.get("target_trackers")
    target_trackers_arg = ",".join(str(tracker) for tracker in target_trackers) if isinstance(target_trackers, list) else str(target_trackers or "")
    client = str(payload.get("client") or "default")
    qbit_options = payload.get("qbit_options") if isinstance(payload.get("qbit_options"), dict) else {}
    source_qbit_options = qbit_options.get("source") if isinstance(qbit_options.get("source"), dict) else {}
    uploaded_qbit_options = qbit_options.get("uploaded") if isinstance(qbit_options.get("uploaded"), dict) else {}
    content_path = payload.get("path")
    path_args = ["--path", str(content_path)] if content_path else []
    uploaded_save_path_args = ["--uploaded-save-path", str(content_path)] if content_path else []
    commands: list[dict[str, str]] = []

    source_torrent_file = artifacts.get("source_torrent_file")
    if source_torrent_file:
        commands.append(
            {
                "stage": "resume-source-torrent",
                "command": _ptcli_command(
                    [
                        "pipeline",
                        "--from",
                        source_tracker,
                        "--source-id",
                        source_torrent_id,
                        "--to",
                        target_trackers_arg,
                        "--client",
                        client,
                        "--source-torrent-file",
                        str(source_torrent_file),
                        "--inject-source",
                        "--save-path",
                        str(content_path or "/downloads"),
                        *_qbit_resume_args(source_qbit_options, prefix=""),
                        "--wait-complete",
                        "--accept-rules",
                        "--write-summary",
                        "--json",
                    ]
                ),
            }
        )

    target_package_dir = artifacts.get("target_package_dir")
    target_torrent_file = artifacts.get("target_torrent_file")
    uploaded_torrent_id = artifacts.get("uploaded_torrent_id")
    if target_package_dir and target_torrent_file:
        commands.append(
            {
                "stage": "resume-target-upload",
                "command": _ptcli_command(
                    [
                        "pipeline",
                        "--from",
                        source_tracker,
                        "--source-id",
                        source_torrent_id,
                        "--to",
                        target_trackers_arg,
                        "--client",
                        client,
                        *path_args,
                        "--package-dir",
                        str(target_package_dir),
                        "--upload-target",
                        "--target-torrent-file",
                        str(target_torrent_file),
                        "--accept-rules",
                        "--target-execute",
                        "--confirm-upload",
                        "--download-uploaded-torrent",
                        "--inject-uploaded-torrent",
                        *uploaded_save_path_args,
                        *_qbit_resume_args(uploaded_qbit_options, prefix="uploaded-"),
                        "--wait-uploaded-complete",
                        "--write-summary",
                        "--json",
                    ]
                ),
            }
        )

    uploaded_torrent_file = artifacts.get("uploaded_torrent_file")
    if target_package_dir and uploaded_torrent_id and not uploaded_torrent_file:
        commands.append(
            {
                "stage": "resume-uploaded-torrent-download",
                "command": _ptcli_command(
                    [
                        "target-upload",
                        "--package-dir",
                        str(target_package_dir),
                        "--client",
                        client,
                        "--uploaded-torrent-id",
                        str(uploaded_torrent_id),
                        "--download-uploaded-torrent",
                        "--inject-uploaded-torrent",
                        *uploaded_save_path_args,
                        *_qbit_resume_args(uploaded_qbit_options, prefix="uploaded-"),
                        "--wait-uploaded-complete",
                        "--write-summary",
                        "--json",
                    ]
                ),
            }
        )
    if target_package_dir and uploaded_torrent_file:
        commands.append(
            {
                "stage": "resume-uploaded-torrent",
                "command": _ptcli_command(
                    [
                        "target-upload",
                        "--package-dir",
                        str(target_package_dir),
                        "--client",
                        client,
                        "--uploaded-torrent-file",
                        str(uploaded_torrent_file),
                        "--inject-uploaded-torrent",
                        *uploaded_save_path_args,
                        *_qbit_resume_args(uploaded_qbit_options, prefix="uploaded-"),
                        "--wait-uploaded-complete",
                        "--write-summary",
                        "--json",
                    ]
                ),
            }
        )
    return commands


def _run_summary_resume_state(payload: dict[str, Any], artifacts: dict[str, Any], resume_commands: list[dict[str, str]]) -> dict[str, Any]:
    closure = payload.get("closure") if isinstance(payload.get("closure"), dict) else {}
    blockers = closure.get("blockers") if isinstance(closure.get("blockers"), list) else []
    commands_by_stage = {str(command.get("stage")): str(command.get("command")) for command in resume_commands if isinstance(command, dict)}
    complete = bool(closure.get("complete"))
    next_command = {"stage": None, "command": None} if complete else _resume_next_command(blockers, commands_by_stage)
    return {
        "complete": complete,
        "resume_available": bool(resume_commands),
        "next_stage": next_command.get("stage"),
        "next_command": next_command.get("command"),
        "available_stages": [str(command.get("stage")) for command in resume_commands if isinstance(command, dict)],
        "artifacts": {
            "source_torrent_file": bool(artifacts.get("source_torrent_file")),
            "target_package_dir": bool(artifacts.get("target_package_dir")),
            "target_torrent_file": bool(artifacts.get("target_torrent_file")),
            "uploaded_torrent_id": bool(artifacts.get("uploaded_torrent_id")),
            "uploaded_torrent_file": bool(artifacts.get("uploaded_torrent_file")),
        },
        "blockers": [str(blocker) for blocker in blockers],
    }


def _resume_next_command(blockers: list[Any], commands_by_stage: dict[str, str]) -> dict[str, str | None]:
    preferred_stages: list[str] = []
    blocker_names = {str(blocker) for blocker in blockers}
    if "source.ready" in blocker_names:
        preferred_stages.append("resume-source-torrent")
    if "target.downloaded" in blocker_names:
        preferred_stages.append("resume-uploaded-torrent-download")
    if "target.uploaded" in blocker_names:
        preferred_stages.append("resume-target-upload")
    if "target.injected" in blocker_names or "target.seeding" in blocker_names:
        preferred_stages.extend(["resume-uploaded-torrent", "resume-uploaded-torrent-download"])
    preferred_stages.extend(["resume-source-torrent", "resume-target-upload", "resume-uploaded-torrent-download", "resume-uploaded-torrent"])
    for stage in preferred_stages:
        command = commands_by_stage.get(stage)
        if command:
            return {"stage": stage, "command": command}
    return {"stage": None, "command": None}


def _qbit_resume_args(options: dict[str, Any], *, prefix: str) -> list[str]:
    args: list[str] = []
    category = options.get("category")
    tags = options.get("tags")
    if category:
        args.extend([f"--{prefix}qbit-category", str(category)])
    if tags:
        args.extend([f"--{prefix}qbit-tags", str(tags)])
    if options.get("paused"):
        args.append(f"--{prefix}paused")
    return args


def _ptcli_command(args: list[str]) -> str:
    return shlex.join(["python3", "ptcli.py", *args])


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
    if blocker.startswith("runtime-check:"):
        return "Install the focused ptcli runtime dependencies with requirements-ptcli.txt, then rerun the pipeline."
    if blocker.startswith("source-download:"):
        return "Fix source torrent download prerequisites, then re-run with --download-source after runtime-check, flow-check, source-info, and rule-check pass."
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
        "source.ready": "Complete the source side: use --path for existing qBittorrent content, run with --download-source --inject-source --save-path and --wait-complete, or resume with --source-torrent-file.",
        "target.prepared": "Prepare the target package with --check-dupes --prepare-target --target-output-dir, or resume with --package-dir after source content is verified.",
        "target.uploaded": "Run the target upload with --upload-target --target-execute --confirm-upload after the package and torrent candidate are ready, or resume seeding with --uploaded-torrent-file if the MTEAM torrent was already downloaded.",
        "target.downloaded": "Download the generated target torrent with --download-uploaded-torrent after live upload succeeds, or provide it with --uploaded-torrent-file.",
        "target.injected": "Inject the generated target torrent into qBittorrent with --inject-uploaded-torrent and a valid uploaded save path, or resume from --uploaded-torrent-file.",
        "target.seeding": "Wait for the injected target torrent to become complete in qBittorrent with --wait-uploaded-complete; if the torrent file is already local, resume with --uploaded-torrent-file.",
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
    source_content_verify = _find_stage(stages, "source-content-verify")
    target_prepare = _find_stage(stages, "target-prepare")
    target_dupe_check = _find_stage(stages, "target-dupe-check")
    target_upload = _find_stage(stages, "target-upload")
    target_upload_result = target_upload.get("result") if target_upload and isinstance(target_upload.get("result"), dict) else {}
    target_dupe_result = target_dupe_check.get("result") if target_dupe_check and isinstance(target_dupe_check.get("result"), dict) else None
    downloaded_torrent = target_upload_result.get("downloaded_torrent") if isinstance(target_upload_result, dict) else None
    injected_torrent = target_upload_result.get("injected_torrent") if isinstance(target_upload_result, dict) else None
    uploaded_wait = target_upload_result.get("uploaded_wait") if isinstance(target_upload_result, dict) else None
    source_download_result = source_download.get("result") if source_download and isinstance(source_download.get("result"), dict) else {}
    wait_complete_result = wait_complete.get("result") if wait_complete and isinstance(wait_complete.get("result"), dict) else {}
    target_prepare_result = target_prepare.get("result") if target_prepare and isinstance(target_prepare.get("result"), dict) else {}
    source_downloaded = _stage_completed(source_download)
    source_injected = _source_injection_verified(inject_source)
    source_complete = _stage_completed(wait_complete)
    source_matched = _match_stage_has_match(match)
    source_content_verified = _source_content_verified(source_content_verify)
    target_injected = _injected_torrent_verified(injected_torrent)
    target_seeding = target_injected and (not isinstance(uploaded_wait, dict) or bool(uploaded_wait.get("complete")))
    injected_target_hash = _torrent_hash_from_result(injected_torrent)
    uploaded_target_hash = target_upload_result.get("uploaded_torrent_hash") if isinstance(target_upload_result, dict) else None
    source = {
        "ready": (source_downloaded and source_injected and source_complete) or (source_matched and source_content_verified),
        "downloaded": source_downloaded,
        "injected": source_injected,
        "injection_verified": source_injected,
        "injected_torrent": inject_source.get("result") if inject_source and isinstance(inject_source.get("result"), dict) else None,
        "injected_torrent_hash": _torrent_hash_from_stage(inject_source),
        "complete": source_complete,
        "matched": source_matched,
        "content_verified": source_content_verified,
        "content_verification": source_content_verify.get("result") if source_content_verify and isinstance(source_content_verify.get("result"), dict) else None,
        "torrent_hash": source_torrent_hash,
        "content_path": content_path,
        "source_torrent": source_download_result if isinstance(source_download_result, dict) else None,
        "source_torrent_path": source_download_result.get("path") if isinstance(source_download_result, dict) else None,
        "source_wait": wait_complete_result if isinstance(wait_complete_result, dict) else None,
        "source_torrent_reused": bool(source_download_result.get("reused")) if isinstance(source_download_result, dict) else False,
    }
    target = {
        "prepared": _stage_completed(target_prepare),
        "uploaded": isinstance(target_upload_result, dict) and target_upload_result.get("status") == "uploaded",
        "downloaded": isinstance(downloaded_torrent, dict),
        "injected": target_injected,
        "injection_verified": target_injected,
        "injected_torrent": injected_torrent if isinstance(injected_torrent, dict) else None,
        "seeding": target_seeding,
        "uploaded_wait": uploaded_wait if isinstance(uploaded_wait, dict) else None,
        "torrent_file": target_torrent_file,
        "uploaded_torrent_hash": uploaded_target_hash or injected_target_hash,
        "uploaded_torrent_id": _uploaded_torrent_id_from_result(target_upload_result),
        "injected_torrent_hash": injected_target_hash,
        "uploaded_torrent": downloaded_torrent if isinstance(downloaded_torrent, dict) else None,
        "uploaded_torrent_path": downloaded_torrent.get("path") if isinstance(downloaded_torrent, dict) else None,
        "fresh_duplicate_check": target_upload_result.get("fresh_duplicate_check") if isinstance(target_upload_result.get("fresh_duplicate_check"), dict) else target_dupe_result,
        "package_reused": bool(target_prepare_result.get("reused")) if isinstance(target_prepare_result, dict) else False,
        "uploaded_torrent_reused": bool(downloaded_torrent.get("reused")) if isinstance(downloaded_torrent, dict) else False,
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
        "resume": {
            "used": bool(source.get("source_torrent_reused") or target.get("package_reused") or target.get("uploaded_torrent_reused")),
            "source_torrent_file": bool(source.get("source_torrent_reused")),
            "target_package": bool(target.get("package_reused")),
            "uploaded_torrent_file": bool(target.get("uploaded_torrent_reused")),
        },
        "source": {
            "ready": bool(source.get("ready")),
            "mode": "resumed_torrent" if source.get("source_torrent_reused") and source.get("injected") and source.get("complete") else "downloaded" if source.get("downloaded") and source.get("injected") and source.get("complete") else "matched" if source.get("matched") else "missing",
            "torrent_hash": source.get("torrent_hash"),
            "injected_torrent_hash": source.get("injected_torrent_hash"),
            "injection_verified": bool(source.get("injection_verified")),
            "content_verified": bool(source.get("content_verified")),
            "content_verification": source.get("content_verification"),
            "content_path": source.get("content_path"),
            "source_torrent": source.get("source_torrent"),
            "source_torrent_path": source.get("source_torrent_path"),
            "source_wait": source.get("source_wait"),
            "qbit_closure": {
                "injection": _qbit_injection_evidence(source.get("injected_torrent")),
                "wait": _qbit_wait_evidence(source.get("source_wait")),
            },
            "source_torrent_reused": bool(source.get("source_torrent_reused")),
        },
        "target": {
            "ready": bool(target.get("prepared") and target.get("uploaded") and target.get("downloaded") and target.get("injected") and target.get("seeding")),
            "torrent_file": target.get("torrent_file"),
            "uploaded_torrent_id": target.get("uploaded_torrent_id"),
            "uploaded_torrent_hash": target.get("uploaded_torrent_hash"),
            "injected_torrent_hash": target.get("injected_torrent_hash"),
            "injection_verified": bool(target.get("injection_verified")),
            "seeding_verified": bool(target.get("seeding")),
            "uploaded_wait": target.get("uploaded_wait"),
            "uploaded_torrent": target.get("uploaded_torrent"),
            "uploaded_torrent_path": target.get("uploaded_torrent_path"),
            "qbit_closure": {
                "injection": _qbit_injection_evidence(target.get("injected_torrent")),
                "wait": _qbit_wait_evidence(target.get("uploaded_wait")),
            },
            "fresh_duplicate_check": target.get("fresh_duplicate_check"),
            "package_reused": bool(target.get("package_reused")),
            "uploaded_torrent_reused": bool(target.get("uploaded_torrent_reused")),
        },
    }


def _injected_torrent_verified(injected_torrent: Any) -> bool:
    if not isinstance(injected_torrent, dict) or injected_torrent.get("blockers"):
        return False
    if _client_verification_blockers(injected_torrent.get("client_verification")):
        return False
    if "verified_in_client" in injected_torrent:
        return bool(injected_torrent.get("verified_in_client"))
    return True


def _source_injection_verified(stage: dict[str, Any] | None) -> bool:
    if not _stage_completed(stage):
        return False
    return _injected_torrent_verified(stage.get("result"))


def _source_content_verified(stage: dict[str, Any] | None) -> bool:
    return True if stage is None else _stage_completed(stage)


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
    return _normalize_torrent_hash(result.get("hash") or result.get("torrent_hash") or result.get("torrenthash") or result.get("infohash"))


def _uploaded_torrent_hash_from_result(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    return _torrent_hash_from_result(result.get("injected_torrent")) or _torrent_hash_from_result(result) or _torrent_hash_from_result(result.get("downloaded_torrent"))


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
    if args.uploaded_torrent_id:
        output_dir = args.uploaded_output_dir or package_dir
        result = await download_mteam_uploaded_torrent(config, args.uploaded_torrent_id, output_dir)
        uploaded_save_path = args.uploaded_save_path or inferred_content_path
        return await _apply_uploaded_torrent_followup(config, args, result, uploaded_save_path)
    if args.uploaded_torrent_file:
        result = await _existing_uploaded_torrent_payload(args.uploaded_torrent_file)
        uploaded_save_path = args.uploaded_save_path or inferred_content_path
        return await _apply_uploaded_torrent_followup(config, args, result, uploaded_save_path)
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
    return await _apply_uploaded_torrent_followup(config, args, result, args.uploaded_save_path or inferred_content_path)


async def _apply_uploaded_torrent_followup(config: dict[str, Any], args: argparse.Namespace, result: dict[str, Any], uploaded_save_path: str | None) -> dict[str, Any]:
    if args.inject_uploaded_torrent and result.get("status") == "uploaded" and isinstance(result.get("downloaded_torrent"), dict):
        result = _with_downloaded_torrent_file_evidence(result)
        downloaded_path = str(result["downloaded_torrent"]["path"])
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


def _with_downloaded_torrent_file_evidence(result: dict[str, Any]) -> dict[str, Any]:
    downloaded_torrent = result.get("downloaded_torrent")
    if not isinstance(downloaded_torrent, dict) or not downloaded_torrent.get("path"):
        return result
    evidence = _torrent_file_evidence(str(downloaded_torrent["path"]))
    return {
        **result,
        "downloaded_torrent": {
            **downloaded_torrent,
            **evidence,
        },
    }


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
    payload = {**result, "injected_torrent": inject_result}
    uploaded_torrent_hash = _uploaded_torrent_hash_from_result(payload)
    if uploaded_torrent_hash:
        payload["uploaded_torrent_hash"] = uploaded_torrent_hash
        downloaded_torrent = payload.get("downloaded_torrent")
        if isinstance(downloaded_torrent, dict):
            downloaded_hash = _torrent_hash_from_result(downloaded_torrent)
            payload["downloaded_torrent"] = {**downloaded_torrent, "hash": downloaded_hash or uploaded_torrent_hash}
    return payload


async def _with_uploaded_wait(config: dict[str, Any], args: argparse.Namespace, result: dict[str, Any], uploaded_save_path: str | None) -> dict[str, Any]:
    uploaded_torrent_hash = _uploaded_torrent_hash_from_result(result)
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
    uploaded_save_path_arg = f"--uploaded-save-path {json.dumps(content_path, ensure_ascii=False)}" if content_path else '--uploaded-save-path "/downloads"'
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
            "stage": "resume-source-torrent",
            "command": f"python3 ptcli.py pipeline --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --source-torrent-file ./tmp/source/{source_tracker}-{source_torrent_id}.torrent --inject-source --save-path \"/downloads\" --wait-complete --accept-rules --json",
        },
        {
            "stage": "rules",
            "command": f"python3 ptcli.py rules --trackers {source_tracker},{target_trackers_arg} --json",
        },
        {
            "stage": "resume-target-package",
            "command": f"python3 ptcli.py pipeline --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --package-dir ./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg} --upload-target --target-torrent-file ./tmp/exported/mteam.torrent --accept-rules --target-execute --confirm-upload --download-uploaded-torrent --inject-uploaded-torrent {uploaded_save_path_arg} --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json",
        },
        {
            "stage": "resume-uploaded-torrent",
            "command": f"python3 ptcli.py target-upload --package-dir ./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg} --uploaded-torrent-file ./tmp/uploaded/MTEAM-<id>.torrent --inject-uploaded-torrent --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --json",
        },
        {
            "stage": "resume-uploaded-torrent-download",
            "command": f"python3 ptcli.py target-upload --package-dir ./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg} --uploaded-torrent-id <id> --download-uploaded-torrent --inject-uploaded-torrent --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --json",
        },
        {
            "stage": "doctor-live",
            "command": f"python3 ptcli.py doctor --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} {doctor_path_arg} --package-dir ./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg} --target-torrent-file ./tmp/exported/mteam.torrent --accept-rules --target-execute --confirm-upload --download-uploaded-torrent --inject-uploaded-torrent --wait-uploaded-complete --connect-qbit --probe-source --probe-target --json",
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
    scope_block = _tracker_scope_block_payload("rule-check", source_tracker, target_trackers)
    if scope_block:
        return scope_block
    return build_rule_check(source_tracker, target_trackers, accept_rules=args.accept_rules)


def _tracker_scope_block_payload(command: str, source_tracker: str | None, target_trackers: list[str] | None = None, *, source_id: str | None = None) -> dict[str, Any] | None:
    trackers = [tracker for tracker in [source_tracker, *(target_trackers or [])] if tracker]
    invalid = unsupported_trackers(trackers)
    if not invalid:
        return None

    payload: dict[str, Any] = {
        "status": "blocked",
        "command": command,
        "source_tracker": source_tracker,
        "target_trackers": target_trackers or [],
        "blockers": [f"Unsupported tracker(s) for focused CLI scope: {', '.join(invalid)}"],
    }
    if source_id is not None:
        payload.update(
            {
                "requested_source_id": source_id,
                "input_source_id": source_id,
                "source_torrent_id": extract_torrent_id(source_id),
            }
        )
    return payload


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

    if payload["status"] == "blocked" and payload.get("blockers"):
        print("Blocked:")
        for blocker in payload["blockers"]:
            print(f"  - {blocker}")
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
            _print_payload(build_sites_payload(), json_output)
            return 0

        if args.command == "rules":
            _print_payload(build_rules_payload(args), json_output)
            return 0

        if args.command == "rule-check":
            payload = build_rule_check_payload(args)
            _print_payload(payload, json_output)
            return 1 if payload.get("status") == "blocked" else 0

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
            return _source_info_exit_code(payload)

        if args.command == "source-download":
            payload = _with_captured_stdout(lambda: asyncio.run(source_download(args)), json_output)
            _print_payload(payload, json_output)
            return _source_download_exit_code(payload)

        if args.command == "flow-check":
            payload = _with_captured_stdout(lambda: flow_check_payload(args), json_output)
            _print_payload(payload, json_output)
            return 1 if payload.get("status") == "blocked" else 0

        if args.command == "doctor":
            payload = _with_captured_stdout(lambda: asyncio.run(doctor_payload(args)), json_output)
            _print_payload(payload, json_output)
            return _doctor_exit_code(args, payload)

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


def _source_info_exit_code(payload: dict[str, Any]) -> int:
    return 0 if payload.get("status") == "ok" and not payload.get("blockers") else 1


def _doctor_exit_code(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    if payload.get("status") == "blocked":
        return 1
    if not getattr(args, "target_execute", False):
        return 0
    return 0 if payload.get("ready") is True and payload.get("live_safe_to_attempt") is True else 1


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
    if _target_upload_result_blockers(payload):
        return False
    if _uploaded_torrent_hash_consistency_blockers(payload):
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
        if not _target_upload_has_followup_action(args):
            return 0
        return 0 if _target_upload_result_ready(
            payload,
            execute=False,
            download_uploaded=bool(getattr(args, "download_uploaded_torrent", False) or getattr(args, "uploaded_torrent_file", None)),
            inject_uploaded=bool(getattr(args, "inject_uploaded_torrent", False)),
            wait_uploaded_complete=bool(getattr(args, "wait_uploaded_complete", False)),
        ) else 1
    if _target_upload_result_ready(
        payload,
        execute=True,
        download_uploaded=bool(getattr(args, "download_uploaded_torrent", False)),
        inject_uploaded=bool(getattr(args, "inject_uploaded_torrent", False)),
        wait_uploaded_complete=bool(getattr(args, "wait_uploaded_complete", False)),
    ):
        return 0
    return 1


def _target_upload_has_followup_action(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "uploaded_torrent_file", None)
        or getattr(args, "download_uploaded_torrent", False)
        or getattr(args, "inject_uploaded_torrent", False)
        or getattr(args, "wait_uploaded_complete", False)
    )


if __name__ == "__main__":
    raise SystemExit(main())
