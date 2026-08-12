# Upload Assistant v2（Go + PostgreSQL）

v2 是当前重构目标：Docker Compose 启动 Go 常驻服务和 PostgreSQL，每个转种步骤、外部调用、阻塞、恢复与证据都持久化并可在 Web 控制台审计。

## 本地启动

```bash
cp .env.example .env
# 先把 `openssl rand -hex 32` 的结果填入 .env 的 UA_POSTGRES_PASSWORD
docker compose up -d --build
docker compose exec upload-assistant upload-assistant admin bootstrap --username admin
```

第二条命令会交互式读取管理员密码，并且只显示一次初始 API token。将 token 保存到密码管理器，不要写入仓库、URL 或命令参数。

若遗失初始 token，可由仍控制服务主机的管理员显式签发恢复 token：

```bash
docker compose exec upload-assistant upload-assistant admin token issue \
  --username admin --name web-recovery --confirm
```

签发动作写入全局审计，新 token 只显示一次，且必须由 Web 验证通过后才会保存在当前标签页会话中。

默认入口：

- Web 控制台：`http://127.0.0.1:8080/`
- readiness：`http://127.0.0.1:8080/health/ready`
- OpenAPI：`http://127.0.0.1:8080/openapi.json`
- AI 工具：`http://127.0.0.1:8080/api/v2/tools`（Bearer token）
- Agent 发现：`http://127.0.0.1:8080/.well-known/upload-assistant.json`
- AgentSkills 文档：`http://127.0.0.1:8080/.well-known/upload-assistant/SKILL.md`

Compose 默认只把 HTTP 端口绑定到 `127.0.0.1`。远程访问应通过具备 TLS 和访问控制的隧道或反向代理，不能把服务无认证暴露到公网。

## 原生 Go CLI

`upload-assistant cli` 是新 API 的本地客户端，默认输出服务端结构化 JSON，不依赖 Python。token 不接受命令行参数，只从 `UA_API_TOKEN`、`UA_API_TOKEN_FILE`、`--token-file` 或 TTY 隐式输入读取。非 loopback 的明文 HTTP 默认拒绝，远程服务应使用 HTTPS；确有受控网络需求时才显式传 `--allow-insecure-http`。

```bash
export UA_API_URL=http://127.0.0.1:8080
export UA_API_TOKEN_FILE=/run/secrets/upload-assistant-api-token

upload-assistant cli health
upload-assistant cli jobs list --limit 20
upload-assistant cli jobs summary <job-id>
upload-assistant cli candidates list --source U2 --target MTEAM
upload-assistant cli rules import --file data/site-rules/U2.md
# 或将宿主机文件安全地送入 Compose 容器 stdin
docker compose exec -T upload-assistant upload-assistant cli rules import --file - < data/site-rules/U2.md
upload-assistant cli rules list U2
upload-assistant cli rules approve <revision-id> --fingerprint <sha256> --comment '已人工核对' --confirm
upload-assistant cli rules activate <revision-id> --confirm
upload-assistant cli integrations list downloaders
upload-assistant cli audit list --resource-type downloader --resource-id box
upload-assistant cli readiness live --source U2 --target MTEAM --downloader box --image-host imgbb --screenshot-profile default --tmdb-provider tmdb-main --ptgen-provider ptgen-main
upload-assistant cli shell
```

创建任务默认使用 `execution_mode=step`，适合逐步审计。未指定 idempotency key 时 CLI 会为单次新意图生成随机 key；自动化重试应显式使用稳定的 `--idempotency-key`。

```bash
upload-assistant cli retorrent create \
  --source-url 'https://u2.dmhy.org/details.php?id=60635' \
  --target MTEAM \
  --downloader box --save-path /downloads \
  --screenshot-profile default --image-host imgbb \
  --tmdb-provider tmdb-main --ptgen-provider ptgen-main \
  --idempotency-key operator-request-20260808-01

upload-assistant cli jobs resume <job-id> \
  --accept-rule U2=<reviewed-fingerprint> \
  --obligation U2:<obligation-id>=<human-evidence>
```

live 上传只能用显式 `--confirm-upload`，且同一次命令必须重新提交源站和目标站的精确规则 fingerprint；CLI 只是减少误操作，服务端仍会重新验证规则、人工 obligation、查重、不可变上传包与做种 gate。

