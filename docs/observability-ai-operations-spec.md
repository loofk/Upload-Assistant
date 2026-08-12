# Upload Assistant 运维可观测性与 AI 诊断 Spec

- Spec ID：`UA-SPEC-OPS-AI-001`
- 状态：`local_ready`
- 目标分支：`codex/new-feature`
- 冻结日期：2026-08-10
- 实现状态：已完成本 spec 的隔离本地实现与验收；机器可读证据见 `tmp/go-v2-local-ready.json`
- 完成日期：2026-08-10

## 1. Goal

在现有 Go/PostgreSQL v2 主线上增加统一运维中心，使操作者能够检索运行日志、关联任务故障、管理异常事件、通过本地或远程大模型生成基于证据的诊断报告，并安全地管理容量告警、API Token 和加密备份。

本阶段完成条件是本地 `LOCAL_READY`，不得把 fixture 或隔离测试解释为真实 Tracker、下载器、图床或 live 上传验证。

可在新对话中使用以下 Goal objective：

> 在 `codex/new-feature` 分支增量实现 `docs/observability-ai-operations-spec.md`：保留现有未提交改动，完成结构化运行日志、请求与任务追踪关联、日志检索/SSE/导出、incident 聚合、兼容本地与远程 OpenAI-compatible 的分级脱敏 AI 诊断、安全白名单修复确认、容量告警、API Token 生命周期、age X25519 加密备份与离线恢复，以及对应 `/api/v2`、OpenAPI、Agent tools、CLI、Web 运维中心、迁移、测试和文档。禁止真实外部调用；以 `make go-check`、隔离 Compose 和备份恢复验收通过并生成 `LOCAL_READY` 结果为完成条件。真实 U2/CHD→MTEAM 验证不属于本 Goal 完成门槛。

## 2. 当前基线与边界

- PostgreSQL 当前迁移到 `0015_system_notifications.sql`；新迁移只追加，不修改既有迁移。
- stdout 已输出基础 JSON 日志，但只包含 method、path、duration 等少量字段，没有可查询存储、HTTP 状态、request ID 或任务关联。
- `audit_events` 用于配置和外部动作审计；`job_events` 是任务级 hash 链。运行日志不能替代或混用这两类审计。
- 任务已有 job、step、attempt、artifact、blocker、attention 和 summary 基础能力。
- API Token 表已有 scope、过期、撤销和最后使用字段，但缺少正常的生命周期 API/Web。
- 当前工作区包含大量前序未提交改动。实现时禁止 reset、checkout 覆盖或清理这些改动；每个阶段只做增量修改。
- 禁止在应用中挂载 Docker socket。容量信息只读取容器可见挂载和 PostgreSQL 统计。

不在本阶段实现：

- 真实 U2/CHD→MTEAM、qBittorrent、图床或模型的联网验收。
- 完整多用户登录、TOTP 或细粒度 RBAC 重构。
- Prometheus、OpenTelemetry、Loki 等外部可观测性栈。
- 新增 Tracker adapter 或任意 Shell/SQL/文件系统修复执行器。

## 3. 运行日志与追踪

### 3.1 日志输出与关联

保留 stdout JSON，同时通过有界异步队列写入 PostgreSQL。数据库不可用、队列已满或服务关停超时时不得阻塞 HTTP 或任务执行；丢弃数量以 `log_sink.dropped` 聚合日志和运维指标暴露。

每条结构化日志至少包含：

- `occurred_at`、`level`、`component`、`message`
- `request_id`、`trace_id`
- `job_id`、`step_key`、`attempt_id`
- HTTP `method`、规范化 `route`、`status_code`、`duration_ms`、`response_bytes`
- `error_code`、有界 `action`、有界 `error_detail`、`actor_type`、`actor_id`
- 经过现有递归脱敏器处理的 `attributes`

HTTP 中间件接受合法 `X-Request-ID`，否则生成新 ID，并始终回写响应头。一个请求产生一个 trace；启动 job attempt 时创建 attempt trace，并把关联 ID 写入该 attempt 下的运行日志和全局审计 `trace_id`。

不得持久化成功的健康检查请求；它们只以 debug 级别输出 stdout。认证头、Cookie、passkey、announce URL、webhook、API key、下载器密码、模型密钥、原始请求/响应正文不得进入日志。

### 3.2 检索、流和保留

日志默认保留 30 天，由数据库中的单实例周期清理任务执行。支持以下组合筛选：时间范围、级别、component、关键词、error code、request/trace/job/attempt ID。

