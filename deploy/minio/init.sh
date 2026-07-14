#!/bin/sh
# ============================================================================
# FinReport Agent — MinIO bucket 初始化脚本
# 在 MinIO 容器启动后执行（通过 docker-compose depends_on + 自定义入口）
# 幂等：重复执行安全（mc mb 在桶已存在时静默跳过）
# ============================================================================
# 用法（容器内）:
#   ./init.sh                          # 使用默认配置
# 用法（宿主机 docker exec）:
#   docker compose -f deploy/docker-compose.yml exec minio mc alias set local http://localhost:9000 minioadmin minioadmin
#   docker compose -f deploy/docker-compose.yml exec minio sh /data/init.sh
# ============================================================================

set -e

# --- 配置 ---
MINIO_ALIAS="${MINIO_ALIAS:-local}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin}"

echo "=== FinReport Agent — MinIO Bucket 初始化 ==="
echo "Alias: ${MINIO_ALIAS} | Endpoint: ${MINIO_ENDPOINT}"

# --- 配置 mc alias ---
if ! mc alias set "${MINIO_ALIAS}" "${MINIO_ENDPOINT}" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}"; then
    echo "[WARN] mc alias 配置失败（可能已存在），继续..."
fi

# --- 创建 6 个 bucket（幂等：桶已存在时静默成功）---
mc mb --ignore-existing "${MINIO_ALIAS}/finreport-uploads"
mc mb --ignore-existing "${MINIO_ALIAS}/finreport-artifacts"
mc mb --ignore-existing "${MINIO_ALIAS}/finreport-reports"
mc mb --ignore-existing "${MINIO_ALIAS}/finreport-models"
mc mb --ignore-existing "${MINIO_ALIAS}/finreport-training"
mc mb --ignore-existing "${MINIO_ALIAS}/finreport-backups"

echo "✓ 6 个 bucket 已就绪"

# --- 生命周期规则 ---
# artifacts: 7 天后自动删除（spec §5.5.3）
mc ilm rule add --expire-days "7" "${MINIO_ALIAS}/finreport-artifacts" || echo "  [WARN] ilm rule for finreport-artifacts failed"
echo "  └─ finreport-artifacts: 7 天过期"

# uploads: 90 天后过期（本地点替代 30 天→IA 转换）
mc ilm rule add --expire-days "90" "${MINIO_ALIAS}/finreport-uploads" || echo "  [WARN] ilm rule for finreport-uploads failed"
echo "  └─ finreport-uploads: 90 天过期"

# --- 访问策略 ---
# reports: 允许公开读取（预签名 URL）
mc anonymous set download "${MINIO_ALIAS}/finreport-reports" || echo "  [WARN] anonymous policy for finreport-reports failed"
echo "  └─ finreport-reports: public-read"

# --- 验证 ---
echo ""
echo "=== Bucket 列表 ==="
mc ls "${MINIO_ALIAS}/"

echo ""
echo "=== 初始化完成 ==="
