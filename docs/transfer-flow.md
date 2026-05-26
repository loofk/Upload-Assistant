# PTCLI 转种闭环流程

本文档描述当前 `ptcli.py` 的中文 PT 转种流程。旧 `upload.py` 的传统上传流程仍保留为迁移期兼容入口，并在帮助信息中标明 legacy 状态；新功能以 `ptcli.py` 为准。

## 目标

`ptcli.py` 的目标是在盒子上完成可审计、可恢复、对 AI 友好的转种闭环：

```text
源站详情/源种 -> qBittorrent 下载或匹配 -> 目标站查重/准备 -> MTEAM 上传 -> 下载新种 -> qBittorrent 注入/等待完成
```

当前 live target upload 只面向 MTEAM。完整闭环源站以 `python3 ptcli.py sites --json` 输出的 `full_live_closure_sources` 为准。

## 一键闭环

```bash
python3 ptcli.py retorrent --from U2 --source-id 60635 --to MTEAM --execute --accept-rules --confirm-upload --save-path "/downloads" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json
```

`retorrent --execute` 会编排完整 pipeline；`retorrent --dry-run` 输出的 `retorrent-execute` 模板也会显式包含上传后新种下载、注入和等待完成这三个 follow-up flag，便于审计完整闭环意图：

1. 检查源站、目标站、规则确认和本地配置。
2. 读取源站详情。
3. 下载源站 `.torrent`。
4. 注入 qBittorrent 并等待下载完成。
5. 从完成的 qBittorrent 任务推导内容路径。
6. 对 MTEAM 做查重。
7. 生成 MTEAM 准备包、描述、字段映射和 upload gate。
8. 从 qBittorrent 导出目标候选种子并清理为 MTEAM-safe torrent。
9. 执行 MTEAM 上传。
10. 下载 MTEAM 上传后生成的新种。
11. 把新种注入 qBittorrent，并等待任务完成。

只有 `closure.complete=true` 且所有请求动作完成时，顶层 `status` 才会是 `complete`。否则命令返回 `status: blocked`、`blockers`、`next_actions` 和非 0 退出码。

## 拆分排障命令

### 能力矩阵

```bash
python3 ptcli.py sites --json
```

重点字段：

- `capabilities.<TRACKER>.source_info`
- `capabilities.<TRACKER>.source_info_adapter`
- `capabilities.<TRACKER>.source_download`
- `capabilities.<TRACKER>.source_download_adapter`
- `capabilities.<TRACKER>.credential_requirements`
- `capabilities.<TRACKER>.target_upload`
- `capabilities.<TRACKER>.full_live_closure_to_mteam`
- `full_live_closure_sources`
- `flows`

### 规则门禁

```bash
python3 ptcli.py rule-check --from U2 --to MTEAM --accept-rules --json
```

输出会包含 source 的 `download_and_retorrent` obligation 和 target 的 `upload_and_seed` obligation。每个 obligation 都带有 `review_scope.required_confirmations`，用于提示 live 前必须人工确认的下载、转载、上传、分类和做种范围。当前 `site_specific_rules_encoded=false`，表示程序不会替用户推断站规，live 前仍必须人工审阅并确认源站/目标站规则。

### live 前检查

```bash
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --check-runtime --connect-qbit --probe-source --probe-target --json
```

`doctor` 可检查配置、cookie、PTCLI runtime 依赖、qBittorrent 连接、源站详情读取、MTEAM 查重 API、目标准备包、目标 torrent 文件和上传后新种 follow-up 条件。内嵌的 `flow_check` 会暴露 `source_capability`、`target_capabilities` 和 `credential_requirements`，便于自动化脚本定位缺哪个站点凭据。带 `--target-execute` 时会按 live pipeline 语义检查上传后下载、注入、等待条件。
`doctor --write-summary` 会写出 `mode` / `target_mode`，让脚本直接区分当前检查的是 `live_upload`、`resumed_uploaded_id`、`resumed_uploaded_torrent`、`prepared` 还是普通 `readiness_check`。

### 源站下载与 qBittorrent 等待

```bash
python3 ptcli.py source-download --tracker CHD --source-id 12345 --to MTEAM --output-dir ./tmp/source --accept-rules --json

python3 ptcli.py pipeline --from CHD --source-id 12345 --to MTEAM --source-torrent-file ./tmp/source/CHD-12345.torrent --inject-source --save-path "/downloads" --wait-complete --accept-rules --json
```

源种文件证据会包含 `exists`、`size_bytes`、`sha1`、`torrent_hash`/`infohash`。注入时会解析 `.torrent` 的真实 infohash，用于后续等待和审计。

### MTEAM 准备与上传

```bash
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path "/downloads/content" --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --upload-target --target-torrent-output-dir ./tmp/exported --target-execute --confirm-upload --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json
```

目标上传前会检查：

