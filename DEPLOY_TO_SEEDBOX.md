# 部署 Upload-Assistant 到盒子（Seedbox）测试指南

## 方式一：直接部署到盒子（推荐）

### 1. 准备文件

在本地准备好以下文件：
- 整个 `Upload-Assistant` 项目目录（包含所有修改）
- `data/config.py` 配置文件（已配置好 MTEAM 的 api_key）

### 2. 上传到盒子

使用 SCP 或 SFTP 上传整个项目目录到盒子：

```bash
# 使用 SCP 上传（在本地执行）
scp -r /Users/liuxiang/Desktop/Project/Upload-Assistant user@your-seedbox-ip:/path/to/destination/

# 或者使用 rsync（推荐，支持断点续传）
rsync -avz --progress /Users/liuxiang/Desktop/Project/Upload-Assistant/ user@your-seedbox-ip:/path/to/Upload-Assistant/
```

### 3. SSH 连接到盒子

```bash
ssh user@your-seedbox-ip
cd /path/to/Upload-Assistant
```

### 4. 安装系统依赖

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y python3 python3-pip ffmpeg mediainfo

# CentOS/RHEL
sudo yum install -y python3 python3-pip ffmpeg mediainfo

# 或者使用 dnf (较新版本)
sudo dnf install -y python3 python3-pip ffmpeg mediainfo
```

### 5. 安装 Python 依赖

```bash
# 进入项目目录
cd /path/to/Upload-Assistant

# 安装依赖（推荐使用虚拟环境）
python3 -m venv venv
source venv/bin/activate

# 安装 Python 包
pip install -r requirements.txt
```

### 6. 验证安装

```bash
# 检查 Python 版本（需要 3.9+）
python3 --version

# 检查依赖是否安装成功
python3 -c "import httpx; import aiofiles; print('Dependencies OK')"

# 检查 ffmpeg 和 mediainfo
ffmpeg -version
mediainfo --version
```

### 7. 配置检查

确保 `data/config.py` 中已配置好：
- `tmdb_api`: TMDb API Key（必需）
- `MTEAM.api_key`: 馒头站点的 Token（必需）

### 8. 运行测试

```bash
# Debug 模式测试（不会实际上传）
python3 upload.py "/path/to/test/video.mkv" --trackers MTEAM --debug --no-seed

# 如果一切正常，会看到：
# - API 验证成功
# - 分类映射结果
# - 准备上传的数据
# - "Debug mode enabled, not uploading."
```

---

## 方式二：使用 Docker（如果盒子支持 Docker）

### 1. 准备配置文件

在本地编辑好 `data/config.py`，确保包含 MTEAM 配置。

### 2. 上传配置文件到盒子

```bash
# 只上传配置文件
scp data/config.py user@your-seedbox-ip:/path/to/config.py
```

### 3. 在盒子上运行 Docker

```bash
# 拉取最新镜像
docker pull ghcr.io/audionut/upload-assistant:latest

# 运行测试（Debug 模式）
docker run --rm -it --network=host \
  -v /path/to/config.py:/Upload-Assistant/data/config.py \
  -v /path/to/downloads:/downloads \
  ghcr.io/audionut/upload-assistant:latest \
  /downloads/path/to/video.mkv \
  --trackers MTEAM \
  --debug \
  --no-seed
```

---

## 方式三：使用 Git 部署（推荐用于更新）

### 1. 在盒子上克隆项目

```bash
cd /path/to
git clone https://github.com/Audionut/Upload-Assistant.git
cd Upload-Assistant
```

### 2. 应用你的修改

由于你修改了代码，需要：
- 手动复制修改的文件到盒子
- 或者创建 git patch 文件

```bash
# 在本地创建 patch
cd /Users/liuxiang/Desktop/Project/Upload-Assistant
git diff > my-changes.patch

# 上传 patch 到盒子
scp my-changes.patch user@your-seedbox-ip:/path/to/Upload-Assistant/

# 在盒子上应用 patch
cd /path/to/Upload-Assistant
git apply my-changes.patch
```

### 3. 安装依赖和运行

同方式一的步骤 4-8。

---

## 快速测试命令

### Debug 模式（推荐首次测试）

```bash
python3 upload.py "/path/to/video.mkv" --trackers MTEAM --debug --no-seed
```

### 带 IMDb ID 的测试

```bash
python3 upload.py "/path/to/video.mkv" --trackers MTEAM --imdb tt0111161 --debug --no-seed
```

### 完整测试（包含截图）

```bash
python3 upload.py "/path/to/video.mkv" --trackers MTEAM --screens 3 --debug --no-seed
```

### 实际测试上传（移除 --debug）

```bash
python3 upload.py "/path/to/video.mkv" --trackers MTEAM --no-seed
```

---

## 常见问题排查

### 1. Python 版本问题

```bash
# 检查 Python 版本
python3 --version  # 需要 3.9+

# 如果版本太低，安装 Python 3.9+
sudo apt-get install -y python3.9 python3.9-venv python3.9-pip
python3.9 -m venv venv
source venv/bin/activate
```

### 2. 依赖安装失败

```bash
# 升级 pip
pip install --upgrade pip

# 使用国内镜像源（如果网络慢）
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3. 权限问题

```bash
# 确保有执行权限
chmod +x upload.py

# 确保配置文件可读
chmod 644 data/config.py
```

### 4. API Key 验证失败

- 检查 `data/config.py` 中的 `api_key` 是否正确
- 检查 Token 是否在站点上有效
- 查看错误日志中的具体错误信息

---

## 测试检查清单

- [ ] Python 3.9+ 已安装
- [ ] ffmpeg 和 mediainfo 已安装
- [ ] Python 依赖已安装（requirements.txt）
- [ ] `data/config.py` 已配置
- [ ] `tmdb_api` 已填写
- [ ] `MTEAM.api_key` 已填写（Token）
- [ ] 测试视频文件已准备好
- [ ] Debug 模式测试通过
- [ ] 实际上传测试（可选）

---

## 下一步

测试成功后，你可以：
1. 将修改提交到 git（如果需要）
2. 设置定时任务自动运行
3. 配置其他 tracker 进行批量上传
