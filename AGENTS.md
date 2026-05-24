# AGENTS.md

本文件为 Codex (Codex.ai/code) 在此仓库中工作时提供指导。

## 项目概述

Upload Assistant (UA) 是一个基于 Python 的工具，用于自动化种子上传到私有 Tracker。功能包括：生成 MediaInfo/BDInfo、截图并上传到图床、从 TMDb/IMDb/TVDB/TVMaze 获取元数据、创建 .torrent 文件、查重、上传到 70+ 个支持的 Tracker 站点。支持 CLI、Discord 机器人和 Web UI 三种界面。

## 常用命令

### 运行
```bash
# CLI 用法（主入口）
python3 upload.py "/path/to/content" --args

# 聚焦版 PT 转种 CLI（新功能入口）
python3 ptcli.py sites --json
python3 ptcli.py rules --trackers MTEAM,TJUPT --json
python3 ptcli.py source-info --tracker U2 --source-id 60635 --json
python3 ptcli.py source-download --tracker CHD --source-id 12345 --output-dir ./tmp/source --json
python3 ptcli.py flow-check --from U2 --source-id 60635 --to MTEAM --json
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --package-dir ./tmp/target/U2-60635-to-MTEAM --target-torrent-file ./tmp/exported/mteam.torrent --accept-rules --target-execute --confirm-upload --download-uploaded-torrent --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --output-dir ./tmp/source --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --inject-source --save-path "/downloads" --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --inject-source --save-path "/downloads" --wait-complete --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --inject-source --save-path "/downloads" --wait-complete --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --upload-target --target-torrent-file ./tmp/exported/mteam.torrent --target-execute --confirm-upload --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --upload-target --target-torrent-file ./tmp/exported/mteam.torrent --target-execute --confirm-upload --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --json
python3 ptcli.py target-upload --package-dir ./tmp/target/U2-60635-to-MTEAM --torrent-file ./tmp/exported/mteam.torrent --write-payload --json
python3 ptcli.py target-upload --config data/config.py --package-dir ./tmp/target/U2-60635-to-MTEAM --torrent-file ./tmp/exported/mteam.torrent --execute --confirm-upload --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --json
python3 ptcli.py inspect --client default --limit 20 --json
python3 ptcli.py match --path "/downloads/content" --json
python3 ptcli.py export --hash "<infohash>" --output-dir ./tmp/exported --json
python3 ptcli.py retorrent --from MTEAM --source-id 12345 --to TJUPT,CHD --path "/downloads/content" --dry-run

# Web UI 模式
python3 upload.py --webui 0.0.0.0:5000

# 生成/更新配置文件
python3 config-generator.py
```

### 安装依赖
```bash
# 运行依赖
pip install -r requirements.txt

# 开发/检查依赖（lint、test、type check、安全扫描）
pip install -r requirements-dev.txt
```

### 代码检查
```bash
# Ruff 检查（配置在 pyproject.toml，行宽 176，目标 py3.9）
python3 -m ruff check --config pyproject.toml .

# 类型检查
python3 -m pyright .

# 安全分析
python3 -m bandit -r . -c bandit.yaml
```

### Docker
```bash
docker compose up  # 使用 docker-compose.yml
```

## 架构

### 入口文件
- **`upload.py`** — 主入口（约 100KB）。编排整个上传流程：元数据收集 → 截图 → 种子创建 → Tracker 上传。核心处理函数为 `do_the_thing()`。同时处理 Web UI 服务启动和优雅关闭。
- **`ptcli.py`** — 聚焦版 PT 转种 CLI 入口。默认仅面向 allowlist 内的中文/PT 站点，支持可审计计划、live 前 doctor 检查、源站信息/种子下载、qBittorrent 检查/注入/等待、从 QB 完成结果推导内容路径、MTEAM 目标站准备包与查重；MTEAM live upload 仅在 `target-upload --execute --confirm-upload` 且 gate/payload ready 时启用，并可显式下载/注入目标站种子。
- **`discordbot.py`** — 基于 discord.py 的 Discord 机器人接口，调用相同的上传流程。
- **`web_ui/server.py`** — 基于 Flask 的 Web UI，包含认证（argon2 + TOTP）、会话管理和文件浏览。
- **`config-generator.py`** — 交互式配置文件生成/更新工具。

### 核心源码 (`src/`)
- **`prep.py`** — `Prep` 类：核心元数据准备。收集媒体信息、识别内容类型、解析 TMDb/IMDb ID、生成名称，构建贯穿整个流程的 `meta` 字典。
- **`trackersetup.py`** — 定义 `TRACKER_SETUP` 字典和分类列表（`api_trackers`、`http_trackers`、`other_api_trackers`），通过 `tracker_class_map` 将 Tracker 缩写映射到类实例。
- **`trackerhandle.py`** — 处理跨 Tracker 的上传（遍历、查重、上传）。
- **`trackers/`** — 约 80 个 Tracker 相关模块，`tracker_class_map` 当前注册 70+ 个 Tracker（如 `BLU.py`、`PTP.py`）。各自实现站点特定的上传逻辑。`COMMON.py` 提供共享的 Tracker 功能。
- **`torrent_clients/`** — 下载客户端集成：qBittorrent、rTorrent、Deluge、Transmission。
- **`args.py`** — 基于 argparse 的 CLI 参数解析。
- **`torrentcreate.py`** — 种子文件创建（支持 mkbrr 二进制工具）。
- **`takescreens.py`** / `uploadscreens.py` — 截图（ffmpeg）和上传到图床。
- **`tmdb.py`**、`imdb.py`、`tvdb.py`、`tvmaze.py` — 元数据提供方集成。
- **`get_name.py`** — 生成符合站点规则的标准化发布名称。
- **`dupe_checking.py`** — 在目标 Tracker 上检查是否已存在相同上传。
- **`clients.py`** — 种子客户端添加/做种操作。

