"""Tests for TTG tracker helper methods."""
import os
from typing import Any
from unittest.mock import patch

import pytest

from src.trackers.TTG import TTG


@pytest.fixture
def ttg(mock_config: dict[str, Any]) -> TTG:
    return TTG(mock_config)


@pytest.fixture
def base_meta(mock_meta: dict[str, Any]) -> dict[str, Any]:
    return mock_meta


# ── get_type_id ─────────────────────────────────────────────────────────


class TestGetTypeId:
    # Movies
    async def test_movie_720p(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['resolution'] = '720p'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 52

    async def test_movie_1080p(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['resolution'] = '1080p'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 53

    async def test_movie_bdmv(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['resolution'] = '1080p'
        base_meta['is_disc'] = 'BDMV'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 54

    # 2160p takes priority
    async def test_2160p_override(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['resolution'] = '2160p'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 108

    async def test_2160p_bdmv(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['resolution'] = '2160p'
        base_meta['is_disc'] = 'BDMV'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 109

    # TV Singles
    async def test_tv_single_1080p_en(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['resolution'] = '1080p'
        base_meta['original_language'] = 'en'
        base_meta['tv_pack'] = 0
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 70

    async def test_tv_single_720p_en(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['resolution'] = '720p'
        base_meta['original_language'] = 'en'
        base_meta['tv_pack'] = 0
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 69

    async def test_tv_single_1080p_chinese(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['resolution'] = '1080p'
        base_meta['original_language'] = 'zh'
        base_meta['tv_pack'] = 0
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 75

    async def test_tv_single_720p_chinese(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['resolution'] = '720p'
        base_meta['original_language'] = 'zh'
        base_meta['tv_pack'] = 0
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 76

    async def test_tv_single_japanese(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['resolution'] = '1080p'
        base_meta['original_language'] = 'ja'
        base_meta['tv_pack'] = 0
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 73

    async def test_tv_single_korean(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['resolution'] = '1080p'
        base_meta['original_language'] = 'ko'
        base_meta['tv_pack'] = 0
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 75

    # TV Packs
    async def test_tv_pack_en(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['resolution'] = '1080p'
        base_meta['original_language'] = 'en'
        base_meta['tv_pack'] = 1
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 87

    async def test_tv_pack_korean(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['resolution'] = '1080p'
        base_meta['original_language'] = 'ko'
        base_meta['tv_pack'] = 1
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 99

    async def test_tv_pack_japanese(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['resolution'] = '1080p'
        base_meta['original_language'] = 'ja'
        base_meta['tv_pack'] = 1
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 88

    async def test_tv_pack_chinese(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'TV'
        base_meta['resolution'] = '1080p'
        base_meta['original_language'] = 'cn'
        base_meta['tv_pack'] = 1
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 90

    # Documentary
    async def test_documentary_720p(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['resolution'] = '720p'
        base_meta['genres'] = 'Documentary'
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 62

    async def test_documentary_1080p(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['resolution'] = '1080p'
        base_meta['genres'] = 'Documentary'
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 63

    async def test_documentary_bdmv(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['resolution'] = '1080p'
        base_meta['is_disc'] = 'BDMV'
        base_meta['genres'] = 'Documentary'
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 64

    # Animation
    async def test_animation_hd(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'MOVIE'
        base_meta['resolution'] = '1080p'
        base_meta['genres'] = 'Animation'
        base_meta['keywords'] = ''
        base_meta['sd'] = 0
        assert await ttg.get_type_id(base_meta) == 58

    # Default
    async def test_default_zero(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['category'] = 'OTHER'
        base_meta['resolution'] = '480p'
        base_meta['genres'] = ''
        base_meta['keywords'] = ''
        assert await ttg.get_type_id(base_meta) == 0


# ── edit_name ───────────────────────────────────────────────────────────


class TestEditName:
    async def test_removes_dubbed(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['name'] = 'Movie 2024 Dubbed 1080p'
        result = await ttg.edit_name(base_meta)
        assert 'Dubbed' not in result

    async def test_replaces_dots(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['name'] = 'Movie.2024.1080p.WEB-DL'
        result = await ttg.edit_name(base_meta)
        assert '{@}' in result
        assert '.' not in result

    async def test_pq10_to_hdr(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['name'] = 'Movie 2024 2160p PQ10'
        result = await ttg.edit_name(base_meta)
        assert 'HDR' in result
        assert 'PQ10' not in result


# ── Cookie migration ────────────────────────────────────────────────────


class TestCookieMigration:
    async def test_pkl_migration_triggers(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        """validate_credentials should attempt migration when pkl exists but json doesn't."""
        cookies_dir = os.path.join(base_meta['base_dir'], 'data', 'cookies')
        os.makedirs(cookies_dir, exist_ok=True)
        pkl_path = os.path.join(cookies_dir, 'TTG.pkl')
        with open(pkl_path, 'w') as f:
            f.write('fake_pickle_data')

        # Mock the migration loader and validate_cookies
        with patch.object(ttg.cookie_validator, '_load_cookies_dict_secure', return_value={}):
            with patch.object(ttg, 'validate_cookies', return_value=True):
                result = await ttg.validate_credentials(base_meta)

        assert result is True


# ── Unattended mode ─────────────────────────────────────────────────────


class TestUnattendedMode:
    async def test_unattended_no_cookie_returns_false(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['unattended'] = True
        result = await ttg.validate_credentials(base_meta)
        assert result is False

    async def test_unattended_invalid_cookie_returns_false(self, ttg: TTG, base_meta: dict[str, Any]) -> None:
        base_meta['unattended'] = True
        cookies_dir = os.path.join(base_meta['base_dir'], 'data', 'cookies')
        os.makedirs(cookies_dir, exist_ok=True)
        json_path = os.path.join(cookies_dir, 'TTG.json')
        with open(json_path, 'w') as f:
            f.write('{}')

        with patch.object(ttg, 'validate_cookies', return_value=False):
            result = await ttg.validate_credentials(base_meta)

        assert result is False


# ── init ────────────────────────────────────────────────────────────────


class TestInit:
    def test_lang_groups_are_frozenset(self, ttg: TTG) -> None:
        assert isinstance(TTG.LANG_ZH, frozenset)
        assert 'ZH' in TTG.LANG_ZH
        assert 'ZH-TW' in TTG.LANG_ZH
        assert 'KO' in TTG.LANG_KR
        assert 'JA' in TTG.LANG_JP

    def test_timeout_defaults(self, mock_config: dict[str, Any]) -> None:
        t = TTG(mock_config)
        assert t.timeout_search == 15
        assert t.timeout_upload == 60
