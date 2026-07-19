# FinReport Agent — Backend Dockerfile (SpringBoot 3.2.x / Java 21)
# 多阶段构建：Maven 编译 → JRE 运行
# 版本：v1.0 | 日期：2026-07-14

# ================================================================================
# Stage 1: Build
# ================================================================================
FROM maven:3.9-eclipse-temurin-21-alpine AS build
WORKDIR /build

# 先复制 pom.xml 以利用 Docker 缓存层
COPY pom.xml .

# 下载依赖（缓存友好的层）
RUN --mount=type=cache,target=/root/.m2 \
    mvn dependency:resolve -DskipTests -B -q

# 复制源码并构建
COPY src ./src
RUN --mount=type=cache,target=/root/.m2 \
    mvn package -DskipTests -B -q && \
    cp target/*.jar app.jar

# ================================================================================
# Stage 2: Runtime
# ================================================================================
FROM eclipse-temurin:21-jre-alpine AS runtime
WORKDIR /app

# 创建非 root 用户
RUN addgroup -S finreport && adduser -S finreport -G finreport

# 安装 curl 用于健康检查
RUN apk add --no-cache curl

# 复制 JAR
COPY --from=build /build/app.jar /app/app.jar

# 暴露端口
EXPOSE 8080

# 以非 root 用户运行
USER finreport

# 健康检查
HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=60s \
    CMD curl -sf http://localhost:8080/internal/health || exit 1

ENTRYPOINT ["sh", "-c", "java ${JAVA_OPTS} -jar /app/app.jar"]

# ================================================================================
# Stage 3: Dev (用于 docker-compose.dev.yml)
# ================================================================================
FROM maven:3.9-eclipse-temurin-21-alpine AS dev
WORKDIR /app

RUN apk add --no-cache curl

COPY pom.xml mvnw ./
COPY .mvn ./.mvn

# 依赖写入镜像层；开发 overlay 不再用空的命名卷覆盖 /root/.m2，
# 保证干净 Compose 环境首次启动无需在健康检查窗口内重新下载全部依赖。
# Windows 工作区可能检出 CRLF；若不规范化，Linux 会把 /bin/sh\r 当成解释器而无法执行 wrapper。
RUN sed -i 's/\r$//' mvnw && chmod +x mvnw && ./mvnw dependency:go-offline -DskipTests -B -q

COPY src ./src

EXPOSE 8080 5005

CMD ["./mvnw", "spring-boot:run"]
