/**
 * SSE (Server-Sent Events) 客户端封装。
 *
 * 支持自动重连（Last-Event-ID）和事件类型路由。
 * M1.11 将扩展为完整的 SSE 进度监听器。
 */

export interface SseOptions {
  onProgress?: (data: Record<string, unknown>) => void
  onComplete?: () => void
  onError?: (error: Event) => void
}

export function createSseConnection(
  taskId: string,
  options: SseOptions = {}
): EventSource {
  const baseUrl = import.meta.env.VITE_SSE_BASE_URL || '/api/v1'
  const url = `${baseUrl}/tasks/${taskId}/stream`

  const eventSource = new EventSource(url)

  eventSource.addEventListener('progress', (event) => {
    try {
      const data = JSON.parse(event.data)
      options.onProgress?.(data)
    } catch {
      // 忽略解析失败的进度事件
    }
  })

  eventSource.addEventListener('complete', () => {
    options.onComplete?.()
    eventSource.close()
  })

  eventSource.addEventListener('error', (event) => {
    options.onError?.(event)
  })

  return eventSource
}
