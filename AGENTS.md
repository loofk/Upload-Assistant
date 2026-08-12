# Upload Assistant v2 开发指南

本文件适用于整个仓库。当前产品主线是 Go v2；旧 Python Upload Assistant 与 `ptcli.py`
只保留用于迁移核对，不是默认运行时、发布入口或新功能落点。

## 当前架构

- 单个 Go 二进制：`cmd/upload-assistant`，提供 `serve`、`migrate`、`admin`、`cli`。
- HTTP API：只发布 `/api/v2`；健康检查、OpenAPI、工具目录和 AgentSkill 使用稳定入口。
- PostgreSQL：保存任务状态机、步骤尝试、调度、通知、配置、规则修订和审计事件。
- Web：`web/` 中的 React/TypeScript 构建后嵌入 `internal/webui/dist/`。
- Docker：`Dockerfile.v2` 与完全一致的 `docker-compose.yml`、`docker-compose.go.yml`。
- 持久目录：`/data` 与 `/downloads`；旧配置固定通过 `/legacy:ro` 只读迁移。

默认 Compose 服务名是 `upload-assistant` 和 `postgres`。不要恢复 `ptcli-api`、Python
运行时、旧 `/v1` 路由或 Flask Web 作为默认部署入口。

## 开发开始前

1. 确认当前分支和工作树，保留用户已有改动。
2. 若线程存在原生 Goal，先读取 Goal 状态并继续当前 objective；不要从旧 Python
   `goal-progress` 推断 Go v2 的进度。
3. 阅读与变更直接相关的 Go package、迁移、OpenAPI、Web 类型和测试。
4. 优先使用 fixture、`httptest` 与隔离 PostgreSQL；未经用户对具体外部动作的授权，禁止
   联系 Tracker、下载器、图床、元数据服务、Discord、Sonarr 或 Radarr。

## 权威验收

快速项目门禁：

```bash
make go-check
```

该命令执行 Web 类型检查/测试/构建、Go 格式检查、`go vet`、全部 Go 测试和二进制构建。

Compose 契约：

```bash
UA_POSTGRES_PASSWORD=compose-config-fixture make go-compose-config
```

完整本地验收：

```bash
make verify-go-v2-local
jq . tmp/go-v2-local-ready.json
```

有效结果必须满足 `ok=true`、`status=local_ready`、`blockers=[]`，且
`checks.external_calls_performed=false`。该结果证明隔离本地基线，不证明真实账号、盒子或
live 上传已验证。不要用旧的 `make check-ptcli`、`make verify-ptcli-local` 或 Python 测试
替代 Go v2 门禁。

PostgreSQL 集成测试只在设置以下变量时运行：

```bash
UA_TEST_DATABASE_URL='postgres://...' go test -p 1 ./... -count=1
```

数据库必须是隔离测试库，不能指向生产或用户现有数据。

## 本地部署

```bash
cp .env.example .env
# 把 openssl rand -hex 32 的结果写入 UA_POSTGRES_PASSWORD
docker compose up -d --build
docker compose exec upload-assistant upload-assistant admin bootstrap --username admin
```

初始 token 遗失时，只能由仍控制服务主机的操作者显式运行
`upload-assistant admin token issue --username admin --name web-recovery --confirm` 签发新 token；
签发会写入审计且 token 只显示一次。不得接受用户自行指定的 token 值。

默认只绑定 `127.0.0.1:8080`。PostgreSQL 不发布宿主机端口；服务容器以 UID/GID 1000、
只读根文件系统、drop ALL capabilities 和 no-new-privileges 运行。不要为了调试弱化这些
默认值。远程访问应通过有 TLS 和访问控制的隧道或反向代理。

主要入口：

- `GET /health/ready`
- `GET /openapi.json`
- `GET /api/v2/tools`
- `GET /api/v2/adapters`
- `GET /.well-known/upload-assistant.json`
- `GET /.well-known/upload-assistant/SKILL.md`

除健康、版本、OpenAPI、Web 和 well-known 发现外，API 默认需要 Bearer token 和相应
scope。token 只能通过环境、受限文件或 TTY 输入，不得放在 URL、日志或普通命令参数中。

## 代码地图

- `cmd/upload-assistant/`：进程装配、服务启动、迁移与管理员入口。
- `internal/server/`：HTTP 路由、认证、OpenAPI 和 AI 工具契约。
- `internal/workflow/`：持久任务、步骤、attempt、事件链、恢复和回放。
- `internal/worker/`：retorrent 与 daily candidate 步骤执行器。
- `internal/rules/`：Markdown 解析、fingerprint、不可变修订、审批和激活。
- `internal/sites/`：站点能力契约、NexusPHP 源站和 MTEAM 目标站 adapter。
- `internal/downloaders/`：qBittorrent、Transmission、rTorrent、Deluge Web。
- `internal/integrations/`：加密配置、路径映射与配置审计。
- `internal/imagehosts/`、`internal/media/`、`internal/torrentmaker/`：图床、素材工具和制种。
- `internal/candidates/`、`internal/schedules/`、`internal/notifications/`：每日候选与投递。
- `internal/legacy/`：不执行 Python 的只读旧配置迁移。
- `internal/clientcli/`：调用同一 `/api/v2` 的结构化 JSON CLI 与交互 shell。
- `migrations/`：有序 PostgreSQL 迁移；只追加，不重写已发布迁移。
- `scripts/verify_go_v2_local_ready.sh`：最终隔离 Compose 验收和机器可读报告。

## 核心安全不变量

### 任务与审计

