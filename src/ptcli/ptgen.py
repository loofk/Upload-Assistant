"""Focused, non-interactive PTGen client used by the PTCLI container."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

DEFAULT_PTGEN_URL = "https://ptgen.zhenzhen.workers.dev"
DOUBAN_URL_RE = re.compile(r"https?://movie\.douban\.com/subject/(\d+)")


async def fetch_ptgen_description(
    meta: dict[str, Any],
    *,
    ptgen_site: str = "",
    retries: int = 3,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Fetch PTGen text without importing the legacy tracker stack.

    The focused service is unattended by design: missing identifiers, remote
    errors, and empty responses are surfaced to the caller instead of opening
    an interactive prompt.
    """
    api_url, api_key = _resolve_api_url(ptgen_site or DEFAULT_PTGEN_URL)
    douban_url = _douban_url(meta.get("douban_url") or meta.get("douban_id") or meta.get("douban"))
    imdb_id = _imdb_id(meta.get("imdb_id"))
    if not douban_url and not imdb_id:
        return ""

    owns_client = client is None
    resolved_client = client or httpx.AsyncClient(timeout=30.0)
    try:
        payload: dict[str, Any] | None = None
        if douban_url:
            payload = await _request_with_retries(resolved_client, api_url, {"url": douban_url}, api_key=api_key, retries=retries)
        elif imdb_id:
            payload = await _request_with_retries(resolved_client, api_url, {"source": "imdb", "sid": f"tt{imdb_id}"}, api_key=api_key, retries=retries)
            discovered_douban = _find_douban_url(payload)
            if discovered_douban:
                douban_url = discovered_douban
                meta["douban_url"] = discovered_douban
                match = DOUBAN_URL_RE.search(discovered_douban)
                if match:
                    meta["douban_id"] = match.group(1)
                payload = await _request_with_retries(resolved_client, api_url, {"url": discovered_douban}, api_key=api_key, retries=retries)

        if not _successful(payload):
            return ""
        meta["ptgen"] = payload
        return _format_description(payload, meta)
    finally:
        if owns_client:
            await resolved_client.aclose()


async def _request_with_retries(
    client: httpx.AsyncClient,
    api_url: str,
    params: dict[str, str],
    *,
    api_key: str | None,
    retries: int,
) -> dict[str, Any] | None:
    request_params = dict(params)
    if api_key:
        request_params["key"] = api_key
    attempts = max(1, min(int(retries) + 1, 6))
    last_payload: dict[str, Any] | None = None
    for _ in range(attempts):
        last_payload = await _request_once(client, api_url, request_params)
        if _successful(last_payload):
            return last_payload
    return last_payload


async def _request_once(client: httpx.AsyncClient, api_url: str, params: dict[str, str]) -> dict[str, Any] | None:
    try:
        response = await client.post(api_url, params=params, timeout=30.0)
        response.raise_for_status()
        decoded = response.json()
        return decoded if isinstance(decoded, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def _resolve_api_url(value: str) -> tuple[str, str | None]:
    parsed = urlparse(value.strip())
    query = parse_qs(parsed.query)
    api_key = query.get("key", [None])[0]
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    if not base_url:
        base_url = DEFAULT_PTGEN_URL
    api_url = base_url if base_url.endswith("/api") or "/api/" in base_url else f"{base_url}/api"
    return api_url, api_key


def _successful(payload: dict[str, Any] | None) -> bool:
    return bool(payload and payload.get("success") is True and not payload.get("error"))


def _format_description(payload: dict[str, Any], meta: dict[str, Any]) -> str:
    text = str(payload.get("format") or "").strip()
    if "[/img]" in text:
        text = text.split("[/img]", 1)[1].lstrip()
    poster = str((meta.get("imdb_info") or {}).get("cover") or meta.get("cover") or payload.get("poster") or "").strip()
    if poster:
        return f"[img]{poster}[/img]{text}"
    return text


def _find_douban_url(value: Any) -> str | None:
    if isinstance(value, str):
        match = DOUBAN_URL_RE.search(value)
        return match.group(0) if match else None
    if isinstance(value, dict):
        for item in value.values():
            found = _find_douban_url(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_douban_url(item)
            if found:
                return found
    return None


def _douban_url(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    match = DOUBAN_URL_RE.search(text)
    if match:
        return match.group(0)
    return f"https://movie.douban.com/subject/{text}/" if text.isdigit() else None


def _imdb_id(value: Any) -> str | None:
    if value is None:
        return None
    match = re.search(r"(?:tt)?(\d+)", str(value).strip(), flags=re.IGNORECASE)
    return match.group(1) if match else None
