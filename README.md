# Upload Assistant v2

Upload Assistant 是面向中文 PT 圈的本地转种、发种与每日候选自动化服务。当前主线是单个 Go 服务、PostgreSQL 持久任务状态机和内嵌简体中文 Web；默认 Docker Compose 不再启动 Python `ptcli-api`，也不发布旧 `/v1` API。

核心安全边界：规则审查、目标查重、人工 obligation、`accept_rules`、`confirm_upload` 和做种要求都是硬门禁。任何 agent、Web 或 CLI 调用都不能静默跳过。

## 当前能力

- `/api/v2` 任务 API：`queued/running/paused/blocked/failed/complete/cancelled`，支持逐步暂停、恢复、重试与幂等创建。
- U2/CHD → MTEAM 参考闭环：源站识别/拉种、远程下载器、MediaInfo/BDInfo/截图、ImgBB/PTPimg/Imgbox/Pixhost、目标查重/上传、新种下载/注入、做种核验与最终 summary。
- PostgreSQL 持久调度：每日候选、逐条评估检查点、排名、风险/阻塞、通知、运行历史与崩溃后租约接管；通知写入结果不明时禁止盲重试并要求显式对账。
- 独立集成配置：qBittorrent、Transmission、rTorrent、Deluge Web、ImgBB、PTPimg、无需 API Key 的 Imgbox/Pixhost、截图策略、Telegram/企业微信/飞书/Discord、Sonarr/Radarr，以及显式 TMDb/PTGen 元数据 provider。
- 站点规则编译：配置 1–20 个规则/FAQ HTTPS 地址，复用加密 Cookie 与普通访问限频顺序采集，后台流式 AI 合并带行号证据的多来源文本并把冲突留给人工裁定；人工只审核单种上传限速、单种下载限速和分类命名三个硬门禁。上传默认在站点上限下预留 20 MB/s，下载按原限值，分类标题由确定性 token 自动生成后校验；其余结论作为转种前提示。
- 站点访问门禁：活动 v2 规则与人工访问策略取更严格值，分别控制普通/搜索最小间隔、小时配额、站点并发和远端 429 cooldown；缺规则或配置默认禁止访问。
- 人工任务处理：`/attention` 只展示一个最终问题和可用方案；安全修复需要显式同意，远端结果未知、规则、查重、上传确认和做种义务不能自动处理。
- 发布预览：live 上传前可视化查看字段、描述、MediaInfo/BDInfo、截图、决策与警告；编辑会创建新的不可变发布包版本、保留旧证据并重置上传确认。
- 审计：每个任务的 hash 链、artifact SHA-256，以及配置和外部动作的全局脱敏日志。
- 运维中心：可检索结构化运行日志、request/trace/job/attempt 关联、incident 聚合、容量/队列告警、API Token 生命周期，以及证据绑定的本地/远程 OpenAI-compatible 诊断。
- 恢复：独立 `/backups` 挂载、age X25519 加密、每日 03:30 策略、保留与校验；恢复仅允许停服后的管理员 CLI，并先验证临时数据库和 staging 文件，再保留原状态回滚点并切换。
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

如果初始 token 已遗失但仍控制服务主机，可显式签发一个新的、带审计记录的管理员 token：

```bash
docker compose exec upload-assistant upload-assistant admin token issue \
  --username admin --name web-recovery --confirm
```

新 token 同样只显示一次。Web 会先验证 token，再写入当前标签页的 `sessionStorage`；任意自行指定的字符串不会被接受。

部署入口：

