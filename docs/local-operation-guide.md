# 📖 本地大批量照片操作指南

> 适用于数千到数万张照片的本地全量处理场景。
> 如果你的照片库达到 3000+ 张，VLM 推理需要数十小时，本指南教你如何高效完成。
>
> 🟩 **Windows 用户请注意**：本文档以 Linux/macOS 为例，Windows 下的关键差异：
> - 命令使用 `python` 而非 `python`（安装时请勾选「Add Python to PATH」）
> - 无 `nohup` 命令，建议使用 Web UI 面板的后台进程管理替代
> - 路径分隔符可用 `\` 或 `/`，建议在 `config.json` 中使用 `\\` 或 `/`
> - `cat` → `type`，`grep` → `findstr`，`wc` → 用 PowerShell 的 `Measure-Object`
> - **强烈建议使用 Web UI 的「🔧 操作」页面执行评分**，自带实时进度条和后台进程管理

---

## 📋 目录

1. [核心流程概述](#核心流程概述)
2. [环境准备](#环境准备)
3. [NAS 照片挂载](#nas-照片挂载)
4. [建立索引](#建立索引)
5. [全量评分](#全量评分)
6. [导出与迁移](#导出与迁移)
7. [Web 管理面板](#web-管理面板)
8. [定时任务配置](#定时任务配置)
9. [性能调优](#性能调优)
10. [常见问题](#常见问题)

---

## 核心流程概述

本地大批量处理采用**两阶段架构**，将"耗时的 AI 评分"和"轻量的日常推送"分离：

```
                     本地 PC（耗时任务）
                     ┌────────────────────────────┐
                     │  ① mount NAS 照片           │
                     │  ② python build_index.py   │ ← 扫描所有照片
                     │  ③ python score.py auto    │ ← AI 评分（数小时~数天）
                     │  ④ python score.py export  │ ← 导出 JSONL
                     └──────────┬─────────────────┘
                                │ 拷贝 results.jsonl
                                ▼
                     Docker / NAS（日常运行）
                     ┌────────────────────────────┐
                     │  ⑤ score.py import          │ ← 导入评分结果
                     │  ⑥ score.py run（每日）      │ ← 增量评分（仅新增）
                     │  ⑦ push.py push（每日）      │ ← 选片推送
                     └────────────────────────────┘
```

**为什么分两阶段？**

| 问题 | 解决方式 |
|------|---------|
| VLM 推理极慢（55s/张） | 本地 PC 有更强的 CPU，扛得住 |
| NAS / Docker 环境受限 | 只需跑增量，每天几分钟 |
| 全量结果怎么搬到目标环境 | JSONL 导出，极小（1 万条约 2MB） |
| 中断后能否续跑 | 哈希去重 + SQLite 断点续传 |

---

## 环境准备

### 硬件要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| CPU | 4核 | 8核+（12-16核最佳） |
| RAM | 8GB | 16GB+ |
| 磁盘空间 | 10GB | 照片所在盘剩余 50GB+ |
| 网络 | — | 千兆（挂载 NAS 流畅） |

> 🌟 **CPU 核心数最关键**：Ollama 的 `num_thread` 参数直接决定推理速度。
> 本机 i5-12500H（12核/16线程）→ 每张 ~55 秒。
> ⚡ `config.json` 中 `num_thread` 已默认为 `0`（自动检测），无需手动设置。
> 自动规则：检测 CPU 核心数，保留 2 核给系统，其余全用于推理。

### 软件要求

```bash
# Python 3.8+
python --version

# 安装依赖
pip install Pillow requests  # Windows 下同样用这条命令

# Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5vl:7b
```

### 配置文件

从 `config.json` 开始，只需调整以下参数：

```json
{
  "paths": {
    "photo_dir": "/path/to/photos",          // NAS 挂载点
    "project_dir": "/path/to/memoir-data", // 本地数据目录
    "index_file": "/path/to/memoir-data/index.txt",
    "score_db": "/path/to/memoir-data/scores.sqlite"
  },
  "ollama": {
    "base_url": "http://localhost:11434",
    "num_thread": 0           // 0=自动检测，无需手动设置
  }
}
```

---

## NAS 照片挂载

> 如果你的照片存储在 NAS 上，需要先挂载到本地 PC。

### SMB/CIFS（推荐）

```bash
# Linux / macOS
sudo mkdir -p /path/to/photos
sudo mount -t cifs //NAS_IP/photo /path/to/photos \
  -o username=你的用户名,password=你的密码,vers=3.0

