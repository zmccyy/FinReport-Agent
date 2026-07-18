#!/bin/sh
# FinReport Agent — Compose MinIO 初始化。
# 幂等：可在每次 compose up 时安全重跑；生产环境应以受管策略替换开发策略。
set -eu

MINIO_ALIAS="finreport"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://minio:9000}"

mc alias set "$MINIO_ALIAS" "$MINIO_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"

# spec §5.5.1 的业务 bucket；a-bucket 是 Milvus standalone 的对象存储 bucket。
for bucket in finreport-uploads finreport-artifacts finreport-reports finreport-models finreport-training finreport-backups a-bucket; do
    mc mb --ignore-existing "$MINIO_ALIAS/$bucket"
done

# 只有最终报告可被开发环境匿名下载；上传文件和中间产物保持私有。
for bucket in finreport-uploads finreport-artifacts finreport-models finreport-training finreport-backups a-bucket; do
    mc anonymous set private "$MINIO_ALIAS/$bucket" >/dev/null
done
mc anonymous set download "$MINIO_ALIAS/finreport-reports" >/dev/null

# mc 在没有生命周期配置时返回非零；已有配置时不重复添加规则。
ensure_expiration_rule() {
    bucket="$1"
    days="$2"
    if ! mc ilm rule ls "$MINIO_ALIAS/$bucket" >/dev/null 2>&1; then
        mc ilm rule add --expire-days "$days" "$MINIO_ALIAS/$bucket" >/dev/null
    fi
}

ensure_expiration_rule finreport-uploads 90
ensure_expiration_rule finreport-artifacts 7

printf '%s\n' 'MinIO buckets, development access policies, and lifecycle rules initialized.'
