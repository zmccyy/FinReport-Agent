import { defineConfig, devices } from '@playwright/test'

/**
 * Frontend E2E（AGENTS.md §6.2：关键交互 Playwright E2E；§7.4 npm run test:e2e）。
 *
 * 无后端依赖：本套件覆盖可离线运行的关键交互（路由守卫、登录页表单、
 * 主题切换与持久化、三态组件、响应式断点），以及 SSE 进度+背压链路
 * （通过 page.route 注入 mock 事件流，真实走 sse.ts 客户端）。
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: true,
    timeout: 120_000,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
})
