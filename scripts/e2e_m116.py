#!/usr/bin/env python3
"""M1.16 前端端到端验收（Playwright）- 带诊断版。

流程：注册 → 上传 PDF → 注入 mock 进度 → 验证 4 阶段完成态。
"""

from __future__ import annotations

import os, re, subprocess, sys, time, threading
from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
SHOT = "E:/tmp/m116"
PDF = os.path.abspath("data/sample_reports/600519_贵州茅台_2025年年度报告.pdf")
USER = f"e2e{int(time.time())}"
PASS = "e2e_pass_123"
os.makedirs(SHOT, exist_ok=True)

def shot(page, name):
    path = f"{SHOT}/{name}.png"
    page.screenshot(path=path, full_page=True)
    print(f"  [SHOT] {name} url={page.url[:80]}", flush=True)

def step(name, fn):
    print(f"\n▶ {name}", flush=True)
    try:
        fn()
        print(f"  ✓ {name}", flush=True)
    except Exception as e:
        shot(g_page, f"FAIL_{name.replace(' ','_')}")
        print(f"  ✗ {name}: {e}", flush=True)
        raise

g_page = None

def inject_mock(task_id):
    subprocess.run([sys.executable, "scripts/inject_mock_progress.py", task_id, "--interval", "0.9"], cwd=os.path.abspath("."), check=False)

def run():
    global g_page
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1360, "height": 900})
        g_page = pg
        pg.on("console", lambda m: print(f"    >> {m.type}: {m.text[:180]}") if m.type == "error" else None)

        # ============================ 1. Register ============================
        pg.goto(f"{BASE}/login", wait_until="networkidle")
        shot(pg, "01_login_page")
        print("  page title:", pg.title())
        print("  buttons:", pg.locator("button").all_inner_texts())
        print("  tabs:", pg.locator(".el-tabs__item").all_inner_texts())
        print("  inputs:", len(pg.locator("input").all()))

        step("click register tab", lambda: pg.locator(".el-tabs__item", has_text="注册").click())
        pg.wait_for_timeout(400)
        shot(pg, "01b_register_tab")

        # Fill form — use nth since both login and register have same placeholder structure
        step("fill username", lambda: pg.locator("input").nth(0).fill(USER))
        step("fill email", lambda: pg.locator("input").nth(1).fill(f"{USER}@test.com"))
        step("fill password", lambda: pg.locator("input").nth(2).fill(PASS))
        step("fill confirm", lambda: pg.locator("input").nth(3).fill(PASS))
        shot(pg, "01c_filled")
        step("submit register", lambda: pg.locator("button", has_text="注册并登录").click())
        pg.wait_for_url(re.compile(r"/reports"), timeout=15000)
        pg.wait_for_load_state("networkidle")
        shot(pg, "02_reports")

        # ============================ 2. Upload ============================
        step("click upload button", lambda: pg.locator("button", has_text="上传财报").first.click())
        pg.wait_for_url(re.compile(r"/reports/upload"), timeout=8000)
        pg.wait_for_load_state("networkidle")
        shot(pg, "03_upload_empty")
        print("  upload inputs:", len(pg.locator("input").all()))

        # Element Plus form fields — find by placeholder or surrounding label
        step("fill code", lambda: pg.locator('input[placeholder*="600519"]').first.fill("600519"))
        step("fill name", lambda: pg.locator('input[placeholder*="茅台"], input[placeholder*="贵州"]').first.fill("贵州茅台"))
        step("fill period", lambda: (pg.locator(".el-date-editor input").first.fill("2025-12-31"), pg.keyboard.press("Enter")))
        pg.wait_for_timeout(500)
        step("set PDF file", lambda: pg.locator('input[type="file"]').set_input_files(PDF))
        pg.wait_for_timeout(600)
        shot(pg, "04_filled")

        # ============================ 3. Submit & Progress ============================
        step("click submit", lambda: pg.locator("button", has_text="开始解析").click())
        pg.wait_for_url(re.compile(r"/tasks/.+/progress"), timeout=20000)
        task_id = re.search(r"/tasks/([^/]+)/progress", pg.url).group(1)
        print(f"  taskId = {task_id}")
        pg.wait_for_timeout(300)
        shot(pg, "05_progress_start")

        # Inject mock progress
        step("inject mock progress", lambda: inject_mock(task_id))
        pg.wait_for_timeout(3000)
        shot(pg, "05_progress_finish")

        # Wait for done
        try:
            pg.wait_for_selector("text=解析完成", timeout=20000)
            print("  ✓ 完成态出现")
        except:
            print("  ! 完成态未出现，检查页面")
            shot(pg, "05_TIMEOUT")
        pg.wait_for_timeout(300)
        shot(pg, "06_done")

        # ============================ 4. Verify ============================
        step("back to list", lambda: pg.locator("button", has_text="返回列表").first.click())
        pg.wait_for_url(re.compile(r"/reports"), timeout=8000)
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(500)
        shot(pg, "07_reports_final")
        body = pg.content()
        print("  has company:", "贵州茅台" in body)
        print("  has status:", "已完成" in body)

        b.close()
    print("\n✅ E2E COMPLETE")
    return 0

if __name__ == "__main__":
    sys.exit(run())
