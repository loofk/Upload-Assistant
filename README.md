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
- `ptcli serve` 会启动本地 JSON HTTP API，提供 `/health`、`/openapi.json`、`/v1/tools`、同步 `/v1/retorrent/check`/`/v1/retorrent`、AI 预检 `/v1/agent/run-preview`、创建任务前同步 `/v1/retorrent/source-url/preflight`、`/v1/deployment/check`、`/v1/readiness/bundle`、站点能力/规则 profile `/v1/sites`、qBittorrent 检查/匹配/导出/注入/等待 `/v1/qbit/inspect`/`/v1/qbit/match`/`/v1/qbit/export`/`/v1/qbit/inject`/`/v1/qbit/wait`、外部元数据/PTGen 准备 `/v1/metadata/prepare`、本地素材准备 `/v1/materials/prepare`、目标站发种包、上传预检和上传闭环 `/v1/target/package/prepare`/`/v1/target/upload/preflight`/`/v1/target/upload`、每日候选 `/v1/candidates/daily`/`/v1/candidates/daily/schedule`，以及任务式 `/v1/jobs/retorrent/check`、`/v1/jobs/retorrent`、`/v1/jobs/retorrent/from-url`、`/v1/jobs/retorrent/from-url/check-and-submit`、`/v1/jobs/retorrent/submit`、`/v1/jobs/metadata/prepare`、`/v1/jobs/materials/prepare`、`/v1/jobs/target/package/prepare`、`/v1/jobs/target/upload`、`/v1/jobs/candidates/daily`、`/v1/jobs/candidates/{job_id}/submit`、`/v1/jobs`、`/v1/jobs/{job_id}`、`/v1/jobs/{job_id}/summary`、`/v1/jobs/{job_id}/resume`、`/v1/jobs/{job_id}/cancel`，方便 AI/自动化工具按 OpenAPI 或简单 JSON 调用。
- `/v1/goal/progress` 是最终目标进度的只读总控短路径，会把 Docker Compose 部署、AI 契约、任务 API、手动源链接转种、metadata/materials、每日候选、站点规则、qBittorrent 执行、站点 adapter、盒子 live 验证和 legacy 清理压成 `completion_estimate`、`capabilities`、`critical_path_remaining`、`evidence`、`next_step` 和 blockers；它会区分 `complete`、`partial`、`ready_to_submit`、`submitted_running`、`submitted_needs_resume`、`submitted_ready_to_report`、`unverified`、`missing`、`not_started`，避免把已有代码骨架或已提交但未闭环的 live job 误报成完成。传入 `job_id`/`live_job_id` 或 `summary_file`/`live_summary_file` 时，它会只读复核对应 job/summary 的 `live_user_report.report_allowed=true`、`missing_evidence=[]` 和 blockers，满足后才把 `seedbox_live_validation` 及其依赖能力提升为完成；若传入的 doctor summary 已通过 `/v1/summary/check.live_submission_package.ready=true`，则会把 `seedbox_live_validation` 标为 `ready_to_submit` 并让 `next_step` 直接指向 `/v1/jobs/retorrent/from-url/check-and-submit`；若传入的是已提交 live job，则会沿用 `live_validation_followup` 给出 poll/resume/report 的下一步。
- 任务响应中的 `job_handoff.recommended_call` 会把下一步 `tool`、`endpoint`、`method`、`request`、`dry_run_request`、`execute_request`、`safe_to_call_now`、`requires_user_review`、`gates` 和 safety/stop 条件聚合成单一对象，便于 OpenClaw/Hermes 按 queued/running/blocked/complete 状态安全轮询、预览恢复、执行恢复或读取 summary；其中 `gates` 会明确 dry-run 预览、execute 请求、`accept_rules`、`confirm_upload` 和缺失 flags，live 执行仍必须满足站点规则和上传确认。
- `closure_summary.completion_report` 是任务收尾首选短路径，会把 `complete`、`ready_for_user_report`、`verdict`、关键 gates、缺失 gates、源/目标 hash/path、重复检查、证据、下一步工具和 blockers 聚合到一个对象；只有 `completion_report.complete=true` 且 `blockers=[]` 时，AI 才应向用户报告该转种闭环已完成。
- `seedbox_live_validation_completion_report` 是盒子首单 live 验证提交 job 后的收尾短路径，会把 `status`（`complete` / `duplicate_stopped` / `running` / `needs_resume` / `blocked`）、`checks`、`missing_evidence`、源/目标 hash、上传后新种路径、qBittorrent 限速/做种证据、`recommended_call`、dry-run/execute 请求、下一步工具和 blockers 固定到一个对象；只有 `ready_for_user_report=true`、`missing_evidence=[]` 且 `blockers=[]` 时，AI 才能把首单验证报告为完成。
- `live_user_report` 是面向 AI 最终回报用户的最短路径，会聚合 `live_completion_gate`、`seedbox_live_validation_completion_report`、`closure_summary` 和 qBittorrent 限速/做种证据；只有 `ready_for_user_report=true`、`report_allowed=true`、`evidence.missing_evidence=[]` 且 `blockers=[]` 时，AI 才能宣告 live 转种完成，否则必须报告 `report_blocked_reason`、`recommended_call` 和 blockers。
- `live_validation_final_report` 是首单 live 验证的最终用户报告出口，会在 job status、summary 和 list 中固定暴露 `report_allowed`、`verdict`、源/目标 hash、重复检查、qBittorrent 证据、audit refs 和下一步调用；它只在 `live_user_report`、`seedbox_live_validation_completion_report`、`closure_summary` 都满足完成条件且无缺失证据时允许报告。
- `policy_execution_final_report` 是规则执行、限速和做种要求的最终安全出口，会在 job status、summary、list、`agent_decision` 和 `workflow_context` 中固定暴露 `ready_for_live`、`verdict`、站点规则确认、qBittorrent 上传/下载限速审计、做种要求、runtime contract、推荐调用和 blockers；OpenClaw/Hermes 应先读它，再按 `read_order` 复核 `policy_execution_report`、`qbit_execution_gate`、`qbit_enforcement_summary` 和 `qbit_limit_audit`。
- `material_preparation_final_report` 是素材/描述链路的最终安全出口，会在 job status、summary、list、`agent_decision` 和 `workflow_context` 中固定暴露 IMDb/TMDb/豆瓣、PTGen 描述、MediaInfo/BDInfo、截图、图床 URL、目标 payload、target package、`ready_for_target_upload`、`verdict`、推荐调用和 blockers；OpenClaw/Hermes 应先读它，再按 `read_order` 复核 `material_evidence_summary`、`material_gap_summary`、`materials_handoff`、`metadata_prepare_handoff`、`materials_prepare_handoff` 和 `target_package_handoff`。
- `manual_retorrent_final_report` 是手动源链接转种/每日候选提交后正式转种 job 的总控报告，会在 job status、summary、list、`agent_decision`、`workflow_context`、`candidate_submission_handoff`、`candidate_submission_summary`、`candidate_submit_followup`、`candidate_submit_sequence` 和每日批次 `submitted_jobs` 中串联重复检查、`policy_execution_final_report`、`material_preparation_final_report`、目标上传、closure、live gate 和最终验证报告，固定暴露 `verdict`、`report_allowed`、`recommended_call`、`complete_when`、`stop_when`、blockers 和下一步动作；AI 应先读它判断是停止重复种、请求确认、补规则、补素材、继续上传闭环、轮询还是向用户报告完成。
- `daily_candidate_final_report` 是每日候选批次的最终报告出口，会在 schedule job、batch status 和 job list 中固定暴露 `verdict`、目标/已选/可提交/短缺/待轮询数量、notification、approval、submission、shortfall recovery、audit refs 和下一步调用；AI 应先读它再决定报告候选、补扫短缺、轮询任务或请求用户批准提交候选。
- `readiness_bundle.live_execution_package` 是盒子首单 live 验证的执行总包，会把 doctor、summary-check、查重提交、poll/resume 和最终 `live_user_report` 读取顺序固定到 `steps`/`run_order`/`report_contract`，AI 应按它执行 U2/CHD -> MTEAM 首单验证。
- 手动转种 job 的请求 schema 已暴露素材准备参数，可在创建任务时直接传 `metadata_file`、`ptgen_description_file`、`mediainfo_file`、`bdinfo_file`、`screenshot_files`、`image_host_file`，或传 `enrich_metadata`、`fetch_ptgen`、`generate_mediainfo`、`generate_bdinfo`、`generate_screenshots`、`upload_screenshots` 让服务在目标站准备包前生成缺失素材；也可先调用 `/v1/metadata/prepare` 或 `/v1/jobs/metadata/prepare` 准备 IMDb/TMDb/豆瓣/PTGen 描述，再调用 `/v1/materials/prepare` 显式生成 MediaInfo/BDInfo、截图和图床上传证据，或用 `/v1/jobs/materials/prepare` 把耗时素材步骤纳入 queued/running/blocked/complete 任务体系。metadata/materials 任务请求都可携带 `parent_job_id`/`job_id`、`source_url`、`target`、`accept_rules`、`confirm_upload` 和 QB 限速/分类上下文；读取返回的 `material_options` / `material_evidence` / `metadata_prepare_handoff.next_step` / `materials_prepare_handoff.next_step` 后，可直接按 handoff 恢复已有 `resume_job`，或用 `source_url_check_and_submit_request` 重新走查重+提交链路；也可以把 `source_info`、metadata/material handoff、`duplicate_check`、`rule_check` 和 qBittorrent 内容证据交给 `/v1/target/package/prepare` 或 `/v1/jobs/target/package/prepare`，生成 MTEAM 发种包、描述草稿、`target_upload_preflight` 和 `target_package_handoff.target_upload_request`，再用 `/v1/target/upload/preflight` 验证 MTEAM-safe torrent，或用 `/v1/jobs/target/upload` 在 `confirm_upload=true`、`download_uploaded_torrent=true`、`inject_uploaded_torrent=true`、`wait_uploaded_complete=true` 后执行上传、下载目标站新种、注入 qBittorrent 并等待做种闭环；这些输入会进入 job `request.material_options` 和 `command_argv`，便于 AI 后续审计和续跑。源链接/手动转种 job 现在还会返回 `retorrent_stage_handoff`：当查重、规则、确认和内容证据齐备时，它会直接给出 `/v1/jobs/target/package/prepare` 或 `/v1/jobs/target/upload` 的下一段 `recommended_request`，让 OpenClaw/Hermes 不必手工拼接发种包或上传闭环参数。
- `sites --json` 和 `/v1/sites` 暴露每个站点的 `source_info`、`source_info_adapter`、`source_download`、`source_download_adapter`、`credential_requirements`、`target_upload`、`full_live_closure_to_mteam` 能力，并提供 `adapter_contract`、`extension_plan`、`extension_checklist`、`extension_validation_matrix`、`extension_handoff` 和 `tracker_rollout_handoff`，把新增中文 PT 站点时缺少的 source adapter、download adapter、target upload adapter、policy profile、参考流、验证端点、必需返回证据、站点规则配置契约、下一步实现动作和源站/目标站 rollout 优先级结构化给 AI。
- `rule-check --json` 暴露 `rule_obligations[].review_scope.required_confirmations`，供 agent 在 live 前逐项提示人工确认。
- `flow-check --json` 暴露 `source_capability`、`target_capabilities` 和去重后的 `credential_requirements`，供盒子脚本在 live 前检查配置缺口。
- `pipeline` 和 `retorrent --execute` 返回 `requested_actions`、`effective_actions`、`closure`、`evidence`、`artifacts`、`resume_commands`、`resume_state`、`next_actions`；`requested_actions` 会区分 `source_torrent_file`、`uploaded_torrent_id` 和 `uploaded_torrent_file` 等恢复输入，`evidence.target.mode` / `summary.target.mode` 会标明目标侧是 `live_upload`、`resumed_uploaded_id` 还是 `resumed_uploaded_torrent`。
- 任务式 API 的 job 状态和 summary 会暴露 `job_control_summary`、`job_progress_handoff`、`job_handoff`、`recovery_handoff`、`job_resume_handoff`、`job_lineage`、`agent_decision`、`materials_handoff`、`target_upload_handoff`、`closure_handoff`、`manual_retorrent_handoff`、`candidate_batch_handoff`、`candidate_submit_followup`、`resume_plan`、`resume_execution_handoff`、`resume_lineage`、`material_resolution` 和 `candidate_submission`；`job_control_summary` 是推荐给 AI 最先读取的首选控制面短路径，会把当前 job 压成 `state`、`action`、`recommended_call`、`dry_run_request`、`execute_request`、`read_order`、`complete_when`、`stop_when`、`sources`、`blockers` 和 `next_actions`，稳定回答“该 poll、停止重复种、补素材 dry-run、执行 resume、上传闭环还是读取 summary”；`job_progress_handoff` 是进度条短路径，会把源站识别、目标查重、规则 gate、素材、目标包、目标上传、上传后做种和最终报告压成 `stages[]`、`current_stage`、`progress.percent`、`next_step`、`recommended_tool`、`stop_when` 和 blockers，适合 AI 给用户汇报当前卡在哪一步；`job_resume_handoff` 是更窄的续跑短路径，会直接给出 `action`、`recommended_tool`、`recommended_endpoint`、`dry_run_request`、`execute_request`、需要补的 overrides/确认、`read_before_execute`、`stop_when` 和 blockers，适合 agent 在 blocked/failed 后先 dry-run 再执行 allowlisted resume；候选提交后的正式转种 job 还会额外暴露 `candidate_submit_followup`，把父候选 job、已提交 retorrent job、当前 action、推荐工具/请求、status/summary/resume endpoint 和 `read_order` 压到一个固定路径，作为从每日候选进入正式转种后的首选追踪字段；`job_handoff` 会继续提供更细的 `wait`、`resume`、`submit_if_clear`、`prepare_materials`、`done`、`stop` 等动作，以及 `recommended_tool`、`recommended_endpoint`、`recommended_request`、`dry_run_request`、`execute_request`、`poll_after_seconds`、`can_resume`、`resume_recommended`、`can_attempt_live`、`candidate_submission_execution`、`material_input_template`、`blockers` 和 `next_actions`；`recovery_handoff` 是长流程恢复详细路径，会把运行轮询、素材补齐、目标 payload 修复、QB 证据修复、resume 预览/执行、summary 收尾统一压成 `phase`、`action`、`recommended_tool`、`recommended_endpoint`、`dry_run_request`、`execute_request`、`gates`、`handoff_sources`、`read_fields`、`blockers` 和 `next_actions`，方便 OpenClaw/Hermes 不必自行合并多个 handoff。候选提交后的 `prepare_materials` 会让 `recommended_request` 直接指向素材模板的 dry-run resume 请求，并同时给出对应 `execute_request`。`job_lineage` 会把父任务、root 任务、子任务、最新子任务、active child 和链路深度暴露出来，方便一次转种经历多次 resume 后仍能追踪完整父子任务链。其他 handoff 会继续给出 `decision`、`recommended_action`、`stop_reason`、`duplicate_check`、`duplicate_clear`、`missing_confirmations`、`should_poll`、`should_resume`、`action`、续跑 endpoint、allowlist 判断、父任务来源、每日候选来源和 `next_command_argv`；`candidate_batch_handoff` 会把单个每日候选 job 中可提交的 `push_items` 压成 `submit_daily_candidate_job` 的 `recommended_endpoint`、`recommended_request`、`required_overrides` 和 `items[]`，让 AI 在用户确认后从候选批次安全创建正式转种 job，而源站/目标站身份仍继承自候选 job；`material_evidence_summary` 是素材/描述缺口的首选短路径，会把 IMDb/TMDb/豆瓣/PTGen、MediaInfo/BDInfo、截图、图床和目标站 payload 压成 `ready`、`missing_domains`、`checks_by_domain`、`recommended_request`、`dry_run_request`、`execute_request`、`complete_when` 和 blockers；`materials_handoff` 仍保留完整 `recommended_inputs`、`material_plan`、`resume_request_template`、`resume_handoff` 和 `next_actions`，其中 `recommended_inputs[].accepted_keys` 与 `material_plan.items[].resume_overrides` 会明确列出可恢复输入（如 `imdb_id`、`tmdb_id`、`douban_id`、`metadata_file`、`fetch_ptgen`、`generate_mediainfo`、`generate_screenshots`、`upload_screenshots`、`image_host_file`），`resume_handoff` 会给出聚合的 `dry_run_request`/`execute_request` 以及逐项 `staged_requests`，便于 AI 先预览再补齐全部素材缺口；`resume_execution_handoff` 会把通用续跑的 `dry_run_request`、`execute_request`、allowlisted overrides、确认/素材 gate 和 stop_when 集中到固定路径，要求先预览再执行；`target_upload_handoff` 会把目标站上传前的 payload/preflight、查重、规则、确认和上传后做种证据压缩到 `action`、`ready_for_live_upload`、`uploaded_seeding_ready`、`blockers`、`next_actions` 和可执行的 `next_step` / `recommended_tool` / `recommended_endpoint`；`manual_retorrent_handoff.live_checklist` 会把源站识别、目标查重、显式确认、站点策略、素材、目标 payload、上传后做种和 QB 限速审计压成固定清单，`closure_handoff.closure_checklist` 会进一步汇总源站下载、目标站新种注入做种、重复种、QB 证据、summary/evidence 和最终 `complete` 状态，并给出统一的 `next_step` / `recommended_tool` / `recommended_endpoint`，方便 agent 判断该停止、补确认、继续轮询、补素材续跑、修复 QB 证据还是收尾；`resume_job` 会把父任务 `materials_handoff` 与本次 allowlisted overrides 对齐成 `material_resolution`，暴露已覆盖和仍未覆盖的推荐素材输入；`/v1/jobs/{job_id}/resume` 支持白名单 overrides，可在续跑前补 `accept_rules`、`confirm_upload`、路径、qBittorrent 分类/标签/限速和素材文件，未知字段会进入 `ignored_overrides` 而不会拼进命令；同时暴露 `workflow_context`，把源站链接解析、目标站、候选来源、查重 gate、规则 gate、素材/描述缺口、qBittorrent 做种证据、缺失确认、续跑 argv 和 blockers 汇总到固定路径。
- `queued` / `running` job 会额外暴露 `runtime.should_poll`、`runtime.poll_after_seconds`、`runtime.status_endpoint`、`runtime.elapsed_seconds` 等字段，方便 OpenClaw/Hermes 按服务建议轮询，不需要自行计算时间戳。
- `ptcli serve` 默认 `PTCLI_MAX_CONCURRENT_JOBS=1`，同一时间只执行一个长耗时 job，其余保持 `queued`；`/v1/jobs` 会返回 `queue.max_concurrent_jobs`、`running_count` 和 `queued_count`，便于 agent 判断排队情况。`/v1/jobs.daily_candidate_batch_summary` 会把每日候选 job 与候选提交后生成的正式转种 job 聚合成批次视图，暴露 `candidate_job_count`、`submitted_retorrent_job_count`、`unsubmitted_safe_count`、`retorrent_status_counts`、`retorrent_action_counts`、`items[].submit_requests`、`items[].submitted_jobs`、`blockers` 和 `next_actions`；`/v1/jobs.daily_candidate_batch_gate` 会进一步把同一批次压成 `action=submit_candidate` / `poll_submitted_jobs` / `resolve_blockers` / `complete` / `inspect_empty`、首个提交请求、首个已提交 job、推荐工具/端点/请求和 stop 条件；`/v1/jobs.daily_candidate_submission_plan` 是刷上传批次的提交短路径，会聚合所有尚未提交的安全候选、`first_submit_request`、`submit_requests[]`、`shortfall_recovery`、`hard_blockers` 与 `shortfall_blockers`，让 OpenClaw/Hermes 能先提交可安全执行的候选，再按 `shortfall_recovery.recommended_request` 补足每日 10 条目标；`/v1/jobs.daily_candidate_execution_summary` 是提交后的结果短路径，会把正式转种 job 分成 `complete_jobs`、`running_jobs`、`blocked_jobs`，暴露 `remaining_submit_count`、`ready_shortfall_count`、`completion_ratio`、下一步工具/端点/请求和 stop 条件；`/v1/jobs.daily_candidate_refill_plan` 是补足 10 条的控制面，会暴露 `ready_shortfall_count`、`scan_count`、`max_scan_count`、`scan_exhausted`、已覆盖/已提交/阻塞 source ids、`pagination_supported=false`、排重策略和重跑请求，避免 AI 重复提交同一候选或假装已有分页能力。若只想读取每日候选批次状态，可调用只读入口 `/v1/jobs/candidates/daily/batch?source_tracker=U2&target=MTEAM`，它返回同一份 `daily_candidate_batch_summary` / `batch_summary`、`daily_candidate_batch_gate` / `batch_gate`、`daily_candidate_submission_plan` / `submission_plan`、`daily_candidate_execution_summary` / `execution_summary` 和 `daily_candidate_refill_plan` / `refill_plan` 别名，不会创建任务、下载或上传。
- `/v1/jobs/{job_id}/cancel` 只允许取消仍处于 `queued` 的任务；`running` 任务会返回 409，不会强行中断 live tracker 或 qBittorrent 操作。
- `ptcli serve` 启动时会把上次进程遗留的 `queued` / `running` job 标记为 `blocked` 并写入 `interruption`，避免容器重启后 agent 永久轮询；如存在 allowlisted `resume_state.next_command_argv`，`resume_plan` 会继续给出可审计续跑入口。
- 转种 job 状态和 summary 也会暴露 `policy_coverage` 和 `policy_handoff`；`policy_handoff` 会把源站/目标站规则页、人工审查 fingerprint、上传/下载限速、做种要求、缺失策略字段和下一步工具压缩成固定路径。`site_policies.policy_readiness_summary` 是站点策略配置的首选短路径，会汇总 ready/phase、缺失限速、缺失做种要求、缺失或占位 fingerprint、首个 blocker、推荐下一步和可复制的 `config.preferred_patch`；`site_policies.policy_repair_gate` 会进一步把策略修复压成 `action=review_rules` / `edit_config` / `resolve_blockers` / `ready_for_live_preflight`、人工审查要求、首个 blocker、preferred patch、手动步骤和重跑请求，作为 AI 修复站规配置前的首选控制面；`site_policies.policy_config_handoff` 会把规则审查请求、限速/做种配置 patch、fingerprint patch 合并顺序、逐站点缺口和复核请求压成固定路径，明确 `rule_review_fingerprint` 只能来自人工审查，AI 不得自动生成后静默应用；`site_policies.policy_execution_handoff` 会进一步把 QB 限速、做种要求、转种过滤规则、规则 obligation、配置模板和继续/停止条件压成 AI 可直接读取的执行 handoff。当 `accept_rules` / `confirm_upload` 已齐但源站或目标站缺少 fingerprint、限速或做种要求时，`agent_decision.decision=configure_policy`，避免 agent 直接进入 live 上传。
- `site_policies.policy_execution_contract` 是创建 live 转种 job 前的规则/限速执行契约，会把 `policy_runtime_contract`、`policy_enforcement_bundle` 和 `policy_execution_sequence` 压成一份短路径：`live_request_template`、`required_request_fields`、`request_defaults`、`protected_fields`、source/target 角色、QB client 字段、做种要求、rule contract、completion evidence 和 resume contract。AI 创建或续跑 live job 前应先读它，保留所有 protected 限速字段，且只在用户明确确认并有更严格/等价证据时覆盖。
- `/v1/site-policies/rule-review` 是人工站规审查后的配置辅助入口：`policy_repair_gate.action=review_rules` 时会把 `recommended_tool` 指向 `site_policy_rule_review`，并给出 `rule_review_request`；只有显式传入 `rules_reviewed=true`、`reviewer` 和 `reviewed_at` 才会生成每站点 `rule_review_fingerprint`、`config_patch.structured_patch` / `flat_patch` 和 `after_edit` 重跑请求。它不联网、不编辑配置、不替用户确认规则，只把人工审查证据转换成可审计 patch。
- retorrent/manual job 会在未显式传入限速时，从 `PTCLI.SITE_POLICIES` 自动补齐 `qbit_download_limit`、`qbit_upload_limit`、`uploaded_qbit_upload_limit`、`uploaded_qbit_download_limit`；job 状态、summary 和 `agent_decision.policy_qbit_defaults` 会记录哪些值来自站点策略、哪些值由请求覆盖，`qbit_plan` 会汇总 source/uploaded 两侧最终的分类、标签、上传/下载限速和来源，`qbit_limit_audit` 会把计划限速与实际 qBittorrent 注入结果中的 `rate_limits.calls` 对齐，区分 `applied`、`pending` 和 `mismatch`；`qbit_handoff` 会把两侧的分类、标签、限速来源、审计状态和下一步动作压缩成 AI 可直接读取的 QB 执行摘要，其中 `enforcement_handoff.roles[]` 会逐项列出 source/uploaded 的预期限速、观察到的限速、是否缺注入证据、是否需要修复限速，以及下一步 resume/get-summary 建议；`qbit_enforcement_summary` 会把同一审计再压缩成 `expected_role_count`、`applied_role_count`、`pending_roles`、`mismatch_roles`、`recommended_tool` 和 blockers；`qbit_execution_gate` 会把这些 QB 限速/注入/做种证据进一步压成 `ready`、`action`、`first_blocker`、`dry_run_request`、`execute_request`、`read_order` 和 `continue_when`/`stop_when`，作为 AI 判断刷上传自动化是否可继续的最短路径；`policy_execution_report` 会进一步把规则接受状态、做种要求、站点策略默认限速、请求覆盖、QB 计划和实际执行摘要压成 AI 首选短路径，便于 OpenClaw/Hermes 判断“站点规则和限速/做种策略是否已经可继续自动化”；resume job 的 `resume_context.inherited_policy` 也会保留父任务的策略上下文。
- `pipeline --write-summary` 会在 `ptcli-run-summary.json` 顶层写入 `flow_check`，并在 `summary.flow` 中保留源站/目标站适配器和凭据要求。
- 带执行动作的命令未闭环时返回 `status: blocked`、顶层 blockers 和非 0 退出码。
- `--write-summary` 会写出带 `automation_handoff` 的 summary JSON，并在本次命令的 JSON 返回中同步给出该字段；其中 `resume_state.next_stage` / `resume_state.next_command` 可供 agent 或脚本直接续跑，`automation_handoff` 则给出检查和执行续跑的 `summary-check` command/argv。
- `summary-check --json` 会暴露 `automation_handoff`、`readiness_summary`、`flow_diagnostics`、`credential_requirements`、`source_mode`、`target_mode`、`automation_reason`、`qbit_wait_retry_hints`、每日候选 `daily_candidate_targets`、`approval_queue`、`top_safe_candidates`、`recommended_approval_request` 和逐条标注可执行性的 `candidate_commands`；`readiness_summary` 汇总源站、素材/描述、规则、目标上传、上传后做种、每日候选目标/短缺、安全候选数量、审批队列 ready 状态和下一条命令状态，方便 AI/盒子脚本按关键闭环优先级续跑；`--print-next-command` 可只输出下一条安全续跑命令，`--print-next-argv` 可输出对应 argv JSON；`--print-shell` 可输出 `PTCLI_*` shell 变量（含 `PTCLI_READINESS_*` / `PTCLI_DAILY_CANDIDATE_*` / `PTCLI_SOURCE_MODE` / `PTCLI_TARGET_MODE` / `PTCLI_AUTOMATION_REASON` / `PTCLI_FLOW_READY` / `PTCLI_CREDENTIAL_REQUIREMENTS` / `PTCLI_RUNNABLE_COMMAND_COUNT` / `PTCLI_CLOSURE_STATUS_*` / `PTCLI_QBIT_WAIT_SOURCE_*` / `PTCLI_QBIT_WAIT_UPLOADED_*`）；`--run-next-command` 可直接执行下一条受限的 `ptcli.py` 续跑命令。
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

