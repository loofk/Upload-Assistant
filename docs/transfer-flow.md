# 转种流程文档

本文档详细说明从 U2/CHD 转种到 MTEAM/TJUPT/TTG 的完整流程，标注所有需要人工干预的环节。

## 典型命令

```bash
# U2 → MTEAM
python3 upload.py /path/to/content -u2 12345 -tk MTEAM

# CHD → MTEAM + TJUPT
python3 upload.py /path/to/content -chd 8888 -tk MTEAM,TJUPT

# 无人值守模式（减少交互）
python3 upload.py /path/to/content -u2 12345 -tk MTEAM --unattended

# 手动指定 IMDb 和豆瓣（跳过搜索）
python3 upload.py /path/to/content -u2 12345 -tk MTEAM -imdb tt1234567 -douban 1291546

# Debug 模式（不实际上传）
python3 upload.py /path/to/content -u2 12345 -tk MTEAM --debug
```

## 完整 9 步流程

```
┌──────────────────────────────────────────────────────────────┐
│  python3 upload.py /path -u2 12345 -tk MTEAM                │
└──────────────┬───────────────────────────────────────────────┘
               │
   ┌───────────▼───────────┐
   │ 步骤 1: 参数解析       │  src/args.py
   │ 解析 -u2, -tk, -imdb  │
   └───────────┬───────────┘
               │
   ┌───────────▼────────────────────┐
   │ 步骤 2: 源站元数据提取          │  src/trackermeta.py
   │ U2/CHD.get_info_from_torrent_id │
   │ → IMDb, TMDb, 豆瓣, 描述       │
   │ ⚠️ 干预点 A: 描述编辑确认       │
   └───────────┬────────────────────┘
               │
   ┌───────────▼────────────────────┐
   │ 步骤 3: IMDb/TMDb 补全         │  src/imdb.py + src/tmdb.py
   │ 步骤2未获得 → 搜索 IMDb/TMDb   │
   │ ⚠️ 干预点 B: IMDb 候选选择      │
   │ ⚠️ 干预点 C: 无结果手动输入     │
   └───────────┬────────────────────┘
               │
   ┌───────────▼───────────────────────┐
   │ 步骤 4: 元数据准备 (Prep)          │  src/prep.py
   │ MediaInfo 提取、名称生成、截图     │
   │ 分辨率/编码/类型判断              │
   └───────────┬───────────────────────┘
               │
   ┌───────────▼─────────────────────┐
   │ 步骤 5: 种子文件创建             │  src/torrentcreate.py
   │ 创建 BASE .torrent               │
   └───────────┬─────────────────────┘
               │
   ┌───────────▼──────────────────────┐
   │ 步骤 6: 描述与截图生成           │  src/get_desc.py
   │ 截图拍摄 → 上传图床 → 描述组装  │
   └───────────┬──────────────────────┘
               │
   ┌───────────▼──────────────────────────┐
   │ 步骤 7: Tracker 预检查               │  src/trackerstatus.py
   │ PTGen 获取中文信息                   │
   │ 查重 (search_existing)               │
   │ banned_group 检查                    │
   │ ⚠️ 干预点 D: 缺 IMDb 时手动输入      │
   └───────────┬──────────────────────────┘
               │
   ┌───────────▼──────────────────────────┐
   │ 步骤 8: 上传执行                      │  src/trackerhandle.py
   │ 对每个目标 tracker 执行:              │
   │   edit_desc → edit_name → upload     │
   │ ⚠️ 干预点 E: 重复确认上传             │
   └───────────┬──────────────────────────┘
               │
   ┌───────────▼──────────────────────────┐
   │ 步骤 9: 做种                          │  src/clients.py
   │ 添加到 qBittorrent/rTorrent 做种     │
   └──────────────────────────────────────┘
```

## 各步骤详细说明

### 步骤 1: 参数解析 (`src/args.py`)

解析命令行参数，填充 `meta` 字典。关键参数：

| 参数 | 说明 |
|------|------|
| `-u2 <id>` | 从 U2 种子页获取元数据 |
| `-chd <id>` | 从 CHD 种子页获取元数据 |
| `-mteam <id>` | 从 MTEAM API 获取元数据 |
| `-tk <trackers>` | 目标上传站点，逗号分隔 |
| `-imdb <id>` | 手动指定 IMDb ID（跳过搜索） |
| `-douban <id>` | 手动指定豆瓣 ID（加速 PTGen） |
| `--unattended` | 无人值守模式 |
| `--debug` | 调试模式，不实际上传 |

### 步骤 2: 源站元数据提取 (`src/trackermeta.py`)

根据 `-u2`/`-chd`/`-mteam` 参数，调用对应 Tracker 的 `get_info_from_torrent_id()` 方法。

**U2** (`src/trackers/U2.py`):
- 通过 Cookie 爬取详情页 (`data/cookies/U2.txt`)
- 提取 IMDb/TMDb 链接、豆瓣链接、AniDB aid
- 若有 AniDB aid → 通过 `ids.moe` API 转换为 IMDb/TMDb（需配置 `ids_moe_api_key`）

