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
      onGiveUp: (reason) => {
        connected.value = false
        if (!isTerminal.value) {
          taskStatus.value = 'FAILED'
          errorMessage.value = reason
        }
      },
    })
  }

  function handleProgress(e: ProgressEvent): void {
    if (typeof e.progress === 'number') {
      overallProgress.value = Math.max(overallProgress.value, e.progress)
    }
    const stage = stepToStage(e.step)

    if (e.status === 'SUCCESS') {
      stages[stage] = 'success'
      activateNext(stage)
      taskStatus.value = 'RUNNING'
    } else if (e.status === 'FAILED') {
      stages[stage] = 'failed'
      taskStatus.value = 'FAILED'
      errorMessage.value = (e.result?.error as string) ?? `阶段 ${stage} 失败`
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
    stages,
    extractSteps,
    isTerminal,
    start,
    stop,
    reset,
  }
})
