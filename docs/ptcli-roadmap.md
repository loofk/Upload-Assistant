# Focused PT CLI Roadmap

目标是把 Upload Assistant 收束成一个适合盒子部署的 PT 转种 CLI，同时保留每个站点适配器里的站点特定规则校验。

## Scope

- 只面向 `src.ptcli.mainland.MAINLAND_PT_TRACKERS` allowlist 内的 PT 站点。
- 入口从 `ptcli.py` 开始，旧 `upload.py` 作为迁移期兼容入口保留。
- 所有真实上传、下载、QB 注入都必须经过 dry-run 可审计计划。
- 非 dry-run 模式必须显式传入 `--accept-rules`，表示用户已确认源站和目标站规则。

## Delivery Priority

继续推进时按“真实关键闭环优先，机器可恢复次之，扩站和瘦身后置”的顺序交付。判断下一步是否该做，优先看它是否能让 U2/CHD -> MTEAM 的真实转种少一个人工断点；如果不能，就放到 P1/P2/P3。

### Critical Path Order

P0 内部也要严格按下面顺序推进，避免先优化边缘体验而漏掉 live 转种必需材料：

1. 源站和 qBittorrent 闭环：读取 U2/CHD 详情、下载源种、注入 QB、等待完成、从 QB 结果推导内容路径，并让 summary 能恢复到同一条源站任务。
2. 目标站材料闭环：基于内容路径和源站信息生成 MTEAM 必需材料，包括 IMDb/TMDb、豆瓣/PTGen、MediaInfo/BDInfo、视频截图、图床 URL、描述草稿和转载来源说明。
3. MTEAM 上传闭环：查重、规则确认、字段映射、目标候选种子净化、安全 gate、live upload、下载 MTEAM 新种、注入 QB 并等待做种。
4. 自动化闭环：把所有失败收敛到 blockers、next_actions、resume_commands、summary-check 和 shell/JSON 字段，保证盒子脚本或 AI agent 可以安全续跑。
5. 体验和瘦身：只有 P0/P1 稳定后，再做命令别名、输出美化、扩站、Docker 精简和 legacy 删除。

### P0: U2/CHD -> MTEAM live closure

第一阶段只服务真实闭环，不扩散目标站和 UI 范围。验收标准是同一条命令或可恢复 summary 能证明：

- 源站详情读取、源种下载、qBittorrent 注入和等待完成可审计。
- 从源站详情或外部元数据补齐 IMDb、TMDb 和豆瓣 ID；没有 ID 时必须在 blockers 中说明缺口，不能静默生成低质量描述。
- 通过 IMDb/TMDb 获取片名、年份、类型、剧集/电影类型和外部链接；当 TMDb API key 缺失或查询失败时，summary 必须给出可恢复的 metadata blocker。
- 获取豆瓣 ID/URL 和 PTGen/豆瓣简介；MTEAM 描述缺少豆瓣/PTGen 内容时不能进入 live upload ready。
- 对本地内容生成 MediaInfo/BDInfo；影片类内容必须支持视频截图并上传图床，图床失败或只生成本地图片但没有可用 URL 时 live upload 不应继续。
- 基于 IMDb/TMDb/豆瓣生成 MTEAM 所需描述，包括 PTGen/豆瓣简介、IMDb/TMDb/Douban 外部链接、MediaInfo/BDInfo、截图 BBCode、图床 URL 和转载来源信息。
- MTEAM 查重、规则 obligation、字段映射、目标种子安全净化和 live upload gate 全部 ready。
- 上传成功后下载 MTEAM 新种，注入 qBittorrent 做种，并在 summary/evidence 中暴露 hash、路径、规则确认和可恢复命令。

P0 的实现不以“命令能跑完”为唯一标准，而以材料和规则 gate 是否足够硬为标准。只要缺少截图图床、IMDb/TMDb、豆瓣/PTGen、MediaInfo/BDInfo 或站规确认中的任一项，live upload 必须返回 blocked，并给出下一条可执行恢复命令或明确的人工动作。

