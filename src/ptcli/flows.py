"""Reference retorrent flow metadata for ptcli."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final


@dataclass(frozen=True)
class FlowProfile:
    source_tracker: str
    target_tracker: str
    source_kind: str
    target_kind: str
    status: str
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["notes"] = list(self.notes)
        return payload


NEXUSPHP_MTEAM_SOURCE_TRACKERS: Final[frozenset[str]] = frozenset(
    {
        "AUDIENCES",
        "CHD",
        "HDSKY",
        "HHAN",
        "PTER",
        "TJUPT",
        "U2",
    }
)

REFERENCE_FLOW_PROFILES: Final[dict[tuple[str, str], FlowProfile]] = {
    ("U2", "MTEAM"): FlowProfile(
        source_tracker="U2",
        target_tracker="MTEAM",
        source_kind="nexusphp",
        target_kind="mteam_api",
        status="reference",
        notes=("Uses U2 details/download.php with cookies/passkey as source.", "Uses MTEAM API as target."),
    ),
    ("CHD", "MTEAM"): FlowProfile(
        source_tracker="CHD",
        target_tracker="MTEAM",
        source_kind="nexusphp",
        target_kind="mteam_api",
        status="reference",
        notes=("Uses CHD details/download.php with cookies/passkey as source.", "Uses MTEAM API as target."),
    ),
}


def _generic_nexusphp_mteam_profile(source_tracker: str, target_tracker: str) -> FlowProfile | None:
    if target_tracker != "MTEAM" or source_tracker not in NEXUSPHP_MTEAM_SOURCE_TRACKERS:
        return None
    return FlowProfile(
        source_tracker=source_tracker,
        target_tracker=target_tracker,
        source_kind="nexusphp",
        target_kind="mteam_api",
        status="source-enabled",
        notes=(
            f"Uses {source_tracker} details/download.php with cookies/passkey as source.",
            "Uses MTEAM API as target.",
            "Site-specific source and target rules still require explicit review before execution.",
        ),
    )


def get_flow_profiles(source_tracker: str, target_trackers: list[str]) -> list[FlowProfile]:
    return [
        profile
        for target_tracker in target_trackers
        if (profile := REFERENCE_FLOW_PROFILES.get((source_tracker, target_tracker)) or _generic_nexusphp_mteam_profile(source_tracker, target_tracker)) is not None
    ]


def flow_profiles_to_dicts(profiles: list[FlowProfile]) -> list[dict[str, Any]]:
    return [profile.to_dict() for profile in profiles]
