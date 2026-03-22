"""Tests for OB (OurBits) tracker helper methods."""
from typing import Any

import pytest

from src.trackers.OB import OB


@pytest.fixture
def ob(mock_config: dict[str, Any]) -> OB:
    return OB(mock_config)


@pytest.fixture
def base_meta(mock_meta: dict[str, Any]) -> dict[str, Any]:
    return mock_meta


# ── get_type_category_id ────────────────────────────────────────────────


class TestGetTypeCategoryId:
    async def test_movie(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ob.get_type_category_id(base_meta) == '401'

    async def test_tv(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ob.get_type_category_id(base_meta) == '404'

    async def test_documentary(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['genres'] = 'Documentary'
        base_meta['keywords'] = ''
        assert await ob.get_type_category_id(base_meta) == '402'

    async def test_animation(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['genres'] = 'Animation'
        base_meta['keywords'] = ''
        assert await ob.get_type_category_id(base_meta) == '405'

    async def test_variety(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['genres'] = 'variety'
        base_meta['keywords'] = ''
        assert await ob.get_type_category_id(base_meta) == '403'

    async def test_default(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'OTHER'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ob.get_type_category_id(base_meta) == '0'


# ── get_type_medium_id ──────────────────────────────────────────────────


class TestGetTypeMediumId:
    async def test_uhd_disc(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = 'BDMV'
        base_meta['resolution'] = '2160p'
        assert await ob.get_type_medium_id(base_meta) == '1'

    async def test_bd_disc(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = 'BDMV'
        base_meta['resolution'] = '1080p'
        assert await ob.get_type_medium_id(base_meta) == '2'

    async def test_dvd(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = 'DVD'
        assert await ob.get_type_medium_id(base_meta) == '7'

    async def test_remux(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = None
        base_meta['type'] = 'REMUX'
        assert await ob.get_type_medium_id(base_meta) == '3'

    async def test_webdl(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = None
        base_meta['type'] = 'WEBDL'
        assert await ob.get_type_medium_id(base_meta) == '5'

    async def test_encode(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = None
        base_meta['type'] = 'ENCODE'
        assert await ob.get_type_medium_id(base_meta) == '6'

    async def test_unknown_returns_zero(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = None
        base_meta['type'] = 'UNKNOWN'
        assert await ob.get_type_medium_id(base_meta) == '0'


# ── get_area_id ─────────────────────────────────────────────────────────


class TestGetAreaId:
    async def test_china(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["中国大陆"]}
        assert await ob.get_area_id(base_meta) == 1

    async def test_usa(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["美国"]}
        assert await ob.get_area_id(base_meta) == 4

    async def test_japan(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["日本"]}
        assert await ob.get_area_id(base_meta) == 6

    async def test_unknown(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['ptgen'] = {"region": ["未知国家"]}
        assert await ob.get_area_id(base_meta) == 8

    def test_shares_area_map_with_tjupt(self) -> None:
        from src.trackers.TJUPT import TJUPT
        assert OB.AREA_MAP is TJUPT.AREA_MAP


# ── edit_name ───────────────────────────────────────────────────────────


class TestEditName:
    async def test_removes_dubbed(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['name'] = 'Movie 2024 1080p Dubbed WEB-DL'
        result = await ob.edit_name(base_meta)
        assert 'Dubbed' not in result

    async def test_pq10_to_hdr(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['name'] = 'Movie 2024 2160p PQ10 WEB-DL'
        result = await ob.edit_name(base_meta)
        assert 'HDR' in result
        assert 'PQ10' not in result


# ── is_zhongzi ──────────────────────────────────────────────────────────


class TestIsZhongzi:
    async def test_chinese_sub(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = ''
        base_meta['mediainfo'] = {
            'media': {
                'track': [
                    {'@type': 'Video'},
                    {'@type': 'Text', 'Language': 'zh'},
                ]
            }
        }
        assert await ob.is_zhongzi(base_meta) == 'yes'

    async def test_no_chinese(self, ob: OB, base_meta: dict[str, Any]) -> None:
        base_meta['is_disc'] = ''
        base_meta['mediainfo'] = {
            'media': {
                'track': [
                    {'@type': 'Video'},
                    {'@type': 'Text', 'Language': 'en'},
                ]
            }
        }
        assert await ob.is_zhongzi(base_meta) is None


# ── init ────────────────────────────────────────────────────────────────


class TestInit:
    def test_basic_init(self, mock_config: dict[str, Any]) -> None:
        ob = OB(mock_config)
        assert ob.tracker == 'OB'
        assert ob.source_flag == 'OB'
        assert ob.passkey == 'fake_ob_passkey'

    def test_timeout_defaults(self, mock_config: dict[str, Any]) -> None:
        ob = OB(mock_config)
        assert ob.timeout_search == 15
        assert ob.timeout_upload == 60
