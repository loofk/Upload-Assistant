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
- `ptcli serve` 会启动本地 JSON HTTP API，提供 `/health`、`/openapi.json`、`/v1/tools`、同步 `/v1/retorrent/check`/`/v1/retorrent`、AI 预检 `/v1/agent/run-preview`、创建任务前同步 `/v1/retorrent/source-url/preflight`、`/v1/deployment/check`、`/v1/readiness/bundle`、站点能力/规则 profile `/v1/sites`、qBittorrent 检查/匹配/导出/注入/等待 `/v1/qbit/inspect`/`/v1/qbit/match`/`/v1/qbit/export`/`/v1/qbit/inject`/`/v1/qbit/wait`、每日候选 `/v1/candidates/daily`/`/v1/candidates/daily/schedule`，以及任务式 `/v1/jobs/retorrent/check`、`/v1/jobs/retorrent`、`/v1/jobs/retorrent/from-url`、`/v1/jobs/retorrent/from-url/check-and-submit`、`/v1/jobs/retorrent/submit`、`/v1/jobs/candidates/daily`、`/v1/jobs/candidates/{job_id}/submit`、`/v1/jobs`、`/v1/jobs/{job_id}`、`/v1/jobs/{job_id}/summary`、`/v1/jobs/{job_id}/resume`、`/v1/jobs/{job_id}/cancel`，方便 AI/自动化工具按 OpenAPI 或简单 JSON 调用。
- 手动转种 job 的请求 schema 已暴露素材准备参数，可在创建任务时直接传 `metadata_file`、`ptgen_description_file`、`mediainfo_file`、`bdinfo_file`、`screenshot_files`、`image_host_file`，或传 `enrich_metadata`、`fetch_ptgen`、`generate_mediainfo`、`generate_bdinfo`、`generate_screenshots`、`upload_screenshots` 让服务在目标站准备包前生成缺失素材；这些输入会进入 job `request.material_options` 和 `command_argv`，便于 AI 后续审计和续跑。
- `sites --json` 和 `/v1/sites` 暴露每个站点的 `source_info`、`source_info_adapter`、`source_download`、`source_download_adapter`、`credential_requirements`、`target_upload`、`full_live_closure_to_mteam` 能力，并提供 `extension_plan` / `extension_checklist` / `extension_handoff`，把新增中文 PT 站点时缺少的 source adapter、download adapter、target upload adapter、policy profile、参考流、验证端点和下一步实现动作结构化给 AI。
- `rule-check --json` 暴露 `rule_obligations[].review_scope.required_confirmations`，供 agent 在 live 前逐项提示人工确认。
- `flow-check --json` 暴露 `source_capability`、`target_capabilities` 和去重后的 `credential_requirements`，供盒子脚本在 live 前检查配置缺口。
- `pipeline` 和 `retorrent --execute` 返回 `requested_actions`、`effective_actions`、`closure`、`evidence`、`artifacts`、`resume_commands`、`resume_state`、`next_actions`；`requested_actions` 会区分 `source_torrent_file`、`uploaded_torrent_id` 和 `uploaded_torrent_file` 等恢复输入，`evidence.target.mode` / `summary.target.mode` 会标明目标侧是 `live_upload`、`resumed_uploaded_id` 还是 `resumed_uploaded_torrent`。
- 任务式 API 的 job 状态和 summary 会暴露 `job_handoff`、`job_lineage`、`agent_decision`、`materials_handoff`、`target_upload_handoff`、`closure_handoff`、`manual_retorrent_handoff`、`candidate_batch_handoff`、`resume_plan`、`resume_execution_handoff`、`resume_lineage`、`material_resolution` 和 `candidate_submission`；`job_handoff` 是推荐给 AI 最先读取的控制面短路径，会把 `wait`、`resume`、`submit_if_clear`、`prepare_materials`、`done`、`stop` 等动作，以及 `recommended_tool`、`recommended_endpoint`、`recommended_request`、`dry_run_request`、`execute_request`、`poll_after_seconds`、`can_resume`、`resume_recommended`、`can_attempt_live`、`candidate_submission_execution`、`material_input_template`、`blockers` 和 `next_actions` 压到固定路径；候选提交后的 `prepare_materials` 会让 `recommended_request` 直接指向素材模板的 dry-run resume 请求，并同时给出对应 `execute_request`。`job_lineage` 会把父任务、root 任务、子任务、最新子任务、active child 和链路深度暴露出来，方便一次转种经历多次 resume 后仍能追踪完整父子任务链。其他 handoff 会继续给出 `decision`、`recommended_action`、`stop_reason`、`duplicate_check`、`duplicate_clear`、`missing_confirmations`、`should_poll`、`should_resume`、`action`、续跑 endpoint、allowlist 判断、父任务来源、每日候选来源和 `next_command_argv`；`candidate_batch_handoff` 会把单个每日候选 job 中可提交的 `push_items` 压成 `submit_daily_candidate_job` 的 `recommended_endpoint`、`recommended_request`、`required_overrides` 和 `items[]`，让 AI 在用户确认后从候选批次安全创建正式转种 job，而源站/目标站身份仍继承自候选 job；`materials_handoff` 会把 IMDb/TMDb/豆瓣/PTGen、MediaInfo/BDInfo、截图、图床和目标站 preflight 缺口压缩到 `ready`、`blockers`、`recommended_inputs`、`material_plan`、`resume_request_template`、`resume_handoff` 和 `next_actions`，其中 `recommended_inputs[].accepted_keys` 与 `material_plan.items[].resume_overrides` 会明确列出可恢复输入（如 `imdb_id`、`tmdb_id`、`douban_id`、`metadata_file`、`fetch_ptgen`、`generate_mediainfo`、`generate_screenshots`、`upload_screenshots`、`image_host_file`），`resume_handoff` 会给出聚合的 `dry_run_request`/`execute_request` 以及逐项 `staged_requests`，便于 AI 先预览再补齐全部素材缺口；`resume_execution_handoff` 会把通用续跑的 `dry_run_request`、`execute_request`、allowlisted overrides、确认/素材 gate 和 stop_when 集中到固定路径，要求先预览再执行；`target_upload_handoff` 会把目标站上传前的 payload/preflight、查重、规则、确认和上传后做种证据压缩到 `action`、`ready_for_live_upload`、`uploaded_seeding_ready`、`blockers`、`next_actions` 和可执行的 `next_step` / `recommended_tool` / `recommended_endpoint`；`manual_retorrent_handoff.live_checklist` 会把源站识别、目标查重、显式确认、站点策略、素材、目标 payload、上传后做种和 QB 限速审计压成固定清单，`closure_handoff.closure_checklist` 会进一步汇总源站下载、目标站新种注入做种、重复种、QB 证据、summary/evidence 和最终 `complete` 状态，并给出统一的 `next_step` / `recommended_tool` / `recommended_endpoint`，方便 agent 判断该停止、补确认、继续轮询、补素材续跑、修复 QB 证据还是收尾；`resume_job` 会把父任务 `materials_handoff` 与本次 allowlisted overrides 对齐成 `material_resolution`，暴露已覆盖和仍未覆盖的推荐素材输入；`/v1/jobs/{job_id}/resume` 支持白名单 overrides，可在续跑前补 `accept_rules`、`confirm_upload`、路径、qBittorrent 分类/标签/限速和素材文件，未知字段会进入 `ignored_overrides` 而不会拼进命令；同时暴露 `workflow_context`，把源站链接解析、目标站、候选来源、查重 gate、规则 gate、素材/描述缺口、qBittorrent 做种证据、缺失确认、续跑 argv 和 blockers 汇总到固定路径。
- `queued` / `running` job 会额外暴露 `runtime.should_poll`、`runtime.poll_after_seconds`、`runtime.status_endpoint`、`runtime.elapsed_seconds` 等字段，方便 OpenClaw/Hermes 按服务建议轮询，不需要自行计算时间戳。
- `ptcli serve` 默认 `PTCLI_MAX_CONCURRENT_JOBS=1`，同一时间只执行一个长耗时 job，其余保持 `queued`；`/v1/jobs` 会返回 `queue.max_concurrent_jobs`、`running_count` 和 `queued_count`，便于 agent 判断排队情况。
- `/v1/jobs/{job_id}/cancel` 只允许取消仍处于 `queued` 的任务；`running` 任务会返回 409，不会强行中断 live tracker 或 qBittorrent 操作。
- `ptcli serve` 启动时会把上次进程遗留的 `queued` / `running` job 标记为 `blocked` 并写入 `interruption`，避免容器重启后 agent 永久轮询；如存在 allowlisted `resume_state.next_command_argv`，`resume_plan` 会继续给出可审计续跑入口。
- 转种 job 状态和 summary 也会暴露 `policy_coverage` 和 `policy_handoff`；`policy_handoff` 会把源站/目标站规则页、人工审查 fingerprint、上传/下载限速、做种要求、缺失策略字段和下一步工具压缩成固定路径。`site_policies.policy_execution_handoff` 会进一步把 QB 限速、做种要求、转种过滤规则、规则 obligation、配置模板和继续/停止条件压成 AI 可直接读取的执行 handoff。当 `accept_rules` / `confirm_upload` 已齐但源站或目标站缺少 fingerprint、限速或做种要求时，`agent_decision.decision=configure_policy`，避免 agent 直接进入 live 上传。
- retorrent/manual job 会在未显式传入限速时，从 `PTCLI.SITE_POLICIES` 自动补齐 `qbit_download_limit`、`qbit_upload_limit`、`uploaded_qbit_upload_limit`、`uploaded_qbit_download_limit`；job 状态、summary 和 `agent_decision.policy_qbit_defaults` 会记录哪些值来自站点策略、哪些值由请求覆盖，`qbit_plan` 会汇总 source/uploaded 两侧最终的分类、标签、上传/下载限速和来源，`qbit_limit_audit` 会把计划限速与实际 qBittorrent 注入结果中的 `rate_limits.calls` 对齐，区分 `applied`、`pending` 和 `mismatch`；`qbit_handoff` 会把两侧的分类、标签、限速来源、审计状态和下一步动作压缩成 AI 可直接读取的 QB 执行摘要，其中 `enforcement_handoff.roles[]` 会逐项列出 source/uploaded 的预期限速、观察到的限速、是否缺注入证据、是否需要修复限速，以及下一步 resume/get-summary 建议；`qbit_enforcement_summary` 会把同一审计再压缩成 `expected_role_count`、`applied_role_count`、`pending_roles`、`mismatch_roles`、`recommended_tool` 和 blockers，作为 OpenClaw/Hermes 判断“站点策略限速是否已经真正落到 QB 任务”的首选短路径；resume job 的 `resume_context.inherited_policy` 也会保留父任务的策略上下文。
- `pipeline --write-summary` 会在 `ptcli-run-summary.json` 顶层写入 `flow_check`，并在 `summary.flow` 中保留源站/目标站适配器和凭据要求。
- 带执行动作的命令未闭环时返回 `status: blocked`、顶层 blockers 和非 0 退出码。
- `--write-summary` 会写出带 `automation_handoff` 的 summary JSON，并在本次命令的 JSON 返回中同步给出该字段；其中 `resume_state.next_stage` / `resume_state.next_command` 可供 agent 或脚本直接续跑，`automation_handoff` 则给出检查和执行续跑的 `summary-check` command/argv。
- `summary-check --json` 会暴露 `automation_handoff`、`readiness_summary`、`flow_diagnostics`、`credential_requirements`、`source_mode`、`target_mode`、`automation_reason`、`qbit_wait_retry_hints`、每日候选 `daily_candidate_targets` 和逐条标注可执行性的 `candidate_commands`；`readiness_summary` 汇总源站、素材/描述、规则、目标上传、上传后做种、每日候选目标/短缺和下一条命令状态，方便 AI/盒子脚本按关键闭环优先级续跑；`--print-next-command` 可只输出下一条安全续跑命令，`--print-next-argv` 可输出对应 argv JSON；`--print-shell` 可输出 `PTCLI_*` shell 变量（含 `PTCLI_READINESS_*` / `PTCLI_DAILY_CANDIDATE_*` / `PTCLI_SOURCE_MODE` / `PTCLI_TARGET_MODE` / `PTCLI_AUTOMATION_REASON` / `PTCLI_FLOW_READY` / `PTCLI_CREDENTIAL_REQUIREMENTS` / `PTCLI_RUNNABLE_COMMAND_COUNT` / `PTCLI_CLOSURE_STATUS_*` / `PTCLI_QBIT_WAIT_SOURCE_*` / `PTCLI_QBIT_WAIT_UPLOADED_*`）；`--run-next-command` 可直接执行下一条受限的 `ptcli.py` 续跑命令。
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

