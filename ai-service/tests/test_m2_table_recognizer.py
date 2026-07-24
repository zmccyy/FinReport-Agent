"""M2.02 TableRecognizer tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.exceptions import AiException
from app.modules.parser.table_recognizer import TableRecognizer
from app.schemas.document import BoundingBox, TableBlock


class _FakeEngine:
    """Captures invocations and returns preset regions."""

    def __init__(self, regions: list[dict[str, Any]]) -> None:
        self.regions = regions
        self.calls: list[bytes] = []

    def __call__(self, image_bytes: bytes) -> list[dict[str, Any]]:
        """Record the call and yield the preset regions."""
        self.calls.append(image_bytes)
        return self.regions


class _FailingEngine:
    """Raises on every invocation."""

    def __call__(self, image_bytes: bytes) -> list[dict[str, Any]]:
        """Always raise to exercise the error-swallowing path."""
        del image_bytes
        raise RuntimeError("pp-structure boom")


def test_table_recognizer_returns_table_blocks() -> None:
    """Valid table regions are converted to TableBlock entries."""
    regions = [
        {
            "type": "table",
            "bbox": [10, 20, 110, 80],
            "res": {
                "html": "<table><tr><td>A</td><td>B</td></tr></table>",
                "score": 0.9,
            },
        },
        {"type": "figure", "bbox": [0, 0, 10, 10], "res": {}},
    ]
    engine = _FakeEngine(regions)
    recognizer = TableRecognizer(engine=engine)

    blocks = recognizer.analyze_page(0, b"PNG")

    assert len(blocks) == 1
    block = blocks[0]
    assert isinstance(block, TableBlock)
    assert block.html.startswith("<table>")
    assert block.rows == [["A", "B"]]
    assert block.confidence == pytest.approx(0.9)
    assert block.bbox == BoundingBox(x0=10, y0=20, x1=110, y1=80)


def test_table_recognizer_skips_malformed_regions() -> None:
    """Regions missing html or bbox are dropped."""
    regions = [
        {"type": "table", "bbox": [1, 2, 3], "res": {"html": "<table></table>"}},
        {"type": "table", "bbox": [1, 2, 3, 4], "res": {}},
        {"type": "table", "bbox": None, "res": {"html": "<table></table>"}},
    ]
    engine = _FakeEngine(regions)
    recognizer = TableRecognizer(engine=engine)

    blocks = recognizer.analyze_page(0, b"PNG")

    assert blocks == []


def test_table_recognizer_swallows_engine_failure() -> None:
    """Engine exceptions do not propagate; an empty list is returned."""
    recognizer = TableRecognizer(engine=_FailingEngine())
    assert recognizer.analyze_page(0, b"PNG") == []


def test_table_recognizer_raises_when_missing_paddle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing paddleocr import surfaces AiException on first use."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "paddleocr":
            raise ImportError("no paddleocr")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    recognizer = TableRecognizer()
    with pytest.raises(AiException, match="PaddleOCR"):
        recognizer.analyze_page(0, b"PNG")


def test_html_to_rows_handles_nested_cells() -> None:
    """_html_to_rows extracts cell text from multi-row tables."""
    html = "<table><tr><td>H1</td><td>H2</td></tr><tr><td><b>v1</b></td><td>v2</td></tr></table>"
    rows = TableRecognizer._html_to_rows(html)
    assert rows == [["H1", "H2"], ["v1", "v2"]]


def test_html_to_rows_returns_empty_for_malformed() -> None:
    """Tables without <tr> wrappers yield an empty matrix."""
    assert TableRecognizer._html_to_rows("<p>no table</p>") == []


def test_strip_tags_collapses_whitespace() -> None:
    """_strip_tags drops inner tags and collapses whitespace."""
    cell = "<b>  hello\nworld  </b>"
    assert TableRecognizer._strip_tags(cell) == "hello world"


def test_engine_is_built_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PP-Structure engine is constructed lazily and cached."""
    built_calls: list[bool] = []

    class _StubPPV3:
        """Mimics PaddleOCR 3.x PPStructureV3 (predict-based)."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built_calls.append(True)

        def predict(self, image: bytes) -> list:
            """Return an empty page list; the adapter handles it gracefully."""
            del image
            return []

    import sys
    import types

    module = types.ModuleType("paddleocr")
    module.PPStructureV3 = _StubPPV3  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", module)

    recognizer = TableRecognizer()
    recognizer.analyze_page(0, b"PNG")
    recognizer.analyze_page(1, b"PNG")

    assert len(built_calls) == 1


def test_ppstructurev3_engine_maps_table_regions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PaddleOCR 3.x adapter projects table_res_list to TableBlocks."""
    from app.modules.parser.table_recognizer import _PPStructureV3Engine

    class _Page:
        """Minimal predict-page result shaped like PPStructureV3 output."""

        table_res_list = [
            {
                "pred_html": "<table><tr><td>X</td></tr></table>",
                "table_region": [0, 0, 50, 30],
            }
        ]

    class _StubPPV3:
        """Returns one page result carrying a single table region."""

        def predict(self, image: bytes) -> list:
            """Yield the page-level result."""
            del image
            return [_Page()]

    engine = _PPStructureV3Engine(_StubPPV3())
    regions = engine(b"PNG")

    assert len(regions) == 1
    assert regions[0]["type"] == "table"
    assert regions[0]["bbox"] == [0, 0, 50, 30]
    assert "<table>" in regions[0]["res"]["html"]


