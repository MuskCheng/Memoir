#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memoir Web 管理后台
Flask 应用，提供 Docker 部署的管理界面
"""

import os
import sys

# ─── Windows UTF-8 强制输出 ─────────────────────────────────────
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import json
import subprocess
import sqlite3
import threading
import re
import time
from pathlib import Path
from datetime import datetime

# 确保项目根目录在 Python 路径中
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import requests
from flask import Flask, render_template, request, jsonify

from webui.scheduler import (
    load_tasks, save_tasks, TaskScheduler,
    ACTION_MAP, ACTION_LABELS, ACTION_ICONS, compute_next_run, TASKS_FILE
)  # fmt: skip
from webui.health import run_health_check, warmup_model
from config_module import load_config as _load_config_full, SCRIPT_DIR, CONFIG_FILE, save_config as _save_config

# ─── 路径 ──────────────────────────────────────────────────────
DB_PATH = Path(os.environ.get("DATA_DIR", SCRIPT_DIR / "data")) / "zectrix_scores.sqlite"

app = Flask(__name__)

# ─── 定时任务调度器（模块级别初始化，确保 API 路由可访问） ────
scheduler = TaskScheduler()

# ─── 禁止浏览器缓存所有响应 ──────────────────────────────────
@app.after_request
def _add_no_cache(response):
    """所有响应添加 no-cache 头，确保页面和 API 都不被浏览器缓存"""
    response.headers.update({
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })
    return response

# ─── 进程管理器 ──────────────────────────────────────────────

def load_config():
    """WebUI 专用的轻量配置加载（不触发 sys.exit）"""
    try:
        return _load_config_full(auto_create=False)
    except Exception:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        # 配置文件不存在时返回默认配置
        from config_module import DEFAULT_CONFIG
        import copy
        return copy.deepcopy(DEFAULT_CONFIG)

def get_db_stats():
    if not DB_PATH.exists():
        return {"total": 0, "done": 0, "pending": 0, "error": 0}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute("SELECT vlm_done, COUNT(*) FROM photo_scores GROUP BY vlm_done")
        rows = cur.fetchall()
        conn.close()
        stats = {"total": 0, "done": 0, "pending": 0, "error": 0}
        for done, cnt in rows:
            stats["total"] += cnt
            if done == 1:
                stats["done"] = cnt
            elif done == 0:
                stats["pending"] = cnt
        # 统计错误
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM photo_scores WHERE vlm_error IS NOT NULL")
            stats["error"] = cur.fetchone()[0]
            conn.close()
        except:
            pass
        return stats
    except:
        return {"total": 0, "done": 0, "pending": 0, "error": 0}

def get_index_count():
    """读取索引文件行数（照片总数，不依赖数据库）"""
    cfg = load_config()
    idx = cfg.get("paths", {}).get("index_file", "")
    if not idx or not Path(idx).exists():
        return 0
    try:
        with open(idx, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except:
        return 0

def run_command(cmd):
    """在 Memoir 目录执行命令，返回输出"""
    try:
        result = subprocess.run(
            cmd,
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout[-5000:],  # 只保留最后5000字符
            "stderr": result.stderr[-2000:],
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": "执行超时（>600秒）", "returncode": -1}
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e), "returncode": -1}

def get_ollama_models():
    """获取 Ollama 可用模型列表"""
    cfg = load_config()
    base_url = cfg.get("ollama", {}).get("base_url", "http://localhost:11434")
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=5)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            return [m["name"] for m in models]
        return []
    except:
        return []

# ─── Ollama 启动检查 + 模型预热 ────────────────────────────────
def check_ollama():
    """检查 Ollama 是否运行，返回状态信息"""
    cfg = load_config()
    base_url = cfg.get("ollama", {}).get("base_url", "http://localhost:11434")
    model = cfg.get("ollama", {}).get("model", "qwen2.5vl:7b")
    
    status = {"running": False, "model_loaded": False, "error": None, "warmup": False}
    
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=5)
        if resp.status_code == 200:
            status["running"] = True
            models = [m["name"] for m in resp.json().get("models", [])]
            status["models"] = models
            if model in models:
                status["model_loaded"] = True
            return status
        return status
    except requests.exceptions.ConnectionError:
        status["error"] = "无法连接 Ollama"
        return status
    except Exception as e:
        status["error"] = str(e)
        return status


def warmup_ollama():
    """预热 Ollama 模型（发送轻量请求，减少首次推理延迟）"""
    cfg = load_config()
    base_url = cfg.get("ollama", {}).get("base_url", "http://localhost:11434")
    model = cfg.get("ollama", {}).get("model", "qwen2.5vl:7b")
    
    print(f"  🚀 预热模型: {model} ...", end="", flush=True)
    try:
        resp = requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": "hi", "keep_alive": "30m"},
            timeout=15,
        )
        if resp.status_code == 200:
            print(" ✅")
            return True
        else:
            print(f" ⚠️ (状态码: {resp.status_code})")
            return False
    except requests.Timeout:
        print(" ⏩ (后台继续)")
        return False
    except Exception as e:
        print(f" ⚠️ ({e})")
        return False

# ─── 后台进程管理器（实时进度） ────────────────────────────────
class ProcessManager:
    """管理长时间运行的后台进程"""
    
    def __init__(self):
        self._lock = threading.Lock()
        self.process = None
        self.running = False
        self.reset()
    
    def reset(self):
        """重置状态（如有旧进程则终止）"""
        if self.process and self.running:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.process = None
        self.running = False
        self.action = ""
        self.label = ""
        self.start_time = 0
        self.stdout_lines = []
        self.stderr_lines = []
        self.progress = {
            # 解析日志得到的进度
            "current": 0,
            "total": 0,
            "avg_time": 0,
            "elapsed_min": 0,
            "remaining_min": 0,
            "success": 0,
            "errors": 0,
            "percent": 0,
            "status_text": "空闲",
            "last_photo": "",
            "interrupted": False,
        }
    
    def _parse_progress(self, line):
        """从日志行解析进度"""
        p = self.progress.copy()
        
        # 任何非空行都表示进程在运行，更新状态文本
        line_stripped = line.strip()
        if line_stripped:
            if p["status_text"] == "启动中...":
                p["status_text"] = line_stripped[:40]
        
        # 匹配: [1/5000] IMG_0001.jpg  (score.py)
        m = re.search(r'\[(\d+)/(\d+)\]', line)
        if m:
            p["current"] = int(m.group(1))
            p["total"] = int(m.group(2))
            if p["total"] > 0:
                p["percent"] = round(p["current"] / p["total"] * 100, 1)
        
        # 匹配: ⏳ 已处理 2000 张 (build_index.py)
        m = re.search(r'⏳\s*已处理\s*(\d+)\s*张', line)
        if m:
            p["current"] = int(m.group(1))
            p["status_text"] = f"索引中 ({p['current']} 张)"
        
        # 匹配: ⏹️ 中断退出 已处理 1500/5000 (score.py 中断)
        m = re.search(r'⏹️\s*中断退出\s*已处理\s*(\d+)/(\d+)', line)
        if m:
            p["current"] = int(m.group(1))
            p["total"] = int(m.group(2))
            p["percent"] = round(p["current"] / p["total"] * 100, 1) if p["total"] > 0 else 0
            p["interrupted"] = True
            p["status_text"] = f"⏹️ 中断于 {p['current']}/{p['total']}"
        
        # 匹配: 📄 索引已写入: ...（共 N 条）  (build_index.py 完成)
        m = re.search(r'共\s*(\d+)\s*条\)', line)
        if m:
            p["total"] = int(m.group(1))
            p["current"] = p["total"]
            p["percent"] = 100
            p["status_text"] = f"✅ 索引完成 ({p['total']} 张)"
        
        # 匹配: ✅ 7.5/10 | 描述 | 55.2s  (score.py 评分)
        m = re.search(r'✅\s+[\d.]+/10\s*\|\s*(.+?)\s*\|\s*([\d.]+)s', line)
        if m:
            p["success"] += 1
            p["last_photo"] = m.group(1).strip()[:20]
            elapsed = float(m.group(2))
        
        # 匹配: ❌ 错误 | 55.2s
        if "❌" in line:
            p["errors"] += 1
        
        # 匹配: 📊 平均 55.1s/张, 预计剩余 458分钟
        m = re.search(r'平均\s+([\d.]+)s/张.*?剩余\s+([\d.]+)分钟', line)
        if m:
            p["avg_time"] = float(m.group(1))
            p["remaining_min"] = float(m.group(2))
        
        # 计算已用时间
        if self.start_time > 0:
            p["elapsed_min"] = round((time.time() - self.start_time) / 60, 1)
        
        # 状态文本
        if p["total"] > 0 and p["current"] > 0:
            p["status_text"] = f"处理中 ({p['current']}/{p['total']})"
        elif p["total"] > 0:
            p["status_text"] = f"共 {p['total']} 张"
        
        return p
    
    def start(self, action, cmd):
        """启动后台进程"""
        self.reset()
        self.action = action
        self.label = ACTION_LABELS.get(action, action)
        self.start_time = time.time()
        
        # 动态构建命令（如 build_index/export 需从配置读取路径）
        if cmd is None and action == "build_index":
            cfg = _load_config_full(strict=False)
            photo_dir = cfg.get("paths", {}).get("photo_dir", "")
            index_file = cfg.get("paths", {}).get("index_file", "")
            cmd = [sys.executable, "build_index.py", photo_dir, "-o", index_file]
        elif cmd is None and action == "export":
            export_file = str(SCRIPT_DIR / "data" / "export.jsonl")
            cmd = [sys.executable, "score.py", "export", "-o", export_file]
        
        # build_index 的输出走 stderr（进度信息），合并到 stdout 实时显示
        _merge_stderr = action == "build_index"
        
        def _run():
            try:
                self.running = True
                self.progress["status_text"] = "启动中..."
                
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(SCRIPT_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT if _merge_stderr else subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    bufsize=1,
                )
                self.process = proc
                
                # 实时读取 stdout
                for line in iter(proc.stdout.readline, ""):
                    with self._lock:
                        self.stdout_lines.append(line)
                        self.progress = self._parse_progress(line)
                    if len(self.stdout_lines) > 5000:
                        self.stdout_lines = self.stdout_lines[-1000:]
                        self.stderr_lines = self.stderr_lines[-200:]
                
                proc.wait()
                with self._lock:
                    # 读 stderr（仅当 stderr 未合并到 stdout 时）
                    if proc.stderr:
                        for line in iter(proc.stderr.readline, ""):
                            self.stderr_lines.append(line)
                    if self.progress.get("interrupted"):
                        self.progress["status_text"] = f"⏹️ 中断于 {self.progress['current']}/{self.progress['total']}"
                    elif proc.returncode == 0:
                        self.progress["status_text"] = "✅ 已完成"
                        self.progress["percent"] = 100
                    else:
                        self.progress["status_text"] = f"❌ 异常退出(code={proc.returncode})"
                    self.running = False
                    self.process = None
            except Exception as e:
                with self._lock:
                    self.progress["status_text"] = f"❌ {e}"
                    self.running = False
                    self.process = None
        
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return {"success": True, "message": f"{self.label} 已启动"}
    
    def stop(self):
        """停止进程"""
        if self.process and self.running:
            self.process.terminate()
            self.progress["status_text"] = "⏹️ 已手动停止"
            self.running = False
            return True
        return False
    
    def get_status(self):
        """获取当前进度"""
        with self._lock:
            return {
                "running": self.running,
                "action": self.action,
                "label": self.label,
                "start_time": self.start_time,
                **self.progress,
            }
    
    def get_pending_count(self):
        """查询数据库中待处理的照片数量（断点续评用）"""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM photo_scores WHERE vlm_done = 0 AND vlm_error IS NULL")
            count = cur.fetchone()[0]
            conn.close()
            return count
        except:
            return 0
    
    def get_output(self, lines=50):
        """获取最近的输出行"""
        with self._lock:
            return "".join(self.stdout_lines[-lines:])

process_mgr = ProcessManager()

# ─── 页面路由 ──────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

# ─── API 路由 ──────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    stats = get_db_stats()
    cfg = load_config()
    
    # 索引文件统计（独立于数据库，即使未评分也能显示）
    index_count = get_index_count()
    
    # 文件大小
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    
    # Ollama 状态
    o_status = check_ollama()
    
    return jsonify({
        "stats": stats,
        "db_size_kb": round(db_size / 1024, 1),
        "config": {
            "ollama_url": cfg.get("ollama", {}).get("base_url", ""),
            "ollama_model": cfg.get("ollama", {}).get("model", ""),
            "photo_dir": cfg.get("paths", {}).get("photo_dir", ""),
            "data_dir": cfg.get("paths", {}).get("project_dir", ""),
            "device_mac": cfg.get("push_settings", {}).get("device_mac", ""),
            "num_thread": cfg.get("ollama", {}).get("num_thread", 0),
            "cpu_count": os.cpu_count() or "?",
        },
        "ollama": {
            "running": o_status.get("running", False),
            "model_loaded": o_status.get("model_loaded", False),
            "has_models": len(o_status.get("models", [])) > 0,
            "model_count": len(o_status.get("models", [])),
        },
        "index_count": index_count,
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = load_config()
    return jsonify(cfg)

@app.route("/api/config", methods=["PUT"])
def api_update_config():
    try:
        new_cfg = request.get_json()
        if not new_cfg:
            return jsonify({"success": False, "error": "无效的 JSON"}), 400
        _save_config(new_cfg)
        return jsonify({"success": True, "message": "配置已保存"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/models")
def api_list_models():
    models = get_ollama_models()
    cfg = load_config()
    current = cfg.get("ollama", {}).get("model", "")
    return jsonify({"models": models, "current": current})

@app.route("/api/devices")
def api_get_devices():
    """获取可用设备列表"""
    from push import get_device_list
    devices = get_device_list()
    cfg = load_config()
    cfg_devices = cfg.get("devices", [])
    default_mac = cfg.get("push_settings", {}).get("device_mac", "")
    return jsonify({"devices": devices, "configs": cfg_devices, "default_mac": default_mac})

@app.route("/api/action/<action>", methods=["POST"])
def api_run_action(action):
    """执行操作（长时间任务后台运行，支持实时进度）"""
    from push import get_device_config as _get_dev_cfg, get_device_list as _get_dev_list
    device_name = (request.get_json(silent=True) or {}).get("device", "")
    
    allowed_async = {
        "score_full": [sys.executable, "score.py", "auto", "--build-index"],
        "score_auto": [sys.executable, "score.py", "auto"],
        "score_run": [sys.executable, "score.py", "run"],
        "score_stats": [sys.executable, "score.py", "stats"],
        "score_top": [sys.executable, "score.py", "top", "--limit", "10"],
        "push_select": [sys.executable, "push.py", "select"],
        "push_push": [sys.executable, "push.py", "push"],
        "retry": [sys.executable, "score.py", "retry"],
        "build_index": None,  # 在 start() 中动态构建命令
    }
    
    # push 命令追加 --device（如果有）
    if action in ("push_push",) and device_name:
        allowed_async[action] = [sys.executable, "push.py", "push", "--device", device_name]
    
    if action not in allowed_async:
        return jsonify({"success": False, "error": f"未知操作: {action}"}), 400
    
    # 长时间操作（评分/重试）→ 后台异步执行
    if action in ("score_full", "score_auto", "score_run", "retry", "build_index"):
        if process_mgr.running:
            return jsonify({"success": False, "error": "已有任务正在运行，请等待或先停止"}), 409
        return jsonify(process_mgr.start(action, allowed_async[action]))
    
    # 短操作 → 同步执行
    result = run_command(allowed_async[action])
    return jsonify(result)

@app.route("/api/action/stop", methods=["POST"])
def api_stop_action():
    """停止正在运行的任务"""
    ok = process_mgr.stop()
    return jsonify({"success": ok, "message": "已停止" if ok else "没有运行中的任务"})

@app.route("/api/progress")
def api_get_progress():
    """获取实时进度"""
    return jsonify(process_mgr.get_status())

@app.route("/api/pending-count")
def api_get_pending_count():
    """获取待处理照片数量（断点续评用）"""
    return jsonify({"pending": process_mgr.get_pending_count()})

@app.route("/api/progress/output")
def api_get_progress_output():
    """获取实时输出"""
    lines = request.args.get("lines", 50, type=int)
    output = process_mgr.get_output(lines)
    return jsonify({"output": output, "running": process_mgr.running})

@app.route("/api/logs")
def api_get_logs():
    log_file = SCRIPT_DIR / "photopush.log"
    lines = []
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            lines = all_lines[-200:]  # 取最后200行
            lines.reverse()           # 倒序：最新在前
    return jsonify({"logs": "".join(lines), "total_lines": len(lines)})

# ─── 定时任务 API ──────────────────────────────────────────────

@app.route("/api/tasks", methods=["GET"])
def api_list_tasks():
    """获取所有定时任务"""
    tasks = load_tasks()
    # 为前端增加 next_run 等额外字段
    for task in tasks:
        task["next_run"] = compute_next_run(task)
        task["action_label"] = ACTION_LABELS.get(task.get("action", ""), "未知操作")
        task["action_icon"] = ACTION_ICONS.get(task.get("action", ""), "🔧")
    return jsonify({"tasks": tasks, "available_actions": [
        {"id": k, "label": ACTION_LABELS.get(k, k), "icon": ACTION_ICONS.get(k, "🔧")}
        for k in ACTION_MAP
    ]})

@app.route("/api/tasks", methods=["POST"])
def api_create_task():
    """创建定时任务"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "无效的请求数据"}), 400
        
        tasks = load_tasks()
        
        # 生成 ID
        import uuid
        task_id = data.get("id") or f"task_{uuid.uuid4().hex[:8]}"
        
        # 检查 ID 唯一性
        if any(t["id"] == task_id for t in tasks):
            return jsonify({"success": False, "error": f"任务ID已存在: {task_id}"}), 400
        
        task = {
            "id": task_id,
            "name": data.get("name", "未命名任务"),
            "description": data.get("description", ""),
            "action": data.get("action", "score_stats"),
            "schedule_type": data.get("schedule_type", "cron"),
            "cron": data.get("cron", "0 6 * * *"),
            "interval_seconds": int(data.get("interval_seconds", 86400)),
            "run_at": data.get("run_at", ""),
            "enabled": data.get("enabled", True),
            "created_at": datetime.now().isoformat(),
            "last_run": None,
        }
        
        tasks.append(task)
        save_tasks(tasks)
        
        # 重新加载调度器
        if task["enabled"]:
            scheduler.add_task(task)
        
        return jsonify({"success": True, "message": "任务已创建", "task": task})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/tasks/<task_id>", methods=["PUT"])
