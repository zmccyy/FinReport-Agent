# FinReport Agent — AI Service Dockerfile (FastAPI / Python 3.11)
# 版本：v1.2 | 日期：2026-08-18（M4.10：构建镜像加速 ARG + pango/CJK 字体补全）

# ================================================================================
# Stage 1: Production Runtime
# ================================================================================
FROM python:3.11-slim AS runtime

# 受限网络构建加速（默认官方源；在 deploy/.env 按机器覆盖，见 .env.example）：
#   APT_MIRROR           — 替换 deb.debian.org 的 apt 镜像域名（如 mirrors.aliyun.com）
#   PIP_INDEX_URL        — pip 全局 index（空 = pypi.org）
#   TORCH_SPEC           — torch 安装规格（默认不固定版本）
#   TORCH_INSTALL_ARGS   — torch 安装参数（默认官方 CPU index；平铺镜像需
#                          --find-links + 显式 +cpu 版本号，避免解析到 CUDA 变体）
#   NUMPY_PIN            — numpy 约束，默认无约束。仅当 TORCH_SPEC 固定为
#                          < 2.4 时才需要 "numpy<2"（torch < 2.4 按 numpy 1.x
#                          ABI 编译，与 numpy 2.x 二进制不兼容）。
ARG APT_MIRROR=""
ARG PIP_INDEX_URL=""
ARG TORCH_SPEC=torch
ARG TORCH_INSTALL_ARGS="--index-url https://download.pytorch.org/whl/cpu"
ARG NUMPY_PIN="numpy"

WORKDIR /app

# 安装系统依赖。python:3.11-slim 现基于 Debian trixie：原 bookworm 的
# libgl1-mesa-glx 已被 libgl1 取代（paddle/opencv 依赖的 libGL.so.1 由其
# 提供），libglib2.0-0 更名为 libglib2.0-0t64。
# M4.10：补 weasyprint（PDF 转换）运行所需的 pango 库与中文字体。
# 字体选 fonts-wqy-zenhei（~16MB）而非 fonts-noto-cjk（~90MB）：图表
# matplotlib 候选列表与报告模板 font-family 均含 "WenQuanYi Zen Hei"，
# 缺中文字体时图表/报告会渲染成豆腐块——prod 镜像首次真实构建时发现。
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgl1 \
    libglib2.0-0t64 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装。
# torch 必须先装 CPU wheel：PyPI 上 Linux 的 torch 默认 wheel 依赖
# nvidia-* 系列约 2GB+ CUDA 运行时。先装 CPU wheel 后，pip 解析
# sentence-transformers 的 torch 依赖时视为已满足，不会再拉 CUDA 版——
# 这是 M4 阶段"prod 镜像 < 3GB"验收标准的唯一路径（2026-08-16 决策 #2）。
# paddlepaddle 的 PyPI 包本身即 CPU 版（GPU 版为 paddlepaddle-gpu），无需处理。
# 不做静默降级：prod 依赖装失败应让构建失败（fail loud），
# 否则镜像看似构建成功、运行时因缺 paddle/torch 而 import 崩溃。
COPY pyproject.toml .
# M4.10 瘦身（prod < 3GB 验收）：安装与剥离必须在同一 RUN 层——Docker 层
# 只增不减，跨层删除不缩层。剥离项与量（真实测量）：
#   __pycache__（~240MB，运行期 root 属主写不回，代价是启动 import
#     即时编译 +10~20s，长驻服务可接受）
#   torch/paddle 的 include/ C++ 头文件（~83MB，仅编译原生扩展需要；
#     本项目纯 Python 调用）
#   各依赖包内置 tests/ 目录（~20MB）
#   pip/wheel（~16MB，构建工具）
# setuptools 必须保留——paddle 导入链（cpp_extension）无条件 import
# setuptools，剥掉会令表格识别直接崩（冒烟验证发现）。
# --retries/--timeout：国内镜像大文件（paddle ~500MB）偶发读超时，加强重试。
RUN if [ -n "$PIP_INDEX_URL" ]; then export PIP_INDEX_URL; fi \
    && pip install --no-cache-dir --retries 5 --timeout 120 \
        ${TORCH_SPEC} ${TORCH_INSTALL_ARGS} \
    && pip install --no-cache-dir --retries 5 --timeout 120 -e ".[prod]" "${NUMPY_PIN}" \
    && find /usr/local/lib/python3.11/site-packages -type d -name __pycache__ -prune -exec rm -rf {} + \
    && rm -rf /usr/local/lib/python3.11/site-packages/torch/include \
              /usr/local/lib/python3.11/site-packages/paddle/include \
    && find /usr/local/lib/python3.11/site-packages -maxdepth 2 -type d -name tests -exec rm -rf {} + \
    && python -m pip uninstall -y pip wheel \
    && rm -rf /root/.cache /tmp/*

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

# 同 runtime：受限网络可用 APT_MIRROR / PIP_INDEX_URL 覆盖（deploy/.env）。
ARG APT_MIRROR=""
ARG PIP_INDEX_URL=""
WORKDIR /app

RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends curl \
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
RUN if [ -n "$PIP_INDEX_URL" ]; then export PIP_INDEX_URL; fi \
    && pip install --no-cache-dir -e ".[dev]" minio pymilvus

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/app"]
