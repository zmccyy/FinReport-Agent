#!/usr/bin/env python3
"""fin_kb Milvus schema 共享定义（spec §5.3）。

init_milvus.py（建 collection）与 build_kb.py（drop+重建）共用，
避免两份字段定义漂移。修改 schema 只改本文件。
"""

#: collection 名（search_kb 工具同源引用）。
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

# 字段规格（数据驱动，不依赖 pymilvus 类型；dtype 字符串对应
# pymilvus.DataType 枚举名，由使用方映射）
FIELD_SPECS = [
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
        # M6.08 评估发现 2：多公司共库检索需按公司过滤（对话绑定报表的
        # 公司代码），否则审计机构类事实问题跨公司污染。
        "name": "company_code",
        "dtype": "VARCHAR",
        "max_length": 16,
        "description": "公司代码（6 位 A 股代码；检索过滤键）",
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
    {
        "name": "text",
        "dtype": "VARCHAR",
        "max_length": 2048,
        "description": "原文（用于召回展示）",
    },
]
