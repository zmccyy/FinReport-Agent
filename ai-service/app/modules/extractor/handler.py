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
from app.modules.extractor.normalize import normalize_item_name
from app.modules.extractor.validator import (
    ValidationResult,
    Validator,
    extract_with_retry,
)
from app.modules.modelhub.modelhub import ModelHub, get_modelhub
from app.modules.parser.handler import parsed_object_key
from app.schemas.document import Document, Page
from app.schemas.statement import ExtractionResult, StatementType
from app.schemas.task import TaskMessage
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# 目录页甄别（M6.08）：金额单元密度阈值——千分位数字（与
# document_parser._AMOUNT_CELL_RE 同口径）少于该值的标题命中页视为
# 目录/封面页，不作报表段起点。
_AMOUNT_CELL_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_TOC_DENSITY_MIN = 3

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
# 全部报表标题（H1 隐式边界）：当前报表的合并标题之外，段内出现
# 任一其它标题（正常文档序中即当前报表的母公司标题；缺失时为下一张
# 报表的合并/母公司标题）即视为段终止，防止把全文档尾并入合并段。
# M6.08 追加银行年报变体：银行年报的母公司段标题为「银行…」而非
# 「母公司…」（平安银行实测）——缺失时 BS 段会越过银行表吞到下一张
# 合并报表，把银行口径表格并入合并段。
_BANK_TITLES: tuple[str, ...] = ("银行资产负债表", "银行利润表", "银行现金流量表")
_ALL_TITLES: tuple[str, ...] = (
    *_MERGED_TITLES.values(), *_PARENT_TITLES.values(), *_BANK_TITLES,
)
# H1 兜底：无任何终止标题时按关键词密度收窄段尾的硬上限（起始页之后
# 允许的最大跨页数；三表实际跨度 2-4 页，7 页留余量并约束 prompt 体积）。
_MAX_SEGMENT_SPAN = 7
# 文本降级表格重建：取值正则（科目行中的金额）。严格千分位分组
# （M4.10 审查修复 M1）：「资产处置收益53553,106,625.19」类附注号
# 粘值中，前导组「53553」不是合法千分位分组，正则将从第 2 位起命中
# 真实金额 553,106,625.19，残留附注号由名称尾随剥号规则清除。无千分
# 位的小值行（如「500.00」）不再匹配——降级路径宁缺勿错。
_NUM_VALUE_RE = re.compile(r"(-?\d{1,3}(?:,\d{3})+\.\d{2})")
# 降级重建名称长度上限：超过视为多名称块熔合特征（如 64/65 页
# 「收到再保业务现金净额保户储金及投资款净增加额…」整块熔合），
# 无列几何不可拆分，跳过以避免错误配对（宁缺勿错）。
_FALLBACK_NAME_MAX_LEN = 30

# 科目名规范化已抽至 normalize.py（单一事实来源，M4.10 审查修复 L2）：
# 此前 handler / eval_m2_f1 / rebuild_moutai_gt 三份拷贝已分叉（GT 侧剥
# 尾随附注号而预测侧不剥），此处 re-export 保持 ``from ...handler import
# normalize_item_name`` 的既有测试导入路径兼容。

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