def test_ppstructurev3_engine_handles_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty predict result yields no regions."""
    from app.modules.parser.table_recognizer import _PPStructureV3Engine

    class _StubPPV3:
        def predict(self, image: bytes) -> list:
            """Return nothing."""
            del image
            return []

    engine = _PPStructureV3Engine(_StubPPV3())
    assert engine(b"PNG") == []


def test_ppstructurev3_engine_skips_regions_without_html() -> None:
    """Table entries missing html or bbox are dropped."""
    from app.modules.parser.table_recognizer import _PPStructureV3Engine

    class _Page:
        table_res_list = [
            {"pred_html": "", "table_region": [1, 2, 3, 4]},
            {"pred_html": "<table></table>", "table_region": None},
        ]

    class _StubPPV3:
        def predict(self, image: bytes) -> list:
            """Yield the page-level result."""
            del image
            return [_Page()]

    engine = _PPStructureV3Engine(_StubPPV3())
    assert engine(b"PNG") == []


def test_ppstructurev3_engine_maps_layout_bbox_real_shape() -> None:
    """Real PPStructureV3 puts bbox on layout_det_res boxes, not on entries.

    Mirrors paddlex LayoutParsingResultV2: table_res_list entries only carry
    pred_html; the table bbox lives in layout_det_res['boxes'][i]['coordinate']
    for boxes whose label contains 'table', in the same order as table_res_list.
    """
    from app.modules.parser.table_recognizer import _PPStructureV3Engine

    class _Layout:
        boxes = [
            {"label": "text", "coordinate": [0, 0, 10, 10]},
            {"label": "table", "coordinate": [11, 22, 111, 88]},
            {"label": "figure", "coordinate": [5, 5, 50, 50]},
            {"label": "table", "coordinate": [200, 300, 400, 500]},
        ]

    class _Page:
        layout_det_res = _Layout()
        table_res_list = [
            {"pred_html": "<table><tr><td>A</td></tr></table>"},
            {"pred_html": "<table><tr><td>B</td></tr></table>"},
        ]

    class _StubPPV3:
        def predict(self, image: bytes) -> list:
            """Yield the page-level result."""
            del image
            return [_Page()]

    engine = _PPStructureV3Engine(_StubPPV3())
    regions = engine(b"PNG")

    assert len(regions) == 2
    assert regions[0]["bbox"] == [11.0, 22.0, 111.0, 88.0]
    assert regions[1]["bbox"] == [200.0, 300.0, 400.0, 500.0]
    assert "A" in regions[0]["res"]["html"]
    assert "B" in regions[1]["res"]["html"]


def test_ppstructurev3_engine_drops_table_without_layout_bbox() -> None:
    """An entry with html but no matching layout table box is dropped."""
    from app.modules.parser.table_recognizer import _PPStructureV3Engine

    class _Layout:
        boxes = [{"label": "text", "coordinate": [0, 0, 10, 10]}]

    class _Page:
        layout_det_res = _Layout()
        table_res_list = [{"pred_html": "<table></table>"}]

    class _StubPPV3:
        def predict(self, image: bytes) -> list:
            """Yield the page-level result."""
            del image
            return [_Page()]

    engine = _PPStructureV3Engine(_StubPPV3())
    assert engine(b"PNG") == []


def test_ensure_engine_falls_back_to_legacy_ppstructure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy PaddleOCR (no PPStructureV3) builds the old PPStructure callable."""
    built: list[bool] = []

    class _LegacyPPStructure:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append(True)

        def __call__(self, image: bytes) -> list:
            """Return no table regions."""
            del image
            return []

    import sys
    import types

    module = types.ModuleType("paddleocr")
    module.PPStructure = _LegacyPPStructure  # type: ignore[attr-defined]
    # No PPStructureV3 attribute — forces the legacy fallback branch
    monkeypatch.setitem(sys.modules, "paddleocr", module)

    recognizer = TableRecognizer()
    recognizer.analyze_page(0, b"PNG")

    assert built == [True]