# macOS（使用 Finder 连接）
# Finder → 前往 → 连接服务器 → smb://NAS_IP/photo
```

**Windows 挂载 NAS：**

```powershell
# 方式一：映射网络驱动器（推荐）
# 在文件管理器 → 此电脑 → 右键「映射网络驱动器」
# 驱动器 Z: → 文件夹 \\NAS_IP\photo

# 方式二：命令行映射
net use Z: \\NAS_IP\photo /persistent:yes

# 方式三：使用 UNC 路径直接访问（无需映射）
# config.json 中 photo_dir 可直接写：
# "photo_dir": "\\\\NAS_IP\\photo\\照片"
# 或正斜杠写法：
# "photo_dir": "//NAS_IP/photo/照片"
```

> 映射后，`config.json` 中的 `photo_dir` 写盘符路径如 `Z:\photo`。
> 注意 PowerShell 脚本或命令中需用引号包裹含空格的路径。

### NFS

```bash
# Linux
sudo mount -t nfs NAS_IP:/path/to/photos /path/to/photos
```

> Windows 挂载 NFS：打开「控制面板 → 程序 → 启用或关闭 Windows 功能」→ 勾选「Services for NFS」→ 确定后：
> ```powershell
> mount NAS_IP:/path/to/photos Z:
> ```

### Synology 特殊目录

群晖 NAS 会在照片目录下生成系统缩略图目录，`config.json` 已预设排除：

```json
"exclude_dirs": ["@eaDir", "#recycle", ".thumbnails", ".DS_Store", "@__thumb"]
```

---

## 建立索引

在运行 `score.py` 之前，需要用 `build_index.py` 扫描照片库建立索引。

### 基本用法

```bash
python build_index.py /path/to/photos > /path/to/memoir-data/index.txt
```

### 索引格式

每行一条：`YYYY-MM-DD|文件绝对路径`

```
2024-08-15|/path/to/photos/2024/08/IMG_1234.jpg
2024-08-16|/path/to/photos/2024/08/IMG_1235.jpg
...
```

### 验证索引

```bash
# 总行数
wc -l /path/to/memoir-data/index.txt

# 随机抽查 5 行
shuf -n 5 /path/to/memoir-data/index.txt
```

---

## 全量评分

这是最耗时的一步。本部分涵盖了完整的操作方法。

### 先试跑（确认一切正常）

```bash
# 只处理 10 张，验证配置
cd /path/to/Memoir
python score.py auto --limit 10

# 查看结果
python score.py stats
python score.py top
```

### 后台全量运行

```bash
# 使用 nohup 后台运行，日志重定向到文件
nohup python score.py auto > batch.log 2>&1 &

# 💡 Windows 替代：使用 Web UI 的「操作」页执行评分，自带后台进程管理
# 或在 PowerShell 中使用 Start-Process：
# Start-Process -NoNewWindow python "score.py auto" -RedirectStandardOutput batch.log

# 查看进程号
echo $!
```

### 实时监控进度

**方式一：命令行**（SSH 或终端）

```bash
# 实时查看日志
tail -f batch.log

# 查看处理进度
python score.py stats

# 查看当前处理了多少张
grep -c "✅" batch.log

# 查看平均速度
tail -20 batch.log | grep "平均"
```

**方式二：Web 浏览器**（推荐，支持实时进度条）

```bash
# 启动 Web 管理面板（与评分进程并行）
python webui/app.py

