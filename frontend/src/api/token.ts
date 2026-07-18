/**
 * Token 持久化存储（localStorage）。
 *
 * 独立成模块以打破 http.ts（请求拦截注入 token / 401 刷新）与
 * auth store（登录后写 token）之间的循环依赖。
 */

const ACCESS_TOKEN_KEY = 'fin:access_token'
const REFRESH_TOKEN_KEY = 'fin:refresh_token'

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_TOKEN_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY)
}

export function setTokens(accessToken: string, refreshToken: string): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, accessToken)
  localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken)
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY)
  localStorage.removeItem(REFRESH_TOKEN_KEY)
}

/** 是否已登录（仅依据本地是否存在 access token，不校验有效性） */
export function hasToken(): boolean {
  return getAccessToken() !== null
}
