# Go v2 站点规则 Markdown

本目录保存可审查、无凭据的本地站点规则源文档。除本说明外，`*.md` 默认被 Git 忽略，
以免把来自认证页面的规则证据意外提交。文件是导入模板和审查草稿，
不会被 Docker Compose 自动挂载、导入、审批或激活；运行时的不可变规则修订保存在
PostgreSQL 和 `/data/rules` 持久卷中。

不要在这里或 Markdown 正文中保存 Cookie、passkey、API key、账号信息或登录后页面中
只属于账号本人的数据。本地文档若尚未取得完整规则，必须保持
`source.complete: false`、`review.status: draft`，不能通过审批或激活。

## 文档格式

每份新文档使用 `---` 分隔的 YAML front matter，kind 固定为
`upload-assistant.site-rule.v2` 且 `schema_version: 2`，后面是保留审查依据的 Markdown 原文。旧 v1 文档仍可导入审阅，但永远不能授权服务访问站点：

- `site`：站点代码、名称及 `source`/`target` 角色；
- `source`：规则页 URL、采集日期、覆盖范围和正文 SHA-256；
- `automation`：下载、上传、转种能力以及可执行的 `auto_pull`/`auto_upload` 开关；
- `access`：服务/搜索访问许可、普通/搜索最小间隔与小时配额、站点最大并发；
- `limits`、`seeding`、`transfer`：限速、做种和转载硬策略；
- `obligations`：程序可验证或必须人工判断的义务；
- `review`：草稿/审批状态。fingerprint 由服务计算，不要手填或复制旧值。

将站点规则文本复制到 Markdown 正文，并在 `source.scope` 中准确写明已覆盖和未覆盖的
页面。只有完整取得本次自动化所需的现行原文、核对引用并计算正文 hash 后，才可设置
`source.complete: true`。无法可靠程序化判断的条款必须保留为
`verification: manual`、`blocking: true`、`resolution: pending`；不能为了通过 gate 改成
程序已验证。程序门禁已经真实实现并有测试时，才可使用
`verification: programmatic`、`resolution: enforced`。

`auto_pull` 和 `auto_upload` 是运行时硬开关，不是能力说明。在完整审查前保持 `false`。
`automation.download`、`upload` 和 `retorrent` 只描述文档覆盖的能力，不能代替自动执行
授权。

`access.service_access` 和 `access.search_access` 只能是 `allowed`、`forbidden` 或
`undetermined`。只有现行原文明确允许自动化服务访问时才可填 `allowed`；规则禁止自动化
必须填 `forbidden`，找不到明确条款时必须填 `undetermined`。数字字段为规则本身的限制，
没有可靠依据时可为 0，但运行时仍要求人工配置全部正数限频。实际调用始终采用规则与人工
策略中更严格的值。

```yaml
schema_version: 2
kind: upload-assistant.site-rule.v2
access:
  service_access: undetermined
  search_access: undetermined
  general_min_interval_seconds: 0
  general_max_requests_per_hour: 0
  search_min_interval_seconds: 0
  search_max_requests_per_hour: 0
  max_concurrency: 0
```

## 导入、审批与激活

推荐在 Web「站点规则」为同一站点录入 1–20 个有序规则/FAQ HTTPS 地址，确认来源范围与
Cookie host，并先配置正数普通访问策略。显式点击采集后，后台只访问这些精确地址，按普通
间隔、小时配额、cooldown 和并发顺序抓取，把每页规范化为带来源和行号的证据，再交给已
启用 `rule_analysis` 的 Provider 合并为不可变草稿。采集不会审批、激活或授权任务。也可
继续粘贴离线全文、导入已经整理好的 Markdown，或使用原生 Go CLI：

```bash
upload-assistant cli rules import --file data/site-rules/U2.md
# Compose 中无需复制文件进容器：
docker compose exec -T upload-assistant \
  upload-assistant cli rules import --file - < data/site-rules/U2.md
upload-assistant cli rules list U2
upload-assistant cli rules get <revision-id>
upload-assistant cli rules sources U2
upload-assistant cli rules sources-set U2 --file rule-sources.json --confirm
upload-assistant cli rules collect U2 \
  --fingerprint <source-set-sha256> --provider <provider-id> --confirm
upload-assistant cli rules collection <run-id>
upload-assistant cli rules approve <revision-id> \
  --fingerprint <server-computed-sha256> \
  --comment '已核对当前规则原文和结构化策略' --confirm
upload-assistant cli rules activate <revision-id> --confirm
```

