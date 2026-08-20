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

# 报表类型 → 页级合并/母公司报表标题（定位报表段区间）。
_MERGED_TITLES: dict[str, str] = {
    "balance_sheet": "合并资产负债表",
    "income_statement": "合并利润表",
    "cash_flow": "合并现金流量表",
}
_PARENT_TITLES: dict[str, str] = {
    "balance_sheet": "母公司资产负债表",
    "income_statement": "母公司利润表",
    "cash_flow": "母公司现金流量表",
}
# 文本降级表格重建：取值正则（科目行中的金额）。
_NUM_VALUE_RE = re.compile(r"(-?\d[\d,]*\.\d{2})")

# 科目名规范化（抽取结果后处理，M4.10 F1 复测：LLM 原样保留表格行号
# “一、营业收入”、行性质前缀“减：营业成本”、括号注释“（损失以…号填列）”
# 与识别空格“现 金”，与 benchmark 科目名对不齐）。
_NAME_PREFIX_RE = re.compile(r"^(?:[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+[、.]|(?:减|加|其中)：)")
_NAME_PAREN_RE = re.compile(r"（[^）]*）")


def normalize_item_name(raw: str) -> str:
    """去行号/行性质前缀/括号注释/全角符号/空格，得到规范科目名。

    与 ``scripts/rebuild_moutai_gt.py`` 的 GT 名称规范化保持同一套规则，
    保证 F1 匹配口径一致（GT 侧已规范，预测侧在落库前规范化）。
    """
    name = raw.strip()
    # 括号注释碎片截断（“损失以“－”号填列” 跨行拆分时只剩半截括号）
    mark = name.find("号填")
    if mark != -1:
        paren = name.rfind("（", 0, mark)
        name = name[:paren] if paren != -1 else name[:mark]
    name = _NAME_PREFIX_RE.sub("", name)
    name = _NAME_PAREN_RE.sub("", name)
    name = name.replace("（", "").replace("）", "")
    name = name.replace("－", "-").replace("—", "-").replace("“", "").replace("”", "")
    return re.sub(r"\s+", "", name).strip()

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


def _page_text(document: Document, page: Page) -> str:
    """页文本 = 文本块 + 该页全部表格 HTML（页级评分/母公司判定用）。

    Args:
        document: Parse 产物（未使用，仅保持签名对称）。
        page: 目标页。

    Returns:
        拼接后的页文本。
    """
    parts = [block.text for block in page.text_blocks]
    parts.extend(table.html for table in page.table_blocks)
    return "\n".join(parts)


def _html_from_text_blocks(page: Page) -> str | None:
    """无表格页的文本降级：从行文本重建简单 HTML 表格。

    M4.10 F1 复测：64 页（合并现金流量表头部）被 PP-DocLayout 漏检，
    无 table_block 但文本层完整（“销售商品、提供劳务收到的现金
    183,990,403,487.80 …”整行）。仅对命中关键词的页触发，供 LLM
    抽取兜底；不含金额的行（无值科目）直接跳过。

    Args:
        page: 无 table_block 的候选页。

    Returns:
        重建的 ``<table>`` HTML；无可重建行时返回 ``None``。
    """
    rows: list[str] = []
    for block in page.text_blocks:
        text = block.text.strip()
        if not text:
            continue
        if re.search(r"\d{1,3} / \d{1,3}", text) or "年度报告" in text:
            continue
        if any(marker in text for marker in ("公司负责人", "单位：元", "币种", "每股收益")):
            continue
        match = _NUM_VALUE_RE.search(text)
        if not match:
            continue
        name = text[: match.start()].strip()
        if not name:
            continue
        value = match.group(1).replace(",", "")
        rows.append(f"<tr><td>{name}</td><td>{value}</td></tr>")
    if not rows:
        return None
    return "<table>" + "".join(rows) + "</table>"


def _title_y0(page: Page, title: str) -> float | None:
    """返回页文本块中 ``title`` 首次出现的 y 坐标；未出现返回 ``None``。

    Args:
        page: 目标页。
        title: 报表标题（如「合并利润表」）。

    Returns:
        标题文本块顶部 y 坐标；页内无标题时 ``None``。
    """
    for block in page.text_blocks:
        if title in block.text:
            return block.bbox.y0
    return None