- MTEAM 查重结果干净。
- upload gate ready。
- rule review package 内的每个 source/target obligation 都有规则 URL、确认状态和非空人工审查范围。
- 描述材料、名称、简介、分类和标准字段可用。
- torrent announce/source/comment 符合 MTEAM-safe 元数据门禁，且没有残留 `announce-list` 或其他非预期顶层字段。
- 源站若暴露 infohash，下载到本地的源种必须能读出 infohash 并与源站元数据一致。
- 已显式传入 `--confirm-upload`。

源站种子和上传成功后的 MTEAM 新种都会在注入 qBittorrent 后做同一类可见性验证。`summary`、`evidence` 和 `closure_audit` 会记录 `uploaded_torrent_id`、`uploaded_torrent_hash`、`uploaded_torrent_path`、独立的 qBittorrent 可见性证据、`injection_verified` 和 `uploaded_wait`；如果 MTEAM API 没返回 hash，但已下载的新种文件能读出 infohash，也会作为 `uploaded_torrent_hash` 暴露，便于后续恢复和审计。只有 `visible_in_client`、`client_verification.visible` 或实际 `client_matches` 能证明注入种子已出现在 qBittorrent 列表中。
所有本地 `.torrent` 文件证据都会同时暴露 `hash`、`torrent_hash` 和 `infohash`（同一个 infohash 值），让脚本可以用统一的 `hash` 字段把下载的种子文件、qBittorrent 注入结果和等待结果串起来。
qBittorrent 等待完成证据还必须匹配请求的 torrent hash 和内容路径；即使缺少 `completion_verification`，也会用 `query` 与 `matches` 复核，避免把别的已完成任务当作本次转种闭环。
`qbit_wait_diagnostics` 会同时暴露请求的 hash、内容路径、保存路径、等待参数，以及 qBittorrent 实际观察到的 hash、内容路径、保存路径、状态和进度，便于自动化脚本在 mismatch 时选择正确的恢复参数。
`qbit_wait_retry_hints` 会在 wait query mismatch 时给出保守的 `retry_recommended`、`suggested_torrent_hash`、`suggested_content_path` 和 `suggested_save_path`，这些值来自 qBittorrent 实际观测结果，供盒子脚本生成下一次恢复参数。
`retorrent --execute` 的 `next_actions` 在遇到 qBittorrent wait mismatch 时也会带出这些 suggested 值，方便人或 agent 不展开完整 diagnostics 就能看到下一次重试应优先核对的 hash/path。
`summary-check` 的 `automation_reason` 也会在 `resolve_qbit_wait_mismatch` 时附带相同 suggested 值，便于只记录 reason 的盒子日志保留恢复线索。
`closure_status` 是面向调度器的机器可读摘要，会把 pipeline 状态、closure blockers、closure audit 缺口、qBittorrent wait mismatch，以及 source/target 两侧的 ready/hash/rule/wait 关键布尔值收敛到一个入口。
单独运行 `target-upload --write-summary` 时，`summary.mode` 也会使用与 pipeline 一致的目标侧模式：`live_upload`、`resumed_uploaded_id`、`resumed_uploaded_torrent`、`prepared` 或 `blocked`。

## 恢复路径

如果 live 上传成功但后续下载/注入/等待中断，可以不重复上传，直接从已知新种 ID 或已下载新种恢复：

```bash
python3 ptcli.py target-upload --package-dir ./tmp/target/U2-60635-to-MTEAM --uploaded-torrent-id 999 --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json

python3 ptcli.py target-upload --package-dir ./tmp/target/U2-60635-to-MTEAM --uploaded-torrent-file ./tmp/uploaded/MTEAM-999.torrent --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json

python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --package-dir ./tmp/target/U2-60635-to-MTEAM --upload-target --uploaded-torrent-id 999 --uploaded-output-dir ./tmp/uploaded --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --json

python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --package-dir ./tmp/target/U2-60635-to-MTEAM --upload-target --uploaded-torrent-file ./tmp/uploaded/MTEAM-999.torrent --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --json
```

`pipeline --package-dir ... --upload-target --uploaded-torrent-id/--uploaded-torrent-file` 会按恢复语义自动开启上传后新种的下载（仅 ID 场景）、注入和等待完成；若准备包里能推导内容路径，可以不额外传 `--uploaded-save-path`。
`target-upload --write-summary` 生成的上传后恢复命令会保留 `--config`、summary 输出目录、qBittorrent 分类/标签和等待参数，避免盒子续跑时退回默认配置。

