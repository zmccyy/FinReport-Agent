#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5.08 前端问答页 E2E 冒烟（开发工具，非 CI）。

流程: 登录 → /reports/17 → 问答 Tab → 新建会话 → 发送问题 → 等待
流式完成 → 断言 ReAct 面板 / Markdown / 工具高亮 → 截图。

用法: python scripts/dev_m5_08_e2e.py
"""

from __future__ import annotations

import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
USERNAME = "m4e2e_1787231227"
PASSWORD = "m4e2e_pass_123"
QUESTION = "贵州茅台2025年总资产是多少？比上年变化多少？"
SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_shots")
os.makedirs(SHOT_DIR, exist_ok=True)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # 1. 登录
        page.goto(f"{BASE}/login")
        page.wait_for_selector('input[placeholder*="用户名"]', timeout=15000)
        page.fill('input[placeholder*="用户名"]', USERNAME)
        page.fill('input[placeholder*="密码"]', PASSWORD)
        page.get_by_role("button", name="登 录").first.click(timeout=15000)
        page.wait_for_url("**/reports*", timeout=20000)
        print("[1] 登录成功")

        # 2. 报告详情 + 问答 Tab
        page.goto(f"{BASE}/reports/17")
        page.wait_for_selector(".page__title", timeout=20000)
        page.get_by_role("tab", name="问答").click(timeout=10000)
        page.wait_for_selector(".chat", timeout=15000)
        print("[2] 问答 Tab 已渲染")

        # 3. 新建会话（空历史 → LLM 走工具 → ReAct 面板可验证）
        page.locator(".chat__sidebar-head button").first.click()
        page.wait_for_selector(".chat__empty", timeout=10000)
        print("[3] 新会话已就绪")

        # 4. 发送并等待流式完成
        page.locator(".chat__composer textarea").first.fill(QUESTION)
        page.get_by_role("button", name="发送").click()

        deadline = time.time() + 150
        peak_steps = 0
        while time.time() < deadline:
            try:
                peak_steps = max(peak_steps, page.locator(".tool-step").count())
            except Exception:
                pass
            msg_count = page.locator(".chat-msg").count()
            md_count = page.locator(".chat__messages .fin-md").count()
            if msg_count >= 2 and (md_count > 0 or peak_steps > 0):
                break
            time.sleep(0.5)
        time.sleep(2)

        # 5. 断言
        final_steps = page.locator(".tool-step").count()
        tool_panels = max(final_steps, peak_steps)
        has_markdown = page.locator(".chat__messages .fin-md").last.count() > 0
        has_thought = page.locator(".tool-step__block--thought").count() > 0
        has_tool_call = page.locator(".tool-step__block--call").count() > 0
        print(f"[5] ReAct 步骤: {tool_panels}, Markdown: {has_markdown}, "
              f"Thought: {has_thought}, ToolCall: {has_tool_call}")
        if has_markdown:
            text = page.locator(".chat__messages .fin-md").last.inner_text()
            print("   答案片段:", text[:100].replace("\n", " / "))

        page.screenshot(path=os.path.join(SHOT_DIR, "chat_flow.png"))
        print("[6] 截图:", os.path.join(SHOT_DIR, "chat_flow.png"))

        real_errors = [e for e in errors if "favicon" not in e.lower()
                       and "ERR_NAME_NOT_RESOLVED" not in e]
        if real_errors:
            print("[WARN] 浏览器错误:", real_errors[:4])
        browser.close()

        ok = tool_panels > 0 and has_markdown and has_thought
        print(("PASS" if ok else "FAIL"), "| E2E 结果:", "通过" if ok else "失败")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
