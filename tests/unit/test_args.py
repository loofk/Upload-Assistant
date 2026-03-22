"""args.py 单元测试：CLI 参数解析验证"""
import pytest

from src.args import Args


@pytest.fixture
def args_parser(mock_config):
    return Args(config=mock_config)


@pytest.fixture
def empty_meta():
    """parse() 需要一个 meta dict 来填充结果。"""
    return {}


class TestArgsBasicParsing:
    """测试基本参数解析。"""

    def test_basic_path(self, args_parser, empty_meta):
        meta, _parser, _before = args_parser.parse(["/path/to/content"], empty_meta)
        assert meta.get("path") == "/path/to/content"

    def test_u2_source(self, args_parser, empty_meta):
        meta, _, _ = args_parser.parse(["/path/to/content", "-u2", "12345"], empty_meta)
        assert meta.get("u2") is not None

    def test_chd_source(self, args_parser, empty_meta):
        meta, _, _ = args_parser.parse(["/path/to/content", "-chd", "8888"], empty_meta)
        assert meta.get("chd") is not None

    def test_mteam_source(self, args_parser, empty_meta):
        meta, _, _ = args_parser.parse(["/path/to/content", "-mteam", "1133442"], empty_meta)
        assert meta.get("mteam") is not None

    def test_tjupt_source(self, args_parser, empty_meta):
        meta, _, _ = args_parser.parse(["/path/to/content", "-tjupt", "55555"], empty_meta)
        assert meta.get("tjupt") is not None

    def test_unattended_flag(self, args_parser, empty_meta):
        meta, _, _ = args_parser.parse(["/path/to/content", "--unattended"], empty_meta)
        assert meta.get("unattended") is True

    def test_debug_flag(self, args_parser, empty_meta):
        meta, _, _ = args_parser.parse(["/path/to/content", "--debug"], empty_meta)
        assert meta.get("debug") is True

    def test_tracker_single(self, args_parser, empty_meta):
        meta, _, _ = args_parser.parse(["/path/to/content", "-tk", "MTEAM"], empty_meta)
        assert meta.get("trackers") == ["MTEAM"]

    def test_tracker_multiple(self, args_parser, empty_meta):
        meta, _, _ = args_parser.parse(["/path/to/content", "-tk", "MTEAM,TJUPT,TTG"], empty_meta)
        assert "MTEAM" in meta.get("trackers", [])
        assert "TJUPT" in meta.get("trackers", [])
        assert "TTG" in meta.get("trackers", [])

    def test_imdb_manual(self, args_parser, empty_meta):
        meta, _, _ = args_parser.parse(["/path/to/content", "-imdb", "tt1234567"], empty_meta)
        assert meta.get("imdb_manual") is not None

    def test_douban_manual(self, args_parser, empty_meta):
        meta, _, _ = args_parser.parse(["/path/to/content", "-douban", "1291546"], empty_meta)
        # -douban 解析后存储到 douban_id/douban 字段
        assert meta.get("douban_id") == "1291546" or meta.get("douban") == "1291546"
