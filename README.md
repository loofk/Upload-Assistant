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
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --check-runtime --connect-qbit --probe-source --probe-target --json

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
- `sites --json` 暴露每个站点的 `source_info`、`source_info_adapter`、`source_download`、`source_download_adapter`、`credential_requirements`、`target_upload`、`full_live_closure_to_mteam` 能力。
- `rule-check --json` 暴露 `rule_obligations[].review_scope.required_confirmations`，供 agent 在 live 前逐项提示人工确认。
- `flow-check --json` 暴露 `source_capability`、`target_capabilities` 和去重后的 `credential_requirements`，供盒子脚本在 live 前检查配置缺口。
- `pipeline` 和 `retorrent --execute` 返回 `requested_actions`、`effective_actions`、`closure`、`evidence`、`artifacts`、`resume_commands`、`resume_state`、`next_actions`；`requested_actions` 会区分 `source_torrent_file`、`uploaded_torrent_id` 和 `uploaded_torrent_file` 等恢复输入，`evidence.target.mode` / `summary.target.mode` 会标明目标侧是 `live_upload`、`resumed_uploaded_id` 还是 `resumed_uploaded_torrent`。
- `pipeline --write-summary` 会在 `ptcli-run-summary.json` 顶层写入 `flow_check`，并在 `summary.flow` 中保留源站/目标站适配器和凭据要求。
- 带执行动作的命令未闭环时返回 `status: blocked`、顶层 blockers 和非 0 退出码。
- `--write-summary` 会写出带 `automation_handoff` 的 summary JSON，并在本次命令的 JSON 返回中同步给出该字段；其中 `resume_state.next_stage` / `resume_state.next_command` 可供 agent 或脚本直接续跑，`automation_handoff` 则给出检查和执行续跑的 `summary-check` command/argv。
- `summary-check --json` 会暴露 `automation_handoff`、`readiness_summary`、`flow_diagnostics`、`credential_requirements`、`source_mode`、`target_mode`、`automation_reason`、`qbit_wait_retry_hints` 和逐条标注可执行性的 `candidate_commands`；`readiness_summary` 汇总源站、素材/描述、规则、目标上传、上传后做种和下一条命令状态，方便 AI/盒子脚本按关键闭环优先级续跑；`--print-next-command` 可只输出下一条安全续跑命令，`--print-next-argv` 可输出对应 argv JSON；`--print-shell` 可输出 `PTCLI_*` shell 变量（含 `PTCLI_READINESS_*` / `PTCLI_SOURCE_MODE` / `PTCLI_TARGET_MODE` / `PTCLI_AUTOMATION_REASON` / `PTCLI_FLOW_READY` / `PTCLI_CREDENTIAL_REQUIREMENTS` / `PTCLI_RUNNABLE_COMMAND_COUNT` / `PTCLI_CLOSURE_STATUS_*` / `PTCLI_QBIT_WAIT_SOURCE_*` / `PTCLI_QBIT_WAIT_UPLOADED_*`）；`--run-next-command` 可直接执行下一条受限的 `ptcli.py` 续跑命令。
- `summary-check --run-next-command` 只允许执行生成的 `pipeline`、`target-upload` 或 `doctor` 续跑命令；其他 ptcli 命令只会输出拒绝信息，避免自动化误跑只读/检查命令。
- `target-upload --write-summary` 的 `summary.mode` 会标明本轮目标侧是 live 上传、按新种 ID 恢复、本地新种文件恢复、仅准备完成或被阻断。
- `doctor --write-summary` 会写出 `mode` / `target_mode`，区分 live 上传检查、按新种 ID 恢复检查、本地新种文件恢复检查和普通 readiness check。
- `doctor --check-runtime`、`pipeline --target-execute`、`retorrent --execute` 和需要 qBittorrent 注入的 `target-upload` 会检查 focused ptcli 运行时依赖，legacy Web UI/Discord 依赖不是默认要求。

## 配置要求