def _html_from_text_blocks(page: Page, min_y0: float = 0.0) -> str | None:
    """无表格页的文本降级：从行文本重建简单 HTML 表格。

    M4.10 F1 复测：64 页（合并现金流量表头部）被 PP-DocLayout 漏检，
    无 table_block 但文本层完整（“销售商品、提供劳务收到的现金
    183,990,403,487.80 …”整行）。仅对命中关键词的页触发，供 LLM
    抽取兜底；不含金额的行（无值科目）直接跳过。

    M4.10 审查修复 M1：

    - ``min_y0``：起始页传合并标题 y0，排除标题上方的上一报表尾块
      （如 CF 头部页上方的利润表尾「六、综合收益总额…」）；
    - 金额取严格千分位分组的首个匹配（附注号粘值防御）；
    - 科目名剥尾随附注号（与 normalize_item_name 对齐）；
    - 清理后名称超 ``_FALLBACK_NAME_MAX_LEN`` 的行视为多名称块熔合，
      跳过（无列几何不可拆分，错误配对危害大于缺行）。

    Args:
        page: 无 table_block 的候选页。
        min_y0: 参与重建的文本块最小 y0（起始页为合并标题位置）。

    Returns:
        重建的 ``<table>`` HTML；无可重建行时返回 ``None``。
    """
    rows: list[str] = []
    for block in page.text_blocks:
        if block.bbox.y0 < min_y0:
            continue
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
        name = re.sub(r"\d{1,3}$", "", text[: match.start()].strip())
        if not name or len(name) > _FALLBACK_NAME_MAX_LEN:
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
    - **隐式边界（H1 修复）**：母公司标题缺失（换行拆块/措辞变体/
      段缺失）时，段内出现的任一其它报表标题（如下一张报表的合并
      标题）作为隐式终止，防止起始页之后到文档末尾的全部表格
      （母公司表、附注表）以「合并」口径并入段内；
    - **密度收窄兜底（H1 修复）**：六类标题均缺失时，按关键词密度
      收窄段尾（最后一个关键词命中页，硬上限起始页后 7 页），
      并输出 warning 保证根因可见；
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

    # 段开始页：合并标题所在页。M6.08：跳过目录页——银行年报目录页的
    # 标题是干净独立文本块（平安实测），会被 _title_y0 命中并把段起点
    # 锚到目录页（目录段无表格 → select_table 返回 None）；以金额单元
    # 密度（千分位数字，与 document_parser._AMOUNT_CELL_RE 同口径）区分
    # 目录页与真实报表页，无密度命中页时回退首个命中（保持旧行为）。
    start_page: Page | None = None
    start_title_y0 = 0.0
    first_hit: tuple[Page, float] | None = None
    for page in document.pages:
        y0 = _title_y0(page, merged_title)
        if y0 is None:
            continue
        if first_hit is None:
            first_hit = (page, y0)
        amount_cells = sum(
            len(_AMOUNT_CELL_RE.findall(block.text)) for block in page.text_blocks
        )
        if amount_cells >= _TOC_DENSITY_MIN:
            start_page = page
            start_title_y0 = y0
            break
    if start_page is None and first_hit is not None:
        start_page, start_title_y0 = first_hit
    if start_page is None:
        LOGGER.warning("[select_table] %s 未找到合并标题段", merged_title)
        return None
    start_idx = start_page.page_index

    # 段结束页：段内首个「其它报表标题」所在页（H1 隐式边界）。正常
    # 文档序中首个命中的即当前报表的母公司标题；母公司标题缺失
    # （换行拆块/措辞变体/段缺失）时为下一张报表的标题，防止把
    # 起始页之后到文档末尾的全部表格并入合并段。
    end_page: Page | None = None
    end_title_y0 = float("inf")
    for page in document.pages:
        if page.page_index < start_idx:
            continue
        for end_title in _ALL_TITLES:
            if end_title == merged_title:
                continue
            y0 = _title_y0(page, end_title)
            if y0 is not None:
                end_page = page
                end_title_y0 = y0
                break
        if end_page is not None:
            break

    # H1 密度收窄兜底：六类标题均缺失时，按关键词密度收窄段尾——
    # 顺序扫描，间隔超过 1 页无命中即认为段已结束（后续命中属附注等
    # 其它段落，不得扩展段尾）；段尾容忍 1 页无命中的续页，并受
    # 起始页后 _MAX_SEGMENT_SPAN 硬上限约束，保证根因可见。
    effective_end = start_idx + _MAX_SEGMENT_SPAN
    if end_page is None:
        last_hit = start_idx
        for page in document.pages:
            if page.page_index < start_idx:
                continue
            if page.page_index > last_hit + 1:
                break
            if any(kw in _page_text(document, page) for kw in keywords):
                last_hit = page.page_index
        effective_end = min(last_hit + 1, start_idx + _MAX_SEGMENT_SPAN)
        LOGGER.warning(
            "[select_table] %s 未找到任何段终止标题（母公司及其它报表标题均缺），"
            "按关键词密度收窄段尾 start=%d end=%d（旧版会并入全文档尾）",
            merged_title,
            start_idx,
            effective_end,
        )

    merged_rows: list[tuple[int, float, str]] = []
    for page in document.pages:
        idx = page.page_index
        if idx < start_idx:
            continue
        if end_page is not None and idx > end_page.page_index:
            continue
        if end_page is None and idx > effective_end:
            continue
        is_start = idx == start_idx
        is_end = end_page is not None and idx == end_page.page_index
        if is_end:
            # 结束页：终止标题上方的表属于合并段尾部（如 59 页合并权益尾）；
            # 同页含合并标题时同时要求位于合并标题下方（M3 修复：旧 start
            # 分支遮蔽此 y 切分，母公司表曾以合并口径并入段内）。
            lo = start_title_y0 if is_start else float("-inf")
            tables = [t for t in page.table_blocks if lo <= t.bbox.y0 < end_title_y0]
            allow_text_fallback = False
        elif is_start:
            # 起始页：合并标题下方的表（标题通常位于页底，此页往往无表）。
            tables = [t for t in page.table_blocks if t.bbox.y0 >= start_title_y0]
            allow_text_fallback = not tables
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
            # 起始页传标题 y0：排除标题上方上一报表的尾块（M1）。
            rebuilt = _html_from_text_blocks(
                page, min_y0=start_title_y0 if is_start else 0.0
            )
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
