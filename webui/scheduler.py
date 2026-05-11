#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memoir 定时任务调度引擎
基于 APScheduler，支持 cron、间隔、一次性任务
任务定义持久化到 JSON 文件
"""

import os
import sys
import json
import subprocess
import logging
import time
from pathlib import Path
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger

# ─── 路径 ──────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent.parent
TASKS_FILE = Path(os.environ.get("DATA_DIR", SCRIPT_DIR / "data")) / "scheduled_tasks.json"
LOG_FILE = Path(os.environ.get("DATA_DIR", SCRIPT_DIR / "data")) / "scheduler.log"

log = logging.getLogger("scheduler")

PYTHON_EXE = sys.executable

# ─── 任务定义 ──────────────────────────────────────────────────
ACTION_MAP = {
    "score_trial": [PYTHON_EXE, "score.py", "auto", "--limit", "10"],
    "score_auto": [PYTHON_EXE, "score.py", "auto"],
    "score_full": [PYTHON_EXE, "score.py", "auto", "--build-index", "--push"],
    "score_run": [PYTHON_EXE, "score.py", "run"],
    "score_stats": [PYTHON_EXE, "score.py", "stats"],
    "push_select": [PYTHON_EXE, "push.py", "select"],
    "push_push": [PYTHON_EXE, "push.py", "push"],
    "retry": [PYTHON_EXE, "score.py", "retry"],
    "export": None,  # 动态构建路径，见 app.py ProcessManager.start()
    "build_index": None,  # 动态构建，见 app.py ProcessManager.start()
}

ACTION_LABELS = {
    "score_trial": "AI评分（试跑10张）",
    "score_auto": "AI评分（全量）",
    "score_full": "建索引+AI评分（一键全流程）",
    "score_run": "增量评分（处理新增照片）",
    "score_stats": "查看统计",
    "push_select": "选片预览",
    "push_push": "选片并推送",
    "retry": "重试失败照片",
    "export": "导出评分结果",
    "build_index": "建立索引",
}

ACTION_ICONS = {
    "score_auto": "🤖",
    "score_trial": "🧪",
    "score_full": "⚡",
    "score_run": "⏳",
    "score_stats": "📊",
    "push_select": "🎯",
    "push_push": "📡",
    "retry": "🔄",
    "export": "📤",
    "build_index": "📑",
}

# ─── 任务存储 ──────────────────────────────────────────────────

def load_tasks():
    """从 JSON 文件加载任务定义"""
    if not TASKS_FILE.exists():
        return []
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"加载任务失败: {e}")
        return []

def save_tasks(tasks):
    """保存任务定义到 JSON 文件"""
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)

# ─── 任务执行 ──────────────────────────────────────────────────

def run_action(action_id):
    """执行指定操作，写入日志"""
    # run_action 被定时器直接调用时不传 device
    _exec_action(action_id)

def _exec_action(action_id, device=""):
    """执行操作，支持设备参数"""
    cmd = ACTION_MAP.get(action_id)
    if not cmd:
        log.error(f"未知操作: {action_id}")
        return
    
    # push 任务追加设备参数（如果有）
    if action_id == "push_push" and device:
        cmd = list(cmd) + ["--device", device]
    
    label = ACTION_LABELS.get(action_id, action_id)
    log.info(f"🔄 [定时任务] 执行: {label}" + (f" -> {device}" if device else ""))
    
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
        if result.returncode == 0:
            log.info(f"✅ [定时任务] {label} 成功")
            if result.stdout:
                for line in result.stdout.strip().split("\n")[-3:]:
                    log.info(f"  {line.strip()}")
        else:
            log.error(f"❌ [定时任务] {label} 失败 (code={result.returncode})")
            if result.stderr:
                log.error(f"  {result.stderr.strip()[-200:]}")
    except subprocess.TimeoutExpired:
        log.error(f"⏰ [定时任务] {label} 超时")
    except Exception as e:
        log.error(f"💥 [定时任务] {label} 异常: {e}")


# ─── 调度器管理 ────────────────────────────────────────────────

class TaskScheduler:
    """定时任务调度器"""
    
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self.running = False
    
    def _make_job_id(self, task):
        """生成 APScheduler job ID"""
        return f"task_{task['id']}"
    
    def _get_trigger(self, task):
        """根据任务类型创建 trigger"""
        ttype = task.get("schedule_type", "cron")
        
        if ttype == "cron":
            expr = task.get("cron", "0 6 * * *")
            try:
                parts = expr.strip().split()
                if len(parts) != 5:
                    raise ValueError(f"cron 表达式需要有5段, 实际: {len(parts)}")
                return CronTrigger(
                    minute=parts[0], hour=parts[1],
                    day=parts[2], month=parts[3], day_of_week=parts[4]
                )
            except Exception as e:
                log.error(f"cron 表达式错误 '{expr}': {e}")
                return IntervalTrigger(hours=24)
        
        elif ttype == "interval":
            seconds = task.get("interval_seconds", 86400)
            return IntervalTrigger(seconds=seconds)
        
        elif ttype == "once":
            run_at = task.get("run_at")
            if run_at:
                try:
                    dt = datetime.fromisoformat(run_at)
                    return DateTrigger(run_date=dt)
                except:
                    pass
            return IntervalTrigger(hours=24)
        
        return IntervalTrigger(hours=24)
    
    def add_task(self, task):
        """注册单个任务到调度器"""
        if not task.get("enabled", False):
            return
        
        job_id = self._make_job_id(task)
        action = task.get("action", "score_stats")
        name = task.get("name", "未命名任务")
        
        try:
            trigger = self._get_trigger(task)
            device = task.get("device", "")
            self.scheduler.add_job(
                run_action,
                trigger=trigger,
                args=[action],
                id=job_id,
                name=name,
                replace_existing=True,
                misfire_grace_time=300,
            )
            log.info(f"  📋 已注册: {name} ({action})" + (f" -> {device}" if device else ""))
            log.info(f"  ✅ 已注册任务: {name} (ID: {task['id']})")
        except Exception as e:
            log.error(f"  ❌ 注册任务失败 {name}: {e}")
    
    def remove_task(self, task_id):
        """从调度器移除任务"""
        job_id = f"task_{task_id}"
        try:
            self.scheduler.remove_job(job_id)
            log.info(f"  已移除任务: {job_id}")
        except:
            pass
    
    def reload_all(self):
        """重新加载所有任务"""
        tasks = load_tasks()
        
        # 移除所有旧任务
        self.scheduler.remove_all_jobs()
        
        # 注册所有启用的任务
        count = 0
        for task in tasks:
            if task.get("enabled", False):
                self.add_task(task)
                count += 1
        
        log.info(f"定时任务已加载: {count} 个活动任务 / {len(tasks)} 个总任务")
        return len(tasks), count
    
    def start(self):
        """启动调度器"""
        if self.running:
            return
        self.scheduler.start()
        self.running = True
        count = self.reload_all()
        log.info("⏰ 定时任务调度器已启动")
    
    def shutdown(self):
        """关闭调度器"""
        if self.running:
            self.scheduler.shutdown(wait=False)
            self.running = False
            log.info("⏰ 定时任务调度器已关闭")


# ─── 辅助函数 ──────────────────────────────────────────────────

def compute_next_run(task):
    """计算任务的下一运行时间（不依赖 APScheduler，用于 UI 显示）"""
    if not task.get("enabled", False):
        return None

    from datetime import datetime, timedelta

    now = datetime.now()

    ttype = task.get("schedule_type", "cron")

    if ttype == "cron":
        expr = task.get("cron", "0 6 * * *")
        try:
            from croniter import croniter
            cron = croniter(expr, now)
            return cron.get_next(datetime).isoformat()
        except ImportError:
            # croniter 不可用时回退到简易估算
            return (now + timedelta(hours=1)).isoformat()
        except Exception:
            return (now + timedelta(hours=1)).isoformat()
    
    elif ttype == "interval":
        seconds = task.get("interval_seconds", 86400)
        last_run = task.get("last_run")
        if last_run:
            try:
                last = datetime.fromisoformat(last_run)
                next_time = last + timedelta(seconds=seconds)
                if next_time < now:
                    return now.isoformat()
                return next_time.isoformat()
            except:
                pass
        return now.isoformat()
    
    elif ttype == "once":
        return task.get("run_at")
    
    return None