## 站点规则 Markdown

本地 `data/site-rules/*.md` 使用 Go v2 YAML front matter，并保留人工审查所依据的规则正文；除说明文件外，它们默认被 Git 忽略，也不会由 Compose 自动导入或激活。未取得或未逐条核对完整规则时必须保持 `source.complete=false`、`review.status=draft`、`auto_pull=false` 和 `auto_upload=false`，不能审批或用于 live gate。

Web「站点规则」以多来源编译为主流程。操作者先通过 `GET/PUT /api/v2/sites/{site_code}/rule-sources` 保存 1–20 个有序、无凭据参数的精确 HTTPS 规则/FAQ 地址，以及稳定 evidence ID、范围、完整性和逐来源认证方式。`auth_mode=none` 不发送任何站点凭据；`auth_mode=site_cookie` 才要求加密 Cookie 与精确 Cookie host 确认。MTEAM 的运行时 API Key 不参与规则页采集。站点还必须配置启用的正数普通访问策略。显式创建 `/rule-collection-runs` 后，后台按同一普通最小间隔、小时配额、429 cooldown 和并发门禁顺序抓取；禁止重定向、私网目标和环境代理，只保存有界规范化文本、认证方式、行号 evidence 与完整 hash。状态可轮询或通过 run 的 `/stream` SSE 读取。该人工启动的窄化通道只允许访问已确认来源，不授权 adapter 操作或定时采集。

采集结果由已启用 `rule_analysis` 的 Provider 合并为不可变草稿，并显式保留每个来源的 URL、范围、抓取时间、hash 与冲突。人工只审核 `upload_limit`、`download_limit`、`naming` 三个硬门禁：上传必须是单种限速，默认以站点声明值减 20 MB/s 作为可调有效值；下载必须是单种限速并使用声明原值；分类命名 profile 通过验证过的 category/resource class 选择，按确定性 token 自动生成标题后同时校验发布标题和内容根名称。盒子专用限速只在下载器被人工标记为 `seedbox` 时应用。来源冲突、未知有效值或非单种 scope 都阻止审批。修正会派生新 revision 并重置三个确认；全部确认后仍需按精确 fingerprint 先 `/approve`、再独立 `/activate`。其他 AI 结论保存在 `advisories` 中并在转种前显示。

在 Web 粘贴整份文档或调用 `POST /api/v2/site-rules/import`、`/analyze` 仍作为离线导入和兼容重分析路径，不访问 Tracker，也不会自动导入、审批、激活或授权任务。`upload-assistant.site-rule.v2` 的 `access` 章节仍必须明确服务/搜索访问许可和规则侧限频；v1 规则不能授权普通业务访问。

兼容分析接口最多接受 8 MiB UTF-8 原文，也可只提交 `source_revision_id`，由服务端读取 checksum 已验证的不可变 Markdown 并忽略客户端来源字段。大文本按证据行分块、顺序提取并在本地保守合并，所有分块和语法修复共享一次 Provider 总超时；跨块冲突不能生成可执行门禁。Web 的 `/api/v2/site-rules/analyze/stream` 立即发送 SSE 进度和心跳；每次点击生成 `Idempotency-Key`，同一用户、同一键和同一输入只共享一次推理或短期结果。浏览器链路中断后使用同一键轮询 `/result`，不会启动第二次调用。Provider 请求按其 `streaming_enabled` 设置，瞬态 SSE/502–524 只可在同一总超时预算内同协议重试一次，不能静默降级为整包输出。成功 SSE 逐事件增量解析并以常量内存累计总字节数和 SHA-256，不保存原始推理事件；规范化最终输出和成功整包响应最多 8 MiB，失败整包响应及单个聚合 SSE event 最多 1 MiB。只有正常 finish/completed 的结果可使用，截断或未完成输出不会进入 JSON 修补。远程 Provider 会在发送前明确显示数据边界。

活动 v2 规则还不足以触发联网。每个站点都必须通过 `GET/PUT /api/v2/sites/{site_code}/access-policy` 或 CLI `rules access`/`rules access-set` 配置人工访问策略。有效策略对规则值和人工值取更严格限制，分别计算 general/search 最小间隔、小时配额、站点并发与 429 cooldown；缺少任一门禁、规则禁止/不确定、配额耗尽或 cooldown 时，adapter 在发请求前拒绝或把任务持久延后。每日候选评估会保存逐条 checkpoint，延后后从未完成条目继续，不会从第一条重复扫描。

