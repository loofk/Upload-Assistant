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
- `GET /api/v2/notifications` 读取任务终态后生成的脱敏本地通知。调度、排名和通知均不代表用户批准候选，也不会自动创建正式转种任务或上传种子。

常驻调度器与 Web 本地通知已可用；外部 webhook、Discord 等主动推送渠道仍属于后续能力。在外部渠道完成并有交付证据前，不应把“已主动推送到第三方渠道”报告为完成。

## 旧配置安全迁移

迁移采用 preview → fingerprint 确认 → import：

- `GET /api/v2/migrations/legacy/preview` 使用非执行字面量解析器读取旧配置，只返回主密钥 HMAC fingerprint、文件大小、资源名称、credential 字段名、禁用原因和 warnings。它不会公开可用于猜测弱密码的普通内容 hash。
- `POST /api/v2/migrations/legacy` 必须同时提交刚刚人工核对的 `source_fingerprint` 与 `confirm_import=true`。预览后任意源文件变化都会使指纹失效。
- 迁移只写入 PostgreSQL 配置和加密 secrets，不探测站点、下载器或图床，不代表同意站规，也不授权 live 上传。
- 原文件始终保持不变。源配置与 allowlist cookie 会作为主密钥加密的快照保留 30 天；API 只显示归档 hash、大小和到期状态，不提供归档明文。到期仅删除密文快照，脱敏迁移报告和审计事件继续保留。
- 旧站点限速不会静默覆盖已审批规则，容器中的 `127.0.0.1/localhost` 下载器会保持禁用，QUI proxy 和尚未实现的集成会明确列入 warnings。

同样的操作可以在 Web「配置 → 旧配置迁移」完成。重复提交同一已完成指纹会返回原迁移记录，不会重复执行资源写入。

## 开发验收

```bash
make go-check
docker compose -f docker-compose.go.yml config --quiet
```

上述测试使用本地 fixture 或 `httptest`，不会访问真实站点、qBittorrent 或图床。真实盒子闭环必须另行完成受控 live 验证，并保留源/目标 torrent hash、内容路径、规则指纹、查重、上传、注入、做种和 summary 证据。
