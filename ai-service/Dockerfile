# FinReport Agent — AI Service Dockerfile (FastAPI / Python 3.11)
# 版本：v1.1 | 日期：2026-08-18（M4.09：CPU wheel + API 化部署）

# ================================================================================
# Stage 1: Production Runtime
# ================================================================================
FROM python:3.11-slim AS runtime
WORKDIR /app

# 安装系统依赖。python:3.11-slim 现基于 Debian trixie：原 bookworm 的
# libgl1-mesa-glx 已被 libgl1 取代（paddle/opencv 依赖的 libGL.so.1 由其
# 提供），libglib2.0-0 更名为 libglib2.0-0t64。
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgl1 \
    libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装。
# torch 必须先从 PyTorch CPU index 安装：PyPI 上 Linux 的 torch 默认 wheel
# 捆绑 CUDA 运行时（+nvidia-* 系列约 2GB+）。先装 CPU wheel 后，pip 解析
# sentence-transformers 的 torch 依赖时视为已满足，不会再拉 CUDA 版——
# 这是 M4 阶段"prod 镜像 < 3GB"验收标准的唯一路径（2026-08-16 决策 #2）。
# paddlepaddle 的 PyPI 包本身即 CPU 版（GPU 版为 paddlepaddle-gpu），无需处理。
# 不做静默降级：prod 依赖装失败应让构建失败（fail loud），
# 否则镜像看似构建成功、运行时因缺 paddle/torch 而 import 崩溃。
COPY pyproject.toml .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -e ".[prod]"

# 复制应用代码
COPY app/ ./app/

# 创建非 root 用户
RUN useradd -m -s /bin/bash finreport && chown -R finreport:finreport /app
USER finreport

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=30s \
    CMD curl -sf http://localhost:8000/internal/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]

# ================================================================================
# Stage 2: Dev (用于 docker-compose.dev.yml)
# ================================================================================
FROM python:3.11-slim AS dev
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install only dev extras + lightweight clients. Heavy prod dependencies
# (paddlepaddle, torch/sentence-transformers, weasyprint, matplotlib, ...)
# are skipped in dev to keep the image small and build fast. App modules
# import them lazily inside functions (BgeSmallEmbedder delays the
# sentence-transformers import until first embed), so the dev server starts
# without them; embed()/parse() only fail loudly when actually invoked
# without the prod stage. The full prod stage above installs everything.
#
# pyproject.toml declares [build-system] + [tool.setuptools.packages.find],
# so `pip install -e .` builds an editable install. app/ is COPY'd so
# setuptools can discover the package at build time; at runtime the dev
# container mounts the host source over /app/app (read-only) via
# docker-compose.dev.yml, so edits are picked up without rebuilding.
COPY pyproject.toml .
COPY app/ ./app/
RUN pip install --no-cache-dir -e ".[dev]" minio pymilvus

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/app"]
