#!/usr/bin/env python3
"""
初始化 MinIO bucket — M1.04

用法:
    python scripts/init_minio.py                          # 使用默认连接参数
    python scripts/init_minio.py --endpoint localhost:9000 # 指定端点
    python scripts/init_minio.py --dry-run                 # 仅打印将要执行的操作

依赖:
    pip install minio
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    from minio import Minio
    from minio.commonconfig import ENABLED, Filter
    from minio.lifecycleconfig import LifecycleConfig, Rule, Expiration
    from minio.error import S3Error
except ImportError:
    print("请先安装 minio 客户端: pip install minio")
    sys.exit(1)


# ============================================================================
# 配置：spec §5.5.1 的 6 个 bucket
# ============================================================================

BUCKETS = {
    "finreport-uploads": {
        "description": "用户上传的原始 PDF",
        "access": "private",
        "lifecycle": {
            "expiry_days": 90,  # spec 原定 30 天转 IA；MinIO 本地无 tier 功能，用 90 天过期兜底
            "note": "生产环境应配置远程 tier 实现 30 天→IA 转换，此处用 90 天过期作为兜底",
        },
    },
    "finreport-artifacts": {
        "description": "中间产物（页面图像、表格 HTML、抽取 JSON）",
        "access": "private",
        "lifecycle": {
            "expiry_days": 7,  # spec §5.5.3: 7 天后自动删除
        },
    },
    "finreport-reports": {
        "description": "最终报告（PDF、Markdown、图表 PNG）",
        "access": "public-read",  # 预签名 URL 公开访问
        "lifecycle": None,  # 报告长期保留
    },
    "finreport-models": {
        "description": "LoRA adapter 存储",
        "access": "private",
        "lifecycle": None,
    },
    "finreport-training": {
        "description": "训练数据集",
        "access": "private",
        "lifecycle": None,
    },
    "finreport-backups": {
        "description": "数据库/配置备份",
        "access": "private",
        "lifecycle": None,
    },
}


def build_lifecycle_config(bucket_name: str, bucket_cfg: dict) -> LifecycleConfig | None:
    """为 bucket 构建生命周期规则。"""
    lifecycle = bucket_cfg.get("lifecycle")
    if lifecycle is None:
        return None

    expiry_days = lifecycle.get("expiry_days")
    if expiry_days is None:
        return None

    rule_id = f"expire-{bucket_name}-{expiry_days}d"
    return LifecycleConfig(
        [
            Rule(
                ENABLED,
                rule_filter=Filter(prefix=""),
                rule_id=rule_id,
                expiration=Expiration(days=expiry_days),
            ),
        ],
    )


def build_public_read_policy(bucket_name: str) -> str:
    """为 reports bucket 构建公开读策略（允许预签名 URL 生成）。"""
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
            },
        ],
    }
    return json.dumps(policy)


def init_buckets(
    endpoint: str = "localhost:9000",
    access_key: str | None = None,
    secret_key: str | None = None,
    secure: bool = False,
    dry_run: bool = False,
) -> dict[str, str]:
    """
    初始化所有 MinIO bucket。

    Args:
        endpoint: MinIO S3 API 地址
        access_key: 访问密钥（默认从环境变量 MINIO_ROOT_USER 读取）
        secret_key: 秘密密钥（默认从环境变量 MINIO_ROOT_PASSWORD 读取）
        secure: 是否使用 HTTPS
        dry_run: True 时仅打印操作不执行

    Returns:
        操作结果字典 {bucket_name: status}
    """
    # 解析环境变量
    if access_key is None:
        access_key = os.environ.get("MINIO_ROOT_USER", "minioadmin")
    if secret_key is None:
        secret_key = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")

    results: dict[str, str] = {}

    if dry_run:
        print("=== DRY RUN 模式 — 不会实际执行 ===\n")
        for name, cfg in BUCKETS.items():
            action = "创建" if cfg["lifecycle"] else "创建（无生命周期）"
            print(f"  [{cfg['access']:>12}] {name}  ← {action}: {cfg['description']}")
            if cfg["lifecycle"]:
                print(f"            └─ 生命周期: {cfg['lifecycle']['expiry_days']} 天后过期")
        print("\n连接信息:")
        print(f"  endpoint: {endpoint}")
        print(f"  access_key: {access_key}")
        print(f"  secure: {secure}")
        print(f"  共 {len(BUCKETS)} 个 bucket 待创建")
        return results

    # --- 连接 MinIO ---
    print(f"连接 MinIO: {endpoint} (secure={secure})")
    try:
        client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        # 快速验证连接
        client.list_buckets()
    except S3Error as e:
        print(f"[ERROR] 连接 MinIO 失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] 无法连接到 MinIO ({endpoint}): {e}")
        print("   请确认 MinIO 容器已启动: docker compose -f deploy/docker-compose.yml up -d minio")
        sys.exit(1)

    print(f"[OK] 已连接到 MinIO (API: {endpoint})\n")

    existing_buckets = {b.name for b in client.list_buckets()}

    # --- 创建 bucket + 设置策略 ---
    for name, cfg in BUCKETS.items():
        try:
            # 创建 bucket（幂等）
            if name in existing_buckets:
                print(f"[SKIP]  [{name}] 已存在，跳过创建")
                results[name] = "exists"
            else:
                client.make_bucket(name)
                print(f"[OK] [{name}] 已创建 — {cfg['description']}")
                results[name] = "created"

            # 设置生命周期规则
            lc_config = build_lifecycle_config(name, cfg)
            if lc_config is not None:
                client.set_bucket_lifecycle(name, lc_config)
                expiry = cfg["lifecycle"]["expiry_days"]  # type: ignore[index]
                print(f"  └─ 生命周期: {expiry} 天后过期")

            # reports bucket 设置公开读策略
            if cfg["access"] == "public-read":
                policy = build_public_read_policy(name)
                client.set_bucket_policy(name, policy)
                print(f"  └─ 访问策略: public-read (预签名 URL)")

        except S3Error as e:
            print(f"[ERROR] [{name}] 操作失败: {e}")
            results[name] = "error"

    # --- 汇总 ---
    print(f"\n{'='*60}")
    created = sum(1 for v in results.values() if v == "created")
    existed = sum(1 for v in results.values() if v == "exists")
    errors = sum(1 for v in results.values() if v == "error")
    print(f"结果: {created} 新建, {existed} 已存在, {errors} 失败")
    print(f"验证: docker compose -f deploy/docker-compose.yml exec minio mc ls local")
    print(f"{'='*60}")

    if errors > 0:
        sys.exit(1)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="初始化 FinReport Agent MinIO bucket（spec §5.5.1）",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
        help="MinIO S3 API 地址（默认: localhost:9000）",
    )
    parser.add_argument(
        "--access-key",
        default=None,
        help="MinIO 访问密钥（默认: $MINIO_ROOT_USER 或 minioadmin）",
    )
    parser.add_argument(
        "--secret-key",
        default=None,
        help="MinIO 秘密密钥（默认: $MINIO_ROOT_PASSWORD 或 minioadmin）",
    )
    parser.add_argument(
        "--secure",
        action="store_true",
        help="使用 HTTPS 连接",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印将要执行的操作，不实际执行",
    )
    args = parser.parse_args()

    init_buckets(
        endpoint=args.endpoint,
        access_key=args.access_key,
        secret_key=args.secret_key,
        secure=args.secure,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
