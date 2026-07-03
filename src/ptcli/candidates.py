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
from src.ptcli.policies import build_site_policy, build_site_policy_coverage, build_site_policy_report, qbit_limits_for_tracker
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
SOURCE_URL_RETORRENT_JOB_ENDPOINT = "/v1/jobs/retorrent/from-url"
SOURCE_URL_RETORRENT_JOB_TOOL = "source_url_retorrent_job"
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
    site_policy = build_site_policy_report(config, [source, *targets], accept_rules=accept_rules)
    source_capability = _source_candidate_capability(source)
    scored_candidates: list[dict[str, Any]] = []
    blockers: list[str] = []
    next_actions: list[str] = []
    try:
        seeds = await fetch_recent_candidate_seeds(source, base_dir=base_dir, limit=MAX_CANDIDATE_SCAN)
    except Exception as exc:
        seeds = []
        blockers.append(str(exc))
        next_actions.extend(_source_fetch_next_actions(source, base_dir))

    for index, seed in enumerate(seeds):
        candidate = await _candidate_from_seed(config, seed, targets, check_dupes=check_dupes, base_dir=base_dir, accept_rules=accept_rules)
        candidate["_source_order"] = index
        scored_candidates.append(candidate)

    scored_candidates.sort(key=_candidate_sort_key, reverse=True)
    candidates = scored_candidates[:limit]
    for candidate in candidates:
        candidate.pop("_source_order", None)
    ready_count = sum(1 for candidate in candidates if candidate.get("status") == "ready")
    partial = bool(blockers or len(candidates) < limit)
    status = "ok" if candidates and not partial else "blocked" if not candidates else "partial"
    if not candidates and not blockers:
        blockers.append("No source candidates were found.")
    payload_blockers = list(blockers)
    if not rule_check.get("ready"):
        payload_blockers.extend(f"rule-check: {blocker}" for blocker in _rule_blockers(rule_check))
    if not site_policy.get("ready"):
        payload_blockers.extend(f"site-policy: {blocker}" for blocker in _string_list(site_policy.get("blockers")))
    digest = _candidate_digest(candidates, payload_blockers, next_actions, limit=limit)
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
        "site_policy": site_policy,
        "ranking": {
            "strategy": "ready-first, then descending score, then source listing order",
            "score_range": "0-100",
            "scan_count": len(seeds),
            "selected_count": len(candidates),
        },
        "digest": digest,
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
    source_policy = build_site_policy(config, seed.tracker).to_dict()
    target_policies = [build_site_policy(config, target).to_dict() for target in targets]
    blockers = _candidate_blockers(seed, source_info_payload, source_info_error, duplicate_check, source_policy, target_policies, accept_rules=accept_rules)
    execute_request = _candidate_execute_request(config, seed, targets, accept_rules=accept_rules)
    policy_summary = _candidate_policy_summary(source_policy, target_policies, execute_request, accept_rules=accept_rules)
    status = "ready" if not blockers else "blocked"
    ranking = _candidate_ranking(seed, source_info_payload, duplicate_check, blockers)
    return {
        "status": status,
        "source": seed.to_dict(),
        "source_info": source_info_payload,
        "source_info_error": source_info_error,
        "duplicate_check": duplicate_check,
        "source_policy": source_policy,
        "target_policies": target_policies,
        "policy_summary": policy_summary,
        "policy_coverage": policy_summary.get("policy_coverage"),
        "ranking": ranking,
        "recommendation": _candidate_recommendation(seed, source_info_payload, duplicate_check, blockers, ranking),
        "blockers": blockers,
        "risk_flags": blockers,
        "agent_workflow": _candidate_agent_workflow(status, blockers),
        "submit_request": execute_request,
        "submit_job_endpoint": SOURCE_URL_RETORRENT_JOB_ENDPOINT,
        "submit_tool": SOURCE_URL_RETORRENT_JOB_TOOL,
        "execute_request": execute_request,
        "execute_job_endpoint": SOURCE_URL_RETORRENT_JOB_ENDPOINT,
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


def _candidate_blockers(
    seed: CandidateSeed,
    source_info: dict[str, Any] | None,
    source_info_error: str | None,
    duplicate_check: dict[str, Any],
    source_policy: dict[str, Any],
    target_policies: list[dict[str, Any]],
    *,
    accept_rules: bool,
) -> list[str]:
    blockers: list[str] = []
    if (source_policy.get("manual_review_required") is True or any(policy.get("manual_review_required") is True for policy in target_policies)) and not accept_rules:
        blockers.append("site-policy: manual source/target rule review must be acknowledged before execution.")
    if source_policy.get("allow_auto_download") is not True:
        blockers.append(f"{seed.tracker} policy: automatic source download is not enabled.")
    if source_policy.get("allow_retorrent") is not True:
        blockers.append(f"{seed.tracker} policy: retorrent automation is not enabled.")
    for target_policy in target_policies:
        target = str(target_policy.get("tracker") or "target")
        if target_policy.get("allow_auto_upload") is not True:
            blockers.append(f"{target} policy: automatic target upload is not enabled.")
        if target_policy.get("allow_retorrent") is not True:
            blockers.append(f"{target} policy: retorrent automation is not enabled.")
        if target_policy.get("freeleech_required") is True and not _promotion_is_free(seed.promotion):
            blockers.append(f"{target} policy: freeleech source candidate is required.")
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


def _candidate_execute_request(config: dict[str, Any], seed: CandidateSeed, targets: list[str], *, accept_rules: bool) -> dict[str, Any]:
    source_reference = seed.details_url or seed.torrent_id
    request = {
        "source": source_reference,
        "source_url": source_reference,
        "source_tracker": seed.tracker,
        "target": ",".join(targets),
        "execute_if_no_duplicate": True,
        "accept_rules": bool(accept_rules),
        "confirm_upload": False,
    }
    source_limits = qbit_limits_for_tracker(config, seed.tracker, role="source")
    if source_limits.get("upload_limit") is not None:
        request["qbit_upload_limit"] = source_limits["upload_limit"]
    if source_limits.get("download_limit") is not None:
        request["qbit_download_limit"] = source_limits["download_limit"]
    if targets:
        target_limits = qbit_limits_for_tracker(config, targets[0], role="target")
        if target_limits.get("upload_limit") is not None:
            request["uploaded_qbit_upload_limit"] = target_limits["upload_limit"]
        if target_limits.get("download_limit") is not None:
            request["uploaded_qbit_download_limit"] = target_limits["download_limit"]
    return request


def _candidate_policy_summary(source_policy: dict[str, Any], target_policies: list[dict[str, Any]], execute_request: dict[str, Any], *, accept_rules: bool) -> dict[str, Any]:
    policies = [source_policy, *target_policies]
    source_coverage = build_site_policy_coverage(source_policy, roles=["source"])
    target_coverages = [build_site_policy_coverage(policy, roles=["target"]) for policy in target_policies]
    return {
        "accept_rules": bool(accept_rules),
        "manual_review_ready": bool(accept_rules) or not any(policy.get("manual_review_required") is True for policy in policies),
        "automation": {
            "source_download": source_policy.get("allow_auto_download"),
            "source_retorrent": source_policy.get("allow_retorrent"),
            "target_upload": all(policy.get("allow_auto_upload") is True for policy in target_policies),
            "target_retorrent": all(policy.get("allow_retorrent") is True for policy in target_policies),
        },
        "qbit_limits": {
            "source": {
                "upload_limit": execute_request.get("qbit_upload_limit"),
                "download_limit": execute_request.get("qbit_download_limit"),
                "policy_upload_limit_human": source_policy.get("upload_rate_limit_human"),
                "policy_download_limit_human": source_policy.get("download_rate_limit_human"),
            },
            "target": {
                "upload_limit": execute_request.get("uploaded_qbit_upload_limit"),
                "download_limit": execute_request.get("uploaded_qbit_download_limit"),
                "policy_upload_limit_human": _first_policy_value(target_policies, "upload_rate_limit_human"),
                "policy_download_limit_human": _first_policy_value(target_policies, "download_rate_limit_human"),
            },
        },
        "seeding_requirements": {
            "source": _policy_seeding_requirements(source_policy),
            "targets": [_policy_seeding_requirements(policy) for policy in target_policies],
        },
        "rules": {
            "source_rules_url": source_policy.get("rules_url"),
            "target_rules_urls": [policy.get("rules_url") for policy in target_policies if policy.get("rules_url")],
            "source_fingerprint": source_policy.get("rule_review_fingerprint"),
            "target_fingerprints": [policy.get("rule_review_fingerprint") for policy in target_policies if policy.get("rule_review_fingerprint")],
        },
        "policy_coverage": _candidate_policy_coverage_summary(source_coverage, target_coverages),
    }


def _candidate_policy_coverage_summary(source_coverage: dict[str, Any], target_coverages: list[dict[str, Any]]) -> dict[str, Any]:
    coverages = [source_coverage, *target_coverages]
    missing = {
        tracker_key: fields
        for tracker_key, fields in [
            ("source", _string_list(source_coverage.get("missing_fields"))),
            *[(f"target:{coverage.get('tracker') or index}", _string_list(coverage.get("missing_fields"))) for index, coverage in enumerate(target_coverages)],
        ]
        if fields
    }
    disabled = {
        tracker_key: fields
        for tracker_key, fields in [
            ("source", _string_list(source_coverage.get("disabled_automation"))),
            *[(f"target:{coverage.get('tracker') or index}", _string_list(coverage.get("disabled_automation"))) for index, coverage in enumerate(target_coverages)],
        ]
        if fields
    }
    return {
        "ready": all(bool(coverage.get("complete")) for coverage in coverages),
        "source": source_coverage,
        "targets": target_coverages,
        "missing_policy_fields": missing,
        "disabled_automation": disabled,
        "recommendations": [recommendation for coverage in coverages for recommendation in _string_list(coverage.get("recommendations"))],
    }


def _policy_seeding_requirements(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "tracker": policy.get("tracker"),
        "min_seed_time_hours": policy.get("min_seed_time_hours"),
        "min_ratio": policy.get("min_ratio"),
        "freeleech_required": policy.get("freeleech_required"),
    }


def _first_policy_value(policies: list[dict[str, Any]], key: str) -> Any:
    for policy in policies:
        value = policy.get(key)
        if value is not None:
            return value
    return None


def _candidate_agent_workflow(status: str, blockers: list[str]) -> dict[str, Any]:
    if status == "ready":
        decision = "submit_when_confirmed"
        recommended_action = f"Review site rules, set confirm_upload=true with a save_path or path, then submit submit_request to {SOURCE_URL_RETORRENT_JOB_TOOL}."
    elif any("target-duplicate" in blocker for blocker in blockers):
        decision = "stop_or_review_duplicate"
        recommended_action = "Inspect duplicate_check.dupes before taking any upload action."
    else:
        decision = "resolve_blockers"
        recommended_action = f"Resolve blockers before submitting this candidate to {SOURCE_URL_RETORRENT_JOB_TOOL}."
    return {
        "tool": SOURCE_URL_RETORRENT_JOB_TOOL,
        "endpoint": SOURCE_URL_RETORRENT_JOB_ENDPOINT,
        "decision": decision,
        "recommended_action": recommended_action,
        "requires": ["accept_rules=true", "confirm_upload=true", "save_path or path"],
    }


def _candidate_ranking(seed: CandidateSeed, source_info: dict[str, Any] | None, duplicate_check: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    score = 50
    reasons: list[str] = []
    penalties: list[str] = []
    metadata_keys = [key for key in ("imdb_id", "tmdb_id", "douban_id", "douban_url", "name") if source_info and source_info.get(key)]
    metadata_ready = bool(metadata_keys)
    duplicate_status = str(duplicate_check.get("status") or "unknown")
    freeleech_like = _promotion_is_free(seed.promotion)

    if blockers:
        penalty = min(30, len(blockers) * 8)
        score -= penalty
        penalties.append(f"{len(blockers)} blocker(s) reduce executable confidence by {penalty}.")
    else:
        score += 25
        reasons.append("All current policy, metadata, duplicate, and adapter gates are ready.")

    if duplicate_status == "not_found":
        score += 15
        reasons.append("Target duplicate search found no existing torrent.")
    elif duplicate_status == "exists":
        score -= 35
        penalties.append("Target duplicate search found possible existing torrents.")
    elif duplicate_status in {"skipped", "unsupported", "unknown"}:
        score -= 10
        penalties.append(f"Target duplicate confidence is limited: {duplicate_status}.")

    if metadata_ready:
        score += 10
        reasons.append(f"Source metadata signal is available: {', '.join(metadata_keys)}.")
    else:
        score -= 20
        penalties.append("Source metadata is missing IMDb/TMDb/Douban/name signals.")

    if freeleech_like:
        score += 5
        reasons.append("Source promotion appears freeleech-like.")
    elif seed.promotion:
        score += 2
        reasons.append(f"Source promotion detected: {seed.promotion}.")

    if seed.seeders is not None:
        if seed.seeders > 0:
            bonus = min(5, max(1, seed.seeders // 5))
            score += bonus
            reasons.append(f"Source has {seed.seeders} seeder(s).")
        else:
            score -= 5
            penalties.append("Source currently reports no seeders.")
    if seed.leechers is not None and seed.leechers > 0:
        bonus = min(5, max(1, seed.leechers // 10))
        score += bonus
        reasons.append(f"Source has {seed.leechers} leecher(s), suggesting active demand.")

    score = max(0, min(100, score))
    if not blockers:
        tier = "ready"
    elif duplicate_check.get("exists") is True or not source_info:
        tier = "blocked"
    else:
        tier = "review"
    return {
        "score": score,
        "tier": tier,
        "reasons": reasons or ["Candidate is from the source tracker recent listing."],
        "penalties": penalties,
        "signals": {
            "duplicate_status": duplicate_status,
            "metadata_ready": metadata_ready,
            "metadata_keys": metadata_keys,
            "promotion": seed.promotion,
            "freeleech_like": freeleech_like,
            "seeders": seed.seeders,
            "leechers": seed.leechers,
            "blocker_count": len(blockers),
        },
    }


def _candidate_recommendation(
    seed: CandidateSeed,
    source_info: dict[str, Any] | None,
    duplicate_check: dict[str, Any],
    blockers: list[str],
    ranking: dict[str, Any],
) -> dict[str, Any]:
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
        "score": ranking.get("score"),
        "tier": ranking.get("tier"),
        "reason": "; ".join(reasons),
        "requires": ["accept_rules", "confirm_upload", "save_path or path"],
    }


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, int]:
    ranking = candidate.get("ranking") if isinstance(candidate.get("ranking"), dict) else {}
    score = int(ranking.get("score", 0) or 0)
    source_order = int(candidate.get("_source_order", 0) or 0)
    ready = 1 if candidate.get("status") == "ready" else 0
    return ready, score, -source_order


def _candidate_digest(candidates: list[dict[str, Any]], blockers: list[str], next_actions: list[str], *, limit: int) -> dict[str, Any]:
    ready_candidates = [candidate for candidate in candidates if candidate.get("status") == "ready"]
    review_count = sum(1 for candidate in candidates if _candidate_tier(candidate) == "review")
    blocked_count = sum(1 for candidate in candidates if candidate.get("status") == "blocked" or _candidate_tier(candidate) == "blocked")
    top_candidate = ready_candidates[0] if ready_candidates else candidates[0] if candidates else None
    if ready_candidates:
        recommendation = "submit_top_candidate_when_confirmed"
    elif candidates:
        recommendation = "resolve_blockers"
    else:
        recommendation = "no_candidates"
    push_items = [_candidate_digest_item(candidate, rank=index + 1) for index, candidate in enumerate(candidates)]
    push_summary = _candidate_push_summary(len(candidates), len(ready_candidates), review_count, blocked_count, recommendation)
    return {
        "kind": "ptcli.daily_candidates_digest",
        "limit": limit,
        "selected_count": len(candidates),
        "ready_count": len(ready_candidates),
        "review_count": review_count,
        "blocked_count": blocked_count,
        "push_title": "Daily PT retorrent candidates",
        "push_summary": push_summary,
        "push_count": len(push_items),
        "recommended_action": _candidate_digest_recommended_action(recommendation),
        "top_candidate": _candidate_digest_item(top_candidate, rank=1) if top_candidate else None,
        "top_submit_request": top_candidate.get("submit_request") if isinstance(top_candidate, dict) and top_candidate.get("status") == "ready" else None,
        "top_submit_job_endpoint": top_candidate.get("submit_job_endpoint") if isinstance(top_candidate, dict) and top_candidate.get("status") == "ready" else None,
        "top_submit_tool": top_candidate.get("submit_tool") if isinstance(top_candidate, dict) and top_candidate.get("status") == "ready" else None,
        "recommendation": recommendation,
        "push_items": push_items,
        "blockers": blockers,
        "next_actions": next_actions,
    }


def _candidate_digest_item(candidate: dict[str, Any] | None, *, rank: int) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    source_info = candidate.get("source_info") if isinstance(candidate.get("source_info"), dict) else {}
    ranking = candidate.get("ranking") if isinstance(candidate.get("ranking"), dict) else {}
    duplicate_check = candidate.get("duplicate_check") if isinstance(candidate.get("duplicate_check"), dict) else {}
    workflow = candidate.get("agent_workflow") if isinstance(candidate.get("agent_workflow"), dict) else {}
    policy_summary = candidate.get("policy_summary") if isinstance(candidate.get("policy_summary"), dict) else {}
    blockers = _string_list(candidate.get("blockers"))
    status = str(candidate.get("status") or "")
    title = source.get("title") or source_info.get("name")
    submit_request = candidate.get("submit_request") if isinstance(candidate.get("submit_request"), dict) else None
    can_submit = bool(status == "ready" and submit_request)
    action_label = "submit_when_confirmed" if can_submit else "review_blockers"
    return {
        "rank": rank,
        "status": status,
        "score": ranking.get("score"),
        "tier": ranking.get("tier"),
        "source_tracker": source.get("tracker"),
        "source_id": source.get("torrent_id"),
        "source_url": source.get("details_url"),
        "title": title,
        "size": source.get("size"),
        "published_at": source.get("published_at"),
        "promotion": source.get("promotion"),
        "metadata": {
            "imdb_id": source_info.get("imdb_id"),
            "tmdb_id": source_info.get("tmdb_id"),
            "douban_id": source_info.get("douban_id"),
            "douban_url": source_info.get("douban_url"),
            "name": source_info.get("name"),
        },
        "duplicate_status": duplicate_check.get("status"),
        "duplicate_count": duplicate_check.get("count"),
        "blocker_count": len(blockers),
        "blockers": blockers,
        "next_actions": _candidate_push_next_actions(candidate, workflow),
        "decision": workflow.get("decision"),
        "recommended_action": workflow.get("recommended_action"),
        "summary_text": _candidate_push_line(rank, status, source, source_info, ranking, duplicate_check, blockers),
        "action_label": action_label,
        "action_endpoint": candidate.get("submit_job_endpoint") if can_submit else None,
        "can_submit": can_submit,
        "policy_coverage": policy_summary.get("policy_coverage"),
        "policy_summary": _candidate_digest_policy_summary(policy_summary),
        "submit_request": submit_request if can_submit else None,
        "submit_job_endpoint": candidate.get("submit_job_endpoint"),
        "submit_tool": candidate.get("submit_tool"),
    }


def _candidate_push_summary(selected_count: int, ready_count: int, review_count: int, blocked_count: int, recommendation: str) -> str:
    if selected_count <= 0:
        return "No daily retorrent candidates are available yet."
    return f"{selected_count} candidate(s): {ready_count} ready, {review_count} need review, {blocked_count} blocked. Recommendation: {recommendation}."


def _candidate_digest_recommended_action(recommendation: str) -> str:
    if recommendation == "submit_top_candidate_when_confirmed":
        return "Review digest.top_candidate, add confirm_upload=true plus save_path or path, then submit digest.top_submit_request to source_url_retorrent_job."
    if recommendation == "resolve_blockers":
        return "Review digest.push_items[].blockers and resolve policy, duplicate, metadata, or adapter blockers before submitting."
    return "Adjust source/target schedule or rerun after source candidates are available."


def _candidate_push_line(
    rank: int,
    status: str,
    source: dict[str, Any],
    source_info: dict[str, Any],
    ranking: dict[str, Any],
    duplicate_check: dict[str, Any],
    blockers: list[str],
) -> str:
    tracker = source.get("tracker") or "?"
    torrent_id = source.get("torrent_id") or "?"
    title = source.get("title") or source_info.get("name") or "untitled"
    size = source.get("size") or "size unknown"
    promotion = source.get("promotion") or "no promo"
    duplicate_status = duplicate_check.get("status") or "unknown"
    score = ranking.get("score")
    blocker_text = f", blockers={len(blockers)}" if blockers else ""
    return f"#{rank} [{status}] {tracker}-{torrent_id} {title} ({size}, {promotion}), score={score}, duplicate={duplicate_status}{blocker_text}."


def _candidate_push_next_actions(candidate: dict[str, Any], workflow: dict[str, Any]) -> list[str]:
    blockers = _string_list(candidate.get("blockers"))
    if blockers:
        return [workflow.get("recommended_action") or "Resolve blockers before submitting this candidate."]
    return [workflow.get("recommended_action") or "Review site rules and submit this candidate after confirmation."]


def _candidate_digest_policy_summary(policy_summary: dict[str, Any]) -> dict[str, Any]:
    qbit_limits = policy_summary.get("qbit_limits") if isinstance(policy_summary.get("qbit_limits"), dict) else {}
    seeding = policy_summary.get("seeding_requirements") if isinstance(policy_summary.get("seeding_requirements"), dict) else {}
    return {
        "manual_review_ready": policy_summary.get("manual_review_ready"),
        "automation": policy_summary.get("automation"),
        "policy_coverage": policy_summary.get("policy_coverage"),
        "qbit_limits": qbit_limits,
        "seeding_requirements": seeding,
    }


def _candidate_tier(candidate: dict[str, Any]) -> str:
    ranking = candidate.get("ranking") if isinstance(candidate.get("ranking"), dict) else {}
    return str(ranking.get("tier") or candidate.get("status") or "")


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
        "ranking": {
            "strategy": "ready-first, then descending score, then source listing order",
            "score_range": "0-100",
            "scan_count": 0,
            "selected_count": 0,
        },
        "digest": _candidate_digest([], blockers, [], limit=limit),
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _promotion_is_free(promotion: str | None) -> bool:
    return bool(promotion and re.search(r"\b(free|2x|50%|免费|免費|限免|freeleech)\b", promotion, flags=re.IGNORECASE))


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
