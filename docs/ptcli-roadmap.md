# Focused PT CLI Roadmap

目标是把 Upload Assistant 收束成一个适合盒子部署的 PT 转种 CLI，同时保留每个站点适配器里的站点特定规则校验。

## Scope

- 只面向 `src.ptcli.mainland.MAINLAND_PT_TRACKERS` allowlist 内的 PT 站点。
- 入口从 `ptcli.py` 开始，旧 `upload.py` 作为迁移期兼容入口保留。
- 所有真实上传、下载、QB 注入都必须经过 dry-run 可审计计划。
- 非 dry-run 模式必须显式传入 `--accept-rules`，表示用户已确认源站和目标站规则。

## CLI Shape

```bash
python3 ptcli.py sites
python3 ptcli.py rules --trackers MTEAM,TJUPT --json
python3 ptcli.py source-info --tracker U2 --source-id 60635 --json
python3 ptcli.py source-download --tracker CHD --source-id 12345 --output-dir ./tmp/source --json
python3 ptcli.py flow-check --from U2 --source-id 60635 --to MTEAM --json
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --path /downloads/movie --package-dir ./tmp/target/U2-60635-to-MTEAM --target-torrent-file ./tmp/exported/mteam.torrent --accept-rules --target-execute --confirm-upload --download-uploaded-torrent --inject-uploaded-torrent --uploaded-save-path /downloads/movie --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path /downloads/movie --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --output-dir ./tmp/source --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --inject-source --save-path /downloads --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --inject-source --save-path /downloads --wait-complete --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path /downloads/movie --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path /downloads/movie --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --upload-target --target-torrent-file ./tmp/exported/mteam.torrent --target-execute --confirm-upload --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-save-path /downloads/movie --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --json
python3 ptcli.py target-upload --package-dir ./tmp/target/U2-60635-to-MTEAM --torrent-file ./tmp/exported/mteam.torrent --write-payload --json
python3 ptcli.py target-upload --config data/config.py --package-dir ./tmp/target/U2-60635-to-MTEAM --torrent-file ./tmp/exported/mteam.torrent --execute --confirm-upload --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-save-path /downloads/movie --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --json
python3 ptcli.py inspect --client default --limit 20 --json
python3 ptcli.py match --path /downloads/movie --json
python3 ptcli.py export --hash "<infohash>" --output-dir ./tmp/exported --json
python3 ptcli.py retorrent --from MTEAM --source-id 12345 --to TJUPT,CHD --path /downloads/movie --dry-run --json
```

后续子命令建议：

- `inspect`: 从源站 torrent id 或 QB hash 读取元数据，不上传。
- `match`: 在 qBittorrent 中匹配本地内容和已有种子。
- `prepare`: 生成目标站描述、截图、种子和规则检查报告。
- `upload`: 只上传通过规则检查和查重的目标站。
- `inject`: 把上传后的种子注入 qBittorrent 做种。

## Rule Compliance

- 站点规则不硬编码为猜测；每个 tracker 适配器负责自己的上传/下载字段、分类、匿名、免费、转载、禁转、截图和描述限制。
- CLI 只做统一前置检查：站点 allowlist、源/目标不重复、非 dry-run 必须确认规则、执行计划可导出。
- 后续应增加 per-site rule profile，记录来源、更新时间和关键限制，避免 AI 或自动化流程凭空推断规则。

## AI-Friendly Output

- 所有关键命令支持 `--json`。
- JSON 输出保持稳定字段名，供外部 agent/自动化脚本消费。
- dry-run 输出应包含计划步骤、阻断原因、目标站状态和下一步建议。

## Migration Steps

1. 新增 `ptcli.py` 和 allowlist，只生成计划，不做真实网络操作。
2. 接入 qBittorrent 只读能力：列出、匹配、导出已有种子。
3. 接入源站 metadata/torrent 下载能力，默认 dry-run。
4. 接入目标站 prepare/check/upload，逐站开启。
5. 将 Web UI、Discord、海外 tracker 和非转种路径移到 legacy 或删除。

当前已完成：

- `sites`: 输出 allowlist。
- `rules`: 输出站点规则审查 profile，不臆造具体规则。
- `source-info`: 首批支持 `U2` / `CHD` / `MTEAM` 的源站详情读取。
- `source-download`: 首批支持 `U2` / `CHD` / `MTEAM` 的源种下载。
- `flow-check`: 本地检查 U2/CHD → MTEAM 参考流所需配置、cookie 和 qBittorrent client。
- `doctor`: live 前 checklist，检查 flow/config/cookie/qBittorrent、路径、目标站准备包、MTEAM upload gate、确认参数和后续下载/注入条件。
- `pipeline`: 串联 `flow-check`、`source-info`、可选 `source-download`、可选 `inject-source`、可选 `wait-complete`、可选 `match`、可选 `target-dupe-check`、可选 `target-prepare` 和可选 `target-upload`；默认不下载、不注入、不等待、不上传。
- `target-prepare`: 目前生成 MTEAM dry-run preview、meta draft、field mapping、description draft 和 upload gate 文件，不调用上传接口。
- `target-dupe-check`: 可选调用 MTEAM API 按 IMDb 查重；没有 IMDb ID 时明确阻断。
- `target-upload`: 读取 MTEAM 准备包并执行上传预检，输出/写入 MTEAM multipart payload 摘要；只有 `--execute --confirm-upload` 且 upload gate/payload 全部 ready 时才调用 MTEAM API，并可下载上传后生成的目标站 `.torrent`、显式注入 qBittorrent 做种。
- `retorrent`: 生成可审计转种计划，并输出可逐步执行的 JSON command plan。
- `inspect`: 只读列出 qBittorrent 任务。
- `match`: 按盒子路径匹配 qBittorrent 任务。
- `export`: 从 qBittorrent 只读导出已有 `.torrent` 到指定目录。
