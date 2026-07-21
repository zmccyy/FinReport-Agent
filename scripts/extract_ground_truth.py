"""Extract key financial statement items from real A-share annual reports.

This script reads a real PDF annual report and extracts a small subset of
key financial items (BS / IS / CF) as ground truth for F1 evaluation.

Usage:
    python scripts/extract_ground_truth.py \\
        --pdf data/sample_reports/000001_平安银行_2025年年度报告.pdf \\
        --output data/benchmark/ground_truth/pingan_2025_sample.json \\
        --report-period 2025-12-31
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "ai-service"))

# Key items to look for in each statement type. These are the most common
# A-share report line items (per Ministry of Finance accounting standards).
KEY_ITEMS_BS = [
    "资产总计",
    "负债合计",
    "所有者权益合计",
    "股东权益合计",
    "货币资金",
    "应收账款",
    "存货",
    "固定资产",
]

KEY_ITEMS_IS = [
    "营业收入",
    "营业利润",
    "利润总额",
    "净利润",
    "归属于母公司股东的净利润",
    "营业成本",
]

KEY_ITEMS_CF = [
    "经营活动产生的现金流量净额",
    "投资活动产生的现金流量净额",
    "筹资活动产生的现金流量净额",
    "现金及现金等价物净增加额",
]

# Number regex: matches "1,234,567,890.12" or "1234567890.12" or "1.23E+09"
# A-share reports typically use thousands separator.
NUMBER_RE = re.compile(
    r"(?<![A-Za-z])"  # not preceded by letter (avoid matching "v1.23")
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?![A-Za-z])"
)


def _clean_number(raw: str) -> float | None:
    """Convert a numeric string with comma separators to a float."""
    if not raw:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _find_item_value(text: str, item: str) -> float | None:
    """Find a numeric value appearing near an item name in text.

    Strategy: look for "item_name  number" pattern in same line; if not found,
    look at the next non-empty line.
    """
    # Pattern 1: "item_name ...  number" on the same line
    pattern = re.compile(
        re.escape(item) + r"[^\d]{0,50}" + NUMBER_RE.pattern,
        re.MULTILINE,
    )
    match = pattern.search(text)
    if match:
        return _clean_number(match.group("num"))
    return None


def _find_source_page(document: Any, item: str) -> int:
    """Find the page index where item first appears."""
    for page in document.pages:
        for block in page.text_blocks:
            if item in block.text:
                return page.page_index + 1  # 1-based for human readability
    return 0


def extract_ground_truth(pdf_path: Path, report_period: str) -> dict[str, Any]:
    """Extract key items from PDF as ground truth."""
    from app.modules.parser.document_parser import DocumentParser  # noqa: PLC0415

    parser = DocumentParser()
    pdf_bytes = pdf_path.read_bytes()
    document = parser.parse_bytes(pdf_bytes, source=pdf_path.name)

    # Concatenate all text from all pages for searching
    full_text = "\n".join(
        block.text
        for page in document.pages
        for block in page.text_blocks
    )

    result: dict[str, Any] = {
        "report_period": report_period,
        "currency": "CNY",
        "unit": "元",
        "company_code": "",
        "company_name": "",
        "source_pdf": pdf_path.name,
        "page_count": document.page_count,
        "statements": {"balance_sheet": [], "income_statement": [], "cash_flow": []},
        "notes": "Auto-extracted sample; manually verify before using for real F1 evaluation.",
    }

    # BS items
    for item in KEY_ITEMS_BS:
        value = _find_item_value(full_text, item)
        if value is not None:
            source_page = _find_source_page(document, item)
            result["statements"]["balance_sheet"].append({
                "item": item,
                "value": value,
                "scope": "合并",
                "period": "本期",
                "source_page": source_page,
            })

    # IS items
    for item in KEY_ITEMS_IS:
        value = _find_item_value(full_text, item)
        if value is not None:
            source_page = _find_source_page(document, item)
            result["statements"]["income_statement"].append({
                "item": item,
                "value": value,
                "scope": "合并",
                "period": "本期",
                "source_page": source_page,
            })

    # CF items
    for item in KEY_ITEMS_CF:
        value = _find_item_value(full_text, item)
        if value is not None:
            source_page = _find_source_page(document, item)
            result["statements"]["cash_flow"].append({
                "item": item,
                "value": value,
                "scope": "合并",
                "period": "本期",
                "source_page": source_page,
            })

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract ground truth from real PDF")
    parser.add_argument("--pdf", required=True, type=Path, help="Path to PDF")
    parser.add_argument("--output", required=True, type=Path, help="Output JSON path")
    parser.add_argument(
        "--report-period",
        default="2025-12-31",
        help="Report period (default: 2025-12-31)",
    )
    parser.add_argument(
        "--company-code",
        default="",
        help="Company code (e.g., 600519)",
    )
    parser.add_argument(
        "--company-name",
        default="",
        help="Company name (e.g., 贵州茅台)",
    )
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"ERROR: PDF not found: {args.pdf}", file=sys.stderr)
        return 1

    result = extract_ground_truth(args.pdf, args.report_period)
    if args.company_code:
        result["company_code"] = args.company_code
    if args.company_name:
        result["company_name"] = args.company_name

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    bs_count = len(result["statements"]["balance_sheet"])
    is_count = len(result["statements"]["income_statement"])
    cf_count = len(result["statements"]["cash_flow"])
    print(
        f"Wrote {args.output}: BS={bs_count} IS={is_count} CF={cf_count} "
        f"pages={result['page_count']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
