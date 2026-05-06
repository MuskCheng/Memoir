# Memoir — ZecTrix Note4 岁月史书推送系统 📸

> AI 驱动的照片筛选与推送系统，专为 [ZecTrix Note4 便利贴](https://wiki.zectrix.com) 设计。

一键部署，让 E-Ink 便利贴每天自动推送一张"值得放在桌面"的回忆照片。

---

## ✨ 功能特性

| 特性 | 说明 |
|------|------|
| 🌐 **浏览器管理** | 无需学习命令行，打开浏览器即可操作全部功能 |
| 🤖 **AI 识图评分** | 基于 qwen2.5vl:7b 理解照片内容，自动评分 |
| ⚡ **一键全流程** | 点一次按钮完成「建索引 + AI 评分」，无需分步操作 |
| 📑 **首次启动引导** | 首次运行自动弹出设置向导，引导配置路径、建索引、创建定时任务 |
| 🎯 **规则过滤** | 模糊/截屏/表情包/过暗过曝 — 低质量照片自动跳过 |
| ⚖️ **年份公平选片** | 兼顾各年份照片，不让某一年垄断推送 |
| 🖼️ **自动水印** | 右下角黑色铭牌 + 白色日期文字，像素字体 |
| 🚀 **断点续传** | 哈希去重 + SQLite 数据库，中断后自动跳过已处理照片 |
| 🐳 **Docker 一行启动** | 支持 NAS 部署、GPU 加速 |
| 📡 **极趣云推送** | Web UI 点一下直接推送到 ZecTrix Note4 设备 |

---

## 🚀 快速开始（3 分钟）

### Docker（推荐）

```bash
# 1. 下载项目
git clone https://github.com/MuskCheng/Memoir.git
cd Memoir

# 2. 准备目录和水印字体
mkdir -p photos data                # 照片目录 + 数据目录

# 下载水印字体到项目根目录（中文显示必需）
# 下载后解压，将 .ttf 文件重命名为 ark-pixel-12px-monospaced-zh_cn.ttf 放在项目根目录

# 3. 构建并启动服务（首次会自动构建 Docker 镜像，约需 2-5 分钟）
#    配置文件首次运行自动生成，也可通过环境变量覆盖
docker compose up -d

# 5. 拉取 AI 模型（首次需要，约 4.7GB，可夜间执行）
docker compose exec ollama ollama pull qwen2.5vl:7b
```

> **镜像构建说明**：项目包含 `Dockerfile`，首次执行 `docker compose up -d` 会自动构建镜像。
> 也可提前单独构建：`docker compose build`

打开浏览器访问 **http://NAS_IP:5000**，首次启动会自动弹出设置向导。

> **有 NVIDIA GPU？** 编辑 `docker-compose.yml`，取消 Ollama 服务中 `deploy` 部分的注释，安装 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) 后重启。

### Windows 本地运行

```powershell
# 1. 安装 Ollama + 拉取模型
ollama pull qwen2.5vl:7b

# 2. 安装依赖
pip install -r requirements.txt

# 3. 下载水印字体到项目根目录（中文显示必需）
curl -L -o ark-pixel-12px-monospaced-zh_cn.ttf `
  https://github.com/TakWolf/ark-pixel-font/releases/download/2024.11.04/ark-pixel-12px-monospaced-zh_cn.ttf

# 4. 首次运行自动生成配置，也可通过环境变量覆盖：
#    $env:PHOTO_DIR="你存放照片的文件夹"
#    或编辑自动生成的 config.json 中的路径为 Windows 风格

