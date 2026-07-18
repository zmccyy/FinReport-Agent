import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as authApi from '@/api/auth'
import { clearTokens, getRefreshToken, hasToken, setTokens } from '@/api/token'
import type { UserInfo } from '@/types'

/**
 * 认证状态 store。
 *
 * token 持久化在 localStorage（见 api/token.ts）；user 信息在登录/恢复时拉取。
 */
export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserInfo | null>(null)
  /** 是否已完成启动时的会话恢复（用于路由守卫等待） */
  const restored = ref(false)

  const isAuthenticated = computed(() => hasToken())

  /** 登录并拉取用户信息。 */
  async function login(username: string, password: string): Promise<void> {
    const tokens = await authApi.login(username, password)
    setTokens(tokens.accessToken, tokens.refreshToken)
    await fetchCurrentUser()
  }

  /** 注册（注册即登录）并拉取用户信息。 */
  async function register(username: string, password: string, email?: string): Promise<void> {
    const tokens = await authApi.register(username, password, email)
    setTokens(tokens.accessToken, tokens.refreshToken)
    await fetchCurrentUser()
  }

  /** 拉取当前用户信息。 */
  async function fetchCurrentUser(): Promise<void> {
    user.value = await authApi.getCurrentUser()
  }

  /**
   * 应用启动时恢复会话：本地有 token 则尝试拉取用户。
   * token 失效时由 http 拦截器统一处理 401（清 token + 跳登录）。
   */
  async function restore(): Promise<void> {
    if (hasToken() && !user.value) {
      try {
        await fetchCurrentUser()
      } catch {
        // 忽略：交给拦截器/后续请求触发登录跳转
      }
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
    user.value = null
  }

  return {
    user,
    restored,
    isAuthenticated,
    login,
    register,
    logout,
    fetchCurrentUser,
    restore,
  }
})