### 数据流
1. 用户提供路径 → `Args` 解析 CLI 参数
2. `Prep` 类构建 `meta` 字典（媒体信息、元数据 ID、截图、名称）
3. `upload.py` 通过 `trackerhandle.py` 遍历选定的 Tracker
4. `src/trackers/` 中的各 Tracker 模块处理站点特定的 API/表单上传
5. 通过 `src/torrent_clients/` 将种子添加到客户端做种

### 配置
- **`data/config.py`** — 用户配置文件（不提交到 Git）。从 `data/example-config.py` 创建。
- 配置是一个 Python 字典，包含以下部分：`DEFAULT`（全局设置、图床）、`TRACKERS`（各 Tracker 的 API 密钥/凭据）、`DISCORD`（机器人设置）。
- `data/tags.json` — 发布分类的标签定义。

### 辅助目录
- **`bin/`** — 外部二进制文件及下载器：mkbrr（种子创建）、BDInfo、MediaInfo（DVD 变体）、ffmpeg 辅助脚本。
- **`cogs/`** — Discord 机器人模块（如 `redaction.py` 用于日志脱敏）。
- **`web_ui/`** — Flask 应用，包含 Jinja2 模板和静态资源。

## 代码风格
- 要求兼容 Python 3.9+（使用 typing_extensions）
- Ruff 配置行宽 176；import 排序将 `cogs`、`data`、`src`、`web_ui` 视为 first-party
- 全面使用异步：核心函数大多为 `async def`，HTTP 请求使用 aiohttp/httpx
- `meta` 字典（类型 `dict[str, Any]`）是贯穿整个流程的核心数据结构
- 终端输出使用 Rich console（`src/console.py`）

## 开发工作流

### Make 命令
```bash
make help        # 查看所有可用命令
make lint        # ruff 代码检查
make test        # pytest（自动排除 live 测试）
make check       # lint + test 一起跑
make smoke       # 快速导入检查（验证核心模块可加载）
make test-live   # 实时集成测试（需要 data/cookies + data/config.py）

# 如需指定解释器：
make smoke PYTHON=.venv/bin/python
```

### 开发 → 部署流程
1. 本地 Mac 开发、`make check` 通过
2. `git push` 到 GitHub
3. GitHub Actions 自动构建 Docker 镜像推送到 `ghcr.io`
4. Seedbox 上 `docker pull` 更新并运行

## 部署方式

### 方式一：Git 部署（传统）
```bash
# Seedbox 上
cd /root/Upload-Assistant
git pull
python3 upload.py "/path/to/content" --trackers MTEAM -u2 60635
```

### 方式二：Docker 部署（推荐）
```bash
# 拉取镜像
docker pull ghcr.io/loofk/upload-assistant:latest

# 运行（CLI 模式）
docker run --rm -it \
  -v /root/config.py:/Upload-Assistant/data/config.py \
  -v /root/cookies:/Upload-Assistant/data/cookies \
  -v /home/user/Downloads:/downloads \
  ghcr.io/loofk/upload-assistant:latest \
  "/downloads/[BDMV]..." --trackers MTEAM -u2 60635
```

Docker 镜像由 `.github/workflows/docker-build.yml` 在每次 push 到 master 时自动构建（仅 amd64）。

## 测试策略

三层测试体系：

| 层级 | 标记 | 运行方式 | 说明 |
|------|------|----------|------|
| Unit | `@pytest.mark.unit` | `make test` | 纯逻辑测试，无网络依赖 |
| Integration | `@pytest.mark.integration` | `make test` | 使用 mock HTTP 的集成测试 |
| Live | `@pytest.mark.live` | `make test-live` | 真实网络请求，需要 cookies 和配置 |

### 如何添加新 Tracker 测试用例

1. 编辑 `tests/fixtures/known_torrents.json`，添加新条目：
```json
{
  "source": "tracker_abbrev",
  "torrent_id": "12345",
  "expected": {
    "imdb_id": 1234567,
    "douban_id": "7654321"
  },
  "description": "简要描述"
}
```
2. 如果是新 Tracker，在 `tests/integration/test_live_tracker_metadata.py` 的 `_get_tracker_instance()` 中添加映射
3. 确保 `data/cookies/<TRACKER>.txt` 存在且有效
4. 运行 `make test-live` 验证
