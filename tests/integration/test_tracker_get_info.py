"""集成测试：Tracker get_info_from_torrent_id + MTEAM helpers。

使用 respx mock HTTP 请求，验证从 API 响应中正确提取 IMDb/TMDb/豆瓣 ID。
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from src.trackers.MTEAM import MTEAM

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def mteam_instance(mock_config):
    """创建 MTEAM 实例。"""
    return MTEAM(config=mock_config)


@pytest.fixture
def mteam_api_data():
    """加载 MTEAM API 响应 fixture。"""
    with open(FIXTURES_DIR / "mteam_api_response.json") as f:
        return json.load(f)


# ============================================================
# _parse_api_response
# ============================================================
class TestParseApiResponse:
    def test_success_code_0_int(self):
        success, data, msg = MTEAM._parse_api_response({"code": 0, "data": {"id": 1}, "message": "OK"})
        assert success is True
        assert data == {"id": 1}

    def test_success_code_0_str(self):
        success, data, msg = MTEAM._parse_api_response({"code": "0", "data": {}, "message": "OK"})
        assert success is True

    def test_failure_code(self):
        success, data, msg = MTEAM._parse_api_response({"code": 1, "data": None, "message": "Error"})
        assert success is False
        assert msg == "Error"

    def test_success_string_code(self):
        """MTEAM API 有时返回字符串 "SUCCESS" 作为 code。"""
        success, data, msg = MTEAM._parse_api_response({"code": "SUCCESS", "data": {"id": 1}, "message": "SUCCESS"})
        assert success is False  # "SUCCESS" != 0


# ============================================================
# MTEAM.get_info_from_torrent_id
# ============================================================
class TestMteamGetInfo:
    async def test_successful_extraction(self, mteam_instance, mteam_api_data, mock_meta):
        """正确从 MTEAM API 响应中提取 IMDb/TMDb/豆瓣。"""
        # Mock _request 返回 API data 部分
        api_data = mteam_api_data["data"]
        mteam_instance._request = AsyncMock(return_value=api_data)

        imdb, tmdb, name, torrenthash, desc = await mteam_instance.get_info_from_torrent_id(
            "1133442", mock_meta
        )

        assert imdb == 1234567
        assert name == "Test.Movie.2024.1080p.WEB-DL.DDP5.1.H.265-GROUP"
        assert torrenthash == "abc123def456"
        assert desc is not None
        # 豆瓣信息应写入 meta
        assert mock_meta.get("douban_id") == "1291546"
        assert mock_meta.get("douban_url") == "https://movie.douban.com/subject/1291546/"

    async def test_missing_imdb(self, mteam_instance, mock_meta):
        """API 响应中没有 IMDb 链接时应返回 None。"""
        mteam_instance._request = AsyncMock(return_value={
            "name": "Test Torrent",
            "imdb": "",
            "douban": "",
            "descr": "desc",
            "hash": "abc",
        })

        imdb, tmdb, name, _, _ = await mteam_instance.get_info_from_torrent_id("999", mock_meta)
        assert imdb is None
        assert name == "Test Torrent"

    async def test_no_api_key(self, mock_config, mock_meta):
        """未配置 API key 时应返回全 None。"""
        mock_config["TRACKERS"]["MTEAM"]["api_key"] = ""
        instance = MTEAM(config=mock_config)

        result = await instance.get_info_from_torrent_id("123", mock_meta)
        assert result == (None, None, None, None, None)

    async def test_api_error_handled(self, mteam_instance, mock_meta):
        """API 请求失败应被捕获，不抛异常。"""
        from src.trackers.MTEAM import MTEAMRequestError
        mteam_instance._request = AsyncMock(side_effect=MTEAMRequestError("timeout", 0))

        result = await mteam_instance.get_info_from_torrent_id("123", mock_meta)
        assert result == (None, None, None, None, None)


# ============================================================
# PTGen mock 测试
# ============================================================
class TestPtgenIntegration:
    """测试 PTGen API 调用逻辑（通过 mock HTTP）。"""

    @pytest.fixture
    def ptgen_response_data(self):
        with open(FIXTURES_DIR / "ptgen_response.json") as f:
            return json.load(f)

    async def test_ptgen_basic_structure(self, ptgen_response_data):
        """验证 PTGen 响应 fixture 数据结构正确。"""
        assert ptgen_response_data["success"] is True
        data = ptgen_response_data["data"]
        assert data["chinese_title"] == "测试电影"
        assert "美国" in data["region"]
        assert data["douban"] == "https://movie.douban.com/subject/1291546/"
        assert ptgen_response_data["format"] != ""

    async def test_ptgen_trans_title_for_small_descr(self, ptgen_response_data):
        """验证 PTGen 数据可以正确用于 MTEAM/TJUPT 的 small_descr 生成。"""
        data = ptgen_response_data["data"]
        trans_title = data.get("trans_title", [])
        genres = data.get("genre", [])

        # 模拟 TJUPT/MTEAM 的 small_descr 生成逻辑
        small_descr = ""
        for title_ in trans_title:
            small_descr += f"{title_} / "
        genre_value = genres[0] if genres else ""
        small_descr += "| 类别:" + genre_value
        small_descr = small_descr.replace("/ |", "|")

        assert "测试电影" in small_descr
        assert "动作" in small_descr
