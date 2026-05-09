#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统健康检查与自动修复模块
零配置设计：启动时自动检测环境、修复问题、预热模型
"""

import os
import sys
import json
import subprocess
import shutil
import time
import sqlite3
import logging
from pathlib import Path
from datetime import datetime

# 确保项目根目录在 Python 路径中
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config_module import (
    load_config, SCRIPT_DIR, CONFIG_FILE,
    DEFAULT_CONFIG, get_db_schema_ddl, save_config,
)

log = logging.getLogger("health")

def ensure_config():
    """确保配置文件存在，不存在则基于环境检测生成智能配置"""
    if not CONFIG_FILE.exists():
        log.info("📝 首次运行，基于环境检测生成配置")
        try:
            from config_module import generate_smart_config
            cfg = generate_smart_config()
        except Exception:
            import copy
            cfg = copy.deepcopy(DEFAULT_CONFIG)
        save_config(cfg)
        return cfg
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def smart_paths(cfg):
    """智能检测路径：自动填充空路径"""
    paths = cfg.get("paths", {})
    changed = False

    # 如果 project_dir 为空，用 ./data
    if not paths.get("project_dir"):
        paths["project_dir"] = str(SCRIPT_DIR / "data")
        changed = True

    pd = Path(paths.get("project_dir", SCRIPT_DIR / "data"))
    pd.mkdir(parents=True, exist_ok=True)

    # 自动填充子路径
    defaults = {
        "index_file": "zectrix_photo_index.txt",
        "score_db": "zectrix_scores.sqlite",
        "shown_file": "zectrix_shown.txt",
        "output_file": "zectrix_today.jpg",
    }
    for key, default in defaults.items():
        if not paths.get(key):
            paths[key] = str(pd / default)
            changed = True

    # 字体文件自动查找
    if not paths.get("font_file"):
        font_candidates = [
            SCRIPT_DIR / "ark-pixel-12px-monospaced-zh_cn.ttf",
            pd / "ark-pixel-12px-monospaced-zh_cn.ttf",
        ]
        for fc in font_candidates:
            if fc.exists():
                paths["font_file"] = str(fc)
                changed = True
                break

    if changed:
        cfg["paths"] = paths
    return changed


def check_python_deps():
    """检查 Python 依赖（模块名 ≠ pip 包名，需区分处理）"""
    # (模块名, pip包名) —— 两者相同时可简写为字符串
    packages = [
        ("PIL", "Pillow"),     # pip install Pillow → import PIL
        "requests",
        "flask",
        "apscheduler",
        "numpy",
        "croniter",
    ]
    missing = []   # 存 pip 包名（用于安装）
    for entry in packages:
        if isinstance(entry, tuple):
            mod_name, pip_name = entry
        else:
            mod_name = pip_name = entry
        try:
            __import__(mod_name)
        except ImportError:
            missing.append(pip_name)
    return missing


def install_deps(deps):
    """自动安装缺失依赖"""
    if not deps:
        return True
    log.info(f"📦 自动安装依赖: {', '.join(deps)}")
    try:
        # --user: 避免权限问题（Windows 系统 Python 可能需要）
        # 不使用 --quiet：保留错误输出便于诊断
        cmd = [sys.executable, "-m", "pip", "install", "--user"] + deps
        log.info(f"  🏃 {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        stderr_tail = result.stderr[-500:].strip()
        stdout_tail = result.stdout[-500:].strip()
        if result.returncode == 0:
            if stderr_tail:
                log.warning(f"  ⚠️ pip 安装有警告: {stderr_tail}")
            log.info(f"  ✅ 依赖安装成功")
            return True
        else:
            log.error(f"  ❌ pip 安装失败 (code={result.returncode})")
            if stderr_tail:
                log.error(f"  {stderr_tail}")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"  ❌ 安装超时(>120s)，请手动执行: pip install {' '.join(deps)}")
        return False
    except Exception as e:
        log.error(f"  ❌ 安装异常: {e}")
        return False


def check_ollama(cfg):
    """检查 Ollama 连通性和模型状态"""
    base_url = cfg.get("ollama", {}).get("base_url", "http://localhost:11434")
    model = cfg.get("ollama", {}).get("model", "qwen2.5vl:7b")

    status = {
        "running": False,
        "model_loaded": False,
        "models": [],
        "error": None,
    }

    try:
        import requests
        resp = requests.get(f"{base_url}/api/tags", timeout=5)
        if resp.status_code == 200:
            status["running"] = True
            status["models"] = [m["name"] for m in resp.json().get("models", [])]
            status["model_loaded"] = any(model == m or model.split(":")[0] == m.split(":")[0] for m in status["models"])
    except requests.exceptions.ConnectionError:
        status["error"] = "无法连接 Ollama（未启动或端口不对）"
    except Exception as e:
        status["error"] = str(e)

    return status


def try_start_ollama(cfg):
    """尝试启动 Ollama"""
    ollama_path = shutil.which("ollama")
    if not ollama_path:
        log.warning("  ⚠️ 未找到 ollama 命令，请手动启动: ollama serve")
        return False

    log.info("  🚀 尝试启动 Ollama ...")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        # 等待启动
        for i in range(10):
            time.sleep(1)
            status = check_ollama(cfg)
            if status["running"]:
                log.info("  ✅ Ollama 启动成功")
                return True
        log.warning("  ⚠️ Ollama 启动超时")
        return False
    except Exception as e:
        log.error(f"  ❌ Ollama 启动失败: {e}")
        return False


def try_pull_model(cfg):
    """尝试拉取模型"""
    ollama_path = shutil.which("ollama")
    if not ollama_path:
        log.warning("  ⚠️ 未找到 ollama 命令，无法拉取模型")
        return False

    model = cfg.get("ollama", {}).get("model", "qwen2.5vl:7b")
    log.info(f"  📦 拉取模型: {model} ...")
    try:
        result = subprocess.run(
            ["ollama", "pull", model],
            capture_output=True, text=True, encoding="utf-8", timeout=600,
        )
        if result.returncode == 0:
            log.info("  ✅ 模型拉取成功")
            return True
        else:
            log.error(f"  ❌ 模型拉取失败: {result.stderr[-300:]}")
            return False
    except Exception as e:
        log.error(f"  ❌ 模型拉取异常: {e}")
        return False


def warmup_model(cfg):
    """预热模型"""
    import requests
    base_url = cfg.get("ollama", {}).get("base_url", "http://localhost:11434")
    model = cfg.get("ollama", {}).get("model", "qwen2.5vl:7b")
    log.info(f"  🔥 预热模型: {model} ...")
    try:
        resp = requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": "hi", "keep_alive": "30m"},
            timeout=120,
        )
        if resp.status_code == 200:
            log.info("  ✅ 模型就绪")
            return True
    except Exception as e:
        log.warning(f"  ⚠️ 预热超时或失败: {e}")
    return False


def check_photo_dir(cfg):
    """检查照片目录，自动发现常见路径"""
    photo_dir = cfg.get("paths", {}).get("photo_dir", "")
    
    # 已配置且存在
    if photo_dir and Path(photo_dir).exists():
        return {"exists": True, "path": photo_dir, "auto": False}
    
    # 自动发现常见照片目录
    candidates = []
    if sys.platform == "win32":
        # Windows 常见路径
        user_home = Path(os.environ.get("USERPROFILE", ""))
        candidates = [
            user_home / "Pictures",
            user_home / "Photos",
            user_home / "照片",
            Path("C:/Users") / os.environ.get("USERNAME", "") / "Pictures",
            Path("D:/Photos"),
        ]
    else:
        # Linux/Mac
        user_home = Path(os.environ.get("HOME", ""))
        candidates = [
            user_home / "Pictures",
            user_home / "Photos",
            user_home / "照片",
        ]
    
    # 排除无效路径
    for c in candidates:
        if c.exists() and any(c.iterdir()):
            return {"exists": True, "path": str(c), "auto": True}
    
    return {"exists": False, "path": "", "auto": False}


def ensure_index(cfg):
    """确保索引文件存在"""
    index_file = Path(cfg.get("paths", {}).get("index_file", ""))
    if index_file.exists() and index_file.stat().st_size > 0:
        return True
    
    photo_dir = cfg.get("paths", {}).get("photo_dir", "")
    if not photo_dir or not Path(photo_dir).exists():
        return False
    
    log.info(f"  📝 自动建立索引: {photo_dir}")
    try:
        from build_index import scan_photos
        # scan_photos 只收录含 EXIF 相机信息的照片（真实相机拍摄）
        records = scan_photos(str(photo_dir))
        if records:
            index_file.parent.mkdir(parents=True, exist_ok=True)
            with open(index_file, "w", encoding="utf-8") as f:
                f.write("\n".join(records) + "\n")
            log.info(f"  ✅ 索引完成: {len(records)} 张")
            return True
        else:
            log.warning(f"  ⚠️ 未找到有效照片（需含 EXIF 相机信息）")
            log.warning(f"     建议手动建立: python build_index.py \"{photo_dir}\" -o \"{index_file}\"")
            return False
    except Exception as e:
        log.error(f"  ❌ 索引失败: {e}")
        return False


def ensure_db(cfg):
    """确保数据库存在（schema 与 score.py ScoreDB 一致）"""
    db_path = Path(cfg.get("paths", {}).get("score_db", ""))
    if db_path.exists():
        return True
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.executescript(get_db_schema_ddl())
        conn.commit()
        conn.close()
        log.info(f"  ✅ 数据库已创建: {db_path}")
        return True
    except Exception as e:
        log.error(f"  ❌ 数据库创建失败: {e}")
        return False


def run_health_check(cfg=None, auto_fix=True):
    """运行完整健康检查，返回检查结果"""
    if cfg is None:
        cfg = ensure_config()

    # 环境检测
    try:
        from env_detect import detect_environment, detect_ollama, detect_paths
        env_info = detect_environment()
        paths_info = detect_paths(env_info)
    except Exception:
        env_info = {}
        paths_info = {}

    results = {
        "config": {"ok": True, "msg": ""},
        "python_deps": {"ok": True, "missing": [], "msg": ""},
        "ollama": {"ok": False, "msg": "", "running": False, "model_loaded": False, "models": []},
        "model": {"ok": False, "msg": "", "pulled": False},
        "photo_dir": {"ok": False, "msg": "", "path": "", "auto": False},
        "index": {"ok": False, "msg": ""},
        "database": {"ok": False, "msg": ""},
        "environment": env_info,
        "all_ok": False,
    }

    # 1. 配置
    smart_paths(cfg)
    results["config"]["ok"] = True
    results["config"]["msg"] = "配置就绪"

    # 2. Python 依赖
    missing = check_python_deps()
    if missing:
        results["python_deps"]["ok"] = False
        results["python_deps"]["missing"] = missing
        results["python_deps"]["msg"] = f"缺少: {', '.join(missing)}"
        if auto_fix:
            install_deps(missing)
            missing2 = check_python_deps()
            if not missing2:
                results["python_deps"]["ok"] = True
                results["python_deps"]["msg"] = "已自动安装"
                # 安装后需要重新导入
    else:
        results["python_deps"]["ok"] = True
        results["python_deps"]["msg"] = "全部就绪"

    # 3. Ollama
    ostatus = check_ollama(cfg)
    results["ollama"]["running"] = ostatus["running"]
    results["ollama"]["model_loaded"] = ostatus["model_loaded"]
    results["ollama"]["models"] = ostatus.get("models", [])
    if ostatus["running"]:
        results["ollama"]["ok"] = True
        results["ollama"]["msg"] = "运行中"
    else:
        results["ollama"]["msg"] = ostatus.get("error", "未运行")
        if auto_fix:
            try_start_ollama(cfg)
            ostatus2 = check_ollama(cfg)
            if ostatus2["running"]:
                results["ollama"]["ok"] = True
                results["ollama"]["msg"] = "已自动启动"
                results["ollama"]["running"] = True

    # 4. 模型
    if results["ollama"]["running"]:
        if ostatus.get("model_loaded"):
            results["model"]["ok"] = True
            results["model"]["msg"] = "已就绪"
            results["model"]["pulled"] = True
        else:
            results["model"]["msg"] = "未拉取"
            if auto_fix:
                if try_pull_model(cfg):
                    results["model"]["ok"] = True
                    results["model"]["msg"] = "已自动拉取"
                    results["model"]["pulled"] = True
                    warmup_model(cfg)

    # 5. 照片目录
    pd = check_photo_dir(cfg)
    results["photo_dir"]["path"] = pd["path"]
    results["photo_dir"]["auto"] = pd["auto"]
    if pd["exists"]:
        results["photo_dir"]["ok"] = True
        results["photo_dir"]["msg"] = pd["path"]
        if pd["auto"]:
            cfg["paths"]["photo_dir"] = pd["path"]
    else:
        results["photo_dir"]["msg"] = "未找到照片目录（需手动配置）"

    # 6. 数据库
    results["database"]["ok"] = ensure_db(cfg)

    # 7. 索引（需要照片目录）
    if results["photo_dir"]["ok"]:
        # 先轻量检查：索引文件是否存在且有内容
        index_file = Path(cfg.get("paths", {}).get("index_file", ""))
        index_exists = index_file.exists() and index_file.stat().st_size > 0
        if index_exists:
            results["index"]["ok"] = True
            results["index"]["msg"] = "索引就绪"
        elif auto_fix:
            # 索引构建需要全量扫描目录，可能耗时数分钟
            # 不适合放在"一键修复"中，请用户到操作页手动执行
            results["index"]["ok"] = False
            results["index"]["msg"] = '未建立（请在「操作」页点击"建立索引"按钮）'
        else:
            results["index"]["ok"] = False
            results["index"]["msg"] = "未建立（需运行 build_index.py 或点击一键修复）"
    else:
        results["index"]["msg"] = "等待照片目录配置"

    # 8. 总体
    results["all_ok"] = all([
        results["config"]["ok"],
        results["python_deps"]["ok"],
        results["ollama"]["ok"],
        results["model"]["ok"],
        results["database"]["ok"],
    ])

    # 保存配置（路径可能更新了）
    save_config(cfg)

    return results, cfg
