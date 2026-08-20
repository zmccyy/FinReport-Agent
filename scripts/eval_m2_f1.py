#!/usr/bin/env python3
"""M2.12 / M4.10 F1 evaluation script — three-statement extraction accuracy.

Plan §4 M2.12 acceptance criteria:
    3 份不同格式年报抽取 F1 平均 ≥ 0.70
M4 阶段验收（docs/progress/m4.md）:
    eval_m2_f1.py 对真实 API 输出 F1 ≥ 0.85（--threshold 默认值）

Usage
-----
1. Mock LLM (only verifies the script runs end-to-end with preset JSON)::

       python scripts/eval_m2_f1.py \\
           --pdf data/sample_reports/600519_贵州茅台_2025年年度报告.pdf \\
           --ground-truth data/benchmark/ground_truth/moutai_2025_sample.json \\
           --mock-llm \\
           --output docs/eval/m2-f1-sample.md

2. Real evaluation via the full E2E pipeline (M4.10): register → upload
   PDF → MQ 编排 → PARSE（表格识别）→ EXTRACT ×3（DeepSeek API）→
   StatementWriter 落库 → 回读 financial_statement 计算 F1。
   Requires the dev compose stack running with ``LLM_API_KEY`` configured;
   not part of CI::

       python scripts/eval_m2_f1.py \\
           --pdf data/sample_reports/600519_贵州茅台_2025年年度报告.pdf \\
           --ground-truth data/benchmark/ground_truth/moutai_2025.json \\
           --backend-url http://localhost:8080 \\
           --output docs/eval/m4-f1-moutai.md

Granularity
-----------
The extractor emits rows for both 合并/母公司 scopes and 本期/上期 periods.
When every ground-truth item of one statement type shares the same
scope+period, predicted rows are filtered to that granularity before
matching, so F1 is computed at the ground truth's granularity.

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
import re
import sys
import time
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


def _normalize_item_name(raw: str) -> str:
    """科目名规范化——与 app/modules/extractor/handler.normalize_item_name 同一套规则。

    M4.10 复测：LLM 输出带表格行号（一、营业收入）、行性质前缀
    （减：营业成本）、括号注释（（损失以…号填列））与识别空格
    （现 金）。GT 重建时已规范，此函数对 GT 幂等，对预测侧清洗。
    """
    name = raw.strip()
    mark = name.find("号填")
    if mark != -1:
        paren = name.rfind("（", 0, mark)
        name = name[:paren] if paren != -1 else name[:mark]
    name = re.sub(
        r"^(?:[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+[、.]|(?:减|加|其中)：)",
        "",
        name,
    )
    name = re.sub(r"（[^）]*）", "", name)
    name = name.replace("（", "").replace("）", "")
    name = name.replace("－", "-").replace("—", "-").replace("“", "").replace("”", "")
    return re.sub(r"\s+", "", name).strip()


def _items_from_list(payload: list[dict[str, Any]]) -> list[StatementItem]:
    """Build StatementItem list from a JSON list payload (names normalized)."""
    items: list[StatementItem] = []
    for row in payload:
        item_name = _normalize_item_name(str(row.get("item", "")))
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

    Real evaluation (M4.10) drives the full E2E pipeline via the L2 backend.
    This mock path exists so contributors can verify the script executes
    end-to-end without a running stack or API key.
    """
    statements = ground_truth.get("statements", {})
    return {
        st_type: _items_from_list(items)
        for st_type, items in statements.items()
    }


