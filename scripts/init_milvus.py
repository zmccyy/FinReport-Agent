#!/usr/bin/env python3
"""
初始化 Milvus collection — M1.05

用法:
    python scripts/init_milvus.py                           # 使用默认连接参数
    python scripts/init_milvus.py --host localhost --port 19530
    python scripts/init_milvus.py --dry-run                  # 仅打印 schema

依赖:
    pip install pymilvus
"""

import argparse
import os
import sys
import time

try:
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        MilvusClient,
        connections,
        utility,
    )
except ImportError:
    print("请先安装 pymilvus: pip install pymilvus")
    sys.exit(1)


# ============================================================================
# 配置：spec §5.3 fin_kb collection
# ============================================================================

COLLECTION_NAME = "fin_kb"
COLLECTION_DESC = "财报知识库 — spec §5.3"

# HNSW 索引参数：spec §5.3 — M=16, efConstruction=200, 查询 ef=64
INDEX_PARAMS = {
    "index_type": "HNSW",
    "metric_type": "IP",  # 内积（bge 输出已归一化）
    "params": {
        "M": 16,
        "efConstruction": 200,
    },
}

# 查询参数
SEARCH_PARAMS = {
    "ef": 64,
}

# 字段定义：spec §5.3 表
FIELDS = [
    FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True, description="自增主键"),
    FieldSchema(name="doc_id", dtype=DataType.INT64, description="关联 report.id"),
    FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64, description="唯一块标识"),
    FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=512, description="bge-small 输出向量"),
    FieldSchema(name="page", dtype=DataType.INT16, description="页码"),
    FieldSchema(name="position", dtype=DataType.INT16, description="页内位置"),
    FieldSchema(name="chunk_type", dtype=DataType.VARCHAR, max_length=16, description="TEXT/TABLE_ROW/TABLE_HEADER"),
    FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=2048, description="原文（用于召回展示）"),
]


def build_schema() -> CollectionSchema:
    """构建 fin_kb collection schema（spec §5.3）。"""
    return CollectionSchema(
        fields=FIELDS,
        description=COLLECTION_DESC,
        enable_dynamic_field=False,
    )


