# 本地站点规则文档

这个目录保存从站点规则页复制并人工核对的私有规则证据。`TRACKER.md` 和编译后的
`site-policies.generated.json` 默认被 Git 忽略；不要提交登录后页面内容、Cookie、passkey、
API key 或任何账号信息。

每个 `TRACKER.md` 由 TOML front matter 和 Markdown 正文组成：

- `# 原始规则`：保留本次审查所依据的原文或明确标注范围的摘录；
- `automation`、`qbit_limits`、`seeding_requirements`、`transfer_rules`：可执行策略；
- `[[obligations]]`：无法可靠程序化判断的人工义务；
- `source_complete=false` 或 blocking obligation 为 `pending` 时，禁止编译运行时快照；
- `review_status=approved` 只能由显式人工审查命令生成，不要手填 fingerprint。

推荐流程：

```bash
python3 ptcli.py site-rule-docs --json
python3 ptcli.py site-rule-validate --file data/site-rules/U2.md --json
python3 ptcli.py site-rule-review \
  --file data/site-rules/U2.md \
  --approve --reviewer YOUR_NAME --reviewed-at 2026-08-08T12:00:00+08:00 \
  --json
# 确认 preview 后才持久化
python3 ptcli.py site-rule-review \
  --file data/site-rules/U2.md \
  --approve --reviewer YOUR_NAME --reviewed-at 2026-08-08T12:00:00+08:00 \
  --write --json
python3 ptcli.py site-rule-compile \
  --rules-dir data/site-rules \
  --output data/site-rules/site-policies.generated.json \
  --write --json
```

Docker Compose 将宿主机 `PTCLI_SITE_RULES_HOST_PATH` 挂载到容器内
`/Upload-Assistant/data/site-rules`。API 也提供只读列举/校验和带双重显式确认的审查、编译接口；
这些接口不访问 tracker、不执行下载或上传，也不能代替人工规则判断。