手动源链接转种的 AI runbook 固定为：先调用 `source_url_retorrent_preflight` 读取 `ready_to_create_job`、`source_reference`、`target_trackers`、`policy_execution_summary`、`policy_execution_handoff`、`duplicate_check.next_request`、`duplicate_check_handoff`、`job_creation_handoff.request`、`next_step` 和 `blockers`；该端点不创建 job、不访问 tracker、不访问 qBittorrent，只用于判断源链接解析、部署、规则、确认和素材前置条件是否足以进入任务创建。若 `ready_to_create_job=false`，AI 必须按 `next_step` 修复部署、策略、确认或配置缺口；若 ready，再调用 `readiness_bundle` 复核 `live_readiness.ready_for_manual_retorrent`、`live_readiness.policy_execution_handoff`、`live_test_handoff.policy_execution_handoff` 和 `manual_job_template.request`；再调用 `site_policies` 确认 `ready=true`、`policy_execution_handoff.ready=true`、`execution_readiness.ready=true` 与 `rule_obligations.*.ready=true`，其中源站 scope 应为 `download_and_retorrent`、目标站 scope 应为 `upload_and_seed`。推荐的一步式入口是 `source_url_check_and_submit` / `/v1/jobs/retorrent/from-url/check-and-submit`：它会先同步执行目标站查重，若 `duplicate_check.exists=true` 则返回 `status=blocked`、重复种信息并且不创建 live job；只有 `duplicate_check.searched=true`、`duplicate_check.exists=false`、`submit_if_clear_handoff.ready=true`、`accept_rules=true`、`confirm_upload=true` 时才创建后续 live job 并返回 `job_id`、`status_endpoint` 和 `summary_endpoint`；该响应的首选短路径是 `check_and_submit_gate`，其 `action` 会稳定区分 `poll_job`、`stop_duplicate` 和 `resolve_gate_blockers`，并同步给出 `duplicate_clear`、`submit_ready`、确认状态、推荐工具/端点/请求和 stop 条件。需要拆分审计时，也可以调用 `retorrent_check` / `/v1/retorrent/check` 或异步 `retorrent_check_job` / `/v1/jobs/retorrent/check` 执行目标站查重，只有 `duplicate_check.searched=true`、`duplicate_check.exists=false` 且 `submit_if_clear_handoff.ready=true` 时，才把同步结果里的 `submit_if_clear_handoff.request` 提交给 `source_url_retorrent_job`，或把已完成的查重 `job_id` 提交给 `submit_checked_retorrent_job` / `/v1/jobs/retorrent/check/{job_id}/submit`；若 `duplicate_check.exists=true`，必须停止并返回重复种信息。由查重派生的 live job 会在 `check_submission` 中记录查重结果、继承 request、执行覆盖参数和 qBittorrent 限速/分类证据，方便审计 AI 没有绕过查重 gate。任务创建后优先读取 `job_handoff`：当 `job_handoff.action=wait` 且 `job_handoff.should_poll=true` 时按 `job_handoff.poll_after_seconds` 轮询 `get_job_status`；当 `job_handoff.action=resume` 时先用 `job_handoff.recommended_request` 预览 `resume_job`；当 `job_handoff.action=stop` 时停止并报告 blockers；当 `job_handoff.action=done` 时读取 `get_job_summary` 并回报闭环证据。完成后仍需优先读 `live_user_report`，并同时参考 `seedbox_live_validation_completion_report`、`closure_summary` / `closure_handoff` 作为详细闭环证明；若 `seedbox_live_validation_completion_report.status` 返回 `duplicate_stopped`、`needs_resume`、`blocked`，或 `closure_summary.action` 返回 `stop_duplicate`、`collect_confirmations`、`configure_policy`、`prepare_materials`、`repair_target_payload`、`repair_qbit` 或 `resolve_blockers`，AI 必须按 `seedbox_live_validation_completion_report.recommended_call`、`job_handoff` 或 `closure_summary.next_step` 和 blockers 停止、补资料或续跑；只有 `live_user_report.report_allowed=true`、`live_user_report.evidence.missing_evidence=[]` 且 `live_user_report.blockers=[]` 才能把任务视为真实闭环完成。调用前应读取 `resume_summary`、`materials_handoff.recommended_inputs`、`target_upload_handoff.blockers`、`closure_summary.blockers` 和 `resume_requirements`，按其中的 `missing_confirmations`、`suggested_overrides`、`recommended_inputs`、`dry_run_request`、`execute_request` 和 `allowed_overrides` 补充缺失确认、路径、限速或素材文件；真正续跑前建议优先调用 `job_handoff.recommended_request`、`resume_summary.next_step` 或 `resume_job` 并设置 `dry_run=true` 预览 patched `command_argv`，确认后再用同一组 allowlisted override 去掉 `dry_run` 执行；调用后应读取 `resume_summary`、`resume_audit`、`resume_context.applied_overrides` 和 `material_resolution.covered_recommended_inputs` / `unresolved_recommended_inputs` 判断父子任务关系、override 是否生效以及本次续跑是否覆盖了缺口；未知 override 会被忽略并记录在 `resume_context.ignored_overrides`。

