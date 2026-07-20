"""M2.01 DocumentParser: PyMuPDF → pages → PP-Structure layout blocks.

The parser pairs a lightweight PyMuPDF text/image extractor with an injectable
layout analyzer. PP-StructureV2 (M2.02) and OCR (M2.03) are supplied as
providers so unit tests can exercise the parser against pure-Python fakes
without pulling in the heavyweight Paddle stack.
"""

from __future__ import annotations

import io
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
    ) -> None:
        """Configure the parser with optional layout/OCR providers.

        Args:
            settings: Application settings (currently informational).
            layout_analyzer: PP-Structure-backed table region detector.
            ocr_provider: PaddleOCR-based scanned-page recognizer.
            render_dpi: DPI used when rasterizing pages for layout/OCR.
        """
        self.settings = settings or Settings()
        self.layout_analyzer = layout_analyzer
        self.ocr_provider = ocr_provider
        self.render_dpi = render_dpi

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

        pages: list[Page] = []
        try:
            for index in range(doc.page_count):
                raw = self._extract_page(fitz, doc, index)
                pages.append(self._build_page(raw))
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
        """Pull raw blocks, plain text, and a rendered image for one page.

        Args:
            fitz: The PyMuPDF module.
            doc: Open fitz document.
            index: 0-based page index.

        Returns:
           A _ParsedPage carrier.
        """
        page = doc.load_page(index)
        raw_blocks = page.get_text("dict")["blocks"]
        text = page.get_text("text")
        image_bytes = self._render_page(fitz, page)
        return _ParsedPage(
            index=index,
            width=float(page.rect.width),
            height=float(page.rect.height),
            blocks=raw_blocks if isinstance(raw_blocks, list) else [],
            text=text or "",
            image_bytes=image_bytes,
        )

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

    def _build_page(self, raw: _ParsedPage) -> Page:
        """Assemble a Page from text blocks plus optional layout/OCR results.

        Args:
            raw: The carrier returned by _extract_page.

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

        table_blocks: list[TableBlock] = []
        if self.layout_analyzer is not None and raw.image_bytes is not None:
            try:
                table_blocks = self.layout_analyzer.analyze_page(
                    raw.index, raw.image_bytes
                )
            except Exception:
                LOGGER.exception("Layout analyzer failed page=%d", raw.index)
                table_blocks = []

        if is_scanned and self.ocr_provider is not None and raw.image_bytes is not None:
            try:
                text_blocks = self.ocr_provider.recognize(raw.index, raw.image_bytes)
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