def create_collection(host: str = "localhost", port: int = 19530, dry_run: bool = False) -> bool:
    """
    创建 fin_kb collection 并建立 HNSW 索引。

    Args:
        host: Milvus 服务地址
        port: Milvus gRPC 端口
        dry_run: True 时仅打印 schema 不执行

    Returns:
        True 如果 collection 已就绪
    """
    if dry_run:
        print("=== DRY RUN — Schema 预览 ===\n")
        schema = build_schema()
        print(f"Collection: {COLLECTION_NAME}")
        print(f"描述: {COLLECTION_DESC}")
        print(f"\n字段 ({len(FIELDS)}):")
        for f in FIELDS:
            print(f"  {f.name:>12} | {str(f.dtype):>18} | {f.description}")
        print(f"\n索引: {INDEX_PARAMS['index_type']}")
        print(f"  距离度量: {INDEX_PARAMS['metric_type']}")
        print(f"  M: {INDEX_PARAMS['params']['M']}")
        print(f"  efConstruction: {INDEX_PARAMS['params']['efConstruction']}")
        print(f"  查询 ef: {SEARCH_PARAMS['ef']}")
        return True

    # --- 连接 Milvus ---
    print(f"连接 Milvus: {host}:{port}")
    max_retries = 10
    for i in range(max_retries):
        try:
            connections.connect(alias="default", host=host, port=port)
            break
        except Exception:
            if i < max_retries - 1:
                print(f"  等待 Milvus 就绪... ({i+1}/{max_retries})")
                time.sleep(3)
            else:
                print(f"[ERROR] 无法连接 Milvus ({host}:{port})")
                print("   请确认: docker compose -f deploy/docker-compose.yml up -d milvus")
                sys.exit(1)

    print(f"[OK] 已连接到 Milvus ({host}:{port})\n")

    # --- 检查 collection 是否已存在 ---
    if utility.has_collection(COLLECTION_NAME):
        print(f"[SKIP]  Collection '{COLLECTION_NAME}' 已存在")

        # 验证 schema
        col = Collection(COLLECTION_NAME)
        existing_desc = col.description
        print(f"   描述: {existing_desc}")
        print(f"   实体数: {col.num_entities}")

        # 检查索引
        has_index = False
        try:
            idx_list = col.indexes
            if idx_list:
                for idx in idx_list:
                    print(f"   索引: {idx.index_name} ({idx.params})")
                has_index = True
        except (AttributeError, TypeError) as e:
            # pymilvus v3 中 .indexes 的访问方式可能不同，视为索引状态未知
            print(f"   [WARN] 无法读取索引状态 ({e})，将在 load 时自动处理")
        except Exception:
            raise  # 其他异常不应静默吞掉

        if not has_index:
            # collection 存在但缺索引 → 补充创建
            print("   [WARN] 缺少索引，补充创建...")
            col.create_index(
                field_name="embedding",
                index_params={
                    "index_type": INDEX_PARAMS["index_type"],
                    "metric_type": INDEX_PARAMS["metric_type"],
                    "params": INDEX_PARAMS["params"],
                },
            )
            print(f"   [OK] 索引已创建: HNSW (M=16, efConstruction=200)")

        # 加载到内存
        col.load()
        print(f"   [OK] 已加载到内存")
        return True

    # --- 创建 collection ---
    print(f"创建 collection: '{COLLECTION_NAME}'")
    schema = build_schema()
    col = Collection(name=COLLECTION_NAME, schema=schema, using="default")
    print(f"[OK] Schema 已创建 ({len(FIELDS)} 字段)")

    # --- 创建 HNSW 索引 ---
    print(f"创建 HNSW 索引: M={INDEX_PARAMS['params']['M']}, efConstruction={INDEX_PARAMS['params']['efConstruction']}")
    col.create_index(
        field_name="embedding",
        index_params={
            "index_type": INDEX_PARAMS["index_type"],
            "metric_type": INDEX_PARAMS["metric_type"],
            "params": INDEX_PARAMS["params"],
        },
    )
    print("[OK] 索引已创建")

    # --- 加载到内存 ---
    col.load()
    print("[OK] Collection 已加载到内存")

    print(f"\n{'='*60}")
    print(f"Collection '{COLLECTION_NAME}' 就绪")
    print(f"验证: python -c \"from pymilvus import utility; print(utility.list_collections())\"")
    print(f"      或访问 Attu UI 查看")
    print(f"{'='*60}")

    return True


def drop_collection(host: str = "localhost", port: int = 19530):
    """删除 fin_kb collection（用于重建）。"""
    connections.connect(alias="default", host=host, port=port)
    if utility.has_collection(COLLECTION_NAME):
        utility.drop_collection(COLLECTION_NAME)
        print(f"[OK] 已删除 collection: '{COLLECTION_NAME}'")
    else:
        print(f"[SKIP]  Collection '{COLLECTION_NAME}' 不存在，无需删除")


def main():
    parser = argparse.ArgumentParser(
        description="初始化 FinReport Agent Milvus collection（spec §5.3）",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MILVUS_HOST", "localhost"),
        help="Milvus 服务地址（默认: localhost）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MILVUS_PORT", "19530")),
        help="Milvus gRPC 端口（默认: 19530）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印 schema 预览，不实际创建",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="删除 collection（用于重建）",
    )
    args = parser.parse_args()

    try:
        if args.drop:
            drop_collection(host=args.host, port=args.port)
        else:
            create_collection(host=args.host, port=args.port, dry_run=args.dry_run)
    finally:
        # 断开连接（dry-run 模式下可能未建立连接，忽略断开异常）
        try:
            connections.disconnect("default")
        except Exception:
            pass


if __name__ == "__main__":
    main()
