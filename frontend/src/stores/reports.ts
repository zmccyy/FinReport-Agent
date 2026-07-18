import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'

/**
 * 已上传财报记录 store（localStorage 持久化）。
 *
 * ⚠️ M1 过渡方案：后端尚未提供 GET /reports 列表接口（M2+ 才补齐），
 * 因此前端把本用户上传的记录持久化在 localStorage，用于列表展示与
 * 重新进入进度页（演示 SSE 断线重连）。待列表接口上线后应改为后端拉取。
 */

export interface TrackedReport {
  taskId: string
  reportId: number | null
  companyCode: string
  companyName: string
  reportType: string
  reportPeriod: string
  /** ISO 时间戳 */
  uploadedAt: string
  /** 最近一次已知任务状态 */
  status: string
}

const STORAGE_KEY = 'fin:tracked_reports'

function load(): TrackedReport[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as TrackedReport[]) : []
  } catch {
    return []
  }
}

export const useReportsStore = defineStore('reports', () => {
  const reports = ref<TrackedReport[]>(load())

  // 任何变更自动持久化
  watch(
    reports,
    (val) => {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(val))
    },
    { deep: true }
  )

  /** 按上传时间倒序 */
  const sorted = computed(() =>
    [...reports.value].sort((a, b) => b.uploadedAt.localeCompare(a.uploadedAt))
  )

  /** 新增一条上传记录（已存在同 taskId 则更新）。 */
  function upsert(report: TrackedReport): void {
    const idx = reports.value.findIndex((r) => r.taskId === report.taskId)
    if (idx >= 0) reports.value[idx] = { ...reports.value[idx], ...report }
    else reports.value.push(report)
  }

  /** 更新某任务的状态。 */
  function updateStatus(taskId: string, status: string, reportId?: number | null): void {
    const r = reports.value.find((x) => x.taskId === taskId)
    if (!r) return
    r.status = status
    if (reportId != null) r.reportId = reportId
  }

  /** 删除一条记录。 */
  function remove(taskId: string): void {
    reports.value = reports.value.filter((r) => r.taskId !== taskId)
  }

  return { reports, sorted, upsert, updateStatus, remove }
})
