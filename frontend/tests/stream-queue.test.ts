/**
 * M6.02 背压队列单测（node --test，零依赖运行：node --test tests/）。
 * 覆盖：成批 flush、顺序保持、背压阻塞与放行、flush/销毁语义。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { setTimeout as wait } from 'node:timers/promises'
import { createFlushQueue } from '../src/api/stream-queue.ts'

test('flush 间隔内的事件成批 dispatch，顺序保持', async () => {
  const received: string[] = []
  const queue = createFlushQueue<string>((e) => received.push(e), { intervalMs: 5 })
  // 同一批 push 在 flush 定时器触发前全部入队
  await queue.push(['a', 'b'])
  await queue.push(['c'])
  assert.equal(received.length, 0, 'flush 前不应 dispatch')
  await wait(20)
  assert.deepEqual(received, ['a', 'b', 'c'], '应成批且按序 dispatch')
  queue.dispose()
})

test('背压：缓冲达到 size 后 push 阻塞，flush 后放行', async () => {
  const received: string[] = []
  const queue = createFlushQueue<string>((e) => received.push(e), { size: 3, intervalMs: 30 })
  await queue.push(['1', '2']) // buffer=2 < 3 → 不阻塞
  let released = false
  const blocked = queue.push(['3', '4']).then(() => {
    released = true
  })
  await wait(5)
  assert.equal(released, false, 'buffer 达到 size 后 push 应阻塞')
  queue.flush() // 立即 flush：dispatch 全部并放行
  await blocked
  assert.equal(released, true, 'flush 后 push 应放行')
  assert.deepEqual(received, ['1', '2', '3', '4'])
  queue.dispose()
})

test('缓冲未满（< size）时 push 不阻塞', async () => {
  const queue = createFlushQueue<number>(() => {}, { size: 64, intervalMs: 60_000 })
  await queue.push(Array.from({ length: 62 }, (_, i) => i)) // 62 < 64 → 不阻塞
  let released = false
  queue.push([62]).then(() => {
    released = true
  })
  await wait(5)
  // 62+1=63 仍 < 64，push 及时放行
  assert.equal(released, true, '未达上限不应阻塞')
  queue.dispose()
})

test('flush() 立即清空缓冲并放行等待者（尾包不丢）', async () => {
  const received: string[] = []
  const queue = createFlushQueue<string>((e) => received.push(e), { size: 64, intervalMs: 10_000 })
  await queue.push(['x'])
  queue.flush()
  assert.deepEqual(received, ['x'], 'flush 应立刻 dispatch')
  queue.dispose()
})

test('dispose 后 push 为 no-op，等待者被放行', async () => {
  const received: string[] = []
  const queue = createFlushQueue<string>((e) => received.push(e), { size: 1, intervalMs: 10_000 })
  await queue.push(['a'])
  let released = false
  const blocked = queue.push(['b']).then(() => {
    released = true
  })
  await wait(5)
  queue.dispose()
  await blocked
  assert.equal(released, true, 'dispose 应放行等待者')
  await queue.push(['c'])
  assert.deepEqual(received, ['a'], 'dispose 后不应再 dispatch')
})