# AI 端到端推荐顺序：先 preflight，按 duplicate_check_handoff 查重，未重复再按 submit_if_clear_handoff 提交任务
curl -X POST http://127.0.0.1:8080/v1/retorrent/source-url/preflight \
  -H "Content-Type: application/json" \
  -d '{"source_url":"https://u2.dmhy.org/details.php?id=60635","target":"MTEAM","accept_rules":true,"confirm_upload":true,"save_path":"/downloads"}'

# 更适合 AI 审计的异步路径：先创建查重 job，确认 submit_if_clear_handoff.ready=true 后由 check job id 派生 live job
curl -X POST http://127.0.0.1:8080/v1/jobs/retorrent/check \
  -H "Content-Type: application/json" \
  -d '{"source_url":"https://u2.dmhy.org/details.php?id=60635","target":"MTEAM","accept_rules":true,"confirm_upload":true,"save_path":"/downloads"}'
curl -X POST http://127.0.0.1:8080/v1/jobs/retorrent/check/<job_id>/submit \
  -H "Content-Type: application/json" \
  -d '{"uploaded_qbit_category":"MTEAM","uploaded_qbit_tags":"retorrent","uploaded_qbit_upload_limit":"2MiB/s"}'

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
python3 ptcli.py daily-schedule --write-summary --summary-output-dir ./tmp/daily-candidates --write-notification --json
docker compose --profile cli run --rm ptcli daily-schedule --write-summary --summary-output-dir /Upload-Assistant/tmp/daily-candidates --write-notification --json
python3 ptcli.py summary-check --summary-file ./tmp/daily-candidates/ptcli-daily-schedule-summary.json --json