接口：

- `GET /api/v2/operational-logs`
- `GET /api/v2/operational-logs/stream`
- `GET /api/v2/operational-logs/export`

列表只返回轻量标量摘要，不读取 `attributes`；选择某一行后再通过
`GET /api/v2/operational-logs/{log_id}/context` 按需读取完整脱敏属性、同 trace 日志和审计动作。
列表使用 `(occurred_at,id)` 稳定 cursor，不执行精确总数查询，也不使用深 OFFSET。关键词只搜索由
message、错误码、动作、失败说明、方法、路由和 request ID 组成的 trigram 索引字段，不扫描 JSON。
SSE 复用相同轻量摘要和筛选条件，使用单调事件 ID，支持 `Last-Event-ID` 恢复；慢客户端不得拖慢日志写入。导出格式固定为 NDJSON，单次最多 10,000 条，并复用相同过滤条件。

权限：查询和 SSE 使用 `logs:read`，导出使用 `logs:export`。

## 4. Incident

Incident 是对可行动异常的聚合，不是审计事件。状态固定为 `open/acknowledged/resolved`，保留首次/最后发生时间、发生次数、severity、fingerprint、关联 job/trace、最近证据和处置人。

自动创建或合并 incident 的条件：

- step/job 进入 `failed`
- 同一非人工 blocker 在 30 分钟内重复两次
- 同一集成连续两次健康探测失败
- 容量超过阈值
- 备份或完整性校验失败

规则确认、上传确认、人工 obligation、正常查重、等待下载或计划内暂停不创建 incident。

接口：

- `GET /api/v2/incidents`
- `GET /api/v2/incidents/{incident_id}`
- `POST /api/v2/incidents/{incident_id}/acknowledge`
- `POST /api/v2/incidents/{incident_id}/resolve`

确认和解决必须写全局审计，权限为 `operations:manage`；读取权限为 `operations:read`。

## 5. AI 诊断

### 5.1 Provider

首个 provider kind 固定为 `openai_compatible`，支持 Ollama、vLLM 和兼容 OpenAI Chat Completions/Responses 的远程服务。配置包含名称、base URL、model、本地/远程数据等级、API 协议、推理强度、允许用途、JSON mode、`streaming_enabled`、timeout、启用状态和加密 API key 引用。根 base URL 规范化到 `/v1`，显式路径保持不变。推理强度允许 `default/none/minimal/low/medium/high/xhigh/max`；`default` 不向兼容接口发送额外字段，只有 Provider 明确报告的模型能力才进入发现结果。允许用途固定为 `incident_diagnosis` 和 `rule_analysis`，调用方不得越过用途门禁。

`streaming_enabled=true` 时，规则分析、诊断、诊断追问和推理探测均使用 SSE，并记录 response headers、首事件、末事件、最大事件间隔、事件数和完成标记。这样长推理可持续穿过支持流式传输的上游网关；本地 600 秒超时仍不覆盖上游自己的限制。成功 SSE 必须逐事件增量解析并以常量内存累计响应字节数和 SHA-256，不得为了证据记录缓冲全部 reasoning 事件；规范化最终输出、成功整包响应均受 8 MiB 上限保护，失败整包响应仍最多读取 1 MiB，单个聚合 SSE event 也不得超过 1 MiB。只有协议明确正常结束的结果才可使用；token 上限截断、非正常 finish 状态和断流一律失败，且不得进入 JSON 修补。关闭该开关时才使用整包响应。对于上游 SSE 明确返回且判定为瞬态的错误事件，或 SSE 建立前返回的 HTTP 502/503/504/520–524 瞬态网关错误，运行时可在同一个总超时预算内执行一次同协议流式重试，并在两次日志中记录 attempt；认证、权限、配额、限流、上下文或输出上限等永久错误不得重试。不得把流式失败自动改成非流式调用，以免掩盖传输问题。

- 远程 endpoint 必须为 HTTPS。
- 本地 endpoint 仅允许 loopback、私网或明确的 Compose 服务地址。
- 禁止云元数据地址、重定向到被禁止地址和凭据出现在 URL。
- Probe 必须由操作者显式触发；保存配置不自动联网。
- Provider 持久显示 `unknown/catalog_ready/ready/failed` 状态。`stage=catalog` 只验证 `/models` 和精确模型 ID，`stage=inference` 使用与生产相同的 Chat Completions/Responses、JSON mode、推理强度、流式解析、正常结束校验和超时路径执行一次 token 有界契约推理；只有后者可设置 `ready`。探测持久化最后时间、阶段、有效路径、HTTP/content type、响应 hash/shape、trace、延迟或稳定错误码，不保存响应正文；任何配置修改都会重置为 `unknown`。
- Provider 管理权限为 `llm:manage`。

