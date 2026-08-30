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
    company_code: str | None = None,
) -> ToolRegistry:
    """构建 6 工具注册表（生产默认装配）。

    Args:
        reader: 只读查询客户端（``ReadOnlyMySqlClient`` 实例）。
        report_id: 对话绑定的报表 ID。
        embedder: 向量编码器（``ModelHub.embed``）；None 时 search_kb
            用缺省（连接失败走业务性提示，不影响其它工具）。
        milvus_host/milvus_port: Milvus 地址。
        company_code: search_kb 公司过滤键（M6.08 评估发现 2）；None 时
            自行按 report_id 查询报表元数据解析，显式空串表示不过滤。

    Returns:
        已注册 6 工具的 ``ToolRegistry``。
    """
    if company_code is None:
        company_code = ""
        try:
            data = reader.fetch_report_statements_by_report_id(report_id)
            if data is not None:
                company_code = data.company_code
        except Exception:  # noqa: BLE001 — 过滤键解析失败降级为不过滤
            pass
    registry = ToolRegistry()
    registry.register(make_query_statement(reader, report_id))
    registry.register(make_compute_yoy(reader, report_id))
    registry.register(make_compute_qoq(reader, report_id))
    registry.register(make_check_accounting(reader, report_id))
    registry.register(
        make_search_kb(
            embedder,
            milvus_host=milvus_host,
            milvus_port=milvus_port,
            company_code=company_code,
        )
    )
    registry.register(make_unit_convert())
    return registry


__all__ = ["build_default_registry"]
