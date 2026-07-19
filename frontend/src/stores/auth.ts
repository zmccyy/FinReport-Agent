import { ref } from 'vue'
import { defineStore } from 'pinia'
import * as authApi from '@/api/auth'
import { clearTokens, getRefreshToken, hasToken, setTokens } from '@/api/token'
import type { UserInfo } from '@/types'
import { useReportsStore } from './reports'

/**
 * 认证状态 store。
 *
 * token 持久化在 localStorage（见 api/token.ts）；user 信息在登录/恢复时拉取。
 *
 * 注意：isAuthenticated 使用手动同步的 ref 而非 computed，因为 hasToken() 读取
 * localStorage（非 Vue 响应式源），computed 依赖追踪无法感知 localStorage 变更。
 */
export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserInfo | null>(null)
  /** 是否已完成启动时的会话恢复（用于路由守卫等待） */
  const restored = ref(false)
  /** 手动同步的认证状态（localStorage 写入后调用 syncAuthState 更新） */
  const authenticated = ref(hasToken())

  function syncAuthState(): void {
    authenticated.value = hasToken()
  }

  /** 登录并拉取用户信息。 */
  async function login(username: string, password: string): Promise<void> {
    const tokens = await authApi.login(username, password)
    setTokens(tokens.accessToken, tokens.refreshToken)
    syncAuthState()
    await fetchCurrentUser()
    useReportsStore().reload()
  }

  /** 注册（注册即登录）并拉取用户信息。 */
  async function register(username: string, password: string, email?: string): Promise<void> {
    const tokens = await authApi.register(username, password, email)
    setTokens(tokens.accessToken, tokens.refreshToken)
    syncAuthState()
    await fetchCurrentUser()
    useReportsStore().reload()
  }

  /** 拉取当前用户信息。 */
  async function fetchCurrentUser(): Promise<void> {
    user.value = await authApi.getCurrentUser()
  }

  /**
   * 应用启动时恢复会话：本地有 token 则尝试拉取用户。
   * 拉取不回退（不阻塞首屏）；token 失效时由 http 拦截器统一处理 401。
   */
  function restore(): void {
    syncAuthState()
    if (authenticated.value && !user.value) {
      // 非阻塞：fire-and-forget，token 有效性由后续 API 调用的拦截器校验
      void fetchCurrentUser().catch(() => syncAuthState())
    }
    restored.value = true
  }

  /** 登出：黑名单 refreshToken（尽力而为），清空本地状态。 */
  async function logout(): Promise<void> {
    const refreshToken = getRefreshToken()
    if (refreshToken) {
      try {
        await authApi.logout(refreshToken)
      } catch {
        // 后端不可达也允许本地登出
      }
    }
    clearTokens()
    syncAuthState()
    useReportsStore().reload()
    user.value = null
  }

  return {
    user,
    restored,
    isAuthenticated: authenticated,
    login,
    register,
    logout,
    fetchCurrentUser,
    restore,
  }
})
