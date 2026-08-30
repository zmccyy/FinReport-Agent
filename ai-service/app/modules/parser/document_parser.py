"""M2.01 DocumentParser: PyMuPDF → pages → PP-Structure layout blocks.

The parser pairs a lightweight PyMuPDF text/image extractor with an injectable
layout analyzer. PP-StructureV2 (M2.02) and OCR (M2.03) are supplied as
providers so unit tests can exercise the parser against pure-Python fakes
without pulling in the heavyweight Paddle stack.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.config import Settings
from app.core.exceptions import AiException
from app.schemas.document import (
    BoundingBox,
    Document,
    Page,
    TableBlock,
    TextBlock,
)
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

DEFAULT_RENDER_DPI = 200

# A 股年报报表页锚点：合并/母公司 三表标题（M4.10）。
_STATEMENT_ANCHOR_RE = re.compile(r"(合并|母公司)(资产负债表|利润表|现金流量表)")
# 金额格式（千分位，小数可选），用于报表页密度门控。M6.08：原
# `\.\d{2}` 强制两位小数——茅台（元，两位小数）能过门控，但平安
# （百万元整数）与宁德（千元整数）的报表页密度恒为 0，报表页被
# 静默跳过，抽取因「no X table found」重试耗尽。去小数强制，
# 保留千分位特征。
_AMOUNT_CELL_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")


class StatementPageFilter:
    """A 股年报「报表页」判定：标题锚点 + 向后窗口 + 金额密度门控。

    背景（M4.10 真实 E2E 实测）：PP-StructureV3 逐页跑 143 页年报不可行
    （>18min）；纯关键词过滤仍命中 23 页且包含附注密集表格页——RT-DETR-L
    单元格检测在附注多表页上内存膨胀，直接把 7.6GB 的 Docker VM 打到
    OOM（容器 28 次重启循环）。

    判定规则（有状态，按页序单遍执行）：

    1. 页文本命中锚点正则（如「合并资产负债表」）→ 记为锚点页；
    2. 锚点页及其后 ``window`` 页内、金额格式单元格数 ≥ ``amount_threshold``
       的页判为报表页（报表续页无标题但金额密集；附注页因距锚点超过
       窗口或密度不足被排除）。

    茅台 2025 年报实测：锚点 55/58/60/62/63/65 → 候选 55-68 共 14 页，
    恰好覆盖 合并/母公司 三表及其续页。
    """

    def __init__(
        self,
        window: int = 4,
        amount_threshold: int = 15,
        anchor_pattern: str | None = None,
    ) -> None:
        """Configure the filter.

        Args:
            window: 锚点页向后覆盖的页数（报表含续页，默认 4）。
            amount_threshold: 金额单元格数下限（低于视为非报表页）。
            anchor_pattern: 自定义锚点正则（默认 A 股年报三表标题）。
        """
        self.window = window
        self.amount_threshold = amount_threshold
        self._anchor_re = (
            re.compile(anchor_pattern) if anchor_pattern else _STATEMENT_ANCHOR_RE
        )
        self._last_anchor = -10_000

    def reset(self) -> None:
        """Reset per-document state (M4.10 审查修复 H4).

        ``DocumentParser`` 是模块级单例（parser/handler.py），``_last_anchor``
        若跨文档保留，前一份长文档的锚点会把后一份文档的早前页误判为
        候选页（重新引入本过滤器要防的 PP-Structure OOM 路径）。
        每次解析新文档前由 ``parse_bytes`` 调用。
        """
        self._last_anchor = -10_000

    def anchor_hit(self, page_text: str) -> bool:
        """Check whether a page's text layer matches the anchor regex.

        Used by ``DocumentParser.parse_bytes`` 的文档级预扫：全文档无任何
        锚点命中（扫描件/标题变体）时退回全页识别旧行为（H4：否则
        ``_last_anchor=-10000`` 使窗口检查永假，零表格识别且无日志）。
        """
        return bool(self._anchor_re.search(page_text))

    def is_candidate(self, page_index: int, page_text: str) -> bool:
        """Decide whether table recognition should run on this page.

        Args:
            page_index: 0-based page index (for the anchor window).
            page_text: The page's raw text layer.

        Returns:
            True when the page is inside an anchor window and passes the
            amount-density gate.
        """
        if self._anchor_re.search(page_text):
            self._last_anchor = page_index
        if not (0 <= page_index - self._last_anchor <= self.window):
            return False
        return len(_AMOUNT_CELL_RE.findall(page_text)) >= self.amount_threshold


class LayoutAnalyzer(Protocol):
    """Identify table regions within a rendered page image."""

    def analyze_page(self, page_index: int, image_bytes: bytes) -> list[TableBlock]:
        """Return table blocks detected on the rendered page.

        Args:
            page_index: 0-based page index.
            image_bytes: PNG bytes of the page rendered at DEFAULT_RENDER_DPI.

        Returns:
            A possibly-empty list of TableBlock instances.
        """
        ...


class OcrProvider(Protocol):
    """OCR fallback for scanned pages (M2.03)."""

    def recognize(self, page_index: int, image_bytes: bytes) -> list[TextBlock]:
        """Return text blocks recognized from the rendered page image.

        Args:
            page_index: 0-based page index.
            image_bytes: PNG bytes of the page rendered at DEFAULT_RENDER_DPI.

        Returns:
            A possibly-empty list of TextBlock instances.
        """
        ...


@dataclass(frozen=True)
class _ParsedPage:
    """Internal carrier for PyMuPDF page geometry and raw text."""

    index: int
    width: float
    height: float
    blocks: list[dict[str, Any]]
    text: str
    image_bytes: bytes | None


class DocumentParser:
    """Extract a structured Document from a PDF byte stream.

    The parser stays usable when the optional Paddle providers are absent:
    text extraction still produces TextBlocks, and tables/scanned pages are
    simply skipped with a warning.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        layout_analyzer: LayoutAnalyzer | None = None,
        ocr_provider: OcrProvider | None = None,
        render_dpi: int = DEFAULT_RENDER_DPI,
        table_page_filter: StatementPageFilter | None = None,
    ) -> None:
        """Configure the parser with optional layout/OCR providers.

        Args:
            settings: Application settings (currently informational).
            layout_analyzer: PP-Structure-backed table region detector.
            ocr_provider: PaddleOCR-based scanned-page recognizer.
            render_dpi: DPI used when rasterizing pages for layout/OCR.
            table_page_filter: 报表页过滤器——非 None 时仅对判为报表页的
                页调用 layout_analyzer（PP-Structure 全页跑 143 页年报
                >18min 且附注密集页 OOM，见 StatementPageFilter 文档；
                None = 不过滤，旧行为，测试用）。
        """
        self.settings = settings or Settings()
        self.layout_analyzer = layout_analyzer
        self.ocr_provider = ocr_provider
        self.render_dpi = render_dpi
        self.table_page_filter = table_page_filter

    def parse_bytes(self, pdf_bytes: bytes, source: str) -> Document:
        """Parse a PDF byte stream into a Document.

        Args:
            pdf_bytes: Raw PDF content.
            source: Object key or path used for tracing and provenance.

        Returns:
            A Document with one Page per PDF page.

        Raises:
            AiException: If the PDF is encrypted, empty, or PyMuPDF fails.
        """
        fitz = self._import_fitz()
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as error:
            raise AiException(f"Failed to open PDF: {error}") from error

        if doc.needs_pass:
            doc.close()
            raise AiException("PDF is encrypted; provide an unencrypted file")

        # M4.10 审查修复 H4：过滤器按文档重置（单例 parser 的
        # ``_last_anchor`` 不得跨文档保留）；文档级预扫无任何锚点命中
        # （扫描件/标题变体）时退回全页识别旧行为——否则窗口检查永假，
        # 全文档零表格识别且无日志可查。取舍：无文本层的大型扫描件
        # 退回全页识别后 OOM 风险回归（旧行为），相对「零识别 + 根因
        # 不可见」是更优失败模式；生产 enable_ocr=False 不变。
        use_filter = self.table_page_filter is not None
        if use_filter:
            self.table_page_filter.reset()
            use_filter = any(
                self.table_page_filter.anchor_hit(
                    doc.load_page(i).get_text("text") or ""
                )
                for i in range(doc.page_count)
            )
            if not use_filter:
                LOGGER.warning(
                    "No statement anchor found in document source=%s pages=%d; "
                    "falling back to full-page table recognition (legacy behavior)",
                    source,
                    doc.page_count,
                )

        pages: list[Page] = []
        try:
            for index in range(doc.page_count):
                raw = self._extract_page(fitz, doc, index)
                pages.append(self._build_page(fitz, doc, raw, use_filter))
        finally:
            doc.close()

        if not pages:
            raise AiException("PDF has no pages")

        LOGGER.info(
            "Parsed document source=%s pages=%d tables=%d scanned=%s",
            source,
            len(pages),
            sum(len(p.table_blocks) for p in pages),
            any(p.is_scanned for p in pages),
        )
        return Document(
            source=source,
            page_count=len(pages),
            pages=pages,
            parser_version="m6-v1",
            metadata={"render_dpi": self.render_dpi},
        )

    def _extract_page(self, fitz: Any, doc: Any, index: int) -> _ParsedPage:
        """Pull raw blocks and plain text for one page (image lazy-rendered).

        M6.08 性能债务 R4：此前每页都先渲染 200 DPI PNG（143-288 页年报
        实测 25s+），而只有报表候选页和扫描页需要图像。文本层预扫先行，
        ``_build_page`` 按需渲染，非候选页的渲染开销整体消除。

        Args:
            fitz: The PyMuPDF module.
            doc: Open fitz document.
            index: 0-based page index.

        Returns:
           A _ParsedPage carrier with ``image_bytes=None``.
        """
        page = doc.load_page(index)
        raw_blocks = page.get_text("dict")["blocks"]
        text = page.get_text("text")
        return _ParsedPage(
            index=index,
            width=float(page.rect.width),
            height=float(page.rect.height),
            blocks=raw_blocks if isinstance(raw_blocks, list) else [],
            text=text or "",
            image_bytes=None,
        )

    def _render_page_index(self, fitz: Any, doc: Any, index: int) -> bytes | None:
        """Render one page to PNG bytes at the configured DPI (lazy path).

        Args:
            fitz: The PyMuPDF module.
            doc: Open fitz document.
            index: 0-based page index.

        Returns:
            PNG bytes, or None if rendering fails (e.g. 页面为纯图且无文本层时).
        """
        try:
            return self._render_page(fitz, doc.load_page(index))
        except Exception:
            LOGGER.warning("Failed to load page for rendering index=%s", index)
            return None

    def _render_page(self, fitz: Any, page: Any) -> bytes | None:
        """Render the page to PNG bytes at the configured DPI.

        Args:
            fitz: The PyMuPDF module.
            page: A fitz page object.

        Returns:
            PNG bytes, or None if rendering fails (e.g. 页面为纯图且无文本层时).
        """
        matrix = fitz.Matrix(self.render_dpi / 72, self.render_dpi / 72)
        try:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            return pix.tobytes("png")
        except Exception:
            LOGGER.warning("Failed to render page image index=%s", page.number)
            return None

    def _build_page(
        self, fitz: Any, doc: Any, raw: _ParsedPage, use_filter: bool = True
    ) -> Page:
        """Assemble a Page from text blocks plus optional layout/OCR results.

        图像按需渲染（M6.08 性能债务 R4）：仅当页被判为报表候选（需布局
        识别）或为扫描页（需 OCR）时才渲染 200 DPI PNG；其余页零渲染开销。

        Args:
            fitz: The PyMuPDF module (lazy rendering).
            doc: Open fitz document (lazy rendering).
            raw: The carrier returned by _extract_page.
            use_filter: Whether the statement-page filter applies to this
                document (False = legacy full-page behavior, see
                ``parse_bytes`` 的文档级预扫).

        Returns:
            A fully populated Page.
        """
        text_blocks = [
            block
            for block in (self._to_text_block(b) for b in raw.blocks)
            if block is not None
        ]

        is_scanned = self._is_scanned(raw, text_blocks)
        ocr_applied = False

        is_table_candidate = self._is_table_candidate(raw.index, raw.text, use_filter)
        needs_layout = self.layout_analyzer is not None and is_table_candidate
        needs_ocr = is_scanned and self.ocr_provider is not None

        image_bytes = raw.image_bytes
        if (needs_layout or needs_ocr) and image_bytes is None:
            image_bytes = self._render_page_index(fitz, doc, raw.index)

        table_blocks: list[TableBlock] = []
        if needs_layout and image_bytes is not None:
            try:
                table_blocks = self.layout_analyzer.analyze_page(raw.index, image_bytes)
            except Exception:
                LOGGER.exception("Layout analyzer failed page=%d", raw.index)
                table_blocks = []

        if needs_ocr and image_bytes is not None:
            try:
                text_blocks = self.ocr_provider.recognize(raw.index, image_bytes)
                ocr_applied = True
            except Exception:
                LOGGER.exception("OCR provider failed page=%d", raw.index)

        return Page(
            page_index=raw.index,
            width=raw.width,
            height=raw.height,
            text_blocks=text_blocks,
            table_blocks=table_blocks,
            figure_blocks=[],
            is_scanned=is_scanned,
            ocr_applied=ocr_applied,
        )

    def _to_text_block(self, raw: dict[str, Any]) -> TextBlock | None:
        """Convert a PyMuPDF dict block into a TextBlock.

        Args:
            raw: One entry from fitz Page.get_text("dict")["blocks"].

        Returns:
            A TextBlock, or None for image-only / empty blocks.
        """
        if raw.get("type", 0) != 0:  # 0 = text block, 1 = image block
            return None
        bbox = raw.get("bbox")
        lines = raw.get("lines", [])
        if not bbox or not lines:
            return None
        text = "".join(
            span.get("text", "") for line in lines for span in line.get("spans", [])
        )
        text = text.strip()
        if not text:
            return None
        x0, y0, x1, y1 = bbox
        bbox_model = BoundingBox(x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1))
        if bbox_model.width <= 0 or bbox_model.height <= 0:
            return None
        return TextBlock(bbox=bbox_model, text=text)

    @staticmethod
    def _is_scanned(raw: _ParsedPage, text_blocks: list[TextBlock]) -> bool:
        """Heuristic: a page is scanned only when no PyMuPDF text layer exists.

        A non-empty text layer — even a short heading — proves the PDF embeds
        a digital text layer and is therefore not a scan. Pages with zero
        extractable blocks but a stray raw-text fragment fall back to the
        raw text length; only truly empty pages are treated as scans.

        Args:
            raw: Extracted page carrier.
            text_blocks: Blocks already converted from the text layer.

        Returns:
            True if the page likely needs OCR.
        """
        if text_blocks:
            return False
        return not raw.text.strip()

    def _is_table_candidate(
        self, page_index: int, page_text: str, use_filter: bool = True
    ) -> bool:
        """Check whether table recognition should run on this page.

        Args:
            page_index: 0-based page index (anchor window bookkeeping).
            page_text: The page's raw text layer.
            use_filter: Whether the filter applies to this document
                (False = legacy full-page behavior for anchor-less docs).

        Returns:
            True when no filter is configured/applies (legacy behavior) or
            the page passes the statement-page filter.
        """
        if not use_filter or self.table_page_filter is None:
            return True
        return self.table_page_filter.is_candidate(page_index, page_text)

    @staticmethod
    def _import_fitz() -> Any:
        """Import PyMuPDF lazily so the service imports without it installed.

        Returns:
            The fitz module.

        Raises:
            AiException: If PyMuPDF is not available.
        """
        try:
            import fitz  # type: ignore[import-untyped]
        except ImportError as error:
            raise AiException(
                "PyMuPDF (pymupdf) is required for DocumentParser"
            ) from error
        return fitz


def parse_pdf_file(
    parser: DocumentParser, path: str, source: str | None = None
) -> Document:
    """Convenience wrapper to parse a PDF file off disk.

    Args:
        parser: A configured DocumentParser.
        path: Local filesystem path to the PDF.
        source: Optional override for the Document.source provenance field.

    Returns:
        A parsed Document.
    """
    with open(path, "rb") as fh:
        return parser.parse_bytes(fh.read(), source or path)


def pixmap_png_to_bytes(png_bytes: bytes) -> io.BytesIO:
    """Wrap PNG bytes into a seekable buffer (helper for providers).

    Args:
        png_bytes: Raw PNG bytes from PyMuPDF.

    Returns:
        A BytesIO view of the same bytes.
    """
    return io.BytesIO(png_bytes)
