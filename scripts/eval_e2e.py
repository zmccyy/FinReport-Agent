#!/usr/bin/env python3
"""M6.08 端到端全量评估（3 份基准年报缩减版，spec §7.4）。

以 HTTP 客户端身份驱动真实全链路（无 mock）：注册 → 上传年报 PDF →
MQ 编排（PARSE/EXTRACT×3/CHECK/REPORT，DeepSeek API 真实调用）→ 回读
三表/勾稽/异常/报告产物 → 问答 SSE 流式提问，对齐 spec §7.4.2 指标：

| 指标 | 门槛 | 口径 |
|---|---|---|
| 三表抽取 F1 | ≥ 0.85 | item 名严格相等 + value 相对误差 ≤1%（复用 eval_m2_f1） |
| 勾稽判定准确率 | ≥ 0.95 | API isPass vs 生产规则跑 GT 数值的期望 verdict |
| 问答关键事实命中率 | ≥ 0.85 | 答案去空格/逗号含任一 key_fact；forbid_words 判未命中 |
| 端到端耗时 P95 | ≤ 5 min | 上传到任务终态 COMPLETED 的墙钟时间 |
| 问答首 token | < 15 s | SSE 首个生成事件（thought，M5 口径） |
| 报告产物完整性 | 5 类 GENERATED | PDF/MARKDOWN/CHART_PIE/LINE/BAR + PDF 魔数 |
| 报告/问答质量 | ≥ 4.0 | --judge manual（默认，AI 代评落盘待填）或 deepseek（LLM-as-judge 入口） |
| 异常召回 | ≥ 0.80 | 需专家标注异常清单——本次描述性输出、不计分 |

用法（先启动 dev compose 栈并配置 LLM_API_KEY）::

    python scripts/eval_e2e.py                          # 3 家全量
    python scripts/eval_e2e.py --companies 600519       # 单家冒烟
    python scripts/eval_e2e.py --skip-qa                # 跳过问答

退出码：0 自动化指标全达标；1 有公司链路失败或指标未达标。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "ai-service"))
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

try:
    import requests
except ImportError:
    print("ERROR: 需要 requests：pip install requests", file=sys.stderr)
    sys.exit(1)

# 复用既有实现（单一来源）：
# - eval_m2_f1：F1 计算（normalize + 1% 相对误差 + 粒度过滤）
# - e2e_m4：MinIO 预签名 URL 的 Host 头改写（SigV4 签名必须保留原始 netloc）
from eval_m2_f1 import (  # noqa: E402
    StatementItem,
    _compute_metrics,
    _filter_to_truth_granularity,
    _items_from_list,
    _report_granularity_coverage,
)
from e2e_m4 import _rewrite_internal_host  # noqa: E402
from app.modules.reasoner.accounting_rules import (  # noqa: E402
    BalanceSheetIdentityRule,
    CashFlowVsNetIncomeRule,
    NetIncomeToRetainedEarningsRule,
)
from app.schemas.reasoning import StatementSnapshot  # noqa: E402
from app.schemas.statement import StatementType  # noqa: E402

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
REQUIRED_ARTIFACTS = ["PDF", "MARKDOWN", "CHART_PIE", "CHART_LINE", "CHART_BAR"]
STATEMENT_KEYS = {
    "balance_sheet": "balanceSheet",
    "income_statement": "incomeStatement",
    "cash_flow": "cashFlow",
}
_RULE_CLASSES = (
    BalanceSheetIdentityRule,
    NetIncomeToRetainedEarningsRule,
    CashFlowVsNetIncomeRule,
)
ST_MAP = {
    "balance_sheet": StatementType.BALANCE_SHEET,
    "income_statement": StatementType.INCOME_STATEMENT,
    "cash_flow": StatementType.CASH_FLOW,
}

# 3 份基准年报（M6.08 缩减口径；GT 由 scripts/rebuild_gt.py 从 PDF 文本层重建）。
COMPANIES = [
    {
        "code": "000001", "name": "平安银行",
        "pdf": "000001_平安银行_2025年年度报告.pdf",
        "gt": "pingan_2025.json", "qa": "qa_questions_000001.json",
        "report_period": "2025-12-31",
    },
    {
        "code": "300750", "name": "宁德时代",
        "pdf": "300750_宁德时代：2025年年度报告.pdf",
        "gt": "catl_2025.json", "qa": "qa_questions_300750.json",
        "report_period": "2025-12-31",
    },
    {
        "code": "600519", "name": "贵州茅台",
        "pdf": "600519_贵州茅台_2025年年度报告.pdf",
        "gt": "moutai_2025.json", "qa": "qa_questions.json",
        "report_period": "2025-12-31",
    },
]

# 问答质量评分 rubric（--judge deepseek 模式；manual 模式由 AI 代评填入）。
REPORT_RUBRIC = (
    "你是财报解析系统的报告质量评审员。对以下自动生成的财报解析报告按 1-5 分评分："
    "结构完整性（公司概况/三表解读/勾稽结论/异常提示/总结，各部分是否齐备）、"
    "数值一致性（报告引用的数值是否前后一致、无自相矛盾）、可读性（表述是否通顺、"
    "逻辑是否清晰）。只输出 JSON：{\"score\": 数字1-5, \"reason\": \"评分依据，引用具体证据\"}。"
)
QA_RUBRIC = (
    "你是问答系统答案质量评审员。对以下针对财报年报的问答按 1-5 分评分："
    "相关性（是否正面回答了问题）、可靠性（数值/事实是否明确而非含糊其辞）、"
    "可读性。只输出 JSON：{\"score\": 数字1-5, \"reason\": \"评分依据\"}。"
)


class EvalError(RuntimeError):
    """单公司评估链路失败。"""


def _norm_text(value: Any) -> str:
    """命中判定口径（M5）：去空格与逗号（半角/全角）。"""
    return re.sub(r"[\s,，]", "", str(value))


def _p95(values: list[float]) -> float:
    """最近邻秩 P95；小样本下近似最大值。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _request_with_retry(method: str, url: str, max_tries: int = 5, **kwargs: Any):
    """429 感知请求：按 Retry-After 等待后重试（M6.03 滑动窗口限流）。"""
    resp = None
    for attempt in range(max_tries):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code != 429:
            return resp
        retry_after = float(resp.headers.get("Retry-After") or 5)
        print(f"  [ratelimit] 429（{url}），按 Retry-After 等待 {retry_after:.0f}s")
        time.sleep(retry_after + 0.5)
    return resp