AI 查询 job 后应优先读取 `job_control_summary`：`action=poll` 时轮询 `get_job_status`，`action=resume_preview` 时先调用 `dry_run_request`，`action=resume_execute` 时在用户确认后调用 `execute_request`，`action=stop_duplicate/blocked/failed/cancelled` 时停止并报告 blockers，`action=read_summary` 时读取 `get_job_summary` 并优先报告 `live_user_report`，同时保留 `seedbox_live_validation_completion_report` 与 `closure_summary` 作为审计证据；`job_handoff` 和 `recovery_handoff` 继续作为详细兼容字段。

每日候选响应会按“ready 优先、score 0-100 降序、源站列表顺序兜底”排序，并固定围绕 `target_count`（默认 10）报告 `scan_count`、`selected_count`、`ready_count`、`shortfall_count`、`target_met` 和 `target_summary`，方便 AI 明确判断“今天是否真的凑够 10 条候选/可提交候选”，而不是把少量结果误判为达标。AI 应优先读取顶层或 `digest.candidate_control_summary`：它把每日候选压成 `action=submit_candidate` / `rerun_daily_candidates` / `poll_candidates` / `resolve_candidate_blockers`、`first_submit_request`、推荐工具/端点/请求、短缺恢复、`read_order`、blockers 和 next actions，作为是否提交、补候选、轮询或停止的首选控制面；详细审批依据再读取 `daily_candidate_batch_report`、`approval_queue` 和 `execution_plan`。每条候选包含 `ranking.score`、`ranking.tier`、`ranking.reasons`、`ranking.penalties` 和 `ranking.signals`，方便 AI 先选择无重复、元数据完整、规则风险低的候选；有阻塞项时仍会保留 `blockers` 和 `next_actions`，不会静默跳过规则或查重。候选还会给出 `policy_summary`、`policy_coverage`、`policy_execution_handoff` 和 `policy_risk_summary`，汇总站点自动化 gate、QB 限速、做种要求、转种过滤规则、规则审查 fingerprint、`rule_obligations_ready` 以及缺失策略字段；`policy_risk_summary` 会把限速 ready、做种要求 ready、规则确认 ready、严格转种过滤数量、policy blockers 和 `execution_priority` 压到固定路径，作为 AI 判断刷上传候选是否“低风险可优先/严格规则需复核/应停止”的首选字段。`policy_summary.rules.fingerprint_status` 会标出源站/目标站 fingerprint 是否缺失或仍是模板占位符，`policy_execution_handoff.ready` 也必须同时满足 rule obligations、限速和做种要求，此时候选会保持 blocked 并给出 `decision_summary.action=configure_policy`，不会只因 `accept_rules=true` 进入可提交状态；同时提供 `decision_summary`，把 `action`、`risk_level`、`policy_risk_level`、`metadata_ready`、`duplicate_clear`、`policy_coverage_ready`、`primary_blocker` 和推荐/惩罚原因压缩成 AI 可直接判断的摘要。`digest.approval_queue`、`digest.approval_prompts`、`digest.top_safe_candidates` 和 `digest.execution_plan` 是 AI 审批每日候选的详细入口：approval queue 只收录 `can_submit=true`、目标查重 clear、`policy_risk_level=low` 且候选风险 low 的条目；每个 `approval_prompt` 会给出可展示给用户的 `approval_text` / `confirm_phrase`、审批后唯一允许调用的 `submit_tool` / `submit_endpoint` / `submit_request`、必需确认、stop 条件和安全边界，提示本字段本身不会执行上传，必须等用户明确确认后才能提交；`execution_plan` 会进一步给出 `safe_to_submit_count`、`ready_shortfall_count`、`recommended_submit_requests`、`shortfall_recovery`、`next_step`、`recommended_tool`、`recommended_request`、`continue_when` 和 `stop_when`，让 AI/cron 能判断是提交第一条安全候选、继续补足 10 条，还是先修复规则/查重/元数据阻塞。中风险候选会进入 guarded 统计，重复、规则缺口或高风险候选会进入 blocked 统计，`continue_when`/`stop_when` 和 `requires_confirmation` 会再次提醒必须由用户确认 `accept_rules=true`、`confirm_upload=true` 和 `save_path`/`path` 后才能提交。`digest.push_payload` 提供可直接推送的 `title`、`summary`、`message`、`target_count`、`shortfall_count`、`target_met`、`approval_queue`、`approval_prompts`、`first_approval_prompt`、`top_safe_candidates`、`execution_plan`、`candidate_control_summary`、`top_item`、`items` 和批次级 `decision_summary`，其中 `decision_summary.policy_risk_counts` 会统计低/中/高策略风险候选数量，`safe_to_submit_count` 会统计可进入 approval queue 的候选数量；`digest.push_items[]` 还会包含 `metadata`、`duplicate_status`、`duplicate_count`、`decision_summary`、`audit_summary`、`policy_risk_summary`、`policy_execution_handoff`、`approval_prompt`、`blockers`、`next_actions`、`can_submit`、`action_label`、`action_endpoint` 和可执行的 `submit_request`；其中 `audit_summary` 会把源站、IMDb/TMDb/豆瓣元数据、目标查重、站点规则/QB 限速、阻塞原因和提交入口汇总成单条候选的首选 AI 审计字段。候选 job 的 `agent_decision` 会在 coverage 不完整时返回 `configure_policy`，避免 agent 直接进入 live 提交。候选还会给出 `agent_workflow`、`submit_request`、`submit_tool=source_url_retorrent_job` 和 `submit_job_endpoint=/v1/jobs/retorrent/from-url`；AI 既可以自己提交 `submit_request`，也可以调用 `/v1/jobs/candidates/{job_id}/submit` 按 `rank` 或 `source_id` 选择候选并只补 `confirm_upload`、`save_path`、QB 分类/标签/限速和素材文件等执行参数，源站和目标站身份会从候选继承，避免误改；提交后的转种 job 会暴露 `candidate_submission_summary` 和 `candidate_submission_handoff`，其中 summary 先给出候选来源、覆盖参数 key、`policy_execution_handoff`、`policy_execution_ready`、`execution_state`、`execution_handoff`、`manual_action`、`closure_action`、`recommended_tool`、`next_step`、`blockers` 和父/子 job endpoint；handoff 再展开继承身份、`submitted_overrides`、`material_options`、`qbit_overrides`、`policy_execution_handoff`、`execution_state`、`execution_handoff` 与完整 `manual_retorrent_handoff`。`execution_state` 会稳定区分 `wait`、`stop_duplicate`、`collect_confirmations`、`configure_policy`、`prepare_materials`、`repair_target_payload`、`repair_qbit`、`resume`、`ready_for_live_upload` 和 `complete`，`execution_handoff` 则给出推荐工具、端点、请求、continue/stop 条件和 blockers；当材料不完整时还会提供 `material_input_template`，把 `metadata_file`、`ptgen_description_file`、`screenshot_files`、`image_host_file` 等推荐输入、dry-run/execute 请求和示例值压到固定路径；同一份 `candidate_submission_execution` 与 `material_input_template` 也会提升到顶层 `agent_decision` 和 `job_handoff`；当状态为 `prepare_materials` 时，`job_handoff.recommended_request` 会直接指向 `material_input_template.dry_run_request`，并同步暴露 `job_handoff.execute_request`，确保 AI 从候选推荐进入 live job 后不用深挖 summary 就能看到站规、限速、做种证据、素材缺口和下一步执行边界。
`/v1/candidates/daily/schedule` 会返回 `schedule_handoff`，即每日 10 条候选的部署入口短路径：没有配置时给出可复制的 `PTCLI_DAILY_CANDIDATE_SCHEDULES` JSON/shell 示例、Docker one-shot/daemon 命令、创建 schedule jobs 的 API 请求、读取顺序和安全条件；默认示例只扫描和推送候选，`confirm_upload=false`，提交单条候选前仍必须人工确认。`/v1/jobs/candidates/daily/schedule` 会额外返回顶层 `schedule_digest`、`candidate_control_summary`、`notification_payload`、`delivery_handoff`、`daily_schedule_gate` 和 `agent_decision`，把多个 schedule job 的 `push_payload`、`push_items`、`approval_queue`、`approval_prompts`、`first_approval_prompt`、`top_safe_candidates`、`top_submit_requests`、`submission_handoff`、状态端点和缺失确认聚合到一个批次结果里，并在批次级汇总 `target_count`、`selected_count`、`ready_count`、`shortfall_count` 与 `target_met`，方便 OpenClaw/Hermes 或外部 cron 直接生成“今日可转种候选”推送；其中顶层 `daily_schedule_gate` 是 schedule 批次的首选控制字段，会用 `action=submit_candidate` / `publish_notification` / `poll_jobs` / `rerun_for_shortfall` / `resolve_blockers` / `inspect_empty` 稳定告诉 AI 下一步，并同步暴露第一条提交请求、推荐工具/端点/请求、10 条目标短缺和 stop 条件；详细推送和交付再看 `delivery_handoff`。`notification_payload` 是后续 webhook/主动推送渠道的稳定载荷，包含 `title`、`summary`、`message`、ready/pending/blocked/目标短缺统计、`approval_queue`、`approval_prompts`、`first_approval_prompt`、`top_safe_candidates`、`top_item`、`submit_items` 和下一步动作。`delivery_handoff` 会进一步把 `publish_ready`、`submission_ready`、目标 10 条是否达标、短缺数量、推送载荷字段、`approval_queue`、`approval_prompts`、提交 handoff 和 stop_when 压到固定路径，作为 AI/cron 判断“现在该推送、继续轮询还是提交候选”的详细字段。CLI 的 `daily-schedule --write-notification` 和常驻 `daily-scheduler` 会把同一份载荷写成 `ptcli-daily-candidates-notification.json` 与 `ptcli-daily-candidates-notification.txt`，方便 AI、本地脚本或 IM/webhook 转发器直接消费；设置 `--notification-webhook-url` 或 `PTCLI_DAILY_CANDIDATE_WEBHOOK_URL` 后还会 POST 同一份 JSON 载荷，并把 `delivery_result` 写入命令输出和 `ptcli-daily-schedule-summary.json`，其中 `file_delivery`、`webhook_delivery`、`agent_handoff`、`blockers` 和 `next_actions` 会明确标出文件/ webhook 是否交付成功、失败后如何重试；同时写入 `delivery_audit`，记录 `payload_fingerprint`、文件/webhook 证据、`mutates_state=false`、`uploads=false`、不接触 tracker/qBittorrent 的安全边界，以及可重试的 `daily-schedule` argv。推送失败不会触发上传或绕过规则，`summary-check` 也会把交付失败作为可审计 blocker 暴露，并原样提升 `delivery_audit` 供 AI 判断是否只需重试交付。`submission_handoff.items[]` 和 `approval_queue.items[]` 优先指向 `/v1/jobs/candidates/{candidate_job_id}/submit`，只要求补 `confirm_upload=true` 和 `save_path`/`path` 等执行参数，源站/目标站身份从候选 job 继承；其中 `approval_prompt.submit_request` 也会切换为该 submit endpoint 的请求模板，保留 `source_url_retorrent_request` 作为审计来源，避免 AI 从 schedule 推送里绕过候选 job 身份继承。`submission_handoff.execution_summary` 会把批次级 `submit_count`、策略 ready 计数、第一条推荐提交请求、`post_submit_flow`、提交后 poll/summary 工具、`job_handoff` 读取路径、材料 dry-run resume 请求来源、重复/规则/确认 stop 条件和逐候选 submit endpoint/request 聚合到固定路径，便于 AI 一次读取就知道提交后该轮询、补素材、配置站点规则还是停止。每个 item 的 `policy_execution` 会把继承的 QB 限速、做种要求、转种/促销规则、rule obligations ready 状态和即将随候选提交继承的 qBittorrent 参数压到固定路径，便于 AI 在提交前复核策略不会被绕过；`after_submit.read_fields` 会把 `job_handoff`、`job_handoff.recommended_request`、`job_handoff.material_input_template` 放到优先读取路径，`after_submit.resume_when` 也会指向 `job_handoff.recommended_tool=resume_job` 且存在 recommended request，材料缺口则通过 `after_submit.material_resume_request=job_handoff.recommended_request when job_handoff.action=prepare_materials` 进入 dry-run resume；`submission_handoff.next_step` 与 `notification_payload.next_step` 会把第一条可提交候选压缩成可直接调用的 `tool`、`endpoint`、`method` 和 `request`。