def select_table(
    document: Document, statement_type: StatementType
) -> tuple[list[int], str, str] | None:
    """按报表标题段定位目标报表的表格集合（跨页合并）。

    M4.10 修复（F1 复测发现）：合并利润表/现金流量表/资产负债表跨
    2-4 页且页内混排母公司表，旧“单表选择”把母公司整页表误选。

    - **段定位**：页文本含「合并XXX表」标题的页 → 段开始；含
      「母公司XXX表」标题的页 → 段结束（排他）。段内命中关键词的
      表格全部并入，按页序拼接 HTML；标题页只取标题上/下方的表
      （59 页既有合并权益尾又有母公司表头，靠 bbox.y0 与标题位置
      切分）；
    - **文本降级**：段内命中关键词但无表格的页（PP-DocLayout 漏检，
      如 64 页合并现金流量表头部），用行文本重建 HTML 兜底；
    - scope 恒为「合并」（段由合并标题锚定；A 股年报合并报表为
      法定披露，缺失合并标题时返回 None 走 DLQ 留排查现场）。

    Args:
        document: M4.03 parse 产物反序列化出的 Document。
        statement_type: 目标报表类型。

    Returns:
        ``(pages, merged_html, scope)``；无命中时返回 ``None``。
    """
    keywords = _TABLE_KEYWORDS[statement_type.value]
    merged_title = _MERGED_TITLES[statement_type.value]
    parent_title = _PARENT_TITLES[statement_type.value]

    # 段开始页：合并标题所在页。
    start_page: Page | None = None
    start_title_y0 = 0.0
    for page in document.pages:
        y0 = _title_y0(page, merged_title)
        if y0 is not None:
            start_page = page
            start_title_y0 = y0
            break
    if start_page is None:
        LOGGER.warning("[select_table] %s 未找到合并标题段", merged_title)
        return None
    start_idx = start_page.page_index

    # 段结束页：母公司标题所在页（可缺——报表可能在文档尾部被切）。
    end_page: Page | None = None
    end_title_y0 = float("inf")
    for page in document.pages:
        if page.page_index < start_idx:
            continue
        y0 = _title_y0(page, parent_title)
        if y0 is not None:
            end_page = page
            end_title_y0 = y0
            break

    merged_rows: list[tuple[int, float, str]] = []
    for page in document.pages:
        if page.page_index < start_idx:
            continue
        if page.page_index == start_idx:
            # 起始页：合并标题下方的表（标题通常位于页底，此页往往无表）。
            tables = [t for t in page.table_blocks if t.bbox.y0 >= start_title_y0]
            allow_text_fallback = not tables
        elif end_page is not None and page.page_index == end_page.page_index:
            # 结束页：母公司标题上方的表属于合并段尾部（如 59 页合并权益尾）。
            tables = [t for t in page.table_blocks if t.bbox.y0 < end_title_y0]
            allow_text_fallback = False
        elif end_page is not None and page.page_index > end_page.page_index:
            continue
        else:
            tables = list(page.table_blocks)
            allow_text_fallback = not tables

        # 段内页整页并入（y 切分已排除混排的其它报表表）——表级不再按
        # 关键词过滤：“（或股东权益）合计”等跨行单元格在 PP 的 HTML 里
        # 不含完整关键词“所有者权益合计”，表级过滤会误丢合并权益尾。
        if tables:
            merged_rows.extend((page.page_index, t.bbox.y0, t.html) for t in tables)
        elif allow_text_fallback and any(
            kw in _page_text(document, page) for kw in keywords
        ):
            rebuilt = _html_from_text_blocks(page)
            if rebuilt:
                merged_rows.append((page.page_index, 0.0, rebuilt))
    merged_rows.sort(key=lambda item: (item[0], item[1]))
    if not merged_rows:
        return None
    merged_html = "\n".join(row[2] for row in merged_rows)
    pages = sorted({row[0] for row in merged_rows})

    LOGGER.info(
        "[select_table] type=%s pages=%s scope=合并 rows=%d",
        statement_type.value,
        pages,
        len(merged_rows),
    )
    return pages, merged_html, "合并"


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
    pages, merged_html, scope = selected

    extractor = _resolve_extractor()
    result, validation = extract_with_retry(
        extractor,
        _resolve_validator(),
        merged_html,
        statement_type,
        report_period=extract_report_period(document),
        scope=scope,
    )
    # retried 推断：>1 次 generate 说明经历过 validator 重试。
    hub = getattr(extractor, "hub", None)
    generate_calls = getattr(hub, "calls", 1)
    retried = generate_calls > 1

    # 科目名规范化：与 benchmark GT 同一套规则（行号/前缀/括号注释/空格）。
    if result.statement is not None:
        for items in result.statement.statements.values():
            for item in items:
                normalized = normalize_item_name(item.item)
                if normalized:
                    item.item = normalized

    LOGGER.info(
        "[handle] extract 完成 taskId=%s step=%s success=%s retried=%s "
        "tokens=%d latency_ms=%.1f source_pages=%s scope=%s",
        message.task_id,
        message.step,
        result.success,
        retried,
        result.prompt_tokens + result.completion_tokens,
        result.latency_ms,
        pages,
        scope,
    )
    return _build_payload(result, validation, pages[0], retried)


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
