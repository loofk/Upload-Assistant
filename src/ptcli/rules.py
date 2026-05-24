"""Rule profile metadata for Chinese-language PT trackers.

This module deliberately avoids inventing tracker rules. It records where rule
review must happen and whether automation is enabled for the focused CLI.
Tracker adapters remain responsible for concrete upload/download validation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final

from src.ptcli.mainland import CHINESE_PT_TRACKERS


@dataclass(frozen=True)
class RuleProfile:
    tracker: str
    rules_url: str
    review_required: bool
    automation_status: str
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["notes"] = list(self.notes)
        return payload


_RULE_URLS: Final[dict[str, str]] = {
    "AUDIENCES": "https://audiences.me/rules.php",
    "CHD": "https://ptchdbits.co/rules.php",
    "HDS": "https://hd-space.org/index.php?page=rules",
    "HDSKY": "https://hdsky.me/rules.php",
    "HHAN": "https://hhanclub.net/rules.php",
    "MTEAM": "https://kp.m-team.cc/rules",
    "OB": "https://ourbits.club/rules.php",
    "PTER": "https://pterclub.com/rules.php",
    "TJUPT": "https://www.tjupt.org/rules.php",
    "TTG": "https://totheglory.im/rules.php",
    "U2": "https://u2.dmhy.org/rules.php",
}

_DEFAULT_NOTES: Final[tuple[str, ...]] = (
    "Do not bypass source or target tracker restrictions.",
    "Do not upload if the source forbids reposting or the target category is not allowed.",
    "Let the tracker adapter perform concrete field/category/description validation before upload.",
)


def get_rule_profile(tracker: str) -> RuleProfile:
    if tracker not in CHINESE_PT_TRACKERS:
        raise ValueError(f"Unsupported tracker for Chinese PT CLI scope: {tracker}")

    return RuleProfile(
        tracker=tracker,
        rules_url=_RULE_URLS.get(tracker, ""),
        review_required=True,
        automation_status="planning",
        notes=_DEFAULT_NOTES,
    )


def get_rule_profiles(trackers: list[str]) -> list[RuleProfile]:
    return [get_rule_profile(tracker) for tracker in trackers]


def rule_profiles_to_dicts(profiles: list[RuleProfile]) -> list[dict[str, Any]]:
    return [profile.to_dict() for profile in profiles]

