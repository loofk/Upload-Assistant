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
PLACEHOLDER_FINGERPRINTS = {"manual-review", "manual-review-yyyy-mm-dd", "reviewed-yyyy-mm-dd"}
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
    source_capability = _source_candidate_capability(source, base_dir=base_dir, limit=limit)
    discovery_handoff = _candidate_discovery_handoff(source_capability, targets, limit=limit)
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
    digest = _candidate_digest(
        candidates,
        payload_blockers,
        next_actions,
        limit=limit,
        scan_count=len(seeds),
        source_tracker=source,
        target_trackers=targets,
        accept_rules=accept_rules,
        check_dupes=check_dupes,
        base_dir=base_dir,
        discovery_handoff=discovery_handoff,
    )
    target_summary = _candidate_target_summary(limit, scan_count=len(seeds), selected_count=len(candidates), ready_count=ready_count)
    return {
        "kind": "ptcli.daily_candidates",
        "status": status,
        "ok": bool(candidates),
        "source_tracker": source,
        "target_trackers": targets,
        "limit": limit,
        "target_count": target_summary["target_count"],
        "scan_count": target_summary["scan_count"],
        "count": len(candidates),
        "ready_count": ready_count,
        "shortfall_count": target_summary["shortfall_count"],
        "target_met": target_summary["target_met"],
        "target_summary": target_summary,
        "source_capability": source_capability,
        "candidate_discovery_handoff": discovery_handoff,
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
    downloadability_summary = await _candidate_downloadability_summary(seed, source_policy, execute_request, accept_rules=accept_rules, base_dir=base_dir)
    discovery_profile = _source_candidate_capability(seed.tracker, base_dir=base_dir, limit=MAX_CANDIDATE_SCAN)
    policy_summary = _candidate_policy_summary(source_policy, target_policies, execute_request, accept_rules=accept_rules)
    policy_risk_summary = _candidate_policy_risk_summary(policy_summary, blockers=blockers)
    policy_summary["policy_risk_summary"] = policy_risk_summary
    site_policy_profile_handoff = policy_summary.get("site_policy_profile_handoff")
    site_policy_summary = policy_summary.get("site_policy_summary")
    status = "ready" if not blockers else "blocked"
    ranking = _candidate_ranking(seed, source_info_payload, duplicate_check, blockers, downloadability_summary)
    return {
        "status": status,
        "source": seed.to_dict(),
        "source_info": source_info_payload,
        "source_info_error": source_info_error,
        "duplicate_check": duplicate_check,
        "candidate_discovery_profile": discovery_profile,
        "downloadability_summary": downloadability_summary,
        "source_policy": source_policy,
        "target_policies": target_policies,
        "policy_summary": policy_summary,
        "policy_risk_summary": policy_risk_summary,
        "policy_coverage": policy_summary.get("policy_coverage"),
        "policy_execution_handoff": policy_summary.get("policy_execution_handoff"),
        "site_policy_profile_handoff": site_policy_profile_handoff,
        "site_policy_summary": site_policy_summary,
        "ranking": ranking,
        "recommendation": _candidate_recommendation(seed, source_info_payload, duplicate_check, blockers, ranking),
        "decision_summary": _candidate_decision_summary(seed, source_info_payload, duplicate_check, blockers, ranking, policy_summary, status, downloadability_summary),
        "blockers": blockers,
        "risk_flags": blockers,
        "agent_workflow": _candidate_agent_workflow(status, blockers),
        "submit_request": execute_request,
        "submit_job_endpoint": SOURCE_URL_RETORRENT_JOB_ENDPOINT,
        "submit_tool": SOURCE_URL_RETORRENT_JOB_TOOL,
        "execute_request": execute_request,
        "execute_job_endpoint": SOURCE_URL_RETORRENT_JOB_ENDPOINT,
    }


async def _candidate_downloadability_summary(seed: CandidateSeed, source_policy: dict[str, Any], execute_request: dict[str, Any], *, accept_rules: bool, base_dir: str | None) -> dict[str, Any]:
    cookie_path = _cookie_path(seed.tracker, base_dir)
    cookie_exists = await asyncio.to_thread(os.path.exists, cookie_path)
    adapter = source_download_adapter(seed.tracker)
    source_url = execute_request.get("source_url") or execute_request.get("source") or seed.details_url
    policy_allows_download = source_policy.get("allow_auto_download") is True
    policy_allows_retorrent = source_policy.get("allow_retorrent") is True
    blockers: list[str] = []
    if not adapter:
        blockers.append(f"{seed.tracker} source download adapter is not enabled.")
    if not policy_allows_download:
        blockers.append(f"{seed.tracker} policy: automatic source download is not enabled.")
    if not policy_allows_retorrent:
        blockers.append(f"{seed.tracker} policy: retorrent automation is not enabled.")
    if not source_url:
        blockers.append("source-url: details URL is missing.")
    if source_policy.get("manual_review_required") is True and not accept_rules:
        blockers.append("site-policy: source rules must be acknowledged before downloading.")
    return {
        "kind": "ptcli.daily_candidate_downloadability_summary",
        "ready": not blockers,
        "downloadable": bool(adapter and policy_allows_download and policy_allows_retorrent and source_url),
        "source_tracker": seed.tracker,
        "source_id": seed.torrent_id,
        "source_url": source_url,
        "source_download_adapter": adapter,
        "source_info_adapter": source_info_adapter(seed.tracker),
        "candidate_discovery_adapter": "generic_recent_cookie" if seed.tracker in GENERIC_DETAILS_BASE_URLS and seed.tracker not in MTEAM_API_TRACKERS else None,
        "candidate_discovery_profile": _source_candidate_capability(seed.tracker, base_dir=base_dir, limit=MAX_CANDIDATE_SCAN),
        "policy_allows_download": policy_allows_download,
        "policy_allows_retorrent": policy_allows_retorrent,
        "rules_accepted": bool(accept_rules),
        "manual_review_required": source_policy.get("manual_review_required") is True,
        "cookie": {
            "required": True,
            "path": cookie_path,
            "exists": cookie_exists,
            "status": "verified" if cookie_exists else "missing",
            "note": "Candidate discovery already requires a valid cookie; source torrent download should reuse the same tracker session.",
        },
        "source_pull": {
            "tool": SOURCE_URL_RETORRENT_JOB_TOOL,
            "endpoint": SOURCE_URL_RETORRENT_JOB_ENDPOINT,
            "request": execute_request,
            "direct_cli_tool": "source-download",
            "direct_cli_args": ["source-download", "--tracker", seed.tracker, "--source-id", seed.torrent_id, "--to", str(execute_request.get("target") or ""), "--accept-rules", "--json"],
        },
        "continue_when": "downloadability_summary.ready=true, user confirms site rules, and source_pull is only executed through approved ptcli tools",
        "stop_when": ["downloadability_summary.blockers is non-empty", "cookie.status='missing' in live environment", "source_download_adapter missing"],
        "blockers": blockers,
        "next_actions": _candidate_downloadability_next_actions(blockers, cookie_exists),
    }


def _candidate_downloadability_next_actions(blockers: list[str], cookie_exists: bool) -> list[str]:
    if blockers:
        return ["Resolve downloadability_summary.blockers before treating this candidate as executable."]
    if not cookie_exists:
        return ["Refresh the source tracker cookie before live source torrent download; candidate discovery may have been mocked or run from another base_dir."]
    return ["Source pull prerequisites are visible; submit only after duplicate, policy, confirmation, and material gates remain clear."]


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
    blockers.extend(_candidate_rule_review_blockers(seed.tracker, source_policy))
    if source_policy.get("allow_auto_download") is not True:
        blockers.append(f"{seed.tracker} policy: automatic source download is not enabled.")
    if source_policy.get("allow_retorrent") is not True:
        blockers.append(f"{seed.tracker} policy: retorrent automation is not enabled.")
    blockers.extend(_candidate_transfer_rule_blockers(seed, source_policy))
    for target_policy in target_policies:
        target = str(target_policy.get("tracker") or "target")
        blockers.extend(_candidate_rule_review_blockers(target, target_policy))
        if target_policy.get("allow_auto_upload") is not True:
            blockers.append(f"{target} policy: automatic target upload is not enabled.")
        if target_policy.get("allow_retorrent") is not True:
            blockers.append(f"{target} policy: retorrent automation is not enabled.")
        blockers.extend(_candidate_transfer_rule_blockers(seed, target_policy))
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


def _candidate_rule_review_blockers(tracker: str, policy: dict[str, Any]) -> list[str]:
    if policy.get("manual_review_required") is not True:
        return []
    fingerprint = str(policy.get("rule_review_fingerprint") or "").strip()
    if not fingerprint:
        return [f"{tracker} policy: rule_review_fingerprint is required before candidate submission."]
    if _placeholder_rule_review_fingerprint(fingerprint):
        return [f"{tracker} policy: rule_review_fingerprint still looks like a placeholder."]
    return []


def _placeholder_rule_review_fingerprint(value: str) -> bool:
    normalized = value.strip().lower()
    return "yyyy" in normalized or normalized in PLACEHOLDER_FINGERPRINTS


def _candidate_transfer_rule_blockers(seed: CandidateSeed, policy: dict[str, Any]) -> list[str]:
    tracker = str(policy.get("tracker") or seed.tracker)
    transfer_rules = policy.get("transfer_rules") if isinstance(policy.get("transfer_rules"), dict) else {}
    freeleech_required = bool(policy.get("freeleech_required") or transfer_rules.get("freeleech_required"))
    required_promotions = _string_list(policy.get("required_promotions") or transfer_rules.get("required_promotions"))
    forbidden_patterns = _string_list(policy.get("forbidden_title_patterns") or transfer_rules.get("forbidden_title_patterns"))
    forbidden_groups = _string_list(policy.get("forbidden_release_groups") or transfer_rules.get("forbidden_release_groups"))
    title = seed.title or ""
    promotion = seed.promotion or ""
    release_group = _release_group_from_title(title)
    blockers: list[str] = []
    if freeleech_required and not _promotion_is_free(promotion):
        blockers.append(f"{tracker} policy: freeleech source candidate is required.")
    if required_promotions and not _promotion_matches_any(promotion, required_promotions):
        blockers.append(f"{tracker} policy: source promotion must match one of: {', '.join(required_promotions)}.")
    blockers.extend(f"{tracker} policy: title matches forbidden pattern {pattern!r}." for pattern in forbidden_patterns if pattern and re.search(pattern, title, flags=re.IGNORECASE))
    if release_group and any(release_group.lower() == group.lower() for group in forbidden_groups):
        blockers.append(f"{tracker} policy: release group {release_group} is forbidden.")
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
    source_coverage = build_site_policy_coverage(source_policy, roles=["source"], accept_rules=accept_rules)
    target_coverages = [build_site_policy_coverage(policy, roles=["target"], accept_rules=accept_rules) for policy in target_policies]
    summary = {
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
        "transfer_rules": {
            "source": _policy_transfer_rules(source_policy),
            "targets": [_policy_transfer_rules(policy) for policy in target_policies],
        },
        "rules": {
            "source_rules_url": source_policy.get("rules_url"),
            "target_rules_urls": [policy.get("rules_url") for policy in target_policies if policy.get("rules_url")],
            "source_fingerprint": source_policy.get("rule_review_fingerprint"),
            "target_fingerprints": [policy.get("rule_review_fingerprint") for policy in target_policies if policy.get("rule_review_fingerprint")],
            "fingerprint_status": {
                "source": _policy_fingerprint_status(source_policy),
                "targets": [_policy_fingerprint_status(policy) for policy in target_policies],
            },
        },
        "policy_coverage": _candidate_policy_coverage_summary(source_coverage, target_coverages),
    }
    summary["policy_execution_handoff"] = _candidate_policy_execution_handoff(summary, execute_request)
    summary["site_policy_profile_handoff"] = _candidate_site_policy_profile_handoff(summary, execute_request)
    summary["site_policy_summary"] = _candidate_site_policy_summary(summary["site_policy_profile_handoff"])
    return summary


def _policy_fingerprint_status(policy: dict[str, Any]) -> dict[str, Any]:
    fingerprint = str(policy.get("rule_review_fingerprint") or "").strip()
    placeholder = bool(fingerprint and _placeholder_rule_review_fingerprint(fingerprint))
    ready = policy.get("manual_review_required") is not True or bool(fingerprint and not placeholder)
    return {
        "tracker": policy.get("tracker"),
        "manual_review_required": policy.get("manual_review_required") is True,
        "ready": ready,
        "missing": policy.get("manual_review_required") is True and not fingerprint,
        "placeholder": placeholder,
        "fingerprint": fingerprint or None,
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
        "ready": all(bool(coverage.get("complete")) and bool((coverage.get("rule_obligations") or {}).get("ready")) for coverage in coverages),
        "rule_obligations_ready": all(bool((coverage.get("rule_obligations") or {}).get("ready")) for coverage in coverages),
        "source": source_coverage,
        "targets": target_coverages,
        "missing_policy_fields": missing,
        "disabled_automation": disabled,
        "recommendations": [recommendation for coverage in coverages for recommendation in _string_list(coverage.get("recommendations"))],
    }


def _candidate_policy_execution_handoff(policy_summary: dict[str, Any], execute_request: dict[str, Any]) -> dict[str, Any]:
    coverage = policy_summary.get("policy_coverage") if isinstance(policy_summary.get("policy_coverage"), dict) else {}
    qbit_limits = policy_summary.get("qbit_limits") if isinstance(policy_summary.get("qbit_limits"), dict) else {}
    seeding = policy_summary.get("seeding_requirements") if isinstance(policy_summary.get("seeding_requirements"), dict) else {}
    transfer_rules = policy_summary.get("transfer_rules") if isinstance(policy_summary.get("transfer_rules"), dict) else {}
    rules = policy_summary.get("rules") if isinstance(policy_summary.get("rules"), dict) else {}
    missing_by_category = _candidate_policy_missing_by_category(coverage)
    blockers = _candidate_policy_execution_blockers(coverage, missing_by_category)
    ready = coverage.get("ready") is True and policy_summary.get("manual_review_ready") is True and not blockers
    return {
        "kind": "ptcli.daily_candidate_policy_execution_handoff",
        "ready": ready,
        "accepted_rules": bool(policy_summary.get("accept_rules")),
        "phase": "ready_for_candidate_submission" if ready else "configure_site_policy",
        "qbit": {
            "ready": not missing_by_category["rate_limits"],
            "source": qbit_limits.get("source") if isinstance(qbit_limits.get("source"), dict) else {},
            "target": qbit_limits.get("target") if isinstance(qbit_limits.get("target"), dict) else {},
            "request": {
                "qbit_upload_limit": execute_request.get("qbit_upload_limit"),
                "qbit_download_limit": execute_request.get("qbit_download_limit"),
                "uploaded_qbit_upload_limit": execute_request.get("uploaded_qbit_upload_limit"),
                "uploaded_qbit_download_limit": execute_request.get("uploaded_qbit_download_limit"),
            },
            "missing": missing_by_category["rate_limits"],
        },
        "seeding": {
            "ready": not missing_by_category["seeding_requirements"],
            "source": seeding.get("source") if isinstance(seeding.get("source"), dict) else {},
            "targets": seeding.get("targets") if isinstance(seeding.get("targets"), list) else [],
            "missing": missing_by_category["seeding_requirements"],
        },
        "transfer_rules": {
            "ready": True,
            "source": transfer_rules.get("source") if isinstance(transfer_rules.get("source"), dict) else {},
            "targets": transfer_rules.get("targets") if isinstance(transfer_rules.get("targets"), list) else [],
        },
        "rule_obligations": {
            "ready": coverage.get("rule_obligations_ready") is True,
            "source": (coverage.get("source") or {}).get("rule_obligations") if isinstance(coverage.get("source"), dict) else {},
            "targets": [target.get("rule_obligations") for target in coverage.get("targets", []) if isinstance(target, dict)],
        },
        "rules": rules,
        "missing_by_category": missing_by_category,
        "continue_when": "policy_execution_handoff.ready=true and user supplies confirm_upload plus save_path/path before submitting candidate",
        "stop_when": [
            "policy_execution_handoff.ready=false",
            "policy_execution_handoff.qbit.missing is non-empty",
            "policy_execution_handoff.seeding.missing is non-empty",
            "policy_execution_handoff.rule_obligations.ready=false",
        ],
        "blockers": blockers,
        "next_actions": _candidate_policy_execution_next_actions(ready, blockers),
    }


def _candidate_policy_missing_by_category(coverage: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    missing_fields = coverage.get("missing_policy_fields") if isinstance(coverage.get("missing_policy_fields"), dict) else {}
    categories = {"rate_limits": [], "seeding_requirements": [], "rule_review": [], "other": []}
    for tracker, fields in missing_fields.items():
        for field in _string_list(fields):
            item = {"tracker": str(tracker), "field": field}
            if field in {"download_rate_limit", "upload_rate_limit"}:
                categories["rate_limits"].append(item)
            elif field in {"min_seed_time_hours", "min_ratio"}:
                categories["seeding_requirements"].append(item)
            elif field == "rule_review_fingerprint":
                categories["rule_review"].append(item)
            else:
                categories["other"].append(item)
    return categories


def _candidate_policy_execution_blockers(coverage: dict[str, Any], missing_by_category: dict[str, list[dict[str, str]]]) -> list[str]:
    blockers = [f"{item['tracker']}: {item['field']}" for items in missing_by_category.values() for item in items]
    if coverage.get("rule_obligations_ready") is False:
        blockers.append("rule_obligations_ready=false")
    blockers.extend(_string_list(coverage.get("recommendations")))
    return list(dict.fromkeys(blockers))


def _candidate_policy_execution_next_actions(ready: bool, blockers: list[str]) -> list[str]:
    if ready:
        return ["Candidate policy execution is ready; require explicit user approval plus confirm_upload and save_path/path before submission."]
    if blockers:
        return ["Resolve policy_execution_handoff.blockers in PTCLI.SITE_POLICIES before submitting this candidate."]
    return ["Inspect policy_execution_handoff before deciding whether this candidate can be submitted."]


def _candidate_site_policy_profile_handoff(policy_summary: dict[str, Any], execute_request: dict[str, Any]) -> dict[str, Any]:
    execution = policy_summary.get("policy_execution_handoff") if isinstance(policy_summary.get("policy_execution_handoff"), dict) else {}
    coverage = policy_summary.get("policy_coverage") if isinstance(policy_summary.get("policy_coverage"), dict) else {}
    qbit_limits = policy_summary.get("qbit_limits") if isinstance(policy_summary.get("qbit_limits"), dict) else {}
    seeding = policy_summary.get("seeding_requirements") if isinstance(policy_summary.get("seeding_requirements"), dict) else {}
    transfer_rules = policy_summary.get("transfer_rules") if isinstance(policy_summary.get("transfer_rules"), dict) else {}
    rules = policy_summary.get("rules") if isinstance(policy_summary.get("rules"), dict) else {}
    source_coverage = coverage.get("source") if isinstance(coverage.get("source"), dict) else {}
    target_coverages = coverage.get("targets") if isinstance(coverage.get("targets"), list) else []
    source_tracker = execute_request.get("source_tracker") or source_coverage.get("tracker")
    target_trackers = execute_request.get("target") or execute_request.get("target_trackers") or [target.get("tracker") for target in target_coverages if isinstance(target, dict)]
    source_ready = bool(source_coverage.get("complete")) and bool((source_coverage.get("rule_obligations") or {}).get("ready")) if source_coverage else False
    targets_ready = all(bool(target.get("complete")) and bool((target.get("rule_obligations") or {}).get("ready")) for target in target_coverages if isinstance(target, dict))
    blockers = _string_list(execution.get("blockers"))
    ready = bool(execution.get("ready") is True and coverage.get("ready") is True and source_ready and targets_ready and not blockers)
    return {
        "kind": "ptcli.daily_candidate_site_policy_profile_handoff",
        "ready": ready,
        "status": "ready" if ready else "blocked",
        "accepted_rules": bool(policy_summary.get("accept_rules")),
        "source_tracker": source_tracker,
        "target_trackers": target_trackers,
        "source_ready": source_ready,
        "targets_ready": targets_ready,
        "qbit_limits": qbit_limits,
        "seeding_requirements": seeding,
        "transfer_rules": transfer_rules,
        "rules": rules,
        "rule_obligations_ready": coverage.get("rule_obligations_ready") is True,
        "policy_coverage": coverage,
        "policy_execution_handoff": execution,
        "continue_when": "site_policy_profile_handoff.ready=true and the user explicitly approves this candidate",
        "stop_when": [
            "site_policy_profile_handoff.ready=false",
            "site policy profile has missing rate limits, seeding requirements, or rule review fingerprint",
            "rule_obligations_ready=false",
        ],
        "blockers": blockers,
        "next_actions": _candidate_site_policy_profile_next_actions(ready, blockers),
    }


def _candidate_site_policy_profile_next_actions(ready: bool, blockers: list[str]) -> list[str]:
    if ready:
        return ["Use site_policy_profile_handoff as the rule/rate-limit/seeding checklist before submitting this candidate."]
    if blockers:
        return ["Resolve site_policy_profile_handoff.blockers in PTCLI.SITE_POLICIES before approval or submission."]
    return ["Review site_policy_profile_handoff.policy_coverage before deciding whether this candidate can be submitted."]


def _candidate_site_policy_summary(site_policy_profile_handoff: dict[str, Any]) -> dict[str, Any]:
    qbit_limits = site_policy_profile_handoff.get("qbit_limits") if isinstance(site_policy_profile_handoff.get("qbit_limits"), dict) else {}
    seeding = site_policy_profile_handoff.get("seeding_requirements") if isinstance(site_policy_profile_handoff.get("seeding_requirements"), dict) else {}
    transfer_rules = site_policy_profile_handoff.get("transfer_rules") if isinstance(site_policy_profile_handoff.get("transfer_rules"), dict) else {}
    return {
        "ready": site_policy_profile_handoff.get("ready") is True,
        "accepted_rules": site_policy_profile_handoff.get("accepted_rules") is True,
        "source_tracker": site_policy_profile_handoff.get("source_tracker"),
        "target_trackers": site_policy_profile_handoff.get("target_trackers"),
        "source_ready": site_policy_profile_handoff.get("source_ready") is True,
        "targets_ready": site_policy_profile_handoff.get("targets_ready") is True,
        "rule_obligations_ready": site_policy_profile_handoff.get("rule_obligations_ready") is True,
        "qbit_limits": qbit_limits,
        "seeding_requirements": seeding,
        "transfer_rules": transfer_rules,
        "blockers": _string_list(site_policy_profile_handoff.get("blockers")),
    }


def _candidate_policy_risk_summary(policy_summary: dict[str, Any], *, blockers: list[str] | None = None) -> dict[str, Any]:
    execution = policy_summary.get("policy_execution_handoff") if isinstance(policy_summary.get("policy_execution_handoff"), dict) else {}
    qbit = execution.get("qbit") if isinstance(execution.get("qbit"), dict) else {}
    seeding = execution.get("seeding") if isinstance(execution.get("seeding"), dict) else {}
    rule_obligations = execution.get("rule_obligations") if isinstance(execution.get("rule_obligations"), dict) else {}
    transfer_rules = policy_summary.get("transfer_rules") if isinstance(policy_summary.get("transfer_rules"), dict) else {}
    source_transfer = transfer_rules.get("source") if isinstance(transfer_rules.get("source"), dict) else {}
    target_transfers = transfer_rules.get("targets") if isinstance(transfer_rules.get("targets"), list) else []
    strict_rules = _candidate_strict_transfer_rules(source_transfer, target_transfers)
    blockers = list(dict.fromkeys(_string_list(execution.get("blockers")) + _string_list(blockers)))
    qbit_ready = qbit.get("ready") is True
    seeding_ready = seeding.get("ready") is True
    rule_ready = rule_obligations.get("ready") is True
    manual_ready = policy_summary.get("manual_review_ready") is True
    ready = execution.get("ready") is True and qbit_ready and seeding_ready and rule_ready and manual_ready and not blockers
    if not ready or blockers:
        risk_level = "high"
        priority = "blocked"
    elif strict_rules:
        risk_level = "medium"
        priority = "guarded"
    else:
        risk_level = "low"
        priority = "preferred"
    return {
        "kind": "ptcli.daily_candidate_policy_risk_summary",
        "ready": ready,
        "risk_level": risk_level,
        "execution_priority": priority,
        "qbit_limit_ready": qbit_ready,
        "seeding_ready": seeding_ready,
        "rule_obligations_ready": rule_ready,
        "manual_review_ready": manual_ready,
        "strict_transfer_rule_count": len(strict_rules),
        "strict_transfer_rules": strict_rules,
        "qbit_missing": qbit.get("missing") if isinstance(qbit.get("missing"), list) else [],
        "seeding_missing": seeding.get("missing") if isinstance(seeding.get("missing"), list) else [],
        "blockers": blockers,
        "next_actions": _candidate_policy_risk_next_actions(ready, strict_rules, blockers),
    }


def _candidate_strict_transfer_rules(source_transfer: dict[str, Any], target_transfers: list[Any]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    if _string_list(source_transfer.get("required_promotions")):
        rules.append({"scope": "source", "field": "required_promotions", "value": _string_list(source_transfer.get("required_promotions"))})
    if source_transfer.get("freeleech_required") is True:
        rules.append({"scope": "source", "field": "freeleech_required", "value": True})
    for field in ("forbidden_title_patterns", "forbidden_release_groups"):
        values = _string_list(source_transfer.get(field))
        if values:
            rules.append({"scope": "source", "field": field, "value": values})
    for index, target_transfer in enumerate(target_transfers):
        if not isinstance(target_transfer, dict):
            continue
        tracker = target_transfer.get("tracker") or f"target:{index}"
        if target_transfer.get("freeleech_required") is True:
            rules.append({"scope": str(tracker), "field": "freeleech_required", "value": True})
        for field in ("required_promotions", "forbidden_title_patterns", "forbidden_release_groups"):
            values = _string_list(target_transfer.get(field))
            if values:
                rules.append({"scope": str(tracker), "field": field, "value": values})
    return rules


def _candidate_policy_risk_next_actions(ready: bool, strict_rules: list[dict[str, Any]], blockers: list[str]) -> list[str]:
    if blockers:
        return ["Resolve policy_risk_summary.blockers before considering this candidate for upload farming."]
    if not ready:
        return ["Read policy_execution_handoff and update SITE_POLICIES before submitting this candidate."]
    if strict_rules:
        return ["Recheck strict transfer rules against the source title/promotion before approving this candidate."]
    return ["Candidate policy risk is low; still require confirm_upload and save_path/path before submission."]


def _policy_seeding_requirements(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "tracker": policy.get("tracker"),
        "min_seed_time_hours": policy.get("min_seed_time_hours"),
        "min_ratio": policy.get("min_ratio"),
        "freeleech_required": policy.get("freeleech_required"),
    }


def _policy_transfer_rules(policy: dict[str, Any]) -> dict[str, Any]:
    transfer_rules = policy.get("transfer_rules") if isinstance(policy.get("transfer_rules"), dict) else {}
    return {
        "tracker": policy.get("tracker"),
        "freeleech_required": bool(policy.get("freeleech_required") or transfer_rules.get("freeleech_required")),
        "required_promotions": _string_list(policy.get("required_promotions") or transfer_rules.get("required_promotions")),
        "forbidden_title_patterns": _string_list(policy.get("forbidden_title_patterns") or transfer_rules.get("forbidden_title_patterns")),
        "forbidden_release_groups": _string_list(policy.get("forbidden_release_groups") or transfer_rules.get("forbidden_release_groups")),
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


def _candidate_ranking(seed: CandidateSeed, source_info: dict[str, Any] | None, duplicate_check: dict[str, Any], blockers: list[str], downloadability_summary: dict[str, Any]) -> dict[str, Any]:
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

    if downloadability_summary.get("ready") is True:
        score += 5
        reasons.append("Source downloadability preflight is ready.")
    else:
        score -= 10
        penalties.append("Source downloadability preflight has blockers or missing evidence.")

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
            "downloadable": downloadability_summary.get("downloadable"),
            "downloadability_ready": downloadability_summary.get("ready"),
            "download_adapter": downloadability_summary.get("source_download_adapter"),
            "download_cookie_status": (downloadability_summary.get("cookie") or {}).get("status") if isinstance(downloadability_summary.get("cookie"), dict) else None,
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


def _candidate_digest(
    candidates: list[dict[str, Any]],
    blockers: list[str],
    next_actions: list[str],
    *,
    limit: int,
    scan_count: int | None = None,
    source_tracker: str | None = None,
    target_trackers: list[str] | None = None,
    accept_rules: bool = False,
    check_dupes: bool = True,
    base_dir: str | None = None,
    discovery_handoff: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    approval_queue = _candidate_approval_queue(push_items)
    target_summary = _candidate_target_summary(limit, scan_count=scan_count, selected_count=len(candidates), ready_count=len(ready_candidates))
    push_summary = _candidate_push_summary(target_summary, review_count, blocked_count, recommendation)
    request_context = _candidate_discovery_request_context(source_tracker, target_trackers, limit, accept_rules=accept_rules, check_dupes=check_dupes, base_dir=base_dir)
    discovery_handoff = discovery_handoff or _candidate_discovery_handoff(_source_candidate_capability(str(source_tracker or ""), base_dir=base_dir, limit=limit), target_trackers or [], limit=limit)
    execution_plan = _candidate_execution_plan(push_items, approval_queue, target_summary, blockers, next_actions, recommendation=recommendation, request_context=request_context)
    daily_candidate_report = _candidate_daily_report(push_items, approval_queue, execution_plan, target_summary, blockers, recommendation=recommendation)
    daily_candidate_batch_report = _candidate_batch_report(push_items, approval_queue, execution_plan, daily_candidate_report, target_summary, blockers)
    candidate_control_summary = _candidate_control_summary(daily_candidate_report, daily_candidate_batch_report, approval_queue, execution_plan, blockers, scope="daily_candidates")
    candidate_executability_matrix = _candidate_executability_matrix(push_items, blockers)
    push_payload = _candidate_push_payload(
        push_summary,
        push_items,
        recommendation,
        blockers,
        next_actions,
        target_summary=target_summary,
        approval_queue=approval_queue,
        execution_plan=execution_plan,
        daily_candidate_report=daily_candidate_report,
        daily_candidate_batch_report=daily_candidate_batch_report,
        candidate_control_summary=candidate_control_summary,
        candidate_executability_matrix=candidate_executability_matrix,
        candidate_discovery_handoff=discovery_handoff,
    )
    return {
        "kind": "ptcli.daily_candidates_digest",
        "limit": limit,
        "target_count": target_summary["target_count"],
        "scan_count": target_summary["scan_count"],
        "selected_count": len(candidates),
        "ready_count": len(ready_candidates),
        "review_count": review_count,
        "blocked_count": blocked_count,
        "shortfall_count": target_summary["shortfall_count"],
        "target_met": target_summary["target_met"],
        "target_summary": target_summary,
        "request_context": request_context,
        "candidate_discovery_handoff": discovery_handoff,
        "push_title": "Daily PT retorrent candidates",
        "push_summary": push_summary,
        "push_payload": push_payload,
        "approval_queue": approval_queue,
        "approval_prompts": approval_queue["approval_prompts"],
        "first_approval_prompt": approval_queue["first_approval_prompt"],
        "top_safe_candidates": approval_queue["top_safe_candidates"],
        "execution_plan": execution_plan,
        "daily_candidate_report": daily_candidate_report,
        "daily_candidate_batch_report": daily_candidate_batch_report,
        "candidate_control_summary": candidate_control_summary,
        "candidate_executability_matrix": candidate_executability_matrix,
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


def _candidate_push_payload(
    push_summary: str,
    push_items: list[dict[str, Any] | None],
    recommendation: str,
    blockers: list[str],
    next_actions: list[str],
    *,
    target_summary: dict[str, Any],
    approval_queue: dict[str, Any],
    execution_plan: dict[str, Any],
    daily_candidate_report: dict[str, Any],
    daily_candidate_batch_report: dict[str, Any],
    candidate_control_summary: dict[str, Any],
    candidate_executability_matrix: dict[str, Any],
    candidate_discovery_handoff: dict[str, Any],
) -> dict[str, Any]:
    items = [item for item in push_items if isinstance(item, dict)]
    ready_items = [item for item in items if item.get("can_submit") is True]
    blocked_items = [item for item in items if item.get("can_submit") is not True]
    lines = [push_summary, *[str(item.get("summary_text")) for item in items if item.get("summary_text")]]
    decision_summary = _push_decision_summary(items, ready_items, blocked_items, recommendation)
    publish_cards = [item["publish_card"] for item in items if isinstance(item.get("publish_card"), dict)]
    candidate_field_completeness = _candidate_field_completeness(items)
    return {
        "kind": "ptcli.daily_candidates_push_payload",
        "title": "Daily PT retorrent candidates",
        "summary": push_summary,
        "message": "\n".join(lines),
        "format": "text/plain",
        "target_count": target_summary["target_count"],
        "scan_count": target_summary["scan_count"],
        "item_count": len(items),
        "ready_count": len(ready_items),
        "blocked_count": len(blocked_items),
        "shortfall_count": target_summary["shortfall_count"],
        "target_met": target_summary["target_met"],
        "target_summary": target_summary,
        "recommendation": recommendation,
        "recommended_action": _candidate_digest_recommended_action(recommendation),
        "decision_summary": decision_summary,
        "candidate_field_completeness": candidate_field_completeness,
        "publish_cards": publish_cards,
        "approval_queue": approval_queue,
        "approval_prompts": approval_queue["approval_prompts"],
        "first_approval_prompt": approval_queue["first_approval_prompt"],
        "top_safe_candidates": approval_queue["top_safe_candidates"],
        "execution_plan": execution_plan,
        "daily_candidate_report": daily_candidate_report,
        "daily_candidate_batch_report": daily_candidate_batch_report,
        "candidate_control_summary": candidate_control_summary,
        "candidate_executability_matrix": candidate_executability_matrix,
        "candidate_discovery_handoff": candidate_discovery_handoff,
        "top_item": ready_items[0] if ready_items else items[0] if items else None,
        "items": items,
        "blockers": blockers,
        "next_actions": next_actions,
    }


def _candidate_executability_matrix(items: list[dict[str, Any] | None], blockers: list[str]) -> dict[str, Any]:
    reports = [_candidate_executability_item(item) for item in items if isinstance(item, dict)]
    ready_items = [item for item in reports if item.get("ready")]
    blocked_items = [item for item in reports if not item.get("ready")]
    phase_summary = _candidate_executability_phase_summary(reports)
    next_item = blocked_items[0] if blocked_items else ready_items[0] if ready_items else None
    safe_items = [item for item in reports if item.get("can_submit_after_approval")]
    return {
        "kind": "ptcli.daily_candidate_executability_matrix",
        "ready": bool(safe_items) and not blockers,
        "status": "ready_for_approval" if safe_items and not blockers else "blocked" if blocked_items or blockers else "empty",
        "item_count": len(reports),
        "safe_to_submit_count": len(safe_items),
        "blocked_count": len(blocked_items),
        "ready_count": len(ready_items),
        "next_source_id": next_item.get("source_id") if isinstance(next_item, dict) else None,
        "next_phase": next_item.get("first_blocked_phase") if isinstance(next_item, dict) else None,
        "next_evidence": next_item.get("first_blocked_check") if isinstance(next_item, dict) else None,
        "items": reports,
        "phase_summary": phase_summary,
        "blockers": blockers,
        "continue_when": "candidate_executability_matrix.safe_to_submit_count>0 and user explicitly approves one candidate with confirm_upload=true",
        "stop_when": ["candidate_executability_matrix.blockers is non-empty", "candidate_executability_matrix.safe_to_submit_count=0"],
        "next_actions": _candidate_executability_next_actions(safe_items, next_item, blockers),
    }


def _candidate_executability_item(item: dict[str, Any]) -> dict[str, Any]:
    publish_card = item.get("publish_card") if isinstance(item.get("publish_card"), dict) else {}
    decision_summary = item.get("decision_summary") if isinstance(item.get("decision_summary"), dict) else {}
    policy_risk_summary = item.get("policy_risk_summary") if isinstance(item.get("policy_risk_summary"), dict) else {}
    site_policy_summary = item.get("site_policy_summary") if isinstance(item.get("site_policy_summary"), dict) else {}
    site_policy_profile_handoff = item.get("site_policy_profile_handoff") if isinstance(item.get("site_policy_profile_handoff"), dict) else {}
    policy_execution_handoff = item.get("policy_execution_handoff") if isinstance(item.get("policy_execution_handoff"), dict) else {}
    submit_request = item.get("submit_request") if isinstance(item.get("submit_request"), dict) else {}
    metadata = publish_card.get("metadata") if isinstance(publish_card.get("metadata"), dict) else item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    duplicate = publish_card.get("duplicate_check") if isinstance(publish_card.get("duplicate_check"), dict) else {}
    downloadability = item.get("downloadability_summary") if isinstance(item.get("downloadability_summary"), dict) else publish_card.get("downloadability") if isinstance(publish_card.get("downloadability"), dict) else {}
    item_blockers = _string_list(item.get("blockers"))
    checks = [
        _candidate_executability_check("source_identity", "source", bool(item.get("source_tracker") and item.get("source_id") and item.get("source_url")), ["source_tracker", "source_id", "source_url"]),
        _candidate_executability_check("metadata", "metadata", _candidate_metadata_ready(metadata), ["imdb_id or tmdb_id or douban_id or douban_url"]),
        _candidate_executability_check("duplicate_clear", "duplicate", duplicate.get("clear") is True or decision_summary.get("duplicate_clear") is True or item.get("duplicate_status") == "not_found", ["duplicate_check.clear=true"]),
        _candidate_executability_check("downloadability", "source_pull", downloadability.get("ready") is True or downloadability.get("downloadable") is True, ["downloadability_summary.ready=true"]),
        _candidate_executability_check("site_policy", "policy", site_policy_summary.get("ready") is True and site_policy_profile_handoff.get("ready") is True, ["site_policy_summary.ready=true", "site_policy_profile_handoff.ready=true"]),
        _candidate_executability_check("policy_execution", "policy", policy_execution_handoff.get("ready") is True, ["policy_execution_handoff.ready=true"]),
        _candidate_executability_check("risk_low", "risk", decision_summary.get("risk_level") == "low" and policy_risk_summary.get("risk_level") == "low" and not item_blockers, ["decision_summary.risk_level=low", "policy_risk_summary.risk_level=low", "blockers=[]"]),
        _candidate_executability_check("submit_request", "submit", bool(submit_request and submit_request.get("target") and (item.get("submit_tool") or item.get("action_endpoint"))), ["submit_request", "target", "submit_tool or action_endpoint"]),
    ]
    blocked_checks = [check for check in checks if check.get("ready") is not True]
    first_blocked = blocked_checks[0] if blocked_checks else None
    requires_human_approval = True
    ready = not blocked_checks
    can_submit_after_approval = ready and item.get("can_submit") is True
    return {
        "rank": item.get("rank"),
        "source_tracker": item.get("source_tracker"),
        "source_id": item.get("source_id"),
        "source_url": item.get("source_url"),
        "target": item.get("target"),
        "title": item.get("title"),
        "ready": ready,
        "can_submit_after_approval": can_submit_after_approval,
        "requires_human_approval": requires_human_approval,
        "status": "ready_for_approval" if can_submit_after_approval else "blocked",
        "first_blocked_phase": first_blocked.get("phase") if isinstance(first_blocked, dict) else None,
        "first_blocked_check": first_blocked.get("name") if isinstance(first_blocked, dict) else None,
        "checks": checks,
        "missing_checks": [check.get("name") for check in blocked_checks],
        "submit_tool": item.get("submit_tool") or "source_url_retorrent_job",
        "submit_endpoint": item.get("submit_job_endpoint") or item.get("action_endpoint"),
        "submit_request": submit_request if can_submit_after_approval else None,
        "required_user_inputs": ["explicit user approval", "accept_rules=true", "confirm_upload=true", "save_path or path"],
        "blockers": list(dict.fromkeys(item_blockers + [str(check.get("name")) for check in blocked_checks])),
    }


def _candidate_executability_check(name: str, phase: str, ready: bool, required_fields: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "phase": phase,
        "ready": bool(ready),
        "status": "ready" if ready else "blocked",
        "required_fields": required_fields,
        "blocking": True,
    }


def _candidate_executability_phase_summary(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for item in items:
        for check in item.get("checks", []) if isinstance(item.get("checks"), list) else []:
            if not isinstance(check, dict):
                continue
            phase = str(check.get("phase") or "unknown")
            phase_summary = summary.setdefault(phase, {"ready": True, "ready_count": 0, "blocked_count": 0, "blocked_source_ids": [], "missing_checks": []})
            if check.get("ready") is True:
                phase_summary["ready_count"] += 1
            else:
                phase_summary["ready"] = False
                phase_summary["blocked_count"] += 1
                if item.get("source_id"):
                    phase_summary["blocked_source_ids"].append(item.get("source_id"))
                phase_summary["missing_checks"].append(check.get("name"))
            phase_summary["blocked_source_ids"] = list(dict.fromkeys(phase_summary["blocked_source_ids"]))
            phase_summary["missing_checks"] = list(dict.fromkeys(phase_summary["missing_checks"]))
    return summary


def _candidate_executability_next_actions(safe_items: list[dict[str, Any]], next_item: dict[str, Any] | None, blockers: list[str]) -> list[str]:
    if safe_items and not blockers:
        return ["Ask the user to approve candidate_executability_matrix.items[0].submit_request with confirm_upload=true, then call submit_daily_candidate_job."]
    if isinstance(next_item, dict) and next_item.get("first_blocked_check"):
        return [f"Resolve candidate {next_item.get('source_id')} phase {next_item.get('first_blocked_phase')} check {next_item.get('first_blocked_check')} before approval."]
    if blockers:
        return ["Resolve candidate_executability_matrix.blockers before submitting daily candidates."]
    return ["Rerun daily candidate discovery after source cookies and site policies are configured."]


def _candidate_field_completeness(items: list[dict[str, Any]]) -> dict[str, Any]:
    required_fields = [
        "source_tracker",
        "target",
        "source_id",
        "source_url",
        "title",
        "size",
        "published_at",
        "promotion",
        "metadata",
        "duplicate_check",
        "downloadability",
        "recommendation",
        "risk",
        "action",
    ]
    reports = [_candidate_field_completeness_item(item, required_fields) for item in items]
    missing_by_source_id = {str(report["source_id"] or report["rank"]): report["missing_fields"] for report in reports if report["missing_fields"]}
    return {
        "kind": "ptcli.daily_candidate_field_completeness",
        "ready": not missing_by_source_id,
        "required_fields": required_fields,
        "item_count": len(reports),
        "ready_count": sum(1 for report in reports if report["ready"]),
        "missing_count": len(missing_by_source_id),
        "missing_by_source_id": missing_by_source_id,
        "items": reports,
        "continue_when": "candidate_field_completeness.ready=true before publishing the daily candidate digest as complete.",
        "stop_when": ["candidate_field_completeness.missing_by_source_id is non-empty"],
    }


def _candidate_field_completeness_item(item: dict[str, Any], required_fields: list[str]) -> dict[str, Any]:
    card = item.get("publish_card") if isinstance(item.get("publish_card"), dict) else {}
    target = card.get("target") or item.get("target") or _nested_value(item, "submit_request", "target")
    values = {
        "source_tracker": card.get("source_tracker") or item.get("source_tracker"),
        "target": target,
        "source_id": card.get("source_id") or item.get("source_id"),
        "source_url": card.get("source_url") or item.get("source_url"),
        "title": card.get("title") or item.get("title"),
        "size": card.get("size") or item.get("size"),
        "published_at": card.get("published_at") or item.get("published_at"),
        "promotion": card.get("promotion") or item.get("promotion"),
        "metadata": _candidate_metadata_ready(card.get("metadata") if isinstance(card.get("metadata"), dict) else item.get("metadata")),
        "duplicate_check": _nested_bool(card, "duplicate_check", "clear") is True or item.get("duplicate_status") == "not_found",
        "downloadability": _nested_bool(card, "downloadability", "ready") is True or _nested_bool(item, "downloadability_summary", "ready") is True,
        "recommendation": bool(_nested_value(card, "recommendation", "reason") or _nested_value(item, "recommendation", "reason") or item.get("recommended_action")),
        "risk": bool(card.get("risk") if isinstance(card.get("risk"), dict) else item.get("policy_risk_summary") or item.get("decision_summary")),
        "action": bool(card.get("action") if isinstance(card.get("action"), dict) else item.get("submit_request") or item.get("action_endpoint")),
    }
    missing = [field for field in required_fields if not values.get(field)]
    return {
        "rank": item.get("rank"),
        "source_tracker": values["source_tracker"],
        "target": target,
        "source_id": values["source_id"],
        "title": values["title"],
        "ready": not missing,
        "missing_fields": missing,
        "can_submit": item.get("can_submit") is True,
    }


def _candidate_metadata_ready(value: Any) -> bool:
    metadata = value if isinstance(value, dict) else {}
    return bool(metadata.get("ready") or metadata.get("imdb_id") or metadata.get("tmdb_id") or metadata.get("douban_id") or metadata.get("douban_url") or metadata.get("name"))


def _nested_bool(payload: dict[str, Any], *keys: str) -> bool | None:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, bool) else None


def _nested_value(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _candidate_daily_report(
    push_items: list[dict[str, Any] | None],
    approval_queue: dict[str, Any],
    execution_plan: dict[str, Any],
    target_summary: dict[str, Any],
    blockers: list[str],
    *,
    recommendation: str,
) -> dict[str, Any]:
    items = [item for item in push_items if isinstance(item, dict)]
    safe_count = int(approval_queue.get("safe_count") or 0)
    guarded_count = int(approval_queue.get("guarded_count") or 0)
    blocked_count = int(approval_queue.get("blocked_count") or 0)
    target_count = int(target_summary.get("target_count") or DEFAULT_CANDIDATE_LIMIT)
    selected_count = int(target_summary.get("selected_count") or len(items))
    ready_count = int(target_summary.get("ready_count") or 0)
    ready_shortfall_count = max(0, target_count - ready_count)
    selected_shortfall_count = max(0, target_count - selected_count)
    report_blockers = list(dict.fromkeys(_string_list(blockers) + _string_list(execution_plan.get("blockers"))))
    if safe_count and not report_blockers:
        decision = "submit_ready"
        action = "submit_first_safe_candidate_after_user_approval"
    elif ready_shortfall_count:
        decision = "shortfall"
        action = "rerun_or_resolve_candidate_shortfall"
    elif not items:
        decision = "no_candidates"
        action = "rerun_after_source_cookie_policy_config"
    else:
        decision = "blocked"
        action = "resolve_candidate_blockers"
    return {
        "kind": "ptcli.daily_candidate_report",
        "scope": "single_schedule",
        "decision": decision,
        "action": action,
        "recommendation": recommendation,
        "target_count": target_count,
        "scan_count": int(target_summary.get("scan_count") or 0),
        "selected_count": selected_count,
        "ready_count": ready_count,
        "safe_to_submit_count": safe_count,
        "guarded_count": guarded_count,
        "blocked_count": blocked_count,
        "selected_shortfall_count": selected_shortfall_count,
        "ready_shortfall_count": ready_shortfall_count,
        "target_met": bool(target_summary.get("target_met")),
        "ready_target_met": bool(target_summary.get("ready_target_met")),
        "approval_ready": bool(approval_queue.get("ready")),
        "submission_ready": bool(safe_count and not report_blockers),
        "push_ready": bool(items) and not report_blockers,
        "recommended_tool": execution_plan.get("recommended_tool"),
        "recommended_endpoint": execution_plan.get("recommended_endpoint"),
        "recommended_request": execution_plan.get("recommended_request"),
        "first_submit_request": (execution_plan.get("recommended_submit_requests") or [None])[0] if isinstance(execution_plan.get("recommended_submit_requests"), list) else None,
        "shortfall_recovery": execution_plan.get("shortfall_recovery") if isinstance(execution_plan.get("shortfall_recovery"), dict) else {},
        "approval_queue_ref": "digest.approval_queue",
        "execution_plan_ref": "digest.execution_plan",
        "continue_when": "daily_candidate_report.submission_ready=true and user approves first_submit_request",
        "stop_when": ["daily_candidate_report.blockers is not empty", "daily_candidate_report.decision in ['blocked', 'shortfall', 'no_candidates']"],
        "blockers": report_blockers,
        "next_actions": _candidate_daily_report_next_actions(decision, execution_plan, ready_shortfall_count),
    }


def _candidate_batch_report(
    push_items: list[dict[str, Any] | None],
    approval_queue: dict[str, Any],
    execution_plan: dict[str, Any],
    daily_candidate_report: dict[str, Any],
    target_summary: dict[str, Any],
    blockers: list[str],
) -> dict[str, Any]:
    items = [item for item in push_items if isinstance(item, dict)]
    safe_items = approval_queue.get("items") if isinstance(approval_queue.get("items"), list) else []
    first_submit = (execution_plan.get("recommended_submit_requests") or [None])[0] if isinstance(execution_plan.get("recommended_submit_requests"), list) else None
    report_blockers = list(dict.fromkeys(_string_list(blockers) + _string_list(execution_plan.get("blockers")) + _string_list(daily_candidate_report.get("blockers"))))
    required_user_inputs = ["approve candidate", "confirm_upload=true", "save_path or path"]
    return {
        "kind": "ptcli.daily_candidate_batch_report",
        "ready": bool(safe_items and not report_blockers),
        "decision": daily_candidate_report.get("decision"),
        "target_count": int(target_summary.get("target_count") or DEFAULT_CANDIDATE_LIMIT),
        "scan_count": int(target_summary.get("scan_count") or 0),
        "selected_count": int(target_summary.get("selected_count") or len(items)),
        "ready_count": int(target_summary.get("ready_count") or 0),
        "safe_to_submit_count": int(approval_queue.get("safe_count") or 0),
        "guarded_count": int(approval_queue.get("guarded_count") or 0),
        "blocked_count": int(approval_queue.get("blocked_count") or 0),
        "selected_shortfall_count": int(execution_plan.get("selected_shortfall_count") or 0),
        "ready_shortfall_count": int(execution_plan.get("ready_shortfall_count") or 0),
        "target_met": bool(target_summary.get("target_met")),
        "ready_target_met": bool(target_summary.get("ready_target_met")),
        "submission_ready": bool(daily_candidate_report.get("submission_ready")),
        "push_ready": bool(daily_candidate_report.get("push_ready")),
        "approval_ready": bool(approval_queue.get("ready")),
        "first_submit_request": first_submit,
        "recommended_tool": execution_plan.get("recommended_tool"),
        "recommended_endpoint": execution_plan.get("recommended_endpoint"),
        "recommended_method": execution_plan.get("recommended_method"),
        "recommended_request": execution_plan.get("recommended_request"),
        "required_user_inputs": required_user_inputs,
        "safe_to_submit_ids": [item.get("source_id") for item in safe_items if item.get("source_id")],
        "blocked_source_ids": approval_queue.get("blocked_source_ids") if isinstance(approval_queue.get("blocked_source_ids"), list) else [],
        "shortfall_recovery": execution_plan.get("shortfall_recovery") if isinstance(execution_plan.get("shortfall_recovery"), dict) else {},
        "continue_when": "daily_candidate_batch_report.ready=true and user approves first_submit_request with confirm_upload and save_path/path",
        "stop_when": [
            "daily_candidate_batch_report.blockers is not empty",
            "daily_candidate_batch_report.ready=false",
            "first_submit_request is missing",
        ],
        "blockers": report_blockers,
        "next_actions": _candidate_batch_report_next_actions(safe_items, report_blockers, execution_plan),
    }


def _candidate_batch_report_next_actions(safe_items: list[dict[str, Any]], blockers: list[str], execution_plan: dict[str, Any]) -> list[str]:
    if safe_items and not blockers:
        return ["Ask the user to approve daily_candidate_batch_report.first_submit_request, then call submit_daily_candidate_job."]
    if blockers:
        return ["Resolve daily_candidate_batch_report.blockers before submitting daily candidates."]
    return _string_list(execution_plan.get("next_actions"))


def _candidate_control_summary(
    daily_candidate_report: dict[str, Any],
    daily_candidate_batch_report: dict[str, Any],
    approval_queue: dict[str, Any],
    execution_plan: dict[str, Any],
    blockers: list[str],
    *,
    scope: str,
) -> dict[str, Any]:
    report_blockers = list(dict.fromkeys(_string_list(blockers) + _string_list(daily_candidate_report.get("blockers")) + _string_list(daily_candidate_batch_report.get("blockers")) + _string_list(execution_plan.get("blockers"))))
    ready = bool(daily_candidate_batch_report.get("ready")) and not report_blockers
    pending_count = int(daily_candidate_report.get("pending_job_count") or daily_candidate_batch_report.get("pending_job_count") or 0)
    safe_count = int(daily_candidate_batch_report.get("safe_to_submit_count") or approval_queue.get("safe_count") or execution_plan.get("safe_to_submit_count") or 0)
    ready_shortfall_count = int(daily_candidate_batch_report.get("ready_shortfall_count") or daily_candidate_report.get("ready_shortfall_count") or execution_plan.get("ready_shortfall_count") or 0)
    if pending_count:
        action = "poll_candidates"
    elif ready and safe_count:
        action = "submit_candidate"
    elif ready_shortfall_count:
        action = "rerun_daily_candidates"
    elif report_blockers:
        action = "resolve_candidate_blockers"
    else:
        action = "inspect_candidates"
    shortfall_recovery = daily_candidate_batch_report.get("shortfall_recovery") if isinstance(daily_candidate_batch_report.get("shortfall_recovery"), dict) else execution_plan.get("shortfall_recovery") if isinstance(execution_plan.get("shortfall_recovery"), dict) else {}
    recommended_tool = daily_candidate_batch_report.get("recommended_tool") or execution_plan.get("recommended_tool")
    recommended_endpoint = daily_candidate_batch_report.get("recommended_endpoint") or execution_plan.get("recommended_endpoint")
    recommended_method = daily_candidate_batch_report.get("recommended_method") or execution_plan.get("recommended_method") or ("POST" if recommended_endpoint else None)
    recommended_request = daily_candidate_batch_report.get("recommended_request") if daily_candidate_batch_report.get("recommended_request") is not None else execution_plan.get("recommended_request")
    if action == "rerun_daily_candidates" and shortfall_recovery:
        recommended_tool = shortfall_recovery.get("recommended_tool") or recommended_tool
        recommended_endpoint = shortfall_recovery.get("recommended_endpoint") or recommended_endpoint
        recommended_method = shortfall_recovery.get("recommended_method") or recommended_method
        recommended_request = shortfall_recovery.get("recommended_request") or recommended_request
    return {
        "kind": "ptcli.candidate_control_summary",
        "scope": scope,
        "ready": ready,
        "action": action,
        "decision": daily_candidate_report.get("decision"),
        "target_count": daily_candidate_batch_report.get("target_count"),
        "selected_count": daily_candidate_batch_report.get("selected_count"),
        "ready_count": daily_candidate_batch_report.get("ready_count"),
        "safe_to_submit_count": safe_count,
        "pending_job_count": pending_count,
        "ready_shortfall_count": ready_shortfall_count,
        "target_met": bool(daily_candidate_batch_report.get("target_met")),
        "first_submit_request": daily_candidate_batch_report.get("first_submit_request"),
        "recommended_tool": recommended_tool,
        "recommended_endpoint": recommended_endpoint,
        "recommended_method": recommended_method,
        "recommended_request": recommended_request,
        "shortfall_recovery": shortfall_recovery,
        "read_order": ["candidate_control_summary", "daily_candidate_batch_report", "approval_queue", "execution_plan", "push_payload"],
        "continue_when": "candidate_control_summary.action='submit_candidate' and user approves first_submit_request",
        "stop_when": ["candidate_control_summary.blockers is not empty", "candidate_control_summary.action in ['resolve_candidate_blockers', 'inspect_candidates']"],
        "blockers": report_blockers,
        "next_actions": _candidate_control_summary_next_actions(action, daily_candidate_batch_report, execution_plan, shortfall_recovery),
    }


def _candidate_control_summary_next_actions(action: str, daily_candidate_batch_report: dict[str, Any], execution_plan: dict[str, Any], shortfall_recovery: dict[str, Any]) -> list[str]:
    if action == "submit_candidate":
        return ["Ask the user to approve candidate_control_summary.first_submit_request, then call candidate_control_summary.recommended_tool."]
    if action == "poll_candidates":
        return ["Poll candidate job status until candidate_control_summary.pending_job_count is 0."]
    if action == "rerun_daily_candidates":
        if shortfall_recovery.get("recommended_request"):
            return ["Call candidate_control_summary.shortfall_recovery.recommended_tool with recommended_request to fill the daily ready shortfall."]
        return ["Rerun daily candidate discovery after resolving ready shortfall."]
    if action == "resolve_candidate_blockers":
        return ["Resolve candidate_control_summary.blockers before submitting daily candidates."]
    return list(dict.fromkeys(_string_list(daily_candidate_batch_report.get("next_actions")) + _string_list(execution_plan.get("next_actions"))))


def _candidate_daily_report_next_actions(decision: str, execution_plan: dict[str, Any], ready_shortfall_count: int) -> list[str]:
    if decision == "submit_ready":
        return ["Ask the user to approve daily_candidate_report.first_submit_request, then call daily_candidate_report.recommended_tool."]
    if decision == "shortfall":
        return [f"Daily ready candidates are short by {ready_shortfall_count}; rerun discovery or resolve blocked candidates before treating the daily target as met."]
    if decision == "no_candidates":
        return ["Check source cookies, site policy gates, and recent source torrents before rerunning daily candidates."]
    return list(dict.fromkeys(["Resolve daily_candidate_report.blockers before submitting candidates.", *_string_list(execution_plan.get("next_actions"))]))


def _candidate_execution_plan(
    push_items: list[dict[str, Any] | None],
    approval_queue: dict[str, Any],
    target_summary: dict[str, Any],
    blockers: list[str],
    next_actions: list[str],
    *,
    recommendation: str,
    request_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = [item for item in push_items if isinstance(item, dict)]
    submit_items = [item for item in items if item.get("can_submit") is True]
    safe_items = approval_queue.get("items") if isinstance(approval_queue.get("items"), list) else []
    blocked_items = [item for item in items if item.get("can_submit") is not True]
    target_count = int(target_summary.get("target_count") or DEFAULT_CANDIDATE_LIMIT)
    selected_count = int(target_summary.get("selected_count") or len(items))
    ready_count = int(target_summary.get("ready_count") or len(submit_items))
    ready_shortfall = max(0, target_count - ready_count)
    selected_shortfall = max(0, target_count - selected_count)
    request_context = request_context if isinstance(request_context, dict) else {}
    shortfall_recovery = _candidate_shortfall_recovery(target_summary, request_context, ready_shortfall=ready_shortfall, selected_shortfall=selected_shortfall)
    recommended_submit_requests = [_candidate_execution_submit_item(item) for item in safe_items]
    if not recommended_submit_requests:
        recommended_submit_requests = [_candidate_execution_submit_item(item) for item in submit_items[:3]]
    plan_blockers = list(dict.fromkeys(_string_list(blockers) + _candidate_execution_plan_blockers(target_summary, submit_items, safe_items, blocked_items)))
    next_step = _candidate_execution_plan_next_step(recommended_submit_requests, plan_blockers, recommendation, shortfall_recovery)
    return {
        "kind": "ptcli.daily_candidate_execution_plan",
        "ready": bool(recommended_submit_requests and not plan_blockers),
        "recommendation": recommendation,
        "target_count": target_count,
        "selected_count": selected_count,
        "ready_count": ready_count,
        "safe_to_submit_count": len(safe_items),
        "blocked_count": len(blocked_items),
        "selected_shortfall_count": selected_shortfall,
        "ready_shortfall_count": ready_shortfall,
        "target_met": target_summary.get("target_met") is True,
        "ready_target_met": target_summary.get("ready_target_met") is True,
        "request_context": request_context,
        "submit_tool": "submit_daily_candidate_job",
        "submit_endpoint_template": "/v1/jobs/candidates/{candidate_job_id}/submit",
        "source_submit_tool": SOURCE_URL_RETORRENT_JOB_TOOL,
        "source_submit_endpoint": SOURCE_URL_RETORRENT_JOB_ENDPOINT,
        "requires_user_input": ["choose candidate rank/source_id", "confirm_upload=true", "save_path or path"],
        "recommended_submit_requests": recommended_submit_requests,
        "blocked_source_ids": [item.get("source_id") for item in blocked_items if item.get("source_id")],
        "shortfall_recovery": shortfall_recovery,
        "next_step": next_step,
        "recommended_tool": next_step.get("tool"),
        "recommended_endpoint": next_step.get("endpoint"),
        "recommended_method": next_step.get("method"),
        "recommended_request": next_step.get("request"),
        "continue_when": "submitted retorrent job returns job_id, then poll job_handoff/recovery_handoff until complete or blocked",
        "stop_when": ["execution_plan.blockers is not empty", "duplicate_check.exists=true", "policy_risk_level=high", "confirm_upload missing", "save_path/path missing"],
        "blockers": plan_blockers,
        "next_actions": _candidate_execution_plan_next_actions(recommended_submit_requests, ready_shortfall, plan_blockers, next_actions),
    }


def _candidate_execution_submit_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": item.get("rank"),
        "source_tracker": item.get("source_tracker"),
        "source_id": item.get("source_id"),
        "title": item.get("title"),
        "score": item.get("score"),
        "risk_level": item.get("risk_level") or (item.get("decision_summary") or {}).get("risk_level") if isinstance(item.get("decision_summary"), dict) else item.get("risk_level"),
        "policy_risk_level": item.get("policy_risk_level") or (item.get("policy_risk_summary") or {}).get("risk_level") if isinstance(item.get("policy_risk_summary"), dict) else item.get("policy_risk_level"),
        "tool": "submit_daily_candidate_job",
        "endpoint_template": "/v1/jobs/candidates/{candidate_job_id}/submit",
        "request": {
            "rank": item.get("rank"),
            "source_id": item.get("source_id"),
            "confirm_upload": True,
            "save_path": "/downloads",
        },
        "source_url_retorrent_request": item.get("request") or item.get("submit_request"),
    }


def _candidate_execution_plan_blockers(target_summary: dict[str, Any], submit_items: list[dict[str, Any]], safe_items: list[dict[str, Any]], blocked_items: list[dict[str, Any]]) -> list[str]:
    blockers: list[str] = []
    if not submit_items:
        blockers.append("no_submittable_candidates")
    if submit_items and not safe_items:
        blockers.append("no_low_risk_approval_queue_candidates")
    if target_summary.get("ready_target_met") is not True:
        blockers.append("ready_target_shortfall")
    if blocked_items and not submit_items:
        blockers.append("all_candidates_blocked")
    return blockers


def _candidate_discovery_request_context(
    source_tracker: str | None,
    target_trackers: list[str] | None,
    limit: int,
    *,
    accept_rules: bool,
    check_dupes: bool,
    base_dir: str | None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "source_tracker": source_tracker,
        "target_trackers": target_trackers or [],
        "target": ",".join(target_trackers or []),
        "limit": limit,
        "accept_rules": accept_rules,
        "check_dupes": check_dupes,
    }
    if base_dir:
        request["base_dir"] = base_dir
    return request


def _candidate_shortfall_recovery(target_summary: dict[str, Any], request_context: dict[str, Any], *, ready_shortfall: int, selected_shortfall: int) -> dict[str, Any]:
    target_count = int(target_summary.get("target_count") or DEFAULT_CANDIDATE_LIMIT)
    retry_request = {
        key: value
        for key, value in {
            "source_tracker": request_context.get("source_tracker"),
            "target": request_context.get("target"),
            "limit": target_count,
            "accept_rules": request_context.get("accept_rules"),
            "check_dupes": True,
            "base_dir": request_context.get("base_dir"),
        }.items()
        if value is not None and value != ""
    }
    return {
        "kind": "ptcli.daily_candidate_shortfall_recovery",
        "action": "rerun_daily_candidates" if ready_shortfall else "none",
        "reason": "fewer than target_count candidates are ready" if ready_shortfall else "ready target met",
        "source_tracker": request_context.get("source_tracker"),
        "target_trackers": request_context.get("target_trackers") if isinstance(request_context.get("target_trackers"), list) else [],
        "target_count": target_count,
        "selected_shortfall_count": selected_shortfall,
        "ready_shortfall_count": ready_shortfall,
        "scan_count": target_summary.get("scan_count"),
        "max_scan_count": MAX_CANDIDATE_SCAN,
        "recommended_tool": "daily_candidates_job" if ready_shortfall else None,
        "recommended_endpoint": "/v1/jobs/candidates/daily" if ready_shortfall else None,
        "recommended_method": "POST" if ready_shortfall else None,
        "recommended_request": retry_request if ready_shortfall else None,
        "recommended_overrides": {"limit": target_count, "check_dupes": True},
        "continue_when": "shortfall_recovery.ready_shortfall_count=0 and daily_candidate_batch_report.ready=true",
        "stop_when": ["shortfall_recovery.ready_shortfall_count>0 after max_scan_count is exhausted", "site policy or duplicate blockers remain"],
    }


def _candidate_execution_plan_next_step(recommended_submit_requests: list[dict[str, Any]], blockers: list[str], recommendation: str, shortfall_recovery: dict[str, Any] | None = None) -> dict[str, Any]:
    if recommended_submit_requests and not blockers:
        return {
            "tool": "submit_daily_candidate_job",
            "endpoint": "/v1/jobs/candidates/{candidate_job_id}/submit",
            "method": "POST",
            "request": recommended_submit_requests[0]["request"],
            "reason": "submit_first_safe_daily_candidate_after_user_approval",
        }
    if recommendation == "no_candidates":
        reason = "no_candidates_found"
    elif "ready_target_shortfall" in blockers:
        reason = "daily_candidate_ready_shortfall"
    else:
        reason = "resolve_candidate_blockers"
    recovery = shortfall_recovery if isinstance(shortfall_recovery, dict) else {}
    return {
        "tool": recovery.get("recommended_tool") or "daily_candidates_job",
        "endpoint": recovery.get("recommended_endpoint") or "/v1/jobs/candidates/daily",
        "method": recovery.get("recommended_method") or "POST",
        "request": recovery.get("recommended_request") or {"limit": DEFAULT_CANDIDATE_LIMIT, "check_dupes": True},
        "reason": reason,
    }


def _candidate_execution_plan_next_actions(recommended_submit_requests: list[dict[str, Any]], ready_shortfall: int, blockers: list[str], fallback_actions: list[str]) -> list[str]:
    if recommended_submit_requests and not blockers:
        return ["Ask the user to approve execution_plan.recommended_submit_requests[0], fill save_path/path if needed, then call submit_daily_candidate_job."]
    actions: list[str] = []
    if ready_shortfall:
        actions.append(f"Daily ready candidates are short by {ready_shortfall}; rerun candidate discovery or resolve blocked push_items before push/submit.")
    if blockers:
        actions.append("Resolve execution_plan.blockers before submitting daily candidates.")
    actions.extend(_string_list(fallback_actions))
    return list(dict.fromkeys(actions))


def _candidate_approval_queue(push_items: list[dict[str, Any] | None], *, limit: int = 3) -> dict[str, Any]:
    items = [item for item in push_items if isinstance(item, dict)]
    safe_items = [item for item in items if _candidate_item_safe_to_submit(item)]
    guarded_items = [item for item in items if item.get("can_submit") is True and not _candidate_item_safe_to_submit(item)]
    blocked_items = [item for item in items if item.get("can_submit") is not True]
    queue_items = [_candidate_approval_queue_item(item) for item in safe_items[:limit]]
    approval_prompts = [item["approval_prompt"] for item in queue_items if isinstance(item.get("approval_prompt"), dict)]
    next_actions = ["Ask the user to approve one approval_queue.items[] entry, then submit its request to source_url_retorrent_job."]
    if not queue_items and items:
        next_actions = ["Resolve guarded or blocked candidate reasons before submitting any daily candidate."]
    if not items:
        next_actions = ["Run the daily candidate scan again after source cookies and site policies are configured."]
    return {
        "kind": "ptcli.daily_candidate_approval_queue",
        "ready": bool(queue_items),
        "safe_count": len(safe_items),
        "guarded_count": len(guarded_items),
        "blocked_count": len(blocked_items),
        "recommended_count": len(queue_items),
        "items": queue_items,
        "approval_prompts": approval_prompts,
        "first_approval_prompt": approval_prompts[0] if approval_prompts else None,
        "top_safe_candidates": queue_items,
        "guarded_source_ids": [item.get("source_id") for item in guarded_items if item.get("source_id")],
        "blocked_source_ids": [item.get("source_id") for item in blocked_items if item.get("source_id")],
        "submit_tool": SOURCE_URL_RETORRENT_JOB_TOOL,
        "submit_endpoint": SOURCE_URL_RETORRENT_JOB_ENDPOINT,
        "requires_confirmation": ["accept_rules=true", "confirm_upload=true", "save_path or path"],
        "continue_when": "approval_queue.ready=true and the user approves one approval_queue.items[] entry",
        "stop_when": ["duplicate_clear=false", "policy_risk_level=high", "can_submit=false"],
        "next_actions": next_actions,
    }


def _candidate_item_safe_to_submit(item: dict[str, Any]) -> bool:
    decision_summary = item.get("decision_summary") if isinstance(item.get("decision_summary"), dict) else {}
    policy_risk_summary = item.get("policy_risk_summary") if isinstance(item.get("policy_risk_summary"), dict) else {}
    return bool(
        item.get("can_submit") is True
        and decision_summary.get("duplicate_clear") is True
        and policy_risk_summary.get("risk_level") == "low"
        and decision_summary.get("risk_level") == "low"
    )


def _candidate_approval_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    decision_summary = item.get("decision_summary") if isinstance(item.get("decision_summary"), dict) else {}
    policy_risk_summary = item.get("policy_risk_summary") if isinstance(item.get("policy_risk_summary"), dict) else {}
    site_policy_profile_handoff = item.get("site_policy_profile_handoff") if isinstance(item.get("site_policy_profile_handoff"), dict) else {}
    site_policy_summary = item.get("site_policy_summary") if isinstance(item.get("site_policy_summary"), dict) else {}
    approval_prompt = _candidate_approval_prompt(
        rank=item.get("rank"),
        source_tracker=item.get("source_tracker"),
        source_id=item.get("source_id"),
        source_url=item.get("source_url"),
        title=item.get("title"),
        score=item.get("score"),
        metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
        duplicate_clear=decision_summary.get("duplicate_clear"),
        risk_level=decision_summary.get("risk_level"),
        policy_risk_level=policy_risk_summary.get("risk_level"),
        submit_tool=item.get("submit_tool") or SOURCE_URL_RETORRENT_JOB_TOOL,
        submit_endpoint=item.get("submit_job_endpoint") or item.get("action_endpoint") or SOURCE_URL_RETORRENT_JOB_ENDPOINT,
        submit_request=item.get("submit_request") if isinstance(item.get("submit_request"), dict) else None,
        site_policy_profile_handoff=site_policy_profile_handoff,
        site_policy_summary=site_policy_summary,
        blockers=_string_list(item.get("blockers")),
    )
    return {
        "rank": item.get("rank"),
        "source_tracker": item.get("source_tracker"),
        "source_id": item.get("source_id"),
        "source_url": item.get("source_url"),
        "title": item.get("title"),
        "score": item.get("score"),
        "risk_level": decision_summary.get("risk_level"),
        "policy_risk_level": policy_risk_summary.get("risk_level"),
        "execution_priority": policy_risk_summary.get("execution_priority"),
        "duplicate_clear": decision_summary.get("duplicate_clear"),
        "metadata": item.get("metadata"),
        "publish_card": item.get("publish_card"),
        "policy_risk_summary": policy_risk_summary,
        "site_policy_profile_handoff": site_policy_profile_handoff,
        "site_policy_summary": site_policy_summary,
        "submit_tool": item.get("submit_tool") or SOURCE_URL_RETORRENT_JOB_TOOL,
        "submit_endpoint": item.get("submit_job_endpoint") or item.get("action_endpoint") or SOURCE_URL_RETORRENT_JOB_ENDPOINT,
        "request": item.get("submit_request"),
        "approval_prompt": approval_prompt,
        "requires_confirmation": ["accept_rules=true", "confirm_upload=true", "save_path or path"],
    }


def _candidate_approval_prompt(
    *,
    rank: Any,
    source_tracker: Any,
    source_id: Any,
    source_url: Any,
    title: Any,
    score: Any,
    metadata: dict[str, Any],
    duplicate_clear: Any,
    risk_level: Any,
    policy_risk_level: Any,
    submit_tool: Any,
    submit_endpoint: Any,
    submit_request: dict[str, Any] | None,
    blockers: list[str],
    site_policy_profile_handoff: dict[str, Any] | None = None,
    site_policy_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ready = bool(submit_request and duplicate_clear is True and risk_level == "low" and policy_risk_level == "low" and not blockers)
    label = f"#{rank} {source_tracker}-{source_id}"
    title_text = str(title or "").strip()
    score_text = f", score {score}" if score is not None else ""
    approval_text = f"Approve daily PT retorrent candidate {label}{score_text}: {title_text}".strip()
    return {
        "kind": "ptcli.daily_candidate_approval_prompt",
        "ready": ready,
        "rank": rank,
        "source_tracker": source_tracker,
        "source_id": source_id,
        "source_url": source_url,
        "title": title,
        "score": score,
        "metadata": {
            "imdb_id": metadata.get("imdb_id"),
            "tmdb_id": metadata.get("tmdb_id"),
            "douban_id": metadata.get("douban_id"),
            "douban_url": metadata.get("douban_url"),
        },
        "duplicate_clear": duplicate_clear,
        "risk_level": risk_level,
        "policy_risk_level": policy_risk_level,
        "site_policy_profile_handoff": site_policy_profile_handoff if isinstance(site_policy_profile_handoff, dict) else {},
        "site_policy_summary": site_policy_summary if isinstance(site_policy_summary, dict) else {},
        "approval_text": approval_text,
        "confirm_phrase": f"Approve {source_tracker}-{source_id} to retorrent after rule review",
        "submit_tool": submit_tool,
        "submit_endpoint": submit_endpoint,
        "submit_request": submit_request if ready else None,
        "required_confirmations": ["explicit user approval", "accept_rules=true", "confirm_upload=true", "save_path or path"],
        "safety": {
            "mutates_state_after_submit": True,
            "live_upload_after_submit": True,
            "does_not_submit_from_prompt": True,
            "requires_human_approval": True,
            "stop_on_duplicate": True,
            "respect_site_rules": True,
        },
        "continue_when": "user explicitly approves approval_prompt.confirm_phrase and required_confirmations are satisfied",
        "stop_when": ["approval_prompt.ready=false", "duplicate_clear=false", "risk_level!='low'", "policy_risk_level!='low'", "blockers is non-empty"],
        "blockers": blockers,
        "next_actions": ["Show approval_prompt.approval_text to the user; after approval, call approval_prompt.submit_tool with approval_prompt.submit_request."] if ready else ["Resolve approval_prompt.blockers before asking the user to approve this candidate."],
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
    policy_risk_summary = candidate.get("policy_risk_summary") if isinstance(candidate.get("policy_risk_summary"), dict) else {}
    decision_summary = candidate.get("decision_summary") if isinstance(candidate.get("decision_summary"), dict) else {}
    downloadability_summary = candidate.get("downloadability_summary") if isinstance(candidate.get("downloadability_summary"), dict) else {}
    blockers = _string_list(candidate.get("blockers"))
    status = str(candidate.get("status") or "")
    title = source.get("title") or source_info.get("name")
    submit_request = candidate.get("submit_request") if isinstance(candidate.get("submit_request"), dict) else None
    can_submit = bool(status == "ready" and submit_request)
    action_label = "submit_when_confirmed" if can_submit else "review_blockers"
    metadata = {
        "imdb_id": source_info.get("imdb_id"),
        "tmdb_id": source_info.get("tmdb_id"),
        "douban_id": source_info.get("douban_id"),
        "douban_url": source_info.get("douban_url"),
        "name": source_info.get("name"),
    }
    policy_digest = _candidate_digest_policy_summary(policy_summary)
    policy_execution_handoff = policy_digest.get("policy_execution_handoff") if isinstance(policy_digest.get("policy_execution_handoff"), dict) else {}
    site_policy_profile_handoff = policy_digest.get("site_policy_profile_handoff") if isinstance(policy_digest.get("site_policy_profile_handoff"), dict) else {}
    site_policy_summary = policy_digest.get("site_policy_summary") if isinstance(policy_digest.get("site_policy_summary"), dict) else {}
    approval_prompt = _candidate_approval_prompt(
        rank=rank,
        source_tracker=source.get("tracker"),
        source_id=source.get("torrent_id"),
        source_url=source.get("details_url"),
        title=title,
        score=ranking.get("score"),
        metadata=metadata,
        duplicate_clear=decision_summary.get("duplicate_clear"),
        risk_level=decision_summary.get("risk_level"),
        policy_risk_level=policy_risk_summary.get("risk_level"),
        submit_tool=candidate.get("submit_tool"),
        submit_endpoint=candidate.get("submit_job_endpoint"),
        submit_request=submit_request if can_submit else None,
        site_policy_profile_handoff=site_policy_profile_handoff,
        site_policy_summary=site_policy_summary,
        blockers=blockers,
    )
    publish_card = _candidate_publish_card(
        rank=rank,
        status=status,
        can_submit=can_submit,
        title=title,
        source=source,
        metadata=metadata,
        duplicate_check=duplicate_check,
        ranking=ranking,
        recommendation=candidate.get("recommendation") if isinstance(candidate.get("recommendation"), dict) else {},
        decision_summary=decision_summary,
        policy_risk_summary=policy_risk_summary,
        blockers=blockers,
        workflow=workflow,
        submit_request=submit_request,
        submit_endpoint=candidate.get("submit_job_endpoint"),
        submit_tool=candidate.get("submit_tool"),
        approval_prompt=approval_prompt,
        downloadability_summary=downloadability_summary,
        site_policy_profile_handoff=site_policy_profile_handoff,
        site_policy_summary=site_policy_summary,
    )
    return {
        "rank": rank,
        "status": status,
        "score": ranking.get("score"),
        "tier": ranking.get("tier"),
        "candidate_discovery_profile": candidate.get("candidate_discovery_profile") if isinstance(candidate.get("candidate_discovery_profile"), dict) else {},
        "source_tracker": source.get("tracker"),
        "target": submit_request.get("target") if isinstance(submit_request, dict) else None,
        "source_id": source.get("torrent_id"),
        "source_url": source.get("details_url"),
        "title": title,
        "size": source.get("size"),
        "published_at": source.get("published_at"),
        "promotion": source.get("promotion"),
        "metadata": metadata,
        "duplicate_status": duplicate_check.get("status"),
        "duplicate_count": duplicate_check.get("count"),
        "downloadability_summary": downloadability_summary,
        "publish_card": publish_card,
        "decision_summary": decision_summary,
        "audit_summary": _candidate_audit_summary(
            rank=rank,
            status=status,
            can_submit=can_submit,
            source=source,
            metadata=metadata,
            duplicate_check=duplicate_check,
            policy_summary=policy_digest,
            decision_summary=decision_summary,
            policy_risk_summary=policy_risk_summary,
            blockers=blockers,
            workflow=workflow,
            submit_request=submit_request,
            submit_endpoint=candidate.get("submit_job_endpoint"),
            submit_tool=candidate.get("submit_tool"),
            downloadability_summary=downloadability_summary,
        ),
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
        "policy_summary": policy_digest,
        "policy_risk_summary": policy_risk_summary,
        "policy_execution_handoff": policy_execution_handoff,
        "site_policy_profile_handoff": site_policy_profile_handoff,
        "site_policy_summary": site_policy_summary,
        "approval_prompt": approval_prompt,
        "submit_request": submit_request if can_submit else None,
        "submit_job_endpoint": candidate.get("submit_job_endpoint"),
        "submit_tool": candidate.get("submit_tool"),
    }


def _candidate_publish_card(
    *,
    rank: int,
    status: str,
    can_submit: bool,
    title: Any,
    source: dict[str, Any],
    metadata: dict[str, Any],
    duplicate_check: dict[str, Any],
    ranking: dict[str, Any],
    recommendation: dict[str, Any],
    decision_summary: dict[str, Any],
    policy_risk_summary: dict[str, Any],
    blockers: list[str],
    workflow: dict[str, Any],
    submit_request: dict[str, Any] | None,
    submit_endpoint: Any,
    submit_tool: Any,
    approval_prompt: dict[str, Any],
    downloadability_summary: dict[str, Any],
    site_policy_profile_handoff: dict[str, Any],
    site_policy_summary: dict[str, Any],
) -> dict[str, Any]:
    metadata_ready = bool([value for value in metadata.values() if value])
    duplicate_clear = duplicate_check.get("searched") is True and duplicate_check.get("exists") is False
    promotion = source.get("promotion")
    return {
        "kind": "ptcli.daily_candidate_publish_card",
        "rank": rank,
        "status": status,
        "source_tracker": source.get("tracker"),
        "target": submit_request.get("target") if isinstance(submit_request, dict) else None,
        "source_id": source.get("torrent_id"),
        "source_url": source.get("details_url"),
        "title": title,
        "size": source.get("size"),
        "published_at": source.get("published_at"),
        "promotion": promotion,
        "freeleech_like": _promotion_is_free(promotion),
        "metadata": {
            "ready": metadata_ready,
            "imdb_id": metadata.get("imdb_id"),
            "tmdb_id": metadata.get("tmdb_id"),
            "douban_id": metadata.get("douban_id"),
            "douban_url": metadata.get("douban_url"),
            "name": metadata.get("name"),
            "missing": [key for key in ("imdb_id", "tmdb_id", "douban_id") if not metadata.get(key)],
        },
        "duplicate_check": {
            "searched": duplicate_check.get("searched"),
            "status": duplicate_check.get("status"),
            "exists": duplicate_check.get("exists"),
            "count": duplicate_check.get("count"),
            "clear": duplicate_clear,
            "dupes": duplicate_check.get("dupes") if isinstance(duplicate_check.get("dupes"), list) else [],
        },
        "downloadability": downloadability_summary,
        "site_policy_profile_handoff": site_policy_profile_handoff,
        "site_policy_summary": site_policy_summary,
        "recommendation": {
            "recommended": recommendation.get("recommended"),
            "score": ranking.get("score"),
            "tier": ranking.get("tier"),
            "reason": recommendation.get("reason"),
            "reasons": _string_list(ranking.get("reasons")),
            "penalties": _string_list(ranking.get("penalties")),
        },
        "risk": {
            "level": decision_summary.get("risk_level"),
            "policy_level": policy_risk_summary.get("risk_level"),
            "execution_priority": policy_risk_summary.get("execution_priority"),
            "blocker_count": len(blockers),
            "primary_blocker": blockers[0] if blockers else None,
            "blockers": blockers,
        },
        "action": {
            "decision": decision_summary.get("action") or workflow.get("decision"),
            "can_submit": can_submit,
            "tool": submit_tool,
            "endpoint": submit_endpoint,
            "request": submit_request if can_submit else None,
            "approval_prompt": approval_prompt,
            "required_user_inputs": ["accept_rules=true", "confirm_upload=true", "save_path or path"],
            "next_actions": workflow.get("recommended_action"),
        },
    }


def _candidate_audit_summary(
    *,
    rank: int,
    status: str,
    can_submit: bool,
    source: dict[str, Any],
    metadata: dict[str, Any],
    duplicate_check: dict[str, Any],
    policy_summary: dict[str, Any],
    decision_summary: dict[str, Any],
    policy_risk_summary: dict[str, Any],
    blockers: list[str],
    workflow: dict[str, Any],
    submit_request: dict[str, Any] | None,
    submit_endpoint: Any,
    submit_tool: Any,
    downloadability_summary: dict[str, Any],
) -> dict[str, Any]:
    duplicate_clear = duplicate_check.get("searched") is True and duplicate_check.get("exists") is False
    qbit_limits = policy_summary.get("qbit_limits") if isinstance(policy_summary.get("qbit_limits"), dict) else {}
    return {
        "rank": rank,
        "status": status,
        "can_submit": can_submit,
        "action": decision_summary.get("action") or workflow.get("decision"),
        "risk_level": decision_summary.get("risk_level"),
        "source": {
            "tracker": source.get("tracker"),
            "torrent_id": source.get("torrent_id"),
            "url": source.get("details_url"),
            "title": source.get("title"),
            "size": source.get("size"),
            "published_at": source.get("published_at"),
            "promotion": source.get("promotion"),
        },
        "metadata": {
            "ready": bool([value for value in metadata.values() if value]),
            "imdb_id": metadata.get("imdb_id"),
            "tmdb_id": metadata.get("tmdb_id"),
            "douban_id": metadata.get("douban_id"),
            "douban_url": metadata.get("douban_url"),
            "name": metadata.get("name"),
            "missing": [key for key in ("imdb_id", "tmdb_id", "douban_id") if not metadata.get(key)],
        },
        "duplicate_check": {
            "searched": duplicate_check.get("searched"),
            "status": duplicate_check.get("status"),
            "exists": duplicate_check.get("exists"),
            "count": duplicate_check.get("count"),
            "clear": duplicate_clear,
        },
        "downloadability": downloadability_summary,
        "policy": {
            "ready": (policy_summary.get("policy_coverage") or {}).get("ready") if isinstance(policy_summary.get("policy_coverage"), dict) else None,
            "manual_review_ready": policy_summary.get("manual_review_ready"),
            "rule_obligations_ready": (policy_summary.get("policy_coverage") or {}).get("rule_obligations_ready") if isinstance(policy_summary.get("policy_coverage"), dict) else None,
            "qbit_limits": qbit_limits,
            "seeding_requirements": policy_summary.get("seeding_requirements"),
            "rules": policy_summary.get("rules"),
            "policy_execution_handoff": policy_summary.get("policy_execution_handoff") if isinstance(policy_summary.get("policy_execution_handoff"), dict) else {},
            "policy_risk_summary": policy_risk_summary,
        },
        "submit": {
            "tool": submit_tool,
            "endpoint": submit_endpoint,
            "request": submit_request if can_submit else None,
            "requires": workflow.get("requires") if isinstance(workflow.get("requires"), list) else ["accept_rules=true", "confirm_upload=true", "save_path or path"],
        },
        "blockers": blockers,
        "primary_blocker": blockers[0] if blockers else None,
        "recommended_action": workflow.get("recommended_action"),
    }


def _candidate_decision_summary(
    seed: CandidateSeed,
    source_info: dict[str, Any] | None,
    duplicate_check: dict[str, Any],
    blockers: list[str],
    ranking: dict[str, Any],
    policy_summary: dict[str, Any],
    status: str,
    downloadability_summary: dict[str, Any],
) -> dict[str, Any]:
    metadata = {
        "imdb_id": source_info.get("imdb_id") if source_info else None,
        "tmdb_id": source_info.get("tmdb_id") if source_info else None,
        "douban_id": source_info.get("douban_id") if source_info else None,
        "douban_url": source_info.get("douban_url") if source_info else None,
        "name": source_info.get("name") if source_info else None,
    }
    metadata_keys = [key for key, value in metadata.items() if value]
    policy_coverage = policy_summary.get("policy_coverage") if isinstance(policy_summary.get("policy_coverage"), dict) else {}
    duplicate_status = str(duplicate_check.get("status") or "unknown")
    duplicate_clear = duplicate_check.get("searched") is True and duplicate_check.get("exists") is False
    can_submit = bool(status == "ready" and not blockers)
    if can_submit:
        action = "submit_when_confirmed"
    elif duplicate_check.get("exists") is True:
        action = "stop_duplicate"
    elif policy_coverage.get("ready") is False or any("rule_review_fingerprint" in blocker for blocker in blockers):
        action = "configure_policy"
    else:
        action = "review_blockers"
    return {
        "action": action,
        "can_submit": can_submit,
        "risk_level": _candidate_risk_level(blockers, duplicate_status, bool(metadata_keys), policy_coverage.get("ready")),
        "score": ranking.get("score"),
        "tier": ranking.get("tier"),
        "metadata_ready": bool(metadata_keys),
        "metadata_keys": metadata_keys,
        "metadata": metadata,
        "downloadability_ready": downloadability_summary.get("ready"),
        "downloadable": downloadability_summary.get("downloadable"),
        "download_cookie_status": (downloadability_summary.get("cookie") or {}).get("status") if isinstance(downloadability_summary.get("cookie"), dict) else None,
        "source_download_adapter": downloadability_summary.get("source_download_adapter"),
        "promotion": seed.promotion,
        "freeleech_like": _promotion_is_free(seed.promotion),
        "duplicate_status": duplicate_status,
        "duplicate_clear": duplicate_clear,
        "duplicate_count": duplicate_check.get("count"),
        "policy_coverage_ready": policy_coverage.get("ready"),
        "manual_review_ready": policy_summary.get("manual_review_ready"),
        "policy_risk_level": (policy_summary.get("policy_risk_summary") or {}).get("risk_level") if isinstance(policy_summary.get("policy_risk_summary"), dict) else None,
        "blocker_count": len(blockers),
        "primary_blocker": blockers[0] if blockers else None,
        "reasons": _string_list(ranking.get("reasons")),
        "penalties": _string_list(ranking.get("penalties")),
    }


def _candidate_risk_level(blockers: list[str], duplicate_status: str, metadata_ready: bool, policy_ready: Any) -> str:
    if any("target-duplicate" in blocker for blocker in blockers) or duplicate_status == "exists":
        return "high"
    if blockers or policy_ready is False or not metadata_ready:
        return "medium"
    return "low"


def _push_decision_summary(items: list[dict[str, Any]], ready_items: list[dict[str, Any]], blocked_items: list[dict[str, Any]], recommendation: str) -> dict[str, Any]:
    review_items = [item for item in items if item.get("tier") == "review"]
    risk_counts = {"low": 0, "medium": 0, "high": 0}
    policy_risk_counts = {"low": 0, "medium": 0, "high": 0}
    for item in items:
        summary = item.get("decision_summary") if isinstance(item.get("decision_summary"), dict) else {}
        risk = str(summary.get("risk_level") or "medium")
        if risk not in risk_counts:
            risk = "medium"
        risk_counts[risk] += 1
        policy_summary = item.get("policy_risk_summary") if isinstance(item.get("policy_risk_summary"), dict) else {}
        policy_risk = str(policy_summary.get("risk_level") or "medium")
        if policy_risk not in policy_risk_counts:
            policy_risk = "medium"
        policy_risk_counts[policy_risk] += 1
    return {
        "recommendation": recommendation,
        "top_action": ready_items[0].get("decision_summary", {}).get("action") if ready_items and isinstance(ready_items[0].get("decision_summary"), dict) else "review_blockers" if items else "no_candidates",
        "ready_count": len(ready_items),
        "review_count": len(review_items),
        "blocked_count": len(blocked_items),
        "risk_counts": risk_counts,
        "policy_risk_counts": policy_risk_counts,
        "safe_to_submit_count": sum(1 for item in items if _candidate_item_safe_to_submit(item)),
        "ready_source_ids": [item.get("source_id") for item in ready_items if item.get("source_id")],
        "blocked_source_ids": [item.get("source_id") for item in blocked_items if item.get("source_id")],
        "submit_ready": bool(ready_items),
    }


def _candidate_target_summary(limit: int, *, scan_count: int | None, selected_count: int, ready_count: int) -> dict[str, Any]:
    target_count = max(1, min(int(limit or DEFAULT_CANDIDATE_LIMIT), DEFAULT_CANDIDATE_LIMIT))
    selected_count = max(0, int(selected_count or 0))
    ready_count = max(0, int(ready_count or 0))
    scan_total = selected_count if scan_count is None else max(0, int(scan_count or 0))
    shortfall = max(0, target_count - selected_count)
    ready_shortfall = max(0, target_count - ready_count)
    return {
        "target_count": target_count,
        "scan_count": scan_total,
        "selected_count": selected_count,
        "ready_count": ready_count,
        "shortfall_count": shortfall,
        "ready_shortfall_count": ready_shortfall,
        "target_met": selected_count >= target_count,
        "ready_target_met": ready_count >= target_count,
        "exhausted_scan": scan_total < target_count or selected_count < target_count,
    }


def _candidate_push_summary(target_summary: dict[str, Any], review_count: int, blocked_count: int, recommendation: str) -> str:
    selected_count = int(target_summary.get("selected_count") or 0)
    ready_count = int(target_summary.get("ready_count") or 0)
    target_count = int(target_summary.get("target_count") or DEFAULT_CANDIDATE_LIMIT)
    shortfall_count = int(target_summary.get("shortfall_count") or 0)
    if selected_count <= 0:
        return f"No daily retorrent candidates are available yet; target is {target_count}."
    shortfall_text = f", shortfall {shortfall_count}" if shortfall_count else ""
    return f"{selected_count}/{target_count} candidate(s): {ready_count} ready, {review_count} need review, {blocked_count} blocked{shortfall_text}. Recommendation: {recommendation}."


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
        "transfer_rules": policy_summary.get("transfer_rules"),
        "rules": policy_summary.get("rules"),
        "policy_execution_handoff": policy_summary.get("policy_execution_handoff") if isinstance(policy_summary.get("policy_execution_handoff"), dict) else {},
        "site_policy_profile_handoff": policy_summary.get("site_policy_profile_handoff") if isinstance(policy_summary.get("site_policy_profile_handoff"), dict) else {},
        "site_policy_summary": policy_summary.get("site_policy_summary") if isinstance(policy_summary.get("site_policy_summary"), dict) else {},
        "policy_risk_summary": policy_summary.get("policy_risk_summary") if isinstance(policy_summary.get("policy_risk_summary"), dict) else {},
    }


def _candidate_tier(candidate: dict[str, Any]) -> str:
    ranking = candidate.get("ranking") if isinstance(candidate.get("ranking"), dict) else {}
    return str(ranking.get("tier") or candidate.get("status") or "")


def _blocked_payload(source: str, targets: list[str], limit: int, blockers: list[str]) -> dict[str, Any]:
    source_capability = _source_candidate_capability(source, limit=limit)
    discovery_handoff = _candidate_discovery_handoff(source_capability, targets, limit=limit)
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
        "target_count": limit,
        "scan_count": 0,
        "shortfall_count": limit,
        "target_met": False,
        "target_summary": _candidate_target_summary(limit, scan_count=0, selected_count=0, ready_count=0),
        "source_capability": source_capability,
        "candidate_discovery_handoff": discovery_handoff,
        "digest": _candidate_digest([], blockers, [], limit=limit, scan_count=0, source_tracker=source, target_trackers=targets, discovery_handoff=discovery_handoff),
        "candidates": [],
        "blockers": blockers,
        "next_actions": [],
    }


def _source_candidate_capability(source: str, *, base_dir: str | None = None, limit: int = MAX_CANDIDATE_SCAN) -> dict[str, Any]:
    normalized = normalize_tracker(source) if source else source
    adapter = _candidate_discovery_adapter(normalized)
    info_adapter = source_info_adapter(normalized)
    download_adapter = source_download_adapter(normalized)
    cookie_path = _cookie_path(normalized, base_dir) if normalized in GENERIC_DETAILS_BASE_URLS else None
    blockers: list[str] = []
    if not adapter:
        blockers.append(f"{normalized} candidate discovery adapter is not enabled.")
    if not info_adapter:
        blockers.append(f"{normalized} source info adapter is not enabled.")
    if not download_adapter:
        blockers.append(f"{normalized} source download adapter is not enabled.")
    return {
        "source_tracker": source,
        "ready": not blockers,
        "source_info_adapter": info_adapter,
        "source_download_adapter": download_adapter,
        "candidate_discovery_adapter": adapter,
        "implementation": "generic_recent_cookie" if adapter == "nexusphp_recent_or_search_html" else adapter,
        "network_mode": "live_recent_listing_with_cookie" if adapter == "nexusphp_recent_or_search_html" else "not_enabled",
        "scan": {
            "limit": max(1, min(int(limit or MAX_CANDIDATE_SCAN), MAX_CANDIDATE_SCAN)),
            "max_limit": MAX_CANDIDATE_SCAN,
            "recent_url": _recent_url(normalized) if normalized in GENERIC_DETAILS_BASE_URLS else None,
            "pagination": "first_recent_page",
        },
        "credentials": {
            "cookie_required": normalized in GENERIC_DETAILS_BASE_URLS and normalized not in MTEAM_API_TRACKERS,
            "cookie_path": cookie_path,
        },
        "required_seed_outputs": ["source_id", "title", "details_url", "size", "published_at", "promotion", "seeders", "leechers"],
        "required_enrichment_outputs": ["imdb_id", "tmdb_id", "douban_id", "name", "description"],
        "safety": {
            "does_not_upload": True,
            "does_not_download_torrent": True,
            "duplicate_check_required_before_submit": True,
            "rules_not_inferred": True,
            "live_submit_requires_confirm_upload": True,
        },
        "blockers": blockers,
    }


def _candidate_discovery_adapter(source: str) -> str | None:
    if source in MTEAM_API_TRACKERS:
        return None
    if source in GENERIC_DETAILS_BASE_URLS:
        return "nexusphp_recent_or_search_html"
    return None


def _candidate_discovery_handoff(source_capability: dict[str, Any], targets: list[str], *, limit: int) -> dict[str, Any]:
    ready = bool(source_capability.get("ready"))
    blockers = _string_list(source_capability.get("blockers"))
    return {
        "kind": "ptcli.daily_candidate_discovery_handoff",
        "ready": ready,
        "source_tracker": source_capability.get("source_tracker"),
        "target_trackers": targets,
        "target_count": max(1, min(int(limit or DEFAULT_CANDIDATE_LIMIT), DEFAULT_CANDIDATE_LIMIT)),
        "adapter": source_capability.get("candidate_discovery_adapter"),
        "implementation": source_capability.get("implementation"),
        "network_mode": source_capability.get("network_mode"),
        "scan": source_capability.get("scan"),
        "credentials": source_capability.get("credentials"),
        "required_seed_outputs": source_capability.get("required_seed_outputs"),
        "required_enrichment_outputs": source_capability.get("required_enrichment_outputs"),
        "candidate_filters": ["source policy gate", "target duplicate check", "downloadability gate", "metadata availability", "ranking score"],
        "safe_to_push_when": "candidate_discovery_handoff.ready=true and digest.push_payload.candidate_field_completeness.ready=true",
        "safe_to_submit_when": "candidate.status=ready and candidate.submit_request is reviewed with confirm_upload=true",
        "extension_contract": {
            "api_tracker": "implement recent/search API discovery that returns CandidateSeed-compatible fields",
            "nexusphp_tracker": "provide details base URL, recent/search path, cookie auth, and parser coverage for torrent id/title/size/promotion/date",
            "target_tracker": "provide duplicate check before any upload submission",
        },
        "safety": source_capability.get("safety"),
        "blockers": blockers,
        "next_actions": _candidate_discovery_next_actions(ready, blockers, source_capability),
    }


def _candidate_discovery_next_actions(ready: bool, blockers: list[str], source_capability: dict[str, Any]) -> list[str]:
    if ready:
        return ["Run daily candidate discovery, then review digest.candidate_executability_matrix before submitting any candidate."]
    actions = [f"Resolve candidate_discovery_handoff.blockers before relying on daily candidate automation ({len(blockers)} blocker(s))."]
    credentials = source_capability.get("credentials") if isinstance(source_capability.get("credentials"), dict) else {}
    cookie_path = credentials.get("cookie_path")
    if cookie_path:
        actions.append(f"Refresh the source cookie at {cookie_path}.")
    return actions


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
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if value in (None, ""):
        return []
    return [str(value)]


def _promotion_is_free(promotion: str | None) -> bool:
    return bool(promotion and re.search(r"\b(free|2x|50%|免费|免費|限免|freeleech)\b", promotion, flags=re.IGNORECASE))


def _promotion_matches_any(promotion: str | None, required_promotions: list[str]) -> bool:
    lower = str(promotion or "").lower()
    return any(str(required).lower() in lower for required in required_promotions)


def _release_group_from_title(title: str | None) -> str | None:
    if not title:
        return None
    match = re.search(r"[-@]([A-Za-z0-9][A-Za-z0-9._-]{1,24})\s*$", title.strip())
    return match.group(1) if match else None


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