# 5. 启动 Web 管理面板
.\start_webui.ps1
# 或: python webui\app.py
```

打开浏览器访问 **http://localhost:5000**。

---

## 🌐 浏览器管理（主推操作方式）

Memoir 的所有功能都可以通过浏览器完成，无需接触命令行。

### 首次启动

打开 Web UI 后，**设置向导**会自动弹出，引导你完成 3 步：

| 步骤 | 说明 |
|------|------|
| ① 确认照片目录 | 设置你的照片存放位置 |
| ② 建立索引 | 扫描目录，生成照片清单 |
| ③ 创建定时任务 | 设置每日自动评分和推送时间 |

> 跳过也无妨，随时可以通过页面操作。

### 📊 面板 — 查看系统状态

打开 Web UI 后默认进入「📊 面板」：

- **评分概览**：已评分照片数、待处理数、失败数
- **Ollama 状态**：模型是否已加载、运行状况
- **任务进度**：后台任务实时进度条
- **配置摘要**：当前照片目录、AI 模型、推送设备等关键信息

### 🔧 操作 — 一键执行

「🔧 操作」页面提供所有常用功能按钮：

| 按钮 | 作用 | 使用时机 |
|------|------|---------|
| **⚡ 一键全流程** | 建立索引 + AI 评分（推荐） | **首次使用**或新增大量照片 |
| 📑 建立索引 | 仅扫描目录，更新照片清单 | 新增照片后 |
| 🤖 AI评分（全量） | 对所有照片逐个 AI 评分 | 需要重新评全部照片 |
| ⏳ 增量评分 | 只评未评过的新照片 | **日常使用** |
| 🎯 选片预览 | 选一张照片但不推送 | 想看今天会推什么 |
| 📡 选片并推送 | 选片 → 处理 → 推送到设备 | **每日推送** |

> 点击「⚡ 一键全流程」即可完成全部初始化，后台自动运行，面板实时显示进度。

### 🖼️ 已评分 — 浏览评分结果

「🖼️ 已评分」页面可按评分排序、按场景筛选、搜索照片，浏览所有已评分照片和 AI 生成的描述。

### ⏰ 定时任务 — 设置自动运行

「⏰ 定时任务」页面支持创建和管理定时计划，无需系统级 cron：

| 推荐任务 | 建议时间 | 说明 |
|---------|---------|------|
| 一键全流程 `score_full` | 每天 3:00 | 建索引 + 增量评分 |
| 选片并推送 `push_push` | 每天 8:00 | 自动推送照片到设备 |

> 直接在浏览器里创建，支持 cron 表达式、间隔、一次性三种调度方式。

### ⚙️ 配置

所有配置可在浏览器中在线编辑：照片目录、Ollama 地址/模型/线程数、推送设备等。

### 📋 日志

浏览器中实时查看运行日志，最新在前，无需翻到底部。

---

## 🐳 Docker 部署详情

### 环境要求

| 组件 | 最低配置 | 推荐配置 |
|------|---------|---------|
| CPU | x86_64 4核 | x86_64 8核+ |
| RAM | 8GB | 16GB+ |
| 磁盘 | 10GB | 50GB+ |
| Docker | 24+ | 最新版 |
| GPU（可选）| NVIDIA + CUDA | 显存 ≥6GB |

### 目录结构

```
memoir/
├── docker-compose.yml     # 服务编排（启动全部服务）
├── photos/                # 你的照片目录（挂载）
├── data/                  # 数据目录（自动创建）
└── config.json            # 配置文件（首次运行自动生成，可选）
```

### 环境变量

可通过环境变量覆盖配置，无需手动编辑 config.json：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama 服务地址 |
| `OLLAMA_MODEL` | `qwen2.5vl:7b` | AI 模型 |
| `CPU_THREADS` | `0`（自动检测）| CPU 线程数 |
| `API_KEY` | — | 极趣云 API Key |
| `DEVICE_MAC` | — | 设备 MAC 地址 |
| `PHOTO_DIR` | `/photos` | 照片目录 |
| `DATA_DIR` | `/data` | 数据目录 |
| `FILTER_ENABLED` | `true` | 是否启用规则过滤 |

### GPU 加速

编辑 `docker-compose.yml`，取消 GPU 配置注释，安装 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) 即可。

---

## 🟩 Windows 本地运行

### 环境安装

**1. 安装 Ollama**

从 [ollama.com/download](https://ollama.com/download) 下载安装包，安装后：

```powershell
ollama pull qwen2.5vl:7b   # 拉取模型（约 4.7GB）
ollama list                 # 确认已就绪
```

> Ollama 安装后默认开机自启，无需手动启动。

**2. 安装 Python**

从 [python.org](https://www.python.org/downloads/) 下载 Python 3.12，安装时**勾选**「Add Python to PATH」。

```powershell
python --version
pip --version
```

**3. 安装依赖**

```powershell
cd memoir
pip install -r requirements.txt
```

### 启动 Web 管理面板

```powershell
# 方式一：一键脚本（推荐）
.\start_webui.ps1

