import { refreshAccessToken } from './http'
import { getAccessToken } from './token'
import type { DoneEvent, ErrorEvent, ProgressEvent } from '@/types'

/**
 * 任务进度 SSE 客户端（fetch + ReadableStream 实现）。
 *
 * 为什么不用原生 EventSource：EventSource 无法携带自定义请求头，
 * 而后端 `/tasks/{id}/stream` 由 JwtFilter 强制校验 `Authorization: Bearer`。
 * 因此改用 fetch 读取流式响应，手动解析 SSE 协议（event:/data:/id:/注释行）。
 *
 * 能力：
 * - 携带 Bearer token；401 时刷新 token 后重连一次
 * - 断线自动重连，指数退避；重连携带 Last-Event-ID 供服务端回放（CLAUDE.md §6.3.2）
 * - 路由 progress / done / error 三类事件到对应回调
 * - 收到 done / error（终态）后自动停止，不再重连
 */

export interface TaskStreamHandlers {
  /** 进度事件（每个步骤的 RUNNING/SUCCESS 推送） */
  onProgress?: (event: ProgressEvent) => void
  /** 任务完成/取消（终态） */
  onDone?: (event: DoneEvent) => void
  /** 任务失败（终态） */
  onError?: (event: ErrorEvent) => void
  /** 连接建立 */
  onOpen?: () => void
  /** 连接中断，即将按退避策略重连 */
  onReconnecting?: (attempt: number, delayMs: number) => void
  /** 达到最大重连次数仍未恢复，放弃 */
  onGiveUp?: (reason: string) => void
}

export interface TaskStreamOptions {
  /** 最大自动重连次数，默认 5 */
  maxRetries?: number
  /** 退避基数（毫秒），默认 1000；第 n 次重连延迟 base * 2^(n-1) */
  retryBaseMs?: number
}

export interface TaskStreamConnection {
  /** 主动关闭（不再重连） */
  close: () => void
}

const DEFAULT_MAX_RETRIES = 5
const DEFAULT_RETRY_BASE_MS = 1000

/** SSE 协议解析器：跨 chunk 维护半行与半事件状态。 */
class SseParser {
  private buffer = ''
  private eventType = 'message'
  private dataLines: string[] = []
  private eventId: string | undefined

  /** 喂入一段文本，返回其中完整的事件。 */
  feed(chunk: string): Array<{ event: string; data: string; id?: string }> {
    this.buffer += chunk
    const events: Array<{ event: string; data: string; id?: string }> = []
    for (;;) {
      const match = this.buffer.match(/\r\n|\r|\n/)
      if (!match || match.index === undefined) break
      const line = this.buffer.slice(0, match.index)
      this.buffer = this.buffer.slice(match.index + match[0].length)
      const parsed = this.processLine(line)
      if (parsed) events.push(parsed)
    }
    return events
  }

  private processLine(line: string): { event: string; data: string; id?: string } | null {
    // 空行 = 事件边界，dispatch
    if (line === '') {
      if (this.dataLines.length > 0) {
        const evt = { event: this.eventType, data: this.dataLines.join('\n'), id: this.eventId }
        this.reset()
        return evt
      }
      this.reset()
      return null
    }
    // 注释行（心跳）以冒号开头，忽略
    if (line.startsWith(':')) return null

    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)

    switch (field) {
      case 'event':
        this.eventType = value
        break
      case 'data':
        this.dataLines.push(value)
        break
      case 'id':
        this.eventId = value
        break
      default:
        // retry 等字段忽略
        break
    }
    return null
  }

  private reset(): void {
    this.eventType = 'message'
    this.dataLines = []
    this.eventId = undefined
  }
}

function newTraceId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

/**
 * 订阅指定任务的 SSE 进度流。
 *
 * @param taskId 任务 ID
 * @param handlers 事件回调
 * @param options 重连策略
 * @returns 连接句柄，调用 close() 主动断开
 */