# 浏览器访问 http://localhost:5000
# 在「操作」页点击「AI评分」→ 面板页自动显示实时进度
```

Web 面板提供图形化进度条，自动刷新，关闭浏览器不丢失进度：

```
┌─────────────────────────────────────────────────────┐
│ 🔄 AI评分（全量）                     [停止]         │
│ ████████████████████░░░░░░░░ 37.2%               │
│                                                     │
│  当前      总计     进度    平均     已用     剩余   │
│  1,861    5,000   37.2%   55.1s   28.4m   47.9m   │
│                                                     │
│         成功 1,832    失败 29                        │
│ 当前照片：IMG_1861 城市街道夜景                      │
└─────────────────────────────────────────────────────┘
```

> 💡 评分过程中的所有进度数据已持久化到 SQLite 数据库，
> 关闭浏览器、断网、甚至重启电脑后打开 Web 面板，进度仍在。
> 后台 `score.py` 使用文件哈希（路径+大小+修改时间）去重，已处理的照片不会重复评分。

日志输出示例：

```
2026-04-30 08:00:00 [INFO] 开始处理 5000 张照片
2026-04-30 08:00:00 [INFO]   CPU线程: 12  图片缩放: 512px  超时: 200s
2026-04-30 08:00:01 [INFO] [1/5000] IMG_0001.jpg
2026-04-30 08:00:56 [INFO]   ✅ 7.5/10 | 城市街道夜景 | 55.2s
2026-04-30 08:01:51 [INFO]   ✅ 8.0/10 | 海边日落 | 54.8s
...
2026-04-30 08:09:16 [INFO]   📊 平均 55.1s/张, 预计剩余 458分钟
```

### 中断后恢复

```bash
# 优雅停处理（会等当前张完成）
kill -SIGINT $(pgrep -f "score.py auto")

# 或直接停止进程
kill $(pgrep -f "score.py auto")

# 恢复运行（自动跳过已处理的，只处理剩余）
python score.py auto
```

> ✅ SQLite 数据库每处理 10 张自动 commit，中断后不丢失已处理的结果。

### 处理失败照片

```bash
# 查看失败数量
python score.py stats

# 重试所有失败照片
python score.py retry
```

---

## 导出与迁移

全量评分完成后，将结果导出为轻量 JSONL 文件，再导入到 Docker/NAS 环境。

### 导出 JSONL

```bash
# 导出全部已完成的评分结果
python score.py export -o results.jsonl
```

### JSONL 文件格式

每行一条完整评分记录：

```json
{
  "file_path": "/path/to/photos/2024/08/IMG_1234.jpg",
  "file_name": "IMG_1234.jpg",
  "file_size": 3276800,
  "mtime": 1723680000.0,
  "scored_at": "2026-04-30T08:05:30",
  "d": "城市街道夜景，街灯点缀其间",
  "t": ["城市", "夜景", "街灯"],
  "s": "夜景",
  "q": 7.5,
  "h": "光线层次丰富",
  "i": "无"
}
```

### 文件大小参考

| 照片数量 | JSONL 大小 | 传输时间（千兆网） |
|---------|-----------|-----------------|
| 1,000 | ~200 KB | <1 秒 |
| 10,000 | ~2 MB | <1 秒 |
| 50,000 | ~10 MB | <1 秒 |
| 100,000 | ~20 MB | ~1 秒 |

> JSONL 非常轻量，只存文本，不存图片本身，几十万张也仅几十 MB。

### 传输到目标环境

```bash
# scp 传送到 NAS
scp results.jsonl user@NAS_IP:/path/to/data/

# 或拷贝到 Docker data 目录
cp results.jsonl /path/to/docker/data/
```

### 在目标环境导入

```bash
# NAS 环境
cd /path/to/data
python score.py import results.jsonl

# Docker 环境
docker compose run --rm -v $(pwd)/results.jsonl:/data/results.jsonl memoir score.py import /data/results.jsonl
```

导入日志：
```
2026-04-30 08:10:00 [INFO] 开始导入: results.jsonl
2026-04-30 08:10:02 [INFO]   已导入 1000 条...
2026-04-30 08:10:03 [INFO] 导入完成: 新增 15000, 跳过 0
```

---

## Web 管理面板

> 浏览器管理界面，支持 Docker 和本地 PC 两种运行方式。
> 评分过程中提供实时进度条、断点续传，关闭浏览器不丢失进度。

### 启动方式

**方式一：Docker 环境**

```bash
docker compose up -d ollama webui
# 访问 http://localhost:5000
```

**方式二：本地 PC 环境**（用于全量评分时实时监控）

```bash
# 在 Memoir 目录下执行
python webui/app.py