def _extract_with_backend_pipeline(
    pdf_path: Path, backend_url: str, ground_truth: dict[str, Any], timeout: int
) -> dict[str, list[StatementItem]]:
    """Real evaluation via the full E2E pipeline (M4.10).

    Drives the production chain end to end — register → upload → MQ 编排
    → PARSE（表格识别）→ EXTRACT ×3（DeepSeek API json_mode）→
    StatementWriter 落库 — then reads back ``financial_statement`` rows via
    ``GET /api/v1/reports/{reportId}/statements``. The measured output is
    exactly what users see, not a synthetic prompt round-trip.

    Args:
        pdf_path: Path to the annual report PDF.
        backend_url: L2 backend base URL.
        ground_truth: Benchmark ground truth JSON (company metadata).
        timeout: Task terminal-status polling deadline in seconds.

    Raises:
        RuntimeError: When any HTTP step fails or the task ends non-COMPLETED.
    """
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "real evaluation requires 'requests' package: pip install requests"
        ) from exc

    # 1. 注册 + 登录（随机用户，避免复用脏数据）
    username = f"f1eval_{int(datetime.now().timestamp())}"
    register_resp = requests.post(
        f"{backend_url}/api/v1/auth/register",
        json={
            "username": username,
            "password": "f1eval_pass_123",
            "email": f"{username}@test.com",
        },
        timeout=15,
    )
    if register_resp.status_code not in (200, 201):
        raise RuntimeError(
            f"register failed HTTP {register_resp.status_code}: "
            f"{register_resp.text[:200]}"
        )
    token = register_resp.json().get("accessToken")
    if not token:
        raise RuntimeError("register response missing accessToken")
    auth_headers = {"Authorization": f"Bearer {token}"}

    # 2. 上传 PDF（公司信息取自 ground truth 元数据）
    company_code = str(ground_truth.get("company_code") or "000000")
    company_name = str(ground_truth.get("company_name") or pdf_path.stem)
    report_period = str(ground_truth.get("report_period") or "2025-12-31")
    with pdf_path.open("rb") as fh:
        upload_resp = requests.post(
            f"{backend_url}/api/v1/reports/upload",
            headers=auth_headers,
            files={"file": (pdf_path.name, fh, "application/pdf")},
            data={
                "companyCode": company_code,
                "companyName": company_name,
                "reportType": "ANNUAL",
                "reportPeriod": report_period,
            },
            timeout=120,
        )
    if upload_resp.status_code not in (200, 201):
        raise RuntimeError(
            f"upload failed HTTP {upload_resp.status_code}: {upload_resp.text[:300]}"
        )
    upload_body = upload_resp.json()
    task_id = upload_body.get("taskId")
    report_id = upload_body.get("reportId")
    if not task_id or not report_id:
        raise RuntimeError(f"upload response missing taskId/reportId: {upload_body}")
    print(f"[eval] taskId={task_id} reportId={report_id}")

    # 3. 轮询任务终态（默认 1500s：PARSE 单步实测 >5min 属已知性能债务，
    # 600s 硬编码会过早超时；由 --timeout 参数控制）
    deadline = time.monotonic() + timeout
    last_status = ""
    task_status = ""
    while time.monotonic() < deadline:
        task_resp = requests.get(
            f"{backend_url}/api/v1/tasks/{task_id}", headers=auth_headers, timeout=15
        )
        if task_resp.status_code != 200:
            raise RuntimeError(
                f"task query failed HTTP {task_resp.status_code}: {task_resp.text[:200]}"
            )
        task_status = task_resp.json().get("status", "")
        if task_status != last_status:
            print(f"[eval] task status: {task_status}")
            last_status = task_status
        if task_status in {"COMPLETED", "FAILED", "CANCELLED"}:
            break
        time.sleep(3)
    else:
        raise RuntimeError(f"task {task_id} did not reach terminal status in {timeout}s")
    if task_status != "COMPLETED":
        raise RuntimeError(
            f"task {task_id} ended with status={task_status} "
            f"(body: {task_resp.text[:500]})"
        )

    # 4. 回读三表（financial_statement 经 StatementWriter 落库后的行）
    statements_resp = requests.get(
        f"{backend_url}/api/v1/reports/{report_id}/statements",
        headers=auth_headers,
        timeout=30,
    )
    if statements_resp.status_code != 200:
        raise RuntimeError(
            f"statements query failed HTTP {statements_resp.status_code}: "
            f"{statements_resp.text[:200]}"
        )
    body = statements_resp.json()
    result: dict[str, list[StatementItem]] = {}
    for st_type, key in [
        ("balance_sheet", "balanceSheet"),
        ("income_statement", "incomeStatement"),
        ("cash_flow", "cashFlow"),
    ]:
        rows = body.get(key) or []
        items: list[StatementItem] = []
        for row in rows:
            name = str(row.get("itemName") or "").strip()
            value = row.get("itemValue")
            if not name or value is None:
                continue
            items.append(
                StatementItem(
                    item=name,
                    value=float(value),
                    scope=str(row.get("scope") or ""),
                    period=str(row.get("periodType") or ""),
                    source_page=row.get("sourcePage"),
                )
            )
        result[st_type] = items
    return result


