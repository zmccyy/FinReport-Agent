import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

/**
 * 路由（spec §6.5.1）。
 *
 * M1 范围：登录 / 财报列表 / 上传 / 解析进度。
 * 详情页三表、勾稽、异常、报告、问答等 Tab 在 M2+ 里程碑补齐。
 */
const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/reports' },
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: { title: '登录', public: true },
  },
  {
    path: '/reports',
    name: 'Reports',
    component: () => import('@/views/Reports.vue'),
    meta: { title: '我的财报', requiresAuth: true },
  },
  {
    path: '/reports/upload',
    name: 'ReportUpload',
    component: () => import('@/views/ReportUpload.vue'),
    meta: { title: '上传财报', requiresAuth: true },
  },
  {
    path: '/tasks/:taskId/progress',
    name: 'TaskProgress',
    component: () => import('@/views/TaskProgress.vue'),
    meta: { title: '解析进度', requiresAuth: true },
  },
  { path: '/:pathMatch(.*)*', redirect: '/reports' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  document.title = to.meta.title ? `${to.meta.title} · FinReport Agent` : 'FinReport Agent'

  const auth = useAuthStore()
  // 首次导航前等待会话恢复，避免刷新后误判为未登录
  if (!auth.restored) {
    await auth.restore()
  }

  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    return { name: 'Login', query: { redirect: to.fullPath } }
  }
  // 已登录访问登录页 → 直接进入主界面
  if (to.name === 'Login' && auth.isAuthenticated) {
    return { name: 'Reports' }
  }
  return true
})

export default router
