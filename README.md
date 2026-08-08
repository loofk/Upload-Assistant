# Upload Assistant v2

Upload Assistant 是面向中文 PT 圈的本地转种、发种与每日候选自动化服务。当前主线是单个 Go 服务、PostgreSQL 持久任务状态机和内嵌简体中文 Web；默认 Docker Compose 不再启动 Python `ptcli-api`，也不发布旧 `/v1` API。

核心安全边界：规则审查、目标查重、人工 obligation、`accept_rules`、`confirm_upload` 和做种要求都是硬门禁。任何 agent、Web 或 CLI 调用都不能静默跳过。

## 当前能力

- `/api/v2` 任务 API：`queued/running/paused/blocked/failed/complete/cancelled`，支持逐步暂停、恢复、重试与幂等创建。
- U2/CHD → MTEAM 参考闭环：源站识别/拉种、远程下载器、MediaInfo/BDInfo/截图、imgbb/PTPimg、目标查重/上传、新种下载/注入、做种核验与最终 summary。
- PostgreSQL 持久调度：每日候选、排名、风险/阻塞、通知、运行历史与崩溃后租约接管。
- 独立集成配置：qBittorrent、Transmission、rTorrent、Deluge Web、imgbb、PTPimg、截图策略、Discord webhook、Sonarr/Radarr，以及显式 TMDb/PTGen 元数据 provider。
- Markdown 站点规则：结构化 front matter、原始规则文本、不可变版本、fingerprint、人工审批和激活。
- 审计：每个任务的 hash 链、artifact SHA-256，以及配置和外部动作的全局脱敏日志。
- AI 调用：OpenAPI、结构化工具目录、OpenClaw/Hermes Agent Skill 和原生 JSON CLI。
- 旧配置迁移：只读解析 `/legacy`，不执行 Python；加密凭据和 30 天加密历史归档。

旧 Python 源码仍保留用于迁移核对，但不在 v2 镜像运行时中，也不属于默认部署或发布入口。

## Docker Compose 部署

要求 Docker Compose v2、linux/amd64 和可用的宿主机下载目录。

```bash
cp .env.example .env
openssl rand -hex 32
```

把生成值粘贴到 `.env` 的 `UA_POSTGRES_PASSWORD`。空密码或未设置时 Compose 会直接拒绝启动。然后按盒子路径修改 `UA_DOWNLOADS_HOST_PATH`；如果希望直接备份 `/data` 或 PostgreSQL，也可以把 `UA_DATA_SOURCE`、`UA_POSTGRES_DATA_SOURCE` 改为绝对宿主机路径。

```bash
docker compose up -d --build
docker compose exec upload-assistant upload-assistant admin bootstrap --username admin
docker compose ps
```

管理员密码从 TTY 读取；初始 API token 只显示一次，应立即保存到密码管理器或权限受限的 token 文件。服务默认只绑定 `127.0.0.1:8080`。远程访问应使用带 TLS 和访问控制的隧道或反向代理，不要改成无认证公网监听。

部署入口：

- Web：`http://127.0.0.1:8080/`
- 健康检查：`GET /health/ready`
- OpenAPI 3.1：`GET /openapi.json`
- AI 工具：`GET /api/v2/tools`
- 适配器能力契约：`GET /api/v2/adapters`（带稳定 SHA-256，明确 callable/config-only、operations 与 gates）
- 安全回放：`POST /api/v2/jobs/{job_id}/replay`（只复制非授权输入，重新执行全部 gate）
- Agent 发现：`GET /.well-known/upload-assistant.json`
- Agent Skill：`GET /.well-known/upload-assistant/SKILL.md`

应用容器以 UID/GID 1000 非 root 运行，根文件系统只读、移除 Linux capabilities，并启用 `no-new-privileges`。`/data` 和 `/downloads` 是仅有的持久可写挂载；旧配置目录固定以 `/legacy:ro` 挂载。PostgreSQL 不发布宿主机端口。

### 连接已有下载器

qBittorrent 位于宿主机时，在 Web「配置」中使用 `http://host.docker.internal:<port>`。下载器在其他容器时，把该容器加入 `.env` 中 `UA_DOCKER_NETWORK` 指定的网络，并使用服务名作为 host。远程下载路径必须显式映射到 Upload Assistant 容器内的 `/downloads`，路径映射和限速会进入任务证据。

