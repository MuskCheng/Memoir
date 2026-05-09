#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
照片 AI 评分系统 v1.0
整合 photo-analyzer 的工程化优点 + Memoir 的规则过滤层
- 集中式配置管理 (config.json)
- 文件哈希去重 + 断点续传
- 完善日志系统 (logging 模块)
- 优雅中断处理
- 进度预估
- 规则过滤层 (filter.py)
- VLM 评分 (Ollama qwen2.5vl:7b)
"""

import os
import sys
import json
import time
import base64
import hashlib
import sqlite3
import logging
import argparse
import signal
import io
import requests
from pathlib import Path
from datetime import datetime
from PIL import Image

# ─── Windows UTF-8 强制输出 ───────────────────────────────────────
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─── 配置加载 ────────────────────────────────────────────────────
from config_module import load_config, SCRIPT_DIR, CONFIG_FILE, get_db_schema_ddl, DEFAULT_CONFIG

CFG = load_config()

# ─── 日志系统 ────────────────────────────────────────────────────
def setup_logging():
    """设置日志系统（如果已配置则跳过，避免重复 force=True 关闭 sys.stdout）"""
    root_logger = logging.getLogger()
    if root_logger.hasHandlers() and any(
        isinstance(h, logging.StreamHandler) for h in root_logger.handlers
    ):
        return logging.getLogger(__name__)
    
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

# ─── 导入规则过滤模块 ─────────────────────────────────────────
try:
    from filter import analyze_image_quality
    RULES_AVAILABLE = True
    log.info("✅ 规则过滤模块已加载")
except ImportError:
    RULES_AVAILABLE = False
    log.warning("⚠️ 规则过滤模块未找到，将跳过规则过滤")

# ─── 数据库管理 ─────────────────────────────────────────────────
class ScoreDB:
    """评分数据库管理类（支持哈希去重 + 断点续传）"""
    
    def __init__(self, db_path=None):
        self.db_path = db_path or CFG.get("paths", {}).get("score_db", 
                                                          SCRIPT_DIR / "zectrix_scores.sqlite")
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
        log.info(f"数据库已连接: {self.db_path}")
    
    def _init_tables(self):
        """初始化数据库表结构"""
        self.conn.executescript(get_db_schema_ddl())
        self.conn.commit()
        log.info("数据库表已初始化")
    
    def file_hash(self, path):
        """计算文件哈希（路径+大小+mtime 的 MD5）"""
        try:
            stat = os.stat(path)
            key = f"{path}:{stat.st_size}:{stat.st_mtime}"
            return hashlib.md5(key.encode()).hexdigest()
        except OSError as e:
            log.error(f"计算哈希失败 {path}: {e}")
            return None
    
    def exists(self, file_hash):
        """检查文件哈希是否已存在"""
        row = self.conn.execute(
            "SELECT 1 FROM photo_scores WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        return row is not None
    
    def insert_pending(self, file_hash, file_path, file_name, file_size, mtime, 
                      shoot_date=None, year=None, month=None, day=None):
        """插入待处理记录"""
        self.conn.execute(
            """INSERT OR IGNORE INTO photo_scores 
               (file_hash, file_path, file_name, file_size, mtime, 
                shoot_date, year, month, day, vlm_done)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (file_hash, file_path, file_name, file_size, mtime,
             shoot_date, year, month, day)
        )
    
    def update_rule_result(self, file_hash, rule_result):
        """更新规则过滤结果"""
        self.conn.execute(
            """UPDATE photo_scores SET 
                rule_pass = ?, rule_blur = ?, rule_bright = ?, 
                rule_ratio = ?, rule_size = ?, rule_color_var = ?, rule_reasons = ?
               WHERE file_hash = ?""",
            (
                rule_result.get("rule_pass", 0),
                rule_result.get("blur_score"),
                rule_result.get("brightness"),
                rule_result.get("aspect_ratio"),
                rule_result.get("file_size_kb"),
                rule_result.get("color_variance"),
                json.dumps(rule_result.get("reject_reasons", []), ensure_ascii=False),
                file_hash
            )
        )
    
    def update_vlm_result(self, file_hash, vlm_result):
        """更新 VLM 评分结果"""
        self.conn.execute(
            """UPDATE photo_scores SET 
                vlm_done = 1, vlm_score = ?, vlm_desc = ?, vlm_tags = ?, 
                vlm_scene = ?, vlm_highlights = ?, vlm_issues = ?, 
                vlm_raw = ?, scored_at = ?
               WHERE file_hash = ?""",
            (
                vlm_result.get("score"),
                vlm_result.get("description", ""),
                json.dumps(vlm_result.get("tags", []), ensure_ascii=False),
                vlm_result.get("scene", ""),
                vlm_result.get("highlights", ""),
                vlm_result.get("issues", ""),
                json.dumps(vlm_result, ensure_ascii=False),
                datetime.now().isoformat(),
                file_hash
            )
        )
    
    def mark_vlm_error(self, file_hash, error_msg):
        """标记 VLM 处理失败"""
        self.conn.execute(
            "UPDATE photo_scores SET vlm_error = ? WHERE file_hash = ?",
            (error_msg, file_hash)
        )
    
    def get_pending(self, limit=None):
        """获取待处理的记录"""
        sql = "SELECT * FROM photo_scores WHERE vlm_done = 0 AND vlm_error IS NULL"
        if limit:
            sql += " LIMIT ?"
            return self.conn.execute(sql, (int(limit),)).fetchall()
        return self.conn.execute(sql).fetchall()
    
    def get_completed(self):
        """获取已完成的记录"""
        return self.conn.execute(
            "SELECT * FROM photo_scores WHERE vlm_done = 1"
        ).fetchall()
    
    def get_stats(self):
        """获取统计信息"""
        stats = {}
        for row in self.conn.execute(
            "SELECT vlm_done, COUNT(*) as cnt FROM photo_scores GROUP BY vlm_done"
        ):
            stats["done" if row[0] == 1 else "pending"] = row[1]
        
        # 规则过滤统计
        rule_pass = self.conn.execute(
            "SELECT COUNT(*) FROM photo_scores WHERE rule_pass = 1"
        ).fetchone()[0]
        stats["rule_pass"] = rule_pass
        
        return stats
    
    def get_top_rated(self, limit=20):
        """获取评分最高的照片"""
        return self.conn.execute(
            "SELECT * FROM photo_scores WHERE vlm_done = 1 ORDER BY vlm_score DESC LIMIT ?",
            (limit,)
        ).fetchall()
    
    def search_by_scene(self, scene):
        """按场景搜索"""
        return self.conn.execute(
            "SELECT * FROM photo_scores WHERE vlm_done = 1 AND vlm_scene LIKE ? ORDER BY vlm_score DESC",
            (f"%{scene}%",)
        ).fetchall()
    
    def commit(self):
        """提交事务"""
        self.conn.commit()
    
    def close(self):
        """关闭数据库连接"""
        self.conn.close()