接口：

- `GET /api/v2/llm-providers`
- `PUT /api/v2/llm-providers/{provider_id}`
- `POST /api/v2/llm-providers/{provider_id}/probe`

### 5.2 证据快照与隐私

每次分析先生成不可变、最大 64 KiB 的证据快照，计算 SHA-256，并记录被截断字段和省略数量。快照只允许包含：

- job attention、blocker、step attempt 摘要
- 有限数量的 job events 和关联 operational logs
- artifact 元数据与 hash，不含 artifact 正文
- 集成健康状态、服务版本和迁移版本

远程 provider 永久移除资源标题、文件名、文件路径、描述正文和原始请求/响应体。本地 provider 可由管理员显式允许更多仍经过脱敏的业务上下文。所有 secret 在两种模式下都必须排除。

Tracker/下载器返回值和日志内容全部视为不可信数据，必须与 system 指令分隔。请求不得提供 tools/functions。

### 5.3 诊断生命周期

诊断状态固定为 `queued/running/failed/complete/cancelled`。每个 provider 每小时最多 5 次；同一 evidence hash、provider、prompt 版本和 provider 执行契约的并发请求必须去重。排队时保存不含密钥的 endpoint/model/protocol/data-boundary/credential-version 指纹，执行前再次核对；配置漂移时不发送证据并以 `provider_configuration_changed` 失败。服务重启时遗留的 `running` 诊断和追问转为明确失败，不自动重复可能已经计费的推理。进程级入口限制总在途/排队数、全局并发和单 Provider 并发，容量满时返回 `provider_busy`。

严格结果结构：

- `summary`
- `severity`
- `confidence`
- `possible_causes[]`
- `evidence_refs[]`
- `recommendations[]`
- `risks[]`
- `limitations[]`

不存在于证据快照的 evidence ref 使结果校验失败。数据库保存验证并脱敏后的结果、响应 hash、token 使用量和延迟，不保存原始模型响应。诊断默认保留 90 天。

支持在同一证据快照上进行有上限的追问；系统状态变化后必须创建新诊断，旧报告不可变。诊断失败不得改变 job 或 incident 状态。

接口：

- `POST /api/v2/diagnostics`
- `GET /api/v2/diagnostics`
- `GET /api/v2/diagnostics/{diagnostic_id}`
- `POST /api/v2/diagnostics/{diagnostic_id}/messages`

读取使用 `diagnostics:read`，触发和追问使用 `diagnostics:run`。

### 5.4 站点规则原文分析

`POST /api/v2/site-rules/analyze` 接收操作者显式选择的 Provider、站点显示名称、来源 URL/范围、完整性声明和最多 8 MiB 的规则或常见问题原文。只有启用 `rule_analysis` 用途的 Provider 可以接收请求。超出单次模型上下文预算的原文按证据行拆分，依序结构化后由本地代码保守合并；所有分段及必要的语法修复共享 Provider 配置的一份总超时，不把每个分段各算 600 秒。跨段限速或命名冲突必须保留为冲突并阻止形成可执行硬门禁。Web 使用 `POST /api/v2/site-rules/analyze/stream`：响应建立后立即 flush，并每 10 秒发送一次不含正文的进度心跳，避免浏览器到服务之间的代理因长时间无响应字节而断开或重放请求。该接口要求 `Idempotency-Key`；相同操作者、相同键、相同输入在 10 分钟有界内存窗口内只触发一次 Provider 调用，键被用于不同输入时返回冲突。协调器和模型入口均有容量上限；满载返回 `provider_busy`。若中间层实施与心跳无关的固定总时长限制，浏览器断流后改用带相同 `Idempotency-Key` 的 `GET /api/v2/site-rules/analyze/result` 短轮询；202 表示原调用仍在执行，完成后返回相同结果或结构化错误，轮询绝不发起新的 Provider 请求。

