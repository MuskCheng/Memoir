#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config_module — Memoir 统一配置模块

所有脚本（score.py, push.py, filter.py, build_index.py, webui/*）的
唯一配置加载入口。消除重复代码，确保行为一致。

设计原则：
- 仅依赖 Python 标准库，不导入项目内其他模块
- 支持环境变量覆盖（Docker 部署友好）
- 自动检测 CPU 线程数
- 提供数据库 canonical DDL
"""

import os
import sys
import json
import copy
from pathlib import Path

# ─── 路径 ───────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
VERSION_FILE = SCRIPT_DIR / "VERSION"

# 配置文件路径：Docker 环境下存放在 DATA_DIR（持久化卷），本地开发存放在项目目录
_data_dir = os.environ.get("DATA_DIR")
if _data_dir and Path(_data_dir).is_dir():
    CONFIG_FILE = Path(_data_dir) / "config.json"
else:
    CONFIG_FILE = SCRIPT_DIR / "config.json"


def get_version():
    """从 VERSION 文件读取版本号"""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "1.0"

# ─── 数据库 Canonical DDL ──────────────────────────────────────
# photo_scores 表的唯一建表语句，score.py 和 health.py 共享
DB_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS photo_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT UNIQUE,
    file_path TEXT NOT NULL,
    file_name TEXT,
    file_size INTEGER,
    mtime REAL,
    shoot_date TEXT,
    year INTEGER,
    month INTEGER,
    day INTEGER,

    -- 规则过滤结果
    rule_pass INTEGER DEFAULT 0,
    rule_blur REAL,
    rule_bright REAL,
    rule_ratio REAL,
    rule_size INTEGER,
    rule_color_var REAL,
    rule_reasons TEXT,

    -- VLM 评分结果
    vlm_done INTEGER DEFAULT 0,
    vlm_score REAL,
    vlm_desc TEXT,
    vlm_tags TEXT,
    vlm_scene TEXT,
    vlm_highlights TEXT,
    vlm_issues TEXT,
    vlm_raw TEXT,
    vlm_error TEXT,
    scored_at TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_file_hash ON photo_scores(file_hash);
CREATE INDEX IF NOT EXISTS idx_vlm_done ON photo_scores(vlm_done);
CREATE INDEX IF NOT EXISTS idx_vlm_score ON photo_scores(vlm_score);
CREATE INDEX IF NOT EXISTS idx_year ON photo_scores(year);
CREATE INDEX IF NOT EXISTS idx_shot_date ON photo_scores(shoot_date);
"""

# ─── 默认配置 ──────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "project_name": "Memoir - ZecTrix Note4",
    "version": get_version(),
    "paths": {
        "photo_dir": "/photos",
        "project_dir": "/data",
        "index_file": "/data/zectrix_photo_index.txt",
        "score_db": "/data/zectrix_scores.sqlite",
        "shown_file": "/data/zectrix_shown.txt",
        "font_file": "/app/ark-pixel-12px-monospaced-zh_cn.ttf",
        "output_file": "/data/zectrix_today.jpg",
    },
    "ollama": {
        "base_url": "http://localhost:11434",
        "api_endpoint": "/api/generate",
        "model": "qwen2.5vl:7b",
        "timeout": 200,
        "temperature": 0.7,
        "num_predict": 256,
        "num_ctx": 2048,
        "num_thread": 0,
        "repeat_penalty": 1.1,
    },
    "image_processing": {
        "max_side": 512,
        "output_width": 400,
        "output_height": 300,
        "jpeg_quality": 80,
        "watermark_enabled": True,
        "watermark_font_size": 12,
        "watermark_position": "bottom_right",
        "watermark_text_format": "{year}年{month}月{day}日",
    },
    "filter_rules": {
        "enabled": True,
        "min_file_size_kb": 50,
        "min_aspect_ratio": 0.20,
        "max_aspect_ratio": 5.0,
        "min_blur_score": 30,
        "min_brightness": 0.03,
        "max_brightness": 0.97,
        "min_color_variance": 0.0005,
        "min_dimension": 200,
        "exclude_filename_patterns": [
            "screenshot", "截图", "screen_capture", "screen-recording", "录屏",
            "thumb", "thumbnail", "缩略图", "cache", "壁纸", "wallpaper",
            "emoji", "表情", "sticker", "贴纸", "icon", "图标", "logo",
            "qrcode", "二维码", "barcode", "条形码"
        ],
        "exclude_filename_regex": [
            "^Screenshot_", "^screen_", "^PANO_",
            "^PXL_", "^MVIMG_", "^IMG_\\d{8}_\\d{6}_COVER", "^received_", "^STK_", "^VID_",
            "^DCIM_", "^Photo_", "^PhotoGrid_", "^Collage_", "^InShot_",
            "^VSCO_", "^Snapseed_", "^PicsArt_", "^BeautyPlus_", "^Meitu_",
            "^B612_", "^SNOW_", "^LINE_", "^WeChat_", "^wx_camera_",
            "^mmexport_", "^microMsg.", "^QQ_Image_", "^QQ图片", "^weibo_",
            "^微博", "^抖音", "^TikTok_", "^快手", "^Kuaishou_", "^小红书_",
            "^XHS_"
        ],
        "exclude_aspect_ratios": ["9:16", "16:9", "9:18", "18:9"],
        "exclude_resolutions": [
            "1080x1920", "1920x1080", "1440x2560", "2560x1440",
            "1080x2340", "2340x1080", "1080x2400", "2400x1080"
        ],
        "max_thumbnail_size": 300,
        "detect_animated_webp": True,
        "detect_animated_png": True,
        "exclude_no_exif": False,
        "exclude_software_tags": [
            "Screenshot", "Screen Capture", "Screen Recording",
            "Adobe Photoshop", "GIMP", "Inkscape", "Canva", "Figma",
            "Sketch", "Illustrator", "CorelDRAW", "Paint.NET"
        ]
    },
    "scoring": {
        "min_score_threshold": 0,
        "decay_factor": 0.15,
        "year_bonus_enabled": True,
        "scene_weights": {
            "landscape": 1.0, "portrait": 1.0, "food": 0.9, "pet": 0.9,
            "architecture": 0.95, "night": 0.85, "document": 0.3, "other": 0.7,
        },
    },
    "devices": [
        {
            "name": "默认设备",
            "device_mac": "",
            "api_key": "",
            "dither": True,
            "page_id": 1,
            "api_base": "https://cloud.zectrix.com/open/v1/devices"
        }
    ],
    "push_settings": {
        "device_mac": "",
        "api_key": "",
        "api_base": "https://cloud.zectrix.com/open/v1/devices",
        "page_id": 1,
        "dither": True,
        "cleanup_old_images": True,
    },
    "logging": {
        "level": "INFO",
        "file": "photopush.log",
        "format": "%(asctime)s [%(levelname)s] %(message)s",
    },
    "processing": {
        "batch_save_interval": 10,
        "max_retries": 2,
        "retry_delay": 3,
        "enable_progress_estimate": True,
    },
    "photo_extensions": [".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tiff"],
    "exclude_dirs": ["@eaDir", "#recycle", ".thumbnails", ".DS_Store", "@__thumb"],
}


def load_config(config_file=None, auto_create=True):
    """
    加载配置文件，支持环境变量覆盖。

    首次运行时如果 config.json 不存在，会自动从 DEFAULT_CONFIG 生成。

    Args:
        config_file: 配置文件路径，默认使用 SCRIPT_DIR/config.json
        auto_create: 为 True 时配置文件不存在则自动创建；为 False 时返回默认配置

    Returns:
        dict: 合并后的配置字典
    """
    cfg_file = Path(config_file) if config_file else CONFIG_FILE

    # 如果路径是目录，删除它并重新创建文件
    if cfg_file.exists() and cfg_file.is_dir():
        import shutil
        shutil.rmtree(cfg_file)
        print(f"已删除目录: {cfg_file}（将重新创建为配置文件）", file=sys.stderr)

    if not cfg_file.exists():
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        _apply_migrations(cfg)
        _apply_env_overrides(cfg)
        _fix_docker_paths(cfg)
        if auto_create:
            save_config(cfg, cfg_file)
            print(f"已自动生成配置文件: {cfg_file}", file=sys.stderr)
        # num_thread 以 0（自动）写入文件，线程检测仅在内存中生效，
        # 保证后续 CPU_THREADS 环境变量与硬件变化仍能生效
        _auto_detect(cfg)
        return cfg

    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"配置文件 {cfg_file} 格式错误，无法解析 JSON: {e}")

    # ── 迁移 / 环境变量覆盖 / 路径修正 / 自动检测 ──────────
    # 注意顺序：env 覆盖必须在 _auto_detect 之前，否则 num_thread
    # 被自动检测填充后 CPU_THREADS 将永远无法生效
    _apply_migrations(cfg)
    _apply_env_overrides(cfg)
    _fix_docker_paths(cfg)
    _auto_detect(cfg)

    # ── 强制使用 VERSION 文件的版本 ─────────────────────
    cfg["version"] = get_version()

    return cfg


def _can_env_override(cfg, section, key):
    """判断配置项是否允许被环境变量覆盖

    值为空或仍等于 DEFAULT_CONFIG 默认值时视为"用户未修改"，
    允许环境变量覆盖；用户在 Web UI 中手动修改过的值优先级最高。
    """
    default_val = DEFAULT_CONFIG.get(section, {}).get(key)
    cur = cfg.get(section, {}).get(key)
    return cur is None or cur == "" or cur == default_val


def _apply_env_overrides(cfg):
    """应用环境变量覆盖（Docker 部署友好）

    优先级：Web UI 中用户手动修改的值 > 环境变量 > 默认值。
    配置值等于默认值时视为未修改，可被环境变量覆盖。

    注意：首次生成的配置文件会将环境变量值固化保存，此后再修改
    环境变量不会覆盖已固化的值（可在 Web UI 中修改，或删除
    config.json 重新生成）。
    """
    env = os.environ

    # Ollama
    if env.get("OLLAMA_BASE_URL") and _can_env_override(cfg, "ollama", "base_url"):
        cfg.setdefault("ollama", {})["base_url"] = env["OLLAMA_BASE_URL"]
    if env.get("OLLAMA_API_ENDPOINT") and _can_env_override(cfg, "ollama", "api_endpoint"):
        cfg.setdefault("ollama", {})["api_endpoint"] = env["OLLAMA_API_ENDPOINT"]
    if env.get("OLLAMA_MODEL") and _can_env_override(cfg, "ollama", "model"):
        cfg.setdefault("ollama", {})["model"] = env["OLLAMA_MODEL"]
    if env.get("OLLAMA_TIMEOUT") and _can_env_override(cfg, "ollama", "timeout"):
        cfg.setdefault("ollama", {})["timeout"] = int(env["OLLAMA_TIMEOUT"])
    if env.get("CPU_THREADS") and _can_env_override(cfg, "ollama", "num_thread"):
        cfg.setdefault("ollama", {})["num_thread"] = int(env["CPU_THREADS"])

    # 路径
    data_dir = env.get("DATA_DIR")
    if data_dir and _can_env_override(cfg, "paths", "project_dir"):
        cfg.setdefault("paths", {})["project_dir"] = data_dir
        cfg.setdefault("paths", {}).update({
            "index_file": f"{data_dir}/zectrix_photo_index.txt",
            "score_db": f"{data_dir}/zectrix_scores.sqlite",
            "shown_file": f"{data_dir}/zectrix_shown.txt",
            "output_file": f"{data_dir}/zectrix_today.jpg",
        })
    if env.get("PHOTO_DIR") and _can_env_override(cfg, "paths", "photo_dir"):
        cfg.setdefault("paths", {})["photo_dir"] = env["PHOTO_DIR"]

    # 推送（同时更新 devices 和 push_settings，确保兼容）
    if env.get("API_KEY") and _can_env_override(cfg, "push_settings", "api_key"):
        cfg.setdefault("push_settings", {})["api_key"] = env["API_KEY"]
        devices = cfg.get("devices", [])
        if devices:
            devices[0]["api_key"] = env["API_KEY"]
    if env.get("DEVICE_MAC") and _can_env_override(cfg, "push_settings", "device_mac"):
        cfg.setdefault("push_settings", {})["device_mac"] = env["DEVICE_MAC"]
        devices = cfg.get("devices", [])
        if devices:
            devices[0]["device_mac"] = env["DEVICE_MAC"]

    # 日志
    if env.get("LOG_LEVEL") and _can_env_override(cfg, "logging", "level"):
        cfg.setdefault("logging", {})["level"] = env["LOG_LEVEL"]

    # 规则过滤（布尔值无"空"状态：等于默认值即视为未修改）
    if env.get("FILTER_ENABLED") and _can_env_override(cfg, "filter_rules", "enabled"):
        val = env["FILTER_ENABLED"].lower()
        cfg.setdefault("filter_rules", {})["enabled"] = val in ("true", "1", "yes")


def _apply_migrations(cfg):
    """旧版本配置自动迁移（幂等，每次加载配置时执行）"""
    # Ollama 原生端点应为 /api/generate，旧版误设为 /v1/chat/completions 时自动修正
    ollama_ep = cfg.get("ollama", {}).get("api_endpoint", "")
    if ollama_ep == "/v1/chat/completions" and not os.environ.get("OLLAMA_API_ENDPOINT"):
        cfg.setdefault("ollama", {})["api_endpoint"] = "/api/generate"

    # 极趣云 API 地址迁移：api.zectrix.com → cloud.zectrix.com
    _OLD_API = "api.zectrix.com"
    _NEW_API = "cloud.zectrix.com"
    ps = cfg.get("push_settings", {})
    if _OLD_API in ps.get("api_base", ""):
        ps["api_base"] = ps["api_base"].replace(_OLD_API, _NEW_API)
    for d in cfg.get("devices", []):
        if _OLD_API in d.get("api_base", ""):
            d["api_base"] = d["api_base"].replace(_OLD_API, _NEW_API)

    # 迁移：移除误伤真实相机照片的排除正则
    # IMG_YYYYMMDD_HHMMSS(_BURST) 是小米/三星/谷歌相机的默认命名，不是截图
    _BAD_REGEXES = {"^IMG_\\d{8}_\\d{6}", "^IMG_\\d{8}_\\d{6}_BURST"}
    fr = cfg.get("filter_rules", {})
    regexes = fr.get("exclude_filename_regex")
    if isinstance(regexes, list):
        cleaned = [r for r in regexes if r not in _BAD_REGEXES]
        if len(cleaned) != len(regexes):
            fr["exclude_filename_regex"] = cleaned


def _auto_detect(cfg):
    """自动检测系统参数"""
    # CPU 线程数：0 表示自动检测
    threads = cfg.get("ollama", {}).get("num_thread", 0)
    if threads is None or threads == 0:
        detected = os.cpu_count() or 4
        auto_threads = max(2, detected - 2) if detected > 4 else detected
        cfg.setdefault("ollama", {})["num_thread"] = auto_threads


def _is_in_docker():
    """检测是否在 Docker 容器中运行"""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as f:
            content = f.read()
            if "docker" in content or "containerd" in content:
                return True
    except (OSError, IOError):
        pass
    return False


def _fix_docker_paths(cfg):
    """非 Docker 环境下，将 Docker 风格路径自动修正为本地路径

    仅匹配 Docker 默认路径前缀（/data、/photos、/app/），
    避免误改用户自定义路径；/data/ 下的自定义文件名会保留。
    """
    if _is_in_docker():
        return

    data_dir = str(SCRIPT_DIR / "data")
    paths = cfg.get("paths", {})

    if paths.get("project_dir") == "/data":
        paths["project_dir"] = data_dir

    for key in ("index_file", "score_db", "shown_file", "output_file"):
        current = str(paths.get(key, ""))
        if current.startswith("/data/") and len(current) > len("/data/"):
            paths[key] = f"{data_dir}/{current.split('/')[-1]}"

    font = str(paths.get("font_file", ""))
    if font.startswith("/app/") and len(font) > len("/app/"):
        paths["font_file"] = str(SCRIPT_DIR / font.split("/")[-1])

    # photo_dir: Docker 默认 /photos，本地不存在时尝试常见目录
    photo_dir = str(paths.get("photo_dir", "")).rstrip("/")
    if photo_dir == "/photos" and not Path("/photos").exists():
        candidates = [
            str(SCRIPT_DIR / "photos"),
            str(Path.home() / "Pictures"),
            str(Path.home() / "photos"),
        ]
        for c in candidates:
            if Path(c).exists():
                paths["photo_dir"] = c
                break

    cfg["paths"] = paths


def get_db_schema_ddl():
    """返回 photo_scores 表的 canonical DDL"""
    return DB_SCHEMA_DDL


def generate_smart_config(env_info=None, paths_info=None):
    """
    基于环境检测结果生成智能配置。

    Args:
        env_info: detect_environment() 返回值（None 则自动调用）
        paths_info: detect_paths() 返回值（None 则自动调用）

    Returns:
        dict: 完整的配置字典
    """
    import copy
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    if env_info is None:
        try:
            from env_detect import detect_environment
            env_info = detect_environment()
        except Exception:
            env_info = {}

    if paths_info is None:
        try:
            from env_detect import detect_paths
            paths_info = detect_paths(env_info)
        except Exception:
            paths_info = {}

    # 自动设置路径
    if paths_info.get("project_dir"):
        cfg["paths"]["project_dir"] = paths_info["project_dir"]
        pd = paths_info["project_dir"]
        cfg["paths"]["index_file"] = f"{pd}/zectrix_photo_index.txt"
        cfg["paths"]["score_db"] = f"{pd}/zectrix_scores.sqlite"
        cfg["paths"]["shown_file"] = f"{pd}/zectrix_shown.txt"
        cfg["paths"]["output_file"] = f"{pd}/zectrix_today.jpg"

    if paths_info.get("photo_dir"):
        cfg["paths"]["photo_dir"] = paths_info["photo_dir"]

    if paths_info.get("font_file"):
        cfg["paths"]["font_file"] = paths_info["font_file"]

    # 自动设置 CPU 线程
    if env_info.get("cpu_threads_auto"):
        cfg["ollama"]["num_thread"] = env_info["cpu_threads_auto"]

    # Docker 环境：Ollama 默认指向宿主机
    if env_info.get("in_docker"):
        cfg["ollama"]["base_url"] = os.environ.get(
            "OLLAMA_BASE_URL", "http://host.docker.internal:11434"
        )

    return cfg


def save_config(cfg, config_file=None):
    """保存配置到 JSON 文件"""
    cfg_file = Path(config_file) if config_file else CONFIG_FILE
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    # 确保保存时使用 VERSION 文件的版本号
    cfg["version"] = get_version()
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg_file
