"""M5.07 chunker 单测：文本切块边界 + Document 分块装配。"""

from __future__ import annotations

from app.modules.parser.chunker import (
    CHUNK_SIZE,
    TYPE_TABLE_HEADER,
    TYPE_TABLE_ROW,
    TYPE_TEXT,
    Chunk,
    chunk_document,
    chunk_text,
)


# ---------------------------------------------------------------------------
# chunk_text（通用文本切块）
# ---------------------------------------------------------------------------


def test_empty_text_returns_empty() -> None:
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_short_text_single_chunk() -> None:
    text = "短文本"
    assert chunk_text(text) == [text]


def test_exact_size_text_single_chunk() -> None:
    text = "字" * CHUNK_SIZE
    assert chunk_text(text) == [text]


def test_long_text_chunked_and_reassembled() -> None:
    """长文本按句切块，拼接后内容与原文一致（去空白）。"""
    text = "第一句。第二句！第三句；" * 30
    chunks = chunk_text(text, chunk_size=100, overlap=0)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


def test_long_sentence_hard_split() -> None:
    """无句边界的超长句按 chunk_size 硬切，不丢失内容。"""
    text = "字" * 300
    chunks = chunk_text(text, chunk_size=100, overlap=0)
    assert chunks == ["字" * 100] * 3


def test_overlap_appends_previous_tail() -> None:
    """overlap>0 时每块（除首块）开头拼接上一块末尾字符。"""
    text = "字" * 200
    chunks = chunk_text(text, chunk_size=100, overlap=20)
    assert chunks[0] == "字" * 100
    assert chunks[1] == "字" * 120  # 100 + 20 重叠
    assert chunks[1][:20] == chunks[0][-20:]


def test_invalid_overlap_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        chunk_text("abc", chunk_size=10, overlap=10)
    with pytest.raises(ValueError):
        chunk_text("abc", chunk_size=0, overlap=0)


# ---------------------------------------------------------------------------
# chunk_document（Document → Chunk 序列）
# ---------------------------------------------------------------------------


def _fake_document():
    """构造假 Document：1 个文本块 + 1 个表格（2 行）。"""
    from app.schemas.document import (
        BoundingBox,
        Document,
        Page,
        TableBlock,
        TextBlock,
    )

    bbox = BoundingBox(x0=0, y0=0, x1=100, y1=100)
    page = Page(
        page_index=0,
        width=595,
        height=842,
        text_blocks=[
            TextBlock(bbox=bbox, text="营收增长。净利润下滑。毛利率稳定。"),
        ],
        table_blocks=[
            TableBlock(
                bbox=bbox,
                html="<table></table>",
                rows=[["科目", "本期"], ["营业收入", "1688"]],
            ),
        ],
    )
    return Document(source="fake.pdf", page_count=1, pages=[page])


def test_chunk_document_builds_typed_chunks() -> None:
    """文本块 → TEXT；表格首行 → TABLE_HEADER，数据行 → TABLE_ROW；页码从 1 起。"""
    chunks = chunk_document(_fake_document(), chunk_size=512, overlap=0)

    assert [c.chunk_type for c in chunks] == [
        TYPE_TEXT,
        TYPE_TABLE_HEADER,
        TYPE_TABLE_ROW,
    ]
    assert chunks[0].page == 1 and chunks[1].page == 1 and chunks[2].page == 1
    assert (
        chunks[0].position == 1 and chunks[1].position == 2 and chunks[2].position == 3
    )
    assert "营收增长" in chunks[0].text
    assert chunks[1].text == "科目 | 本期"
    assert chunks[2].text == "营业收入 | 1688"
    assert isinstance(chunks[0], Chunk)


def test_chunk_document_long_text_block_splits() -> None:
    """长文本块被切成多个 TEXT chunk，position 递增。"""
    from app.schemas.document import BoundingBox, Document, Page, TextBlock

    bbox = BoundingBox(x0=0, y0=0, x1=100, y1=100)
    page = Page(
        page_index=2,
        width=595,
        height=842,
        text_blocks=[TextBlock(bbox=bbox, text="字" * 600)],
    )
    chunks = chunk_document(
        Document(source="f.pdf", page_count=3, pages=[page]), chunk_size=100, overlap=0
    )

    assert len(chunks) == 6  # 600/100
    assert all(c.page == 3 for c in chunks)
    assert [c.position for c in chunks] == [1, 2, 3, 4, 5, 6]


def test_chunk_document_empty_rows_skipped() -> None:
    """空表格行（全空白单元格）不产生 chunk。"""
    from app.schemas.document import BoundingBox, Document, Page, TableBlock

    bbox = BoundingBox(x0=0, y0=0, x1=100, y1=100)
    page = Page(
        page_index=0,
        width=595,
        height=842,
        table_blocks=[
            TableBlock(
                bbox=bbox,
                html="<table></table>",
                rows=[["", "  "], ["科目", "本期"], ["营收", "1688"]],
            )
        ],
    )
    chunks = chunk_document(
        Document(source="f.pdf", page_count=1, pages=[page]), chunk_size=512, overlap=0
    )

    # 全空白行被过滤；首个非空行（科目|本期）仍标为表头
    assert [c.chunk_type for c in chunks] == [TYPE_TABLE_HEADER, TYPE_TABLE_ROW]
    assert chunks[0].text == "科目 | 本期"
    assert chunks[1].text == "营收 | 1688"
