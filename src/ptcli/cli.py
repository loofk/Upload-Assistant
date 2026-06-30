"""Dedicated CLI surface for focused PT retorrent automation."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from torf import Torrent

from src.ptcli.config import load_config, resolve_client_config
from src.ptcli.credentials import build_flow_check
from src.ptcli.doctor import build_doctor_check, build_runtime_dependency_check, extend_doctor_check
from src.ptcli.flows import MTEAM_SOURCE_FLOW_TRACKERS, flow_profiles_to_dicts, get_flow_profiles
from src.ptcli.mainland import CHINESE_PT_TRACKERS, normalize_tracker, parse_tracker_list, unsupported_trackers
from src.ptcli.materials import generate_bdinfo_material, generate_mediainfo_material, generate_screenshot_materials, upload_screenshot_image_hosts
from src.ptcli.metadata import enrich_source_metadata, load_metadata_overrides, normalize_metadata_overrides
from src.ptcli.qbit import QbitReadOnlyService, match_torrents, summaries_to_dicts
from src.ptcli.rules import build_rule_check, get_rule_profiles, rule_profiles_to_dicts
from src.ptcli.source import (
    COOKIE_DOWNLOAD_URLS,
    DIRECT_DOWNLOAD_TRACKER_CLASSES,
    GENERIC_DETAILS_BASE_URLS,
    MTEAM_API_TRACKERS,
    NEXUS_DOWNLOAD_BASE_URLS,
    SOURCE_TRACKER_CLASSES,
    TTG_DOWNLOAD_BASE_URLS,
    download_source_torrent,
    extract_torrent_id,
    fetch_source_info,
    source_credential_requirements,
    source_download_adapter,
    source_info_adapter,
    source_info_has_signal,
)
from src.ptcli.target import (
    build_mteam_upload_preflight,
    create_mteam_upload_torrent_candidate,
    download_mteam_uploaded_torrent,
    load_mteam_prepare_package,
    mteam_upload_torrent_candidate_summary,
    search_mteam_duplicates,
    upload_mteam_from_package,
    write_mteam_prepare_package,
)

SUMMARY_SCHEMA_VERSION = 1
SUPPORTED_SUMMARY_KINDS = ("ptcli.pipeline.run_summary", "ptcli.target_upload.summary", "ptcli.doctor.live_readiness")
SUMMARY_CHECK_RUN_COMMANDS = {"pipeline", "target-upload", "doctor"}


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
    source_download.add_argument("--client", default="default", help="Configured qBittorrent client name for the generated source injection handoff.")
    source_download.add_argument("--save-path", help="qBittorrent save path for the generated source injection handoff.")
    source_download.add_argument("--qbit-category", help="Optional qBittorrent category for the generated source injection handoff.")
    source_download.add_argument("--qbit-tags", help="Optional qBittorrent tags for the generated source injection handoff.")
    source_download.add_argument("--paused", action="store_true", help="Mark the generated source injection handoff as paused.")
    source_download.add_argument("--wait-timeout", type=float, default=3600.0, help="Seconds for the generated source completion wait handoff.")
    source_download.add_argument("--wait-interval", type=float, default=30.0, help="Polling interval seconds for the generated source completion wait handoff.")
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
    doctor.add_argument("--uploaded-wait-timeout", type=float, default=600.0, help="Seconds to wait with --wait-uploaded-complete.")
    doctor.add_argument("--uploaded-wait-interval", type=float, default=15.0, help="Polling interval seconds for --wait-uploaded-complete.")
    doctor.add_argument("--connect-qbit", action="store_true", help="Probe qBittorrent connectivity by listing one torrent.")
    doctor.add_argument("--probe-source", action="store_true", help="Probe source tracker metadata lookup with the configured credentials/cookies.")
    doctor.add_argument("--probe-target", action="store_true", help="Probe MTEAM target duplicate-search API with the source metadata signal.")
    doctor.add_argument("--check-runtime", action="store_true", help="Verify focused ptcli runtime dependencies are importable without requiring legacy Web UI/Discord dependencies.")
    doctor.add_argument("--write-summary", action="store_true", help="Write ptcli-doctor-summary.json for live-readiness audit handoff.")
    doctor.add_argument("--summary-output-dir", help="Directory for --write-summary. Defaults to --package-dir or ./tmp/retorrent-runs.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    summary_check = subparsers.add_parser("summary-check", help="Read a ptcli summary JSON and return a unified automation verdict.")
    summary_check.add_argument("--summary-file", required=True, help="Path to ptcli-run-summary.json, ptcli-target-upload-summary.json, or ptcli-doctor-summary.json.")
    summary_output = summary_check.add_mutually_exclusive_group()
    summary_output.add_argument("--print-next-command", action="store_true", help="Print only the next resumable command; exits 0 when complete or command-ready, 1 when blocked without a command.")
    summary_output.add_argument("--print-next-argv", action="store_true", help="Print only the next safe resumable command argv as JSON; exits 0 when complete or argv-ready, 1 when blocked without argv.")
    summary_output.add_argument("--print-first-runnable-command", action="store_true", help="Print only the first allowlisted candidate command; exits 0 when complete or runnable, 1 when blocked without a runnable candidate.")
    summary_output.add_argument("--print-first-runnable-argv", action="store_true", help="Print only the first allowlisted candidate argv as JSON; exits 0 when complete or runnable, 1 when blocked without a runnable candidate.")
    summary_output.add_argument("--print-shell", action="store_true", help="Print shell export lines for automation wrappers; inspect PTCLI_AUTOMATION_EXIT_CODE for the verdict.")
    summary_output.add_argument("--run-next-command", action="store_true", help="Run the next resumable ptcli.py command without invoking a shell; exits with the child command status.")
    summary_output.add_argument("--run-first-runnable-command", action="store_true", help="Run the first allowlisted candidate ptcli.py command without invoking a shell; exits with the child command status.")
    summary_check.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

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
    pipeline.add_argument("--enrich-metadata", dest="enrich_metadata", action="store_true", default=None, help="Fill missing IMDb/TMDb/Douban metadata before duplicate check and target preparation. Enabled by default for --target-execute.")
    pipeline.add_argument("--no-enrich-metadata", dest="enrich_metadata", action="store_false", help="Skip metadata enrichment during --target-execute.")
    pipeline.add_argument("--fetch-ptgen", dest="fetch_ptgen", action="store_true", default=None, help="Fetch PTGen/Douban movie information during metadata enrichment for the MTEAM description draft. Enabled by default for --target-execute.")
    pipeline.add_argument("--no-fetch-ptgen", dest="fetch_ptgen", action="store_false", help="Skip PTGen/Douban description fetching during --target-execute.")
    pipeline.add_argument("--metadata-file", help="JSON object with imdb_id, tmdb_id, douban_id, douban_url, or ptgen_description overrides for --enrich-metadata.")
    pipeline.add_argument("--imdb-id", help="IMDb id override for --enrich-metadata.")
    pipeline.add_argument("--tmdb-id", help="TMDb id override for --enrich-metadata.")
    pipeline.add_argument("--douban-id", help="Douban id override for --enrich-metadata.")
    pipeline.add_argument("--douban-url", help="Douban URL override for --enrich-metadata.")
    pipeline.add_argument("--prepare-target", action="store_true", help="Build a dry-run target preparation preview after prior stages.")
    pipeline.add_argument("--package-dir", help="Reuse an existing MTEAM package created by pipeline --prepare-target.")
    pipeline.add_argument("--target-output-dir", default="./tmp/target", help="Directory for --prepare-target review package files.")
    pipeline.add_argument("--mediainfo-file", help="Existing MediaInfo text file to record in the MTEAM preparation materials manifest.")
    pipeline.add_argument("--bdinfo-file", help="Existing BDInfo text file to record in the MTEAM preparation materials manifest.")
    pipeline.add_argument("--generate-bdinfo", dest="generate_bdinfo", action="store_true", default=None, help="Generate BDInfo from BDMV content before preparing the MTEAM package. Enabled by default for --target-execute when --bdinfo-file is absent.")
    pipeline.add_argument("--no-generate-bdinfo", dest="generate_bdinfo", action="store_false", help="Skip BDInfo generation during target preparation.")
    pipeline.add_argument("--bdinfo-playlist", help="Optional BDMV playlist filename for --generate-bdinfo, e.g. 00800.mpls. Defaults to the first playlist file.")
    pipeline.add_argument("--generate-mediainfo", dest="generate_mediainfo", action="store_true", default=None, help="Generate MediaInfo files from --path or the resolved qBittorrent content path before preparing the MTEAM package. Enabled by default for --target-execute.")
    pipeline.add_argument("--no-generate-mediainfo", dest="generate_mediainfo", action="store_false", help="Skip MediaInfo generation during --target-execute target preparation.")
    pipeline.add_argument("--generate-screenshots", dest="generate_screenshots", action="store_true", default=None, help="Generate local video screenshots from --path or the resolved qBittorrent content path before preparing the MTEAM package. Enabled by default for --target-execute.")
    pipeline.add_argument("--no-generate-screenshots", dest="generate_screenshots", action="store_false", help="Skip screenshot generation during --target-execute target preparation.")
    pipeline.add_argument("--screenshot-count", type=int, default=3, help="Number of screenshots to generate with --generate-screenshots.")
    pipeline.add_argument("--screenshot-file", action="append", default=[], help="Existing screenshot image file to record in the MTEAM preparation materials manifest. May be repeated.")
    pipeline.add_argument("--upload-screenshots", dest="upload_screenshots", action="store_true", default=None, help="Upload screenshot files to the configured image host before preparing the MTEAM package. Enabled by default for --target-execute.")
    pipeline.add_argument("--no-upload-screenshots", dest="upload_screenshots", action="store_false", help="Skip screenshot image-host uploads during --target-execute target preparation.")
    pipeline.add_argument("--image-host", help="Image host name for --upload-screenshots. Defaults to DEFAULT.img_host_1.")
    pipeline.add_argument("--image-host-file", help="Existing image-host upload JSON file to record in the MTEAM preparation materials manifest.")
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
    retorrent.add_argument("--enrich-metadata", dest="enrich_metadata", action="store_true", default=None, help="Fill missing IMDb/TMDb/Douban metadata during --execute target preparation. Enabled by default for --execute.")
    retorrent.add_argument("--no-enrich-metadata", dest="enrich_metadata", action="store_false", help="Skip metadata enrichment during --execute.")
    retorrent.add_argument("--fetch-ptgen", dest="fetch_ptgen", action="store_true", default=None, help="Fetch PTGen/Douban movie information during --execute target preparation. Enabled by default for --execute.")
    retorrent.add_argument("--no-fetch-ptgen", dest="fetch_ptgen", action="store_false", help="Skip PTGen/Douban description fetching during --execute.")
    retorrent.add_argument("--metadata-file", help="JSON object with imdb_id, tmdb_id, douban_id, douban_url, or ptgen_description overrides during --execute.")
    retorrent.add_argument("--imdb-id", help="IMDb id override during --execute metadata enrichment.")
    retorrent.add_argument("--tmdb-id", help="TMDb id override during --execute metadata enrichment.")
    retorrent.add_argument("--douban-id", help="Douban id override during --execute metadata enrichment.")
    retorrent.add_argument("--douban-url", help="Douban URL override during --execute metadata enrichment.")
    retorrent.add_argument("--package-dir", help="Reuse an existing MTEAM package during --execute instead of preparing a new one.")
    retorrent.add_argument("--target-output-dir", default="./tmp/target", help="Directory for MTEAM target preparation package.")
    retorrent.add_argument("--mediainfo-file", help="Existing MediaInfo text file to record in the MTEAM preparation materials manifest.")
    retorrent.add_argument("--bdinfo-file", help="Existing BDInfo text file to record in the MTEAM preparation materials manifest.")
    retorrent.add_argument("--generate-bdinfo", dest="generate_bdinfo", action="store_true", default=None, help="Generate BDInfo from BDMV content during --execute target preparation. Enabled by default for --execute when --bdinfo-file is absent.")
    retorrent.add_argument("--no-generate-bdinfo", dest="generate_bdinfo", action="store_false", help="Skip BDInfo generation during --execute target preparation.")
    retorrent.add_argument("--bdinfo-playlist", help="Optional BDMV playlist filename for --generate-bdinfo, e.g. 00800.mpls.")
    retorrent.add_argument("--generate-mediainfo", dest="generate_mediainfo", action="store_true", default=None, help="Generate MediaInfo files from --path or the resolved qBittorrent content path during --execute target preparation. Enabled by default for --execute.")
    retorrent.add_argument("--no-generate-mediainfo", dest="generate_mediainfo", action="store_false", help="Skip MediaInfo/BDInfo generation during --execute target preparation.")
    retorrent.add_argument("--generate-screenshots", dest="generate_screenshots", action="store_true", default=None, help="Generate local video screenshots from --path or the resolved qBittorrent content path during --execute target preparation. Enabled by default for --execute.")
    retorrent.add_argument("--no-generate-screenshots", dest="generate_screenshots", action="store_false", help="Skip screenshot generation during --execute target preparation.")
    retorrent.add_argument("--screenshot-count", type=int, default=3, help="Number of screenshots to generate with --generate-screenshots.")
    retorrent.add_argument("--screenshot-file", action="append", default=[], help="Existing screenshot image file to record in the MTEAM preparation materials manifest. May be repeated.")
    retorrent.add_argument("--upload-screenshots", dest="upload_screenshots", action="store_true", default=None, help="Upload screenshot files to the configured image host during --execute target preparation. Enabled by default for --execute.")
    retorrent.add_argument("--no-upload-screenshots", dest="upload_screenshots", action="store_false", help="Skip screenshot image-host uploads during --execute target preparation.")
    retorrent.add_argument("--image-host", help="Image host name for --upload-screenshots. Defaults to DEFAULT.img_host_1.")
    retorrent.add_argument("--image-host-file", help="Existing image-host upload JSON file to record in the MTEAM preparation materials manifest.")
    retorrent.add_argument("--target-torrent-file", help="MTEAM .torrent file used by the live upload stage.")
    retorrent.add_argument("--export-target-torrent", action="store_true", help="Export the matched qBittorrent .torrent as the target upload candidate if --target-torrent-file is omitted.")
    retorrent.add_argument("--target-torrent-output-dir", default="./tmp/exported", help="Directory for --export-target-torrent output.")
    retorrent.add_argument("--no-sanitize-target-torrent", dest="sanitize_target_torrent", action="store_false", default=True, help="Use the provided --target-torrent-file as-is instead of creating a cleaned MTEAM upload candidate.")
    retorrent.add_argument("--write-payload", action="store_true", help="Write mteam-upload-payload.json during upload preflight.")
    retorrent.add_argument("--confirm-upload", action="store_true", help="Required with --execute to confirm manual rule review and live upload intent.")
    retorrent.add_argument("--download-uploaded-torrent", action="store_true", help="After target upload succeeds, download the generated MTEAM torrent file. Enabled automatically by --execute.")
    retorrent.add_argument("--uploaded-output-dir", help="Directory for --download-uploaded-torrent. Defaults to ./tmp/uploaded during retorrent --execute.")
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
    source_torrent = _torrent_file_evidence(output_path, require_metadata=True)
    source_torrent_verification = _source_torrent_verify_stage({"result": source_torrent}, source.get("torrenthash"))
    qbit_handoff = _source_download_qbit_handoff(args, source_torrent["path"], target_trackers)
    recommended_commands = [qbit_handoff["command"]]
    if not source_torrent_verification.get("ok"):
        verification = source_torrent_verification.get("result", {})
        verification_blockers = verification.get("blockers") if isinstance(verification, dict) else None
        blockers = [f"source-torrent-verify: {blocker}" for blocker in verification_blockers] if isinstance(verification_blockers, list) else ["source-torrent-verify: Downloaded source torrent infohash does not match source tracker metadata."]
        handoff_fields = _source_download_handoff_fields("blocked", blockers, qbit_handoff, recommended_commands)
        return {
            "status": "blocked",
            "tracker": source_tracker,
            **source_id_context,
            "target_trackers": target_trackers,
            "rule_check": rule_check,
            "source": source,
            "source_torrent": source_torrent,
            "source_torrent_verification": verification,
            "qbit_handoff": qbit_handoff,
            "recommended_commands": recommended_commands,
            **handoff_fields,
            "path": source_torrent["path"],
            "blockers": blockers,
        }
    handoff_fields = _source_download_handoff_fields("ok", [], qbit_handoff, recommended_commands)
    return {
        "status": "ok",
        "tracker": source_tracker,
        **source_id_context,
        "target_trackers": target_trackers,
        "rule_check": rule_check,
        "source": source,
        "source_torrent": source_torrent,
        "source_torrent_verification": source_torrent_verification["result"],
        "qbit_handoff": qbit_handoff,
        "recommended_commands": recommended_commands,
        **handoff_fields,
        "path": source_torrent["path"],
    }


def _source_download_handoff_fields(status: str, blockers: list[str], qbit_handoff: dict[str, Any], recommended_commands: list[dict[str, Any]]) -> dict[str, Any]:
    next_command = qbit_handoff["command"]["command"]
    next_command_argv = qbit_handoff["command"]["argv"]
    command_audit = _resume_command_audit_fields(recommended_commands, next_command, next_command_argv)
    if status != "ok":
        automation_action = "resolve_blockers"
    elif command_audit.get("next_command_placeholder"):
        automation_action = "fill_command_placeholders"
    elif command_audit.get("next_command_run_allowed"):
        automation_action = "run_next_command"
    else:
        automation_action = "unsupported_next_command"
    payload = {"status": status, "blockers": blockers, "next_stage": qbit_handoff["stage"]}
    return {
        "next_stage": qbit_handoff["stage"],
        "next_command": next_command,
        "next_command_argv": next_command_argv,
        **command_audit,
        "automation_action": automation_action,
        "automation_reason": _summary_automation_reason(payload, automation_action, blockers, next_command_run_blocker=command_audit.get("next_command_run_blocker")),
        "automation_exit_code": 1,
        "should_execute_next_command": automation_action == "run_next_command",
    }


def _source_download_qbit_handoff(args: argparse.Namespace, source_torrent_file: str, target_trackers: list[str]) -> dict[str, Any]:
    command_args = [
        "pipeline",
        "--from",
        normalize_tracker(args.tracker),
        "--source-id",
        extract_torrent_id(args.source_id),
        "--to",
        ",".join(target_trackers),
        "--source-torrent-file",
        source_torrent_file,
        "--inject-source",
        "--save-path",
        args.save_path or "<save-path>",
        "--wait-complete",
        "--accept-rules",
        "--write-summary",
        "--json",
    ]
    _append_optional_command_arg(command_args, "--config", args.config)
    _append_optional_command_arg(command_args, "--client", args.client)
    _append_optional_command_arg(command_args, "--base-dir", args.base_dir)
    _append_optional_command_arg(command_args, "--qbit-category", args.qbit_category)
    _append_optional_command_arg(command_args, "--qbit-tags", args.qbit_tags)
    if args.paused:
        command_args.append("--paused")
    _append_float_command_arg(command_args, "--wait-timeout", args.wait_timeout, default=3600.0)
    _append_float_command_arg(command_args, "--wait-interval", args.wait_interval, default=30.0)
    ready = bool(args.save_path)
    return {
        "stage": "resume-source-torrent",
        "ready": ready,
        "blockers": [] if ready else ["--save-path is required before the generated qBittorrent source injection handoff can run."],
        "command": _ptcli_command_entry("resume-source-torrent", command_args),
    }


def _append_optional_command_arg(command_args: list[str], option: str, value: Any) -> None:
    if value is not None and value != "":
        command_args.extend([option, str(value)])


def _append_float_command_arg(command_args: list[str], option: str, value: Any, *, default: float) -> None:
    if value is not None and float(value) != default:
        command_args.extend([option, f"{float(value):g}"])


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
    commands = build_plan_commands(source_tracker, source_torrent_id, target_trackers, args.content_path, config=args.config, client=args.client, base_dir=args.base_dir)

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
    package_upload_resume = bool(args.package_dir and (args.uploaded_torrent_file or args.uploaded_torrent_id))
    if not args.content_path and not args.save_path and not package_upload_resume:
        return {"status": "blocked", "plan": plan_payload, "blockers": ["--path or --save-path is required with retorrent --execute unless --package-dir is used with an uploaded torrent resume."]}

    pipeline_args = _pipeline_args_from_retorrent(args)
    pipeline_result = await pipeline_payload(pipeline_args)
    closure = pipeline_result.get("closure") if isinstance(pipeline_result.get("closure"), dict) else None
    evidence = pipeline_result.get("evidence") if isinstance(pipeline_result.get("evidence"), dict) else None
    summary = pipeline_result.get("summary") if isinstance(pipeline_result.get("summary"), dict) else None
    closure_audit = pipeline_result.get("closure_audit") if isinstance(pipeline_result.get("closure_audit"), dict) else _pipeline_closure_audit(closure, evidence)
    ready = bool(pipeline_result.get("ready"))
    artifacts = _retorrent_execute_artifacts(pipeline_result, evidence, closure)
    evidence = _evidence_with_target_material_chain(evidence, artifacts)
    material_diagnostics = pipeline_result.get("material_diagnostics") if isinstance(pipeline_result.get("material_diagnostics"), dict) else _summary_material_diagnostics({"artifacts": artifacts})
    target_preflight_diagnostics = (
        pipeline_result.get("target_preflight_diagnostics") if isinstance(pipeline_result.get("target_preflight_diagnostics"), dict) else _summary_target_preflight_diagnostics({"artifacts": artifacts})
    )
    blockers = _retorrent_execute_blockers(pipeline_result, closure, ready, artifacts, material_diagnostics, target_preflight_diagnostics)
    target_upload_payload_recovery = _target_upload_payload_recovery_summary(artifacts)
    next_actions = _retorrent_execute_next_actions({**pipeline_result, "target_upload_payload_recovery": target_upload_payload_recovery}, blockers)
    qbit_wait_diagnostics = _summary_qbit_wait_diagnostics(pipeline_result)
    qbit_wait_mismatches = _summary_qbit_wait_mismatches(qbit_wait_diagnostics)
    qbit_wait_retry_hints = _summary_qbit_wait_retry_hints(qbit_wait_diagnostics)
    closure_status = pipeline_result.get("closure_status") if isinstance(pipeline_result.get("closure_status"), dict) else _closure_status_summary(pipeline_result)
    resume_commands = pipeline_result.get("resume_commands", [])
    closure_review = pipeline_result.get("closure_review") if isinstance(pipeline_result.get("closure_review"), dict) else _pipeline_closure_review(pipeline_result, artifacts)
    flow_diagnostics = _summary_flow_diagnostics(pipeline_result)
    target_upload_diagnostics = _summary_target_upload_diagnostics(pipeline_result)
    target_payload_review = _summary_target_payload_review(artifacts, artifacts.get("target_preparation_audit") if isinstance(artifacts.get("target_preparation_audit"), dict) else {})
    if target_payload_review and not target_upload_diagnostics.get("payload_review"):
        target_upload_diagnostics = {
            **target_upload_diagnostics,
            "payload_review": {
                **target_payload_review,
                "recovery_missing": target_upload_payload_recovery.get("recovery_missing", []),
                "next_actions": target_upload_payload_recovery.get("next_actions", []),
            },
        }
    completion_matrix = _summary_completion_matrix(
        flow_diagnostics=flow_diagnostics,
        material_diagnostics=material_diagnostics,
        target_upload_diagnostics=target_upload_diagnostics,
        closure_review=closure_review,
        closure_status=closure_status,
        qbit_wait_mismatches=qbit_wait_mismatches,
    )
    completion_next_stages = _completion_matrix_preferred_stages(completion_matrix, kind="ptcli.pipeline.run_summary")
    resume_state = _retorrent_execute_resume_state(pipeline_result, artifacts, blockers, resume_commands, preferred_stages=completion_next_stages)
    resume_command_audit = _resume_command_audit_fields(resume_commands, resume_state.get("next_command"), resume_state.get("next_command_argv"))
    retorrent_status = "complete" if not blockers else "blocked"
    retorrent_complete = not blockers
    automation_fields = _retorrent_automation_fields(
        status=retorrent_status,
        blockers=blockers,
        qbit_wait_mismatch=bool(qbit_wait_mismatches),
        qbit_wait_mismatches=qbit_wait_mismatches,
        qbit_wait_retry_hints=qbit_wait_retry_hints,
        resume_state=resume_state,
        resume_command_audit=resume_command_audit,
    )
    summary_file = pipeline_result.get("summary_file")
    readiness_summary = _retorrent_readiness_summary(
        status=retorrent_status,
        ready=ready,
        complete=retorrent_complete,
        blockers=blockers,
        completion_matrix=completion_matrix,
        material_diagnostics=material_diagnostics,
        target_upload_diagnostics=target_upload_diagnostics,
        target_preflight_diagnostics=target_preflight_diagnostics,
        qbit_wait_mismatches=qbit_wait_mismatches,
        resume_state=resume_state,
        automation_fields=automation_fields,
        summary_file=summary_file,
    )
    return {
        "status": retorrent_status,
        "plan": plan_payload,
        "config": args.config,
        "base_dir": args.base_dir,
        "client": args.client,
        "output_options": pipeline_result.get("output_options") if isinstance(pipeline_result.get("output_options"), dict) else _pipeline_output_options(pipeline_args),
        "wait_options": pipeline_result.get("wait_options") if isinstance(pipeline_result.get("wait_options"), dict) else _pipeline_wait_options(pipeline_args),
        "material_options": pipeline_result.get("material_options") if isinstance(pipeline_result.get("material_options"), dict) else _pipeline_material_options(pipeline_args),
        "pipeline": pipeline_result,
        "closure": closure,
        "closure_audit": closure_audit,
        "closure_status": closure_status,
        "closure_review": closure_review,
        "evidence": evidence,
        "summary": summary,
        "flow_diagnostics": flow_diagnostics,
        "material_diagnostics": material_diagnostics,
        "target_upload_diagnostics": target_upload_diagnostics,
        "target_preflight_diagnostics": target_preflight_diagnostics,
        "target_upload_payload_recovery": target_upload_payload_recovery,
        "completion_matrix": completion_matrix,
        "completion_next_stages": list(completion_next_stages),
        "readiness_summary": readiness_summary,
        "summary_file": summary_file,
        "automation_handoff": pipeline_result.get("automation_handoff") if isinstance(pipeline_result.get("automation_handoff"), dict) else _summary_automation_handoff(str(summary_file)) if summary_file else None,
        "qbit_wait_diagnostics": qbit_wait_diagnostics,
        "qbit_wait_mismatch": bool(qbit_wait_mismatches),
        "qbit_wait_mismatches": qbit_wait_mismatches,
        "qbit_wait_retry_hints": qbit_wait_retry_hints,
        "artifacts": artifacts,
        "resume_commands": resume_commands,
        "resume_state": resume_state,
        "next_stage": resume_state.get("next_stage"),
        "next_command": resume_state.get("next_command"),
        "next_command_argv": resume_state.get("next_command_argv"),
        **resume_command_audit,
        "ready": ready,
        "complete": retorrent_complete,
        "blockers": blockers,
        **automation_fields,
        "next_actions": next_actions,
    }


def _retorrent_readiness_summary(
    *,
    status: str,
    ready: bool,
    complete: bool,
    blockers: list[str],
    completion_matrix: dict[str, Any],
    material_diagnostics: dict[str, Any],
    target_upload_diagnostics: dict[str, Any],
    target_preflight_diagnostics: dict[str, Any],
    qbit_wait_mismatches: list[str],
    resume_state: dict[str, Any],
    automation_fields: dict[str, Any],
    summary_file: Any,
) -> dict[str, Any]:
    domains = completion_matrix.get("domains") if isinstance(completion_matrix.get("domains"), dict) else {}

    def domain(name: str) -> dict[str, Any]:
        value = domains.get(name)
        return value if isinstance(value, dict) else {}

    def domain_ready(name: str) -> bool | None:
        value = domain(name).get("ready")
        return value if isinstance(value, bool) else None

    def domain_evidence(name: str) -> dict[str, Any]:
        evidence = domain(name).get("evidence")
        return evidence if isinstance(evidence, dict) else {}

    def first_bool(*values: Any) -> bool | None:
        for value in values:
            if isinstance(value, bool):
                return value
        return None

    materials_evidence = domain_evidence("materials")
    materials_domain = domain("materials")
    target_upload_evidence = domain_evidence("target_upload")
    live_gate = material_diagnostics.get("live_gate") if isinstance(material_diagnostics.get("live_gate"), dict) else {}
    description_input_chain = material_diagnostics.get("description_input_chain") if isinstance(material_diagnostics.get("description_input_chain"), dict) else {}
    if not description_input_chain:
        material_description = material_diagnostics.get("description") if isinstance(material_diagnostics.get("description"), dict) else {}
        description_input_chain = material_description.get("input_chain") if isinstance(material_description.get("input_chain"), dict) else {}
    if not description_input_chain:
        description_input_chain = _description_input_chain_from_readiness(material_diagnostics.get("readiness"))
    material_description_evidence = _readiness_material_description_evidence(material_diagnostics, target_upload_diagnostics)
    screenshot_coverage_evidence = material_description_evidence.get("screenshot_coverage") if isinstance(material_description_evidence.get("screenshot_coverage"), dict) else {}
    external_id_evidence = material_description_evidence.get("external_ids") if isinstance(material_description_evidence.get("external_ids"), dict) else {}
    ptgen_evidence = material_description_evidence.get("ptgen_description") if isinstance(material_description_evidence.get("ptgen_description"), dict) else {}
    mediainfo_evidence = material_description_evidence.get("mediainfo_or_bdinfo") if isinstance(material_description_evidence.get("mediainfo_or_bdinfo"), dict) else {}
    screenshots_evidence = material_description_evidence.get("screenshots") if isinstance(material_description_evidence.get("screenshots"), dict) else {}
    metadata_chain_evidence = material_description_evidence.get("metadata_chain") if isinstance(material_description_evidence.get("metadata_chain"), dict) else {}
    media_info_chain_evidence = material_description_evidence.get("media_info_chain") if isinstance(material_description_evidence.get("media_info_chain"), dict) else {}
    screenshot_chain_evidence = material_description_evidence.get("screenshot_chain") if isinstance(material_description_evidence.get("screenshot_chain"), dict) else {}
    next_command_argv = _argv_list(resume_state.get("next_command_argv"))
    material_recovery = _readiness_material_recovery_summary(resume_state)
    source_followup = _readiness_source_followup_summary(resume_state)
    uploaded_followup = _readiness_uploaded_followup_summary(resume_state)
    target_payload_review = target_upload_diagnostics.get("payload_review") if isinstance(target_upload_diagnostics.get("payload_review"), dict) else {}
    return {
        "status": status,
        "ready": ready,
        "complete": complete,
        "completion_ready": completion_matrix.get("ready") if isinstance(completion_matrix.get("ready"), bool) else None,
        "missing_domains": _string_list(completion_matrix.get("missing_domains")),
        "blockers": list(blockers),
        "next_stage": resume_state.get("next_stage"),
        "next_command": resume_state.get("next_command"),
        "next_command_argv": next_command_argv if next_command_argv is not None else [],
        "automation_action": automation_fields.get("automation_action"),
        "should_execute_next_command": automation_fields.get("should_execute_next_command"),
        "automation_exit_code": automation_fields.get("automation_exit_code"),
        "summary_file": str(summary_file) if summary_file else None,
        "flow_ready": domain_ready("flow"),
        "source_ready": domain_ready("source"),
        "materials_ready": domain_ready("materials"),
        "rules_ready": domain_ready("rules"),
        "target_upload_ready": domain_ready("target_upload"),
        "qbit_wait_ready": domain_ready("qbit_wait"),
        "ready_for_mteam_upload": first_bool(material_diagnostics.get("ready_for_mteam_upload"), materials_evidence.get("ready_for_mteam_upload")),
        "material_critical_ready": material_diagnostics.get("critical_ready") if isinstance(material_diagnostics.get("critical_ready"), bool) else None,
        "material_missing": _string_list(materials_domain.get("missing")),
        "material_upload_gates": material_diagnostics.get("upload_material_gates") if isinstance(material_diagnostics.get("upload_material_gates"), dict) else {},
        "material_upload_blockers": _string_list(material_diagnostics.get("upload_material_blockers")),
        "material_live_gate_present": live_gate.get("present") if isinstance(live_gate.get("present"), bool) else None,
        "material_live_gate_ready": live_gate.get("ready") if isinstance(live_gate.get("ready"), bool) else None,
        "material_live_gate_missing": _string_list(live_gate.get("missing")),
        "material_live_gate_blockers": _string_list(live_gate.get("blockers")),
        "material_live_gate_next_actions": _string_list(live_gate.get("next_actions")),
        "material_description_evidence": material_description_evidence,
        "material_description_ptgen_ready": ptgen_evidence.get("ready") if isinstance(ptgen_evidence.get("ready"), bool) else None,
        "material_description_external_ids_ready": external_id_evidence.get("ready") if isinstance(external_id_evidence.get("ready"), bool) else None,
        "material_description_external_id_missing": _string_list(external_id_evidence.get("missing")),
        "material_description_mediainfo_ready": mediainfo_evidence.get("ready") if isinstance(mediainfo_evidence.get("ready"), bool) else None,
        "material_description_screenshots_ready": screenshots_evidence.get("ready") if isinstance(screenshots_evidence.get("ready"), bool) else None,
        "material_description_screenshot_coverage_ready": screenshot_coverage_evidence.get("ready") if isinstance(screenshot_coverage_evidence.get("ready"), bool) else None,
        "material_description_screenshot_coverage_missing_count": screenshot_coverage_evidence.get("missing_count"),
        "material_description_screenshot_coverage_missing_urls": _string_list(screenshot_coverage_evidence.get("missing_urls")),
        "material_description_metadata_chain_ready": metadata_chain_evidence.get("ready") if isinstance(metadata_chain_evidence.get("ready"), bool) else None,
        "material_description_metadata_chain_missing": _description_chain_recovery_missing("metadata_chain", metadata_chain_evidence),
        "material_description_metadata_chain_next_actions": _description_chain_next_actions("metadata_chain", metadata_chain_evidence),
        "material_description_media_info_chain_ready": media_info_chain_evidence.get("ready") if isinstance(media_info_chain_evidence.get("ready"), bool) else None,
        "material_description_media_info_chain_missing": _description_chain_recovery_missing("media_info_chain", media_info_chain_evidence),
        "material_description_media_info_chain_next_actions": _description_chain_next_actions("media_info_chain", media_info_chain_evidence),
        "material_description_screenshot_chain_ready": screenshot_chain_evidence.get("ready") if isinstance(screenshot_chain_evidence.get("ready"), bool) else None,
        "material_description_screenshot_chain_missing": _description_chain_recovery_missing("screenshot_chain", screenshot_chain_evidence),
        "material_description_screenshot_chain_next_actions": _description_chain_next_actions("screenshot_chain", screenshot_chain_evidence),
        "material_description_input_chain_ready": description_input_chain.get("ready") if isinstance(description_input_chain.get("ready"), bool) else None,
        "material_description_input_chain_missing": _string_list(description_input_chain.get("missing")),
        "material_description_input_chain_next_actions": _string_list(description_input_chain.get("next_actions")),
        "material_description_input_chain": description_input_chain,
        "target_preflight_ready": target_preflight_diagnostics.get("ready") if isinstance(target_preflight_diagnostics.get("ready"), bool) else None,
        "target_preflight_materials_ready": target_preflight_diagnostics.get("materials_ready") if isinstance(target_preflight_diagnostics.get("materials_ready"), bool) else None,
        "target_preflight_description_ready": target_preflight_diagnostics.get("description_ready") if isinstance(target_preflight_diagnostics.get("description_ready"), bool) else None,
        "target_preflight_payload_ready": target_preflight_diagnostics.get("payload_ready") if isinstance(target_preflight_diagnostics.get("payload_ready"), bool) else None,
        "target_preflight_missing": _string_list(target_preflight_diagnostics.get("missing")),
        "target_preflight_description_missing": _string_list(target_preflight_diagnostics.get("description_missing")),
        "target_preflight_blockers": _string_list(target_preflight_diagnostics.get("blockers")),
        "target_upload_payload_recovery_missing": _string_list(target_payload_review.get("recovery_missing")),
        "target_upload_payload_next_actions": _string_list(target_payload_review.get("next_actions")),
        "ready_for_uploaded_seeding": first_bool(target_upload_diagnostics.get("ready_for_uploaded_seeding"), target_upload_evidence.get("ready_for_uploaded_seeding")),
        "ready_for_source_seeding": source_followup.get("ready_for_source_seeding") if isinstance(source_followup.get("ready_for_source_seeding"), bool) else None,
        "qbit_wait_mismatch": bool(qbit_wait_mismatches),
        "qbit_wait_mismatches": list(qbit_wait_mismatches),
        "material_recovery": material_recovery,
        "source_followup": source_followup,
        "uploaded_followup": uploaded_followup,
    }


def _readiness_material_description_evidence(material_diagnostics: dict[str, Any], target_upload_diagnostics: dict[str, Any]) -> dict[str, Any]:
    description = material_diagnostics.get("description") if isinstance(material_diagnostics.get("description"), dict) else {}
    evidence = description.get("evidence") if isinstance(description.get("evidence"), dict) else {}
    if evidence:
        return evidence
    evidence = _description_evidence_from_readiness(material_diagnostics.get("readiness"))
    if evidence:
        return evidence
    payload_review = target_upload_diagnostics.get("payload_review") if isinstance(target_upload_diagnostics.get("payload_review"), dict) else {}
    payload_description = payload_review.get("description") if isinstance(payload_review.get("description"), dict) else {}
    evidence = payload_description.get("evidence") if isinstance(payload_description.get("evidence"), dict) else {}
    return evidence


def _description_evidence_from_readiness(readiness: Any) -> dict[str, Any]:
    if not isinstance(readiness, dict):
        return {}
    evidence: dict[str, Any] = {}
    chain_fields = {
        "metadata_chain": ("material_description_metadata_chain_ready", "material_description_metadata_chain_missing"),
        "media_info_chain": ("material_description_media_info_chain_ready", "material_description_media_info_chain_missing"),
        "screenshot_chain": ("material_description_screenshot_chain_ready", "material_description_screenshot_chain_missing"),
    }
    for chain_name, (ready_key, missing_key) in chain_fields.items():
        ready = readiness.get(ready_key)
        missing = _string_list(readiness.get(missing_key))
        if not isinstance(ready, bool) and not missing:
            continue
        chain: dict[str, Any] = {}
        if isinstance(ready, bool):
            chain["ready"] = ready
        if missing:
            chain["missing"] = missing
        evidence[chain_name] = chain
    return evidence


def _description_input_chain_from_readiness(readiness: Any) -> dict[str, Any]:
    if not isinstance(readiness, dict):
        return {}
    ready = readiness.get("material_description_input_chain_ready")
    missing = _string_list(readiness.get("material_description_input_chain_missing"))
    next_actions = _string_list(readiness.get("material_description_input_chain_next_actions"))
    if not isinstance(ready, bool) and not missing and not next_actions:
        return {}
    result: dict[str, Any] = {}
    if isinstance(ready, bool):
        result["ready"] = ready
    if missing:
        result["missing"] = missing
    if next_actions:
        result["next_actions"] = next_actions
    return result


def _description_chain_recovery_missing(chain_name: str, chain: Any) -> list[str]:
    if not isinstance(chain, dict):
        return []
    explicit_missing = _string_list(chain.get("missing"))
    if explicit_missing:
        return explicit_missing
    return _description_evidence_recovery_missing({chain_name: chain})


def _description_chain_next_actions(chain_name: str, chain: Any) -> list[str]:
    explicit = _string_list(chain.get("next_actions")) if isinstance(chain, dict) else []
    if explicit:
        return explicit
    return _target_preparation_missing_next_actions(_description_chain_recovery_missing(chain_name, chain))


def _target_material_chain_summary(payload_review: Any) -> dict[str, Any]:
    if not isinstance(payload_review, dict):
        return {}
    description = payload_review.get("description") if isinstance(payload_review.get("description"), dict) else {}
    evidence = description.get("evidence") if isinstance(description.get("evidence"), dict) else {}
    chains: dict[str, Any] = {}
    for chain_name in ("metadata_chain", "media_info_chain", "screenshot_chain"):
        chain = evidence.get(chain_name) if isinstance(evidence.get(chain_name), dict) else {}
        if not chain:
            continue
        chains[chain_name] = {
            "ready": chain.get("ready") if isinstance(chain.get("ready"), bool) else None,
            "missing": _description_chain_recovery_missing(chain_name, chain),
            "next_actions": _description_chain_next_actions(chain_name, chain),
        }
    if not chains:
        return {}
    ready_values = [chain["ready"] for chain in chains.values() if isinstance(chain.get("ready"), bool)]
    return {
        "ready": all(ready_values) if ready_values else None,
        "chains": chains,
    }


def _evidence_with_target_material_chain(evidence: Any, artifacts: dict[str, Any]) -> dict[str, Any] | Any:
    if not isinstance(evidence, dict):
        return evidence
    material_chain = artifacts.get("target_material_chain") if isinstance(artifacts.get("target_material_chain"), dict) else {}
    if not material_chain:
        return evidence
    target = evidence.get("target") if isinstance(evidence.get("target"), dict) else {}
    if isinstance(target.get("target_material_chain"), dict):
        return evidence
    enriched = dict(evidence)
    enriched["target"] = {**target, "target_material_chain": material_chain}
    return enriched


def _summary_target_material_chain(payload: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    material_chain = artifacts.get("target_material_chain") if isinstance(artifacts.get("target_material_chain"), dict) else {}
    if material_chain:
        return material_chain
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    evidence_target = evidence.get("target") if isinstance(evidence.get("target"), dict) else {}
    return evidence_target.get("target_material_chain") if isinstance(evidence_target.get("target_material_chain"), dict) else {}


def _target_material_chain_evidence(material_chain: Any) -> dict[str, Any]:
    if not isinstance(material_chain, dict):
        return {}
    chains = material_chain.get("chains") if isinstance(material_chain.get("chains"), dict) else {}
    evidence: dict[str, Any] = {}
    for chain_name in ("metadata_chain", "media_info_chain", "screenshot_chain"):
        chain = chains.get(chain_name) if isinstance(chains.get(chain_name), dict) else {}
        if not chain:
            continue
        evidence[chain_name] = {
            "ready": chain.get("ready") if isinstance(chain.get("ready"), bool) else None,
            "missing": _string_list(chain.get("missing")),
            "next_actions": _string_list(chain.get("next_actions")),
        }
    return evidence


def _readiness_material_recovery_summary(resume_state: dict[str, Any]) -> dict[str, Any]:
    materials = resume_state.get("materials") if isinstance(resume_state.get("materials"), dict) else {}
    recovery_hints = materials.get("recovery_hints") if isinstance(materials.get("recovery_hints"), list) else []
    first_recovery_command = _first_material_recovery_command(recovery_hints)
    first_argv = _argv_list(first_recovery_command.get("argv")) or []
    command_coverage = _material_recovery_command_coverage(recovery_hints)
    completion_command = _material_recovery_completion_command(resume_state, recovery_hints, command_coverage)
    return {
        "present": bool(materials),
        "target_materials_missing": _string_list(materials.get("target_materials_missing")),
        "target_preparation_missing": _string_list(materials.get("target_preparation_missing")),
        "next_actions": _string_list(materials.get("next_actions")),
        "hint_count": len(recovery_hints),
        "keys": [str(hint.get("key")) for hint in recovery_hints if isinstance(hint, dict) and hint.get("key")],
        "required_flags": _material_recovery_required_flags(recovery_hints),
        "missing_flags": _material_recovery_missing_flags(recovery_hints),
        "existing_file_options": _material_recovery_existing_file_options(recovery_hints),
        "first_command": first_recovery_command.get("command"),
        "first_command_argv": first_argv,
        "command_coverage": command_coverage,
        "completion_command": completion_command.get("command"),
        "completion_command_argv": completion_command.get("argv"),
        "hints": recovery_hints,
    }


def _readiness_source_followup_summary(resume_state: dict[str, Any]) -> dict[str, Any]:
    followup = resume_state.get("source_followup") if isinstance(resume_state.get("source_followup"), dict) else {}
    torrent_evidence = followup.get("source_torrent_file_evidence") if isinstance(followup.get("source_torrent_file_evidence"), dict) else {}
    wait_query = followup.get("source_wait_query") if isinstance(followup.get("source_wait_query"), dict) else {}
    wait_retry = followup.get("wait_retry") if isinstance(followup.get("wait_retry"), dict) else {}
    return {
        "present": bool(followup),
        "ready": followup.get("ready") if isinstance(followup.get("ready"), bool) else None,
        "ready_for_source_seeding": followup.get("ready_for_source_seeding") if isinstance(followup.get("ready_for_source_seeding"), bool) else None,
        "missing": _string_list(followup.get("missing")),
        "blockers": _string_list(followup.get("blockers")),
        "next_actions": _string_list(followup.get("next_actions")),
        "source_torrent_hash": followup.get("source_torrent_hash"),
        "source_torrent_file": followup.get("source_torrent_file"),
        "source_torrent_file_evidence": torrent_evidence,
        "source_save_path": followup.get("source_save_path"),
        "source_qbit_category": followup.get("source_qbit_category"),
        "source_qbit_tags": followup.get("source_qbit_tags"),
        "source_paused": followup.get("source_paused") if isinstance(followup.get("source_paused"), bool) else None,
        "injected_torrent_hash": followup.get("injected_torrent_hash"),
        "injection_visible_in_client": followup.get("injection_visible_in_client") if isinstance(followup.get("injection_visible_in_client"), bool) else None,
        "injection_verified": followup.get("injection_verified") if isinstance(followup.get("injection_verified"), bool) else None,
        "source_wait_evidence": followup.get("source_wait_evidence") if isinstance(followup.get("source_wait_evidence"), bool) else None,
        "hash_consistent": followup.get("hash_consistent") if isinstance(followup.get("hash_consistent"), bool) else None,
        "source_wait_query": wait_query,
        "qbit_wait_mismatch": followup.get("qbit_wait_mismatch") if isinstance(followup.get("qbit_wait_mismatch"), bool) else None,
        "qbit_wait_mismatches": _string_list(followup.get("qbit_wait_mismatches")),
        "wait_retry": wait_retry,
        "gates": followup.get("gates") if isinstance(followup.get("gates"), dict) else {},
    }


def _first_bool_value(*values: Any) -> bool | None:
    for value in values:
        if isinstance(value, bool):
            return value
    return None


def _readiness_uploaded_followup_summary(resume_state: dict[str, Any]) -> dict[str, Any]:
    followup = resume_state.get("uploaded_followup") if isinstance(resume_state.get("uploaded_followup"), dict) else {}
    torrent_evidence = followup.get("uploaded_torrent_file_evidence") if isinstance(followup.get("uploaded_torrent_file_evidence"), dict) else {}
    wait_query = followup.get("uploaded_wait_query") if isinstance(followup.get("uploaded_wait_query"), dict) else {}
    wait_retry = followup.get("wait_retry") if isinstance(followup.get("wait_retry"), dict) else {}
    gates = followup.get("gates") if isinstance(followup.get("gates"), dict) else {}
    return {
        "present": bool(followup),
        "ready": followup.get("ready") if isinstance(followup.get("ready"), bool) else None,
        "ready_for_uploaded_seeding": followup.get("ready_for_uploaded_seeding") if isinstance(followup.get("ready_for_uploaded_seeding"), bool) else None,
        "missing": _string_list(followup.get("missing")),
        "blockers": _string_list(followup.get("blockers")),
        "next_actions": _string_list(followup.get("next_actions")),
        "uploaded": _first_bool_value(followup.get("uploaded"), gates.get("uploaded")),
        "downloaded": _first_bool_value(followup.get("downloaded"), gates.get("downloaded")),
        "injected": _first_bool_value(followup.get("injected"), gates.get("injected")),
        "uploaded_torrent_id": followup.get("uploaded_torrent_id"),
        "uploaded_torrent_hash": followup.get("uploaded_torrent_hash"),
        "injected_torrent_hash": followup.get("injected_torrent_hash"),
        "injection_visible_in_client": followup.get("injection_visible_in_client") if isinstance(followup.get("injection_visible_in_client"), bool) else None,
        "injection_verified": _first_bool_value(followup.get("injection_verified"), gates.get("injection_verified")),
        "uploaded_wait_evidence": _first_bool_value(followup.get("uploaded_wait_evidence"), gates.get("uploaded_wait_evidence")),
        "hash_consistent": followup.get("hash_consistent") if isinstance(followup.get("hash_consistent"), bool) else None,
        "duplicate_clean": followup.get("duplicate_clean") if isinstance(followup.get("duplicate_clean"), bool) else None,
        "rule_obligations_ready": followup.get("rule_obligations_ready") if isinstance(followup.get("rule_obligations_ready"), bool) else None,
        "uploaded_torrent_file": followup.get("uploaded_torrent_file"),
        "uploaded_torrent_file_evidence": torrent_evidence,
        "uploaded_save_path": followup.get("uploaded_save_path"),
        "uploaded_wait_query": wait_query,
        "qbit_wait_mismatch": followup.get("qbit_wait_mismatch") if isinstance(followup.get("qbit_wait_mismatch"), bool) else None,
        "qbit_wait_mismatches": _string_list(followup.get("qbit_wait_mismatches")),
        "wait_retry": wait_retry,
        "gates": gates,
    }


def _retorrent_execute_artifacts(pipeline_result: dict[str, Any], evidence: dict[str, Any] | None, closure: dict[str, Any] | None) -> dict[str, Any]:
    artifacts = pipeline_result.get("artifacts")
    merged = dict(artifacts) if isinstance(artifacts, dict) else {}
    evidence_source = evidence.get("source") if isinstance(evidence, dict) and isinstance(evidence.get("source"), dict) else {}
    closure_source = closure.get("source") if isinstance(closure, dict) and isinstance(closure.get("source"), dict) else {}
    evidence_target = evidence.get("target") if isinstance(evidence, dict) and isinstance(evidence.get("target"), dict) else {}
    closure_target = closure.get("target") if isinstance(closure, dict) and isinstance(closure.get("target"), dict) else {}
    summary = pipeline_result.get("summary") if isinstance(pipeline_result.get("summary"), dict) else {}
    summary_source = summary.get("source") if isinstance(summary.get("source"), dict) else {}
    summary_target = summary.get("target") if isinstance(summary.get("target"), dict) else {}
    for key in (
        "source_torrent_hash",
        "source_torrent_file",
        "source_torrent_file_evidence",
        "source_save_path",
        "source_qbit_category",
        "source_qbit_tags",
        "source_paused",
        "source_hash_consistent",
        "source_injected_torrent_hash",
        "source_injection_visible_in_client",
        "source_injection_verified",
        "source_wait_evidence",
    ):
        if key in merged and _artifact_value_present(merged.get(key)):
            continue
        source_key = _source_artifact_evidence_key(key)
        value = evidence_source.get(source_key) or closure_source.get(source_key) or summary_source.get(source_key)
        if _artifact_value_present(value):
            merged[key] = value
    if "source_injection_visible_in_client" not in merged:
        source_injection = evidence_source.get("qbit_closure", {}).get("injection") if isinstance(evidence_source.get("qbit_closure"), dict) else None
        if isinstance(source_injection, dict):
            merged["source_injection_visible_in_client"] = _injected_torrent_visible(source_injection)
    for key in (
        "uploaded_torrent_id",
        "uploaded_torrent_hash",
        "uploaded_torrent_file_evidence",
        "injected_torrent_hash",
        "injection_visible_in_client",
        "injection_verified",
        "uploaded_torrent_path",
        "uploaded_save_path",
        "uploaded_qbit_category",
        "uploaded_qbit_tags",
        "uploaded_paused",
        "uploaded_wait_evidence",
        "fresh_duplicate_check",
        "target_hash_consistent",
        "target_duplicate_clean",
        "target_rule_obligations",
        "target_preparation_audit",
        "target_payload_review",
        "target_material_chain",
        "target_preparation_ready",
        "target_preparation_missing",
    ):
        if key in merged and _artifact_value_present(merged.get(key)):
            continue
        target_key = _target_artifact_evidence_key(key)
        value = evidence_target.get(target_key) or closure_target.get(target_key) or summary_target.get(target_key)
        if _artifact_value_present(value):
            merged[key] = value
    preparation_audit = merged.get("target_preparation_audit")
    if "target_preparation_ready" not in merged and isinstance(preparation_audit, dict):
        merged["target_preparation_ready"] = bool(preparation_audit.get("ready"))
    if "target_preparation_missing" not in merged and isinstance(preparation_audit, dict):
        merged["target_preparation_missing"] = _string_list(preparation_audit.get("missing"))
    if "target_preflight_gates" not in merged and isinstance(preparation_audit, dict):
        merged["target_preflight_gates"] = _target_preflight_gates({"status": "ready" if preparation_audit.get("ready") else "blocked"}, preparation_audit)
    if "target_material_chain" not in merged:
        material_chain = _target_material_chain_summary(merged.get("target_payload_review"))
        if material_chain:
            merged["target_material_chain"] = material_chain
    if "injection_visible_in_client" not in merged:
        target_injection = evidence_target.get("qbit_closure", {}).get("injection") if isinstance(evidence_target.get("qbit_closure"), dict) else None
        if isinstance(target_injection, dict):
            merged["injection_visible_in_client"] = _injected_torrent_visible(target_injection)
    if not merged.get("uploaded_torrent_file") and merged.get("uploaded_torrent_path"):
        merged["uploaded_torrent_file"] = merged["uploaded_torrent_path"]
    return merged


def _retorrent_execute_resume_state(pipeline_result: dict[str, Any], artifacts: dict[str, Any], blockers: list[str], resume_commands: Any, preferred_stages: tuple[str, ...] = ()) -> dict[str, Any]:
    pipeline_resume = pipeline_result.get("resume_state") if isinstance(pipeline_result.get("resume_state"), dict) else {}
    complete = not blockers
    commands = resume_commands if isinstance(resume_commands, list) else []
    commands_by_stage = {str(command.get("stage")): str(command.get("command")) for command in commands if isinstance(command, dict)}
    next_stage = None if complete else pipeline_resume.get("next_stage")
    next_command = None if complete else pipeline_resume.get("next_command")
    next_command_argv = None if complete else _argv_list(pipeline_resume.get("next_command_argv"))
    preferred = None if complete else _resume_next_command_from_stages(preferred_stages, commands_by_stage)
    if not complete and preferred and preferred.get("stage") and preferred.get("stage") != next_stage:
        next_stage = preferred.get("stage")
        next_command = preferred.get("command")
        next_command_argv = _resume_state_next_command_argv(preferred, commands)
    elif not complete and not next_command:
        fallback = preferred or _resume_next_command(blockers, commands_by_stage)
        next_stage = fallback.get("stage")
        next_command = fallback.get("command")
        next_command_argv = _resume_state_next_command_argv(fallback, commands)
    elif not complete and next_command_argv is None:
        next_command_argv = _resume_state_next_command_argv({"stage": next_stage, "command": next_command}, commands)
    target_preflight = artifacts.get("target_preflight_gates") if isinstance(artifacts.get("target_preflight_gates"), dict) else {}
    target_preflight_torrent = target_preflight.get("torrent_file") if isinstance(target_preflight.get("torrent_file"), dict) else {}
    materials = pipeline_resume.get("materials") if isinstance(pipeline_resume.get("materials"), dict) else {}
    if not materials:
        materials = _run_summary_material_resume_state(pipeline_result, artifacts, commands)
    return {
        "complete": complete,
        "pipeline_complete": _retorrent_pipeline_complete(pipeline_result, pipeline_resume),
        "resume_available": bool(pipeline_resume.get("resume_available") or commands),
        "next_stage": next_stage,
        "next_command": next_command,
        "next_command_argv": next_command_argv,
        "available_stages": pipeline_resume.get("available_stages") or [str(command.get("stage")) for command in commands if isinstance(command, dict)],
        "artifacts": {
            "source_torrent_file": bool(artifacts.get("source_torrent_file")),
            "source_torrent_file_evidence": bool(artifacts.get("source_torrent_file_evidence")),
            "source_torrent_hash": bool(artifacts.get("source_torrent_hash")),
            "source_save_path": bool(artifacts.get("source_save_path")),
            "source_qbit_category": bool(artifacts.get("source_qbit_category")),
            "source_qbit_tags": bool(artifacts.get("source_qbit_tags")),
            "source_paused": "source_paused" in artifacts,
            "source_hash_consistent": bool(artifacts.get("source_hash_consistent")),
            "source_injected_torrent_hash": bool(artifacts.get("source_injected_torrent_hash")),
            "source_injection_visible_in_client": bool(artifacts.get("source_injection_visible_in_client")),
            "source_injection_verified": bool(artifacts.get("source_injection_verified")),
            "source_wait_evidence": bool(artifacts.get("source_wait_evidence")),
            "target_package_dir": bool(artifacts.get("target_package_dir")),
            "target_torrent_file": bool(artifacts.get("target_torrent_file")),
            "uploaded_torrent_id": bool(artifacts.get("uploaded_torrent_id")),
            "uploaded_torrent_file": bool(artifacts.get("uploaded_torrent_file")),
            "uploaded_torrent_file_evidence": bool(artifacts.get("uploaded_torrent_file_evidence")),
            "uploaded_torrent_hash": bool(artifacts.get("uploaded_torrent_hash")),
            "injected_torrent_hash": bool(artifacts.get("injected_torrent_hash")),
            "injection_visible_in_client": bool(artifacts.get("injection_visible_in_client")),
            "injection_verified": bool(artifacts.get("injection_verified")),
            "uploaded_save_path": bool(artifacts.get("uploaded_save_path")),
            "uploaded_qbit_category": bool(artifacts.get("uploaded_qbit_category")),
            "uploaded_qbit_tags": bool(artifacts.get("uploaded_qbit_tags")),
            "uploaded_paused": "uploaded_paused" in artifacts,
            "uploaded_wait_evidence": bool(artifacts.get("uploaded_wait_evidence")),
            "target_hash_consistent": bool(artifacts.get("target_hash_consistent")),
            "target_duplicate_clean": bool(artifacts.get("target_duplicate_clean")),
            "target_rule_obligations": _rule_obligations_artifact_ready(artifacts.get("target_rule_obligations")),
            "target_preparation_ready": bool(artifacts.get("target_preparation_ready")),
            "target_preflight_gates_ready": bool(target_preflight.get("ready")),
            "target_preflight_materials_ready": bool(target_preflight.get("materials_ready")),
            "target_preflight_description_ready": bool(target_preflight.get("description_ready")),
            "target_preflight_payload_ready": bool(target_preflight.get("payload_ready")),
            "target_preflight_torrent_safe": bool(target_preflight_torrent.get("mteam_safe")),
        },
        "materials": materials,
        "source_followup": _retorrent_source_followup_closure(pipeline_result, artifacts),
        "blockers": [str(blocker) for blocker in blockers],
    }


def _retorrent_source_followup_closure(pipeline_result: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    source_torrent_file_evidence = artifacts.get("source_torrent_file_artifact") if isinstance(artifacts.get("source_torrent_file_artifact"), dict) else {}
    if not source_torrent_file_evidence and isinstance(artifacts.get("source_torrent_file_evidence"), dict):
        source_torrent_file_evidence = artifacts["source_torrent_file_evidence"]
    source_wait_query = _retorrent_source_wait_query(pipeline_result, artifacts)
    qbit_wait_fields = _qbit_wait_summary_fields(pipeline_result)
    qbit_retry_hints = qbit_wait_fields.get("qbit_wait_retry_hints") if isinstance(qbit_wait_fields.get("qbit_wait_retry_hints"), dict) else {}
    source_retry_hint = qbit_retry_hints.get("source") if isinstance(qbit_retry_hints.get("source"), dict) else {}
    qbit_wait_mismatches = [mismatch for mismatch in _string_list(qbit_wait_fields.get("qbit_wait_mismatches")) if mismatch.startswith("source.")]
    source_torrent_hash = artifacts.get("source_torrent_hash") or source_torrent_file_evidence.get("torrent_hash") or source_torrent_file_evidence.get("hash") or source_torrent_file_evidence.get("infohash")
    checks = {
        "source_torrent_file": bool(artifacts.get("source_torrent_file") or source_torrent_file_evidence.get("path")),
        "source_torrent_file_evidence": _torrent_file_evidence_ready(source_torrent_file_evidence),
        "source_torrent_hash": bool(source_torrent_hash),
        "injected_torrent_hash": bool(artifacts.get("source_injected_torrent_hash")),
        "injection_visible_in_client": bool(artifacts.get("source_injection_visible_in_client")),
        "injection_verified": bool(artifacts.get("source_injection_verified")),
        "source_wait_evidence": bool(artifacts.get("source_wait_evidence")),
        "hash_consistent": bool(artifacts.get("source_hash_consistent")),
    }
    missing = [name for name, ok in checks.items() if not ok]
    ready = not missing
    return {
        "ready": ready,
        "ready_for_source_seeding": ready,
        "gates": checks,
        "blockers": _retorrent_source_followup_blockers(missing),
        "missing": missing,
        "source_torrent_hash": source_torrent_hash,
        "source_torrent_file": artifacts.get("source_torrent_file") or source_torrent_file_evidence.get("path"),
        "source_torrent_file_evidence": {
            "path": source_torrent_file_evidence.get("path"),
            "exists": source_torrent_file_evidence.get("exists"),
            "is_file": source_torrent_file_evidence.get("is_file"),
            "size_bytes": source_torrent_file_evidence.get("size_bytes"),
            "sha1": source_torrent_file_evidence.get("sha1"),
            "torrent_hash": source_torrent_file_evidence.get("torrent_hash") or source_torrent_file_evidence.get("hash") or source_torrent_file_evidence.get("infohash"),
            "metadata_readable": source_torrent_file_evidence.get("metadata_readable"),
            "reused": source_torrent_file_evidence.get("reused"),
        },
        "source_save_path": artifacts.get("source_save_path"),
        "source_qbit_category": artifacts.get("source_qbit_category"),
        "source_qbit_tags": artifacts.get("source_qbit_tags"),
        "source_paused": artifacts.get("source_paused") if isinstance(artifacts.get("source_paused"), bool) else None,
        "injected_torrent_hash": artifacts.get("source_injected_torrent_hash"),
        "injection_visible_in_client": artifacts.get("source_injection_visible_in_client") if isinstance(artifacts.get("source_injection_visible_in_client"), bool) else None,
        "injection_verified": artifacts.get("source_injection_verified") if isinstance(artifacts.get("source_injection_verified"), bool) else None,
        "source_wait_evidence": artifacts.get("source_wait_evidence") if isinstance(artifacts.get("source_wait_evidence"), bool) else None,
        "hash_consistent": artifacts.get("source_hash_consistent") if isinstance(artifacts.get("source_hash_consistent"), bool) else None,
        "source_wait_query": source_wait_query,
        "qbit_wait_mismatch": bool(qbit_wait_mismatches),
        "qbit_wait_mismatches": qbit_wait_mismatches,
        "wait_retry": source_retry_hint if source_retry_hint else None,
        "next_actions": _retorrent_source_followup_next_actions(missing),
    }


def _torrent_file_evidence_ready(evidence: dict[str, Any]) -> bool:
    return bool(evidence.get("path") and evidence.get("exists") is True and evidence.get("is_file") is True and evidence.get("metadata_readable") is True)


def _retorrent_source_wait_query(pipeline_result: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    summary = pipeline_result.get("summary") if isinstance(pipeline_result.get("summary"), dict) else {}
    source_summary = summary.get("source") if isinstance(summary.get("source"), dict) else {}
    source_wait = source_summary.get("source_wait") if isinstance(source_summary.get("source_wait"), dict) else {}
    query = source_wait.get("query") if isinstance(source_wait.get("query"), dict) else {}
    if query:
        return query
    wait_options = pipeline_result.get("wait_options") if isinstance(pipeline_result.get("wait_options"), dict) else {}
    source_wait_options = wait_options.get("source") if isinstance(wait_options.get("source"), dict) else {}
    query = {}
    if artifacts.get("source_injected_torrent_hash") or artifacts.get("source_torrent_hash"):
        query["torrent_hash"] = artifacts.get("source_injected_torrent_hash") or artifacts.get("source_torrent_hash")
    if artifacts.get("source_save_path"):
        query["save_path"] = artifacts.get("source_save_path")
    if source_wait_options.get("timeout") is not None:
        query["timeout"] = source_wait_options.get("timeout")
    if source_wait_options.get("interval") is not None:
        query["interval"] = source_wait_options.get("interval")
    return query


def _retorrent_source_followup_blockers(missing: list[str]) -> list[str]:
    labels = {
        "source_torrent_file": "source torrent file has not been downloaded or provided",
        "source_torrent_file_evidence": "source torrent file evidence is incomplete or unreadable",
        "source_torrent_hash": "source torrent hash evidence is missing",
        "injected_torrent_hash": "source torrent has not been injected into qBittorrent",
        "injection_visible_in_client": "source torrent is not visible in qBittorrent after injection",
        "injection_verified": "source torrent injection is not verified in qBittorrent",
        "source_wait_evidence": "qBittorrent has not reported the source torrent as complete",
        "hash_consistent": "source torrent hash and qBittorrent injected hash are not verified consistent",
    }
    return [labels.get(item, item) for item in missing]


def _retorrent_source_followup_next_actions(missing: list[str]) -> list[str]:
    actions: list[str] = []
    if any(item in missing for item in ("source_torrent_file", "source_torrent_file_evidence", "source_torrent_hash")):
        actions.append("Download or provide the source torrent, then resume source torrent injection.")
    if any(item in missing for item in ("injected_torrent_hash", "injection_visible_in_client", "injection_verified")):
        actions.append("Inject the source torrent into qBittorrent with the correct save path.")
    if "source_wait_evidence" in missing:
        actions.append("Wait for qBittorrent to report the source torrent as matched and complete.")
    if "hash_consistent" in missing:
        actions.append("Verify the source torrent hash matches the injected qBittorrent task.")
    return actions


def _resume_command_audit_fields(resume_commands: Any, next_command: Any, next_command_argv: Any) -> dict[str, Any]:
    commands = resume_commands if isinstance(resume_commands, list) else []
    candidate_commands = _summary_candidate_commands({"resume_commands": commands})
    first_runnable_command = _first_runnable_candidate_command(candidate_commands)
    next_argv = _summary_next_command_raw_argv(next_command_argv) if next_command_argv else _summary_next_command_raw_argv(str(next_command)) if next_command else None
    next_metadata = _summary_next_command_metadata(next_argv)
    return {
        "next_command_subcommand": next_metadata["subcommand"],
        "next_command_ready": bool(next_command) and not bool(next_metadata["placeholder"]),
        "next_command_placeholder": bool(next_metadata["placeholder"]),
        "next_command_run_allowed": bool(next_command and next_metadata["run_allowed"]),
        "next_command_run_blocker": next_metadata["run_blocker"],
        "candidate_commands": candidate_commands,
        "candidate_command_count": len(candidate_commands),
        "runnable_command_count": sum(1 for command in candidate_commands if isinstance(command, dict) and command.get("run_allowed") is True),
        "first_runnable_stage": first_runnable_command.get("stage"),
        "first_runnable_command": first_runnable_command.get("command"),
        "first_runnable_command_argv": first_runnable_command.get("argv"),
        "first_runnable_command_source": first_runnable_command.get("source"),
        "first_runnable_command_subcommand": first_runnable_command.get("subcommand"),
        **_rejected_candidate_command_summary(candidate_commands),
    }


def _retorrent_automation_fields(
    *,
    status: str,
    blockers: list[str],
    qbit_wait_mismatch: bool,
    qbit_wait_mismatches: list[str],
    qbit_wait_retry_hints: dict[str, Any],
    resume_state: dict[str, Any],
    resume_command_audit: dict[str, Any],
) -> dict[str, Any]:
    next_command = resume_state.get("next_command")
    payload = {
        "status": "ok" if status == "complete" else "blocked",
        "blockers": blockers,
        "qbit_wait_mismatch": qbit_wait_mismatch,
        "qbit_wait_mismatches": qbit_wait_mismatches,
        "qbit_wait_retry_hints": qbit_wait_retry_hints,
        "next_stage": resume_state.get("next_stage"),
    }
    if status == "complete":
        automation_action = "complete"
    elif qbit_wait_mismatch:
        automation_action = "resolve_qbit_wait_mismatch"
    elif resume_command_audit.get("next_command_placeholder"):
        automation_action = "fill_command_placeholders"
    elif next_command and resume_command_audit.get("next_command_run_allowed"):
        automation_action = "run_next_command"
    elif next_command and resume_command_audit.get("next_command_ready"):
        automation_action = "unsupported_next_command"
    else:
        automation_action = "resolve_blockers"
    return {
        "automation_action": automation_action,
        "automation_reason": _summary_automation_reason(payload, automation_action, blockers, next_command_run_blocker=resume_command_audit.get("next_command_run_blocker")),
        "automation_exit_code": 0 if status == "complete" else 1,
        "should_execute_next_command": automation_action == "run_next_command",
    }


def _retorrent_pipeline_complete(pipeline_result: dict[str, Any], pipeline_resume: dict[str, Any]) -> bool:
    if "complete" in pipeline_resume:
        return pipeline_resume.get("complete") is True
    if "complete" in pipeline_result:
        return pipeline_result.get("complete") is True
    closure = pipeline_result.get("closure") if isinstance(pipeline_result.get("closure"), dict) else None
    return bool(isinstance(closure, dict) and closure.get("complete") is True)


def _retorrent_execute_blockers(
    pipeline_result: dict[str, Any],
    closure: dict[str, Any] | None,
    ready: bool,
    artifacts: dict[str, Any] | None = None,
    material_diagnostics: dict[str, Any] | None = None,
    target_preflight_diagnostics: dict[str, Any] | None = None,
) -> list[str]:
    blockers: list[str] = []
    if closure is None:
        blockers.append("pipeline did not return a closure summary.")
    else:
        closure_blockers = closure.get("blockers")
        if isinstance(closure_blockers, list):
            blockers.extend(str(blocker) for blocker in closure_blockers)
        if closure.get("complete") is not True and not blockers:
            blockers.append("retorrent closure did not complete.")
    _extend_unique_string(blockers, _string_list(pipeline_result.get("blockers")))
    summary = pipeline_result.get("summary") if isinstance(pipeline_result.get("summary"), dict) else {}
    _extend_unique_string(blockers, _string_list(summary.get("blockers")))
    _extend_unique_string(blockers, _summary_closure_audit_status(pipeline_result)["missing_closure_audit"])
    _extend_unique_string(blockers, _qbit_wait_mismatch_blockers(_summary_check_diagnostics(pipeline_result)))
    _extend_unique_string(blockers, _retorrent_execute_preflight_material_blockers(material_diagnostics, target_preflight_diagnostics))
    if not ready:
        blockers.append("pipeline did not report ready.")
    if ready and closure is not None and closure.get("complete") is True:
        artifact_values = artifacts if isinstance(artifacts, dict) else {}
        if artifact_values.get("source_wait_evidence") is not True:
            blockers.append("source.wait_evidence")
        if _source_injection_audit_required(pipeline_result, closure):
            if artifact_values.get("source_torrent_file_evidence") is not True:
                blockers.append("source.torrent_file_evidence")
            if artifact_values.get("source_torrent_hash") is None:
                blockers.append("source.torrent_hash")
            if artifact_values.get("source_injected_torrent_hash") is None:
                blockers.append("source.injected_torrent_hash")
            if artifact_values.get("source_injection_visible_in_client") is not True:
                blockers.append("source.injection_visible_in_client")
            if artifact_values.get("source_injection_verified") is not True:
                blockers.append("source.injection_verified")
        if artifact_values.get("uploaded_wait_evidence") is not True:
            blockers.append("target.uploaded_wait_evidence")
        if artifact_values.get("uploaded_torrent_file_evidence") is not True:
            blockers.append("target.uploaded_torrent_file_evidence")
        if artifact_values.get("uploaded_torrent_hash") is None:
            blockers.append("target.uploaded_torrent_hash")
        if artifact_values.get("injected_torrent_hash") is None:
            blockers.append("target.injected_torrent_hash")
        if artifact_values.get("injection_visible_in_client") is not True:
            blockers.append("target.injection_visible_in_client")
        if artifact_values.get("injection_verified") is not True:
            blockers.append("target.injection_verified")
    if pipeline_result.get("status") not in {None, "ok", "complete"}:
        blockers.append(f"pipeline status is {pipeline_result.get('status')}.")
    return blockers


def _retorrent_execute_preflight_material_blockers(material_diagnostics: dict[str, Any] | None, target_preflight_diagnostics: dict[str, Any] | None) -> list[str]:
    blockers: list[str] = []
    material = material_diagnostics if isinstance(material_diagnostics, dict) else {}
    if material.get("present") is True and material.get("ready_for_mteam_upload") is False:
        material_blockers = _string_list(material.get("upload_material_blockers"))
        if material_blockers:
            _extend_unique_string(blockers, material_blockers)
        else:
            _append_unique_string(blockers, "materials are not ready for MTEAM upload")
    preflight = target_preflight_diagnostics if isinstance(target_preflight_diagnostics, dict) else {}
    if preflight.get("present") and preflight.get("ready") is False:
        for blocker in _string_list(preflight.get("blockers")):
            _append_unique_string(blockers, f"target preflight: {blocker}")
    return blockers


def _source_artifact_evidence_key(artifact_key: str) -> str:
    return {
        "source_torrent_hash": "torrent_hash",
        "source_torrent_file": "source_torrent_path",
        "source_torrent_file_evidence": "torrent_file_evidence",
        "source_qbit_category": "source_qbit_category",
        "source_qbit_tags": "source_qbit_tags",
        "source_paused": "source_paused",
        "source_hash_consistent": "hash_consistent",
        "source_injected_torrent_hash": "injected_torrent_hash",
        "source_injection_visible_in_client": "source_injection_visible_in_client",
        "source_injection_verified": "injection_verified",
    }.get(artifact_key, artifact_key)


def _source_injection_audit_required(pipeline_result: dict[str, Any], closure: dict[str, Any] | None) -> bool:
    evidence = pipeline_result.get("evidence") if isinstance(pipeline_result.get("evidence"), dict) else {}
    evidence_source = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
    mode = evidence_source.get("mode")
    if mode in {"downloaded", "resumed_torrent"}:
        return True
    closure_source = closure.get("source") if isinstance(closure, dict) and isinstance(closure.get("source"), dict) else {}
    return bool(closure_source.get("downloaded") or closure_source.get("injected") or closure_source.get("source_torrent_reused"))


def _target_artifact_evidence_key(artifact_key: str) -> str:
    return {
        "uploaded_qbit_category": "uploaded_qbit_category",
        "uploaded_qbit_tags": "uploaded_qbit_tags",
        "uploaded_paused": "uploaded_paused",
        "target_hash_consistent": "hash_consistent",
        "target_duplicate_clean": "duplicate_clean",
        "target_rule_obligations": "rule_obligations",
        "target_preparation_audit": "preparation_audit",
        "target_payload_review": "payload_review",
        "target_preparation_missing": "preparation_missing",
        "uploaded_torrent_file_evidence": "uploaded_torrent_file_evidence",
    }.get(artifact_key, artifact_key)


def _artifact_value_present(value: Any) -> bool:
    return value is not None and value != ""


def _retorrent_execute_next_actions(pipeline_result: dict[str, Any], blockers: list[str]) -> list[str]:
    if not blockers:
        return ["Retorrent closure is complete; verify the target tracker page and qBittorrent seeding state."]
    actions: list[str] = []
    for action in _retorrent_execute_qbit_mismatch_actions(pipeline_result):
        _append_unique_string(actions, action)
    for action in _target_upload_payload_recovery_next_actions(pipeline_result.get("target_upload_payload_recovery")):
        _append_unique_string(actions, action)
    for blocker in blockers:
        _append_unique_string(actions, _retorrent_execute_blocker_next_action(str(blocker)))
        if str(blocker) in {"target_preparation_ready", "target.preparation_ready", "target.materials_ready"}:
            for action in _target_preparation_missing_next_actions(_target_preparation_missing_from_pipeline_result(pipeline_result)):
                _append_unique_string(actions, action)
    pipeline_actions = pipeline_result.get("next_actions")
    if isinstance(pipeline_actions, list) and pipeline_actions:
        for action in pipeline_actions:
            action_text = str(action)
            if action_text.startswith("Retorrent closure is complete;"):
                continue
            _append_unique_string(actions, action_text)
    return actions or [f"Fix {blocker}" for blocker in blockers]


def _target_upload_payload_recovery_next_actions(recovery: Any) -> list[str]:
    if not isinstance(recovery, dict):
        return []
    return _string_list(recovery.get("next_actions"))


def _merge_target_upload_payload_recovery_next_actions(next_actions: Any, recovery: Any) -> list[str]:
    actions = _string_list(next_actions)
    for action in _target_upload_payload_recovery_next_actions(recovery):
        _append_unique_string(actions, action)
    return actions


def _retorrent_execute_qbit_mismatch_actions(pipeline_result: dict[str, Any]) -> list[str]:
    diagnostics = _summary_qbit_wait_diagnostics(pipeline_result)
    mismatches = set(_summary_qbit_wait_mismatches(diagnostics))
    retry_hints = _summary_qbit_wait_retry_hints(diagnostics)
    actions: list[str] = []
    if any(mismatch.startswith("source.") for mismatch in mismatches):
        actions.append(_qbit_wait_retry_action("source", retry_hints.get("source")))
    if any(mismatch.startswith("uploaded.") for mismatch in mismatches):
        actions.append(_qbit_wait_retry_action("uploaded", retry_hints.get("uploaded")))
    return actions


def _qbit_wait_retry_action(scope: str, hint: Any) -> str:
    target = "source" if scope == "source" else "uploaded"
    fallback = (
        "Resolve the source qBittorrent wait mismatch before rerunning: inspect qbit_wait_diagnostics for observed hashes/paths and retry with the matching source torrent or content path."
        if target == "source"
        else "Resolve the uploaded qBittorrent wait mismatch before rerunning: inspect qbit_wait_diagnostics for observed hashes/paths and retry with the downloaded target-site torrent and matching uploaded save path."
    )
    if not isinstance(hint, dict) or not hint.get("retry_recommended"):
        return fallback
    details = []
    if hint.get("suggested_torrent_hash"):
        details.append(f"hash={hint['suggested_torrent_hash']}")
    if hint.get("suggested_content_path"):
        details.append(f"path={hint['suggested_content_path']}")
    if hint.get("suggested_save_path"):
        details.append(f"save_path={hint['suggested_save_path']}")
    suffix = f" Suggested retry values from qBittorrent: {', '.join(details)}." if details else ""
    return f"{fallback}{suffix}"


def _retorrent_execute_blocker_next_action(blocker: str) -> str:
    if blocker == "source.wait_evidence":
        return "Re-run the source qBittorrent completion wait with --wait-complete, or provide a verified completed --path before target upload."
    if blocker == "source.torrent_file_evidence":
        return "Re-run the source side with --download-source or provide --source-torrent-file so the downloaded source .torrent has exists/size/sha1/infohash evidence."
    if blocker in {"source.torrent_hash", "source.injected_torrent_hash", "source.injection_visible_in_client", "source.injection_verified"}:
        return "Re-run the source side with --download-source or --source-torrent-file plus --inject-source and --wait-complete so qBittorrent source injection evidence is recorded."
    if blocker == "target.uploaded_wait_evidence":
        return "Re-run the uploaded MTEAM torrent follow-up with --inject-uploaded-torrent and --wait-uploaded-complete until qBittorrent reports matched seeding evidence."
    if blocker == "target.uploaded_torrent_file_evidence":
        return "Re-run the uploaded MTEAM torrent follow-up with --download-uploaded-torrent so the generated target .torrent has exists/size/sha1/infohash evidence."
    if blocker in {"target.uploaded_torrent_hash", "target.injected_torrent_hash", "target.injection_visible_in_client", "target.injection_verified"}:
        return "Re-run the uploaded MTEAM torrent follow-up with --inject-uploaded-torrent so qBittorrent visibility and exact uploaded torrent hash evidence are recorded."
    if blocker == "target.injected":
        return "Inject the generated target torrent into qBittorrent with --inject-uploaded-torrent and a valid uploaded save path."
    if blocker == "target.seeding":
        return "Verify the uploaded target torrent is active in qBittorrent, then re-run with --wait-uploaded-complete."
    if blocker == "target.uploaded":
        return "Resume the MTEAM target upload stage after duplicate and rule gates are ready."
    if blocker in {"target_preparation_ready", "target.preparation_ready", "target.materials_ready"}:
        return "Regenerate the MTEAM target package after completing IMDb/TMDb/Douban metadata, PTGen/Douban description, MediaInfo/BDInfo, screenshot, and image-host materials."
    if blocker == "pipeline did not report ready.":
        return "Inspect the pipeline blockers and resume from the first incomplete stage."
    if blocker.startswith("pipeline status is "):
        return "Inspect the pipeline status and stage blockers before retrying retorrent --execute."
    return f"Fix {blocker}"


def _target_preparation_missing_from_pipeline_result(pipeline_result: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    artifacts = pipeline_result.get("artifacts") if isinstance(pipeline_result.get("artifacts"), dict) else {}
    _extend_unique_string(missing, _string_list(artifacts.get("target_preparation_missing")))
    _extend_unique_string(missing, _string_list(artifacts.get("target_materials_missing")))
    closure_review = pipeline_result.get("closure_review") if isinstance(pipeline_result.get("closure_review"), dict) else {}
    target_review = closure_review.get("target") if isinstance(closure_review.get("target"), dict) else {}
    _extend_unique_string(missing, _string_list(target_review.get("preparation_missing")))
    description = target_review.get("description") if isinstance(target_review.get("description"), dict) else {}
    _extend_unique_string(missing, _string_list(description.get("missing")))
    evidence = pipeline_result.get("evidence") if isinstance(pipeline_result.get("evidence"), dict) else {}
    evidence_target = evidence.get("target") if isinstance(evidence.get("target"), dict) else {}
    preparation_audit = evidence_target.get("preparation_audit") if isinstance(evidence_target.get("preparation_audit"), dict) else {}
    _extend_unique_string(missing, _string_list(preparation_audit.get("missing")))
    materials = evidence_target.get("materials") if isinstance(evidence_target.get("materials"), dict) else {}
    _extend_unique_string(missing, _string_list(materials.get("missing")))
    return missing


def _target_preparation_missing_next_actions(missing: list[str]) -> list[str]:
    actions: list[str] = []
    for item in missing:
        action = _target_preparation_missing_next_action(item)
        if action:
            _append_unique_string(actions, action)
    return actions


def _target_preparation_missing_next_action(missing: str) -> str | None:
    normalized = _target_preparation_missing_key(missing)
    if normalized in {"description.ptgen_description", "metadata.ptgen_description"}:
        return "Fetch PTGen/Douban description with --fetch-ptgen or supply metadata containing ptgen_description, then rerun resume-target-package."
    if normalized in {"metadata.imdb", "metadata.imdb_id", "description.external_ids.imdb", "payload.imdb"}:
        return "Fetch IMDb metadata with --enrich-metadata or supply it with --metadata-file/--imdb-id, then rerun resume-target-package."
    if normalized in {"metadata.tmdb", "metadata.tmdb_id", "description.external_ids.tmdb"}:
        return "Fetch TMDb metadata with --enrich-metadata or supply it with --metadata-file/--tmdb-id, then rerun resume-target-package."
    if normalized in {"metadata.douban", "metadata.douban_id", "metadata.douban_url", "description.external_ids.douban", "payload.douban"}:
        return "Fetch Douban metadata with --fetch-ptgen or supply it with --metadata-file/--douban-id/--douban-url, then rerun resume-target-package."
    if normalized.startswith("metadata.") or normalized.startswith("description.external_ids"):
        return "Fetch or supply IMDb/TMDb/Douban metadata with --enrich-metadata, --fetch-ptgen, --metadata-file, --imdb-id, --tmdb-id, or --douban-id, then rerun resume-target-package."
    if normalized in {"assets.mediainfo_or_bdinfo", "description.mediainfo_or_bdinfo", "payload.mediainfo"}:
        return "Generate or provide MediaInfo/BDInfo with --generate-mediainfo, --mediainfo-file, --generate-bdinfo, or --bdinfo-file, then rerun resume-target-package."
    if normalized == "assets.bdinfo_for_disc":
        return "Provide BDInfo for BDMV disc content with --bdinfo-file or --generate-bdinfo, then rerun resume-target-package."
    if normalized == "assets.screenshots":
        return "Generate or provide screenshots with --generate-screenshots or --screenshot-file, then rerun resume-target-package."
    if normalized == "description.screenshot_bbcode":
        return "Regenerate the MTEAM description after screenshot and image-host materials are ready."
    if normalized in {"assets.image_host_uploads", "description.screenshot_coverage"}:
        return "Upload screenshots to an image host with --upload-screenshots/--image-host or provide --image-host-file, then rerun resume-target-package."
    if normalized in {"description.content", "description.regenerate"}:
        return "Regenerate the MTEAM description after metadata, MediaInfo/BDInfo, screenshot, and image-host materials are ready."
    return None


def _target_preparation_recovery_hints(missing: list[str]) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in missing:
        hint = _target_preparation_recovery_hint(item)
        if not hint or str(hint["key"]) in seen:
            continue
        seen.add(str(hint["key"]))
        hints.append(hint)
    return hints


def _attach_material_recovery_resume_commands(hints: list[dict[str, Any]], resume_commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not hints:
        return []
    command_entry = _resume_command_entry_by_stage(resume_commands, "resume-target-package")
    command = command_entry.get("command") if command_entry else None
    argv = command_entry.get("argv") if command_entry and isinstance(command_entry.get("argv"), list) else []
    enriched: list[dict[str, Any]] = []
    for hint in hints:
        command_flags = _material_recovery_action_flags(hint)
        missing_command_flags = [flag for flag in command_flags if flag not in argv]
        command_covers_hint = bool(command_entry) and not missing_command_flags
        enriched.append(
            {
                **hint,
                "required_command_flags": command_flags,
                "missing_command_flags": missing_command_flags if command_entry else command_flags,
                "resume_command_available": command_covers_hint,
                "resume_command_stage": command_entry.get("stage") if command_entry else None,
                "resume_command": command if command_covers_hint else None,
                "resume_command_argv": argv if command_covers_hint else [],
            }
        )
    return enriched


def _material_recovery_action_flags(hint: dict[str, Any]) -> list[str]:
    value_options = {"--screenshot-count", "--image-host"}
    return [flag for flag in _string_list(hint.get("command_flags")) if flag not in value_options]


def _resume_command_entry_by_stage(resume_commands: list[dict[str, Any]], stage: str) -> dict[str, Any] | None:
    for command in resume_commands:
        if isinstance(command, dict) and command.get("stage") == stage:
            return command
    return None


def _target_preparation_recovery_hint(missing: str) -> dict[str, Any] | None:
    normalized = _target_preparation_missing_key(missing)
    if normalized in {"description.ptgen_description", "metadata.ptgen_description"}:
        return _material_recovery_hint(
            "metadata.ptgen_description",
            "Fetch PTGen/Douban description before regenerating the MTEAM package.",
            ["--enrich-metadata", "--fetch-ptgen"],
            ["--metadata-file"],
        )
    if normalized in {"metadata.imdb", "metadata.imdb_id", "description.external_ids.imdb", "payload.imdb"}:
        return _material_recovery_hint(
            "metadata.imdb_id",
            "Fetch or supply IMDb metadata before regenerating the MTEAM package.",
            ["--enrich-metadata"],
            ["--metadata-file", "--imdb-id"],
        )
    if normalized in {"metadata.tmdb", "metadata.tmdb_id", "description.external_ids.tmdb"}:
        return _material_recovery_hint(
            "metadata.tmdb_id",
            "Fetch or supply TMDb metadata before regenerating the MTEAM package.",
            ["--enrich-metadata"],
            ["--metadata-file", "--tmdb-id"],
        )
    if normalized in {"metadata.douban", "metadata.douban_id", "metadata.douban_url", "description.external_ids.douban", "payload.douban"}:
        return _material_recovery_hint(
            "metadata.douban",
            "Fetch or supply Douban metadata before regenerating the MTEAM package.",
            ["--enrich-metadata", "--fetch-ptgen"],
            ["--metadata-file", "--douban-id", "--douban-url"],
        )
    if normalized.startswith("description.external_ids"):
        return _material_recovery_hint(
            "metadata.external_ids",
            "Fetch or supply IMDb/TMDb/Douban metadata before regenerating the MTEAM package.",
            ["--enrich-metadata", "--fetch-ptgen"],
            ["--metadata-file", "--imdb-id", "--tmdb-id", "--douban-id", "--douban-url"],
        )
    if normalized.startswith("metadata."):
        return _material_recovery_hint(
            "metadata.external_ids",
            "Fetch or supply IMDb/TMDb/Douban metadata before regenerating the MTEAM package.",
            ["--enrich-metadata"],
            ["--metadata-file", "--imdb-id", "--tmdb-id", "--douban-id", "--douban-url"],
        )
    if normalized in {"assets.mediainfo_or_bdinfo", "description.mediainfo_or_bdinfo", "payload.mediainfo"}:
        return _material_recovery_hint(
            "assets.mediainfo_or_bdinfo",
            "Generate or provide MediaInfo/BDInfo before regenerating the MTEAM package.",
            ["--generate-mediainfo"],
            ["--mediainfo-file", "--bdinfo-file"],
        )
    if normalized == "assets.bdinfo_for_disc":
        return _material_recovery_hint(
            "assets.bdinfo_for_disc",
            "Generate or provide BDInfo for BDMV content before regenerating the MTEAM package.",
            ["--generate-bdinfo"],
            ["--bdinfo-file"],
        )
    if normalized == "assets.screenshots":
        return _material_recovery_hint(
            "assets.screenshots",
            "Generate or provide screenshots before regenerating the MTEAM package.",
            ["--generate-screenshots", "--screenshot-count"],
            ["--screenshot-file"],
        )
    if normalized == "description.screenshot_bbcode":
        return _material_recovery_hint(
            "description.screenshot_bbcode",
            "Regenerate the MTEAM description after screenshot and image-host materials are ready.",
            ["--prepare-target"],
            [],
        )
    if normalized in {"assets.image_host_uploads", "description.screenshot_coverage"}:
        return _material_recovery_hint(
            "assets.image_host_uploads",
            "Upload screenshots to an image host or provide existing image-host upload evidence before regenerating the MTEAM package.",
            ["--upload-screenshots", "--image-host"],
            ["--image-host-file"],
        )
    if normalized in {"description.content", "description.regenerate"}:
        return _material_recovery_hint(
            "description.content",
            "Regenerate the MTEAM description after metadata and media materials are ready.",
            ["--prepare-target"],
            [],
        )
    return None


def _target_preparation_missing_key(missing: str) -> str:
    key = str(missing).split(":", 1)[0].strip()
    return key.removeprefix("target.materials.").removeprefix("materials.")


def _material_recovery_hint(key: str, reason: str, command_flags: list[str], existing_file_options: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "resume_stage": "resume-target-package",
        "reason": reason,
        "command_flags": command_flags,
        "existing_file_options": existing_file_options,
    }


def _pipeline_args_from_retorrent(args: argparse.Namespace) -> argparse.Namespace:
    package_upload_resume = bool(args.package_dir and (args.uploaded_torrent_file or args.uploaded_torrent_id))
    needs_source_download = not bool(args.content_path or args.source_torrent_file or package_upload_resume)
    needs_source_injection = not bool(args.content_path or package_upload_resume)
    target_execute = not bool(args.uploaded_torrent_file or args.uploaded_torrent_id)
    needs_target_torrent = bool(target_execute and not (args.uploaded_torrent_file or args.uploaded_torrent_id))
    enrich_metadata = _retorrent_execute_default(args.enrich_metadata, True)
    fetch_ptgen = _retorrent_execute_default(args.fetch_ptgen, True)
    generate_bdinfo = _retorrent_execute_default(args.generate_bdinfo, not bool(args.bdinfo_file))
    generate_mediainfo = _retorrent_execute_default(args.generate_mediainfo, not bool(args.mediainfo_file or args.bdinfo_file))
    generate_screenshots = _retorrent_execute_default(args.generate_screenshots, not bool(getattr(args, "screenshot_file", []) or []))
    upload_screenshots = _retorrent_execute_default(args.upload_screenshots, not bool(args.image_host_file))
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
        enrich_metadata=enrich_metadata or fetch_ptgen,
        fetch_ptgen=fetch_ptgen,
        metadata_file=args.metadata_file,
        imdb_id=args.imdb_id,
        tmdb_id=args.tmdb_id,
        douban_id=args.douban_id,
        douban_url=args.douban_url,
        prepare_target=not bool(args.package_dir),
        package_dir=args.package_dir,
        target_output_dir=args.target_output_dir,
        mediainfo_file=args.mediainfo_file,
        bdinfo_file=args.bdinfo_file,
        generate_bdinfo=generate_bdinfo,
        bdinfo_playlist=args.bdinfo_playlist,
        generate_mediainfo=generate_mediainfo,
        generate_screenshots=generate_screenshots,
        screenshot_count=args.screenshot_count,
        screenshot_file=list(getattr(args, "screenshot_file", []) or []),
        upload_screenshots=upload_screenshots,
        image_host=args.image_host,
        image_host_file=args.image_host_file,
        check_dupes=not bool(args.package_dir and (args.uploaded_torrent_file or args.uploaded_torrent_id)),
        accept_rules=args.accept_rules,
        upload_target=True,
        target_torrent_file=args.target_torrent_file,
        export_target_torrent=bool(needs_target_torrent and (args.export_target_torrent or not args.target_torrent_file)),
        target_torrent_output_dir=args.target_torrent_output_dir,
        sanitize_target_torrent=bool(needs_target_torrent and args.sanitize_target_torrent),
        target_execute=target_execute,
        confirm_upload=args.confirm_upload,
        write_payload=args.write_payload,
        download_uploaded_torrent=True,
        uploaded_output_dir=args.uploaded_output_dir or "./tmp/uploaded",
        uploaded_torrent_id=args.uploaded_torrent_id,
        uploaded_torrent_file=args.uploaded_torrent_file,
        inject_uploaded_torrent=True,
        uploaded_save_path=args.uploaded_save_path or args.content_path or args.save_path,
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


def _retorrent_execute_default(value: bool | None, default: bool) -> bool:
    return default if value is None else bool(value)


def build_sites_payload() -> dict[str, Any]:
    sites = sorted(CHINESE_PT_TRACKERS)
    source_info_trackers = sorted((set(SOURCE_TRACKER_CLASSES) | set(GENERIC_DETAILS_BASE_URLS) | set(MTEAM_API_TRACKERS)) & set(CHINESE_PT_TRACKERS))
    source_download_trackers = sorted(
        (set(NEXUS_DOWNLOAD_BASE_URLS) | set(DIRECT_DOWNLOAD_TRACKER_CLASSES) | set(TTG_DOWNLOAD_BASE_URLS) | set(COOKIE_DOWNLOAD_URLS) | set(MTEAM_API_TRACKERS))
        & set(CHINESE_PT_TRACKERS)
    )
    mteam_flow_sources = sorted(MTEAM_SOURCE_FLOW_TRACKERS & set(CHINESE_PT_TRACKERS))
    full_live_sources = sorted(set(source_download_trackers) & set(mteam_flow_sources))
    target_upload_trackers = ["MTEAM"] if "MTEAM" in CHINESE_PT_TRACKERS else []
    capabilities = {
        tracker: {
            "source_info": tracker in source_info_trackers,
            "source_info_adapter": source_info_adapter(tracker),
            "source_download": tracker in source_download_trackers,
            "source_download_adapter": source_download_adapter(tracker),
            "credential_requirements": source_credential_requirements(tracker),
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
        payload = {**payload, "summary_file": summary_file, "automation_handoff": _summary_automation_handoff(summary_file)}
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
    if not args.wait_uploaded_complete:
        blockers.append("--wait-uploaded-complete is required with target-upload --execute for full live retorrent closure.")
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
    if args.inject_uploaded_torrent and not args.wait_uploaded_complete:
        blockers.append("--wait-uploaded-complete is required with --inject-uploaded-torrent for full uploaded torrent seeding closure.")
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
    summary = _target_upload_summary(result, preflight, args)
    qbit_wait_fields = _qbit_wait_summary_fields({"summary": summary, "result": result})
    handoff_fields = _target_upload_handoff_fields(summary, result, preflight, args, summary_file=None)
    if not getattr(args, "write_summary", False):
        return {**result, **handoff_fields, **qbit_wait_fields}
    summary_file = _write_target_upload_summary(result, preflight, args, args.summary_output_dir or args.package_dir)
    handoff_fields = _target_upload_handoff_fields(summary, result, preflight, args, summary_file=summary_file)
    return {**result, **handoff_fields, "summary_file": summary_file, "automation_handoff": _summary_automation_handoff(summary_file), **qbit_wait_fields}


def _target_upload_handoff_fields(summary: dict[str, Any], result: dict[str, Any], preflight: dict[str, Any], args: argparse.Namespace, *, summary_file: str | None) -> dict[str, Any]:
    artifacts = _target_upload_summary_artifacts(result, preflight, args, summary_file or "")
    qbit_wait_fields = _qbit_wait_summary_fields({"summary": summary, "result": result})
    recommended_commands = _target_upload_recommended_commands(summary, args, artifacts, qbit_wait_fields=qbit_wait_fields)
    resume_state = _target_upload_resume_state(summary, artifacts, recommended_commands)
    command_audit = _resume_command_audit_fields(recommended_commands, resume_state.get("next_command"), resume_state.get("next_command_argv"))
    automation_fields = _target_upload_automation_fields(summary, resume_state, command_audit)
    return {
        "summary": summary,
        "artifacts": artifacts,
        "recommended_commands": recommended_commands,
        "resume_state": resume_state,
        "next_stage": resume_state.get("next_stage"),
        "next_command": resume_state.get("next_command"),
        "next_command_argv": resume_state.get("next_command_argv"),
        **command_audit,
        **automation_fields,
    }


def _target_upload_automation_fields(summary: dict[str, Any], resume_state: dict[str, Any], command_audit: dict[str, Any]) -> dict[str, Any]:
    blockers = _string_list(summary.get("blockers"))
    missing_audit = _target_upload_missing_audit_artifacts(resume_state) if summary.get("ready") else []
    blockers = [*blockers, *[f"missing audit artifact: {name}" for name in missing_audit]]
    next_command = resume_state.get("next_command")
    if summary.get("ready") and not blockers and not next_command:
        automation_action = "complete"
    elif command_audit.get("next_command_placeholder"):
        automation_action = "fill_command_placeholders"
    elif next_command and command_audit.get("next_command_run_allowed"):
        automation_action = "run_next_command"
    elif next_command and command_audit.get("next_command_ready"):
        automation_action = "unsupported_next_command"
    else:
        automation_action = "resolve_blockers"
    payload = {
        "status": "ok" if summary.get("ready") and not blockers else "blocked",
        "blockers": blockers,
        "next_stage": resume_state.get("next_stage"),
    }
    return {
        "automation_action": automation_action,
        "automation_reason": _summary_automation_reason(payload, automation_action, blockers, next_command_run_blocker=command_audit.get("next_command_run_blocker")),
        "automation_exit_code": 0 if automation_action == "complete" else 1,
        "should_execute_next_command": automation_action == "run_next_command",
    }


def _target_upload_missing_audit_artifacts(resume_state: dict[str, Any]) -> list[str]:
    artifact_status = _summary_artifact_status(resume_state)
    required = (
        "target_preparation_ready",
        "uploaded_torrent_hash",
        "injected_torrent_hash",
        "injection_visible_in_client",
        "injection_verified",
        "target_hash_consistent",
        "target_duplicate_clean",
        "target_rule_obligations",
        "uploaded_wait_evidence",
    )
    return _missing_required_summary_artifacts(artifact_status, required)


def _write_target_upload_summary(result: dict[str, Any], preflight: dict[str, Any], args: argparse.Namespace, output_dir: str) -> str:
    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / "ptcli-target-upload-summary.json"
    summary = _target_upload_summary(result, preflight, args)
    artifacts = _target_upload_summary_artifacts(result, preflight, args, str(destination))
    material_diagnostics = _summary_material_diagnostics({"artifacts": artifacts})
    qbit_wait_fields = _qbit_wait_summary_fields({"summary": summary, "result": result})
    recommended_commands = _target_upload_recommended_commands(summary, args, artifacts, qbit_wait_fields=qbit_wait_fields)
    payload = {
        "schema_version": 1,
        "kind": "ptcli.target_upload.summary",
        "summary_file": str(destination),
        "automation_handoff": _summary_automation_handoff(str(destination)),
        "client": args.client,
        "qbit_options": _target_upload_qbit_options(args),
        "output_options": _target_upload_output_options(args),
        "wait_options": _target_upload_wait_options(args),
        "summary": summary,
        "artifacts": artifacts,
        "material_diagnostics": material_diagnostics,
        "recommended_commands": recommended_commands,
        "resume_state": _target_upload_resume_state(summary, artifacts, recommended_commands),
        "preflight": preflight,
        "result": result,
    }
    payload.update(qbit_wait_fields)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(destination)


def _target_upload_summary_artifacts(result: dict[str, Any], preflight: dict[str, Any], args: argparse.Namespace, summary_file: str) -> dict[str, Any]:
    downloaded_torrent = result.get("downloaded_torrent")
    uploaded_torrent_path = downloaded_torrent.get("path") if isinstance(downloaded_torrent, dict) else args.uploaded_torrent_file
    package_content_path = _mteam_package_content_path(preflight)
    rule_obligations = preflight.get("rule_obligation_review")
    duplicate_check = result.get("fresh_duplicate_check") if isinstance(result.get("fresh_duplicate_check"), dict) else _duplicate_check_from_target_package(preflight)
    preparation_audit = _target_preparation_audit_from_preflight(preflight)
    target_materials = _target_package_materials_from_dir(args.package_dir)
    return {
        "summary_file": summary_file,
        "package_dir": _path_artifact(args.package_dir),
        "package_content_path": _path_artifact(package_content_path),
        "target_torrent_file": _path_artifact(args.torrent_file),
        "target_materials": target_materials,
        "target_preparation_audit": preparation_audit,
        "target_preparation_ready": bool(preparation_audit.get("ready")),
        "target_materials_ready": preparation_audit.get("materials_ready"),
        "target_materials_missing": _string_list(target_materials.get("missing")),
        "target_materials_warnings": _string_list(target_materials.get("warnings")),
        "target_preparation_missing": _string_list(preparation_audit.get("missing")),
        "uploaded_torrent_id": _uploaded_torrent_id_from_result(result) or args.uploaded_torrent_id,
        "uploaded_torrent_hash": _uploaded_torrent_hash_from_result(result),
        "uploaded_torrent_file": _uploaded_torrent_file_artifact(downloaded_torrent, uploaded_torrent_path),
        "injection_visible_in_client": _injected_torrent_visible(result.get("injected_torrent")),
        "injection_verified": _injected_torrent_verified(result.get("injected_torrent")),
        "injected_torrent_hash": _torrent_hash_from_result(result.get("injected_torrent")),
        "uploaded_save_path": _path_artifact(_uploaded_save_path_from_result(result) or args.uploaded_save_path or package_content_path),
        "uploaded_wait_evidence": _wait_result_completed(result.get("uploaded_wait")),
        "fresh_duplicate_check": duplicate_check,
        "target_hash_consistent": not _uploaded_torrent_hash_consistency_blockers(result),
        "target_duplicate_clean": _fresh_duplicate_check_clean(duplicate_check),
        "target_rule_obligations": rule_obligations if isinstance(rule_obligations, dict) else None,
    }


def _uploaded_torrent_file_artifact(downloaded_torrent: Any, uploaded_torrent_path: Any) -> dict[str, Any] | None:
    base = _path_artifact(str(uploaded_torrent_path)) if uploaded_torrent_path else None
    if not isinstance(downloaded_torrent, dict):
        return base
    artifact = dict(base or {})
    for key in ("torrent_id", "reused", "size_bytes", "sha1", "hash", "torrent_hash", "infohash", "metadata_readable"):
        if key in downloaded_torrent:
            artifact[key] = downloaded_torrent.get(key)
    if "path" not in artifact and downloaded_torrent.get("path"):
        artifact["path"] = str(downloaded_torrent["path"])
    return artifact or None


def _target_upload_recommended_commands(summary: dict[str, Any], args: argparse.Namespace, artifacts: dict[str, Any], *, qbit_wait_fields: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    package_artifact = artifacts.get("package_dir")
    uploaded_torrent_artifact = artifacts.get("uploaded_torrent_file")
    uploaded_torrent_id = artifacts.get("uploaded_torrent_id")
    uploaded_save_path_artifact = artifacts.get("uploaded_save_path")
    uploaded_wait_retry = _uploaded_wait_retry_hint(qbit_wait_fields)
    retry_save_path = _uploaded_wait_retry_save_path(uploaded_wait_retry) or _artifact_path(uploaded_save_path_artifact)
    retry_save_path_artifact = {"path": retry_save_path} if retry_save_path else uploaded_save_path_artifact
    retry_args = _target_upload_retry_args(args)
    if not args.uploaded_save_path and retry_save_path:
        retry_args.extend(["--uploaded-save-path", retry_save_path])
    target_package_resume = _target_upload_target_package_resume_command(args, artifacts)
    if target_package_resume:
        commands.append(target_package_resume)
    commands.append(_ptcli_command_entry("target-upload-retry", retry_args))
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
        if args.config:
            download_args.extend(["--config", args.config])
        if args.uploaded_output_dir:
            download_args.extend(["--uploaded-output-dir", args.uploaded_output_dir])
        if args.summary_output_dir:
            download_args.extend(["--summary-output-dir", args.summary_output_dir])
        if retry_save_path:
            download_args.extend(["--uploaded-save-path", retry_save_path])
        if args.uploaded_qbit_category:
            download_args.extend(["--uploaded-qbit-category", args.uploaded_qbit_category])
        if args.uploaded_qbit_tags:
            download_args.extend(["--uploaded-qbit-tags", args.uploaded_qbit_tags])
        if args.uploaded_paused:
            download_args.append("--uploaded-paused")
        _append_uploaded_wait_options(download_args, args)
        commands.append(_ptcli_command_entry("resume-uploaded-torrent-download", download_args))
        retorrent_args = _target_upload_retorrent_resume_args(args, str(package_artifact.get("path") or args.package_dir), uploaded_torrent_id=str(uploaded_torrent_id), uploaded_save_path_artifact=retry_save_path_artifact)
        if retorrent_args:
            commands.append(_ptcli_command_entry("retorrent-resume-uploaded-torrent-download", retorrent_args))
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
        if args.config:
            resume_args.extend(["--config", args.config])
        if retry_save_path:
            resume_args.extend(["--uploaded-save-path", retry_save_path])
        if args.summary_output_dir:
            resume_args.extend(["--summary-output-dir", args.summary_output_dir])
        if args.uploaded_qbit_category:
            resume_args.extend(["--uploaded-qbit-category", args.uploaded_qbit_category])
        if args.uploaded_qbit_tags:
            resume_args.extend(["--uploaded-qbit-tags", args.uploaded_qbit_tags])
        if args.uploaded_paused:
            resume_args.append("--uploaded-paused")
        _append_uploaded_wait_options(resume_args, args)
        commands.append(_ptcli_command_entry("resume-uploaded-torrent", resume_args))
        retorrent_args = _target_upload_retorrent_resume_args(
            args,
            str(package_artifact.get("path") or args.package_dir),
            uploaded_torrent_file=str(uploaded_torrent_artifact["path"]),
            uploaded_save_path_artifact=retry_save_path_artifact,
        )
        if retorrent_args:
            commands.append(_ptcli_command_entry("retorrent-resume-uploaded-torrent", retorrent_args))
    if summary.get("ready"):
        commands.append(_ptcli_command_entry("verify-seeding", ["inspect", "--client", args.client, "--json"]))
    return commands


def _target_upload_target_package_resume_command(args: argparse.Namespace, artifacts: dict[str, Any]) -> dict[str, Any] | None:
    material_missing = _target_package_material_recovery_missing(artifacts)
    if not material_missing:
        return None
    package_dir = _artifact_path(artifacts.get("package_dir")) or args.package_dir
    source_info = _source_info_from_existing_target_package(package_dir)
    if not isinstance(source_info, dict) or not source_info.get("tracker") or not source_info.get("torrent_id"):
        return None
    content_path = _artifact_path(artifacts.get("package_content_path"))
    if not content_path:
        return None
    target_output_dir = str(Path(package_dir).expanduser().parent) if package_dir else "./tmp/target"
    resume_args = [
        "pipeline",
        "--from",
        normalize_tracker(str(source_info["tracker"])),
        "--source-id",
        str(source_info["torrent_id"]),
        "--to",
        "MTEAM",
        "--client",
        args.client,
        "--path",
        content_path,
        "--check-dupes",
        "--prepare-target",
        "--target-output-dir",
        target_output_dir,
        "--accept-rules",
        "--write-summary",
        "--json",
        *_target_package_material_resume_args({}, {}, {}, artifacts),
    ]
    if args.config:
        resume_args.extend(["--config", args.config])
    if args.summary_output_dir:
        resume_args.extend(["--summary-output-dir", args.summary_output_dir])
    return _ptcli_command_entry("resume-target-package", resume_args)


def _uploaded_wait_retry_hint(qbit_wait_fields: dict[str, Any] | None) -> dict[str, Any]:
    retry_hints = qbit_wait_fields.get("qbit_wait_retry_hints") if isinstance(qbit_wait_fields, dict) and isinstance(qbit_wait_fields.get("qbit_wait_retry_hints"), dict) else {}
    hint = retry_hints.get("uploaded") if isinstance(retry_hints.get("uploaded"), dict) else {}
    return hint if hint.get("retry_recommended") is True else {}


def _uploaded_wait_retry_save_path(hint: dict[str, Any]) -> str | None:
    return _qbit_wait_retry_save_path(hint)


def _source_qbit_wait_retry_save_path(hint: dict[str, Any]) -> str | None:
    value = hint.get("suggested_save_path")
    return value if isinstance(value, str) and value else None


def _qbit_wait_retry_save_path(hint: dict[str, Any]) -> str | None:
    for key in ("suggested_content_path", "suggested_save_path"):
        value = hint.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _target_upload_retorrent_resume_args(
    args: argparse.Namespace,
    package_dir: str,
    *,
    uploaded_torrent_file: str | None = None,
    uploaded_torrent_id: str | None = None,
    uploaded_save_path_artifact: Any = None,
) -> list[str] | None:
    source_info = _source_info_from_existing_target_package(package_dir)
    if not isinstance(source_info, dict) or not source_info.get("tracker") or not source_info.get("torrent_id"):
        return None
    retorrent_args = [
        "retorrent",
        "--from",
        str(source_info["tracker"]),
        "--source-id",
        str(source_info["torrent_id"]),
        "--to",
        "MTEAM",
        "--client",
        args.client,
        "--execute",
        "--accept-rules",
        "--confirm-upload",
        "--package-dir",
        package_dir,
    ]
    if args.config:
        retorrent_args.extend(["--config", args.config])
    if uploaded_torrent_file:
        retorrent_args.extend(["--uploaded-torrent-file", uploaded_torrent_file])
    if uploaded_torrent_id:
        retorrent_args.extend(["--uploaded-torrent-id", uploaded_torrent_id, "--download-uploaded-torrent"])
    if args.uploaded_output_dir and uploaded_torrent_id:
        retorrent_args.extend(["--uploaded-output-dir", args.uploaded_output_dir])
    if isinstance(uploaded_save_path_artifact, dict) and uploaded_save_path_artifact.get("path"):
        retorrent_args.extend(["--uploaded-save-path", str(uploaded_save_path_artifact["path"])])
    if args.uploaded_qbit_category:
        retorrent_args.extend(["--uploaded-qbit-category", args.uploaded_qbit_category])
    if args.uploaded_qbit_tags:
        retorrent_args.extend(["--uploaded-qbit-tags", args.uploaded_qbit_tags])
    if args.uploaded_paused:
        retorrent_args.append("--uploaded-paused")
    _append_uploaded_wait_options(retorrent_args, args)
    retorrent_args.append("--write-summary")
    if args.summary_output_dir:
        retorrent_args.extend(["--summary-output-dir", args.summary_output_dir])
    retorrent_args.append("--json")
    return retorrent_args


def _append_uploaded_wait_options(command_args: list[str], args: argparse.Namespace) -> None:
    _append_wait_options(
        command_args,
        timeout_option="--uploaded-wait-timeout",
        timeout=getattr(args, "uploaded_wait_timeout", None),
        default_timeout=600.0,
        interval_option="--uploaded-wait-interval",
        interval=getattr(args, "uploaded_wait_interval", None),
        default_interval=15.0,
    )


def _append_wait_options(
    command_args: list[str],
    *,
    timeout_option: str,
    timeout: Any,
    default_timeout: float,
    interval_option: str,
    interval: Any,
    default_interval: float,
) -> None:
    if timeout is not None and float(timeout) != default_timeout:
        command_args.extend([timeout_option, _format_number_arg(timeout)])
    if interval is not None and float(interval) != default_interval:
        command_args.extend([interval_option, _format_number_arg(interval)])


def _format_number_arg(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _target_upload_resume_state(summary: dict[str, Any], artifacts: dict[str, Any], recommended_commands: list[dict[str, Any]]) -> dict[str, Any]:
    commands_by_stage = {str(command.get("stage")): str(command.get("command")) for command in recommended_commands if isinstance(command, dict)}
    resume_available = any(stage.startswith("resume-") for stage in commands_by_stage)
    next_command = _target_upload_next_command(summary, commands_by_stage) if resume_available else {"stage": None, "command": None}
    next_command_argv = _resume_state_next_command_argv(next_command, recommended_commands)
    qbit_wait_fields = _qbit_wait_summary_fields({"summary": summary})
    return {
        "ready": bool(summary.get("ready")),
        "resume_available": resume_available,
        "next_stage": next_command.get("stage"),
        "next_command": next_command.get("command"),
        "next_command_argv": next_command_argv,
        "available_stages": [str(command.get("stage")) for command in recommended_commands if isinstance(command, dict)],
        "artifacts": {
            "package_dir": bool(_path_artifact_exists(artifacts.get("package_dir"))),
            "package_content_path": bool(isinstance(artifacts.get("package_content_path"), dict) and artifacts["package_content_path"].get("path")),
            "target_torrent_file": bool(_path_artifact_exists(artifacts.get("target_torrent_file"))),
            "uploaded_torrent_id": bool(artifacts.get("uploaded_torrent_id")),
            "uploaded_torrent_file": bool(_path_artifact_exists(artifacts.get("uploaded_torrent_file"))),
            "uploaded_torrent_hash": bool(artifacts.get("uploaded_torrent_hash")),
            "injection_visible_in_client": bool(artifacts.get("injection_visible_in_client")),
            "injection_verified": bool(artifacts.get("injection_verified")),
            "injected_torrent_hash": bool(artifacts.get("injected_torrent_hash")),
            "uploaded_save_path": bool(_path_artifact_exists(artifacts.get("uploaded_save_path"))),
            "uploaded_wait_evidence": bool(artifacts.get("uploaded_wait_evidence")),
            "target_hash_consistent": bool(artifacts.get("target_hash_consistent")),
            "target_duplicate_clean": bool(artifacts.get("target_duplicate_clean")),
            "target_rule_obligations": _rule_obligations_artifact_ready(artifacts.get("target_rule_obligations")),
            "target_preparation_ready": bool(artifacts.get("target_preparation_ready")),
        },
        "uploaded_followup": _target_upload_followup_closure(summary, artifacts, qbit_wait_fields),
        "blockers": _string_list(summary.get("blockers")),
    }


def _target_upload_followup_closure(summary: dict[str, Any], artifacts: dict[str, Any], qbit_wait_fields: dict[str, Any]) -> dict[str, Any]:
    uploaded_torrent_file = artifacts.get("uploaded_torrent_file") if isinstance(artifacts.get("uploaded_torrent_file"), dict) else {}
    uploaded_save_path = artifacts.get("uploaded_save_path") if isinstance(artifacts.get("uploaded_save_path"), dict) else {}
    downloaded = bool(_path_artifact_exists(uploaded_torrent_file))
    uploaded = bool(summary.get("uploaded"))
    injected = bool(summary.get("injected") or artifacts.get("injection_verified"))
    injection_visible = bool(artifacts.get("injection_visible_in_client"))
    injection_verified = bool(artifacts.get("injection_verified"))
    wait_evidence = bool(artifacts.get("uploaded_wait_evidence"))
    hash_consistent = bool(artifacts.get("target_hash_consistent"))
    duplicate_clean = bool(artifacts.get("target_duplicate_clean"))
    rule_obligations_ready = _rule_obligations_artifact_ready(artifacts.get("target_rule_obligations"))
    ready = bool(uploaded and downloaded and injected and injection_verified and wait_evidence and hash_consistent and duplicate_clean and rule_obligations_ready)
    checks = {
        "uploaded": uploaded,
        "downloaded": downloaded,
        "uploaded_torrent_hash": bool(artifacts.get("uploaded_torrent_hash")),
        "injected_torrent_hash": bool(artifacts.get("injected_torrent_hash")),
        "injection_visible_in_client": injection_visible,
        "injection_verified": injection_verified,
        "uploaded_wait_evidence": wait_evidence,
        "hash_consistent": hash_consistent,
        "duplicate_clean": duplicate_clean,
        "rule_obligations_ready": rule_obligations_ready,
    }
    missing = [name for name, ok in checks.items() if not ok]
    blockers = _target_upload_followup_blockers(missing)
    qbit_retry_hints = qbit_wait_fields.get("qbit_wait_retry_hints") if isinstance(qbit_wait_fields.get("qbit_wait_retry_hints"), dict) else {}
    uploaded_retry_hint = qbit_retry_hints.get("uploaded") if isinstance(qbit_retry_hints.get("uploaded"), dict) else {}
    qbit_wait_mismatches = _string_list(qbit_wait_fields.get("qbit_wait_mismatches"))
    uploaded_wait = summary.get("uploaded_wait") if isinstance(summary.get("uploaded_wait"), dict) else {}
    uploaded_wait_query = uploaded_wait.get("query") if isinstance(uploaded_wait.get("query"), dict) else {}
    return {
        "ready": ready,
        "ready_for_uploaded_seeding": ready,
        "gates": checks,
        "blockers": blockers,
        "uploaded": uploaded,
        "downloaded": downloaded,
        "injected": injected,
        "injection_visible_in_client": injection_visible,
        "injection_verified": injection_verified,
        "uploaded_wait_evidence": wait_evidence,
        "hash_consistent": hash_consistent,
        "duplicate_clean": duplicate_clean,
        "rule_obligations_ready": rule_obligations_ready,
        "missing": missing,
        "uploaded_torrent_id": artifacts.get("uploaded_torrent_id"),
        "uploaded_torrent_hash": artifacts.get("uploaded_torrent_hash"),
        "injected_torrent_hash": artifacts.get("injected_torrent_hash"),
        "uploaded_torrent_file": uploaded_torrent_file.get("path"),
        "uploaded_torrent_file_evidence": {
            "path": uploaded_torrent_file.get("path"),
            "exists": uploaded_torrent_file.get("exists"),
            "is_file": uploaded_torrent_file.get("is_file"),
            "size_bytes": uploaded_torrent_file.get("size_bytes"),
            "sha1": uploaded_torrent_file.get("sha1"),
            "torrent_hash": uploaded_torrent_file.get("torrent_hash") or uploaded_torrent_file.get("hash") or uploaded_torrent_file.get("infohash"),
            "metadata_readable": uploaded_torrent_file.get("metadata_readable"),
            "reused": uploaded_torrent_file.get("reused"),
        },
        "uploaded_save_path": uploaded_save_path.get("path"),
        "qbit_wait_mismatch": bool(qbit_wait_mismatches),
        "qbit_wait_mismatches": qbit_wait_mismatches,
        "uploaded_wait_query": uploaded_wait_query,
        "wait_retry": uploaded_retry_hint if uploaded_retry_hint else None,
        "next_actions": _target_upload_followup_next_actions(missing),
    }


def _target_upload_followup_blockers(missing: list[str]) -> list[str]:
    labels = {
        "uploaded": "target upload did not report a completed MTEAM upload",
        "downloaded": "uploaded MTEAM torrent file has not been downloaded or provided",
        "uploaded_torrent_hash": "uploaded MTEAM torrent hash evidence is missing",
        "injected_torrent_hash": "uploaded MTEAM torrent has not been injected into qBittorrent",
        "injection_visible_in_client": "uploaded MTEAM torrent is not visible in qBittorrent after injection",
        "injection_verified": "uploaded MTEAM torrent injection is not verified in qBittorrent",
        "uploaded_wait_evidence": "qBittorrent has not reported the uploaded MTEAM torrent as complete",
        "hash_consistent": "uploaded torrent hash and qBittorrent injected hash are not verified consistent",
        "duplicate_clean": "fresh MTEAM duplicate check evidence is not clean",
        "rule_obligations_ready": "source and MTEAM rule obligations are not ready",
    }
    return [labels.get(item, item) for item in missing]


def _target_upload_followup_next_actions(missing: list[str]) -> list[str]:
    actions: list[str] = []
    if any(item in missing for item in ("downloaded", "uploaded_torrent_hash")):
        actions.append("Download or provide the uploaded MTEAM torrent, then resume uploaded torrent injection.")
    if any(item in missing for item in ("injected_torrent_hash", "injection_verified")):
        actions.append("Inject the uploaded MTEAM torrent into qBittorrent with the correct save path.")
    if "uploaded_wait_evidence" in missing:
        actions.append("Wait for qBittorrent to report the uploaded MTEAM torrent as matched and complete.")
    if "hash_consistent" in missing:
        actions.append("Verify the downloaded uploaded torrent hash matches the injected qBittorrent task.")
    if "duplicate_clean" in missing:
        actions.append("Rerun a fresh MTEAM duplicate check before treating the upload as closed.")
    if "rule_obligations_ready" in missing:
        actions.append("Confirm source and MTEAM rule obligations before treating the upload as closed.")
    return actions


def _target_upload_next_command(summary: dict[str, Any], commands_by_stage: dict[str, str]) -> dict[str, str | None]:
    completion_review = summary.get("completion_review") if isinstance(summary.get("completion_review"), dict) else {}
    if summary.get("ready") and completion_review.get("complete") is not False:
        return {"stage": None, "command": None}
    uploaded_wait = summary.get("uploaded_wait")
    uploaded_wait_complete = _wait_result_completed(uploaded_wait)
    blockers = _string_list(summary.get("blockers"))
    blocker_text = "\n".join(blockers)
    preferred_stages: list[str] = []
    completion_missing = _string_list(completion_review.get("missing"))
    needs_retry = (
        "uploaded_torrent_hash" in blocker_text
        or "duplicate" in blocker_text
        or "rule obligation" in blocker_text
        or "preflight" in blocker_text
        or summary.get("hash_consistent") is False
        or summary.get("duplicate_clean") is False
        or not _rule_obligations_artifact_ready(summary.get("rule_obligations"))
        or any(item in completion_missing for item in ("hash_consistent", "duplicate_clean", "rule_obligations_ready"))
    )
    if needs_retry:
        preferred_stages.append("target-upload-retry")
    if any(blocker.startswith(("materials.", "target.materials.", "description.")) for blocker in blockers) or any(item.startswith(("materials.", "target.materials.", "description.")) for item in completion_missing):
        preferred_stages.insert(0, "resume-target-package")
    if "downloaded_torrent" in blocker_text or not summary.get("downloaded"):
        preferred_stages.append("resume-uploaded-torrent-download")
    if (
        "injected_torrent" in blocker_text
        or "uploaded_wait" in blocker_text
        or any(item in completion_missing for item in ("injection_visible_in_client", "injection_verified", "uploaded_wait_complete"))
        or not summary.get("injected")
        or not summary.get("seeding_verified")
    ):
        preferred_stages.append("resume-uploaded-torrent")
    if not needs_retry and summary.get("injected") and (summary.get("seeding_verified") or uploaded_wait_complete):
        preferred_stages.append("target-upload-retry")
    preferred_stages.extend(["resume-target-package", "resume-uploaded-torrent-download", "resume-uploaded-torrent", "target-upload-retry"])
    for stage in preferred_stages:
        command = commands_by_stage.get(stage)
        if command:
            return {"stage": stage, "command": command}
    return {"stage": None, "command": None}


def _path_artifact_exists(artifact: Any) -> bool:
    return isinstance(artifact, dict) and bool(artifact.get("path")) and artifact.get("exists") is not False


def _target_upload_retry_args(args: argparse.Namespace) -> list[str]:
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
        ("--uploaded-wait-timeout", _format_number_arg(args.uploaded_wait_timeout) if args.wait_uploaded_complete and args.uploaded_wait_timeout != 600.0 else None),
        ("--uploaded-wait-interval", _format_number_arg(args.uploaded_wait_interval) if args.wait_uploaded_complete and args.uploaded_wait_interval != 15.0 else None),
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
    return retry_args


def _target_upload_retry_command(args: argparse.Namespace) -> str:
    return _ptcli_command(_target_upload_retry_args(args))


def _target_upload_qbit_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "uploaded": {
            "category": args.uploaded_qbit_category,
            "tags": args.uploaded_qbit_tags,
            "paused": bool(args.uploaded_paused),
        },
    }


def _target_upload_output_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "uploaded_output_dir": args.uploaded_output_dir,
        "summary_output_dir": args.summary_output_dir,
    }


def _target_upload_wait_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "uploaded": {
            "timeout": args.uploaded_wait_timeout,
            "interval": args.uploaded_wait_interval,
        },
    }


def summary_check_payload(args: argparse.Namespace) -> dict[str, Any]:
    summary_path = Path(args.summary_file).expanduser()
    if not summary_path.exists():
        return _summary_check_result({
            "status": "blocked",
            "summary_file": str(summary_path),
            "automation_handoff": _summary_automation_handoff(str(summary_path)),
            "expected_schema_version": SUMMARY_SCHEMA_VERSION,
            "supported_kinds": list(SUPPORTED_SUMMARY_KINDS),
            "blockers": ["Summary file does not exist."],
        })
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _summary_check_result({
            "status": "blocked",
            "summary_file": str(summary_path),
            "automation_handoff": _summary_automation_handoff(str(summary_path)),
            "expected_schema_version": SUMMARY_SCHEMA_VERSION,
            "supported_kinds": list(SUPPORTED_SUMMARY_KINDS),
            "blockers": [f"Summary file is not valid JSON: {exc.msg}"],
        })
    if not isinstance(payload, dict):
        return _summary_check_result({
            "status": "blocked",
            "summary_file": str(summary_path),
            "automation_handoff": _summary_automation_handoff(str(summary_path)),
            "expected_schema_version": SUMMARY_SCHEMA_VERSION,
            "supported_kinds": list(SUPPORTED_SUMMARY_KINDS),
            "blockers": ["Summary file root must be a JSON object."],
        })
    return _summary_check_from_payload(payload, str(summary_path))


def _summary_check_from_payload(payload: dict[str, Any], summary_file: str) -> dict[str, Any]:
    diagnostics = _summary_check_diagnostics(payload)
    kind = str(diagnostics["kind"])
    blockers = []
    if not diagnostics["schema_version_ok"]:
        blockers.append(f"Unsupported summary schema_version: {diagnostics['schema_version']!r}; expected {SUMMARY_SCHEMA_VERSION}.")
    if not diagnostics["kind_supported"]:
        blockers.append(f"Unsupported ptcli summary kind: {kind}")
    if blockers:
        return _summary_check_result({
            "status": "blocked",
            "summary_file": summary_file,
            "automation_handoff": _summary_automation_handoff(summary_file),
            "blockers": blockers,
            **diagnostics,
        })
    if kind == "ptcli.pipeline.run_summary":
        return _pipeline_summary_check(payload, summary_file)
    if kind == "ptcli.target_upload.summary":
        return _target_upload_summary_check(payload, summary_file)
    if kind == "ptcli.doctor.live_readiness":
        return _doctor_summary_check(payload, summary_file)
    msg = "summary kind dispatch escaped validation"
    raise RuntimeError(msg)


def _summary_check_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    schema_version = payload.get("schema_version")
    kind = str(payload.get("kind") or "unknown")
    qbit_wait_diagnostics = _summary_qbit_wait_diagnostics(payload)
    qbit_wait_mismatches = _summary_qbit_wait_mismatches(qbit_wait_diagnostics)
    qbit_wait_retry_hints = _summary_qbit_wait_retry_hints(qbit_wait_diagnostics)
    flow_diagnostics = _summary_flow_diagnostics(payload)
    material_diagnostics = _summary_material_diagnostics(payload)
    target_upload_diagnostics = _summary_target_upload_diagnostics(payload)
    target_preflight_diagnostics = _summary_target_preflight_diagnostics(payload, target_upload_diagnostics)
    closure_review = payload.get("closure_review") if isinstance(payload.get("closure_review"), dict) else _pipeline_closure_review(payload)
    closure_modes = _summary_closure_modes(payload)
    closure_status = payload.get("closure_status") if isinstance(payload.get("closure_status"), dict) else _closure_status_summary(payload)
    completion_matrix = _summary_completion_matrix(
        flow_diagnostics=flow_diagnostics,
        material_diagnostics=material_diagnostics,
        target_upload_diagnostics=target_upload_diagnostics,
        closure_review=closure_review,
        closure_status=closure_status,
        qbit_wait_mismatches=qbit_wait_mismatches,
    )
    return {
        "schema_version": schema_version,
        "expected_schema_version": SUMMARY_SCHEMA_VERSION,
        "schema_version_ok": schema_version == SUMMARY_SCHEMA_VERSION,
        "kind": kind,
        "supported_kinds": list(SUPPORTED_SUMMARY_KINDS),
        "kind_supported": kind in SUPPORTED_SUMMARY_KINDS,
        "flow_diagnostics": flow_diagnostics,
        "credential_requirements": flow_diagnostics.get("credential_requirements", []),
        "material_diagnostics": material_diagnostics,
        "target_upload_diagnostics": target_upload_diagnostics,
        "target_preflight_diagnostics": target_preflight_diagnostics,
        "closure_review": closure_review,
        "qbit_wait_diagnostics": qbit_wait_diagnostics,
        "qbit_wait_mismatch": bool(qbit_wait_mismatches),
        "qbit_wait_mismatches": qbit_wait_mismatches,
        "qbit_wait_retry_hints": qbit_wait_retry_hints,
        "closure_modes": closure_modes,
        "closure_status": closure_status,
        "completion_matrix": completion_matrix,
        "source_mode": closure_modes.get("source"),
        "target_mode": closure_modes.get("target"),
    }


def _summary_target_preflight_diagnostics(payload: dict[str, Any], target_upload_diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    doctor_preflight = artifacts.get("target_preflight_gates") if isinstance(artifacts.get("target_preflight_gates"), dict) else {}
    upload_diagnostics = target_upload_diagnostics if isinstance(target_upload_diagnostics, dict) else {}
    upload_preflight = upload_diagnostics.get("preflight") if isinstance(upload_diagnostics.get("preflight"), dict) else {}
    preflight = doctor_preflight or upload_preflight
    if not preflight:
        return {
            "present": False,
            "source": None,
            "status": None,
            "ready": None,
            "blockers": [],
            "missing": [],
            "description_missing": [],
            "torrent_file": None,
        }
    source = "doctor" if doctor_preflight else "target_upload"
    torrent_file = preflight.get("torrent_file") if isinstance(preflight.get("torrent_file"), dict) else None
    return {
        "present": True,
        "source": source,
        "status": preflight.get("status"),
        "ready": preflight.get("ready"),
        "blockers": _string_list(preflight.get("blockers")),
        "missing": _string_list(preflight.get("missing")),
        "description_missing": _string_list(preflight.get("description_missing")),
        "target_preparation_ready": preflight.get("target_preparation_ready"),
        "materials_ready": preflight.get("materials_ready"),
        "metadata_ready": preflight.get("metadata_ready"),
        "assets_ready": preflight.get("assets_ready"),
        "description_ready": preflight.get("description_ready"),
        "payload_ready": preflight.get("payload_ready"),
        "payload_checks_ready": preflight.get("payload_checks_ready"),
        "description_checks_ready": preflight.get("description_checks_ready"),
        "materials_ready_required": preflight.get("materials_ready_required"),
        "torrent_file": torrent_file,
    }


def _summary_completion_matrix(
    *,
    flow_diagnostics: dict[str, Any],
    material_diagnostics: dict[str, Any],
    target_upload_diagnostics: dict[str, Any],
    closure_review: dict[str, Any],
    closure_status: dict[str, Any],
    qbit_wait_mismatches: list[str],
) -> dict[str, Any]:
    closure_source = closure_status.get("source") if isinstance(closure_status.get("source"), dict) else {}
    closure_target = closure_status.get("target") if isinstance(closure_status.get("target"), dict) else {}
    review_target = closure_review.get("target") if isinstance(closure_review.get("target"), dict) else {}
    target_completion = target_upload_diagnostics.get("completion") if isinstance(target_upload_diagnostics.get("completion"), dict) else {}
    target_checks = target_completion.get("checks") if isinstance(target_completion.get("checks"), dict) else {}
    ready_for_uploaded_seeding = _target_upload_ready_for_uploaded_seeding(target_upload_diagnostics, closure_target, review_target)
    domains = {
        "flow": _completion_domain(
            flow_diagnostics.get("ready"),
            [] if flow_diagnostics.get("ready") is True else _string_list(flow_diagnostics.get("credential_requirements")),
            {"source_tracker": flow_diagnostics.get("source_tracker"), "target_trackers": _string_list(flow_diagnostics.get("target_trackers"))},
        ),
        "source": _completion_domain(
            _source_matrix_ready(closure_source),
            _false_keys(closure_source, ("ready", "hash_consistent", "wait_evidence", "injection_verified")),
            {
                "mode": closure_source.get("mode"),
                "hash_consistent": closure_source.get("hash_consistent"),
                "wait_evidence": closure_source.get("wait_evidence"),
                "injection_verified": closure_source.get("injection_verified"),
            },
        ),
        "materials": _completion_domain(
            _material_matrix_ready(material_diagnostics, review_target),
            _material_matrix_missing(material_diagnostics, review_target),
            {
                "target_materials_ready": material_diagnostics.get("target_materials_ready"),
                "target_preparation_ready": material_diagnostics.get("target_preparation_ready") or review_target.get("preparation_ready"),
                "critical_ready": material_diagnostics.get("critical_ready"),
                "ready_for_mteam_upload": material_diagnostics.get("ready_for_mteam_upload"),
                "description_ready": review_target.get("description_ready"),
            },
        ),
        "rules": _completion_domain(
            _rules_matrix_ready(closure_target, review_target, target_checks),
            ["rule_obligations_ready"] if _rules_matrix_ready(closure_target, review_target, target_checks) is False else [],
            {
                "closure_ready": closure_target.get("rule_obligations_ready"),
                "review_ready": review_target.get("rule_obligations_ready"),
                "target_upload_check": target_checks.get("rule_obligations_ready"),
            },
        ),
        "target_upload": _completion_domain(
            _target_upload_matrix_ready(target_upload_diagnostics, closure_target, review_target),
            _target_upload_matrix_missing(target_upload_diagnostics, closure_target, review_target),
            {
                "mode": target_upload_diagnostics.get("mode") or review_target.get("mode"),
                "completion_complete": target_completion.get("complete"),
                "ready_for_uploaded_seeding": ready_for_uploaded_seeding,
                "closure_ready": closure_target.get("ready"),
                "uploaded_wait_evidence": closure_target.get("uploaded_wait_evidence") or review_target.get("uploaded_wait_evidence"),
                "injection_verified": closure_target.get("injection_verified") or review_target.get("injection_verified"),
            },
        ),
        "qbit_wait": _completion_domain(
            not qbit_wait_mismatches,
            qbit_wait_mismatches,
            {
                "mismatch": bool(qbit_wait_mismatches),
                "source_wait_evidence": closure_source.get("wait_evidence"),
                "uploaded_wait_evidence": closure_target.get("uploaded_wait_evidence") or review_target.get("uploaded_wait_evidence"),
            },
        ),
    }
    required = [name for name, domain in domains.items() if domain["ready"] is not None]
    missing_domains = [name for name in required if domains[name]["ready"] is not True]
    return {
        "ready": bool(required) and not missing_domains,
        "required": required,
        "missing_domains": missing_domains,
        "domains": domains,
    }


def _completion_domain(ready: Any, missing: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "ready": ready if isinstance(ready, bool) else None,
        "missing": _string_list(missing),
        "evidence": evidence,
    }


def _false_keys(payload: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    return [key for key in keys if payload.get(key) is False]


def _material_matrix_ready(material_diagnostics: dict[str, Any], review_target: dict[str, Any]) -> bool | None:
    if material_diagnostics.get("present"):
        return bool(material_diagnostics.get("ready_for_mteam_upload"))
    if review_target:
        return bool(review_target.get("materials_ready") and review_target.get("preparation_ready") and review_target.get("description_ready"))
    return None


def _source_matrix_ready(closure_source: dict[str, Any]) -> bool | None:
    has_evidence = bool(closure_source.get("mode")) or any(closure_source.get(key) is True for key in ("ready", "hash_consistent", "wait_evidence", "injection_verified"))
    if not has_evidence:
        return None
    return bool(closure_source.get("ready"))


def _material_matrix_missing(material_diagnostics: dict[str, Any], review_target: dict[str, Any]) -> list[str]:
    missing = _string_list(material_diagnostics.get("critical_missing"))
    _extend_unique_string(missing, _string_list(material_diagnostics.get("target_materials_missing")))
    _extend_unique_string(missing, _string_list(material_diagnostics.get("target_preparation_missing") or review_target.get("preparation_missing")))
    _extend_unique_string(missing, _string_list(material_diagnostics.get("upload_material_blockers")))
    return missing


def _rules_matrix_ready(closure_target: dict[str, Any], review_target: dict[str, Any], target_checks: dict[str, Any]) -> bool | None:
    for value in (target_checks.get("rule_obligations_ready"), review_target.get("rule_obligations_ready"), closure_target.get("rule_obligations_ready")):
        if isinstance(value, bool):
            return value
    return None


def _target_upload_matrix_ready(target_upload_diagnostics: dict[str, Any], closure_target: dict[str, Any], review_target: dict[str, Any]) -> bool | None:
    completion = target_upload_diagnostics.get("completion") if isinstance(target_upload_diagnostics.get("completion"), dict) else {}
    if target_upload_diagnostics.get("present"):
        return bool(completion.get("complete"))
    if closure_target or review_target:
        return bool(closure_target.get("ready") or review_target.get("ready"))
    return None


def _target_upload_ready_for_uploaded_seeding(target_upload_diagnostics: dict[str, Any], closure_target: dict[str, Any], review_target: dict[str, Any]) -> bool | None:
    ready = target_upload_diagnostics.get("ready_for_uploaded_seeding")
    if isinstance(ready, bool):
        return ready
    target_ready = closure_target.get("ready")
    uploaded_wait_evidence = closure_target.get("uploaded_wait_evidence") or review_target.get("uploaded_wait_evidence")
    injection_verified = closure_target.get("injection_verified") or review_target.get("injection_verified")
    if any(isinstance(value, bool) for value in (target_ready, uploaded_wait_evidence, injection_verified)):
        return bool(target_ready and uploaded_wait_evidence and injection_verified)
    return None


def _target_upload_matrix_missing(target_upload_diagnostics: dict[str, Any], closure_target: dict[str, Any], review_target: dict[str, Any]) -> list[str]:
    completion = target_upload_diagnostics.get("completion") if isinstance(target_upload_diagnostics.get("completion"), dict) else {}
    missing = _string_list(completion.get("missing"))
    if target_upload_diagnostics.get("present"):
        return missing
    for key in ("ready", "hash_consistent", "duplicate_clean", "uploaded_wait_evidence", "injection_verified"):
        if closure_target.get(key) is False or review_target.get(key) is False:
            _append_unique_string(missing, key)
    return missing


def _summary_target_upload_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("kind") != "ptcli.target_upload.summary":
        return {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    completion_review = summary.get("completion_review") if isinstance(summary.get("completion_review"), dict) else {}
    checks = completion_review.get("checks") if isinstance(completion_review.get("checks"), dict) else {}
    uploaded_wait_query = completion_review.get("uploaded_wait_query") if isinstance(completion_review.get("uploaded_wait_query"), dict) else {}
    preparation_audit = summary.get("target_preparation_audit") if isinstance(summary.get("target_preparation_audit"), dict) else {}
    preparation_payload = preparation_audit.get("payload") if isinstance(preparation_audit.get("payload"), dict) else {}
    preflight_torrent = preparation_payload.get("torrent_file") if isinstance(preparation_payload.get("torrent_file"), dict) else {}
    payload_review = _target_upload_payload_review_from_summary(payload)
    resume_state = payload.get("resume_state") if isinstance(payload.get("resume_state"), dict) else {}
    uploaded_followup = resume_state.get("uploaded_followup") if isinstance(resume_state.get("uploaded_followup"), dict) else {}
    return {
        "present": bool(summary),
        "mode": summary.get("mode"),
        "ready": summary.get("ready"),
        "uploaded": summary.get("uploaded"),
        "ready_for_uploaded_seeding": uploaded_followup.get("ready_for_uploaded_seeding") if "ready_for_uploaded_seeding" in uploaded_followup else uploaded_followup.get("ready"),
        "uploaded_followup": uploaded_followup,
        "completion": {
            "complete": completion_review.get("complete"),
            "missing": _string_list(completion_review.get("missing")),
            "checks": checks,
            "uploaded_torrent_id": completion_review.get("uploaded_torrent_id"),
            "uploaded_torrent_hash": completion_review.get("uploaded_torrent_hash"),
            "uploaded_torrent_path": completion_review.get("uploaded_torrent_path"),
            "injected_torrent_hash": completion_review.get("injected_torrent_hash"),
            "uploaded_save_path": completion_review.get("uploaded_save_path"),
            "uploaded_wait_query": uploaded_wait_query,
            "preflight_status": completion_review.get("preflight_status"),
        },
        "preflight": {
            "status": summary.get("preflight_status"),
            "ready": summary.get("preflight_status") == "ready" and bool(preparation_audit.get("ready")),
            "blockers": _string_list(summary.get("preflight_blockers")),
            "missing": _string_list(preparation_audit.get("missing")),
            "target_preparation_ready": summary.get("target_preparation_ready"),
            "materials_ready": preparation_audit.get("materials_ready"),
            "metadata_ready": preparation_audit.get("metadata_ready"),
            "assets_ready": preparation_audit.get("assets_ready"),
            "description_ready": preparation_audit.get("description_ready"),
            "description_missing": _string_list((preparation_audit.get("description") if isinstance(preparation_audit.get("description"), dict) else {}).get("missing")),
            "payload_ready": preparation_audit.get("payload_ready"),
            "payload_checks_ready": preparation_payload.get("payload_checks_ready"),
            "description_checks_ready": preparation_payload.get("description_checks_ready"),
            "materials_ready_required": preparation_payload.get("materials_ready_required"),
            "torrent_file": preflight_torrent,
        },
        "payload_review": payload_review,
    }


def _target_upload_payload_review_from_summary(payload: dict[str, Any]) -> dict[str, Any]:
    preflight = payload.get("preflight") if isinstance(payload.get("preflight"), dict) else {}
    upload_payload = preflight.get("upload_payload") if isinstance(preflight.get("upload_payload"), dict) else {}
    return _payload_review_summary_from_upload_payload(upload_payload)


def _payload_review_summary_from_upload_payload(upload_payload: dict[str, Any]) -> dict[str, Any]:
    review = upload_payload.get("review") if isinstance(upload_payload.get("review"), dict) else {}
    description = review.get("description") if isinstance(review.get("description"), dict) else {}
    materials = review.get("materials") if isinstance(review.get("materials"), dict) else {}
    external_id_readiness = description.get("external_id_readiness") if isinstance(description.get("external_id_readiness"), dict) else {}
    screenshot_coverage = description.get("screenshot_coverage") if isinstance(description.get("screenshot_coverage"), dict) else {}
    description_evidence = description.get("evidence") if isinstance(description.get("evidence"), dict) else {}
    return {
        "present": bool(review),
        "recovery_missing": _string_list(upload_payload.get("recovery_missing")),
        "next_actions": _string_list(upload_payload.get("next_actions")),
        "description": {
            "external_links": description.get("external_links") if isinstance(description.get("external_links"), dict) else {},
            "external_id_readiness": external_id_readiness,
            "external_id_missing": _string_list(description.get("external_id_missing")),
            "evidence": description_evidence,
            "completeness": _description_completeness_summary(description),
            "has_ptgen_description": description.get("has_ptgen_description"),
            "ptgen_description_length": description.get("ptgen_description_length"),
            "has_mediainfo_or_bdinfo": description.get("has_mediainfo_or_bdinfo"),
            "has_screenshot_bbcode": description.get("has_screenshot_bbcode"),
            "bbcode_image_count": description.get("bbcode_image_count"),
            "bbcode_image_urls": _string_list(description.get("bbcode_image_urls")),
            "screenshot_coverage": {
                "ready": screenshot_coverage.get("ready"),
                "expected_urls": _string_list(screenshot_coverage.get("expected_urls")),
                "description_urls": _string_list(screenshot_coverage.get("description_urls")),
                "missing_urls": _string_list(screenshot_coverage.get("missing_urls")),
            },
        },
        "materials": {
            "mediainfo_or_bdinfo_source": materials.get("mediainfo_or_bdinfo_source"),
            "mediainfo_or_bdinfo_length": materials.get("mediainfo_or_bdinfo_length"),
            "screenshot_file_count": materials.get("screenshot_file_count"),
            "image_host_count": materials.get("image_host_count"),
            "image_host_urls": _string_list(materials.get("image_host_urls")),
        },
    }


def _target_upload_payload_recovery_summary(artifacts: dict[str, Any] | None) -> dict[str, Any]:
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    preparation_audit = artifacts.get("target_preparation_audit") if isinstance(artifacts.get("target_preparation_audit"), dict) else {}
    payload_review = _summary_target_payload_review(artifacts, preparation_audit)
    description = payload_review.get("description") if isinstance(payload_review.get("description"), dict) else {}
    completeness = description.get("completeness") if isinstance(description.get("completeness"), dict) else {}
    recovery_missing = _string_list(payload_review.get("recovery_missing"))
    _extend_unique_string(recovery_missing, _string_list(completeness.get("recovery_missing")))
    _extend_unique_string(recovery_missing, _description_evidence_recovery_missing(description.get("evidence")))
    next_actions = _string_list(payload_review.get("next_actions"))
    _extend_unique_string(next_actions, _string_list(completeness.get("next_actions")))
    if not next_actions:
        next_actions = _target_preparation_missing_next_actions(recovery_missing)
    return {
        "present": bool(payload_review),
        "recovery_missing": recovery_missing,
        "next_actions": next_actions,
    }


def _target_preflight_gates(preflight: dict[str, Any] | None, preparation_audit: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(preflight, dict) or not preflight:
        return {
            "present": False,
            "status": None,
            "ready": False,
            "blockers": ["target upload preflight is missing."],
            "missing": ["target_upload_preflight"],
            "description_missing": [],
            "target_preparation_ready": False,
            "materials_ready": False,
            "metadata_ready": False,
            "assets_ready": False,
            "description_ready": False,
            "payload_ready": False,
            "payload_checks_ready": False,
            "description_checks_ready": False,
            "materials_ready_required": False,
            "torrent_file": None,
        }
    audit = preparation_audit if isinstance(preparation_audit, dict) else _target_preparation_audit_from_preflight(preflight)
    payload = audit.get("payload") if isinstance(audit.get("payload"), dict) else {}
    return {
        "present": True,
        "status": preflight.get("status"),
        "ready": preflight.get("status") == "ready" and bool(audit.get("ready")),
        "blockers": _string_list(preflight.get("blockers")) or _string_list(audit.get("blockers")),
        "missing": _string_list(audit.get("missing")),
        "description_missing": _string_list((audit.get("description") if isinstance(audit.get("description"), dict) else {}).get("missing")),
        "target_preparation_ready": bool(audit.get("ready")),
        "materials_ready": bool(audit.get("materials_ready")),
        "metadata_ready": bool(audit.get("metadata_ready")),
        "assets_ready": bool(audit.get("assets_ready")),
        "description_ready": bool(audit.get("description_ready")),
        "payload_ready": bool(audit.get("payload_ready")),
        "payload_checks_ready": bool(payload.get("payload_checks_ready")),
        "description_checks_ready": bool(payload.get("description_checks_ready")),
        "materials_ready_required": bool(payload.get("materials_ready_required")),
        "torrent_file": payload.get("torrent_file") if isinstance(payload.get("torrent_file"), dict) else None,
    }


def _summary_material_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    provided_diagnostics = payload.get("material_diagnostics") if isinstance(payload.get("material_diagnostics"), dict) else {}
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    if not artifacts and provided_diagnostics:
        return provided_diagnostics
    material_generation = artifacts.get("material_generation") if isinstance(artifacts.get("material_generation"), dict) else {}
    live_material_gate = artifacts.get("live_material_gate") if isinstance(artifacts.get("live_material_gate"), dict) else {}
    target_materials = artifacts.get("target_materials") if isinstance(artifacts.get("target_materials"), dict) else {}
    target_assets = target_materials.get("assets") if isinstance(target_materials.get("assets"), dict) else {}
    description_input_chain = target_materials.get("description") if isinstance(target_materials.get("description"), dict) else {}
    target_preparation_audit = artifacts.get("target_preparation_audit") if isinstance(artifacts.get("target_preparation_audit"), dict) else {}
    target_material_chain = _summary_target_material_chain(payload, artifacts)
    disc_structure = target_assets.get("disc_structure") if isinstance(target_assets.get("disc_structure"), dict) else {}
    description = target_preparation_audit.get("description") if isinstance(target_preparation_audit.get("description"), dict) else {}
    target_payload_review = _summary_target_payload_review(artifacts, target_preparation_audit)
    target_payload_description = target_payload_review.get("description") if isinstance(target_payload_review.get("description"), dict) else {}
    description_completeness = (
        target_payload_description.get("completeness")
        if isinstance(target_payload_description.get("completeness"), dict)
        else _description_completeness_summary(description)
    )
    description_completeness_present = bool(target_payload_review.get("present")) or bool(target_payload_description.get("completeness"))
    sections = {
        key: _summary_material_section(material_generation.get(key))
        for key in ("prerequisites", "metadata", "bdinfo", "mediainfo", "screenshots", "image_host")
        if isinstance(material_generation.get(key), dict)
    }
    if "metadata" not in sections:
        target_metadata_section = _summary_target_material_metadata_section(target_materials)
        if target_metadata_section:
            sections["metadata"] = target_metadata_section
    metadata_fields = _summary_metadata_field_status(sections.get("metadata") if isinstance(sections.get("metadata"), dict) else {})
    blockers: list[str] = []
    for key, section in sections.items():
        section_blockers = _string_list(section.get("all_blockers")) or _string_list(section.get("blockers"))
        for blocker in section_blockers:
            _append_unique_string(blockers, f"{key}: {blocker}")
    _extend_unique_string(blockers, _string_list(artifacts.get("target_materials_warnings")))
    image_host_evidence = _summary_image_host_evidence(sections, target_assets)
    material_missing = _summary_material_missing(artifacts, target_materials)
    critical_missing = _critical_material_missing(material_missing)
    material_evidence_blockers = _material_evidence_blockers(sections)
    material_evidence_ready = not material_evidence_blockers
    description_completeness_blockers = _description_completeness_blockers(description_completeness, description_completeness_present)
    description_completeness_ready = not description_completeness_blockers
    bdinfo_required = bool(disc_structure.get("bdmv"))
    target_materials_ready = artifacts.get("target_materials_ready") if "target_materials_ready" in artifacts else target_materials.get("ready")
    if target_materials_ready is None and "materials_ready" in target_preparation_audit:
        target_materials_ready = target_preparation_audit.get("materials_ready")
    target_preparation_ready = artifacts.get("target_preparation_ready")
    if target_preparation_ready is None and artifacts.get("target_preparation_missing"):
        target_preparation_ready = False
    ready_for_mteam_upload = bool(
        critical_missing == []
        and target_materials_ready is True
        and target_preparation_ready is True
        and material_evidence_ready
        and description_completeness_ready
    )
    upload_material_gates = {
        "critical_ready": not critical_missing,
        "target_materials_ready": target_materials_ready,
        "target_preparation_ready": target_preparation_ready,
        "material_evidence_ready": material_evidence_ready,
        "description_completeness_ready": description_completeness_ready,
    }
    critical_domains = _material_critical_domains(critical_missing)
    critical_path = _material_critical_path_summary(
        critical_domains=critical_domains,
        target_materials_ready=target_materials_ready,
        target_preparation_ready=target_preparation_ready,
        description_ready=target_preparation_audit.get("description_ready"),
        material_missing=material_missing,
    )
    description_evidence = (
        description.get("evidence")
        if isinstance(description.get("evidence"), dict)
        else target_payload_description.get("evidence")
        if isinstance(target_payload_description.get("evidence"), dict)
        else _target_material_chain_evidence(target_material_chain)
    )
    metadata_chain_evidence = description_evidence.get("metadata_chain") if isinstance(description_evidence.get("metadata_chain"), dict) else {}
    media_info_chain_evidence = description_evidence.get("media_info_chain") if isinstance(description_evidence.get("media_info_chain"), dict) else {}
    screenshot_chain_evidence = description_evidence.get("screenshot_chain") if isinstance(description_evidence.get("screenshot_chain"), dict) else {}
    return {
        "present": bool(material_generation or target_materials or target_preparation_audit or target_material_chain),
        "generation_present": bool(material_generation),
        "target_materials_present": bool(target_materials),
        "generation_ready": all(bool(section.get("ok")) for section in sections.values()) if sections else None,
        "target_materials_ready": target_materials_ready,
        "target_preparation_ready": target_preparation_ready,
        "target_materials_missing": _string_list(artifacts.get("target_materials_missing") or target_materials.get("missing")),
        "target_preparation_missing": _string_list(artifacts.get("target_preparation_missing")),
        "ready_for_mteam_upload": ready_for_mteam_upload,
        "upload_material_gates": upload_material_gates,
        "upload_material_blockers": _material_upload_blockers(
            upload_material_gates,
            material_missing,
            critical_missing,
            material_evidence_blockers,
            description_completeness_blockers,
        ),
        "live_gate": _material_live_gate_summary(live_material_gate),
        "critical_ready": not critical_missing,
        "critical_missing": critical_missing,
        "critical_domains": critical_domains,
        "critical_path": critical_path,
        "target_material_critical_path": target_materials.get("critical_path") if isinstance(target_materials.get("critical_path"), dict) else {},
        "target_material_recovery_plan": target_materials.get("recovery_plan") if isinstance(target_materials.get("recovery_plan"), dict) else {},
        "description_input_chain": description_input_chain,
        "description_input_chain_ready": description_input_chain.get("ready") if isinstance(description_input_chain.get("ready"), bool) else None,
        "description_input_chain_missing": _string_list(description_input_chain.get("missing")),
        "description_input_chain_next_actions": _string_list(description_input_chain.get("next_actions")),
        "disc_structure": disc_structure,
        "bdinfo_required": bdinfo_required,
        "media_info_requirement": "bdinfo" if bdinfo_required else "mediainfo_or_bdinfo",
        "metadata_fields": metadata_fields,
        "readiness": {
            "material_description_metadata_chain_ready": metadata_chain_evidence.get("ready") if isinstance(metadata_chain_evidence.get("ready"), bool) else None,
            "material_description_metadata_chain_missing": _description_chain_recovery_missing("metadata_chain", metadata_chain_evidence),
            "material_description_media_info_chain_ready": media_info_chain_evidence.get("ready") if isinstance(media_info_chain_evidence.get("ready"), bool) else None,
            "material_description_media_info_chain_missing": _description_chain_recovery_missing("media_info_chain", media_info_chain_evidence),
            "material_description_screenshot_chain_ready": screenshot_chain_evidence.get("ready") if isinstance(screenshot_chain_evidence.get("ready"), bool) else None,
            "material_description_screenshot_chain_missing": _description_chain_recovery_missing("screenshot_chain", screenshot_chain_evidence),
            "material_description_input_chain_ready": description_input_chain.get("ready") if isinstance(description_input_chain.get("ready"), bool) else None,
            "material_description_input_chain_missing": _string_list(description_input_chain.get("missing")),
        },
        "description": {
            "ready": target_preparation_audit.get("description_ready"),
            "input_chain": description_input_chain,
            "path": description.get("path"),
            "exists": description.get("exists"),
            "char_length": description.get("char_length"),
            "expected_length": description.get("expected_length"),
            "has_ptgen_description": description.get("has_ptgen_description"),
            "ptgen_description_length": description.get("ptgen_description_length"),
            "has_external_ids": description.get("has_external_ids"),
            "external_id_readiness": description.get("external_id_readiness") if isinstance(description.get("external_id_readiness"), dict) else {},
            "external_id_missing": _string_list(description.get("external_id_missing")),
            "external_links": description.get("external_links") if isinstance(description.get("external_links"), dict) else {},
            "evidence": description_evidence,
            "has_mediainfo_or_bdinfo": description.get("has_mediainfo_or_bdinfo"),
            "media_info": description.get("media_info") if isinstance(description.get("media_info"), dict) else {},
            "has_screenshot_bbcode": description.get("has_screenshot_bbcode"),
            "bbcode_image_count": description.get("bbcode_image_count"),
            "bbcode_image_urls": _string_list(description.get("bbcode_image_urls")),
            "screenshot_coverage": description.get("screenshot_coverage") if isinstance(description.get("screenshot_coverage"), dict) else {},
            "missing": _string_list(description.get("missing")),
            "completeness": description_completeness,
        },
        "sections": sections,
        "image_host_urls": _image_host_url_summary(image_host_evidence.get("items") if isinstance(image_host_evidence, dict) else []),
        "blockers": blockers,
    }


def _summary_material_missing(artifacts: dict[str, Any], target_materials: dict[str, Any]) -> list[str]:
    missing = _string_list(artifacts.get("target_materials_missing") or target_materials.get("missing"))
    _extend_unique_string(missing, _string_list(artifacts.get("target_preparation_missing")))
    return missing


def _material_live_gate_summary(live_material_gate: dict[str, Any]) -> dict[str, Any]:
    if not live_material_gate:
        return {"present": False}
    return {
        "present": True,
        "ready": live_material_gate.get("ready") if isinstance(live_material_gate.get("ready"), bool) else None,
        "message": live_material_gate.get("message"),
        "ready_for_mteam_upload": live_material_gate.get("ready_for_mteam_upload") if isinstance(live_material_gate.get("ready_for_mteam_upload"), bool) else None,
        "gates": live_material_gate.get("gates") if isinstance(live_material_gate.get("gates"), dict) else {},
        "missing": _string_list(live_material_gate.get("missing")),
        "blockers": _string_list(live_material_gate.get("blockers")),
        "next_actions": _string_list(live_material_gate.get("next_actions")),
        "critical_path": live_material_gate.get("critical_path") if isinstance(live_material_gate.get("critical_path"), dict) else {},
        "readiness": live_material_gate.get("readiness") if isinstance(live_material_gate.get("readiness"), dict) else {},
    }


def _summary_target_payload_review(artifacts: dict[str, Any], target_preparation_audit: dict[str, Any]) -> dict[str, Any]:
    payload_review = artifacts.get("target_payload_review") if isinstance(artifacts.get("target_payload_review"), dict) else {}
    if payload_review:
        return payload_review
    payload_review = target_preparation_audit.get("payload_review") if isinstance(target_preparation_audit.get("payload_review"), dict) else {}
    return payload_review


def _description_completeness_blockers(completeness: dict[str, Any], present: bool) -> list[str]:
    if not present or completeness.get("ready") is True:
        return []
    missing = _string_list(completeness.get("recovery_missing")) or _string_list(completeness.get("missing"))
    return [f"description completeness missing: {item}" for item in missing]


def _material_evidence_blockers(sections: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    evidence_fields = {
        "bdinfo": ("bdinfo_file_evidence", "raw_bdinfo_file_evidence"),
        "mediainfo": ("mediainfo_file_evidence", "mediainfo_summary_file_evidence", "mediainfo_json_file_evidence"),
        "screenshots": ("screenshot_files_evidence",),
        "image_host": ("image_host_file_evidence",),
    }
    for section_name, keys in evidence_fields.items():
        section = sections.get(section_name) if isinstance(sections.get(section_name), dict) else {}
        for key in keys:
            if key not in section:
                continue
            ready = _material_evidence_all_files_exist(section.get(key))
            if ready is not True:
                _append_unique_string(blockers, f"material evidence invalid: {section_name}.{key.removesuffix('_evidence')}")
    return blockers


def _material_upload_blockers(
    gates: dict[str, Any],
    material_missing: list[str],
    critical_missing: list[str],
    material_evidence_blockers: list[str] | None = None,
    description_completeness_blockers: list[str] | None = None,
) -> list[str]:
    blockers: list[str] = []
    if gates.get("critical_ready") is not True:
        _extend_unique_string(blockers, [f"critical material missing: {item}" for item in critical_missing])
    if gates.get("target_materials_ready") is not True:
        _append_unique_string(blockers, "target materials are not ready")
    if gates.get("target_preparation_ready") is not True:
        _append_unique_string(blockers, "target preparation is not ready")
    if gates.get("material_evidence_ready") is not True:
        _extend_unique_string(blockers, _string_list(material_evidence_blockers))
    if gates.get("description_completeness_ready") is not True:
        _extend_unique_string(blockers, _string_list(description_completeness_blockers))
    if not blockers:
        return []
    _extend_unique_string(blockers, [f"material missing: {item}" for item in material_missing if item not in critical_missing])
    return blockers


def _summary_target_material_metadata_section(target_materials: dict[str, Any]) -> dict[str, Any]:
    metadata = target_materials.get("metadata") if isinstance(target_materials.get("metadata"), dict) else {}
    if not metadata:
        return {}
    return {
        "ok": bool(target_materials.get("metadata_ready")),
        "ready": metadata.get("enrichment_ready"),
        "status": metadata.get("enrichment_status"),
        "sources": metadata.get("sources") if isinstance(metadata.get("sources"), list) else [],
        "applied": metadata.get("applied") if isinstance(metadata.get("applied"), dict) else {},
        "readiness": metadata.get("readiness") if isinstance(metadata.get("readiness"), dict) else {},
        "field_evidence": metadata.get("field_evidence") if isinstance(metadata.get("field_evidence"), dict) else {},
        "missing": _string_list(metadata.get("missing")),
        "blockers": _string_list(metadata.get("blockers")),
        "readiness_blockers": _string_list(metadata.get("readiness_blockers")),
        "all_blockers": [*_string_list(metadata.get("blockers")), *_string_list(metadata.get("readiness_blockers"))],
        "imdb_id": metadata.get("imdb_id"),
        "tmdb_id": metadata.get("tmdb_id"),
        "douban_id": metadata.get("douban_id"),
        "douban_url": metadata.get("douban_url"),
        "ptgen_description_length": metadata.get("ptgen_description_length"),
    }


def _summary_metadata_field_status(metadata: dict[str, Any]) -> dict[str, Any]:
    readiness = metadata.get("readiness") if isinstance(metadata.get("readiness"), dict) else {}
    field_evidence = metadata.get("field_evidence") if isinstance(metadata.get("field_evidence"), dict) else {}
    field_values = {
        "imdb_id": metadata.get("imdb_id"),
        "tmdb_id": metadata.get("tmdb_id"),
        "douban_id": metadata.get("douban_id"),
        "douban_url": metadata.get("douban_url"),
        "ptgen_description": {
            "length": metadata.get("ptgen_description_length"),
        },
    }
    fields: dict[str, Any] = {}
    for key, value in field_values.items():
        field_readiness = readiness.get(key) if isinstance(readiness.get(key), dict) else {}
        evidence = field_evidence.get(key) if isinstance(field_evidence.get(key), dict) else {}
        ready = field_readiness.get("ready")
        required = field_readiness.get("required")
        source = field_readiness.get("source")
        if key == "ptgen_description":
            length = value.get("length") if isinstance(value, dict) else evidence.get("length")
            if ready is None:
                ready = evidence.get("ready")
            if required is None:
                required = evidence.get("required")
            if source is None:
                source = evidence.get("source")
            if ready is None:
                ready = bool(length)
            fields[key] = {
                "ready": ready if isinstance(ready, bool) else None,
                "required": required if isinstance(required, bool) else None,
                "source": source,
                "length": length,
            }
            continue
        if value is None:
            value = evidence.get("value")
        if ready is None:
            ready = evidence.get("ready")
        if required is None:
            required = evidence.get("required")
        if source is None:
            source = evidence.get("source")
        if ready is None:
            ready = bool(value)
        fields[key] = {
            "ready": ready if isinstance(ready, bool) else None,
            "required": required if isinstance(required, bool) else None,
            "source": source,
            "value": value,
        }
    return fields


def _description_completeness_summary(description: dict[str, Any]) -> dict[str, Any]:
    external_id_readiness = description.get("external_id_readiness") if isinstance(description.get("external_id_readiness"), dict) else {}
    screenshot_coverage = description.get("screenshot_coverage") if isinstance(description.get("screenshot_coverage"), dict) else {}
    checks = [
        {"name": "ptgen_description", "ready": _description_bool(description.get("has_ptgen_description"))},
        {"name": "external_ids", "ready": _description_external_ids_ready(description, external_id_readiness)},
        {"name": "mediainfo_or_bdinfo", "ready": _description_bool(description.get("has_mediainfo_or_bdinfo"))},
        {"name": "screenshot_bbcode", "ready": _description_bool(description.get("has_screenshot_bbcode"))},
        {"name": "screenshot_coverage", "ready": _description_bool(screenshot_coverage.get("ready"))},
    ]
    missing = [str(check["name"]) for check in checks if check.get("ready") is not True]
    recovery_missing = [_description_completeness_recovery_key(item) for item in missing]
    return {
        "ready": not missing,
        "missing": missing,
        "recovery_missing": recovery_missing,
        "next_actions": _target_preparation_missing_next_actions(recovery_missing),
        "checks": checks,
    }


def _description_completeness_recovery_key(key: str) -> str:
    return {
        "ptgen_description": "description.ptgen_description",
        "external_ids": "description.external_ids",
        "mediainfo_or_bdinfo": "description.mediainfo_or_bdinfo",
        "screenshot_bbcode": "description.screenshot_bbcode",
        "screenshot_coverage": "description.screenshot_coverage",
    }.get(key, f"description.{key}")


def _description_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _description_external_ids_ready(description: dict[str, Any], external_id_readiness: dict[str, Any]) -> bool | None:
    if isinstance(description.get("has_external_ids"), bool):
        return bool(description.get("has_external_ids"))
    if external_id_readiness:
        return all(external_id_readiness.get(name) is True for name in ("imdb", "tmdb", "douban"))
    return None


def _summary_material_section(section: Any) -> dict[str, Any]:
    if not isinstance(section, dict):
        return {}
    payload = {
        "ok": section.get("ok") if isinstance(section.get("ok"), bool) else None,
        "skipped": bool(section.get("skipped")),
        "message": section.get("message"),
        "status": section.get("status"),
        "blockers": _string_list(section.get("blockers")),
    }
    for key in (
        "ready",
        "missing",
        "readiness",
        "field_evidence",
        "sources",
        "applied",
        "blockers",
        "readiness_blockers",
        "all_blockers",
        "imdb_id",
        "tmdb_id",
        "douban_id",
        "douban_url",
        "ptgen_description_length",
        "bdinfo_file",
        "bdinfo_file_evidence",
        "raw_bdinfo_file",
        "raw_bdinfo_file_evidence",
        "mediainfo_file",
        "mediainfo_file_evidence",
        "mediainfo_summary_file",
        "mediainfo_summary_file_evidence",
        "mediainfo_json_file",
        "mediainfo_json_file_evidence",
        "screenshot_files",
        "screenshot_files_evidence",
        "image_host_file",
        "image_host_file_evidence",
        "count",
        "requested_count",
        "host",
        "items",
        "checks",
    ):
        if key in section:
            payload[key] = section.get(key)
    if isinstance(payload.get("items"), list):
        payload["urls"] = _image_host_url_summary(payload["items"])
    return payload


def _summary_image_host_evidence(sections: dict[str, Any], target_assets: dict[str, Any]) -> dict[str, Any]:
    generated = sections.get("image_host") if isinstance(sections.get("image_host"), dict) else {}
    if isinstance(generated.get("items"), list) and generated.get("items"):
        return generated
    target_image_hosts = target_assets.get("image_hosts") if isinstance(target_assets.get("image_hosts"), dict) else {}
    return target_image_hosts or generated


def _image_host_url_summary(items: Any) -> dict[str, Any]:
    raw_urls: list[str] = []
    img_urls: list[str] = []
    web_urls: list[str] = []
    item_count = 0
    valid_count = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        item_count += 1
        raw_url = _nonempty_str(item.get("raw_url") or item.get("url"))
        img_url = _nonempty_str(item.get("img_url") or raw_url)
        web_url = _nonempty_str(item.get("web_url") or item.get("url_viewer") or raw_url or img_url)
        if img_url and web_url:
            valid_count += 1
        if raw_url:
            _append_unique_string(raw_urls, raw_url)
        if img_url:
            _append_unique_string(img_urls, img_url)
        if web_url:
            _append_unique_string(web_urls, web_url)
    return {
        "raw_urls": raw_urls,
        "img_urls": img_urls,
        "web_urls": web_urls,
        "item_count": item_count,
        "valid_count": valid_count,
        "invalid_count": item_count - valid_count,
    }


def _nonempty_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _summary_flow_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    flow = payload.get("flow_check")
    if not isinstance(flow, dict):
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        flow = summary.get("flow")
    if not isinstance(flow, dict):
        return {
            "present": False,
            "ready": None,
            "source_capability": None,
            "target_capabilities": [],
            "credential_requirements": [],
        }
    return {
        "present": True,
        "ready": flow.get("ready") if isinstance(flow.get("ready"), bool) else None,
        "source_tracker": flow.get("source_tracker"),
        "source_torrent_id": flow.get("source_torrent_id"),
        "target_trackers": flow.get("target_trackers") if isinstance(flow.get("target_trackers"), list) else [],
        "source_capability": flow.get("source_capability") if isinstance(flow.get("source_capability"), dict) else None,
        "target_capabilities": flow.get("target_capabilities") if isinstance(flow.get("target_capabilities"), list) else [],
        "credential_requirements": _string_list(flow.get("credential_requirements")),
    }


def _summary_closure_modes(payload: dict[str, Any]) -> dict[str, str | None]:
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    evidence_source = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
    evidence_target = evidence.get("target") if isinstance(evidence.get("target"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    summary_source = summary.get("source") if isinstance(summary.get("source"), dict) else {}
    summary_target = summary.get("target") if isinstance(summary.get("target"), dict) else {}
    return {
        "source": _string_or_none(evidence_source.get("mode")) or _string_or_none(summary_source.get("mode")) or _string_or_none(payload.get("source_mode")),
        "target": _string_or_none(evidence_target.get("mode")) or _string_or_none(summary_target.get("mode")) or _string_or_none(summary.get("mode")) or _string_or_none(payload.get("target_mode")) or _string_or_none(payload.get("mode")),
    }


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _summary_qbit_wait_mismatches(qbit_wait_diagnostics: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for scope, diagnostics in qbit_wait_diagnostics.items():
        if not isinstance(diagnostics, dict) or diagnostics.get("request_mismatch") is not True:
            continue
        if diagnostics.get("requested_hash_matched") is False:
            mismatches.append(f"{scope}.requested_hash")
        if diagnostics.get("requested_content_path_matched") is False:
            mismatches.append(f"{scope}.requested_content_path")
    return mismatches


def _qbit_wait_summary_fields(payload: dict[str, Any]) -> dict[str, Any]:
    qbit_wait_diagnostics = _summary_qbit_wait_diagnostics(payload)
    qbit_wait_mismatches = _summary_qbit_wait_mismatches(qbit_wait_diagnostics)
    return {
        "qbit_wait_diagnostics": qbit_wait_diagnostics,
        "qbit_wait_mismatch": bool(qbit_wait_mismatches),
        "qbit_wait_mismatches": qbit_wait_mismatches,
        "qbit_wait_retry_hints": _summary_qbit_wait_retry_hints(qbit_wait_diagnostics),
    }


def _qbit_wait_mismatch_blockers(diagnostics: dict[str, Any]) -> list[str]:
    return [f"qBittorrent wait mismatch: {name}" for name in _string_list(diagnostics.get("qbit_wait_mismatches"))]


def _summary_qbit_wait_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    evidence_source = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
    evidence_target = evidence.get("target") if isinstance(evidence.get("target"), dict) else {}
    closure = payload.get("closure") if isinstance(payload.get("closure"), dict) else {}
    closure_source = closure.get("source") if isinstance(closure.get("source"), dict) else {}
    closure_target = closure.get("target") if isinstance(closure.get("target"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    summary_source = summary.get("source") if isinstance(summary.get("source"), dict) else {}
    summary_target = summary.get("target") if isinstance(summary.get("target"), dict) else {}
    diagnostics: dict[str, Any] = {}

    source_wait = (
        _summary_qbit_wait_from(evidence_source, "source_wait")
        or _summary_qbit_wait_from(closure_source, "source_wait")
        or _summary_qbit_wait_from(summary_source, "source_wait")
    )
    if source_wait:
        diagnostics["source"] = source_wait

    uploaded_wait = (
        _summary_qbit_wait_from(evidence_target, "uploaded_wait")
        or _summary_qbit_wait_from(closure_target, "uploaded_wait")
        or _summary_qbit_wait_from(summary, "uploaded_wait")
        or _summary_qbit_wait_from(summary_target, "uploaded_wait")
    )
    if uploaded_wait:
        diagnostics["uploaded"] = uploaded_wait

    return diagnostics


def _summary_qbit_wait_from(container: dict[str, Any], fallback_key: str) -> dict[str, Any] | None:
    qbit_closure = container.get("qbit_closure") if isinstance(container.get("qbit_closure"), dict) else {}
    wait_result = qbit_closure.get("wait") if isinstance(qbit_closure.get("wait"), dict) else container.get(fallback_key)
    if not isinstance(wait_result, dict):
        return None
    query = wait_result.get("query") if isinstance(wait_result.get("query"), dict) else {}
    verification = wait_result.get("completion_verification") if isinstance(wait_result.get("completion_verification"), dict) else {}
    requested_hash_matched = verification.get("requested_hash_matched")
    requested_content_path_matched = verification.get("requested_content_path_matched")
    return {
        "complete": bool(wait_result.get("complete")),
        "requested_hash": _normalize_torrent_hash(query.get("torrent_hash")),
        "requested_content_path": query.get("content_path"),
        "requested_save_path": query.get("save_path"),
        "requested_timeout": query.get("timeout"),
        "requested_interval": query.get("interval"),
        "matched_count": verification.get("matched_count", wait_result.get("matched_count")),
        "complete_count": verification.get("complete_count"),
        "any_complete": verification.get("any_complete"),
        "all_matches_complete": verification.get("all_matches_complete"),
        "seeding_state_count": verification.get("seeding_state_count"),
        "requested_hash_matched": requested_hash_matched,
        "requested_content_path_matched": requested_content_path_matched,
        "request_mismatch": requested_hash_matched is False or requested_content_path_matched is False,
        "observed_hashes": verification.get("observed_hashes", []),
        "observed_content_paths": verification.get("observed_content_paths", []),
        "observed_save_paths": verification.get("observed_save_paths", []),
        "observed_states": verification.get("observed_states", []),
        "observed_progress": verification.get("observed_progress", []),
        "blockers": _string_list(wait_result.get("blockers")),
    }


def _summary_qbit_wait_retry_hints(qbit_wait_diagnostics: dict[str, Any]) -> dict[str, Any]:
    hints: dict[str, Any] = {}
    for scope, diagnostics in qbit_wait_diagnostics.items():
        if not isinstance(diagnostics, dict):
            continue
        request_mismatch = diagnostics.get("request_mismatch") is True
        observed_hash = _first_string(diagnostics.get("observed_hashes"))
        observed_content_path = _first_string(diagnostics.get("observed_content_paths"))
        observed_save_path = _first_string(diagnostics.get("observed_save_paths"))
        observed_candidates = _qbit_wait_observed_candidates(diagnostics)
        suggested_hash = observed_hash if diagnostics.get("requested_hash_matched") is False or not diagnostics.get("requested_hash") else diagnostics.get("requested_hash")
        suggested_content_path = (
            observed_content_path
            if diagnostics.get("requested_hash_matched") is False or diagnostics.get("requested_content_path_matched") is False or not diagnostics.get("requested_content_path")
            else diagnostics.get("requested_content_path")
        )
        hints[str(scope)] = {
            "retry_recommended": request_mismatch,
            "suggested_torrent_hash": suggested_hash,
            "suggested_content_path": suggested_content_path,
            "suggested_save_path": observed_save_path or diagnostics.get("requested_save_path"),
            "observed_candidate_count": len(observed_candidates),
            "observed_candidates": observed_candidates,
            "reason": _qbit_wait_retry_reason(str(scope), diagnostics) if request_mismatch else None,
        }
    return hints


def _qbit_wait_observed_candidates(diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    observed_hashes = diagnostics.get("observed_hashes") if isinstance(diagnostics.get("observed_hashes"), list) else []
    observed_content_paths = diagnostics.get("observed_content_paths") if isinstance(diagnostics.get("observed_content_paths"), list) else []
    observed_save_paths = diagnostics.get("observed_save_paths") if isinstance(diagnostics.get("observed_save_paths"), list) else []
    observed_states = diagnostics.get("observed_states") if isinstance(diagnostics.get("observed_states"), list) else []
    observed_progress = diagnostics.get("observed_progress") if isinstance(diagnostics.get("observed_progress"), list) else []
    candidate_count = max(len(observed_hashes), len(observed_content_paths), len(observed_save_paths), len(observed_states), len(observed_progress))
    candidates: list[dict[str, Any]] = []
    for index in range(candidate_count):
        candidate = {
            "hash": _list_value(observed_hashes, index),
            "content_path": _list_value(observed_content_paths, index),
            "save_path": _list_value(observed_save_paths, index),
            "state": _list_value(observed_states, index),
            "progress": _list_value(observed_progress, index),
        }
        candidates.append({key: value for key, value in candidate.items() if value is not None})
    return candidates


def _list_value(items: list[Any], index: int) -> Any:
    if index >= len(items):
        return None
    value = items[index]
    return value if isinstance(value, (str, int, float, bool)) else None


def _qbit_wait_retry_reason(scope: str, diagnostics: dict[str, Any]) -> str:
    mismatches: list[str] = []
    if diagnostics.get("requested_hash_matched") is False:
        mismatches.append("requested_hash")
    if diagnostics.get("requested_content_path_matched") is False:
        mismatches.append("requested_content_path")
    suffix = ", ".join(mismatches) if mismatches else "requested wait query"
    return f"{scope} qBittorrent wait matched a different torrent/content than {suffix}."


def _first_string(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, str) and item:
            return item
    return None


def _closure_status_summary(payload: dict[str, Any]) -> dict[str, Any]:
    closure = payload.get("closure") if isinstance(payload.get("closure"), dict) else {}
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    closure_source = closure.get("source") if isinstance(closure.get("source"), dict) else {}
    closure_target = closure.get("target") if isinstance(closure.get("target"), dict) else {}
    evidence_source = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
    evidence_target = evidence.get("target") if isinstance(evidence.get("target"), dict) else {}
    if isinstance(payload.get("closure_audit"), dict):
        closure_audit = payload["closure_audit"]
    elif closure or evidence:
        closure_audit = _pipeline_closure_audit(closure, evidence)
    else:
        closure_audit = {}
    qbit_wait_diagnostics = _summary_qbit_wait_diagnostics(payload)
    qbit_wait_mismatches = _summary_qbit_wait_mismatches(qbit_wait_diagnostics)
    blockers = _string_list(payload.get("blockers"))
    closure_blockers = _string_list(closure.get("blockers"))
    audit_missing = _string_list(closure_audit.get("missing")) if isinstance(closure_audit, dict) else []
    ready = bool(payload.get("ready"))
    closure_complete = bool(closure.get("complete"))
    audit_ready = bool(closure_audit.get("ready")) if isinstance(closure_audit, dict) else False
    payload_complete = bool(payload.get("complete")) if "complete" in payload else closure_complete
    complete = payload_complete and ready and closure_complete and audit_ready and not blockers and not qbit_wait_mismatches
    return {
        "complete": complete,
        "ready": ready,
        "pipeline_status": payload.get("status"),
        "pipeline_blockers": blockers,
        "closure_complete": closure_complete,
        "closure_blockers": closure_blockers,
        "audit_ready": audit_ready,
        "audit_missing": audit_missing,
        "qbit_wait_mismatch": bool(qbit_wait_mismatches),
        "qbit_wait_mismatches": qbit_wait_mismatches,
        "source": {
            "ready": bool(closure_source.get("ready") or evidence_source.get("ready")),
            "mode": evidence_source.get("mode"),
            "hash_consistent": bool(closure_source.get("hash_consistent") or evidence_source.get("hash_consistent")),
            "wait_evidence": _wait_result_completed(closure_source.get("source_wait")) or bool(evidence_source.get("source_wait_evidence")),
            "injection_verified": bool(closure_source.get("injection_verified") or evidence_source.get("injection_verified")),
        },
        "target": {
            "ready": bool(
                evidence_target.get("ready")
                or (
                    closure_target.get("prepared")
                    and closure_target.get("uploaded")
                    and closure_target.get("downloaded")
                    and closure_target.get("injected")
                    and closure_target.get("seeding")
                )
            ),
            "mode": evidence_target.get("mode"),
            "hash_consistent": bool(closure_target.get("hash_consistent") or evidence_target.get("hash_consistent")),
            "duplicate_clean": bool(closure_target.get("duplicate_clean") or evidence_target.get("duplicate_clean")),
            "rule_obligations_ready": _rule_obligations_artifact_ready(
                closure_target.get("rule_obligations") if isinstance(closure_target.get("rule_obligations"), dict) else evidence_target.get("rule_obligations")
            ),
            "uploaded_wait_evidence": _wait_result_completed(closure_target.get("uploaded_wait")) or bool(evidence_target.get("uploaded_wait_evidence")),
            "injection_verified": bool(closure_target.get("injection_verified") or evidence_target.get("injection_verified")),
        },
    }


def _pipeline_closure_review(payload: dict[str, Any], artifacts: dict[str, Any] | None = None) -> dict[str, Any]:
    artifacts = artifacts if isinstance(artifacts, dict) else payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    closure_status = payload.get("closure_status") if isinstance(payload.get("closure_status"), dict) else _closure_status_summary(payload)
    supplied_audit = payload.get("closure_audit") if isinstance(payload.get("closure_audit"), dict) else {}
    closure = payload.get("closure") if isinstance(payload.get("closure"), dict) else None
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else None
    closure_source = closure.get("source") if isinstance(closure, dict) and isinstance(closure.get("source"), dict) else {}
    evidence_source = evidence.get("source") if isinstance(evidence, dict) and isinstance(evidence.get("source"), dict) else {}
    target_preparation = artifacts.get("target_preparation_audit") if isinstance(artifacts.get("target_preparation_audit"), dict) else {}
    target_description = target_preparation.get("description") if isinstance(target_preparation.get("description"), dict) else {}
    computed_audit = _pipeline_closure_audit(closure, evidence) if closure or evidence else {}
    closure_audit = computed_audit or supplied_audit
    checks: dict[str, bool] = {
        str(item.get("name")): bool(item.get("ok"))
        for item in closure_audit.get("items", [])
        if isinstance(item, dict) and item.get("name")
    } if isinstance(closure_audit.get("items"), list) else {}
    if isinstance(supplied_audit.get("items"), list):
        checks.update({str(item.get("name")): bool(item.get("ok")) for item in supplied_audit["items"] if isinstance(item, dict) and item.get("name")})
    missing = [name for name, ok in checks.items() if not ok] or _string_list(closure_audit.get("missing"))
    _extend_unique_string(missing, [name for name in _string_list(supplied_audit.get("missing")) if not checks.get(name)])
    if closure_status.get("qbit_wait_mismatch"):
        _append_unique_string(missing, "qbit_wait_mismatch")
    if _string_list(closure_status.get("pipeline_blockers")):
        _append_unique_string(missing, "pipeline.blockers")
    if _string_list(closure_status.get("closure_blockers")):
        _append_unique_string(missing, "closure.blockers")
    source_torrent_artifact = artifacts.get("source_torrent_file_artifact") if isinstance(artifacts.get("source_torrent_file_artifact"), dict) else {}
    return {
        "complete": bool(closure_status.get("complete")) and not missing,
        "missing": missing,
        "checks": checks,
        "source": {
            "mode": closure_status.get("source", {}).get("mode") if isinstance(closure_status.get("source"), dict) else None,
            "ready": closure_status.get("source", {}).get("ready") if isinstance(closure_status.get("source"), dict) else None,
            "hash_consistent": closure_status.get("source", {}).get("hash_consistent") if isinstance(closure_status.get("source"), dict) else None,
            "wait_evidence": closure_status.get("source", {}).get("wait_evidence") if isinstance(closure_status.get("source"), dict) else None,
            "injection_verified": closure_status.get("source", {}).get("injection_verified") if isinstance(closure_status.get("source"), dict) else None,
            "torrent_hash": artifacts.get("source_torrent_hash"),
            "torrent_file": artifacts.get("source_torrent_file"),
            "torrent_file_evidence": artifacts.get("source_torrent_file_evidence"),
            "torrent_file_artifact": source_torrent_artifact,
            "injected_torrent_hash": artifacts.get("source_injected_torrent_hash"),
            "injection_visible_in_client": artifacts.get("source_injection_visible_in_client"),
            "save_path": artifacts.get("source_save_path"),
            "qbit_category": artifacts.get("source_qbit_category"),
            "qbit_tags": artifacts.get("source_qbit_tags"),
            "paused": artifacts.get("source_paused"),
            "content_path": closure_source.get("content_path") or evidence_source.get("content_path"),
        },
        "target": {
            "mode": closure_status.get("target", {}).get("mode") if isinstance(closure_status.get("target"), dict) else None,
            "ready": closure_status.get("target", {}).get("ready") if isinstance(closure_status.get("target"), dict) else None,
            "hash_consistent": closure_status.get("target", {}).get("hash_consistent") if isinstance(closure_status.get("target"), dict) else None,
            "duplicate_clean": closure_status.get("target", {}).get("duplicate_clean") if isinstance(closure_status.get("target"), dict) else None,
            "rule_obligations_ready": closure_status.get("target", {}).get("rule_obligations_ready") if isinstance(closure_status.get("target"), dict) else None,
            "uploaded_wait_evidence": closure_status.get("target", {}).get("uploaded_wait_evidence") if isinstance(closure_status.get("target"), dict) else None,
            "injection_verified": closure_status.get("target", {}).get("injection_verified") if isinstance(closure_status.get("target"), dict) else None,
            "uploaded_torrent_id": artifacts.get("uploaded_torrent_id"),
            "uploaded_torrent_hash": artifacts.get("uploaded_torrent_hash"),
            "uploaded_torrent_file": artifacts.get("uploaded_torrent_file"),
            "injected_torrent_hash": artifacts.get("injected_torrent_hash"),
            "uploaded_save_path": artifacts.get("uploaded_save_path"),
            "materials_ready": target_preparation.get("materials_ready"),
            "metadata_ready": target_preparation.get("metadata_ready"),
            "assets_ready": target_preparation.get("assets_ready"),
            "description_ready": target_preparation.get("description_ready"),
            "preparation_ready": artifacts.get("target_preparation_ready"),
            "preparation_missing": _string_list(artifacts.get("target_preparation_missing") or target_preparation.get("missing")),
            "description": {
                "path": target_description.get("path"),
                "exists": target_description.get("exists"),
                "char_length": target_description.get("char_length"),
                "expected_length": target_description.get("expected_length"),
                "has_ptgen_description": target_description.get("has_ptgen_description"),
                "ptgen_description_length": target_description.get("ptgen_description_length"),
                "has_external_ids": target_description.get("has_external_ids"),
                "external_links": target_description.get("external_links") if isinstance(target_description.get("external_links"), dict) else {},
                "has_mediainfo_or_bdinfo": target_description.get("has_mediainfo_or_bdinfo"),
                "has_screenshot_bbcode": target_description.get("has_screenshot_bbcode"),
                "bbcode_image_count": target_description.get("bbcode_image_count"),
                "bbcode_image_urls": _string_list(target_description.get("bbcode_image_urls")),
                "missing": _string_list(target_description.get("missing")),
            },
        },
    }


def _summary_check_result(payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "blocked")
    blockers = _string_list(payload.get("blockers"))
    summary_file = str(payload.get("summary_file") or "")
    automation_handoff = payload.get("automation_handoff") if isinstance(payload.get("automation_handoff"), dict) else _summary_automation_handoff(summary_file)
    next_command = payload.get("next_command")
    next_command_argv = _summary_next_command_raw_argv(payload.get("next_command_argv")) if payload.get("next_command_argv") else _summary_next_command_raw_argv(str(next_command)) if next_command else None
    next_command_metadata = _summary_next_command_metadata(next_command_argv)
    material_recovery_run_blocker = _summary_material_recovery_command_run_blocker(payload, str(payload.get("next_stage") or ""))
    candidate_commands = payload.get("candidate_commands") if isinstance(payload.get("candidate_commands"), list) else _summary_candidate_commands(payload)
    first_runnable_command = _first_runnable_candidate_command(candidate_commands)
    rejected_command_summary = _rejected_candidate_command_summary(candidate_commands)
    next_command_placeholder = bool(next_command_metadata["placeholder"])
    next_command_ready = bool(next_command) and not next_command_placeholder
    next_command_run_allowed = bool(next_command_ready and next_command_metadata["run_allowed"] and not material_recovery_run_blocker)
    if status == "ok":
        automation_action = "complete"
    elif payload.get("qbit_wait_mismatch"):
        automation_action = "resolve_qbit_wait_mismatch"
    elif next_command_placeholder:
        automation_action = "fill_command_placeholders"
    elif material_recovery_run_blocker:
        automation_action = "complete_material_recovery_command"
    elif next_command_run_allowed:
        automation_action = "run_next_command"
    elif next_command_ready:
        automation_action = "unsupported_next_command"
    elif payload.get("schema_version_ok") is False or payload.get("kind_supported") is False:
        automation_action = "replace_summary"
    elif any("does not exist" in blocker for blocker in blockers):
        automation_action = "provide_summary"
    elif payload.get("missing_artifacts"):
        automation_action = "restore_artifacts"
    elif payload.get("missing_closure_audit"):
        automation_action = "repair_closure"
    else:
        automation_action = "resolve_blockers"
    next_command_run_blocker = material_recovery_run_blocker or next_command_metadata["run_blocker"]
    automation_reason = _summary_automation_reason(payload, automation_action, blockers, next_command_run_blocker=next_command_run_blocker)
    result = {
        **payload,
        "automation_handoff": automation_handoff,
        "automation_action": automation_action,
        "automation_reason": automation_reason,
        "next_command_ready": next_command_ready,
        "next_command_placeholder": next_command_placeholder,
        "next_command_run_allowed": next_command_run_allowed,
        "next_command_subcommand": next_command_metadata["subcommand"],
        "next_command_run_blocker": next_command_run_blocker,
        "next_command_source": payload.get("next_command_source"),
        "candidate_commands": candidate_commands,
        "candidate_command_count": len(candidate_commands),
        "runnable_command_count": sum(1 for command in candidate_commands if isinstance(command, dict) and command.get("run_allowed") is True),
        "first_runnable_stage": first_runnable_command.get("stage"),
        "first_runnable_command": first_runnable_command.get("command"),
        "first_runnable_command_argv": first_runnable_command.get("argv"),
        "first_runnable_command_source": first_runnable_command.get("source"),
        "first_runnable_command_subcommand": first_runnable_command.get("subcommand"),
        **rejected_command_summary,
        "should_execute_next_command": automation_action == "run_next_command",
        "automation_exit_code": 0 if status == "ok" else 1,
    }
    result["readiness_summary"] = _summary_check_readiness_summary(result)
    return result


def _summary_check_readiness_summary(payload: dict[str, Any]) -> dict[str, Any]:
    completion_matrix = payload.get("completion_matrix") if isinstance(payload.get("completion_matrix"), dict) else {}
    material_diagnostics = payload.get("material_diagnostics") if isinstance(payload.get("material_diagnostics"), dict) else {}
    target_upload_diagnostics = payload.get("target_upload_diagnostics") if isinstance(payload.get("target_upload_diagnostics"), dict) else {}
    target_preflight_diagnostics = payload.get("target_preflight_diagnostics") if isinstance(payload.get("target_preflight_diagnostics"), dict) else {}
    resume_state = _summary_resume_state_with_material_recovery(payload)
    return _retorrent_readiness_summary(
        status=str(payload.get("status") or "blocked"),
        ready=payload.get("ready") is True,
        complete=payload.get("complete") is True,
        blockers=_string_list(payload.get("blockers")),
        completion_matrix=completion_matrix,
        material_diagnostics=material_diagnostics,
        target_upload_diagnostics=target_upload_diagnostics,
        target_preflight_diagnostics=target_preflight_diagnostics,
        qbit_wait_mismatches=_string_list(payload.get("qbit_wait_mismatches")),
        resume_state={
            **resume_state,
            "next_stage": payload.get("next_stage"),
            "next_command": payload.get("next_command"),
            "next_command_argv": payload.get("next_command_argv"),
        },
        automation_fields={
            "automation_action": payload.get("automation_action"),
            "should_execute_next_command": payload.get("should_execute_next_command"),
            "automation_exit_code": payload.get("automation_exit_code"),
        },
        summary_file=payload.get("summary_file"),
    )


def _summary_material_recovery_command_run_blocker(payload: dict[str, Any], next_stage: str) -> str | None:
    if next_stage != "resume-target-package":
        return None
    resume_state = _summary_resume_state_with_material_recovery(payload)
    material_recovery = _readiness_material_recovery_summary(resume_state)
    coverage = material_recovery.get("command_coverage") if isinstance(material_recovery.get("command_coverage"), dict) else {}
    if not coverage.get("hint_count") or coverage.get("ready") is True:
        return None
    key = coverage.get("first_uncovered_key")
    flags = ",".join(_string_list(coverage.get("first_uncovered_missing_flags")))
    if key and flags:
        return f"material recovery command does not cover {key}; missing flags: {flags}"
    if key:
        return f"material recovery command does not cover {key}"
    return "material recovery command does not cover all missing materials"


def _summary_resume_state_with_material_recovery(payload: dict[str, Any]) -> dict[str, Any]:
    resume_state = dict(payload.get("resume_state")) if isinstance(payload.get("resume_state"), dict) else {}
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    resume_commands = _summary_command_entries(payload)
    materials = resume_state.get("materials") if isinstance(resume_state.get("materials"), dict) else {}
    generated_materials = _run_summary_material_resume_state(payload, artifacts, resume_commands) if artifacts or resume_commands else {}
    if generated_materials:
        merged_materials = {**generated_materials, **materials}
        if not materials.get("recovery_hints") and generated_materials.get("recovery_hints"):
            merged_materials["recovery_hints"] = generated_materials["recovery_hints"]
        if not materials.get("next_actions") and generated_materials.get("next_actions"):
            merged_materials["next_actions"] = generated_materials["next_actions"]
        if not materials.get("closure") and generated_materials.get("closure"):
            merged_materials["closure"] = generated_materials["closure"]
        resume_state["materials"] = merged_materials

    if not resume_state.get("next_command") and payload.get("next_command"):
        resume_state["next_stage"] = payload.get("next_stage")
        resume_state["next_command"] = payload.get("next_command")
        resume_state["next_command_argv"] = _argv_list(payload.get("next_command_argv"))
    elif not resume_state.get("next_command_argv"):
        resume_state["next_command_argv"] = _resume_state_next_command_argv({"stage": resume_state.get("next_stage"), "command": resume_state.get("next_command")}, resume_commands)

    if not resume_state.get("available_stages") and resume_commands:
        resume_state["available_stages"] = [str(command.get("stage")) for command in resume_commands if isinstance(command, dict) and command.get("stage")]
    if "resume_available" not in resume_state and resume_commands:
        resume_state["resume_available"] = True
    return resume_state


def _summary_automation_reason(payload: dict[str, Any], automation_action: str, blockers: list[str], *, next_command_run_blocker: str | None = None) -> str:
    if automation_action == "complete":
        return "Summary is complete and no follow-up command is required."
    if automation_action == "resolve_qbit_wait_mismatch":
        mismatches = ", ".join(_string_list(payload.get("qbit_wait_mismatches")))
        hint_text = _qbit_wait_retry_hint_reason(payload.get("qbit_wait_retry_hints"))
        base = f"qBittorrent wait evidence mismatched the requested torrent/content: {mismatches}." if mismatches else "qBittorrent wait evidence mismatched the requested torrent/content."
        return f"{base} {hint_text}" if hint_text else base
    if automation_action == "fill_command_placeholders":
        return "Next command contains placeholders and requires manual values before execution."
    if automation_action == "run_next_command":
        stage = payload.get("next_stage")
        return f"Next generated ptcli command is ready to run for stage {stage}." if stage else "Next generated ptcli command is ready to run."
    if automation_action == "unsupported_next_command":
        return f"Next command is present but is not allowed for automatic execution: {next_command_run_blocker}." if next_command_run_blocker else "Next command is present but is not allowed for automatic execution."
    if automation_action == "complete_material_recovery_command":
        return f"Material recovery command needs additional flags before automatic execution: {next_command_run_blocker}." if next_command_run_blocker else "Material recovery command needs additional flags before automatic execution."
    if automation_action == "replace_summary":
        return "Summary schema or kind is unsupported; regenerate the summary with the current ptcli."
    if automation_action == "provide_summary":
        return "Summary file is missing and must be provided before automation can continue."
    if automation_action == "restore_artifacts":
        return "Required artifacts are missing from the summary and must be restored or regenerated."
    if automation_action == "repair_closure":
        return "Closure audit is missing required evidence and must be repaired before automation can continue."
    if blockers:
        return f"Resolve blockers before automation can continue: {blockers[0]}"
    return "Resolve blockers before automation can continue."


def _qbit_wait_retry_hint_reason(qbit_wait_retry_hints: Any) -> str:
    if not isinstance(qbit_wait_retry_hints, dict):
        return ""
    hints: list[str] = []
    for scope in ("source", "uploaded"):
        hint = qbit_wait_retry_hints.get(scope)
        if not isinstance(hint, dict) or not hint.get("retry_recommended"):
            continue
        details = []
        if hint.get("suggested_torrent_hash"):
            details.append(f"hash={hint['suggested_torrent_hash']}")
        if hint.get("suggested_content_path"):
            details.append(f"path={hint['suggested_content_path']}")
        if hint.get("suggested_save_path"):
            details.append(f"save_path={hint['suggested_save_path']}")
        if details:
            hints.append(f"{scope} suggested retry values: {', '.join(details)}")
    return " ".join(f"{hint}." for hint in hints)


def _pipeline_summary_check(payload: dict[str, Any], summary_file: str) -> dict[str, Any]:
    diagnostics = _summary_check_diagnostics(payload)
    resume_state = _summary_resume_state_with_material_recovery(payload)
    blockers = _string_list(payload.get("blockers"))
    if not blockers:
        blockers = _string_list(resume_state.get("blockers"))
    complete = bool(payload.get("complete"))
    ready = bool(payload.get("ready"))
    artifact_status = _summary_artifact_status(resume_state)
    required = [
        "source_hash_consistent",
        "source_wait_evidence",
        "uploaded_torrent_hash",
        "injected_torrent_hash",
        "injection_visible_in_client",
        "injection_verified",
        "target_hash_consistent",
        "target_duplicate_clean",
        "target_rule_obligations",
        "target_preparation_ready",
        "uploaded_wait_evidence",
    ]
    if _summary_source_injection_audit_required(payload):
        required.extend(["source_torrent_hash", "source_injected_torrent_hash", "source_injection_visible_in_client", "source_injection_verified"])
    missing_audit = _missing_required_summary_artifacts(artifact_status, required) if complete and ready else []
    closure_audit_status = _summary_closure_audit_status(payload)
    missing_closure_audit = closure_audit_status["missing_closure_audit"]
    _extend_unique_string(artifact_status["missing_artifacts"], missing_audit)
    blockers = [*blockers, *[f"missing audit artifact: {name}" for name in missing_audit], *[f"closure audit missing: {name}" for name in missing_closure_audit]]
    _extend_unique_string(blockers, _qbit_wait_mismatch_blockers(diagnostics))
    check_status = "ok" if complete and ready and not blockers else "blocked"
    readiness_summary = _summary_check_readiness_summary({
        "status": check_status,
        "summary_file": summary_file,
        "ready": ready,
        "complete": complete,
        "blockers": blockers,
        "resume_state": resume_state,
        **diagnostics,
    })
    next_command = _summary_next_command(
        payload,
        resume_state,
        (
            *_readiness_summary_preferred_stages(readiness_summary, kind=str(payload.get("kind") or "")),
            *_completion_matrix_preferred_stages(diagnostics.get("completion_matrix"), kind=str(payload.get("kind") or "")),
            *_pipeline_summary_preferred_stages([*missing_audit, *missing_closure_audit]),
        ),
    )
    next_command = _prefer_material_recovery_next_command(next_command, readiness_summary)
    closure_status = _closure_status_summary({**payload, "status": check_status, "blockers": blockers})
    check_payload = {
        "status": check_status,
        "kind": payload.get("kind"),
        "summary_file": summary_file,
        "automation_handoff": _summary_automation_handoff(summary_file),
        "ready": ready,
        "complete": complete,
        "live_safe_to_attempt": complete and ready and not blockers,
        "blockers": blockers,
        "next_stage": next_command.get("stage"),
        "next_command": next_command.get("command"),
        "next_command_argv": next_command.get("argv"),
        "next_command_source": next_command.get("source"),
        "resume_commands": payload.get("resume_commands"),
        "recommended_commands": payload.get("recommended_commands"),
        "resume_state": resume_state,
        **diagnostics,
        "closure_status": closure_status,
        **artifact_status,
        **closure_audit_status,
    }
    check_payload["candidate_commands"] = _summary_candidate_commands(check_payload)
    return _summary_check_result(check_payload)


def _prefer_material_recovery_next_command(next_command: dict[str, Any], readiness_summary: dict[str, Any]) -> dict[str, Any]:
    if next_command.get("stage") != "resume-target-package":
        return next_command
    material_recovery = readiness_summary.get("material_recovery") if isinstance(readiness_summary.get("material_recovery"), dict) else {}
    command = material_recovery.get("first_command")
    argv = _argv_list(material_recovery.get("first_command_argv"))
    if not command or not argv:
        return next_command
    return {"stage": "resume-target-package", "command": str(command), "argv": argv, "source": "material_recovery"}


def _target_upload_summary_check(payload: dict[str, Any], summary_file: str) -> dict[str, Any]:
    diagnostics = _summary_check_diagnostics(payload)
    target_upload_diagnostics = diagnostics.get("target_upload_diagnostics") if isinstance(diagnostics.get("target_upload_diagnostics"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    resume_state = _summary_resume_state_with_material_recovery(payload)
    blockers = _string_list(summary.get("blockers")) or _string_list(resume_state.get("blockers"))
    ready = bool(summary.get("ready"))
    complete = _target_upload_summary_complete(target_upload_diagnostics, ready)
    _extend_unique_string(blockers, _target_upload_summary_completion_blockers(target_upload_diagnostics, ready, complete))
    artifact_status = _summary_artifact_status(resume_state)
    missing_audit = _target_upload_missing_audit_artifacts(resume_state) if ready and complete else []
    _extend_unique_string(artifact_status["missing_artifacts"], missing_audit)
    blockers = [*blockers, *[f"missing audit artifact: {name}" for name in missing_audit]]
    _extend_unique_string(blockers, _qbit_wait_mismatch_blockers(diagnostics))
    check_status = "ok" if ready and complete and not blockers else "blocked"
    readiness_summary = _summary_check_readiness_summary({
        "status": check_status,
        "summary_file": summary_file,
        "ready": ready,
        "complete": complete,
        "blockers": blockers,
        "resume_state": resume_state,
        **diagnostics,
    })
    next_command = _summary_next_command(
        payload,
        resume_state,
        (
            *_target_upload_summary_preferred_stages(missing_audit),
            *_readiness_summary_preferred_stages(readiness_summary, kind=str(payload.get("kind") or "")),
            *_completion_matrix_preferred_stages(diagnostics.get("completion_matrix"), kind=str(payload.get("kind") or "")),
        ),
    )
    check_payload = {
        "status": check_status,
        "kind": payload.get("kind"),
        "summary_file": summary_file,
        "automation_handoff": _summary_automation_handoff(summary_file),
        "ready": ready,
        "complete": complete,
        "live_safe_to_attempt": ready and complete and not blockers,
        "blockers": blockers,
        "next_stage": next_command.get("stage"),
        "next_command": next_command.get("command"),
        "next_command_argv": next_command.get("argv"),
        "next_command_source": next_command.get("source"),
        "resume_commands": payload.get("resume_commands"),
        "recommended_commands": payload.get("recommended_commands"),
        "resume_state": resume_state,
        **diagnostics,
        **artifact_status,
    }
    check_payload["candidate_commands"] = _summary_candidate_commands(check_payload)
    return _summary_check_result(check_payload)


def _target_upload_summary_complete(target_upload_diagnostics: dict[str, Any], ready: bool) -> bool:
    completion = target_upload_diagnostics.get("completion") if isinstance(target_upload_diagnostics.get("completion"), dict) else {}
    if isinstance(completion.get("complete"), bool):
        return bool(completion.get("complete"))
    uploaded_followup = target_upload_diagnostics.get("uploaded_followup") if isinstance(target_upload_diagnostics.get("uploaded_followup"), dict) else {}
    for key in ("ready_for_uploaded_seeding", "ready"):
        if isinstance(uploaded_followup.get(key), bool):
            return bool(uploaded_followup.get(key))
    return ready


def _target_upload_summary_completion_blockers(target_upload_diagnostics: dict[str, Any], ready: bool, complete: bool) -> list[str]:
    if not ready or complete:
        return []
    completion = target_upload_diagnostics.get("completion") if isinstance(target_upload_diagnostics.get("completion"), dict) else {}
    if not isinstance(completion.get("complete"), bool):
        return []
    missing = _string_list(completion.get("missing"))
    if not missing:
        return ["target upload follow-up is incomplete."]
    return [f"target upload follow-up incomplete: {', '.join(missing)}."]


def _doctor_summary_check(payload: dict[str, Any], summary_file: str) -> dict[str, Any]:
    resume_state = _summary_resume_state_with_material_recovery(payload)
    blockers = _string_list(payload.get("failed_check_names"))
    ready = bool(payload.get("ready"))
    live_safe = bool(payload.get("live_safe_to_attempt"))
    artifact_status = _summary_artifact_status(resume_state)
    required = (
        "flow_check_ready",
        "rule_check_ready",
        "rules_acknowledged",
        "live_upload_confirmation",
        "target_rule_obligations",
        "target_package_preflight_ready",
        "target_preparation_ready",
        "download_uploaded_torrent",
        "inject_uploaded_torrent",
        "effective_uploaded_save_path",
        "wait_uploaded_complete",
    )
    missing_audit = _missing_required_summary_artifacts(artifact_status, required) if ready and live_safe else []
    _extend_unique_string(artifact_status["missing_artifacts"], missing_audit)
    blockers = [*blockers, *[f"missing audit artifact: {name}" for name in missing_audit]]
    next_command = _summary_next_command(payload, resume_state, ("resume-uploaded-torrent", "resume-uploaded-torrent-download", "pipeline-live", "doctor-live-probes", "doctor-retry"))
    check_payload = {
        "status": "ok" if ready and live_safe and not blockers else "blocked",
        "kind": payload.get("kind"),
        "summary_file": summary_file,
        "automation_handoff": _summary_automation_handoff(summary_file),
        "ready": ready,
        "complete": live_safe,
        "live_safe_to_attempt": live_safe and not blockers,
        "blockers": blockers,
        "next_stage": next_command.get("stage"),
        "next_command": next_command.get("command"),
        "next_command_argv": next_command.get("argv"),
        "next_command_source": next_command.get("source"),
        "resume_commands": payload.get("resume_commands"),
        "recommended_commands": payload.get("recommended_commands"),
        **_summary_check_diagnostics(payload),
        **artifact_status,
    }
    check_payload["candidate_commands"] = _summary_candidate_commands(check_payload)
    return _summary_check_result(check_payload)


def _summary_next_command(payload: dict[str, Any], resume_state: dict[str, Any], preferred_stages: tuple[str, ...]) -> dict[str, Any]:
    stage = resume_state.get("next_stage")
    command = resume_state.get("next_command")
    if command:
        stage_text = str(stage) if stage else None
        return {"stage": stage_text, "command": str(command), "argv": _summary_command_argv(payload, stage_text, str(command)), "source": "resume_state"}
    commands = _summary_command_entries(payload)
    commands_by_stage = {str(command.get("stage")): command for command in commands if command.get("stage") and command.get("command")}
    for preferred_stage in preferred_stages:
        command_entry = commands_by_stage.get(preferred_stage)
        if command_entry:
            command_text = str(command_entry["command"])
            return {"stage": preferred_stage, "command": command_text, "argv": _argv_list(command_entry.get("argv")), "source": command_entry.get("_summary_command_source")}
    return {"stage": None, "command": None, "argv": None, "source": None}


def _summary_command_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for key in ("resume_commands", "recommended_commands"):
        value = payload.get(key)
        if isinstance(value, list):
            commands.extend({**command, "_summary_command_source": key} for command in value if isinstance(command, dict))
    return commands


def _summary_candidate_commands(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    entries = _summary_command_entries(payload)
    completion_entry = _summary_material_recovery_completion_command_entry(payload)
    if completion_entry:
        entries.append(completion_entry)
    for command_entry in entries:
        command = command_entry.get("command")
        if not command:
            continue
        argv = _argv_list(command_entry.get("argv")) or _summary_next_command_raw_argv(str(command))
        metadata = _summary_next_command_metadata(argv)
        run_allowed = metadata["run_allowed"]
        run_blocker = metadata["run_blocker"]
        if command_entry.get("stage") == "resume-target-package" and command_entry.get("_summary_command_source") != "material_recovery_completion":
            material_recovery_blocker = _summary_material_recovery_command_run_blocker(payload, "resume-target-package")
            if material_recovery_blocker:
                run_allowed = False
                run_blocker = material_recovery_blocker
        candidates.append({
            "stage": command_entry.get("stage"),
            "command": str(command),
            "argv": argv,
            "source": command_entry.get("_summary_command_source"),
            "subcommand": metadata["subcommand"],
            "run_allowed": run_allowed,
            "run_blocker": run_blocker,
            "placeholder": metadata["placeholder"],
        })
    return candidates


def _summary_material_recovery_completion_command_entry(payload: dict[str, Any]) -> dict[str, Any] | None:
    resume_state = _summary_resume_state_with_material_recovery(payload)
    material_recovery = _readiness_material_recovery_summary(resume_state)
    command = material_recovery.get("completion_command")
    argv = _argv_list(material_recovery.get("completion_command_argv"))
    if not command or not argv:
        return None
    return {
        "stage": "resume-target-package",
        "command": str(command),
        "argv": argv,
        "_summary_command_source": "material_recovery_completion",
    }


def _first_runnable_candidate_command(candidate_commands: list[Any]) -> dict[str, Any]:
    for command in candidate_commands:
        if isinstance(command, dict) and command.get("run_allowed") is True:
            return command
    return {}


def _rejected_candidate_command_summary(candidate_commands: list[Any]) -> dict[str, Any]:
    rejected = [command for command in candidate_commands if isinstance(command, dict) and command.get("run_allowed") is not True]
    first_rejected = rejected[0] if rejected else {}
    blockers = list(dict.fromkeys(command["run_blocker"] for command in rejected if isinstance(command.get("run_blocker"), str) and command.get("run_blocker")))
    return {
        "rejected_command_count": len(rejected),
        "rejected_command_blockers": blockers,
        "first_rejected_stage": first_rejected.get("stage"),
        "first_rejected_command": first_rejected.get("command"),
        "first_rejected_command_source": first_rejected.get("source"),
        "first_rejected_command_subcommand": first_rejected.get("subcommand"),
        "first_rejected_command_blocker": first_rejected.get("run_blocker"),
    }


def _summary_command_argv(payload: dict[str, Any], stage: str | None, command: str) -> list[str] | None:
    for command_entry in _summary_command_entries(payload):
        if stage and command_entry.get("stage") != stage:
            continue
        if command_entry.get("command") != command:
            continue
        return _argv_list(command_entry.get("argv"))
    return None


def _resume_state_next_command_argv(next_command: dict[str, Any], command_entries: list[dict[str, Any]]) -> list[str] | None:
    command = next_command.get("command")
    if not command:
        return None
    stage = next_command.get("stage")
    for command_entry in command_entries:
        if not isinstance(command_entry, dict):
            continue
        if stage and command_entry.get("stage") != stage:
            continue
        if command_entry.get("command") != command:
            continue
        argv = _argv_list(command_entry.get("argv"))
        if argv is not None:
            return argv
    try:
        return shlex.split(str(command))
    except ValueError:
        return None


def _argv_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def _pipeline_summary_preferred_stages(missing_audit: list[str]) -> tuple[str, ...]:
    preferred: list[str] = []
    if "target_preparation_ready" in missing_audit:
        preferred.append("resume-target-package")
    if any(
        name in missing_audit
        for name in (
            "source_wait_evidence",
            "source_hash_consistent",
            "source_torrent_hash",
            "source_injected_torrent_hash",
            "source_injection_visible_in_client",
            "source_injection_verified",
            "source.ready",
            "source.hash_consistent",
            "source.wait_evidence",
            "source.torrent_hash",
            "source.injected_torrent_hash",
            "source.injection_visible_in_client",
            "source.injection_verified",
        )
    ):
        preferred.append("resume-source-torrent")
    if any(
        name in missing_audit
        for name in (
            "uploaded_torrent_hash",
            "injected_torrent_hash",
            "injection_visible_in_client",
            "injection_verified",
            "uploaded_wait_evidence",
            "target.downloaded",
            "target.injected",
            "target.seeding",
            "target.hash_consistent",
            "target.uploaded_torrent_hash",
            "target.injected_torrent_hash",
            "target.injection_visible_in_client",
            "target.injection_verified",
            "target.uploaded_wait_evidence",
        )
    ):
        preferred.extend(["resume-uploaded-torrent", "resume-uploaded-torrent-download"])
    if "target_hash_consistent" in missing_audit:
        preferred.extend(["resume-uploaded-torrent", "resume-uploaded-torrent-download"])
    if any(name in missing_audit for name in ("target.uploaded", "target_torrent_file")):
        preferred.append("resume-target-torrent")
    if any(name in missing_audit for name in ("target_duplicate_clean", "target_rule_obligations", "target.prepared", "target.uploaded", "target.duplicate_clean", "target.rule_obligations")):
        preferred.append("resume-target-upload")
    preferred.extend(["resume-source-torrent", "resume-target-package", "resume-target-torrent", "resume-target-upload", "resume-uploaded-torrent-download", "resume-uploaded-torrent"])
    return tuple(dict.fromkeys(preferred))


def _completion_matrix_preferred_stages(completion_matrix: Any, *, kind: str) -> tuple[str, ...]:
    if not isinstance(completion_matrix, dict):
        return ()
    return _missing_domains_preferred_stages(_string_list(completion_matrix.get("missing_domains")), kind=kind)


def _readiness_summary_preferred_stages(readiness_summary: Any, *, kind: str) -> tuple[str, ...]:
    if not isinstance(readiness_summary, dict):
        return ()
    return _missing_domains_preferred_stages(_string_list(readiness_summary.get("missing_domains")), kind=kind)


def _missing_domains_preferred_stages(missing_domains: list[str], *, kind: str) -> tuple[str, ...]:
    preferred: list[str] = []
    for domain in missing_domains:
        if domain == "source":
            preferred.extend(["resume-source-torrent", "resume-source-download"])
        elif domain == "materials":
            preferred.append("resume-target-package")
        elif domain == "rules":
            preferred.append("target-upload-retry" if kind == "ptcli.target_upload.summary" else "resume-target-upload")
        elif domain in {"target_upload", "qbit_wait"}:
            if kind == "ptcli.target_upload.summary":
                preferred.extend(["resume-uploaded-torrent", "resume-uploaded-torrent-download"])
            else:
                preferred.extend(["resume-uploaded-torrent", "resume-uploaded-torrent-download", "resume-target-upload", "resume-target-torrent"])
    return tuple(dict.fromkeys(preferred))


def _summary_source_injection_audit_required(payload: dict[str, Any]) -> bool:
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    source = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
    mode = source.get("mode")
    if mode in {"downloaded", "resumed_torrent"}:
        return True
    closure = payload.get("closure") if isinstance(payload.get("closure"), dict) else {}
    closure_source = closure.get("source") if isinstance(closure.get("source"), dict) else {}
    return bool(closure_source.get("downloaded") or closure_source.get("injected") or closure_source.get("source_torrent_reused"))


def _summary_closure_audit_status(payload: dict[str, Any]) -> dict[str, Any]:
    audit = payload.get("closure_audit")
    if not isinstance(audit, dict):
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        audit = summary.get("closure_audit")
    if not isinstance(audit, dict):
        return {"closure_audit": None, "missing_closure_audit": []}
    missing = audit.get("missing")
    if isinstance(missing, list):
        missing_items = [str(item) for item in missing if isinstance(item, str)]
    else:
        items = audit.get("items")
        missing_items = [str(item.get("name")) for item in items if isinstance(item, dict) and item.get("ok") is not True and item.get("name")] if isinstance(items, list) else []
    return {
        "closure_audit": audit,
        "missing_closure_audit": missing_items,
    }


def _target_upload_summary_preferred_stages(missing_audit: list[str]) -> tuple[str, ...]:
    preferred: list[str] = []
    if "target_preparation_ready" in missing_audit:
        preferred.append("resume-target-package")
    if any(name in missing_audit for name in ("injection_visible_in_client", "injection_verified", "uploaded_wait_evidence", "target_hash_consistent")):
        preferred.extend(["resume-uploaded-torrent", "resume-uploaded-torrent-download"])
    if "target_duplicate_clean" in missing_audit or "target_rule_obligations" in missing_audit:
        preferred.append("target-upload-retry")
    preferred.extend(["resume-target-package", "resume-uploaded-torrent", "resume-uploaded-torrent-download", "target-upload-retry"])
    return tuple(dict.fromkeys(preferred))


def _summary_artifact_status(resume_state: dict[str, Any]) -> dict[str, Any]:
    artifacts = resume_state.get("artifacts") if isinstance(resume_state.get("artifacts"), dict) else {}
    normalized = {str(key): bool(value) for key, value in artifacts.items()}
    return {
        "artifacts": normalized,
        "missing_artifacts": [key for key, ready in normalized.items() if not ready],
        "available_stages": resume_state.get("available_stages") if isinstance(resume_state.get("available_stages"), list) else [],
    }


def _missing_required_summary_artifacts(artifact_status: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    artifacts = artifact_status.get("artifacts") if isinstance(artifact_status.get("artifacts"), dict) else {}
    return [name for name in required if artifacts.get(name) is not True]


def _rule_obligations_artifact_ready(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("ready") is True
    return bool(value)


def _write_doctor_summary(payload: dict[str, Any], args: argparse.Namespace, output_dir: str | None) -> str:
    destination_dir = Path(output_dir).expanduser() if output_dir else Path("./tmp/retorrent-runs").expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / "ptcli-doctor-summary.json"
    summary_payload = _doctor_summary_payload(payload, args, str(destination))
    destination.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(destination)


def _summary_automation_handoff(summary_file: str) -> dict[str, dict[str, Any]]:
    base = ["python3", "ptcli.py", "summary-check", "--summary-file", summary_file]
    commands = {
        "json": [*base, "--json"],
        "print_next_command": [*base, "--print-next-command"],
        "print_next_argv": [*base, "--print-next-argv"],
        "print_first_runnable_command": [*base, "--print-first-runnable-command"],
        "print_first_runnable_argv": [*base, "--print-first-runnable-argv"],
        "print_shell": [*base, "--print-shell"],
        "run_next_command": [*base, "--run-next-command"],
        "run_first_runnable_command": [*base, "--run-first-runnable-command"],
    }
    return {name: {"command": shlex.join(argv), "argv": argv} for name, argv in commands.items()}


def _doctor_summary_payload(payload: dict[str, Any], args: argparse.Namespace, summary_file: str) -> dict[str, Any]:
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    failed_checks = [check for check in checks if isinstance(check, dict) and not check.get("ok")]
    artifacts = _doctor_summary_artifacts(args, payload, payload.get("effective_uploaded_save_path"))
    material_diagnostics = _summary_material_diagnostics({"artifacts": artifacts})
    recommended_commands = _doctor_recommended_commands(payload, args, artifacts)
    return {
        "schema_version": 1,
        "kind": "ptcli.doctor.live_readiness",
        "summary_file": summary_file,
        "automation_handoff": _summary_automation_handoff(summary_file),
        "status": payload.get("status"),
        "mode": _doctor_summary_mode(args),
        "target_mode": _doctor_summary_mode(args),
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
        "material_gate": payload.get("material_gate") if isinstance(payload.get("material_gate"), dict) else {},
        "material_diagnostics": material_diagnostics,
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
        "target_preparation_audit": artifacts.get("target_preparation_audit"),
    }


def _doctor_resume_state(payload: dict[str, Any], artifacts: dict[str, Any], failed_checks: list[Any], recommended_commands: list[dict[str, Any]]) -> dict[str, Any]:
    commands_by_stage = {str(command.get("stage")): str(command.get("command")) for command in recommended_commands if isinstance(command, dict)}
    next_command = _doctor_next_command(payload, commands_by_stage)
    next_command_argv = _resume_state_next_command_argv(next_command, recommended_commands)
    target_preflight = artifacts.get("target_preflight_gates") if isinstance(artifacts.get("target_preflight_gates"), dict) else {}
    target_preflight_torrent = target_preflight.get("torrent_file") if isinstance(target_preflight.get("torrent_file"), dict) else {}
    return {
        "ready": bool(payload.get("ready")),
        "live_safe_to_attempt": bool(payload.get("live_safe_to_attempt")),
        "resume_available": any(stage != "doctor-retry" for stage in commands_by_stage),
        "next_stage": next_command.get("stage"),
        "next_command": next_command.get("command"),
        "next_command_argv": next_command_argv,
        "available_stages": [str(command.get("stage")) for command in recommended_commands if isinstance(command, dict)],
        "artifacts": {
            "content_path": bool(_path_artifact_exists(artifacts.get("content_path"))),
            "source_torrent_file": bool(_path_artifact_exists(artifacts.get("source_torrent_file"))),
            "package_dir": bool(_path_artifact_exists(artifacts.get("package_dir"))),
            "target_torrent_file": bool(_path_artifact_exists(artifacts.get("target_torrent_file"))),
            "uploaded_torrent_id": bool(artifacts.get("uploaded_torrent_id")),
            "uploaded_torrent_file": bool(_path_artifact_exists(artifacts.get("uploaded_torrent_file"))),
            "effective_uploaded_save_path": bool(_path_artifact_exists(artifacts.get("effective_uploaded_save_path"))),
            "flow_check_ready": bool(artifacts.get("flow_check_ready")),
            "rule_check_ready": bool(artifacts.get("rule_check_ready")),
            "rules_acknowledged": bool(artifacts.get("rules_acknowledged")),
            "live_upload_confirmation": bool(artifacts.get("live_upload_confirmation")),
            "target_rule_obligations": bool(artifacts.get("target_rule_obligations")),
            "target_package_preflight_ready": bool(artifacts.get("target_package_preflight_ready")),
            "target_preparation_ready": bool(artifacts.get("target_preparation_ready")),
            "target_preflight_gates_ready": bool(target_preflight.get("ready")),
            "target_preflight_materials_ready": bool(target_preflight.get("materials_ready")),
            "target_preflight_description_ready": bool(target_preflight.get("description_ready")),
            "target_preflight_payload_ready": bool(target_preflight.get("payload_ready")),
            "target_preflight_torrent_safe": bool(target_preflight_torrent.get("mteam_safe")),
            "material_gate_ready": artifacts.get("material_gate", {}).get("ready") is True if isinstance(artifacts.get("material_gate"), dict) else False,
            "download_uploaded_torrent": bool(artifacts.get("download_uploaded_torrent")),
            "inject_uploaded_torrent": bool(artifacts.get("inject_uploaded_torrent")),
            "wait_uploaded_complete": bool(artifacts.get("wait_uploaded_complete")),
        },
        "failed_check_names": [str(check.get("name")) for check in failed_checks if isinstance(check, dict)],
    }


def _doctor_next_command(payload: dict[str, Any], commands_by_stage: dict[str, str]) -> dict[str, str | None]:
    preferred_stages = ["resume-uploaded-torrent", "resume-uploaded-torrent-download", "pipeline-live", "doctor-live-probes"] if payload.get("live_safe_to_attempt") else ["resume-target-package", "doctor-retry"]
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


def _doctor_summary_mode(args: argparse.Namespace) -> str:
    if args.uploaded_torrent_file:
        return "resumed_uploaded_torrent"
    if args.uploaded_torrent_id:
        return "resumed_uploaded_id"
    if args.target_execute:
        return "live_upload"
    if args.package_dir:
        return "prepared"
    return "readiness_check"


def _doctor_summary_artifacts(args: argparse.Namespace, payload: dict[str, Any], effective_uploaded_save_path: Any = None) -> dict[str, Any]:
    flow_check = payload.get("flow_check") if isinstance(payload.get("flow_check"), dict) else {}
    rule_check = payload.get("rule_check") if isinstance(payload.get("rule_check"), dict) else {}
    compliance = payload.get("compliance") if isinstance(payload.get("compliance"), dict) else {}
    package_preflight = payload.get("package_preflight") if isinstance(payload.get("package_preflight"), dict) else {}
    material_gate = payload.get("material_gate") if isinstance(payload.get("material_gate"), dict) else {}
    target_rule_obligations = compliance.get("target_rule_obligation_review")
    if not isinstance(target_rule_obligations, dict):
        target_rule_obligations = package_preflight.get("rule_obligation_review") if isinstance(package_preflight.get("rule_obligation_review"), dict) else None
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    preparation_audit = _target_preparation_audit_from_preflight(package_preflight)
    target_preflight = _target_preflight_gates(package_preflight, preparation_audit)
    target_materials = _target_package_materials_from_dir(args.package_dir)
    content_path = args.content_path or package_preflight.get("content_path")
    return {
        "content_path": _path_artifact(str(content_path)) if content_path else None,
        "source_torrent_file": _path_artifact(args.source_torrent_file),
        "package_dir": _path_artifact(args.package_dir),
        "target_torrent_file": _path_artifact(args.target_torrent_file),
        "target_materials": target_materials,
        "target_preparation_audit": preparation_audit,
        "target_preparation_ready": bool(preparation_audit.get("ready")),
        "target_materials_missing": _string_list(target_materials.get("missing")),
        "target_materials_warnings": _string_list(target_materials.get("warnings")),
        "target_preparation_missing": _string_list(preparation_audit.get("missing")),
        "target_materials_ready": preparation_audit.get("materials_ready"),
        "target_preflight_gates": target_preflight,
        "material_gate": material_gate,
        "live_material_gate": _doctor_live_material_gate_artifact(material_gate),
        "uploaded_torrent_id": args.uploaded_torrent_id,
        "uploaded_torrent_file": _path_artifact(args.uploaded_torrent_file),
        "effective_uploaded_save_path": _path_artifact(str(effective_uploaded_save_path)) if effective_uploaded_save_path else None,
        "flow_check_ready": bool(flow_check.get("ready")),
        "rule_check_ready": bool(rule_check.get("ready")),
        "rules_acknowledged": bool(compliance.get("rules_acknowledged")),
        "live_upload_confirmation": _doctor_check_ok(checks, "live_upload_confirmation"),
        "rule_obligations": compliance.get("rule_obligations") if isinstance(compliance.get("rule_obligations"), dict) else None,
        "target_rule_obligations": target_rule_obligations,
        "target_package_preflight_ready": package_preflight.get("status") == "ready" if package_preflight else False,
        "download_uploaded_torrent": _doctor_check_ok(checks, "download_uploaded_torrent"),
        "inject_uploaded_torrent": _doctor_check_ok(checks, "inject_uploaded_torrent"),
        "wait_uploaded_complete": _doctor_check_ok(checks, "wait_uploaded_complete"),
    }


def _doctor_check_ok(checks: list[Any], name: str) -> bool:
    return any(isinstance(check, dict) and check.get("name") == name and check.get("ok") is True for check in checks)


def _doctor_live_material_gate_artifact(material_gate: dict[str, Any]) -> dict[str, Any]:
    if not material_gate:
        return {}
    return {
        "ready": material_gate.get("ready") if isinstance(material_gate.get("ready"), bool) else None,
        "skipped": material_gate.get("present") is False,
        "message": material_gate.get("message"),
        "ready_for_mteam_upload": material_gate.get("ready") if isinstance(material_gate.get("ready"), bool) else None,
        "gates": material_gate.get("gates") if isinstance(material_gate.get("gates"), dict) else {},
        "missing": _string_list(material_gate.get("missing")),
        "blockers": _string_list(material_gate.get("blockers")),
        "next_actions": _string_list(material_gate.get("next_actions")),
        "critical_path": material_gate.get("critical_path") if isinstance(material_gate.get("critical_path"), dict) else {},
        "readiness": material_gate.get("readiness") if isinstance(material_gate.get("readiness"), dict) else {},
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


def _target_package_materials_from_dir(package_dir: Any) -> dict[str, Any]:
    if not package_dir:
        return {}
    try:
        package = load_mteam_prepare_package(str(package_dir))
    except Exception:
        return {}
    return _target_materials_summary(package)


def _doctor_recommended_commands(payload: dict[str, Any], args: argparse.Namespace, artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    commands = [
        _ptcli_command_entry("doctor-retry", _doctor_retry_args(args)),
    ]
    target_package_resume = _doctor_target_package_resume_command(args, artifacts)
    if target_package_resume:
        commands.append(target_package_resume)
    if not payload.get("live_safe_to_attempt"):
        return commands

    commands.append(_ptcli_command_entry("doctor-live-probes", _doctor_retry_args(args, force_probes=True)))
    uploaded_save_path = args.uploaded_save_path or _artifact_path(artifacts.get("effective_uploaded_save_path"))

    if args.uploaded_torrent_file and args.package_dir:
        uploaded_resume_args = [
            "target-upload",
            "--package-dir",
            args.package_dir,
            "--client",
            args.client,
            "--uploaded-torrent-file",
            args.uploaded_torrent_file,
            "--inject-uploaded-torrent",
            "--wait-uploaded-complete",
            "--write-summary",
            "--json",
        ]
        if args.config:
            uploaded_resume_args.extend(["--config", args.config])
        if args.summary_output_dir:
            uploaded_resume_args.extend(["--summary-output-dir", args.summary_output_dir])
        if uploaded_save_path:
            uploaded_resume_args.extend(["--uploaded-save-path", uploaded_save_path])
        if args.uploaded_qbit_category:
            uploaded_resume_args.extend(["--uploaded-qbit-category", args.uploaded_qbit_category])
        if args.uploaded_qbit_tags:
            uploaded_resume_args.extend(["--uploaded-qbit-tags", args.uploaded_qbit_tags])
        if args.uploaded_paused:
            uploaded_resume_args.append("--uploaded-paused")
        _append_uploaded_wait_options(uploaded_resume_args, args)
        commands.append(_ptcli_command_entry("resume-uploaded-torrent", uploaded_resume_args))
        return commands

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
        if args.config:
            uploaded_resume_args.extend(["--config", args.config])
        if args.summary_output_dir:
            uploaded_resume_args.extend(["--summary-output-dir", args.summary_output_dir])
        if uploaded_save_path:
            uploaded_resume_args.extend(["--uploaded-save-path", uploaded_save_path])
        if args.uploaded_qbit_category:
            uploaded_resume_args.extend(["--uploaded-qbit-category", args.uploaded_qbit_category])
        if args.uploaded_qbit_tags:
            uploaded_resume_args.extend(["--uploaded-qbit-tags", args.uploaded_qbit_tags])
        if args.uploaded_paused:
            uploaded_resume_args.append("--uploaded-paused")
        _append_uploaded_wait_options(uploaded_resume_args, args)
        commands.append(_ptcli_command_entry("resume-uploaded-torrent-download", uploaded_resume_args))
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
        "--client",
        args.client,
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
    if args.config:
        pipeline_args.extend(["--config", args.config])
    if args.base_dir:
        pipeline_args.extend(["--base-dir", args.base_dir])
    if args.summary_output_dir:
        pipeline_args.extend(["--summary-output-dir", args.summary_output_dir])
    _extend_command_path(pipeline_args, "--path", artifacts.get("content_path"))
    _extend_command_path(pipeline_args, "--source-torrent-file", artifacts.get("source_torrent_file"))
    _extend_command_path(pipeline_args, "--package-dir", artifacts.get("package_dir"))
    _extend_command_path(pipeline_args, "--target-torrent-file", artifacts.get("target_torrent_file"))
    if uploaded_save_path:
        pipeline_args.extend(["--uploaded-save-path", uploaded_save_path])
    if args.uploaded_qbit_category:
        pipeline_args.extend(["--uploaded-qbit-category", args.uploaded_qbit_category])
    if args.uploaded_qbit_tags:
        pipeline_args.extend(["--uploaded-qbit-tags", args.uploaded_qbit_tags])
    if args.uploaded_paused:
        pipeline_args.append("--uploaded-paused")
    _append_uploaded_wait_options(pipeline_args, args)
    commands.append(_ptcli_command_entry("pipeline-live", pipeline_args))
    return commands


def _doctor_target_package_resume_command(args: argparse.Namespace, artifacts: dict[str, Any]) -> dict[str, Any] | None:
    material_missing = _target_package_material_recovery_missing(artifacts)
    if not material_missing:
        return None
    content_path = _artifact_path(artifacts.get("content_path"))
    if not content_path:
        return None
    package_dir = _artifact_path(artifacts.get("package_dir"))
    target_output_dir = str(Path(package_dir).expanduser().parent) if package_dir else "./tmp/target"
    resume_args = [
        "pipeline",
        "--from",
        normalize_tracker(args.source_tracker),
        "--source-id",
        extract_torrent_id(args.source_id),
        "--to",
        ",".join(parse_tracker_list(args.target_trackers)),
        "--client",
        args.client,
        "--path",
        content_path,
        "--check-dupes",
        "--prepare-target",
        "--target-output-dir",
        target_output_dir,
        "--accept-rules",
        "--write-summary",
        "--json",
        *_target_package_material_resume_args({}, {}, {}, artifacts),
    ]
    if args.config:
        resume_args.extend(["--config", args.config])
    if args.base_dir:
        resume_args.extend(["--base-dir", args.base_dir])
    if args.summary_output_dir:
        resume_args.extend(["--summary-output-dir", args.summary_output_dir])
    return _ptcli_command_entry("resume-target-package", resume_args)


def _artifact_path(artifact: Any) -> str | None:
    if isinstance(artifact, dict) and artifact.get("path"):
        return str(artifact["path"])
    return None


def _doctor_retry_command(args: argparse.Namespace, *, force_probes: bool = False) -> str:
    return _ptcli_command(_doctor_retry_args(args, force_probes=force_probes))


def _doctor_retry_args(args: argparse.Namespace, *, force_probes: bool = False) -> list[str]:
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
    _append_uploaded_wait_options(retry_args, args)
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
    return retry_args


def _extend_command_path(command: list[str], option: str, artifact: Any) -> None:
    if isinstance(artifact, dict) and artifact.get("path"):
        command.extend([option, str(artifact["path"])])


def _target_upload_summary(result: dict[str, Any], preflight: dict[str, Any], args: argparse.Namespace | None = None) -> dict[str, Any]:
    downloaded_torrent = result.get("downloaded_torrent")
    injected_torrent = result.get("injected_torrent")
    uploaded_wait = result.get("uploaded_wait")
    blockers = _target_upload_result_blockers(result)
    uploaded_torrent_hash = _uploaded_torrent_hash_from_result(result)
    duplicate_check = result.get("fresh_duplicate_check") if isinstance(result.get("fresh_duplicate_check"), dict) else _duplicate_check_from_target_package(preflight)
    rule_obligations = preflight.get("rule_obligation_review", {})
    _extend_unique_string(blockers, _target_rule_obligation_blockers(rule_obligations))
    preparation_audit = _target_preparation_audit_from_preflight(preflight)
    qbit_closure = {
        "injection": _qbit_injection_evidence(injected_torrent),
        "wait": _qbit_wait_evidence(uploaded_wait),
    }
    completion_review = _target_upload_completion_review(result, preflight, preparation_audit, duplicate_check, rule_obligations)
    return {
        "status": result.get("status"),
        "mode": _target_upload_summary_mode(result, preflight, args),
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
        "seeding_verified": _wait_result_completed(uploaded_wait),
        "uploaded_torrent_hash": uploaded_torrent_hash,
        "uploaded_wait": uploaded_wait if isinstance(uploaded_wait, dict) else None,
        "qbit_closure": qbit_closure,
        "blockers": blockers,
        "preflight_status": preflight.get("status"),
        "preflight_blockers": preflight.get("blockers", []),
        "target_preparation_audit": preparation_audit,
        "target_preparation_ready": bool(preparation_audit.get("ready")),
        "completion_review": completion_review,
        "fresh_duplicate_check": duplicate_check,
        "hash_consistent": not _uploaded_torrent_hash_consistency_blockers(result),
        "duplicate_clean": _fresh_duplicate_check_clean(duplicate_check),
        "rule_obligations": rule_obligations,
    }


def _target_upload_completion_review(
    result: dict[str, Any],
    preflight: dict[str, Any],
    preparation_audit: dict[str, Any],
    duplicate_check: Any,
    rule_obligations: Any,
) -> dict[str, Any]:
    downloaded_torrent = result.get("downloaded_torrent")
    injected_torrent = result.get("injected_torrent")
    uploaded_wait = result.get("uploaded_wait")
    uploaded_torrent_file_ready = _torrent_file_evidence_complete(downloaded_torrent)
    injection_visible = _injected_torrent_visible(injected_torrent)
    injection_verified = _injected_torrent_verified(injected_torrent)
    wait_complete = _wait_result_completed(uploaded_wait)
    hash_consistent = not _uploaded_torrent_hash_consistency_blockers(result)
    duplicate_clean = _fresh_duplicate_check_clean(duplicate_check)
    rule_obligations_ready = isinstance(rule_obligations, dict) and rule_obligations.get("ready") is True
    checks = {
        "target_preparation_ready": bool(preparation_audit.get("ready")),
        "uploaded": result.get("status") == "uploaded",
        "uploaded_torrent_id": bool(_uploaded_torrent_id_from_result(result)),
        "uploaded_torrent_file": uploaded_torrent_file_ready,
        "uploaded_torrent_hash": bool(_uploaded_torrent_hash_from_result(result)),
        "injection_visible_in_client": injection_visible,
        "injection_verified": injection_verified,
        "uploaded_wait_complete": wait_complete,
        "hash_consistent": hash_consistent,
        "duplicate_clean": duplicate_clean,
        "rule_obligations_ready": rule_obligations_ready,
    }
    missing = [name for name, ok in checks.items() if not ok]
    return {
        "complete": not missing,
        "missing": missing,
        "checks": checks,
        "uploaded_torrent_id": _uploaded_torrent_id_from_result(result),
        "uploaded_torrent_hash": _uploaded_torrent_hash_from_result(result),
        "uploaded_torrent_path": downloaded_torrent.get("path") if isinstance(downloaded_torrent, dict) else None,
        "injected_torrent_hash": _torrent_hash_from_result(injected_torrent),
        "uploaded_save_path": _uploaded_save_path_from_result(result),
        "uploaded_wait_query": uploaded_wait.get("query") if isinstance(uploaded_wait, dict) else None,
        "preflight_status": preflight.get("status"),
    }


def _target_preparation_audit_from_preflight(preflight: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(preflight, dict) or not preflight:
        return {"ready": False, "blockers": ["target upload preflight is missing."]}
    upload_payload = preflight.get("upload_payload") if isinstance(preflight.get("upload_payload"), dict) else {}
    description_file = upload_payload.get("description_file") if isinstance(upload_payload.get("description_file"), dict) else {}
    content = description_file.get("content") if isinstance(description_file.get("content"), dict) else {}
    review = upload_payload.get("review") if isinstance(upload_payload.get("review"), dict) else {}
    review_description = review.get("description") if isinstance(review.get("description"), dict) else {}
    review_materials = review.get("materials") if isinstance(review.get("materials"), dict) else {}
    payload_review = _payload_review_summary_from_upload_payload(upload_payload)
    material_checks = upload_payload.get("material_checks") if isinstance(upload_payload.get("material_checks"), list) else []
    payload_checks = _target_preparation_checks(material_checks, "payload.")
    description_checks = _target_preparation_checks(material_checks, "materials.description.")
    screenshot_coverage = _target_preparation_screenshot_coverage(material_checks)
    metadata_checks = _target_preparation_checks(material_checks, "materials.metadata.")
    asset_checks = _target_preparation_checks(material_checks, "materials.assets.")
    required_description_checks = {
        "materials.description.ptgen_description",
        "materials.description.external_ids",
        "materials.description.mediainfo_or_bdinfo",
        "materials.description.screenshot_bbcode",
        "materials.description.screenshot_coverage",
    }
    description_checks_ready = all(any(check.get("name") == name and check.get("ok") is True for check in description_checks) for name in required_description_checks)
    payload_checks_ready = bool(payload_checks) and all(check.get("ok") is True for check in payload_checks)
    metadata_ready = bool(metadata_checks) and all(check.get("ok") is True for check in metadata_checks)
    assets_ready = bool(asset_checks) and all(check.get("ok") is True for check in asset_checks)
    materials_ready = metadata_ready and assets_ready
    payload_ready = preflight.get("status") == "ready" and payload_checks_ready
    missing = _target_preparation_missing_from_checks(material_checks)
    description_missing = _target_preparation_missing_from_checks(description_checks)
    if not payload_ready:
        _append_unique_string(missing, "payload.preflight")
    if not description_checks_ready:
        _append_unique_string(missing, "description.content")
    blockers = _string_list(preflight.get("blockers"))
    _extend_unique_string(blockers, [str(check.get("message")) for check in material_checks if isinstance(check, dict) and check.get("ok") is not True and check.get("message")])
    return {
        "ready": bool(payload_ready and materials_ready and description_checks_ready),
        "materials_ready": materials_ready,
        "metadata_ready": metadata_ready,
        "assets_ready": assets_ready,
        "description_ready": description_checks_ready,
        "payload_ready": payload_ready,
        "package_dir": preflight.get("package_dir"),
        "files": preflight.get("files") if isinstance(preflight.get("files"), dict) else {},
        "description": {
            "path": description_file.get("path"),
            "exists": bool(description_file.get("exists")),
            "char_length": description_file.get("char_length"),
            "expected_length": description_file.get("expected_length"),
            "has_ptgen_description": bool(content.get("has_ptgen_description")),
            "ptgen_description_length": review_description.get("ptgen_description_length"),
            "has_external_ids": bool(content.get("has_imdb") and content.get("has_tmdb") and content.get("has_douban")),
            "external_id_readiness": content.get("external_id_readiness") if isinstance(content.get("external_id_readiness"), dict) else {
                "imdb": bool(content.get("has_imdb")),
                "tmdb": bool(content.get("has_tmdb")),
                "douban": bool(content.get("has_douban")),
            },
            "external_id_missing": _string_list(content.get("external_id_missing")),
            "external_links": content.get("external_links") if isinstance(content.get("external_links"), dict) else {},
            "evidence": review_description.get("evidence") if isinstance(review_description.get("evidence"), dict) else {},
            "has_mediainfo_or_bdinfo": bool(content.get("has_mediainfo_or_bdinfo")),
            "media_info": _target_preparation_media_info(content, review_materials),
            "has_screenshot_bbcode": bool(content.get("has_screenshot_bbcode")),
            "bbcode_image_count": int(content.get("bbcode_image_count", 0) or 0),
            "bbcode_image_urls": _string_list(content.get("bbcode_image_urls")),
            "screenshot_coverage": screenshot_coverage,
            "missing": description_missing,
        },
        "payload": {
            "status": preflight.get("status"),
            "torrent_file": upload_payload.get("torrent_file") if isinstance(upload_payload.get("torrent_file"), dict) else None,
            "materials_ready_required": bool(upload_payload.get("materials_ready_required")) if upload_payload else False,
            "payload_checks_ready": payload_checks_ready,
            "description_checks_ready": description_checks_ready,
        },
        "payload_review": payload_review,
        "missing": missing,
        "blockers": blockers,
    }


def _target_preparation_checks(checks: list[Any], prefix: str) -> list[dict[str, Any]]:
    return [check for check in checks if isinstance(check, dict) and str(check.get("name") or "").startswith(prefix)]


def _target_preparation_missing_from_checks(checks: list[Any]) -> list[str]:
    return [str(check.get("name")) for check in checks if isinstance(check, dict) and check.get("ok") is not True and check.get("name")]


def _target_preparation_screenshot_coverage(material_checks: list[Any]) -> dict[str, Any]:
    for check in material_checks:
        if not isinstance(check, dict) or check.get("name") != "materials.description.screenshot_coverage":
            continue
        return {
            "ready": bool(check.get("ok")),
            "expected_urls": _string_list(check.get("expected_urls")),
            "description_urls": _string_list(check.get("description_urls")),
            "missing_urls": _string_list(check.get("missing_urls")),
        }
    return {"ready": None, "expected_urls": [], "description_urls": [], "missing_urls": []}


def _target_preparation_media_info(content: dict[str, Any], review_materials: dict[str, Any]) -> dict[str, Any]:
    length = review_materials.get("mediainfo_or_bdinfo_length")
    return {
        "has_excerpt": bool(content.get("has_mediainfo_or_bdinfo")),
        "source": review_materials.get("mediainfo_or_bdinfo_source"),
        "length": length if isinstance(length, int) else None,
    }


def _target_upload_summary_mode(result: dict[str, Any], preflight: dict[str, Any], args: argparse.Namespace | None = None) -> str:
    if args is not None:
        if getattr(args, "uploaded_torrent_file", None):
            return "resumed_uploaded_torrent"
        if getattr(args, "uploaded_torrent_id", None):
            return "resumed_uploaded_id"
        if getattr(args, "execute", False) and result.get("status") == "uploaded":
            return "live_upload"
    downloaded_torrent = result.get("downloaded_torrent")
    if isinstance(downloaded_torrent, dict) and downloaded_torrent.get("reused"):
        return "resumed_uploaded_torrent"
    if result.get("uploaded_torrent_id") and result.get("status") == "uploaded" and not result.get("submitted_torrent_hash"):
        return "resumed_uploaded_id"
    if result.get("status") == "uploaded":
        return "live_upload"
    if result.get("status") == "ready" or preflight.get("status") == "ready":
        return "prepared"
    if result.get("status") == "blocked":
        return "blocked"
    return "missing"


def _target_rule_obligation_blockers(rule_obligations: Any) -> list[str]:
    if not isinstance(rule_obligations, dict):
        return ["target rule obligations are missing from MTEAM preflight."]
    if rule_obligations.get("ready") is True:
        return []
    missing = rule_obligations.get("missing")
    if isinstance(missing, list) and missing:
        return [f"target rule obligations are not ready: missing {', '.join(str(item) for item in missing)}."]
    blockers = _string_list(rule_obligations.get("blockers"))
    if blockers:
        return [f"target rule obligations are not ready: {blocker}" for blocker in blockers]
    return ["target rule obligations are not ready."]


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
        "visible_in_client",
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
        "completion_verification",
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
    package_upload_resume = _package_upload_resume_requested(args)
    uploaded_followup_resume = bool(args.upload_target and package_upload_resume)
    package_source_info = _source_info_from_existing_target_package(args.package_dir) if package_upload_resume and args.package_dir else None
    runtime_check_requested = bool(getattr(args, "check_runtime", False) or live_target_upload)
    source_download_requested = bool(args.download_source or (live_target_upload and not args.content_path and not args.source_torrent_file))
    source_injection_requested = bool(args.inject_source or (live_target_upload and not args.content_path))
    source_wait_requested = bool(args.wait_complete or (live_target_upload and (source_injection_requested or args.content_path)))
    if live_target_upload or uploaded_followup_resume:
        if not args.uploaded_torrent_file:
            args.download_uploaded_torrent = True
        args.inject_uploaded_torrent = True
        args.wait_uploaded_complete = True
    _apply_pipeline_live_metadata_defaults(args, live_target_upload=live_target_upload, package_upload_resume=package_upload_resume)
    _apply_pipeline_live_material_defaults(args, live_target_upload=live_target_upload, package_upload_resume=package_upload_resume)

    stages: list[dict[str, Any]] = []
    if runtime_check_requested:
        runtime_check = build_runtime_dependency_check()
        stages.append({"stage": "runtime-check", "ok": bool(runtime_check.get("ok")), "message": runtime_check.get("message"), "result": runtime_check})
    else:
        stages.append({"stage": "runtime-check", "ok": True, "skipped": True, "message": "--check-runtime not provided; runtime dependency check skipped."})
    material_prerequisite_check = _pipeline_material_prerequisite_check(config, args)
    stages.append(
        {
            "stage": "material-prerequisite-check",
            "ok": bool(material_prerequisite_check.get("ok")),
            "skipped": bool(material_prerequisite_check.get("skipped")),
            "message": material_prerequisite_check.get("message"),
            "result": material_prerequisite_check,
        }
    )

    if package_upload_resume:
        flow_check_result = {
            "ready": True,
            "skipped": True,
            "message": "Skipped source flow prerequisite check because this run resumes from an existing MTEAM package and uploaded torrent.",
        }
        stages.append({"stage": "flow-check", "ok": True, "skipped": True, "result": flow_check_result})
    else:
        flow_check_result = build_flow_check(config, source_tracker, source_torrent_id, ",".join(target_trackers), args.client, base_dir=args.base_dir)
        stages.append({"stage": "flow-check", "ok": bool(flow_check_result.get("ready")), "result": flow_check_result})

    if package_source_info:
        source_info_result = {
            "stage": "source-info",
            "ok": True,
            "skipped": True,
            "message": "Loaded source metadata from existing MTEAM package for uploaded torrent resume.",
            "result": package_source_info,
        }
    else:
        source_info_result = await _pipeline_stage(
            "source-info",
            lambda: fetch_source_info(config, source_tracker, source_torrent_id, base_dir=args.base_dir),
            lambda info: info.to_dict(),
            validate=lambda info: source_info_has_signal(info),
            invalid_message="Source metadata lookup returned no usable identifiers, name, hash, description, or Douban data.",
        )
    stages.append(source_info_result)
    if getattr(args, "enrich_metadata", False) or getattr(args, "fetch_ptgen", False):
        source_info_result = await _pipeline_metadata_enrichment_stage(config, args, source_info_result)
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
                lambda path: _torrent_file_evidence(path, require_metadata=True),
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
            inject_result = _source_injection_hash_verify_stage(inject_result, source_download_stage)
            stages.append(inject_result)
            if inject_result.get("ok"):
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
                validate=_wait_result_completed,
                invalid_message="qBittorrent task did not complete with matched source torrent evidence.",
            )
            stages.append(wait_result)
            effective_content_path = effective_content_path or _content_path_from_stage(wait_result)
            effective_source_torrent_hash = _torrent_hash_from_stage(wait_result) or effective_source_torrent_hash
        elif args.content_path:
            wait_result = await _pipeline_stage(
                "wait-complete",
                lambda: _wait_complete_with_config(config, args.client, content_path=args.content_path, torrent_hash=effective_source_torrent_hash, timeout=args.wait_timeout, interval=args.wait_interval),
                lambda payload: payload,
                validate=_wait_result_completed,
                invalid_message="qBittorrent task did not complete with matched source torrent evidence.",
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
        source_content_verify_stage = _source_content_verify_stage(match_result, effective_source_torrent_hash)
        stages.append(source_content_verify_stage)
        if source_content_verify_stage.get("ok"):
            effective_source_torrent_hash = effective_source_torrent_hash or _torrent_hash_from_stage(match_result)
    else:
        stages.append({"stage": "match", "ok": True, "skipped": True, "message": "--path not provided; qBittorrent match skipped."})

    if args.check_dupes:
        source_stage = _latest_source_info_stage(stages)
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
        source_stage = _latest_source_info_stage(stages)
        source_result = source_stage.get("result") if source_stage and source_stage.get("ok") else None
        material_files = _mteam_material_files_from_args(args)
        if args.generate_bdinfo:
            bdinfo_stage = await _pipeline_bdinfo_material_stage(args, source_result if isinstance(source_result, dict) else None, effective_content_path, material_files)
            stages.append(bdinfo_stage)
            generated_bdinfo = bdinfo_stage.get("result", {}).get("bdinfo_file") if bdinfo_stage.get("ok") else None
            if generated_bdinfo and not material_files.get("bdinfo_file"):
                material_files["bdinfo_file"] = str(generated_bdinfo)
        if args.generate_mediainfo:
            material_stage = await _pipeline_mediainfo_material_stage(args, source_result if isinstance(source_result, dict) else None, effective_content_path, material_files)
            stages.append(material_stage)
            generated_mediainfo = material_stage.get("result", {}).get("mediainfo_file") if material_stage.get("ok") else None
            if generated_mediainfo and not material_files.get("mediainfo_file"):
                material_files["mediainfo_file"] = str(generated_mediainfo)
        if args.generate_screenshots:
            screenshot_stage = await _pipeline_screenshot_material_stage(args, source_result if isinstance(source_result, dict) else None, effective_content_path, material_files)
            stages.append(screenshot_stage)
            generated_screenshots = screenshot_stage.get("result", {}).get("screenshot_files") if screenshot_stage.get("ok") else None
            if generated_screenshots and not material_files.get("screenshot_files"):
                material_files["screenshot_files"] = list(generated_screenshots)
        if args.upload_screenshots:
            if not _material_prerequisite_check_ready(stages):
                prerequisite_result = material_prerequisite_check if isinstance(material_prerequisite_check, dict) else {}
                stages.append(
                    {
                        "stage": "materials-image-host",
                        "ok": False,
                        "skipped": True,
                        "message": "Skipped because material-prerequisite-check did not pass.",
                        "result": {"status": "blocked", "blockers": _string_list(prerequisite_result.get("blockers"))},
                    }
                )
            else:
                image_host_stage = await _pipeline_image_host_material_stage(config, args, source_result if isinstance(source_result, dict) else None, material_files)
                stages.append(image_host_stage)
                generated_image_host_file = image_host_stage.get("result", {}).get("image_host_file") if image_host_stage.get("ok") else None
                if generated_image_host_file and not material_files.get("image_host_file"):
                    material_files["image_host_file"] = str(generated_image_host_file)
        target_prepare = write_mteam_prepare_package(
            source_result if isinstance(source_result, dict) else None,
            target_trackers,
            stages,
            effective_content_path,
            args.target_output_dir,
            accept_rules=args.accept_rules,
            material_files=material_files,
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
        elif str(effective_target_torrent_file).endswith(".mteam-upload.torrent") and (candidate_summary := mteam_upload_torrent_candidate_summary(effective_target_torrent_file)).get("mteam_safe"):
            stages.append(
                {
                    "stage": "target-torrent-sanitize",
                    "ok": True,
                    "skipped": True,
                    "message": "Target torrent file is already a verified MTEAM upload candidate.",
                    "result": candidate_summary,
                }
            )
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

    if args.target_execute and args.upload_target:
        stages.append(_pipeline_live_material_gate_stage(stages, effective_target_torrent_file))

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
        elif args.inject_uploaded_torrent and not args.wait_uploaded_complete:
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "--wait-uploaded-complete is required with --inject-uploaded-torrent for full uploaded torrent seeding closure."})
        elif args.target_execute and not _source_ready_for_live_target_upload(stages):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "Skipped because current pipeline run did not verify complete source qBittorrent content before live target upload."})
        elif args.target_execute and not (args.uploaded_torrent_file or args.uploaded_torrent_id) and not _target_duplicate_ready_for_live_upload(stages):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "Skipped because current pipeline run did not complete a clean MTEAM duplicate check before live target upload."})
        elif args.target_execute and not _runtime_check_ready(stages):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "Skipped because focused ptcli runtime dependencies are not ready for live target upload."})
        elif args.target_execute and not _material_prerequisite_check_ready(stages):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "Skipped because metadata/material prerequisites are not ready for live target upload."})
        elif args.target_execute and not _live_material_gate_ready(stages):
            stages.append({"stage": "target-upload", "ok": False, "skipped": True, "message": "Skipped because MTEAM material and description gates are not ready for live target upload."})
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
    if args.prepare_target or args.upload_target or args.target_execute:
        _extend_unique_string(blockers, _pipeline_target_material_blockers(stages))
    closure = _pipeline_closure(stages, effective_content_path, effective_source_torrent_hash, effective_target_torrent_file)
    if live_target_upload and closure.get("complete") is not True:
        _extend_unique_string(blockers, _string_list(closure.get("blockers")))
    evidence = _pipeline_evidence(closure)
    closure_audit = _pipeline_closure_audit(closure, evidence)
    if live_target_upload:
        _extend_unique_string(blockers, _closure_audit_blockers(closure_audit))
    flow_check_summary = _pipeline_flow_check_summary(stages)
    summary = _pipeline_run_summary(stages, ready, blockers, closure, evidence)
    summary["flow"] = flow_check_summary
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
        "config": args.config,
        "base_dir": args.base_dir,
        "client": args.client,
        "qbit_options": _pipeline_qbit_options(args),
        "output_options": _pipeline_output_options(args),
        "wait_options": _pipeline_wait_options(args),
        "material_options": _pipeline_material_options(args),
        "path": effective_content_path,
        "requested_path": args.content_path,
        "source_save_path": args.save_path,
        "target_torrent_file": effective_target_torrent_file,
        "ready": ready,
        "complete": bool(closure.get("complete")),
        "blockers": blockers,
        "requested_actions": requested_actions,
        "effective_actions": effective_actions,
        "closure": closure,
        "closure_audit": closure_audit,
        "evidence": evidence,
        "flow_check": flow_check_summary,
        "summary": summary,
        "next_actions": _pipeline_next_actions(args, blockers, closure),
        "stages": stages,
    }
    payload.update(_qbit_wait_summary_fields(payload))
    payload["closure_status"] = _closure_status_summary(payload)
    if getattr(args, "write_summary", False):
        summary_file = _write_run_summary(payload, args.summary_output_dir)
        payload["summary_file"] = summary_file
        payload["automation_handoff"] = _summary_automation_handoff(summary_file)
        summary["summary_file"] = summary_file
    artifacts = _run_summary_artifacts(payload, str(payload.get("summary_file") or ""))
    payload["artifacts"] = artifacts
    payload["evidence"] = _evidence_with_target_material_chain(payload.get("evidence"), artifacts)
    payload["closure_review"] = _pipeline_closure_review(payload, artifacts)
    payload["material_diagnostics"] = _summary_material_diagnostics({"artifacts": artifacts})
    payload["target_preflight_diagnostics"] = _summary_target_preflight_diagnostics({"artifacts": artifacts})
    payload["target_upload_payload_recovery"] = _target_upload_payload_recovery_summary(artifacts)
    payload["next_actions"] = _merge_target_upload_payload_recovery_next_actions(payload.get("next_actions"), payload["target_upload_payload_recovery"])
    summary["closure_review"] = payload["closure_review"]
    summary["material_diagnostics"] = payload["material_diagnostics"]
    summary["target_preflight_diagnostics"] = payload["target_preflight_diagnostics"]
    summary["target_upload_payload_recovery"] = payload["target_upload_payload_recovery"]
    summary["next_actions"] = payload["next_actions"]
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


def _material_prerequisite_check_ready(stages: list[dict[str, Any]]) -> bool:
    stage = _find_stage(stages, "material-prerequisite-check")
    return True if stage is None or stage.get("skipped") else bool(stage.get("ok"))


def _live_material_gate_ready(stages: list[dict[str, Any]]) -> bool:
    stage = _find_stage(stages, "live-material-gate")
    return True if stage is None or stage.get("skipped") else bool(stage.get("ok"))


def _pipeline_live_material_gate_stage(stages: list[dict[str, Any]], target_torrent_file: str | None) -> dict[str, Any]:
    target_prepare = _find_stage(stages, "target-prepare")
    package = target_prepare.get("result") if isinstance(target_prepare, dict) else None
    if not isinstance(package, dict):
        result = {
            "ready_for_mteam_upload": False,
            "missing": ["target.package"],
            "blockers": ["target preparation package is missing."],
            "next_actions": ["Prepare the MTEAM target package before live target upload."],
        }
        return {"stage": "live-material-gate", "ok": False, "message": "MTEAM material live upload gate has blockers.", "result": result}

    target_materials = _target_materials_summary(package)
    preparation_audit = _target_preparation_audit(package, target_torrent_file)
    material_preparation_ready = bool(preparation_audit.get("materials_ready") and preparation_audit.get("description_ready"))
    artifacts = {
        "material_generation": _material_generation_artifacts(stages),
        "target_materials": target_materials,
        "target_materials_ready": target_materials.get("ready"),
        "target_materials_missing": _string_list(target_materials.get("missing")),
        "target_materials_warnings": _string_list(target_materials.get("warnings")),
        "target_preparation_audit": preparation_audit,
        "target_preparation_ready": material_preparation_ready,
        "target_preparation_missing": _live_material_gate_preparation_missing(preparation_audit),
        "target_payload_review": preparation_audit.get("payload_review") if isinstance(preparation_audit.get("payload_review"), dict) else {},
    }
    diagnostics = _summary_material_diagnostics({"artifacts": artifacts})
    missing = _live_material_gate_missing(diagnostics)
    next_actions = _live_material_gate_next_actions(diagnostics)
    result = {
        "ready_for_mteam_upload": diagnostics.get("ready_for_mteam_upload"),
        "gates": diagnostics.get("upload_material_gates") if isinstance(diagnostics.get("upload_material_gates"), dict) else {},
        "missing": missing,
        "blockers": _string_list(diagnostics.get("upload_material_blockers")),
        "next_actions": next_actions,
        "critical_path": diagnostics.get("critical_path") if isinstance(diagnostics.get("critical_path"), dict) else {},
        "readiness": diagnostics.get("readiness") if isinstance(diagnostics.get("readiness"), dict) else {},
    }
    return {
        "stage": "live-material-gate",
        "ok": bool(diagnostics.get("ready_for_mteam_upload")),
        "message": "MTEAM material live upload gate is ready." if diagnostics.get("ready_for_mteam_upload") else "MTEAM material live upload gate has blockers.",
        "result": result,
    }


def _live_material_gate_missing(diagnostics: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    _extend_unique_string(missing, _string_list(diagnostics.get("critical_missing")))
    _extend_unique_string(missing, _string_list(diagnostics.get("target_materials_missing")))
    _extend_unique_string(missing, _string_list(diagnostics.get("target_preparation_missing")))
    readiness = diagnostics.get("readiness") if isinstance(diagnostics.get("readiness"), dict) else {}
    for key in (
        "material_description_metadata_chain_missing",
        "material_description_media_info_chain_missing",
        "material_description_screenshot_chain_missing",
    ):
        _extend_unique_string(missing, _string_list(readiness.get(key)))
    return missing


def _live_material_gate_preparation_missing(preparation_audit: dict[str, Any]) -> list[str]:
    missing = _string_list(preparation_audit.get("missing"))
    return [item for item in missing if not (item.startswith("payload.") or item.startswith("torrent_file."))]


def _live_material_gate_next_actions(diagnostics: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    readiness = diagnostics.get("readiness") if isinstance(diagnostics.get("readiness"), dict) else {}
    for key in (
        "material_description_metadata_chain_missing",
        "material_description_media_info_chain_missing",
        "material_description_screenshot_chain_missing",
    ):
        for action in _target_preparation_missing_next_actions(_string_list(readiness.get(key))):
            _append_unique_string(actions, action)
    for action in _target_preparation_missing_next_actions(_live_material_gate_missing(diagnostics)):
        _append_unique_string(actions, action)
    return actions or _string_list(diagnostics.get("description", {}).get("completeness", {}).get("next_actions") if isinstance(diagnostics.get("description"), dict) else [])


def _pipeline_material_prerequisite_check(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if getattr(args, "enrich_metadata", False):
        checks.append(_pipeline_material_check("metadata.external_ids", True, "IMDb/TMDb/Douban enrichment is requested; source metadata, overrides, and TMDb/PTGen stages will provide final readiness."))
    if getattr(args, "fetch_ptgen", False):
        checks.append(_pipeline_material_check("metadata.ptgen_description", True, "PTGen/Douban description fetching is requested; metadata-enrich will verify the fetched description."))
    if getattr(args, "generate_mediainfo", False):
        checks.append(_pipeline_material_check("assets.mediainfo", True, "MediaInfo generation is requested; pymediainfo availability is covered by runtime-check and the generation stage will verify native parsing."))
    if getattr(args, "generate_screenshots", False):
        ffmpeg = shutil.which("ffmpeg")
        checks.append(
            _pipeline_material_check(
                "assets.ffmpeg",
                bool(ffmpeg),
                f"ffmpeg binary is available: {ffmpeg}" if ffmpeg else "ffmpeg binary is required for screenshot generation.",
            )
        )
    if getattr(args, "upload_screenshots", False) and not getattr(args, "image_host_file", None):
        checks.extend(_pipeline_image_host_prerequisite_checks(config, getattr(args, "image_host", None)))
    if not checks:
        return {
            "name": "material.prerequisites",
            "ok": True,
            "skipped": True,
            "message": "No metadata/material generation or image-host upload actions requested.",
            "checks": [],
            "blockers": [],
        }
    blockers = [str(check["message"]) for check in checks if not check.get("ok")]
    return {
        "name": "material.prerequisites",
        "ok": not blockers,
        "message": "Material prerequisites are ready." if not blockers else "Material prerequisites have blockers.",
        "checks": checks,
        "blockers": blockers,
    }


def _pipeline_material_check(name: str, ok: bool, message: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "message": message}


def _pipeline_image_host_prerequisite_checks(config: dict[str, Any], image_host: str | None) -> list[dict[str, Any]]:
    default = config.get("DEFAULT", {}) if isinstance(config, dict) else {}
    configured_host = str(image_host or default.get("img_host_1") or default.get("img_host") or default.get("imghost") or "").strip().lower() if isinstance(default, dict) else str(image_host or "").strip().lower()
    checks = [
        _pipeline_material_check(
            "assets.image_host",
            bool(configured_host),
            f"Image host is configured: {configured_host}" if configured_host else "--upload-screenshots requires --image-host, --image-host-file, or DEFAULT.img_host_1.",
        )
    ]
    if not configured_host:
        return checks
    required_keys = {
        "ptpimg": ("ptpimg_api",),
        "imgbb": ("imgbb_api",),
        "dalexni": ("dalexni_api",),
        "ptscreens": ("ptscreens_api",),
        "utppm": ("utppm_api",),
        "onlyimage": ("onlyimage_api",),
        "lensdump": ("lensdump_api",),
        "passtheimage": ("passtheimage_api", "passtheima_ge_api"),
        "zipline": ("zipline_url", "zipline_api_key"),
        "sharex": ("sharex_api_key",),
    }
    if configured_host == "pixhost":
        checks.append(_pipeline_material_check("assets.image_host.credentials", True, "pixhost upload does not require local API credentials."))
        return checks
    if configured_host == "imgbox":
        checks.append(_pipeline_material_check("assets.image_host.supported", False, "imgbox requires legacy pyimgbox; use an API image host or provide --image-host-file for focused ptcli."))
        return checks
    keys = required_keys.get(configured_host)
    if keys is None:
        checks.append(_pipeline_material_check("assets.image_host.supported", False, f"Unsupported focused ptcli image host: {configured_host}."))
        return checks
    if configured_host == "passtheimage":
        has_key = any(str(default.get(key) or "").strip() for key in keys)
        checks.append(
            _pipeline_material_check(
                "assets.image_host.credentials",
                has_key,
                "Image host credential field is configured for passtheimage." if has_key else "Image host passtheimage is missing DEFAULT.passtheima_ge_api.",
            )
        )
        return checks
    missing = [key for key in keys if not str(default.get(key) or "").strip()]
    checks.append(
        _pipeline_material_check(
            "assets.image_host.credentials",
            not missing,
            f"Image host credential fields are configured for {configured_host}." if not missing else f"Image host {configured_host} is missing DEFAULT field(s): {', '.join(missing)}.",
        )
    )
    return checks


def _source_ready_for_live_target_upload(stages: list[dict[str, Any]]) -> bool:
    source_download = _find_stage(stages, "source-download")
    inject_source = _find_stage(stages, "inject-source")
    wait_complete = _find_stage(stages, "wait-complete")
    match = _find_stage(stages, "match")
    source_content_verify = _find_stage(stages, "source-content-verify")
    source_downloaded_flow_ready = _stage_completed(source_download) and _source_injection_verified(inject_source) and _source_wait_completed(wait_complete)
    existing_content_ready = _match_stage_has_match(match) and _source_content_verified(source_content_verify) and not _wait_stage_attempt_failed(wait_complete)
    return source_downloaded_flow_ready or existing_content_ready


def _wait_stage_attempt_failed(stage: dict[str, Any] | None) -> bool:
    return bool(stage and not stage.get("skipped") and not _source_wait_completed(stage))


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


def _mteam_material_files_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "mediainfo_file": getattr(args, "mediainfo_file", None),
        "bdinfo_file": getattr(args, "bdinfo_file", None),
        "screenshot_files": list(getattr(args, "screenshot_file", []) or []),
        "image_host_file": getattr(args, "image_host_file", None),
    }


async def _pipeline_metadata_enrichment_stage(config: dict[str, Any], args: argparse.Namespace, source_info_stage: dict[str, Any]) -> dict[str, Any]:
    source_info = source_info_stage.get("result") if source_info_stage.get("ok") else None
    if not isinstance(source_info, dict):
        return {
            "stage": "metadata-enrich",
            "ok": False,
            "skipped": True,
            "message": "Skipped because source-info did not produce metadata.",
        }
    try:
        file_overrides = load_metadata_overrides(getattr(args, "metadata_file", None))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "stage": "metadata-enrich",
            "ok": False,
            "message": f"Metadata override file could not be loaded: {exc}",
            "result": {"status": "blocked", "blockers": [str(exc)]},
        }
    cli_overrides = normalize_metadata_overrides(
        {
            "imdb_id": getattr(args, "imdb_id", None),
            "tmdb_id": getattr(args, "tmdb_id", None),
            "douban_id": getattr(args, "douban_id", None),
            "douban_url": getattr(args, "douban_url", None),
        }
    )
    result = await enrich_source_metadata(
        config,
        source_info,
        overrides={**file_overrides, **cli_overrides},
        fetch_ptgen=bool(getattr(args, "fetch_ptgen", False)),
        base_dir=getattr(args, "base_dir", None),
    )
    enriched_source = result.get("source_info") if isinstance(result.get("source_info"), dict) else source_info
    stage_result = {
        **enriched_source,
        "metadata_enrichment": {key: result.get(key) for key in ("status", "ready", "applied", "missing", "readiness", "field_evidence", "sources", "blockers")},
    }
    readiness_blockers = _metadata_enrichment_readiness_blockers(result, fetch_ptgen=bool(getattr(args, "fetch_ptgen", False)))
    stage_result["metadata_enrichment"]["readiness_blockers"] = readiness_blockers
    blockers = [*_string_list(result.get("blockers")), *readiness_blockers]
    return {
        "stage": "metadata-enrich",
        "ok": not blockers,
        "result": stage_result,
        "message": "Metadata enrichment completed." if not blockers else "Metadata enrichment completed with blockers.",
    }


def _metadata_enrichment_readiness_blockers(result: dict[str, Any], *, fetch_ptgen: bool) -> list[str]:
    blockers = []
    missing = _string_list(result.get("missing"))
    if missing:
        blockers.append(f"Missing metadata after enrichment: {', '.join(missing)}")
    source_info = result.get("source_info") if isinstance(result.get("source_info"), dict) else {}
    if fetch_ptgen and not source_info.get("ptgen_description"):
        blockers.append("PTGen/Douban description is missing after enrichment.")
    return blockers


async def _pipeline_bdinfo_material_stage(
    args: argparse.Namespace,
    source_info: dict[str, Any] | None,
    content_path: str | None,
    material_files: dict[str, Any],
) -> dict[str, Any]:
    if material_files.get("bdinfo_file"):
        return {
            "stage": "materials-bdinfo",
            "ok": True,
            "skipped": True,
            "message": "Existing BDInfo material file supplied; generation skipped.",
            "result": {"status": "provided", "bdinfo_file": material_files.get("bdinfo_file")},
        }
    if not content_path:
        return {
            "stage": "materials-bdinfo",
            "ok": False,
            "skipped": True,
            "message": "--generate-bdinfo requires --path or a resolved qBittorrent content path.",
        }
    output_dir = _mteam_material_output_dir(args, source_info)
    result = await generate_bdinfo_material(str(content_path), str(output_dir), base_dir=getattr(args, "base_dir", None), playlist=getattr(args, "bdinfo_playlist", None))
    if result.get("status") == "skipped":
        return {
            "stage": "materials-bdinfo",
            "ok": True,
            "skipped": True,
            "result": result,
            "message": "BDInfo generation skipped because no BDMV structure was detected.",
        }
    return {
        "stage": "materials-bdinfo",
        "ok": result.get("status") == "generated",
        "result": result,
        "message": "Generated BDInfo material files." if result.get("status") == "generated" else "BDInfo material generation failed.",
    }


async def _pipeline_mediainfo_material_stage(
    args: argparse.Namespace,
    source_info: dict[str, Any] | None,
    content_path: str | None,
    material_files: dict[str, Any],
) -> dict[str, Any]:
    if material_files.get("mediainfo_file") or material_files.get("bdinfo_file"):
        provided: dict[str, Any] = {"status": "provided"}
        if material_files.get("mediainfo_file"):
            provided["mediainfo_file"] = material_files.get("mediainfo_file")
        if material_files.get("bdinfo_file"):
            provided["bdinfo_file"] = material_files.get("bdinfo_file")
        return {
            "stage": "materials-mediainfo",
            "ok": True,
            "skipped": True,
            "message": "Existing MediaInfo/BDInfo material file supplied; generation skipped.",
            "result": provided,
        }
    if not content_path:
        return {
            "stage": "materials-mediainfo",
            "ok": False,
            "skipped": True,
            "message": "--generate-mediainfo requires --path or a resolved qBittorrent content path.",
        }
    output_dir = _mteam_material_output_dir(args, source_info)
    result = await generate_mediainfo_material(str(content_path), str(output_dir))
    return {
        "stage": "materials-mediainfo",
        "ok": result.get("status") == "generated",
        "result": result,
        "message": "Generated MediaInfo material files." if result.get("status") == "generated" else "MediaInfo material generation failed.",
    }


async def _pipeline_screenshot_material_stage(
    args: argparse.Namespace,
    source_info: dict[str, Any] | None,
    content_path: str | None,
    material_files: dict[str, Any],
) -> dict[str, Any]:
    if material_files.get("screenshot_files"):
        screenshot_files = list(material_files.get("screenshot_files") or [])
        return {
            "stage": "materials-screenshots",
            "ok": True,
            "skipped": True,
            "message": "Existing screenshot material files supplied; generation skipped.",
            "result": {"status": "provided", "screenshot_files": screenshot_files, "count": len(screenshot_files)},
        }
    if not content_path:
        return {
            "stage": "materials-screenshots",
            "ok": False,
            "skipped": True,
            "message": "--generate-screenshots requires --path or a resolved qBittorrent content path.",
        }
    output_dir = _mteam_material_output_dir(args, source_info) / "screenshots"
    result = await generate_screenshot_materials(str(content_path), str(output_dir), count=args.screenshot_count)
    return {
        "stage": "materials-screenshots",
        "ok": result.get("status") == "generated",
        "result": result,
        "message": "Generated screenshot material files." if result.get("status") == "generated" else "Screenshot material generation failed.",
    }


async def _pipeline_image_host_material_stage(
    config: dict[str, Any],
    args: argparse.Namespace,
    source_info: dict[str, Any] | None,
    material_files: dict[str, Any],
) -> dict[str, Any]:
    if material_files.get("image_host_file"):
        return {
            "stage": "materials-image-host",
            "ok": True,
            "skipped": True,
            "message": "Existing image-host upload file supplied; upload skipped.",
            "result": {"status": "provided", "image_host_file": material_files.get("image_host_file")},
        }
    screenshot_files = list(material_files.get("screenshot_files") or [])
    if not screenshot_files:
        return {
            "stage": "materials-image-host",
            "ok": False,
            "skipped": True,
            "message": "--upload-screenshots requires --screenshot-file or successful --generate-screenshots output.",
        }
    output_dir = _mteam_material_output_dir(args, source_info)
    result = await upload_screenshot_image_hosts(config, screenshot_files, str(output_dir), image_host=args.image_host)
    return {
        "stage": "materials-image-host",
        "ok": result.get("status") == "uploaded",
        "result": result,
        "message": "Uploaded screenshots to image host." if result.get("status") == "uploaded" else "Screenshot image-host upload failed.",
    }


def _mteam_material_output_dir(args: argparse.Namespace, source_info: dict[str, Any] | None) -> Path:
    source_tracker = str(source_info.get("tracker") or args.source_tracker).upper() if isinstance(source_info, dict) else str(args.source_tracker).upper()
    source_id = str(source_info.get("torrent_id") or args.source_id).strip() if isinstance(source_info, dict) else str(args.source_id).strip()
    package_dir = Path(args.target_output_dir).expanduser() / f"{source_tracker}-{extract_torrent_id(source_id)}-to-MTEAM"
    return package_dir / "materials"


def _package_upload_resume_requested(args: argparse.Namespace) -> bool:
    return bool(args.package_dir and args.upload_target and (args.uploaded_torrent_file or args.uploaded_torrent_id) and not args.target_execute)


def _apply_pipeline_live_metadata_defaults(args: argparse.Namespace, *, live_target_upload: bool, package_upload_resume: bool) -> None:
    if live_target_upload and args.prepare_target and not package_upload_resume:
        args.enrich_metadata = _pipeline_live_material_default(args.enrich_metadata, True)
        args.fetch_ptgen = _pipeline_live_material_default(args.fetch_ptgen, bool(args.enrich_metadata))
        if args.fetch_ptgen:
            args.enrich_metadata = True
        return
    args.enrich_metadata = bool(getattr(args, "enrich_metadata", False))
    args.fetch_ptgen = bool(getattr(args, "fetch_ptgen", False))


def _apply_pipeline_live_material_defaults(args: argparse.Namespace, *, live_target_upload: bool, package_upload_resume: bool) -> None:
    if live_target_upload and args.prepare_target and not package_upload_resume:
        args.generate_bdinfo = _pipeline_live_material_default(args.generate_bdinfo, not bool(args.bdinfo_file))
        args.generate_mediainfo = _pipeline_live_material_default(args.generate_mediainfo, not bool(args.mediainfo_file or args.bdinfo_file))
        args.generate_screenshots = _pipeline_live_material_default(args.generate_screenshots, not bool(getattr(args, "screenshot_file", []) or []))
        args.upload_screenshots = _pipeline_live_material_default(args.upload_screenshots, not bool(args.image_host_file))
        return
    args.generate_bdinfo = bool(getattr(args, "generate_bdinfo", False))
    args.generate_mediainfo = bool(getattr(args, "generate_mediainfo", False))
    args.generate_screenshots = bool(getattr(args, "generate_screenshots", False))
    args.upload_screenshots = bool(getattr(args, "upload_screenshots", False))


def _pipeline_live_material_default(value: Any, enabled: bool) -> bool:
    if value is None:
        return bool(enabled)
    return bool(value)


def _source_info_from_existing_target_package(package_dir: str) -> dict[str, Any] | None:
    try:
        package = load_mteam_prepare_package(package_dir)
    except Exception:
        return None
    source_info = _source_info_from_mteam_preflight(package)
    if not isinstance(source_info, dict):
        return None
    if source_info.get("tracker"):
        source_info = {**source_info, "tracker": normalize_tracker(str(source_info["tracker"]))}
    match = re.match(r"(?P<tracker>[A-Za-z0-9]+)-(?P<torrent_id>.+)-to-[A-Za-z0-9,]+$", Path(package_dir).expanduser().name)
    if match:
        source_info = {
            **source_info,
            "tracker": source_info.get("tracker") or normalize_tracker(match.group("tracker")),
            "torrent_id": source_info.get("torrent_id") or match.group("torrent_id"),
        }
    return source_info


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
    return await asyncio.to_thread(_validate_existing_torrent_file, source_torrent_file, "Source", True)


async def _existing_uploaded_torrent_payload(uploaded_torrent_file: str) -> dict[str, Any]:
    torrent = await asyncio.to_thread(_validate_existing_torrent_file, uploaded_torrent_file, "Uploaded target", True)
    return {
        "status": "uploaded",
        "downloaded_torrent": torrent,
        "uploaded_torrent_file": torrent["path"],
    }


def _validate_existing_torrent_file(torrent_file: str, label: str, require_metadata: bool = False) -> dict[str, Any]:
    path = Path(torrent_file).expanduser()
    if not path.exists():
        raise ValueError(f"{label} torrent file not found: {path}")
    if not path.is_file():
        raise ValueError(f"{label} torrent path is not a file: {path}")
    with path.open("rb") as torrent_file:
        if torrent_file.read(1) != b"d":
            raise ValueError(f"{label} torrent file does not look like a .torrent file.")
    return {
        **_torrent_file_evidence(path, require_metadata=require_metadata),
        "reused": True,
    }


def _torrent_file_evidence(torrent_file: str | Path, *, require_metadata: bool = False) -> dict[str, Any]:
    path = Path(torrent_file).expanduser()
    payload: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "is_file": path.is_file(),
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
            payload["hash"] = infohash
            payload["torrent_hash"] = infohash
            payload["infohash"] = infohash
            payload["metadata_readable"] = True
        elif require_metadata:
            payload["metadata_readable"] = False
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
    if isinstance(result, dict) and result.get("metadata_readable") is False:
        return {
            "stage": "source-torrent-verify",
            "ok": False,
            "message": "Downloaded source torrent metadata is not readable.",
            "result": {
                "verified": False,
                "expected_hash": expected,
                "actual_hash": actual_hash,
                "blockers": ["source torrent metadata is not readable"],
            },
        }
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
    if expected and not actual_hash:
        return {
            "stage": "source-torrent-verify",
            "ok": False,
            "message": "Downloaded source torrent infohash is unavailable for source tracker metadata verification.",
            "result": {
                "verified": False,
                "expected_hash": expected,
                "actual_hash": actual_hash,
                "blockers": [f"source torrent infohash unavailable: expected {expected}"],
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


def _source_injection_hash_verify_stage(inject_stage: dict[str, Any], source_download_stage: dict[str, Any]) -> dict[str, Any]:
    if not inject_stage.get("ok"):
        return inject_stage
    expected = _torrent_hash_from_stage(source_download_stage)
    actual = _torrent_hash_from_stage(inject_stage)
    result = inject_stage.get("result")
    if not isinstance(result, dict):
        result = {}
    if expected and actual and expected != actual:
        blockers = _string_list(result.get("blockers"))
        _append_unique_string(blockers, f"injected source torrent hash mismatch: expected {expected}, got {actual}")
        return {
            **inject_stage,
            "ok": False,
            "error": "Injected source torrent infohash does not match downloaded source torrent.",
            "result": {
                **result,
                "hash_matched": False,
                "expected_hash": expected,
                "actual_hash": actual,
                "blockers": blockers,
            },
        }
    if expected and actual:
        return {
            **inject_stage,
            "result": {
                **result,
                "hash_matched": True,
                "expected_hash": expected,
                "actual_hash": actual,
            },
        }
    return inject_stage


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


def _pipeline_target_material_blockers(stages: list[dict[str, Any]]) -> list[str]:
    target_prepare = _find_stage(stages, "target-prepare")
    result = target_prepare.get("result") if isinstance(target_prepare, dict) else None
    if not isinstance(result, dict):
        return []
    materials = _target_materials_summary(result)
    if materials.get("ready"):
        return []
    warnings = _string_list(materials.get("warnings"))
    missing = _string_list(materials.get("missing"))
    blockers: list[str] = []
    for index, name in enumerate(missing):
        message = warnings[index] if index < len(warnings) else "MTEAM target material is missing."
        blockers.append(f"target.materials.{name}: {message}")
    return blockers


def _stage_result_blockers(stage_name: str, result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []
    if stage_name == "live-material-gate":
        blockers = _string_list(result.get("blockers"))
        for item in _string_list(result.get("missing")):
            _append_unique_string(blockers, item)
        return blockers
    if stage_name == "target-upload":
        return _target_upload_result_blockers(result)
    if stage_name == "metadata-enrich":
        return _metadata_enrich_result_blockers(result)
    if stage_name == "inject-source":
        return _source_inject_result_blockers(result)
    if stage_name == "wait-complete":
        return _wait_complete_result_blockers(result)
    return _string_list(result.get("blockers"))


def _metadata_enrich_result_blockers(result: dict[str, Any]) -> list[str]:
    blockers = _string_list(result.get("blockers"))
    enrichment = result.get("metadata_enrichment")
    if isinstance(enrichment, dict):
        _extend_unique_string(blockers, _string_list(enrichment.get("blockers")))
        _extend_unique_string(blockers, _string_list(enrichment.get("readiness_blockers")))
    return blockers


def _source_inject_result_blockers(result: dict[str, Any]) -> list[str]:
    blockers = _string_list(result.get("blockers"))
    if result.get("verified_in_client") is False:
        _append_unique_string(blockers, "qBittorrent did not verify the injected source torrent in the client list.")
    if not _injected_torrent_visible(result):
        _append_unique_string(blockers, "qBittorrent did not list the injected source torrent after add.")
    _extend_unique_string(blockers, _client_verification_blockers(result.get("client_verification")))
    return blockers


def _wait_complete_result_blockers(result: dict[str, Any]) -> list[str]:
    blockers = _string_list(result.get("blockers"))
    if result.get("complete") is False:
        _append_unique_string(blockers, "qBittorrent did not report the source torrent as complete.")
    elif result.get("complete") is True and not _wait_result_completed(result):
        _extend_unique_string(blockers, _wait_completion_verification_blockers(result))
    return blockers


def _target_upload_result_blockers(result: dict[str, Any]) -> list[str]:
    blockers = _string_list(result.get("blockers"))
    _extend_unique_string(blockers, _nested_blockers(result.get("downloaded_torrent"), "downloaded_torrent"))
    _extend_unique_string(blockers, _downloaded_torrent_file_blockers(result.get("downloaded_torrent")))
    _extend_unique_string(blockers, _nested_blockers(result.get("injected_torrent"), "injected_torrent"))
    _extend_unique_string(blockers, _uploaded_torrent_hash_consistency_blockers(result))
    injected_torrent = result.get("injected_torrent")
    if isinstance(injected_torrent, dict):
        if not _injected_torrent_visible(injected_torrent):
            _append_unique_string(blockers, "injected_torrent: qBittorrent did not list the injected torrent after add.")
        _extend_unique_string(blockers, [f"injected_torrent: {blocker}" for blocker in _client_verification_blockers(injected_torrent.get("client_verification"))])
    _extend_unique_string(blockers, _nested_blockers(result.get("uploaded_wait"), "uploaded_wait"))
    uploaded_wait = result.get("uploaded_wait")
    if isinstance(uploaded_wait, dict) and uploaded_wait.get("complete") is False:
        _append_unique_string(blockers, "uploaded_wait: qBittorrent did not report the uploaded target torrent as complete.")
    elif isinstance(uploaded_wait, dict) and uploaded_wait.get("complete") is True and not _wait_result_completed(uploaded_wait):
        _extend_unique_string(blockers, [f"uploaded_wait: {blocker}" for blocker in _wait_completion_verification_blockers(uploaded_wait)])
    return blockers


def _wait_completion_verification_blockers(wait_result: dict[str, Any]) -> list[str]:
    verification = wait_result.get("completion_verification")
    blockers: list[str] = []
    if isinstance(verification, dict):
        if verification.get("requested_hash_matched") is False:
            blockers.append("qBittorrent completion wait matched torrents, but not the requested hash.")
        if verification.get("requested_content_path_matched") is False:
            blockers.append("qBittorrent completion wait matched torrents, but not the requested content path.")
    elif _wait_result_request_mismatch(wait_result):
        blockers.extend(_wait_result_request_mismatch_blockers(wait_result))
    if not blockers:
        blockers.append("qBittorrent completion wait did not include matched torrent evidence.")
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
        "hash_matched": "qBittorrent did not report the expected infohash for the injected torrent.",
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
                evidence["uploaded_wait_query"] = wait_hash
        evidence.update(_uploaded_wait_hash_evidence(uploaded_wait))
    return evidence


def _uploaded_wait_hash_evidence(uploaded_wait: dict[str, Any]) -> dict[str, str]:
    hashes: list[str] = []
    matches = uploaded_wait.get("matches")
    if isinstance(matches, list):
        for match in matches:
            if isinstance(match, dict):
                torrent_hash = _normalize_torrent_hash(match.get("hash") or match.get("torrent_hash") or match.get("torrenthash"))
                if torrent_hash:
                    hashes.append(torrent_hash)
    verification = uploaded_wait.get("completion_verification")
    if isinstance(verification, dict):
        observed_hashes = verification.get("observed_hashes")
        if isinstance(observed_hashes, list):
            for value in observed_hashes:
                torrent_hash = _normalize_torrent_hash(value)
                if torrent_hash:
                    hashes.append(torrent_hash)
    unique_hashes = list(dict.fromkeys(hashes))
    if len(unique_hashes) == 1:
        return {"uploaded_wait_match": unique_hashes[0]}
    return {f"uploaded_wait_match_{index}": torrent_hash for index, torrent_hash in enumerate(unique_hashes, start=1)}


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
    artifacts = _run_summary_artifacts({"closure": closure, "evidence": evidence, "stages": stages}, "")
    material_diagnostics = _summary_material_diagnostics({"artifacts": artifacts})
    target_preflight_diagnostics = _summary_target_preflight_diagnostics({"artifacts": artifacts})
    return {
        "ready": ready,
        "complete": bool(closure.get("complete")),
        "status": "complete" if ready and closure.get("complete") else "blocked" if blockers or closure.get("blockers") else "incomplete",
        "blockers": blockers or (closure.get("blockers") if isinstance(closure.get("blockers"), list) else []),
        "closure_audit": _pipeline_closure_audit(closure, evidence),
        "material_diagnostics": material_diagnostics,
        "target_preflight_diagnostics": target_preflight_diagnostics,
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


def _pipeline_flow_check_summary(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    flow_stage = _find_stage(stages, "flow-check")
    if not isinstance(flow_stage, dict):
        return None
    result = flow_stage.get("result")
    if not isinstance(result, dict):
        return None
    return {
        "ready": bool(result.get("ready")),
        "source_tracker": result.get("source_tracker"),
        "source_torrent_id": result.get("source_torrent_id"),
        "target_trackers": result.get("target_trackers"),
        "source_capability": result.get("source_capability"),
        "target_capabilities": result.get("target_capabilities"),
        "credential_requirements": result.get("credential_requirements"),
    }


def _pipeline_requested_actions(args: argparse.Namespace) -> dict[str, bool]:
    return {
        "download_source": bool(args.download_source),
        "source_torrent_file": bool(args.source_torrent_file),
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
        "uploaded_torrent_file": bool(args.uploaded_torrent_file),
        "inject_uploaded_torrent": bool(args.inject_uploaded_torrent),
        "wait_uploaded_complete": bool(args.wait_uploaded_complete),
        "enrich_metadata": bool(getattr(args, "enrich_metadata", False)),
        "fetch_ptgen": bool(getattr(args, "fetch_ptgen", False)),
        "generate_bdinfo": bool(getattr(args, "generate_bdinfo", False)),
        "generate_mediainfo": bool(getattr(args, "generate_mediainfo", False)),
        "generate_screenshots": bool(getattr(args, "generate_screenshots", False)),
        "upload_screenshots": bool(getattr(args, "upload_screenshots", False)),
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
        "enrich_metadata": bool(getattr(args, "enrich_metadata", False)),
        "fetch_ptgen": bool(getattr(args, "fetch_ptgen", False)),
        "generate_bdinfo": bool(getattr(args, "generate_bdinfo", False)),
        "generate_mediainfo": bool(getattr(args, "generate_mediainfo", False)),
        "generate_screenshots": bool(getattr(args, "generate_screenshots", False)),
        "upload_screenshots": bool(getattr(args, "upload_screenshots", False)),
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


def _pipeline_output_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "source_output_dir": args.output_dir,
        "target_output_dir": args.target_output_dir,
        "target_torrent_output_dir": args.target_torrent_output_dir,
        "uploaded_output_dir": args.uploaded_output_dir,
        "summary_output_dir": args.summary_output_dir,
    }


def _pipeline_wait_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "source": {
            "timeout": args.wait_timeout,
            "interval": args.wait_interval,
        },
        "uploaded": {
            "timeout": args.uploaded_wait_timeout,
            "interval": args.uploaded_wait_interval,
        },
    }


def _pipeline_material_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "metadata_file": getattr(args, "metadata_file", None),
        "imdb_id": getattr(args, "imdb_id", None),
        "tmdb_id": getattr(args, "tmdb_id", None),
        "douban_id": getattr(args, "douban_id", None),
        "douban_url": getattr(args, "douban_url", None),
        "mediainfo_file": getattr(args, "mediainfo_file", None),
        "bdinfo_file": getattr(args, "bdinfo_file", None),
        "bdinfo_playlist": getattr(args, "bdinfo_playlist", None),
        "screenshot_files": list(getattr(args, "screenshot_file", []) or []),
        "screenshot_count": getattr(args, "screenshot_count", None),
        "image_host": getattr(args, "image_host", None),
        "image_host_file": getattr(args, "image_host_file", None),
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
    closure_review = payload.get("closure_review") if isinstance(payload.get("closure_review"), dict) else _pipeline_closure_review(payload, artifacts)
    material_diagnostics = _summary_material_diagnostics({"artifacts": artifacts})
    target_preflight_diagnostics = _summary_target_preflight_diagnostics({"artifacts": artifacts})
    target_upload_payload_recovery = _target_upload_payload_recovery_summary(artifacts)
    evidence = _evidence_with_target_material_chain(payload.get("evidence"), artifacts)
    summary_payload = {
        "schema_version": 1,
        "kind": "ptcli.pipeline.run_summary",
        "summary_file": str(destination),
        "automation_handoff": _summary_automation_handoff(str(destination)),
        "status": payload.get("status"),
        "source_tracker": payload.get("source_tracker"),
        "requested_source_id": payload.get("requested_source_id"),
        "input_source_id": payload.get("input_source_id"),
        "source_torrent_id": payload.get("source_torrent_id"),
        "target_trackers": payload.get("target_trackers"),
        "config": payload.get("config"),
        "base_dir": payload.get("base_dir"),
        "client": payload.get("client"),
        "qbit_options": payload.get("qbit_options"),
        "output_options": payload.get("output_options"),
        "wait_options": payload.get("wait_options"),
        "material_options": payload.get("material_options"),
        "path": payload.get("path"),
        "source_save_path": payload.get("source_save_path"),
        "target_torrent_file": payload.get("target_torrent_file"),
        "ready": payload.get("ready"),
        "complete": payload.get("complete"),
        "blockers": payload.get("blockers", []),
        "requested_actions": payload.get("requested_actions", {}),
        "effective_actions": payload.get("effective_actions", {}),
        "closure": payload.get("closure"),
        "closure_audit": payload.get("closure_audit"),
        "closure_status": payload.get("closure_status") if isinstance(payload.get("closure_status"), dict) else _closure_status_summary(payload),
        "closure_review": closure_review,
        "material_diagnostics": material_diagnostics,
        "target_preflight_diagnostics": target_preflight_diagnostics,
        "target_upload_payload_recovery": target_upload_payload_recovery,
        "flow_check": payload.get("flow_check"),
        "summary": payload.get("summary"),
        "evidence": evidence,
        "next_actions": _merge_target_upload_payload_recovery_next_actions(payload.get("next_actions", []), target_upload_payload_recovery),
        **_qbit_wait_summary_fields(payload),
        "artifacts": artifacts,
        "resume_commands": resume_commands,
        "resume_state": _run_summary_resume_state(payload, artifacts, resume_commands),
        "stages": payload.get("stages", []),
    }
    destination.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(destination)


def _run_summary_artifacts(payload: dict[str, Any], summary_file: str) -> dict[str, Any]:
    stages = payload.get("stages")
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    evidence_source = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
    evidence_target = evidence.get("target") if isinstance(evidence.get("target"), dict) else {}
    source_download = _find_stage(stages, "source-download") if isinstance(stages, list) else None
    inject_source = _find_stage(stages, "inject-source") if isinstance(stages, list) else None
    target_prepare = _find_stage(stages, "target-prepare") if isinstance(stages, list) else None
    live_material_gate = _find_stage(stages, "live-material-gate") if isinstance(stages, list) else None
    target_upload = _find_stage(stages, "target-upload") if isinstance(stages, list) else None

    artifacts: dict[str, Any] = {
        "summary_file": summary_file,
        "source_torrent_hash": payload.get("source_torrent_hash"),
        "target_torrent_file": payload.get("target_torrent_file"),
    }
    if isinstance(stages, list):
        material_generation = _material_generation_artifacts(stages)
        if material_generation:
            artifacts["material_generation"] = material_generation
    if isinstance(live_material_gate, dict):
        gate_result = live_material_gate.get("result") if isinstance(live_material_gate.get("result"), dict) else {}
        artifacts["live_material_gate"] = {
            "ready": bool(live_material_gate.get("ok")),
            "skipped": bool(live_material_gate.get("skipped")),
            "message": live_material_gate.get("error") or live_material_gate.get("message"),
            "ready_for_mteam_upload": gate_result.get("ready_for_mteam_upload") if isinstance(gate_result.get("ready_for_mteam_upload"), bool) else None,
            "gates": gate_result.get("gates") if isinstance(gate_result.get("gates"), dict) else {},
            "missing": _string_list(gate_result.get("missing")),
            "blockers": _string_list(gate_result.get("blockers")),
            "next_actions": _string_list(gate_result.get("next_actions")),
            "critical_path": gate_result.get("critical_path") if isinstance(gate_result.get("critical_path"), dict) else {},
            "readiness": gate_result.get("readiness") if isinstance(gate_result.get("readiness"), dict) else {},
        }
    qbit_wait_fields = _qbit_wait_summary_fields(payload)
    if qbit_wait_fields["qbit_wait_mismatch"]:
        artifacts["qbit_wait_mismatches"] = qbit_wait_fields["qbit_wait_mismatches"]
        artifacts["qbit_wait_retry_hints"] = qbit_wait_fields["qbit_wait_retry_hints"]
    if _artifact_value_present(evidence_source.get("hash_consistent")):
        artifacts["source_hash_consistent"] = evidence_source.get("hash_consistent")
    if _artifact_value_present(evidence_source.get("injected_torrent_hash")):
        artifacts["source_injected_torrent_hash"] = evidence_source.get("injected_torrent_hash")
    if _artifact_value_present(evidence_source.get("injection_verified")):
        artifacts["source_injection_verified"] = evidence_source.get("injection_verified")
    source_injection = evidence_source.get("qbit_closure", {}).get("injection") if isinstance(evidence_source.get("qbit_closure"), dict) else None
    if isinstance(source_injection, dict):
        artifacts["source_injection_visible_in_client"] = _injected_torrent_visible(source_injection)
    if _wait_result_completed(evidence_source.get("source_wait")):
        artifacts["source_wait_evidence"] = True
    if _artifact_value_present(evidence_target.get("hash_consistent")):
        artifacts["target_hash_consistent"] = evidence_target.get("hash_consistent")
    if _artifact_value_present(evidence_target.get("duplicate_clean")):
        artifacts["target_duplicate_clean"] = evidence_target.get("duplicate_clean")
    if _artifact_value_present(evidence_target.get("rule_obligations")):
        artifacts["target_rule_obligations"] = evidence_target.get("rule_obligations")
    if _artifact_value_present(evidence_target.get("preparation_audit")):
        artifacts["target_preparation_audit"] = evidence_target.get("preparation_audit")
        artifacts["target_preparation_ready"] = bool(evidence_target["preparation_audit"].get("ready")) if isinstance(evidence_target.get("preparation_audit"), dict) else False
        artifacts["target_preparation_missing"] = _string_list(evidence_target["preparation_audit"].get("missing")) if isinstance(evidence_target.get("preparation_audit"), dict) else []
        artifacts["target_preflight_gates"] = _target_preflight_gates({"status": "ready" if artifacts["target_preparation_ready"] else "blocked"}, artifacts["target_preparation_audit"])
    if _artifact_value_present(evidence_target.get("payload_review")):
        artifacts["target_payload_review"] = evidence_target.get("payload_review")
    if _artifact_value_present(evidence_target.get("target_material_chain")):
        artifacts["target_material_chain"] = evidence_target.get("target_material_chain")
    if "target_material_chain" not in artifacts:
        material_chain = _target_material_chain_summary(artifacts.get("target_payload_review"))
        if material_chain:
            artifacts["target_material_chain"] = material_chain
    if isinstance(source_download, dict):
        source_result = source_download.get("result")
        if isinstance(source_result, dict):
            artifacts["source_torrent_file"] = source_result.get("path")
            artifacts["source_torrent_file_evidence"] = _torrent_file_evidence_complete(source_result)
            artifacts["source_torrent_file_artifact"] = {
                key: source_result.get(key)
                for key in (
                    "path",
                    "exists",
                    "is_file",
                    "size_bytes",
                    "sha1",
                    "hash",
                    "torrent_hash",
                    "infohash",
                    "metadata_readable",
                    "reused",
                )
                if _artifact_value_present(source_result.get(key))
            }
    if isinstance(inject_source, dict):
        inject_result = inject_source.get("result")
        if isinstance(inject_result, dict):
            artifacts["source_save_path"] = inject_result.get("save_path")
            artifacts["source_qbit_category"] = inject_result.get("category")
            artifacts["source_qbit_tags"] = inject_result.get("tags")
            artifacts["source_paused"] = bool(inject_result.get("paused"))
            artifacts["source_injected_torrent_hash"] = _torrent_hash_from_result(inject_result)
            artifacts["source_injection_visible_in_client"] = _injected_torrent_visible(inject_result)
            artifacts["source_injection_verified"] = _injected_torrent_verified(inject_result)
    if isinstance(target_prepare, dict):
        prepare_result = target_prepare.get("result")
        if isinstance(prepare_result, dict):
            artifacts["target_package_dir"] = prepare_result.get("package_dir")
            artifacts["target_package_files"] = prepare_result.get("files")
            artifacts["target_materials"] = _target_materials_summary(prepare_result)
            artifacts["target_materials_ready"] = artifacts["target_materials"].get("ready")
            artifacts["target_materials_missing"] = _string_list(artifacts["target_materials"].get("missing")) if isinstance(artifacts.get("target_materials"), dict) else []
            artifacts["target_materials_warnings"] = _string_list(artifacts["target_materials"].get("warnings")) if isinstance(artifacts.get("target_materials"), dict) else []
            if "target_preparation_missing" not in artifacts and isinstance(artifacts.get("target_preparation_audit"), dict):
                artifacts["target_preparation_missing"] = _string_list(artifacts["target_preparation_audit"].get("missing"))
    if isinstance(target_upload, dict):
        upload_result = target_upload.get("result")
        if isinstance(upload_result, dict):
            artifacts["uploaded_torrent_id"] = _uploaded_torrent_id_from_result(upload_result)
            artifacts["uploaded_torrent_hash"] = _uploaded_torrent_hash_from_result(upload_result)
            artifacts["injected_torrent_hash"] = _torrent_hash_from_result(upload_result.get("injected_torrent"))
            artifacts["injection_visible_in_client"] = _injected_torrent_visible(upload_result.get("injected_torrent"))
            artifacts["injection_verified"] = _injected_torrent_verified(upload_result.get("injected_torrent"))
            if _wait_result_completed(upload_result.get("uploaded_wait")):
                artifacts["uploaded_wait_evidence"] = True
            injected_torrent = upload_result.get("injected_torrent")
            if isinstance(injected_torrent, dict):
                artifacts["uploaded_save_path"] = injected_torrent.get("save_path")
                artifacts["uploaded_qbit_category"] = injected_torrent.get("category")
                artifacts["uploaded_qbit_tags"] = injected_torrent.get("tags")
                artifacts["uploaded_paused"] = bool(injected_torrent.get("paused"))
            fresh_duplicate_check = upload_result.get("fresh_duplicate_check")
            if isinstance(fresh_duplicate_check, dict):
                artifacts["fresh_duplicate_check"] = fresh_duplicate_check
            downloaded_torrent = upload_result.get("downloaded_torrent")
            if isinstance(downloaded_torrent, dict):
                artifacts["uploaded_torrent_file"] = downloaded_torrent.get("path")
                artifacts["uploaded_torrent_file_evidence"] = _torrent_file_evidence_complete(downloaded_torrent)
    if "fresh_duplicate_check" not in artifacts and isinstance(stages, list):
        target_dupe_check = _find_stage(stages, "target-dupe-check")
        dupe_result = target_dupe_check.get("result") if isinstance(target_dupe_check, dict) else None
        if isinstance(dupe_result, dict):
            artifacts["fresh_duplicate_check"] = dupe_result
    return artifacts


def _material_generation_artifacts(stages: list[dict[str, Any]]) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    prerequisite_stage = _find_stage(stages, "material-prerequisite-check")
    if isinstance(prerequisite_stage, dict):
        result = prerequisite_stage.get("result") if isinstance(prerequisite_stage.get("result"), dict) else {}
        artifacts["prerequisites"] = {
            "ok": bool(prerequisite_stage.get("ok")),
            "skipped": bool(prerequisite_stage.get("skipped")),
            "message": prerequisite_stage.get("error") or prerequisite_stage.get("message"),
            "checks": result.get("checks") if isinstance(result.get("checks"), list) else [],
            "blockers": _string_list(result.get("blockers")),
        }
    metadata_stage = _find_stage(stages, "metadata-enrich")
    if isinstance(metadata_stage, dict):
        result = metadata_stage.get("result") if isinstance(metadata_stage.get("result"), dict) else {}
        enrichment = result.get("metadata_enrichment") if isinstance(result.get("metadata_enrichment"), dict) else {}
        artifacts["metadata"] = {
            "ok": bool(metadata_stage.get("ok")),
            "skipped": bool(metadata_stage.get("skipped")),
            "message": metadata_stage.get("error") or metadata_stage.get("message"),
            "ready": bool(enrichment.get("ready")),
            "status": enrichment.get("status"),
            "sources": enrichment.get("sources") if isinstance(enrichment.get("sources"), list) else [],
            "applied": enrichment.get("applied") if isinstance(enrichment.get("applied"), dict) else {},
            "readiness": enrichment.get("readiness") if isinstance(enrichment.get("readiness"), dict) else {},
            "field_evidence": enrichment.get("field_evidence") if isinstance(enrichment.get("field_evidence"), dict) else {},
            "missing": _string_list(enrichment.get("missing")),
            "blockers": _string_list(enrichment.get("blockers")),
            "readiness_blockers": _string_list(enrichment.get("readiness_blockers")),
            "all_blockers": [*_string_list(enrichment.get("blockers")), *_string_list(enrichment.get("readiness_blockers"))],
            "imdb_id": result.get("imdb_id"),
            "tmdb_id": result.get("tmdb_id"),
            "douban_id": result.get("douban_id"),
            "douban_url": result.get("douban_url"),
            "ptgen_description_length": len(str(result.get("ptgen_description") or "")),
        }
    _append_material_file_stage_artifact(artifacts, stages, "bdinfo", "materials-bdinfo", ("bdinfo_file", "raw_bdinfo_file"))
    _append_material_file_stage_artifact(artifacts, stages, "mediainfo", "materials-mediainfo", ("mediainfo_file", "mediainfo_summary_file", "mediainfo_json_file"))
    _append_material_file_stage_artifact(artifacts, stages, "screenshots", "materials-screenshots", ("screenshot_files",))
    _append_material_file_stage_artifact(artifacts, stages, "image_host", "materials-image-host", ("image_host_file",))
    return artifacts


def _append_material_file_stage_artifact(artifacts: dict[str, Any], stages: list[dict[str, Any]], key: str, stage_name: str, file_keys: tuple[str, ...]) -> None:
    stage = _find_stage(stages, stage_name)
    if not isinstance(stage, dict):
        return
    result = stage.get("result") if isinstance(stage.get("result"), dict) else {}
    payload: dict[str, Any] = {
        "ok": bool(stage.get("ok")),
        "skipped": bool(stage.get("skipped")),
        "message": stage.get("error") or stage.get("message"),
        "status": result.get("status"),
        "blockers": _string_list(result.get("blockers")),
    }
    for file_key in file_keys:
        if file_key in result:
            value = result.get(file_key)
            payload[file_key] = value
            evidence = _material_file_evidence(value)
            if evidence:
                payload[f"{file_key}_evidence"] = evidence
    if "media_file" in result:
        payload["media_file"] = result.get("media_file")
    if "count" in result:
        payload["count"] = result.get("count")
    if "requested_count" in result:
        payload["requested_count"] = result.get("requested_count")
    if "host" in result:
        payload["host"] = result.get("host")
    if "items" in result and isinstance(result.get("items"), list):
        payload["items"] = result.get("items")
    artifacts[key] = payload


def _material_file_evidence(value: Any) -> dict[str, Any] | list[dict[str, Any]]:
    if isinstance(value, list):
        evidence_items = [_material_file_evidence(item) for item in value]
        return [item for item in evidence_items if isinstance(item, dict)]
    if not isinstance(value, (str, Path)) or not str(value):
        return {}
    path = Path(value).expanduser()
    evidence: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
    }
    if path.is_file():
        try:
            data = path.read_bytes()
        except OSError as exc:
            evidence["read_error"] = str(exc)
        else:
            evidence["size_bytes"] = len(data)
            evidence["sha1"] = hashlib.sha1(data).hexdigest()
    return evidence


def _run_summary_resume_commands(payload: dict[str, Any], artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    source_tracker = str(payload.get("source_tracker") or "")
    source_torrent_id = str(payload.get("source_torrent_id") or "")
    target_trackers = payload.get("target_trackers")
    target_trackers_arg = ",".join(str(tracker) for tracker in target_trackers) if isinstance(target_trackers, list) else str(target_trackers or "")
    client = str(payload.get("client") or "default")
    config_args = ["--config", str(payload["config"])] if payload.get("config") else []
    base_dir_args = ["--base-dir", str(payload["base_dir"])] if payload.get("base_dir") else []
    qbit_options = payload.get("qbit_options") if isinstance(payload.get("qbit_options"), dict) else {}
    output_options = payload.get("output_options") if isinstance(payload.get("output_options"), dict) else {}
    material_options = payload.get("material_options") if isinstance(payload.get("material_options"), dict) else {}
    source_qbit_options = qbit_options.get("source") if isinstance(qbit_options.get("source"), dict) else {}
    uploaded_qbit_options = qbit_options.get("uploaded") if isinstance(qbit_options.get("uploaded"), dict) else {}
    source_output_dir = output_options.get("source_output_dir") or "./tmp/source"
    target_output_dir = output_options.get("target_output_dir") or "./tmp/target"
    uploaded_output_dir = output_options.get("uploaded_output_dir")
    uploaded_output_dir_args = ["--uploaded-output-dir", str(uploaded_output_dir)] if uploaded_output_dir else []
    summary_output_dir = output_options.get("summary_output_dir")
    summary_output_dir_args = ["--summary-output-dir", str(summary_output_dir)] if summary_output_dir else []
    wait_options = payload.get("wait_options") if isinstance(payload.get("wait_options"), dict) else {}
    source_wait_options = wait_options.get("source") if isinstance(wait_options.get("source"), dict) else {}
    uploaded_wait_options = wait_options.get("uploaded") if isinstance(wait_options.get("uploaded"), dict) else {}
    content_path = payload.get("path")
    path_args = ["--path", str(content_path)] if content_path else []
    qbit_retry_hints = artifacts.get("qbit_wait_retry_hints") if isinstance(artifacts.get("qbit_wait_retry_hints"), dict) else {}
    source_retry_hint = qbit_retry_hints.get("source") if isinstance(qbit_retry_hints.get("source"), dict) and qbit_retry_hints.get("source", {}).get("retry_recommended") is True else {}
    uploaded_retry_hint = qbit_retry_hints.get("uploaded") if isinstance(qbit_retry_hints.get("uploaded"), dict) and qbit_retry_hints.get("uploaded", {}).get("retry_recommended") is True else {}
    source_save_path = artifacts.get("source_save_path") or payload.get("source_save_path") or content_path or "/downloads"
    source_save_path = _source_qbit_wait_retry_save_path(source_retry_hint) or source_save_path
    uploaded_save_path = artifacts.get("uploaded_save_path") or content_path
    uploaded_save_path = _uploaded_wait_retry_save_path(uploaded_retry_hint) or uploaded_save_path
    uploaded_save_path_args = ["--uploaded-save-path", str(uploaded_save_path)] if uploaded_save_path else []
    source_wait_args: list[str] = []
    _append_wait_options(
        source_wait_args,
        timeout_option="--wait-timeout",
        timeout=source_wait_options.get("timeout"),
        default_timeout=3600.0,
        interval_option="--wait-interval",
        interval=source_wait_options.get("interval"),
        default_interval=30.0,
    )
    uploaded_wait_args: list[str] = []
    _append_wait_options(
        uploaded_wait_args,
        timeout_option="--uploaded-wait-timeout",
        timeout=uploaded_wait_options.get("timeout"),
        default_timeout=600.0,
        interval_option="--uploaded-wait-interval",
        interval=uploaded_wait_options.get("interval"),
        default_interval=15.0,
    )
    commands: list[dict[str, Any]] = []

    requested_actions = payload.get("requested_actions") if isinstance(payload.get("requested_actions"), dict) else {}
    effective_actions = payload.get("effective_actions") if isinstance(payload.get("effective_actions"), dict) else {}
    source_torrent_file = artifacts.get("source_torrent_file")
    source_download_planned = bool(requested_actions.get("download_source") or effective_actions.get("download_source"))
    source_closure_planned = bool(
        requested_actions.get("inject_source")
        or requested_actions.get("wait_complete")
        or effective_actions.get("inject_source")
        or effective_actions.get("wait_complete")
        or effective_actions.get("live_target_upload")
    )
    if not source_torrent_file and source_download_planned:
        source_download_args = [
            "pipeline",
            "--from",
            source_tracker,
            "--source-id",
            source_torrent_id,
            "--to",
            target_trackers_arg,
            *config_args,
            *base_dir_args,
            "--client",
            client,
            "--download-source",
            "--output-dir",
            str(source_output_dir),
        ]
        if source_closure_planned:
            source_download_args.extend(
                [
                    "--inject-source",
                    "--save-path",
                    str(source_save_path),
                    *_qbit_resume_args(source_qbit_options, prefix=""),
                    "--wait-complete",
                    *source_wait_args,
                ]
            )
        source_download_args.extend(["--accept-rules", "--write-summary", *summary_output_dir_args, "--json"])
        commands.append(_ptcli_command_entry("resume-source-download", source_download_args))

    if source_torrent_file:
        commands.append(
            _ptcli_command_entry(
                "resume-source-torrent",
                [
                    "pipeline",
                    "--from",
                    source_tracker,
                    "--source-id",
                    source_torrent_id,
                    "--to",
                    target_trackers_arg,
                    *config_args,
                    *base_dir_args,
                    "--client",
                    client,
                    "--source-torrent-file",
                    str(source_torrent_file),
                    "--inject-source",
                    "--save-path",
                    str(source_save_path),
                    *_qbit_resume_args(source_qbit_options, prefix=""),
                    "--wait-complete",
                    *source_wait_args,
                    "--accept-rules",
                    "--write-summary",
                    *summary_output_dir_args,
                    "--json",
                ],
            )
        )

    target_package_dir = artifacts.get("target_package_dir")
    target_torrent_file = artifacts.get("target_torrent_file")
    uploaded_torrent_id = artifacts.get("uploaded_torrent_id")
    target_materials_ready = bool(artifacts.get("target_materials_ready"))
    target_preparation_ready = bool(artifacts.get("target_preparation_ready"))
    target_package_planned = bool(requested_actions.get("prepare_target") or effective_actions.get("prepare_target") or effective_actions.get("live_target_upload"))
    if target_package_planned and (not target_package_dir or not target_materials_ready or not target_preparation_ready):
        commands.append(
            _ptcli_command_entry(
                "resume-target-package",
                [
                    "pipeline",
                    "--from",
                    source_tracker,
                    "--source-id",
                    source_torrent_id,
                    "--to",
                    target_trackers_arg,
                    *config_args,
                    *base_dir_args,
                    "--client",
                    client,
                    *path_args,
                    *(_target_package_material_resume_args(requested_actions, effective_actions, material_options, artifacts)),
                    "--check-dupes",
                    "--prepare-target",
                    "--target-output-dir",
                    str(target_output_dir),
                    "--accept-rules",
                    "--write-summary",
                    *summary_output_dir_args,
                    "--json",
                ],
            )
        )
    if target_package_dir and target_package_planned and target_materials_ready and target_preparation_ready and not target_torrent_file:
        commands.append(
            _ptcli_command_entry(
                "resume-target-torrent",
                [
                    "pipeline",
                    "--from",
                    source_tracker,
                    "--source-id",
                    source_torrent_id,
                    "--to",
                    target_trackers_arg,
                    *config_args,
                    *base_dir_args,
                    "--client",
                    client,
                    *path_args,
                    "--package-dir",
                    str(target_package_dir),
                    "--check-dupes",
                    "--upload-target",
                    "--export-target-torrent",
                    "--target-torrent-output-dir",
                    str(output_options.get("target_torrent_output_dir") or "./tmp/exported"),
                    "--target-execute",
                    "--confirm-upload",
                    "--download-uploaded-torrent",
                    *uploaded_output_dir_args,
                    "--inject-uploaded-torrent",
                    *uploaded_save_path_args,
                    *_qbit_resume_args(uploaded_qbit_options, prefix="uploaded-"),
                    "--wait-uploaded-complete",
                    *uploaded_wait_args,
                    "--write-summary",
                    *summary_output_dir_args,
                    "--json",
                ],
            )
        )
    if target_package_dir and target_torrent_file:
        commands.append(
            _ptcli_command_entry(
                "resume-target-upload",
                [
                    "pipeline",
                    "--from",
                    source_tracker,
                    "--source-id",
                    source_torrent_id,
                    "--to",
                    target_trackers_arg,
                    *config_args,
                    *base_dir_args,
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
                    *uploaded_output_dir_args,
                    "--inject-uploaded-torrent",
                    *uploaded_save_path_args,
                    *_qbit_resume_args(uploaded_qbit_options, prefix="uploaded-"),
                    "--wait-uploaded-complete",
                    *uploaded_wait_args,
                    "--write-summary",
                    *summary_output_dir_args,
                    "--json",
                ],
            )
        )

    uploaded_torrent_file = artifacts.get("uploaded_torrent_file")
    if target_package_dir and uploaded_torrent_id and not uploaded_torrent_file:
        commands.append(
            _ptcli_command_entry(
                "resume-uploaded-torrent-download",
                [
                    "pipeline",
                    "--from",
                    source_tracker,
                    "--source-id",
                    source_torrent_id,
                    "--to",
                    target_trackers_arg,
                    *config_args,
                    *base_dir_args,
                    "--package-dir",
                    str(target_package_dir),
                    "--client",
                    client,
                    "--upload-target",
                    "--uploaded-torrent-id",
                    str(uploaded_torrent_id),
                    "--download-uploaded-torrent",
                    *uploaded_output_dir_args,
                    "--inject-uploaded-torrent",
                    *uploaded_save_path_args,
                    *_qbit_resume_args(uploaded_qbit_options, prefix="uploaded-"),
                    "--wait-uploaded-complete",
                    *uploaded_wait_args,
                    "--write-summary",
                    *summary_output_dir_args,
                    "--json",
                ],
            )
        )
    if target_package_dir and uploaded_torrent_file:
        commands.append(
            _ptcli_command_entry(
                "resume-uploaded-torrent",
                [
                    "pipeline",
                    "--from",
                    source_tracker,
                    "--source-id",
                    source_torrent_id,
                    "--to",
                    target_trackers_arg,
                    *config_args,
                    *base_dir_args,
                    "--package-dir",
                    str(target_package_dir),
                    "--client",
                    client,
                    "--upload-target",
                    "--uploaded-torrent-file",
                    str(uploaded_torrent_file),
                    "--inject-uploaded-torrent",
                    *uploaded_save_path_args,
                    *_qbit_resume_args(uploaded_qbit_options, prefix="uploaded-"),
                    "--wait-uploaded-complete",
                    *uploaded_wait_args,
                    "--write-summary",
                    *summary_output_dir_args,
                    "--json",
                ],
            )
        )
    return commands


def _run_summary_resume_state(payload: dict[str, Any], artifacts: dict[str, Any], resume_commands: list[dict[str, Any]]) -> dict[str, Any]:
    closure = payload.get("closure") if isinstance(payload.get("closure"), dict) else {}
    blockers = closure.get("blockers") if isinstance(closure.get("blockers"), list) else []
    blockers = [str(blocker) for blocker in blockers]
    _extend_unique_string(blockers, _string_list(payload.get("blockers")))
    commands_by_stage = {str(command.get("stage")): str(command.get("command")) for command in resume_commands if isinstance(command, dict)}
    complete = bool(closure.get("complete"))
    next_command = {"stage": None, "command": None} if complete else _resume_next_command(blockers, commands_by_stage)
    next_command_argv = _resume_state_next_command_argv(next_command, resume_commands)
    materials = _run_summary_material_resume_state(payload, artifacts, resume_commands)
    return {
        "complete": complete,
        "resume_available": bool(resume_commands),
        "next_stage": next_command.get("stage"),
        "next_command": next_command.get("command"),
        "next_command_argv": next_command_argv,
        "available_stages": [str(command.get("stage")) for command in resume_commands if isinstance(command, dict)],
        "artifacts": {
            "source_torrent_file": bool(artifacts.get("source_torrent_file")),
            "source_torrent_hash": bool(artifacts.get("source_torrent_hash")),
            "source_save_path": bool(artifacts.get("source_save_path")),
            "source_qbit_category": bool(artifacts.get("source_qbit_category")),
            "source_qbit_tags": bool(artifacts.get("source_qbit_tags")),
            "source_paused": "source_paused" in artifacts,
            "source_hash_consistent": bool(artifacts.get("source_hash_consistent")),
            "source_injected_torrent_hash": bool(artifacts.get("source_injected_torrent_hash")),
            "source_injection_visible_in_client": bool(artifacts.get("source_injection_visible_in_client")),
            "source_injection_verified": bool(artifacts.get("source_injection_verified")),
            "source_wait_evidence": bool(artifacts.get("source_wait_evidence")),
            "target_package_dir": bool(artifacts.get("target_package_dir")),
            "target_materials_ready": bool(artifacts.get("target_materials_ready")),
            "target_torrent_file": bool(artifacts.get("target_torrent_file")),
            "uploaded_torrent_id": bool(artifacts.get("uploaded_torrent_id")),
            "uploaded_torrent_file": bool(artifacts.get("uploaded_torrent_file")),
            "uploaded_torrent_hash": bool(artifacts.get("uploaded_torrent_hash")),
            "injected_torrent_hash": bool(artifacts.get("injected_torrent_hash")),
            "injection_visible_in_client": bool(artifacts.get("injection_visible_in_client")),
            "injection_verified": bool(artifacts.get("injection_verified")),
            "uploaded_save_path": bool(artifacts.get("uploaded_save_path")),
            "uploaded_qbit_category": bool(artifacts.get("uploaded_qbit_category")),
            "uploaded_qbit_tags": bool(artifacts.get("uploaded_qbit_tags")),
            "uploaded_paused": "uploaded_paused" in artifacts,
            "uploaded_wait_evidence": bool(artifacts.get("uploaded_wait_evidence")),
            "target_hash_consistent": bool(artifacts.get("target_hash_consistent")),
            "target_duplicate_clean": bool(artifacts.get("target_duplicate_clean")),
            "target_rule_obligations": _rule_obligations_artifact_ready(artifacts.get("target_rule_obligations")),
            "target_preparation_ready": bool(artifacts.get("target_preparation_ready")),
        },
        "materials": materials,
        "blockers": blockers,
    }


def _run_summary_material_resume_state(payload: dict[str, Any], artifacts: dict[str, Any], resume_commands: list[dict[str, Any]]) -> dict[str, Any]:
    target_materials_missing = _string_list(artifacts.get("target_materials_missing"))
    target_preparation_missing = _string_list(artifacts.get("target_preparation_missing"))
    material_missing = _target_package_material_recovery_missing(artifacts)
    _extend_unique_string(material_missing, _target_preflight_recovery_missing(payload.get("target_preflight_diagnostics")))
    material_recovery_hints = _target_preparation_recovery_hints(material_missing)
    material_recovery_hints = _attach_material_recovery_resume_commands(material_recovery_hints, resume_commands)
    return {
        "target_materials_ready": bool(artifacts.get("target_materials_ready")),
        "target_preparation_ready": bool(artifacts.get("target_preparation_ready")),
        "target_materials_missing": target_materials_missing,
        "target_preparation_missing": target_preparation_missing,
        "closure": _run_summary_material_closure(artifacts, material_missing),
        "recovery_hints": material_recovery_hints,
        "next_actions": _target_preparation_missing_next_actions(material_missing),
    }


def _run_summary_material_closure(artifacts: dict[str, Any], material_missing: list[str]) -> dict[str, Any]:
    target_materials = artifacts.get("target_materials") if isinstance(artifacts.get("target_materials"), dict) else {}
    target_assets = target_materials.get("assets") if isinstance(target_materials.get("assets"), dict) else {}
    target_metadata = target_materials.get("metadata") if isinstance(target_materials.get("metadata"), dict) else {}
    generation = artifacts.get("material_generation") if isinstance(artifacts.get("material_generation"), dict) else {}
    preparation = artifacts.get("target_preparation_audit") if isinstance(artifacts.get("target_preparation_audit"), dict) else {}
    description = preparation.get("description") if isinstance(preparation.get("description"), dict) else {}
    media_info = description.get("media_info") if isinstance(description.get("media_info"), dict) else {}
    screenshot_coverage = description.get("screenshot_coverage") if isinstance(description.get("screenshot_coverage"), dict) else {}
    external_id_readiness = description.get("external_id_readiness") if isinstance(description.get("external_id_readiness"), dict) else {}
    external_links = description.get("external_links") if isinstance(description.get("external_links"), dict) else {}
    disc_structure = target_assets.get("disc_structure") if isinstance(target_assets.get("disc_structure"), dict) else {}
    metadata_section = generation.get("metadata") if isinstance(generation.get("metadata"), dict) else {}
    bdinfo_asset = target_assets.get("bdinfo") if isinstance(target_assets.get("bdinfo"), dict) else {}
    mediainfo_asset = target_assets.get("mediainfo") if isinstance(target_assets.get("mediainfo"), dict) else {}
    screenshot_asset = target_assets.get("screenshots") if isinstance(target_assets.get("screenshots"), dict) else {}
    image_host_asset = target_assets.get("image_hosts") if isinstance(target_assets.get("image_hosts"), dict) else {}
    description_input_chain = target_materials.get("description") if isinstance(target_materials.get("description"), dict) else {}
    bdinfo_generation = generation.get("bdinfo") if isinstance(generation.get("bdinfo"), dict) else {}
    mediainfo_generation = generation.get("mediainfo") if isinstance(generation.get("mediainfo"), dict) else {}
    screenshot_generation = generation.get("screenshots") if isinstance(generation.get("screenshots"), dict) else {}
    image_host_generation = generation.get("image_host") if isinstance(generation.get("image_host"), dict) else {}
    image_host_items = image_host_asset.get("items") if isinstance(image_host_asset.get("items"), list) else image_host_generation.get("items")
    metadata_ready = bool(target_materials.get("metadata_ready"))
    assets_ready = bool(target_materials.get("assets_ready"))
    description_ready = bool(preparation.get("description_ready"))
    bdinfo_required = bool(disc_structure.get("bdmv"))
    critical_missing = _critical_material_missing(material_missing)
    critical_domains = _material_critical_domains(critical_missing)
    target_screenshot_paths = _string_list(screenshot_asset.get("paths"))
    return {
        "ready": bool(target_materials.get("ready") and preparation.get("ready")),
        "critical_ready": not critical_missing,
        "critical_missing": critical_missing,
        "critical_domains": critical_domains,
        "critical_path": _material_critical_path_summary(
            critical_domains=critical_domains,
            target_materials_ready=target_materials.get("ready"),
            target_preparation_ready=preparation.get("ready"),
            description_ready=description_ready,
            material_missing=material_missing,
        ),
        "metadata_ready": metadata_ready,
        "assets_ready": assets_ready,
        "description_ready": description_ready,
        "missing": material_missing,
        "metadata": {
            "ready": metadata_ready,
            "generated": _material_generation_section_ready(metadata_section),
            "missing": _missing_with_prefix(material_missing, "metadata."),
            "readiness": target_metadata.get("readiness") if isinstance(target_metadata.get("readiness"), dict) else metadata_section.get("readiness") if isinstance(metadata_section.get("readiness"), dict) else {},
            "sources": target_metadata.get("sources") if isinstance(target_metadata.get("sources"), list) else metadata_section.get("sources") if isinstance(metadata_section.get("sources"), list) else [],
            "applied": target_metadata.get("applied") if isinstance(target_metadata.get("applied"), dict) else metadata_section.get("applied") if isinstance(metadata_section.get("applied"), dict) else {},
            "imdb_id": target_metadata.get("imdb_id") or metadata_section.get("imdb_id"),
            "tmdb_id": target_metadata.get("tmdb_id") or metadata_section.get("tmdb_id"),
            "douban_id": target_metadata.get("douban_id") or metadata_section.get("douban_id"),
            "douban_url": target_metadata.get("douban_url") or metadata_section.get("douban_url"),
            "ptgen_description_length": target_metadata.get("ptgen_description_length") or metadata_section.get("ptgen_description_length"),
        },
        "mediainfo": {
            "ready": bool(mediainfo_asset.get("ready")),
            "generated": _material_generation_section_ready(mediainfo_generation),
            "missing": _missing_with_prefix(material_missing, ("assets.mediainfo", "assets.mediainfo_or_bdinfo")),
            "path": mediainfo_asset.get("path") or mediainfo_generation.get("mediainfo_file"),
        },
        "bdinfo": {
            "ready": bool(bdinfo_asset.get("ready")),
            "required": bdinfo_required,
            "generated": _material_generation_section_ready(bdinfo_generation),
            "missing": _missing_with_prefix(material_missing, ("assets.bdinfo", "assets.bdinfo_for_disc")),
            "path": bdinfo_asset.get("path") or bdinfo_generation.get("bdinfo_file"),
        },
        "screenshots": {
            "ready": bool(screenshot_asset.get("ready")),
            "generated": _material_generation_section_ready(screenshot_generation),
            "missing": _missing_with_prefix(material_missing, "assets.screenshots"),
            "count": int(screenshot_asset.get("count", 0) or screenshot_generation.get("count", 0) or 0),
            "requested_count": screenshot_generation.get("requested_count"),
            "files": screenshot_generation.get("screenshot_files") if isinstance(screenshot_generation.get("screenshot_files"), list) else target_screenshot_paths,
        },
        "image_host": {
            "ready": bool(image_host_asset.get("ready")),
            "generated": _material_generation_section_ready(image_host_generation),
            "missing": _missing_with_prefix(material_missing, ("assets.image_host", "assets.image_host_uploads")),
            "host": image_host_generation.get("host"),
            "count": int(image_host_asset.get("count", 0) or image_host_generation.get("count", 0) or 0),
            "image_host_file": image_host_generation.get("image_host_file"),
            "urls": _image_host_url_summary(image_host_items),
        },
        "description": {
            "ready": description_ready,
            "input_chain": description_input_chain,
            "input_chain_ready": description_input_chain.get("ready") if isinstance(description_input_chain.get("ready"), bool) else None,
            "input_chain_missing": _string_list(description_input_chain.get("missing")),
            "input_chain_next_actions": _string_list(description_input_chain.get("next_actions")),
            "missing": _string_list(description.get("missing")),
            "path": description.get("path"),
            "has_ptgen_description": bool(description.get("has_ptgen_description")),
            "ptgen_description_length": description.get("ptgen_description_length"),
            "has_external_ids": bool(description.get("has_external_ids")),
            "external_id_readiness": external_id_readiness,
            "external_id_missing": _string_list(description.get("external_id_missing")),
            "external_links": external_links,
            "has_mediainfo_or_bdinfo": bool(description.get("has_mediainfo_or_bdinfo")),
            "media_info": {
                "has_excerpt": bool(media_info.get("has_excerpt")) if "has_excerpt" in media_info else bool(description.get("has_mediainfo_or_bdinfo")),
                "source": media_info.get("source"),
                "length": media_info.get("length"),
            },
            "has_screenshot_bbcode": bool(description.get("has_screenshot_bbcode")),
            "bbcode_image_count": int(description.get("bbcode_image_count", 0) or 0),
            "bbcode_image_urls": _string_list(description.get("bbcode_image_urls")),
            "screenshot_coverage": {
                "ready": screenshot_coverage.get("ready"),
                "expected_urls": _string_list(screenshot_coverage.get("expected_urls")),
                "description_urls": _string_list(screenshot_coverage.get("description_urls")),
                "missing_urls": _string_list(screenshot_coverage.get("missing_urls")),
            },
        },
    }


def _critical_material_missing(missing: list[str]) -> list[str]:
    critical: list[str] = []
    for item in missing:
        normalized = _target_preparation_missing_key(str(item))
        if normalized.startswith(("metadata.", "assets.", "description.")):
            _append_unique_string(critical, normalized)
    return critical


def _material_critical_domains(critical_missing: list[str]) -> dict[str, Any]:
    domains = {
        "metadata": ["metadata.imdb", "metadata.tmdb", "metadata.douban", "metadata.ptgen_description", "description.external_ids", "description.ptgen_description"],
        "media_info": ["assets.mediainfo_or_bdinfo", "assets.bdinfo_for_disc", "description.mediainfo_or_bdinfo"],
        "screenshots": ["assets.screenshots"],
        "image_host": ["assets.image_host_uploads", "description.screenshot_coverage"],
        "description": ["description.content", "description.screenshot_bbcode"],
    }
    return {
        name: {
            "ready": not any(item.startswith(tuple(prefixes)) for item in critical_missing),
            "missing": [item for item in critical_missing if item.startswith(tuple(prefixes))],
        }
        for name, prefixes in domains.items()
    }


def _material_critical_path_summary(
    *,
    critical_domains: dict[str, Any],
    target_materials_ready: Any,
    target_preparation_ready: Any,
    description_ready: Any = None,
    material_missing: list[str] | None = None,
) -> dict[str, Any]:
    material_missing = material_missing or []

    def domain_step(name: str, label: str) -> dict[str, Any]:
        domain = critical_domains.get(name) if isinstance(critical_domains.get(name), dict) else {}
        return {
            "name": name,
            "label": label,
            "ready": bool(domain.get("ready")),
            "missing": _string_list(domain.get("missing")),
        }

    description_domain = critical_domains.get("description") if isinstance(critical_domains.get("description"), dict) else {}
    description_missing = _string_list(description_domain.get("missing"))
    if description_ready is False:
        _append_unique_string(description_missing, "description.content")

    steps = [
        domain_step("metadata", "IMDb/TMDb/Douban/PTGen metadata"),
        domain_step("media_info", "MediaInfo/BDInfo"),
        domain_step("screenshots", "video screenshots"),
        domain_step("image_host", "image-host uploads"),
        {
            "name": "description",
            "label": "MTEAM description draft",
            "ready": bool(description_domain.get("ready")) and description_ready is not False,
            "missing": description_missing,
        },
        {
            "name": "target_materials",
            "label": "MTEAM material manifest",
            "ready": target_materials_ready is True,
            "missing": [] if target_materials_ready is True else _string_list(material_missing),
        },
        {
            "name": "target_preparation",
            "label": "MTEAM upload preflight",
            "ready": target_preparation_ready is True,
            "missing": [] if target_preparation_ready is True else _string_list(material_missing),
        },
    ]
    missing: list[str] = []
    for step in steps:
        _extend_unique_string(missing, _string_list(step.get("missing")))
    next_step = next((step["name"] for step in steps if not step.get("ready")), None)
    return {
        "ready": next_step is None,
        "next_step": next_step,
        "missing": missing,
        "steps": steps,
    }


def _material_generation_section_ready(section: dict[str, Any]) -> bool:
    return bool(section.get("ok") and not section.get("skipped"))


def _missing_with_prefix(missing: list[str], prefix: str | tuple[str, ...]) -> list[str]:
    prefixes = (prefix,) if isinstance(prefix, str) else prefix
    return [item for item in missing if item.startswith(prefixes)]


def _resume_next_command(blockers: list[Any], commands_by_stage: dict[str, str]) -> dict[str, str | None]:
    preferred_stages: list[str] = []
    stage_detail_preferred: list[str] = []
    stage_generic_preferred: list[str] = []
    blocker_names = {str(blocker) for blocker in blockers}
    for blocker in (str(blocker) for blocker in blockers):
        if blocker.startswith("source-download:"):
            stage_detail_preferred.extend(["resume-source-download", "resume-source-torrent"])
        elif blocker.startswith(("source-torrent-verify:", "inject-source:", "wait-complete:", "source-content-verify:")):
            stage_detail_preferred.append("resume-source-torrent")
        if blocker.startswith(("target-upload: downloaded_torrent:", "target-upload: downloaded_torrent_hash:")):
            stage_detail_preferred.append("resume-uploaded-torrent-download")
        elif blocker.startswith(("target-upload: injected_torrent:", "target-upload: uploaded_wait:", "target-upload: uploaded_torrent_hash:")):
            stage_detail_preferred.extend(["resume-uploaded-torrent", "resume-uploaded-torrent-download"])
        elif blocker.startswith("target-upload:"):
            stage_generic_preferred.append("resume-target-upload")
        elif blocker.startswith(("target-prepare:", "materials-", "metadata-enrich:")):
            stage_detail_preferred.append("resume-target-package")
    preferred_stages.extend(stage_detail_preferred)
    if (
        "source.ready" in blocker_names
        or "source.hash_consistent" in blocker_names
        or "source.wait_evidence" in blocker_names
        or "source.torrent_hash" in blocker_names
        or "source.injected_torrent_hash" in blocker_names
        or "source.injection_visible_in_client" in blocker_names
        or "source.injection_verified" in blocker_names
    ):
        preferred_stages.extend(["resume-source-torrent", "resume-source-download"])
    if "target.downloaded" in blocker_names:
        preferred_stages.append("resume-uploaded-torrent-download")
    if "target.uploaded" in blocker_names:
        preferred_stages.extend(["resume-target-torrent", "resume-target-upload"])
    if (
        "target.prepared" in blocker_names
        or "target.materials_ready" in blocker_names
        or "target_preparation_ready" in blocker_names
        or "missing audit artifact: target_preparation_ready" in blocker_names
        or "closure audit missing: target.materials_ready" in blocker_names
    ):
        preferred_stages.append("resume-target-package")
    if (
        "target.injected" in blocker_names
        or "target.seeding" in blocker_names
        or "target.hash_consistent" in blocker_names
        or "target.uploaded_wait_evidence" in blocker_names
        or "target.uploaded_torrent_hash" in blocker_names
        or "target.injected_torrent_hash" in blocker_names
        or "target.injection_visible_in_client" in blocker_names
        or "target.injection_verified" in blocker_names
    ):
        preferred_stages.extend(["resume-uploaded-torrent", "resume-uploaded-torrent-download"])
    preferred_stages.extend(stage_generic_preferred)
    preferred_stages.extend(["resume-source-torrent", "resume-source-download", "resume-target-package", "resume-target-torrent", "resume-target-upload", "resume-uploaded-torrent-download", "resume-uploaded-torrent"])
    for stage in preferred_stages:
        command = commands_by_stage.get(stage)
        if command:
            return {"stage": stage, "command": command}
    return {"stage": None, "command": None}


def _resume_next_command_from_stages(preferred_stages: tuple[str, ...], commands_by_stage: dict[str, str]) -> dict[str, str | None] | None:
    for stage in preferred_stages:
        command = commands_by_stage.get(stage)
        if command:
            return {"stage": stage, "command": command}
    return None


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


def _target_package_material_resume_args(requested_actions: dict[str, Any], effective_actions: dict[str, Any], material_options: dict[str, Any], artifacts: dict[str, Any] | None = None) -> list[str]:
    artifact_options = _target_package_material_artifact_options(artifacts)
    auto_flags = _target_package_material_auto_flags(artifacts)
    args: list[str] = []
    include_metadata = bool(
        requested_actions.get("enrich_metadata") or effective_actions.get("enrich_metadata") or requested_actions.get("fetch_ptgen") or effective_actions.get("fetch_ptgen") or "--enrich-metadata" in auto_flags or "--fetch-ptgen" in auto_flags
    )
    if requested_actions.get("enrich_metadata") or effective_actions.get("enrich_metadata") or "--enrich-metadata" in auto_flags:
        args.append("--enrich-metadata")
    if requested_actions.get("fetch_ptgen") or effective_actions.get("fetch_ptgen") or "--fetch-ptgen" in auto_flags:
        args.append("--fetch-ptgen")
    if include_metadata:
        _append_option(args, "--metadata-file", material_options.get("metadata_file"))
        _append_option(args, "--imdb-id", material_options.get("imdb_id") or artifact_options.get("imdb_id"))
        _append_option(args, "--tmdb-id", material_options.get("tmdb_id") or artifact_options.get("tmdb_id"))
        _append_option(args, "--douban-id", material_options.get("douban_id") or artifact_options.get("douban_id"))
        _append_option(args, "--douban-url", material_options.get("douban_url") or artifact_options.get("douban_url"))
    _append_option(args, "--mediainfo-file", material_options.get("mediainfo_file") or artifact_options.get("mediainfo_file"))
    _append_option(args, "--bdinfo-file", material_options.get("bdinfo_file") or artifact_options.get("bdinfo_file"))
    if requested_actions.get("generate_bdinfo") or effective_actions.get("generate_bdinfo") or "--generate-bdinfo" in auto_flags:
        args.append("--generate-bdinfo")
        _append_option(args, "--bdinfo-playlist", material_options.get("bdinfo_playlist"))
    if requested_actions.get("generate_mediainfo") or effective_actions.get("generate_mediainfo") or "--generate-mediainfo" in auto_flags:
        args.append("--generate-mediainfo")
    screenshot_files = material_options.get("screenshot_files") if isinstance(material_options.get("screenshot_files"), list) else artifact_options.get("screenshot_files")
    for screenshot_file in screenshot_files if isinstance(screenshot_files, list) else []:
        _append_option(args, "--screenshot-file", screenshot_file)
    if requested_actions.get("generate_screenshots") or effective_actions.get("generate_screenshots") or "--generate-screenshots" in auto_flags:
        args.append("--generate-screenshots")
        _append_option(args, "--screenshot-count", material_options.get("screenshot_count"))
    _append_option(args, "--image-host-file", material_options.get("image_host_file") or artifact_options.get("image_host_file"))
    if requested_actions.get("upload_screenshots") or effective_actions.get("upload_screenshots") or "--upload-screenshots" in auto_flags:
        args.append("--upload-screenshots")
        _append_option(args, "--image-host", material_options.get("image_host"))
    return args


def _target_package_material_auto_flags(artifacts: dict[str, Any] | None) -> set[str]:
    missing = _target_package_material_recovery_missing(artifacts)
    flags: set[str] = set()
    flags.update(_target_material_recovery_plan_flags(artifacts))
    for item in missing:
        normalized = _target_preparation_missing_key(item)
        if normalized in {"metadata.ptgen_description", "description.ptgen_description"}:
            flags.update({"--enrich-metadata", "--fetch-ptgen"})
        elif normalized.startswith("metadata.") or normalized.startswith("description.external_ids"):
            flags.add("--enrich-metadata")
            if normalized in {"metadata.douban", "metadata.douban_id", "metadata.douban_url", "description.external_ids", "description.external_ids.douban"}:
                flags.add("--fetch-ptgen")
        elif normalized in {"assets.mediainfo_or_bdinfo", "description.mediainfo_or_bdinfo"}:
            flags.add("--generate-mediainfo")
        elif normalized == "assets.bdinfo_for_disc":
            flags.add("--generate-bdinfo")
        elif normalized == "assets.screenshots":
            flags.add("--generate-screenshots")
        elif normalized == "description.screenshot_bbcode":
            flags.add("--prepare-target")
        elif normalized in {"assets.image_host_uploads", "description.screenshot_coverage"}:
            flags.add("--upload-screenshots")
    return flags


def _target_package_material_recovery_missing(artifacts: dict[str, Any] | None) -> list[str]:
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    missing = _string_list(artifacts.get("target_materials_missing"))
    _extend_unique_string(missing, _string_list(artifacts.get("target_preparation_missing")))
    _extend_unique_string(missing, _target_material_recovery_plan_missing(artifacts))
    _extend_unique_string(missing, _target_payload_review_description_recovery_missing(artifacts.get("target_payload_review")))
    preparation_audit = artifacts.get("target_preparation_audit") if isinstance(artifacts.get("target_preparation_audit"), dict) else {}
    _extend_unique_string(missing, _target_material_recovery_plan_missing(preparation_audit))
    _extend_unique_string(missing, _target_payload_review_description_recovery_missing(preparation_audit.get("payload_review")))
    _extend_unique_string(missing, _target_preflight_recovery_missing(artifacts.get("target_preflight_gates")))
    return missing


def _target_material_recovery_plan(artifacts: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(artifacts, dict):
        return {}
    target_materials = artifacts.get("target_materials") if isinstance(artifacts.get("target_materials"), dict) else {}
    plan = target_materials.get("recovery_plan") if isinstance(target_materials.get("recovery_plan"), dict) else {}
    if plan:
        return plan
    materials = artifacts.get("materials") if isinstance(artifacts.get("materials"), dict) else {}
    return materials.get("recovery_plan") if isinstance(materials.get("recovery_plan"), dict) else {}


def _target_material_recovery_plan_missing(artifacts: dict[str, Any] | None) -> list[str]:
    missing: list[str] = []
    plan = _target_material_recovery_plan(artifacts)
    domains = plan.get("domains") if isinstance(plan.get("domains"), list) else []
    for domain in domains:
        if isinstance(domain, dict) and domain.get("ready") is not True:
            for item in _string_list(domain.get("missing")):
                _append_unique_string(missing, "description.content" if item == "description.regenerate" else item)
    return missing


def _target_material_recovery_plan_flags(artifacts: dict[str, Any] | None) -> set[str]:
    flags: set[str] = set()
    plan = _target_material_recovery_plan(artifacts)
    domains = plan.get("domains") if isinstance(plan.get("domains"), list) else []
    for domain in domains:
        if isinstance(domain, dict) and domain.get("ready") is not True:
            flags.update(_string_list(domain.get("flags")))
    flags.discard("--metadata-file")
    flags.discard("--imdb-id")
    flags.discard("--tmdb-id")
    flags.discard("--douban-id")
    flags.discard("--douban-url")
    flags.discard("--mediainfo-file")
    flags.discard("--bdinfo-file")
    flags.discard("--screenshot-file")
    flags.discard("--image-host-file")
    flags.discard("--image-host")
    return flags


def _target_preflight_recovery_missing(target_preflight: Any) -> list[str]:
    if not isinstance(target_preflight, dict):
        return []
    missing = _string_list(target_preflight.get("missing"))
    _extend_unique_string(missing, _string_list(target_preflight.get("description_missing")))
    return missing


def _target_payload_review_description_recovery_missing(payload_review: Any) -> list[str]:
    if not isinstance(payload_review, dict):
        return []
    description = payload_review.get("description") if isinstance(payload_review.get("description"), dict) else {}
    completeness = description.get("completeness") if isinstance(description.get("completeness"), dict) else {}
    missing = _string_list(completeness.get("recovery_missing"))
    _extend_unique_string(missing, _description_evidence_recovery_missing(description.get("evidence")))
    return missing


def _description_evidence_recovery_missing(evidence: Any) -> list[str]:
    if not isinstance(evidence, dict):
        return []
    missing: list[str] = []
    ptgen = evidence.get("ptgen_description") if isinstance(evidence.get("ptgen_description"), dict) else {}
    if ptgen.get("ready") is False:
        missing.append("description.ptgen_description")
    external_ids = evidence.get("external_ids") if isinstance(evidence.get("external_ids"), dict) else {}
    external_missing = _string_list(external_ids.get("missing"))
    if external_ids.get("ready") is False:
        if external_missing:
            for name in external_missing:
                normalized = str(name).strip().lower()
                if normalized in {"imdb", "tmdb", "douban"}:
                    _append_unique_string(missing, f"description.external_ids.{normalized}")
        else:
            _append_unique_string(missing, "description.external_ids")
    metadata_chain = evidence.get("metadata_chain") if isinstance(evidence.get("metadata_chain"), dict) else {}
    if metadata_chain.get("ready") is False:
        items = metadata_chain.get("items") if isinstance(metadata_chain.get("items"), dict) else {}
        for name in ("imdb", "tmdb", "douban"):
            item = items.get(name) if isinstance(items.get(name), dict) else {}
            if not item or item.get("ready") is True:
                continue
            if not item.get("expected_link"):
                _append_unique_string(missing, f"metadata.{name if name != 'imdb' else 'imdb_id'}")
            if item.get("description_link") != item.get("expected_link"):
                _append_unique_string(missing, f"description.external_ids.{name}")
            if item.get("payload_required") and item.get("payload_value") != item.get("expected_link"):
                _append_unique_string(missing, f"payload.{name}")
    mediainfo = evidence.get("mediainfo_or_bdinfo") if isinstance(evidence.get("mediainfo_or_bdinfo"), dict) else {}
    if mediainfo.get("ready") is False:
        _append_unique_string(missing, "description.mediainfo_or_bdinfo")
    media_info_chain = evidence.get("media_info_chain") if isinstance(evidence.get("media_info_chain"), dict) else {}
    if media_info_chain.get("ready") is False:
        if not media_info_chain.get("material_source") or int(media_info_chain.get("material_length", 0) or 0) <= 0:
            _append_unique_string(missing, "assets.mediainfo_or_bdinfo")
        if media_info_chain.get("description_has_excerpt") is False:
            _append_unique_string(missing, "description.mediainfo_or_bdinfo")
        if not media_info_chain.get("payload_source") or int(media_info_chain.get("payload_length", 0) or 0) <= 0:
            _append_unique_string(missing, "payload.mediainfo")
    screenshots = evidence.get("screenshots") if isinstance(evidence.get("screenshots"), dict) else {}
    if screenshots.get("ready") is False:
        _append_unique_string(missing, "description.screenshot_bbcode")
    screenshot_coverage = evidence.get("screenshot_coverage") if isinstance(evidence.get("screenshot_coverage"), dict) else {}
    if screenshot_coverage.get("ready") is False:
        _append_unique_string(missing, "description.screenshot_coverage")
    screenshot_chain = evidence.get("screenshot_chain") if isinstance(evidence.get("screenshot_chain"), dict) else {}
    if screenshot_chain.get("ready") is False:
        if int(screenshot_chain.get("local_screenshot_count", 0) or 0) <= 0:
            _append_unique_string(missing, "assets.screenshots")
        if int(screenshot_chain.get("image_host_count", 0) or 0) <= 0:
            _append_unique_string(missing, "assets.image_host_uploads")
        if int(screenshot_chain.get("description_image_count", 0) or 0) <= 0:
            _append_unique_string(missing, "description.screenshot_bbcode")
        if _string_list(screenshot_chain.get("missing_urls")):
            _append_unique_string(missing, "description.screenshot_coverage")
    return missing


def _target_package_material_artifact_options(artifacts: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(artifacts, dict):
        return {}
    generation = artifacts.get("material_generation") if isinstance(artifacts.get("material_generation"), dict) else {}
    target_materials = artifacts.get("target_materials") if isinstance(artifacts.get("target_materials"), dict) else {}
    target_assets = target_materials.get("assets") if isinstance(target_materials.get("assets"), dict) else {}
    bdinfo = generation.get("bdinfo") if isinstance(generation.get("bdinfo"), dict) else {}
    mediainfo = generation.get("mediainfo") if isinstance(generation.get("mediainfo"), dict) else {}
    screenshots = generation.get("screenshots") if isinstance(generation.get("screenshots"), dict) else {}
    image_host = generation.get("image_host") if isinstance(generation.get("image_host"), dict) else {}
    target_bdinfo = target_assets.get("bdinfo") if isinstance(target_assets.get("bdinfo"), dict) else {}
    target_mediainfo = target_assets.get("mediainfo") if isinstance(target_assets.get("mediainfo"), dict) else {}
    target_screenshots = target_assets.get("screenshots") if isinstance(target_assets.get("screenshots"), dict) else {}
    target_image_host = target_assets.get("image_hosts") if isinstance(target_assets.get("image_hosts"), dict) else {}
    metadata = generation.get("metadata") if isinstance(generation.get("metadata"), dict) else {}
    target_metadata = target_materials.get("metadata") if isinstance(target_materials.get("metadata"), dict) else {}
    return {
        "imdb_id": metadata.get("imdb_id") or target_metadata.get("imdb_id"),
        "tmdb_id": metadata.get("tmdb_id") or target_metadata.get("tmdb_id"),
        "douban_id": metadata.get("douban_id") or target_metadata.get("douban_id"),
        "douban_url": metadata.get("douban_url") or target_metadata.get("douban_url"),
        "bdinfo_file": bdinfo.get("bdinfo_file") or target_bdinfo.get("path"),
        "mediainfo_file": mediainfo.get("mediainfo_file") or target_mediainfo.get("path"),
        "screenshot_files": screenshots.get("screenshot_files") if isinstance(screenshots.get("screenshot_files"), list) else target_screenshots.get("paths"),
        "image_host_file": image_host.get("image_host_file") or target_image_host.get("path"),
    }


def _append_option(args: list[str], option: str, value: Any) -> None:
    if value is not None and value != "":
        args.extend([option, str(value)])


def _ptcli_command(args: list[str]) -> str:
    return shlex.join(["python3", "ptcli.py", *args])


def _ptcli_command_entry(stage: str, args: list[str]) -> dict[str, Any]:
    argv = ["python3", "ptcli.py", *args]
    return {
        "stage": stage,
        "command": shlex.join(argv),
        "argv": argv,
    }


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
    if blocker.startswith("material-prerequisite-check:"):
        return "Fix the metadata/material prerequisites such as ffmpeg and image-host configuration, then rerun target preparation."
    if blocker.startswith("live-material-gate:"):
        detail = blocker.removeprefix("live-material-gate:").strip()
        return _target_preparation_missing_next_action(detail) or "Complete MTEAM material and description gates, then rerun live target upload."
    if blocker.startswith("metadata-enrich:"):
        return "Fetch or supply IMDb/TMDb/Douban metadata and PTGen/Douban description, then rerun target preparation."
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
    material_action = _target_preparation_missing_next_action(blocker)
    if material_action:
        return material_action
    return f"Fix {blocker}"


def _pipeline_closure_next_action(blocker: str) -> str:
    mapping = {
        "source.ready": "Complete the source side: use --path for existing qBittorrent content, run with --download-source --inject-source --save-path and --wait-complete, or resume with --source-torrent-file.",
        "target.prepared": "Prepare the target package with --check-dupes --prepare-target --target-output-dir, or resume with --package-dir after source content is verified.",
        "target.uploaded": "Run the target upload with --upload-target --target-execute --confirm-upload after the package and torrent candidate are ready, or resume seeding with --uploaded-torrent-file if the MTEAM torrent was already downloaded.",
        "target.downloaded": "Download the generated target torrent with --download-uploaded-torrent after live upload succeeds, or provide it with --uploaded-torrent-file.",
        "target.injected": "Inject the generated target torrent into qBittorrent with --inject-uploaded-torrent and a valid uploaded save path, or resume from --uploaded-torrent-file.",
        "target.seeding": "Wait for the injected target torrent to become complete in qBittorrent with --wait-uploaded-complete; if the torrent file is already local, resume with --uploaded-torrent-file.",
        "source.hash_consistent": "Re-verify the source torrent evidence: use the original --source-torrent-file, re-run source injection/wait, and stop if qBittorrent reports a different source hash.",
        "target.hash_consistent": "Re-verify the uploaded MTEAM torrent evidence: download or provide the generated target torrent again, inject that exact file, and stop if qBittorrent reports a different uploaded hash.",
        "target.duplicate_clean": "Re-run a fresh MTEAM duplicate check immediately before upload, and stop if MTEAM reports an existing torrent.",
        "target.rule_obligations": "Rebuild the MTEAM package after acknowledging both source download/retorrent and MTEAM upload/seed rule obligations; do not live upload until both obligations are ready.",
    }
    return mapping.get(blocker, f"Resolve closure blocker: {blocker}")


def _find_stage(stages: list[dict[str, Any]], stage_name: str) -> dict[str, Any] | None:
    for stage in stages:
        if stage.get("stage") == stage_name:
            return stage
    return None


def _latest_source_info_stage(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    return _find_stage(stages, "metadata-enrich") or _find_stage(stages, "source-info")


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
    fresh_duplicate_check = target_upload_result.get("fresh_duplicate_check") if isinstance(target_upload_result.get("fresh_duplicate_check"), dict) else target_dupe_result
    source_download_result = source_download.get("result") if source_download and isinstance(source_download.get("result"), dict) else {}
    wait_complete_result = wait_complete.get("result") if wait_complete and isinstance(wait_complete.get("result"), dict) else {}
    target_prepare_result = target_prepare.get("result") if target_prepare and isinstance(target_prepare.get("result"), dict) else {}
    if not isinstance(fresh_duplicate_check, dict) and target_upload_result.get("status") == "uploaded" and isinstance(downloaded_torrent, dict):
        fresh_duplicate_check = _duplicate_check_from_target_package(target_prepare_result)
    rule_review = target_prepare_result.get("rule_review") if isinstance(target_prepare_result, dict) else None
    rule_obligations = _rule_obligation_summary(rule_review)
    target_materials = _target_materials_summary(target_prepare_result)
    target_preparation_audit = _target_preparation_audit(target_prepare_result, target_torrent_file)
    target_payload_review = target_preparation_audit.get("payload_review") if isinstance(target_preparation_audit.get("payload_review"), dict) else {}
    source_downloaded = _stage_completed(source_download) and _torrent_file_present(source_download_result)
    source_injected = _source_injection_verified(inject_source)
    source_complete = _source_wait_completed(wait_complete)
    source_matched = _match_stage_has_match(match)
    source_content_verified = _source_content_verified(source_content_verify)
    source_hash_consistent = _source_hash_consistent(source_torrent_hash, source_download, inject_source, wait_complete, source_content_verify)
    target_injected = _injected_torrent_verified(injected_torrent)
    target_seeding = target_injected and _wait_result_completed(uploaded_wait)
    target_hash_consistent = not _uploaded_torrent_hash_consistency_blockers(target_upload_result)
    injected_target_hash = _torrent_hash_from_result(injected_torrent)
    uploaded_target_hash = target_upload_result.get("uploaded_torrent_hash") if isinstance(target_upload_result, dict) else None
    downloaded_target_hash = _torrent_hash_from_result(downloaded_torrent)
    existing_source_ready = source_matched and source_content_verified and not _wait_stage_attempt_failed(wait_complete)
    source = {
        "ready": (source_downloaded and source_injected and source_complete) or existing_source_ready,
        "downloaded": source_downloaded,
        "injected": source_injected,
        "injection_verified": source_injected,
        "injected_torrent": inject_source.get("result") if inject_source and isinstance(inject_source.get("result"), dict) else None,
        "injected_torrent_hash": _torrent_hash_from_stage(inject_source),
        "source_save_path": inject_source.get("result", {}).get("save_path") if inject_source and isinstance(inject_source.get("result"), dict) else None,
        "source_qbit_category": inject_source.get("result", {}).get("category") if inject_source and isinstance(inject_source.get("result"), dict) else None,
        "source_qbit_tags": inject_source.get("result", {}).get("tags") if inject_source and isinstance(inject_source.get("result"), dict) else None,
        "source_paused": bool(inject_source.get("result", {}).get("paused")) if inject_source and isinstance(inject_source.get("result"), dict) else False,
        "complete": source_complete,
        "matched": source_matched,
        "content_verified": source_content_verified,
        "hash_consistent": source_hash_consistent,
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
        "downloaded": _torrent_file_present(downloaded_torrent),
        "injected": target_injected,
        "injection_verified": target_injected,
        "hash_consistent": target_hash_consistent,
        "injected_torrent": injected_torrent if isinstance(injected_torrent, dict) else None,
        "seeding": target_seeding,
        "uploaded_wait": uploaded_wait if isinstance(uploaded_wait, dict) else None,
        "torrent_file": target_torrent_file,
        "uploaded_qbit_category": injected_torrent.get("category") if isinstance(injected_torrent, dict) else None,
        "uploaded_qbit_tags": injected_torrent.get("tags") if isinstance(injected_torrent, dict) else None,
        "uploaded_paused": bool(injected_torrent.get("paused")) if isinstance(injected_torrent, dict) else False,
        "uploaded_torrent_hash": uploaded_target_hash or injected_target_hash or downloaded_target_hash,
        "uploaded_torrent_id": _uploaded_torrent_id_from_result(target_upload_result),
        "injected_torrent_hash": injected_target_hash,
        "uploaded_torrent": downloaded_torrent if isinstance(downloaded_torrent, dict) else None,
        "uploaded_torrent_path": downloaded_torrent.get("path") if isinstance(downloaded_torrent, dict) else None,
        "fresh_duplicate_check": fresh_duplicate_check,
        "duplicate_clean": _fresh_duplicate_check_clean(fresh_duplicate_check),
        "rule_obligations": rule_obligations,
        "materials": target_materials,
        "materials_ready": bool(target_materials.get("ready")),
        "preparation_audit": target_preparation_audit,
        "payload_review": target_payload_review,
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
    if source.get("ready") and not source.get("hash_consistent"):
        blockers.append("source.hash_consistent")
    if target.get("uploaded") and target.get("downloaded") and target.get("injected") and not target.get("hash_consistent"):
        blockers.append("target.hash_consistent")
    if target.get("uploaded") and not target.get("duplicate_clean"):
        blockers.append("target.duplicate_clean")
    if target.get("prepared") and _target_preparation_gate_required(target) and not target.get("materials_ready"):
        blockers.append("target.materials_ready")
    rule_obligations = target.get("rule_obligations")
    if target.get("uploaded") and (not isinstance(rule_obligations, dict) or not rule_obligations.get("ready")):
        blockers.append("target.rule_obligations")
    return blockers


def _target_preparation_gate_required(target: dict[str, Any]) -> bool:
    preparation_audit = target.get("preparation_audit")
    if isinstance(preparation_audit, dict):
        payload = preparation_audit.get("payload")
        if isinstance(payload, dict) and payload.get("materials_ready_required"):
            return True
    materials = target.get("materials")
    if not isinstance(materials, dict):
        return False
    if materials.get("ready"):
        return True
    missing = _string_list(materials.get("missing"))
    if missing and missing != ["materials"]:
        return True
    metadata = materials.get("metadata")
    assets = materials.get("assets")
    return bool(metadata or assets)


def _fresh_duplicate_check_clean(fresh_duplicate_check: Any) -> bool:
    if not isinstance(fresh_duplicate_check, dict):
        return False
    return bool(fresh_duplicate_check.get("searched")) and int(fresh_duplicate_check.get("count", 0) or 0) == 0


def _duplicate_check_from_target_package(package: Any) -> dict[str, Any] | None:
    if not isinstance(package, dict):
        return None
    upload_gate = package.get("upload_gate")
    if not isinstance(upload_gate, dict):
        return None
    duplicate_check = None
    checks = upload_gate.get("checks")
    if isinstance(checks, list):
        duplicate_check = next((check for check in checks if isinstance(check, dict) and check.get("name") == "duplicate_check"), None)
    if duplicate_check is None:
        return None
    count = int(upload_gate.get("dupe_count", 0) or 0)
    check_ok = bool(duplicate_check.get("ok"))
    return {
        "searched": bool(check_ok or count > 0),
        "count": count,
        "dupes": [],
        "source": "target_package_upload_gate",
        "ok": check_ok,
        "message": duplicate_check.get("message"),
    }


def _target_materials_summary(package: Any) -> dict[str, Any]:
    materials = package.get("materials") if isinstance(package, dict) else None
    if not isinstance(materials, dict):
        return {
            "ready": False,
            "metadata_ready": False,
            "assets_ready": False,
            "metadata": {},
            "assets": {},
            "missing": ["materials"],
            "warnings": ["MTEAM materials manifest is missing."],
        }
    checks = materials.get("checks") if isinstance(materials.get("checks"), dict) else {}
    metadata_checks = checks.get("metadata") if isinstance(checks.get("metadata"), list) else []
    asset_checks = checks.get("assets") if isinstance(checks.get("assets"), list) else []
    missing = _target_material_missing_checks(metadata_checks, "metadata") + _target_material_missing_checks(asset_checks, "assets")
    assets = materials.get("assets") if isinstance(materials.get("assets"), dict) else {}
    description = materials.get("description") if isinstance(materials.get("description"), dict) else {}
    return {
        "ready": bool(materials.get("ready")),
        "metadata_ready": bool(metadata_checks) and not _target_material_missing_checks(metadata_checks, "metadata"),
        "assets_ready": bool(asset_checks) and not _target_material_missing_checks(asset_checks, "assets"),
        "metadata": materials.get("metadata") if isinstance(materials.get("metadata"), dict) else {},
        "assets": {
            "mediainfo": _material_asset_ready(assets, "mediainfo"),
            "bdinfo": _material_asset_ready(assets, "bdinfo"),
            "screenshots": _material_asset_count_ready(assets, "screenshots"),
            "image_hosts": _material_asset_count_ready(assets, "image_hosts"),
            "disc_structure": assets.get("disc_structure") if isinstance(assets.get("disc_structure"), dict) else {},
        },
        "description": description,
        "missing": missing,
        "warnings": materials.get("warnings") if isinstance(materials.get("warnings"), list) else [],
        "critical_path": materials.get("critical_path") if isinstance(materials.get("critical_path"), dict) else {},
        "recovery_plan": materials.get("recovery_plan") if isinstance(materials.get("recovery_plan"), dict) else {},
        "next_actions": materials.get("next_actions") if isinstance(materials.get("next_actions"), list) else [],
    }


def _target_preparation_audit(package: Any, target_torrent_file: str | None = None) -> dict[str, Any]:
    if not isinstance(package, dict):
        return {"ready": False, "blockers": ["target preparation package is missing."]}
    materials = _target_materials_summary(package)
    package_dir = package.get("package_dir")
    preflight = _target_preparation_preflight(package_dir, target_torrent_file)
    upload_payload = preflight.get("upload_payload") if isinstance(preflight.get("upload_payload"), dict) else {}
    description_file = upload_payload.get("description_file") if isinstance(upload_payload.get("description_file"), dict) else {}
    content = description_file.get("content") if isinstance(description_file.get("content"), dict) else {}
    review = upload_payload.get("review") if isinstance(upload_payload.get("review"), dict) else {}
    review_description = review.get("description") if isinstance(review.get("description"), dict) else {}
    review_materials = review.get("materials") if isinstance(review.get("materials"), dict) else {}
    payload_review = _payload_review_summary_from_upload_payload(upload_payload)
    material_checks = upload_payload.get("material_checks") if isinstance(upload_payload.get("material_checks"), list) else []
    payload_checks = [check for check in material_checks if isinstance(check, dict) and str(check.get("name") or "").startswith("payload.")]
    description_checks = [check for check in material_checks if isinstance(check, dict) and str(check.get("name") or "").startswith("materials.description.")]
    description_missing = _target_preparation_missing_from_checks(description_checks)
    screenshot_coverage = _target_preparation_screenshot_coverage(material_checks)
    required_description_checks = {
        "materials.description.ptgen_description",
        "materials.description.external_ids",
        "materials.description.mediainfo_or_bdinfo",
        "materials.description.screenshot_bbcode",
        "materials.description.screenshot_coverage",
    }
    description_checks_ready = bool(required_description_checks) and all(
        any(check.get("name") == name and check.get("ok") is True for check in description_checks) for name in required_description_checks
    )
    payload_ready = bool(preflight and preflight.get("status") == "ready")
    blockers: list[str] = []
    _extend_unique_string(blockers, _string_list(materials.get("warnings")) if not materials.get("ready") else [])
    _extend_unique_string(blockers, _string_list(preflight.get("blockers")) if preflight else ["target upload preflight could not be built from the package."])
    return {
        "ready": bool(materials.get("ready") and description_checks_ready and payload_ready),
        "materials_ready": bool(materials.get("ready")),
        "metadata_ready": bool(materials.get("metadata_ready")),
        "assets_ready": bool(materials.get("assets_ready")),
        "description_ready": description_checks_ready,
        "payload_ready": payload_ready,
        "package_dir": package_dir,
        "files": package.get("files") if isinstance(package.get("files"), dict) else {},
        "description": {
            "path": description_file.get("path"),
            "exists": bool(description_file.get("exists")),
            "char_length": description_file.get("char_length"),
            "expected_length": description_file.get("expected_length"),
            "has_ptgen_description": bool(content.get("has_ptgen_description")),
            "ptgen_description_length": review_description.get("ptgen_description_length"),
            "has_external_ids": bool(content.get("has_imdb") and content.get("has_tmdb") and content.get("has_douban")),
            "external_id_readiness": content.get("external_id_readiness") if isinstance(content.get("external_id_readiness"), dict) else {
                "imdb": bool(content.get("has_imdb")),
                "tmdb": bool(content.get("has_tmdb")),
                "douban": bool(content.get("has_douban")),
            },
            "external_id_missing": _string_list(content.get("external_id_missing")),
            "external_links": content.get("external_links") if isinstance(content.get("external_links"), dict) else {},
            "evidence": review_description.get("evidence") if isinstance(review_description.get("evidence"), dict) else {},
            "has_mediainfo_or_bdinfo": bool(content.get("has_mediainfo_or_bdinfo")),
            "media_info": _target_preparation_media_info(content, review_materials),
            "has_screenshot_bbcode": bool(content.get("has_screenshot_bbcode")),
            "bbcode_image_count": int(content.get("bbcode_image_count", 0) or 0),
            "bbcode_image_urls": _string_list(content.get("bbcode_image_urls")),
            "screenshot_coverage": screenshot_coverage,
            "missing": description_missing,
        },
        "payload": {
            "status": preflight.get("status") if preflight else None,
            "torrent_file": upload_payload.get("torrent_file") if isinstance(upload_payload.get("torrent_file"), dict) else None,
            "materials_ready_required": bool(upload_payload.get("materials_ready_required")) if upload_payload else False,
            "payload_checks_ready": bool(payload_checks) and all(check.get("ok") is True for check in payload_checks),
            "description_checks_ready": description_checks_ready,
        },
        "payload_review": payload_review,
        "materials": materials,
        "missing": _target_preparation_missing(materials, payload_ready, description_checks_ready, description_missing),
        "blockers": blockers,
    }


def _target_preparation_preflight(package_dir: Any, target_torrent_file: str | None) -> dict[str, Any]:
    if not package_dir:
        return {}
    try:
        return build_mteam_upload_preflight(str(package_dir), execute=True, torrent_file=target_torrent_file)
    except Exception as exc:
        return {"status": "blocked", "blockers": [f"target upload preflight failed: {exc}"]}


def _target_preparation_missing(materials: dict[str, Any], payload_ready: bool, description_ready: bool, description_missing: list[str] | None = None) -> list[str]:
    missing = _string_list(materials.get("missing"))
    _extend_unique_string(missing, _string_list(description_missing))
    if not description_ready:
        _append_unique_string(missing, "description.content")
    if not payload_ready:
        _append_unique_string(missing, "payload.preflight")
    return missing


def _target_material_missing_checks(checks: list[Any], scope: str) -> list[str]:
    return [f"{scope}.{check.get('name')}" for check in checks if isinstance(check, dict) and not check.get("ok")]


def _material_asset_ready(assets: dict[str, Any], key: str) -> dict[str, Any]:
    asset = assets.get(key) if isinstance(assets.get(key), dict) else {}
    return {
        "ready": bool(asset.get("ready")),
        "path": asset.get("path"),
    }


def _material_asset_count_ready(assets: dict[str, Any], key: str) -> dict[str, Any]:
    asset = assets.get(key) if isinstance(assets.get(key), dict) else {}
    payload = {
        "ready": bool(asset.get("ready")),
        "count": int(asset.get("count", 0) or 0),
    }
    if asset.get("path"):
        payload["path"] = asset.get("path")
    if isinstance(asset.get("files"), list):
        payload["files"] = asset.get("files")
        payload["paths"] = [file.get("path") for file in asset["files"] if isinstance(file, dict) and file.get("path")]
    if isinstance(asset.get("items"), list):
        payload["items"] = asset.get("items")
    return payload


def _source_hash_consistent(
    source_torrent_hash: str | None,
    source_download: dict[str, Any] | None,
    inject_source: dict[str, Any] | None,
    wait_complete: dict[str, Any] | None,
    source_content_verify: dict[str, Any] | None,
) -> bool:
    hashes = {_normalize_torrent_hash(source_torrent_hash)}
    hashes.add(_torrent_hash_from_stage(source_download))
    hashes.add(_torrent_hash_from_stage(inject_source))
    hashes.add(_torrent_hash_from_stage(wait_complete))
    result = source_content_verify.get("result") if isinstance(source_content_verify, dict) else None
    if isinstance(result, dict):
        matched_hashes = result.get("matched_hashes")
        if isinstance(matched_hashes, list):
            hashes.update(_normalize_torrent_hash(hash_value) for hash_value in matched_hashes)
    concrete_hashes = {hash_value for hash_value in hashes if hash_value}
    return len(concrete_hashes) <= 1


def _target_evidence_mode(target: dict[str, Any]) -> str:
    if target.get("uploaded_torrent_reused"):
        return "resumed_uploaded_torrent"
    if target.get("package_reused") and target.get("uploaded_torrent_id"):
        return "resumed_uploaded_id"
    if target.get("uploaded"):
        return "live_upload"
    if target.get("prepared"):
        return "prepared"
    return "missing"


def _pipeline_evidence(closure: dict[str, Any]) -> dict[str, Any]:
    source = closure.get("source") if isinstance(closure.get("source"), dict) else {}
    target = closure.get("target") if isinstance(closure.get("target"), dict) else {}
    target_wait = target.get("uploaded_wait") if isinstance(target.get("uploaded_wait"), dict) else {}
    target_seeding = bool(target.get("seeding") or _wait_result_completed(target_wait))
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
            "downloaded": bool(source.get("downloaded")),
            "torrent_file_evidence": _torrent_file_evidence_complete(source.get("source_torrent")),
            "injected": bool(source.get("injected")),
            "complete": bool(source.get("complete")),
            "matched": bool(source.get("matched")),
            "mode": "resumed_torrent" if source.get("source_torrent_reused") and source.get("injected") and source.get("complete") else "downloaded" if source.get("downloaded") and source.get("injected") and source.get("complete") else "matched" if source.get("matched") else "missing",
            "torrent_hash": source.get("torrent_hash"),
            "injected_torrent_hash": source.get("injected_torrent_hash"),
            "injection_verified": bool(source.get("injection_verified")),
            "content_verified": bool(source.get("content_verified")),
            "hash_consistent": bool(source.get("hash_consistent")),
            "content_verification": source.get("content_verification"),
            "content_path": source.get("content_path"),
            "source_torrent": source.get("source_torrent"),
            "source_torrent_path": source.get("source_torrent_path"),
            "source_save_path": source.get("source_save_path"),
            "source_qbit_category": source.get("source_qbit_category"),
            "source_qbit_tags": source.get("source_qbit_tags"),
            "source_paused": bool(source.get("source_paused")),
            "source_wait": source.get("source_wait"),
            "source_wait_evidence": _wait_result_completed(source.get("source_wait")),
            "qbit_closure": {
                "injection": _qbit_injection_evidence(source.get("injected_torrent")),
                "wait": _qbit_wait_evidence(source.get("source_wait")),
            },
            "source_torrent_reused": bool(source.get("source_torrent_reused")),
        },
        "target": {
            "ready": bool(target.get("prepared") and target.get("uploaded") and target.get("downloaded") and target.get("injected") and target_seeding),
            "mode": _target_evidence_mode(target),
            "prepared": bool(target.get("prepared")),
            "uploaded": bool(target.get("uploaded")),
            "downloaded": bool(target.get("downloaded")),
            "uploaded_torrent_file_evidence": _torrent_file_evidence_complete(target.get("uploaded_torrent")),
            "injected": bool(target.get("injected")),
            "seeding": target_seeding,
            "hash_consistent": bool(target.get("hash_consistent")),
            "duplicate_clean": bool(target.get("duplicate_clean")),
            "rule_obligations": target.get("rule_obligations"),
            "materials": target.get("materials"),
            "materials_ready": bool(target.get("materials_ready")),
            "preparation_audit": target.get("preparation_audit"),
            "payload_review": target.get("payload_review"),
            "torrent_file": target.get("torrent_file"),
            "uploaded_torrent_id": target.get("uploaded_torrent_id"),
            "uploaded_torrent_hash": target.get("uploaded_torrent_hash"),
            "injected_torrent_hash": target.get("injected_torrent_hash"),
            "injection_verified": bool(target.get("injection_verified")),
            "seeding_verified": target_seeding,
            "uploaded_wait": target.get("uploaded_wait"),
            "uploaded_wait_evidence": _wait_result_completed(target.get("uploaded_wait")),
            "uploaded_torrent": target.get("uploaded_torrent"),
            "uploaded_torrent_path": target.get("uploaded_torrent_path"),
            "uploaded_qbit_category": target.get("uploaded_qbit_category"),
            "uploaded_qbit_tags": target.get("uploaded_qbit_tags"),
            "uploaded_paused": bool(target.get("uploaded_paused")),
            "qbit_closure": {
                "injection": _qbit_injection_evidence(target.get("injected_torrent")),
                "wait": _qbit_wait_evidence(target.get("uploaded_wait")),
            },
            "fresh_duplicate_check": target.get("fresh_duplicate_check"),
            "package_reused": bool(target.get("package_reused")),
            "uploaded_torrent_reused": bool(target.get("uploaded_torrent_reused")),
        },
    }


def _pipeline_closure_audit(closure: dict[str, Any] | None, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    closure_source = closure.get("source") if isinstance(closure, dict) and isinstance(closure.get("source"), dict) else {}
    closure_target = closure.get("target") if isinstance(closure, dict) and isinstance(closure.get("target"), dict) else {}
    evidence_source = evidence.get("source") if isinstance(evidence, dict) and isinstance(evidence.get("source"), dict) else {}
    evidence_target = evidence.get("target") if isinstance(evidence, dict) and isinstance(evidence.get("target"), dict) else {}
    source_mode = evidence_source.get("mode")
    source_injection_required = source_mode in {"downloaded", "resumed_torrent"} or bool(
        closure_source.get("downloaded") or closure_source.get("injected") or closure_source.get("source_torrent_reused")
    )

    items: list[dict[str, Any]] = []

    def add(name: str, ok: Any, *, scope: str, evidence_keys: list[str]) -> None:
        items.append({"name": name, "scope": scope, "ok": bool(ok), "evidence": evidence_keys})

    add("source.ready", closure_source.get("ready") or evidence_source.get("ready"), scope="source", evidence_keys=["closure.source.ready", "evidence.source.ready"])
    add("source.hash_consistent", closure_source.get("hash_consistent") or evidence_source.get("hash_consistent"), scope="source", evidence_keys=["closure.source.hash_consistent", "evidence.source.hash_consistent"])
    add("source.wait_evidence", _wait_result_completed(closure_source.get("source_wait")) or bool(evidence_source.get("source_wait_evidence")), scope="source", evidence_keys=["closure.source.source_wait", "evidence.source.source_wait_evidence"])
    if source_injection_required:
        add("source.torrent_hash", closure_source.get("torrent_hash") or evidence_source.get("torrent_hash"), scope="source", evidence_keys=["closure.source.torrent_hash", "evidence.source.torrent_hash"])
        add(
            "source.injected_torrent_hash",
            closure_source.get("injected_torrent_hash") or evidence_source.get("injected_torrent_hash"),
            scope="source",
            evidence_keys=["closure.source.injected_torrent_hash", "evidence.source.injected_torrent_hash"],
        )
        add(
            "source.injection_visible_in_client",
            _injected_torrent_visible(closure_source.get("injected_torrent")) or _injected_torrent_visible(evidence_source.get("qbit_closure", {}).get("injection") if isinstance(evidence_source.get("qbit_closure"), dict) else None),
            scope="source",
            evidence_keys=["closure.source.injected_torrent", "evidence.source.qbit_closure.injection"],
        )
        add("source.injection_verified", closure_source.get("injection_verified") or evidence_source.get("injection_verified"), scope="source", evidence_keys=["closure.source.injection_verified", "evidence.source.injection_verified"])

    add("target.prepared", closure_target.get("prepared") or evidence_target.get("prepared"), scope="target", evidence_keys=["closure.target.prepared", "evidence.target.prepared"])
    add("target.uploaded", closure_target.get("uploaded") or evidence_target.get("uploaded"), scope="target", evidence_keys=["closure.target.uploaded", "evidence.target.uploaded"])
    add("target.downloaded", closure_target.get("downloaded") or evidence_target.get("downloaded"), scope="target", evidence_keys=["closure.target.downloaded", "evidence.target.downloaded"])
    add("target.injected", closure_target.get("injected") or evidence_target.get("injected"), scope="target", evidence_keys=["closure.target.injected", "evidence.target.injected"])
    add("target.seeding", closure_target.get("seeding") or evidence_target.get("seeding"), scope="target", evidence_keys=["closure.target.seeding", "evidence.target.seeding"])
    add("target.hash_consistent", closure_target.get("hash_consistent") or evidence_target.get("hash_consistent"), scope="target", evidence_keys=["closure.target.hash_consistent", "evidence.target.hash_consistent"])
    add("target.duplicate_clean", closure_target.get("duplicate_clean") or evidence_target.get("duplicate_clean"), scope="target", evidence_keys=["closure.target.duplicate_clean", "evidence.target.duplicate_clean"])
    target_rules = closure_target.get("rule_obligations") if isinstance(closure_target.get("rule_obligations"), dict) else evidence_target.get("rule_obligations")
    add("target.rule_obligations", isinstance(target_rules, dict) and target_rules.get("ready"), scope="target", evidence_keys=["closure.target.rule_obligations", "evidence.target.rule_obligations"])
    target_preparation = closure_target.get("preparation_audit") if isinstance(closure_target.get("preparation_audit"), dict) else evidence_target.get("preparation_audit")
    if closure_target.get("prepared") or evidence_target.get("prepared") or isinstance(target_preparation, dict):
        add("target.preparation_ready", isinstance(target_preparation, dict) and target_preparation.get("ready"), scope="target", evidence_keys=["closure.target.preparation_audit", "evidence.target.preparation_audit"])
    if closure_target.get("prepared") or evidence_target.get("prepared") or isinstance(evidence_target.get("materials"), dict):
        add("target.materials_ready", closure_target.get("materials_ready") or evidence_target.get("materials_ready"), scope="target", evidence_keys=["closure.target.materials_ready", "evidence.target.materials_ready"])
    add("target.uploaded_torrent_hash", closure_target.get("uploaded_torrent_hash") or evidence_target.get("uploaded_torrent_hash"), scope="target", evidence_keys=["closure.target.uploaded_torrent_hash", "evidence.target.uploaded_torrent_hash"])
    add("target.injected_torrent_hash", closure_target.get("injected_torrent_hash") or evidence_target.get("injected_torrent_hash"), scope="target", evidence_keys=["closure.target.injected_torrent_hash", "evidence.target.injected_torrent_hash"])
    target_injection_evidence = evidence_target.get("qbit_closure", {}).get("injection") if isinstance(evidence_target.get("qbit_closure"), dict) else None
    if closure_target.get("injected") or closure_target.get("injected_torrent_hash") or isinstance(target_injection_evidence, dict):
        add(
            "target.injection_visible_in_client",
            _injected_torrent_visible(closure_target.get("injected_torrent")) or _injected_torrent_visible(target_injection_evidence),
            scope="target",
            evidence_keys=["closure.target.injected_torrent", "evidence.target.qbit_closure.injection"],
        )
    add("target.injection_verified", closure_target.get("injection_verified") or evidence_target.get("injection_verified"), scope="target", evidence_keys=["closure.target.injection_verified", "evidence.target.injection_verified"])
    add("target.uploaded_wait_evidence", _wait_result_completed(closure_target.get("uploaded_wait")) or bool(evidence_target.get("uploaded_wait_evidence")), scope="target", evidence_keys=["closure.target.uploaded_wait", "evidence.target.uploaded_wait_evidence"])

    missing = [item["name"] for item in items if not item["ok"]]
    return {
        "ready": not missing,
        "missing": missing,
        "source_injection_audit_required": source_injection_required,
        "items": items,
    }


def _closure_audit_blockers(closure_audit: Any) -> list[str]:
    if not isinstance(closure_audit, dict):
        return ["closure audit missing: closure_audit"]
    return [f"closure audit missing: {name}" for name in _string_list(closure_audit.get("missing"))]


def _injected_torrent_verified(injected_torrent: Any) -> bool:
    if not isinstance(injected_torrent, dict) or injected_torrent.get("blockers"):
        return False
    if _client_verification_blockers(injected_torrent.get("client_verification")):
        return False
    if not _injected_torrent_visible(injected_torrent):
        return False
    if "verified_in_client" in injected_torrent:
        return bool(injected_torrent.get("verified_in_client"))
    client_verification = injected_torrent.get("client_verification")
    return isinstance(client_verification, dict) and bool(client_verification.get("visible"))


def _injected_torrent_visible(injected_torrent: Any) -> bool:
    if not isinstance(injected_torrent, dict):
        return False
    if "visible_in_client" in injected_torrent:
        return bool(injected_torrent.get("visible_in_client"))
    client_verification = injected_torrent.get("client_verification")
    if isinstance(client_verification, dict) and "visible" in client_verification:
        return bool(client_verification.get("visible"))
    client_matches = injected_torrent.get("client_matches")
    if isinstance(client_matches, list):
        return bool(client_matches)
    return False


def _source_injection_verified(stage: dict[str, Any] | None) -> bool:
    if not _stage_completed(stage):
        return False
    return _injected_torrent_verified(stage.get("result"))


def _source_wait_completed(stage: dict[str, Any] | None) -> bool:
    if not _stage_completed(stage):
        return False
    result = stage.get("result")
    return _wait_result_completed(result)


def _wait_result_completed(wait_result: Any) -> bool:
    if not isinstance(wait_result, dict) or not wait_result.get("complete"):
        return False
    verification = wait_result.get("completion_verification")
    if isinstance(verification, dict):
        if verification.get("any_complete") is False or verification.get("complete_count") == 0:
            return False
        if verification.get("matched_count") == 0:
            return False
        if verification.get("requested_hash_matched") is False:
            return False
        if verification.get("requested_content_path_matched") is False:
            return False
    elif _wait_result_request_mismatch(wait_result):
        return False
    matches = wait_result.get("matches")
    if isinstance(matches, list):
        return any(_match_has_evidence(match) for match in matches)
    matched_count = wait_result.get("matched_count")
    if matched_count is not None:
        try:
            return int(matched_count) > 0
        except (TypeError, ValueError):
            return False
    return False


def _wait_result_request_mismatch(wait_result: dict[str, Any]) -> bool:
    query = wait_result.get("query")
    matches = wait_result.get("matches")
    if not isinstance(query, dict) or not isinstance(matches, list):
        return False
    requested_hash = _normalize_torrent_hash(query.get("torrent_hash"))
    if requested_hash and not any(_normalize_torrent_hash(match.get("hash") or match.get("torrent_hash") or match.get("torrenthash")) == requested_hash for match in matches if isinstance(match, dict)):
        return True
    requested_path = query.get("content_path")
    return bool(requested_path) and not any(_match_path_matches_request(match, str(requested_path)) for match in matches if isinstance(match, dict))


def _wait_result_request_mismatch_blockers(wait_result: dict[str, Any]) -> list[str]:
    query = wait_result.get("query")
    if not isinstance(query, dict):
        return []
    blockers: list[str] = []
    if query.get("torrent_hash"):
        requested_hash = _normalize_torrent_hash(query.get("torrent_hash"))
        matches = wait_result.get("matches")
        if requested_hash and isinstance(matches, list) and not any(_normalize_torrent_hash(match.get("hash") or match.get("torrent_hash") or match.get("torrenthash")) == requested_hash for match in matches if isinstance(match, dict)):
            blockers.append("qBittorrent completion wait matched torrents, but not the requested hash.")
    if query.get("content_path"):
        requested_path = str(query["content_path"])
        matches = wait_result.get("matches")
        if isinstance(matches, list) and not any(_match_path_matches_request(match, requested_path) for match in matches if isinstance(match, dict)):
            blockers.append("qBittorrent completion wait matched torrents, but not the requested content path.")
    return blockers


def _match_path_matches_request(match: dict[str, Any], requested_path: str) -> bool:
    normalized_request = os.path.normpath(requested_path)
    for key in ("content_path", "save_path", "path"):
        value = match.get(key)
        if not value:
            continue
        normalized_value = os.path.normpath(str(value))
        if normalized_value == normalized_request or normalized_value.startswith(f"{normalized_request}{os.sep}"):
            return True
    return False


def _source_content_verified(stage: dict[str, Any] | None) -> bool:
    return True if stage is None else _stage_completed(stage)


def _stage_completed(stage: dict[str, Any] | None) -> bool:
    return bool(stage and stage.get("ok") and not stage.get("skipped"))


def _torrent_file_present(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if not (value.get("path") or value.get("torrent_path")):
        return False
    if value.get("exists") is False:
        return False
    if value.get("size_bytes") == 0:
        return False
    return value.get("metadata_readable") is not False


def _torrent_file_evidence_complete(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if not (value.get("path") or value.get("torrent_path")):
        return False
    if value.get("exists") is not True:
        return False
    if not isinstance(value.get("size_bytes"), int) or value["size_bytes"] <= 0:
        return False
    sha1 = value.get("sha1")
    if not isinstance(sha1, str) or len(sha1.strip()) != 40:
        return False
    return bool(_torrent_hash_from_result(value)) and value.get("metadata_readable") is True


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
    upload_torrent_file = target_torrent_file or args.target_torrent_file
    if args.target_execute:
        fresh_dupe_check = await _fresh_mteam_dupe_check_for_target_package(config, package_dir)
        dupe_blockers = _fresh_mteam_dupe_check_blockers(fresh_dupe_check)
        if dupe_blockers:
            return {"status": "blocked", "fresh_duplicate_check": fresh_dupe_check, "blockers": dupe_blockers}
    else:
        fresh_dupe_check = None
    result = await upload_mteam_from_package(
        config,
        package_dir,
        upload_torrent_file,
        execute=args.target_execute,
        confirm_upload=args.confirm_upload,
        write_payload=args.write_payload,
        download_uploaded=args.download_uploaded_torrent,
        uploaded_output_dir=args.uploaded_output_dir,
    )
    if isinstance(fresh_dupe_check, dict):
        result = {**result, "fresh_duplicate_check": fresh_dupe_check}
    return await _apply_uploaded_torrent_followup(config, args, result, args.uploaded_save_path or inferred_content_path)


async def _fresh_mteam_dupe_check_for_target_package(config: dict[str, Any], package_dir: str) -> dict[str, Any]:
    package = load_mteam_prepare_package(package_dir)
    return await search_mteam_duplicates(config, _source_info_from_mteam_preflight(package))


async def _apply_uploaded_torrent_followup(config: dict[str, Any], args: argparse.Namespace, result: dict[str, Any], uploaded_save_path: str | None) -> dict[str, Any]:
    result = _with_downloaded_torrent_file_evidence(result)
    if args.inject_uploaded_torrent and result.get("status") == "uploaded" and isinstance(result.get("downloaded_torrent"), dict):
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
    evidence = _torrent_file_evidence(str(downloaded_torrent["path"]), require_metadata=True)
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
    if uploaded_torrent_hash and not _normalize_torrent_hash(payload.get("uploaded_torrent_hash")):
        payload["uploaded_torrent_hash"] = uploaded_torrent_hash
    if uploaded_torrent_hash:
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


def build_plan_commands(
    source_tracker: str,
    source_torrent_id: str,
    target_trackers: list[str],
    content_path: str | None,
    *,
    config: str | None = None,
    client: str = "default",
    base_dir: str | None = None,
) -> list[dict[str, Any]]:
    target_trackers_arg = ",".join(target_trackers)
    retorrent_path_arg = f"--path {json.dumps(content_path, ensure_ascii=False)}" if content_path else '--save-path "/downloads"'
    doctor_path_arg = f"--path {json.dumps(content_path, ensure_ascii=False)}" if content_path else '--uploaded-save-path "/downloads"'
    uploaded_save_path_arg = f"--uploaded-save-path {json.dumps(content_path, ensure_ascii=False)}" if content_path else '--uploaded-save-path "/downloads"'
    commands = [
        _plan_command_entry(
            "source-info",
            f"python3 ptcli.py source-info --tracker {source_tracker} --source-id {source_torrent_id} --json",
            ["source-info", "--tracker", source_tracker, "--source-id", source_torrent_id, "--json"],
        ),
        _plan_command_entry(
            "source-download",
            f"python3 ptcli.py source-download --tracker {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --output-dir ./tmp/source --accept-rules --json",
            ["source-download", "--tracker", source_tracker, "--source-id", source_torrent_id, "--to", target_trackers_arg, "--output-dir", "./tmp/source", "--accept-rules", "--json"],
        ),
        _plan_command_entry(
            "resume-source-torrent",
            f"python3 ptcli.py pipeline --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --source-torrent-file ./tmp/source/{source_tracker}-{source_torrent_id}.torrent --inject-source --save-path \"/downloads\" --wait-complete --accept-rules --json",
            ["pipeline", "--from", source_tracker, "--source-id", source_torrent_id, "--to", target_trackers_arg, "--source-torrent-file", f"./tmp/source/{source_tracker}-{source_torrent_id}.torrent", "--inject-source", "--save-path", "/downloads", "--wait-complete", "--accept-rules", "--json"],
        ),
        _plan_command_entry(
            "rules",
            f"python3 ptcli.py rules --trackers {source_tracker},{target_trackers_arg} --json",
            ["rules", "--trackers", f"{source_tracker},{target_trackers_arg}", "--json"],
        ),
        _plan_command_entry(
            "resume-target-package",
            f"python3 ptcli.py pipeline --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --package-dir ./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg} --upload-target --target-torrent-file ./tmp/exported/mteam.torrent --accept-rules --target-execute --confirm-upload --download-uploaded-torrent --inject-uploaded-torrent {uploaded_save_path_arg} --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json",
            ["pipeline", "--from", source_tracker, "--source-id", source_torrent_id, "--to", target_trackers_arg, "--package-dir", f"./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg}", "--upload-target", "--target-torrent-file", "./tmp/exported/mteam.torrent", "--accept-rules", "--target-execute", "--confirm-upload", "--download-uploaded-torrent", "--inject-uploaded-torrent", "--uploaded-save-path", content_path or "/downloads", "--uploaded-qbit-category", "MTEAM", "--uploaded-qbit-tags", "retorrent", "--wait-uploaded-complete", "--write-summary", "--json"],
        ),
        _plan_command_entry(
            "resume-uploaded-torrent",
            f"python3 ptcli.py pipeline --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --package-dir ./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg} --upload-target --uploaded-torrent-file ./tmp/uploaded/MTEAM-<id>.torrent {uploaded_save_path_arg} --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --json",
            ["pipeline", "--from", source_tracker, "--source-id", source_torrent_id, "--to", target_trackers_arg, "--package-dir", f"./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg}", "--upload-target", "--uploaded-torrent-file", "./tmp/uploaded/MTEAM-<id>.torrent", "--uploaded-save-path", content_path or "/downloads", "--uploaded-qbit-category", "MTEAM", "--uploaded-qbit-tags", "retorrent", "--json"],
        ),
        _plan_command_entry(
            "resume-uploaded-torrent-download",
            f"python3 ptcli.py pipeline --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --package-dir ./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg} --upload-target --uploaded-torrent-id <id> --uploaded-output-dir ./tmp/uploaded {uploaded_save_path_arg} --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --json",
            ["pipeline", "--from", source_tracker, "--source-id", source_torrent_id, "--to", target_trackers_arg, "--package-dir", f"./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg}", "--upload-target", "--uploaded-torrent-id", "<id>", "--uploaded-output-dir", "./tmp/uploaded", "--uploaded-save-path", content_path or "/downloads", "--uploaded-qbit-category", "MTEAM", "--uploaded-qbit-tags", "retorrent", "--json"],
        ),
        _plan_command_entry(
            "retorrent-resume-uploaded-torrent",
            f"python3 ptcli.py retorrent --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --execute --accept-rules --confirm-upload --package-dir ./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg} --uploaded-torrent-file ./tmp/uploaded/MTEAM-<id>.torrent --inject-uploaded-torrent {uploaded_save_path_arg} --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json",
            ["retorrent", "--from", source_tracker, "--source-id", source_torrent_id, "--to", target_trackers_arg, "--execute", "--accept-rules", "--confirm-upload", "--package-dir", f"./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg}", "--uploaded-torrent-file", "./tmp/uploaded/MTEAM-<id>.torrent", "--inject-uploaded-torrent", "--uploaded-save-path", content_path or "/downloads", "--uploaded-qbit-category", "MTEAM", "--uploaded-qbit-tags", "retorrent", "--write-summary", "--json"],
        ),
        _plan_command_entry(
            "retorrent-resume-uploaded-torrent-download",
            f"python3 ptcli.py retorrent --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --execute --accept-rules --confirm-upload --package-dir ./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg} --uploaded-torrent-id <id> --download-uploaded-torrent --inject-uploaded-torrent {uploaded_save_path_arg} --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json",
            ["retorrent", "--from", source_tracker, "--source-id", source_torrent_id, "--to", target_trackers_arg, "--execute", "--accept-rules", "--confirm-upload", "--package-dir", f"./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg}", "--uploaded-torrent-id", "<id>", "--download-uploaded-torrent", "--inject-uploaded-torrent", "--uploaded-save-path", content_path or "/downloads", "--uploaded-qbit-category", "MTEAM", "--uploaded-qbit-tags", "retorrent", "--write-summary", "--json"],
        ),
        _plan_command_entry(
            "doctor-live",
            f"python3 ptcli.py doctor --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} {doctor_path_arg} --package-dir ./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg} --target-torrent-file ./tmp/exported/mteam.torrent --accept-rules --target-execute --confirm-upload --download-uploaded-torrent --inject-uploaded-torrent --wait-uploaded-complete --connect-qbit --probe-source --probe-target --json",
            ["doctor", "--from", source_tracker, "--source-id", source_torrent_id, "--to", target_trackers_arg, "--path" if content_path else "--uploaded-save-path", content_path or "/downloads", "--package-dir", f"./tmp/target/{source_tracker}-{source_torrent_id}-to-{target_trackers_arg}", "--target-torrent-file", "./tmp/exported/mteam.torrent", "--accept-rules", "--target-execute", "--confirm-upload", "--download-uploaded-torrent", "--inject-uploaded-torrent", "--wait-uploaded-complete", "--connect-qbit", "--probe-source", "--probe-target", "--json"],
        ),
        _plan_command_entry(
            "retorrent-execute",
            f"python3 ptcli.py retorrent --from {source_tracker} --source-id {source_torrent_id} --to {target_trackers_arg} --execute --accept-rules --confirm-upload {retorrent_path_arg} --fetch-ptgen --generate-mediainfo --generate-screenshots --upload-screenshots --download-uploaded-torrent --inject-uploaded-torrent --wait-uploaded-complete --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json",
            ["retorrent", "--from", source_tracker, "--source-id", source_torrent_id, "--to", target_trackers_arg, "--execute", "--accept-rules", "--confirm-upload", "--path" if content_path else "--save-path", content_path or "/downloads", "--fetch-ptgen", "--generate-mediainfo", "--generate-screenshots", "--upload-screenshots", "--download-uploaded-torrent", "--inject-uploaded-torrent", "--wait-uploaded-complete", "--uploaded-qbit-category", "MTEAM", "--uploaded-qbit-tags", "retorrent", "--write-summary", "--json"],
        ),
    ]
    if content_path:
        commands.append(
            _plan_command_entry(
                "match",
                f"python3 ptcli.py match --path {json.dumps(content_path, ensure_ascii=False)} --json",
                ["match", "--path", content_path, "--json"],
            )
        )
    return _augment_plan_commands(commands, config=config, client=client, base_dir=base_dir)


def _augment_plan_commands(commands: list[dict[str, Any]], *, config: str | None, client: str, base_dir: str | None) -> list[dict[str, Any]]:
    supports_config = {"source-info", "source-download", "flow-check", "doctor", "pipeline", "target-upload", "retorrent", "match", "export", "inspect"}
    supports_client = {"flow-check", "doctor", "pipeline", "target-upload", "retorrent", "match", "export", "inspect"}
    supports_base_dir = {"source-info", "source-download", "flow-check", "doctor", "pipeline", "retorrent"}
    augmented: list[dict[str, Any]] = []
    for command in commands:
        argv = _argv_list(command.get("argv"))
        if len(argv) < 3:
            augmented.append(command)
            continue
        original_argv = list(argv)
        subcommand = argv[2]
        if config and subcommand in supports_config:
            _append_absent_option(argv, "--config", config)
        if client != "default" and subcommand in supports_client:
            _append_absent_option(argv, "--client", client)
        if base_dir and subcommand in supports_base_dir:
            _append_absent_option(argv, "--base-dir", base_dir)
        if argv == original_argv:
            augmented.append(command)
        else:
            augmented.append({"stage": command.get("stage"), "command": shlex.join(argv), "argv": argv})
    return augmented


def _append_absent_option(argv: list[str], option: str, value: str) -> None:
    if option not in argv:
        argv.extend([option, value])


def _plan_command_entry(stage: str, command: str, args: list[str]) -> dict[str, Any]:
    return {"stage": stage, "command": command, "argv": ["python3", "ptcli.py", *args]}


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

        if args.command == "summary-check":
            payload = summary_check_payload(args)
            if args.print_next_command:
                return _summary_check_print_next_command(payload)
            if args.print_next_argv:
                return _summary_check_print_next_argv(payload)
            if args.print_first_runnable_command:
                return _summary_check_print_first_runnable_command(payload)
            if args.print_first_runnable_argv:
                return _summary_check_print_first_runnable_argv(payload)
            if args.print_shell:
                return _summary_check_print_shell(payload)
            if args.run_next_command:
                return _summary_check_run_next_command(payload)
            if args.run_first_runnable_command:
                return _summary_check_run_first_runnable_command(payload)
            _print_payload(payload, json_output)
            return 0 if payload.get("status") == "ok" else 1

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


def _summary_check_print_next_command(payload: dict[str, Any]) -> int:
    command = payload.get("next_command")
    if payload.get("should_execute_next_command") and command:
        print(str(command))
        return 0
    return 0 if payload.get("status") == "ok" else 1


def _summary_check_print_next_argv(payload: dict[str, Any]) -> int:
    argv = payload.get("next_command_argv")
    if payload.get("should_execute_next_command") and argv:
        print(json.dumps(argv, ensure_ascii=False))
        return 0
    return 0 if payload.get("status") == "ok" else 1


def _summary_check_print_first_runnable_command(payload: dict[str, Any]) -> int:
    command = payload.get("first_runnable_command")
    if command:
        print(str(command))
        return 0
    return 0 if payload.get("status") == "ok" else 1


def _summary_check_print_first_runnable_argv(payload: dict[str, Any]) -> int:
    argv = payload.get("first_runnable_command_argv")
    if argv:
        print(json.dumps(argv, ensure_ascii=False))
        return 0
    return 0 if payload.get("status") == "ok" else 1


def _summary_check_print_shell(payload: dict[str, Any]) -> int:
    flow_diagnostics = payload.get("flow_diagnostics") if isinstance(payload.get("flow_diagnostics"), dict) else {}
    closure_status = payload.get("closure_status") if isinstance(payload.get("closure_status"), dict) else {}
    closure_source = closure_status.get("source") if isinstance(closure_status.get("source"), dict) else {}
    closure_target = closure_status.get("target") if isinstance(closure_status.get("target"), dict) else {}
    material_diagnostics = payload.get("material_diagnostics") if isinstance(payload.get("material_diagnostics"), dict) else {}
    target_upload_diagnostics = payload.get("target_upload_diagnostics") if isinstance(payload.get("target_upload_diagnostics"), dict) else {}
    target_preflight_diagnostics = payload.get("target_preflight_diagnostics") if isinstance(payload.get("target_preflight_diagnostics"), dict) else {}
    closure_review = payload.get("closure_review") if isinstance(payload.get("closure_review"), dict) else {}
    completion_matrix = payload.get("completion_matrix") if isinstance(payload.get("completion_matrix"), dict) else {}
    qbit_wait_diagnostics = payload.get("qbit_wait_diagnostics") if isinstance(payload.get("qbit_wait_diagnostics"), dict) else {}
    qbit_wait_retry_hints = payload.get("qbit_wait_retry_hints") if isinstance(payload.get("qbit_wait_retry_hints"), dict) else {}
    resume_state = payload.get("resume_state") if isinstance(payload.get("resume_state"), dict) else {}
    readiness_summary = payload.get("readiness_summary") if isinstance(payload.get("readiness_summary"), dict) else {}
    fields = {
        "PTCLI_SUMMARY_STATUS": payload.get("status"),
        "PTCLI_AUTOMATION_ACTION": payload.get("automation_action"),
        "PTCLI_AUTOMATION_REASON": payload.get("automation_reason"),
        "PTCLI_AUTOMATION_EXIT_CODE": payload.get("automation_exit_code"),
        "PTCLI_BLOCKERS": ",".join(_string_list(payload.get("blockers"))),
        "PTCLI_MISSING_ARTIFACTS": ",".join(_string_list(payload.get("missing_artifacts"))),
        "PTCLI_MISSING_CLOSURE_AUDIT": ",".join(_string_list(payload.get("missing_closure_audit"))),
        "PTCLI_FLOW_READY": _shell_bool(flow_diagnostics.get("ready")) if flow_diagnostics.get("ready") is not None else None,
        "PTCLI_FLOW_SOURCE_TRACKER": flow_diagnostics.get("source_tracker"),
        "PTCLI_FLOW_SOURCE_ID": flow_diagnostics.get("source_torrent_id"),
        "PTCLI_FLOW_TARGET_TRACKERS": ",".join(_string_list(flow_diagnostics.get("target_trackers"))),
        "PTCLI_CREDENTIAL_REQUIREMENTS": ",".join(_string_list(payload.get("credential_requirements"))),
        "PTCLI_NEXT_STAGE": payload.get("next_stage"),
        "PTCLI_NEXT_COMMAND": payload.get("next_command"),
        "PTCLI_NEXT_COMMAND_ARGV": json.dumps(payload.get("next_command_argv"), ensure_ascii=False) if payload.get("next_command_argv") else None,
        "PTCLI_NEXT_COMMAND_SOURCE": payload.get("next_command_source"),
        "PTCLI_NEXT_COMMAND_SUBCOMMAND": payload.get("next_command_subcommand"),
        "PTCLI_NEXT_COMMAND_RUN_ALLOWED": _shell_bool(payload.get("next_command_run_allowed")),
        "PTCLI_NEXT_COMMAND_RUN_BLOCKER": payload.get("next_command_run_blocker"),
        "PTCLI_CANDIDATE_COMMAND_COUNT": payload.get("candidate_command_count"),
        "PTCLI_RUNNABLE_COMMAND_COUNT": payload.get("runnable_command_count"),
        "PTCLI_FIRST_RUNNABLE_STAGE": payload.get("first_runnable_stage"),
        "PTCLI_FIRST_RUNNABLE_COMMAND": payload.get("first_runnable_command"),
        "PTCLI_FIRST_RUNNABLE_COMMAND_ARGV": json.dumps(payload.get("first_runnable_command_argv"), ensure_ascii=False) if payload.get("first_runnable_command_argv") else None,
        "PTCLI_FIRST_RUNNABLE_COMMAND_SOURCE": payload.get("first_runnable_command_source"),
        "PTCLI_FIRST_RUNNABLE_COMMAND_SUBCOMMAND": payload.get("first_runnable_command_subcommand"),
        "PTCLI_REJECTED_COMMAND_COUNT": payload.get("rejected_command_count"),
        "PTCLI_REJECTED_COMMAND_BLOCKERS": ",".join(_string_list(payload.get("rejected_command_blockers"))),
        "PTCLI_FIRST_REJECTED_STAGE": payload.get("first_rejected_stage"),
        "PTCLI_FIRST_REJECTED_COMMAND": payload.get("first_rejected_command"),
        "PTCLI_FIRST_REJECTED_COMMAND_SOURCE": payload.get("first_rejected_command_source"),
        "PTCLI_FIRST_REJECTED_COMMAND_SUBCOMMAND": payload.get("first_rejected_command_subcommand"),
        "PTCLI_FIRST_REJECTED_COMMAND_BLOCKER": payload.get("first_rejected_command_blocker"),
        "PTCLI_SHOULD_EXECUTE_NEXT_COMMAND": _shell_bool(payload.get("should_execute_next_command")),
        "PTCLI_NEXT_COMMAND_READY": _shell_bool(payload.get("next_command_ready")),
        "PTCLI_QBIT_WAIT_MISMATCH": _shell_bool(payload.get("qbit_wait_mismatch")),
        "PTCLI_QBIT_WAIT_MISMATCHES": ",".join(_string_list(payload.get("qbit_wait_mismatches"))),
        "PTCLI_CLOSURE_STATUS_COMPLETE": _shell_bool(closure_status.get("complete")) if "complete" in closure_status else None,
        "PTCLI_CLOSURE_STATUS_READY": _shell_bool(closure_status.get("ready")) if "ready" in closure_status else None,
        "PTCLI_CLOSURE_STATUS_PIPELINE_STATUS": closure_status.get("pipeline_status"),
        "PTCLI_CLOSURE_STATUS_PIPELINE_BLOCKERS": ",".join(_string_list(closure_status.get("pipeline_blockers"))),
        "PTCLI_CLOSURE_STATUS_CLOSURE_COMPLETE": _shell_bool(closure_status.get("closure_complete")) if "closure_complete" in closure_status else None,
        "PTCLI_CLOSURE_STATUS_CLOSURE_BLOCKERS": ",".join(_string_list(closure_status.get("closure_blockers"))),
        "PTCLI_CLOSURE_STATUS_AUDIT_READY": _shell_bool(closure_status.get("audit_ready")) if "audit_ready" in closure_status else None,
        "PTCLI_CLOSURE_STATUS_AUDIT_MISSING": ",".join(_string_list(closure_status.get("audit_missing"))),
        "PTCLI_CLOSURE_STATUS_QBIT_WAIT_MISMATCH": _shell_bool(closure_status.get("qbit_wait_mismatch")) if "qbit_wait_mismatch" in closure_status else None,
        "PTCLI_CLOSURE_STATUS_QBIT_WAIT_MISMATCHES": ",".join(_string_list(closure_status.get("qbit_wait_mismatches"))),
        "PTCLI_COMPLETION_MATRIX_READY": _shell_bool(completion_matrix.get("ready")) if completion_matrix.get("ready") is not None else None,
        "PTCLI_COMPLETION_MATRIX_REQUIRED": ",".join(_string_list(completion_matrix.get("required"))),
        "PTCLI_COMPLETION_MATRIX_MISSING_DOMAINS": ",".join(_string_list(completion_matrix.get("missing_domains"))),
        "PTCLI_CLOSURE_SOURCE_READY": _shell_bool(closure_source.get("ready")) if "ready" in closure_source else None,
        "PTCLI_CLOSURE_SOURCE_HASH_CONSISTENT": _shell_bool(closure_source.get("hash_consistent")) if "hash_consistent" in closure_source else None,
        "PTCLI_CLOSURE_SOURCE_WAIT_EVIDENCE": _shell_bool(closure_source.get("wait_evidence")) if "wait_evidence" in closure_source else None,
        "PTCLI_CLOSURE_SOURCE_INJECTION_VERIFIED": _shell_bool(closure_source.get("injection_verified")) if "injection_verified" in closure_source else None,
        "PTCLI_CLOSURE_TARGET_READY": _shell_bool(closure_target.get("ready")) if "ready" in closure_target else None,
        "PTCLI_CLOSURE_TARGET_HASH_CONSISTENT": _shell_bool(closure_target.get("hash_consistent")) if "hash_consistent" in closure_target else None,
        "PTCLI_CLOSURE_TARGET_DUPLICATE_CLEAN": _shell_bool(closure_target.get("duplicate_clean")) if "duplicate_clean" in closure_target else None,
        "PTCLI_CLOSURE_TARGET_RULE_OBLIGATIONS_READY": _shell_bool(closure_target.get("rule_obligations_ready")) if "rule_obligations_ready" in closure_target else None,
        "PTCLI_CLOSURE_TARGET_UPLOADED_WAIT_EVIDENCE": _shell_bool(closure_target.get("uploaded_wait_evidence")) if "uploaded_wait_evidence" in closure_target else None,
        "PTCLI_CLOSURE_TARGET_INJECTION_VERIFIED": _shell_bool(closure_target.get("injection_verified")) if "injection_verified" in closure_target else None,
        "PTCLI_SOURCE_MODE": payload.get("source_mode"),
        "PTCLI_TARGET_MODE": payload.get("target_mode"),
        "PTCLI_COMPLETE": _shell_bool(payload.get("complete")),
        "PTCLI_LIVE_SAFE_TO_ATTEMPT": _shell_bool(payload.get("live_safe_to_attempt")),
        "PTCLI_SUMMARY_FILE": payload.get("summary_file"),
    }
    fields.update(_summary_check_closure_review_shell_fields(closure_review))
    fields.update(_summary_check_completion_matrix_shell_fields(completion_matrix))
    fields.update(_summary_check_material_shell_fields(material_diagnostics))
    fields.update(_summary_check_target_preflight_shell_fields(target_preflight_diagnostics))
    fields.update(_summary_check_target_upload_shell_fields(target_upload_diagnostics))
    fields.update(_summary_check_readiness_shell_fields(readiness_summary))
    fields.update(_summary_check_resume_material_shell_fields(resume_state))
    fields.update(_summary_check_uploaded_followup_shell_fields(resume_state))
    fields.update(_summary_check_qbit_wait_shell_fields(qbit_wait_diagnostics))
    fields.update(_summary_check_qbit_retry_shell_fields(qbit_wait_retry_hints))
    for key, value in fields.items():
        print(f"export {key}={shlex.quote('' if value is None else str(value))}")
    return 0


def _summary_check_readiness_shell_fields(readiness_summary: dict[str, Any]) -> dict[str, Any]:
    material_recovery = readiness_summary.get("material_recovery") if isinstance(readiness_summary.get("material_recovery"), dict) else {}
    material_recovery_coverage = material_recovery.get("command_coverage") if isinstance(material_recovery.get("command_coverage"), dict) else {}
    material_description_evidence = readiness_summary.get("material_description_evidence") if isinstance(readiness_summary.get("material_description_evidence"), dict) else {}
    source_followup = readiness_summary.get("source_followup") if isinstance(readiness_summary.get("source_followup"), dict) else {}
    source_wait_query = source_followup.get("source_wait_query") if isinstance(source_followup.get("source_wait_query"), dict) else {}
    source_wait_retry = source_followup.get("wait_retry") if isinstance(source_followup.get("wait_retry"), dict) else {}
    uploaded_followup = readiness_summary.get("uploaded_followup") if isinstance(readiness_summary.get("uploaded_followup"), dict) else {}
    uploaded_wait_query = uploaded_followup.get("uploaded_wait_query") if isinstance(uploaded_followup.get("uploaded_wait_query"), dict) else {}
    uploaded_wait_retry = uploaded_followup.get("wait_retry") if isinstance(uploaded_followup.get("wait_retry"), dict) else {}
    return {
        "PTCLI_READINESS_STATUS": readiness_summary.get("status"),
        "PTCLI_READINESS_READY": _shell_bool(readiness_summary.get("ready")) if readiness_summary.get("ready") is not None else None,
        "PTCLI_READINESS_COMPLETE": _shell_bool(readiness_summary.get("complete")) if readiness_summary.get("complete") is not None else None,
        "PTCLI_READINESS_COMPLETION_READY": _shell_bool(readiness_summary.get("completion_ready")) if readiness_summary.get("completion_ready") is not None else None,
        "PTCLI_READINESS_MISSING_DOMAINS": ",".join(_string_list(readiness_summary.get("missing_domains"))),
        "PTCLI_READINESS_BLOCKERS": ",".join(_string_list(readiness_summary.get("blockers"))),
        "PTCLI_READINESS_NEXT_STAGE": readiness_summary.get("next_stage"),
        "PTCLI_READINESS_NEXT_COMMAND": readiness_summary.get("next_command"),
        "PTCLI_READINESS_NEXT_COMMAND_ARGV": json.dumps(readiness_summary.get("next_command_argv"), ensure_ascii=False) if readiness_summary.get("next_command_argv") else None,
        "PTCLI_READINESS_AUTOMATION_ACTION": readiness_summary.get("automation_action"),
        "PTCLI_READINESS_SHOULD_EXECUTE_NEXT_COMMAND": _shell_bool(readiness_summary.get("should_execute_next_command"))
        if readiness_summary.get("should_execute_next_command") is not None
        else None,
        "PTCLI_READINESS_AUTOMATION_EXIT_CODE": readiness_summary.get("automation_exit_code"),
        "PTCLI_READINESS_FLOW_READY": _shell_bool(readiness_summary.get("flow_ready")) if readiness_summary.get("flow_ready") is not None else None,
        "PTCLI_READINESS_SOURCE_READY": _shell_bool(readiness_summary.get("source_ready")) if readiness_summary.get("source_ready") is not None else None,
        "PTCLI_READINESS_MATERIALS_READY": _shell_bool(readiness_summary.get("materials_ready")) if readiness_summary.get("materials_ready") is not None else None,
        "PTCLI_READINESS_RULES_READY": _shell_bool(readiness_summary.get("rules_ready")) if readiness_summary.get("rules_ready") is not None else None,
        "PTCLI_READINESS_TARGET_UPLOAD_READY": _shell_bool(readiness_summary.get("target_upload_ready")) if readiness_summary.get("target_upload_ready") is not None else None,
        "PTCLI_READINESS_QBIT_WAIT_READY": _shell_bool(readiness_summary.get("qbit_wait_ready")) if readiness_summary.get("qbit_wait_ready") is not None else None,
        "PTCLI_READINESS_READY_FOR_MTEAM_UPLOAD": _shell_bool(readiness_summary.get("ready_for_mteam_upload"))
        if readiness_summary.get("ready_for_mteam_upload") is not None
        else None,
        "PTCLI_READINESS_MATERIAL_MISSING": ",".join(_string_list(readiness_summary.get("material_missing"))),
        "PTCLI_READINESS_MATERIAL_UPLOAD_GATES": json.dumps(readiness_summary.get("material_upload_gates"), ensure_ascii=False) if isinstance(readiness_summary.get("material_upload_gates"), dict) else None,
        "PTCLI_READINESS_MATERIAL_UPLOAD_BLOCKERS": "|".join(_string_list(readiness_summary.get("material_upload_blockers"))),
        "PTCLI_READINESS_MATERIAL_LIVE_GATE_PRESENT": _shell_bool(readiness_summary.get("material_live_gate_present")) if readiness_summary.get("material_live_gate_present") is not None else None,
        "PTCLI_READINESS_MATERIAL_LIVE_GATE_READY": _shell_bool(readiness_summary.get("material_live_gate_ready")) if readiness_summary.get("material_live_gate_ready") is not None else None,
        "PTCLI_READINESS_MATERIAL_LIVE_GATE_MISSING": ",".join(_string_list(readiness_summary.get("material_live_gate_missing"))),
        "PTCLI_READINESS_MATERIAL_LIVE_GATE_BLOCKERS": "|".join(_string_list(readiness_summary.get("material_live_gate_blockers"))),
        "PTCLI_READINESS_MATERIAL_LIVE_GATE_NEXT_ACTIONS": " | ".join(_string_list(readiness_summary.get("material_live_gate_next_actions"))),
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_EVIDENCE": json.dumps(material_description_evidence, ensure_ascii=False) if material_description_evidence else None,
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_PTGEN_READY": _shell_bool(readiness_summary.get("material_description_ptgen_ready")) if readiness_summary.get("material_description_ptgen_ready") is not None else None,
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_EXTERNAL_IDS_READY": _shell_bool(readiness_summary.get("material_description_external_ids_ready")) if readiness_summary.get("material_description_external_ids_ready") is not None else None,
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_EXTERNAL_ID_MISSING": ",".join(_string_list(readiness_summary.get("material_description_external_id_missing"))),
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_MEDIAINFO_READY": _shell_bool(readiness_summary.get("material_description_mediainfo_ready")) if readiness_summary.get("material_description_mediainfo_ready") is not None else None,
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_SCREENSHOTS_READY": _shell_bool(readiness_summary.get("material_description_screenshots_ready")) if readiness_summary.get("material_description_screenshots_ready") is not None else None,
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_READY": _shell_bool(readiness_summary.get("material_description_screenshot_coverage_ready")) if readiness_summary.get("material_description_screenshot_coverage_ready") is not None else None,
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_MISSING_COUNT": readiness_summary.get("material_description_screenshot_coverage_missing_count"),
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_MISSING_URLS": ",".join(_string_list(readiness_summary.get("material_description_screenshot_coverage_missing_urls"))),
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_METADATA_CHAIN_READY": _shell_bool(readiness_summary.get("material_description_metadata_chain_ready")) if readiness_summary.get("material_description_metadata_chain_ready") is not None else None,
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_METADATA_CHAIN_MISSING": ",".join(_string_list(readiness_summary.get("material_description_metadata_chain_missing"))),
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_METADATA_CHAIN_NEXT_ACTIONS": " | ".join(_string_list(readiness_summary.get("material_description_metadata_chain_next_actions"))),
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_MEDIA_INFO_CHAIN_READY": _shell_bool(readiness_summary.get("material_description_media_info_chain_ready")) if readiness_summary.get("material_description_media_info_chain_ready") is not None else None,
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_MEDIA_INFO_CHAIN_MISSING": ",".join(_string_list(readiness_summary.get("material_description_media_info_chain_missing"))),
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_MEDIA_INFO_CHAIN_NEXT_ACTIONS": " | ".join(_string_list(readiness_summary.get("material_description_media_info_chain_next_actions"))),
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_SCREENSHOT_CHAIN_READY": _shell_bool(readiness_summary.get("material_description_screenshot_chain_ready")) if readiness_summary.get("material_description_screenshot_chain_ready") is not None else None,
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_SCREENSHOT_CHAIN_MISSING": ",".join(_string_list(readiness_summary.get("material_description_screenshot_chain_missing"))),
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_SCREENSHOT_CHAIN_NEXT_ACTIONS": " | ".join(_string_list(readiness_summary.get("material_description_screenshot_chain_next_actions"))),
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_INPUT_CHAIN_READY": _shell_bool(readiness_summary.get("material_description_input_chain_ready")) if readiness_summary.get("material_description_input_chain_ready") is not None else None,
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_INPUT_CHAIN_MISSING": ",".join(_string_list(readiness_summary.get("material_description_input_chain_missing"))),
        "PTCLI_READINESS_MATERIAL_DESCRIPTION_INPUT_CHAIN_NEXT_ACTIONS": " | ".join(_string_list(readiness_summary.get("material_description_input_chain_next_actions"))),
        "PTCLI_READINESS_TARGET_PREFLIGHT_MISSING": ",".join(_string_list(readiness_summary.get("target_preflight_missing"))),
        "PTCLI_READINESS_TARGET_PREFLIGHT_DESCRIPTION_MISSING": ",".join(_string_list(readiness_summary.get("target_preflight_description_missing"))),
        "PTCLI_READINESS_TARGET_PREFLIGHT_BLOCKERS": "|".join(_string_list(readiness_summary.get("target_preflight_blockers"))),
        "PTCLI_READINESS_TARGET_UPLOAD_PAYLOAD_RECOVERY_MISSING": ",".join(_string_list(readiness_summary.get("target_upload_payload_recovery_missing"))),
        "PTCLI_READINESS_TARGET_UPLOAD_PAYLOAD_NEXT_ACTIONS": " | ".join(_string_list(readiness_summary.get("target_upload_payload_next_actions"))),
        "PTCLI_READINESS_READY_FOR_UPLOADED_SEEDING": _shell_bool(readiness_summary.get("ready_for_uploaded_seeding"))
        if readiness_summary.get("ready_for_uploaded_seeding") is not None
        else None,
        "PTCLI_READINESS_READY_FOR_SOURCE_SEEDING": _shell_bool(readiness_summary.get("ready_for_source_seeding")) if readiness_summary.get("ready_for_source_seeding") is not None else None,
        "PTCLI_READINESS_QBIT_WAIT_MISMATCH": _shell_bool(readiness_summary.get("qbit_wait_mismatch")) if readiness_summary.get("qbit_wait_mismatch") is not None else None,
        "PTCLI_READINESS_QBIT_WAIT_MISMATCHES": ",".join(_string_list(readiness_summary.get("qbit_wait_mismatches"))),
        "PTCLI_READINESS_MATERIAL_RECOVERY_PRESENT": _shell_bool(material_recovery.get("present")) if "present" in material_recovery else None,
        "PTCLI_READINESS_MATERIAL_RECOVERY_TARGET_MATERIALS_MISSING": ",".join(_string_list(material_recovery.get("target_materials_missing"))),
        "PTCLI_READINESS_MATERIAL_RECOVERY_TARGET_PREPARATION_MISSING": ",".join(_string_list(material_recovery.get("target_preparation_missing"))),
        "PTCLI_READINESS_MATERIAL_RECOVERY_HINT_COUNT": material_recovery.get("hint_count"),
        "PTCLI_READINESS_MATERIAL_RECOVERY_KEYS": ",".join(_string_list(material_recovery.get("keys"))),
        "PTCLI_READINESS_MATERIAL_RECOVERY_REQUIRED_FLAGS": ",".join(_string_list(material_recovery.get("required_flags"))),
        "PTCLI_READINESS_MATERIAL_RECOVERY_MISSING_FLAGS": ",".join(_string_list(material_recovery.get("missing_flags"))),
        "PTCLI_READINESS_MATERIAL_RECOVERY_EXISTING_FILE_OPTIONS": ",".join(_string_list(material_recovery.get("existing_file_options"))),
        "PTCLI_READINESS_MATERIAL_FIRST_RECOVERY_COMMAND": material_recovery.get("first_command"),
        "PTCLI_READINESS_MATERIAL_FIRST_RECOVERY_COMMAND_ARGV": json.dumps(material_recovery.get("first_command_argv"), ensure_ascii=False) if material_recovery.get("first_command_argv") else None,
        "PTCLI_READINESS_MATERIAL_RECOVERY_COMPLETION_COMMAND": material_recovery.get("completion_command"),
        "PTCLI_READINESS_MATERIAL_RECOVERY_COMPLETION_COMMAND_ARGV": json.dumps(material_recovery.get("completion_command_argv"), ensure_ascii=False) if material_recovery.get("completion_command_argv") else None,
        "PTCLI_READINESS_MATERIAL_RECOVERY_COMMAND_COVERAGE_READY": _shell_bool(material_recovery_coverage.get("ready")) if material_recovery_coverage.get("ready") is not None else None,
        "PTCLI_READINESS_MATERIAL_RECOVERY_COMMAND_COVERAGE_AVAILABLE": material_recovery_coverage.get("available_count"),
        "PTCLI_READINESS_MATERIAL_RECOVERY_COMMAND_COVERAGE_MISSING": material_recovery_coverage.get("missing_count"),
        "PTCLI_READINESS_MATERIAL_RECOVERY_FIRST_UNCOVERED_KEY": material_recovery_coverage.get("first_uncovered_key"),
        "PTCLI_READINESS_MATERIAL_RECOVERY_FIRST_UNCOVERED_FLAGS": ",".join(_string_list(material_recovery_coverage.get("first_uncovered_missing_flags"))),
        "PTCLI_READINESS_MATERIAL_RECOVERY_NEXT_ACTIONS": " | ".join(_string_list(material_recovery.get("next_actions"))),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_PRESENT": _shell_bool(source_followup.get("present")) if "present" in source_followup else None,
        "PTCLI_READINESS_SOURCE_FOLLOWUP_READY": _shell_bool(source_followup.get("ready")) if source_followup.get("ready") is not None else None,
        "PTCLI_READINESS_SOURCE_FOLLOWUP_READY_FOR_SEEDING": _shell_bool(source_followup.get("ready_for_source_seeding")) if source_followup.get("ready_for_source_seeding") is not None else None,
        "PTCLI_READINESS_SOURCE_FOLLOWUP_MISSING": ",".join(_string_list(source_followup.get("missing"))),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_BLOCKERS": "|".join(_string_list(source_followup.get("blockers"))),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_NEXT_ACTIONS": " | ".join(_string_list(source_followup.get("next_actions"))),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_TORRENT_HASH": source_followup.get("source_torrent_hash"),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_INJECTED_HASH": source_followup.get("injected_torrent_hash"),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_INJECTION_VISIBLE": _shell_bool(source_followup.get("injection_visible_in_client"))
        if source_followup.get("injection_visible_in_client") is not None
        else None,
        "PTCLI_READINESS_SOURCE_FOLLOWUP_INJECTION_VERIFIED": _shell_bool(source_followup.get("injection_verified")) if source_followup.get("injection_verified") is not None else None,
        "PTCLI_READINESS_SOURCE_FOLLOWUP_WAIT_EVIDENCE": _shell_bool(source_followup.get("source_wait_evidence")) if source_followup.get("source_wait_evidence") is not None else None,
        "PTCLI_READINESS_SOURCE_FOLLOWUP_HASH_CONSISTENT": _shell_bool(source_followup.get("hash_consistent")) if source_followup.get("hash_consistent") is not None else None,
        "PTCLI_READINESS_SOURCE_FOLLOWUP_TORRENT_FILE": source_followup.get("source_torrent_file"),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_SAVE_PATH": source_followup.get("source_save_path"),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_WAIT_QUERY_HASH": source_wait_query.get("torrent_hash"),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_WAIT_QUERY_SAVE_PATH": source_wait_query.get("save_path"),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_WAIT_QUERY_TIMEOUT": source_wait_query.get("timeout"),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_WAIT_QUERY_INTERVAL": source_wait_query.get("interval"),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_WAIT_MISMATCH": _shell_bool(source_followup.get("qbit_wait_mismatch")) if source_followup.get("qbit_wait_mismatch") is not None else None,
        "PTCLI_READINESS_SOURCE_FOLLOWUP_WAIT_MISMATCHES": ",".join(_string_list(source_followup.get("qbit_wait_mismatches"))),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_WAIT_RETRY_RECOMMENDED": _shell_bool(source_wait_retry.get("retry_recommended")) if source_wait_retry.get("retry_recommended") is not None else None,
        "PTCLI_READINESS_SOURCE_FOLLOWUP_WAIT_SUGGESTED_HASH": source_wait_retry.get("suggested_torrent_hash"),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_WAIT_SUGGESTED_CONTENT_PATH": source_wait_retry.get("suggested_content_path"),
        "PTCLI_READINESS_SOURCE_FOLLOWUP_WAIT_SUGGESTED_SAVE_PATH": source_wait_retry.get("suggested_save_path"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_PRESENT": _shell_bool(uploaded_followup.get("present")) if "present" in uploaded_followup else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_READY": _shell_bool(uploaded_followup.get("ready")) if uploaded_followup.get("ready") is not None else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_READY_FOR_SEEDING": _shell_bool(uploaded_followup.get("ready_for_uploaded_seeding"))
        if uploaded_followup.get("ready_for_uploaded_seeding") is not None
        else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_MISSING": ",".join(_string_list(uploaded_followup.get("missing"))),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_BLOCKERS": "|".join(_string_list(uploaded_followup.get("blockers"))),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_NEXT_ACTIONS": " | ".join(_string_list(uploaded_followup.get("next_actions"))),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_UPLOADED": _shell_bool(uploaded_followup.get("uploaded")) if uploaded_followup.get("uploaded") is not None else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_DOWNLOADED": _shell_bool(uploaded_followup.get("downloaded")) if uploaded_followup.get("downloaded") is not None else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_INJECTED": _shell_bool(uploaded_followup.get("injected")) if uploaded_followup.get("injected") is not None else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_TORRENT_ID": uploaded_followup.get("uploaded_torrent_id"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_TORRENT_HASH": uploaded_followup.get("uploaded_torrent_hash"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_INJECTED_HASH": uploaded_followup.get("injected_torrent_hash"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_INJECTION_VISIBLE": _shell_bool(uploaded_followup.get("injection_visible_in_client"))
        if uploaded_followup.get("injection_visible_in_client") is not None
        else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_INJECTION_VERIFIED": _shell_bool(uploaded_followup.get("injection_verified"))
        if uploaded_followup.get("injection_verified") is not None
        else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_EVIDENCE": _shell_bool(uploaded_followup.get("uploaded_wait_evidence"))
        if uploaded_followup.get("uploaded_wait_evidence") is not None
        else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_HASH_CONSISTENT": _shell_bool(uploaded_followup.get("hash_consistent")) if uploaded_followup.get("hash_consistent") is not None else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_DUPLICATE_CLEAN": _shell_bool(uploaded_followup.get("duplicate_clean")) if uploaded_followup.get("duplicate_clean") is not None else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_RULES_READY": _shell_bool(uploaded_followup.get("rule_obligations_ready"))
        if uploaded_followup.get("rule_obligations_ready") is not None
        else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_TORRENT_FILE": uploaded_followup.get("uploaded_torrent_file"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_SAVE_PATH": uploaded_followup.get("uploaded_save_path"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_QUERY_HASH": uploaded_wait_query.get("torrent_hash"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_QUERY_CONTENT_PATH": uploaded_wait_query.get("content_path"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_QUERY_TIMEOUT": uploaded_wait_query.get("timeout"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_QUERY_INTERVAL": uploaded_wait_query.get("interval"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_MISMATCH": _shell_bool(uploaded_followup.get("qbit_wait_mismatch")) if uploaded_followup.get("qbit_wait_mismatch") is not None else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_MISMATCHES": ",".join(_string_list(uploaded_followup.get("qbit_wait_mismatches"))),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_RETRY_RECOMMENDED": _shell_bool(uploaded_wait_retry.get("retry_recommended"))
        if uploaded_wait_retry.get("retry_recommended") is not None
        else None,
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_HASH": uploaded_wait_retry.get("suggested_torrent_hash"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_CONTENT_PATH": uploaded_wait_retry.get("suggested_content_path"),
        "PTCLI_READINESS_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_SAVE_PATH": uploaded_wait_retry.get("suggested_save_path"),
    }


def _summary_check_target_preflight_shell_fields(target_preflight_diagnostics: dict[str, Any]) -> dict[str, Any]:
    torrent_file = target_preflight_diagnostics.get("torrent_file") if isinstance(target_preflight_diagnostics.get("torrent_file"), dict) else {}
    return {
        "PTCLI_TARGET_PREFLIGHT_PRESENT": _shell_bool(target_preflight_diagnostics.get("present")) if "present" in target_preflight_diagnostics else None,
        "PTCLI_TARGET_PREFLIGHT_SOURCE": target_preflight_diagnostics.get("source"),
        "PTCLI_TARGET_PREFLIGHT_STATUS": target_preflight_diagnostics.get("status"),
        "PTCLI_TARGET_PREFLIGHT_READY": _shell_bool(target_preflight_diagnostics.get("ready")) if target_preflight_diagnostics.get("ready") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_BLOCKERS": "|".join(_string_list(target_preflight_diagnostics.get("blockers"))),
        "PTCLI_TARGET_PREFLIGHT_MISSING": ",".join(_string_list(target_preflight_diagnostics.get("missing"))),
        "PTCLI_TARGET_PREFLIGHT_DESCRIPTION_MISSING": ",".join(_string_list(target_preflight_diagnostics.get("description_missing"))),
        "PTCLI_TARGET_PREFLIGHT_PREPARATION_READY": _shell_bool(target_preflight_diagnostics.get("target_preparation_ready")) if target_preflight_diagnostics.get("target_preparation_ready") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_MATERIALS_READY": _shell_bool(target_preflight_diagnostics.get("materials_ready")) if target_preflight_diagnostics.get("materials_ready") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_METADATA_READY": _shell_bool(target_preflight_diagnostics.get("metadata_ready")) if target_preflight_diagnostics.get("metadata_ready") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_ASSETS_READY": _shell_bool(target_preflight_diagnostics.get("assets_ready")) if target_preflight_diagnostics.get("assets_ready") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_DESCRIPTION_READY": _shell_bool(target_preflight_diagnostics.get("description_ready")) if target_preflight_diagnostics.get("description_ready") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_PAYLOAD_READY": _shell_bool(target_preflight_diagnostics.get("payload_ready")) if target_preflight_diagnostics.get("payload_ready") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_PAYLOAD_CHECKS_READY": _shell_bool(target_preflight_diagnostics.get("payload_checks_ready")) if target_preflight_diagnostics.get("payload_checks_ready") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_DESCRIPTION_CHECKS_READY": _shell_bool(target_preflight_diagnostics.get("description_checks_ready")) if target_preflight_diagnostics.get("description_checks_ready") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_MATERIALS_READY_REQUIRED": _shell_bool(target_preflight_diagnostics.get("materials_ready_required")) if target_preflight_diagnostics.get("materials_ready_required") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_TORRENT_PATH": torrent_file.get("path"),
        "PTCLI_TARGET_PREFLIGHT_TORRENT_MTEAM_SAFE": _shell_bool(torrent_file.get("mteam_safe")) if torrent_file.get("mteam_safe") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_TORRENT_METADATA_READABLE": _shell_bool(torrent_file.get("metadata_readable")) if torrent_file.get("metadata_readable") is not None else None,
        "PTCLI_TARGET_PREFLIGHT_TORRENT_SOURCE_FLAG": torrent_file.get("source_flag"),
    }


def _summary_check_completion_matrix_shell_fields(completion_matrix: dict[str, Any]) -> dict[str, Any]:
    domains = completion_matrix.get("domains") if isinstance(completion_matrix.get("domains"), dict) else {}
    fields: dict[str, Any] = {}
    for name in ("flow", "source", "materials", "rules", "target_upload", "qbit_wait"):
        domain = domains.get(name) if isinstance(domains.get(name), dict) else {}
        evidence = domain.get("evidence") if isinstance(domain.get("evidence"), dict) else {}
        prefix = f"PTCLI_COMPLETION_{name.upper()}"
        fields[f"{prefix}_READY"] = _shell_bool(domain.get("ready")) if domain.get("ready") is not None else None
        fields[f"{prefix}_MISSING"] = ",".join(_string_list(domain.get("missing")))
        if name == "materials":
            fields[f"{prefix}_READY_FOR_MTEAM_UPLOAD"] = _shell_bool(evidence.get("ready_for_mteam_upload")) if evidence.get("ready_for_mteam_upload") is not None else None
        if name == "target_upload":
            fields[f"{prefix}_READY_FOR_UPLOADED_SEEDING"] = _shell_bool(evidence.get("ready_for_uploaded_seeding")) if evidence.get("ready_for_uploaded_seeding") is not None else None
    return fields


def _summary_check_closure_review_shell_fields(closure_review: dict[str, Any]) -> dict[str, Any]:
    source = closure_review.get("source") if isinstance(closure_review.get("source"), dict) else {}
    target = closure_review.get("target") if isinstance(closure_review.get("target"), dict) else {}
    source_torrent_artifact = source.get("torrent_file_artifact") if isinstance(source.get("torrent_file_artifact"), dict) else {}
    description = target.get("description") if isinstance(target.get("description"), dict) else {}
    external_links = description.get("external_links") if isinstance(description.get("external_links"), dict) else {}
    checks = closure_review.get("checks") if isinstance(closure_review.get("checks"), dict) else {}
    return {
        "PTCLI_CLOSURE_REVIEW_COMPLETE": _shell_bool(closure_review.get("complete")) if closure_review.get("complete") is not None else None,
        "PTCLI_CLOSURE_REVIEW_MISSING": ",".join(_string_list(closure_review.get("missing"))),
        "PTCLI_CLOSURE_REVIEW_SOURCE_MODE": source.get("mode"),
        "PTCLI_CLOSURE_REVIEW_SOURCE_READY": _shell_bool(source.get("ready")) if source.get("ready") is not None else None,
        "PTCLI_CLOSURE_REVIEW_SOURCE_HASH_CONSISTENT": _shell_bool(source.get("hash_consistent")) if source.get("hash_consistent") is not None else None,
        "PTCLI_CLOSURE_REVIEW_SOURCE_WAIT_EVIDENCE": _shell_bool(source.get("wait_evidence")) if source.get("wait_evidence") is not None else None,
        "PTCLI_CLOSURE_REVIEW_SOURCE_INJECTION_VERIFIED": _shell_bool(source.get("injection_verified")) if source.get("injection_verified") is not None else None,
        "PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_HASH": source.get("torrent_hash"),
        "PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_FILE": source.get("torrent_file"),
        "PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_FILE_EVIDENCE": _shell_bool(source.get("torrent_file_evidence")) if source.get("torrent_file_evidence") is not None else None,
        "PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_EXISTS": _shell_bool(source_torrent_artifact.get("exists")) if source_torrent_artifact.get("exists") is not None else None,
        "PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_IS_FILE": _shell_bool(source_torrent_artifact.get("is_file")) if source_torrent_artifact.get("is_file") is not None else None,
        "PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_SIZE_BYTES": source_torrent_artifact.get("size_bytes"),
        "PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_SHA1": source_torrent_artifact.get("sha1"),
        "PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_INFOHASH": source_torrent_artifact.get("torrent_hash") or source_torrent_artifact.get("infohash") or source_torrent_artifact.get("hash"),
        "PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_METADATA_READABLE": _shell_bool(source_torrent_artifact.get("metadata_readable")) if source_torrent_artifact.get("metadata_readable") is not None else None,
        "PTCLI_CLOSURE_REVIEW_SOURCE_TORRENT_REUSED": _shell_bool(source_torrent_artifact.get("reused")) if source_torrent_artifact.get("reused") is not None else None,
        "PTCLI_CLOSURE_REVIEW_SOURCE_INJECTED_HASH": source.get("injected_torrent_hash"),
        "PTCLI_CLOSURE_REVIEW_SOURCE_INJECTION_VISIBLE": _shell_bool(source.get("injection_visible_in_client")) if source.get("injection_visible_in_client") is not None else None,
        "PTCLI_CLOSURE_REVIEW_SOURCE_SAVE_PATH": source.get("save_path"),
        "PTCLI_CLOSURE_REVIEW_SOURCE_QBIT_CATEGORY": source.get("qbit_category"),
        "PTCLI_CLOSURE_REVIEW_SOURCE_QBIT_TAGS": source.get("qbit_tags"),
        "PTCLI_CLOSURE_REVIEW_SOURCE_PAUSED": _shell_bool(source.get("paused")) if source.get("paused") is not None else None,
        "PTCLI_CLOSURE_REVIEW_SOURCE_CONTENT_PATH": source.get("content_path"),
        "PTCLI_CLOSURE_REVIEW_TARGET_MODE": target.get("mode"),
        "PTCLI_CLOSURE_REVIEW_TARGET_READY": _shell_bool(target.get("ready")) if target.get("ready") is not None else None,
        "PTCLI_CLOSURE_REVIEW_TARGET_HASH_CONSISTENT": _shell_bool(target.get("hash_consistent")) if target.get("hash_consistent") is not None else None,
        "PTCLI_CLOSURE_REVIEW_TARGET_DUPLICATE_CLEAN": _shell_bool(target.get("duplicate_clean")) if target.get("duplicate_clean") is not None else None,
        "PTCLI_CLOSURE_REVIEW_TARGET_RULES_READY": _shell_bool(target.get("rule_obligations_ready")) if target.get("rule_obligations_ready") is not None else None,
        "PTCLI_CLOSURE_REVIEW_TARGET_UPLOADED_WAIT_EVIDENCE": _shell_bool(target.get("uploaded_wait_evidence")) if target.get("uploaded_wait_evidence") is not None else None,
        "PTCLI_CLOSURE_REVIEW_TARGET_INJECTION_VERIFIED": _shell_bool(target.get("injection_verified")) if target.get("injection_verified") is not None else None,
        "PTCLI_CLOSURE_REVIEW_UPLOADED_TORRENT_ID": target.get("uploaded_torrent_id"),
        "PTCLI_CLOSURE_REVIEW_UPLOADED_TORRENT_HASH": target.get("uploaded_torrent_hash"),
        "PTCLI_CLOSURE_REVIEW_UPLOADED_TORRENT_FILE": target.get("uploaded_torrent_file"),
        "PTCLI_CLOSURE_REVIEW_INJECTED_HASH": target.get("injected_torrent_hash"),
        "PTCLI_CLOSURE_REVIEW_UPLOADED_SAVE_PATH": target.get("uploaded_save_path"),
        "PTCLI_CLOSURE_REVIEW_TARGET_MATERIALS_READY": _shell_bool(target.get("materials_ready")) if target.get("materials_ready") is not None else None,
        "PTCLI_CLOSURE_REVIEW_TARGET_METADATA_READY": _shell_bool(target.get("metadata_ready")) if target.get("metadata_ready") is not None else None,
        "PTCLI_CLOSURE_REVIEW_TARGET_ASSETS_READY": _shell_bool(target.get("assets_ready")) if target.get("assets_ready") is not None else None,
        "PTCLI_CLOSURE_REVIEW_TARGET_DESCRIPTION_READY": _shell_bool(target.get("description_ready")) if target.get("description_ready") is not None else None,
        "PTCLI_CLOSURE_REVIEW_TARGET_PREPARATION_READY": _shell_bool(target.get("preparation_ready")) if target.get("preparation_ready") is not None else None,
        "PTCLI_CLOSURE_REVIEW_TARGET_PREPARATION_MISSING": ",".join(_string_list(target.get("preparation_missing"))),
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_PATH": description.get("path"),
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_LENGTH": description.get("char_length"),
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_EXPECTED_LENGTH": description.get("expected_length"),
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_HAS_PTGEN": _shell_bool(description.get("has_ptgen_description")) if description.get("has_ptgen_description") is not None else None,
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_PTGEN_LENGTH": description.get("ptgen_description_length"),
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_HAS_EXTERNAL_IDS": _shell_bool(description.get("has_external_ids")) if description.get("has_external_ids") is not None else None,
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_IMDB_LINK": external_links.get("imdb"),
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_TMDB_LINK": external_links.get("tmdb"),
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_DOUBAN_LINK": external_links.get("douban"),
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_HAS_MEDIAINFO_OR_BDINFO": _shell_bool(description.get("has_mediainfo_or_bdinfo")) if description.get("has_mediainfo_or_bdinfo") is not None else None,
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_HAS_SCREENSHOTS": _shell_bool(description.get("has_screenshot_bbcode")) if description.get("has_screenshot_bbcode") is not None else None,
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_IMAGE_COUNT": description.get("bbcode_image_count"),
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_IMAGE_URLS": ",".join(_string_list(description.get("bbcode_image_urls"))),
        "PTCLI_CLOSURE_REVIEW_DESCRIPTION_MISSING": ",".join(_string_list(description.get("missing"))),
        "PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_READY": _summary_check_bool_field(checks, "source.ready"),
        "PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_TORRENT_FILE": _summary_check_bool_field(checks, "source.torrent_file_evidence"),
        "PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_TORRENT_HASH": _summary_check_bool_field(checks, "source.torrent_hash"),
        "PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_INJECTED_HASH": _summary_check_bool_field(checks, "source.injected_torrent_hash"),
        "PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_INJECTION_VISIBLE": _summary_check_bool_field(checks, "source.injection_visible_in_client"),
        "PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_INJECTION_VERIFIED": _summary_check_bool_field(checks, "source.injection_verified"),
        "PTCLI_CLOSURE_REVIEW_CHECK_SOURCE_WAIT": _summary_check_bool_field(checks, "source.wait_evidence"),
        "PTCLI_CLOSURE_REVIEW_CHECK_TARGET_PREPARATION": _summary_check_bool_field(checks, "target.preparation_ready"),
        "PTCLI_CLOSURE_REVIEW_CHECK_TARGET_UPLOADED_WAIT": _summary_check_bool_field(checks, "target.uploaded_wait_evidence"),
        "PTCLI_CLOSURE_REVIEW_CHECK_TARGET_RULES": _summary_check_bool_field(checks, "target.rule_obligations"),
    }


def _summary_check_resume_material_shell_fields(resume_state: dict[str, Any]) -> dict[str, Any]:
    materials = resume_state.get("materials") if isinstance(resume_state.get("materials"), dict) else {}
    closure = materials.get("closure") if isinstance(materials.get("closure"), dict) else {}
    critical_domains = closure.get("critical_domains") if isinstance(closure.get("critical_domains"), dict) else {}
    metadata = closure.get("metadata") if isinstance(closure.get("metadata"), dict) else {}
    mediainfo = closure.get("mediainfo") if isinstance(closure.get("mediainfo"), dict) else {}
    bdinfo = closure.get("bdinfo") if isinstance(closure.get("bdinfo"), dict) else {}
    screenshots = closure.get("screenshots") if isinstance(closure.get("screenshots"), dict) else {}
    image_host = closure.get("image_host") if isinstance(closure.get("image_host"), dict) else {}
    image_host_urls = image_host.get("urls") if isinstance(image_host.get("urls"), dict) else {}
    description = closure.get("description") if isinstance(closure.get("description"), dict) else {}
    media_info = description.get("media_info") if isinstance(description.get("media_info"), dict) else {}
    screenshot_coverage = description.get("screenshot_coverage") if isinstance(description.get("screenshot_coverage"), dict) else {}
    external_id_readiness = description.get("external_id_readiness") if isinstance(description.get("external_id_readiness"), dict) else {}
    external_links = description.get("external_links") if isinstance(description.get("external_links"), dict) else {}
    critical_path = closure.get("critical_path") if isinstance(closure.get("critical_path"), dict) else {}
    recovery_hints = materials.get("recovery_hints") if isinstance(materials.get("recovery_hints"), list) else []
    first_recovery_command = _first_material_recovery_command(recovery_hints)
    recovery_command_coverage = _material_recovery_command_coverage(recovery_hints)
    recovery_completion_command = _material_recovery_completion_command(resume_state, recovery_hints, recovery_command_coverage)
    return {
        "PTCLI_RESUME_MATERIALS_PRESENT": _shell_bool(bool(materials)) if resume_state else None,
        "PTCLI_RESUME_TARGET_MATERIALS_READY": _shell_bool(materials.get("target_materials_ready")) if materials.get("target_materials_ready") is not None else None,
        "PTCLI_RESUME_TARGET_PREPARATION_READY": _shell_bool(materials.get("target_preparation_ready")) if materials.get("target_preparation_ready") is not None else None,
        "PTCLI_RESUME_TARGET_MATERIALS_MISSING": ",".join(_string_list(materials.get("target_materials_missing"))),
        "PTCLI_RESUME_TARGET_PREPARATION_MISSING": ",".join(_string_list(materials.get("target_preparation_missing"))),
        "PTCLI_RESUME_MATERIAL_CLOSURE_READY": _shell_bool(closure.get("ready")) if closure.get("ready") is not None else None,
        "PTCLI_RESUME_MATERIAL_CLOSURE_MISSING": ",".join(_string_list(closure.get("missing"))),
        "PTCLI_RESUME_MATERIAL_CRITICAL_READY": _shell_bool(closure.get("critical_ready")) if closure.get("critical_ready") is not None else None,
        "PTCLI_RESUME_MATERIAL_CRITICAL_MISSING": ",".join(_string_list(closure.get("critical_missing"))),
        "PTCLI_RESUME_MATERIAL_CRITICAL_METADATA_READY": _shell_bool(_material_critical_domain_ready(critical_domains, "metadata")),
        "PTCLI_RESUME_MATERIAL_CRITICAL_METADATA_MISSING": ",".join(_material_critical_domain_missing(critical_domains, "metadata")),
        "PTCLI_RESUME_MATERIAL_CRITICAL_MEDIA_INFO_READY": _shell_bool(_material_critical_domain_ready(critical_domains, "media_info")),
        "PTCLI_RESUME_MATERIAL_CRITICAL_MEDIA_INFO_MISSING": ",".join(_material_critical_domain_missing(critical_domains, "media_info")),
        "PTCLI_RESUME_MATERIAL_CRITICAL_SCREENSHOTS_READY": _shell_bool(_material_critical_domain_ready(critical_domains, "screenshots")),
        "PTCLI_RESUME_MATERIAL_CRITICAL_SCREENSHOTS_MISSING": ",".join(_material_critical_domain_missing(critical_domains, "screenshots")),
        "PTCLI_RESUME_MATERIAL_CRITICAL_IMAGE_HOST_READY": _shell_bool(_material_critical_domain_ready(critical_domains, "image_host")),
        "PTCLI_RESUME_MATERIAL_CRITICAL_IMAGE_HOST_MISSING": ",".join(_material_critical_domain_missing(critical_domains, "image_host")),
        "PTCLI_RESUME_MATERIAL_CRITICAL_DESCRIPTION_READY": _shell_bool(_material_critical_domain_ready(critical_domains, "description")),
        "PTCLI_RESUME_MATERIAL_CRITICAL_DESCRIPTION_MISSING": ",".join(_material_critical_domain_missing(critical_domains, "description")),
        "PTCLI_RESUME_MATERIAL_CRITICAL_PATH_READY": _shell_bool(critical_path.get("ready")) if critical_path.get("ready") is not None else None,
        "PTCLI_RESUME_MATERIAL_CRITICAL_PATH_NEXT_STEP": critical_path.get("next_step"),
        "PTCLI_RESUME_MATERIAL_CRITICAL_PATH_MISSING": ",".join(_string_list(critical_path.get("missing"))),
        "PTCLI_RESUME_MATERIAL_CRITICAL_PATH": json.dumps(critical_path, ensure_ascii=False) if critical_path else None,
        "PTCLI_RESUME_MATERIAL_METADATA_READY": _shell_bool(metadata.get("ready")) if metadata.get("ready") is not None else None,
        "PTCLI_RESUME_MATERIAL_METADATA_GENERATED": _shell_bool(metadata.get("generated")) if metadata.get("generated") is not None else None,
        "PTCLI_RESUME_MATERIAL_METADATA_MISSING": ",".join(_string_list(metadata.get("missing"))),
        "PTCLI_RESUME_MATERIAL_METADATA_READINESS": json.dumps(metadata.get("readiness"), ensure_ascii=False) if isinstance(metadata.get("readiness"), dict) and metadata.get("readiness") else None,
        "PTCLI_RESUME_MATERIAL_METADATA_IMDB_ID": metadata.get("imdb_id"),
        "PTCLI_RESUME_MATERIAL_METADATA_TMDB_ID": metadata.get("tmdb_id"),
        "PTCLI_RESUME_MATERIAL_METADATA_DOUBAN_ID": metadata.get("douban_id"),
        "PTCLI_RESUME_MATERIAL_METADATA_DOUBAN_URL": metadata.get("douban_url"),
        "PTCLI_RESUME_MATERIAL_PTGEN_DESCRIPTION_LENGTH": metadata.get("ptgen_description_length"),
        "PTCLI_RESUME_MATERIAL_MEDIAINFO_READY": _shell_bool(mediainfo.get("ready")) if mediainfo.get("ready") is not None else None,
        "PTCLI_RESUME_MATERIAL_MEDIAINFO_GENERATED": _shell_bool(mediainfo.get("generated")) if mediainfo.get("generated") is not None else None,
        "PTCLI_RESUME_MATERIAL_MEDIAINFO_FILE": mediainfo.get("path"),
        "PTCLI_RESUME_MATERIAL_BDINFO_READY": _shell_bool(bdinfo.get("ready")) if bdinfo.get("ready") is not None else None,
        "PTCLI_RESUME_MATERIAL_BDINFO_REQUIRED": _shell_bool(bdinfo.get("required")) if bdinfo.get("required") is not None else None,
        "PTCLI_RESUME_MATERIAL_BDINFO_GENERATED": _shell_bool(bdinfo.get("generated")) if bdinfo.get("generated") is not None else None,
        "PTCLI_RESUME_MATERIAL_BDINFO_FILE": bdinfo.get("path"),
        "PTCLI_RESUME_MATERIAL_SCREENSHOTS_READY": _shell_bool(screenshots.get("ready")) if screenshots.get("ready") is not None else None,
        "PTCLI_RESUME_MATERIAL_SCREENSHOTS_GENERATED": _shell_bool(screenshots.get("generated")) if screenshots.get("generated") is not None else None,
        "PTCLI_RESUME_MATERIAL_SCREENSHOTS_COUNT": screenshots.get("count"),
        "PTCLI_RESUME_MATERIAL_SCREENSHOTS_FILES": ",".join(_string_list(screenshots.get("files"))),
        "PTCLI_RESUME_MATERIAL_IMAGE_HOST_READY": _shell_bool(image_host.get("ready")) if image_host.get("ready") is not None else None,
        "PTCLI_RESUME_MATERIAL_IMAGE_HOST_GENERATED": _shell_bool(image_host.get("generated")) if image_host.get("generated") is not None else None,
        "PTCLI_RESUME_MATERIAL_IMAGE_HOST_HOST": image_host.get("host"),
        "PTCLI_RESUME_MATERIAL_IMAGE_HOST_COUNT": image_host.get("count"),
        "PTCLI_RESUME_MATERIAL_IMAGE_HOST_FILE": image_host.get("image_host_file"),
        "PTCLI_RESUME_MATERIAL_IMAGE_HOST_ITEM_COUNT": image_host_urls.get("item_count"),
        "PTCLI_RESUME_MATERIAL_IMAGE_HOST_VALID_COUNT": image_host_urls.get("valid_count"),
        "PTCLI_RESUME_MATERIAL_IMAGE_HOST_INVALID_COUNT": image_host_urls.get("invalid_count"),
        "PTCLI_RESUME_MATERIAL_IMAGE_HOST_RAW_URLS": ",".join(_string_list(image_host_urls.get("raw_urls"))),
        "PTCLI_RESUME_MATERIAL_IMAGE_HOST_IMG_URLS": ",".join(_string_list(image_host_urls.get("img_urls"))),
        "PTCLI_RESUME_MATERIAL_IMAGE_HOST_WEB_URLS": ",".join(_string_list(image_host_urls.get("web_urls"))),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_READY": _shell_bool(description.get("ready")) if description.get("ready") is not None else None,
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_PTGEN": _shell_bool(description.get("has_ptgen_description")) if description.get("has_ptgen_description") is not None else None,
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_PTGEN_LENGTH": description.get("ptgen_description_length"),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_EXTERNAL_IDS": _shell_bool(description.get("has_external_ids")) if description.get("has_external_ids") is not None else None,
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_EXTERNAL_ID_READINESS": json.dumps(external_id_readiness, ensure_ascii=False) if external_id_readiness else None,
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_EXTERNAL_ID_MISSING": ",".join(_string_list(description.get("external_id_missing"))),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_IMDB": _shell_bool(external_id_readiness.get("imdb")) if "imdb" in external_id_readiness else None,
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_TMDB": _shell_bool(external_id_readiness.get("tmdb")) if "tmdb" in external_id_readiness else None,
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_DOUBAN": _shell_bool(external_id_readiness.get("douban")) if "douban" in external_id_readiness else None,
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_IMDB_LINK": external_links.get("imdb"),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_TMDB_LINK": external_links.get("tmdb"),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_DOUBAN_LINK": external_links.get("douban"),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_MEDIAINFO_OR_BDINFO": _shell_bool(description.get("has_mediainfo_or_bdinfo")) if description.get("has_mediainfo_or_bdinfo") is not None else None,
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_MEDIAINFO_SOURCE": media_info.get("source"),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_MEDIAINFO_LENGTH": media_info.get("length"),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_MEDIAINFO_HAS_EXCERPT": _shell_bool(media_info.get("has_excerpt")) if media_info.get("has_excerpt") is not None else None,
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_HAS_SCREENSHOTS": _shell_bool(description.get("has_screenshot_bbcode")) if description.get("has_screenshot_bbcode") is not None else None,
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_IMAGE_COUNT": description.get("bbcode_image_count"),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_IMAGE_URLS": ",".join(_string_list(description.get("bbcode_image_urls"))),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_READY": _shell_bool(screenshot_coverage.get("ready")) if screenshot_coverage.get("ready") is not None else None,
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_EXPECTED_URLS": ",".join(_string_list(screenshot_coverage.get("expected_urls"))),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_DESCRIPTION_URLS": ",".join(_string_list(screenshot_coverage.get("description_urls"))),
        "PTCLI_RESUME_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_MISSING_URLS": ",".join(_string_list(screenshot_coverage.get("missing_urls"))),
        "PTCLI_RESUME_MATERIAL_RECOVERY_HINT_COUNT": len(recovery_hints),
        "PTCLI_RESUME_MATERIAL_RECOVERY_KEYS": ",".join(str(hint.get("key")) for hint in recovery_hints if isinstance(hint, dict) and hint.get("key")),
        "PTCLI_RESUME_MATERIAL_RECOVERY_COMMAND_AVAILABLE": _shell_bool(any(bool(hint.get("resume_command_available")) for hint in recovery_hints if isinstance(hint, dict))),
        "PTCLI_RESUME_MATERIAL_RECOVERY_COMMAND_STAGES": ",".join(str(hint.get("resume_command_stage")) for hint in recovery_hints if isinstance(hint, dict) and hint.get("resume_command_available") and hint.get("resume_command_stage")),
        "PTCLI_RESUME_MATERIAL_RECOVERY_MISSING_FLAGS": ",".join(_material_recovery_missing_flags(recovery_hints)),
        "PTCLI_RESUME_MATERIAL_RECOVERY_REQUIRED_FLAGS": ",".join(_material_recovery_required_flags(recovery_hints)),
        "PTCLI_RESUME_MATERIAL_RECOVERY_EXISTING_FILE_OPTIONS": ",".join(_material_recovery_existing_file_options(recovery_hints)),
        "PTCLI_RESUME_MATERIAL_RECOVERY_COMPLETION_COMMAND": recovery_completion_command.get("command"),
        "PTCLI_RESUME_MATERIAL_RECOVERY_COMPLETION_COMMAND_ARGV": json.dumps(recovery_completion_command.get("argv"), ensure_ascii=False) if recovery_completion_command.get("argv") else None,
        "PTCLI_RESUME_MATERIAL_RECOVERY_COMMAND_COVERAGE_READY": _shell_bool(recovery_command_coverage.get("ready")) if recovery_command_coverage.get("ready") is not None else None,
        "PTCLI_RESUME_MATERIAL_RECOVERY_COMMAND_COVERAGE_AVAILABLE": recovery_command_coverage.get("available_count"),
        "PTCLI_RESUME_MATERIAL_RECOVERY_COMMAND_COVERAGE_MISSING": recovery_command_coverage.get("missing_count"),
        "PTCLI_RESUME_MATERIAL_RECOVERY_FIRST_UNCOVERED_KEY": recovery_command_coverage.get("first_uncovered_key"),
        "PTCLI_RESUME_MATERIAL_RECOVERY_FIRST_UNCOVERED_FLAGS": ",".join(_string_list(recovery_command_coverage.get("first_uncovered_missing_flags"))),
        "PTCLI_RESUME_MATERIAL_FIRST_RECOVERY_COMMAND": first_recovery_command.get("command"),
        "PTCLI_RESUME_MATERIAL_FIRST_RECOVERY_COMMAND_ARGV": json.dumps(first_recovery_command.get("argv"), ensure_ascii=False) if first_recovery_command.get("argv") else None,
        "PTCLI_RESUME_MATERIAL_RECOVERY_HINTS": json.dumps(recovery_hints, ensure_ascii=False) if recovery_hints else None,
        "PTCLI_RESUME_MATERIAL_NEXT_ACTIONS": " | ".join(_string_list(materials.get("next_actions"))),
    }


def _material_recovery_missing_flags(recovery_hints: list[Any]) -> list[str]:
    missing: list[str] = []
    for hint in recovery_hints:
        if isinstance(hint, dict):
            _extend_unique_string(missing, _string_list(hint.get("missing_command_flags")))
    return missing


def _material_recovery_required_flags(recovery_hints: list[Any]) -> list[str]:
    flags: list[str] = []
    for hint in recovery_hints:
        if isinstance(hint, dict):
            _extend_unique_string(flags, _string_list(hint.get("required_command_flags") or hint.get("command_flags")))
    return flags


def _material_recovery_existing_file_options(recovery_hints: list[Any]) -> list[str]:
    options: list[str] = []
    for hint in recovery_hints:
        if isinstance(hint, dict):
            _extend_unique_string(options, _string_list(hint.get("existing_file_options")))
    return options


def _material_recovery_command_coverage(recovery_hints: list[Any]) -> dict[str, Any]:
    hints = [hint for hint in recovery_hints if isinstance(hint, dict)]
    uncovered = [hint for hint in hints if hint.get("resume_command_available") is not True]
    first_uncovered = uncovered[0] if uncovered else {}
    return {
        "ready": bool(hints) and not uncovered,
        "hint_count": len(hints),
        "available_count": len(hints) - len(uncovered),
        "missing_count": len(uncovered),
        "first_uncovered_key": first_uncovered.get("key"),
        "first_uncovered_missing_flags": _string_list(first_uncovered.get("missing_command_flags")),
        "uncovered_keys": [str(hint.get("key")) for hint in uncovered if hint.get("key")],
    }


def _material_recovery_completion_command(resume_state: dict[str, Any], recovery_hints: list[Any], command_coverage: dict[str, Any]) -> dict[str, Any]:
    if command_coverage.get("ready") is True or not command_coverage.get("hint_count"):
        return {"command": None, "argv": []}
    if resume_state.get("next_stage") != "resume-target-package":
        return {"command": None, "argv": []}
    argv = _argv_list(resume_state.get("next_command_argv"))
    if not argv:
        return {"command": None, "argv": []}
    completed_argv = list(argv)
    for flag in _material_recovery_missing_flags(recovery_hints):
        if flag not in completed_argv:
            completed_argv.append(flag)
    if completed_argv == argv:
        return {"command": None, "argv": []}
    return {"command": shlex.join(completed_argv), "argv": completed_argv}


def _first_material_recovery_command(recovery_hints: list[Any]) -> dict[str, Any]:
    for hint in recovery_hints:
        if isinstance(hint, dict) and hint.get("resume_command_available"):
            argv = hint.get("resume_command_argv") if isinstance(hint.get("resume_command_argv"), list) else []
            return {"command": hint.get("resume_command"), "argv": argv}
    return {"command": None, "argv": []}


def _material_critical_domain_ready(domains: dict[str, Any], name: str) -> bool:
    domain = domains.get(name) if isinstance(domains.get(name), dict) else {}
    return bool(domain.get("ready"))


def _material_critical_domain_missing(domains: dict[str, Any], name: str) -> list[str]:
    domain = domains.get(name) if isinstance(domains.get(name), dict) else {}
    return _string_list(domain.get("missing"))


def _summary_check_target_upload_shell_fields(target_upload_diagnostics: dict[str, Any]) -> dict[str, Any]:
    completion = target_upload_diagnostics.get("completion") if isinstance(target_upload_diagnostics.get("completion"), dict) else {}
    checks = completion.get("checks") if isinstance(completion.get("checks"), dict) else {}
    wait_query = completion.get("uploaded_wait_query") if isinstance(completion.get("uploaded_wait_query"), dict) else {}
    preflight = target_upload_diagnostics.get("preflight") if isinstance(target_upload_diagnostics.get("preflight"), dict) else {}
    preflight_torrent = preflight.get("torrent_file") if isinstance(preflight.get("torrent_file"), dict) else {}
    payload_review = target_upload_diagnostics.get("payload_review") if isinstance(target_upload_diagnostics.get("payload_review"), dict) else {}
    payload_description = payload_review.get("description") if isinstance(payload_review.get("description"), dict) else {}
    payload_materials = payload_review.get("materials") if isinstance(payload_review.get("materials"), dict) else {}
    payload_description_evidence = payload_description.get("evidence") if isinstance(payload_description.get("evidence"), dict) else {}
    external_id_readiness = payload_description.get("external_id_readiness") if isinstance(payload_description.get("external_id_readiness"), dict) else {}
    external_links = payload_description.get("external_links") if isinstance(payload_description.get("external_links"), dict) else {}
    payload_description_completeness = payload_description.get("completeness") if isinstance(payload_description.get("completeness"), dict) else {}
    screenshot_coverage = payload_description.get("screenshot_coverage") if isinstance(payload_description.get("screenshot_coverage"), dict) else {}
    return {
        "PTCLI_TARGET_UPLOAD_PRESENT": _shell_bool(target_upload_diagnostics.get("present")) if "present" in target_upload_diagnostics else None,
        "PTCLI_TARGET_UPLOAD_MODE": target_upload_diagnostics.get("mode"),
        "PTCLI_TARGET_UPLOAD_READY": _shell_bool(target_upload_diagnostics.get("ready")) if target_upload_diagnostics.get("ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_UPLOADED": _shell_bool(target_upload_diagnostics.get("uploaded")) if target_upload_diagnostics.get("uploaded") is not None else None,
        "PTCLI_TARGET_UPLOAD_COMPLETE": _shell_bool(completion.get("complete")) if completion.get("complete") is not None else None,
        "PTCLI_TARGET_UPLOAD_MISSING": ",".join(_string_list(completion.get("missing"))),
        "PTCLI_TARGET_UPLOAD_TORRENT_ID": completion.get("uploaded_torrent_id"),
        "PTCLI_TARGET_UPLOAD_TORRENT_HASH": completion.get("uploaded_torrent_hash"),
        "PTCLI_TARGET_UPLOAD_TORRENT_PATH": completion.get("uploaded_torrent_path"),
        "PTCLI_TARGET_UPLOAD_INJECTED_HASH": completion.get("injected_torrent_hash"),
        "PTCLI_TARGET_UPLOAD_SAVE_PATH": completion.get("uploaded_save_path"),
        "PTCLI_TARGET_UPLOAD_WAIT_QUERY_HASH": wait_query.get("torrent_hash"),
        "PTCLI_TARGET_UPLOAD_WAIT_QUERY_CONTENT_PATH": wait_query.get("content_path"),
        "PTCLI_TARGET_UPLOAD_WAIT_QUERY_TIMEOUT": wait_query.get("timeout"),
        "PTCLI_TARGET_UPLOAD_WAIT_QUERY_INTERVAL": wait_query.get("interval"),
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_STATUS": completion.get("preflight_status"),
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_READY": _shell_bool(preflight.get("ready")) if preflight.get("ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_BLOCKERS": "|".join(_string_list(preflight.get("blockers"))),
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_MISSING": ",".join(_string_list(preflight.get("missing"))),
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_DESCRIPTION_MISSING": ",".join(_string_list(preflight.get("description_missing"))),
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_PREPARATION_READY": _shell_bool(preflight.get("target_preparation_ready")) if preflight.get("target_preparation_ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_MATERIALS_READY": _shell_bool(preflight.get("materials_ready")) if preflight.get("materials_ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_METADATA_READY": _shell_bool(preflight.get("metadata_ready")) if preflight.get("metadata_ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_ASSETS_READY": _shell_bool(preflight.get("assets_ready")) if preflight.get("assets_ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_DESCRIPTION_READY": _shell_bool(preflight.get("description_ready")) if preflight.get("description_ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_PAYLOAD_READY": _shell_bool(preflight.get("payload_ready")) if preflight.get("payload_ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_PAYLOAD_CHECKS_READY": _shell_bool(preflight.get("payload_checks_ready")) if preflight.get("payload_checks_ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_DESCRIPTION_CHECKS_READY": _shell_bool(preflight.get("description_checks_ready")) if preflight.get("description_checks_ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_MATERIALS_READY_REQUIRED": _shell_bool(preflight.get("materials_ready_required")) if preflight.get("materials_ready_required") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_TORRENT_PATH": preflight_torrent.get("path"),
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_TORRENT_MTEAM_SAFE": _shell_bool(preflight_torrent.get("mteam_safe")) if preflight_torrent.get("mteam_safe") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_TORRENT_METADATA_READABLE": _shell_bool(preflight_torrent.get("metadata_readable")) if preflight_torrent.get("metadata_readable") is not None else None,
        "PTCLI_TARGET_UPLOAD_PREFLIGHT_TORRENT_SOURCE_FLAG": preflight_torrent.get("source_flag"),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_REVIEW_PRESENT": _shell_bool(payload_review.get("present")) if "present" in payload_review else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_RECOVERY_MISSING": ",".join(_string_list(payload_review.get("recovery_missing"))),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_NEXT_ACTIONS": " | ".join(_string_list(payload_review.get("next_actions"))),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETE": _shell_bool(payload_description_completeness.get("ready")) if payload_description_completeness.get("ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETENESS_MISSING": ",".join(_string_list(payload_description_completeness.get("missing"))),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETENESS_RECOVERY_MISSING": ",".join(_string_list(payload_description_completeness.get("recovery_missing"))),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETENESS_NEXT_ACTIONS": " | ".join(_string_list(payload_description_completeness.get("next_actions"))),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_COMPLETENESS_CHECKS": json.dumps(payload_description_completeness.get("checks"), ensure_ascii=False) if isinstance(payload_description_completeness.get("checks"), list) else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_DESCRIPTION_EVIDENCE": json.dumps(payload_description_evidence, ensure_ascii=False) if payload_description_evidence else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_PTGEN": _shell_bool(payload_description.get("has_ptgen_description")) if payload_description.get("has_ptgen_description") is not None else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_PTGEN_LENGTH": payload_description.get("ptgen_description_length"),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_EXTERNAL_IDS": _shell_bool(all(external_id_readiness.get(name) is True for name in ("imdb", "tmdb", "douban"))) if external_id_readiness else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_EXTERNAL_ID_MISSING": ",".join(_string_list(payload_description.get("external_id_missing"))),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_IMDB": _shell_bool(external_id_readiness.get("imdb")) if "imdb" in external_id_readiness else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_TMDB": _shell_bool(external_id_readiness.get("tmdb")) if "tmdb" in external_id_readiness else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_DOUBAN": _shell_bool(external_id_readiness.get("douban")) if "douban" in external_id_readiness else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_IMDB_LINK": external_links.get("imdb"),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_TMDB_LINK": external_links.get("tmdb"),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_DOUBAN_LINK": external_links.get("douban"),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_MEDIAINFO_OR_BDINFO": _shell_bool(payload_description.get("has_mediainfo_or_bdinfo")) if payload_description.get("has_mediainfo_or_bdinfo") is not None else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_MEDIAINFO_SOURCE": payload_materials.get("mediainfo_or_bdinfo_source"),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_MEDIAINFO_LENGTH": payload_materials.get("mediainfo_or_bdinfo_length"),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_HAS_SCREENSHOTS": _shell_bool(payload_description.get("has_screenshot_bbcode")) if payload_description.get("has_screenshot_bbcode") is not None else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_IMAGE_COUNT": payload_description.get("bbcode_image_count"),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_IMAGE_URLS": ",".join(_string_list(payload_description.get("bbcode_image_urls"))),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_IMAGE_HOST_COUNT": payload_materials.get("image_host_count"),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_IMAGE_HOST_URLS": ",".join(_string_list(payload_materials.get("image_host_urls"))),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_SCREENSHOT_COVERAGE_READY": _shell_bool(screenshot_coverage.get("ready")) if screenshot_coverage.get("ready") is not None else None,
        "PTCLI_TARGET_UPLOAD_PAYLOAD_SCREENSHOT_COVERAGE_EXPECTED_URLS": ",".join(_string_list(screenshot_coverage.get("expected_urls"))),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_SCREENSHOT_COVERAGE_DESCRIPTION_URLS": ",".join(_string_list(screenshot_coverage.get("description_urls"))),
        "PTCLI_TARGET_UPLOAD_PAYLOAD_SCREENSHOT_COVERAGE_MISSING_URLS": ",".join(_string_list(screenshot_coverage.get("missing_urls"))),
        "PTCLI_TARGET_UPLOAD_CHECK_PREPARATION_READY": _summary_check_bool_field(checks, "target_preparation_ready"),
        "PTCLI_TARGET_UPLOAD_CHECK_UPLOADED": _summary_check_bool_field(checks, "uploaded"),
        "PTCLI_TARGET_UPLOAD_CHECK_TORRENT_FILE": _summary_check_bool_field(checks, "uploaded_torrent_file"),
        "PTCLI_TARGET_UPLOAD_CHECK_TORRENT_HASH": _summary_check_bool_field(checks, "uploaded_torrent_hash"),
        "PTCLI_TARGET_UPLOAD_CHECK_INJECTION_VISIBLE": _summary_check_bool_field(checks, "injection_visible_in_client"),
        "PTCLI_TARGET_UPLOAD_CHECK_INJECTION_VERIFIED": _summary_check_bool_field(checks, "injection_verified"),
        "PTCLI_TARGET_UPLOAD_CHECK_WAIT_COMPLETE": _summary_check_bool_field(checks, "uploaded_wait_complete"),
        "PTCLI_TARGET_UPLOAD_CHECK_HASH_CONSISTENT": _summary_check_bool_field(checks, "hash_consistent"),
        "PTCLI_TARGET_UPLOAD_CHECK_DUPLICATE_CLEAN": _summary_check_bool_field(checks, "duplicate_clean"),
        "PTCLI_TARGET_UPLOAD_CHECK_RULES_READY": _summary_check_bool_field(checks, "rule_obligations_ready"),
    }


def _summary_check_uploaded_followup_shell_fields(resume_state: dict[str, Any]) -> dict[str, Any]:
    followup = resume_state.get("uploaded_followup") if isinstance(resume_state.get("uploaded_followup"), dict) else {}
    wait_retry = followup.get("wait_retry") if isinstance(followup.get("wait_retry"), dict) else {}
    wait_query = followup.get("uploaded_wait_query") if isinstance(followup.get("uploaded_wait_query"), dict) else {}
    torrent_evidence = followup.get("uploaded_torrent_file_evidence") if isinstance(followup.get("uploaded_torrent_file_evidence"), dict) else {}
    gates = followup.get("gates") if isinstance(followup.get("gates"), dict) else {}
    next_actions = _string_list(followup.get("next_actions"))
    return {
        "PTCLI_UPLOADED_FOLLOWUP_PRESENT": _shell_bool(bool(followup)) if resume_state else None,
        "PTCLI_UPLOADED_FOLLOWUP_READY": _shell_bool(followup.get("ready")) if followup.get("ready") is not None else None,
        "PTCLI_READY_FOR_UPLOADED_SEEDING": _shell_bool(followup.get("ready_for_uploaded_seeding")) if followup.get("ready_for_uploaded_seeding") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_GATES": json.dumps(gates, ensure_ascii=False) if gates else None,
        "PTCLI_UPLOADED_FOLLOWUP_BLOCKERS": "|".join(_string_list(followup.get("blockers"))),
        "PTCLI_UPLOADED_FOLLOWUP_UPLOADED": _shell_bool(followup.get("uploaded")) if followup.get("uploaded") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_DOWNLOADED": _shell_bool(followup.get("downloaded")) if followup.get("downloaded") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_INJECTED": _shell_bool(followup.get("injected")) if followup.get("injected") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_INJECTION_VERIFIED": _shell_bool(followup.get("injection_verified")) if followup.get("injection_verified") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_EVIDENCE": _shell_bool(followup.get("uploaded_wait_evidence")) if followup.get("uploaded_wait_evidence") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_HASH_CONSISTENT": _shell_bool(followup.get("hash_consistent")) if followup.get("hash_consistent") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_DUPLICATE_CLEAN": _shell_bool(followup.get("duplicate_clean")) if followup.get("duplicate_clean") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_RULES_READY": _shell_bool(followup.get("rule_obligations_ready")) if followup.get("rule_obligations_ready") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_MISSING": ",".join(_string_list(followup.get("missing"))),
        "PTCLI_UPLOADED_FOLLOWUP_TORRENT_ID": followup.get("uploaded_torrent_id"),
        "PTCLI_UPLOADED_FOLLOWUP_TORRENT_HASH": followup.get("uploaded_torrent_hash"),
        "PTCLI_UPLOADED_FOLLOWUP_INJECTED_HASH": followup.get("injected_torrent_hash"),
        "PTCLI_UPLOADED_FOLLOWUP_TORRENT_FILE": followup.get("uploaded_torrent_file"),
        "PTCLI_UPLOADED_FOLLOWUP_TORRENT_EXISTS": _shell_bool(torrent_evidence.get("exists")) if torrent_evidence.get("exists") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_TORRENT_IS_FILE": _shell_bool(torrent_evidence.get("is_file")) if torrent_evidence.get("is_file") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_TORRENT_SIZE_BYTES": torrent_evidence.get("size_bytes"),
        "PTCLI_UPLOADED_FOLLOWUP_TORRENT_SHA1": torrent_evidence.get("sha1"),
        "PTCLI_UPLOADED_FOLLOWUP_TORRENT_INFOHASH": torrent_evidence.get("torrent_hash"),
        "PTCLI_UPLOADED_FOLLOWUP_TORRENT_METADATA_READABLE": _shell_bool(torrent_evidence.get("metadata_readable")) if torrent_evidence.get("metadata_readable") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_TORRENT_REUSED": _shell_bool(torrent_evidence.get("reused")) if torrent_evidence.get("reused") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_SAVE_PATH": followup.get("uploaded_save_path"),
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_MISMATCH": _shell_bool(followup.get("qbit_wait_mismatch")) if followup.get("qbit_wait_mismatch") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_MISMATCHES": ",".join(_string_list(followup.get("qbit_wait_mismatches"))),
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_QUERY_HASH": wait_query.get("torrent_hash"),
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_QUERY_CONTENT_PATH": wait_query.get("content_path"),
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_QUERY_TIMEOUT": wait_query.get("timeout"),
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_QUERY_INTERVAL": wait_query.get("interval"),
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_RETRY_RECOMMENDED": _shell_bool(wait_retry.get("retry_recommended")) if wait_retry.get("retry_recommended") is not None else None,
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_HASH": wait_retry.get("suggested_torrent_hash"),
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_CONTENT_PATH": wait_retry.get("suggested_content_path"),
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_SUGGESTED_SAVE_PATH": wait_retry.get("suggested_save_path"),
        "PTCLI_UPLOADED_FOLLOWUP_WAIT_RETRY_REASON": wait_retry.get("reason"),
        "PTCLI_UPLOADED_FOLLOWUP_NEXT_ACTION_COUNT": len(next_actions),
        "PTCLI_UPLOADED_FOLLOWUP_FIRST_NEXT_ACTION": next_actions[0] if next_actions else None,
        "PTCLI_UPLOADED_FOLLOWUP_NEXT_ACTIONS": " | ".join(next_actions),
    }


def _summary_check_bool_field(checks: dict[str, Any], key: str) -> str | None:
    return _shell_bool(checks.get(key)) if key in checks else None


def _summary_check_material_shell_fields(material_diagnostics: dict[str, Any]) -> dict[str, Any]:
    sections = material_diagnostics.get("sections") if isinstance(material_diagnostics.get("sections"), dict) else {}
    prerequisites = sections.get("prerequisites") if isinstance(sections.get("prerequisites"), dict) else {}
    metadata = sections.get("metadata") if isinstance(sections.get("metadata"), dict) else {}
    metadata_readiness = metadata.get("readiness") if isinstance(metadata.get("readiness"), dict) else {}
    metadata_field_evidence = metadata.get("field_evidence") if isinstance(metadata.get("field_evidence"), dict) else {}
    metadata_fields = material_diagnostics.get("metadata_fields") if isinstance(material_diagnostics.get("metadata_fields"), dict) else {}
    imdb_field = metadata_fields.get("imdb_id") if isinstance(metadata_fields.get("imdb_id"), dict) else {}
    tmdb_field = metadata_fields.get("tmdb_id") if isinstance(metadata_fields.get("tmdb_id"), dict) else {}
    douban_id_field = metadata_fields.get("douban_id") if isinstance(metadata_fields.get("douban_id"), dict) else {}
    douban_url_field = metadata_fields.get("douban_url") if isinstance(metadata_fields.get("douban_url"), dict) else {}
    ptgen_field = metadata_fields.get("ptgen_description") if isinstance(metadata_fields.get("ptgen_description"), dict) else {}
    bdinfo = sections.get("bdinfo") if isinstance(sections.get("bdinfo"), dict) else {}
    mediainfo = sections.get("mediainfo") if isinstance(sections.get("mediainfo"), dict) else {}
    screenshots = sections.get("screenshots") if isinstance(sections.get("screenshots"), dict) else {}
    image_host = sections.get("image_host") if isinstance(sections.get("image_host"), dict) else {}
    bdinfo_evidence = bdinfo.get("bdinfo_file_evidence") or bdinfo.get("raw_bdinfo_file_evidence")
    mediainfo_evidence = mediainfo.get("mediainfo_file_evidence")
    screenshot_evidence = screenshots.get("screenshot_files_evidence")
    image_host_file_evidence = image_host.get("image_host_file_evidence")
    image_host_urls = material_diagnostics.get("image_host_urls") if isinstance(material_diagnostics.get("image_host_urls"), dict) else {}
    disc_structure = material_diagnostics.get("disc_structure") if isinstance(material_diagnostics.get("disc_structure"), dict) else {}
    description = material_diagnostics.get("description") if isinstance(material_diagnostics.get("description"), dict) else {}
    description_input_chain = material_diagnostics.get("description_input_chain") if isinstance(material_diagnostics.get("description_input_chain"), dict) else {}
    if not description_input_chain and isinstance(description.get("input_chain"), dict):
        description_input_chain = description["input_chain"]
    description_input_inputs = description_input_chain.get("inputs") if isinstance(description_input_chain.get("inputs"), dict) else {}
    description_input_metadata = description_input_inputs.get("metadata") if isinstance(description_input_inputs.get("metadata"), dict) else {}
    description_input_media_info = description_input_inputs.get("media_info") if isinstance(description_input_inputs.get("media_info"), dict) else {}
    description_input_screenshots = description_input_inputs.get("screenshots") if isinstance(description_input_inputs.get("screenshots"), dict) else {}
    description_input_image_host = description_input_inputs.get("image_host") if isinstance(description_input_inputs.get("image_host"), dict) else {}
    description_completeness = description.get("completeness") if isinstance(description.get("completeness"), dict) else {}
    description_evidence = description.get("evidence") if isinstance(description.get("evidence"), dict) else {}
    if not description_evidence:
        description_evidence = _description_evidence_from_readiness(material_diagnostics.get("readiness"))
    description_links = description.get("external_links") if isinstance(description.get("external_links"), dict) else {}
    description_external_id_readiness = description.get("external_id_readiness") if isinstance(description.get("external_id_readiness"), dict) else {}
    media_info = description.get("media_info") if isinstance(description.get("media_info"), dict) else {}
    screenshot_coverage = description.get("screenshot_coverage") if isinstance(description.get("screenshot_coverage"), dict) else {}
    metadata_chain = description_evidence.get("metadata_chain") if isinstance(description_evidence.get("metadata_chain"), dict) else {}
    media_info_chain = description_evidence.get("media_info_chain") if isinstance(description_evidence.get("media_info_chain"), dict) else {}
    screenshot_chain = description_evidence.get("screenshot_chain") if isinstance(description_evidence.get("screenshot_chain"), dict) else {}
    critical_domains = material_diagnostics.get("critical_domains") if isinstance(material_diagnostics.get("critical_domains"), dict) else {}
    critical_path = material_diagnostics.get("critical_path") if isinstance(material_diagnostics.get("critical_path"), dict) else {}
    live_gate = material_diagnostics.get("live_gate") if isinstance(material_diagnostics.get("live_gate"), dict) else {}
    return {
        "PTCLI_MATERIAL_PRESENT": _shell_bool(material_diagnostics.get("present")) if "present" in material_diagnostics else None,
        "PTCLI_MATERIAL_GENERATION_PRESENT": _shell_bool(material_diagnostics.get("generation_present")) if "generation_present" in material_diagnostics else None,
        "PTCLI_MATERIAL_GENERATION_READY": _shell_bool(material_diagnostics.get("generation_ready")) if material_diagnostics.get("generation_ready") is not None else None,
        "PTCLI_TARGET_MATERIALS_PRESENT": _shell_bool(material_diagnostics.get("target_materials_present")) if "target_materials_present" in material_diagnostics else None,
        "PTCLI_TARGET_MATERIALS_READY": _shell_bool(material_diagnostics.get("target_materials_ready")) if material_diagnostics.get("target_materials_ready") is not None else None,
        "PTCLI_TARGET_PREPARATION_READY": _shell_bool(material_diagnostics.get("target_preparation_ready")) if material_diagnostics.get("target_preparation_ready") is not None else None,
        "PTCLI_TARGET_MATERIALS_MISSING": ",".join(_string_list(material_diagnostics.get("target_materials_missing"))),
        "PTCLI_TARGET_PREPARATION_MISSING": ",".join(_string_list(material_diagnostics.get("target_preparation_missing"))),
        "PTCLI_READY_FOR_MTEAM_UPLOAD": _shell_bool(material_diagnostics.get("ready_for_mteam_upload")) if material_diagnostics.get("ready_for_mteam_upload") is not None else None,
        "PTCLI_MATERIAL_UPLOAD_BLOCKERS": "|".join(_string_list(material_diagnostics.get("upload_material_blockers"))),
        "PTCLI_MATERIAL_UPLOAD_GATES": json.dumps(material_diagnostics.get("upload_material_gates"), ensure_ascii=False) if isinstance(material_diagnostics.get("upload_material_gates"), dict) else None,
        "PTCLI_MATERIAL_LIVE_GATE_PRESENT": _shell_bool(live_gate.get("present")) if "present" in live_gate else None,
        "PTCLI_MATERIAL_LIVE_GATE_READY": _shell_bool(live_gate.get("ready")) if live_gate.get("ready") is not None else None,
        "PTCLI_MATERIAL_LIVE_GATE_READY_FOR_MTEAM_UPLOAD": _shell_bool(live_gate.get("ready_for_mteam_upload")) if live_gate.get("ready_for_mteam_upload") is not None else None,
        "PTCLI_MATERIAL_LIVE_GATE_MISSING": ",".join(_string_list(live_gate.get("missing"))),
        "PTCLI_MATERIAL_LIVE_GATE_BLOCKERS": "|".join(_string_list(live_gate.get("blockers"))),
        "PTCLI_MATERIAL_LIVE_GATE_NEXT_ACTIONS": " | ".join(_string_list(live_gate.get("next_actions"))),
        "PTCLI_MATERIAL_LIVE_GATE_GATES": json.dumps(live_gate.get("gates"), ensure_ascii=False) if isinstance(live_gate.get("gates"), dict) else None,
        "PTCLI_MATERIAL_CRITICAL_READY": _shell_bool(material_diagnostics.get("critical_ready")) if material_diagnostics.get("critical_ready") is not None else None,
        "PTCLI_MATERIAL_CRITICAL_MISSING": ",".join(_string_list(material_diagnostics.get("critical_missing"))),
        "PTCLI_MATERIAL_CRITICAL_METADATA_READY": _shell_bool(_material_critical_domain_ready(critical_domains, "metadata")),
        "PTCLI_MATERIAL_CRITICAL_METADATA_MISSING": ",".join(_material_critical_domain_missing(critical_domains, "metadata")),
        "PTCLI_MATERIAL_CRITICAL_MEDIA_INFO_READY": _shell_bool(_material_critical_domain_ready(critical_domains, "media_info")),
        "PTCLI_MATERIAL_CRITICAL_MEDIA_INFO_MISSING": ",".join(_material_critical_domain_missing(critical_domains, "media_info")),
        "PTCLI_MATERIAL_CRITICAL_SCREENSHOTS_READY": _shell_bool(_material_critical_domain_ready(critical_domains, "screenshots")),
        "PTCLI_MATERIAL_CRITICAL_SCREENSHOTS_MISSING": ",".join(_material_critical_domain_missing(critical_domains, "screenshots")),
        "PTCLI_MATERIAL_CRITICAL_IMAGE_HOST_READY": _shell_bool(_material_critical_domain_ready(critical_domains, "image_host")),
        "PTCLI_MATERIAL_CRITICAL_IMAGE_HOST_MISSING": ",".join(_material_critical_domain_missing(critical_domains, "image_host")),
        "PTCLI_MATERIAL_CRITICAL_DESCRIPTION_READY": _shell_bool(_material_critical_domain_ready(critical_domains, "description")),
        "PTCLI_MATERIAL_CRITICAL_DESCRIPTION_MISSING": ",".join(_material_critical_domain_missing(critical_domains, "description")),
        "PTCLI_MATERIAL_CRITICAL_PATH_READY": _shell_bool(critical_path.get("ready")) if critical_path.get("ready") is not None else None,
        "PTCLI_MATERIAL_CRITICAL_PATH_NEXT_STEP": critical_path.get("next_step"),
        "PTCLI_MATERIAL_CRITICAL_PATH_MISSING": ",".join(_string_list(critical_path.get("missing"))),
        "PTCLI_MATERIAL_CRITICAL_PATH": json.dumps(critical_path, ensure_ascii=False) if critical_path else None,
        "PTCLI_MATERIAL_BLOCKERS": ",".join(_string_list(material_diagnostics.get("blockers"))),
        "PTCLI_MATERIAL_DISC_TYPE": disc_structure.get("type"),
        "PTCLI_MATERIAL_DISC_BDMV": _shell_bool(disc_structure.get("bdmv")) if "bdmv" in disc_structure else None,
        "PTCLI_MATERIAL_DISC_PATH": disc_structure.get("path"),
        "PTCLI_MATERIAL_BDINFO_REQUIRED": _shell_bool(material_diagnostics.get("bdinfo_required")) if material_diagnostics.get("bdinfo_required") is not None else None,
        "PTCLI_MATERIAL_MEDIA_INFO_REQUIREMENT": material_diagnostics.get("media_info_requirement"),
        "PTCLI_MATERIAL_PREREQUISITES_OK": _summary_material_section_shell_bool(prerequisites),
        "PTCLI_MATERIAL_METADATA_OK": _summary_material_section_shell_bool(metadata),
        "PTCLI_MATERIAL_METADATA_MISSING": ",".join(_string_list(metadata.get("missing"))),
        "PTCLI_MATERIAL_METADATA_READINESS": json.dumps(metadata_readiness, ensure_ascii=False) if metadata_readiness else None,
        "PTCLI_MATERIAL_METADATA_FIELD_EVIDENCE": json.dumps(metadata_field_evidence, ensure_ascii=False) if metadata_field_evidence else None,
        "PTCLI_MATERIAL_METADATA_SOURCES": ",".join(_string_list(metadata.get("sources"))),
        "PTCLI_MATERIAL_METADATA_APPLIED_KEYS": ",".join(sorted(str(key) for key in metadata.get("applied", {}) if isinstance(metadata.get("applied"), dict))),
        "PTCLI_MATERIAL_METADATA_BLOCKERS": "|".join(_string_list(metadata.get("blockers"))),
        "PTCLI_MATERIAL_METADATA_READINESS_BLOCKERS": "|".join(_string_list(metadata.get("readiness_blockers"))),
        "PTCLI_MATERIAL_METADATA_BLOCKER_COUNT": len(_string_list(metadata.get("blockers"))),
        "PTCLI_MATERIAL_METADATA_READINESS_BLOCKER_COUNT": len(_string_list(metadata.get("readiness_blockers"))),
        "PTCLI_MATERIAL_METADATA_IMDB_ID": metadata.get("imdb_id"),
        "PTCLI_MATERIAL_METADATA_TMDB_ID": metadata.get("tmdb_id"),
        "PTCLI_MATERIAL_METADATA_DOUBAN_ID": metadata.get("douban_id"),
        "PTCLI_MATERIAL_METADATA_DOUBAN_URL": metadata.get("douban_url"),
        "PTCLI_MATERIAL_PTGEN_DESCRIPTION_LENGTH": metadata.get("ptgen_description_length"),
        "PTCLI_MATERIAL_METADATA_IMDB_READY": _shell_bool(imdb_field.get("ready")) if imdb_field.get("ready") is not None else None,
        "PTCLI_MATERIAL_METADATA_IMDB_SOURCE": imdb_field.get("source"),
        "PTCLI_MATERIAL_METADATA_TMDB_READY": _shell_bool(tmdb_field.get("ready")) if tmdb_field.get("ready") is not None else None,
        "PTCLI_MATERIAL_METADATA_TMDB_SOURCE": tmdb_field.get("source"),
        "PTCLI_MATERIAL_METADATA_DOUBAN_ID_READY": _shell_bool(douban_id_field.get("ready")) if douban_id_field.get("ready") is not None else None,
        "PTCLI_MATERIAL_METADATA_DOUBAN_ID_SOURCE": douban_id_field.get("source"),
        "PTCLI_MATERIAL_METADATA_DOUBAN_URL_READY": _shell_bool(douban_url_field.get("ready")) if douban_url_field.get("ready") is not None else None,
        "PTCLI_MATERIAL_METADATA_DOUBAN_URL_SOURCE": douban_url_field.get("source"),
        "PTCLI_MATERIAL_METADATA_PTGEN_READY": _shell_bool(ptgen_field.get("ready")) if ptgen_field.get("ready") is not None else None,
        "PTCLI_MATERIAL_METADATA_PTGEN_REQUIRED": _shell_bool(ptgen_field.get("required")) if ptgen_field.get("required") is not None else None,
        "PTCLI_MATERIAL_METADATA_PTGEN_SOURCE": ptgen_field.get("source"),
        "PTCLI_MATERIAL_BDINFO_OK": _summary_material_section_shell_bool(bdinfo),
        "PTCLI_MATERIAL_BDINFO_FILE": bdinfo.get("bdinfo_file"),
        "PTCLI_MATERIAL_BDINFO_FILE_EXISTS": _shell_bool(_material_evidence_all_files_exist(bdinfo_evidence)) if _material_evidence_all_files_exist(bdinfo_evidence) is not None else None,
        "PTCLI_MATERIAL_BDINFO_FILE_SHA1": _material_evidence_first_sha1(bdinfo_evidence),
        "PTCLI_MATERIAL_MEDIAINFO_OK": _summary_material_section_shell_bool(mediainfo),
        "PTCLI_MATERIAL_MEDIAINFO_FILE": mediainfo.get("mediainfo_file"),
        "PTCLI_MATERIAL_MEDIAINFO_FILE_EXISTS": _shell_bool(_material_evidence_all_files_exist(mediainfo_evidence)) if _material_evidence_all_files_exist(mediainfo_evidence) is not None else None,
        "PTCLI_MATERIAL_MEDIAINFO_FILE_SHA1": _material_evidence_first_sha1(mediainfo_evidence),
        "PTCLI_MATERIAL_SCREENSHOTS_OK": _summary_material_section_shell_bool(screenshots),
        "PTCLI_MATERIAL_SCREENSHOTS_COUNT": screenshots.get("count"),
        "PTCLI_MATERIAL_SCREENSHOTS_FILES_EXIST": _shell_bool(_material_evidence_all_files_exist(screenshot_evidence)) if _material_evidence_all_files_exist(screenshot_evidence) is not None else None,
        "PTCLI_MATERIAL_SCREENSHOTS_SHA1S": ",".join(_material_evidence_sha1s(screenshot_evidence)),
        "PTCLI_MATERIAL_IMAGE_HOST_OK": _summary_material_section_shell_bool(image_host),
        "PTCLI_MATERIAL_IMAGE_HOST_HOST": image_host.get("host"),
        "PTCLI_MATERIAL_IMAGE_HOST_COUNT": image_host.get("count"),
        "PTCLI_MATERIAL_IMAGE_HOST_FILE": image_host.get("image_host_file"),
        "PTCLI_MATERIAL_IMAGE_HOST_FILE_EXISTS": _shell_bool(_material_evidence_all_files_exist(image_host_file_evidence)) if _material_evidence_all_files_exist(image_host_file_evidence) is not None else None,
        "PTCLI_MATERIAL_IMAGE_HOST_FILE_SHA1": _material_evidence_first_sha1(image_host_file_evidence),
        "PTCLI_MATERIAL_IMAGE_HOST_ITEM_COUNT": image_host_urls.get("item_count"),
        "PTCLI_MATERIAL_IMAGE_HOST_VALID_COUNT": image_host_urls.get("valid_count"),
        "PTCLI_MATERIAL_IMAGE_HOST_INVALID_COUNT": image_host_urls.get("invalid_count"),
        "PTCLI_MATERIAL_IMAGE_HOST_RAW_URLS": ",".join(_string_list(image_host_urls.get("raw_urls"))),
        "PTCLI_MATERIAL_IMAGE_HOST_IMG_URLS": ",".join(_string_list(image_host_urls.get("img_urls"))),
        "PTCLI_MATERIAL_IMAGE_HOST_WEB_URLS": ",".join(_string_list(image_host_urls.get("web_urls"))),
        "PTCLI_MATERIAL_DESCRIPTION_READY": _shell_bool(description.get("ready")) if description.get("ready") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_CHAIN_READY": _shell_bool(description_input_chain.get("ready")) if description_input_chain.get("ready") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_CHAIN_MISSING": ",".join(_string_list(description_input_chain.get("missing"))),
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_CHAIN_NEXT_ACTIONS": " | ".join(_string_list(description_input_chain.get("next_actions"))),
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_METADATA_READY": _shell_bool(description_input_metadata.get("ready")) if description_input_metadata.get("ready") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_METADATA_IMDB": _shell_bool(description_input_metadata.get("imdb")) if description_input_metadata.get("imdb") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_METADATA_TMDB": _shell_bool(description_input_metadata.get("tmdb")) if description_input_metadata.get("tmdb") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_METADATA_DOUBAN": _shell_bool(description_input_metadata.get("douban")) if description_input_metadata.get("douban") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_METADATA_PTGEN": _shell_bool(description_input_metadata.get("ptgen_description")) if description_input_metadata.get("ptgen_description") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_MEDIA_INFO_READY": _shell_bool(description_input_media_info.get("ready")) if description_input_media_info.get("ready") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_MEDIAINFO_OR_BDINFO": _shell_bool(description_input_media_info.get("mediainfo_or_bdinfo"))
        if description_input_media_info.get("mediainfo_or_bdinfo") is not None
        else None,
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_BDINFO_FOR_DISC": _shell_bool(description_input_media_info.get("bdinfo_for_disc")) if description_input_media_info.get("bdinfo_for_disc") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_SCREENSHOTS_READY": _shell_bool(description_input_screenshots.get("ready")) if description_input_screenshots.get("ready") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_INPUT_IMAGE_HOST_READY": _shell_bool(description_input_image_host.get("ready")) if description_input_image_host.get("ready") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_COMPLETE": _shell_bool(description_completeness.get("ready")) if description_completeness.get("ready") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_COMPLETENESS_MISSING": ",".join(_string_list(description_completeness.get("missing"))),
        "PTCLI_MATERIAL_DESCRIPTION_COMPLETENESS_RECOVERY_MISSING": ",".join(_string_list(description_completeness.get("recovery_missing"))),
        "PTCLI_MATERIAL_DESCRIPTION_COMPLETENESS_NEXT_ACTIONS": " | ".join(_string_list(description_completeness.get("next_actions"))),
        "PTCLI_MATERIAL_DESCRIPTION_COMPLETENESS_CHECKS": json.dumps(description_completeness.get("checks"), ensure_ascii=False) if isinstance(description_completeness.get("checks"), list) else None,
        "PTCLI_MATERIAL_DESCRIPTION_EVIDENCE": json.dumps(description_evidence, ensure_ascii=False) if description_evidence else None,
        "PTCLI_MATERIAL_DESCRIPTION_PATH": description.get("path"),
        "PTCLI_MATERIAL_DESCRIPTION_LENGTH": description.get("char_length"),
        "PTCLI_MATERIAL_DESCRIPTION_EXPECTED_LENGTH": description.get("expected_length"),
        "PTCLI_MATERIAL_DESCRIPTION_HAS_PTGEN": _shell_bool(description.get("has_ptgen_description")) if description.get("has_ptgen_description") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_PTGEN_LENGTH": description.get("ptgen_description_length"),
        "PTCLI_MATERIAL_DESCRIPTION_HAS_EXTERNAL_IDS": _shell_bool(description.get("has_external_ids")) if description.get("has_external_ids") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_EXTERNAL_ID_READINESS": json.dumps(description_external_id_readiness, ensure_ascii=False) if description_external_id_readiness else None,
        "PTCLI_MATERIAL_DESCRIPTION_EXTERNAL_ID_MISSING": ",".join(_string_list(description.get("external_id_missing"))),
        "PTCLI_MATERIAL_DESCRIPTION_HAS_IMDB": _shell_bool(description_external_id_readiness.get("imdb")) if "imdb" in description_external_id_readiness else None,
        "PTCLI_MATERIAL_DESCRIPTION_HAS_TMDB": _shell_bool(description_external_id_readiness.get("tmdb")) if "tmdb" in description_external_id_readiness else None,
        "PTCLI_MATERIAL_DESCRIPTION_HAS_DOUBAN": _shell_bool(description_external_id_readiness.get("douban")) if "douban" in description_external_id_readiness else None,
        "PTCLI_MATERIAL_DESCRIPTION_IMDB_LINK": description_links.get("imdb"),
        "PTCLI_MATERIAL_DESCRIPTION_TMDB_LINK": description_links.get("tmdb"),
        "PTCLI_MATERIAL_DESCRIPTION_DOUBAN_LINK": description_links.get("douban"),
        "PTCLI_MATERIAL_DESCRIPTION_HAS_MEDIAINFO_OR_BDINFO": _shell_bool(description.get("has_mediainfo_or_bdinfo")) if description.get("has_mediainfo_or_bdinfo") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_MEDIAINFO_SOURCE": media_info.get("source"),
        "PTCLI_MATERIAL_DESCRIPTION_MEDIAINFO_LENGTH": media_info.get("length"),
        "PTCLI_MATERIAL_DESCRIPTION_MEDIAINFO_HAS_EXCERPT": _shell_bool(media_info.get("has_excerpt")) if media_info.get("has_excerpt") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_HAS_SCREENSHOTS": _shell_bool(description.get("has_screenshot_bbcode")) if description.get("has_screenshot_bbcode") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_IMAGE_COUNT": description.get("bbcode_image_count"),
        "PTCLI_MATERIAL_DESCRIPTION_IMAGE_URLS": ",".join(_string_list(description.get("bbcode_image_urls"))),
        "PTCLI_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_READY": _shell_bool(screenshot_coverage.get("ready")) if screenshot_coverage.get("ready") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_EXPECTED_URLS": ",".join(_string_list(screenshot_coverage.get("expected_urls"))),
        "PTCLI_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_DESCRIPTION_URLS": ",".join(_string_list(screenshot_coverage.get("description_urls"))),
        "PTCLI_MATERIAL_DESCRIPTION_SCREENSHOT_COVERAGE_MISSING_URLS": ",".join(_string_list(screenshot_coverage.get("missing_urls"))),
        "PTCLI_MATERIAL_DESCRIPTION_METADATA_CHAIN_READY": _shell_bool(metadata_chain.get("ready")) if metadata_chain.get("ready") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_METADATA_CHAIN_MISSING": ",".join(_description_chain_recovery_missing("metadata_chain", metadata_chain)),
        "PTCLI_MATERIAL_DESCRIPTION_METADATA_CHAIN_NEXT_ACTIONS": " | ".join(_description_chain_next_actions("metadata_chain", metadata_chain)),
        "PTCLI_MATERIAL_DESCRIPTION_MEDIA_INFO_CHAIN_READY": _shell_bool(media_info_chain.get("ready")) if media_info_chain.get("ready") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_MEDIA_INFO_CHAIN_MISSING": ",".join(_description_chain_recovery_missing("media_info_chain", media_info_chain)),
        "PTCLI_MATERIAL_DESCRIPTION_MEDIA_INFO_CHAIN_NEXT_ACTIONS": " | ".join(_description_chain_next_actions("media_info_chain", media_info_chain)),
        "PTCLI_MATERIAL_DESCRIPTION_SCREENSHOT_CHAIN_READY": _shell_bool(screenshot_chain.get("ready")) if screenshot_chain.get("ready") is not None else None,
        "PTCLI_MATERIAL_DESCRIPTION_SCREENSHOT_CHAIN_MISSING": ",".join(_description_chain_recovery_missing("screenshot_chain", screenshot_chain)),
        "PTCLI_MATERIAL_DESCRIPTION_SCREENSHOT_CHAIN_NEXT_ACTIONS": " | ".join(_description_chain_next_actions("screenshot_chain", screenshot_chain)),
        "PTCLI_MATERIAL_DESCRIPTION_MISSING": ",".join(_string_list(description.get("missing"))),
    }


def _summary_material_section_shell_bool(section: dict[str, Any]) -> str | None:
    return _shell_bool(section.get("ok")) if "ok" in section and section.get("ok") is not None else None


def _material_evidence_items(evidence: Any) -> list[dict[str, Any]]:
    if isinstance(evidence, dict):
        return [evidence]
    if isinstance(evidence, list):
        return [item for item in evidence if isinstance(item, dict)]
    return []


def _material_evidence_all_files_exist(evidence: Any) -> bool | None:
    items = _material_evidence_items(evidence)
    if not items:
        return None
    return all(item.get("exists") is True and item.get("is_file") is True for item in items)


def _material_evidence_sha1s(evidence: Any) -> list[str]:
    return [str(item["sha1"]) for item in _material_evidence_items(evidence) if isinstance(item.get("sha1"), str) and item.get("sha1")]


def _material_evidence_first_sha1(evidence: Any) -> str | None:
    sha1s = _material_evidence_sha1s(evidence)
    return sha1s[0] if sha1s else None


def _summary_check_qbit_wait_shell_fields(qbit_wait_diagnostics: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for scope, prefix in (("source", "PTCLI_QBIT_WAIT_SOURCE"), ("uploaded", "PTCLI_QBIT_WAIT_UPLOADED")):
        diagnostics = qbit_wait_diagnostics.get(scope) if isinstance(qbit_wait_diagnostics.get(scope), dict) else {}
        fields.update(
            {
                f"{prefix}_COMPLETE": _shell_bool(diagnostics.get("complete")) if "complete" in diagnostics else None,
                f"{prefix}_REQUEST_MISMATCH": _shell_bool(diagnostics.get("request_mismatch")) if "request_mismatch" in diagnostics else None,
                f"{prefix}_REQUESTED_HASH": diagnostics.get("requested_hash"),
                f"{prefix}_REQUESTED_CONTENT_PATH": diagnostics.get("requested_content_path"),
                f"{prefix}_REQUESTED_SAVE_PATH": diagnostics.get("requested_save_path"),
                f"{prefix}_REQUESTED_TIMEOUT": diagnostics.get("requested_timeout"),
                f"{prefix}_REQUESTED_INTERVAL": diagnostics.get("requested_interval"),
                f"{prefix}_REQUESTED_HASH_MATCHED": _shell_bool(diagnostics.get("requested_hash_matched")) if diagnostics.get("requested_hash_matched") is not None else None,
                f"{prefix}_REQUESTED_CONTENT_PATH_MATCHED": _shell_bool(diagnostics.get("requested_content_path_matched")) if diagnostics.get("requested_content_path_matched") is not None else None,
                f"{prefix}_MATCHED_COUNT": diagnostics.get("matched_count"),
                f"{prefix}_COMPLETE_COUNT": diagnostics.get("complete_count"),
                f"{prefix}_ANY_COMPLETE": _shell_bool(diagnostics.get("any_complete")) if diagnostics.get("any_complete") is not None else None,
                f"{prefix}_OBSERVED_HASHES": _shell_join_list(diagnostics.get("observed_hashes")),
                f"{prefix}_OBSERVED_CONTENT_PATHS": _shell_join_list(diagnostics.get("observed_content_paths")),
                f"{prefix}_OBSERVED_SAVE_PATHS": _shell_join_list(diagnostics.get("observed_save_paths")),
                f"{prefix}_OBSERVED_STATES": _shell_join_list(diagnostics.get("observed_states")),
                f"{prefix}_OBSERVED_PROGRESS": _shell_join_list(diagnostics.get("observed_progress")),
                f"{prefix}_BLOCKERS": ",".join(_string_list(diagnostics.get("blockers"))),
            }
        )
    return fields


def _shell_join_list(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return ",".join(str(item) for item in value if isinstance(item, (str, int, float, bool)))


def _summary_check_qbit_retry_shell_fields(qbit_wait_retry_hints: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for scope, prefix in (("source", "PTCLI_QBIT_WAIT_SOURCE"), ("uploaded", "PTCLI_QBIT_WAIT_UPLOADED")):
        hint = qbit_wait_retry_hints.get(scope) if isinstance(qbit_wait_retry_hints.get(scope), dict) else {}
        first_candidate = _first_qbit_wait_candidate(hint)
        fields.update(
            {
                f"{prefix}_RETRY_RECOMMENDED": _shell_bool(hint.get("retry_recommended")) if "retry_recommended" in hint else None,
                f"{prefix}_SUGGESTED_HASH": hint.get("suggested_torrent_hash"),
                f"{prefix}_SUGGESTED_CONTENT_PATH": hint.get("suggested_content_path"),
                f"{prefix}_SUGGESTED_SAVE_PATH": hint.get("suggested_save_path"),
                f"{prefix}_OBSERVED_CANDIDATE_COUNT": hint.get("observed_candidate_count"),
                f"{prefix}_FIRST_CANDIDATE_HASH": first_candidate.get("hash"),
                f"{prefix}_FIRST_CANDIDATE_CONTENT_PATH": first_candidate.get("content_path"),
                f"{prefix}_FIRST_CANDIDATE_SAVE_PATH": first_candidate.get("save_path"),
                f"{prefix}_FIRST_CANDIDATE_STATE": first_candidate.get("state"),
                f"{prefix}_FIRST_CANDIDATE_PROGRESS": first_candidate.get("progress"),
                f"{prefix}_RETRY_REASON": hint.get("reason"),
                f"{prefix}_RETRY_ACTION": _qbit_wait_retry_action(scope, hint) if hint.get("retry_recommended") is True else None,
            }
        )
    return fields


def _first_qbit_wait_candidate(hint: dict[str, Any]) -> dict[str, Any]:
    candidates = hint.get("observed_candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return {}
    return candidates[0]


def _summary_check_run_next_command(payload: dict[str, Any]) -> int:
    command = payload.get("next_command")
    if not payload.get("should_execute_next_command") or not command:
        if command and payload.get("automation_action") == "unsupported_next_command":
            reason = payload.get("next_command_run_blocker") or "unsupported summary next_command"
            print(f"Refusing to run unsupported summary next_command: {command} ({reason})", file=sys.stderr)
            return 2
        return 0 if payload.get("status") == "ok" else 1
    argv = _summary_next_command_argv(payload.get("next_command_argv")) or _summary_next_command_argv(str(command))
    if argv is None:
        print(f"Refusing to run unsupported summary next_command: {command}", file=sys.stderr)
        return 2
    completed = subprocess.run(argv, check=False)  # noqa: S603 - argv is restricted to generated ptcli.py commands.
    return int(completed.returncode)


def _summary_check_run_first_runnable_command(payload: dict[str, Any]) -> int:
    command = payload.get("first_runnable_command")
    argv = _summary_next_command_argv(payload.get("first_runnable_command_argv")) if command else None
    if command and argv:
        completed = subprocess.run(argv, check=False)  # noqa: S603 - argv is restricted to allowlisted generated ptcli.py commands.
        return int(completed.returncode)
    return 0 if payload.get("status") == "ok" else 1


def _summary_next_command_metadata(argv: list[str] | None) -> dict[str, Any]:
    if argv is None:
        return {"subcommand": None, "run_allowed": False, "run_blocker": "next command is missing or unparsable", "placeholder": False}
    if len(argv) < 3:
        return {"subcommand": None, "run_allowed": False, "run_blocker": "next command must include python, ptcli.py, and a subcommand", "placeholder": _argv_has_placeholder(argv)}
    subcommand = argv[2]
    if _argv_has_placeholder(argv):
        return {"subcommand": subcommand, "run_allowed": False, "run_blocker": "next command contains placeholders", "placeholder": True}
    interpreter = Path(argv[0]).name
    script = Path(argv[1]).name
    if interpreter not in {"python", "python3"} or script != "ptcli.py":
        return {"subcommand": subcommand, "run_allowed": False, "run_blocker": "next command is not a generated ptcli.py invocation", "placeholder": False}
    if subcommand not in SUMMARY_CHECK_RUN_COMMANDS:
        return {"subcommand": subcommand, "run_allowed": False, "run_blocker": f"ptcli subcommand {subcommand} is not in the summary-check auto-run allowlist", "placeholder": False}
    return {"subcommand": subcommand, "run_allowed": True, "run_blocker": None, "placeholder": False}


def _summary_next_command_argv(command: Any) -> list[str] | None:
    argv = _summary_next_command_raw_argv(command)
    if argv is None:
        return None
    if len(argv) < 3:
        return None
    if _argv_has_placeholder(argv):
        return None
    interpreter = Path(argv[0]).name
    script = Path(argv[1]).name
    if interpreter not in {"python", "python3"} or script != "ptcli.py":
        return None
    if argv[2] not in SUMMARY_CHECK_RUN_COMMANDS:
        return None
    argv[0] = sys.executable
    argv[1] = str(_ptcli_script_path())
    return argv


def _argv_has_placeholder(argv: list[str] | None) -> bool:
    return any("<" in arg or ">" in arg for arg in argv or [])


def _summary_next_command_raw_argv(command: Any) -> list[str] | None:
    if isinstance(command, list):
        return _argv_list(command)
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return None
    return None


def _ptcli_script_path() -> Path:
    return Path(__file__).resolve().parents[2] / "ptcli.py"


def _shell_bool(value: Any) -> str:
    return "1" if bool(value) else "0"


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
        return _wait_result_completed(uploaded_wait)
    return True


def _pipeline_exit_code(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    if not _pipeline_has_action(args):
        return 0
    if getattr(args, "target_execute", False) and _closure_audit_blockers(payload.get("closure_audit")):
        return 1
    if getattr(args, "target_execute", False) and payload.get("complete") is not True:
        return 1
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
