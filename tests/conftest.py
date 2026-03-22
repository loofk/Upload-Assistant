"""公共 fixture，供所有测试使用。"""
import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def mock_config() -> dict[str, Any]:
    """最小化配置，覆盖转种所需的核心字段。"""
    return {
        "DEFAULT": {
            "tmdb_api": "fake_tmdb_api_key",
            "img_host_1": "imgbb",
            "imgbb_api": "fake_imgbb_key",
            "screens": 3,
            "update_notification": False,
            "suppress_warnings": True,
            "sfx_on_prompt": False,
            "tracker_pass_checks": 1,
        },
        "TRACKERS": {
            "MTEAM": {
                "api_key": "fake_mteam_api_key",
                "uid": 12345,
                "ptgen_api": "",
                "anon": True,
                "img_rehost": False,
            },
            "CHD": {
                "passkey": "fake_chd_passkey",
                "ptgen_api": "",
                "anon": False,
            },
            "U2": {
                "passkey": "fake_u2_passkey",
                "ptgen_api": "",
                "ids_moe_api_key": "fake_ids_moe_key",
                "anon": False,
            },
            "TJUPT": {
                "passkey": "fake_tjupt_passkey",
                "ptgen_api": "",
                "anon": False,
                "username": "",
                "password": "",
                "img_rehost": False,
            },
            "TTG": {
                "username": "",
                "password": "",
                "login_question": "0",
                "login_answer": "",
                "user_id": "",
                "announce_url": "",
            },
            "OB": {
                "passkey": "fake_ob_passkey",
                "ptgen_api": "",
                "anon": False,
                "username": "",
                "password": "",
                "img_rehost": False,
            },
        },
    }


@pytest.fixture
def mock_meta(tmp_path: Any) -> dict[str, Any]:
    """标准 meta 字典，包含转种流程所需的基本字段。"""
    uuid = "test-uuid-1234"
    base_dir = str(tmp_path)
    # 创建必要的目录结构
    os.makedirs(os.path.join(base_dir, "tmp", uuid), exist_ok=True)
    os.makedirs(os.path.join(base_dir, "data", "cookies"), exist_ok=True)

    return {
        "base_dir": base_dir,
        "uuid": uuid,
        "path": "/fake/path/to/content",
        "video": "/fake/path/to/content/video.mkv",

        # 元数据 ID
        "imdb_id": 0,
        "imdb": "0",
        "tmdb_id": 0,
        "tvdb_id": 0,
        "tvmaze_id": 0,
        "mal_id": 0,
        "douban_id": None,
        "douban_url": None,

        # 分类和类型
        "category": "MOVIE",
        "type": "WEBDL",
        "resolution": "1080p",
        "is_disc": None,
        "bdinfo": None,

        # 名称
        "name": "Test Movie 2024 1080p WEB-DL DDP5.1 H.265-GROUP",
        "title": "Test Movie",
        "year": "2024",
        "original_language": "en",

        # 媒体信息
        "mediainfo": None,
        "filelist": ["/fake/path/to/content/video.mkv"],
        "video_codec": "H.265",
        "audio_codec": "E-AC-3",

        # 状态标志
        "debug": False,
        "unattended": False,
        "anime": False,
        "tv_pack": 0,
        "season": None,
        "episode": None,

        # PTGen 数据
        "ptgen": {},

        # Tracker 状态
        "tracker_status": {},
        "anon": 0,

        # HDR
        "hdr": None,
    }


@pytest.fixture
def mock_console():
    """Mock Rich console 以避免终端输出干扰测试。"""
    return MagicMock()
