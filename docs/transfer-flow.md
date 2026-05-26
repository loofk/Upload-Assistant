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

`retorrent --execute` 会编排完整 pipeline：

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

源站种子和上传成功后的 MTEAM 新种都会在注入 qBittorrent 后做同一类可见性验证。`summary`、`evidence` 和 `closure_audit` 会记录 `uploaded_torrent_id`、`uploaded_torrent_hash`、`uploaded_torrent_path`、独立的 qBittorrent 可见性证据、`injection_verified` 和 `uploaded_wait`；只有 `visible_in_client`、`client_verification.visible` 或实际 `client_matches` 能证明注入种子已出现在 qBittorrent 列表中。
qBittorrent 等待完成证据还必须匹配请求的 torrent hash 和内容路径；即使缺少 `completion_verification`，也会用 `query` 与 `matches` 复核，避免把别的已完成任务当作本次转种闭环。

## 恢复路径

如果 live 上传成功但后续下载/注入/等待中断，可以不重复上传，直接从已知新种 ID 或已下载新种恢复：

```bash
python3 ptcli.py target-upload --package-dir ./tmp/target/U2-60635-to-MTEAM --uploaded-torrent-id 999 --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json

python3 ptcli.py target-upload --package-dir ./tmp/target/U2-60635-to-MTEAM --uploaded-torrent-file ./tmp/uploaded/MTEAM-999.torrent --inject-uploaded-torrent --uploaded-save-path "/downloads/content" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json
```

`retorrent --execute` 和 `pipeline --write-summary` 会在 `ptcli-run-summary.json` 中写入 `resume_commands`，优先使用这些命令续跑。盒子脚本可用 `summary-check --json` 读取 `flow_diagnostics` 和 `credential_requirements`，用 `summary-check --print-next-command` 只取下一条安全命令，用 `summary-check --print-shell` 输出 `PTCLI_AUTOMATION_ACTION`、`PTCLI_NEXT_COMMAND`、`PTCLI_AUTOMATION_EXIT_CODE` 等 shell 变量，或用 `summary-check --run-next-command` 直接执行下一条受限的 `ptcli.py` 续跑命令。带 `<id>` 这类占位符的命令会返回 `automation_action=fill_command_placeholders`，不会被自动执行。

`ptcli-run-summary.json` 顶层的 `flow_check` 和 `summary.flow` 会保留源站详情/下载适配器、目标站上传适配器和去重后的凭据要求，便于恢复脚本在重试前检查配置是否仍然满足当前 flow。

## 人工边界

当前仍需要人工处理或真实环境验证的部分：

- 在 live automation 前审阅源站下载/转载规则和目标站上传/做种规则。
- 提供真实盒子环境里的源站 cookie、MTEAM API key、qBittorrent 连接和内容路径。
- MTEAM 以外目标站尚未实现 live upload 闭环。
- Web UI、Discord、海外 tracker 和非转种路径仍处于迁移期兼容状态，尚未完成瘦身。
