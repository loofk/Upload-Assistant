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

## 开发验收

```bash
make go-check
docker compose -f docker-compose.go.yml config --quiet
```

上述测试使用本地 fixture 或 `httptest`，不会访问真实站点、qBittorrent 或图床。真实盒子闭环必须另行完成受控 live 验证，并保留源/目标 torrent hash、内容路径、规则指纹、查重、上传、注入、做种和 summary 证据。
