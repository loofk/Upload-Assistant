# AGENTS.md

本文件为 Codex (Codex.ai/code) 在此仓库中工作时提供指导。

## 项目概述

Upload Assistant (UA) 是一个基于 Python 的工具，用于自动化种子上传到私有 Tracker。功能包括：生成 MediaInfo/BDInfo、截图并上传到图床、从 TMDb/IMDb/TVDB/TVMaze 获取元数据、创建 .torrent 文件、查重、上传到 70+ 个支持的 Tracker 站点。支持 CLI、Discord 机器人和 Web UI 三种界面。

当前新方向是 `ptcli.py`：把 UA 聚焦改造成面向中文 PT 圈的 Docker Compose 本地/盒子部署服务，提供 AI 可调用的 CLI、HTTP API、OpenAPI、OpenClaw/Hermes skill manifest、任务式转种/发种/刷上传工作流。后续 agent 继续目标时，应优先读取 `python3 ptcli.py goal-progress --json` 或 HTTP `/v1/goal/progress`，再根据 `goal_distance_report.next_work` 选择下一步。

### 当前 ptcli 目标状态

已基本落地：
- Docker Compose 部署骨架：`ptcli-api` 常驻服务、健康检查、挂载 config/cookies/downloads/tmp/job 目录、`.env.ptcli.example`、部署检查 `/v1/deployment/check`。
- AI 工具契约：`/openapi.json`、`/v1/tools`、`/.well-known/ptcli-agent.json`、`/v1/openclaw/skill.json`、`/v1/hermes/skill.json`，离线模板位于 `ai/openclaw/ptcli.skill.json` 和 `ai/hermes/ptcli.skill.json`。
- 任务式 API：retorrent/check/from-url/check-and-submit、metadata/materials/target package/target upload、daily candidates job、job status/summary/resume/cancel/list，响应统一暴露 `job_handoff`、`job_progress_handoff`、`recovery_handoff`、`next_call`、`blockers`、`next_actions`。
- 手动源链接转种：`/v1/retorrent/source-url/preflight` 与 `/v1/jobs/retorrent/from-url/check-and-submit` 支持源站链接识别、目标查重、规则 gate、确认 gate、任务创建和后续恢复。
- 素材链路：metadata/PTGen、MediaInfo/BDInfo、截图、图床、目标站 package/upload preflight 已纳入同步 API 与 job API，并通过 final report/handoff 暴露缺口和续跑参数。
- 站点规则配置：`PTCLI.SITE_POLICIES` 支持下载/上传限速、做种要求、转种/促销限制、人工规则审查 fingerprint；`/v1/site-policies` 与 `/v1/site-policies/rule-review` 会给出 rule obligations、配置补丁、copyable config、执行合同和安全 gate。
- qBittorrent 执行：inspect/match/export/inject/wait/limits，任务中会审计 hash/path/size/sha1、限速应用、上传后新种注入和做种证据。
- 每日 10 条候选：`/v1/candidates/daily`、schedule、scheduler、deliver、run-and-deliver、batch/refill、候选提交 job 已有服务入口；候选 digest 会暴露查重、元数据、可下载性、策略风险、提交入口和审批要求。

仍未完成或仍需真实环境证明：
- U2/CHD -> MTEAM 在真实盒子、真实 cookie/API、真实 qBittorrent 上的端到端 live 验证；只有 `live_validation_completion_audit.report_allowed=true` 且 blockers/缺失证据为空，才能向用户宣告闭环完成。
- 更多中文 PT 站点的完整 source discovery/source info/source download/target upload adapter；当前优先保证 U2、CHD 到 MTEAM，其他站点应按 `/v1/sites` 的 `site_extension_readiness_final_report` 补齐。
- 每个站点上传/下载/转种规则的逐站维护与程序化 gate；无法程序化判断的规则必须保留人工 review obligation，不得由 AI 猜测。
- 每日候选的主动推送渠道和真实运行验收；当前已有本地文件/webhook 载荷与 scheduler，但仍需盒子环境验证。
- legacy 瘦身：旧 Web UI、Discord、海外 tracker 和无关代码应在关键服务闭环稳定后再清理。

## 常用命令

