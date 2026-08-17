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
from app.schemas.reasoning import Anomaly, CheckResult, RuleResult, Severity
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

_STATEMENT_SQL = (
    "SELECT statement_type, item_name, item_value, currency, unit, "
    "scope, period_type FROM financial_statement WHERE report_id = %s"
)

# M4.06 report 侧：勾稽结果回读（accounting_check + anomaly 两表）。
_CHECK_SQL = (
    "SELECT rule_type, rule_name, expected, actual, diff, is_pass, "
    "severity, note FROM accounting_check WHERE report_id = %s"
)
_ANOMALY_SQL = (
    "SELECT item_name, anomaly_type, metric_value, threshold, "
    "description, severity FROM anomaly WHERE report_id = %s"
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
    # M4.06：report 生成需要公司名做标题/概况段；旧调用方缺省空串兼容。
    company_name: str = ""


class StatementReader(Protocol):
    """check/report handler 依赖的只读查询契约（测试可注入 fake）。"""

    def fetch_report_statements(self, task_id: str) -> ReportStatements:
        """按 taskId 取报表元信息 + 三表科目行。"""
        ...

    def fetch_year_ago_statements(
        self, company_code: str, current_period: str
    ) -> ReportStatements | None:
        """取同公司本期之前最近一期报表（同比对比期）；无历史返回 None。"""
        ...

    def fetch_check_result(self, report_id: int) -> CheckResult | None:
        """回读 CHECK 步骤持久化的勾稽 + 异常结果；未写入返回 None。"""
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
            "SELECT id, company_code, company_name, report_period FROM report WHERE task_id = %s",
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
                "SELECT id, company_code, company_name, report_period FROM report "
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

    def fetch_check_result(self, report_id: int) -> CheckResult | None:
        """回读 CHECK 步骤持久化的勾稽 + 异常结果（M4.06 report 侧）。

        保留 LLM 复核 note（accounting_check.note 已含复核标记），避免
        report 阶段重复调 API。tolerance / missing_items / llm_reviewed
        未入库，用 schema 默认值（report 生成只用 note / is_pass /
        severity / diff）。confidence 按 RuleEngine 同款公式重算。

        Args:
            report_id: 报表 ID。

        Returns:
            重建的 ``CheckResult``；两表均无行返回 None。

        Raises:
            AiException: 查询失败。
        """
        check_rows = self._query(_CHECK_SQL, (report_id,))
        anomaly_rows = self._query(_ANOMALY_SQL, (report_id,))
        if not check_rows and not anomaly_rows:
            return None

        rules: list[RuleResult] = []
        for row in check_rows:
            try:
                rules.append(_row_to_rule(row))
            except ValueError as error:
                # 未知 rule_type / severity（未来新增规则）跳过，不炸整体。
                LOGGER.warning(
                    "[fetch_check_result] 跳过未知规则行 reportId=%s: %s",
                    report_id,
                    error,
                )
        anomalies = [
            Anomaly(
                item_name=str(row["item_name"] or ""),
                anomaly_type=str(row["anomaly_type"]),
                metric_value=_to_decimal(row["metric_value"]),
                threshold=_to_decimal(row["threshold"]),
                description=str(row["description"] or ""),
                severity=Severity(str(row["severity"])),
            )
            for row in anomaly_rows
        ]
        return CheckResult(
            rules=rules,
            anomalies=anomalies,
            confidence=_recompute_confidence(rules),
            # report 表的 report_period 与 CHECK 写入时同源。
            report_period=str(
                self._query("SELECT report_period FROM report WHERE id = %s", (report_id,))[0][
                    "report_period"
                ]
            ),
        )

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
            company_name=str(report_row.get("company_name") or ""),
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


# ============================================================================
# fetch_check_result 辅助（模块级纯函数，便于单测）
# ============================================================================


def _to_decimal(value: Any) -> Decimal | None:
    """DB 数值列 → Decimal（None 透传）。"""
    if value is None:
        return None
    return Decimal(str(value))


def _row_to_rule(row: dict[str, Any]) -> RuleResult:
    """accounting_check 一行 → ``RuleResult``。

    Args:
        row: 含 rule_type / rule_name / expected / actual / diff / is_pass /
            severity / note 的字典行。

    Returns:
        重建的 ``RuleResult``（tolerance / missing_items 用 schema 默认值）。

    Raises:
        ValueError: rule_type / severity 不在枚举内（pydantic 校验失败）。
    """
    return RuleResult(
        rule_type=str(row["rule_type"]),
        rule_name=str(row["rule_name"]),
        expected=_to_decimal(row["expected"]),
        actual=_to_decimal(row["actual"]),
        diff=_to_decimal(row["diff"]),
        is_pass=bool(row["is_pass"]),
        severity=Severity(str(row["severity"])),
        note=str(row["note"] or ""),
    )


def _recompute_confidence(rules: list[RuleResult]) -> float:
    """按 RuleEngine._compute_confidence 同款公式重算置信度。

    基础分 = 通过数 / 总数；每条 CRITICAL 失败额外扣 0.2；[0.0, 1.0] 截断。
    公式镜像而非 import，保持 core 层不依赖 modules（分层约定）。

    Args:
        rules: 重建的规则结果列表。

    Returns:
        置信度 [0.0, 1.0]。
    """
    if not rules:
        return 0.0
    passed = sum(1 for r in rules if r.is_pass)
    critical_failures = sum(1 for r in rules if not r.is_pass and r.severity == Severity.CRITICAL)
    score = passed / len(rules) - 0.2 * critical_failures
    return round(max(0.0, min(1.0, score)), 4)
