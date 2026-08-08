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
TMDB_ID_KEYS = ("tmdb_id", "tmdb", "tmdb_url", "themoviedb", "themoviedb_url")
TMDB_TYPE_KEYS = ("tmdb_type", "tmdb_media_type", "tmdb_kind")
PTGEN_DESCRIPTION_KEYS = ("ptgen_description", "ptgen", "douban_description", "description")


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
    blocker_records: list[tuple[str | None, str]] = []
    sources: list[str] = []
    ptgen_evidence: dict[str, Any] = {}
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
            tmdb_type = _normalize_tmdb_type(tmdb_result.get("tmdb_type") or tmdb_result.get("media_type"))
            if tmdb_type and not base.get("tmdb_type"):
                base["tmdb_type"] = tmdb_type
                applied["tmdb_type"] = tmdb_type
                field_sources["tmdb_type"] = "tmdb_api"
            sources.append("tmdb_api")
        elif tmdb_result.get("blocker"):
            blocker_records.append(("tmdb_id", str(tmdb_result["blocker"])))

    if base.get("tmdb_id") and not base.get("imdb_id"):
        imdb_result = await _imdb_from_tmdb(config, base.get("tmdb_id"), tmdb_type=base.get("tmdb_type"))
        if imdb_result.get("imdb_id"):
            base["imdb_id"] = imdb_result["imdb_id"]
            applied["imdb_id"] = imdb_result["imdb_id"]
            field_sources["imdb_id"] = "tmdb_api"
            tmdb_type = _normalize_tmdb_type(imdb_result.get("tmdb_type") or imdb_result.get("media_type"))
            if tmdb_type and not base.get("tmdb_type"):
                base["tmdb_type"] = tmdb_type
                applied["tmdb_type"] = tmdb_type
                field_sources["tmdb_type"] = "tmdb_api"
            sources.append("tmdb_api")
        elif imdb_result.get("blocker"):
            blocker_records.append(("imdb_id", str(imdb_result["blocker"])))

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

    if fetch_ptgen and not base.get("ptgen_description"):
        ptgen_result = await _ptgen_from_metadata(config, base, base_dir=base_dir)
        if ptgen_result.get("description"):
            base["ptgen_description"] = ptgen_result["description"]
            applied["ptgen_description"] = {"length": len(str(ptgen_result["description"]))}
            field_sources["ptgen_description"] = "ptgen"
            sources.append("ptgen")
        if isinstance(ptgen_result.get("evidence"), dict):
            ptgen_evidence = ptgen_result["evidence"]
        if ptgen_result.get("ptgen"):
            base["ptgen"] = ptgen_result["ptgen"]
        for key in ("imdb_id", "tmdb_id"):
            if ptgen_result.get(key) and not base.get(key):
                base[key] = ptgen_result[key]
                applied[key] = ptgen_result[key]
                field_sources[key] = "ptgen"
        tmdb_type = _normalize_tmdb_type(ptgen_result.get("tmdb_type"))
        if tmdb_type and not base.get("tmdb_type"):
            base["tmdb_type"] = tmdb_type
            applied["tmdb_type"] = tmdb_type
            field_sources["tmdb_type"] = "ptgen"
        if ptgen_result.get("douban_id") and not base.get("douban_id"):
            base["douban_id"] = ptgen_result["douban_id"]
            applied["douban_id"] = ptgen_result["douban_id"]
            field_sources["douban_id"] = "ptgen"
        if ptgen_result.get("douban_url") and not base.get("douban_url"):
            base["douban_url"] = ptgen_result["douban_url"]
            applied["douban_url"] = ptgen_result["douban_url"]
            field_sources["douban_url"] = "ptgen"
        if ptgen_result.get("blocker"):
            blocker_records.append(("ptgen_description", str(ptgen_result["blocker"])))

    missing = [key for key in METADATA_KEYS if not base.get(key)]
    blockers = _unresolved_metadata_blockers(blocker_records, base)
    return {
        "status": "enriched" if applied else "unchanged",
        "ready": not missing and (not fetch_ptgen or bool(base.get("ptgen_description"))),
        "source_info": base,
        "applied": applied,
        "missing": missing,
        "readiness": _metadata_readiness(base, field_sources, fetch_ptgen=fetch_ptgen),
        "field_evidence": _metadata_field_evidence(base, field_sources, fetch_ptgen=fetch_ptgen),
        "sources": sources,
        "ptgen_evidence": ptgen_evidence,
        "blockers": blockers,
    }