### 运行
```bash
# CLI 用法（主入口）
python3 upload.py "/path/to/content" --args

# 聚焦版 PT 转种 CLI（新功能入口）
python3 ptcli.py sites --json
python3 ptcli.py goal-progress --from U2 --source-id 60635 --target MTEAM --downloads-path /downloads --json
python3 ptcli.py serve --host 127.0.0.1 --port 8080
python3 ptcli.py daily-schedule --write-summary --write-notification --json
python3 ptcli.py daily-scheduler --once --write-summary --write-notification --json
python3 ptcli.py rules --trackers MTEAM,TJUPT --json
python3 ptcli.py site-policy-rule-review --from U2 --to MTEAM --json
python3 ptcli.py site-policy-rule-review --source-url "https://u2.dmhy.org/details.php?id=60635" --to MTEAM --rules-reviewed --reviewer "<reviewer>" --reviewed-at "<YYYY-MM-DD>" --json
python3 ptcli.py site-policy-rule-review --source-url "https://u2.dmhy.org/details.php?id=60635" --to MTEAM --rules-reviewed --reviewer "<reviewer>" --reviewed-at "<YYYY-MM-DD>" --print-python-update-snippet
python3 ptcli.py rule-check --from U2 --to MTEAM --accept-rules --json
python3 ptcli.py source-info --tracker U2 --source-id 60635 --json
python3 ptcli.py source-download --tracker CHD --source-id 12345 --to MTEAM --output-dir ./tmp/source --accept-rules --json
python3 ptcli.py flow-check --from U2 --source-id 60635 --to MTEAM --json
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --package-dir ./tmp/target/U2-60635-to-MTEAM --target-torrent-file ./tmp/exported/mteam.torrent --accept-rules --target-execute --confirm-upload --write-summary --json
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --source-torrent-file ./tmp/source/U2-60635.torrent --package-dir ./tmp/target/U2-60635-to-MTEAM --uploaded-torrent-file ./tmp/uploaded/MTEAM-999.torrent --accept-rules --target-execute --confirm-upload --json
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --connect-qbit --json
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --probe-source --probe-target --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --output-dir ./tmp/source --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --source-torrent-file ./tmp/source/U2-60635.torrent --inject-source --save-path "/downloads" --wait-complete --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --inject-source --save-path "/downloads" --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --inject-source --save-path "/downloads" --wait-complete --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --upload-target --target-torrent-output-dir ./tmp/exported --target-execute --confirm-upload --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --save-path "/downloads" --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --upload-target --target-torrent-output-dir ./tmp/exported --target-execute --confirm-upload --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --package-dir ./tmp/target/U2-60635-to-MTEAM --upload-target --target-torrent-file ./tmp/exported/mteam.torrent --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --upload-target --target-torrent-file ./tmp/exported/mteam.torrent --target-execute --confirm-upload --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --package-dir ./tmp/target/U2-60635-to-MTEAM --upload-target --uploaded-torrent-file ./tmp/uploaded/MTEAM-999.torrent --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --wait-uploaded-complete --json
python3 ptcli.py target-upload --package-dir ./tmp/target/U2-60635-to-MTEAM --torrent-file ./tmp/exported/mteam.torrent --write-payload --json
python3 ptcli.py target-upload --config data/config.py --package-dir ./tmp/target/U2-60635-to-MTEAM --torrent-file ./tmp/exported/mteam.torrent --execute --confirm-upload --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json
python3 ptcli.py target-upload --config data/config.py --package-dir ./tmp/target/U2-60635-to-MTEAM --uploaded-torrent-id 999 --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json
python3 ptcli.py target-upload --config data/config.py --package-dir ./tmp/target/U2-60635-to-MTEAM --uploaded-torrent-file ./tmp/uploaded/MTEAM-999.torrent --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json
python3 ptcli.py inspect --client default --limit 20 --json
python3 ptcli.py match --path "/downloads/content" --json
python3 ptcli.py export --hash "<infohash>" --output-dir ./tmp/exported --json
python3 ptcli.py retorrent --from MTEAM --source-id 12345 --to TJUPT,CHD --path "/downloads/content" --dry-run
python3 ptcli.py retorrent --from U2 --source-id 60635 --to MTEAM --execute --accept-rules --confirm-upload --save-path "/downloads" --target-torrent-file ./tmp/exported/mteam.torrent --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json
python3 ptcli.py retorrent --from U2 --source-id 60635 --to MTEAM --execute --accept-rules --confirm-upload --save-path "/downloads" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json
python3 ptcli.py retorrent --from U2 --source-id 60635 --to MTEAM --execute --accept-rules --confirm-upload --source-torrent-file ./tmp/source/U2-60635.torrent --save-path "/downloads" --uploaded-torrent-file ./tmp/uploaded/MTEAM-999.torrent --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json

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
docker compose up -d --build ptcli-api
docker compose --profile daily up -d ptcli-daily-scheduler
docker compose --profile daily run --rm ptcli-daily-schedule
```

## 架构

