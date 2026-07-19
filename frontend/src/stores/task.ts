import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { connectTaskStream, type TaskStreamConnection } from '@/api/sse'
import type { DoneEvent, ErrorEvent, ProgressEvent, TaskStepName } from '@/types'

/**
 * 任务进度 store：订阅单个任务的 SSE 流并维护 4 阶段进度状态。
 *
 * 后端步骤（TaskStepName）：PARSE / EXTRACT_BS / EXTRACT_IS / EXTRACT_CF / CHECK / REPORT。
 * UI 聚合为 4 阶段：PARSE / EXTRACT（三表并行）/ CHECK / REPORT。
 */

export type StageKey = 'PARSE' | 'EXTRACT' | 'CHECK' | 'REPORT'
export type StageStatus = 'pending' | 'active' | 'success' | 'failed'

export const STAGE_ORDER: StageKey[] = ['PARSE', 'EXTRACT', 'CHECK', 'REPORT']

/** 后端步骤名 → UI 阶段。 */
function stepToStage(step: TaskStepName): StageKey {
  return step.startsWith('EXTRACT') ? 'EXTRACT' : (step as StageKey)
}

function freshStages(): Record<StageKey, StageStatus> {
  return { PARSE: 'pending', EXTRACT: 'pending', CHECK: 'pending', REPORT: 'pending' }
}

export const useTaskStore = defineStore('task', () => {
  const taskId = ref<string | null>(null)
  const reportId = ref<number | null>(null)
  /** 整体进度 0-100（取事件 progress 的最大值，单调不回退） */
  const overallProgress = ref(0)
  /** 任务级状态：PENDING / RUNNING / COMPLETED / FAILED / CANCELLED */
  const taskStatus = ref<string>('PENDING')
  const errorMessage = ref<string | null>(null)
  /** SSE 是否已建立（用于连接状态提示） */
  const connected = ref(false)
  /** SSE 连接已断开且重连耗尽（连接 ≠ 任务状态） */
  const disconnected = ref(false)

  const stages = reactive<Record<StageKey, StageStatus>>(freshStages())
  /** EXTRACT 三表子步骤状态（EXTRACT_BS/IS/CF），用于展开展示 */
  const extractSteps = reactive<Record<string, StageStatus>>({})

  let connection: TaskStreamConnection | null = null

  const isTerminal = computed(() =>
    ['COMPLETED', 'FAILED', 'CANCELLED'].includes(taskStatus.value)
  )

  /** 订阅任务进度。先复位再连接；乐观把首阶段置为 active。 */
  function start(id: string, initialReportId?: number | null): void {
    reset()
    taskId.value = id
    reportId.value = initialReportId ?? null
    taskStatus.value = 'RUNNING'
    stages.PARSE = 'active'

    connection = connectTaskStream(id, {
      onOpen: () => {
        connected.value = true
      },
      onProgress: handleProgress,
      onDone: handleDone,
      onError: handleError,
      onReconnecting: () => {
        connected.value = false
      },
      onGiveUp: (_reason) => {
        connected.value = false
        // 连接中断 ≠ 任务失败；保留后端真实状态不变，仅标记已断开
        disconnected.value = true
      },
    })
  }

  function handleProgress(e: ProgressEvent): void {
    if (typeof e.progress === 'number') {
      overallProgress.value = Math.max(overallProgress.value, e.progress)
    }
    const stage = stepToStage(e.step)

    if (e.status === 'SUCCESS') {
      // EXTRACT 阶段需三表全部成功才标记完成（CR review finding #3）
      if (stage === 'EXTRACT') {
        extractSteps[e.step] = 'success'
        const allDone = ['EXTRACT_BS', 'EXTRACT_IS', 'EXTRACT_CF'].every(
          (s) => extractSteps[s] === 'success'
        )
        if (allDone) {
          stages.EXTRACT = 'success'
          activateNext('EXTRACT')
        } else {
          if (stages[stage] === 'pending') stages[stage] = 'active'
        }
      } else {
        stages[stage] = 'success'
        activateNext(stage)
      }
      taskStatus.value = 'RUNNING'
    } else if (e.status === 'FAILED') {
      stages[stage] = 'failed'
      // 步骤 FAILED ≠ 任务终态；后端最多重试 3 次后才发 done(FAILED) / error 事件
      // 只标记该阶段为失败，不提前设置 taskStatus
    } else {
      // RUNNING / RETRY 等中间态
      if (stages[stage] === 'pending') stages[stage] = 'active'
      taskStatus.value = 'RUNNING'
    }

    if (e.step.startsWith('EXTRACT')) {
      extractSteps[e.step] =
        e.status === 'SUCCESS' ? 'success' : e.status === 'FAILED' ? 'failed' : 'active'
    }
  }

  function handleDone(e: DoneEvent): void {
    taskStatus.value = e.status ?? 'COMPLETED'
    if (e.reportId != null) reportId.value = e.reportId
    if (taskStatus.value === 'COMPLETED') {
      overallProgress.value = 100
      for (const s of STAGE_ORDER) if (stages[s] !== 'failed') stages[s] = 'success'
    }
  }

  function handleError(e: ErrorEvent): void {
    taskStatus.value = 'FAILED'
    errorMessage.value = e.message || '任务失败'
    for (const s of STAGE_ORDER) if (stages[s] === 'active') stages[s] = 'failed'
  }

  function activateNext(stage: StageKey): void {
    const idx = STAGE_ORDER.indexOf(stage)
    if (idx >= 0 && idx + 1 < STAGE_ORDER.length) {
      const next = STAGE_ORDER[idx + 1]
      if (stages[next] === 'pending') stages[next] = 'active'
    }
  }

  /** 断开 SSE（保留当前状态，用于组件卸载）。 */
  function stop(): void {
    connection?.close()
    connection = null
    connected.value = false
  }

  /** 断开并清空全部状态。 */
  function reset(): void {
    stop()
    taskId.value = null
    reportId.value = null
    overallProgress.value = 0
    taskStatus.value = 'PENDING'
    errorMessage.value = null
    connected.value = false
    disconnected.value = false
    Object.assign(stages, freshStages())
    for (const k of Object.keys(extractSteps)) delete extractSteps[k]
  }

  return {
    taskId,
    reportId,
    overallProgress,
    taskStatus,
    errorMessage,
    connected,
    disconnected,
    stages,
    extractSteps,
    isTerminal,
    start,
    stop,
    reset,
  }
})
