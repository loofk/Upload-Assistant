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

## 真实环境就绪交接

在进行任何站点、下载器、图床或元数据提供方联网探测前，先调用 `GET /api/v2/readiness/live`，或使用 Web 顶部「就绪检查」和 CLI `readiness live`。当前完整参考路径限定为 U2/CHD → MTEAM。该只读检查仅验证：已审批激活的规则 fingerprint 与阻塞 obligations、站点凭据字段是否存在、源/目标下载器、图床、截图策略、显式 TMDb/PTGen provider、`/downloads` 挂载以及 MediaInfo/BDInfo/FFmpeg/FFprobe/mkbrr 是否可用。

普通视频文件使用 MediaInfo JSON；检测到 `BDMV/STREAM` 时改为对原盘根目录运行非交互 BDInfo 整盘扫描，并把原始 UTF-8 文本作为独立 `bdinfo` artifact 绑定到 MTEAM 上传包。每个 attempt 使用独立临时报告目录，只接受唯一、受限大小的普通文本文件。Docker 镜像从固定 commit 构建自包含 BDInfo，并校验源归档 SHA-256、保留 LGPL 许可证。`VIDEO_TS` 不会被 MediaInfo 或 BDInfo 冒充处理，而会明确返回 `dvdinfo_adapter_required`。

就绪报告固定返回 `external_calls_performed=false`、`live_upload_authorized=false`，其 `resume_state.confirm_upload` 也固定为 `false`。即使 `configuration_ready=true`，也只表示本地配置可以进入受控外部验证，不代表凭据真实有效、站点无重复、资源可下载或用户同意上传。agent 应展示报告中的 blocker、next_actions、精确规则 fingerprint 与 obligation IDs，再分别取得联网探测授权；最终 live 上传仍必须在不可变上传包和最终查重可审阅后取得显式 `accept_rules` 与 `confirm_upload`。

## 审计读取

- `GET /api/v2/jobs/{job_id}/attempts` 按步骤与尝试号稳定分页，展示每次执行的状态、时间、adapter、稳定错误码及脱敏输出；原始输入快照只返回 SHA-256，不会经 API 泄漏。
- `GET /api/v2/jobs/{job_id}/events` 是单个任务的 append-only hash 链；配合 attempts、steps、artifacts 与 summary 可验证每次重试、租约恢复、转种边界和证据文件。
- `downloader_add`、`image_upload`、`target_upload` 与 `target_inject` 的远端写入执行中不允许中途 pause/cancel；需要停机时应使用逐步模式或 `stop_after_step` 停在边界。若 Worker 在这些步骤租约过期，任务不会自动重跑，而是以原 attempt 进入 unknown-outcome reconciliation。
- `POST /api/v2/jobs/{job_id}/replay` 只允许从 blocked/failed/cancelled 任务创建带 `replay_of_job_id` 的全新任务，默认逐步模式；运行中、暂停、已完成、待对账、外部写入结果未知、正在恢复，或已经完成任一下载器/目标站/图床写入的任务会被硬拒绝。新任务会清除旧规则接受、人工 obligation、resume_state 和 `confirm_upload`，不能用回放绕过 gate，也不能重复任何已记录的远端写入。
- 未知远端写入、下载器 partial-add 与下载器结果审计失败会自动在 `resume_state.reconciliation` 写入 blocker 与当前 attempt 模板。普通 resume、CLI `jobs retry` 和 replay 均返回稳定的 `reconciliation_required`/`replay_not_allowed`；只有与当前 attempt 精确绑定、由操作者确认、包含 RFC3339 观察时间与人工证据 SHA-256 的允许决定才能续跑。目标上传的 `verified_not_applied` 会重新查重并再次要求显式 `confirm_upload`；目标 `verified_uploaded` 必须绑定目标种子 ID 与 submitted torrent SHA-256，Worker 只读确认后生成 recovery 回执。图床在网络结果未知、配置竞态或本地 receipt 落库失败时同样停止；若已有可信返回证据，图床 `verified_uploaded` 必须绑定服务器生成的 pending evidence SHA-256，Worker 只补写本地回执。下载器 Add 在远端调用前先持久化 intent；partial 或 unknown 结果必须以 `verified_remote_state` 绑定模板中的精确 submitted infohash，Worker 只读核对远端身份、大小、保存路径、标签、暂停状态与限速后生成证据。以上恢复路径都绝不二次写入。事件链只保存决定、证据 hash 与 reconciliation hash，不复制可能含凭据的原始材料。
- `GET /api/v2/audit-events` 是配置变更、远程下载器/图床动作、通知、迁移和 Sonarr/Radarr 等全局动作的脱敏审计，可按 `actor_type`、`action`、`resource_type`、`resource_id` 精确过滤，并使用不透明 cursor 稳定翻页。该全局日志不会被描述为任务 hash 链。
- Web 顶部「审计」和 CLI `audit list` 读取同一接口。API 返回前会递归脱敏 credential、cookie、token、passkey、announce URL 等敏感字段；需要证明某个任务完整性时仍应回到该任务的事件链和 artifact SHA-256。

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
- `GET /api/v2/notifications` 读取脱敏的本地/外部投递状态、尝试次数、payload SHA-256 和远端回执 hash。`POST /api/v2/notifications/{notification_id}/reconcile` 只处理 `outcome_unknown`：`verified_not_delivered` 显式允许一次排队重试，`verified_delivered` 绑定人工证据与精确 Discord message ID 且不再次调用 webhook。调度、排名和通知均不代表用户批准候选，也不会自动创建正式转种任务或上传种子。
- `GET/PUT /api/v2/notification-channels` 独立管理 Discord incoming webhook。Webhook URL 进入加密 secret；每日调度只有在 `config.notification_channels` 显式列出已启用渠道后才投递。

