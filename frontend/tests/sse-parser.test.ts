/**
 * SseParser 回归单测（node --test，零依赖：node --test tests/）。
 * 覆盖 SSE 协议边角：CRLF/孤 \r 跨 chunk、多行 data、事件 ID、注释与心跳、
 * event 字段缺省为 message、终态后 reset。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SseParser } from '../src/api/sse-parser.ts'

test('标准事件：event + data + id，CRLF 行尾', () => {
  const p = new SseParser()
  const events = p.feed('id: 42\r\nevent: progress\r\ndata: {"step":1}\r\n\r\n')
  assert.equal(events.length, 1)
  assert.deepEqual(events[0], { event: 'progress', data: '{"step":1}', id: '42' })
})

test('跨 chunk 截断的行能拼回（LF + CRLF 混合）', () => {
  const p = new SseParser()
  assert.equal(p.feed('event: done\nda').length, 0, '半行不产出事件')
  assert.equal(p.feed('ta: [1,2]\r\n\r\n').length, 1)
  const events = p.feed('')
  assert.deepEqual(events, [])
})

test('孤 \\r 行尾（无 \\n）：下一字符触发空行边界', () => {
  const p = new SseParser()
  // 末尾 \r 会延后（可能与下个 chunk 组成 CRLF），需后续字符确认它是行尾
  assert.equal(p.feed('data: x\rdata: y').length, 0)
  const events = p.feed('\r\r\n')
  assert.equal(events.length, 1)
  assert.deepEqual(events[0].data, 'x\ny')
})

test('跨 chunk 的 CRLF：\\r 在 chunk 末尾应延后判定', () => {
  const p = new SseParser()
  assert.equal(p.feed('data: a\r').length, 0, '尾部 \\r 不算行尾')
  // 后续 chunk 补 \n 后拼回：后面无空行边界，a/b 是同一事件的多行 data（SSE 规范）
  const events = p.feed('\ndata: b\r\n\r\n')
  assert.equal(events.length, 1)
  assert.equal(events[0].data, 'a\nb')
})

test('多行 data 合并（\\n 连接），尾随空格修剪', () => {
  const p = new SseParser()
  const events = p.feed('data: first\ndata: second\n\n')
  assert.equal(events.length, 1)
  assert.equal(events[0].data, 'first\nsecond')
  // SSH 规范：冒号后跟随的单个空格属于分隔符被剥；多余空格是值的一部分
  const trimmed = p.feed('data:   spaced  \n\n')
  assert.equal(trimmed[0].data, '  spaced  ', '仅分隔空格被剥，其余保留')
})

test('注释行（心跳）与空行忽略', () => {
  const p = new SseParser()
  const events = p.feed(': ping\n\n: ping\n\ndata: real\n\n')
  assert.equal(events.length, 1)
  assert.equal(events[0].data, 'real')
})

test('event 字段缺省时默认 message', () => {
  const p = new SseParser()
  const events = p.feed('data: hello\n\n')
  assert.equal(events[0].event, 'message')
})

test('未知字段（retry 等）忽略，不重置数据', () => {
  const p = new SseParser()
  const events = p.feed('retry: 100\ndata: keep\n\n')
  assert.equal(events.length, 1)
  assert.equal(events[0].data, 'keep')
})

test('无 data 的 event 行不产出事件，但 eventType 被保留？', () => {
  // SSE 协议：event: 后无 data 的空行边界不产出（dataLines 空）；语义上可忽略
  const p = new SseParser()
  const events = p.feed('event: foo\n\n')
  assert.equal(events.length, 0)
  const after = p.feed('data: bar\n\n')
  assert.equal(after[0].event, 'message', '空边界重置事件类型')
})