- Web：`http://127.0.0.1:8080/`
- 健康检查：`GET /health/ready`
- OpenAPI 3.1：`GET /openapi.json`
- AI 工具：`GET /api/v2/tools`
- 适配器能力契约：`GET /api/v2/adapters`（带稳定 SHA-256，明确 callable/config-only、operations 与 gates）
- 安全回放：`POST /api/v2/jobs/{job_id}/replay`（只复制非授权输入，重新执行全部 gate）
- 当前问题：`GET /api/v2/jobs/{job_id}/attention`；只有响应中 `executable=true` 的动作才可提交到 `/actions`
- 发布预览：`GET /api/v2/jobs/{job_id}/upload-preview`；编辑通过 `/upload-preview/revisions` 生成新版本
- 站点访问策略：`GET/PUT /api/v2/sites/{site_code}/access-policy`
- 运维总览与日志：`GET /api/v2/operations/overview`、`GET /api/v2/operational-logs`
- Incident 与诊断：`GET /api/v2/incidents`、`POST/GET /api/v2/diagnostics`
- Token 与备份：`/api/v2/api-tokens`、`/api/v2/backups`；这两类写接口不进入 Agent tool 目录，restore 也不通过 HTTP 提供
- 未知结果对账：任务内目标站、图床和下载器 Add 的普通 retry/resume/replay 会被硬拒绝；续跑必须绑定当前 attempt 和人工证据 SHA-256。独立限速写入出现未知结果时同样禁止自动重发，必须按精确信息哈希只读核对。已确认的恢复均不会重复外部写入
- Agent 发现：`GET /.well-known/upload-assistant.json`
- Agent Skill：`GET /.well-known/upload-assistant/SKILL.md`

应用容器以 UID/GID 1000 非 root 运行，根文件系统只读、移除 Linux capabilities，并启用 `no-new-privileges`。`/data`、`/downloads` 和独立 `/backups` 是持久可写挂载；旧配置目录固定以 `/legacy:ro` 挂载。PostgreSQL 不发布宿主机端口。

### 连接已有下载器

qBittorrent 位于宿主机时，在 Web「配置」中使用 `http://host.docker.internal:<port>`。下载器在其他容器时，把该容器加入 `.env` 中 `UA_DOCKER_NETWORK` 指定的网络，并使用服务名作为 host。远程下载路径必须显式映射到 Upload Assistant 容器内的 `/downloads`，路径映射和限速会进入任务证据。

配置并启用后，可在 Web「下载器」工作台查看 qBittorrent、Transmission、rTorrent 或 Deluge 的实时速度、任务进度、分享率、分类和文件详情。该页面只执行受控读取，不提供暂停、删除或强制校验操作。

## 首次配置顺序

1. 在 Web 中配置源站 U2 或 CHD、目标站 MTEAM；凭据只写不读并加密保存。
2. 为站点配置正数普通/搜索访问频次、小时配额和并发；在「站点规则」录入有序规则地址，确认范围与 Cookie host 后手动采集。采集只访问这些精确地址，按普通策略计数并生成不可变草稿；也可继续从 `data/site-rules/*.md` 导入离线文档。
3. 对照逐来源证据解决冲突，只审核单种上传限速、单种下载限速和分类命名三个硬门禁；需要调整时派生新草稿。全部确认后按精确 fingerprint 先审批、再独立激活。其他结论会进入上传预览的转种前提示；普通业务访问仍要求活动规则明确允许。
4. 配置下载器、图床、截图策略以及参考闭环所需的 TMDb/PTGen provider；按需配置 Telegram、企业微信、飞书、Discord 和 Sonarr/Radarr。保存配置不会联网，“未测试”资源可在对应卡片显式测试；图床测试会上传一张 100×100 图片，通知测试会发送一条真实消息，因此都要求二次确认且不会自动重试。PTGen 不会回退到内置公共地址；若当前网络无法访问 `workers.dev`，请为 Worker 绑定可达的自定义域名并配置其 `/api` 地址。
5. 打开「就绪检查」，或调用 `GET /api/v2/readiness/live`，修复所有本地 blocker。
6. 获得操作者授权后，分别执行真实站点/下载器探测，再用 `execution_mode=step` 创建受控任务。
7. 在发布预览中审核当前不可变版本和最终查重；修改会生成新版本并重置确认。确认无误后再提交精确 `accept_rules`、人工 obligation 证据及显式 `confirm_upload=true`。

就绪检查永远返回 `external_calls_performed=false`、`live_upload_authorized=false` 和 `resume_state.confirm_upload=false`。`configuration_ready=true` 只表示本地配置齐全，不代表凭据有效、查重通过或用户已同意联网/上传。

