# Go v2 站点规则 Markdown

本目录保存可审查、无凭据的本地站点规则源文档。除本说明外，`*.md` 默认被 Git 忽略，
以免把来自认证页面的规则证据意外提交。文件是导入模板和审查草稿，
不会被 Docker Compose 自动挂载、导入、审批或激活；运行时的不可变规则修订保存在
PostgreSQL 和 `/data/rules` 持久卷中。

不要在这里或 Markdown 正文中保存 Cookie、passkey、API key、账号信息或登录后页面中
只属于账号本人的数据。本地文档若尚未取得完整规则，必须保持
`source.complete: false`、`review.status: draft`，不能通过审批或激活。

## 文档格式

每份文档使用 `---` 分隔的 YAML front matter，kind 固定为
`upload-assistant.site-rule.v1`，后面是保留审查依据的 Markdown 原文：

- `site`：站点代码、名称及 `source`/`target` 角色；
- `source`：规则页 URL、采集日期、覆盖范围和正文 SHA-256；
- `automation`：下载、上传、转种能力以及可执行的 `auto_pull`/`auto_upload` 开关；
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

## 导入、审批与激活

可在 Web「规则中心」粘贴整份 Markdown，或使用原生 Go CLI：

```bash
upload-assistant cli rules import --file data/site-rules/U2.md
# Compose 中无需复制文件进容器：
docker compose exec -T upload-assistant \
  upload-assistant cli rules import --file - < data/site-rules/U2.md
upload-assistant cli rules list U2
upload-assistant cli rules get <revision-id>
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
```

只有 `source.complete=true`、结构化策略与正文都已人工审查时，才提交精确 fingerprint
进行审批，再单独激活：

```text
POST /api/v2/site-rules/{revision_id}/approve
POST /api/v2/site-rules/{revision_id}/activate
```

导入、审批和激活都不访问站点，也不代表某次任务已经接受规则。任务仍须绑定当前激活修订
的精确 fingerprint，并为所有 blocking manual obligations 提交人工证据；live 上传还必须
在最终查重和上传包可审阅后显式提交 `confirm_upload=true`。

legacy TOML `+++` 文档仅为旧数据迁移兼容，不应再作为仓库规则源格式。复制规则正文后先
在本地运行 `go test ./internal/rules -count=1`；完整项目验收使用 `make go-check` 和
`make verify-go-v2-local`，测试不会联系真实站点。
