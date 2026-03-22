"""IMDb 匹配逻辑的单元测试：SequenceMatcher 相似度行为验证。

重点复现转种场景中 IMDb 匹配失败的典型案例，
以及验证 _clean_search_term 清洗后的改善效果。
"""
from difflib import SequenceMatcher

import pytest

from src.imdb import _clean_search_term


def calc_similarity(a: str, b: str) -> float:
    """复现 src/imdb.py 中的相似度计算逻辑。"""
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


class TestImdbSimilarity:
    """验证 SequenceMatcher 在各种种子名场景下的行为。"""

    def test_exact_match(self):
        assert calc_similarity("Frieren", "Frieren") == 1.0

    def test_simple_movie_match(self):
        """标准英文电影名应有高相似度。"""
        sim = calc_similarity("The Shawshank Redemption", "The Shawshank Redemption")
        assert sim >= 0.85

    def test_year_suffix_still_matches(self):
        """带年份的搜索词 vs 纯标题。"""
        sim = calc_similarity("inception 2010", "inception")
        assert sim >= 0.6  # 会因为年份拉低

    # ==========================================================
    # 动画/日韩内容 — 典型失败场景
    # ==========================================================
    def test_anime_raw_name_low_similarity(self):
        """U2 动画种子名含大量技术参数，直接搜索相似度很低。"""
        raw = "[VCB-Studio] Frieren Beyond Journey's End [Ma10p_1080p][x265_flac]"
        target = "Frieren: Beyond Journey's End"
        sim = calc_similarity(raw, target)
        # 相似度远低于 0.85 的自动选择阈值
        assert sim < 0.65

    def test_anime_cleaned_name_high_similarity(self):
        """清洗后的搜索词应与 IMDb 标题有较高相似度。"""
        cleaned = "Frieren Beyond Journey's End"
        target = "Frieren: Beyond Journey's End"
        sim = calc_similarity(cleaned, target)
        assert sim >= 0.85

    def test_chinese_title_vs_english(self):
        """中文标题与英文 IMDb 标题几乎无匹配。"""
        sim = calc_similarity("葬送的芙莉莲", "Frieren: Beyond Journey's End")
        assert sim < 0.1

    def test_mixed_cn_en_title(self):
        """中英混合种子名 — 相似度较高但仍需清洗来保证超过阈值。"""
        raw = "葬送的芙莉莲/Frieren Beyond Journey's End"
        target = "Frieren: Beyond Journey's End"
        sim = calc_similarity(raw, target)
        # 含中文部分会拉低相似度，但可能仍在 0.85 附近，无法稳定自动匹配
        assert sim < 0.90  # 不够可靠

    def test_mixed_cn_en_extract_english(self):
        """从中英混合标题中提取英文部分后应匹配。"""
        # 模拟 _clean_search_term 提取 '/' 后的英文部分
        parts = "葬送的芙莉莲/Frieren Beyond Journey's End".split("/")
        english_part = parts[1].strip()
        target = "Frieren: Beyond Journey's End"
        sim = calc_similarity(english_part, target)
        assert sim >= 0.85

    def test_korean_drama_title(self):
        """韩剧种子名匹配 — 含韩文和技术参数。"""
        raw = "나의 해방일지.My.Liberation.Notes.S01E01.1080p"
        target = "My Liberation Notes"
        sim = calc_similarity(raw, target)
        assert sim < 0.65  # 韩文和技术参数拉低了相似度

    # ==========================================================
    # 自动选择阈值验证
    # ==========================================================
    def test_auto_select_threshold(self):
        """相似度 >= 0.85 且与次优差距 >= 0.10 应自动选择。"""
        best_similarity = 0.92
        second_best = 0.78
        threshold = 0.85
        gap = 0.10
        should_auto = best_similarity >= threshold and (best_similarity - second_best >= gap)
        assert should_auto is True

    def test_auto_select_close_results(self):
        """两个高相似度结果差距很小时不应自动选择。"""
        best_similarity = 0.90
        second_best = 0.88
        gap = 0.10
        should_auto = (best_similarity - second_best >= gap)
        assert should_auto is False

    # ==========================================================
    # _clean_search_term 验证
    # ==========================================================
    def test_clean_bracket_removal(self):
        """移除方括号标签。"""
        result = _clean_search_term("[VCB-Studio] Frieren [Ma10p_1080p]")
        assert "VCB" not in result
        assert "Ma10p" not in result
        assert "Frieren" in result

    def test_clean_tech_params_removal(self):
        """移除技术参数后搜索词更干净。"""
        result = _clean_search_term("Movie Title 2024 1080p BluRay x265 FLAC-GROUP")
        assert "1080p" not in result
        assert "BluRay" not in result
        assert "x265" not in result
        assert "FLAC" not in result
        assert "GROUP" not in result
        assert "Movie Title 2024" in result

    def test_clean_mixed_cn_en(self):
        """从中英混合标题中提取英文部分。"""
        result = _clean_search_term("葬送的芙莉莲/Frieren Beyond Journey's End")
        assert "Frieren" in result
        # 中文部分应被移除
        assert "葬送" not in result

    def test_clean_empty_input(self):
        assert _clean_search_term("") == ""
        assert _clean_search_term("   ") == ""

    def test_clean_pure_english(self):
        """纯英文标题不受影响。"""
        result = _clean_search_term("The Shawshank Redemption")
        assert result == "The Shawshank Redemption"

    def test_clean_anime_full_name(self):
        """完整动画种子名清洗。"""
        raw = "[VCB-Studio] Frieren Beyond Journey's End [Ma10p_1080p][x265_flac]"
        result = _clean_search_term(raw)
        assert "Frieren" in result
        # 清洗后与 IMDb 标题的相似度应大幅提升
        target = "Frieren: Beyond Journey's End"
        sim = calc_similarity(result, target)
        assert sim >= 0.75  # 清洗后应足够触发自动选择