### P1: automation hardening

第二阶段只围绕盒子长期运行补强，不增加新站点复杂度：

- `doctor` / `summary-check` 对运行时依赖、cookie/API、qBittorrent、图床、外部元数据服务给出机器可读诊断。
- 所有失败都能落到 `blockers`、`next_actions`、`resume_commands` 和稳定 JSON 字段。
- 对材料链单独暴露诊断字段，例如 metadata 是否含 IMDb/TMDb/豆瓣、MediaInfo/BDInfo 是否存在、截图数量、图床 URL 有效数量、描述是否 ready。
- focused Docker/requirements 只包含 ptcli 闭环必需依赖，避免旧 Web UI/Discord 依赖泄漏到盒子 CLI。

### P2: more mainland PT flows

第三阶段再扩展中文 PT 站点：

- 先扩展源站详情/源种下载，再扩展目标站 prepare/upload。
- 每个站点必须有 rule profile、credential requirements、支持范围和 live 前人工确认边界。
- MTEAM 以外目标站只有在分类、描述、截图、种子、安全字段和上传后做种都能闭环时才标记 live ready。

### P3: legacy slimming

最后再精简旧代码：

- Web UI、Discord、海外 tracker 和非转种路径保留在 legacy 入口，待 P0/P1 稳定后再迁移、隔离或删除。
- 删除前必须确认 focused `ptcli.py` 不再依赖对应 legacy 模块。

## CLI Shape

```bash
python3 ptcli.py sites
python3 ptcli.py rules --trackers MTEAM,TJUPT --json
python3 ptcli.py source-info --tracker U2 --source-id 60635 --json
python3 ptcli.py source-download --tracker CHD --source-id 12345 --to MTEAM --output-dir ./tmp/source --accept-rules --json
python3 ptcli.py flow-check --from U2 --source-id 60635 --to MTEAM --json
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --path /downloads/movie --package-dir ./tmp/target/U2-60635-to-MTEAM --target-torrent-file ./tmp/exported/mteam.torrent --accept-rules --target-execute --confirm-upload --json
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --connect-qbit --json
python3 ptcli.py doctor --from U2 --source-id 60635 --to MTEAM --probe-source --probe-target --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path /downloads/movie --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --output-dir ./tmp/source --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --inject-source --save-path /downloads --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --download-source --inject-source --save-path /downloads --wait-complete --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path /downloads/movie --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --upload-target --target-torrent-output-dir ./tmp/exported --target-execute --confirm-upload --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --save-path /downloads --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --upload-target --target-torrent-output-dir ./tmp/exported --target-execute --confirm-upload --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path /downloads/movie --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --json
python3 ptcli.py pipeline --from U2 --source-id 60635 --to MTEAM --path /downloads/movie --check-dupes --prepare-target --target-output-dir ./tmp/target --accept-rules --upload-target --target-torrent-file ./tmp/exported/mteam.torrent --target-execute --confirm-upload --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --write-summary --json
python3 ptcli.py target-upload --package-dir ./tmp/target/U2-60635-to-MTEAM --torrent-file ./tmp/exported/mteam.torrent --write-payload --json
python3 ptcli.py target-upload --config data/config.py --package-dir ./tmp/target/U2-60635-to-MTEAM --torrent-file ./tmp/exported/mteam.torrent --execute --confirm-upload --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-save-path /downloads/movie --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json
python3 ptcli.py target-upload --config data/config.py --package-dir ./tmp/target/U2-60635-to-MTEAM --uploaded-torrent-id 999 --download-uploaded-torrent --uploaded-output-dir ./tmp/uploaded --inject-uploaded-torrent --uploaded-save-path /downloads/movie --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json
python3 ptcli.py target-upload --config data/config.py --package-dir ./tmp/target/U2-60635-to-MTEAM --uploaded-torrent-file ./tmp/uploaded/MTEAM-999.torrent --inject-uploaded-torrent --uploaded-save-path /downloads/movie --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --wait-uploaded-complete --write-summary --json
python3 ptcli.py inspect --client default --limit 20 --json
python3 ptcli.py match --path /downloads/movie --json
python3 ptcli.py export --hash "<infohash>" --output-dir ./tmp/exported --json
python3 ptcli.py retorrent --from MTEAM --source-id 12345 --to TJUPT,CHD --path /downloads/movie --dry-run --json
python3 ptcli.py retorrent --from U2 --source-id 60635 --to MTEAM --execute --accept-rules --confirm-upload --save-path /downloads --target-torrent-file ./tmp/exported/mteam.torrent --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --json
python3 ptcli.py retorrent --from U2 --source-id 60635 --to MTEAM --execute --accept-rules --confirm-upload --save-path /downloads --uploaded-qbit-category MTEAM --uploaded-qbit-tags retorrent --json
```

