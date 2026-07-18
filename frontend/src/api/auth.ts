import http from './http'
import type { TokenResponse, UserInfo } from '@/types'

/**
 * 认证 API（spec §6.2.1）。
 * 所有端点除 /users/me 外均为公开（SecurityConfig 中 PUBLIC_PATH_PREFIXES = /api/v1/auth/）。
 */

/** 登录。成功返回 TokenResponse（含 accessToken + refreshToken）。 */
export async function login(username: string, password: string): Promise<TokenResponse> {
  const resp = await http.post<TokenResponse>('/auth/login', { username, password })
  return resp.data
}

/** 注册。成功返回 TokenResponse（注册即登录）。email 可选。 */
export async function register(
  username: string,
  password: string,
  email?: string
): Promise<TokenResponse> {
  const resp = await http.post<TokenResponse>('/auth/register', { username, password, email })
  return resp.data
}

/**
 * 登出。把 refreshToken 加入黑名单（后端 204）。
 * 失败不阻断本地登出（调用方应忽略错误并清空本地 token）。
 */
export async function logout(refreshToken: string): Promise<void> {
  await http.post('/auth/logout', { refreshToken })
}

/** 查询当前登录用户信息。 */
export async function getCurrentUser(): Promise<UserInfo> {
  const resp = await http.get<UserInfo>('/users/me')
  return resp.data
}