# 访问 http://localhost:5000
# 然后在「操作」页点击「AI评分」→ 自动跳转面板显示实时进度
# 评分过程中可随时关闭浏览器，下次打开重新连接即可
```

### 面板功能

| 页面 | 功能 |
|------|------|
| 📊 面板 | 系统概览 + **实时进度条**（评分时自动弹出，含 8 项统计指标） |
| ⏰ 定时任务 | 创建/编辑/启用/禁用定时计划任务 |
| ⚙️ 配置 | 在线编辑 `config.json` 所有参数，自动从 Ollama 拉取可用模型列表 |
| 📋 日志 | 查看最近 200 行运行日志，快速定位问题 |
| 🔧 操作 | 一键执行增量评分、选片、推送等操作，无需命令 |

### 断点续传与去重

- **哈希去重**：每张照片使用 `MD5(路径+大小+修改时间)` 作为唯一标识
- **SQLite 持久化**：评分结果写入库后即使断电也不丢失
- **自动跳过**：已评分的照片重新运行会自动跳过，不会重复处理
- **进度恢复**：关闭浏览器、断网、重启电脑后打开面板，进度仍在

---

## PC 端本地识别完整操作流程

> 含 Web UI 实时监控。适合在本地电脑上处理大量照片。
> 你已跑通本地 Ollama（`qwen2.5vl:7b`），这套流程直接用。

### 流程图

```
① 安装依赖  →  ② 建立索引  →  ③ 启动 Web UI  →  ④ 开始评分  →  ⑤ 实时监控
                                                           ↓
                                                     关闭浏览器不丢失
                                                     重新打开继续看
```

### 第一步：安装依赖（一次性）

```bash
cd Memoir
pip install -r requirements.txt
# 注意：需要安装 Flask 和 APScheduler，已写入 requirements.txt
```

### 第二步：建立索引（一次性）

```bash
# 扫描照片目录，生成索引文件
python build_index.py /path/to/photos -o /path/to/index.txt

# 确认索引条目数
wc -l /path/to/index.txt
```

### 第三步：修改配置

编辑 `config.json`（或用 Web UI 的「配置」页在线修改），关键参数：

```json
{
  "paths": {
    "photo_dir": "/path/to/photos",
    "project_dir": "/path/to/data",
    "index_file": "/path/to/data/zectrix_photo_index.txt",
    "score_db": "/path/to/data/zectrix_scores.sqlite"
  },
  "ollama": {
    "base_url": "http://localhost:11434",
    "model": "qwen2.5vl:7b",
    "num_thread": 0
  }
}
```

### 第四步：启动 Web UI（核心操作）

```bash
# 在 Memoir 目录下执行
python webui/app.py
```

终端输出：
```
🌐 Memoir Web 管理后台启动: http://0.0.0.0:5000
   配置文件: config.json
   数据库: /path/to/data/zectrix_scores.sqlite
```

浏览器打开 **http://localhost:5000**，看到 Web 管理面板即可。

### 第五步：启动评分

在 Web UI 中：

```
① 点击「操作」标签页
② 点击「AI评分（全量）」按钮
③ 自动跳转回「面板」标签页
④ 实时进度条开始滚动 ↓
```

面板页实时显示：

```
┌─────────────────────────────────────────────────────┐
│ 🔄 AI评分（全量）                     [停止]         │
│ ████████████████████░░░░░░░░ 37.2%               │
│                                                     │
│  当前      总计     进度    平均     已用     剩余   │
│  1,861    5,000   37.2%   55.1s   28.4m   47.9m   │
│                                                     │
│         成功 1,832    失败 29                        │
│ 当前照片：IMG_1861 城市街道夜景                      │
└─────────────────────────────────────────────────────┘
```

### 第六步：评分过程中

| 操作 | 说明 |
|------|------|
| **关闭浏览器** | 不影响后台评分，重新打开 `http://localhost:5000` 自动恢复进度 |
| **停止评分** | 点击「停止」按钮，下次重新评分自动跳过已处理的 |
| **电脑关机** | 先关 Web UI 再关评分进程（Ctrl+C），数据不会丢失 |
| **查看日志** | 点「日志」标签页看实时输出，或看 `memoir.log` 文件 |

