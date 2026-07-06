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
- `ptcli serve` 会启动本地 JSON HTTP API，提供 `/health`、`/openapi.json`、`/v1/tools`、同步 `/v1/retorrent/check`/`/v1/retorrent`、AI 预检 `/v1/agent/run-preview`、`/v1/deployment/check`、`/v1/readiness/bundle`、每日候选 `/v1/candidates/daily`/`/v1/candidates/daily/schedule`，以及任务式 `/v1/jobs/retorrent/check`、`/v1/jobs/retorrent`、`/v1/jobs/retorrent/from-url`、`/v1/jobs/retorrent/submit`、`/v1/jobs/candidates/daily`、`/v1/jobs/candidates/{job_id}/submit`、`/v1/jobs`、`/v1/jobs/{job_id}`、`/v1/jobs/{job_id}/summary`、`/v1/jobs/{job_id}/resume`、`/v1/jobs/{job_id}/cancel`，方便 AI/自动化工具按 OpenAPI 或简单 JSON 调用。
- `sites --json` 暴露每个站点的 `source_info`、`source_info_adapter`、`source_download`、`source_download_adapter`、`credential_requirements`、`target_upload`、`full_live_closure_to_mteam` 能力。
- `rule-check --json` 暴露 `rule_obligations[].review_scope.required_confirmations`，供 agent 在 live 前逐项提示人工确认。
- `flow-check --json` 暴露 `source_capability`、`target_capabilities` 和去重后的 `credential_requirements`，供盒子脚本在 live 前检查配置缺口。
- `pipeline` 和 `retorrent --execute` 返回 `requested_actions`、`effective_actions`、`closure`、`evidence`、`artifacts`、`resume_commands`、`resume_state`、`next_actions`；`requested_actions` 会区分 `source_torrent_file`、`uploaded_torrent_id` 和 `uploaded_torrent_file` 等恢复输入，`evidence.target.mode` / `summary.target.mode` 会标明目标侧是 `live_upload`、`resumed_uploaded_id` 还是 `resumed_uploaded_torrent`。
- 任务式 API 的 job 状态和 summary 会暴露 `agent_decision`、`materials_handoff`、`target_upload_handoff`、`closure_handoff`、`manual_retorrent_handoff`、`resume_plan`、`resume_lineage`、`material_resolution` 和 `candidate_submission`，直接给出 `decision`、`recommended_action`、`stop_reason`、`duplicate_check`、`duplicate_clear`、`missing_confirmations`、`should_poll`、`should_resume`、`action`、续跑 endpoint、allowlist 判断、父任务来源、每日候选来源和 `next_command_argv`；`materials_handoff` 会把 IMDb/TMDb/豆瓣/PTGen、MediaInfo/BDInfo、截图、图床和目标站 preflight 缺口压缩到 `ready`、`blockers`、`recommended_inputs` 和 `next_actions`，`target_upload_handoff` 会把目标站上传前的 payload/preflight、查重、规则、确认和上传后做种证据压缩到 `action`、`ready_for_live_upload`、`uploaded_seeding_ready`、`blockers`、`next_actions` 和可执行的 `next_step` / `recommended_tool` / `recommended_endpoint`，`closure_handoff` 会进一步汇总源站下载、目标站新种注入做种、重复种、QB 证据、summary/evidence 和最终 `complete` 状态，并给出统一的 `next_step` / `recommended_tool` / `recommended_endpoint`，方便 agent 判断该停止、补确认、继续轮询、补素材续跑、修复 QB 证据还是收尾；`resume_job` 会把父任务 `materials_handoff` 与本次 allowlisted overrides 对齐成 `material_resolution`，暴露已覆盖和仍未覆盖的推荐素材输入；`/v1/jobs/{job_id}/resume` 支持白名单 overrides，可在续跑前补 `accept_rules`、`confirm_upload`、路径、qBittorrent 分类/标签/限速和素材文件，未知字段会进入 `ignored_overrides` 而不会拼进命令；同时暴露 `workflow_context`，把源站链接解析、目标站、候选来源、查重 gate、规则 gate、素材/描述缺口、qBittorrent 做种证据、缺失确认、续跑 argv 和 blockers 汇总到固定路径。
- `queued` / `running` job 会额外暴露 `runtime.should_poll`、`runtime.poll_after_seconds`、`runtime.status_endpoint`、`runtime.elapsed_seconds` 等字段，方便 OpenClaw/Hermes 按服务建议轮询，不需要自行计算时间戳。
- `ptcli serve` 默认 `PTCLI_MAX_CONCURRENT_JOBS=1`，同一时间只执行一个长耗时 job，其余保持 `queued`；`/v1/jobs` 会返回 `queue.max_concurrent_jobs`、`running_count` 和 `queued_count`，便于 agent 判断排队情况。
- `/v1/jobs/{job_id}/cancel` 只允许取消仍处于 `queued` 的任务；`running` 任务会返回 409，不会强行中断 live tracker 或 qBittorrent 操作。
- `ptcli serve` 启动时会把上次进程遗留的 `queued` / `running` job 标记为 `blocked` 并写入 `interruption`，避免容器重启后 agent 永久轮询；如存在 allowlisted `resume_state.next_command_argv`，`resume_plan` 会继续给出可审计续跑入口。
- 转种 job 状态和 summary 也会暴露 `policy_coverage` 和 `policy_handoff`；`policy_handoff` 会把源站/目标站规则页、人工审查 fingerprint、上传/下载限速、做种要求、缺失策略字段和下一步工具压缩成固定路径。当 `accept_rules` / `confirm_upload` 已齐但源站或目标站缺少 fingerprint、限速或做种要求时，`agent_decision.decision=configure_policy`，避免 agent 直接进入 live 上传。
- retorrent/manual job 会在未显式传入限速时，从 `PTCLI.SITE_POLICIES` 自动补齐 `qbit_download_limit`、`qbit_upload_limit`、`uploaded_qbit_upload_limit`、`uploaded_qbit_download_limit`；job 状态、summary 和 `agent_decision.policy_qbit_defaults` 会记录哪些值来自站点策略、哪些值由请求覆盖，`qbit_plan` 会汇总 source/uploaded 两侧最终的分类、标签、上传/下载限速和来源，`qbit_limit_audit` 会把计划限速与实际 qBittorrent 注入结果中的 `rate_limits.calls` 对齐，区分 `applied`、`pending` 和 `mismatch`；`qbit_handoff` 会把两侧的分类、标签、限速来源、审计状态和下一步动作压缩成 AI 可直接读取的 QB 执行摘要，resume job 的 `resume_context.inherited_policy` 也会保留父任务的策略上下文。
- `pipeline --write-summary` 会在 `ptcli-run-summary.json` 顶层写入 `flow_check`，并在 `summary.flow` 中保留源站/目标站适配器和凭据要求。
- 带执行动作的命令未闭环时返回 `status: blocked`、顶层 blockers 和非 0 退出码。
- `--write-summary` 会写出带 `automation_handoff` 的 summary JSON，并在本次命令的 JSON 返回中同步给出该字段；其中 `resume_state.next_stage` / `resume_state.next_command` 可供 agent 或脚本直接续跑，`automation_handoff` 则给出检查和执行续跑的 `summary-check` command/argv。
- `summary-check --json` 会暴露 `automation_handoff`、`readiness_summary`、`flow_diagnostics`、`credential_requirements`、`source_mode`、`target_mode`、`automation_reason`、`qbit_wait_retry_hints` 和逐条标注可执行性的 `candidate_commands`；`readiness_summary` 汇总源站、素材/描述、规则、目标上传、上传后做种和下一条命令状态，方便 AI/盒子脚本按关键闭环优先级续跑；`--print-next-command` 可只输出下一条安全续跑命令，`--print-next-argv` 可输出对应 argv JSON；`--print-shell` 可输出 `PTCLI_*` shell 变量（含 `PTCLI_READINESS_*` / `PTCLI_SOURCE_MODE` / `PTCLI_TARGET_MODE` / `PTCLI_AUTOMATION_REASON` / `PTCLI_FLOW_READY` / `PTCLI_CREDENTIAL_REQUIREMENTS` / `PTCLI_RUNNABLE_COMMAND_COUNT` / `PTCLI_CLOSURE_STATUS_*` / `PTCLI_QBIT_WAIT_SOURCE_*` / `PTCLI_QBIT_WAIT_UPLOADED_*`）；`--run-next-command` 可直接执行下一条受限的 `ptcli.py` 续跑命令。
- `summary-check --run-next-command` 只允许执行生成的 `pipeline`、`target-upload` 或 `doctor` 续跑命令；其他 ptcli 命令只会输出拒绝信息，避免自动化误跑只读/检查命令。
- `target-upload --write-summary` 的 `summary.mode` 会标明本轮目标侧是 live 上传、按新种 ID 恢复、本地新种文件恢复、仅准备完成或被阻断。
- `doctor --write-summary` 会写出 `mode` / `target_mode`，区分 live 上传检查、按新种 ID 恢复检查、本地新种文件恢复检查和普通 readiness check。
- `doctor --check-runtime`、`pipeline --target-execute`、`retorrent --execute` 和需要 qBittorrent 注入的 `target-upload` 会检查 focused ptcli 运行时依赖，legacy Web UI/Discord 依赖不是默认要求。