## 真实环境就绪交接

在进行任何站点、下载器、图床或元数据提供方联网探测前，先调用 `GET /api/v2/readiness/live`，或使用 Web 顶部「就绪检查」和 CLI `readiness live`。当前完整参考路径限定为 U2/CHD → MTEAM。该只读检查仅验证：已审批激活的规则 fingerprint 与阻塞 obligations、站点凭据字段是否存在、源/目标下载器、图床、截图策略、显式 TMDb/PTGen provider、`/downloads` 挂载以及 MediaInfo/BDInfo/FFmpeg/FFprobe/mkbrr 是否可用。

普通视频文件使用 MediaInfo JSON；检测到 `BDMV/STREAM` 时改为对原盘根目录运行非交互 BDInfo 整盘扫描，并把原始 UTF-8 文本作为独立 `bdinfo` artifact 绑定到 MTEAM 上传包。每个 attempt 使用独立临时报告目录，只接受唯一、受限大小的普通文本文件。Docker 镜像从固定 commit 构建自包含 BDInfo，并校验源归档 SHA-256、保留 LGPL 许可证。`VIDEO_TS` 不会被 MediaInfo 或 BDInfo 冒充处理，而会明确返回 `dvdinfo_adapter_required`。

就绪报告固定返回 `external_calls_performed=false`、`live_upload_authorized=false`，其 `resume_state.confirm_upload` 也固定为 `false`。即使 `configuration_ready=true`，也只表示本地配置可以进入受控外部验证，不代表凭据真实有效、站点无重复、资源可下载或用户同意上传。agent 应展示报告中的 blocker、next_actions、精确规则 fingerprint 与 obligation IDs，再分别取得联网探测授权；最终 live 上传仍必须在不可变上传包和最终查重可审阅后取得显式 `accept_rules` 与 `confirm_upload`。

## 运维中心、诊断与恢复

Web「运维中心」把总览、异常事件、运行日志、AI 诊断、备份和 API Token 放在同一个入口。运行日志与全局 `audit_events`、任务 `job_events` hash 链是三种不同证据：`/api/v2/operational-logs` 用于按时间、级别、component、关键词、error/request/trace/job/attempt ID 检索请求和 Worker 运行情况；`/stream` 使用可续传的 SSE ID，`/export` 最多导出 10,000 条 NDJSON。成功健康检查不会写入数据库。日志进入有界异步队列，数据库异常或慢客户端不会阻塞任务，丢弃总数显示在运维总览。

Incident 只聚合可行动异常：任务步骤失败、30 分钟内重复的非人工 blocker、连续两次集成健康探测失败、容量越界以及备份失败。规则/上传确认、人工 obligation、正常查重、等待和计划内暂停不会创建 incident。确认与解决写入全局审计，但不修改任务事件链。

OpenAI-compatible provider 分为 local/remote 数据等级，可选择 Chat Completions 或 Responses API，并分别启用异常诊断或站点规则分析用途。根 Base URL 保存时规范化到 `/v1`，显式路径保持不变。推理强度接受 `default/none/minimal/low/medium/high/xhigh/max`；模型目录只展示 Provider 明确报告的能力，不猜测未报告值。保存配置不会联网且会把连接状态重置为“未测试”。显式 `stage=catalog` probe 只调用 `/models` 并可进入“目录可达、推理未验证”；独立 `stage=inference` probe 才以所选协议和模型执行一次 token 有界推理并进入 `ready`。两阶段都会持久化不含正文的路径、HTTP/content type、响应结构/hash、trace、延迟和稳定错误码。remote 只接受 HTTPS，local 只接受 loopback、私网或 Compose 服务名，云元数据地址和重定向均被阻断。每次诊断固定生成最大 64 KiB 的不可变证据快照和 SHA-256；incident 快照会沿 `audit_event_id` 与 trace 收集失败审计和运行日志，remote 仍永久去掉标题、文件名、路径、描述与请求/响应正文。模型请求不包含 tools/functions，外部证据由不可信数据边界包裹，结果必须通过固定 schema 和 evidence ref 校验。原始模型响应不落库，模型建议也不能直接执行 `/attention` 之外的动作。自动诊断只有在管理员配置 incident kind/provider allowlist 后启用；remote provider 还必须有独立 outbound consent。