`retorrent --dry-run` 计划模板、`retorrent --execute` 和 `pipeline --write-summary` 写出的 `resume_commands` 都优先使用这些命令续跑。pipeline summary 里的上传后新种恢复命令会回到 `pipeline --package-dir ... --upload-target --uploaded-torrent-id/--uploaded-torrent-file`，并携带源站、目标站、config、base-dir、QB 标签和等待参数，便于盒子脚本保持同一个高层闭环上下文。盒子脚本可用 `summary-check --json` 读取 `flow_diagnostics`、`credential_requirements`、`source_mode`、`target_mode` 和 `automation_reason`，用 `summary-check --print-next-command` 只取下一条安全命令，用 `summary-check --print-next-argv` 只取下一条安全命令的 argv JSON，用 `summary-check --print-shell` 输出 `PTCLI_AUTOMATION_ACTION`、`PTCLI_AUTOMATION_REASON`、`PTCLI_NEXT_COMMAND`、`PTCLI_AUTOMATION_EXIT_CODE`、`PTCLI_SOURCE_MODE`、`PTCLI_TARGET_MODE` 等 shell 变量，或用 `summary-check --run-next-command` 直接执行下一条受限的 `ptcli.py` 续跑命令。带 `<id>` 这类占位符的命令会返回 `automation_action=fill_command_placeholders`，不会被自动执行。
所有 `--write-summary` 写出的 summary 顶层都会带 `automation_handoff`，对应命令本次 JSON 返回也会同步给出该字段；它内含 `summary-check --json`、`--print-next-command`、`--print-next-argv`、`--print-shell` 和 `--run-next-command` 的 command/argv，盒子脚本可以直接读取 stdout 或落盘 summary 继续检查/续跑，无需自己拼 summary 路径。`summary-check --json` 也会按传入的 summary 路径补齐同样的 `automation_handoff`，即使输入 summary 是旧 schema、未知 kind 或文件缺失，也能返回可审计的下一步检查入口。
`summary-check` 还会暴露 `next_command_source`、`next_command_subcommand`、`next_command_run_allowed`、`next_command_run_blocker`、`candidate_commands`、`candidate_command_count` 和 `runnable_command_count`；每条候选命令都会标注来源、argv、子命令、占位符状态和是否在自动执行白名单内。`--print-shell` 对应导出 `PTCLI_BLOCKERS`、`PTCLI_MISSING_ARTIFACTS`、`PTCLI_MISSING_CLOSURE_AUDIT`、`PTCLI_FLOW_READY`、`PTCLI_FLOW_SOURCE_TRACKER`、`PTCLI_FLOW_SOURCE_ID`、`PTCLI_FLOW_TARGET_TRACKERS`、`PTCLI_CREDENTIAL_REQUIREMENTS`、`PTCLI_NEXT_COMMAND_SOURCE`、`PTCLI_NEXT_COMMAND_SUBCOMMAND`、`PTCLI_NEXT_COMMAND_RUN_ALLOWED`、`PTCLI_NEXT_COMMAND_RUN_BLOCKER`、`PTCLI_CANDIDATE_COMMAND_COUNT` 和 `PTCLI_RUNNABLE_COMMAND_COUNT`，也会导出 `PTCLI_CLOSURE_STATUS_*`、`PTCLI_CLOSURE_SOURCE_*`、`PTCLI_CLOSURE_TARGET_*` 这些闭环摘要变量，以及 `PTCLI_QBIT_WAIT_SOURCE_*` / `PTCLI_QBIT_WAIT_UPLOADED_*` 请求、观测和 retry hint 字段，盒子脚本可以区分命令来自 `resume_state` 还是 fallback，并在调用 `--run-next-command` 前先判断这条命令是否属于受限自动执行白名单。
`retorrent --dry-run` 生成的计划命令会继承显式传入的 `--config`、非默认 `--client` 和 `--base-dir`，让 AI/盒子脚本从计划阶段到实际续跑都保持同一个配置文件、cookie 根目录和 qBittorrent client。
`summary-check --run-next-command` 的受限执行白名单只包含 `pipeline`、`target-upload` 和 `doctor`，因此 `inspect` 等只读命令即使出现在推荐命令里也不会被自动执行。

`ptcli-run-summary.json` 顶层的 `flow_check` 和 `summary.flow` 会保留源站详情/下载适配器、目标站上传适配器和去重后的凭据要求，便于恢复脚本在重试前检查配置是否仍然满足当前 flow。
`requested_actions` / `effective_actions` 会同时写入顶层和 `summary`，其中 `requested_actions.source_torrent_file`、`requested_actions.uploaded_torrent_id` 与 `requested_actions.uploaded_torrent_file` 可区分“已有源种文件恢复”、“按新种 ID 下载恢复”和“已有本地新种文件恢复”。`evidence.target.mode` / `summary.target.mode` 会把目标侧闭环归类为 `live_upload`、`resumed_uploaded_id` 或 `resumed_uploaded_torrent`，让脚本不用反推本轮到底是新上传还是续跑恢复。

## 人工边界

当前仍需要人工处理或真实环境验证的部分：

- 在 live automation 前审阅源站下载/转载规则和目标站上传/做种规则。
- 提供真实盒子环境里的源站 cookie、MTEAM API key、qBittorrent 连接和内容路径。
- MTEAM 以外目标站尚未实现 live upload 闭环。
- Web UI、Discord、海外 tracker 和非转种路径仍处于迁移期兼容状态，尚未完成瘦身。
