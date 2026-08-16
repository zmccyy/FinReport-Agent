"""M4.04 extract handler — MinIO 拉 parse 产物 + 表格筛选 + DeepSeek 真实抽取.

数据链路（decision record #6：L2 payload 不携带上游 result，走对象存储中转）：

1. 从 ``finreport-artifacts/parsed/{taskId}.json`` 拉取 M4.03 parse 产物；
2. 按特征科目关键词给表格评分，选出目标报表（BS/IS/CF）最匹配的表格；
3. ``extract_with_retry`` 走 ModelHub → DeepSeek API（json_mode + temp=0.1 重试）；
4. 组装 M2.09 契约 payload，供 L2 ``StatementWriter`` 写入 financial_statement。

失败语义：产物缺失 / schema 不匹配 / 找不到匹配表格均抛 ``AiException``，
由 MQ consumer 发 FAILED progress + nack 进 DLQ（表格缺失是永久性错误，
重试无益但进 DLQ 保留排查现场，与 parse 处理一致）。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import AiException
from app.core.minio_client import MinioObjectClient, ObjectStore
from app.modules.extractor.extractor import Extractor
from app.modules.extractor.validator import (
    ValidationResult,
    Validator,
    extract_with_retry,
)
from app.modules.modelhub.modelhub import ModelHub, get_modelhub
from app.modules.parser.handler import parsed_object_key
from app.schemas.document import Document, TableBlock
from app.schemas.statement import ExtractionResult, StatementType
from app.schemas.task import TaskMessage
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# step name → L3 StatementType.value（L2 锁定同一组字符串值）。
_STEP_TO_TYPE = {
    "extract.bs": "balance_sheet",
    "extract.is": "income_statement",
    "extract.cf": "cash_flow",
}

# 每张报表的特征科目关键词（表格筛选评分；命中越多越可能是目标报表）。
_TABLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "balance_sheet": (
        "资产总计",
        "负债合计",
        "所有者权益合计",
        "货币资金",
        "应收账款",
    ),
    "income_statement": (
        "营业收入",
        "营业成本",
        "利润总额",
        "净利润",
    ),
    "cash_flow": (
        "经营活动产生的现金流量",
        "投资活动产生的现金流量",
        "筹资活动产生的现金流量",
        "现金及现金等价物",
    ),
}

# 报告期提示：从封面标题推断报告期末（A 股披露惯例）。
_REPORT_TYPE_SUFFIX = {
    "第一季度报告": "03-31",
    "半年度报告": "06-30",
    "第三季度报告": "09-30",
    "年度报告": "12-31",
}
_REPORT_TITLE_RE = re.compile(r"(20\d{2})\s*年.*?(第[一二三]季度报告|半年度报告|年度报告)")

_extractor: Extractor | None = None
_validator: Validator | None = None
_object_store: ObjectStore | None = None


class _CountingHub:
    """ModelHub 适配器：抽取场景注入 json_mode + 统计 generate 次数。

    职责一（AGENTS.md §8.1）：抽取场景开启 ``response_format=json_object``，
    在适配层注入而不改 ``Extractor``，避免破坏其既有调用契约；
    职责二：M2.09 契约的 ``retried`` 字段表示本次抽取是否经历 validator
    重试，``extract_with_retry`` 不暴露尝试轮次，通过计数推断
    （一次 generate = 首抽，>1 = 发生过重试）。
    """

    def __init__(self, hub: ModelHub) -> None:
        self._hub = hub
        self.settings = hub.settings
        self.calls = 0

    def generate(self, prompt: str, **kwargs: Any) -> Any:
        """Delegate to the wrapped hub, forcing json_mode and counting."""
        self.calls += 1
        kwargs.setdefault("json_mode", True)
        return self._hub.generate(prompt, **kwargs)


def configure_handler(
    *,
    extractor: Extractor | None = None,
    validator: Validator | None = None,
    object_store: ObjectStore | None = None,
) -> None:
    """Inject extractor/validator/object-store dependencies (used by unit tests).

    Args:
        extractor: Optional Extractor override.
        validator: Optional Validator override.
        object_store: Optional MinIO/object-store override.
    """
    global _extractor, _validator, _object_store
    _extractor = extractor
    _validator = validator
    _object_store = object_store


def reset_handler() -> None:
    """Clear injected dependencies so defaults are rebuilt lazily."""
    configure_handler(extractor=None, validator=None, object_store=None)


def _resolve_extractor() -> Extractor:
    """Return the configured extractor or build the production default."""
    if _extractor is not None:
        return _extractor
    # 包装 hub 以统计 generate 次数（retried 标记依赖它）。
    return Extractor(_CountingHub(get_modelhub()))


def _resolve_validator() -> Validator:
    """Return the configured validator or build the production default."""
    return _validator if _validator is not None else Validator()


def _resolve_object_store() -> ObjectStore:
    """Return the configured object store or build the production default."""
    return _object_store if _object_store is not None else MinioObjectClient(Settings())


def select_table(
    document: Document, statement_type: StatementType
) -> tuple[int, TableBlock] | None:
    """按特征科目命中率选出目标报表最匹配的表格。

    评分 = 特征关键词命中数 - 母公司降权（0.5）。A 股年报同时披露
    合并/母公司两套报表且科目名相同，勾稽与报告应基于合并口径；
    母公司表降权而非排除（仅有母公司表时仍可计算会计恒等式）。

    Args:
        document: M4.03 parse 产物反序列化出的 Document。
        statement_type: 目标报表类型。

    Returns:
        ``(page_index, table)``；无任何命中时返回 ``None``。
    """
    keywords = _TABLE_KEYWORDS[statement_type.value]
    best: tuple[float, int, TableBlock] | None = None
    for page in document.pages:
        for table in page.table_blocks:
            score = float(sum(1 for kw in keywords if kw in table.html))
            if "母公司" in table.html:
                score -= 0.5
            if score > 0 and (best is None or score > best[0]):
                best = (score, page.page_index, table)
    if best is None:
        return None
    LOGGER.info(
        "[select_table] type=%s page=%d score=%.1f",
        statement_type.value,
        best[1],
        best[0],
    )
    return best[1], best[2]


def extract_report_period(document: Document) -> str:
    """从封面标题推断报告期末（YYYY-MM-DD），推断失败返回空串。

    优先匹配「2024年年度报告」类标题；A 股年报报告期末固定为 12-31。
    这只是 prompt 提示，真正的 report_period 由模型从表格内容确认。

    Args:
        document: Parse 产物反序列化出的 Document。

    Returns:
        报告期末字符串（如 ``2024-12-31``）或空串。
    """
    for page in document.pages[:3]:
        for block in page.text_blocks:
            match = _REPORT_TITLE_RE.search(block.text)
            if match:
                year, title = match.groups()
                suffix = _REPORT_TYPE_SUFFIX[title]
                # 三季报期末是 9-30，修正字典里的占位值。
                if title == "第三季度报告":
                    suffix = "09-30"
                return f"{year}-{suffix}"
    return ""


def _estimate_confidence(validation: ValidationResult) -> float:
    """按校验问题数估算置信度（error 重罚、warning 轻罚，下限 0.5）。

    Args:
        validation: Validator 输出。

    Returns:
        [0.5, 1.0] 区间的置信度。
    """
    penalty = 0.05 * validation.error_count + 0.01 * validation.warning_count
    return max(0.5, round(1.0 - penalty, 2))


def _fetch_document(store: ObjectStore, task_id: str) -> Document:
    """从 MinIO artifacts 桶拉取并校验 parse 产物。

    Args:
        store: 对象存储客户端。
        task_id: 任务 ID（定位 parsed/{taskId}.json）。

    Returns:
        反序列化后的 Document。

    Raises:
        AiException: 产物缺失或 schema 不匹配。
    """
    settings = Settings()
    raw = store.fetch_bytes(parsed_object_key(task_id), bucket=settings.minio_artifact_bucket)
    try:
        return Document.model_validate_json(raw)
    except ValidationError as error:
        raise AiException(f"parse artifact schema mismatch taskId={task_id}: {error}") from error


async def handle(message: TaskMessage) -> dict[str, Any]:
    """拉取 parse 产物，筛选目标表格，走 DeepSeek 真实抽取。

    Args:
        message: Validated extract task message; ``message.step`` is one
            of ``extract.bs`` / ``extract.is`` / ``extract.cf``.

    Returns:
        Dict matching the M2.09 contract consumed by L2 ``StatementWriter``.

    Raises:
        ValueError: When the routing step is unknown.
        AiException: When the artifact is missing/unreadable or no
            matching table exists.
    """
    statement_value = _STEP_TO_TYPE.get(message.step)
    if statement_value is None:
        # 未知 step 显式报错走 DLQ，避免路由错误污染数据（M2 review fix 保留）。
        raise ValueError(
            f"Unknown extract step: {message.step!r}, "
            f"expected one of {sorted(_STEP_TO_TYPE.keys())}"
        )
    statement_type = StatementType(statement_value)

    document = _fetch_document(_resolve_object_store(), message.task_id)
    selected = select_table(document, statement_type)
    if selected is None:
        raise AiException(
            f"no {statement_value} table found in parse artifact taskId={message.task_id}"
        )
    page_index, table = selected

    extractor = _resolve_extractor()
    result, validation = extract_with_retry(
        extractor,
        _resolve_validator(),
        table.html,
        statement_type,
        report_period=extract_report_period(document),
    )
    # retried 推断：>1 次 generate 说明经历过 validator 重试。
    hub = getattr(extractor, "hub", None)
    generate_calls = getattr(hub, "calls", 1)
    retried = generate_calls > 1

    LOGGER.info(
        "[handle] extract 完成 taskId=%s step=%s success=%s retried=%s "
        "tokens=%d latency_ms=%.1f source_page=%d",
        message.task_id,
        message.step,
        result.success,
        retried,
        result.prompt_tokens + result.completion_tokens,
        result.latency_ms,
        page_index,
    )
    return _build_payload(result, validation, page_index, retried)


def _build_payload(
    result: ExtractionResult,
    validation: ValidationResult,
    page_index: int,
    retried: bool,
) -> dict[str, Any]:
    """组装 M2.09 契约 payload（L2 StatementWriter 消费）。

    Args:
        result: 抽取结果（含 statement 或 error）。
        validation: 校验结果。
        page_index: 选中表格所在页码。
        retried: 是否经历过 validator 重试。

    Returns:
        M2.09 契约 payload。
    """
    return {
        "success": result.success,
        "statement": (result.statement.model_dump(mode="json") if result.statement else {}),
        "validation": {
            "is_valid": validation.is_valid,
            "issues": [issue.model_dump() for issue in validation.issues],
            "error_hint": validation.error_hint,
        },
        "confidence": _estimate_confidence(validation),
        "source_page": page_index,
        "retried": retried,
        "tokens_used": result.prompt_tokens + result.completion_tokens,
        "latency_ms": round(result.latency_ms, 1),
        "error": result.error,
    }