def _register_user(base_url: str) -> tuple[str, str]:
    username = f"e2eeval_{int(time.time() * 1000) % 10**10}"
    resp = _request_with_retry(
        "POST", f"{base_url}/api/v1/auth/register",
        json={"username": username, "password": "e2eeval_pass_123",
              "email": f"{username}@test.com"},
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        raise EvalError(f"注册失败 HTTP {resp.status_code}: {resp.text[:200]}")
    token = resp.json().get("accessToken")
    if not token:
        raise EvalError(f"注册响应缺少 accessToken: {resp.text[:200]}")
    return username, token


def _upload_pdf(base_url: str, token: str, pdf_path: Path, company: dict) -> tuple[str, int]:
    with pdf_path.open("rb") as fh:
        resp = _request_with_retry(
            "POST", f"{base_url}/api/v1/reports/upload",
            headers={"Authorization": f"Bearer {token}",
                     "X-Trace-Id": f"eval-e2e-{company['code']}-{int(time.time())}"},
            files={"file": (pdf_path.name, fh, "application/pdf")},
            data={"companyCode": company["code"], "companyName": company["name"],
                  "reportType": "ANNUAL", "reportPeriod": company["report_period"]},
            timeout=120,
        )
    if resp.status_code not in (200, 201):
        raise EvalError(f"上传失败 HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if not body.get("taskId") or not body.get("reportId"):
        raise EvalError(f"上传响应缺少 taskId/reportId: {body}")
    return str(body["taskId"]), int(body["reportId"])


def _wait_task(base_url: str, token: str, task_id: str, timeout: int,
               interval: float) -> tuple[dict[str, Any], float]:
    """轮询任务终态，返回 (task, 上传→终态的墙钟秒数)。"""
    started = time.monotonic()
    deadline = started + timeout
    last_status = ""
    consecutive_errors = 0
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{base_url}/api/v1/tasks/{task_id}",
                                headers={"Authorization": f"Bearer {token}"}, timeout=30)
        except requests.RequestException as error:
            consecutive_errors += 1
            if consecutive_errors > 20:
                raise EvalError(f"查询任务连续网络失败: {error}") from error
            time.sleep(interval)
            continue
        consecutive_errors = 0
        if resp.status_code != 200:
            raise EvalError(f"查询任务失败 HTTP {resp.status_code}: {resp.text[:200]}")
        task = resp.json()
        status = task.get("status", "")
        if status != last_status:
            print(f"  [task] {status} ({time.monotonic() - started:.0f}s)")
            last_status = status
        if status in TERMINAL_STATUSES:
            return task, time.monotonic() - started
        time.sleep(interval)
    raise EvalError(f"任务 {task_id} 在 {timeout}s 内未达终态（最后状态 {last_status}）")


def _get_json(base_url: str, token: str, path: str) -> Any:
    resp = _request_with_retry(
        "GET", f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    if resp.status_code != 200:
        raise EvalError(f"GET {path} 失败 HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _fetch_statements(base_url: str, token: str, report_id: int) -> dict[str, list[StatementItem]]:
    body = _get_json(base_url, token, f"/api/v1/reports/{report_id}/statements")
    result: dict[str, list[StatementItem]] = {}
    for st_type, key in STATEMENT_KEYS.items():
        items: list[StatementItem] = []
        for row in body.get(key) or []:
            name = str(row.get("itemName") or "").strip()
            value = row.get("itemValue")
            if not name or value is None:
                continue
            items.append(StatementItem(
                item=name, value=float(value),
                scope=str(row.get("scope") or ""),
                period=str(row.get("periodType") or ""),
            ))
        result[st_type] = items
    return result


def _expected_check_results(ground_truth: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """勾稽预期结果：生产规则类跑 GT 数值（GT 来自 PDF 文本层，与抽取链路独立）。"""
    statements = {
        st_map: {r["item"]: Decimal(str(r["value"])) for r in rows}
        for st, rows in ground_truth.get("statements", {}).items()
        for st_map in [ST_MAP[st]]
    }
    snap = StatementSnapshot(
        report_period=str(ground_truth.get("report_period") or "2025-12-31"),
        currency=str(ground_truth.get("currency") or "CNY"),
        unit=str(ground_truth.get("unit") or "元"),
        statements=statements,
    )
    out: dict[str, dict[str, Any]] = {}
    for rule_cls in _RULE_CLASSES:
        result = rule_cls().check(snap)
        out[result.rule_type.value] = {
            "rule_name": result.rule_name, "is_pass": result.is_pass,
            "diff": str(result.diff), "tolerance": str(result.tolerance),
            "severity": result.severity.value, "missing_items": result.missing_items,
        }
    return out


def _verify_artifacts(base_url: str, token: str, report_id: int,
                      minio_public_url: str, artifacts_dir: Path) -> dict[str, Any]:
    """五类产物 GENERATED 断言 + PDF 预签名下载魔数校验 + Markdown 落盘。"""
    artifacts = _get_json(base_url, token, f"/api/v1/reports/{report_id}/artifacts")
    by_type = {a.get("artifactType"): a for a in artifacts}
    missing = [t for t in REQUIRED_ARTIFACTS if t not in by_type]
    not_generated = [t for t in REQUIRED_ARTIFACTS
                     if t in by_type and by_type[t].get("status") != "GENERATED"]
    if missing or not_generated:
        raise EvalError(f"产物不完整: missing={missing} not_generated={not_generated}")
    pdf_url = by_type["PDF"].get("downloadUrl")
    if not pdf_url:
        raise EvalError("PDF 产物缺少 downloadUrl")
    pdf_url, host_headers = _rewrite_internal_host(pdf_url, minio_public_url)
    pdf_resp = requests.get(pdf_url, headers=host_headers, timeout=60)
    if pdf_resp.status_code != 200 or pdf_resp.content[:5] != b"%PDF-":
        raise EvalError(f"PDF 产物下载/魔数校验失败 HTTP {pdf_resp.status_code}")
    md_url = by_type["MARKDOWN"].get("downloadUrl")
    markdown_text = ""
    if md_url:
        md_url, md_headers = _rewrite_internal_host(md_url, minio_public_url)
        md_resp = requests.get(md_url, headers=md_headers, timeout=60)
        if md_resp.status_code == 200:
            markdown_text = md_resp.text
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "report.md").write_text(markdown_text, encoding="utf-8")
    (artifacts_dir / "artifacts.json").write_text(
        json.dumps(artifacts, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"complete": True, "markdown_chars": len(markdown_text),
            "pdf_bytes": len(pdf_resp.content)}


def _expected_from_direction(direction: str | None, answer: str) -> bool:
    if direction == "down":
        return any(w in answer for w in ("下降", "减少", "下滑", "负增长", "-", "降低"))
    if direction == "up":
        return any(w in answer for w in ("上升", "增长", "增加", "提升", "+"))
    return True


def _ask_question(base_url: str, token: str, session_id: str, question: str,
                  timeout: float = 180.0) -> dict[str, Any]:
    """POST SSE 问答：M5 口径首 token = 首个生成事件（thought）；累计 token.content。"""
    started = time.monotonic()
    first_event_s: float | None = None
    answer_parts: list[str] = []
    tool_calls: list[str] = []
    finished_reason = ""
    error_message = ""
    resp = _request_with_retry(
        "POST", f"{base_url}/api/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}",
                 "Idempotency-Key": str(uuid.uuid4())},
        json={"content": question}, stream=True, timeout=timeout,
    )
    if resp.status_code != 200:
        raise EvalError(f"问答请求失败 HTTP {resp.status_code}: {resp.text[:200]}")
    current_event = ""
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        if raw_line.startswith("event:"):
            current_event = raw_line[len("event:"):].strip()
            continue
        if not raw_line.startswith("data:"):
            continue
        data = raw_line[len("data:"):].strip()
        if not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if first_event_s is None and current_event in ("thought", "token"):
            first_event_s = time.monotonic() - started
        if current_event == "token":
            answer_parts.append(str(payload.get("content") or ""))
        elif current_event == "tool_call":
            tool_calls.append(str(payload.get("toolName") or payload.get("name") or "tool"))
        elif current_event == "done":
            finished_reason = str(payload.get("finishedReason") or "")
            break
        elif current_event == "error":
            error_message = str(payload.get("message") or payload)
            break
    total_s = time.monotonic() - started
    return {"answer": "".join(answer_parts), "first_event_s": first_event_s,
            "total_s": total_s, "tools": tool_calls,
            "finished_reason": finished_reason, "error": error_message}


def _judge_qa_answer(q: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """命中判定（M5 口径）：去空格/逗号含任一 key_fact；forbid_words 判未命中；
    direction 需方向词一致。"""
    answer_norm = _norm_text(result["answer"])
    matched = next((f for f in q.get("key_facts", []) if _norm_text(f) in answer_norm), None)
    forbidden = [w for w in q.get("forbid_words", []) if _norm_text(w) in answer_norm]
    direction_ok = _expected_from_direction(q.get("direction"), result["answer"])
    hit = matched is not None and not forbidden and direction_ok
    return {"hit": hit, "matched_fact": matched, "forbidden": forbidden,
            "direction_ok": direction_ok}


def _run_qa(base_url: str, token: str, report_id: int, qa_path: Path,
            artifacts_dir: Path) -> dict[str, Any]:
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    resp = _request_with_retry(
        "POST", f"{base_url}/api/v1/chat/sessions",
        headers={"Authorization": f"Bearer {token}",
                 "Idempotency-Key": str(uuid.uuid4())},  # M6.04 写操作幂等强制
        json={"reportId": report_id, "title": f"eval-{qa_path.stem}"},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise EvalError(f"创建会话失败 HTTP {resp.status_code}: {resp.text[:200]}")
    session_id = str(resp.json().get("id") or "")
    if not session_id:
        raise EvalError(f"创建会话响应缺少 id: {resp.text[:200]}")
    results: list[dict[str, Any]] = []
    for q in qa.get("questions", []):
        result = _ask_question(base_url, token, session_id, q["question"])
        verdict = _judge_qa_answer(q, result)
        results.append({"id": q["id"], "question": q["question"],
                        "capability": q.get("capability"), **verdict, **result})
        print(f"  [qa] {q['id']}: hit={verdict['hit']} "
              f"first_event={result['first_event_s'] and round(result['first_event_s'], 2)}s "
              f"total={result['total_s']:.1f}s tools={len(result['tools'])}")
        time.sleep(1.0)
    acceptance = qa.get("acceptance", {})
    hits = sum(1 for r in results if r["hit"])
    hit_rate = hits / len(results) if results else 0.0
    first_events = [r["first_event_s"] for r in results if r["first_event_s"] is not None]
    summary = {"session_id": session_id, "hit_rate": hit_rate, "hits": hits,
               "total": len(results), "hit_rate_min": acceptance.get("hit_rate_min", 0.85),
               "first_event_p95_s": _p95(first_events),
               "first_token_max_s": acceptance.get("first_token_seconds_max", 15.0)}
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "qa_transcript.json").write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return summary


def _judge_deepseek(kind: str, content: str) -> dict[str, Any]:
    """LLM-as-judge 入口（--judge deepseek）：DeepSeek API json_mode 打分。

    本次开发期评估默认 --judge manual（AI 代评），该入口保留供未来一键切换；
    依赖 deploy/.env 注入的 LLM_API_KEY / LLM_API_BASE_URL / LLM_API_MODEL。
    """
    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        return {"error": "LLM_API_KEY 未设置（需 source deploy/.env）"}
    base = os.environ.get("LLM_API_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("LLM_API_MODEL", "deepseek-chat")
    rubric = REPORT_RUBRIC if kind == "report" else QA_RUBRIC
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "temperature": 0.1,
              "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": rubric},
                           {"role": "user", "content": content[:8000]}]},
        timeout=180,
    )
    if resp.status_code != 200:
        return {"error": f"judge HTTP {resp.status_code}: {resp.text[:200]}"}
    try:
        return json.loads(resp.json()["choices"][0]["message"]["content"])
    except (KeyError, json.JSONDecodeError) as error:
        return {"error": f"judge 响应解析失败: {error}"}


def _scrape_llm_metrics(ai_service_url: str) -> dict[str, float]:
    """抓取 L3 /metrics 的 LLM 计数器（成本记录用）；不可用时返回空。"""
    out: dict[str, float] = {}
    try:
        text = requests.get(f"{ai_service_url}/metrics", timeout=10).text
    except requests.RequestException:
        return out
    for name in ("llm_calls_total", "llm_tokens_total", "llm_retries_total"):
        total = 0.0
        for line in text.splitlines():
            if line.startswith(name) and not line.startswith("#"):
                try:
                    total += float(line.rsplit(" ", 1)[1])
                except (ValueError, IndexError):
                    continue
        out[name] = total
    return out


def _eval_company(company: dict, args: argparse.Namespace) -> dict[str, Any]:
    gt_path = args.ground_truth_dir / company["gt"]
    ground_truth = json.loads(gt_path.read_text(encoding="utf-8"))
    truth: dict[str, list[StatementItem]] = {
        st: _items_from_list(items)
        for st, items in ground_truth.get("statements", {}).items()
    }
    record: dict[str, Any] = {"company": company["name"], "code": company["code"],
                              "unit": ground_truth.get("unit"), "failures": []}

    print(f"\n========== {company['name']}（{company['code']}） ==========")
    print("▶ 注册 / 上传")
    username, token = _register_user(args.backend_url)
    pdf_path = args.reports_dir / company["pdf"]
    task_id, report_id = _upload_pdf(args.backend_url, token, pdf_path, company)
    record.update({"username": username, "task_id": task_id, "report_id": report_id})
    print(f"  taskId={task_id} reportId={report_id}")

    print("▶ 等待任务终态（真实 DeepSeek API 全链路）")
    task, elapsed = _wait_task(args.backend_url, token, task_id, args.timeout, args.poll_interval)
    record["e2e_seconds"] = round(elapsed, 1)
    if task.get("status") != "COMPLETED":
        raise EvalError(f"任务终态 {task.get('status')}: {str(task.get('errorMsg'))[:300]}")
    print(f"  端到端耗时 {elapsed:.0f}s")

    print("▶ 三表抽取 F1")
    predicted = _fetch_statements(args.backend_url, token, report_id)
    _report_granularity_coverage(predicted)
    predicted = _filter_to_truth_granularity(predicted, truth)
    metrics_list = [_compute_metrics(st, predicted.get(st, []), truth.get(st, []))
                    for st in STATEMENT_KEYS]
    overall_f1 = sum(m.f1 for m in metrics_list) / len(metrics_list)
    record["f1"] = {"overall": round(overall_f1, 4), "statements": [
        {"type": m.statement_type, "precision": round(m.precision, 4),
         "recall": round(m.recall, 4), "f1": round(m.f1, 4),
         "tp": m.tp, "fp": m.fp, "fn": m.fn,
         "unmatched_predicted": m.unmatched_predicted[:8],
         "unmatched_truth": m.unmatched_ground_truth[:8]} for m in metrics_list]}
    print(f"  Overall F1 = {overall_f1:.4f}")

    print("▶ 勾稽判定（期望=生产规则跑 GT）")
    expected = _expected_check_results(ground_truth)
    checks = _get_json(args.backend_url, token, f"/api/v1/reports/{report_id}/checks")
    actual_by_rule = {c.get("ruleType"): c for c in checks}
    check_rows: list[dict[str, Any]] = []
    matches = 0
    for rule_type, exp in expected.items():
        actual = actual_by_rule.get(rule_type)
        actual_pass = actual.get("isPass") if actual else None
        match = actual_pass is not None and bool(actual_pass) == bool(exp["is_pass"])
        matches += int(match)
        check_rows.append({"rule_type": rule_type, "rule_name": exp["rule_name"],
                           "expected_pass": exp["is_pass"], "actual_pass": actual_pass,
                           "match": match, "expected_diff": exp["diff"],
                           "actual_diff": str(actual.get("diff")) if actual else None,
                           "severity": exp["severity"]})
        print(f"  [check] {rule_type}: expected_pass={exp['is_pass']} "
              f"actual_pass={actual_pass} match={match}")
    record["checks"] = {"rows": check_rows, "matches": matches, "total": len(expected)}

    print("▶ 异常检测（描述性输出）")
    anomalies = _get_json(args.backend_url, token, f"/api/v1/reports/{report_id}/anomalies")
    by_type: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    for a in anomalies:
        by_type[a.get("anomalyType")] = by_type.get(a.get("anomalyType"), 0) + 1
        by_sev[a.get("severity")] = by_sev.get(a.get("severity"), 0) + 1
    record["anomalies"] = {"count": len(anomalies), "by_type": by_type,
                           "by_severity": by_sev, "items": anomalies[:10]}
    print(f"  异常 {len(anomalies)} 条：type={by_type} severity={by_sev}")

    print("▶ 报告产物完整性")
    company_dir = args.artifacts_dir / f"{company['code']}_{company['name']}"
    artifacts = _verify_artifacts(args.backend_url, token, report_id,
                                  args.minio_public_url, company_dir)
    record["artifacts"] = artifacts
    print(f"  5 类产物齐备，PDF {artifacts['pdf_bytes']} bytes，"
          f"Markdown {artifacts['markdown_chars']} chars")

    if args.skip_qa:
        record["qa"] = None
        print("▶ 问答（--skip-qa 跳过）")
    else:
        print("▶ 问答关键事实命中")
        qa_path = args.qa_dir / company["qa"]
        record["qa"] = _run_qa(args.backend_url, token, report_id, qa_path, company_dir)

    if args.judge == "deepseek" and not args.skip_qa:
        print("▶ LLM-as-judge（DeepSeek 入口）")
        report_md = (company_dir / "report.md")
        record["judge_report"] = _judge_deepseek(
            "report", report_md.read_text(encoding="utf-8")) if report_md.is_file() else {"error": "report.md 缺失"}
        transcript = (company_dir / "qa_transcript.json")
        if transcript.is_file():
            qa_data = json.loads(transcript.read_text(encoding="utf-8"))
            scores = [_judge_deepseek("qa", f"问题：{r['question']}\n答案：{r['answer']}")
                      for r in qa_data["results"]]
            record["judge_qa"] = scores
        else:
            record["judge_qa"] = []
    return record


def _render_report(records: list[dict[str, Any]], args: argparse.Namespace,
                   llm_delta: dict[str, float], generated_at: str) -> str:
    valid = [r for r in records if "failures" not in r or not r["failures"]]
    f1_overall = sum(r["f1"]["overall"] for r in valid) / len(valid) if valid else 0.0
    check_total = sum(r["checks"]["total"] for r in valid)
    check_matches = sum(r["checks"]["matches"] for r in valid)
    check_acc = check_matches / check_total if check_total else 0.0
    qa_records = [r["qa"] for r in valid if r.get("qa")]
    qa_hits = sum(r["hits"] for r in qa_records)
    qa_total = sum(r["total"] for r in qa_records)
    qa_hit_rate = qa_hits / qa_total if qa_total else 0.0
    qa_first_p95 = _p95([r["first_event_p95_s"] for r in qa_records])
    e2e_p95 = _p95([r["e2e_seconds"] for r in valid]) / 60.0
    artifacts_ok = all(r["artifacts"]["complete"] for r in valid) and bool(valid)

    lines = [
        "# M6.08 端到端评估报告（3 份基准年报缩减版）",
        "",
        f"> 生成时间：{generated_at}",
        f"> 评估脚本：`scripts/eval_e2e.py`（真实全链路，无 mock）",
        f"> 基准：{len(records)} 份 A 股 2025 年报（平安银行/宁德时代/贵州茅台）——"
        f"M6.08 缩减口径（原 30 份，见决策记录）",
        f"> GT：`data/benchmark/ground_truth/`（scripts/rebuild_gt.py 从 PDF 文本层全量重建）",
        "",
        "## §7.4.2 指标总表",
        "",
        "| 指标 | 目标 | 实测 | 达标 | 口径 |",
        "|---|---|---|---|---|",
        f"| 三表抽取 F1（3 家宏平均） | ≥ 0.85 | **{f1_overall:.4f}** | "
        f"{'✅' if f1_overall >= 0.85 else '❌'} | item 名严格相等 + value 相对误差 ≤1%，"
        f"GT 粒度（合并+本期） |",
        f"| 勾稽判定准确率 | ≥ 0.95 | **{check_acc:.4f}**（{check_matches}/{check_total}） | "
        f"{'✅' if check_acc >= 0.95 else '❌'} | API isPass vs 生产规则跑 GT 数值期望 |",
        f"| 问答关键事实命中率 | ≥ 0.85 | **{qa_hit_rate:.4f}**（{qa_hits}/{qa_total}） | "
        f"{'✅' if qa_hit_rate >= 0.85 else '❌'} | 答案去空格/逗号含任一 key_fact；forbid_words 判未命中 |",
        f"| 问答首 token P95 | < 15 s | {qa_first_p95:.2f} s | "
        f"{'✅' if qa_first_p95 < 15 else '❌'} | SSE 首个生成事件（thought，M5 口径） |",
        f"| 端到端耗时 P95 | ≤ 5 min | **{e2e_p95:.2f} min** | "
        f"{'✅' if e2e_p95 <= 5 else '❌'} | 上传 → 任务终态 COMPLETED 墙钟 |",
        f"| 报告产物完整性 | 5 类 GENERATED | {'3/3 家齐备' if artifacts_ok else '不完整'} | "
        f"{'✅' if artifacts_ok else '❌'} | PDF/MARKDOWN/CHART_PIE/LINE/BAR + PDF 魔数 |",
        "| 报告质量（人工） | ≥ 4.0 | 待人工（AI 代评见下） | ⏳ | "
        f"judge 模式={args.judge}：manual=AI 代评；deepseek=LLM-as-judge 入口 |",
        "| 异常召回 | ≥ 0.80 | 待人工标注，不计分 | ⏳ | 需专家标注异常清单；本次输出分布 |",
        "",
    ]

    lines += ["## 分公司明细", ""]
    for r in records:
        lines += [f"### {r['company']}（{r['code']}，单位：{r.get('unit')}）", ""]
        if r.get("failures"):
            lines += [f"- ❌ 链路失败：{r['failures']}", ""]
            continue
        lines += [
            f"- taskId=`{r['task_id']}` reportId={r['report_id']} 用户 `{r['username']}`",
            f"- 端到端耗时：{r['e2e_seconds']}s",
            "",
            "#### 三表 F1",
            "",
            "| 表 | Precision | Recall | F1 | TP | FP | FN |",
            "|---|---|---|---|---|---|---|",
        ]
        for st in r["f1"]["statements"]:
            lines.append(f"| {st['type']} | {st['precision']:.4f} | {st['recall']:.4f} | "
                         f"**{st['f1']:.4f}** | {st['tp']} | {st['fp']} | {st['fn']} |")
        lines += [f"| **overall** | | | **{r['f1']['overall']:.4f}** | | | |", ""]
        for st in r["f1"]["statements"]:
            if st["unmatched_predicted"] or st["unmatched_truth"]:
                lines += [f"- `{st['type']}` 预测多余（FP 前 8）：{st['unmatched_predicted']}",
                          f"- `{st['type']}` GT 漏抽（FN 前 8）：{st['unmatched_truth']}"]
        lines += [""]
        lines += ["#### 勾稽对照（期望 = 生产规则跑 GT 数值）", "",
                  "| 规则 | 期望 isPass | 实际 isPass | 判定一致 | 期望 diff | 实际 diff |",
                  "|---|---|---|---|---|---|"]
        for c in r["checks"]["rows"]:
            lines.append(f"| {c['rule_name']}（{c['rule_type']}） | {c['expected_pass']} | "
                         f"{c['actual_pass']} | {'✅' if c['match'] else '❌'} | "
                         f"{c['expected_diff']} | {c['actual_diff']} |")
        lines += [""]
        lines += [f"#### 异常检测（描述性，{r['anomalies']['count']} 条）", ""]
        if r["anomalies"]["count"]:
            lines += [f"- 类型分布：{r['anomalies']['by_type']}",
                      f"- 级别分布：{r['anomalies']['by_severity']}"]
            for a in r["anomalies"]["items"][:5]:
                lines.append(f"- [{a.get('severity')}] {a.get('itemName')} "
                             f"（{a.get('anomalyType')}）：{str(a.get('description'))[:80]}")
        else:
            lines += ["- 无异常检出（单期上传下属正常输出）"]
        lines += [""]
        qa = r.get("qa")
        if qa:
            lines += [f"#### 问答（会话 `{qa['session_id']}`）", "",
                      f"- 命中率：{qa['hits']}/{qa['total']} = **{qa['hit_rate']:.2f}**"
                      f"（门槛 {qa['hit_rate_min']}）；首事件 P95 {qa['first_event_p95_s']:.2f}s"
                      f"（门槛 {qa['first_token_max_s']}s）",
                      "- 逐问明细见 `docs/eval/artifacts/` 下 qa_transcript.json", ""]
        else:
            lines += ["#### 问答", "", "- --skip-qa 跳过", ""]
        if r.get("judge_report") or r.get("judge_qa"):
            lines += ["#### LLM-as-judge（DeepSeek 入口）", "",
                      f"- 报告评分：{r.get('judge_report')}",
                      f"- 问答评分：{r.get('judge_qa')}", ""]

    lines += ["## AI 代评（报告质量 / 问答相关性）", "",
              f"> judge 模式：`{args.judge}`（manual = 开发期由 AI 代评；deepseek 入口保留，"
              "经 `--judge deepseek` 启用）。以下评分栏由 AI 代评填写：", ""]
    for r in records:
        if r.get("failures"):
            continue
        lines += [f"### {r['company']}", "",
                  "| 维度 | 报告质量评分 | 问答相关性评分 | 评分依据 |",
                  "|---|---|---|---|",
                  f"| 分数（1-5） | 待填 | 待填 | 依据产物 `docs/eval/artifacts/` |", ""]

    lines += [
        "## 环境与成本",
        "",
        f"- backend：{args.backend_url}；MinIO（宿主可达）：{args.minio_public_url}",
        f"- LLM：DeepSeek API（被测系统内部调用，deploy/.env 注入 LLM_API_KEY）",
        f"- 本次运行 L3 计数器增量：calls={llm_delta.get('llm_calls_total', 0):.0f}，"
        f"tokens={llm_delta.get('llm_tokens_total', 0):.0f}，"
        f"retries={llm_delta.get('llm_retries_total', 0):.0f}"
        f"（{'成本参考：单份年报约 ¥0.5-1，AGENTS.md §8.1' if llm_delta else 'metrics 抓取不可用'}）",
        "",
        "## 口径说明",
        "",
        "- M6.08 缩减口径：3 份年报（10 行业×3 公司中已有的 3 家）替代 30 份；"
        "问答基准 5 问/家由 GT 数值与 PDF 文本机械推导，无需金融知识",
        "- GT 局限：regex/文本层自动重建、未人工逐项核对；平安银行单位为百万元、"
        "宁德时代为千元（GT 与抽取链路同源文档单位，validator 允许 元/万元/百万元/千元）",
        "- 勾稽期望 = 生产规则类跑 GT 数值；规则 2/3 为生产既定启发式（WARN 级），"
        "对真实年报预期即 FAIL，口径为「管线 verdict 与 GT 期望是否一致」而非「规则须全过」",
        "- 异常召回需专家标注异常清单，本次不计分；报告质量/问答相关性人工评分由 AI 代评，"
        "可经 `--judge deepseek` 切换为 LLM-as-judge",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="M6.08 端到端全量评估（3 份缩减版）")
    parser.add_argument("--backend-url", default="http://localhost:8080")
    parser.add_argument("--ai-service-url", default="http://localhost:8000")
    parser.add_argument("--minio-public-url", default="http://localhost:9000")
    parser.add_argument("--reports-dir", type=Path,
                        default=REPOSITORY_ROOT / "data" / "sample_reports")
    parser.add_argument("--ground-truth-dir", type=Path,
                        default=REPOSITORY_ROOT / "data" / "benchmark" / "ground_truth")
    parser.add_argument("--qa-dir", type=Path,
                        default=REPOSITORY_ROOT / "data" / "benchmark")
    parser.add_argument("--artifacts-dir", type=Path, default=None,
                        help="产物落盘目录（默认 docs/eval/artifacts/{run_ts}）")
    parser.add_argument("--companies", default="000001,300750,600519",
                        help="逗号分隔的公司代码子集（冒烟用单家）")
    parser.add_argument("--timeout", type=int, default=1500,
                        help="单公司任务终态等待上限秒数（默认 1500）")
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--skip-qa", action="store_true")
    parser.add_argument("--judge", choices=["manual", "deepseek"], default="manual",
                        help="质量评分模式：manual=AI 代评（默认）；deepseek=LLM-as-judge 入口")
    parser.add_argument("--output", type=Path,
                        default=REPOSITORY_ROOT / "docs" / "eval" / "m6-e2e-3reports.md")
    args = parser.parse_args()

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.artifacts_dir is None:
        args.artifacts_dir = REPOSITORY_ROOT / "docs" / "eval" / "artifacts" / run_ts
    args.artifacts_dir.mkdir(parents=True, exist_ok=True)

    selected = [c for c in COMPANIES if c["code"] in args.companies.split(",")]
    if not selected:
        print(f"ERROR: --companies 未匹配任何公司: {args.companies}", file=sys.stderr)
        return 1

    llm_before = _scrape_llm_metrics(args.ai_service_url)
    records: list[dict[str, Any]] = []
    for company in selected:
        try:
            records.append(_eval_company(company, args))
        except EvalError as error:
            print(f"  ✗ {company['name']} 评估失败: {error}", file=sys.stderr)
            records.append({"company": company["name"], "code": company["code"],
                            "failures": [str(error)]})
    llm_after = _scrape_llm_metrics(args.ai_service_url)
    llm_delta = {k: round(llm_after.get(k, 0) - llm_before.get(k, 0), 0)
                 for k in llm_after} if llm_before and llm_after else {}

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (args.artifacts_dir / "run_summary.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    report = _render_report(records, args, llm_delta, generated_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"\n评估报告：{args.output}")
    print(f"产物目录：{args.artifacts_dir}")

    # 退出码 = 自动化指标是否全达标（人工类指标不阻塞）
    valid = [r for r in records if not r.get("failures")]
    if len(valid) != len(records):
        return 1
    f1_overall = sum(r["f1"]["overall"] for r in valid) / len(valid) if valid else 0.0
    check_total = sum(r["checks"]["total"] for r in valid)
    check_acc = sum(r["checks"]["matches"] for r in valid) / check_total if check_total else 0.0
    qa_records = [r["qa"] for r in valid if r.get("qa")]
    qa_total = sum(r["total"] for r in qa_records)
    qa_hit = sum(r["hits"] for r in qa_records) / qa_total if qa_total else 1.0
    e2e_p95_min = _p95([r["e2e_seconds"] for r in valid]) / 60.0 if valid else 99.0
    artifacts_ok = all(r["artifacts"]["complete"] for r in valid)
    passed = (f1_overall >= 0.85 and check_acc >= 0.95
              and (args.skip_qa or qa_hit >= 0.85)
              and e2e_p95_min <= 5.0 and artifacts_ok)
    print(f"\n{'✅ 全量评估达标' if passed else '❌ 存在未达标指标'} "
          f"(F1={f1_overall:.4f} check={check_acc:.4f} qa={'skip' if args.skip_qa else f'{qa_hit:.4f}'} "
          f"p95={e2e_p95_min:.2f}min artifacts={artifacts_ok})")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