`retorrent --execute` 是高层闭环命令，会默认执行上传后目标站种子下载、qBittorrent 注入和完成等待，并默认写出 `ptcli-run-summary.json`；未提供 `--target-torrent-file` 时，会默认从匹配到的 qBittorrent 任务导出目标候选种子。`pipeline --target-execute` 现在也会按 live 闭环语义自动补齐源站下载/注入/等待、目标 torrent 导出/净化，以及上传后 MTEAM 新种下载/注入/等待；仍可保留显式参数，便于拆分排障和人工复核。顶层 JSON 只有在 `closure.complete=true` 且 pipeline ready 时才返回 `status: complete`；否则返回 `status: blocked` 和 blockers，并以非 0 退出码结束。source 侧闭环可由“源种下载 + QB 注入 + 等待完成”证明，也可由“已有 `--path` 能匹配 QB 任务”证明。带执行动作的 `pipeline` 未 ready 时返回 `status: blocked`、顶层 blockers 和非 0 退出码，并在 `closure`/`evidence` 汇总 source/target 的 hash、路径、闭环模式和完整 torrent 文件证据；`--write-summary` 会把 `closure`、`summary`、`evidence`、`artifacts`、`resume_commands` 和 next actions 写入 `ptcli-run-summary.json`。run summary 的 `summary.compliance` 会明确暴露规则确认状态、站点具体规则是否已编码、实际程序化策略检查范围和每个 source/target obligation；当前 `site_specific_rules_encoded=false`，live automation 前仍必须人工审阅源站和目标站规则。`retorrent --execute` 也会把该 evidence、artifacts 和 resume commands 提升到顶层。`doctor --target-execute` 会按 live pipeline 默认 follow-up 检查，只有 ready 且 `live_safe_to_attempt=true` 才返回 0；如果 MTEAM 上传已成功但只拿到了新种 ID，可用 `doctor --uploaded-torrent-id` 诊断并生成 `resume-uploaded-torrent-download` 命令。`target-upload --execute`、`target-upload --uploaded-torrent-id` 或 `target-upload --uploaded-torrent-file` follow-up 只有完成请求的下载、注入和等待动作时才返回 0；target-upload summary 会包含 `uploaded_torrent_id` 以及 `uploaded_torrent` 的 `exists`、`size_bytes`、`sha1`、`torrent_hash`/`infohash` 文件证据。

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
4. 优先打通 U2/CHD -> MTEAM live closure，包括源种下载、QB 下载/匹配、IMDb/TMDb/豆瓣补全、MediaInfo/BDInfo、截图上传图床、MTEAM 描述、查重、规则门禁、上传、新种下载和 QB 做种。
5. 补强盒子自动化：runtime doctor、summary-check、恢复命令、focused Docker/requirements 和 AI 友好 JSON。
6. 扩展更多中文 PT 源站与目标站，每个站点先完成 rule profile 和凭据诊断，再开启 live upload。
7. 将 Web UI、Discord、海外 tracker 和非转种路径移到 legacy 或删除。

当前已完成：

