#!/usr/bin/env python3
"""M5.07 财报知识库构建脚本 — spec §3.4。

把 data/sample_reports/（或 --reports-dir 指定目录）的年度报告 PDF 解析、
切块、embedding 后写入 Milvus fin_kb collection 与 MySQL kb_chunks 元数据。
默认先清空重建（幂等）；--dry-run 只解析+切块并打印统计（不连 Milvus/MySQL、
不加载 embedding 模型）。

用法:
    python scripts/build_kb.py                                   # 默认 sample_reports
    python scripts/build_kb.py --reports-dir data/sample_reports --dry-run
    python scripts/build_kb.py --milvus-host milvus --mysql-host mysql  # 容器内

依赖: ai-service 的 app 模块（sys.path 注入），需 prod 依赖已装的环境。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- 注入 ai-service 模块路径（脚本在仓库根/scripts 下运行；容器内可用
#     FINREPORT_AI_SERVICE 环境变量指向 /app/app） ---
_REPO_ROOT = Path(__file__).resolve().parent.parent
_AI_SERVICE = Path(
    __import__("os").environ.get("FINREPORT_AI_SERVICE", str(_REPO_ROOT / "ai-service"))
)
if str(_AI_SERVICE) not in sys.path:
    sys.path.insert(0, str(_AI_SERVICE))

from app.modules.agent.tools.search_kb import COLLECTION_NAME  # noqa: E402
from app.modules.parser.chunker import Chunk, chunk_document  # noqa: E402
from app.modules.parser.document_parser import DocumentParser  # noqa: E402
from app.modules.parser.parser_factory import create_document_parser  # noqa: E402

DEFAULT_REPORTS_DIR = _REPO_ROOT / "data" / "sample_reports"
EMBED_BATCH_SIZE = 32


@dataclass
class BuildStats:
    """单份报告构建统计。"""

    source: str
    doc_id: int
    pages: int
    chunks: int


def _pdf_md5(pdf_bytes: bytes) -> str:
    return hashlib.md5(pdf_bytes).hexdigest()


def _resolve_doc_id(mysql_conn: Any, pdf_md5: str, source: str) -> int:
    """按 pdf_md5 匹配 report 表（doc_id 关联 report.id，spec §3.4）。

    匹配不到时用文件名派生稳定唯一 doc_id（32 位哈希）：同文件重复构建
    幂等，不同文件不冲突（避免固定 0 使 MySQL 按 doc_id 清理时互相覆盖）。
    """
    if mysql_conn is None:
        return int(hashlib.md5(source.encode()).hexdigest()[:8], 16)
    with mysql_conn.cursor() as cursor:
        cursor.execute("SELECT id FROM report WHERE pdf_md5 = %s", (pdf_md5,))
        row = cursor.fetchone()
    if row:
        return int(row[0])
    derived = int(hashlib.md5(source.encode()).hexdigest()[:8], 16)
    print(
        f"[WARN] {source} 未在 report 表匹配（pdf_md5={pdf_md5[:12]}…），"
        f"doc_id 取文件名派生值 {derived}"
    )
    return derived


def _chunk_id(doc_id: int, chunk: Chunk) -> str:
    """唯一块标识（doc_id + 页码 + 页内序号）。"""
    return f"doc{doc_id}-p{chunk.page}-n{chunk.position}"


def _insert_milvus(client: Any, rows: list[dict[str, Any]]) -> None:
    """批量插入 Milvus（rows 已含 embedding 向量，MilvusClient API）。"""
    for i in range(0, len(rows), EMBED_BATCH_SIZE):
        client.insert(
            collection_name=COLLECTION_NAME, data=rows[i : i + EMBED_BATCH_SIZE]
        )


def _ensure_collection(client: Any, host: str, port: int) -> None:
    """drop 并重建 fin_kb（幂等全量重建）。

    字段/索引与 scripts/init_milvus.py 的 FIELD_SPECS / INDEX_PARAMS 保持一致
    （两处修改需同步）。重建而非 delete：Milvus delete 是软删（tombstone），
    多次构建会累积可检索的旧数据。
    """
    from pymilvus import CollectionSchema, DataType, FieldSchema
    from pymilvus.milvus_client.index import IndexParams

    dtype_map = {
        "INT64": DataType.INT64,
        "VARCHAR": DataType.VARCHAR,
        "FLOAT_VECTOR": DataType.FLOAT_VECTOR,
        "INT16": DataType.INT16,
    }
    fields = []
    for spec in _MILVUS_FIELD_SPECS:
        kwargs: dict[str, Any] = {
            "name": spec["name"],
            "dtype": dtype_map[spec["dtype"]],
            "description": spec["description"],
        }
        if "is_primary" in spec:
            kwargs["is_primary"] = spec["is_primary"]
        if "auto_id" in spec:
            kwargs["auto_id"] = spec["auto_id"]
        if "max_length" in spec:
            kwargs["max_length"] = spec["max_length"]
        if "dim" in spec:
            kwargs["dim"] = spec["dim"]
        fields.append(FieldSchema(**kwargs))
    schema = CollectionSchema(
        fields=fields, description="财报知识库 — spec §5.3", enable_dynamic_field=False
    )
    if client.has_collection(COLLECTION_NAME):
        client.drop_collection(COLLECTION_NAME)
    client.create_collection(collection_name=COLLECTION_NAME, schema=schema)
    index_params = IndexParams()
    index_params.add_index(
        field_name="embedding",
        index_type="HNSW",
        metric_type="IP",
        params={"M": 16, "efConstruction": 200},
        index_name="emb_idx",
    )
    client.create_index(collection_name=COLLECTION_NAME, index_params=index_params)
    print(f"[INFO] fin_kb 已重建（{host}:{port}）")


#: 与 scripts/init_milvus.py FIELD_SPECS 一致的 fin_kb schema（同步维护）。
_MILVUS_FIELD_SPECS = [
    {
        "name": "id",
        "dtype": "INT64",
        "is_primary": True,
        "auto_id": True,
        "description": "自增主键",
    },
    {
        "name": "doc_id",
        "dtype": "INT64",
        "description": "关联 report.id 或文件名派生 id",
    },
    {
        "name": "chunk_id",
        "dtype": "VARCHAR",
        "max_length": 64,
        "description": "唯一块标识",
    },
    {
        "name": "embedding",
        "dtype": "FLOAT_VECTOR",
        "dim": 512,
        "description": "bge-small 输出向量",
    },
    {"name": "page", "dtype": "INT16", "description": "页码"},
    {"name": "position", "dtype": "INT16", "description": "页内位置"},
    {
        "name": "chunk_type",
        "dtype": "VARCHAR",
        "max_length": 16,
        "description": "TEXT/TABLE_ROW/TABLE_HEADER",
    },
    {"name": "text", "dtype": "VARCHAR", "max_length": 2048, "description": "原文"},
]


def _insert_mysql(mysql_conn: Any, rows: list[tuple[Any, ...]], doc_id: int) -> None:
    """写 MySQL kb_chunks 元数据（幂等：按 doc_id 先删后插）。"""
    with mysql_conn.cursor() as cursor:
        cursor.execute("DELETE FROM kb_chunks WHERE doc_id = %s", (doc_id,))
        cursor.executemany(
            "INSERT INTO kb_chunks (doc_id, chunk_id, chunk_type, text, page, position)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            rows,
        )
    mysql_conn.commit()


def build_one(
    pdf_path: Path,
    parser: DocumentParser,
    *,
    milvus_host: str,
    milvus_port: int,
    mysql_conn: Any,
    embed_fn: Any,
    dry_run: bool,
) -> BuildStats:
    """处理单份 PDF：解析 → 切块 →（embed → 入库）。"""
    pdf_bytes = pdf_path.read_bytes()
    document = parser.parse_bytes(pdf_bytes, source=pdf_path.name)
    chunks = chunk_document(document)
    if dry_run:
        return BuildStats(pdf_path.name, 0, document.page_count, len(chunks))
    if not chunks:
        print(f"[WARN] {pdf_path.name} 无有效块，跳过")
        return BuildStats(pdf_path.name, 0, document.page_count, 0)

    doc_id = _resolve_doc_id(mysql_conn, _pdf_md5(pdf_bytes), pdf_path.name)

    # 批量 embedding（bge-small-zh-v1.5 CPU，512 维归一化）。
    vectors = embed_fn([c.text for c in chunks])
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": _chunk_id(doc_id, chunk),
            "embedding": vector,
            "page": chunk.page,
            "position": chunk.position,
            "chunk_type": chunk.chunk_type,
            "text": chunk.text,
        }
        for chunk, vector in zip(chunks, vectors)
    ]
    mysql_rows = [
        (
            doc_id,
            _chunk_id(doc_id, chunk),
            chunk.chunk_type,
            chunk.text,
            chunk.page,
            chunk.position,
        )
        for chunk in chunks
    ]

    from pymilvus import MilvusClient

    client = MilvusClient(uri=f"http://{milvus_host}:{milvus_port}", timeout=30)
    _insert_milvus(client, rows)
    _insert_mysql(mysql_conn, mysql_rows, doc_id)
    return BuildStats(pdf_path.name, doc_id, document.page_count, len(chunks))


def main() -> int:
    parser = argparse.ArgumentParser(description="财报知识库构建（spec §3.4）")
    parser.add_argument(
        "--reports-dir", default=str(DEFAULT_REPORTS_DIR), help="PDF 目录"
    )
    parser.add_argument("--milvus-host", default="localhost")
    parser.add_argument("--milvus-port", type=int, default=19530)
    parser.add_argument("--mysql-host", default="localhost")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--mysql-user", default="finreport")
    parser.add_argument("--mysql-password", default="finreport")
    parser.add_argument("--mysql-database", default="finreport")
    parser.add_argument("--dry-run", action="store_true", help="仅解析+切块统计")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    pdfs = sorted(reports_dir.glob("*.pdf"))
    if not pdfs:
        print(f"[ERROR] {args.reports_dir} 下没有 PDF 文件")
        return 1

    # 生产装配：PP-Structure 表格识别 + 报表页过滤（裸 DocumentParser 无
    # layout_analyzer，知识库会丢失表格块）。
    document_parser = create_document_parser()
    mysql_conn = None
    if not args.dry_run:
        import pymysql

        mysql_conn = pymysql.connect(
            host=args.mysql_host,
            port=args.mysql_port,
            user=args.mysql_user,
            password=args.mysql_password,
            database=args.mysql_database,
            charset="utf8mb4",
        )
        # 全量重建：drop+重建 fin_kb（tombstone 问题，见 _ensure_collection），
        # 清空 kb_chunks，然后逐份报告解析入库。
        from pymilvus import MilvusClient

        client = MilvusClient(
            uri=f"http://{args.milvus_host}:{args.milvus_port}", timeout=30
        )
        _ensure_collection(client, args.milvus_host, args.milvus_port)
        with mysql_conn.cursor() as cursor:
            cursor.execute("DELETE FROM kb_chunks")
        mysql_conn.commit()
        print(f"[INFO] kb_chunks 已清空，将由 {args.reports_dir} 重建")

    from app.modules.modelhub.modelhub import get_modelhub

    hub = get_modelhub() if not args.dry_run else None
    stats: list[BuildStats] = []
    for pdf in pdfs:
        print(f"[1/2] 解析+切块: {pdf.name}")
        result = build_one(
            pdf,
            document_parser,
            milvus_host=args.milvus_host,
            milvus_port=args.milvus_port,
            mysql_conn=mysql_conn,
            embed_fn=hub.embed if hub else None,
            dry_run=args.dry_run,
        )
        stats.append(result)
        print(
            f"      pages={result.pages} chunks={result.chunks} doc_id={result.doc_id}"
        )

    if mysql_conn is not None:
        with mysql_conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM kb_chunks")
            total_kb = int(cursor.fetchone()[0])
        mysql_conn.close()
    else:
        total_kb = sum(s.chunks for s in stats)

    print(
        f"\n构建完成: {len(stats)} 份报告, 总 chunk 数 {total_kb}"
        f"{'（dry-run，未写入）' if args.dry_run else '（已写入 Milvus fin_kb + MySQL kb_chunks）'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