`digest.daily_candidate_report` 和 `schedule_digest.daily_candidate_report` 是每日候选给 AI 的最短决策路径，会统一暴露 `decision`、`target_count`、`selected_count`、`ready_count`、`safe_to_submit_count`、`ready_shortfall_count`、`target_met`、`submission_ready`、`push_ready`、`first_submit_request`、推荐工具/端点/请求、`continue_when`、`stop_when`、`blockers` 和 `next_actions`。其中 `decision=submit_ready` 只表示已有低风险候选可在用户确认后提交；若 `target_met=false` 或 `ready_shortfall_count>0`，AI 仍应把当天 10 条目标视为未完全达标并继续补候选。
`digest.daily_candidate_batch_report` 是单个每日候选批次的验收短路径，会把今天目标数量、已扫描/已选/ready/safe 计数、短缺、是否可提交、第一条提交请求、必需用户输入、safe source ids、blocked ids、短缺恢复动作和 blockers 压到一个对象；`execution_plan.request_context` 会保留本次源站、目标站、`accept_rules`、`check_dupes` 和 `limit`，`shortfall_recovery.recommended_request` 会给出可直接提交给 `daily_candidates_job` 的补候选请求；只有 `ready=true` 且用户补齐 `confirm_upload=true` 与 `save_path`/`path` 后，AI 才应提交候选转种 job。
`schedule_digest.daily_candidate_batch_report` 则是多个每日候选 schedule 的总验收短路径，会同步出现在 `push_payload`、`notification_payload` 和 `delivery_handoff` 中，便于盒子 cron/OpenClaw/Hermes 一次判断今日整体推送是否 ready、是否有可提交候选、是否仍有目标短缺或待轮询 job；当目标 10 条不足时，`shortfall_recovery.shortfall_items[]` 会列出短缺的 schedule，`shortfall_recovery.recommended_request` 会给出可直接 POST 到 `/v1/jobs/candidates/daily/schedule` 的重跑请求模板。