- `sites`: 输出 allowlist、能力矩阵、源站详情/下载适配器类型和所需凭据提示。
- `rules`: 输出站点规则审查 profile，不臆造具体规则。
- `source-info`: 支持 allowlist 内已启用的中文源站详情读取，MTEAM 走 API，其余已启用源站优先走 cookie 详情页解析。
- `source-download`: 支持 allowlist 内已启用的中文源站源种下载，包括 NexusPHP passkey 下载、TTG announce/passkey 下载、HDS cookie 下载和 MTEAM API 下载。
- `flow-check`: 本地检查已启用 source → MTEAM 流所需配置、cookie/API key 和 qBittorrent client。
- `doctor`: live 前 checklist，检查 flow/config/cookie/qBittorrent、路径、目标站准备包、MTEAM upload gate、确认参数和后续下载/注入/等待条件；`--target-execute` 会自动按 live pipeline 默认 follow-up 检查上传后新种下载、注入和等待，未达到 ready 或 `live_safe_to_attempt=false` 会返回非 0；`--connect-qbit` 会真实连接 qBittorrent 并读取一个任务作为盒子连通性探针；`--probe-source/--probe-target` 会显式探测源站详情读取和 MTEAM 查重 API。
- `pipeline`: 串联 `flow-check`、`source-info`、可选 `source-download`、可选 `inject-source`、可选 `wait-complete`、可选 `match`、可选 `target-dupe-check`、可选 `target-prepare`、可选 `target-torrent-export` 和可选 `target-upload`；普通 dry-run 默认不下载、不注入、不等待、不上传；任何源站下载/注入或目标上传执行动作都需要 `--accept-rules` 通过规则 gate；`--upload-target --target-execute` 会自动补齐 live 闭环所需的源种下载/注入/等待、目标 torrent 导出/净化、MTEAM 上传后新种下载/注入/等待；当未显式传 `--path` 时，可从完成的 QB match 结果推导后续内容路径；`--export-target-torrent` 可从匹配 QB 任务导出 `.torrent`，再生成清理过 announce/comment 且带 MTEAM source flag 的上传候选种子；`summary.compliance` 会为 AI/自动化消费者说明规则确认状态和当前仍需人工审阅站规的边界。
- `target-prepare`: 目前生成 MTEAM dry-run preview、meta draft、field mapping、description draft 和 upload gate 文件，不调用上传接口。
- `target-dupe-check`: 可选调用 MTEAM API 按 IMDb 查重；没有 IMDb ID 时明确阻断。
- `target-upload`: 读取 MTEAM 准备包并执行上传预检，输出/写入 MTEAM multipart payload 摘要和上传种子的 announce/source/comment 安全状态；只有 `--execute --confirm-upload` 且 upload gate/payload 全部 ready 时才调用 MTEAM API，并可下载上传后生成的目标站 `.torrent`、显式注入 qBittorrent 做种；也可用 `--uploaded-torrent-id` 复用已知 MTEAM 新种 ID 补下载/注入/等待，或用 `--uploaded-torrent-file` 复用已下载的新种续跑注入/等待，未完成请求的 follow-up 时返回非 0。
- `retorrent`: 默认生成可审计转种计划；`--execute` 会在 `--accept-rules --confirm-upload` 和 reference flow 通过后编排完整 pipeline：源种下载/注入/等待、查重、可选从 QB 导出目标上传种子候选、MTEAM prepare/upload、下载并注入目标站种子，并默认写出可恢复的 `ptcli-run-summary.json`。
- `inspect`: 只读列出 qBittorrent 任务。
- `match`: 按盒子路径匹配 qBittorrent 任务。
- `export`: 从 qBittorrent 只读导出已有 `.torrent` 到指定目录。

当前优先缺口：

- 真实盒子环境验证 U2/CHD -> MTEAM 的完整 live closure。
- 把截图上传图床、IMDb/TMDb、豆瓣/PTGen、MediaInfo/BDInfo 明确纳入 MTEAM live upload 的关键材料 gate。
- 检查 focused runtime 是否仍通过 legacy 截图/图床模块引入无关依赖，并收束到 `requirements-ptcli.txt`。
- 为图床、外部元数据和材料生成失败补齐机器可读 blockers 与恢复建议。
