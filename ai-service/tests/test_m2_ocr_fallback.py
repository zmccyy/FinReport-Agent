"""M2.03 OcrFallback tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.exceptions import AiException
from app.modules.parser.ocr_fallback import OcrFallback
from app.schemas.document import TextBlock


class _FakeOcrEngine:
    """Returns preset OCR line entries."""

    def __init__(self, lines: list[Any]) -> None:
        self.lines = lines
        self.calls: list[bytes] = []

    def __call__(self, image_bytes: bytes) -> list[Any]:
        """Record the call and yield the preset lines."""
        self.calls.append(image_bytes)
        return self.lines


class _FailingOcrEngine:
    """Always raise."""

    def __call__(self, image_bytes: bytes) -> list[Any]:
        """Raise to exercise the error-swallowing path."""
        del image_bytes
        raise RuntimeError("ocr boom")


def _line(text: str, box: list[list[float]] | None = None, score: float = 0.95) -> list:
    box = box or [[0, 0], [10, 0], [10, 5], [0, 5]]
    return [box, (text, score)]


def test_recognize_returns_text_blocks() -> None:
    """Each PaddleOCR line becomes a TextBlock with bounding box."""
    lines = [
        _line("第一行"),
        _line("第二行", box=[[20, 20], [80, 20], [80, 30], [20, 30]]),
    ]
    engine = _FakeOcrEngine(lines)
    ocr = OcrFallback(engine=engine)

    blocks = ocr.recognize(0, b"PNG")

    assert [b.text for b in blocks] == ["第一行", "第二行"]
    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].bbox.x0 == 0
    assert blocks[0].bbox.x1 == 10
    assert blocks[1].bbox.x0 == 20
    assert blocks[1].confidence == pytest.approx(0.95)


def test_recognize_skips_blank_and_malformed_lines() -> None:
    """Empty text or malformed box entries are dropped."""
    lines = [
        _line(""),
        ["not-a-valid-entry"],
        _line(
            "ok", box=[[1, 1], [3, 1], [3, 3]]
        ),  # malformed polygon (3 pts is fine, but text 'ok')
    ]
    engine = _FakeOcrEngine(lines)
    ocr = OcrFallback(engine=engine)

    blocks = ocr.recognize(0, b"PNG")

    texts = [b.text for b in blocks]
    assert "ok" in texts
    assert "" not in texts


def test_recognize_normalizes_single_page_wrapping() -> None:
    """PaddleOCR wraps pages as [[...lines...]]; the provider flattens them."""
    payload = [_line("a"), _line("b")]
    engine = _FakeOcrEngine(payload)  # returns flat list (one page shape)
    ocr = OcrFallback(engine=engine)
    assert len(ocr.recognize(0, b"PNG")) == 2

    engine2 = _FakeOcrEngine([payload])  # nested page wrapping
    ocr2 = OcrFallback(engine=engine2)
    assert len(ocr2.recognize(0, b"PNG")) == 2


def test_recognize_swallows_engine_failure() -> None:
    """Engine exceptions return an empty list instead of propagating."""
    ocr = OcrFallback(engine=_FailingOcrEngine())
    assert ocr.recognize(0, b"PNG") == []


def test_recognize_raises_when_missing_paddle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing paddleocr surfaces AiException."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "paddleocr":
            raise ImportError("no paddleocr")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ocr = OcrFallback()
    with pytest.raises(AiException, match="PaddleOCR"):
        ocr.recognize(0, b"PNG")


def test_engine_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PaddleOCR engine is built only once and reused."""
    built_calls: list[bool] = []

    class _StubPaddle:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built_calls.append(True)

        def predict(self, image: bytes) -> list:
            """Return no pages; the adapter must still construct once."""
            del image
            return []

    import sys
    import types

    module = types.ModuleType("paddleocr")
    module.PaddleOCR = _StubPaddle  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", module)

    ocr = OcrFallback()
    ocr.recognize(0, b"PNG")
    ocr.recognize(1, b"PNG")

    assert len(built_calls) == 1


def test_paddleocr_engine_maps_predict_results() -> None:
    """The PaddleOCR 3.x adapter projects rec_polys/rec_texts to line entries."""
    from app.modules.parser.ocr_fallback import _PaddleOCREngine

    class _Page:
        rec_polys = [[[0, 0], [10, 0], [10, 5], [0, 5]]]
        rec_texts = ["hi"]
        rec_scores = [0.9]

    class _StubPaddle:
        def predict(self, image: bytes) -> list:
            """Yield one page carrying one recognized line."""
            del image
            return [_Page()]

    engine = _PaddleOCREngine(_StubPaddle())
    lines = engine(b"PNG")

    assert len(lines) == 1
    box, (text, score) = lines[0]
    assert text == "hi"
    assert score == pytest.approx(0.9)
    assert box == [[0, 0], [10, 0], [10, 5], [0, 5]]


def test_paddleocr_engine_handles_empty_and_blank() -> None:
    """Empty predict results or blank lines yield no entries."""
    from app.modules.parser.ocr_fallback import _PaddleOCREngine

    class _Page:
        rec_polys = [[[]]]
        rec_texts = ["", "ok"]
        rec_scores = [0.1, 0.8]

    class _StubPaddle:
        def predict(self, image: bytes) -> list:
            """Yield one page."""
            del image
            return []

    engine = _PaddleOCREngine(_StubPaddle())
    assert engine(b"PNG") == []


def test_falls_back_to_3x_default_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the old 2.x kwargs raise TypeError, the 3.x default is used."""
    built: dict[str, Any] = {}

    class _StubPaddle:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # First call (with legacy kwargs) -> raise; second call (lang=) records
            if "use_angle_cls" in kwargs or "use_gpu" in kwargs:
                raise TypeError("unsupported legacy arg")
            built["kwargs"] = kwargs

        def predict(self, image: bytes) -> list:
            """No pages."""
            del image
            return []

    import sys
    import types

    module = types.ModuleType("paddleocr")
    module.PaddleOCR = _StubPaddle  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", module)

    ocr = OcrFallback()
    ocr.recognize(0, b"PNG")

    assert "use_angle_cls" not in built["kwargs"]
    assert built["kwargs"].get("lang") == "ch"