## 首次配置顺序

1. 在 Web 中配置源站 U2 或 CHD、目标站 MTEAM；凭据只写不读并加密保存。
2. 导入完整规则 Markdown，审阅解析策略和原文，按精确 fingerprint 审批并激活。
3. 配置下载器、图床、截图策略以及参考闭环所需的 TMDb/PTGen provider；按需配置 Discord 和 Sonarr/Radarr。PTGen 不会回退到内置公共地址。
4. 打开「就绪检查」，或调用 `GET /api/v2/readiness/live`，修复所有本地 blocker。
5. 获得操作者授权后，分别执行真实站点/下载器探测，再用 `execution_mode=step` 创建受控任务。
6. 审阅不可变上传包和最终查重后，提交精确 `accept_rules`、人工 obligation 证据及显式 `confirm_upload=true`。

就绪检查永远返回 `external_calls_performed=false`、`live_upload_authorized=false` 和 `resume_state.confirm_upload=false`。`configuration_ready=true` 只表示本地配置齐全，不代表凭据有效、查重通过或用户已同意联网/上传。

## 原生 CLI

CLI 调用同一套 `/api/v2`，默认输出结构化 JSON，不依赖 Python。token 通过 `UA_API_TOKEN_FILE`、`UA_API_TOKEN` 或交互输入提供，不能放进 URL 或命令参数。

```bash
export UA_API_URL=http://127.0.0.1:8080
export UA_API_TOKEN_FILE=/run/secrets/upload-assistant-api-token

upload-assistant cli health
upload-assistant cli tools
upload-assistant cli jobs list --limit 20
upload-assistant cli candidates list --source U2 --target MTEAM
upload-assistant cli audit list --resource-type downloader --resource-id box
upload-assistant cli readiness live \
  --source U2 --target MTEAM \
  --downloader box --image-host imgbb --screenshot-profile default \
  --tmdb-provider tmdb-main --ptgen-provider ptgen-main
```

创建任务默认建议 `execution_mode=step`。live 上传时，源站和目标站的精确规则 fingerprint、所有阻塞 obligation 证据和 `--confirm-upload` 必须在同一次明确意图中提供，服务端仍会重新验证所有 gate。

## Agent 接入

OpenClaw 可读取仓库内 `.agents/skills/upload-assistant/SKILL.md`；Hermes 或其他 agent 可从部署后的 well-known URL 安装。调用顺序应为：

1. `/health/ready`
2. `/openapi.json`
3. 鉴权后的 `/api/v2/tools`
4. `/api/v2/readiness/live`
5. 根据 blocker/next_actions 创建或恢复任务

Agent 只能把 API 的 `status`、`ok`、`blockers`、`next_actions`、`resume_state`、`job_id` 和 summary 作为决策依据，不得从候选排名、通知送达或配置就绪推断上传许可。

## 开发与验收

```bash
make go-check
UA_POSTGRES_PASSWORD=compose-config-fixture make go-compose-config
make verify-go-v2-local
```

`make go-check` 执行 TypeScript 类型检查、Web 测试/构建、Go 格式/vet/测试和二进制构建。`verify-go-v2-local` 创建全新隔离 Compose 项目和卷，验证 linux/amd64、非 root、只读根文件系统、capability/no-new-privileges、PostgreSQL 无宿主机端口、主密钥权限、MediaInfo/BDInfo/FFmpeg/FFprobe/mkbrr 原生工具链、健康检查、安全响应头、鉴权、OpenAPI、44 个 AI 工具、带 golden fingerprint 的 26 项适配器能力契约、Agent Skill、中文 Web、CLI、迁移、全部 PostgreSQL store 与完整 retorrent/每日候选 fixture、幂等、重启持久性和安全阻塞的 live 交接，完成后自动清理。

本地与 CI 测试只使用 fixture、`httptest` 和隔离 PostgreSQL，不联系真实 Tracker、下载器或图床。真实 U2→MTEAM、CHD→MTEAM、qBittorrent、MediaInfo/BDInfo/截图/mkbrr 与 imgbb/PTPimg 闭环必须由操作者提供合法账号、资源和显式授权后执行；在此之前必须保持为外部验证阻塞，不能伪造完成。

更完整的运维、规则、下载器、迁移和审计说明见 [docs/go-v2.md](docs/go-v2.md)。
