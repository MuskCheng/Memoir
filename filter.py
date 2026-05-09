"""
filter_rules.py — 规则过滤层
用传统图像处理（Pillow）毫秒级判断照片基础质量，
不合格的照片直接标记 rule_pass=0，不送入 VLM 推理，节省成本。

主要过滤：
1. NAS缩略图、系统缓存图片
2. 手机截图、录屏
3. 表情包、壁纸、图标
4. 动图（WEBP/APNG）
5. 过小尺寸图片（缩略图）
6. 低质量图片（模糊、过暗/过曝、色彩单一）
"""

import os
import sys
import re
import struct
import numpy as np
from PIL import Image

# ─── 配置加载 ────────────────────────────────────────────────────
from config_module import load_config, DEFAULT_CONFIG

def _get_filter_config():
    """从 config_module 获取过滤配置，支持热加载"""
    cfg = load_config(auto_create=False)
    fr = cfg.get("filter_rules", {})
    default_fr = DEFAULT_CONFIG.get("filter_rules", {})
    
    return {
        "enabled": fr.get("enabled", default_fr.get("enabled", True)),
        "min_file_size": fr.get("min_file_size_kb", default_fr.get("min_file_size_kb", 50)) * 1024,
        "min_aspect_ratio": fr.get("min_aspect_ratio", default_fr.get("min_aspect_ratio", 0.20)),
        "max_aspect_ratio": fr.get("max_aspect_ratio", default_fr.get("max_aspect_ratio", 5.0)),
        "min_blur_score": fr.get("min_blur_score", default_fr.get("min_blur_score", 30)),
        "min_brightness": fr.get("min_brightness", default_fr.get("min_brightness", 0.03)),
        "max_brightness": fr.get("max_brightness", default_fr.get("max_brightness", 0.97)),
        "min_color_variance": fr.get("min_color_variance", default_fr.get("min_color_variance", 0.0005)),
        "min_dimension": fr.get("min_dimension", default_fr.get("min_dimension", 200)),
        "exclude_filename_patterns": fr.get("exclude_filename_patterns", default_fr.get("exclude_filename_patterns", [])),
        "exclude_filename_regex": fr.get("exclude_filename_regex", default_fr.get("exclude_filename_regex", [])),
        "exclude_aspect_ratios": fr.get("exclude_aspect_ratios", default_fr.get("exclude_aspect_ratios", [])),
        "exclude_resolutions": fr.get("exclude_resolutions", default_fr.get("exclude_resolutions", [])),
        "max_thumbnail_size": fr.get("max_thumbnail_size", default_fr.get("max_thumbnail_size", 300)),
        "detect_animated_webp": fr.get("detect_animated_webp", default_fr.get("detect_animated_webp", True)),
        "detect_animated_png": fr.get("detect_animated_png", default_fr.get("detect_animated_png", True)),
        "exclude_no_exif": fr.get("exclude_no_exif", default_fr.get("exclude_no_exif", False)),
        "exclude_software_tags": fr.get("exclude_software_tags", default_fr.get("exclude_software_tags", [])),
    }


def _check_filename_patterns(filename: str, patterns: list) -> bool:
    """检查文件名是否包含指定模式（不区分大小写）"""
    filename_lower = filename.lower()
    for pattern in patterns:
        if pattern.lower() in filename_lower:
            return True
    return False


