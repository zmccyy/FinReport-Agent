#!/usr/bin/env python3
"""M2.12 F1 evaluation script — three-statement extraction accuracy.

Plan §4 M2.12 acceptance criteria:
    3 份不同格式年报抽取 F1 平均 ≥ 0.70

Usage
-----
1. Mock LLM (no GPU; only verifies the script runs end-to-end with preset JSON)::

       python scripts/eval_m2_f1.py \\
           --pdf data/sample_reports/600519_贵州茅台_2025年年度报告.pdf \\
           --ground-truth data/benchmark/ground_truth/moutai_2025_sample.json \\
           --mock-llm \\
           --output docs/eval/m2-f1-sample.md

2. Real 7B inference (requires GPU + 4-bit model already loaded in
   ``ai-service`` ModelHub; not part of CI)::

       python scripts/eval_m2_f1.py \\
           --pdf data/sample_reports/600519_贵州茅台_2025年年度报告.pdf \\
           --ground-truth data/benchmark/ground_truth/moutai_2025.json \\
           --ai-service-url http://localhost:8000 \\
           --output docs/eval/m2-f1-moutai.md

F1 definition
-------------
For each statement type (BS / IS / CF), an extracted item matches a ground
truth item when:
    * ``item`` (科目名) is exactly equal (case-sensitive)
    * ``value`` (数值) is within 1% relative tolerance (容忍浮点误差)

Per statement type:
    precision = TP / (TP + FP)
    recall    = TP / (TP + FN)
    F1        = 2 * precision * recall / (precision + recall)

Overall F1 is the macro-average of three statement types.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "ai-service"))

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class StatementItem:
    """One statement item — name + value + optional metadata."""

    item: str
    value: float
    scope: str = ""
    period: str = ""
    source_page: int | None = None


@dataclass
class StatementMetrics:
    """Per-statement precision/recall/F1 + lists for debugging."""

    statement_type: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    matched_pairs: list[tuple[str, str]] = field(default_factory=list)
    unmatched_predicted: list[str] = field(default_factory=list)
    unmatched_ground_truth: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _items_from_list(payload: list[dict[str, Any]]) -> list[StatementItem]:
    """Build StatementItem list from a JSON list payload."""
    items: list[StatementItem] = []
    for row in payload:
        item_name = str(row.get("item", "")).strip()
        if not item_name:
            continue
        try:
            value = float(row.get("value", 0))
        except (TypeError, ValueError):
            continue
        items.append(
            StatementItem(
                item=item_name,
                value=value,
                scope=str(row.get("scope", "")),
                period=str(row.get("period", "")),
                source_page=row.get("source_page"),
            )
        )
    return items


def _value_matches(predicted: float, truth: float, relative_tolerance: float = 0.01) -> bool:
    """Check if predicted value matches truth within relative tolerance."""
    if math.isclose(truth, 0.0):
        return math.isclose(predicted, 0.0, abs_tol=1.0)
    return math.isclose(predicted, truth, rel_tol=relative_tolerance)


def _match_items(
    predicted: list[StatementItem], truth: list[StatementItem]
) -> tuple[list[tuple[StatementItem, StatementItem]], list[StatementItem], list[StatementItem]]:
    """Match predicted items to truth items by name + value tolerance."""
    matched: list[tuple[StatementItem, StatementItem]] = []
    truth_pool = list(truth)
    unmatched_predicted: list[StatementItem] = []

    for pred in predicted:
        # Find first truth item with same name and matching value
        for i, truth_item in enumerate(truth_pool):
            if truth_item.item == pred.item and _value_matches(pred.value, truth_item.value):
                matched.append((pred, truth_item))
                truth_pool.pop(i)
                break
        else:
            unmatched_predicted.append(pred)

    unmatched_truth = truth_pool
    return matched, unmatched_predicted, unmatched_truth


def _compute_metrics(
    statement_type: str,
    predicted: list[StatementItem],
    truth: list[StatementItem],
) -> StatementMetrics:
    """Compute precision/recall/F1 for one statement type."""
    matched, unmatched_pred, unmatched_truth = _match_items(predicted, truth)
    tp = len(matched)
    fp = len(unmatched_pred)
    fn = len(unmatched_truth)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return StatementMetrics(
        statement_type=statement_type,
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        matched_pairs=[(p.item, t.item) for p, t in matched],
        unmatched_predicted=[p.item for p in unmatched_pred],
        unmatched_ground_truth=[t.item for t in unmatched_truth],
    )


# ---------------------------------------------------------------------------
# Extractor invocation
# ---------------------------------------------------------------------------


def _extract_with_mock_llm(
    ground_truth: dict[str, Any],
) -> dict[str, list[StatementItem]]:
    """Mock LLM: return ground truth itself — for script-run validation only.

    Real LLM invocation requires GPU + loaded 7B model. This mock path exists
    so contributors can verify the script executes end-to-end without a GPU.
    """
    statements = ground_truth.get("statements", {})
    return {
        st_type: _items_from_list(items)
        for st_type, items in statements.items()
    }


def _extract_with_real_llm(
    pdf_path: Path, ai_service_url: str
) -> dict[str, list[StatementItem]]:
    """Real LLM invocation: send PDF to ai-service /parse/upload then /extract.

    NOTE: This path is intentionally simple. Production F1 evaluation should
    use the MQ-driven task lifecycle (TaskOrchestrator.createTask) which
    parallelizes 3 EXTRACT steps and writes financial_statement rows. Here we
    only invoke a synchronous round-trip per statement type for the eval script.
    """
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "real LLM path requires 'requests' package: pip install requests"
        ) from exc

    # Upload PDF to ai-service
    with pdf_path.open("rb") as f:
        upload_resp = requests.post(
            f"{ai_service_url}/parse/upload",
            files={"file": (pdf_path.name, f, "application/pdf")},
            timeout=300,
        )
        upload_resp.raise_for_status()
        document = upload_resp.json()["document"]

    # Extract per statement type
    result: dict[str, list[StatementItem]] = {}
    for st_type, step in [
        ("balance_sheet", "extract.bs"),
        ("income_statement", "extract.is"),
        ("cash_flow", "extract.cf"),
    ]:
        extract_resp = requests.post(
            f"{ai_service_url}/internal/models/generate",
            json={
                "prompt": f"Extract {st_type} from document {document.get('source', '')}",
                "max_new_tokens": 2048,
                "temperature": 0.0,
            },
            timeout=120,
        )
        extract_resp.raise_for_status()
        raw_text = extract_resp.json()["text"]
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            result[st_type] = []
            continue
        statements = parsed.get("statements", {})
        result[st_type] = _items_from_list(statements.get(st_type, []))

    return result


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_markdown_report(
    pdf_path: Path,
    ground_truth_path: Path,
    metrics_list: list[StatementMetrics],
    used_mock_llm: bool,
) -> str:
    """Render Markdown evaluation report."""
    overall_f1 = sum(m.f1 for m in metrics_list) / len(metrics_list) if metrics_list else 0.0
    overall_precision = (
        sum(m.precision for m in metrics_list) / len(metrics_list) if metrics_list else 0.0
    )
    overall_recall = (
        sum(m.recall for m in metrics_list) / len(metrics_list) if metrics_list else 0.0
    )

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines = [
        "# M2.12 抽取 F1 评估报告",
        "",
        f"> 生成时间：{timestamp}",
        f"> PDF：`{pdf_path}`",
        f"> Ground truth：`{ground_truth_path}`",
        f"> LLM 模式：{'mock（无 GPU）' if used_mock_llm else 'real 7B（GPU）'}",
        "",
        "## 总体指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| Overall F1 | **{overall_f1:.4f}** |",
        f"| Overall Precision | {overall_precision:.4f} |",
        f"| Overall Recall | {overall_recall:.4f} |",
        f"| M2.12 门槛 (F1 ≥ 0.70) | {'✅ 通过' if overall_f1 >= 0.70 else '❌ 未达标'} |",
        "",
        "## 各表指标",
        "",
        "| 表类型 | Precision | Recall | F1 | TP | FP | FN |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in metrics_list:
        lines.append(
            f"| {m.statement_type} | {m.precision:.4f} | {m.recall:.4f} | "
            f"**{m.f1:.4f}** | {m.tp} | {m.fp} | {m.fn} |"
        )

    lines.extend(["", "## 各表详情", ""])
    for m in metrics_list:
        lines.extend(
            [
                f"### {m.statement_type}",
                "",
                f"- 匹配项（TP）：{m.tp}",
                f"- 多余项（FP）：{m.fp} — {m.unmatched_predicted[:5]}",
                f"- 漏项（FN）：{m.fn} — {m.unmatched_ground_truth[:5]}",
                "",
            ]
        )

    lines.extend(
        [
            "## 备注",
            "",
            "- F1 计算口径：item 名严格相等 + value 相对误差 ≤ 1%",
            "- Mock 模式仅验证脚本可运行性，F1 必为 1.0（用 ground truth 自身作为模型输出）",
            "- Real 7B 模式需要 GPU + 已加载 Qwen2.5-7B-Int4 模型",
            "- 真实评估请参考 `data/benchmark/README.md` 补齐完整 ground truth JSON",
            "",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="M2.12 F1 evaluation script")
    parser.add_argument(
        "--pdf", required=True, type=Path, help="Path to PDF annual report"
    )
    parser.add_argument(
        "--ground-truth",
        required=True,
        type=Path,
        help="Path to ground truth JSON (see data/benchmark/README.md)",
    )
    parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="Use mock LLM (preset ground truth as model output) — no GPU needed",
    )
    parser.add_argument(
        "--ai-service-url",
        default="http://localhost:8000",
        help="ai-service base URL for real LLM mode (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output Markdown file path (default: stdout)",
    )
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"ERROR: PDF not found: {args.pdf}", file=sys.stderr)
        return 1
    if not args.ground_truth.exists():
        print(f"ERROR: ground truth not found: {args.ground_truth}", file=sys.stderr)
        return 1

    ground_truth = json.loads(args.ground_truth.read_text(encoding="utf-8"))
    truth_statements: dict[str, list[StatementItem]] = {
        st_type: _items_from_list(items)
        for st_type, items in ground_truth.get("statements", {}).items()
    }

    if args.mock_llm:
        predicted = _extract_with_mock_llm(ground_truth)
    else:
        predicted = _extract_with_real_llm(args.pdf, args.ai_service_url)

    metrics_list: list[StatementMetrics] = []
    for st_type in ["balance_sheet", "income_statement", "cash_flow"]:
        truth = truth_statements.get(st_type, [])
        pred = predicted.get(st_type, [])
        metrics_list.append(_compute_metrics(st_type, pred, truth))

    report = _render_markdown_report(
        pdf_path=args.pdf,
        ground_truth_path=args.ground_truth,
        metrics_list=metrics_list,
        used_mock_llm=args.mock_llm,
    )

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"F1 report written to {args.output}")
    else:
        print(report)

    # Exit code: 0 if F1 >= 0.70, 1 otherwise
    overall_f1 = sum(m.f1 for m in metrics_list) / len(metrics_list) if metrics_list else 0.0
    return 0 if overall_f1 >= 0.70 else 1


if __name__ == "__main__":
    sys.exit(main())
