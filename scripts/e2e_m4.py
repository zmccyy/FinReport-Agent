#!/usr/bin/env python3
"""M4.10 真实 PDF 端到端验收 — L2 API 驱动全链路（无 mock）。

链路覆盖（M4 阶段验收标准第 1 条）：

    注册/登录 → 上传真实年报 PDF → MQ 任务编排
    → PARSE（PP-Structure 表格识别）
    → EXTRACT ×3（DeepSeek API json_mode 真实抽取）
    → CHECK（RuleEngine + AnomalyDetector + LLM 复核）
    → REPORT（报告 Markdown + 3 图表 + PDF）
    → 三表/勾稽/异常/产物四组查询端点断言

使用方式（先启动 dev compose 栈并配置 LLM_API_KEY）::

    python scripts/e2e_m4.py
    python scripts/e2e_m4.py --pdf data/sample_reports/600519_贵州茅台_2025年年度报告.pdf

退出码：0 全链路通过；1 任一环节失败（附失败环节明细）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

try:
    import requests
except ImportError:
    print("ERROR: 需要 requests：pip install requests", file=sys.stderr)
    sys.exit(1)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF = REPOSITORY_ROOT / "data" / "sample_reports" / "600519_贵州茅台_2025年年度报告.pdf"

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
REQUIRED_ARTIFACTS = {"PDF", "MARKDOWN", "CHART_PIE", "CHART_LINE", "CHART_BAR"}
STATEMENT_KEYS = {
    "balance_sheet": "balanceSheet",
    "income_statement": "incomeStatement",
    "cash_flow": "cashFlow",
}


class E2EError(RuntimeError):
    """One failed verification step with its human-readable context."""


def _register_and_login(base_url: str) -> tuple[str, str]:
    """Register a fresh random user and return (username, accessToken)."""
    username = f"m4e2e_{int(time.time())}"
    resp = requests.post(
        f"{base_url}/api/v1/auth/register",
        json={"username": username, "password": "m4e2e_pass_123",
              "email": f"{username}@test.com"},
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        raise E2EError(f"注册失败 HTTP {resp.status_code}: {resp.text[:200]}")
    token = resp.json().get("accessToken")
    if not token:
        raise E2EError(f"注册响应缺少 accessToken: {resp.text[:200]}")
    return username, token


def _upload_pdf(
    base_url: str,
    token: str,
    pdf_path: Path,
    company_code: str,
    company_name: str,
    report_period: str,
    report_type: str,
) -> tuple[str, int]:
    """Upload the PDF and return (taskId, reportId)."""
    with pdf_path.open("rb") as fh:
        resp = requests.post(
            f"{base_url}/api/v1/reports/upload",
            headers={"Authorization": f"Bearer {token}",
                     "X-Trace-Id": f"m4e2e-{int(time.time())}"},
            files={"file": (pdf_path.name, fh, "application/pdf")},
            data={
                "companyCode": company_code,
                "companyName": company_name,
                "reportType": report_type,
                "reportPeriod": report_period,
            },
            timeout=120,
        )
    if resp.status_code not in (200, 201):
        raise E2EError(f"上传失败 HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    task_id = body.get("taskId")
    report_id = body.get("reportId")
    if not task_id or not report_id:
        raise E2EError(f"上传响应缺少 taskId/reportId: {body}")
    return task_id, int(report_id)


def _wait_task(
    base_url: str, token: str, task_id: str, timeout: int, interval: float
) -> dict[str, Any]:
    """Poll GET /tasks/{id} until a terminal status; return the task body.

    Transient network errors (read timeout during CPU-saturated parse steps)
    are retried until the overall deadline instead of aborting the script.
    """
    deadline = time.monotonic() + timeout
    last_status = ""
    consecutive_errors = 0
    while time.monotonic() < deadline:
        try:
            resp = requests.get(
                f"{base_url}/api/v1/tasks/{task_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        except requests.RequestException as error:
            consecutive_errors += 1
            if consecutive_errors > 20:
                raise E2EError(f"查询任务连续网络失败: {error}") from error
            time.sleep(interval)
            continue
        consecutive_errors = 0
        if resp.status_code != 200:
            raise E2EError(f"查询任务失败 HTTP {resp.status_code}: {resp.text[:200]}")
        task = resp.json()
        status = task.get("status", "")
        if status != last_status:
            print(f"  [task] {status}")
            last_status = status
        if status in TERMINAL_STATUSES:
            return task
        time.sleep(interval)
    raise E2EError(f"任务 {task_id} 在 {timeout}s 内未达终态（最后状态 {last_status}）")


def _get_json_list(
    base_url: str, token: str, path: str
) -> Any:
    """GET a JSON endpoint (object or array) with auth; raise on HTTP error."""
    resp = requests.get(
        f"{base_url}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=30
    )
    if resp.status_code != 200:
        raise E2EError(f"GET {path} 失败 HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _verify_statements(base_url: str, token: str, report_id: int) -> dict[str, int]:
    """Assert all three statement lists are non-empty; return per-type counts."""
    body = _get_json_list(base_url, token, f"/api/v1/reports/{report_id}/statements")
    counts: dict[str, int] = {}
    for st_type, key in STATEMENT_KEYS.items():
        items = body.get(key) or []
        counts[st_type] = len(items)
        if not items:
            raise E2EError(f"{st_type} 抽取结果为空（{key}=[]）")
    # 抽样展示前 3 条，肉眼确认是真实科目而非占位数据。
    for st_type, key in STATEMENT_KEYS.items():
        for item in (body.get(key) or [])[:3]:
            print(
                f"  [statement:{st_type}] {item.get('itemName')} = "
                f"{item.get('itemValue')} scope={item.get('scope')} "
                f"period={item.get('periodType')}"
            )
    return counts


def _verify_checks(base_url: str, token: str, report_id: int) -> int:
    """Assert the accounting-check list is non-empty; print rule outcomes."""
    checks = _get_json_list(base_url, token, f"/api/v1/reports/{report_id}/checks")
    if not isinstance(checks, list) or not checks:
        raise E2EError("勾稽核对结果为空（/checks 返回空列表）")
    for check in checks:
        print(
            f"  [check] {check.get('ruleName')} "
            f"pass={check.get('isPass')} diff={check.get('diff')}"
        )
    return len(checks)


def _verify_anomalies(base_url: str, token: str, report_id: int) -> int:
    """Print anomaly detections; empty list is allowed (no anomaly is valid)."""
    anomalies = _get_json_list(
        base_url, token, f"/api/v1/reports/{report_id}/anomalies"
    )
    if not isinstance(anomalies, list):
        raise E2EError(f"异常检测结果格式异常: {str(anomalies)[:200]}")
    for anomaly in anomalies:
        print(
            f"  [anomaly] {anomaly.get('itemName')} type={anomaly.get('anomalyType')} "
            f"severity={anomaly.get('severity')}"
        )
    if not anomalies:
        print("  [anomaly] 无异常（单期数据下属正常，多期对比留 M5）")
    return len(anomalies)


def _rewrite_internal_host(url: str, host_replacement: str) -> tuple[str, dict[str, str]]:
    """Rewrite compose-internal hosts (minio/ai-service) to a host-reachable one.

    L2 generates presigned MinIO URLs against its in-network endpoint
    (``http://minio:9000/...``), which is unreachable from the host running
    this script. The URL netloc is replaced with ``host_replacement``, but
    the S3 SigV4 signature covers the ``host`` header
    (``X-Amz-SignedHeaders=host``), so the request must still send the
    ORIGINAL host — otherwise MinIO answers 403 SignatureDoesNotMatch
    (M4.10 run10 实测：裸改 host 直接 403).

    Returns:
        ``(rewritten_url, {"Host": original_netloc})`` for the HTTP call.
    """
    import re

    original_netloc = re.match(r"^https?://([^/]+)", url).group(1)
    rewritten = re.sub(r"^https?://[^/]+", host_replacement, url, count=1)
    return rewritten, {"Host": original_netloc}


def _verify_artifacts(
    base_url: str, token: str, report_id: int, minio_public_url: str
) -> list[dict[str, Any]]:
    """Assert all five artifacts are GENERATED; validate the PDF downloads."""
    artifacts = _get_json_list(
        base_url, token, f"/api/v1/reports/{report_id}/artifacts"
    )
    if not isinstance(artifacts, list):
        raise E2EError(f"产物列表格式异常: {str(artifacts)[:200]}")
    by_type = {a.get("artifactType"): a for a in artifacts}
    missing = REQUIRED_ARTIFACTS - set(by_type)
    if missing:
        raise E2EError(f"缺少产物类型: {sorted(missing)}（实际: {sorted(by_type)}）")
    not_generated = [
        t for t, a in by_type.items()
        if t in REQUIRED_ARTIFACTS and a.get("status") != "GENERATED"
    ]
    if not_generated:
        raise E2EError(f"产物状态非 GENERATED: {not_generated}")

    pdf_url = by_type["PDF"].get("downloadUrl")
    if not pdf_url:
        raise E2EError("PDF 产物缺少 downloadUrl（预签名 URL）")
    # 容器内网地址（minio:9000）在宿主机不可达，重写为对外映射地址；
    # 同时携带原始 Host 头以保持 SigV4 签名有效（见 _rewrite_internal_host）。
    pdf_url, host_headers = _rewrite_internal_host(pdf_url, minio_public_url)
    pdf_resp = requests.get(pdf_url, headers=host_headers, timeout=60)
    if pdf_resp.status_code != 200:
        raise E2EError(f"PDF 产物下载失败 HTTP {pdf_resp.status_code}: {pdf_url}")
    if not pdf_resp.content[:5] == b"%PDF-":
        raise E2EError(
            f"下载内容不是合法 PDF（前 5 字节: {pdf_resp.content[:5]!r}）"
        )
    for a in artifacts:
        print(
            f"  [artifact] {a.get('artifactType'):<12} status={a.get('status')} "
            f"key={a.get('objectKey')}"
        )
    print(f"  [artifact] PDF 预签名下载校验通过（{len(pdf_resp.content)} bytes）")
    return artifacts


def run(args: argparse.Namespace) -> int:
    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        print(f"ERROR: PDF 不存在: {pdf_path}", file=sys.stderr)
        return 1

    print(f"[e2e-m4] backend={args.backend_url} pdf={pdf_path.name}")

    steps: list[tuple[str, Any]] = []
    try:
        print("\n▶ 1/6 注册并登录")
        username, token = _register_and_login(args.backend_url)
        print(f"  user={username}")

        print("\n▶ 2/6 上传 PDF")
        task_id, report_id = _upload_pdf(
            args.backend_url, token, pdf_path,
            args.company_code, args.company_name,
            args.report_period, args.report_type,
        )
        print(f"  taskId={task_id} reportId={report_id}")

        print("\n▶ 3/6 等待任务终态（真实 API 全链路）")
        started = time.monotonic()
        task = _wait_task(
            args.backend_url, token, task_id, args.timeout, args.poll_interval
        )
        elapsed = time.monotonic() - started
        status = task.get("status")
        print(f"  终态={status} 耗时={elapsed:.0f}s")
        if status != "COMPLETED":
            raise E2EError(
                f"任务终态为 {status}（期望 COMPLETED）；task={str(task)[:500]}"
            )

        print("\n▶ 4/6 三表抽取结果")
        counts = _verify_statements(args.backend_url, token, report_id)
        print(f"  科目数: {counts}")

        print("\n▶ 5/6 勾稽 + 异常")
        check_count = _verify_checks(args.backend_url, token, report_id)
        anomaly_count = _verify_anomalies(args.backend_url, token, report_id)
        print(f"  勾稽规则 {check_count} 条，异常 {anomaly_count} 条")

        print("\n▶ 6/6 报告产物（Markdown/图表/PDF）")
        _verify_artifacts(
            args.backend_url, token, report_id, args.minio_public_url
        )

        steps.append(("done", None))
    except E2EError as error:
        print(f"\n✗ E2E 失败: {error}", file=sys.stderr)
        return 1

    print(f"\n✅ E2E COMPLETE — taskId={task_id} reportId={report_id} 全链路无 mock")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="M4.10 真实 PDF 端到端验收")
    parser.add_argument("--backend-url", default="http://localhost:8080")
    parser.add_argument(
        "--minio-public-url",
        default="http://localhost:9000",
        help="预签名 URL 的宿主可达 MinIO 地址（替换容器内网 minio:9000）",
    )
    parser.add_argument("--pdf", default=str(DEFAULT_PDF))
    parser.add_argument("--company-code", default="600519")
    parser.add_argument("--company-name", default="贵州茅台")
    parser.add_argument("--report-period", default="2025-12-31")
    parser.add_argument("--report-type", default="ANNUAL")
    parser.add_argument("--timeout", type=int, default=600,
                        help="任务终态等待上限秒数（默认 600，SLA 8min）")
    parser.add_argument("--poll-interval", type=float, default=3.0)
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
