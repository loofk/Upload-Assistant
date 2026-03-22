"""Live integration tests — verify tracker metadata extraction against real sites.

These tests require:
  - data/config.py with valid tracker credentials
  - data/cookies/<TRACKER>.txt for cookie-based trackers
  - Network access to tracker sites

Run with: make test-live  (or: pytest -m live -v)
"""

import json
import os
from typing import Any

import pytest

# Project root (two levels up from this file)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES_PATH = os.path.join(PROJECT_ROOT, "tests", "fixtures", "known_torrents.json")


def _load_known_torrents() -> list[dict[str, Any]]:
    with open(FIXTURES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _make_ids() -> list[str]:
    """Generate pytest parametrize IDs like 'u2-60635'."""
    return [f"{t['source']}-{t['torrent_id']}" for t in _load_known_torrents()]


KNOWN_TORRENTS = _load_known_torrents()


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize("torrent_case", KNOWN_TORRENTS, ids=_make_ids())
async def test_tracker_metadata_extraction(
    torrent_case: dict[str, Any],
    real_config: dict[str, Any],
    real_meta: dict[str, Any],
) -> None:
    """Given a known torrent ID, verify that get_info_from_torrent_id extracts
    the correct IMDb / Douban IDs from the tracker page.
    """
    source = torrent_case["source"].upper()
    torrent_id = torrent_case["torrent_id"]
    expected = torrent_case["expected"]

    # Check cookie file exists for cookie-based trackers
    cookie_path = os.path.join(PROJECT_ROOT, "data", "cookies", f"{source}.txt")
    if not os.path.exists(cookie_path):
        pytest.skip(f"Cookie file not found: {cookie_path}")

    # Dynamically import the tracker module
    tracker_instance = _get_tracker_instance(source, real_config)
    assert tracker_instance is not None, f"Unsupported tracker: {source}"

    # Call get_info_from_torrent_id
    result = await tracker_instance.get_info_from_torrent_id(torrent_id, meta=real_meta)

    # Unpack result — U2 returns (imdb, tmdb, name, hash, description)
    imdb_id = result[0]
    tmdb_id = result[1] if len(result) > 1 else None

    # Verify IMDb ID
    if "imdb_id" in expected:
        assert imdb_id == expected["imdb_id"], (
            f"[{source}#{torrent_id}] IMDb mismatch: got {imdb_id}, expected {expected['imdb_id']}"
        )

    # Verify TMDb ID
    if "tmdb_id" in expected:
        assert tmdb_id == expected["tmdb_id"], (
            f"[{source}#{torrent_id}] TMDb mismatch: got {tmdb_id}, expected {expected['tmdb_id']}"
        )

    # Verify Douban ID (set via meta side-effect)
    if "douban_id" in expected:
        actual_douban = real_meta.get("douban_id")
        assert str(actual_douban) == str(expected["douban_id"]), (
            f"[{source}#{torrent_id}] Douban mismatch: got {actual_douban}, expected {expected['douban_id']}"
        )


def _get_tracker_instance(source: str, config: dict[str, Any]) -> Any:
    """Create a tracker class instance by source abbreviation."""
    tracker_map: dict[str, tuple[str, str]] = {
        "U2": ("src.trackers.U2", "U2"),
        "MTEAM": ("src.trackers.MTEAM", "MTEAM"),
        "CHD": ("src.trackers.CHD", "CHD"),
        "TJUPT": ("src.trackers.TJUPT", "TJUPT"),
        "HDSKY": ("src.trackers.HDSKY", "HDSKY"),
        "PTER": ("src.trackers.PTER", "PTER"),
        "AUDIENCES": ("src.trackers.AUDIENCES", "AUDIENCES"),
        "HHAN": ("src.trackers.HHAN", "HHAN"),
        "HDB": ("src.trackers.HDB", "HDB"),
    }

    if source not in tracker_map:
        return None

    module_path, class_name = tracker_map[source]
    import importlib

    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls(config=config)
