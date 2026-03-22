"""MTEAM.py 纯函数单元测试：编码映射、分辨率映射、类型推断"""
import pytest

from src.trackers.MTEAM import (
    _infer_res_from_name,
    _infer_type_from_name,
    _parse_codec_ids_from_mediainfo_text,
    _source_id_to_type,
    _standard_id_to_res,
)


# ============================================================
# _standard_id_to_res
# ============================================================
class TestStandardIdToRes:
    @pytest.mark.parametrize(
        "std_id, expected",
        [
            ("1", "1080p"),
            ("2", "1080i"),
            ("3", "720p"),
            ("5", "SD"),
            ("6", "2160p"),
            ("7", "8K"),
            (1, "1080p"),       # int 输入
            ("99", ""),         # 未知 ID
            ("", ""),
            (None, ""),
        ],
    )
    def test_mapping(self, std_id, expected):
        assert _standard_id_to_res(std_id) == expected


# ============================================================
# _source_id_to_type
# ============================================================
class TestSourceIdToType:
    @pytest.mark.parametrize(
        "src_id, expected",
        [
            ("8", "WEBDL"),
            ("1", "BluRay"),
            ("4", "REMUX"),
            ("5", "HDTV"),
            ("3", "DVD"),
            ("6", "Other"),
            ("999", ""),
        ],
    )
    def test_mapping(self, src_id, expected):
        assert _source_id_to_type(src_id) == expected


# ============================================================
# _parse_codec_ids_from_mediainfo_text
# ============================================================
class TestParseCodecIds:
    @pytest.mark.parametrize(
        "text, expected_video, expected_audio",
        [
            # 基本编码
            ("Video: HEVC, Audio: DTS-HD MA", 16, 11),
            ("Format: AVC, Codec: AAC", 1, 6),
            ("H.265 / E-AC3", 16, 12),
            ("H.264 / TrueHD Atmos", 1, 10),
            ("VC-1 / AC3", 2, 8),
            ("MPEG-2 / FLAC", 4, 1),
            ("AV1 / LPCM", 19, 14),
            # E-AC3 Atmos 特殊处理
            ("DDP5.1 Atmos / E-AC3 Atmos", None, 13),
            # 大小写不敏感
            ("hevc / truehd", 16, 9),
            ("x265 / dts", 16, 3),
            # 空输入
            ("", None, None),
            (None, None, None),
            # 无匹配
            ("random text", None, None),
        ],
    )
    def test_parse(self, text, expected_video, expected_audio):
        vid, aud = _parse_codec_ids_from_mediainfo_text(text)
        assert vid == expected_video
        assert aud == expected_audio


# ============================================================
# _infer_type_from_name
# ============================================================
class TestInferTypeFromName:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("Movie.2024.1080p.WEB-DL.DDP5.1.H.265-GROUP", "WEBDL"),
            # REMUX 在名称中出现但 BluRay 也出现 — _infer_type_from_name 按顺序匹配，BluRay 先匹配
            ("Movie.2024.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-GROUP", "BluRay"),
            ("Movie.2024.1080p.BluRay.x264-GROUP", "BluRay"),
            ("Show.S01E01.HDTV.x264-GROUP", "HDTV"),
            ("Movie.2024.AMZN.WEB-DL", "WEBDL"),
            ("unknown format", ""),
        ],
    )
    def test_infer(self, name, expected):
        assert _infer_type_from_name(name) == expected


# ============================================================
# _infer_res_from_name
# ============================================================
class TestInferResFromName:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("Movie.2024.2160p.UHD.BluRay", "2160p"),
            ("Movie.2024.1080p.WEB-DL", "1080p"),
            ("Movie.2024.720p.HDTV", "720p"),
            ("Movie.2024.480p.DVD", "SD"),
            ("Movie.2024.4K.WEB-DL", "2160p"),
            ("unknown", ""),
        ],
    )
    def test_infer(self, name, expected):
        assert _infer_res_from_name(name) == expected
