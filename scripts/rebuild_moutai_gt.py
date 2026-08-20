#!/usr/bin/env python3
"""M4.10 重建茅台 GT：从 PDF 文本层全量解析三表（合并+本期）。

背景：data/benchmark/ground_truth/moutai_2025.json 是 M2.12 regex 抽样的
17 项关键科目（README 明示需人工核对），M4.10 要求对真实 API 输出
F1 >= 0.85——稀疏 GT 下 precision 被抽取全量行拖死，且 regex 抓到
错误数值（营业利润 1000.008、所有者权益合计 12.6 亿）。

本脚本从 PDF 文本层（PyMuPDF）解析三张合并报表的完整科目行：
- 段边界：页文本含「合并资产负债表 / 合并利润表 / 合并现金流量表」
  标题后进入对应报表段；出现「母公司…」标题后退出（只取合并段）。
- 行状态机：非数字行累积科目名（跨行）；数字行产出科目值
  （第 1 个数=本期，第 2 个=上期；本期取第 1 个）。
- 科目名规范化与抽取链路一致（去行号/加减前缀/括号注释/空格）。

用法::

    python scripts/rebuild_moutai_gt.py --output data/benchmark/ground_truth/moutai_2025.json

输出直接覆盖既有 GT 文件（先备份原文件）。依赖：pymupdf（conda env1-py311 已装）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

import fitz  # pymupdf

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF = REPOSITORY_ROOT / "data" / "sample_reports" / "600519_贵州茅台_2025年年度报告.pdf"

_NUM_RE = re.compile(r"-?\d[\d,]*\.\d{2}")
_PAGE_HEADER_RE = re.compile(r"(年度报告|/\s*\d{1,3}\s*$)")
# 段边界标题（出现即切换报表上下文；含“母公司”即退出合并段）。
_STATEMENT_START = {
    "合并资产负债表": "balance_sheet",
    "母公司资产负债表": None,  # 母公司段不采集
    "合并利润表": "income_statement",
    "母公司利润表": None,
    "合并现金流量表": "cash_flow",
    "母公司现金流量表": None,
    "合并所有者权益变动表": None,
    "母公司所有者权益变动表": None,
}
# 表内非科目行（表头/单位/签字行等，不参与科目名累积）。
_SKIP_ROWS = {
    "项目", "附注", "2025年度", "2024年度", "2025 年度", "2024 年度",
    "单位：元", "币种：人民币", "公司负责人", "主管会计工作负责人",
    "会计机构负责人", "合并资产负债表", "母公司资产负债表",
    "合并利润表", "母公司利润表", "合并现金流量表", "母公司现金流量表",
}
# 单行纯附注号（如 "4"、"60(1)"）。
_NOTE_ONLY_RE = re.compile(r"^\d{1,3}(\(\d+\))?$")


def normalize_name(raw: str) -> str:
    """与抽取链路一致的科目名规范化（去行号/前缀/括号注释/空格）。

    额外处理文本层跨行碎片：括号注释被拆行后只剩孤括号（如
    ``列）投资收益（损失以-号填``），按 ``号填`` 截断清理。
    """
    name = raw.strip()
    # 表外行（每股收益元/股 等）不构成科目——caller 先行排除。
    # 括号注释碎片截断：含“号填列”字样时整段丢弃；若有左括号则从括号
    # 起截（“营业利润（亏损以“－”号填列）” → “营业利润”）。
    mark = name.find("号填")
    if mark != -1:
        paren = name.rfind("（", 0, mark)
        name = name[:paren] if paren != -1 else name[:mark]
    # 行号编号：一、二、… / （一）（二） / 1. 2. / 1、2、
    name = re.sub(r"^[一二三四五六七八九十]+、", "", name)
    name = re.sub(r"^（[一二三四五六七八九十]+）", "", name)
    name = re.sub(r"^\d+[、.]", "", name)
    # 行性质前缀：减：/加：/其中：
    name = re.sub(r"^(减|加|其中)：", "", name)
    # 括号注释（损失以“－”号填列 等）整体去除；残留孤括号剔除
    name = re.sub(r"（[^）]*）", "", name)
    name = name.replace("（", "").replace("）", "")
    # 全角符号转半角
    name = name.replace("－", "-").replace("—", "-").replace("“", "").replace("”", "")
    # 名称内部空格（表格识别拆分：现 金 → 现金）
    name = re.sub(r"\s+", "", name)
    return name


def _row_is_headerish(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if _PAGE_HEADER_RE.search(stripped):
        return True
    if _NOTE_ONLY_RE.match(stripped):
        return True
    if stripped in _SKIP_ROWS:
        return True
    return False


def extract_full_ground_truth(pdf_path: Path) -> dict[str, list[dict]]:
    """Parse consolidated statement rows from the PDF text layer (word grid).

    Uses PyMuPDF word coordinates to rebuild the table grid: words are
    clustered into rows by y-coordinate, then split by x-coordinate into
    the item-name column (left) and value columns (right). Rows without a
    value (e.g. sub-accounts with no current-period figure) still form
    their own name line and are skipped — they never merge into the next
    valued row.

    Returns:
        ``{"balance_sheet": [...], "income_statement": [...], "cash_flow": [...]}``
        — each row ``{"item": normalized, "value": float, "scope": "合并",
        "period": "本期", "source_page": int}``.
    """
    doc = fitz.open(str(pdf_path))
    result: dict[str, list[dict]] = {"balance_sheet": [], "income_statement": [], "cash_flow": []}
    current: str | None = None  # active statement type or None
    seen_rows: set[tuple[str, str]] = set()
    # 科目列/数值列的 x 分界：茅台年报表格科目列止于 ~270pt（595pt 页宽）。
    COLUMN_SPLIT_X = 280.0
    # 同行聚类容差：y0 差小于该值视为同一表格行。
    ROW_Y_TOLERANCE = 6.0

    def flush(statement: str, name: str, value: float, page: int) -> None:
        """Append one row, dropping duplicates (same name+value from page splits)."""
        key = (name, f"{value:.2f}")
        if key in seen_rows:
            return
        seen_rows.add(key)
        result[statement].append(
            {"item": name, "value": round(value, 4), "scope": "合并", "period": "本期",
             "source_page": page + 1}
        )

    for page_no, page in enumerate(doc):
        words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
        # 严格同行聚类（gap=3pt）——不跨单元格；跨行配对交给 y 区间重叠。
        line_rows: list[list[tuple[float, float, float, float, str]]] = []
        for w in sorted(words, key=lambda w: (w[1], w[0])):
            placed = False
            for row in line_rows:
                if abs(row[0][1] - w[1]) <= 3.0:
                    row.append(w)
                    placed = True
                    break
            if not placed:
                line_rows.append([w])

        # 值行：含数值 word 的行。科目行：x<分界、无数值、非表头。
        value_rows: list[tuple[str | None, list[tuple[float, float, float, float, str]]]] = []
        name_rows: list[list[tuple[float, float, float, float, str]]] = []
        for row in line_rows:
            text = "".join(w[4] for w in row)
            hit = next((t for t in _STATEMENT_START if t in text), None)
            if hit is not None:
                current = _STATEMENT_START[hit]
                continue
            if current is None:
                continue
            if _row_is_headerish(text):
                continue
            if any(_NUM_RE.search(w[4]) for w in row):
                value_rows.append((current, row))
            else:
                name_rows.append(row)

        # 每值行配对科目名：行内科目词（单行表，如「应收票据 | 4 | 值」）
        # 优先；否则与 y 区间重叠的科目行拼接（跨行单元格两段均与值行重叠）。
        for statement_at_row, value_row in value_rows:
            if statement_at_row is None:
                continue
            inline_left = [
                w for w in value_row if w[0] < COLUMN_SPLIT_X
                and not _NOTE_ONLY_RE.match(w[4])  # 附注号（60(1)）非科目词
            ]
            if inline_left:
                paired = inline_left
            else:
                v_y0 = min(w[1] for w in value_row)
                v_y1 = max(w[3] for w in value_row)
                paired = [
                    w
                    for row in name_rows
                    for w in row
                    if w[1] <= v_y1 + 3.0 and w[3] >= v_y0 - 3.0
                ]
            if not paired:
                continue
            paired.sort(key=lambda w: (w[1], w[0]))
            raw_name = "".join(w[4] for w in paired)
            raw_name = re.sub(r"\d{1,3}$", "", raw_name)  # 科目名尾随附注号
            # 表外行（每股收益等，单位不是元）不构成三表科目
            if not raw_name or "元/股" in raw_name or "元/份" in raw_name:
                continue
            name = normalize_name(raw_name)
            if not name:
                continue
            value_text = "".join(
                w[4]
                for w in value_row
                if not _NOTE_ONLY_RE.match(w[4])  # 附注号（44/60(1)）不参与值
            )
            numbers = _NUM_RE.findall(value_text)
            if not numbers:
                continue
            value = float(numbers[0].replace(",", ""))
            flush(statement_at_row, name, value, page_no)
    doc.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="M4.10 rebuild moutai full ground truth")
    parser.add_argument("--pdf", default=str(DEFAULT_PDF))
    parser.add_argument(
        "--output",
        default=str(REPOSITORY_ROOT / "data" / "benchmark" / "ground_truth" / "moutai_2025.json"),
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        print(f"ERROR: PDF not found: {pdf_path}", file=sys.stderr)
        return 1

    statements = extract_full_ground_truth(pdf_path)
    output = {
        "report_period": "2025-12-31",
        "currency": "CNY",
        "unit": "元",
        "company_code": "600519",
        "company_name": "贵州茅台",
        "source_pdf": pdf_path.name,
        "page_count": fitz.open(str(pdf_path)).page_count,
        "statements": statements,
        "notes": (
            "M4.10 重建：由 PDF 文本层全量解析（合并+本期），科目名规范化；"
            "原 M2.12 regex 抽样版本已备份为 moutai_2025.m2sample.json.bak"
        ),
    }
    out = Path(args.output)
    backup = out.with_suffix(".m2sample.json.bak")
    if out.is_file() and not backup.is_file():
        backup.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[gt] 原文件备份: {backup.name}")
    out.write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")

    for st, rows in statements.items():
        print(f"[gt] {st}: {len(rows)} 行")
        for r in rows[:5]:
            print(f"   {r['item']} = {r['value']}")
        if len(rows) > 5:
            print(f"   …（共 {len(rows)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
