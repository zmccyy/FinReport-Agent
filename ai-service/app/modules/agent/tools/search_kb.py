"""M5.02 search_kb 工具：Milvus 财报知识库检索（M5.07 构建后可用）。

知识库（``fin_kb`` collection）尚未构建时，工具业务性返回
``ok=True + data=null + reason``——LLM 看到原因后基于已有数据回答，
不消耗工具报错计数。Milvus 连接惰性建立 + 进程内复用（失败后下次
重试），embed 走注入的 embedder（ModelHub.embed，512 维归一化）。
"""

from __future__ import annotations

from typing import Any, Protocol

from app.modules.agent.tool_registry import ToolSpec
from app.modules.agent.tools._common import ok_data, ok_null

# 与 scripts/init_milvus.py 一致的检索参数（spec §5.3）。
SEARCH_PARAMS = {"ef": 64}
COLLECTION_NAME = "fin_kb"
TOP_K = 3


class Embedder(Protocol):
    """embed 契约（ModelHub.embed 满足）。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """返回每文本的 512 维归一化向量。"""
        ...


class _MilvusSearcher:
    """惰性连接 Milvus 的检索器（进程内复用连接，MilvusClient 2.4+）。"""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._client: Any = None

    def search(self, vector: list[float], top_k: int) -> list[dict[str, Any]]:
        """向量检索 top_k 条；连接/检索失败抛异常（由调用方转业务结果）。"""
        if self._client is None:
            from pymilvus import MilvusClient

            self._client = MilvusClient(
                uri=f"http://{self.host}:{self.port}", timeout=10
            )
            # drop+重建后的 collection 未驻留内存，检索前必须 load（幂等）。
            self._client.load_collection(COLLECTION_NAME)
        results = self._client.search(
            collection_name=COLLECTION_NAME,
            data=[vector],
            limit=top_k,
            search_params={"metric_type": "IP", "params": SEARCH_PARAMS},
            output_fields=["text", "page", "doc_id", "chunk_type"],
        )
        hits: list[dict[str, Any]] = []
        for hit in (results or [[]])[0]:
            entity = hit.get("entity", {})
            hits.append(
                {
                    "text": entity.get("text", ""),
                    "page": entity.get("page"),
                    "doc_id": entity.get("doc_id"),
                    "chunk_type": entity.get("chunk_type", ""),
                    "score": round(float(hit.get("distance", 0.0)), 4),
                }
            )
        return hits


def make_search_kb(
    embedder: Embedder,
    *,
    milvus_host: str = "localhost",
    milvus_port: int = 19530,
) -> ToolSpec:
    """构建 search_kb 工具。

    Args:
        embedder: 向量编码器（ModelHub.embed 注入）。
        milvus_host/milvus_port: Milvus 地址（默认本地开发栈）。

    Returns:
        ToolSpec；handler 入参 ``{"keywords"}``。
    """
    searcher = _MilvusSearcher(milvus_host, milvus_port)

    def handler(arguments: dict) -> dict:
        keywords = str(arguments.get("keywords") or "").strip()
        if not keywords:
            return ok_null("缺少关键词参数 keywords")
        if embedder is None:
            return ok_null(
                "embedder 未配置——知识库需 M5.07 构建后可用，请基于报表数据回答"
            )
        try:
            vector = embedder.embed([keywords])[0]
            hits = searcher.search(vector, TOP_K)
        except Exception as error:  # 知识库未就绪/连接失败——业务性提示
            return ok_null(
                f"知识库检索不可用：{type(error).__name__}: {error}；"
                "知识库需 M5.07 构建（Milvus fin_kb）后可用，请基于报表数据回答"
            )
        if not hits:
            return ok_null(f"知识库未检索到与 {keywords!r} 相关的段落")
        return ok_data({"query": keywords, "hits": hits})

    return ToolSpec(
        name="search_kb",
        description="在财报知识库中检索与关键词相关的段落（含页码与来源）",
        parameters={
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "检索关键词，如「产能扩张 2025」",
                },
            },
            "required": ["keywords"],
        },
        handler=handler,
    )