def api_update_task(task_id):
    """更新定时任务"""
    try:
        data = request.get_json()
        tasks = load_tasks()
        
        found = False
        for i, t in enumerate(tasks):
            if t["id"] == task_id:
                # 更新字段
                for key in ["name", "description", "action", "schedule_type",
                           "cron", "interval_seconds", "run_at", "enabled"]:
                    if key in data:
                        t[key] = data[key]
                found = True
                break
        
        if not found:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        
        save_tasks(tasks)
        
        # 重新加载调度器
        scheduler.reload_all()
        
        return jsonify({"success": True, "message": "任务已更新"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/tasks/<task_id>", methods=["DELETE"])
def api_delete_task(task_id):
    """删除定时任务"""
    tasks = load_tasks()
    before = len(tasks)
    tasks = [t for t in tasks if t["id"] != task_id]
    
    if len(tasks) == before:
        return jsonify({"success": False, "error": "任务不存在"}), 404
    
    save_tasks(tasks)
    scheduler.reload_all()
    
    return jsonify({"success": True, "message": "任务已删除"})

@app.route("/api/tasks/<task_id>/toggle", methods=["POST"])
def api_toggle_task(task_id):
    """启用/禁用定时任务"""
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["enabled"] = not t.get("enabled", True)
            save_tasks(tasks)
            scheduler.reload_all()
            return jsonify({
                "success": True,
                "enabled": t["enabled"],
                "message": "已启用" if t["enabled"] else "已禁用"
            })
    
    return jsonify({"success": False, "error": "任务不存在"}), 404

# ─── 已评分照片 API ────────────────────────────────────────────

@app.route("/api/scored-photos")
def api_scored_photos():
    """已评分照片列表，支持排序和筛选"""
    from config_module import get_db_schema_ddl
    cfg = load_config()
    db_path = Path(cfg.get("paths", {}).get("score_db", ""))
    
    if not db_path.exists():
        return jsonify({"photos": [], "total": 0, "scenes": []})
    
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # ── 参数 ────────────────────────────────────────────
        sort = request.args.get("sort", "scored_at_desc")
        min_score = request.args.get("min_score", type=float)
        max_score = request.args.get("max_score", type=float)
        scene = request.args.get("scene", "")
        limit = min(request.args.get("limit", 50, type=int), 200)
        offset = request.args.get("offset", 0, type=int)
        
        # ── WHERE 条件 ─────────────────────────────────────
        where = ["vlm_done = 1"]
        params = []
        if min_score is not None:
            where.append("vlm_score >= ?")
            params.append(min_score)
        if max_score is not None:
            where.append("vlm_score <= ?")
            params.append(max_score)
        if scene:
            where.append("vlm_scene = ?")
            params.append(scene)
        
        where_sql = " AND ".join(where)
        
        # ── ORDER BY ────────────────────────────────────────
        sort_map = {
            "score_desc": "vlm_score DESC",
            "score_asc": "vlm_score ASC",
            "date_desc": "shoot_date DESC NULLS LAST",
            "date_asc": "shoot_date ASC NULLS LAST",
            "scored_at_desc": "scored_at DESC NULLS LAST",
            "scored_at_asc": "scored_at ASC NULLS LAST",
        }
        order_by = sort_map.get(sort, "scored_at DESC")
        
        # ── 查询总数 ────────────────────────────────────────
        total = conn.execute(
            f"SELECT COUNT(*) FROM photo_scores WHERE {where_sql}", params
        ).fetchone()[0]
        
        # ── 查询数据 ────────────────────────────────────────
        rows = conn.execute(
            f"SELECT * FROM photo_scores WHERE {where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()
        
        # ── 可用场景列表（供前端下拉使用） ───────────────────
        scenes_raw = conn.execute(
            "SELECT DISTINCT vlm_scene FROM photo_scores WHERE vlm_done = 1 AND vlm_scene IS NOT NULL AND vlm_scene != '' ORDER BY vlm_scene"
        ).fetchall()
        scenes = [r[0] for r in scenes_raw]
        
        conn.close()
        
        photos = []
        for r in rows:
            photos.append({
                "file_name": r["file_name"],
                "file_path": r["file_path"],
                "score": r["vlm_score"],
                "scene": r["vlm_scene"] or "",
                "description": r["vlm_desc"] or "",
                "tags": r["vlm_tags"] or "",
                "highlights": r["vlm_highlights"] or "",
                "shoot_date": r["shoot_date"] or "",
                "scored_at": r["scored_at"] or "",
            })
        
        return jsonify({"photos": photos, "total": total, "scenes": scenes})
    except Exception as e:
        return jsonify({"photos": [], "total": 0, "scenes": [], "error": str(e)}), 500

# ─── 健康检查 API ──────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    """完整健康检查 + 环境信息"""
    results, cfg = run_health_check(auto_fix=False)
    return jsonify({"success": True, "data": results})

@app.route("/api/health/fix", methods=["POST"])
def api_health_fix():
    """运行自动修复（安装依赖、启动 Ollama、拉模型、创建目录）"""
    results, cfg = run_health_check(auto_fix=True)
    return jsonify({"success": True, "data": results})

@app.route("/api/health/pull-model", methods=["POST"])
def api_pull_model():
    """拉取 Ollama 模型"""
    from webui.health import try_pull_model
    cfg = load_config()
    success = try_pull_model(cfg)
    return jsonify({"success": success})

@app.route("/api/health/restart-ollama", methods=["POST"])
def api_restart_ollama():
    """尝试重启 Ollama"""
    from webui.health import try_start_ollama, check_ollama
    cfg = load_config()
    try_start_ollama(cfg)
    ostatus = check_ollama(cfg)
    return jsonify({
        "success": ostatus["running"],
        "running": ostatus["running"],
        "error": ostatus.get("error", ""),
    })

# ─── 初始化引导 API ──────────────────────────────────────────

def _is_docker_default_path(path):
    """判断路径是否仍为 Docker 默认值（未初始化）"""
    if not path:
        return True
    docker_patterns = ["/data", "/app/", "/photos", "/mnt/"]
    return any(path.startswith(p) for p in docker_patterns)

@app.route("/api/setup/status")
def api_setup_status():
    """检查系统是否需要初始化引导"""
    cfg = load_config()
    paths = cfg.get("paths", {})
    photo_dir = paths.get("photo_dir", "")
    index_file = paths.get("index_file", "")
    
    need_setup = False
    reasons = []
    
    # 1. 检查 photo_dir 是否还是 Docker 默认路径
    if _is_docker_default_path(photo_dir):
        need_setup = True
        reasons.append("照片目录未配置")
    
    # 2. 检查索引文件是否存在且有内容
    if index_file and Path(index_file).exists() and Path(index_file).stat().st_size > 0:
        index_ok = True
    else:
        need_setup = True
        reasons.append("照片索引未建立")
    
    # 3. 检查是否有定时任务
    from webui.scheduler import load_tasks
    tasks = load_tasks()
    if not tasks:
        need_setup = True
        reasons.append("定时任务未配置")
    
    return jsonify({
        "need_setup": need_setup,
        "reasons": reasons,
        "photo_dir": photo_dir,
        "index_ok": index_file and Path(index_file).exists() and Path(index_file).stat().st_size > 0,
        "tasks_count": len(tasks),
    })

@app.route("/api/setup/apply", methods=["POST"])
def api_setup_apply():
    """执行初始化：自动检测目录、建立索引、创建默认定时任务"""
    results = {"photo_dir": "", "index": False, "tasks": [], "errors": []}
    
    try:
        # 1. 自动检测照片目录
        from webui.health import check_photo_dir
        data = request.get_json() or {}
        photo_dir = data.get("photo_dir", "")
        
        if not photo_dir:
            # 自动检测
            pd_result = check_photo_dir(load_config())
            if pd_result["exists"]:
                photo_dir = pd_result["path"]
            else:
                photo_dir = os.environ.get("USERPROFILE", "") + "\\Pictures"
        
        # 写入配置
        cfg = load_config()
        cfg.setdefault("paths", {})["photo_dir"] = photo_dir
        cfg["paths"]["project_dir"] = str(SCRIPT_DIR / "data")
        cfg["paths"]["index_file"] = str(SCRIPT_DIR / "data" / "zectrix_photo_index.txt")
        cfg["paths"]["score_db"] = str(SCRIPT_DIR / "data" / "zectrix_scores.sqlite")
        _save_config(cfg)
        results["photo_dir"] = photo_dir
        
        # 2. 建立索引（如果勾选）
        if data.get("build_index", True):
            from webui.health import ensure_index
            index_ok = ensure_index(cfg)
            results["index"] = index_ok
        
        # 3. 创建默认定时任务
        if data.get("create_tasks", True):
            import uuid
            from webui.scheduler import load_tasks, save_tasks
            tasks = load_tasks()
            default_tasks = [
                {
                    "id": f"task_{uuid.uuid4().hex[:8]}",
                    "name": "每日增量评分",
                    "action": "score_full",
                    "schedule_type": "cron",
                    "cron": "0 3 * * *",
                    "enabled": True,
                    "description": "每天凌晨3点：增量建索引 + AI评分",
                },
                {
                    "id": f"task_{uuid.uuid4().hex[:8]}",
                    "name": "每日推送",
                    "action": "push_push",
                    "schedule_type": "cron",
                    "cron": "0 8 * * *",
                    "enabled": True,
                    "description": "每天早上8点：选片推送至 Note4",
                },
            ]
            tasks.extend(default_tasks)
            save_tasks(tasks)
            if scheduler.running:
                scheduler.reload_all()
            results["tasks"] = [t["name"] for t in default_tasks]
        
        return jsonify({"success": True, "results": results})
    
    except Exception as e:
        print(f"❌ 初始化失败: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ─── 启动 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    # 启动调度器（BackgroundScheduler 需在 __main__ 中启动）
    scheduler.start()
    
    port = int(os.environ.get("WEBUI_PORT", 5000))
    host = os.environ.get("WEBUI_HOST", "0.0.0.0")
    print(f"🌐 Memoir Web 管理后台启动: http://0.0.0.0:{port}")
    print(f"   配置文件: {CONFIG_FILE}")
    print(f"   数据库: {DB_PATH}")
    print(f"   定时任务: {TASKS_FILE}")
    
    # 检查 Ollama 连接
    print("  🔍 检查 Ollama ...", end=" ", flush=True)
    ostatus = check_ollama()
    if ostatus["running"]:
        print("✅")
        if ostatus.get("model_loaded"):
            print(f"  📦 模型 {ostatus['models'][0] if ostatus.get('models') else '-'} 已就绪")
        else:
            print(f"  📦 模型未拉取，请执行: ollama pull qwen2.5vl:7b")
        # 预热模型
        warmup_ollama()
    else:
        print(f"❌ ({ostatus.get('error', '未知错误')})")
        print("  ⚠️ 请确保 Ollama 已启动: ollama serve")
    
    app.run(host=host, port=port, debug=False)
