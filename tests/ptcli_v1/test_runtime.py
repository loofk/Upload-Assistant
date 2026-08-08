from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.ptcli.contracts import json_size_bytes
from src.ptcli.doctor import build_runtime_dependency_check
from src.ptcli.ptgen import fetch_ptgen_description
from src.ptcli.service import public_goal_progress_payload

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_focused_ptgen_uses_imdb_then_douban_without_legacy_common() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.params.get("source") == "imdb":
            return httpx.Response(200, json={"success": True, "data": [{"link": "https://movie.douban.com/subject/1292052/"}]})
        return httpx.Response(200, json={"success": True, "format": "[img]remote[/img]PTGen body", "poster": "https://example.test/poster.jpg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        meta = {"imdb_id": 1109124, "imdb_info": {"cover": "https://example.test/imdb.jpg"}}
        description = await fetch_ptgen_description(meta, ptgen_site="https://ptgen.example.test?key=secret", retries=0, client=client)

    assert len(requests) == 2
    assert "key=secret" in requests[0]
    assert meta["douban_id"] == "1292052"
    assert description == "[img]https://example.test/imdb.jpg[/img]PTGen body"


def test_runtime_doctor_checks_focused_ptgen_adapter() -> None:
    check = build_runtime_dependency_check()
    ptgen = next(item for item in check["internal_imports"] if item["name"] == "ptgen_adapter")
    assert ptgen["module"] == "src.ptcli.ptgen"
    assert ptgen["available"] is True


def test_goal_progress_without_config_is_structured_and_brief() -> None:
    payload = public_goal_progress_payload({"config": "/definitely/missing/config.py"})

    assert payload["status"] == "blocked"
    assert payload["ok"] is False
    assert any("Config file not found" in blocker for blocker in payload["blockers"])
    assert json_size_bytes(payload) < 8 * 1024
    json.dumps(payload)


def test_dockerfile_sets_runtime_tmpdir_only_after_creating_it() -> None:
    dockerfile = (ROOT / "Dockerfile.ptcli").read_text(encoding="utf-8")

    create_tmp = dockerfile.index("mkdir -p /Upload-Assistant/data/cookies /Upload-Assistant/data/site-rules /Upload-Assistant/tmp")
    set_tmpdir = dockerfile.index("ENV TMPDIR=/Upload-Assistant/tmp")

    assert create_tmp < set_tmpdir
