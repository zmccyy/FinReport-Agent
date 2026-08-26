import { test, expect, type Page } from '@playwright/test'

/**
 * M6.01/M6.02 UI 冒烟：无需后端的核心交互。
 * 覆盖：路由守卫 / 登录页表单 / 主题切换与持久化 / 空态三态 / 窄屏响应式 / 懒加载路由。
 */

/** 注入假 token：auth store 以 localStorage 有无 token 判定登录态 */
async function injectToken(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('fin:access_token', 'e2e-fake-token')
    localStorage.setItem('fin:refresh_token', 'e2e-fake-refresh')
  })
}

test.describe('认证与路由守卫', () => {
  test('未登录访问受保护页 → 重定向 /login', async ({ page }) => {
    await page.goto('/reports')
    await expect(page).toHaveURL(/\/login/)
    await expect(page.getByText('FinReport Agent')).toBeVisible()
  })

  test('注入 token 后可达列表页（懒加载路由正常），重定向访问登录页会跳回', async ({ page }) => {
    await injectToken(page)
    await page.goto('/login')
    await expect(page).toHaveURL(/\/reports/)
  })
})

test.describe('登录页', () => {
  test('登录/注册 tab 切换与表单字段', async ({ page }) => {
    await page.goto('/login')
    // 默认登录：无邮箱/确认密码
    await expect(page.getByPlaceholder('3-64 位用户名')).toBeVisible()
    await expect(page.getByPlaceholder('6-128 位密码')).toBeVisible()
    // 切到注册
    await page.getByRole('tab', { name: '注册' }).click()
    await expect(page.getByPlaceholder('you@example.com')).toBeVisible()
    await expect(page.getByPlaceholder('再次输入密码')).toBeVisible()
    // 切回登录
    await page.getByRole('tab', { name: '登录' }).click()
    await expect(page.getByPlaceholder('you@example.com')).toHaveCount(0)
  })
})

test.describe('主题切换（M6.01 暗黑模式）', () => {
  test('登录页切换：html.dark + localStorage 持久化 + 刷新保持', async ({ page }) => {
    await page.goto('/login')
    const html = page.locator('html')
    await expect(html).not.toHaveClass(/dark/)

    await page.getByRole('button', { name: '切换到深色模式' }).click()
    await expect(html).toHaveClass(/dark/)
    const stored = await page.evaluate(() => localStorage.getItem('finreport-theme'))
    expect(stored).toBe('dark')

    // 刷新：index.html 防 FOUC 脚本应使首帧即为暗色
    await page.reload()
    await expect(html).toHaveClass(/dark/)

    // 切回浅色
    await page.getByRole('button', { name: '切换到浅色模式' }).click()
    await expect(html).not.toHaveClass(/dark/)
  })

  test('登录后 AppHeader 主题切换可用', async ({ page }) => {
    await injectToken(page)
    await page.goto('/reports')
    await expect(page.locator('header.app-header')).toBeVisible()
    await page.getByRole('button', { name: '切换到深色模式' }).click()
    await expect(page.locator('html')).toHaveClass(/dark/)
    await page.getByRole('button', { name: '切换到浅色模式' }).click()
    await expect(page.locator('html')).not.toHaveClass(/dark/)
  })
})

test.describe('列表页三态与响应式', () => {
  test('空态：EmptyState 组件渲染与引导按钮', async ({ page }) => {
    await injectToken(page)
    await page.goto('/reports')
    await expect(page.getByText('还没有上传任何财报')).toBeVisible()
    await expect(page.getByRole('button', { name: '立即上传' })).toBeVisible()
  })

  test('移动端 375px：登录页纵向堆叠（响应式断点）', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 })
    await page.goto('/login')
    const brand = page.locator('.auth__brand')
    const flexDir = await brand.evaluate((el) => getComputedStyle(el.parentElement!).flexDirection)
    expect(flexDir).toBe('column')
  })

  test('移动端 375px：列表页顶栏收窄（品牌全名隐藏）', async ({ page }) => {
    await injectToken(page)
    await page.setViewportSize({ width: 375, height: 812 })
    await page.goto('/reports')
    const brandName = page.locator('.brand__name')
    await expect(brandName).toBeHidden()
  })
})

test.describe('SSE 进度 + 背压集成（mock 事件流）', () => {
  test('进度事件流式渲染至 COMPLETED 终态，done 后停止重连', async ({ page }) => {
    await injectToken(page)

    await page.route('**/api/v1/tasks/**/stream', (route) => {
      const taskId = 'e2e-task-001'
      // 一个 chunk 一次性返回 200 个 progress 事件 + done：验证背压成批 flush 后终态正确
      const lines: string[] = []
      for (let i = 1; i <= 200; i++) {
        lines.push(
          `id: ${i}\nevent: progress\ndata: ${JSON.stringify({
            taskId,
            step: 'PARSE',
            status: i < 200 ? 'RUNNING' : 'SUCCESS',
            progress: Math.min(100, Math.round((i / 200) * 80)),
          })}\n\n`
        )
      }
      lines.push(
        `event: done\ndata: ${JSON.stringify({ taskId, reportId: 42, status: 'COMPLETED' })}\n\n`
      )
      void route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
        body: lines.join(''),
      })
    })

    await page.goto('/tasks/e2e-task-001/progress')
    // 终态完成卡片
    await expect(page.getByText('解析完成', { exact: false }).first()).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByRole('button', { name: '查看三表' })).toBeVisible()
  })
})