# ─── 图片预处理 ──────────────────────────────────────────────────
def preprocess_image(img_path):
    """图片预处理（缩放 + 压缩，CPU 推理优化关键）"""
    max_side = CFG.get("image_processing", {}).get("max_side", 512)
    quality = CFG.get("image_processing", {}).get("jpeg_quality", 80)
    
    try:
        img = Image.open(img_path)
        
        # 转换色彩模式
        if img.mode not in ("RGB",):
            img = img.convert("RGB")
        
        # 等比缩放
        w, h = img.size
        if max(w, h) > max_side:
            ratio = max_side / max(w, h)
            new_w, new_h = int(w * ratio), int(h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)
        
        # 输出为 JPEG bytes
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    
    except Exception as e:
        log.warning(f"图片预处理失败 {img_path}: {e}，使用原图")
        with open(img_path, "rb") as f:
            return f.read()

def encode_image(img_path):
    """将图片编码为 base64"""
    img_bytes = preprocess_image(img_path)
    return base64.b64encode(img_bytes).decode()

# ─── VLM 推理 ────────────────────────────────────────────────────
def call_vlm(img_path, retries=None):
    """调用 Ollama VLM 进行图片评分"""
    # 从 config_module 获取默认值
    default_ollama = DEFAULT_CONFIG.get("ollama", {})
    default_processing = DEFAULT_CONFIG.get("processing", {})
    
    # 重试配置从 processing 段获取
    processing_cfg = CFG.get("processing", {})
    max_retries = retries or processing_cfg.get("max_retries", default_processing.get("max_retries", 2))
    retry_delay = processing_cfg.get("retry_delay", default_processing.get("retry_delay", 3))
    
    # Ollama 配置
    ollama_cfg = CFG.get("ollama", {})
    ollama_url = ollama_cfg.get("base_url", default_ollama.get("base_url", "http://localhost:11434"))
    api_endpoint = ollama_cfg.get("api_endpoint", default_ollama.get("api_endpoint", "/v1/chat/completions"))
    model = ollama_cfg.get("model", default_ollama.get("model", "qwen2.5vl:7b"))
    timeout = ollama_cfg.get("timeout", default_ollama.get("timeout", 200))
    
    # 构建 API 地址
    if api_endpoint.startswith("http://") or api_endpoint.startswith("https://"):
        url = api_endpoint
    elif api_endpoint.startswith("/"):
        url = ollama_url + api_endpoint
    else:
        url = ollama_url + "/" + api_endpoint
    
    # 构建提示词（不硬编码分数，引导模型自己判断）
    prompt = """请分析这张照片的质量和内容，按以下 JSON 格式输出（不要其他文字）：
{
  "score": 0~10之间的浮点数（照片质量分，考虑构图/光线/内容/情感价值，要求严格评分，不要全给高分，普通照片4~6分，好照片6~8分，精品8~10分）,
  "description": "15字内简洁描述照片内容",
  "tags": ["3-5个相关标签"],
  "scene": "人像/风景/美食/宠物/建筑/文档/夜景/其他（只选一个最合适的）",
  "highlights": "照片亮点（20字内）",
  "issues": "不足或问题（若无则填"无"）"
}"""
    
    img_b64 = encode_image(img_path)
    
    # 判断使用哪个 API 格式
    # /api/generate (Ollama 原生): 兼容性好，支持多模态
    # /v1/chat/completions (OpenAI 兼容): 某些模型(qwen2.5vl)返回空 content
    use_ollama_gen = "/api/generate" in url
    
    for attempt in range(1, max_retries + 1):
        try:
            if use_ollama_gen:
                # ── Ollama 原生 /api/generate ──────────────────────
                payload = {
                    "model": model,
                    "prompt": prompt,
                    "images": [img_b64],
                    "stream": False,
                    "options": {
                        "temperature": ollama_cfg.get("temperature", default_ollama.get("temperature", 0.1)),
                        "num_predict": ollama_cfg.get("num_predict", default_ollama.get("num_predict", 256)),
                        "num_ctx": ollama_cfg.get("num_ctx", default_ollama.get("num_ctx", 4096)),
                        "num_thread": ollama_cfg.get("num_thread", default_ollama.get("num_thread", 0)),
                        "repeat_penalty": ollama_cfg.get("repeat_penalty", default_ollama.get("repeat_penalty", 1.1)),
                    },
                }
            else:
                # ── OpenAI 兼容 /v1/chat/completions ─────────────────
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                        ]}
                    ],
                    "stream": False,
                    "options": {
                        "temperature": ollama_cfg.get("temperature", default_ollama.get("temperature", 0.1)),
                        "num_predict": ollama_cfg.get("num_predict", default_ollama.get("num_predict", 256)),
                        "num_ctx": ollama_cfg.get("num_ctx", default_ollama.get("num_ctx", 4096)),
                        "num_thread": ollama_cfg.get("num_thread", default_ollama.get("num_thread", 0)),
                        "repeat_penalty": ollama_cfg.get("repeat_penalty", default_ollama.get("repeat_penalty", 1.1)),
                    },
                }
            
            resp = requests.post(url, json=payload, timeout=timeout, verify=True)
            resp.raise_for_status()
            d = resp.json()
            
            if use_ollama_gen:
                raw = d.get("response", "")
            else:
                raw = d.get("message", {}).get("content", "")
            
            result = parse_json_response(raw)
            if result:
                return result
            raise ValueError(f"JSON 解析失败: {raw[:150]}")
        except Exception as e:
            if attempt < max_retries:
                log.warning(f"  重试 {attempt}/{max_retries}: {e}")
                time.sleep(retry_delay)
            else:
                raise