API Token 默认 30 天、最长 365 天，明文仅在创建响应中出现一次；调用者只能签发自己 role 与 token scope 的子集。列表不返回 hash 或明文，撤销立即使鉴权失败。本地主机 `admin token issue` 仍是失去 Web token 后的审计恢复入口。

备份使用独立 `/backups` 挂载和 age X25519。服务只保存 public recipient，生成的 private identity 只显示一次并应离线保管。默认计划为每天 03:30，保留最近 7 份成功 bundle；活跃写任务会使 run 进入 `deferred`，不会被强制终止。bundle 包含 PostgreSQL logical dump、master key、规则文档以及 artifact/summary 元数据和受控文件，不包含 `/downloads`、legacy、tmp 或旧备份。

恢复没有 Web/API/Agent tool 入口。先停服务，再从受限 identity 文件显式执行：

```bash
docker compose stop upload-assistant
docker compose run --rm --no-deps upload-assistant \
  admin backup restore --bundle /backups/<bundle>.age \
  --identity /backups/<offline-identity-file> --confirm
docker compose start upload-assistant
```

CLI 会先校验独立 receipt 中的 bundle SHA/大小/版本，再校验 age、manifest 和每个内部文件 hash；数据库先恢复到临时库并执行迁移/完整性检查，文件先进入 staging。全部通过后才切换数据库和 master key/规则/artifact，原数据库和文件保存在 `/data/restore-rollbacks/` 作为可回滚状态。服务锁仍被持有时恢复会被拒绝。

## 审计读取

- `GET /api/v2/jobs/{job_id}/attempts` 按步骤与尝试号稳定分页，展示每次执行的状态、时间、adapter、稳定错误码及脱敏输出；原始输入快照只返回 SHA-256，不会经 API 泄漏。
- `GET /api/v2/jobs/{job_id}/events` 是单个任务的 append-only hash 链；配合 attempts、steps、artifacts 与 summary 可验证每次重试、租约恢复、转种边界和证据文件。
- `downloader_add`、`image_upload`、`target_upload` 与 `target_inject` 的远端写入执行中不允许中途 pause/cancel；需要停机时应使用逐步模式或 `stop_after_step` 停在边界。若 Worker 在这些步骤租约过期，任务不会自动重跑，而是以原 attempt 进入 unknown-outcome reconciliation。
- `POST /api/v2/jobs/{job_id}/replay` 只允许从 blocked/failed/cancelled 任务创建带 `replay_of_job_id` 的全新任务，默认逐步模式；运行中、暂停、已完成、待对账、外部写入结果未知、正在恢复，或已经完成任一下载器/目标站/图床写入的任务会被硬拒绝。新任务会清除旧规则接受、人工 obligation、resume_state 和 `confirm_upload`，不能用回放绕过 gate，也不能重复任何已记录的远端写入。
- 未知远端写入、下载器 partial-add 与下载器结果审计失败会自动在 `resume_state.reconciliation` 写入 blocker 与当前 attempt 模板。普通 resume、CLI `jobs retry` 和 replay 均返回稳定的 `reconciliation_required`/`replay_not_allowed`；只有与当前 attempt 精确绑定、由操作者确认、包含 RFC3339 观察时间与人工证据 SHA-256 的允许决定才能续跑。目标上传的 `verified_not_applied` 会重新查重并再次要求显式 `confirm_upload`；目标 `verified_uploaded` 必须绑定目标种子 ID 与 submitted torrent SHA-256，Worker 只读确认后生成 recovery 回执。图床在网络结果未知、配置竞态或本地 receipt 落库失败时同样停止；若已有可信返回证据，图床 `verified_uploaded` 必须绑定服务器生成的 pending evidence SHA-256，Worker 只补写本地回执。下载器 Add 在远端调用前先持久化 intent；partial 或 unknown 结果必须以 `verified_remote_state` 绑定模板中的精确 submitted infohash，Worker 只读核对远端身份、大小、保存路径、标签、暂停状态与限速后生成证据。以上恢复路径都绝不二次写入。事件链只保存决定、证据 hash 与 reconciliation hash，不复制可能含凭据的原始材料。
- 独立 `POST /api/v2/downloaders/{name}/torrents/{hash}/limits` 同样先持久化 `torrent.set_limits_intent`，再执行远端写入、读回两个有效限速并记录结果审计。部分写入、读回不一致或结果审计失败统一返回 `downloader_limits_outcome_unknown`、原始期望值和可用的脱敏 observation；调用方必须先用只读 torrent inspect 对账，不能自动重发限速写入。
- `GET /api/v2/audit-events` 是配置变更、远程下载器/图床动作、通知、迁移和 Sonarr/Radarr 等全局动作的脱敏审计，可按 `actor_type`、`action`、`resource_type`、`resource_id` 精确过滤，并使用不透明 cursor 稳定翻页。该全局日志不会被描述为任务 hash 链。
- Web 顶部「审计」和 CLI `audit list` 读取同一接口。API 返回前会递归脱敏 credential、cookie、token、passkey、announce URL 等敏感字段；需要证明某个任务完整性时仍应回到该任务的事件链和 artifact SHA-256。

