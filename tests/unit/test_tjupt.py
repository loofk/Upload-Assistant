"""Tests for TJUPT tracker helper methods."""
from typing import Any

import pytest

from src.trackers.TJUPT import TJUPT


@pytest.fixture
def tjupt(mock_config: dict[str, Any]) -> TJUPT:
    return TJUPT(mock_config)


@pytest.fixture
def base_meta(mock_meta: dict[str, Any]) -> dict[str, Any]:
    return mock_meta


# ── get_type_category_id ────────────────────────────────────────────────


class TestGetTypeCategoryId:
    async def test_movie(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await tjupt.get_type_category_id(base_meta) == '401'

    async def test_tv(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await tjupt.get_type_category_id(base_meta) == '402'

    async def test_documentary(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['genres'] = 'Documentary'
        base_meta['keywords'] = ''
        assert await tjupt.get_type_category_id(base_meta) == '411'

    async def test_animation(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['genres'] = 'Animation'
        base_meta['keywords'] = ''
        assert await tjupt.get_type_category_id(base_meta) == '405'

    async def test_variety(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['genres'] = 'variety'
        base_meta['keywords'] = ''
        assert await tjupt.get_type_category_id(base_meta) == '403'

    async def test_default(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'OTHER'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await tjupt.get_type_category_id(base_meta) == '0'


# ── get_type_medium_id ──────────────────────────────────────────────────


class TestGetTypeMediumId:
    async def test_uhd_disc(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = 'BDMV'
        base_meta['resolution'] = '2160p'
        assert await tjupt.get_type_medium_id(base_meta) == '1'

    async def test_bd_disc(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = 'BDMV'
        base_meta['resolution'] = '1080p'
        assert await tjupt.get_type_medium_id(base_meta) == '2'

    async def test_dvd(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = 'DVD'
        assert await tjupt.get_type_medium_id(base_meta) == '7'

    async def test_hdtv(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = None
        base_meta['type'] = 'HDTV'
        assert await tjupt.get_type_medium_id(base_meta) == '4'

    async def test_encode(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = None
        base_meta['type'] = 'ENCODE'
        assert await tjupt.get_type_medium_id(base_meta) == '6'

    async def test_webrip(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = None
        base_meta['type'] = 'WEBRIP'
        assert await tjupt.get_type_medium_id(base_meta) == '6'

    async def test_remux(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = None
        base_meta['type'] = 'REMUX'
        assert await tjupt.get_type_medium_id(base_meta) == '3'

    async def test_webdl(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = None
        base_meta['type'] = 'WEBDL'
        assert await tjupt.get_type_medium_id(base_meta) == '5'

    async def test_unknown_returns_zero(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = None
        base_meta['type'] = 'SOMETHING_UNKNOWN'
        assert await tjupt.get_type_medium_id(base_meta) == '0'

    async def test_elif_prevents_overwrite(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        """DVD disc should not be overridden by type checks."""
        base_meta['is_disc'] = 'DVD'
        base_meta['type'] = 'REMUX'  # should not override DVD
        assert await tjupt.get_type_medium_id(base_meta) == '7'


# ── get_area_id ─────────────────────────────────────────────────────────


class TestGetAreaId:
    async def test_china_mainland(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["中国大陆"]}
        assert await tjupt.get_area_id(base_meta) == 1

    async def test_hong_kong(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["中国香港"]}
        assert await tjupt.get_area_id(base_meta) == 2

    async def test_taiwan(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["中国台湾"]}
        assert await tjupt.get_area_id(base_meta) == 3

    async def test_usa(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["美国"]}
        assert await tjupt.get_area_id(base_meta) == 4

    async def test_japan(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["日本"]}
        assert await tjupt.get_area_id(base_meta) == 6

    async def test_korea(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["韩国"]}
        assert await tjupt.get_area_id(base_meta) == 5

    async def test_india(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["印度"]}
        assert await tjupt.get_area_id(base_meta) == 7

    async def test_russia_maps_to_europe(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["俄罗斯"]}
        assert await tjupt.get_area_id(base_meta) == 4

    async def test_thailand_maps_to_other(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["泰国"]}
        assert await tjupt.get_area_id(base_meta) == 8

    async def test_unknown_region(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["火星"]}
        assert await tjupt.get_area_id(base_meta) == 8

    async def test_empty_ptgen(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {}
        assert await tjupt.get_area_id(base_meta) == 8


# ── edit_name ───────────────────────────────────────────────────────────


class TestEditName:
    async def test_removes_dubbed(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['name'] = 'Movie 2024 1080p Dubbed WEB-DL H.265'
        base_meta['aka'] = ''
        result = await tjupt.edit_name(base_meta)
        assert 'Dubbed' not in result

    async def test_removes_dual_audio(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['name'] = 'Movie 2024 1080p Dual-Audio WEB-DL H.265'
        base_meta['aka'] = ''
        result = await tjupt.edit_name(base_meta)
        assert 'Dual-Audio' not in result

    async def test_pq10_to_hdr(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['name'] = 'Movie 2024 2160p PQ10 WEB-DL'
        base_meta['aka'] = ''
        result = await tjupt.edit_name(base_meta)
        assert 'HDR' in result
        assert 'PQ10' not in result


# ── is_zhongzi ──────────────────────────────────────────────────────────


class TestIsZhongzi:
    async def test_chinese_sub_track(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = ''
        base_meta['mediainfo'] = {
            'media': {
                'track': [
                    {'@type': 'Video'},
                    {'@type': 'Text', 'Language': 'zh'},
                ]
            }
        }
        assert await tjupt.is_zhongzi(base_meta) == 'yes'

    async def test_no_chinese_sub(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = ''
        base_meta['mediainfo'] = {
            'media': {
                'track': [
                    {'@type': 'Video'},
                    {'@type': 'Text', 'Language': 'en'},
                ]
            }
        }
        assert await tjupt.is_zhongzi(base_meta) is None

    async def test_bdmv_chinese(self, tjupt: TJUPT, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = 'BDMV'
        base_meta['bdinfo'] = {'subtitles': ['English', 'Chinese']}
        assert await tjupt.is_zhongzi(base_meta) == 'yes'


# ── _extract_douban_id ──────────────────────────────────────────────────


class TestExtractDoubanId:
    def test_link_in_soup(self) -> None:
        from bs4 import BeautifulSoup
        html = '<html><body><a href="https://movie.douban.com/subject/12345/">豆瓣</a></body></html>'
        soup = BeautifulSoup(html, 'html.parser')
        assert TJUPT._extract_douban_id(soup, html) == '12345'

    def test_fallback_regex(self) -> None:
        from bs4 import BeautifulSoup
        html = '<html><body>Check https://movie.douban.com/subject/67890/ for info</body></html>'
        soup = BeautifulSoup(html, 'html.parser')
        assert TJUPT._extract_douban_id(soup, html) == '67890'

    def test_no_douban(self) -> None:
        from bs4 import BeautifulSoup
        html = '<html><body>No douban links here</body></html>'
        soup = BeautifulSoup(html, 'html.parser')
        assert TJUPT._extract_douban_id(soup, html) is None

    def test_relative_url(self) -> None:
        from bs4 import BeautifulSoup
        html = '<html><body><a href="/subject/11111/">豆瓣</a></body></html>'
        soup = BeautifulSoup(html, 'html.parser')
        assert TJUPT._extract_douban_id(soup, html) is None


# ── init ────────────────────────────────────────────────────────────────


class TestInit:
    def test_timeout_defaults(self, mock_config: dict[str, Any]) -> None:
        tjupt = TJUPT(mock_config)
        assert tjupt.timeout_search == 15
        assert tjupt.timeout_upload == 60

    def test_custom_timeout(self, mock_config: dict[str, Any]) -> None:
        mock_config['TRACKERS']['TJUPT']['timeout_search'] = 30
        mock_config['TRACKERS']['TJUPT']['timeout_upload'] = 120
        tjupt = TJUPT(mock_config)
        assert tjupt.timeout_search == 30
        assert tjupt.timeout_upload == 120

    def test_area_map_class_constant(self) -> None:
        assert '中国大陆' in TJUPT.AREA_MAP
        assert TJUPT.AREA_MAP['俄罗斯'] == 4