def parse_json_response(raw):
    """解析 VLM 返回的 JSON（兼容 markdown 代码块）"""
    text = raw.strip()
    if text.startswith("```"):
        lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None

# ─── 文件扫描 ─────────────────────────────────────────────────────
def _convert_host_path(file_path):
    """将宿主机路径转换为容器内路径（Docker 环境）"""
    import re
    m = re.match(r'^([A-Za-z]):[/\\](.+)$', file_path)
    if m:
        relative_path = m.group(2).replace('\\', '/')
        container_photo_dir = os.environ.get("PHOTO_DIR", "/photos")
        return f"{container_photo_dir}/{relative_path}"
    return file_path

def scan_photos(index_file, db: ScoreDB):
    """扫描索引文件，发现新照片"""
    if not os.path.exists(index_file):
        log.error(f"索引文件不存在: {index_file}")
        return 0
    
    extensions = set(CFG.get("photo_extensions", [".jpg", ".jpeg", ".png"]))
    new_count = 0
    skip_count = 0
    
    log.info(f"开始扫描索引: {index_file}")
    
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
            
            try:
                file_hash = db.file_hash(file_path)
                if not file_hash:
                    continue
                
                if db.exists(file_hash):
                    skip_count += 1
                    continue
                
                stat = os.stat(file_path)
                file_name = os.path.basename(file_path)
                
                # 解析日期
                year, month, day = None, None, None
                if shoot_date and len(shoot_date) >= 10:
                    try:
                        dt = datetime.strptime(shoot_date, "%Y-%m-%d")
                        year, month, day = dt.year, dt.month, dt.day
                    except ValueError:
                        pass
                
                db.insert_pending(file_hash, file_path, file_name, stat.st_size, 
                                stat.st_mtime, shoot_date, year, month, day)
                new_count += 1
                
            except (OSError, PermissionError) as e:
                log.warning(f"无法访问 {file_path}: {e}")
    
    db.commit()
    log.info(f"扫描完成: 新增 {new_count}, 跳过 {skip_count}")
    return new_count