def _check_filename_regex(filename: str, patterns: list) -> bool:
    """检查文件名是否匹配正则表达式"""
    for pattern in patterns:
        try:
            if re.match(pattern, filename, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _check_aspect_ratio_in_list(aspect_ratio: float, exclude_list: list) -> bool:
    """检查宽高比是否在排除列表中"""
    for ratio_str in exclude_list:
        try:
            w, h = ratio_str.split(":")
            ratio = float(w) / float(h)
            # 允许5%的误差
            if abs(aspect_ratio - ratio) / ratio < 0.05:
                return True
        except (ValueError, ZeroDivisionError):
            continue
    return False


def _check_resolution_in_list(width: int, height: int, exclude_list: list) -> bool:
    """检查分辨率是否在排除列表中"""
    resolution = f"{width}x{height}"
    return resolution in exclude_list


def _is_animated_webp(img_path: str) -> bool:
    """检测WEBP是否为动图"""
    try:
        with open(img_path, 'rb') as f:
            # 读取WEBP文件头
            header = f.read(12)
            if len(header) < 12:
                return False
            
            # 检查WEBP签名
            if header[0:4] != b'RIFF' or header[8:12] != b'WEBP':
                return False
            
            # 检查是否有ANIM块（动图标志）
            f.seek(12)
            while True:
                chunk_header = f.read(8)
                if len(chunk_header) < 8:
                    break
                
                chunk_type = chunk_header[0:4]
                chunk_size = struct.unpack('<I', chunk_header[4:8])[0]
                
                if chunk_type == b'ANIM':
                    return True
                
                current_pos = f.tell()
                chunk_total = chunk_size + (1 if chunk_size % 2 == 1 else 0)
                f.seek(min(chunk_total, os.path.getsize(img_path) - current_pos), 1)
            
            return False
    except Exception:
        return False


def _is_animated_png(img_path: str) -> bool:
    """检测PNG是否为动图（APNG）"""
    try:
        with open(img_path, 'rb') as f:
            # 读取PNG文件头
            header = f.read(8)
            if len(header) < 8:
                return False
            
            # 检查PNG签名
            if header != b'\x89PNG\r\n\x1a\n':
                return False
            
            # 查找acTL块（动画控制块）
            while True:
                chunk_header = f.read(8)
                if len(chunk_header) < 8:
                    break
                
                chunk_length = struct.unpack('>I', chunk_header[0:4])[0]
                chunk_type = chunk_header[4:8]
                
                if chunk_type == b'acTL':
                    return True
                
                current_pos = f.tell()
                chunk_total = chunk_length + 4
                f.seek(min(chunk_total, os.path.getsize(img_path) - current_pos), 1)
            
            return False
    except Exception:
        return False


def _check_exif_software(img_path: str, exclude_tags: list) -> bool:
    """检查EXIF中的Software标签是否在排除列表中"""
    try:
        from PIL import ExifTags
        img = Image.open(img_path)
        exif = img.getexif()
        
        for tag_id, value in exif.items():
            tag_name = ExifTags.TAGS.get(tag_id, tag_id)
            if tag_name == 'Software' and isinstance(value, str):
                for tag in exclude_tags:
                    if tag.lower() in value.lower():
                        return True
        return False
    except Exception:
        return False


def get_laplacian_variance(img_rgb: Image.Image) -> float:
    """
    用 Laplacian 算子评估图像清晰度。
    返回方差值，越高表示越清晰。
    """
    try:
        import cv2
        arr = np.array(img_rgb.convert("L"))
        return float(cv2.Laplacian(arr, cv2.CV_64F).var())
    except ImportError:
        # 没有 OpenCV 时用 Pillow 近似评估（通过高通滤波）
        arr = np.array(img_rgb.convert("L")).astype(float)
        # 简单的高通滤波：减去模糊版本
        from PIL import ImageFilter
        blurred = np.array(img_rgb.convert("L").filter(ImageFilter.BLUR)).astype(float)
        diff = arr - blurred
        return float(np.var(diff))


def get_brightness(img_rgb: Image.Image) -> float:
    """
    返回图像平均亮度，0.0（纯黑）~ 1.0（纯白）。
    使用感知亮度公式：0.299*R + 0.587*G + 0.114*B
    """
    arr = np.array(img_rgb)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    brightness = 0.299 * r + 0.587 * g + 0.114 * b
    return float(np.mean(brightness) / 255.0)


def get_color_variance(img_rgb: Image.Image) -> float:
    """
    返回 RGB 三个通道的方差均值，方差越小说明色彩越单一（可能是纯色图/渐变图）。
    """
    arr = np.array(img_rgb).astype(float) / 255.0
    variances = [float(np.var(arr[:, :, c])) for c in range(3)]
    return float(np.mean(variances))


def analyze_image_quality(img_path: str) -> dict:
    """
    主入口：对单张照片执行规则检测。
    （每次调用重新读取 config.json，配置修改后立即生效）

    返回 dict:
    {
        "rule_pass": True/False,        # 是否通过所有硬性过滤
        "rule_enabled": True/False,     # 规则过滤是否启用
        "file_size": int,               # 文件大小（字节）
        "width": int, "height": int,    # 图片尺寸
        "aspect_ratio": float,          # 宽/高比
        "blur_score": float,            # Laplacian 方差（越高越清晰）
        "brightness": float,            # 平均亮度 0.0~1.0
        "color_variance": float,        # 色彩方差
        "reject_reasons": list[str],    # 所有不通过原因列表
        "filter_category": str,         # 过滤类别：photo/screenshot/thumbnail/animated/other
    }
    """
    # 从 config_module 读取最新配置
    t = _get_filter_config()
    
    # 如果规则过滤未启用，直接返回通过
    if not t["enabled"]:
        return {
            "rule_pass": True,
            "rule_enabled": False,
            "file_size": None,
            "width": None,
            "height": None,
            "aspect_ratio": None,
            "blur_score": None,
            "brightness": None,
            "color_variance": None,
            "reject_reasons": [],
            "filter_category": "unknown",
        }
    
    MIN_FILE_SIZE = t["min_file_size"]
    MIN_ASPECT_RATIO = t["min_aspect_ratio"]
    MAX_ASPECT_RATIO = t["max_aspect_ratio"]
    MIN_BRIGHTNESS = t["min_brightness"]
    MAX_BRIGHTNESS = t["max_brightness"]
    MIN_COLOR_VARIANCE = t["min_color_variance"]
    MIN_BLUR_SCORE = t["min_blur_score"]
    MIN_DIMENSION = t["min_dimension"]
    MAX_THUMBNAIL_SIZE = t["max_thumbnail_size"]

    reject_reasons = []
    filter_category = "photo"  # 默认为照片

    # 1. 文件存在检查
    try:
        file_size = os.path.getsize(img_path)
    except OSError:
        return {
            "rule_pass": False,
            "rule_enabled": True,
            "reject_reasons": ["文件不存在或无法访问"],
            **{k: None for k in ["file_size", "width", "height",
                                  "aspect_ratio", "blur_score",
                                  "brightness", "color_variance"]},
            "filter_category": "not_found",
        }
    filename = os.path.basename(img_path)
    
    # 2. 文件大小检查
    if file_size < MIN_FILE_SIZE:
        reject_reasons.append(f"文件过小({file_size // 1024}KB < {MIN_FILE_SIZE // 1024}KB)")

    # 3. 文件名模式过滤（截图、缩略图、表情包等）
    if _check_filename_patterns(filename, t["exclude_filename_patterns"]):
        reject_reasons.append(f"文件名包含排除模式")
        filter_category = "screenshot"
    
    if _check_filename_regex(filename, t["exclude_filename_regex"]):
        reject_reasons.append(f"文件名匹配排除规则")
        filter_category = "screenshot"

    # 4. 动图检测
    ext = os.path.splitext(filename)[1].lower()
    if t["detect_animated_webp"] and ext == ".webp":
        if _is_animated_webp(img_path):
            reject_reasons.append("WEBP动图")
            filter_category = "animated"
    
    if t["detect_animated_png"] and ext == ".png":
        if _is_animated_png(img_path):
            reject_reasons.append("APNG动图")
            filter_category = "animated"

    # 5. 读取图片
    try:
        img = Image.open(img_path)
        img.verify()        # 验证文件完整性
        img = Image.open(img_path)  # verify 后需重新打开
        img_rgb = img.convert("RGB")
    except Exception as e:
        return {
            "rule_pass": False,
            "rule_enabled": True,
            "reject_reasons": [f"图片读取失败: {e}"] + reject_reasons,
            "file_size": file_size,
            **{k: None for k in ["width", "height", "aspect_ratio",
                                  "blur_score", "brightness", "color_variance"]},
            "filter_category": filter_category,
        }

    width, height = img.size
    aspect_ratio = width / height

    # 6. 尺寸检查（缩略图过滤）
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        reject_reasons.append(f"尺寸过小({width}x{height} < {MIN_DIMENSION}x{MIN_DIMENSION})")
        filter_category = "thumbnail"
    
    # 检查是否为缩略图（宽高都小于阈值）
    if width < MAX_THUMBNAIL_SIZE and height < MAX_THUMBNAIL_SIZE:
        reject_reasons.append(f"疑似缩略图({width}x{height})")
        filter_category = "thumbnail"

    # 7. 宽高比检查
    if aspect_ratio < MIN_ASPECT_RATIO:
        reject_reasons.append(f"宽高比异常({aspect_ratio:.2f} < {MIN_ASPECT_RATIO})")
    if aspect_ratio > MAX_ASPECT_RATIO:
        reject_reasons.append(f"宽高比异常({aspect_ratio:.2f} > {MAX_ASPECT_RATIO})")
    
    # 检查是否为手机截图分辨率
    if _check_aspect_ratio_in_list(aspect_ratio, t["exclude_aspect_ratios"]):
        reject_reasons.append(f"疑似截图宽高比({aspect_ratio:.2f})")
        filter_category = "screenshot"
    
    if _check_resolution_in_list(width, height, t["exclude_resolutions"]):
        reject_reasons.append(f"疑似截图分辨率({width}x{height})")
        filter_category = "screenshot"

    # 8. EXIF软件标签检查
    if t["exclude_software_tags"]:
        if _check_exif_software(img_path, t["exclude_software_tags"]):
            reject_reasons.append("EXIF软件标签匹配")
            filter_category = "edited"

    # 9. 亮度检测
    brightness = get_brightness(img_rgb)
    if brightness < MIN_BRIGHTNESS:
        reject_reasons.append(f"亮度过低({brightness:.2f} < {MIN_BRIGHTNESS})")
    if brightness > MAX_BRIGHTNESS:
        reject_reasons.append(f"亮度过高({brightness:.2f} > {MAX_BRIGHTNESS})")

    # 10. 色彩方差检测
    color_variance = get_color_variance(img_rgb)
    if color_variance < MIN_COLOR_VARIANCE:
        reject_reasons.append(f"色彩过于单一({color_variance:.5f} < {MIN_COLOR_VARIANCE})")

    # 11. 模糊检测（硬性排除严重模糊）
    blur_score = get_laplacian_variance(img_rgb)
    if blur_score < MIN_BLUR_SCORE:
        reject_reasons.append(f"画面模糊({blur_score:.1f} < {MIN_BLUR_SCORE})")

    rule_pass = len(reject_reasons) == 0

    return {
        "rule_pass": rule_pass,
        "rule_enabled": True,
        "file_size": file_size,
        "width": width,
        "height": height,
        "aspect_ratio": aspect_ratio,
        "blur_score": blur_score,
        "brightness": brightness,
        "color_variance": color_variance,
        "reject_reasons": reject_reasons,
        "filter_category": filter_category,
    }


def batch_filter(img_paths: list[str]) -> list[tuple[str, dict]]:
    """
    批量处理一组照片路径，返回 [(path, result), ...]。
    适合在 score_photos.py 中批量预检。
    """
    results = []
    for path in img_paths:
        results.append((path, analyze_image_quality(path)))
    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python filter_rules.py <图片路径>")
        sys.exit(1)

    path = sys.argv[1]
    result = analyze_image_quality(path)

    print(f"图片: {path}")
    print(f"规则过滤: {'启用' if result['rule_enabled'] else '禁用'}")
    print(f"规则通过: {'✅' if result['rule_pass'] else '❌'}")
    print(f"过滤类别: {result.get('filter_category', 'unknown')}")
    if result["reject_reasons"]:
        for r in result["reject_reasons"]:
            print(f"  不通过原因: {r}")
    if result["rule_enabled"] and result["file_size"]:
        print(f"  文件大小: {result['file_size'] // 1024}KB")
        if result["width"]:
            print(f"  尺寸: {result['width']}x{result['height']}")
            print(f"  宽高比: {result['aspect_ratio']:.2f}")
        if result["blur_score"]:
            print(f"  模糊分: {result['blur_score']:.1f}")
            print(f"  亮度: {result['brightness']:.3f}")
            print(f"  色彩方差: {result['color_variance']:.5f}")