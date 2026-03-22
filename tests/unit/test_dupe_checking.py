"""dupe_checking.py 单元测试：normalize_filename, is_season_episode_match, refine_hdr_terms, has_matching_hdr"""
import pytest

from src.dupe_checking import (
    DupeChecker,
    has_matching_hdr,
    is_season_episode_match,
    normalize_filename,
    refine_hdr_terms,
)


# ============================================================
# normalize_filename
# ============================================================
class TestNormalizeFilename:
    @pytest.mark.parametrize(
        "input_val, expected",
        [
            # 基本替换：'-' 前加空格, '.' 替换为空格
            ("Test-Movie.2024", "test -movie 2024"),
            # 纯字符串
            ("hello world", "hello world"),
            # 含 dict
            ({"name": "My.Movie-2024"}, "my movie -2024"),
            # 空字符串
            ("", ""),
            # 多空格合并（注意当前实现只是替换 " " -> " "，不是多空格合并）
            ("Test  Movie", "test  movie"),
            # 中文标题
            ("葬送的芙莉莲.S01E01", "葬送的芙莉莲 s01e01"),
        ],
    )
    async def test_normalize(self, input_val, expected):
        result = await normalize_filename(input_val)
        assert result == expected

    async def test_normalize_dict_missing_name(self):
        result = await normalize_filename({"other_key": "value"})
        assert result == ""

    async def test_normalize_invalid_type_raises(self):
        with pytest.raises(ValueError):
            await DupeChecker.normalize_filename(123)  # type: ignore


# ============================================================
# is_season_episode_match
# ============================================================
class TestIsSeasonEpisodeMatch:
    @pytest.mark.parametrize(
        "filename, season, episode, expected",
        [
            # 完整 S01E01 匹配
            ("Show.S01E01.1080p", "S01", "E01", (True, False)),
            # 季包（无 E 编号）
            ("Show.S01.Complete.1080p", "S01", None, (True, True)),
            # 季包 vs 有 episode
            ("Show.S01.Complete.1080p", "S01", "E01", (True, True)),
            # 不匹配的季
            ("Show.S02E01.1080p", "S01", "E01", (False, False)),
            # Episode 不匹配
            ("Show.S01E02.1080p", "S01", "E01", (False, False)),
            # 无 season 信息
            ("Show.E01.1080p", None, "E01", (False, False)),
            # 电影（无 S/E）— 注意：is_season_episode_match 对 "S01" 搜索但文件名中没有时返回 (False, True) 因为 is_season_pack 为 True
            ("Movie.2024.1080p", "S01", "E01", (False, True)),
        ],
    )
    async def test_match(self, filename, season, episode, expected):
        result = await is_season_episode_match(filename, season, episode)
        assert result == expected


# ============================================================
# refine_hdr_terms
# ============================================================
class TestRefineHdrTerms:
    @pytest.mark.parametrize(
        "hdr_input, expected",
        [
            (None, set()),
            ("", set()),
            ("HDR", {"HDR"}),
            ("HDR10", {"HDR"}),
            ("HDR10+", {"HDR"}),
            ("DV", {"DV"}),
            ("DoVi", {"DV"}),
            ("DV HDR", {"DV", "HDR"}),
            ("Dolby Vision HDR10", {"HDR"}),  # "Dolby Vision" 不含 "DV"/"DOVI" 子串
            ("SDR", set()),
        ],
    )
    async def test_refine(self, hdr_input, expected):
        result = await refine_hdr_terms(hdr_input)
        assert result == expected


# ============================================================
# has_matching_hdr
# ============================================================
class TestHasMatchingHdr:
    async def test_both_hdr(self, mock_meta):
        assert await has_matching_hdr({"HDR"}, {"HDR"}, mock_meta) is True

    async def test_both_empty(self, mock_meta):
        assert await has_matching_hdr(set(), set(), mock_meta) is True

    async def test_mismatch(self, mock_meta):
        assert await has_matching_hdr({"HDR"}, set(), mock_meta) is False

    async def test_dv_hdr_treated_as_hdr(self, mock_meta):
        """DV+HDR 组合应简化为 HDR 用于比较"""
        result = await has_matching_hdr({"DV", "HDR"}, {"HDR"}, mock_meta)
        assert result is True

    async def test_dv_only_non_web(self, mock_meta):
        """非 WEB 源的 DV 应包含 HDR"""
        mock_meta["type"] = "BluRay"
        result = await has_matching_hdr({"DV"}, {"DV"}, mock_meta)
        # DV non-web -> simplified to {DV, HDR}, 两边相同
        assert result is True

    async def test_ant_tracker_dv(self, mock_meta):
        """ANT tracker 的 DV 应始终包含 HDR"""
        mock_meta["type"] = "WEBDL"
        result = await has_matching_hdr({"DV"}, {"DV"}, mock_meta, tracker="ANT")
        assert result is True
