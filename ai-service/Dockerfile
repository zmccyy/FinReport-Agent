# FinReport Agent — AI Service Dockerfile (FastAPI / Python 3.11)
# 版本：v1.0 | 日期：2026-07-14

# ================================================================================
# Stage 1: Production Runtime
# ================================================================================
FROM python:3.11-slim AS runtime
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[prod]" 2>/dev/null || \
    pip install --no-cache-dir fastapi uvicorn[standard]

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
# (paddlepaddle, torch, transformers, weasyprint, matplotlib, ...) are skipped
# in dev to keep the image small and build fast. App modules import them
# lazily inside functions, so the dev server starts and serves mocked
# endpoints without them. The full prod stage above installs everything.
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]" minio pymilvus

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/app"]
