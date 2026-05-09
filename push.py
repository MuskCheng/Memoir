#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZecTrix Note4 岁月史书推送系统 v1.0
整合 photo-analyzer 的工程化优点：
- 集中式配置管理 (config.json)
- 完善日志系统 (logging 模块)
- 保留年份公平权重选片逻辑
- 保留水印功能
- 与优化后的 score_photos.py 共享数据库
"""

import os
import sys
import json
import time
import requests
import sqlite3
import logging
import argparse
import io
from pathlib import Path
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont

# ─── Windows UTF-8 强制输出 ───────────────────────────────────────
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─── 配置加载 ────────────────────────────────────────────────────
from config_module import load_config, SCRIPT_DIR, CONFIG_FILE

CFG = load_config()

# ─── 日志系统 ────────────────────────────────────────────────────
def setup_logging():
    log_file = CFG.get("logging", {}).get("file", "photopush.log")
    log_level = CFG.get("logging", {}).get("level", "INFO")
    log_format = CFG.get("logging", {}).get("format", "%(asctime)s [%(levelname)s] %(message)s")
    
    log_file_path = SCRIPT_DIR / log_file
    
    logging.basicConfig(
        level=getattr(logging, log_level),
        format=log_format,
        handlers=[
            logging.FileHandler(log_file_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True
    )
    return logging.getLogger(__name__)

log = setup_logging()

# ─── 配置常量（从 config.json 读取）────────────────────────────
# 路径配置
PHOTO_DIR = CFG.get("paths", {}).get("photo_dir", "")
PROJECT_DIR = CFG.get("paths", {}).get("project_dir", "")
INDEX_FILE = CFG.get("paths", {}).get("index_file", "")
SCORE_DB = CFG.get("paths", {}).get("score_db", SCRIPT_DIR / "zectrix_scores.sqlite")
SHOWN_FILE = CFG.get("paths", {}).get("shown_file", SCRIPT_DIR / "zectrix_shown.txt")
FONT_FILE = CFG.get("paths", {}).get("font_file", SCRIPT_DIR / "ark-pixel-12px-monospaced-zh_cn.ttf")
OUTPUT_FILE = CFG.get("paths", {}).get("output_file", SCRIPT_DIR / "zectrix_today.jpg")

# 推送配置（设备列表从 config 获取）
# 为保持向后兼容，push_settings 作为默认设备配置
_DEFAULT_DEVICE = CFG.get("devices", [])
if not _DEFAULT_DEVICE:
    # 从旧 push_settings 构建设备
    ps = CFG.get("push_settings", {})
    _DEFAULT_DEVICE = [{
        "name": "默认设备",
        "device_mac": ps.get("device_mac", ""),
        "api_key": ps.get("api_key", ""),
        "api_base": ps.get("api_base", "https://api.zectrix.com/open/v1/devices"),
        "page_id": ps.get("page_id", 1),
        "dither": ps.get("dither", True),
    }]

CLEANUP_OLD = CFG.get("push_settings", {}).get("cleanup_old_images", True)

def get_device_config(name=None):
    """按名称获取设备配置，name=None 返回第一个"""
    devices = CFG.get("devices", _DEFAULT_DEVICE)
    if not devices:
        return None
    if name:
        for d in devices:
            if d.get("name") == name:
                return d
        log.warning(f"设备 '{name}' 未找到，使用第一个")
    return devices[0]

def get_device_list():
    """获取所有设备名称列表"""
    devices = CFG.get("devices", _DEFAULT_DEVICE)
    return [d.get("name", "未命名") for d in devices]

# 评分配置
USE_SCORE_RANKING = CFG.get("scoring", {}).get("min_score_threshold", 0) > 0 or CFG.get("scoring", {}).get("year_bonus_enabled", True)
SCORE_MIN_THRESHOLD = CFG.get("scoring", {}).get("min_score_threshold", 0)
SCORE_DECAY_FACTOR = CFG.get("scoring", {}).get("decay_factor", 0.15)

# ─── 水印配置 ────────────────────────────────────────────────────
WATERMARK_ENABLED = CFG.get("image_processing", {}).get("watermark_enabled", True)
WATERMARK_FONT_SIZE = CFG.get("image_processing", {}).get("watermark_font_size", 12)
WATERMARK_POSITION = CFG.get("image_processing", {}).get("watermark_position", "bottom_right")
WATERMARK_TEXT_FORMAT = CFG.get("image_processing", {}).get("watermark_text_format", "{year}年{month}月{day}日")

# ─── 加载评分数据 ────────────────────────────────────────────────
def load_score_db():
    """从 SQLite 加载评分数据到内存字典"""
    if not os.path.exists(SCORE_DB):
        log.warning(f"评分数据库不存在: {SCORE_DB}")
        return {}
    
    try:
        conn = sqlite3.connect(SCORE_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT file_path, vlm_score, vlm_scene FROM photo_scores WHERE vlm_done = 1"
        ).fetchall()
        conn.close()
        
        score_map = {}
        for row in rows:
            score_map[row["file_path"]] = {
                "score": row["vlm_score"],
                "scene": row["vlm_scene"]
            }
        
        log.info(f"已加载 {len(score_map)} 条评分数据")
        return score_map
    except Exception as e:
        log.error(f"加载评分数据库失败: {e}")
        return {}

# ─── 计算加权分数 ────────────────────────────────────────────────
def get_weighted_score(score_entry, file_path, shown_set):
    """计算加权选中概率（评分 + 年份公平 + 去重）"""
    if not score_entry:
        return 0  # 无评分的照片不应被选中
    
    base_score = score_entry.get("score", 5.0)
    
    # 评分权重（归一化到 0-1）
    score_weight = base_score / 10.0
    
    # 去重惩罚
    decay = 1.0
    if file_path in shown_set:
        decay = SCORE_DECAY_FACTOR
    
    # 年份公平奖励（在 load_index 中处理）
    # 这里只返回基础权重
    return score_weight * decay

# ─── 年份公平 + 分数加权选片 ───────────────────────────────────
def pick_by_weighted_scores(candidates_by_year, shown_set, score_map):
    """年份公平 + 分数加重的轮盘赌选片，返回 (file_path, shoot_date) 或 None"""
    if not candidates_by_year:
        return None

    # 收集所有候选照片，计算加权分数
    all_candidates = []
    for year, photos in candidates_by_year.items():
        for photo_path, shoot_date in photos:
            score_entry = score_map.get(photo_path)
            if not score_entry:
                continue  # 只从已评分照片中选择
            weight = get_weighted_score(score_entry, photo_path, shown_set)

            # 年份公平：给历史年份更高权重
            year_bonus = 1.0
            if CFG.get("scoring", {}).get("year_bonus_enabled", True):
                current_year = datetime.now().year
                if year < current_year:
                    year_bonus = 1.0 + (current_year - year) * 0.1

            final_weight = weight * year_bonus
            all_candidates.append((photo_path, shoot_date, final_weight))

    if not all_candidates:
        return None

    # 轮盘赌选择
    total_weight = sum(w for _, _, w in all_candidates)
    if total_weight <= 0:
        # 所有权重为0，随机选择
        import random
        item = random.choice(all_candidates)
        return item[0], item[1]

    import random
    r = random.uniform(0, total_weight)
    cumulative = 0
    for photo_path, shoot_date, weight in all_candidates:
        cumulative += weight
        if cumulative >= r:
            return photo_path, shoot_date

    # 兜底：返回最后一个
    last = all_candidates[-1]
    return last[0], last[1]

# ─── 加载索引文件 ────────────────────────────────────────────────
def _convert_host_path(file_path):
    """将宿主机路径转换为容器内路径（Docker 环境）"""
    import re
    # 匹配 Windows 路径格式：D:/photos/xxx.jpg 或 D:\photos\xxx.jpg
    m = re.match(r'^([A-Za-z]):[/\\](.+)$', file_path)
    if m:
        # 转换为容器内路径 /photos/xxx.jpg
        relative_path = m.group(2).replace('\\', '/')
        container_photo_dir = os.environ.get("PHOTO_DIR", "/photos")
        return f"{container_photo_dir}/{relative_path}"
    return file_path

def load_index(index_file):
    """加载索引文件，按年份分组，返回 {year: [(file_path, shoot_date), ...]}"""
    if not os.path.exists(index_file):
        log.error(f"索引文件不存在: {index_file}")
        return {}

    candidates_by_year = {}

    with open(index_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            parts = line.split("|")
            if len(parts) < 2:
                continue

            shoot_date = parts[0].strip()
            file_path = parts[1].strip()

            # 转换宿主机路径为容器内路径
            file_path = _convert_host_path(file_path)

            if not os.path.exists(file_path):
                continue

            # 解析年份
            year = None
            if shoot_date and len(shoot_date) >= 4:
                try:
                    year = int(shoot_date[:4])
                except ValueError:
                    pass

            if year:
                if year not in candidates_by_year:
                    candidates_by_year[year] = []
                candidates_by_year[year].append((file_path, shoot_date))

    log.info(f"索引加载完成: {sum(len(v) for v in candidates_by_year.values())} 张照片，{len(candidates_by_year)} 个年份")
    return candidates_by_year

# ─── 加载已推送记录 ─────────────────────────────────────────────
def load_shown_set(shown_file):
    """加载已推送照片集合"""
    shown_set = set()
    if os.path.exists(shown_file):
        with open(shown_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    shown_set.add(line)
    return shown_set

def save_shown_set(shown_file, shown_set):
    """保存已推送照片集合"""
    with open(shown_file, "w", encoding="utf-8") as f:
        for path in shown_set:
            f.write(path + "\n")

# ─── 图片处理 ────────────────────────────────────────────────────
def add_watermark(img, shoot_date):
    """添加水印（右下角黑色小铭牌，白字显示日期）"""
    if not WATERMARK_ENABLED:
        return img
    
    try:
        # 解析日期
        year, month, day = None, None, None
        if shoot_date and len(shoot_date) >= 10:
            try:
                dt = datetime.strptime(shoot_date, "%Y-%m-%d")
                year, month, day = dt.year, dt.month, dt.day
            except ValueError:
                pass
        
        if not year:
            return img
        
        # 生成水印文字
        watermark_text = WATERMARK_TEXT_FORMAT.format(year=year, month=month, day=day)
        
        # 加载字体
        try:
            font = ImageFont.truetype(FONT_FILE, WATERMARK_FONT_SIZE)
        except Exception:
            font = ImageFont.load_default()
        
        # 创建绘图对象
        draw = ImageDraw.Draw(img)
        
        # 计算文字大小
        bbox = draw.textbbox((0, 0), watermark_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        # 计算位置（右下角，留 10px 边距）
        margin = 10
        x = img.width - text_width - margin
        y = img.height - text_height - margin
        
        # 绘制黑色背景
        padding = 4
        draw.rectangle(
            [x - padding, y - padding, x + text_width + padding, y + text_height + padding],
            fill=(0, 0, 0)
        )
        
        # 绘制白色文字
        draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255))
        
        return img
    except Exception as e:
        log.warning(f"添加水印失败: {e}")
        return img

MAX_IMAGE_SIZE = 50 * 1024 * 1024

def process_image(img_path, output_path, shoot_date, cfg=None):
    """处理图片：缩放 + 添加水印"""
    if cfg is None:
        cfg = CFG
    try:
        file_size = os.path.getsize(img_path)
        if file_size > MAX_IMAGE_SIZE:
            log.error(f"图片文件过大 ({file_size / 1024 / 1024:.1f}MB)，跳过: {img_path}")
            return False
        img = Image.open(img_path)

        # 转换色彩模式
        if img.mode not in ("RGB",):
            img = img.convert("RGB")

        # 从配置读取 EPD 输出分辨率
        img_proc = cfg.get("image_processing", {})
        out_w = img_proc.get("output_width", 400)
        out_h = img_proc.get("output_height", 300)
        img = img.resize((out_w, out_h), Image.LANCZOS)

        # 添加水印
        img = add_watermark(img, shoot_date)

        # 保存（使用配置的 JPEG 质量）
        jpeg_q = img_proc.get("jpeg_quality", 80)
        img.save(output_path, "JPEG", quality=jpeg_q)
        
        log.info(f"图片处理完成: {output_path}")
        return True
    except Exception as e:
        log.error(f"图片处理失败 {img_path}: {e}")
        return False

# ─── 推送功能 ────────────────────────────────────────────────────
def push_to_device(img_path, device_config=None):
    """推送图片到指定设备"""
    if device_config is None:
        device_config = get_device_config()
    if not device_config:
        log.error("没有可用设备配置")
        return False
    
    api_key = device_config.get("api_key", "")
    device_mac = device_config.get("device_mac", "")
    api_base = device_config.get("api_base", "https://api.zectrix.com/open/v1/devices")
    page_id = device_config.get("page_id", 1)
    dither = device_config.get("dither", True)
    
    if not api_key or not device_mac:
        log.error(f"设备 '{device_config.get('name', '?')}' 的 API_KEY 或 DEVICE_MAC 未配置")
        return False
    
    url = f"{api_base}/{device_mac}/display/image"

    safe_img_path = os.path.realpath(img_path)
    if not os.path.isfile(safe_img_path):
        log.error(f"文件不存在或路径无效: {img_path}")
        return False

    try:
        with open(safe_img_path, "rb") as f:
            files = {"image": f}
            data = {
                "pageId": page_id,
                "dither": "true" if dither else "false"
            }
            headers = {"Authorization": f"Bearer {api_key}"}
            
            masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "****"
            log.debug(f"请求头 Authorization: Bearer {masked_key}")

            resp = requests.post(url, files=files, data=data, headers=headers, timeout=30)
            resp.raise_for_status()

            log.info(f"推送到 '{device_config.get('name', '?')}' 成功")
            return True
    except Exception as e:
        log.error(f"推送到 '{device_config.get('name', '?')}' 失败: {e}")
        return False

def cleanup_old_images(project_dir):
    """清理旧的推送图片"""
    if not CLEANUP_OLD or not project_dir:
        return
    
    try:
        for f in os.listdir(project_dir):
            if f.endswith(".jpg") and f != "zectrix_today.jpg":
                os.remove(os.path.join(project_dir, f))
        log.info("旧图片清理完成")
    except Exception as e:
        log.warning(f"清理旧图片失败: {e}")

# ─── 主流程 ──────────────────────────────────────────────────────
def get_history_image():
    """主选片流程"""
    log.info("=" * 50)
    log.info("开始选片流程")

    # 1. 加载索引
    candidates_by_year = load_index(INDEX_FILE)
    if not candidates_by_year:
        log.error("没有可用的照片索引")
        return None

    # 2. 加载已推送记录
    shown_set = load_shown_set(SHOWN_FILE)

    # 每年1月1日重置
    import time as _time
    _now_year = _time.localtime().tm_year
    if os.path.exists(SHOWN_FILE):
        _mtime_year = _time.localtime(os.path.getmtime(SHOWN_FILE)).tm_year
        if _mtime_year < _now_year:
            log.info(f"📅 新年重置: {_mtime_year} → {_now_year}")
            shown_set.clear()
            save_shown_set(SHOWN_FILE, shown_set)

    # 3. 加载评分数据（如果启用）
    score_map = {}
    if USE_SCORE_RANKING:
        score_map = load_score_db()

    # 4. 选片
    selected_photo = None
    shoot_date = None

    if score_map and USE_SCORE_RANKING:
        # 使用评分加权选片
        log.info("使用评分加权选片")
        result = pick_by_weighted_scores(candidates_by_year, shown_set, score_map)
        if result:
            selected_photo, shoot_date = result
    else:
        # 降级：随机选择（仅从已评分照片）
        log.info("降级：仅从已评分照片中随机选择")
        import random
        scored_photos = [(p, sd) for photos in candidates_by_year.values() for p, sd in photos if p in score_map]
        if scored_photos:
            selected_photo, shoot_date = random.choice(scored_photos)

    if not selected_photo:
        log.error("选片失败")
        return None

    log.info(f"选中照片: {os.path.basename(selected_photo)}")

    # 5. 处理图片
    if not process_image(selected_photo, OUTPUT_FILE, shoot_date):
        return None

    # 6. 更新已推送记录
    shown_set.add(selected_photo)
    save_shown_set(SHOWN_FILE, shown_set)

    log.info("=" * 50)
    return OUTPUT_FILE

# ─── CLI 入口 ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ZecTrix Note4 岁月史书推送系统 v2.0")
    sub = parser.add_subparsers(dest="command")
    
    # 选片并推送（可选指定设备）
    p = sub.add_parser("push", help="选片并推送到设备")
    p.add_argument("--device", default="", help="设备名称（默认使用第一个）")
    
    # 仅选片（不推送）
    sub.add_parser("select", help="仅选片（不推送）")
    
    # 查看配置
    sub.add_parser("config", help="查看当前配置")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    if args.command == "push":
        result = get_history_image()
        if result:
            log.info(f"选片成功: {result}")
            # 推送
            device_cfg = get_device_config(args.device) if args.device else get_device_config()
            if device_cfg and device_cfg.get("device_mac") and device_cfg.get("api_key"):
                push_to_device(result, device_cfg)
            else:
                log.warning("未配置推送参数，跳过推送")
        else:
            log.error("选片失败")
    
    elif args.command == "select":
        result = get_history_image()
        if result:
            log.info(f"选片成功: {result}")
        else:
            log.error("选片失败")
    
    elif args.command == "config":
        print(json.dumps(CFG, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
