"""Tracker scope for the focused Chinese-language PT CLI.

The allowlist is intentionally explicit. Adding a tracker here means the new
CLI is allowed to plan retorrent work for it, but the tracker module still owns
the actual upload/download implementation and site-specific validation.
"""

from __future__ import annotations

from typing import Final

CHINESE_PT_TRACKERS: Final[frozenset[str]] = frozenset(
    {
        "AUDIENCES",
        "CHD",
        "HDS",
        "HDSKY",
        "HHAN",
        "MTEAM",
        "OB",
        "PTER",
        "TJUPT",
        "TTG",
        "U2",
    }
)

MAINLAND_PT_TRACKERS: Final[frozenset[str]] = CHINESE_PT_TRACKERS

TRACKER_ALIASES: Final[dict[str, str]] = {
    "AUDIENCE": "AUDIENCES",
    "AUDIENCES": "AUDIENCES",
    "CHD": "CHD",
    "HDS": "HDS",
    "HDSKY": "HDSKY",
    "HHAN": "HHAN",
    "MTEAM": "MTEAM",
    "M-TEAM": "MTEAM",
    "OB": "OB",
    "PTER": "PTER",
    "PTERCLUB": "PTER",
    "TJUPT": "TJUPT",
    "TTG": "TTG",
    "U2": "U2",
}


def normalize_tracker(value: str) -> str:
    """Return the canonical tracker code for user input."""
    normalized = value.strip().upper().replace("_", "-")
    return TRACKER_ALIASES.get(normalized, normalized)


def parse_tracker_list(value: str) -> list[str]:
    """Parse comma-separated tracker names into canonical, de-duplicated codes."""
    trackers: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        tracker = normalize_tracker(raw)
        if not tracker or tracker in seen:
            continue
        trackers.append(tracker)
        seen.add(tracker)
    return trackers


def unsupported_trackers(trackers: list[str]) -> list[str]:
    """Return trackers outside the focused CLI scope."""
    return [tracker for tracker in trackers if tracker not in CHINESE_PT_TRACKERS]