# ─── 主处理流程 ──────────────────────────────────────────────────
_interrupted = False

def signal_handler(sig, frame):
    """优雅中断处理"""
    global _interrupted
    _interrupted = True
    log.info("收到中断信号，处理完当前照片后退出...")

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def process_pending(db: ScoreDB, limit=None):
    """处理待评分的照片"""
    global _interrupted
    
    pending = db.get_pending(limit=limit)
    total = len(pending)
    
    if total == 0:
        log.info("没有待处理的照片")
        return 0
    
    # 从 config_module 获取默认值
    default_ollama = DEFAULT_CONFIG.get("ollama", {})
    default_image_processing = DEFAULT_CONFIG.get("image_processing", {})
    
    log.info(f"开始处理 {total} 张照片")
    log.info(f"  CPU线程: {CFG.get('ollama', {}).get('num_thread', default_ollama.get('num_thread', 0))}  "
             f"图片缩放: {CFG.get('image_processing', {}).get('max_side', default_image_processing.get('max_side', 512))}px  "
             f"超时: {CFG.get('ollama', {}).get('timeout', default_ollama.get('timeout', 200))}s")
    
    success = 0
    errors = 0
    start_time = time.time()
    times = []
    
    for i, row in enumerate(pending):
        if _interrupted:
            log.info(f"中断退出，已处理 {i}/{total}")
            print(f"\n⏹️ 中断退出 已处理 {i}/{total}", flush=True)
            break
        
        path = row["file_path"]
        fhash = row["file_hash"]
        fname = row["file_name"]
        
        log.info(f"[{i+1}/{total}] {fname}")
        t0 = time.time()
        
        try:
            # 步骤1: 规则过滤（如果启用）
            if RULES_AVAILABLE and CFG.get("filter_rules", {}).get("enabled", True):
                rule_result = analyze_image_quality(path)
                db.update_rule_result(fhash, rule_result)
                
                if not rule_result.get("rule_pass", 0):
                    log.info(f"  ⏭️ 规则过滤未通过: {rule_result.get('reject_reasons', [])}")
                    # 仍然调用 VLM，但标记为规则未通过
            
            # 步骤2: VLM 评分
            vlm_result = call_vlm(path)
            elapsed = time.time() - t0
            times.append(elapsed)
            
            db.update_vlm_result(fhash, vlm_result)
            score = vlm_result.get("score", "?")
            desc = vlm_result.get("description", "")[:25]
            log.info(f"  ✅ {score}/10 | {desc} | {elapsed:.1f}s")
            success += 1
            
        except Exception as e:
            elapsed = time.time() - t0
            times.append(elapsed)
            db.mark_vlm_error(fhash, str(e))
            log.error(f"  ❌ {e} | {elapsed:.1f}s")
            errors += 1
        
        # 批量提交
        default_processing = DEFAULT_CONFIG.get("processing", {})
        if (i + 1) % CFG.get("processing", {}).get("batch_save_interval", default_processing.get("batch_save_interval", 10)) == 0:
            db.commit()
            if times and CFG.get("processing", {}).get("enable_progress_estimate", default_processing.get("enable_progress_estimate", True)):
                avg = sum(times) / len(times)
                remaining = (total - i - 1) * avg
                log.info(f"  📊 平均 {avg:.1f}s/张, 预计剩余 {remaining/60:.0f}分钟")
    
    db.commit()
    elapsed = time.time() - start_time
    avg_time = sum(times) / len(times) if times else 0
    
    log.info(f"\n{'='*50}")
    log.info(f"处理完成!")
    log.info(f"  成功: {success}  失败: {errors}")
    log.info(f"  总耗时: {elapsed/60:.1f} 分钟")
    log.info(f"  平均: {avg_time:.1f} 秒/张")
    log.info(f"{'='*50}")
    return success