任务页优先读取 `GET /api/v2/jobs/{job_id}/attention`：它只投影一个当前最终问题以及可选方案。只有 `executable=true` 的 retry/safe repair 才能通过 `/actions` 执行，且必须绑定读取时的 status、step 和 blocker；safe repair 还要求显式 `confirmed=true`。远程结果未知、站规接受、人工 obligation、重复、live 上传确认和做种要求只给人工处理入口，永远不会成为可自动同意的动作。

进入 `confirm_upload_required` 后，使用 `GET /api/v2/jobs/{job_id}/upload-preview` 查看当前包的字段、完整描述、MediaInfo/BDInfo、截图证据、推导决策与警告。修改通过 `/upload-preview/revisions` 提交当前 package SHA-256 和完整审核字段：旧 artifact/attempt 保留，新版本重新生成，所有下游步骤失效，`confirm_upload` 强制重置为 false，必须再次审核后才可确认上传。

旧版配置迁移使用独立的只读挂载。默认读取宿主机 `./data`，可在启动前设置：

```bash
export UA_LEGACY_DATA_HOST_PATH=/absolute/path/to/old-upload-assistant/data
docker compose up -d --build
```

容器内固定挂载为 `/legacy:ro`。服务只读取 `/legacy/config.py` 和 allowlist 内的 `/legacy/cookies/{SITE}.txt`，不会执行 Python，也不接受 API 传入任意文件路径。

## OpenClaw / Hermes

OpenClaw 可直接发现项目内的 `.agents/skills/upload-assistant/SKILL.md`。Hermes 可从部署后的 `/.well-known/upload-assistant/SKILL.md` URL 安装技能。两者都应先读取 OpenAPI 和鉴权后的工具目录，再按照技能中的硬门禁操作。

技能不会代表用户接受站规或确认 live 上传。`accept_rules` 必须绑定当前生效规则指纹和人工 obligation；`confirm_upload` 必须在最终查重和不可变上传包可审阅后由用户显式提供。

## 每日候选

每日候选也是 PostgreSQL 持久任务，不是同步抓取请求：

- `POST /api/v2/candidates/daily` 创建 `daily_candidates` job；`candidate_rules`、`candidate_scan`、`candidate_evaluate`、`candidate_rank`、`candidate_summary` 五步均可审计和恢复。
- `GET /api/v2/candidates/daily` 读取按日期持久化的候选、排序、推荐理由、风险、阻塞、metadata 和目标站查重证据。
- `POST /api/v2/candidates/{candidate_id}/retorrent-job` 只创建安全的未确认转种 job；它不会推断 `accept_rules`，并固定以 `confirm_upload=false` 开始。
- `GET/POST /api/v2/schedules/daily-candidates` 与 `PATCH /api/v2/schedules/daily-candidates/{schedule_id}` 管理 PostgreSQL 持久的每日扫描计划；`GET .../{schedule_id}/runs` 可审计每次触发、租约、重试次数和关联 job。当前 cron 明确限定为 `分 时 * * *`，避免接受服务无法可靠解释的表达式。
- `GET /api/v2/notifications` 读取脱敏的本地/外部投递状态、尝试次数、payload SHA-256 和远端回执 hash。通知渠道支持 Telegram Bot、企业微信群机器人、飞书群机器人和兼容的 Discord webhook；每个渠道可订阅任务创建、完成、失败、阻塞、限频延后、暂停/继续/取消、发布包重生成与人工对账等高层事件。任务事件与通知在同一数据库事务中进入持久 outbox。调度、排名和通知均不代表用户批准候选，也不会自动创建正式转种任务或上传种子。
- `GET/PUT /api/v2/notification-channels` 独立管理渠道和加密凭据。每日调度还可在 `config.notification_channels` 显式列出已启用渠道。