export function connectTaskStream(
  taskId: string,
  handlers: TaskStreamHandlers = {},
  options: TaskStreamOptions = {}
): TaskStreamConnection {
  const maxRetries = options.maxRetries ?? DEFAULT_MAX_RETRIES
  const retryBaseMs = options.retryBaseMs ?? DEFAULT_RETRY_BASE_MS
  const baseUrl: string = import.meta.env.VITE_SSE_BASE_URL || '/api/v1'

  let closed = false
  let terminal = false
  let attempts = 0
  let retriedAfterRefresh = false
  let lastEventId: string | undefined
  let controller: AbortController | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null

  async function connect(): Promise<void> {
    if (closed || terminal) return
    controller = new AbortController()
    const url = `${baseUrl}/tasks/${encodeURIComponent(taskId)}/stream`

    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      'X-Trace-Id': newTraceId(),
    }
    const token = getAccessToken()
    if (token) headers['Authorization'] = `Bearer ${token}`
    if (lastEventId) headers['Last-Event-ID'] = lastEventId

    let response: Response
    try {
      response = await fetch(url, { headers, signal: controller.signal })
    } catch (err) {
      // 主动 close 导致的 abort 不算失败
      if (closed || (err instanceof DOMException && err.name === 'AbortError')) return
      scheduleReconnect()
      return
    }

    if (response.status === 401) {
      // token 过期：刷新后重连一次，仍 401 则放弃并跳转登录
      if (!retriedAfterRefresh) {
        retriedAfterRefresh = true
        const newToken = await refreshAccessToken()
        if (newToken) {
          await connect()
          return
        }
      }
      giveUp('认证失败，请重新登录')
      redirectToLogin()
      return
    }

    if (!response.ok || !response.body) {
      scheduleReconnect()
      return
    }

    // 连接成功
    attempts = 0
    retriedAfterRefresh = false
    handlers.onOpen?.()

    try {
      await readBody(response.body)
    } catch {
      // 读流异常，按中断处理
    }
    if (!closed && !terminal) scheduleReconnect()
  }

  async function readBody(body: ReadableStream<Uint8Array>): Promise<void> {
    const reader = body.getReader()
    const decoder = new TextDecoder('utf-8')
    const parser = new SseParser()
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      const events = parser.feed(decoder.decode(value, { stream: true }))
      for (const evt of events) dispatch(evt)
      if (terminal) {
        // 终态事件已处理，主动取消读取
        await reader.cancel().catch(() => undefined)
        return
      }
    }
  }

  function dispatch(evt: { event: string; data: string; id?: string }): void {
    if (evt.id) lastEventId = evt.id
    let payload: unknown
    try {
      payload = JSON.parse(evt.data)
    } catch {
      return // 忽略无法解析的事件
    }
    switch (evt.event) {
      case 'progress':
        handlers.onProgress?.(payload as ProgressEvent)
        break
      case 'done':
        terminal = true
        handlers.onDone?.(payload as DoneEvent)
        break
      case 'error':
        terminal = true
        handlers.onError?.(payload as ErrorEvent)
        break
      default:
        break
    }
  }

  function scheduleReconnect(): void {
    if (closed || terminal) return
    if (attempts >= maxRetries) {
      giveUp('连接多次中断，已停止重试')
      return
    }
    attempts += 1
    const delay = retryBaseMs * Math.pow(2, attempts - 1)
    handlers.onReconnecting?.(attempts, delay)
    reconnectTimer = setTimeout(() => {
      void connect()
    }, delay)
  }

  function giveUp(reason: string): void {
    terminal = true
    handlers.onGiveUp?.(reason)
  }

  void connect()

  return {
    close() {
      closed = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      controller?.abort()
    },
  }
}

function redirectToLogin(): void {
  if (window.location.pathname.startsWith('/login')) return
  const target = window.location.pathname + window.location.search
  window.location.href = `/login?redirect=${encodeURIComponent(target)}`
}