模型只返回结构化提取结果，服务端固定 `schema_version=2`、站点身份、来源 SHA-256、`review.status=draft`、`manual_review_required=true`、`auto_pull=false` 和 `auto_upload=false`。单种上传限速、单种下载限速和可确定生成校验的分类命名进入三项人工审核硬门禁；其余提取内容和 Provider 返回的 obligation 均归一化为有界 `advisories`，在转种前提示。若最终内容只有围栏或前后说明，服务端先确定性提取 JSON 对象；仍有语法错误时最多执行一次同 Provider、有界且禁止增删事实的 JSON 语法修复，并向审核人显示警告。生成的 Markdown 必须先通过权威规则解析器和来源 hash 校验才返回 Web；原文和原始模型响应不写运行日志。接口只返回可编辑草稿，不导入、不审批、不激活、不访问 Tracker，也不授权上传。

Provider 调用失败必须返回可检索的稳定错误码；超时使用 `provider_timeout` 和 HTTP 504，上游 SSE 错误事件使用 `provider_stream_error`，容量满使用 `provider_busy` 和 HTTP 503，配置漂移使用 `provider_configuration_changed` 和 HTTP 409，不得压缩为通用 502。HTTP 运行日志保存脱敏的失败说明、Provider 路径、协议、模型、推理强度、超时、attempt、响应头/总延迟、上游状态及响应 hash/shape。推理请求日志只保存字段、角色和 content 字节数等结构元数据，响应日志只保存结构、输出字节数/哈希和 token usage；规则原文、诊断证据、模型输出、Authorization、Cookie、API key 与推理过程均不得进入预览。错误事件另存最多 2000 字符、递归脱敏的 event type/code/message 和 retryable 判定；未完整读取的响应只保留 captured-prefix hash，不能声称拥有完整响应 hash。失败分析同时写入 `site_rule.ai_analyze_failed` 审计。Web 在触发位置展示失败原因与恢复动作，并允许从日志展开完整 request/trace ID。

### 5.5 自动触发与修复边界

默认支持人工触发；管理员可针对 incident 类型开启自动诊断。远程 provider 的自动外发必须另行启用明确的 outbound consent。

模型输出永远只是建议。服务端忽略任何 shell、SQL、URL 调用或自由文本“执行”声明。只有同时满足以下条件的建议才能显示“同意修复”：

1. 建议可映射到服务端已实现的安全动作；
2. 该动作存在于目标对象最新 `/attention` 响应；
3. 目标状态、attempt 和证据 hash 未发生变化；
4. 当前操作者拥有动作原本要求的 scope；
5. 操作者再次显式确认。

本阶段不新增通用修复执行器。规则 gate、查重、`accept_rules`、`confirm_upload`、做种要求和未知外部写入 reconciliation 均不得被 AI 绕过。

## 6. 运维基础

### 6.1 运维总览与容量告警

`GET /api/v2/operations/overview` 返回：

- `/data`、`/downloads`、`/backups` 的容量和使用率
- PostgreSQL 数据库及主要表大小
- 30 天日志增长
- queued job/notification 数量及最老等待时间
- open incident、最近失败、最近备份和应用版本

默认阈值：文件系统 80% warning、90% critical；数据库预算 10 GiB；队列超过 20 条或最老等待超过 15 分钟告警。恢复阈值使用 5 个百分点迟滞，同一 fingerprint 的通知冷却时间为 1 小时。通知复用 Telegram、企业微信、飞书和 Discord。

配置接口：`GET/PUT /api/v2/operations/settings`，写入必须审计。

### 6.2 API Token 生命周期

新增 scopes：

- `logs:read`、`logs:export`
- `diagnostics:read`、`diagnostics:run`
- `operations:read`、`operations:manage`
- `llm:manage`
- `backups:read`、`backups:manage`
- `tokens:manage`

接口：

- `GET /api/v2/api-tokens`
- `POST /api/v2/api-tokens`
- `DELETE /api/v2/api-tokens/{token_id}`

新 token 默认 30 天、最长 365 天，只显示一次明文。调用者不能签发超出自身 role 和 token scopes 的权限。列表只显示 prefix、name、scopes、created/expires/last-used/revoked。撤销立即生效并写审计。

保留本地主机 `admin token issue` 恢复路径。本阶段不实现 Web 密码登录、TOTP 或完整用户管理。

### 6.3 加密备份与恢复

Compose 增加独立 `/backups` 持久挂载。备份内容：

- PostgreSQL logical backup
- 服务 master key 文件
- 本地站点规则文档
- artifact 和 summary 元数据及必要文件

排除 `/downloads`、legacy 只读源、tmp 和既有备份目录。

使用 age X25519 加密。服务只保存 public recipient；private identity 生成后只显示一次，由操作者离线保管。配置后默认每天 03:30 备份，保留最近 7 份成功结果。备份期间进入只读维护窗口；存在不可安全中断的写任务时延期，不强制终止任务。