# 方式二：手动
python webui\app.py
```

浏览器打开 **http://localhost:5000**，首次启动会自动弹出设置向导。

> 如果端口 5000 被占用：`$env:WEBUI_PORT=8080; python webui\app.py`

---

## ⚙️ 配置指南

配置文件首次运行自动生成，也可以在 Web UI「⚙️ 配置」页面在线编辑。

### 核心配置项

```json
{
  "paths": {
    "photo_dir": "/photos",              // 照片目录
    "project_dir": "/data",              // 数据目录（索引、数据库）
    "score_db": "/data/zectrix_scores.sqlite"
  },
  "ollama": {
    "base_url": "http://localhost:11434",
    "model": "qwen2.5vl:7b",
    "num_thread": 0,                     // 0=自动检测CPU核心数
    "temperature": 0.7
  },
  "devices": [
    {
      "name": "客厅 Note4",
      "device_mac": "20:6E:...",
      "api_key": "zt_..."
    }
  ]
}
```

---

## 📖 CLI 命令（进阶用户）

所有 CLI 命令均可在 Web UI「🔧 操作」页面一键完成。以下供自动化脚本或排查问题使用。

```bash
# 建立索引
python build_index.py /photos -o /data/index.txt

# 全量评分
python score.py auto

# 增量评分（日常使用）
python score.py run

# 选片并推送
python push.py push

# 查看统计
python score.py stats

# 查看高分照片
python score.py top --limit 10

# 按场景搜索
python score.py search 风景

# 重试失败照片
python score.py retry
```

---

## 📁 项目结构

```
memoir/
├── start_webui.ps1          # Windows 一键启动脚本
├── score.py                 # AI 评分引擎
├── push.py                  # 选片 + 推送
├── filter.py                # 规则过滤
├── build_index.py           # 照片扫描
├── Dockerfile               # Docker 构建
├── docker-compose.yml       # Docker 编排
├── webui/                   # 🌐 Web 管理面板（主推入口）
│   ├── app.py               #   Flask 后端
│   ├── health.py             #   启动自检
│   ├── scheduler.py          #   定时任务
│   └── templates/index.html  #   单页管理界面
└── docs/
    └── local-operation-guide.md  # 大批量照片处理指南
```

> 📥 `ark-pixel-12px-monospaced-zh_cn.ttf`（水印字体）需自行下载，见 [FAQ](#水印字体在哪下载)。

---

## 📊 性能参考（i5-12500H / CPU 模式）

| 图片缩放 | 单张耗时 | 1,000 张 | 10,000 张 |
|---------|---------|----------|----------|
| 512px（默认）| ~55 秒 | ~15 小时 | ~6.4 天 |
| 384px（快速）| ~35 秒 | ~10 小时 | ~4 天 |

- **规则过滤**：毫秒级，淘汰约 20-30% 低质量照片
- **GPU 加速**（NVIDIA）：预计提速 3-5 倍
- **增量处理**：每天新增 ≤50 张，几分钟完成

---

## ❓ 常见问题

### 没有 NVIDIA GPU 能用吗？
能。本项目设计目标就是 CPU 也能跑，图片已缩放到 512px。

### 水印字体在哪下载？
水印使用的像素字体 [Ark Pixel Font](https://github.com/TakWolf/ark-pixel-font/) 是开源项目。
项目仓库不包含字体文件，请自行下载。

> 缺少字体不影响主要功能运行，水印会回退使用系统默认字体（中文可能显示异常）。

### 怎么排查问题？
1. 看 Web UI「📋 日志」页（最新在前）
2. 检查 Ollama：`curl http://localhost:11434/api/tags`
3. Web UI「🔧 操作」→ 试跑「🧪 AI评分（试跑10张）」

### 数据库在哪？
配置文件中的 `paths.score_db`（默认 `/data/zectrix_scores.sqlite`），单文件可直接拷贝迁移。

---

## ⚖️ 开源协议

[MIT License](LICENSE) — 自由使用、修改、发布。

## 🔗 相关链接

- [ZecTrix Note4 产品页](https://wiki.zectrix.com)
- [极趣云 API 文档](./docs/api.md)
- [Ollama](https://ollama.com)