stdin 导入同样受 8 MiB decoded UTF-8 上限约束。上例还需要按部署说明通过容器环境或 token
文件提供 `UA_API_TOKEN`，不要把 token 写在命令参数中。CLI 的审批和激活必须显式传入
`--confirm`；它不能绕过服务端的完整性、状态或 fingerprint 检查。也可直接调用 Go v2 API：

```http
POST /api/v2/site-rules/import
Authorization: Bearer <token>
Content-Type: application/json

{"markdown":"---\n...\n---\n\n# 原始规则\n..."}
```

导入只创建不可变草稿，不调用 Tracker。随后读取修订详情、原始 Markdown 和服务端计算的
fingerprint：

```text
GET /api/v2/site-rules/{revision_id}
GET /api/v2/site-rules/{revision_id}/markdown
GET /api/v2/site-rules/{revision_id}/review
```

所有站点只要求人工审核三个可执行硬门禁：`upload_limit`（单种上传限速）、
`download_limit`（单种下载限速）和 `naming`（按分类生成并校验发布标题与内容根名称）。
上传有效值默认从站点声明值减 20 MB/s 并允许人工微调；下载使用站点声明原值。盒子专用
上传限速只在下载器被人工标记为 `seedbox` 时应用。非 `per_torrent` scope、缺失有效值或
未解决的多来源冲突都会阻止审批。其他结构化结论保存在 `advisories`，不增加审核项。

如果同一站点按电影、剧集、动画等资源类型采用不同强制标题格式，应在
`naming.profiles` 中为每种类型配置稳定 `id`、显示名称、模板和完整锚定的 Go/RE2 正则。
目标包必须保存明确的 `target_package.naming_profile`，或由经过验证的媒体类型证据推导；
无法确定 profile 或最终标题不匹配时，任务会在上传前阻塞。

```text
PUT /api/v2/site-rules/{revision_id}/review/{section}
{"fingerprint":"<sha256>","decision":"confirmed|needs_changes","comment":"人工审查依据"}
```

每条决定都绑定当前 fingerprint 并写入全局审计。内容变化会产生新 revision 和 fingerprint，
旧确认不会继承。只有 `source.complete=true`、来源冲突已解决且三个硬门禁都针对当前
fingerprint 标记为 `confirmed` 时，才可提交审批，再单独激活：

```text
POST /api/v2/site-rules/{revision_id}/approve
POST /api/v2/site-rules/{revision_id}/activate
```

站点目录的 aliases 只作为隐藏搜索元数据，tags 用于展示和筛选。任务、规则导入与适配器调用始终
要求规范大写站点代码；新增 `config_only` 站点不会自动获得抓取、查重或上传能力。

导入、审批和激活都不访问站点，也不代表某次任务已经接受规则。任务仍须绑定当前激活修订
的精确 fingerprint，并为所有 blocking manual obligations 提交人工证据；live 上传还必须
在最终查重和上传包可审阅后显式提交 `confirm_upload=true`。

激活 v2 规则也不会直接联网。操作者还必须通过 Web 或
`upload-assistant cli rules access-set SITE ... --confirm` 配置独立访问策略。规则采集是唯一
启动例外：人工确认的 run 可在规则激活前使用已启用的 operator 普通策略访问精确来源 URL，
但不授予其他 adapter operation；其他调用遇到缺少规则、`forbidden`/`undetermined`、配额、
并发或 cooldown 都会在 HTTP 请求前 fail-closed。

legacy TOML `+++` 文档仅为旧数据迁移兼容，不应再作为仓库规则源格式。复制规则正文后先
在本地运行 `go test ./internal/rules -count=1`；完整项目验收使用 `make go-check` 和
`make verify-go-v2-local`，测试不会联系真实站点。