- 状态覆盖 `draft/queued/running/paused/blocked/failed/complete/cancelled`。
- 每个步骤和每次尝试独立持久化，输入只保存脱敏快照或 SHA-256。
- 任务事件形成 append-only hash 链；artifact 保存大小、SHA-256、来源步骤和受限下载策略。
- `complete` 只能在全部硬 gate、目标新种注入和做种核验通过后产生不可变 summary。
- replay 创建安全重置的新任务，绝不继承规则接受、人工 obligation、resume state 或上传确认。

### 外部写入

目标上传、图床上传、下载器 Add/注入、下载器限速和 Discord 投递必须在远端调用前保存
intent。网络结果未知、部分成功、租约过期或本地回执失败时，不得盲重试：任务进入稳定的
reconciliation blocker，绑定当前 attempt、期望 hash/ID 和人工证据 SHA-256，只允许实现过
且经过测试的只读对账恢复路径。

不得把超时当作“未执行”，也不得用 pause、cancel、retry 或 replay 绕过未知结果。新增任何
外部写入时必须同时设计 intent、结果审计、未知结果状态、只读核对方案和崩溃恢复测试。

### 规则与上传

- 站点规则是带 YAML front matter 的 Markdown，kind 为
  `upload-assistant.site-rule.v1`。
- 实际规则文档放在本地 `data/site-rules/*.md`，默认被 Git 忽略，避免提交认证页面证据；
  不得保存 Cookie、passkey、API key 或账号信息。
- `source.complete=false` 的修订不能审批或激活。fingerprint 由正文和可执行策略计算，审批
  必须提交服务端返回的精确值。
- `auto_pull` 与 `auto_upload` 是运行时硬开关；规则未完整审查时保持 false。
- 无法可靠程序化判断的条款必须是 blocking manual obligation，不得伪装成 enforced。
- live 上传必须同时满足当前活动规则、目标查重、人工 obligation、不可变上传包、精确
  `accept_rules` 和由用户显式提供的 `confirm_upload=true`。

规则本地工作流：

```bash
upload-assistant cli rules import --file data/site-rules/U2.md
upload-assistant cli rules list U2
upload-assistant cli rules get <revision-id>
upload-assistant cli rules approve <revision-id> --fingerprint <sha256> --comment '人工审查说明' --confirm
upload-assistant cli rules activate <revision-id> --confirm
```

导入、审批和激活都不访问 Tracker，也不等于具体任务已接受规则。

### 凭据与日志

- 配置凭据使用主密钥加密，API 只返回字段存在性和脱敏元数据。
- API、日志、attempt、审计和错误必须递归清除 token、Cookie、passkey、announce URL、
  webhook、API key 和下载器密码。
- 不读取或输出真实 secret 文件来“检查配置”。测试使用合成 token 和 `.invalid`/httptest
  endpoint。
- `/data/master-keys` 必须保持 0600；迁移归档加密保存并有 30 天到期状态，API 不返回明文。

## Adapter 范围

当前完整参考路径是 U2/CHD → MTEAM：U2/CHD 通过 NexusPHP 源 adapter，MTEAM 通过 API
目标 adapter。allowlist 其他中文站点可以独立配置，但只有 `/api/v2/adapters` 中
`runtime_supported=true` 且声明了对应 operation 的 adapter 才能被调用。config-only 站点
必须保持没有 operation 和明确 unavailable reason；不能根据站点名称推断能力。

下载器支持 qBittorrent、Transmission、rTorrent 和 Deluge Web。调用前读取
`/api/v2/downloader-adapters` 的精确能力；不支持 category、tags、`skip_checking` 或有效限速
时必须明确阻塞，不能静默忽略。

## Web 与 API 契约

修改 API 时同步检查：

1. `internal/server/openapi.json`
2. `internal/server/api_docs.go` 的 `/api/v2/tools`
3. `web/src/api.ts` 与相关 React 页面
4. `internal/agentskill/SKILL.md` 和 `.agents/skills/upload-assistant/SKILL.md`
5. server、CLI、Web 和 Compose verifier 中的契约断言

若修改 AgentSkill，两个副本必须完全一致，并运行：

```bash
.venv/bin/python /root/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/upload-assistant
cmp -s internal/agentskill/SKILL.md .agents/skills/upload-assistant/SKILL.md
```

所有 AI 响应应保留短路径字段：`ok`、`status`、`blockers`、`next_actions`、`job_id`、
`summary`/`summary_file` 和 `resume_state`。候选排名、通知送达或 `configuration_ready=true` 都
不能被解释为上传授权。

## 旧代码边界

`upload.py`、`ptcli.py`、`src/`、旧 Flask Web、Discord bot、旧 Dockerfile 和海外 tracker
实现仍保留用于迁移与对照。除非任务明确要求 legacy 修复或清理，不要在这些文件中增加 Go
v2 功能，也不要让 Go 镜像导入/执行 Python。旧配置迁移只能读取固定 `/legacy:ro` 路径，
生成脱敏预览并在用户确认源 fingerprint 后写入新数据库；不得删除或修改旧数据。

## 真实环境边界

`GET /api/v2/readiness/live` 只做本地检查，必须固定返回
`external_calls_performed=false`、`live_upload_authorized=false` 和
`resume_state.confirm_upload=false`。真实 U2/CHD→MTEAM、下载器、MediaInfo/BDInfo、截图、
mkbrr、imgbb/PTPimg 验证需要用户提供合法账号、合法测试资源、当前已审批规则、明确的联网
探测授权以及最终上传确认。在这些条件具备前，报告 `blocked_external` 是正确状态，不能伪造
完成或用 fixture 冒充 live 证据。
