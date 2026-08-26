/**
 * SSE 协议解析器（fetch + ReadableStream 手解 SSE）。
 *
 * 与 sse.ts 拆分：本模块零依赖、无浏览器环境依赖，可用 node --test 直接单测
 * （sse.ts 顶层会读取 import.meta.env，不适合 Node 直载）。
 * 跨 chunk 维护半行与半事件状态；支持 LF / CRLF / 孤 \r（跨 chunk）三种行尾。
 */

export interface SseParsedEvent {
  event: string
  data: string
  id?: string
}

export class SseParser {
  private buffer = ''
  private eventType = 'message'
  private dataLines: string[] = []
  private eventId: string | undefined

  /** 喂入一段文本，返回其中完整的事件。 */
  feed(chunk: string): SseParsedEvent[] {
    this.buffer += chunk
    const events: SseParsedEvent[] = []
    for (;;) {
      const lfIdx = this.buffer.indexOf('\n')
      const crIdx = this.buffer.indexOf('\r')
      let lineEnd: number
      let cutAfter: number
      if (crIdx !== -1 && crIdx < lfIdx) {
        if (crIdx === lfIdx - 1) {
          lineEnd = crIdx // 相邻的 CRLF 完整行尾
          cutAfter = lfIdx + 1
        } else if (crIdx === this.buffer.length - 1) {
          break // 孤 \r 在缓冲区末尾：可能是跨 chunk CRLF 前半，延后判定
        } else {
          lineEnd = crIdx // 孤 \r 行尾（其后非 \n）
          cutAfter = crIdx + 1
        }
      } else if (lfIdx !== -1) {
        lineEnd = lfIdx // LF 行尾
        cutAfter = lfIdx + 1
      } else if (crIdx !== -1 && crIdx === this.buffer.length - 1) {
        break // 仅有孤 \r 且位于末尾 → 延后判定
      } else if (crIdx !== -1) {
        lineEnd = crIdx // 孤 \r 行尾
        cutAfter = crIdx + 1
      } else {
        break // 无分隔符
      }
      const line = this.buffer.slice(0, lineEnd)
      this.buffer = this.buffer.slice(cutAfter)
      const parsed = this.processLine(line)
      if (parsed) events.push(parsed)
    }
    return events
  }

  private processLine(line: string): SseParsedEvent | null {
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