> 💡 评分时长参考：3000 张照片在 i5-12500H 上约需 46 小时。
> 建议白天启动评分，下班后不用管，电脑不关机即可。

### 第七步：评分完成后

```bash
# 在命令行导出结果（备用）
python score.py export -o /path/to/results.jsonl

# 或直接在 Web UI 操作页点「导出评分结果」
```

### 第八步：日常维护

Ollama 保持运行，以后新增照片后：

```bash
# 启动 Web UI（如果没开）
python webui/app.py

# 浏览器操作：
# ① 「操作」→「增量评分」→ 面板看进度（每天几分钟）
# ② 「操作」→「选片并推送」→ 推送到 ZecTrix Note4
```

---

## 定时任务配置

> **Docker 专用功能**。通过 Web 面板创建定时任务，替代系统 cron。

### 支持的定时操作

| 操作 | 说明 | 推荐执行时间 |
|------|------|-------------|
| `score_run` | 增量评分（处理新增照片） | 每日凌晨 3:00，照片库低负载 |
| `push_push` | 选片并推送 | 每日 8:00，起床前推送到设备 |
| `retry` | 重试失败照片 | 每周日 4:00 |
| `export` | 导出评分结果为 JSONL | 每周一 4:00，数据备份 |
| `score_stats` | 查看统计（记录日志） | 每日 23:59，日志归档 |

### 调度方式

| 类型 | 说明 | 示例 |
|------|------|------|
| **Cron 表达式** | 按固定时间规则 | `0 8 * * *` = 每天 8:00 |
| **间隔（秒）** | 按固定间隔 | `86400` = 24 小时一次 |
| **一次性** | 在指定时间执行一次 | 某日某时执行 |

### 常用 Cron 表达式

| 表达式 | 含义 | 适用场景 |
|--------|------|---------|
| `0 3 * * *` | 每天 3:00 | 增量评分（避开使用高峰） |
| `0 8 * * *` | 每天 8:00 | 定时推送 |
| `0 4 * * 0` | 每周日 4:00 | 重试失败照片 |
| `0 4 * * 1` | 每周一 4:00 | 导出备份 |
| `*/30 * * * *` | 每 30 分钟 | 测试/调试 |

### 完整每日流程示例

在 Web 面板中创建以下两个任务即可实现自动化：

```
任务 A: 每日增量评分
  - 操作: 增量评分（处理新增照片）
  - 调度: Cron `0 3 * * *`
  - 说明: 每天凌晨 3 点自动评分新增照片

任务 B: 每日定时推送
  - 操作: 选片并推送
  - 调度: Cron `0 8 * * *`
  - 说明: 每天 8 点自动推送照片到设备
```

> 💡 任务定义持久化在 `data/scheduled_tasks.json`，容器重启不丢失。

---

## 性能调优

### 关键参数

| 参数 | 默认 | 说明 | 调优建议 |
|------|------|------|---------|
| `num_thread` | 0（自动检测） | CPU 推理线程数 | 设为 `0` 自动检测 CPU 核心，保留 2 核给系统 |
| `max_side` | 512 | 图片缩放边长（px） | 384 更快，768 更精细 |
| `jpeg_quality` | 80 | 图片压缩质量 | 50 更快，90 更清晰 |
| `timeout` | 200 | VLM 请求超时（秒） | CPU 慢的话设为 300 |
| `num_predict` | 256 | 模型输出 token 数 | 128 更快 |

### 调优策略

在 `config.json` 中调整：

```json
{
  "ollama": {
    "num_thread": 0,            // 0=自动检测（保留2核给系统）
    "timeout": 300
  },
  "image_processing": {
    "max_side": 384,            // 缩小到 384px，提速约 30%
    "jpeg_quality": 50          // 降低质量，提速约 10%
  },
  "processing": {
    "batch_save_interval": 20   // 减少 commit 频率
  }
}
```

### 速度对照表

| 缩放 + 质量 | CPU核心 | 单张耗时 | 3,000 张 | 10,000 张 |
|------------|--------|---------|---------|----------|
| 512px + 80 | 12核 | ~55s | ~46h | ~6.4天 |
| 384px + 50 | 12核 | ~35s | ~29h | ~4天 |
| 512px + 80 | 16核 | ~45s | ~37h | ~5.2天 |
| 384px + 80 | 16核 | ~30s | ~25h | ~3.5天 |