**CHD** (`src/trackers/CHD.py`):
- 通过 Cookie 爬取详情页 (`data/cookies/CHD.txt`)
- 提取 IMDb/TMDb/豆瓣链接
- 提取完整描述（HTML 格式）

**MTEAM** (`src/trackers/MTEAM.py`):
- 通过 API 调用 (`api_key` 认证)
- 返回 JSON 格式的结构化数据

### 步骤 3: IMDb/TMDb 补全 (`src/imdb.py`, `src/tmdb.py`)

如果步骤 2 未获得 IMDb ID，执行搜索：
1. 从种子名提取搜索词
2. 用 SequenceMatcher 计算与 IMDb 结果的相似度
3. 相似度 >= 0.85 且与次优差距 >= 0.10 → 自动选择
4. 否则展示候选列表让用户选择

**常见失败原因**：
- 动画种子名含 `[组名]`、技术参数，搜索词噪声大
- 中文标题无法匹配英文 IMDb 条目
- 日韩内容的命名格式与 IMDb 差异大

### 步骤 4-6: 元数据准备 → 种子创建 → 描述生成

这三步通常自动完成，无需干预。

### 步骤 7: Tracker 预检查 (`src/trackerstatus.py`)

对每个目标 Tracker 执行：
1. **PTGen 调用** (`COMMON.ptgen()`): 用 IMDb/豆瓣 ID 获取中文标题、地区、类别
2. **查重** (`search_existing()`): 在目标站搜索是否已存在同名种子
3. **banned_group 检查**: 确认发布组不在禁止列表

### 步骤 8: 上传执行 (`src/trackerhandle.py`)

对每个通过预检查的 Tracker：
1. `edit_desc(meta)` — 生成目标站格式的描述（BBCode/Markdown）
2. `edit_name(meta)` — 调整种子名称（移除不兼容内容）
3. `upload(meta, disctype)` — 提交到目标站

### 步骤 9: 做种 (`src/clients.py`)

将新种子添加到 qBittorrent/rTorrent/Deluge/Transmission。

---

## 人工干预点汇总

| ID | 位置 | 触发条件 | 发生频率 | 干预方式 |
|----|------|---------|---------|---------|
| **A** | `trackermeta.py:487` | 源站提取到描述后确认 | 每次 | 选择: 编辑(e) / 丢弃(d) / 保留(Enter) |
| **B** | `imdb.py:841` | IMDb 搜索返回多个候选 | **高**（动画/中文/日韩） | 输入编号选择，或输入 tt1234567 |
| **C** | `imdb.py:883` | IMDb 搜索无结果 | 中 | 手动输入 IMDb ID 或输入 0 跳过 |
| **D** | `trackerstatus.py:68` | 预检查时缺少 IMDb ID | 中 | 手动输入 IMDb ID |
| **E** | `uphelper.py:113/157` | 查重发现可能的重复 | 低 | 确认是否继续上传 |
| **F** | `tmdb.py:767/961` | TMDb 搜索需要选择 | 低 | 选择 TMDb 条目或手动输入 |
| **G** | `get_tracker_data.py:347` | 从其他 Tracker 搜到 ID | 低 | 确认是否使用 |

### unattended 模式下的行为

| 干预点 | unattended 行为 | 结果 |
|--------|-----------------|------|
| A 描述确认 | **仍会弹出确认** ⚠️ | 需改善 |
| B IMDb 多候选 | 自动选择最佳匹配（>= 0.85） | OK |
| C IMDb 无结果 | 返回 0，跳过 IMDb | 导致后续 PTGen 也失败 |
| D 缺 IMDb | **仍会弹出输入** ⚠️ | 需改善 |
| E 重复确认 | 使用 default 值 | OK |
| F TMDb 选择 | **仍会弹出选择** ⚠️ | 需改善 |
| G Tracker ID 确认 | **仍会弹出确认** ⚠️ | 需改善 |

标记 ⚠️ 的是 unattended 模式下仍会阻塞的干预点，将在阶段三修复。

---

## 配置要求

### Cookie 文件

| Tracker | 文件路径 | 格式 |
|---------|---------|------|
| U2 | `data/cookies/U2.txt` | Netscape (Firefox 导出) |
| CHD | `data/cookies/CHD.txt` | Netscape |
| TJUPT | `data/cookies/TJUPT.txt` | Netscape |
| TTG | `data/cookies/TTG.json` | JSON (**注意与其他站不同**) |
| OB | `data/cookies/OB.txt` | Netscape |

### API Key

| Tracker | 配置字段 | 获取方式 |
|---------|---------|---------|
| MTEAM | `TRACKERS.MTEAM.api_key` | 控制台 → 实验室 → 存取令牌 |
| ids.moe | `TRACKERS.U2.ids_moe_api_key` | https://ids.moe 申请 |
| TMDb | `DEFAULT.tmdb_api` | https://www.themoviedb.org/settings/api |