# Docker Compose 常驻每日触发器：按每个 schedule 的 time/timezone 每天执行候选扫描并写 summary；不会上传
python3 ptcli.py daily-scheduler --once --summary-output-dir ./tmp/daily-candidates --write-notification --json
docker compose --profile daily up -d ptcli-daily-scheduler

# 可选 webhook 推送：不配置时只写本地 notification JSON/TXT 文件；配置后会 POST 同一份 JSON 载荷
PTCLI_DAILY_CANDIDATE_WEBHOOK_URL=https://hooks.example/ptcli docker compose --profile daily up -d ptcli-daily-scheduler

# 站点策略矩阵：审计自动化 gate、QB 限速、做种要求、规则 URL、rule_obligations 和人工确认状态；不会联系站点
curl -X POST http://127.0.0.1:8080/v1/site-policies \
  -H "Content-Type: application/json" \
  -d '{"trackers":"U2,MTEAM","accept_rules":true}'
python3 ptcli.py site-policies --from U2 --to MTEAM --accept-rules --json
```

若需要把 API 暴露给其他容器或局域网工具，建议设置 `PTCLI_API_TOKEN`，调用时添加 `Authorization: Bearer <token>`。服务端点不会绕过站点规则；live 下载/上传仍依赖现有 rule gate、dupe gate 和 `confirm_upload`。

手动源链接转种的 AI runbook 固定为：先调用 `source_url_retorrent_preflight` 读取 `ready_to_create_job`、`source_reference`、`target_trackers`、`policy_execution_summary`、`policy_execution_handoff`、`duplicate_check.next_request`、`duplicate_check_handoff`、`job_creation_handoff.request`、`next_step` 和 `blockers`；该端点不创建 job、不访问 tracker、不访问 qBittorrent，只用于判断源链接解析、部署、规则、确认和素材前置条件是否足以进入任务创建。若 `ready_to_create_job=false`，AI 必须按 `next_step` 修复部署、策略、确认或配置缺口；若 ready，再调用 `readiness_bundle` 复核 `live_readiness.ready_for_manual_retorrent`、`live_readiness.policy_execution_handoff`、`live_test_handoff.policy_execution_handoff` 和 `manual_job_template.request`；再调用 `site_policies` 确认 `ready=true`、`policy_execution_handoff.ready=true`、`execution_readiness.ready=true` 与 `rule_obligations.*.ready=true`，其中源站 scope 应为 `download_and_retorrent`、目标站 scope 应为 `upload_and_seed`。推荐的一步式入口是 `source_url_check_and_submit` / `/v1/jobs/retorrent/from-url/check-and-submit`：它会先同步执行目标站查重，若 `duplicate_check.exists=true` 则返回 `status=blocked`、重复种信息并且不创建 live job；只有 `duplicate_check.searched=true`、`duplicate_check.exists=false`、`submit_if_clear_handoff.ready=true`、`accept_rules=true`、`confirm_upload=true` 时才创建后续 live job 并返回 `job_id`、`status_endpoint` 和 `summary_endpoint`。需要拆分审计时，也可以调用 `retorrent_check` / `/v1/retorrent/check` 或异步 `retorrent_check_job` / `/v1/jobs/retorrent/check` 执行目标站查重，只有 `duplicate_check.searched=true`、`duplicate_check.exists=false` 且 `submit_if_clear_handoff.ready=true` 时，才把同步结果里的 `submit_if_clear_handoff.request` 提交给 `source_url_retorrent_job`，或把已完成的查重 `job_id` 提交给 `submit_checked_retorrent_job` / `/v1/jobs/retorrent/check/{job_id}/submit`；若 `duplicate_check.exists=true`，必须停止并返回重复种信息。由查重派生的 live job 会在 `check_submission` 中记录查重结果、继承 request、执行覆盖参数和 qBittorrent 限速/分类证据，方便审计 AI 没有绕过查重 gate。任务创建后优先读取 `job_handoff`：当 `job_handoff.action=wait` 且 `job_handoff.should_poll=true` 时按 `job_handoff.poll_after_seconds` 轮询 `get_job_status`；当 `job_handoff.action=resume` 时先用 `job_handoff.recommended_request` 预览 `resume_job`；当 `job_handoff.action=stop` 时停止并报告 blockers；当 `job_handoff.action=done` 时读取 `get_job_summary` 并回报闭环证据。完成后仍需读 `closure_summary` / `closure_handoff` 作为最终闭环证明；若 `closure_summary.action` 返回 `stop_duplicate`、`collect_confirmations`、`configure_policy`、`prepare_materials`、`repair_target_payload`、`repair_qbit` 或 `resolve_blockers`，AI 必须按 `job_handoff` 或 `closure_summary.next_step` 和 blockers 停止、补资料或续跑；只有 `job_handoff.action=done`、`closure_summary.complete=true` 且 `closure_summary.blockers=[]` 才能把任务视为真实闭环完成。调用前应读取 `resume_summary`、`materials_handoff.recommended_inputs`、`target_upload_handoff.blockers`、`closure_summary.blockers` 和 `resume_requirements`，按其中的 `missing_confirmations`、`suggested_overrides`、`recommended_inputs`、`dry_run_request`、`execute_request` 和 `allowed_overrides` 补充缺失确认、路径、限速或素材文件；真正续跑前建议优先调用 `job_handoff.recommended_request`、`resume_summary.next_step` 或 `resume_job` 并设置 `dry_run=true` 预览 patched `command_argv`，确认后再用同一组 allowlisted override 去掉 `dry_run` 执行；调用后应读取 `resume_summary`、`resume_audit`、`resume_context.applied_overrides` 和 `material_resolution.covered_recommended_inputs` / `unresolved_recommended_inputs` 判断父子任务关系、override 是否生效以及本次续跑是否覆盖了缺口；未知 override 会被忽略并记录在 `resume_context.ignored_overrides`。

每日候选响应会按“ready 优先、score 0-100 降序、源站列表顺序兜底”排序，并固定围绕 `target_count`（默认 10）报告 `scan_count`、`selected_count`、`ready_count`、`shortfall_count`、`target_met` 和 `target_summary`，方便 AI 明确判断“今天是否真的凑够 10 条候选/可提交候选”，而不是把少量结果误判为达标。每条候选包含 `ranking.score`、`ranking.tier`、`ranking.reasons`、`ranking.penalties` 和 `ranking.signals`，方便 AI 先选择无重复、元数据完整、规则风险低的候选；有阻塞项时仍会保留 `blockers` 和 `next_actions`，不会静默跳过规则或查重。候选还会给出 `policy_summary`、`policy_coverage`、`policy_execution_handoff` 和 `policy_risk_summary`，汇总站点自动化 gate、QB 限速、做种要求、转种过滤规则、规则审查 fingerprint、`rule_obligations_ready` 以及缺失策略字段；`policy_risk_summary` 会把限速 ready、做种要求 ready、规则确认 ready、严格转种过滤数量、policy blockers 和 `execution_priority` 压到固定路径，作为 AI 判断刷上传候选是否“低风险可优先/严格规则需复核/应停止”的首选字段。`policy_summary.rules.fingerprint_status` 会标出源站/目标站 fingerprint 是否缺失或仍是模板占位符，`policy_execution_handoff.ready` 也必须同时满足 rule obligations、限速和做种要求，此时候选会保持 blocked 并给出 `decision_summary.action=configure_policy`，不会只因 `accept_rules=true` 进入可提交状态；同时提供 `decision_summary`，把 `action`、`risk_level`、`policy_risk_level`、`metadata_ready`、`duplicate_clear`、`policy_coverage_ready`、`primary_blocker` 和推荐/惩罚原因压缩成 AI 可直接判断的摘要。`digest.approval_queue` 和 `digest.top_safe_candidates` 是 AI 审批每日候选的首选入口，只收录 `can_submit=true`、目标查重 clear、`policy_risk_level=low` 且候选风险 low 的条目；中风险候选会进入 guarded 统计，重复、规则缺口或高风险候选会进入 blocked 统计，`continue_when`/`stop_when` 和 `requires_confirmation` 会再次提醒必须由用户确认 `accept_rules=true`、`confirm_upload=true` 和 `save_path`/`path` 后才能提交。`digest.push_payload` 提供可直接推送的 `title`、`summary`、`message`、`target_count`、`shortfall_count`、`target_met`、`approval_queue`、`top_safe_candidates`、`top_item`、`items` 和批次级 `decision_summary`，其中 `decision_summary.policy_risk_counts` 会统计低/中/高策略风险候选数量，`safe_to_submit_count` 会统计可进入 approval queue 的候选数量；`digest.push_items[]` 还会包含 `metadata`、`duplicate_status`、`duplicate_count`、`decision_summary`、`audit_summary`、`policy_risk_summary`、`policy_execution_handoff`、`blockers`、`next_actions`、`can_submit`、`action_label`、`action_endpoint` 和可执行的 `submit_request`；其中 `audit_summary` 会把源站、IMDb/TMDb/豆瓣元数据、目标查重、站点规则/QB 限速、阻塞原因和提交入口汇总成单条候选的首选 AI 审计字段。候选 job 的 `agent_decision` 会在 coverage 不完整时返回 `configure_policy`，避免 agent 直接进入 live 提交。候选还会给出 `agent_workflow`、`submit_request`、`submit_tool=source_url_retorrent_job` 和 `submit_job_endpoint=/v1/jobs/retorrent/from-url`；AI 既可以自己提交 `submit_request`，也可以调用 `/v1/jobs/candidates/{job_id}/submit` 按 `rank` 或 `source_id` 选择候选并只补 `confirm_upload`、`save_path`、QB 分类/标签/限速和素材文件等执行参数，源站和目标站身份会从候选继承，避免误改；提交后的转种 job 会暴露 `candidate_submission_summary` 和 `candidate_submission_handoff`，其中 summary 先给出候选来源、覆盖参数 key、`policy_execution_handoff`、`policy_execution_ready`、`execution_state`、`execution_handoff`、`manual_action`、`closure_action`、`recommended_tool`、`next_step`、`blockers` 和父/子 job endpoint；handoff 再展开继承身份、`submitted_overrides`、`material_options`、`qbit_overrides`、`policy_execution_handoff`、`execution_state`、`execution_handoff` 与完整 `manual_retorrent_handoff`。`execution_state` 会稳定区分 `wait`、`stop_duplicate`、`collect_confirmations`、`configure_policy`、`prepare_materials`、`repair_target_payload`、`repair_qbit`、`resume`、`ready_for_live_upload` 和 `complete`，`execution_handoff` 则给出推荐工具、端点、请求、continue/stop 条件和 blockers；当材料不完整时还会提供 `material_input_template`，把 `metadata_file`、`ptgen_description_file`、`screenshot_files`、`image_host_file` 等推荐输入、dry-run/execute 请求和示例值压到固定路径；同一份 `candidate_submission_execution` 与 `material_input_template` 也会提升到顶层 `agent_decision` 和 `job_handoff`；当状态为 `prepare_materials` 时，`job_handoff.recommended_request` 会直接指向 `material_input_template.dry_run_request`，并同步暴露 `job_handoff.execute_request`，确保 AI 从候选推荐进入 live job 后不用深挖 summary 就能看到站规、限速、做种证据、素材缺口和下一步执行边界。
`/v1/jobs/candidates/daily/schedule` 会额外返回顶层 `schedule_digest`、`notification_payload`、`delivery_handoff` 和 `agent_decision`，把多个 schedule job 的 `push_payload`、`push_items`、`top_submit_requests`、`submission_handoff`、状态端点和缺失确认聚合到一个批次结果里，并在批次级汇总 `target_count`、`selected_count`、`ready_count`、`shortfall_count` 与 `target_met`，方便 OpenClaw/Hermes 或外部 cron 直接生成“今日可转种候选”推送；`notification_payload` 是后续 webhook/主动推送渠道的稳定载荷，包含 `title`、`summary`、`message`、ready/pending/blocked/目标短缺统计、`top_item`、`submit_items` 和下一步动作。`delivery_handoff` 会进一步把 `publish_ready`、`submission_ready`、目标 10 条是否达标、短缺数量、推送载荷字段、提交 handoff 和 stop_when 压到固定路径，作为 AI/cron 判断“现在该推送、继续轮询还是提交候选”的首选字段。CLI 的 `daily-schedule --write-notification` 和常驻 `daily-scheduler` 会把同一份载荷写成 `ptcli-daily-candidates-notification.json` 与 `ptcli-daily-candidates-notification.txt`，方便 AI、本地脚本或 IM/webhook 转发器直接消费；设置 `--notification-webhook-url` 或 `PTCLI_DAILY_CANDIDATE_WEBHOOK_URL` 后还会 POST 同一份 JSON 载荷，并把 `delivery_result` 写入命令输出和 `ptcli-daily-schedule-summary.json`，其中 `file_delivery`、`webhook_delivery`、`agent_handoff`、`blockers` 和 `next_actions` 会明确标出文件/ webhook 是否交付成功、失败后如何重试，推送失败不会触发上传或绕过规则，`summary-check` 也会把交付失败作为可审计 blocker 暴露。`submission_handoff.items[]` 优先指向 `/v1/jobs/candidates/{candidate_job_id}/submit`，只要求补 `confirm_upload=true` 和 `save_path`/`path` 等执行参数，源站/目标站身份从候选 job 继承；`submission_handoff.execution_summary` 会把批次级 `submit_count`、策略 ready 计数、第一条推荐提交请求、`post_submit_flow`、提交后 poll/summary 工具、`job_handoff` 读取路径、材料 dry-run resume 请求来源、重复/规则/确认 stop 条件和逐候选 submit endpoint/request 聚合到固定路径，便于 AI 一次读取就知道提交后该轮询、补素材、配置站点规则还是停止。每个 item 的 `policy_execution` 会把继承的 QB 限速、做种要求、转种/促销规则、rule obligations ready 状态和即将随候选提交继承的 qBittorrent 参数压到固定路径，便于 AI 在提交前复核策略不会被绕过；`after_submit.read_fields` 会把 `job_handoff`、`job_handoff.recommended_request`、`job_handoff.material_input_template` 放到优先读取路径，`after_submit.resume_when` 也会指向 `job_handoff.recommended_tool=resume_job` 且存在 recommended request，材料缺口则通过 `after_submit.material_resume_request=job_handoff.recommended_request when job_handoff.action=prepare_materials` 进入 dry-run resume；`submission_handoff.next_step` 与 `notification_payload.next_step` 会把第一条可提交候选压缩成可直接调用的 `tool`、`endpoint`、`method` 和 `request`。

OpenClaw/Hermes 可直接读取 `/.well-known/ptcli-agent.json` 或 `/v1/openclaw/skill.json`、`/v1/hermes/skill.json`，其中包含 OpenAPI 地址、工具列表、鉴权方式、live 上传安全边界、`closure_handoff` 动作契约，以及每个关键工具的 `input_schema`、`response_contract`、`safety`。反向代理或容器内外地址不一致时，设置 `PTCLI_PUBLIC_BASE_URL=https://your-host.example` 让 manifest 输出外部可访问地址；仓库内也提供 `ai/openclaw/ptcli.skill.json` 和 `ai/hermes/ptcli.skill.json` 作为离线模板。