## 本地 AI 调用服务

```bash
# 本机启动，仅监听 localhost
python3 ptcli.py serve --host 127.0.0.1 --port 8080

# 查看 AI 可用工具、OpenAPI 和 OpenClaw/Hermes 友好 manifest
curl http://127.0.0.1:8080/v1/tools
curl http://127.0.0.1:8080/openapi.json
curl http://127.0.0.1:8080/.well-known/ptcli-agent.json
curl http://127.0.0.1:8080/v1/openclaw/skill.json
curl http://127.0.0.1:8080/v1/hermes/skill.json

# AI 工作流预演：不联系站点/qBittorrent，不创建 job，只输出工具顺序、请求模板和 closure_handoff 处理规则
curl -X POST http://127.0.0.1:8080/v1/agent/run-preview \
  -H 'Content-Type: application/json' \
  -d '{"source_url":"https://u2.dmhy.org/details.php?id=60635","target":"MTEAM","accept_rules":true,"confirm_upload":true,"save_path":"/downloads"}'

# 每日候选/刷上传预演：输出候选扫描、提交候选和 closure_handoff 跟踪链路
curl -X POST http://127.0.0.1:8080/v1/agent/run-preview \
  -H 'Content-Type: application/json' \
  -d '{"workflow":"daily_candidates","source_tracker":"U2","target":"MTEAM","accept_rules":true,"confirm_upload":true,"save_path":"/downloads"}'

# 只查源站信息和目标站是否已有种子，不上传
curl -X POST http://127.0.0.1:8080/v1/retorrent/check \
  -H "Content-Type: application/json" \
  -d '{"source":"https://u2.dmhy.org/details.php?id=60635","target":"MTEAM"}'

# 不存在重复种时尝试一键转种；规则确认和上传确认必须显式给出
curl -X POST http://127.0.0.1:8080/v1/retorrent \
  -H "Content-Type: application/json" \
  -d '{"source":"https://u2.dmhy.org/details.php?id=60635","target":"MTEAM","execute":true,"accept_rules":true,"confirm_upload":true,"save_path":"/downloads","uploaded_qbit_category":"MTEAM","uploaded_qbit_tags":"retorrent"}'

# 任务式 API：提交后返回 job_id，适合下载/截图/上传这类长任务
curl -X POST http://127.0.0.1:8080/v1/jobs/retorrent \
  -H "Content-Type: application/json" \
  -d '{"source":"https://u2.dmhy.org/details.php?id=60635","target":"MTEAM","execute":true,"accept_rules":true,"confirm_upload":true,"save_path":"/downloads","uploaded_qbit_category":"MTEAM","uploaded_qbit_tags":"retorrent","uploaded_qbit_upload_limit":"2MiB/s"}'

# AI 主路径：源站链接 + 目标站，服务查重后仅在规则和确认 gate 允许时继续转种
curl -X POST http://127.0.0.1:8080/v1/jobs/retorrent/from-url \
  -H "Content-Type: application/json" \
  -d '{"source_url":"https://u2.dmhy.org/details.php?id=60635","target":"MTEAM","accept_rules":true,"confirm_upload":true,"save_path":"/downloads","uploaded_qbit_category":"MTEAM","uploaded_qbit_tags":"retorrent","uploaded_qbit_upload_limit":"2MiB/s"}'

# AI 端到端推荐顺序：先读 readiness bundle，再按返回的 manual_job_template 提交任务
curl -X POST http://127.0.0.1:8080/v1/readiness/bundle \
  -H "Content-Type: application/json" \
  -d '{"source_url":"https://u2.dmhy.org/details.php?id=60635","target":"MTEAM","accept_rules":true,"confirm_upload":true,"save_path":"/downloads"}'

# 不启动 HTTP 服务时，也可以在盒子本地直接生成同一份 readiness bundle
python3 ptcli.py readiness-bundle --from U2 --source-id 60635 --target MTEAM --accept-rules --confirm-upload --downloads-path /downloads --json

# 轮询状态、读取 summary、按生成的 allowlisted next_command_argv 续跑
curl "http://127.0.0.1:8080/v1/jobs?status=blocked&limit=10"
curl http://127.0.0.1:8080/v1/jobs/<job_id>
curl http://127.0.0.1:8080/v1/jobs/<job_id>/summary
curl -X POST http://127.0.0.1:8080/v1/jobs/<job_id>/resume
curl -X POST http://127.0.0.1:8080/v1/jobs/<job_id>/cancel -d '{"reason":"submitted by mistake"}'

# 每日候选推荐：指定源站和目标站，返回最多 10 条已评分排序的可转种候选；
# 响应里的 digest.top_candidate / digest.push_items / digest.top_submit_request 适合 AI 或定时推送直接消费
curl -X POST http://127.0.0.1:8080/v1/candidates/daily \
  -H "Content-Type: application/json" \
  -d '{"source_tracker":"U2","target":"MTEAM","limit":10}'

# 长耗时/真实环境下也可用任务式候选接口；轮询 job 时可直接读取 candidate_digest 和 agent_decision
curl -X POST http://127.0.0.1:8080/v1/jobs/candidates/daily \
  -H "Content-Type: application/json" \
  -d '{"source_tracker":"U2","target":"MTEAM","limit":10}'

# 从候选 job 中选择第 1 条创建正式转种 job；source/target 从候选继承，只补确认、路径、QB 分类/限速等执行参数
curl -X POST http://127.0.0.1:8080/v1/jobs/candidates/<candidate_job_id>/submit \
  -H "Content-Type: application/json" \
  -d '{"rank":1,"confirm_upload":true,"save_path":"/downloads","uploaded_qbit_category":"MTEAM","uploaded_qbit_tags":"retorrent","uploaded_qbit_upload_limit":"2MiB/s"}'

# 每日候选计划预览：把请求或 PTCLI_DAILY_CANDIDATE_SCHEDULES 规范化成可由 cron/AI 执行的候选 job_request
curl -X POST http://127.0.0.1:8080/v1/candidates/daily/schedule \
  -H "Content-Type: application/json" \
  -d '{"schedules":[{"name":"u2-to-mteam","source_tracker":"U2","target":"MTEAM","limit":10,"time":"09:00","accept_rules":true}]}'

# 每日候选计划执行：只为每个 enabled schedule 创建候选扫描 job，返回 job_id；不会上传
curl -X POST http://127.0.0.1:8080/v1/jobs/candidates/daily/schedule \
  -H "Content-Type: application/json" \
  -d '{"schedules":[{"name":"u2-to-mteam","source_tracker":"U2","target":"MTEAM","limit":10,"time":"09:00","accept_rules":true}]}'

# 盒子/cron 本地触发：读取 PTCLI_DAILY_CANDIDATE_SCHEDULES 或传入 schedules 文件，inline 执行候选扫描并返回 schedule_digest；不会上传
python3 ptcli.py daily-schedule --write-summary --summary-output-dir ./tmp/daily-candidates --json
docker compose --profile cli run --rm ptcli daily-schedule --write-summary --summary-output-dir /Upload-Assistant/tmp/daily-candidates --json
python3 ptcli.py summary-check --summary-file ./tmp/daily-candidates/ptcli-daily-schedule-summary.json --json

# 站点策略矩阵：审计自动化 gate、QB 限速、做种要求、规则 URL 和人工确认状态；不会联系站点
curl -X POST http://127.0.0.1:8080/v1/site-policies \
  -H "Content-Type: application/json" \
  -d '{"trackers":"U2,MTEAM","accept_rules":true}'
python3 ptcli.py site-policies --from U2 --to MTEAM --accept-rules --json
```