def _unresolved_metadata_blockers(blocker_records: list[tuple[str | None, str]], source_info: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for field_name, message in blocker_records:
        if field_name and source_info.get(field_name):
            continue
        if message not in blockers:
            blockers.append(message)
    return blockers


def _initial_metadata_field_sources(source_info: dict[str, Any]) -> dict[str, str]:
    field_sources = {key: "source" for key in METADATA_KEYS if source_info.get(key)}
    if source_info.get("ptgen_description"):
        field_sources["ptgen_description"] = "source"
    if source_info.get("tmdb_type"):
        field_sources["tmdb_type"] = "source"
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


def _metadata_field_evidence(source_info: dict[str, Any], field_sources: dict[str, str], *, fetch_ptgen: bool) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key in METADATA_KEYS:
        value = source_info.get(key)
        evidence[key] = {
            "ready": bool(value),
            "required": True,
            "source": field_sources.get(key),
            "value": value,
        }
    ptgen_description = str(source_info.get("ptgen_description") or "")
    evidence["ptgen_description"] = {
        "ready": bool(ptgen_description),
        "required": bool(fetch_ptgen),
        "source": field_sources.get("ptgen_description"),
        "length": len(ptgen_description),
    }
    return evidence


def load_metadata_overrides(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("metadata override file must contain a JSON object")
    return normalize_metadata_overrides(payload)


def load_ptgen_description_override(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    text = Path(path).expanduser().read_text(encoding="utf-8")
    overrides = normalize_metadata_overrides({"ptgen_description": text})
    if not overrides.get("ptgen_description"):
        raise ValueError("PTGen description file is empty")
    return overrides


def normalize_metadata_overrides(payload: dict[str, Any]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    imdb_id = _normalize_int(payload.get("imdb_id") or payload.get("imdb") or payload.get("imdbID"))
    tmdb_value = _first_payload_value(payload, TMDB_ID_KEYS)
    tmdb_id, tmdb_type_from_value = _extract_tmdb_ref_from_text(str(tmdb_value or ""))
    tmdb_id = tmdb_id or _normalize_int(tmdb_value)
    douban_id = _normalize_douban_id(payload.get("douban_id") or payload.get("douban"))
    douban_url = _normalize_douban_url(payload.get("douban_url") or payload.get("douban"))
    if not douban_id and douban_url:
        douban_id = _normalize_douban_id(douban_url)
    if imdb_id:
        overrides["imdb_id"] = imdb_id
    if tmdb_id:
        overrides["tmdb_id"] = tmdb_id
    tmdb_type = _normalize_tmdb_type(_first_payload_value(payload, TMDB_TYPE_KEYS)) or tmdb_type_from_value
    if tmdb_type:
        overrides["tmdb_type"] = tmdb_type
    if douban_id:
        overrides["douban_id"] = douban_id
    if douban_url:
        overrides["douban_url"] = douban_url
    elif douban_id:
        overrides["douban_url"] = f"https://movie.douban.com/subject/{douban_id}/"
    ptgen_description = _normalize_ptgen_description(payload)
    if ptgen_description:
        text_overrides = _metadata_overrides_from_text(ptgen_description)
        for key, value in text_overrides.items():
            if key not in overrides and value:
                overrides[key] = value
        if "douban_id" in overrides and "douban_url" not in overrides:
            overrides["douban_url"] = f"https://movie.douban.com/subject/{overrides['douban_id']}/"
        overrides["ptgen_description"] = ptgen_description
    return overrides


def _first_payload_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((payload.get(key) for key in keys if payload.get(key)), None)


def _normalize_ptgen_description(payload: dict[str, Any]) -> str | None:
    for key in PTGEN_DESCRIPTION_KEYS:
        value = payload.get(key)
        if value is None:
            continue
        text = value.get("description") or value.get("text") or value.get("content") if isinstance(value, dict) else value
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if normalized:
            return normalized
    return None


def _metadata_overrides_from_text(text: str) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    imdb_id = _extract_imdb_id_from_text(text)
    tmdb_id, tmdb_type = _extract_tmdb_ref_from_text(text)
    douban_id = _extract_douban_id_from_text(text)
    if imdb_id:
        overrides["imdb_id"] = imdb_id
    if tmdb_id:
        overrides["tmdb_id"] = tmdb_id
    if tmdb_type:
        overrides["tmdb_type"] = tmdb_type
    if douban_id:
        overrides["douban_id"] = douban_id
        overrides["douban_url"] = f"https://movie.douban.com/subject/{douban_id}/"
    return overrides


def _extract_imdb_id_from_text(text: str) -> int | None:
    for pattern in (r"imdb\.com/title/tt(\d{5,10})", r"\btt(\d{5,10})\b", r"\bimdb(?:[_\s:-]*id)?[^\d]{0,40}(\d{5,10})\b"):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _normalize_int(match.group(1))
    return None


def _extract_tmdb_id_from_text(text: str) -> int | None:
    tmdb_id, _tmdb_type = _extract_tmdb_ref_from_text(text)
    return tmdb_id


def _extract_tmdb_ref_from_text(text: str) -> tuple[int | None, str | None]:
    url_match = re.search(r"themoviedb\.org/(movie|tv)/(\d{2,10})", text, flags=re.IGNORECASE)
    if url_match:
        return _normalize_int(url_match.group(2)), url_match.group(1).lower()
    for pattern in (r"themoviedb\.org/(?:movie|tv)/(\d{2,10})", r"\btmdb(?:[_\s:-]*id)?[^\d]{0,40}(\d{2,10})\b"):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _normalize_int(match.group(1)), None
    return None, None


def _extract_douban_id_from_text(text: str) -> str | None:
    for pattern in (r"douban\.com/subject/(\d{5,})", r"(?:douban|豆瓣)[^\d]{0,40}(\d{5,})"):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return str(match.group(1))
    return None


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
    match = _tmdb_match_from_find_payload(payload)
    return match if match else {"blocker": "TMDb enrichment returned no TMDb id."}


async def _imdb_from_tmdb(config: dict[str, Any], tmdb_id: Any, *, tmdb_type: Any = None) -> dict[str, Any]:
    default = config.get("DEFAULT", {}) if isinstance(config, dict) else {}
    tmdb_api = str(default.get("tmdb_api") or "").strip() if isinstance(default, dict) else ""
    if not tmdb_api:
        return {"blocker": "TMDb enrichment requires DEFAULT.tmdb_api."}
    tmdb_value = _normalize_int(tmdb_id)
    if not tmdb_value:
        return {"blocker": "TMDb enrichment requires a valid TMDb id."}

    blockers = []
    preferred_type = _normalize_tmdb_type(tmdb_type)
    media_types = (preferred_type, "tv" if preferred_type == "movie" else "movie") if preferred_type else ("movie", "tv")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for media_type in media_types:
                response = await client.get(
                    f"https://api.themoviedb.org/3/{media_type}/{tmdb_value}/external_ids",
                    params={"api_key": tmdb_api},
                )
                if getattr(response, "status_code", 200) == 404:
                    continue
                response.raise_for_status()
                normalized = _imdb_id_from_external_ids_payload(response.json())
                if normalized:
                    return {"imdb_id": normalized, "media_type": media_type, "tmdb_type": media_type}
    except httpx.TimeoutException:
        return {"blocker": "TMDb external-id enrichment timed out."}
    except (httpx.HTTPError, ValueError) as exc:
        blockers.append(f"TMDb external-id enrichment failed: {exc}")
    return {"blocker": blockers[0] if blockers else "TMDb enrichment returned no IMDb id."}


def _tmdb_id_from_find_payload(payload: Any) -> int | None:
    match = _tmdb_match_from_find_payload(payload)
    return _normalize_int(match.get("tmdb_id")) if match else None


def _tmdb_match_from_find_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    for key, media_type in (("movie_results", "movie"), ("tv_results", "tv")):
        results = payload.get(key)
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_int(item.get("id"))
            if normalized:
                return {"tmdb_id": normalized, "tmdb_type": media_type, "media_type": media_type}
    return None


def _imdb_id_from_external_ids_payload(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    return _normalize_int(payload.get("imdb_id"))


async def _ptgen_from_metadata(config: dict[str, Any], source_info: dict[str, Any], *, base_dir: str | None) -> dict[str, Any]:
    from src.ptcli.ptgen import fetch_ptgen_description

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
        description = await fetch_ptgen_description(meta, ptgen_site=ptgen_api, retries=ptgen_retry)
    except Exception as exc:
        return {"blocker": f"PTGen enrichment failed: {exc}"}
    if not description.strip():
        return {"blocker": "PTGen enrichment returned no description text."}

    description_text = str(description).strip()
    description_ids = _metadata_overrides_from_text(description_text)
    douban_id, douban_source = _douban_id_from_ptgen_result(meta, description_text, douban_url)
    douban_url_value = _normalize_douban_url(meta.get("douban_url")) or _normalize_douban_url(douban_id)
    ptgen_payload = meta.get("ptgen") if isinstance(meta.get("ptgen"), dict) else None
    evidence: dict[str, Any] = {
        "description_length": len(description_text),
        "douban_id": douban_id,
        "douban_url": douban_url_value,
        "douban_source": douban_source,
        "payload_keys": sorted(ptgen_payload.keys()) if isinstance(ptgen_payload, dict) else [],
    }
    if description_ids.get("imdb_id"):
        evidence["imdb_id"] = description_ids["imdb_id"]
        evidence["imdb_source"] = "description"
    if description_ids.get("tmdb_id"):
        evidence["tmdb_id"] = description_ids["tmdb_id"]
        evidence["tmdb_source"] = "description"
    if description_ids.get("tmdb_type"):
        evidence["tmdb_type"] = description_ids["tmdb_type"]
    return {
        "description": description,
        "ptgen": ptgen_payload,
        "imdb_id": description_ids.get("imdb_id"),
        "tmdb_id": description_ids.get("tmdb_id"),
        "tmdb_type": description_ids.get("tmdb_type"),
        "douban_id": douban_id,
        "douban_url": douban_url_value,
        "evidence": evidence,
    }


def _douban_id_from_ptgen_result(meta: dict[str, Any], description: str, fallback_url: str | None) -> tuple[str | None, str | None]:
    for key in ("douban_id", "douban", "douban_url"):
        douban_id = _normalize_douban_id(meta.get(key))
        if douban_id:
            return douban_id, f"meta.{key}"
    fallback_id = _normalize_douban_id(fallback_url)
    if fallback_id:
        return fallback_id, "input.douban_url"
    payload_id = _extract_douban_id_from_value(meta.get("ptgen"))
    if payload_id:
        return payload_id, "ptgen_payload"
    description_id = _extract_douban_id_from_text(description)
    if description_id:
        return description_id, "description"
    return None, None


def _extract_douban_id_from_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        priority_keys = ("douban_id", "douban", "douban_url", "link", "url", "format", "data")
        for key in priority_keys:
            if key in value:
                douban_id = _extract_douban_id_from_value(value[key])
                if douban_id:
                    return douban_id
        for item in value.values():
            douban_id = _extract_douban_id_from_value(item)
            if douban_id:
                return douban_id
        return None
    if isinstance(value, list):
        for item in value:
            douban_id = _extract_douban_id_from_value(item)
            if douban_id:
                return douban_id
        return None
    return _extract_douban_id_from_text(str(value))


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


def _normalize_tmdb_type(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"movie", "film"}:
        return "movie"
    if text in {"tv", "show", "series"}:
        return "tv"
    return None
