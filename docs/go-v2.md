# Upload Assistant v2（Go + PostgreSQL）

v2 是当前重构目标：Docker Compose 启动 Go 常驻服务和 PostgreSQL，每个转种步骤、外部调用、阻塞、恢复与证据都持久化并可在 Web 控制台审计。

## 本地启动

```bash
docker compose -f docker-compose.go.yml up -d --build
docker compose -f docker-compose.go.yml exec upload-assistant upload-assistant admin bootstrap --username admin
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
upload-assistant cli shell
```

创建任务默认使用 `execution_mode=step`，适合逐步审计。未指定 idempotency key 时 CLI 会为单次新意图生成随机 key；自动化重试应显式使用稳定的 `--idempotency-key`。

```bash
upload-assistant cli retorrent create \
  --source-url 'https://u2.dmhy.org/details.php?id=60635' \
  --target MTEAM \
  --downloader box --save-path /downloads \
  --screenshot-profile default --image-host imgbb \
  --idempotency-key operator-request-20260808-01

upload-assistant cli jobs resume <job-id> \
  --accept-rule U2=<reviewed-fingerprint> \
  --obligation U2:<obligation-id>=<human-evidence>
```

live 上传只能用显式 `--confirm-upload`，且同一次命令必须重新提交源站和目标站的精确规则 fingerprint；CLI 只是减少误操作，服务端仍会重新验证规则、人工 obligation、查重、不可变上传包与做种 gate。

旧版配置迁移使用独立的只读挂载。默认读取宿主机 `./data`，可在启动前设置：

```bash
export UA_LEGACY_DATA_HOST_PATH=/absolute/path/to/old-upload-assistant/data
docker compose -f docker-compose.go.yml up -d --build
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
- `GET /api/v2/notifications` 读取脱敏的本地/外部投递状态、尝试次数、payload SHA-256 和远端回执 hash。调度、排名和通知均不代表用户批准候选，也不会自动创建正式转种任务或上传种子。
- `GET/PUT /api/v2/notification-channels` 独立管理 Discord incoming webhook。Webhook URL 进入加密 secret；每日调度只有在 `config.notification_channels` 显式列出已启用渠道后才投递。

Discord 投递由 PostgreSQL 队列和独立 Worker 执行，使用租约、最多 8 次指数退避、崩溃后接管和 `wait=true` 送达回执。消息禁用 mentions，只含候选摘要与本地任务 ID。`sent` 只表示通知被 Discord 接收，不表示候选获批或种子已上传。

## Sonarr / Radarr

- `GET/PUT /api/v2/media-managers` 独立管理多个 Sonarr/Radarr v3 实例；API key 加密且只回显字段名。
- `POST /api/v2/media-managers/{name}/probe` 是显式、只读的 `/api/v3/system/status` 探测，保存版本、配置 hash 和响应 hash。
- `POST /api/v2/media-managers/{name}/lookup` 复刻 legacy 的只读补元数据语义：Sonarr 接受 TVDB ID 或 path+title，Radarr 接受 TMDb ID 或精确 path。审计只保存 query/response SHA-256 与规范化 ID，不保存 API key、原始响应或本地路径。
- HTTP 重定向被禁止，响应体有大小上限，失败只持久化稳定错误码。当前它们是显式 metadata helper，不会向 Sonarr/Radarr 添加、删除、重命名或刷新媒体。

## 远程下载器

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
docker compose -f docker-compose.go.yml config --quiet
```

上述测试使用本地 fixture 或 `httptest`，不会访问真实站点、下载器或图床。真实盒子闭环必须另行完成受控 live 验证，并保留源/目标 torrent hash、内容路径、规则指纹、查重、上传、注入、做种和 summary 证据。
