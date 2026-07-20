"""M6 document parsing schemas (Page / TextBlock / TableBlock / Document)."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class BlockType(str, Enum):
    """Layout block categories produced by the document parser."""

    TEXT = "text"
    TITLE = "title"
    TABLE = "table"
    FIGURE = "figure"
    HEADER = "header"
    FOOTER = "footer"
    UNKNOWN = "unknown"


class BoundingBox(BaseModel):
    """Axis-aligned rectangle in page coordinates (points, origin top-left)."""

    x0: float = Field(ge=0)
    y0: float = Field(ge=0)
    x1: float = Field(ge=0)
    y1: float = Field(ge=0)

    @field_validator("x1", "y1")
    @classmethod
    def _end_after_start(cls, v: float, info: Any) -> float:
        """Ensure the box has non-zero width and height."""
        start = info.data.get(f"{info.field_name[0]}0")
        if start is not None and v < start:
            raise ValueError(f"{info.field_name} must be >= {info.field_name[0]}0")
        return v

    @property
    def width(self) -> float:
        """Box width."""
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        """Box height."""
        return self.y1 - self.y0


class TextBlock(BaseModel):
    """A paragraph, heading, or caption extracted from the page."""

    type: BlockType = Field(default=BlockType.TEXT)
    bbox: BoundingBox
    text: str = Field(min_length=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class TableBlock(BaseModel):
    """A table region restored to HTML plus its raw cell matrix."""

    type: BlockType = Field(default=BlockType.TABLE, frozen=True)
    bbox: BoundingBox
    html: str = Field(min_length=1)
    rows: list[list[str]] = Field(default_factory=list)
    source: str = Field(default="pp-structure", description="识别引擎来源")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class FigureBlock(BaseModel):
    """An image or chart region on the page."""

    type: BlockType = Field(default=BlockType.FIGURE, frozen=True)
    bbox: BoundingBox
    image_key: str | None = Field(default=None, description="MinIO 对象 key，若有导出")


class Page(BaseModel):
    """A single parsed PDF page composed of layout blocks."""

    page_index: int = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    text_blocks: list[TextBlock] = Field(default_factory=list)
    table_blocks: list[TableBlock] = Field(default_factory=list)
    figure_blocks: list[FigureBlock] = Field(default_factory=list)
    is_scanned: bool = Field(default=False, description="该页是否疑似扫描件")
    ocr_applied: bool = Field(default=False, description="是否触发 OCR 兜底")

    @property
    def blocks(self) -> list[Any]:
        """All blocks in declaration order for downstream consumers."""
        return [*self.text_blocks, *self.table_blocks, *self.figure_blocks]


class Document(BaseModel):
    """A fully parsed PDF document (spec §2.3 M6)."""

    source: str = Field(description="MinIO 对象 key 或本地路径")
    page_count: int = Field(ge=0)
    pages: list[Page] = Field(default_factory=list)
    parser_version: str = Field(default="m6-v1")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def total_tables(self) -> int:
        """Count of tables across all pages."""
        return sum(len(p.table_blocks) for p in self.pages)

    @property
    def is_scanned(self) -> bool:
        """Whether any page required OCR fallback."""
        return any(p.is_scanned for p in self.pages)