OpenClaw/Hermes 可直接读取 `/.well-known/ptcli-agent.json` 或 `/v1/openclaw/skill.json`、`/v1/hermes/skill.json`，其中包含 OpenAPI 地址、工具列表、鉴权方式、live 上传安全边界、`skill_contract` 主入口、`agent_instructions` 执行纪律、`tool_selection` 工具选择表、`closure_handoff` 动作契约，以及每个关键工具的 `input_schema`、`response_contract`、`safety`。反向代理或容器内外地址不一致时，设置 `PTCLI_PUBLIC_BASE_URL=https://your-host.example` 让 manifest 输出外部可访问地址；仓库内也提供 `ai/openclaw/ptcli.skill.json` 和 `ai/hermes/ptcli.skill.json` 作为离线模板。

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
  `required_promotions`、`freeleech_required`、`forbidden_title_patterns`、`forbidden_release_groups` 是本地自动化 gate，会在每日候选和策略审计中暴露/执行；无法程序化判断的站规仍必须通过 `rule_review_fingerprint` 和 `accept_rules` 人工确认。HTTP 服务也提供 `/v1/site-policies`，可直接读取 `policy_matrix[].automation`、`policy_matrix[].qbit_limits`、`policy_matrix[].seeding_requirements`、`policy_matrix[].transfer_rules`、`policy_matrix[].policy_coverage`、`policy_matrix[].execution_readiness` 和 `policy_matrix[].policy_profile` 供 AI 或部署脚本审计；其中 `policy_profile.template` 是可复制到 `config["PTCLI"]["SITE_POLICIES"][TRACKER]` 的站点配置模板，`policy_profile.config_audit` 会稳定暴露当前配置是 `flat`、`structured`、`mixed` 还是默认值、哪些字段已显式配置、哪些仍是默认/缺失/占位符、各字段来源和下一步动作，顶层 `config_templates.trackers` / `config_templates.structured_trackers` / `config_templates.config_audits` 会汇总本次涉及站点的模板和审计。`config_update_plan` 会进一步给出可复制的 `flat_patch` / `structured_patch`、逐站 `manual_steps`、`apply_order`、缺失/占位字段 blockers、重跑 `site_policies` 的请求和 `safe_to_auto_apply=false` 安全标记，用于 AI 生成配置修改建议但不自动改写配置。`policy_coverage` 会按源站/目标站角色列出缺失的 fingerprint、限速和做种要求，顶层 `policy_gap_summary` 会按 `source`/`target` 角色和 `rate_limits`、`seeding_requirements`、`rule_review` 分类聚合缺口，`execution_readiness` 则给出每个站点按角色是否可下载/上传/转种以及 blockers；`policy_setup_summary` 会把缺失或仍像模板占位符的 `rule_review_fingerprint` 压缩到 `missing_fingerprints` / `placeholder_fingerprints`，并在 `/v1/readiness/bundle.live_readiness` 中同步暴露；`policy_execution_summary` 会进一步把源站/目标站执行状态、QB 限速计划、做种要求、转种过滤规则、缺失配置和 `next_step` 压缩成 AI 可直接判断的执行摘要；`policy_execution_handoff` 固定暴露 `qbit`、`seeding`、`transfer_rules`、`rule_obligations`、`config.templates`、`continue_when` 和 `stop_when`，让 OpenClaw/Hermes 能判断是继续 live preflight 还是先补站点策略；`policy_handoff.next_step` 会在策略缺失时返回 `edit_config` 请求模板，在策略就绪时指回 `readiness_bundle` 继续 live 前检查。
  `policy_runtime_contract` 是 live job 前后的硬约束短路径：它会列出必须出现在请求里的 `required_request_fields`、从站点策略继承的 `request_defaults`、不可静默移除或放宽的 `protected_fields`、QB/做种/规则 contract，以及完成后必须读取的 `completion_contract.summary_fields` 和 `required_evidence`；同一份 contract 也会嵌入 `policy_enforcement_bundle.runtime_contract`，并在 job status、job summary、`agent_decision`、`policy_execution_report`、`manual_retorrent_handoff`、`candidate_submission_handoff` 和 `candidate_submission_summary` 顶层暴露，便于 AI 复核没有绕过限速、做种和规则 gate。