# ─── 查询功能 ────────────────────────────────────────────────────
def show_stats(db: ScoreDB):
    """显示统计信息"""
    stats = db.get_stats()
    total = sum(stats.values())
    
    print(f"\n{'='*50}")
    print(f"  📊 照片评分统计")
    print(f"{'='*50}")
    print(f"  总计索引:   {total}")
    print(f"  已完成评分: {stats.get('done', 0)}")
    print(f"  待处理:     {stats.get('pending', 0)}")
    print(f"  规则通过:   {stats.get('rule_pass', 0)}")
    print(f"{'='*50}\n")

def show_top(db: ScoreDB, limit=20):
    """显示评分最高的照片"""
    rows = db.get_top_rated(limit)
    if not rows:
        print("暂无已完成的照片")
        return
    
    print(f"\n🏆 评分最高的 {len(rows)} 张:\n")
    for i, row in enumerate(rows, 1):
        tags = json.loads(row["vlm_tags"]) if row["vlm_tags"] else []
        tag_str = " ".join(f"#{t}" for t in tags[:5])
        print(f"  {i}. [{row['vlm_score']}/10] {row['file_name']}")
        print(f"     {row['vlm_desc']}")
        print(f"     {tag_str}  场景:{row['vlm_scene']}")
        print()

def search_by_scene(db: ScoreDB, scene):
    """按场景搜索"""
    rows = db.search_by_scene(scene)
    if not rows:
        print(f"没有找到「{scene}」的照片")
        return
    
    print(f"\n🔍 「{scene}」共 {len(rows)} 张:\n")
    for row in rows[:20]:
        print(f"  [{row['vlm_score']}/10] {row['file_name']} — {row['vlm_desc']}")

