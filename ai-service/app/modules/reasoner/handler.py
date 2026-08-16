"""M4.05 check handler — 只读 MySQL 拉三表 + RuleEngine/AnomalyDetector/LLMReviewer.

数据链路（decision record #6：check/report 数据从 MySQL 读，L3 只读）：

1. ``ReadOnlyMySqlClient.fetch_report_statements`` 按 taskId 拉
   report 元信息 + financial_statement 科目行；
2. 组装 ``StatementSnapshot``（合并口径 + 本期优先）；
3. ``RuleEngine.check`` 跑三条勾稽规则；
4. ``AnomalyDetector.detect`` 跑同比异常（同公司上一期报表，缺历史降级跳过）；
5. ``LLMReviewer.review`` 复核 WARN/ERROR 规则（降级内建：API 失败保留规则结果）；
6. 返回 ``CheckResult.to_dict()``（M3.04 契约，L2 CheckResultWriter 消费）。

``report`` 步骤仍返回 mock payload —— M4.06 将新增 generator handler
并切换路由（本模块只保留 check 分支）。
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.exceptions import AiException
from app.core.mysql_client import (
    ReadOnlyMySqlClient,
    ReportStatements,
    StatementReader,
    StatementRow,
)
from app.modules.modelhub.modelhub import get_modelhub
from app.modules.reasoner.anomaly_detector import AnomalyDetector
from app.modules.reasoner.llm_reviewer import LLMReviewer
from app.modules.reasoner.rule_engine import RuleEngine
from app.schemas.reasoning import StatementSnapshot
from app.schemas.statement import StatementType
from app.schemas.task import TaskMessage
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# 科目行筛选偏好（A 股年报同时披露合并/母公司、本期/上期两套口径）。
_PREFERRED_SCOPE = "合并"
_PREFERRED_PERIOD = "本期"

_reader: StatementReader | None = None
_reviewer: LLMReviewer | None = None


def configure_handler(
    *,
    reader: StatementReader | None = None,
    reviewer: LLMReviewer | None = None,
) -> None:
    """Inject reader/reviewer dependencies (used by unit tests).

    Args:
        reader: Optional StatementReader override.
        reviewer: Optional LLMReviewer override.
    """
    global _reader, _reviewer
    _reader = reader
    _reviewer = reviewer


def reset_handler() -> None:
    """Clear injected dependencies so defaults are rebuilt lazily."""
    configure_handler(reader=None, reviewer=None)


def _resolve_reader() -> StatementReader:
    """Return the configured reader or build the production default."""
    return _reader if _reader is not None else ReadOnlyMySqlClient(Settings())


def _resolve_reviewer() -> LLMReviewer:
    """Return the configured reviewer or build the production default."""
    return _reviewer if _reviewer is not None else LLMReviewer(get_modelhub())


def build_snapshot(data: ReportStatements) -> StatementSnapshot:
    """把 DB 科目行组装成规则引擎快照（合并 + 本期优先）。

    每个表类型先按 period_type=本期 过滤（无则保留全部），再按
    scope=合并 过滤（无则保留全部）——年报常同时存合并/母公司两套
    完整报表，直接混入会导致同名科目互相覆盖。

    Args:
        data: MySQL 查询结果。

    Returns:
        可直接喂给 ``RuleEngine.check`` 的快照。
    """
    by_type: dict[str, list[StatementRow]] = {}
    for row in data.rows:
        by_type.setdefault(row.statement_type, []).append(row)

    statements: dict[StatementType, dict[str, Any]] = {}
    for type_value, rows in by_type.items():
        try:
            st_type = StatementType(type_value)
        except ValueError:
            LOGGER.warning("[build_snapshot] 未知 statement_type=%s，跳过", type_value)
            continue
        picked = _prefer(rows, lambda r: r.period_type == _PREFERRED_PERIOD)
        picked = _prefer(picked, lambda r: r.scope == _PREFERRED_SCOPE)
        statements[st_type] = {
            row.item_name: row.item_value for row in picked if row.item_value is not None
        }
    return StatementSnapshot(
        report_period=data.report_period,
        currency=data.currency,
        unit=data.unit,
        statements=statements,
    )


def _prefer(rows: list[StatementRow], predicate: Any) -> list[StatementRow]:
    """命中谓词的行非空时只保留命中行，否则原样返回。"""
    picked = [row for row in rows if predicate(row)]
    return picked if picked else rows


async def handle(message: TaskMessage) -> dict[str, Any]:
    """按 step 分发：check 走真实勾稽链路，report 暂留 mock（M4.06 归位）。

    Args:
        message: Validated task message (``check`` / ``report``).

    Returns:
        check → ``CheckResult.to_dict()``（M3.04 契约）；
        report → mock payload。

    Raises:
        ValueError: When the routing step is unknown.
        AiException: When the report data is missing or unreadable.
    """
    if message.step == "check":
        return await _handle_check(message)
    if message.step == "report":
        return {"operation": "report", "status": "mock-complete"}
    raise ValueError(f"Unknown reasoner step: {message.step!r}, expected 'check' or 'report'")


async def _handle_check(message: TaskMessage) -> dict[str, Any]:
    """真实勾稽链路：读库 → 规则 → 异常 → LLM 复核 → 契约 payload。

    Args:
        message: check 任务消息。

    Returns:
        ``CheckResult.to_dict()``。

    Raises:
        AiException: 报表数据缺失或不可读。
    """
    reader = _resolve_reader()
    data = reader.fetch_report_statements(message.task_id)
    if not data.rows:
        raise AiException(
            f"no financial_statement rows for taskId={message.task_id} "
            f"reportId={data.report_id}（EXTRACT 步骤未写入或失败）"
        )
    snapshot = build_snapshot(data)

    result = RuleEngine().check(snapshot)

    # 同比异常：同公司上一期报表；缺历史时降级为仅勾稽规则。
    year_ago_data = reader.fetch_year_ago_statements(data.company_code, data.report_period)
    if year_ago_data is not None and year_ago_data.rows:
        year_ago = build_snapshot(year_ago_data)
        anomalies = AnomalyDetector().detect(snapshot, year_ago=year_ago)
        result = result.model_copy(update={"anomalies": anomalies})

    # LLM 复核 WARN/ERROR 规则（单条失败降级保留规则结果，AGENTS.md §8.2）。
    reviewer = _resolve_reviewer()
    result = await reviewer.review(result, snapshot)

    LOGGER.info(
        "[_handle_check] 勾稽完成 taskId=%s rules=%d anomalies=%d confidence=%.2f period=%s",
        message.task_id,
        len(result.rules),
        len(result.anomalies),
        result.confidence,
        result.report_period,
    )
    return result.to_dict()
