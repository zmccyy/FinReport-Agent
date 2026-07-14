# FinReport Agent — Frontend Dockerfile (Vue3 / Vite / Nginx)
# 多阶段构建：Node 构建 → Nginx Alpine 运行
# 版本：v1.0 | 日期：2026-07-14

# ================================================================================
# Stage 1: Build
# ================================================================================
FROM node:20-alpine AS build
WORKDIR /app

# Build-time args
ARG VITE_API_BASE_URL=http://localhost:8080
ARG VITE_SSE_BASE_URL=http://localhost:8080
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ENV VITE_SSE_BASE_URL=${VITE_SSE_BASE_URL}

# 先复制 package.json 以利用缓存
COPY package.json package-lock.json* ./

# 安装依赖
RUN --mount=type=cache,target=/root/.npm \
    npm ci --prefer-offline --no-audit 2>/dev/null || npm install --prefer-offline --no-audit

# 复制源码并构建
COPY . .
RUN npm run build

# ================================================================================
# Stage 2: Runtime (Nginx)
# ================================================================================
FROM nginx:1.27-alpine AS runtime

# 安装 curl 用于健康检查
RUN apk add --no-cache curl

# 复制自定义 Nginx 配置
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 复制构建产物
COPY --from=build /app/dist /usr/share/nginx/html

# 创建健康检查端点
RUN echo "OK" > /usr/share/nginx/html/health

# 暴露端口
EXPOSE 80

# 健康检查
HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=10s \
    CMD curl -sf http://localhost:80/health || exit 1

CMD ["nginx", "-g", "daemon off;"]

# ================================================================================
# Stage 3: Dev (用于 docker-compose.dev.yml)
# ================================================================================
FROM node:20-alpine AS dev
WORKDIR /app

RUN apk add --no-cache curl

COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --prefer-offline --no-audit 2>/dev/null || npm install --prefer-offline --no-audit

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