# ─── 导出功能（JSONL） ──────────────────────────────────────────
def export_done_photos(db: ScoreDB, output_path=None):
    """将已完成的评分结果导出为 JSONL 文件"""
    rows = db.conn.execute(
        "SELECT * FROM photo_scores WHERE vlm_done = 1 ORDER BY scored_at"
    ).fetchall()
    
    if not rows:
        log.info("没有可导出的结果")
        return 0
    
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = SCRIPT_DIR / f"export_{timestamp}.jsonl"
    
    count = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for row in rows:
            tags = json.loads(row["vlm_tags"]) if row["vlm_tags"] else []
            record = {
                "file_path": row["file_path"],
                "file_name": row["file_name"],
                "file_size": row["file_size"],
                "mtime": row["mtime"],
                "scored_at": row["scored_at"],
                "d": row["vlm_desc"],
                "t": tags,
                "s": row["vlm_scene"],
                "q": row["vlm_score"],
                "h": row["vlm_highlights"],
                "i": row["vlm_issues"],
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    
    file_size = os.path.getsize(output_path) / 1024
    log.info(f"✅ 已导出 {count} 条 → {output_path} ({file_size:.1f} KB)")
    return count


# ─── 导入功能（JSONL） ──────────────────────────────────────────
def import_results(jsonl_path, db: ScoreDB):
    """从 JSONL 文件导入评分结果到数据库"""
    if not os.path.exists(jsonl_path):
        log.error(f"文件不存在: {jsonl_path}")
        return 0
    
    imported = 0
    skipped = 0
    
    log.info(f"开始导入: {jsonl_path}")
    
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                log.warning(f"第 {line_num} 行 JSON 解析失败，跳过")
                continue
            
            file_path = record.get("file_path", "")
            file_name = record.get("file_name", "")
            file_size = record.get("file_size", 0)
            mtime = record.get("mtime", 0)
            scored_at = record.get("scored_at", datetime.now().isoformat())
            
            # 计算哈希
            fhash = hashlib.md5(f"{file_path}:{file_size}:{mtime}".encode()).hexdigest()
            
            if db.exists(fhash):
                skipped += 1
                continue
            
            # 构建 VLM 结果
            vlm_result = {
                "score": record.get("q", 0),
                "description": record.get("d", ""),
                "tags": record.get("t", []),
                "scene": record.get("s", ""),
                "highlights": record.get("h", ""),
                "issues": record.get("i", ""),
            }
            
            # 插入记录
            db.insert_pending(fhash, file_path, file_name, file_size, mtime)
            db.update_vlm_result(fhash, vlm_result)
            imported += 1
            
            if imported % 1000 == 0:
                db.commit()
                log.info(f"  已导入 {imported} 条...")
    
    db.commit()
    log.info(f"导入完成: 新增 {imported}, 跳过 {skipped}")
    return imported

# ─── CLI 入口 ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="照片 AI 评分系统 v2.0")
    sub = parser.add_subparsers(dest="command")
    
    # 扫描命令
    p_scan = sub.add_parser("scan", help="扫描索引文件，发现新照片")
    p_scan.add_argument("--index", help="索引文件路径")
    
    # 处理命令
    p_run = sub.add_parser("run", help="处理待评分照片")
    p_run.add_argument("--limit", type=int, help="限制处理数量")
    
    # 自动命令（扫描+处理）
    p_auto = sub.add_parser("auto", help="扫描+处理")
    p_auto.add_argument("--index", help="索引文件路径")
    p_auto.add_argument("--limit", type=int, help="限制处理数量")
    p_auto.add_argument("--build-index", action="store_true", help="先建立/更新索引，再评分")
    
    # 重试命令
    sub.add_parser("retry", help="重试失败的照片")
    
    # 统计命令
    sub.add_parser("stats", help="查看统计")
    
    # 高分照片命令
    p_top = sub.add_parser("top", help="查看高分照片")
    p_top.add_argument("--limit", type=int, default=20)
    
    # 搜索命令
    p_search = sub.add_parser("search", help="按场景搜索")
    p_search.add_argument("scene", help="场景关键词")
    
    # 导出命令（JSONL）
    p_export = sub.add_parser("export", help="导出已完成的结果为 JSONL")
    p_export.add_argument("-o", "--output", help="输出文件路径")
    
    # 导入命令（JSONL）
    p_import = sub.add_parser("import", help="从 JSONL 文件导入结果")
    p_import.add_argument("file", help="JSONL 文件路径")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # 初始化数据库
    db = ScoreDB()
    
    try:
        if args.command == "scan":
            index_file = args.index or CFG.get("paths", {}).get("index_file")
            scan_photos(index_file, db)
        elif args.command == "run":
            process_pending(db, limit=args.limit)
        elif args.command == "auto":
            index_file = args.index or CFG.get("paths", {}).get("index_file")
            
            # 可选：先建立/更新索引（扫描磁盘，生成索引文件）
            if args.build_index:
                from build_index import scan_photos as disk_scan
                photo_dir = CFG.get("paths", {}).get("photo_dir", "")
                log.info(f"先建立索引: {photo_dir}")
                try:
                    records = disk_scan(str(photo_dir))
                except Exception as e:
                    log.error(f"建立索引失败: {e}")
                    records = []
                if records and index_file:
                    os.makedirs(os.path.dirname(index_file), exist_ok=True)
                    with open(index_file, "w", encoding="utf-8") as f:
                        f.write("\n".join(records) + "\n")
                    log.info(f"索引完成: {len(records)} 张")
                elif not records:
                    log.warning("索引完成: 0 张 (目录中无有效图片)")
            
            scan_photos(index_file, db)
            process_pending(db, limit=args.limit)
        elif args.command == "retry":
            # 重置失败记录
            db.conn.execute("UPDATE photo_scores SET vlm_error = NULL WHERE vlm_error IS NOT NULL")
            db.commit()
            log.info("已重置失败记录")
            process_pending(db)
        elif args.command == "stats":
            show_stats(db)
        elif args.command == "top":
            show_top(db, args.limit)
        elif args.command == "search":
            search_by_scene(db, args.scene)
        elif args.command == "export":
            export_done_photos(db, args.output)
        elif args.command == "import":
            import_results(args.file, db)
    finally:
        db.close()

if __name__ == "__main__":
    main()
