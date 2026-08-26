/**
 * SSE 流事件背压队列（M6.02，plan §M6.02：背压 buffer 64）。
 *
 * 为什么需要：SSE 服务端可能一次性推送大量事件（如进度步骤、问答 token），
 * 若逐条同步 dispatch 会放大 Vue 状态更新的次数，主线程被事件循环挤满后
 * 渲染掉帧（spec §12.2「SSE 流里不同步等待」）。
 *
 * 机制：push() 先入缓冲，由定时器按 intervalMs 成批 flush（同一批次状态更新
 * 会被 Vue 合并为一次渲染）；缓冲达到 size 上限时 push() 返回 Promise 等待
 * flush，调用方（读取流循环）会阻塞该 Promise → 暂停 reader.read()，由网络
 * 层自然背压，不产生无界内存增长。
 */

export interface EventFlushQueueOptions {
  /** 背压阈值：缓冲事件数达到该值后 push() 阻塞，默认 64（plan M6.02） */
  size?: number
  /** flush 间隔毫秒，默认 25（高频流合并为一帧批处理） */
  intervalMs?: number
}

export interface EventFlushQueue {
  /**
   * 入队一组事件。缓冲超限时返回的 Promise 在下次 flush 后 resolve，
   * 调用方据此暂停读取（背压）。
   */
  push(events: readonly unknown[]): Promise<void>
  /** 立即 flush 剩余缓冲（流结束时调用，保证尾包不丢） */
  flush(): void
  /** 销毁：清定时器并放行所有等待者（不 flush 剩余缓冲，调 flush 后再调） */
  dispose(): void
}

export function createFlushQueue<T = unknown>(
  dispatch: (event: T) => void,
  options: EventFlushQueueOptions = {}
): EventFlushQueue {
  const size = options.size ?? 64
  const intervalMs = options.intervalMs ?? 25

  let buffer: T[] = []
  let timer: ReturnType<typeof setTimeout> | null = null
  let waiters: Array<() => void> = []
  let disposed = false

  function schedule(): void {
    if (timer != null || disposed) return
    timer = setTimeout(() => {
      timer = null
      flushBuffer()
      // 若 flush 后有新事件且未销毁，续期定时器
      if (buffer.length > 0 && !disposed) schedule()
    }, intervalMs)
  }

  function flushBuffer(): void {
    if (buffer.length === 0) return
    const batch = buffer
    buffer = []
    // 先 dispatch 后放行等待者：terminal 标志由 dispatch 同步更新，
    // 调用方在 push 恢复时即可看到终态并停止读流
    for (const event of batch) dispatch(event)
    const pending = waiters
    waiters = []
    for (const resolve of pending) resolve()
  }

  return {
    async push(events) {
      if (disposed) return
      buffer.push(...(events as T[]))
      schedule()
      if (buffer.length >= size) {
        await new Promise<void>((resolve) => waiters.push(resolve))
      }
    },
    flush() {
      flushBuffer()
    },
    dispose() {
      disposed = true
      if (timer != null) {
        clearTimeout(timer)
        timer = null
      }
      const pending = waiters
      waiters = []
      for (const resolve of pending) resolve()
    },
  }
}
