#!/usr/bin/env python3
"""M6.08 通用 GT 重建：从年报 PDF 文本层全量解析三表（合并+本期）。

rebuild_moutai_gt.py（M4.10，保留作历史记录）的参数化泛化，支持不同
披露格式的 A 股年报。相对茅台版的差异（由三家基准年报的实测布局驱动）：

* **单位无关**：茅台=元（两位小数）、平安银行=百万元（千分位整数）、
  宁德时代=千元（千分位整数）；GT 数值保持文档原单位（与抽取链路
  validator._VALID_UNITS 对齐），由 --unit 显式声明。
* **数值词识别**：千分位整数/括号负数/小数均识别（茅台版 regex 强制
  两位小数，对平安/宁德解析出 0 行）；附注号与数值的歧义按「数值列
  x 分区」消解——分区起点取该页强数值词（千分位/小数/括号负数）的
  最小 x0，普通 1-3 位整数只有落在分区内才算数值，否则视为附注号。
* **段边界扩展**：银行年报的母公司段标题为「银行资产负债表」等
  （非「母公司…」），作为退出合并段的边界标题。
* **孤立数值行兜底配对**：括号负数行（如平安利息支出）与科目名不
  同行且 y 不重叠时，回退到纵向最近的科目行（≤15pt，跳过「：」结尾
  的节标题），减少 GT 缺行。

用法::

    python scripts/rebuild_gt.py \
        --pdf data/sample_reports/000001_平安银行_2025年年度报告.pdf \
        --company-code 000001 --company-name 平安银行 \
        --unit 百万元 --report-period 2025-12-31 \
        --output data/benchmark/ground_truth/pingan_2025.json
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

# 科目名规范化单源化（M4.10 审查修复 L2/L4）：与抽取链路、F1 评估共用
# 同一实现（含尾随附注号剥离与未闭合括号碎片截断），消除多份拷贝分叉。
sys.path.insert(0, str(REPOSITORY_ROOT / "ai-service"))
from app.modules.extractor.normalize import (  # noqa: E402
    normalize_item_name as normalize_name,
)

# 强数值词：千分位分组 / 小数 / 括号负数（会计惯例括号=负值）。
# 括号形式要求千分位、≥4 位整数或带小数——排除附注子号「(1)」。
_COMMA_NUM = r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?"
_DEC_NUM = r"-?\d+\.\d+"
_STRONG_VALUE_RE = re.compile(
    rf"^(?:{_COMMA_NUM}|{_DEC_NUM})$"
    rf"|^\({_COMMA_NUM}\)$|^\(-?\d{{4,}}(?:\.\d+)?\)$|^\(-?\d+\.\d+\)$"
)
# 1-3 位纯整数：附注号与真实小值歧义，靠数值列分区消解。
_PLAIN_INT_RE = re.compile(r"^\d{1,3}$")
# 括号数值（任意位数负值，如 "(74)"）：与附注子号 "(1)" 歧义，
# 仅当落在数值列分区内才算数值（is_value_word 位置门控）。
_PAREN_VALUE_RE = re.compile(r"^\(-?\d[\d,]*(?:\.\d+)?\)$")
# 单行纯附注号（如 "4"、"60(1)"）。
_NOTE_ONLY_RE = re.compile(r"^\d{1,3}(\(\d+\))?$")
_PAGE_HEADER_RE = re.compile(r"(年度报告|/\s*\d{1,3}\s*$)")
# 段标题行的编号/修饰剥离：行号（1、/（一））与续表后缀（(续)），
# 剥离后须整体等于标题才触发段切换——防止附注交叉引用
# （如「合并资产负债表项目注释」）误触发（宁德实测 890 行吞并）。
_TITLE_NUM_PREFIX_RE = re.compile(r"^(?:[（(]?[一二三四五六七八九十\d]{1,3}[）)、.、]|\s)+")
_TITLE_CONT_SUFFIX_RE = re.compile(r"[（(]续[）)]$")

# 段边界标题：出现即切换报表上下文；None=退出合并段。
# 银行年报母公司段为「银行…」而非「母公司…」（平安银行实测）。
_STATEMENT_START: dict[str, str | None] = {
    "合并资产负债表": "balance_sheet",
    "母公司资产负债表": None,
    "银行资产负债表": None,
    "合并利润表": "income_statement",
    "母公司利润表": None,
    "银行利润表": None,
    "合并现金流量表": "cash_flow",
    "母公司现金流量表": None,
    "银行现金流量表": None,
    "合并所有者权益变动表": None,
    "母公司所有者权益变动表": None,
    "银行所有者权益变动表": None,
}
# 表内非科目行（表头/单位/签字行等，不参与科目名累积）。
_SKIP_ROWS = {
    "项目", "附注", "2025年度", "2024年度", "2025 年度", "2024 年度",
    "单位：元", "币种：人民币", "公司负责人", "主管会计工作负责人",
    "会计机构负责人", "合并资产负债表", "母公司资产负债表",
    "合并利润表", "母公司利润表", "合并现金流量表", "母公司现金流量表",
}
# 科目列/数值列 x 分界（595pt 页宽实测，茅台/平安/宁德科目名均止于 ~270pt）。
COLUMN_SPLIT_X = 280.0
# 数值分区的普通整数容差：分区起点左侧 25pt 内的整数仍算数值。
_PLAIN_INT_SLACK_X = 25.0
# 孤立数值行兜底配对的最大纵向距离（pt）。
_FALLBACK_PAIR_Y = 15.0
# 折行科目名拼接：上方 name-only 行与值行的最大间距（pt），
# 以及续行相对首行的最小缩进（pt）——平安长科目名折行实测缩进 ~10pt。
_WRAP_JOIN_MAX_GAP_Y = 14.0
_WRAP_JOIN_MIN_INDENT_X = 5.0
# 节标题类行不参与折行拼接：编号节标题（「二、投资活动…」）与冒号结尾
# 行。「其中：/加：/减：」前缀允许参与——它们常是折行名首行（平安 IS
# 「其中：以摊余成本…终止确认产/生的收益」实测），normalize 会剥前缀。
_SECTION_LIKE_RE = re.compile(r"^(?:[一二三四五六七八九十]{1,3}、|[（(][一二三四五六七八九十]+[）)]|\d+、)")


def _is_section_like(text: str) -> bool:
    return bool(_SECTION_LIKE_RE.match(text)) or text.endswith("：")


def _parse_value(word: str) -> float | None:
    """Parse a value word; parenthesized = negative; None for non-values."""
    text = word.strip()
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace(",", "").replace("－", "-")
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def _row_is_headerish(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if _PAGE_HEADER_RE.search(stripped):
        return True
    if stripped in _SKIP_ROWS:
        return True
    return False


def extract_full_ground_truth(pdf_path: Path, wrap_join: bool = False) -> dict[str, list[dict]]:
    """Parse consolidated statement rows from the PDF text layer (word grid).

    Uses PyMuPDF word coordinates to rebuild the table grid: words are
    clustered into rows by y-coordinate, then split by x-coordinate into
    the item-name column (left) and value columns (right). The value zone
    start per page is derived from strong value words so note-number
    columns (which may sit between names and values, e.g. 平安银行) are
    excluded positionally.

    Returns:
        ``{"balance_sheet": [...], "income_statement": [...], "cash_flow": [...]}``
        — each row ``{"item": normalized, "value": float, "scope": "合并",
        "period": "本期", "source_page": int}``.
    """
    doc = fitz.open(str(pdf_path))
    result: dict[str, list[dict]] = {"balance_sheet": [], "income_statement": [], "cash_flow": []}
    current: str | None = None  # active statement type or None
    seen_rows: set[tuple[str, str]] = set()

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

        # 数值分区起点：该页强数值词的最小 x0（附注号列必在其左侧）。
        strong_xs = [
            w[0] for row in line_rows for w in row if _STRONG_VALUE_RE.match(w[4])
        ]
        zone_x = (min(strong_xs) - _PLAIN_INT_SLACK_X) if strong_xs else COLUMN_SPLIT_X

        def is_value_word(w: tuple[float, float, float, float, str]) -> bool:
            if _STRONG_VALUE_RE.match(w[4]):
                return True
            if _PAREN_VALUE_RE.match(w[4]):
                return w[0] >= zone_x
            return _PLAIN_INT_RE.match(w[4]) is not None and w[0] >= zone_x

        def is_name_word(w: tuple[float, float, float, float, str]) -> bool:
            return w[0] < COLUMN_SPLIT_X and not _NOTE_ONLY_RE.match(w[4])

        # 值行：含数值词的行。科目行：仅科目列词、无数值词、非表头。
        value_rows: list[tuple[str | None, list[tuple[float, float, float, float, str]]]] = []
        name_rows: list[list[tuple[float, float, float, float, str]]] = []
        for row in line_rows:
            text = "".join(w[4] for w in row)
            # 段标题严格匹配：去编号前缀与续表后缀后须整体等于标题，
            # 防止正文/附注中的交叉引用误触发段切换。
            candidate = _TITLE_CONT_SUFFIX_RE.sub("", text).strip()
            candidate = _TITLE_NUM_PREFIX_RE.sub("", candidate).strip()
            if candidate in _STATEMENT_START:
                current = _STATEMENT_START[candidate]
                continue
            if current is None:
                continue
            if _row_is_headerish(text):
                continue
            if any(is_value_word(w) for w in row):
                value_rows.append((current, row))
            else:
                name_rows.append(row)

        # 每值行配对科目名：行内科目词（单行表）优先；其次与 y 区间重叠的
        # 科目行拼接（跨行单元格）；最后回退纵向最近科目行（括号负数行）。
        for statement_at_row, value_row in value_rows:
            if statement_at_row is None:
                continue
            inline_left = [w for w in value_row if is_name_word(w)]
            if inline_left:
                paired = inline_left
            else:
                v_y0 = min(w[1] for w in value_row)
                v_y1 = max(w[3] for w in value_row)
                overlapping = [
                    w for row in name_rows for w in row
                    if is_name_word(w) and w[1] <= v_y1 + 3.0 and w[3] >= v_y0 - 3.0
                ]
                if overlapping:
                    paired = overlapping
                else:
                    # 兜底：取纵向中心最近的科目行（≤15pt）；节标题
                    # （「流动资产：」）不参与，避免张冠李戴。
                    v_center = (v_y0 + v_y1) / 2
                    candidates = [
                        row for row in name_rows
                        if is_name_word(row[0])
                        and not normalize_name("".join(w[4] for w in row)).endswith("：")
                    ]
                    if not candidates:
                        continue
                    best = min(
                        candidates,
                        key=lambda row: abs(
                            (min(w[1] for w in row) + max(w[3] for w in row)) / 2 - v_center
                        ),
                    )
                    best_center = (min(w[1] for w in best) + max(w[3] for w in best)) / 2
                    if abs(best_center - v_center) > _FALLBACK_PAIR_Y:
                        continue
                    paired = [w for w in best if is_name_word(w)]
            if not paired:
                continue
            # 折行科目名拼接（--wrap-join 启用）：值行内联科目词为缩进
            # 续行残片时，拼接上方紧邻的 name-only 行。仅用于平安类
            # 「长科目名 + 续行缩进 + 残片与数值同行」布局；茅台/宁德
            # 的折行名已由 y-overlap 路径处理，且其缩进小计行与上方
            # 残片行同构（p58「其他非流动负债」+「非流动负债合计」），
            # 无法用位置规则区分，故默认关闭。
            if wrap_join:
                while True:
                    inline_xs = [w[0] for w in paired if w[0] < COLUMN_SPLIT_X]
                    top_y0 = min(w[1] for w in paired)
                    above = None
                    for row in name_rows:
                        words = [w for w in row if is_name_word(w)]
                        if not words or any(is_value_word(w) for w in row):
                            continue
                        row_y1 = max(w[3] for w in words)
                        row_text = "".join(w[4] for w in words)
                        if (
                            0 <= top_y0 - row_y1 <= _WRAP_JOIN_MAX_GAP_Y
                            and min(inline_xs) >= min(w[0] for w in words) + _WRAP_JOIN_MIN_INDENT_X
                            and not _is_section_like(row_text)
                        ):
                            above = words
                            break
                    if above is None:
                        break
                    paired = above + paired
            paired.sort(key=lambda w: (w[1], w[0]))
            raw_name = "".join(w[4] for w in paired)
            # 表外行（每股收益等，单位不是元）不构成三表科目
            if not raw_name or "元/股" in raw_name or "元/份" in raw_name:
                continue
            name = normalize_name(raw_name)
            if not name:
                continue
            value_words = [w for w in value_row if is_value_word(w)]
            value_words.sort(key=lambda w: w[0])
            numbers = [_parse_value(w[4]) for w in value_words]
            numbers = [n for n in numbers if n is not None]
            if not numbers:
                continue
            value = numbers[0]
            flush(statement_at_row, name, value, page_no)
    doc.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="M6.08 通用全量 GT 重建（PDF 文本层，合并+本期）"
    )
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--company-code", required=True)
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--unit", default="元", help="文档数值单位（元/万元/百万元/千元）")
    parser.add_argument("--report-period", default="2025-12-31")
    parser.add_argument("--output", required=True)
    parser.add_argument("--show", action="store_true", help="打印全部科目行")
    parser.add_argument(
        "--wrap-join", action="store_true",
        help="启用折行科目名拼接（平安类长科目名布局；茅台/宁德勿开）",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        print(f"ERROR: PDF not found: {pdf_path}", file=sys.stderr)
        return 1

    statements = extract_full_ground_truth(pdf_path, wrap_join=args.wrap_join)
    output = {
        "report_period": args.report_period,
        "currency": "CNY",
        "unit": args.unit,
        "company_code": args.company_code,
        "company_name": args.company_name,
        "source_pdf": pdf_path.name,
        "page_count": fitz.open(str(pdf_path)).page_count,
        "statements": statements,
        "notes": (
            "M6.08 重建：由 PDF 文本层全量解析（合并+本期），科目名规范化；"
            f"数值单位与文档一致（{args.unit}）"
        ),
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")

    for st, rows in statements.items():
        print(f"[gt] {st}: {len(rows)} 行")
        if args.show:
            for r in rows:
                print(f"   p{r['source_page']:>3} {r['item']} = {r['value']}")
        else:
            for r in rows[:5]:
                print(f"   {r['item']} = {r['value']}")
            if len(rows) > 5:
                print(f"   …（共 {len(rows)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
