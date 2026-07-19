#!/usr/bin/env python3
"""M1.16 前端端到端验收（Playwright）。

流程：注册 → 上传 PDF → 由 L3 RabbitMQ worker 回报进度 → 验证四阶段完成态和报告列表。
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

BASE_URL = "http://localhost:5173"
SCREENSHOT_DIRECTORY = Path(__file__).resolve().parents[1] / "artifacts" / "m116"
SAMPLE_PDF = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "sample_reports"
    / "600519_贵州茅台_2025年年度报告.pdf"
)
USER_NAME = f"e2e{int(time.time())}"
PASSWORD = "e2e_pass_123"
SCREENSHOT_DIRECTORY.mkdir(parents=True, exist_ok=True)

current_page: Page | None = None


def take_screenshot(page: Page, name: str) -> None:
    """Store a diagnostic screenshot for the current browser state."""
    path = SCREENSHOT_DIRECTORY / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"  [SHOT] {name} url={page.url[:80]}", flush=True)


def run_step(name: str, action: Callable[[], object]) -> None:
    """Execute one E2E action and retain a screenshot if it fails."""
    print(f"\n▶ {name}", flush=True)
    try:
        action()
        print(f"  ✓ {name}", flush=True)
    except Exception as error:
        if current_page is not None:
            take_screenshot(current_page, f"FAIL_{name.replace(' ', '_')}")
        print(f"  ✗ {name}: {error}", flush=True)
        raise


def extract_task_id(url: str) -> str:
    """Return the task identifier only when the browser is on a task-progress route."""
    match = re.fullmatch(rf"{re.escape(BASE_URL)}/tasks/([^/]+)/progress(?:\?.*)?", url)
    assert match is not None, f"Expected task progress URL, received: {url}"
    return match.group(1)


def assert_task_progress_completed(page: Page, task_id: str) -> None:
    """Wait for the submitted task's progress card to reach its completed state."""
    assert (
        extract_task_id(page.url) == task_id
    ), "Browser left the submitted task progress route"
    progress_card = page.locator(".progress-card")
    assert progress_card.count() == 1, "Expected exactly one task progress card"
    progress_card.locator(".done__title", has_text="解析完成").wait_for(timeout=60_000)
    assert (
        f"任务 {task_id[:8]}" in progress_card.inner_text()
    ), "Progress card belongs to another task"


def assert_completed_report_row(page: Page, company_name: str) -> None:
    """Require the current user's uploaded company row to show the completed status."""
    matching_rows = page.locator(".el-table__body-wrapper tr").filter(
        has_text=company_name
    )
    assert (
        matching_rows.count() == 1
    ), f"Expected exactly one report row for {company_name}"
    assert matching_rows.first.get_by_text(
        "已完成", exact=True
    ).is_visible(), f"Report row for {company_name} is not completed"


def run() -> int:
    """Execute the M1 browser acceptance flow and return a process exit code."""
    global current_page

    if not SAMPLE_PDF.is_file():
        raise FileNotFoundError(f"M1 sample PDF is missing: {SAMPLE_PDF}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        current_page = page
        page.on(
            "console",
            lambda message: (
                print(f"    >> {message.type}: {message.text[:180]}")
                if message.type == "error"
                else None
            ),
        )

        page.goto(f"{BASE_URL}/login", wait_until="networkidle")
        take_screenshot(page, "01_login_page")
        print("  page title:", page.title())
        print("  buttons:", page.locator("button").all_inner_texts())
        print("  tabs:", page.locator(".el-tabs__item").all_inner_texts())
        print("  inputs:", page.locator("input").count())

        run_step(
            "click register tab",
            lambda: page.locator(".el-tabs__item", has_text="注册").click(),
        )
        page.wait_for_timeout(400)
        take_screenshot(page, "01b_register_tab")

        run_step("fill username", lambda: page.locator("input").nth(0).fill(USER_NAME))
        run_step(
            "fill email",
            lambda: page.locator("input").nth(1).fill(f"{USER_NAME}@test.com"),
        )
        run_step("fill password", lambda: page.locator("input").nth(2).fill(PASSWORD))
        run_step("fill confirm", lambda: page.locator("input").nth(3).fill(PASSWORD))
        take_screenshot(page, "01c_filled")
        run_step(
            "submit register",
            lambda: page.locator("button", has_text="注册并登录").click(),
        )
        page.wait_for_url(re.compile(r"/reports"), timeout=15_000)
        page.wait_for_load_state("networkidle")
        take_screenshot(page, "02_reports")

        run_step(
            "click upload button",
            lambda: page.locator("button", has_text="上传财报").first.click(),
        )
        page.wait_for_url(re.compile(r"/reports/upload"), timeout=8_000)
        page.wait_for_load_state("networkidle")
        take_screenshot(page, "03_upload_empty")
        print("  upload inputs:", page.locator("input").count())

        run_step(
            "fill code",
            lambda: page.locator('input[placeholder*="600519"]').first.fill("600519"),
        )
        run_step(
            "fill name",
            lambda: page.locator(
                'input[placeholder*="茅台"], input[placeholder*="贵州"]'
            ).first.fill("贵州茅台"),
        )
        run_step(
            "fill period",
            lambda: (
                page.locator(".el-date-editor input").first.fill("2025-12-31"),
                page.keyboard.press("Enter"),
            ),
        )
        page.wait_for_timeout(500)
        run_step(
            "set PDF file",
            lambda: page.locator('input[type="file"]').set_input_files(str(SAMPLE_PDF)),
        )
        page.wait_for_timeout(600)
        take_screenshot(page, "04_filled")

        run_step(
            "click submit", lambda: page.locator("button", has_text="开始解析").click()
        )
        page.wait_for_url(re.compile(r"/tasks/.+/progress"), timeout=20_000)
        task_id = extract_task_id(page.url)
        print(f"  taskId = {task_id}")
        page.wait_for_timeout(300)
        take_screenshot(page, "05_progress_start")

        run_step(
            "wait for submitted task completion",
            lambda: assert_task_progress_completed(page, task_id),
        )
        take_screenshot(page, "05_progress_finish")
        page.wait_for_timeout(300)
        take_screenshot(page, "06_done")

        run_step(
            "back to list",
            lambda: page.locator("button", has_text="返回列表").first.click(),
        )
        page.wait_for_url(re.compile(r"/reports"), timeout=8_000)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)
        take_screenshot(page, "07_reports_final")
        run_step(
            "assert completed report row",
            lambda: assert_completed_report_row(page, "贵州茅台"),
        )

        browser.close()

    print("\n✅ E2E COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(run())
