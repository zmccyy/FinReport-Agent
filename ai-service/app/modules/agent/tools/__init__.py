"""M5.02 工具工厂汇总：构建生产 ToolRegistry（依赖注入，测试可替换）。"""

from __future__ import annotations

from app.core.mysql_client import StatementReader
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.agent.tools.check_accounting import make_check_accounting
from app.modules.agent.tools.compute_qoq import make_compute_qoq
from app.modules.agent.tools.compute_yoy import make_compute_yoy
from app.modules.agent.tools.query_statement import make_query_statement
from app.modules.agent.tools.search_kb import make_search_kb
from app.modules.agent.tools.unit_convert import make_unit_convert


def build_default_registry(
    reader: StatementReader,
    report_id: int,
    embedder=None,
    *,
    milvus_host: str = "localhost",
    milvus_port: int = 19530,
) -> ToolRegistry:
    """构建 6 工具注册表（生产默认装配）。

    Args:
        reader: 只读查询客户端（``ReadOnlyMySqlClient`` 实例）。
        report_id: 对话绑定的报表 ID。
        embedder: 向量编码器（``ModelHub.embed``）；None 时 search_kb
            用缺省（连接失败走业务性提示，不影响其它工具）。
        milvus_host/milvus_port: Milvus 地址。

    Returns:
        已注册 6 工具的 ``ToolRegistry``。
    """
    registry = ToolRegistry()
    registry.register(make_query_statement(reader, report_id))
    registry.register(make_compute_yoy(reader, report_id))
    registry.register(make_compute_qoq(reader, report_id))
    registry.register(make_check_accounting(reader, report_id))
    registry.register(
        make_search_kb(embedder, milvus_host=milvus_host, milvus_port=milvus_port)
    )
    registry.register(make_unit_convert())
    return registry


__all__ = ["build_default_registry"]
