# ─── 阶段 1: 构建依赖 ──────────────────────────────────────────
FROM docker.1ms.run/library/python:3.12-slim AS builder

WORKDIR /app

# 安装构建时依赖（使用阿里云镜像加速）
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install \
    -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com

# ─── 阶段 2: 运行时镜像 ────────────────────────────────────────
FROM docker.1ms.run/library/python:3.12-slim

# 安装运行时系统依赖（最小化）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 从构建阶段复制 Python 依赖
COPY --from=builder /install /usr/local

WORKDIR /app

# 复制应用代码
COPY . .

# 设置环境变量
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8
ENV PYTHONDONTWRITEBYTECODE=1

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:5000')" || exit 1