- qBittorrent client 配置沿用 `data/config.py`。
- 源站 cookie 放在 `data/cookies/<TRACKER>.txt` 或对应适配器要求的位置。
- MTEAM 需要 `TRACKERS.MTEAM.api_key`。
- `Dockerfile.ptcli` 是 focused CLI 镜像，只安装 `requirements-ptcli.txt` 和 ptcli 需要的系统依赖；旧 `Dockerfile` 保留给 legacy/full UA 入口。
- 默认发布构建使用 `Dockerfile.ptcli`，镜像入口是 `ptcli.py`；release 工作流会额外发布 `*-legacy-webui` 标签给旧 Web UI 镜像。
- 旧 `upload.py` 需要显式覆盖 entrypoint、使用 legacy Dockerfile，或拉取 `*-legacy-webui` 标签才会运行。
- `docker-compose.yml` 默认提供 `ptcli` 一次性 CLI 服务，可用 `docker compose run --rm ptcli retorrent ...` 在盒子上执行；legacy Web UI 需要显式 `--profile legacy-webui`。
- live 验证需要在真实盒子环境中提供有效 cookie、MTEAM API key、qBittorrent 连接和实际内容路径。

## Docker/Seedbox

```bash
# 检查 focused CLI 能力矩阵
docker compose run --rm ptcli sites --json

# 本地构建 focused CLI 镜像
docker compose build ptcli

# 盒子上一键闭环示例
docker compose run --rm ptcli retorrent --from U2 --source-id 60635 --to MTEAM --execute --accept-rules --confirm-upload --save-path "/downloads" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json

# 仅在需要旧 Web UI 时启用；该服务使用 *-legacy-webui 镜像或旧 Dockerfile
docker compose --profile legacy-webui up legacy-webui
```

## 开发命令

```bash
pip install -r requirements-ptcli.txt
pip install -r requirements-dev.txt
make smoke-ptcli PYTHON=.venv/bin/python
make smoke PYTHON=.venv/bin/python
make test PYTHON=.venv/bin/python
python3 -m ruff check --config pyproject.toml src/ptcli tests/unit/test_ptcli.py
```

`make smoke` 默认等同于 focused PT CLI smoke。`requirements.txt`、旧 `Dockerfile` 和 `make smoke-legacy` 仍保留给迁移期 legacy/full UA 入口；默认 Docker 镜像使用 `Dockerfile.ptcli` + `requirements-ptcli.txt`。

## 迁移状态

已实现：新 CLI 入口、中文 PT allowlist、规则 obligation 输出、qBittorrent inspect/match/export、源站信息和源种下载、MTEAM 准备/查重/上传预检和 live upload、上传后新种下载/注入/等待、`pipeline`/`retorrent --execute` 闭环证据与恢复命令。

当前优先级：先完成 U2/CHD -> MTEAM 真实闭环。P0 内部按固定顺序推进：源站详情/源种下载/QB 等待完成；IMDb/TMDb、豆瓣/PTGen、MediaInfo/BDInfo、视频截图上传图床和 MTEAM 描述这些关键材料 gate；MTEAM 查重/规则确认/上传/下载新种/QB 做种；最后补齐 summary-check 和续跑命令。之后再补强盒子自动化、扩展更多中文 PT 站点，最后瘦身 Web UI/Discord/海外 tracker 等 legacy 代码。

仍未完成：真实 U2/CHD cookie + MTEAM API + qBittorrent live 环境验证；材料链在真实盒子环境里的截图/图床/IMDb/TMDb/豆瓣/PTGen/MediaInfo/BDInfo 验证；旧 Web UI/Discord/海外 tracker 代码瘦身；所有站点规则的逐站程序化编码；MTEAM 以外目标站的 live upload 闭环。

## 原项目说明

本仓库基于 L4G 的 Upload Assistant 及后续 fork 演进。原始 UA 的 `upload.py` 仍可作为迁移期兼容入口，用于传统媒体信息、截图、描述和多 tracker 上传流程。
