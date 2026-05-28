"""Metadata enrichment helpers for ptcli retorrent flows."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.tmdb import TmdbManager

METADATA_KEYS = ("imdb_id", "tmdb_id", "douban_id", "douban_url")


async def enrich_source_metadata(
    config: dict[str, Any],
    source_info: dict[str, Any],
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = dict(source_info)
    applied: dict[str, Any] = {}
    blockers: list[str] = []
    sources: list[str] = []
    override_values = normalize_metadata_overrides(overrides or {})
    for key, value in override_values.items():
        if value and not base.get(key):
            base[key] = value
            applied[key] = value
    if applied:
        sources.append("overrides")

    if base.get("imdb_id") and not base.get("tmdb_id"):
        tmdb_result = await _tmdb_from_imdb(config, base.get("imdb_id"))
        if tmdb_result.get("tmdb_id"):
            base["tmdb_id"] = tmdb_result["tmdb_id"]
            applied["tmdb_id"] = tmdb_result["tmdb_id"]
            sources.append("tmdb_api")
        elif tmdb_result.get("blocker"):
            blockers.append(str(tmdb_result["blocker"]))

    if base.get("douban_id") and not base.get("douban_url"):
        base["douban_url"] = f"https://movie.douban.com/subject/{base['douban_id']}/"
        applied["douban_url"] = base["douban_url"]

    missing = [key for key in METADATA_KEYS if not base.get(key)]
    return {
        "status": "enriched" if applied else "unchanged",
        "ready": not missing,
        "source_info": base,
        "applied": applied,
        "missing": missing,
        "sources": sources,
        "blockers": blockers,
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
    if not isinstance(default, dict) or not default.get("tmdb_api"):
        return {"blocker": "TMDb enrichment requires DEFAULT.tmdb_api."}
    imdb_value = _normalize_imdb_tt(imdb_id)
    if not imdb_value:
        return {"blocker": "TMDb enrichment requires a valid IMDb id."}
    try:
        manager = TmdbManager(config)
        _category, tmdb_id, _language, _filename_search = await manager.get_tmdb_from_imdb(imdb_value, mode="discord")
    except Exception as exc:
        return {"blocker": f"TMDb enrichment failed: {exc}"}
    normalized = _normalize_int(tmdb_id)
    return {"tmdb_id": normalized} if normalized else {"blocker": "TMDb enrichment returned no TMDb id."}


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
