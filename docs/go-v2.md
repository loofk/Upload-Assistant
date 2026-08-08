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

## 开发验收

```bash
make go-check
docker compose -f docker-compose.go.yml config --quiet
```

上述测试使用本地 fixture 或 `httptest`，不会访问真实站点、qBittorrent 或图床。真实盒子闭环必须另行完成受控 live 验证，并保留源/目标 torrent hash、内容路径、规则指纹、查重、上传、注入、做种和 summary 证据。