def _filter_to_truth_granularity(
    predicted: dict[str, list[StatementItem]],
    truth: dict[str, list[StatementItem]],
) -> dict[str, list[StatementItem]]:
    """Filter predicted rows to the ground truth's scope+period granularity.

    The extractor emits 合并/母公司 × 本期/上期 rows; benchmark ground truth
    is built at one granularity (typically 合并+本期). When all truth items
    of a statement type share the same scope and period, keep only predicted
    rows at that granularity so F1 compares like with like.
    """
    filtered: dict[str, list[StatementItem]] = {}
    for st_type, truth_items in truth.items():
        pred_items = predicted.get(st_type, [])
        if not truth_items or not pred_items:
            filtered[st_type] = list(pred_items)
            continue
        scopes = {t.scope for t in truth_items}
        periods = {t.period for t in truth_items}
        if len(scopes) == 1 and len(periods) == 1:
            target_scope, target_period = scopes.pop(), periods.pop()
            kept = [
                p
                for p in pred_items
                if (not p.scope or p.scope == target_scope)
                and (not p.period or p.period == target_period)
            ]
            print(
                f"[eval] {st_type}: 按粒度筛选 scope={target_scope} "
                f"period={target_period}（{len(pred_items)} → {len(kept)} 行）"
            )
            filtered[st_type] = kept
        else:
            filtered[st_type] = list(pred_items)
    return filtered


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_markdown_report(
    pdf_path: Path,
    ground_truth_path: Path,
    metrics_list: list[StatementMetrics],
    llm_mode: str,
    threshold: float,
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
        "# 抽取 F1 评估报告（M2.12 / M4.10）",
        "",
        f"> 生成时间：{timestamp}",
        f"> PDF：`{pdf_path}`",
        f"> Ground truth：`{ground_truth_path}`",
        f"> LLM 模式：{llm_mode}",
        "",
        "## 总体指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| Overall F1 | **{overall_f1:.4f}** |",
        f"| Overall Precision | {overall_precision:.4f} |",
        f"| Overall Recall | {overall_recall:.4f} |",
        f"| 门槛 (F1 ≥ {threshold:.2f}) | {'✅ 通过' if overall_f1 >= threshold else '❌ 未达标'} |",
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
            "- 粒度对齐：ground truth 全部科目同 scope/period 时，预测行先筛选到同粒度再匹配",
            "- Mock 模式仅验证脚本可运行性，F1 必为 1.0（用 ground truth 自身作为模型输出）",
            "- 真实模式（M4.10）走完整 E2E 管道：上传 PDF → MQ 编排 → DeepSeek API 抽取"
            " → StatementWriter 落库 → 回读 financial_statement",
            "- 真实评估请参考 `data/benchmark/README.md` 补齐完整 ground truth JSON",
            "",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="M2.12 / M4.10 F1 evaluation script")
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
        help="Use mock LLM (preset ground truth as model output) — no backend needed",
    )
    parser.add_argument(
        "--backend-url",
        default="http://localhost:8080",
        help="L2 backend base URL for real evaluation (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1500,
        help="任务终态等待上限秒数（默认 1500；PARSE 单步实测 >5min 属已知性能债务）",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.85,
        help="Overall F1 pass threshold (default 0.85 per M4 acceptance)",
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
        llm_mode = "mock（ground truth 自身作为输出，仅验证脚本）"
    else:
        try:
            predicted = _extract_with_backend_pipeline(
                args.pdf, args.backend_url, ground_truth, args.timeout
            )
        except RuntimeError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        llm_mode = f"真实 E2E 管道（DeepSeek API，backend={args.backend_url}）"
        predicted = _filter_to_truth_granularity(predicted, truth_statements)

    metrics_list: list[StatementMetrics] = []
    for st_type in ["balance_sheet", "income_statement", "cash_flow"]:
        truth = truth_statements.get(st_type, [])
        pred = predicted.get(st_type, [])
        metrics_list.append(_compute_metrics(st_type, pred, truth))

    report = _render_markdown_report(
        pdf_path=args.pdf,
        ground_truth_path=args.ground_truth,
        metrics_list=metrics_list,
        llm_mode=llm_mode,
        threshold=args.threshold,
    )

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"F1 report written to {args.output}")
    else:
        print(report)

    # Exit code: 0 if F1 >= threshold (default 0.85 per M4 acceptance)
    overall_f1 = sum(m.f1 for m in metrics_list) / len(metrics_list) if metrics_list else 0.0
    return 0 if overall_f1 >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