### 入口文件
- **`upload.py`** — 主入口（约 100KB）。编排整个上传流程：元数据收集 → 截图 → 种子创建 → Tracker 上传。核心处理函数为 `do_the_thing()`。同时处理 Web UI 服务启动和优雅关闭。
- **`ptcli.py`** — 聚焦版 PT 转种 CLI 入口。默认仅面向 allowlist 内的中文/PT 站点，支持可审计计划（`retorrent --dry-run` 的 plan 内含 `rule_check.rule_obligations`）、`rule-check` 可执行规则门禁（输出逐站点 `rule_obligations`，区分源站 `download_and_retorrent` 与目标站 `upload_and_seed`，并写入 MTEAM rule review package；live upload preflight 和 doctor 都会强校验这些 obligation 的 scope、规则 URL 和确认状态；pipeline summary 的 `gates.rule_review.rule_obligations` 以及 target-upload summary 的 `rule_obligations` 会暴露 ready/missing 状态）、`retorrent --execute` 一键编排、live 前 doctor 检查（可选真实 qBittorrent/源站/MTEAM API 连通性探针；`doctor --target-execute` 会按 live pipeline 默认行为检查上传后下载、注入和等待目标站种子；可检查 `--source-torrent-file`、`--package-dir`、`--uploaded-torrent-file` 等重入产物；`rule_obligations` 是 `live_safe_to_attempt` 的必要条件；未达到 ready/live safe 时返回非 0；`--write-summary` 会写出 `ptcli-doctor-summary.json`）、源站信息/种子下载、qBittorrent 检查/注入/等待（优先沿用源站 hash，注入时会从 `.torrent` 解析真实 infohash 并用于后续等待；源种下载/续跑结果包含 `exists`、`size_bytes`、`sha1`、`torrent_hash`/`infohash` 文件证据）、从 QB 完成结果推导内容路径、从匹配 QB 任务导出或手动传入目标种子并清理为 MTEAM 上传候选种子、MTEAM 目标站准备包与查重、上传种子安全状态预检；`pipeline` 和 `retorrent --execute` 顶层提供带 `complete`/`blockers` 的 `closure` 汇总，source 侧闭环支持“源种下载+QB 注入+等待完成”“已有源种文件注入+等待完成”或“已有路径已匹配 QB 任务”等证据，并在 evidence/summary 中暴露 `source_torrent`、`source_torrent_path` 和 `source_wait`；`pipeline --target-execute` 会在需要时自动执行源种下载/注入/等待、目标 torrent 导出/净化，以及上传后 MTEAM 新种下载/注入/等待；`pipeline --write-summary` 会把 `closure`、`summary`、`evidence` 和 next actions 写入 `ptcli-run-summary.json`；MTEAM live upload 仅在 `target-upload --execute --confirm-upload`、`pipeline --target-execute --confirm-upload` 或 `retorrent --execute --confirm-upload` 且 gate/payload ready、目标种子通过 MTEAM-safe 元数据门禁时启用；`target-upload --execute` 必须同时请求下载并注入上传后生成的 MTEAM 种子，也可用 `--uploaded-torrent-file` 从已下载的新种恢复注入/等待；target-upload summary 会返回 `uploaded_torrent`、`uploaded_torrent_path`、`uploaded_torrent_hash`、`injected_torrent_hash`、`injection_verified`、`uploaded_wait`，其中 `uploaded_torrent` / `downloaded_torrent` 包含 `exists`、`size_bytes`、`sha1`、`torrent_hash`/`infohash` 文件证据，便于独立续跑审计；高层 `retorrent --execute` 会默认下载并注入目标站种子，未传 `--target-torrent-file` 时会默认从匹配 QB 任务导出目标候选种子，只有完整闭环时才返回顶层 `status: complete`，否则返回 `status: blocked` 和 blockers 并以非 0 退出码结束；带执行动作的 `pipeline` 未 ready 时返回 `status: blocked`、顶层 blockers 和非 0 退出码，并在 `evidence` 汇总 source/target 的 hash、路径、闭环模式和 `resume` 重入来源（`source_torrent_file`、`target_package`、`uploaded_torrent_file`，源种文件续跑时 source mode 为 `resumed_torrent`），`retorrent --execute` 也会把该 evidence 提升到顶层，`target-upload --execute` 或 `target-upload --uploaded-torrent-file` follow-up 只有实际完成请求的下载/注入/等待动作时返回 0，返回 `uploaded_torrent_hash` 便于审计。
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
# 本地/盒子服务模式
cp .env.ptcli.example .env
# 编辑 .env 中的 PTCLI_API_TOKEN、PTCLI_PUBLIC_BASE_URL、config/cookies/downloads/tmp 路径和每日候选 schedule
docker compose up -d --build ptcli-api
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/openapi.json
curl -fsS http://127.0.0.1:8080/v1/tools

# 读取当前目标进度和下一步
docker compose --profile cli run --rm ptcli goal-progress --from U2 --source-id 60635 --target MTEAM --downloads-path /downloads --json
```

Docker 镜像由 `.github/workflows/docker-build.yml` 在 push 时构建。`Dockerfile.ptcli` 会打包 ptcli 服务入口和 `ai/` skill 模板。

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