## 原生 CLI

CLI 调用同一套 `/api/v2`，默认输出结构化 JSON，不依赖 Python。token 通过 `UA_API_TOKEN_FILE`、`UA_API_TOKEN` 或交互输入提供，不能放进 URL 或命令参数。

```bash
export UA_API_URL=http://127.0.0.1:8080
export UA_API_TOKEN_FILE=/run/secrets/upload-assistant-api-token

upload-assistant cli health
upload-assistant cli tools
upload-assistant cli jobs list --limit 20
upload-assistant cli jobs attention <job-id>
upload-assistant cli jobs preview <job-id>
upload-assistant cli candidates list --source U2 --target MTEAM
upload-assistant cli rules import --file data/site-rules/U2.md
upload-assistant cli rules analyze <revision-id> --provider <provider-id> --confirm
upload-assistant cli rules sources MTEAM
upload-assistant cli rules sources-set MTEAM --file rule-sources.json --confirm
upload-assistant cli rules collect MTEAM \
  --fingerprint <source-set-sha256> --provider <provider-id> --confirm
upload-assistant cli rules collection <run-id>
upload-assistant cli rules approve <revision-id> --fingerprint <sha256> --comment '已人工核对' --confirm
upload-assistant cli rules activate <revision-id> --confirm
upload-assistant cli rules access U2
upload-assistant cli rules access-set U2 \
  --general-interval 10 --general-hourly 120 \
  --search-interval 30 --search-hourly 30 --concurrency 1 --confirm
upload-assistant cli audit list --resource-type downloader --resource-id box
upload-assistant cli operations overview
upload-assistant cli logs list --level error --job-id <job-id>
upload-assistant cli incidents list --status open
upload-assistant cli diagnostics create --provider <provider-id> --incident-id <incident-id>
upload-assistant cli providers probe <provider-id> --stage catalog
upload-assistant cli providers probe <provider-id> --stage inference
upload-assistant cli backups runs
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
5. 用 `/attention` 获取一个当前问题，只执行其明确标记为 executable 的解决方案
6. 在 `/upload-preview` 审核最终发布内容后，再由用户决定是否提交上传确认

Agent 只能把 API 的 `status`、`ok`、`blockers`、`next_actions`、`resume_state`、`job_id` 和 summary 作为决策依据，不得从候选排名、通知送达或配置就绪推断上传许可。

## 开发与验收

```bash
make go-check
UA_POSTGRES_PASSWORD=compose-config-fixture make go-compose-config
make verify-go-v2-local
```

`make go-check` 执行 TypeScript 类型检查、Web 测试/构建、Go 格式/vet/测试和二进制构建。`verify-go-v2-local` 创建全新隔离 Compose 项目和卷，验证 linux/amd64、非 root、只读根文件系统、capability/no-new-privileges、PostgreSQL 无宿主机端口、主密钥权限、MediaInfo/BDInfo/FFmpeg/FFprobe/mkbrr/age/PostgreSQL 17 客户端工具链、健康检查、安全响应头、鉴权、OpenAPI、68 个 AI 工具（运维只暴露只读查询和诊断创建/读取）、带 golden fingerprint 的 31 项适配器能力契约、Agent Skill、中文 Web、CLI、迁移、运维可观测性、加密备份恢复、全部 PostgreSQL store 与完整 retorrent/每日候选 fixture、幂等、重启持久性和安全阻塞的 live 交接，完成后自动清理。

本地与 CI 测试只使用 fixture、`httptest` 和隔离 PostgreSQL，不联系真实 Tracker、下载器或图床。真实 U2→MTEAM、CHD→MTEAM、qBittorrent、MediaInfo/BDInfo/截图/mkbrr 与图床闭环必须由操作者提供合法账号、资源和显式授权后执行；在此之前必须保持为外部验证阻塞，不能伪造完成。

更完整的运维、规则、下载器、迁移和审计说明见 [docs/go-v2.md](docs/go-v2.md)。