外部投递由 PostgreSQL 队列和独立 Worker 执行；外部 POST 前先提交 `notification.delivery_started` 意图审计，并使用租约、对明确拒绝最多 8 次指数退避及渠道专用成功回执校验。网络无响应、5xx、成功响应不可解析、本地回执事务失败或发送租约过期均进入 `outcome_unknown`，自动投递器不会接管重发；只有人工对账可决定后续。消息只含本地任务 ID、状态、当前环节和简短问题，不包含凭据或规则原文。`sent` 只表示通知渠道确认接收，不表示候选获批或种子已上传。

配置页的“发送测试消息”需要二次确认，使用独立的持久通知记录并固定只尝试一次；明确失败不会进入普通通知的自动退避重试，未知结果仍通过原通知记录对账。

## Sonarr / Radarr

- `GET/PUT /api/v2/media-managers` 独立管理多个 Sonarr/Radarr v3 实例；API key 加密且只回显字段名。
- `POST /api/v2/media-managers/{name}/probe` 是显式、只读的 `/api/v3/system/status` 探测，保存版本、配置 hash 和响应 hash。
- `POST /api/v2/media-managers/{name}/lookup` 复刻 legacy 的只读补元数据语义：Sonarr 接受 TVDB ID 或 path+title，Radarr 接受 TMDb ID 或精确 path。审计只保存 query/response SHA-256 与规范化 ID，不保存 API key、原始响应或本地路径。
- HTTP 重定向被禁止，响应体有大小上限，失败只持久化稳定错误码。当前它们是显式 metadata helper，不会向 Sonarr/Radarr 添加、删除、重命名或刷新媒体。

## TMDb / PTGen 元数据 provider

- `GET/PUT /api/v2/metadata-providers` 独立管理 TMDb/PTGen endpoint；key 加密保存且只回显字段名。TMDb 启用时必须提供 key，PTGen key 可选，但 endpoint 始终必须显式配置，不会回退到公共服务。
- 保存 provider 只写入加密配置并保持“未测试”。配置页的“测试查询”会使用稳定公开引用执行一次与生产相同的只读查询契约，返回和审计仅保留调用元数据、配置/query/response hash，不向 Web 返回测试资源标题或简介。
- `POST /api/v2/metadata-providers/{name}/resolve` 是显式 external-read。TMDb 按官方 v3 `find/{imdb}` 或 `movie|tv/{id}/external_ids` 契约规范化 IMDb/TMDb ID，并阻断跨类型歧义或 ID 冲突；PTGen 兼容 legacy 的 IMDb→豆瓣二段查询和直接豆瓣 subject 查询。
- 所有请求禁重定向、限时、限响应大小。全局审计只保存配置、查询、响应和 PTGen 描述 SHA-256、规范化 ID 与字节数，不保存 API key、原始响应或简介。配置成功不是联网许可，`matched=false` 也不是材料已完成。
- 新建 retorrent 工作流版本 2 把 `metadata_tmdb` 与 `metadata_ptgen` 作为两个独立、可暂停和可恢复的必需步骤。任务只有在输入或 resume state 显式选择 provider 时才调用远端；也可由人工在对应 resume 字段提交已复核 ID/简介。每步把 provider 配置 fingerprint、查询/响应 hash 和不可变 artifact 绑定到事件链。
- MTEAM 打包硬性要求 IMDb、带 movie/tv 类型的 TMDb、豆瓣 subject 和非空 PTGen/豆瓣简介。原始简介只保存在受控 artifact；步骤响应与 summary 只暴露 hash 和字节数，打包前重新校验 artifact SHA-256，并把 BBCode 标记安全文本化，避免外部简介注入上传描述结构。

