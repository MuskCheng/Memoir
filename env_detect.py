#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
env_detect — Memoir 环境自动检测模块

自动识别运行环境、检测依赖可用性、发现可用路径。
支持 Docker (NAS) 和 PC (Windows/Linux/macOS) 两种部署场景。

设计原则：
- 惰性检测，函数按需调用
- 不依赖项目内其他模块（config_module 除外）
- 失败时返回有意义的信息而非抛异常
"""

import os
import sys
import platform
import shutil
import subprocess
from pathlib import Path

# ─── 导入项目配置 ──────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent


def detect_environment():
    """
    检测运行环境基本信息。

    Returns:
        dict: {
            "os": str,              # "linux" | "win32" | "darwin"
            "os_version": str,      # 详细版本
            "in_docker": bool,      # 是否在 Docker 容器中
            "python_version": str,  # e.g. "3.12.2"
            "python_executable": str,
            "cpu_count": int,
            "cpu_threads_auto": int,  # 推荐的 Ollama 线程数
            "architecture": str,    # "x86_64" | "aarch64" | ...
            "has_gpu": bool,        # 是否检测到 NVIDIA GPU
            "gpu_info": str,        # GPU 型号或空字符串
        }
    """
    # OS 检测
    current_os = sys.platform  # "linux", "win32", "darwin"
    os_version = platform.platform()

    # Docker 检测
    in_docker = False
    if current_os == "linux":
        if os.path.exists("/.dockerenv"):
            in_docker = True
        elif os.environ.get("DOCKER_CONTAINER") == "true":
            in_docker = True
        else:
            # 检查 /proc/1/cgroup
            try:
                with open("/proc/1/cgroup", "r", encoding="utf-8") as f:
                    content = f.read()
                    if "docker" in content or "containerd" in content:
                        in_docker = True
            except (OSError, IOError):
                pass

    # CPU
    cpu_count = os.cpu_count() or 4
    if cpu_count > 4:
        cpu_threads_auto = max(2, cpu_count - 2)
    else:
        cpu_threads_auto = cpu_count

    # GPU 检测
    has_gpu = False
    gpu_info = ""

    # 查找 nvidia-smi 可执行文件
    nvidia_smi_path = shutil.which("nvidia-smi")
    if not nvidia_smi_path and current_os == "win32":
        # Windows 下 nvidia-smi 默认不在 PATH 中
        win_candidates = [
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.7\bin\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6\bin\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.5\bin\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4\bin\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.3\bin\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.2\bin\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.1\bin\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.0\bin\nvidia-smi.exe",
        ]
        for c in win_candidates:
            if os.path.exists(c):
                nvidia_smi_path = c
                break

    if nvidia_smi_path:
        try:
            result = subprocess.run(
                [nvidia_smi_path, "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                has_gpu = True
                gpu_info = result.stdout.strip().split("\n")[0]
        except (subprocess.TimeoutExpired, OSError):
            pass
    elif os.environ.get("CUDA_VISIBLE_DEVICES"):
        has_gpu = True
        gpu_info = f"CUDA_VISIBLE_DEVICES={os.environ['CUDA_VISIBLE_DEVICES']}"

    return {
        "os": current_os,
        "os_version": os_version,
        "in_docker": in_docker,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "cpu_count": cpu_count,
        "cpu_threads_auto": cpu_threads_auto,
        "architecture": platform.machine(),
        "has_gpu": has_gpu,
        "gpu_info": gpu_info,
    }


def detect_ollama(base_url=None):
    """
    检测 Ollama 服务状态。

    Args:
        base_url: Ollama API 地址，默认 http://localhost:11434

    Returns:
        dict: {
            "running": bool,
            "reachable": bool,
            "version": str,
            "models": list[str],
            "model_loaded": bool,
            "has_gpu": bool,
            "gpu_info": str,
        }
    """
    if base_url is None:
        base_url = "http://localhost:11434"

    result = {
        "running": False,
        "reachable": False,
        "version": "",
        "models": [],
        "model_loaded": False,
        "has_gpu": False,
        "gpu_info": "",
    }

    try:
        import requests
        # 检查 Ollama API 版本
        resp = requests.get(f"{base_url}/api/version", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            result["version"] = data.get("version", "")
            result["running"] = True
            result["reachable"] = True
        else:
            return result
    except Exception:
        return result

    try:
        import requests
        # 获取模型列表
        resp = requests.get(f"{base_url}/api/tags", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            result["models"] = models

            # 检查是否有已加载的模型
            running = [m for m in data.get("models", []) if m.get("size", 0) > 0]
            if running:
                result["model_loaded"] = True
    except Exception:
        pass

    # GPU 信息
    try:
        import requests
        resp = requests.get(f"{base_url}/api/ps", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("models"):
                result["has_gpu"] = True
                for m in data["models"]:
                    if m.get("gpu", {}).get("name"):
                        result["gpu_info"] = m["gpu"]["name"]
                        break
    except Exception:
        pass

    return result


def detect_paths(env_info=None):
    """
    自动发现可用路径。

    Args:
        env_info: detect_environment() 的返回值，为 None 时自动调用

    Returns:
        dict: {
            "photo_dir": str,      # 发现的相册目录
            "project_dir": str,    # 数据存储目录
            "font_file": str,      # 字体文件路径
            "photo_candidates": list[str],  # 候选相册目录列表
        }
    """
    if env_info is None:
        env_info = detect_environment()

    result = {
        "photo_dir": "",
        "project_dir": "",
        "font_file": "",
        "photo_candidates": [],
    }

    # 项目/数据目录
    if env_info["in_docker"]:
        # Docker 环境：使用 docker-compose.yml 定义的标准路径
        result["project_dir"] = "/data"
        photo_candidates = ["/photos"]
    elif env_info["os"] == "win32":
        # Windows 环境
        user_home = Path(os.environ.get("USERPROFILE", Path.home()))
        result["project_dir"] = str(SCRIPT_DIR / "data")
        photo_candidates = [
            str(user_home / "Photos"),
            str(user_home / "Pictures"),
            "D:\\Photos",
            "D:\\Pictures",
            "E:\\Photos",
            "E:\\Pictures",
        ]
    else:
        # Linux / macOS
        user_home = Path.home()
        result["project_dir"] = str(SCRIPT_DIR / "data")
        photo_candidates = [
            str(user_home / "Photos"),
            str(user_home / "Pictures"),
            str(user_home / "photos"),
            "/photos",
        ]

    # 筛选实际存在的目录
    existing = [p for p in photo_candidates if os.path.isdir(p)]
    result["photo_candidates"] = existing
    if existing:
        result["photo_dir"] = existing[0]

    # 字体文件搜索
    font_candidates = [
        SCRIPT_DIR / "ark-pixel-12px-monospaced-zh_cn.ttf",
        SCRIPT_DIR / "data" / "ark-pixel-12px-monospaced-zh_cn.ttf",
    ]
    if env_info["in_docker"]:
        font_candidates.insert(0, Path("/app/ark-pixel-12px-monospaced-zh_cn.ttf"))
    for fc in font_candidates:
        if fc.exists():
            result["font_file"] = str(fc)
            break

    return result


def detect_tools():
    """
    检测可用的外部工具。

    Returns:
        dict: { "tool_name": str|None, ... }
            值为工具路径（str）或 None（未找到）
    """
    tools = ["ollama", "git", "ffmpeg"]
    result = {}
    for tool in tools:
        path = shutil.which(tool)
        result[tool] = path
    return result


def check_python_deps():
    """
    检查 Python 依赖包。

    Returns:
        dict: { "package_name": bool, ... }
    """
    packages = {
        "Pillow": "PIL",
        "requests": "requests",
        "flask": "flask",
        "apscheduler": "apscheduler",
        "numpy": "numpy",
        "croniter": "croniter",
        "opencv-python": "cv2",
    }
    result = {}
    for name, import_name in packages.items():
        try:
            __import__(import_name)
            result[name] = True
        except ImportError:
            result[name] = False
    return result


def validate_config(cfg):
    """
    验证配置文件的完整性。

    Args:
        cfg: 配置字典

    Returns:
        list[dict]: 问题列表，每项包含 "level" (error/warning), "message"
    """
    issues = []
    paths = cfg.get("paths", {})

    # 路径检查
    path_checks = [
        ("photo_dir", "相册目录"),
        ("project_dir", "数据目录"),
        ("font_file", "字体文件"),
    ]
    for key, label in path_checks:
        val = paths.get(key, "")
        if not val:
            issues.append({"level": "warning", "message": f"{label}(paths.{key}) 未配置"})
        elif not os.path.exists(val):
            issues.append({"level": "error", "message": f"{label} 不存在: {val}"})

    # Ollama 检查
    ollama_url = cfg.get("ollama", {}).get("base_url", "http://localhost:11434")
    ollama_model = cfg.get("ollama", {}).get("model", "")
    if not ollama_model:
        issues.append({"level": "warning", "message": "Ollama 模型未配置"})

    # API 密钥检查
    push = cfg.get("push_settings", {})
    if not push.get("api_key"):
        issues.append({"level": "warning", "message": "推送 API Key 未配置（推送功能不可用）"})
    if not push.get("device_mac"):
        issues.append({"level": "warning", "message": "设备 MAC 未配置（推送功能不可用）"})

    # 索引文件
    index_file = paths.get("index_file", "")
    if index_file and not os.path.exists(index_file):
        issues.append({"level": "warning", "message": f"索引文件不存在: {index_file}（需要运行 build_index.py）"})

    return issues
