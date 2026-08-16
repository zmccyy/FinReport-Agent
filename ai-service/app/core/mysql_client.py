"""M4.05 L3 只读 MySQL 客户端（check 数据通路查询侧）.

分层约定（spec §3.2 + decision record #6）：L3 对 MySQL 只读（SELECT），
写入归 L2。查询目标：

* ``report`` — taskId → report_id / company_code / report_period；
* ``financial_statement`` — report_id → 三表科目行（勾稽快照原料）；
* 同比对比期 — 同 company_code 下 report_period 早于本期的最近一期。

实现要点：

1. **延迟导入 pymysql** — 单测注入 fake reader 时无需安装驱动；
2. **短连接** — 每次 query 开关连接，量小（CHECK 每任务 2-3 次）无池化必要；
3. **参数化查询** — 全部走 %s 占位符（AGENTS.md §8.5 禁止拼接）；
4. **失败抛 AiException** — consumer 转 FAILED progress，任务可重试。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from app.core.config import Settings
from app.core.exceptions import AiException
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

_STATEMENT_SQL = (
    "SELECT statement_type, item_name, item_value, currency, unit, "
    "scope, period_type FROM financial_statement WHERE report_id = %s"
)


@dataclass(frozen=True)
class StatementRow:
    """financial_statement 一行（勾稽快照原料）。"""

    statement_type: str
    item_name: str
    item_value: Decimal | None
    scope: str
    period_type: str


@dataclass(frozen=True)
class ReportStatements:
    """一个报表的元信息 + 科目行集合。"""

    report_id: int
    company_code: str
    report_period: str
    currency: str
    unit: str
    rows: tuple[StatementRow, ...]


class StatementReader(Protocol):
    """check handler 依赖的只读查询契约（测试可注入 fake）。"""

    def fetch_report_statements(self, task_id: str) -> ReportStatements:
        """按 taskId 取报表元信息 + 三表科目行。"""
        ...

    def fetch_year_ago_statements(
        self, company_code: str, current_period: str
    ) -> ReportStatements | None:
        """取同公司本期之前最近一期报表（同比对比期）；无历史返回 None。"""
        ...


class ReadOnlyMySqlClient:
    """PyMySQL 实现的只读客户端（SELECT-only，短连接）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        """Create a read-only MySQL client.

        Args:
            settings: Runtime settings (host/port/user/password/database).
        """
        self.settings = settings or Settings()

    def fetch_report_statements(self, task_id: str) -> ReportStatements:
        """按 taskId 取报表元信息 + 三表科目行。

        Args:
            task_id: 任务 ID（report.task_id 唯一键）。

        Returns:
            报表元信息 + 科目行。

        Raises:
            AiException: task 无关联 report 或查询失败。
        """
        reports = self._query(
            "SELECT id, company_code, report_period FROM report WHERE task_id = %s",
            (task_id,),
        )
        if not reports:
            raise AiException(f"report not found for taskId={task_id}")
        return self._load_statements(reports[0])

    def fetch_year_ago_statements(
        self, company_code: str, current_period: str
    ) -> ReportStatements | None:
        """取同公司本期之前最近一期报表（同比对比期）。

        Args:
            company_code: 公司代码（精确匹配）。
            current_period: 本期报告期末 YYYY-MM-DD（字符串比较）。

        Returns:
            对比期报表；无历史或查询失败返回 None（同比是增强能力，
            缺历史时降级为仅勾稽规则，不阻断 CHECK）。
        """
        try:
            reports = self._query(
                "SELECT id, company_code, report_period FROM report "
                "WHERE company_code = %s AND report_period < %s "
                "ORDER BY report_period DESC LIMIT 1",
                (company_code, current_period),
            )
        except AiException as error:
            LOGGER.warning(
                "[fetch_year_ago_statements] 同比对比期查询失败，降级跳过: %s",
                error,
            )
            return None
        if not reports:
            return None
        return self._load_statements(reports[0])

    def _load_statements(self, report_row: dict[str, Any]) -> ReportStatements:
        """组装单个 report 的科目行集合。

        Args:
            report_row: 含 id / company_code / report_period 的行。

        Returns:
            完整 ReportStatements。

        Raises:
            AiException: 查询失败。
        """
        report_id = int(report_row["id"])
        raw_rows = self._query(_STATEMENT_SQL, (report_id,))
        rows = tuple(
            StatementRow(
                statement_type=str(row["statement_type"]),
                item_name=str(row["item_name"]),
                item_value=(
                    Decimal(str(row["item_value"])) if row["item_value"] is not None else None
                ),
                scope=str(row.get("scope") or "合并"),
                period_type=str(row.get("period_type") or "本期"),
            )
            for row in raw_rows
        )
        currency = str(raw_rows[0]["currency"]) if raw_rows else "CNY"
        unit = str(raw_rows[0]["unit"]) if raw_rows else "元"
        return ReportStatements(
            report_id=report_id,
            company_code=str(report_row["company_code"]),
            report_period=str(report_row["report_period"]),
            currency=currency,
            unit=unit,
            rows=rows,
        )

    def _query(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        """执行一条参数化 SELECT 并返回字典行列表。

        Args:
            sql: SELECT 语句（%s 占位符）。
            params: 占位符参数。

        Returns:
            字典行列表。

        Raises:
            AiException: 驱动缺失 / 连接失败 / 查询失败。
        """
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
        except AiException:
            raise
        except Exception as error:  # noqa: BLE001 — 统一转 AiException
            raise AiException(f"MySQL query failed: {error}") from error
        finally:
            connection.close()

    def _connect(self) -> Any:
        """建立短连接。

        Returns:
            PyMySQL connection（DictCursor 模式）。

        Raises:
            AiException: 驱动缺失或连接失败。
        """
        try:
            import pymysql
        except ImportError as error:
            raise AiException("pymysql not installed; run pip install pymysql") from error
        try:
            return pymysql.connect(
                host=self.settings.mysql_host,
                port=self.settings.mysql_port,
                user=self.settings.mysql_user,
                password=self.settings.mysql_password,
                database=self.settings.mysql_database,
                charset="utf8mb4",
                connect_timeout=5,
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
            )
        except AiException:
            raise
        except Exception as error:  # noqa: BLE001 — 统一转 AiException
            raise AiException(f"MySQL connect failed: {error}") from error