## 配置要求

- qBittorrent client 配置沿用 `data/config.py`。
- HTTP 服务的 `/v1/qbit/inspect` 和 `/v1/qbit/match` 提供只读 QB 证据，返回 `hash`、`save_path`、`content_path`、`progress`、`state`、`category`、`tags` 和 `agent_summary`，不会添加种子、导出种子或修改限速；`/v1/qbit/export` 可按 hash 导出 `.torrent` 并生成 MTEAM-safe 目标候选种，返回 `target_torrent_file` 和 `target_upload_handoff`，只写文件、不上传、不改 QB 状态；`/v1/qbit/inject` 会把显式传入的本地 `.torrent` 加入 qBittorrent，并应用 category、tag、上传/下载限速后返回 `visible_in_client`、`verified_in_client`、`client_verification` 和 `rate_limits` 证据；`/v1/qbit/wait` 可按 hash 或内容路径等待完成，返回 `completion_verification`、匹配任务和 blockers，供源站下载完成或目标站新种做种闭环使用。
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
  新增站点或重写站点策略时也可以使用结构化配置：`automation.download/upload/retorrent`、`qbit_limits.download_limit/upload_limit`、`seeding_requirements.min_seed_time_hours/min_ratio`、`transfer_rules.required_promotions/freeleech_required/forbidden_title_patterns/forbidden_release_groups`；旧的 flat 字段仍兼容，`/v1/site-policies` 会同时返回 `policy_profile.flat_template` 和 `policy_profile.structured_template` 供 AI 复制。
  `data/example-config.py` 也提供 U2/CHD -> MTEAM 的 `PTCLI.SITE_POLICIES` 样板，但其中 `rule_review_fingerprint` 默认留空，复制后仍会阻塞 live 自动化，直到你完成站规审查并写入自己的审查标记。
  `required_promotions`、`freeleech_required`、`forbidden_title_patterns`、`forbidden_release_groups` 是本地自动化 gate，会在每日候选和策略审计中暴露/执行；无法程序化判断的站规仍必须通过 `rule_review_fingerprint` 和 `accept_rules` 人工确认。HTTP 服务也提供 `/v1/site-policies`，可直接读取 `policy_matrix[].automation`、`policy_matrix[].qbit_limits`、`policy_matrix[].seeding_requirements`、`policy_matrix[].transfer_rules`、`policy_matrix[].policy_coverage`、`policy_matrix[].execution_readiness` 和 `policy_matrix[].policy_profile` 供 AI 或部署脚本审计；其中 `policy_profile.template` 是可复制到 `config["PTCLI"]["SITE_POLICIES"][TRACKER]` 的站点配置模板，顶层 `config_templates.trackers` 会汇总本次涉及站点的模板。`policy_coverage` 会按源站/目标站角色列出缺失的 fingerprint、限速和做种要求，顶层 `policy_gap_summary` 会按 `source`/`target` 角色和 `rate_limits`、`seeding_requirements`、`rule_review` 分类聚合缺口，`execution_readiness` 则给出每个站点按角色是否可下载/上传/转种以及 blockers；`policy_setup_summary` 会把缺失或仍像模板占位符的 `rule_review_fingerprint` 压缩到 `missing_fingerprints` / `placeholder_fingerprints`，并在 `/v1/readiness/bundle.live_readiness` 中同步暴露；`policy_execution_summary` 会进一步把源站/目标站执行状态、QB 限速计划、做种要求、转种过滤规则、缺失配置和 `next_step` 压缩成 AI 可直接判断的执行摘要；`policy_execution_handoff` 固定暴露 `qbit`、`seeding`、`transfer_rules`、`rule_obligations`、`config.templates`、`continue_when` 和 `stop_when`，让 OpenClaw/Hermes 能判断是继续 live preflight 还是先补站点策略；`policy_handoff.next_step` 会在策略缺失时返回 `edit_config` 请求模板，在策略就绪时指回 `readiness_bundle` 继续 live 前检查。
