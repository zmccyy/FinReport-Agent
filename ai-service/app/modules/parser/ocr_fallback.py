"""M2.03 OcrFallback: PaddleOCR recognition for scanned pages.

The fallback produces ``TextBlock`` instances from a rendered page image using
PaddleOCR's detection+recognition pipeline. Like the table recognizer, it
imports PaddleOCR lazily and accepts an injected engine for fast unit tests.

PaddleOCR 3.x replaces the 2.x ``PaddleOCR(ocr_results)`` callable with a
``PaddleOCR.predict()`` method; we adapt the real engine to the same
``engine(image_bytes) -> list[line]`` contract used by tests.
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import AiException
from app.schemas.document import BoundingBox, TextBlock
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)


class _PaddleOCREngine:
    """Adapts PaddleOCR 3.x ``PaddleOCR`` to the engine callable contract.

    ``PaddleOCR.predict()`` returns a list of page results; we map the first
    page's ``rec_polys``/``rec_texts``/``rec_scores`` entries to the legacy
    ``[box, (text, score)]`` line shape so callers stay unchanged.
    """

    def __init__(self, engine: Any) -> None:
        """Wrap a built PaddleOCR instance.

        Args:
            engine: A PaddleOCR instance.
        """
        self._engine = engine

    def __call__(self, image_bytes: bytes) -> list[Any]:
        """Return recognized line entries for the rendered page image.

        Args:
            image_bytes: PNG bytes of the rendered page.

        Returns:
            A list of ``[box, (text, score)]`` entries.
        """
        page = self._first_predict_page(self._engine.predict(image_bytes))
        if page is None:
            return []
        polys = OcrFallback._field(page, "rec_polys", default=None) or []
        texts = OcrFallback._field(page, "rec_texts", default=None) or []
        scores = OcrFallback._field(page, "rec_scores", default=None) or []
        lines: list[Any] = []
        for box, text, score in zip(polys, texts, scores):
            if text:
                lines.append([list(box), (text, float(score))])
        return lines

    @staticmethod
    def _first_predict_page(results: Any) -> Any:
        """Return the first page result regardless of list/iterator shape.

        Args:
            results: Output of PaddleOCR.predict().

        Returns:
            The first page result, or None when empty.
        """
        try:
            return next(iter(results))
        except StopIteration:
            return None
        except TypeError:
            return None


class OcrFallback:
    """Recognize text on scanned page images via PaddleOCR."""

    def __init__(
        self,
        lang: str = "ch",
        use_gpu: bool = False,
        engine: Any | None = None,
    ) -> None:
        """Configure the OCR provider.

        Args:
            lang: PaddleOCR language flag (defaults to Chinese).
            use_gpu: Whether PaddleOCR may use the GPU.
            engine: Optional pre-built engine for tests (callable or instance).
        """
        self.lang = lang
        self.use_gpu = use_gpu
        self._engine = engine
        self._initialized = engine is not None

    def recognize(self, page_index: int, image_bytes: bytes) -> list[TextBlock]:
        """Run OCR over the page image and return recognized text blocks.

        Args:
            page_index: 0-based page index (for logging only).
            image_bytes: PNG bytes of the rendered page.

        Returns:
            Recognized text blocks; empty when OCR yields nothing.
        """
        engine = self._ensure_engine()
        try:
            results = engine(image_bytes) or []
        except Exception:
            LOGGER.exception("PaddleOCR inference failed page=%d", page_index)
            return []

        blocks: list[TextBlock] = []
        for line in self._iter_lines(results):
            block = self._to_text_block(line)
            if block is not None:
                blocks.append(block)
        LOGGER.debug("OCR recognized page=%d blocks=%d", page_index, len(blocks))
        return blocks

    def _to_text_block(self, line: Any) -> TextBlock | None:
        """Convert one PaddleOCR line result to a TextBlock.

        PaddleOCR emits ``[[box, (text, score)], ...]`` where ``box`` is a
        4-point polygon. We collapse the polygon to its axis-aligned bbox.

        Args:
            line: A single recognition entry.

        Returns:
            A TextBlock, or None when the entry is malformed.
        """
        try:
            box, (text, score) = line
        except (TypeError, ValueError):
            return None
        if not text:
            return None
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        if not xs or not ys:
            return None
        bbox = BoundingBox(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))
        return TextBlock(bbox=bbox, text=text, confidence=float(score))

    def _iter_lines(self, results: Any) -> list[Any]:
        """Normalize the several PaddleOCR result shapes into a flat list.

        Args:
            results: Raw PaddleOCR output (list of pages or list of lines,
                possibly wrapped by modelscope distributions).

        Returns:
            A flat list of per-line entries.
        """
        if (
            isinstance(results, list)
            and len(results) == 1
            and isinstance(results[0], list)
        ):
            return results[0]
        if isinstance(results, list):
            return results
        return []

    def _ensure_engine(self) -> Any:
        """Lazily build the PaddleOCR engine on first use.

        Returns:
            A callable engine accepting PNG bytes and returning line entries.

        Raises:
            AiException: When PaddleOCR is not importable.
        """
        if self._engine is not None and self._initialized:
            return self._engine
        try:
            from paddleocr import PaddleOCR  # type: ignore[import-untyped]
        except ImportError as error:
            raise AiException("PaddleOCR is required for OcrFallback") from error
        LOGGER.info(
            "Initializing PaddleOCR engine lang=%s gpu=%s", self.lang, self.use_gpu
        )
        try:
            engine = PaddleOCR(use_angle_cls=True, lang=self.lang, use_gpu=self.use_gpu)
        except TypeError:
            # PaddleOCR 3.x removed the 2.x kwargs; build with the defaults.
            LOGGER.warning("Falling back to PaddleOCR 3.x default constructor")
            engine = PaddleOCR(lang=self.lang)
        self._engine = _PaddleOCREngine(engine)
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
