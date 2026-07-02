"""Daily retorrent candidate discovery for ptcli service APIs."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from src.ptcli.mainland import normalize_tracker, parse_tracker_list, unsupported_trackers
from src.ptcli.rules import build_rule_check
from src.ptcli.source import (
    GENERIC_DETAILS_BASE_URLS,
    MTEAM_API_TRACKERS,
    extract_torrent_id,
    fetch_source_info,
    source_download_adapter,
    source_info_adapter,
)
from src.ptcli.source import _load_cookie_file as load_cookie_file
from src.ptcli.target import search_mteam_duplicates

DEFAULT_CANDIDATE_LIMIT = 10
MAX_CANDIDATE_SCAN = 50
RECENT_PATHS: dict[str, str] = {
    "HDS": "/index.php?page=torrents",
    "TTG": "/browse.php",
}


@dataclass(frozen=True)
class CandidateSeed:
    tracker: str
    torrent_id: str
    title: str | None
    details_url: str | None
    size: str | None = None
    published_at: str | None = None
    promotion: str | None = None
    seeders: int | None = None
    leechers: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def build_daily_candidates(
    config: dict[str, Any],
    source_tracker: str,
    target_trackers_raw: str,
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    base_dir: str | None = None,
    accept_rules: bool = False,
    check_dupes: bool = True,
) -> dict[str, Any]:
    source = normalize_tracker(source_tracker)
    targets = parse_tracker_list(target_trackers_raw)
    limit = max(1, min(int(limit or DEFAULT_CANDIDATE_LIMIT), DEFAULT_CANDIDATE_LIMIT))
    invalid = unsupported_trackers([source, *targets])
    if invalid:
        return _blocked_payload(source, targets, limit, [f"Unsupported tracker(s) for focused candidate scope: {', '.join(invalid)}"])
    if source in targets:
        return _blocked_payload(source, targets, limit, ["Source tracker cannot also be a target tracker."])

    rule_check = build_rule_check(source, targets, accept_rules=accept_rules)
    source_capability = _source_candidate_capability(source)
    candidates: list[dict[str, Any]] = []
    blockers: list[str] = []
    next_actions: list[str] = []
    try:
        seeds = await fetch_recent_candidate_seeds(source, base_dir=base_dir, limit=MAX_CANDIDATE_SCAN)
    except Exception as exc:
        seeds = []
        blockers.append(str(exc))
        next_actions.extend(_source_fetch_next_actions(source, base_dir))

    for seed in seeds:
        if len(candidates) >= limit:
            break
        candidates.append(await _candidate_from_seed(config, seed, targets, check_dupes=check_dupes, base_dir=base_dir, accept_rules=accept_rules))

    ready_count = sum(1 for candidate in candidates if candidate.get("status") == "ready")
    partial = bool(blockers or len(candidates) < limit)
    status = "ok" if candidates and not partial else "blocked" if not candidates else "partial"
    if not candidates and not blockers:
        blockers.append("No source candidates were found.")
    payload_blockers = list(blockers)
    if not rule_check.get("ready"):
        payload_blockers.extend(f"rule-check: {blocker}" for blocker in _rule_blockers(rule_check))
    return {
        "kind": "ptcli.daily_candidates",
        "status": status,
        "ok": bool(candidates),
        "source_tracker": source,
        "target_trackers": targets,
        "limit": limit,
        "count": len(candidates),
        "ready_count": ready_count,
        "source_capability": source_capability,
        "rule_check": rule_check,
        "candidates": candidates,
        "blockers": payload_blockers,
        "next_actions": next_actions,
    }


async def fetch_recent_candidate_seeds(tracker: str, *, base_dir: str | None = None, limit: int = MAX_CANDIDATE_SCAN) -> list[CandidateSeed]:
    source = normalize_tracker(tracker)
    if source in MTEAM_API_TRACKERS:
        raise ValueError("MTEAM candidate discovery is not enabled yet; use MTEAM as a target first.")
    if source not in GENERIC_DETAILS_BASE_URLS:
        raise ValueError(f"Candidate discovery is not enabled for tracker: {source}")
    cookiefile = _cookie_path(source, base_dir)
    cookie_exists = await asyncio.to_thread(os.path.exists, cookiefile)
    if not cookie_exists:
        raise ValueError(f"{source} cookie file is required for candidate discovery: {cookiefile}")
    cookies = await load_cookie_file(cookiefile)
    url = _recent_url(source)
    async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
        response = await client.get(url)
    response.raise_for_status()
    return parse_recent_candidate_seeds(source, response.text, base_url=GENERIC_DETAILS_BASE_URLS[source], limit=limit)


def parse_recent_candidate_seeds(tracker: str, html: str, *, base_url: str, limit: int = MAX_CANDIDATE_SCAN) -> list[CandidateSeed]:
    source = normalize_tracker(tracker)
    soup = BeautifulSoup(html, "lxml")
    seeds: list[CandidateSeed] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        if not _looks_like_details_link(href):
            continue
        try:
            torrent_id = extract_torrent_id(href)
        except ValueError:
            continue
        if torrent_id in seen:
            continue
        seen.add(torrent_id)
        row = link.find_parent("tr")
        row_text = " ".join(row.get_text(" ", strip=True).split()) if row else ""
        seed = CandidateSeed(
            tracker=source,
            torrent_id=torrent_id,
            title=_candidate_title(link, row_text),
            details_url=urljoin(base_url, href),
            size=_extract_size(row_text),
            published_at=_extract_published_at(row_text),
            promotion=_extract_promotion(row_text),
            seeders=_extract_named_int(row_text, ("seeders", "做种", "種子")),
            leechers=_extract_named_int(row_text, ("leechers", "下载", "下載")),
        )
        seeds.append(seed)
        if len(seeds) >= limit:
            break
    return seeds


async def _candidate_from_seed(config: dict[str, Any], seed: CandidateSeed, targets: list[str], *, check_dupes: bool, base_dir: str | None, accept_rules: bool) -> dict[str, Any]:
    source_info_payload: dict[str, Any] | None = None
    source_info_error: str | None = None
    try:
        source_info = await fetch_source_info(config, seed.tracker, seed.torrent_id, base_dir=base_dir)
        source_info_payload = source_info.to_dict()
    except Exception as exc:
        source_info_error = str(exc)
    duplicate_check = await _candidate_duplicate_check(config, source_info_payload, targets, check_dupes=check_dupes)
    blockers = _candidate_blockers(seed, source_info_payload, source_info_error, duplicate_check)
    execute_request = _candidate_execute_request(seed, targets, accept_rules=accept_rules)
    status = "ready" if not blockers else "blocked"
    return {
        "status": status,
        "source": seed.to_dict(),
        "source_info": source_info_payload,
        "source_info_error": source_info_error,
        "duplicate_check": duplicate_check,
        "recommendation": _candidate_recommendation(seed, source_info_payload, duplicate_check, blockers),
        "blockers": blockers,
        "risk_flags": blockers,
        "execute_request": execute_request,
        "execute_job_endpoint": "/v1/jobs/retorrent",
    }


async def _candidate_duplicate_check(config: dict[str, Any], source_info: dict[str, Any] | None, targets: list[str], *, check_dupes: bool) -> dict[str, Any]:
    if not check_dupes:
        return {"searched": False, "status": "skipped", "exists": None, "count": None, "dupes": [], "reason": "duplicate check disabled"}
    if targets != ["MTEAM"]:
        return {"searched": False, "status": "unsupported", "exists": None, "count": None, "dupes": [], "reason": "candidate duplicate check currently supports MTEAM target only"}
    result = await search_mteam_duplicates(config, source_info)
    count = int(result.get("count", 0) or 0) if isinstance(result, dict) else 0
    searched = bool(result.get("searched")) if isinstance(result, dict) else False
    return {
        **(result if isinstance(result, dict) else {}),
        "status": "exists" if searched and count > 0 else "not_found" if searched else "unknown",
        "exists": count > 0 if searched else None,
    }


def _candidate_blockers(seed: CandidateSeed, source_info: dict[str, Any] | None, source_info_error: str | None, duplicate_check: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if not source_download_adapter(seed.tracker):
        blockers.append(f"{seed.tracker} source download adapter is not enabled.")
    if source_info_error:
        blockers.append(f"source-info: {source_info_error}")
    if not source_info:
        blockers.append("source-info: metadata is unavailable.")
    elif not any(source_info.get(key) for key in ("imdb_id", "tmdb_id", "douban_id", "douban_url", "name")):
        blockers.append("source-info: no usable IMDb/TMDb/Douban/name signal.")
    if duplicate_check.get("exists") is True:
        blockers.append("target-duplicate: target tracker already has possible existing torrents.")
    if duplicate_check.get("searched") is False:
        blockers.append(f"target-duplicate: {duplicate_check.get('reason') or 'duplicate search did not run.'}")
    return blockers


def _candidate_execute_request(seed: CandidateSeed, targets: list[str], *, accept_rules: bool) -> dict[str, Any]:
    return {
        "source": seed.details_url or seed.torrent_id,
        "source_tracker": seed.tracker,
        "target": ",".join(targets),
        "execute": True,
        "accept_rules": bool(accept_rules),
        "confirm_upload": False,
    }


def _candidate_recommendation(seed: CandidateSeed, source_info: dict[str, Any] | None, duplicate_check: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    reasons: list[str] = []
    if seed.promotion:
        reasons.append(f"source promotion detected: {seed.promotion}")
    if source_info and source_info.get("imdb_id"):
        reasons.append("IMDb metadata is available for target duplicate check.")
    if duplicate_check.get("status") == "not_found":
        reasons.append("MTEAM duplicate search found no existing torrent.")
    if not reasons:
        reasons.append("Candidate is from the source tracker recent listing.")
    return {
        "recommended": not blockers,
        "reason": "; ".join(reasons),
        "requires": ["accept_rules", "confirm_upload", "save_path or path"],
    }


def _blocked_payload(source: str, targets: list[str], limit: int, blockers: list[str]) -> dict[str, Any]:
    return {
        "kind": "ptcli.daily_candidates",
        "status": "blocked",
        "ok": False,
        "source_tracker": source,
        "target_trackers": targets,
        "limit": limit,
        "count": 0,
        "ready_count": 0,
        "candidates": [],
        "blockers": blockers,
        "next_actions": [],
    }


def _source_candidate_capability(source: str) -> dict[str, Any]:
    return {
        "source_tracker": source,
        "source_info_adapter": source_info_adapter(source),
        "source_download_adapter": source_download_adapter(source),
        "candidate_discovery_adapter": "generic_recent_cookie" if source in GENERIC_DETAILS_BASE_URLS and source not in MTEAM_API_TRACKERS else None,
    }


def _source_fetch_next_actions(source: str, base_dir: str | None) -> list[str]:
    return [f"Create or refresh data/cookies/{source}.txt under {os.path.abspath(base_dir or os.getcwd())}, then rerun daily candidates."]


def _rule_blockers(rule_check: dict[str, Any]) -> list[str]:
    blockers = rule_check.get("blockers")
    if isinstance(blockers, list):
        return [str(item) for item in blockers]
    checks = rule_check.get("checks")
    if not isinstance(checks, list):
        return []
    return [str(check.get("message") or check.get("name")) for check in checks if isinstance(check, dict) and not check.get("ok")]


def _cookie_path(tracker: str, base_dir: str | None) -> str:
    root = os.path.abspath(base_dir or os.getcwd())
    return os.path.join(root, "data", "cookies", f"{tracker}.txt")


def _recent_url(tracker: str) -> str:
    base_url = GENERIC_DETAILS_BASE_URLS[tracker]
    return urljoin(base_url, RECENT_PATHS.get(tracker, "/torrents.php?incldead=0"))


def _looks_like_details_link(href: str) -> bool:
    return bool(re.search(r"(?:details\.php|torrent-details|/details/|/detail/|/torrent/).*(?:[?&]id=|/\d+)", href, flags=re.IGNORECASE))


def _candidate_title(link: Any, row_text: str) -> str | None:
    for value in (link.get("title"), link.get_text(" ", strip=True), row_text):
        title = " ".join(str(value or "").split())
        if title:
            return title[:240]
    return None


def _extract_size(text: str) -> str | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(TiB|GiB|MiB|TB|GB|MB)\b", text, flags=re.IGNORECASE)
    return f"{match.group(1)} {match.group(2)}" if match else None


def _extract_published_at(text: str) -> str | None:
    match = re.search(r"\b(20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2})?)\b", text)
    return match.group(1) if match else None


def _extract_promotion(text: str) -> str | None:
    promotion_tokens = ("free", "2x", "50%", "30%", "免費", "免费", "限免", "促销", "促銷")
    lower = text.lower()
    matches = [token for token in promotion_tokens if token.lower() in lower]
    return ",".join(matches) if matches else None


def _extract_named_int(text: str, labels: tuple[str, ...]) -> int | None:
    for label in labels:
        match = re.search(rf"{re.escape(label)}[^\d]{{0,8}}(\d+)", text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None
