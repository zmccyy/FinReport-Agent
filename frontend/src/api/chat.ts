import http from './http'
import { refreshAccessToken } from './http'
import { getAccessToken } from './token'
import { SseParser } from './sse'
import type { ChatMessage, ChatSession, ChatStreamEvent } from '@/types'

/**
 * 问答 API（spec §6.2.3 / M5.08）：
 * - 会话 CRUD 走 axios（JSON 请求）
 * - 消息发送走 fetch + ReadableStream 手解 SSE —— 与任务进度 SSE 同因：
 *   EventSource 不能携带 Authorization 头，而后端 JwtFilter 强制 Bearer。
 */

/** 创建问答会话。 */
export async function createSession(reportId: number, title?: string): Promise<ChatSession> {
  const resp = await http.post<ChatSession>('/chat/sessions', {
    reportId,
    title: title ?? undefined,
  })
  return resp.data
}

/** 查询当前用户的会话列表。 */
export async function listSessions(): Promise<ChatSession[]> {
  const resp = await http.get<ChatSession[]>('/chat/sessions')
  return resp.data
}

/** 查询会话历史消息（时间正序）。 */
export async function listMessages(sessionId: number): Promise<ChatMessage[]> {
  const resp = await http.get<ChatMessage[]>(`/chat/sessions/${sessionId}/messages`)
  return resp.data
}

/** 删除会话及其消息。 */
export async function deleteSession(sessionId: number): Promise<void> {
  await http.delete(`/chat/sessions/${sessionId}`)
}

export interface ChatStreamHandlers {
  /** 每个 SSE 事件（thought / tool_call / tool_result / token / done / error） */
  onEvent: (event: ChatStreamEvent) => void
  /** 连接建立（后端开始输出） */
  onOpen?: () => void
  /** 认证失败（刷新后仍 401），应用侧应跳转登录 */
  onAuthError?: () => void
}

export interface ChatStreamConnection {
  close: () => void
}

function newTraceId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

function redirectToLogin(): void {
  if (window.location.pathname.startsWith('/login')) return
  const target = window.location.pathname + window.location.search
  window.location.href = `/login?redirect=${encodeURIComponent(target)}`
}

/**
 * 发送消息并拉取 SSE 问答流。
 *
 * 事件顺序（spec §6.3.3）：thought → tool_call → tool_result → token… → done；
 * 失败以 error 事件终结。终端事件后自动停止（不再重连——长连接由连接层兜底，
 * 会话内追问是新一轮 POST）。
 *
 * @param sessionId 会话 ID
 * @param content   用户消息
 * @param handlers  事件回调
 * @returns 连接句柄，close() 主动断开
 */
export function connectChatStream(
  sessionId: number,
  content: string,
  handlers: ChatStreamHandlers
): ChatStreamConnection {
  const baseUrl = import.meta.env.VITE_SSE_BASE_URL || '/api/v1'
  let closed = false
  let terminal = false
  let retriedAfterRefresh = false
  let controller: AbortController | null = null

  async function connect(): Promise<void> {
    if (closed || terminal) return
    controller = new AbortController()

    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
      'X-Trace-Id': newTraceId(),
    }
    const token = getAccessToken()
    if (token) headers['Authorization'] = `Bearer ${token}`

    let response: Response
    try {
      response = await fetch(`${baseUrl}/chat/sessions/${sessionId}/messages`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ content }),
        signal: controller.signal,
      })
    } catch (err) {
      if (closed || (err instanceof DOMException && err.name === 'AbortError')) return
      terminal = true
      handlers.onEvent({ type: 'error', data: { code: 'CHAT_NETWORK', message: '网络连接失败，请重试' } })
      return
    }

    if (response.status === 401) {
      if (!retriedAfterRefresh) {
        retriedAfterRefresh = true
        const newToken = await refreshAccessToken()
        if (newToken) {
          await connect()
          return
        }
      }
      terminal = true
      handlers.onAuthError?.()
      redirectToLogin()
      return
    }
    // 400/404 等业务错误：读取问题体，终止
    if (!response.ok) {
      terminal = true
      let message = `请求失败（${response.status}）`
      try {
        const body = await response.json()
        if (body && typeof body.message === 'string') message = body.message
      } catch {
        /* 非 JSON 响应体，使用默认文案 */
      }
      handlers.onEvent({ type: 'error', data: { code: 'CHAT_HTTP_ERROR', message } })
      return
    }
    if (!response.body) {
      terminal = true
      handlers.onEvent({ type: 'error', data: { code: 'CHAT_NO_BODY', message: '服务未返回数据流' } })
      return
    }

    handlers.onOpen?.()
    try {
      const reader = response.body.getReader()
      const decoder = new TextDecoder('utf-8')
      const parser = new SseParser()
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        const events = parser.feed(decoder.decode(value, { stream: true }))
        for (const evt of events) dispatch(evt)
        if (terminal) {
          await reader.cancel().catch(() => undefined)
          return
        }
      }
    } catch (err) {
      if (!closed && !terminal) {
        handlers.onEvent({ type: 'error', data: { code: 'CHAT_STREAM_BROKEN', message: '连接中断，请重试' } })
      }
    }
  }

  function dispatch(evt: { event: string; data: string }): void {
    let payload: unknown
    try {
      payload = JSON.parse(evt.data)
    } catch {
      return // 忽略无法解析的事件
    }
    switch (evt.event) {
      case 'thought':
      case 'tool_call':
      case 'tool_result':
      case 'token':
        handlers.onEvent({ type: evt.event, data: payload } as ChatStreamEvent)
        break
      case 'done':
        terminal = true // done 是终态：服务端随后关流，客户端停止读取
        handlers.onEvent({ type: 'done', data: payload } as ChatStreamEvent)
        break
      case 'error':
        terminal = true
        handlers.onEvent({ type: 'error', data: payload } as ChatStreamEvent)
        break
      default:
        break
    }
  }

  void connect()

  return {
    close() {
      closed = true
      controller?.abort()
    },
  }
}

export const chatEventTypeNames = {
  thought: '思考',
  tool_call: '工具调用',
  tool_result: '工具结果',
} as const
