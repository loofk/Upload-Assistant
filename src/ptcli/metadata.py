"""Metadata enrichment helpers for ptcli retorrent flows."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx

METADATA_KEYS = ("imdb_id", "tmdb_id", "douban_id", "douban_url")


async def enrich_source_metadata(
    config: dict[str, Any],
    source_info: dict[str, Any],
    *,
    overrides: dict[str, Any] | None = None,
    fetch_ptgen: bool = False,
    base_dir: str | None = None,
) -> dict[str, Any]:
    base = dict(source_info)
    applied: dict[str, Any] = {}
    blockers: list[str] = []
    sources: list[str] = []
    field_sources = _initial_metadata_field_sources(base)
    override_values = normalize_metadata_overrides(overrides or {})
    for key, value in override_values.items():
        if value and not base.get(key):
            base[key] = value
            applied[key] = value
            field_sources[key] = "overrides"
    if applied:
        sources.append("overrides")

    if base.get("imdb_id") and not base.get("tmdb_id"):
        tmdb_result = await _tmdb_from_imdb(config, base.get("imdb_id"))
        if tmdb_result.get("tmdb_id"):
            base["tmdb_id"] = tmdb_result["tmdb_id"]
            applied["tmdb_id"] = tmdb_result["tmdb_id"]
            field_sources["tmdb_id"] = "tmdb_api"
            sources.append("tmdb_api")
        elif tmdb_result.get("blocker"):
            blockers.append(str(tmdb_result["blocker"]))

    if base.get("douban_id") and not base.get("douban_url"):
        base["douban_url"] = f"https://movie.douban.com/subject/{base['douban_id']}/"
        applied["douban_url"] = base["douban_url"]
        field_sources["douban_url"] = field_sources.get("douban_id") or "derived"
    if base.get("douban_url") and not base.get("douban_id"):
        douban_id = _normalize_douban_id(base.get("douban_url"))
        if douban_id:
            base["douban_id"] = douban_id
            applied["douban_id"] = douban_id
            field_sources["douban_id"] = field_sources.get("douban_url") or "derived"

    if fetch_ptgen:
        ptgen_result = await _ptgen_from_metadata(config, base, base_dir=base_dir)
        if ptgen_result.get("description"):
            base["ptgen_description"] = ptgen_result["description"]
            applied["ptgen_description"] = {"length": len(str(ptgen_result["description"]))}
            field_sources["ptgen_description"] = "ptgen"
            sources.append("ptgen")
        if ptgen_result.get("ptgen"):
            base["ptgen"] = ptgen_result["ptgen"]
        if ptgen_result.get("douban_id") and not base.get("douban_id"):
            base["douban_id"] = ptgen_result["douban_id"]
            applied["douban_id"] = ptgen_result["douban_id"]
            field_sources["douban_id"] = "ptgen"
        if ptgen_result.get("douban_url") and not base.get("douban_url"):
            base["douban_url"] = ptgen_result["douban_url"]
            applied["douban_url"] = ptgen_result["douban_url"]
            field_sources["douban_url"] = "ptgen"
        if ptgen_result.get("blocker"):
            blockers.append(str(ptgen_result["blocker"]))

    missing = [key for key in METADATA_KEYS if not base.get(key)]
    return {
        "status": "enriched" if applied else "unchanged",
        "ready": not missing and (not fetch_ptgen or bool(base.get("ptgen_description"))),
        "source_info": base,
        "applied": applied,
        "missing": missing,
        "readiness": _metadata_readiness(base, field_sources, fetch_ptgen=fetch_ptgen),
        "sources": sources,
        "blockers": blockers,
    }


def _initial_metadata_field_sources(source_info: dict[str, Any]) -> dict[str, str]:
    field_sources = {key: "source" for key in METADATA_KEYS if source_info.get(key)}
    if source_info.get("ptgen_description"):
        field_sources["ptgen_description"] = "source"
    return field_sources


def _metadata_readiness(source_info: dict[str, Any], field_sources: dict[str, str], *, fetch_ptgen: bool) -> dict[str, Any]:
    keys = (*METADATA_KEYS, "ptgen_description")
    return {
        key: {
            "ready": bool(source_info.get(key)),
            "required": key != "ptgen_description" or fetch_ptgen,
            "source": field_sources.get(key),
        }
        for key in keys
    }


def load_metadata_overrides(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("metadata override file must contain a JSON object")
    return normalize_metadata_overrides(payload)


def normalize_metadata_overrides(payload: dict[str, Any]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    imdb_id = _normalize_int(payload.get("imdb_id") or payload.get("imdb") or payload.get("imdbID"))
    tmdb_id = _normalize_int(payload.get("tmdb_id") or payload.get("tmdb"))
    douban_id = _normalize_douban_id(payload.get("douban_id") or payload.get("douban"))
    douban_url = _normalize_douban_url(payload.get("douban_url") or payload.get("douban"))
    if not douban_id and douban_url:
        douban_id = _normalize_douban_id(douban_url)
    if imdb_id:
        overrides["imdb_id"] = imdb_id
    if tmdb_id:
        overrides["tmdb_id"] = tmdb_id
    if douban_id:
        overrides["douban_id"] = douban_id
    if douban_url:
        overrides["douban_url"] = douban_url
    elif douban_id:
        overrides["douban_url"] = f"https://movie.douban.com/subject/{douban_id}/"
    return overrides


async def _tmdb_from_imdb(config: dict[str, Any], imdb_id: Any) -> dict[str, Any]:
    default = config.get("DEFAULT", {}) if isinstance(config, dict) else {}
    tmdb_api = str(default.get("tmdb_api") or "").strip() if isinstance(default, dict) else ""
    if not tmdb_api:
        return {"blocker": "TMDb enrichment requires DEFAULT.tmdb_api."}
    imdb_value = _normalize_imdb_tt(imdb_id)
    if not imdb_value:
        return {"blocker": "TMDb enrichment requires a valid IMDb id."}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"https://api.themoviedb.org/3/find/{imdb_value}",
                params={"api_key": tmdb_api, "external_source": "imdb_id"},
            )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException:
        return {"blocker": "TMDb enrichment timed out."}
    except (httpx.RequestError, ValueError) as exc:
        return {"blocker": f"TMDb enrichment failed: {exc}"}
    normalized = _tmdb_id_from_find_payload(payload)
    return {"tmdb_id": normalized} if normalized else {"blocker": "TMDb enrichment returned no TMDb id."}


def _tmdb_id_from_find_payload(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("movie_results", "tv_results"):
        results = payload.get(key)
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_int(item.get("id"))
            if normalized:
                return normalized
    return None


async def _ptgen_from_metadata(config: dict[str, Any], source_info: dict[str, Any], *, base_dir: str | None) -> dict[str, Any]:
    from src.trackers.COMMON import COMMON

    imdb_id = _normalize_int(source_info.get("imdb_id"))
    douban_url = _normalize_douban_url(source_info.get("douban_url") or source_info.get("douban_id"))
    if not imdb_id and not douban_url:
        return {"blocker": "PTGen enrichment requires IMDb id or Douban URL."}

    tracker_config = config.get("TRACKERS", {}).get("MTEAM", {}) if isinstance(config, dict) else {}
    ptgen_api = str(tracker_config.get("ptgen_api") or "").strip() if isinstance(tracker_config, dict) else ""
    ptgen_retry = int(tracker_config.get("ptgen_retry", 3) or 3) if isinstance(tracker_config, dict) else 3
    uuid = _ptgen_uuid(source_info)
    work_root = await asyncio.to_thread(_prepare_ptgen_work_root, base_dir, uuid)
    meta = _ptgen_meta(source_info, work_root, uuid, imdb_id, douban_url)

    try:
        description = await COMMON(config=config).ptgen(meta, ptgen_api, ptgen_retry)
    except Exception as exc:
        return {"blocker": f"PTGen enrichment failed: {exc}"}
    if not description.strip():
        return {"blocker": "PTGen enrichment returned no description text."}

    douban_id = _normalize_douban_id(meta.get("douban_id") or meta.get("douban") or douban_url)
    return {
        "description": description,
        "ptgen": meta.get("ptgen") if isinstance(meta.get("ptgen"), dict) else None,
        "douban_id": douban_id,
        "douban_url": _normalize_douban_url(meta.get("douban_url") or douban_id),
    }


def _ptgen_meta(source_info: dict[str, Any], work_root: Path, uuid: str, imdb_id: int | None, douban_url: str | None) -> dict[str, Any]:
    title = _title_from_source(source_info)
    meta: dict[str, Any] = {
        "base_dir": str(work_root),
        "uuid": uuid,
        "name": source_info.get("name") or title or uuid,
        "title": title or source_info.get("name") or uuid,
        "original_title": title or source_info.get("name") or uuid,
        "year": _year_from_name(str(source_info.get("name") or "")),
        "imdb_id": imdb_id or 0,
        "imdb": str(imdb_id) if imdb_id else "",
        "tmdb": source_info.get("tmdb_id") or source_info.get("tmdb") or 0,
        "tmdb_id": source_info.get("tmdb_id"),
        "douban_id": _normalize_douban_id(source_info.get("douban_id") or douban_url),
        "douban": _normalize_douban_id(source_info.get("douban_id") or douban_url) or "",
        "douban_url": douban_url or "",
        "unattended": True,
        "unattended_confirm": False,
        "debug": False,
    }
    return meta


def _prepare_ptgen_work_root(base_dir: str | None, uuid: str) -> Path:
    work_root = Path(base_dir).expanduser() if base_dir else Path(tempfile.gettempdir()) / "ptcli-ptgen"
    (work_root / "tmp" / uuid).mkdir(parents=True, exist_ok=True)
    return work_root


def _ptgen_uuid(source_info: dict[str, Any]) -> str:
    tracker = str(source_info.get("tracker") or "SOURCE")
    torrent_id = str(source_info.get("torrent_id") or "unknown")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", f"ptcli-{tracker}-{torrent_id}")[:120]


def _title_from_source(source_info: dict[str, Any]) -> str | None:
    name = str(source_info.get("name") or "").strip()
    if not name:
        return None
    title = re.split(r"\b(?:19|20)\d{2}\b", name, maxsplit=1)[0].strip(". -_")
    return title.replace(".", " ") if title else name


def _year_from_name(name: str) -> str:
    match = re.search(r"\b((?:19|20)\d{2})\b", name)
    return match.group(1) if match else ""


def _normalize_int(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"(\d+)", str(value))
    return int(match.group(1)) if match else None


def _normalize_imdb_tt(value: Any) -> str | None:
    normalized = _normalize_int(value)
    return f"tt{normalized:07d}" if normalized else None


def _normalize_douban_id(value: Any) -> str | None:
    if value is None:
        return None
    match = re.search(r"(\d{5,})", str(value))
    return match.group(1) if match else None


def _normalize_douban_url(value: Any) -> str | None:
    douban_id = _normalize_douban_id(value)
    if not douban_id:
        return None
    return f"https://movie.douban.com/subject/{douban_id}/"
