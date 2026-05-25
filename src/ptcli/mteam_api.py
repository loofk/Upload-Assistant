"""Small MTEAM API client used by the focused ptcli workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiofiles
import httpx


class MTeamApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class MTeamApiClient:
    def __init__(self, config: dict[str, Any]) -> None:
        tracker_config = config.get("TRACKERS", {}).get("MTEAM", {})
        self.api_key = str(tracker_config.get("api_key", "")).strip() if isinstance(tracker_config, dict) else ""
        self.session = httpx.AsyncClient(
            headers={
                "User-Agent": "Upload Assistant",
                "accept": "application/json",
                "x-api-key": self.api_key,
            },
            timeout=60.0,
        )

    async def aclose(self) -> None:
        await self.session.aclose()

    async def __aenter__(self) -> MTeamApiClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.aclose()

    async def request(self, url: str, *, data: dict[str, Any] | None = None, json: dict[str, Any] | None = None, files: dict[str, Any] | None = None) -> Any:
        if not self.api_key:
            raise MTeamApiError("MTEAM API key is not configured.")
        try:
            if json is not None:
                response = await self.session.post(url, json=json)
            else:
                response = await self.session.post(url, data=data, files=files)
        except httpx.TimeoutException as exc:
            raise MTeamApiError(f"Request timed out: {exc}", 0) from exc
        except httpx.RequestError as exc:
            raise MTeamApiError(str(exc), 0) from exc

        if response.status_code != 200:
            raise MTeamApiError(_response_error_message(response), response.status_code)

        try:
            body = response.json()
        except Exception as exc:
            raise MTeamApiError("Invalid JSON", 200) from exc
        if not isinstance(body, dict):
            raise MTeamApiError("Response is not dict", 200)

        success, payload, message = parse_mteam_response(body)
        if not success:
            raise MTeamApiError(message or "API returned error", 200)
        return payload

    async def search_by_imdb(self, imdb: str) -> Any:
        return await self.request(
            "https://api.m-team.cc/api/torrent/search",
            json={
                "mode": "normal",
                "visible": 1,
                "categories": [],
                "pageNumber": 1,
                "pageSize": 100,
                "imdb": imdb,
            },
        )

    async def torrent_detail(self, torrent_id: str) -> Any:
        return await self.request("https://api.m-team.cc/api/torrent/detail", json={"id": int(torrent_id)})

    async def upload_torrent(self, data: dict[str, Any], files: dict[str, Any]) -> Any:
        return await self.request("https://api.m-team.cc/api/torrent/createOredit", data=data, files=files)

    async def download_torrent(self, torrent_id: str, torrent_path: str | Path) -> None:
        download_url = await self.request("https://api.m-team.cc/api/torrent/genDlToken", data={"id": torrent_id})
        if not isinstance(download_url, str) or not download_url:
            raise MTeamApiError("MTEAM genDlToken response did not include a download URL.", 200)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(download_url)
            response.raise_for_status()
            async with aiofiles.open(torrent_path, "wb") as torrent_file:
                await torrent_file.write(response.content)


def parse_mteam_response(response_json: dict[str, Any]) -> tuple[bool, Any, str]:
    code = response_json.get("code")
    success = code == 0 or code == "0" or str(code) == "0"
    return success, response_json.get("data", {}), str(response_json.get("message", ""))


def _response_error_message(response: httpx.Response) -> str:
    message = response.text[:200] if response.text else f"HTTP {response.status_code}"
    if response.headers.get("content-type", "").startswith("application/json"):
        try:
            body = response.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("error") or message)
    if response.status_code in {401, 403}:
        return "Authentication failed (403 Forbidden or 401 Unauthorized). Please check your API key." + (f" {message}" if message else "")
    return message