若需要把 API 暴露给其他容器或局域网工具，建议设置 `PTCLI_API_TOKEN`，调用时添加 `Authorization: Bearer <token>`。服务端点不会绕过站点规则；live 下载/上传仍依赖现有 rule gate、dupe gate 和 `confirm_upload`。

手动源链接转种的 AI runbook 固定为：先调用 `readiness_bundle` 读取 `live_readiness.ready_for_manual_retorrent` 和 `manual_job_template.request`；再调用 `site_policies` 确认 `ready=true` 与 `execution_readiness.ready=true`；然后提交 `source_url_retorrent_job`；按 `runtime.status_endpoint` 轮询 `get_job_status`，当 `status=queued/running` 且 `runtime.should_poll=true` 时继续轮询；完成后读 `get_job_summary` 并优先读取 `closure_handoff`。若 `closure_handoff.action` 返回 `stop_duplicate`、`collect_confirmations`、`configure_policy`、`prepare_materials`、`repair_target_payload`、`repair_qbit` 或 `resolve_blockers`，AI 必须按 `closure_handoff.next_step` 和 blockers 停止、补资料或续跑；只有 `closure_handoff.complete=true` 才能把任务视为真实闭环完成。调用前应读取 `materials_handoff.recommended_inputs`、`target_upload_handoff.blockers`、`closure_handoff.blockers` 和 `resume_requirements`，按其中的 `missing_confirmations`、`suggested_overrides`、`recommended_inputs` 和 `allowed_overrides` 补充缺失确认、路径、限速或素材文件；调用后应读取 `material_resolution.covered_recommended_inputs` / `unresolved_recommended_inputs` 判断本次续跑是否覆盖了缺口；未知 override 会被忽略并记录在 `resume_context.ignored_overrides`。