## 图床

- ImgBB、PTPimg、Imgbox 与 Pixhost 都通过同一图床配置和工作流回执运行。ImgBB/PTPimg 需要加密保存的 `api_key`；Imgbox/Pixhost 不接收凭据。
- 配置页的“测试图床”在二次确认后上传一张内置 100×100 PNG，以兼容图床的常规缩略图处理；远端可能保留该测试图。测试沿用正式上传的 intent、回执校验和未知结果门禁，不会因失败或超时自动重试。
- PTGen 测试会区分 DNS、TLS、连接、超时、认证、限流、无效响应和缺少简介等失败，且错误中不保留可能含 `key` 的请求 URL。若运行环境不能访问 `workers.dev`，应给 Worker 绑定可达的自定义域名，并把 Provider 地址配置为该域名的 `/api`。
- Imgbox 使用同一隔离 Cookie 会话获取 CSRF 与临时上传令牌，接受 PNG/JPEG；Pixhost 使用无需 Key 的 API v2，接受 PNG/JPEG/WebP。两者单图上限为 10 MiB，格式或大小不兼容会在持久化写入意图和联网前阻断。
- 上传前必须持久化绑定源图 SHA-256 与配置 fingerprint 的 intent；网络结果不明、成功响应无效或本地回执失败时保持 reconciliation blocker，不能自动换图床或盲目重传。所有返回链接必须是 HTTPS 并匹配对应图床的域名和 URL 结构。

## 远程下载器

- `GET /api/v2/adapters` 是跨站点、下载器、图床、元数据、媒体管理器、通知与本地媒体工具的统一能力契约。响应固定排序并带 `catalog_sha256`；所有 callable adapter 必须声明 operations、credential 字段、safety gates 和 constraints，尚未实现的站点只显示为 `runtime_supported=false` 且没有 operation。契约变更必须人工更新 golden fingerprint，AI 不得根据名称猜测支持。
- `GET /api/v2/downloader-adapters` 是运行时能力的权威目录。调用方应先检查 `runtime_supported` 和逐项 `operations`，不能只依据 adapter 名称推断能力。
- Web「下载器」工作台通过 `GET /api/v2/downloaders/{name}/snapshot` 显式读取实时任务数据，支持状态/关键词筛选和最多 200 条的服务端分页；同一实例的并发刷新会合并并使用 3 秒短缓存。轮询响应包含任务名称、进度、速度、分享率、分类与标签，但排除保存路径、Tracker URL、announce 与凭据，不会暂停、删除、校验或修改任务。文件列表只在人工打开单种详情时读取，HTTP 响应最多返回 500 条。
- qBittorrent、Transmission、rTorrent 与 Deluge Web 已支持独立 endpoint、加密凭据、远程路径映射、探测、加种、状态和文件查询、单种限速与等待完成；工作流证据查询与远端写入会记录脱敏审计，短周期仪表盘轮询不会制造逐次审计噪声。
- Transmission 同时兼容 4.0 及以前的旧 RPC 和 4.1 起的 JSON-RPC 2.0，并按官方 `X-Transmission-Rpc-Version` 握手自动选择协议。Transmission 不支持 `skip_checking`，请求该选项会明确失败，绝不静默忽略。加种后的限速通过独立 `torrent-set` 应用；若加种成功但限速失败，会记录 `torrent.add_partial` 审计并要求按 hash 对账后重试。
- rTorrent 通过原生 Go HTTP XML-RPC 运行时调用 `load.raw` 后，以 hash 逐项设置目录、`custom1` 标签、命名 throttle 并启动；不拼接命令字符串，也不伪造 fast-resume。它只接受含 v1 infohash 的种子，且不支持 `skip_checking`。分类和标签会稳定扁平化到一个 `custom1` 值。命名 throttle 必须读回非零且不超过请求值；若全局 throttle 令其失效，步骤会以 blocker 停下。做种时长按当前连续活跃窗口保守计算，可能少算历史会话，但不会把暂停时间冒充做种时间。
- Deluge 通过原生 Go HTTP 客户端调用 Web JSON-RPC `/json`，使用独立 Web 密码登录，并强制确认 Web 已连接 daemon；它不接受旧式原生 daemon RPC 用户名/密码。`core.add_torrent_file` 后必须按 v1 infohash 读回状态，单种限速通过 `core.set_torrent_options` 设置并读回验证。核心 API 不承诺 Label 插件，因此能力目录明确返回 `category=false`、`tags=false`、`skip_checking=false`；这些字段必须在任何远端写入前阻断，不能静默丢弃。完整工作流需显式提交 `apply_labels=false`，此选择会进入步骤快照和注入 receipt；默认仍应用标签，不会因为选了 Deluge 就暗中省略。目标注入与做种验证绑定实际 adapter、配置 hash、infohash、路径、文件清单和限速，不再硬编码 qBittorrent。旧 Deluge daemon RPC 地址无法无歧义转换成 Web endpoint，迁移只返回 `legacy_deluge_web_endpoint_required` warning，不保存旧明文凭据。