Discord 投递由 PostgreSQL 队列和独立 Worker 执行；外部 POST 前先提交 `notification.delivery_started` 意图审计，并使用租约、对明确拒绝最多 8 次指数退避和 `wait=true` 送达回执。网络无响应、5xx、成功响应不可解析、本地回执事务失败或发送租约过期均进入 `outcome_unknown`，自动投递器不会接管重发；只有人工对账可决定排队重试或仅补录送达回执。消息禁用 mentions，只含候选摘要与本地任务 ID。`sent` 只表示通知被 Discord 接收或经人工证据确认，不表示候选获批或种子已上传。

## Sonarr / Radarr

- `GET/PUT /api/v2/media-managers` 独立管理多个 Sonarr/Radarr v3 实例；API key 加密且只回显字段名。
- `POST /api/v2/media-managers/{name}/probe` 是显式、只读的 `/api/v3/system/status` 探测，保存版本、配置 hash 和响应 hash。
- `POST /api/v2/media-managers/{name}/lookup` 复刻 legacy 的只读补元数据语义：Sonarr 接受 TVDB ID 或 path+title，Radarr 接受 TMDb ID 或精确 path。审计只保存 query/response SHA-256 与规范化 ID，不保存 API key、原始响应或本地路径。
- HTTP 重定向被禁止，响应体有大小上限，失败只持久化稳定错误码。当前它们是显式 metadata helper，不会向 Sonarr/Radarr 添加、删除、重命名或刷新媒体。

## TMDb / PTGen 元数据 provider

- `GET/PUT /api/v2/metadata-providers` 独立管理 TMDb/PTGen endpoint；key 加密保存且只回显字段名。TMDb 启用时必须提供 key，PTGen key 可选，但 endpoint 始终必须显式配置，不会回退到公共服务。
- `POST /api/v2/metadata-providers/{name}/resolve` 是显式 external-read。TMDb 按官方 v3 `find/{imdb}` 或 `movie|tv/{id}/external_ids` 契约规范化 IMDb/TMDb ID，并阻断跨类型歧义或 ID 冲突；PTGen 兼容 legacy 的 IMDb→豆瓣二段查询和直接豆瓣 subject 查询。
- 所有请求禁重定向、限时、限响应大小。全局审计只保存配置、查询、响应和 PTGen 描述 SHA-256、规范化 ID 与字节数，不保存 API key、原始响应或简介。配置成功不是联网许可，`matched=false` 也不是材料已完成。
- 新建 retorrent 工作流版本 2 把 `metadata_tmdb` 与 `metadata_ptgen` 作为两个独立、可暂停和可恢复的必需步骤。任务只有在输入或 resume state 显式选择 provider 时才调用远端；也可由人工在对应 resume 字段提交已复核 ID/简介。每步把 provider 配置 fingerprint、查询/响应 hash 和不可变 artifact 绑定到事件链。
- MTEAM 打包硬性要求 IMDb、带 movie/tv 类型的 TMDb、豆瓣 subject 和非空 PTGen/豆瓣简介。原始简介只保存在受控 artifact；步骤响应与 summary 只暴露 hash 和字节数，打包前重新校验 artifact SHA-256，并把 BBCode 标记安全文本化，避免外部简介注入上传描述结构。

## 远程下载器

- `GET /api/v2/adapters` 是跨站点、下载器、图床、元数据、媒体管理器、通知与本地媒体工具的统一能力契约。响应固定排序并带 `catalog_sha256`；所有 callable adapter 必须声明 operations、credential 字段、safety gates 和 constraints，尚未实现的站点只显示为 `runtime_supported=false` 且没有 operation。契约变更必须人工更新 golden fingerprint，AI 不得根据名称猜测支持。
- `GET /api/v2/downloader-adapters` 是运行时能力的权威目录。调用方应先检查 `runtime_supported` 和逐项 `operations`，不能只依据 adapter 名称推断能力。
- qBittorrent、Transmission、rTorrent 与 Deluge Web 已支持独立 endpoint、加密凭据、远程路径映射、探测、加种、状态和文件查询、单种限速与等待完成；每次动作都会记录脱敏审计证据。
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

`verify-go-v2-local` 会创建名称和卷均隔离的临时 Compose 栈，使用随机数据库密码和临时管理员，验证 linux/amd64、非 root、只读根文件系统、capability/no-new-privileges、PostgreSQL 无宿主机端口、主密钥权限、MediaInfo/BDInfo/FFmpeg/FFprobe/mkbrr 原生工具链、健康检查、安全响应头、鉴权拒绝、OpenAPI/tool/AgentSkill、中文 Web、原生 CLI、安全阻塞的 live 就绪交接、迁移完整性、任务幂等与服务重启后的任务持久性，然后精确删除临时栈和卷。机器可读结果写入 `tmp/go-v2-local-ready.json`，报告不会包含临时凭据。

上述测试使用本地 fixture、`httptest` 或隔离 Compose，不会访问真实站点、下载器或图床。报告中的 `live_validation.status=blocked_external` 是有意保留的真实边界。真实盒子闭环必须另行完成受控 live 验证，并保留源/目标 torrent hash、内容路径、规则指纹、查重、上传、注入、做种和 summary 证据。