- 任务式 API 默认把 job 文件写入 `PTCLI_JOB_DIR`，未设置时写入 `TMPDIR/ptcli-jobs`；Docker Compose 默认设置为 `/Upload-Assistant/tmp/ptcli-jobs`。
- `Dockerfile.ptcli` 是 focused CLI 镜像，只安装 `requirements-ptcli.txt` 和 ptcli 需要的系统依赖；旧 `Dockerfile` 保留给 legacy/full UA 入口。
- 默认发布构建使用 `Dockerfile.ptcli`，镜像入口是 `ptcli.py`；release 工作流会额外发布 `*-legacy-webui` 标签给旧 Web UI 镜像。
- 旧 `upload.py` 需要显式覆盖 entrypoint、使用 legacy Dockerfile，或拉取 `*-legacy-webui` 标签才会运行。
- `docker-compose.yml` 默认提供 `ptcli-api` 常驻 HTTP API 服务，使用项目内 `ptcli-net` 网络并带 `/health` healthcheck；一次性 CLI 服务放在 `cli` profile，可用 `docker compose --profile cli run --rm ptcli retorrent ...` 在盒子上执行；legacy Web UI 需要显式 `--profile legacy-webui`。
- `/v1/deployment/check` 会输出 `mounts`、`qbit`、`daily_candidates`、`docker_compose`、`deployment_handoff`、`agent_summary` 和 `agent_handoff`：AI 可以直接判断 config/cookies/tmp/job/downloads 挂载是否就绪、qBittorrent 是否配置、`PTCLI_DAILY_CANDIDATE_SCHEDULES` 是否已提供每日候选计划，以及 `docker-compose.yml` 是否包含可用的 `ptcli-api` 常驻服务（serve 命令、localhost 端口、healthcheck、API token env、host-gateway、downloads/config/cookies/tmp 挂载）和 `ptcli-daily-scheduler` 常驻 daily profile 服务或一次性 `ptcli-daily-schedule` 服务；`deployment_handoff` 会把 API base URL、`/health`、`/openapi.json`、`/v1/tools`、agent manifest、token 建议、手动一键查重提交入口和每日候选入口压缩到固定路径，`agent_summary` 也会提供 `compose_deployable`、`manual_workflow_ready`、`daily_workflow_ready`、`api_local_only`、`api_auth_recommended` 等短路径；`agent_handoff` 会给出手动转种和每日候选的推荐工具、端点、最小请求模板、必需确认和阻塞原因。`/v1/readiness/bundle` 会进一步把 deployment、site policies、daily schedule、非 live `live_verification` 凭据/图床/素材链路清单、doctor 命令模板和 `source_url_retorrent_job` 请求模板汇总到 `live_readiness`/`agent_decision`，用于 AI 在 live 前一次性判断是否还缺 cookie、MTEAM API key、qBittorrent 配置、图床、规则确认、目标站点、源站链接或盒子配置；`live_test_handoff.preflight_checklist` 会逐项暴露 deployment/site policy/credentials/materials/confirmations/doctor/manual job 是否 ready，`live_test_handoff.execution_plan` 会给出修复预检、运行 `ptcli doctor`、通过后提交 `source_url_check_and_submit` 的顺序；`seedbox_live_validation_handoff` 会把 compose API、qBittorrent、站点策略、凭据/素材、doctor 请求和 check-and-submit 请求压缩到同一个只读对象，作为 OpenClaw/Hermes 在盒子上尝试第一单 live 验证前的首选字段；`live_test_handoff.next_step` 会在就绪时给出可执行 `ptcli doctor` argv，未就绪时指向 deployment/site policy/readiness 修复路径。doctor 写出 `ptcli-doctor-summary.json` 后，`summary-check --json` 会返回 `doctor_result_handoff`，把 `live_safe_to_attempt`、blockers、summary-check 命令和通过后的 `source_url_retorrent_job` 入口压缩到固定路径。每日候选或 compose 定时服务未配置只作为 warning，不阻塞手动转种 API。
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

# 启动常驻每日候选触发器；按 PTCLI_DAILY_CANDIDATE_SCHEDULES 中的 time/timezone 每天扫描
docker compose --profile daily up -d ptcli-daily-scheduler

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
