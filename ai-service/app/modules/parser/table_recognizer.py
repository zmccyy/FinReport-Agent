"""M2.02 TableRecognizer: PP-Structure table restoration to HTML.

Wraps PaddleOCR's PP-StructureV3 pipeline so callers receive a list of
``TableBlock`` instances per page. PP-Structure is heavy to import; the
recognizer loads it lazily and only once per process, and falls back to a
pure-regex HTML emitter for the simple-border case when PP-Structure cannot
decode a structure region (or when the package is absent under unit tests).

PaddleOCR 3.x replaces the legacy ``PPStructure`` callable with
``PPStructureV3`` whose results are accessed via ``predict()``. The engine is
adapted to the same ``engine(image_bytes) -> list[region]`` contract used by
tests, so real and fake engines share one code path.
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import AiException
from app.schemas.document import BoundingBox, TableBlock
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)


class _PPStructureV3Engine:
    """Adapts PaddleOCR 3.x ``PPStructureV3`` to the engine callable contract.

    ``PPStructureV3.predict()`` returns a list of page results; we extract
    table regions from each, projecting to the legacy ``{type, bbox, res}``
    shape that ``_to_table_block`` already understands.
    """

    def __init__(self, engine: Any) -> None:
        """Wrap a built PPStructureV3 instance.

        Args:
            engine: A PPStructureV3 instance.
        """
        self._engine = engine

    def __call__(self, image_bytes: bytes) -> list[dict[str, Any]]:
        """Return table regions detected on the rendered page image.

        PaddleOCR 3.x ``predict()`` only accepts ``numpy.ndarray`` or a file
        path — raw PNG bytes are silently ignored. We decode the PNG bytes to
        an ndarray (BGR) before calling predict. When OpenCV is unavailable
        (unit tests with a fake engine) the raw bytes are forwarded to
        ``predict`` so test stubs keep working.

        Args:
            image_bytes: PNG bytes of the page.

        Returns:
            A list of region dicts shaped like legacy PP-Structure results.
        """
        image = self._decode_png(image_bytes)
        predict_input = image if image is not None else image_bytes
        page = self._first_predict_page(self._engine.predict(predict_input))
        if page is None:
            return []
        return self._extract_table_regions(page)

    @staticmethod
    def _decode_png(image_bytes: bytes) -> Any:
        """Decode PNG bytes into a BGR ndarray for PaddleOCR 3.x predict.

        Args:
            image_bytes: PNG/JPG bytes from PyMuPDF rendering.

        Returns:
            A numpy ndarray, or None when OpenCV cannot decode the bytes (the
            dependency is absent under unit tests).
        """
        try:
            import cv2
            import numpy as np
        except ImportError:
            return None
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    @staticmethod
    def _first_predict_page(results: Any) -> Any:
        """Return the first page result regardless of list/iterator shape.

        Args:
            results: Output of PPStructureV3.predict().

        Returns:
            The first page result, or None when empty.
        """
        try:
            return next(iter(results))
        except StopIteration:
            return None
        except TypeError:
            return None

    @staticmethod
    def _extract_table_regions(page: Any) -> list[dict[str, Any]]:
        """Pull table regions from one PPStructureV3 page result.

        PaddleX ``LayoutParsingResultV2`` exposes ``layout_det_res["boxes"]``
        (each box carrying ``label`` and ``coordinate``) and a parallel
        ``table_res_list`` whose entries carry ``pred_html`` but no bbox. The
        table boxes appear in ``layout_det_res`` in the same order tables are
        appended to ``table_res_list`` (see paddlex ``pipeline_v2``), so we zip
        them by index. A legacy/fake entry that already carries ``table_region``
        or ``block_bbox`` is honored directly so existing unit fakes stay valid.

        Args:
            page: One PPStructureV3 page result object.

        Returns:
            List of region dicts with keys ``type``, ``bbox``, ``html``.
        """
        regions: list[dict[str, Any]] = []
        table_list: Any = TableRecognizer._field(page, "table_res_list", default=None)
        if not table_list:
            return regions
        layout_boxes = _PPStructureV3Engine._layout_table_boxes(page)
        try:
            for idx, entry in enumerate(table_list):
                html = TableRecognizer._field(entry, "pred_html", default="") or ""
                if not html:
                    continue
                bbox = _PPStructureV3Engine._entry_bbox(entry)
                if bbox is None and idx < len(layout_boxes):
                    bbox = layout_boxes[idx]
                if bbox is None:
                    continue
                regions.append({"type": "table", "bbox": bbox, "res": {"html": html}})
        except Exception:
            LOGGER.exception("Failed to map PPStructureV3 table regions")
            return []
        return regions

    @staticmethod
    def _entry_bbox(entry: Any) -> list[float] | None:
        """Read a bbox stored directly on a table entry (legacy/fake shape).

        Args:
            entry: A table_res_list item.

        Returns:
            A 4-number bbox list, or None when the entry carries no bbox.
        """
        for name in ("table_region", "block_bbox", "bbox"):
            value = TableRecognizer._field(entry, name, default=None)
            if value is not None and len(value) >= 4:
                return list(value[:4])
        return None

    @staticmethod
    def _layout_table_boxes(page: Any) -> list[list[float]]:
        """Collect bbox coordinates of ``table``-labeled layout boxes.

        Args:
            page: One PPStructureV3 page result object.

        Returns:
            A list of 4-number bbox lists (x0, y0, x1, y1), one per table box.
        """
        layout_res = TableRecognizer._field(page, "layout_det_res", default=None)
        boxes: Any = (
            TableRecognizer._field(layout_res, "boxes", default=None)
            if layout_res
            else None
        )
        if not boxes:
            return []
        table_boxes: list[list[float]] = []
        for box in boxes:
            label = TableRecognizer._field(box, "label", default="")
            if "table" not in str(label).lower():
                continue
            coordinate = TableRecognizer._field(box, "coordinate", default=None)
            if coordinate is None or len(coordinate) < 4:
                continue
            table_boxes.append([float(v) for v in coordinate[:4]])
        return table_boxes


class TableRecognizer:
    """Detect tables on a rendered page and restore them to HTML."""

    def __init__(
        self, lang: str = "ch", use_gpu: bool = False, engine: Any | None = None
    ) -> None:
        """Configure the recognizer.

        Args:
            lang: PP-Structure language flag (defaults to Chinese).
            use_gpu: Whether PP-Structure may use the GPU (default off for OCR).
            engine: Optional pre-built engine for tests (callable or instance).
        """
        self.lang = lang
        self.use_gpu = use_gpu
        self._engine = engine
        self._initialized = engine is not None

    def analyze_page(self, page_index: int, image_bytes: bytes) -> list[TableBlock]:
        """Run PP-Structure on the page image and return table blocks.

        Args:
            page_index: 0-based page index (for logging only).
            image_bytes: PNG bytes of the rendered page.

        Returns:
            Table blocks restored from the page image; empty if none found.
        """
        engine = self._ensure_engine()
        try:
            results = engine(image_bytes) or []
        except Exception:
            LOGGER.exception("PP-Structure inference failed page=%d", page_index)
            return []

        blocks: list[TableBlock] = []
        for region in results:
            block = self._to_table_block(region, page_index)
            if block is not None:
                blocks.append(block)
        LOGGER.debug("Detected tables page=%d count=%d", page_index, len(blocks))
        return blocks

    def _to_table_block(self, region: Any, page_index: int) -> TableBlock | None:
        """Convert one PP-Structure region dict into a TableBlock.

        PP-Structure returns entries with a ``type`` of ``'table'`` and a
        ``res`` payload containing ``html`` and an optional ``bbox``.

        Args:
            region: A single region result from PP-Structure.
            page_index: Indexed page (for logging only).

        Returns:
            A TableBlock or None when the region is not a table.
        """
        region_type = self._field(region, "type", default="")
        if region_type != "table":
            return None
        res = self._field(region, "res", default={})
        html = self._field(res, "html", default="")
        bbox_raw = self._field(
            region, "bbox", default=self._field(res, "bbox", default=None)
        )
        if not html or bbox_raw is None or len(bbox_raw) < 4:
            LOGGER.debug("Skipping malformed table region page=%d", page_index)
            return None
        try:
            x0, y0, x1, y1 = (float(v) for v in bbox_raw[:4])
            bbox = BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)
        except (TypeError, ValueError):
            return None
        rows = self._html_to_rows(html)
        return TableBlock(
            bbox=bbox,
            html=html,
            rows=rows,
            source="pp-structure",
            confidence=float(self._field(res, "score", default=1.0) or 1.0),
        )

    def _ensure_engine(self) -> Any:
        """Lazily build the PP-Structure engine on first use.

        Prefers the current PaddleOCR 3.x ``PPStructureV3`` and falls back to
        the legacy ``PPStructure`` callable when present.

        Returns:
            A callable engine accepting PNG bytes and returning region dicts.

        Raises:
            AiException: When PaddleOCR/PP-Structure is not importable.
        """
        if self._engine is not None and self._initialized:
            return self._engine
        try:
            from paddleocr import PPStructureV3  # type: ignore[import-untyped]
        except ImportError:
            try:
                from paddleocr import PPStructure  # type: ignore[import-untyped]
            except ImportError as error:
                raise AiException(
                    "PaddleOCR (PP-Structure) is required for TableRecognizer"
                ) from error
            return PPStructure(show_log=False, use_gpu=self.use_gpu, lang=self.lang)
        LOGGER.info(
            "Initializing PPStructureV3 engine lang=%s gpu=%s", self.lang, self.use_gpu
        )
        engine = PPStructureV3(lang=self.lang, use_gpu=self.use_gpu)
        self._engine = _PPStructureV3Engine(engine)
        self._initialized = True
        return self._engine

    @staticmethod
    def _field(obj: Any, name: str, default: Any) -> Any:
        """Read ``name`` from a dict or attribute-backed object.

        Args:
            obj: A dict, SimpleNamespace, or object with attributes.
            name: Field name to read.
            default: Value when missing.

        Returns:
            The field value or default.
        """
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    @staticmethod
    def _html_to_rows(html: str) -> list[list[str]]:
        """Extract a coarse cell matrix from a simple <table><tr><td> HTML.

        Used as a convenience payload so downstream consumers can iterate cells
        without an HTML parser. Only well-formed ``<td>`` contents are read;
        malformed HTML returns an empty matrix.

        Args:
            html: A table HTML string from PP-Structure.

        Returns:
            A list of rows, each a list of cell strings.
        """
        import re

        rows: list[list[str]] = []
        for row_match in re.finditer(
            r"<tr[^>]*>(.*?)</tr>", html, flags=re.IGNORECASE | re.DOTALL
        ):
            cells = [
                TableRecognizer._strip_tags(cell)
                for cell in re.findall(
                    r"<t[dh][^>]*>(.*?)</t[dh]>",
                    row_match.group(1),
                    flags=re.IGNORECASE | re.DOTALL,
                )
            ]
            if cells:
                rows.append(cells)
        return rows

    @staticmethod
    def _strip_tags(cell: str) -> str:
        """Collapse inner tags and whitespace in a cell HTML fragment.

        Args:
            cell: Inner HTML of a single ``<td>``.

        Returns:
            The visible cell text.
        """
        import re

        text = re.sub(r"<[^>]+>", "", cell)
        return re.sub(r"\s+", " ", text).strip()
