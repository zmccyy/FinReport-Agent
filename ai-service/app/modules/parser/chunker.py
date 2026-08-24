"""M5.07 知识库文本切块（spec §3.4：chunk_size=512, overlap=64）。

把解析后的 Document 转成检索块：文本块按句/段贪心合并到目标长度
（超长句硬切），表格块按行切（表头行 → TABLE_HEADER，数据行 →
TABLE_ROW）。纯标准库实现，不引入新依赖（不增镜像体积）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: 默认目标块长度（字符）。
CHUNK_SIZE = 512
#: 相邻块重叠长度（字符，spec §3.4）。
OVERLAP = 64

#: 块类型（与 Milvus fin_kb chunk_type 字段一致）。
TYPE_TEXT = "TEXT"
TYPE_TABLE_HEADER = "TABLE_HEADER"
TYPE_TABLE_ROW = "TABLE_ROW"

# 句子边界（中文句号/问号/感叹号/分号 + 换行）。
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？；\n])")


@dataclass(frozen=True)
class Chunk:
    """一个检索块（对应 fin_kb 一行）。"""

    text: str
    page: int
    position: int
    chunk_type: str = TYPE_TEXT


def chunk_text(
    text: str, chunk_size: int = CHUNK_SIZE, overlap: int = OVERLAP
) -> list[str]:
    """把长文本切为重叠块（句边界优先，超长句硬切）。

    Args:
        text: 输入文本（去除首尾空白）。
        chunk_size: 目标块长度（字符）。
        overlap: 相邻块尾部重叠长度（字符）。

    Returns:
        文本块列表（不重叠时块长 ≤ chunk_size；重叠块开头可能超长）。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be in [0, chunk_size)")

    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    # 先按句子拆分，再贪心合并到 chunk_size；单句超长则硬切。
    sentences = [s for s in _SENTENCE_BOUNDARY.split(text) if s]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) <= chunk_size:
            current += sentence
            continue
        if current:
            chunks.append(current)
        # 超长单句按 chunk_size 硬切（防无限循环）。
        while len(sentence) > chunk_size:
            chunks.append(sentence[:chunk_size])
            sentence = sentence[chunk_size:]
        current = sentence
    if current:
        chunks.append(current)

    if overlap <= 0:
        return chunks

    # 加 overlap：每块开头拼接上一块末尾 overlap 字符（首块除外）。
    overlapped: list[str] = []
    for i, chunk in enumerate(chunks):
        if i == 0:
            overlapped.append(chunk)
            continue
        tail = chunks[i - 1][-overlap:]
        overlapped.append(tail + chunk)
    return overlapped


def chunk_document(
    document: Any, chunk_size: int = CHUNK_SIZE, overlap: int = OVERLAP
) -> list[Chunk]:
    """把解析后的 Document 转成检索块序列。

    Args:
        document: ``Document``（app.schemas.document），含分页 block。
        chunk_size: 目标块长度（字符）。
        overlap: 相邻块重叠长度（字符）。

    Returns:
        按页序排列的 ``Chunk`` 列表（page 从 1 开始，position 为页内序号）。
    """
    chunks: list[Chunk] = []
    for page in document.pages:
        position = 0
        for block in page.blocks:
            if block.type.value == "text" or block.type.value == "title":
                for text in chunk_text(block.text, chunk_size, overlap):
                    position += 1
                    chunks.append(
                        Chunk(
                            text=text,
                            page=page.page_index + 1,
                            position=position,
                            chunk_type=TYPE_TEXT,
                        )
                    )
            elif block.type.value == "table":
                # 跳过全空白行后再标记表头：首个非空行视为表头。
                non_empty_rows = [row for row in block.rows if _row_to_text(row)]
                for row_index, row in enumerate(non_empty_rows):
                    position += 1
                    chunk_type = TYPE_TABLE_HEADER if row_index == 0 else TYPE_TABLE_ROW
                    chunks.append(
                        Chunk(
                            text=_row_to_text(row),
                            page=page.page_index + 1,
                            position=position,
                            chunk_type=chunk_type,
                        )
                    )
    return chunks


def _row_to_text(row: list[str]) -> str:
    """表格行单元格用竖线连接（保留列结构，供检索与展示）。"""
    cells = [cell.strip() for cell in row if cell and cell.strip()]
    return " | ".join(cells)


__all__ = [
    "CHUNK_SIZE",
    "OVERLAP",
    "TYPE_TEXT",
    "TYPE_TABLE_HEADER",
    "TYPE_TABLE_ROW",
    "Chunk",
    "chunk_text",
    "chunk_document",
]
