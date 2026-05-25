# Upload Assistant PT CLI

本分支正在把 Upload Assistant 收束为一个适合盒子部署的中文 PT 转种 CLI。当前主入口是 `ptcli.py`：它面向 allowlist 内的中文/PT 站点，输出稳定 JSON，围绕 qBittorrent、源站拉种、MTEAM 目标站准备/查重/上传和上传后做种建立可审计闭环。

原始 `upload.py`、Web UI、Discord bot 和大量海外 tracker 代码仍保留为迁移期兼容内容；`upload.py --help` 会标明这是 legacy 入口。新功能优先在 `ptcli.py` 与 `src/ptcli/` 下推进。

## 当前范围

- 只面向 `src.ptcli.mainland.MAINLAND_PT_TRACKERS` 中的中文/PT 站点。
- 当前 allowlist：`AUDIENCES`, `CHD`, `HDS`, `HDSKY`, `HHAN`, `MTEAM`, `OB`, `PTER`, `TJUPT`, `TTG`, `U2`。
- MTEAM 是当前已实现 live target upload 的目标站。
- U2/CHD -> MTEAM 是最早的参考流；同类 NexusPHP 源站逐步扩展到完整闭环。
- 所有真实下载、上传、qBittorrent 注入动作都要求显式规则确认；上传还要求 `--confirm-upload`。
- 站点规则不由 AI 猜测。CLI 会暴露规则 obligation、每个 obligation 的人工审查范围、确认状态和当前程序化检查范围。

## 常用命令

```bash
# 查看站点和机器可读能力矩阵
python3 ptcli.py sites --json

# 查看规则审查 profile 和 obligation
python3 ptcli.py rule-check --from U2 --to MTEAM --accept-rules --json

# live 前检查，可选探测 qBittorrent、源站和 MTEAM API
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --connect-qbit --probe-source --probe-target --json

# 高层一键闭环：源站拉种 -> QB 注入/等待 -> MTEAM 查重/准备/上传 -> 下载新种 -> QB 注入/等待
python3 ptcli.py retorrent --from U2 --source-id 60635 --to MTEAM --execute --accept-rules --confirm-upload --save-path "/downloads" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json

# 拆分排障：下载源种
python3 ptcli.py source-download --tracker CHD --source-id 12345 --to MTEAM --output-dir ./tmp/source --accept-rules --json

# 拆分排障：从已有源种续跑到源站 QB 等待完成
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --source-torrent-file ./tmp/source/U2-60635.torrent --inject-source --save-path "/downloads" --wait-complete --accept-rules --json

# 拆分排障：准备并上传 MTEAM，随后下载新种、注入 QB、等待完成
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --upload-target --target-torrent-output-dir ./tmp/exported --target-execute --confirm-upload --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json

# 上传已成功但后续中断时，用 MTEAM 新种 ID 恢复下载/注入/等待
python3 ptcli.py target-upload --package-dir ./tmp/target/U2-60635-to-MTEAM --uploaded-torrent-id 999 --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json
```

## AI 友好输出

- 关键命令支持 `--json`。
- `sites --json` 暴露每个站点的 `source_info`、`source_download`、`target_upload`、`full_live_closure_to_mteam` 能力。
- `rule-check --json` 暴露 `rule_obligations[].review_scope.required_confirmations`，供 agent 在 live 前逐项提示人工确认。
- `pipeline` 和 `retorrent --execute` 返回 `closure`、`evidence`、`artifacts`、`resume_commands`、`next_actions`。
- 带执行动作的命令未闭环时返回 `status: blocked`、顶层 blockers 和非 0 退出码。
- `--write-summary` 会写出 `ptcli-run-summary.json`，便于 agent 或脚本续跑。

## 配置要求

- qBittorrent client 配置沿用 `data/config.py`。
- 源站 cookie 放在 `data/cookies/<TRACKER>.txt` 或对应适配器要求的位置。
- MTEAM 需要 `TRACKERS.MTEAM.api_key`。
- live 验证需要在真实盒子环境中提供有效 cookie、MTEAM API key、qBittorrent 连接和实际内容路径。

## 开发命令

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
make smoke-ptcli PYTHON=.venv/bin/python
make test PYTHON=.venv/bin/python
python3 -m ruff check --config pyproject.toml src/ptcli tests/unit/test_ptcli.py
```

## 迁移状态

已实现：新 CLI 入口、中文 PT allowlist、规则 obligation 输出、qBittorrent inspect/match/export、源站信息和源种下载、MTEAM 准备/查重/上传预检和 live upload、上传后新种下载/注入/等待、`pipeline`/`retorrent --execute` 闭环证据与恢复命令。

仍未完成：真实 U2/CHD cookie + MTEAM API + qBittorrent live 环境验证；旧 Web UI/Discord/海外 tracker 代码瘦身；所有站点规则的逐站程序化编码；MTEAM 以外目标站的 live upload 闭环。

## 原项目说明

本仓库基于 L4G 的 Upload Assistant 及后续 fork 演进。原始 UA 的 `upload.py` 仍可作为迁移期兼容入口，用于传统媒体信息、截图、描述和多 tracker 上传流程。