- 任务式 API 默认把 job 文件写入 `PTCLI_JOB_DIR`，未设置时写入 `TMPDIR/ptcli-jobs`；Docker Compose 默认设置为 `/Upload-Assistant/tmp/ptcli-jobs`。
- `Dockerfile.ptcli` 是 focused CLI 镜像，只安装 `requirements-ptcli.txt` 和 ptcli 需要的系统依赖；旧 `Dockerfile` 保留给 legacy/full UA 入口。
- 默认发布构建使用 `Dockerfile.ptcli`，镜像入口是 `ptcli.py`；release 工作流会额外发布 `*-legacy-webui` 标签给旧 Web UI 镜像。
- 旧 `upload.py` 需要显式覆盖 entrypoint、使用 legacy Dockerfile，或拉取 `*-legacy-webui` 标签才会运行。
- `docker-compose.yml` 默认提供 `ptcli-api` 常驻 HTTP API 服务，使用项目内 `ptcli-net` 网络并带 `/health` healthcheck；一次性 CLI 服务放在 `cli` profile，可用 `docker compose --profile cli run --rm ptcli retorrent ...` 在盒子上执行；legacy Web UI 需要显式 `--profile legacy-webui`。
- `.env.ptcli.example` 是 Docker Compose 的本地部署 env 模板；复制为 `.env` 后至少应设置 `PTCLI_API_TOKEN`、`PTCLI_PUBLIC_BASE_URL`、`PTCLI_MAX_CONCURRENT_JOBS`、`PTCLI_JOB_DIR`、`PTCLI_DOWNLOADS_PATH`，需要每日候选时再设置 `PTCLI_DAILY_CANDIDATE_SCHEDULES`。
- `/v1/deployment/check` 会输出 `mounts`、`qbit`、`daily_candidates`、`deployment_env`、`docker_compose`、`deployment_runbook`、`deployment_handoff`、`seedbox_bootstrap_handoff`、`agent_summary` 和 `agent_handoff`：AI 可以直接判断 config/cookies/tmp/job/downloads 挂载是否就绪、qBittorrent 是否配置、`.env.ptcli.example` 是否包含 API/job/daily 所需 env 键、`PTCLI_DAILY_CANDIDATE_SCHEDULES` 是否已提供每日候选计划，以及 `docker-compose.yml` 是否包含可用的 `ptcli-api` 常驻服务（serve 命令、localhost 端口、healthcheck、API token env、host-gateway、downloads/config/cookies/tmp 挂载）和 `ptcli-daily-scheduler` 常驻 daily profile 服务或一次性 `ptcli-daily-schedule` 服务；`deployment_runbook` 会把准备 `.env`、`docker compose up -d --build ptcli-api`、检查 `/health`/`/openapi.json`/`/v1/tools`/agent manifest、调用 `/v1/deployment/check`、调用 `/v1/readiness/bundle` 和第一单 live validation 的命令/API 顺序、continue/stop 条件压成固定 steps；`deployment_handoff` 会把 API base URL、`/health`、`/openapi.json`、`/v1/tools`、agent manifest、token 建议、env 模板、手动一键查重提交入口和每日候选入口压缩到固定路径；`seedbox_bootstrap_handoff` 是只读盒子初始化交接对象，会列出缺失挂载、可执行的 `mkdir` 建议、config/env/compose 准备项、qBittorrent 提示、启动 API/每日候选服务命令、验证请求和下一步推荐工具；`agent_summary` 也会提供 `compose_deployable`、`manual_workflow_ready`、`daily_workflow_ready`、`api_local_only`、`api_auth_recommended`、`env_template_ready` 等短路径；`agent_handoff` 会给出手动转种和每日候选的推荐工具、端点、最小请求模板、必需确认和阻塞原因。`/v1/readiness/bundle` 会进一步把 deployment、site policies、daily schedule、非 live `live_verification` 凭据/图床/素材链路清单、doctor 命令模板和 `source_url_retorrent_job` 请求模板汇总到 `live_readiness`/`agent_decision`，用于 AI 在 live 前一次性判断是否还缺 cookie、MTEAM API key、qBittorrent 配置、图床、规则确认、目标站点、源站链接或盒子配置；`live_test_handoff.preflight_checklist` 会逐项暴露 deployment/site policy/credentials/materials/confirmations/doctor/manual job 是否 ready，`live_test_handoff.execution_plan` 会给出修复预检、运行 `ptcli doctor`、通过后提交 `source_url_check_and_submit` 的顺序；`seedbox_live_validation_handoff` 会把 compose API、qBittorrent、站点策略、凭据/素材、doctor 请求和 check-and-submit 请求压缩到同一个只读对象，作为 OpenClaw/Hermes 在盒子上尝试第一单 live 验证前的首选字段；顶层 `live_validation_summary` 和 handoff 内的同名字段则是最短路径，会直接暴露 `status`、`can_run_doctor`、`can_submit_after_doctor`、`first_blocker`、`doctor_request`、`check_and_submit_request`、提交后读取字段、轮询/收尾工具和 required order；顶层 `seedbox_live_validation_report` 是盒子首单 live 验证的报告式入口，会把当前 step、doctor/summary-check、check-and-submit、提交后 poll/resume/finish、最终证据字段、runbook、组件计数、complete/stop 条件和 blockers 聚合到固定路径；顶层 `live_validation_repair_plan` 会把 deployment、compose API、qBittorrent、site policy、credentials/materials、confirmations、doctor command、manual job request 和 preflight checklist 的缺口按类别列出 `blocked_categories`、`categories[].blockers`、`categories[].continue_when`、`next_step` 和推荐端点/请求，AI 应先修到 `live_validation_repair_plan.ready=true` 再进入 doctor；其中 `validation_report` 会按 deployment、compose API、qBittorrent、site policy、credentials/materials、confirmations、doctor command、manual job request 和 preflight checklist 输出 ready/blocked 组件、计数、first_blocker 和 next_step，`validation_plan` 固定给出 `preflight -> doctor -> check_and_submit -> poll_job -> recover_or_finish` 五步、每步 `read` 字段、continue/stop 条件和 `/v1/summary/check` 入口，`post_submit_handoff` 固定告诉 AI 提交后如何轮询 `recovery_handoff`、何时 dry-run/execute `resume_job`、何时读取最终 summary，`evidence_contract` 列出 closure/qBittorrent/materials 中必须回报的 hash、路径、做种和限速证据；`live_test_handoff.next_step` 会在就绪时给出可执行 `ptcli doctor` argv，未就绪时指向 deployment/site policy/readiness 修复路径。doctor 写出 `ptcli-doctor-summary.json` 后，优先调用 `/v1/summary/check` 读取 `live_validation_result`，也可用 CLI `summary-check --json` 兜底；该对象会把 `ready`、`status=safe_to_submit`、`can_submit_check_and_submit`、`check_and_submit_request`、`post_submit_read`、`final_evidence_read`、blockers 和最终完成条件压缩到固定路径，同时保留原始 `doctor_result_handoff` 供审计。每日候选或 compose 定时服务未配置只作为 warning，不阻塞手动转种 API。
- `/v1/deployment/check.runtime_tools` 会报告 `ffmpeg`、`mediainfo`、`ffprobe` 和可选 BDInfo 二进制状态；focused `Dockerfile.ptcli` 默认安装 `ffmpeg` 和 `mediainfo`。当 `/v1/readiness/bundle` 请求里包含 `generate_mediainfo` 或 `generate_screenshots` 时，缺少对应二进制会在 `live_verification.materials.missing_runtime_tools` 中阻塞暴露，避免 AI 到截图/MediaInfo 步骤才发现容器不可用。
- `/v1/summary/check.live_submission_package` 是 doctor summary 通过后的只读提交包：它会把 `source_url_check_and_submit` 的 request/curl、提交后 `get_job_status` 轮询、`resume_job` 预览/执行条件、`get_job_summary` 收尾读取字段和 `live_user_report` 最终报告条件固定到一个对象；提交 request 会携带 `live_validation_submission` 来源证明，提交后的 job status/summary/list 会暴露 `live_validation_followup`，让 AI 直接判断下一步是 poll、resume 还是读取最终报告；该字段不会执行上传，AI 仍必须单独调用提交 endpoint，并在 `live_user_report.report_allowed=true` 与 `seedbox_live_validation_completion_report.ready_for_user_report=true` 后才能报告首单 live 验证完成。
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

## 每日候选可下载性证据

每日候选的每个 candidate、`digest.push_items[]`、`publish_card.downloadability` 和 `audit_summary.downloadability` 都会暴露同一份 `downloadability_summary`。AI 判断“可转种候选”时应同时检查 `downloadability_summary.ready=true`、`downloadability_summary.downloadable=true`、`downloadability_summary.source_pull.request`、目标站 `duplicate_check.exists=false`、策略 gate ready，以及用户确认状态。

`downloadability_summary` 会记录源站详情 URL、源站拉种 adapter、候选发现 adapter、cookie 路径/状态、站点策略是否允许自动下载/转种、可调用的 `source_url_retorrent_job` 请求和等价的 `source-download` CLI 参数。若 `downloadability_summary.blockers` 非空，或真实盒子环境里 `cookie.status=missing`，应先修复源站登录、cookie 挂载或站点策略，不应把该候选提交为 live 转种任务。

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
