import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { ApiError, toApiError } from './errors'
import { clearTokens, getAccessToken, getRefreshToken, setTokens } from './token'
import type { TokenResponse } from '@/types'

/** mergeConfig 保留这些属性的值，故可跨越 retry 轮次标记已重试 */
interface RetriedConfig extends InternalAxiosRequestConfig {
  _finRetried?: true
}

/**
 * Axios 实例。
 *
 * - baseURL 默认 `/api/v1`（开发环境经 Vite proxy / 生产经 Nginx 转发到 L2）
 * - 请求拦截：注入 X-Trace-Id（traceId 起点，CLAUDE.md §11.3）+ Bearer token
 * - 响应拦截：401 时单飞（single-flight）刷新 access token 并重试原请求；
 *   刷新失败则清空 token 并跳转登录页
 * - 错误统一转为 ApiError（RFC 9457）
 */

const baseURL: string = import.meta.env.VITE_API_BASE_URL || '/api/v1'

const http = axios.create({
  baseURL,
  timeout: 30000,
})

function newTraceId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

// 单飞刷新：并发 401 共享同一次 refresh，避免刷新风暴
let refreshPromise: Promise<string | null> | null = null

async function doRefresh(): Promise<string | null> {
  const refreshToken = getRefreshToken()
  if (!refreshToken) return null
  try {
    // 用裸 axios 调用，避开响应拦截器，防止 refresh 自身 401 造成递归
    const resp = await axios.post<TokenResponse>(`${baseURL}/auth/refresh`, { refreshToken })
    setTokens(resp.data.accessToken, resp.data.refreshToken)
    return resp.data.accessToken
  } catch (err) {
    // 仅服务端 4xx 明确宣示 refresh token 失效时才清空;
    // 瞬时网络错误保留 token，由调用方决定退避（sse）还是放弃（http）
    if (err instanceof AxiosError && err.response && err.response.status < 500) {
      clearTokens()
    }
    return null
  }
}

function singleFlightRefresh(): Promise<string | null> {
  if (!refreshPromise) {
    refreshPromise = doRefresh().finally(() => {
      refreshPromise = null
    })
  }
  return refreshPromise
}

/**
 * 供 SSE（fetch 实现，不经过 axios 拦截器）在收到 401 时复用的刷新入口。
 * 返回新的 access token；失败返回 null。
 */
export function refreshAccessToken(): Promise<string | null> {
  return singleFlightRefresh()
}

http.interceptors.request.use((config) => {
  config.headers.set('X-Trace-Id', newTraceId())
  const token = getAccessToken()
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`)
  }
  return config
})

http.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const original = error.config as RetriedConfig | undefined
    const status = error.response?.status ?? 0
    const isAuthEndpoint = original?.url?.includes('/auth/') ?? false

    // _finRetried 标签在 mergeConfig 中保留（custom own-property），WeakSet 按对象身份是 dead code
    if (status === 401 && original && !isAuthEndpoint && !original._finRetried) {
      original._finRetried = true
      const newToken = await singleFlightRefresh()
      if (newToken) {
        original.headers.set('Authorization', `Bearer ${newToken}`)
        return http(original)
      }
    }

    if (status === 401) {
      clearTokens()
      redirectToLogin()
    }

    // 映射常见 Axios 错误码为更精确的 ApiError 文案
    if (error.response) {
      return Promise.reject(toApiError(error.response.data, status))
    }
    const code = error.code === 'ECONNABORTED' ? 'TIMEOUT' : 'NETWORK_ERROR'
    const msg = error.code === 'ECONNABORTED'
      ? '请求超时，请检查文件大小或网络速度'
      : '网络异常，请检查后端服务是否可达'
    return Promise.reject(new ApiError({ status: 0, code, message: msg }))
  }
)

function redirectToLogin(): void {
  if (window.location.pathname.startsWith('/login')) return
  const target = window.location.pathname + window.location.search
  window.location.href = `/login?redirect=${encodeURIComponent(target)}`
}

export default http