每日候选响应会按“ready 优先、score 0-100 降序、源站列表顺序兜底”排序。每条候选包含 `ranking.score`、`ranking.tier`、`ranking.reasons`、`ranking.penalties` 和 `ranking.signals`，方便 AI 先选择无重复、元数据完整、规则风险低的候选；有阻塞项时仍会保留 `blockers` 和 `next_actions`，不会静默跳过规则或查重。候选还会给出 `policy_summary` 和 `policy_coverage`，汇总站点自动化 gate、QB 限速、做种要求、规则审查 fingerprint 以及缺失策略字段；同时提供 `decision_summary`，把 `action`、`risk_level`、`metadata_ready`、`duplicate_clear`、`policy_coverage_ready`、`primary_blocker` 和推荐/惩罚原因压缩成 AI 可直接判断的摘要。`digest.push_payload` 提供可直接推送的 `title`、`summary`、`message`、`top_item`、`items` 和批次级 `decision_summary`，`digest.push_items[]` 还会包含 `metadata`、`duplicate_status`、`duplicate_count`、`decision_summary`、`blockers`、`next_actions`、`can_submit`、`action_label`、`action_endpoint` 和可执行的 `submit_request`。候选 job 的 `agent_decision` 会在 coverage 不完整时返回 `configure_policy`，避免 agent 直接进入 live 提交。候选还会给出 `agent_workflow`、`submit_request`、`submit_tool=source_url_retorrent_job` 和 `submit_job_endpoint=/v1/jobs/retorrent/from-url`；AI 既可以自己提交 `submit_request`，也可以调用 `/v1/jobs/candidates/{job_id}/submit` 按 `rank` 或 `source_id` 选择候选并只补 `confirm_upload`、`save_path`、QB 分类/标签/限速和素材文件等执行参数，源站和目标站身份会从候选继承，避免误改；提交后的转种 job 会暴露 `candidate_submission_handoff`，把候选来源、继承身份、提交后的 `manual_retorrent_handoff` 和父/子 job endpoint 串起来。
`/v1/jobs/candidates/daily/schedule` 会额外返回顶层 `schedule_digest`、`notification_payload` 和 `agent_decision`，把多个 schedule job 的 `push_payload`、`push_items`、`top_submit_requests`、`submission_handoff`、状态端点和缺失确认聚合到一个批次结果里，方便 OpenClaw/Hermes 或外部 cron 直接生成“今日可转种候选”推送；`notification_payload` 是后续 webhook/主动推送渠道的稳定载荷，包含 `title`、`summary`、`message`、ready/pending/blocked 统计、`top_item`、`submit_items` 和下一步动作。`submission_handoff.items[]` 优先指向 `/v1/jobs/candidates/{candidate_job_id}/submit`，只要求补 `confirm_upload=true` 和 `save_path`/`path` 等执行参数，源站/目标站身份从候选 job 继承；`submission_handoff.next_step` 与 `notification_payload.next_step` 会把第一条可提交候选压缩成可直接调用的 `tool`、`endpoint`、`method` 和 `request`。