## 旧配置安全迁移

迁移采用 preview → fingerprint 确认 → import：

- `GET /api/v2/migrations/legacy/preview` 使用非执行字面量解析器读取旧配置，只返回主密钥 HMAC fingerprint、文件大小、资源名称、credential 字段名、禁用原因和 warnings。它不会公开可用于猜测弱密码的普通内容 hash。
- `POST /api/v2/migrations/legacy` 必须同时提交刚刚人工核对的 `source_fingerprint` 与 `confirm_import=true`。预览后任意源文件变化都会使指纹失效。
- 迁移只写入 PostgreSQL 配置和加密 secrets，不探测站点、下载器、图床、Discord、Sonarr 或 Radarr，不代表同意站规，也不授权 live 上传。
- 原文件始终保持不变。源配置与 allowlist cookie 会作为主密钥加密的快照保留 30 天；API 只显示归档 hash、大小和到期状态，不提供归档明文。到期仅删除密文快照，脱敏迁移报告和审计事件继续保留。
- 旧 Sonarr/Radarr v3 endpoint 与 API key 可迁移，容器中的 `127.0.0.1/localhost` 实例会保持禁用。旧 Discord bot token/频道不能无歧义转换为 incoming webhook，只返回 `legacy_discord_bot_requires_webhook` warning，要求在 Web 中人工新建渠道。
- 旧站点限速不会静默覆盖已审批规则，容器中的 `127.0.0.1/localhost` 下载器会保持禁用，QUI proxy 和未实现字段会明确列入 warnings。

同样的操作可以在 Web「配置 → 旧配置迁移」完成。重复提交同一已完成指纹会返回原迁移记录，不会重复执行资源写入。

## 开发验收

```bash
make go-check
UA_POSTGRES_PASSWORD=compose-config-fixture docker compose config --quiet
make verify-go-v2-local
```

`verify-go-v2-local` 会创建名称、数据库、数据和备份目录均隔离的临时 Compose 栈，使用随机数据库密码和临时管理员，验证 linux/amd64、非 root、只读根文件系统、capability/no-new-privileges、PostgreSQL 无宿主机端口、主密钥权限、MediaInfo/BDInfo/FFmpeg/FFprobe/mkbrr/age/PostgreSQL 17 工具链、健康检查、安全响应头、鉴权拒绝、OpenAPI/tool/AgentSkill、中文 Web、原生 CLI、安全阻塞的 live 就绪交接、迁移完整性、任务幂等和重启持久性。验收还实际完成一次“加密备份 → 修改数据库/规则/key/artifact → 停服 → 错误密钥/损坏/版本拒绝 → 临时库离线恢复 → 重启后逐项校验”，并确认原状态回滚证据存在；随后精确删除临时栈和卷。机器可读结果写入 `tmp/go-v2-local-ready.json`，报告不会包含临时凭据。

上述测试使用本地 fixture、`httptest` 或隔离 Compose，不会访问真实站点、下载器或图床。报告中的 `live_validation.status=blocked_external` 是有意保留的真实边界。真实盒子闭环必须另行完成受控 live 验证，并保留源/目标 torrent hash、内容路径、规则指纹、查重、上传、注入、做种和 summary 证据。