> ⚡ **GPU 加速**：如果有 NVIDIA GPU（≥6GB 显存），速度可提升 3-5 倍。

### 分批处理建议

对于 3000+ 张照片，建议分批运行：

```bash
# 方法 1: 使用 --limit 分批
python score.py auto --limit 500
# ❗ 完成后继续下一批
python score.py run --limit 500

# 方法 2: 一次性后台运行（自动处理所有，支持中断恢复）
# Linux: nohup python score.py auto > batch.log 2>&1 &
# Windows (PowerShell): Start-Process -NoNewWindow python "score.py auto"
# 推荐直接用 Web UI 的操作页面启动评分
```

---

## 完整工作流速查

### 方式一：Web UI 监控版（推荐）

```bash
# ====== 本地 PC （全量处理）======

# 1. 安装依赖（一次性）
pip install -r requirements.txt

# 2. 建立索引
python build_index.py /path/to/photos > /path/to/index.txt

# 3. 编辑 config.json
#    设置 paths.photo_dir、paths.index_file、paths.score_db

# 4. 启动 Web UI
python webui/app.py

# 5. 浏览器打开 http://localhost:5000
#    配置页 → 检查参数是否正确（或用 curl 导入索引）
#    操作页 → 点击「AI评分（全量）」→ 自动跳转面板看进度
```

### 方式二：CLI 命令行版

```bash
# ====== 本地 PC ======

# 1. 挂载 NAS
sudo mount -t cifs //NAS_IP/photo /path/to/photos -o username=user

# 2. 调整配置
vim config.json
# paths.photo_dir = /path/to/photos
# ollama.num_thread = 0  （0=自动检测，无需手动设置）

# 3. 建立索引
python build_index.py /path/to/photos > /path/to/index.txt

# 4. 检查照片数量
wc -l /path/to/index.txt

# 5. 试跑 10 张
python score.py auto --limit 10

# 6. 后台全量运行（推荐用 Web UI 的「操作」页执行评分）
# Linux: nohup python score.py auto > batch.log 2>&1 &
# Windows (PowerShell): Start-Process -NoNewWindow python "score.py auto"
# 查看实时日志
tail -f batch.log

# 7. 完成后导出
python score.py export -o results.jsonl

# ====== 传输到 NAS/Docker ======
scp results.jsonl user@NAS_IP:/data/

# ====== Docker 环境 ======
docker compose run --rm -v $(pwd)/results.jsonl:/data/results.jsonl \
  memoir score.py import /data/results.jsonl
```

### 日常维护

```bash
# ====== Docker/NAS 环境（每天） ======

# 增量评分（只处理新增照片，通常几分钟）
docker compose run --rm memoir score.py run

# 选片推送
docker compose run --rm memoir push.py push
```

---

## 常见问题

### Q: 处理到一半电脑关机了怎么办？

直接重新运行即可。SQLite 数据库已记录处理状态，自动跳过已处理的照片。

### Q: 怎么知道还要多久？

日志中每 10 张会输出一次预估：
```
📊 平均 55.1s/张, 预计剩余 458分钟
```
也可以用 `score.py stats` 查看当前进度。

### Q: 很多照片评分失败了怎么办？

```bash
# 查看失败数量
python score.py stats

# 重试
python score.py retry
```

### Q: 评分后 NAS 上照片路径变了，导入后找不到文件？

如果导入环境的照片路径与导出环境不同，可以在导入前用 `sed` 替换路径：

```bash
# 替换路径前缀
sed 's|/path/to/photos|/path/to/photos|g' results.jsonl > results_fixed.jsonl
python score.py import results_fixed.jsonl
```

### Q: TensorFlow/PyTorch 相关报错？

本项目的 AI 评分完全通过 Ollama API 调用，不需要 TensorFlow 或 PyTorch。错误可能是其他依赖冲突。只需 `pip install Pillow requests` 即可。

### Q: 内存不足怎么办？

```bash
# 减少 num_thread
config.json 中 ollama.num_thread 会自动检测，或手动设为较小的值（如 4）

# 减少图片尺寸
image_processing.max_side = 384
```