OpenClaw/Hermes 可直接读取 `/.well-known/ptcli-agent.json` 或 `/v1/openclaw/skill.json`、`/v1/hermes/skill.json`，其中包含 OpenAPI 地址、工具列表、鉴权方式、live 上传安全边界、`closure_handoff` 动作契约，以及每个关键工具的 `input_schema`、`response_contract`、`safety`。反向代理或容器内外地址不一致时，设置 `PTCLI_PUBLIC_BASE_URL=https://your-host.example` 让 manifest 输出外部可访问地址；仓库内也提供 `ai/openclaw/ptcli.skill.json` 和 `ai/hermes/ptcli.skill.json` 作为离线模板。

## 配置要求

- qBittorrent client 配置沿用 `data/config.py`。
- 源站 cookie 放在 `data/cookies/<TRACKER>.txt` 或对应适配器要求的位置。
- MTEAM 需要 `TRACKERS.MTEAM.api_key`。
- 站点自动化策略可写在 `config["PTCLI"]["SITE_POLICIES"]` 或顶层 `config["SITE_POLICIES"]`。默认只启用当前参考闭环的保守自动化能力；限速、做种要求和人工审查指纹建议按站点规则自行维护，例如：
  ```python
  config["PTCLI"] = {
      "SITE_POLICIES": {
          "U2": {
              "allow_auto_download": True,
              "allow_retorrent": True,
              "download_rate_limit": "20MiB/s",
              "upload_rate_limit": "500KiB/s",
              "min_seed_time_hours": 72,
              "required_promotions": ["free"],
              "forbidden_release_groups": ["BADGRP"],
              "rule_review_fingerprint": "manual-review-2026-07",
          },
          "MTEAM": {
              "allow_auto_upload": True,
              "allow_retorrent": True,
              "upload_rate_limit": "2MiB/s",
              "min_ratio": 1.0,
              "freeleech_required": True,
              "forbidden_title_patterns": ["禁转", "Do\\.Not\\.Repost"],
              "rule_review_fingerprint": "manual-review-2026-07",
          },
      }
  }
  ```
  CLI/API 显式传入的 `qbit_upload_limit`、`qbit_download_limit`、`uploaded_qbit_upload_limit`、`uploaded_qbit_download_limit` 会覆盖站点策略默认值。
  `required_promotions`、`freeleech_required`、`forbidden_title_patterns`、`forbidden_release_groups` 是本地自动化 gate，会在每日候选和策略审计中暴露/执行；无法程序化判断的站规仍必须通过 `rule_review_fingerprint` 和 `accept_rules` 人工确认。HTTP 服务也提供 `/v1/site-policies`，可直接读取 `policy_matrix[].automation`、`policy_matrix[].qbit_limits`、`policy_matrix[].seeding_requirements`、`policy_matrix[].transfer_rules`、`policy_matrix[].policy_coverage`、`policy_matrix[].execution_readiness` 和 `policy_matrix[].policy_profile` 供 AI 或部署脚本审计；其中 `policy_profile.template` 是可复制到 `config["PTCLI"]["SITE_POLICIES"][TRACKER]` 的站点配置模板，顶层 `config_templates.trackers` 会汇总本次涉及站点的模板。`policy_coverage` 会按源站/目标站角色列出缺失的 fingerprint、限速和做种要求，顶层 `policy_gap_summary` 会按 `source`/`target` 角色和 `rate_limits`、`seeding_requirements`、`rule_review` 分类聚合缺口，`execution_readiness` 则给出每个站点按角色是否可下载/上传/转种以及 blockers；`policy_handoff.next_step` 会在策略缺失时返回 `edit_config` 请求模板，在策略就绪时指回 `readiness_bundle` 继续 live 前检查。