Web/API 允许管理 policy、查看 run、手动创建和校验：

- `GET/PUT /api/v2/backups/policy`
- `GET /api/v2/backups/runs`
- `POST /api/v2/backups`
- `POST /api/v2/backups/{backup_id}/verify`

恢复只能使用停服后的管理员 CLI。CLI 必须校验 bundle SHA、manifest、应用版本和内部文件 hash，在临时数据库和 staging 目录完成迁移及完整性检查，成功后再切换，并保留可回滚的原状态。服务运行中必须拒绝恢复。Web、Agent tool 和 AI 均不提供 restore。

镜像需包含与 PostgreSQL 17 匹配的 `pg_dump`/`pg_restore` 和 age 工具。

## 7. Web、OpenAPI 与 Agent

Web 新增单一“运维中心”，内部使用二级导航：

- 总览
- 异常事件
- 运行日志
- AI 诊断
- 备份
- API Token

正文、表格和输入控件字体不得低于 13px。job、attempt、incident、日志和诊断报告应互相提供上下文跳转。失败任务默认先展示最终问题、建议处理和“重试/同意修复”；完整步骤和原始审计折叠显示。

所有新增 API 必须同步更新：

- `/openapi.json`
- `/api/v2/tools`
- Go CLI
- Web API types/client
- 内嵌 Web build
- 两份 Upload Assistant Skill 文档

Agent tools 只暴露运维总览、日志查询、incident 查询和诊断创建/读取。不得暴露 token 创建/撤销、provider secret 修改、备份恢复或任意执行接口。

响应继续使用 AI 友好短路径字段：`ok`、`status`、`blockers`、`next_actions`、关联 ID、`summary` 和 `resume_state`。

## 8. 实施顺序

1. 请求/trace/job/attempt 关联和数据库迁移。
2. 异步日志 sink、查询、SSE、导出和保留策略。
3. Incident 聚合、确认、解决和通知。
4. LLM provider、证据快照、诊断队列、结果校验和追问。
5. 容量总览、阈值、API Token 生命周期。
6. age 加密备份、完整性校验和离线恢复 CLI。
7. Web 运维中心、OpenAPI、tools、CLI、Skill 和文档。
8. 全量本地验收和 `LOCAL_READY` 报告。

每一步都必须保持 `make go-check` 可恢复通过。不得等到最后才补脱敏、权限、审计或测试。

## 9. 测试与验收

### 9.1 自动测试

- 日志：递归脱敏、HTTP status/bytes、队列满、数据库失败、关联 ID、组合过滤、保留清理、SSE 续传和慢客户端。
- Incident：fingerprint 合并、误报排除、ack/resolve、并发和通知冷却。
- AI：本地/远程隐私差异、无 tools、提示注入、SSRF、redirect、timeout、quota、去重、schema/evidence 校验、追问上限和动作状态重验。
- Token：scope 子集、默认/最大过期、一次性明文、last-used、撤销即时生效和审计。
- Capacity：80/90 阈值、5% 迟滞、队列年龄和通知冷却。
- Backup：加密、manifest/hash、保留策略、活跃任务延期、错误密钥、损坏 bundle、版本不兼容和运行中拒绝恢复。

### 9.2 本地验收

1. 执行 `make go-check`。
2. 在隔离 PostgreSQL 中运行全部集成测试。
3. 执行 Compose 契约测试。
4. 在隔离 Compose 中完成“创建加密备份 → 修改数据 → 停服 → 离线恢复 → 校验数据库/规则/key/artifact → 重启服务”。
5. 构造失败任务，验证“关联日志 → incident → AI 报告 → 同证据追问 → 仅确认当前 attention 白名单动作”的完整链路。
6. 验证容量告警、通知投递记录、token 撤销和备份完整性状态。
7. 更新本地 verifier，生成机器可读结果，要求：
   - `ok=true`
   - `status=local_ready`
   - `blockers=[]`
   - `external_calls_performed=false`

测试必须使用 fixture、`.invalid` endpoint 或 `httptest`。未经用户对具体外部动作再次授权，不得访问真实 Tracker、下载器、图床、通知平台或模型。

## 10. Goal 完成语义

本 spec 的 Goal 在以上本地验收全部通过时可以完成。原长期项目 Goal 仍可能因真实账号、合法资源和 live 上传确认缺失而处于 `blocked_external`，二者不能混淆，也不得以本地报告伪造 live 闭环完成。
