#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
照片索引构建工具
扫描照片目录，提取 EXIF 信息，过滤真实照片并输出索引文件。

用法:
  python3 build_index.py /path/to/photos -o /path/to/index.txt
  python3 build_index.py /path/to/photos                     # 使用配置文件中的默认路径
"""

import os
import sys
import time
import argparse
import logging
from pathlib import Path
from PIL import Image, ExifTags

# ─── Windows UTF-8 强制输出 ─────────────────────────────────────
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─── 配置加载 ────────────────────────────────────────────────────
from config_module import load_config, DEFAULT_CONFIG

# ─── 日志系统 ────────────────────────────────────────────────────
def setup_logging():
    """设置日志系统（如果已配置则跳过，避免 force=True 关闭 sys.stdout）"""
    # 检查是否已有活跃的 log handler（score.py 已配置时就跳过）
    root_logger = logging.getLogger()
    if root_logger.hasHandlers() and any(
        isinstance(h, logging.StreamHandler) for h in root_logger.handlers
    ):
        return logging.getLogger(__name__)
    
    cfg = load_config(auto_create=False)
    log_file = cfg.get("logging", {}).get("file", "photopush.log")
    log_level = cfg.get("logging", {}).get("level", "INFO")
    log_format = cfg.get("logging", {}).get("format", "%(asctime)s [%(levelname)s] %(message)s")
    
    log_file_path = Path(__file__).parent / log_file
    
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

# ─── 从 config_module 获取配置 ─────────────────────────────────
def _get_build_config():
    """获取索引构建相关配置"""
    cfg = load_config(auto_create=False)
    default_cfg = DEFAULT_CONFIG
    
    extensions = set(cfg.get("photo_extensions", default_cfg.get("photo_extensions", [])))
    if not extensions:
        extensions = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.bmp', '.tiff'}
    
    exclude_dirs = set(cfg.get("exclude_dirs", default_cfg.get("exclude_dirs", [])))
    if not exclude_dirs:
        exclude_dirs = {"@eaDir", "#recycle", ".thumbnails", ".DS_Store", "@__thumb"}
    
    index_file = cfg.get("paths", {}).get("index_file", "")
    progress_interval = cfg.get("processing", {}).get("progress_interval", 2000)
    
    return {
        "extensions": extensions,
        "exclude_dirs": exclude_dirs,
        "index_file": index_file,
        "progress_interval": progress_interval,
    }

# 获取配置
_build_cfg = _get_build_config()
PHOTO_EXTENSIONS = _build_cfg["extensions"]
EXCLUDE_DIRS = _build_cfg["exclude_dirs"]
DEFAULT_INDEX_FILE = _build_cfg["index_file"]
PROGRESS_INTERVAL = _build_cfg["progress_interval"]


def get_photo_info(image_path):
    """
    判断是否为有效照片，提取拍摄日期。
    放宽策略：只要 Pillow 能打开就收录，日期优先 EXIF，其次文件修改时间。
    """
    try:
        img = Image.open(image_path)
        img.verify()  # 确认不是损坏文件
        
        # 重新打开（verify 后需要重新打开才能读 EXIF）
        img = Image.open(image_path)
        
        shoot_date = None
        
        # 先尝试从 EXIF 获取日期
        try:
            exif = img.getexif()
            for tag_id, value in exif.items():
                tag_name = ExifTags.TAGS.get(tag_id, tag_id)
                if tag_name in ('DateTimeOriginal', 'DateTime') and isinstance(value, str):
                    if len(value) >= 10:
                        shoot_date = value[:10].replace(':', '-')
                        break
        except Exception:
            pass
        
        # EXIF 无日期则用文件修改时间
        if not shoot_date:
            timestamp = os.path.getmtime(image_path)
            shoot_date = time.strftime('%Y-%m-%d', time.localtime(timestamp))
        
        return True, shoot_date
    except Exception:
        return False, None


def scan_photos(photo_dir, extensions=None):
    """扫描目录，返回所有真实照片的 (日期, 路径) 列表"""
    if extensions is None:
        extensions = PHOTO_EXTENSIONS
    
    if not os.path.isdir(photo_dir):
        log.error(f"目录不存在: {photo_dir}")
        sys.exit(1)
    
    valid_records = []
    processed_count = 0
    
    log.info(f"开始扫描: {photo_dir}")
    
    try:
        for root, dirs, files in os.walk(photo_dir):
            # 排除系统目录
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext not in extensions:
                    continue
                
                processed_count += 1
                full_path = os.path.join(root, file)
                
                try:
                    is_real, p_date = get_photo_info(full_path)
                except Exception:
                    continue
                
                if is_real and p_date:
                    valid_records.append(f"{p_date}|{full_path.replace(os.sep, '/')}")
                
                if processed_count % PROGRESS_INTERVAL == 0:
                    log.info(f"已处理 {processed_count} 张，收录 {len(valid_records)} 张真实照片...")
    except PermissionError:
        log.warning(f"无权限访问目录: {root}，跳过")
    except OSError as e:
        log.warning(f"遍历目录出错: {e}，跳过")
    
    log.info(f"扫描完成: 共处理 {processed_count} 张，收录 {len(valid_records)} 张")
    return valid_records


def main():
    parser = argparse.ArgumentParser(description="照片索引构建工具")
    parser.add_argument("photo_dir", nargs='?', help="照片目录路径（可选，默认使用配置文件中的路径）")
    parser.add_argument("-o", "--output", help="输出索引文件路径（默认使用配置文件中的路径）")
    args = parser.parse_args()
    
    # 获取照片目录
    photo_dir = args.photo_dir
    if not photo_dir:
        cfg = load_config(auto_create=False)
        photo_dir = cfg.get("paths", {}).get("photo_dir", "")
        if not photo_dir:
            log.error("未指定照片目录，请通过命令行参数或配置文件指定")
            sys.exit(1)
    
    # 获取输出文件路径
    output_file = args.output
    if not output_file:
        output_file = DEFAULT_INDEX_FILE
    
    # 扫描照片
    records = scan_photos(photo_dir)
    
    # 输出结果
    output_lines = "\n".join(records)
    
    if output_file:
        # 确保输出目录存在
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(output_lines + "\n")
        log.info(f"索引已写入: {output_file}（共 {len(records)} 条）")
    else:
        print(output_lines)


if __name__ == "__main__":
    main()