- 任务式 API 默认把 job 文件写入 `PTCLI_JOB_DIR`，未设置时写入 `TMPDIR/ptcli-jobs`；Docker Compose 默认设置为 `/Upload-Assistant/tmp/ptcli-jobs`。
- `Dockerfile.ptcli` 是 focused CLI 镜像，只安装 `requirements-ptcli.txt` 和 ptcli 需要的系统依赖；旧 `Dockerfile` 保留给 legacy/full UA 入口。
- 默认发布构建使用 `Dockerfile.ptcli`，镜像入口是 `ptcli.py`；release 工作流会额外发布 `*-legacy-webui` 标签给旧 Web UI 镜像。
- 旧 `upload.py` 需要显式覆盖 entrypoint、使用 legacy Dockerfile，或拉取 `*-legacy-webui` 标签才会运行。
- `docker-compose.yml` 默认提供 `ptcli-api` 常驻 HTTP API 服务，使用项目内 `ptcli-net` 网络并带 `/health` healthcheck；一次性 CLI 服务放在 `cli` profile，可用 `docker compose --profile cli run --rm ptcli retorrent ...` 在盒子上执行；legacy Web UI 需要显式 `--profile legacy-webui`。
- `/v1/deployment/check` 会输出 `mounts`、`qbit`、`daily_candidates`、`docker_compose`、`agent_summary` 和 `agent_handoff`：AI 可以直接判断 config/cookies/tmp/job/downloads 挂载是否就绪、qBittorrent 是否配置、`PTCLI_DAILY_CANDIDATE_SCHEDULES` 是否已提供每日候选计划，以及 `docker-compose.yml` 是否包含可用的 `ptcli-daily-schedule` daily profile 服务；`agent_handoff` 会给出手动转种和每日候选的推荐工具、端点、最小请求模板、必需确认和阻塞原因。`/v1/readiness/bundle` 会进一步把 deployment、site policies、daily schedule、非 live `live_verification` 凭据/图床/素材链路清单、doctor 命令模板和 `source_url_retorrent_job` 请求模板汇总到 `live_readiness`/`agent_decision`，用于 AI 在 live 前一次性判断是否还缺 cookie、MTEAM API key、qBittorrent 配置、图床、规则确认、目标站点、源站链接或盒子配置；`live_test_handoff.next_step` 会在就绪时给出可执行 `ptcli doctor` argv，未就绪时指向 deployment/site policy/readiness 修复路径。doctor 写出 `ptcli-doctor-summary.json` 后，`summary-check --json` 会返回 `doctor_result_handoff`，把 `live_safe_to_attempt`、blockers、summary-check 命令和通过后的 `source_url_retorrent_job` 入口压缩到固定路径。每日候选或 compose 定时服务未配置只作为 warning，不阻塞手动转种 API。
- 如果 qBittorrent 跑在宿主机上，容器内的 `data/config.py` 可把 `qbit_url` 写成 `http://host.docker.internal` 并保持对应 `qbit_port`；如果 qBittorrent 也是 Docker 容器，把两边放到同一个 Docker 网络后使用 qBittorrent 的服务名作为 host。
- live 验证需要在真实盒子环境中提供有效 cookie、MTEAM API key、qBittorrent 连接和实际内容路径。

## Docker/Seedbox

```bash
# 首次部署建议先准备 .env
cp .env.ptcli.example .env

# 构建并启动本地 API 服务
docker compose build ptcli-api
docker compose up -d ptcli-api

# 检查 API 服务
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/v1/deployment/check
curl http://127.0.0.1:8080/v1/tools
docker compose ps ptcli-api

# 检查 focused CLI 能力矩阵
docker compose --profile cli run --rm ptcli sites --json

# 本地 dry-run 预检：不联系站点/qBittorrent，输出 AI handoff 和下一步工具
docker compose --profile cli run --rm ptcli readiness-bundle --from U2 --source-id 60635 --target MTEAM --accept-rules --confirm-upload --downloads-path /downloads --json

# 执行一次每日候选扫描；适合放进宿主机 cron，结果写到挂载的 tmp/daily-candidates
docker compose --profile daily run --rm ptcli-daily-schedule

# 盒子上一键闭环示例
docker compose --profile cli run --rm ptcli retorrent --from U2 --source-id 60635 --to MTEAM --execute --accept-rules --confirm-upload --save-path "/downloads" --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json

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
