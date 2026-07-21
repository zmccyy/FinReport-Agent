import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { getReportDetail, getStatements } from '@/api/statements'
import { ApiError } from '@/api/errors'
import type { ReportDetail, StatementItem, StatementsResponse } from '@/types'

/**
 * 三表展示 store — M2.11。
 *
 * 加载 report 详情 + 三表数据；维护「编辑中的数值」本地状态
 * （plan M2.11：可手动编辑数值但暂不写回）。
 *
 * 状态分层：
 * - loading / error：请求态
 * - report / statements：服务端原始数据
 * - editedValues：用户编辑后的本地覆盖（key=`${statementType}:${itemId}`）
 */

type Tab = 'balance_sheet' | 'income_statement' | 'cash_flow'

interface EditedValueMap {
  /** key=`${statementType}:${itemId}` → 用户编辑后的数值 */
  values: Record<string, string>
}

const EMPTY_STATEMENTS: StatementsResponse = {
  balanceSheet: [],
  incomeStatement: [],
  cashFlow: [],
}

function editedKey(statementType: string, itemId: number): string {
  return `${statementType}:${itemId}`
}

export const useStatementsStore = defineStore('statements', () => {
  const report = ref<ReportDetail | null>(null)
  const statements = ref<StatementsResponse>(structuredClone(EMPTY_STATEMENTS))
  const loading = ref(false)
  const error = ref<string | null>(null)

  /** 本地编辑的数值（暂不写回后端，spec §3.10 / plan M2.11） */
  const edited = ref<EditedValueMap>({ values: {} })

  /** 当前激活 Tab */
  const activeTab = ref<Tab>('balance_sheet')

  const reportId = computed<number | null>(() => report.value?.id ?? null)

  /** 当前 Tab 的科目列表 */
  const currentItems = computed<StatementItem[]>(() => {
    switch (activeTab.value) {
      case 'balance_sheet':
        return statements.value.balanceSheet
      case 'income_statement':
        return statements.value.incomeStatement
      case 'cash_flow':
        return statements.value.cashFlow
      default:
        return []
    }
  })

  const hasEdited = computed(() => Object.keys(edited.value.values).length > 0)

  /** 加载详情 + 三表（并发） */
  async function load(reportId: number): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const [detail, resp] = await Promise.all([
        getReportDetail(reportId),
        getStatements(reportId),
      ])
      report.value = detail
      statements.value = resp
      edited.value = { values: {} }
    } catch (err) {
      error.value = err instanceof ApiError ? err.message : '加载失败，请稍后重试'
      report.value = null
      statements.value = structuredClone(EMPTY_STATEMENTS)
      edited.value = { values: {} }
    } finally {
      loading.value = false
    }
  }

  /** 切换 Tab */
  function setTab(tab: Tab): void {
    activeTab.value = tab
  }

  /** 暂存用户编辑的数值（暂不写回） */
  function editValue(statementType: string, itemId: number, value: string): void {
    edited.value.values[editedKey(statementType, itemId)] = value
  }

  /** 取某条目的「展示值」：本地编辑优先，否则用后端 itemValue */
  function displayValue(item: StatementItem): string {
    const key = editedKey(item.statementType, item.id)
    if (key in edited.value.values) {
      return edited.value.values[key]
    }
    if (item.itemValue == null) return ''
    return formatNumber(item.itemValue)
  }

  /** 重置所有编辑 */
  function resetEdits(): void {
    edited.value = { values: {} }
  }

  /** 退出时复位 */
  function reset(): void {
    report.value = null
    statements.value = structuredClone(EMPTY_STATEMENTS)
    loading.value = false
    error.value = null
    edited.value = { values: {} }
    activeTab.value = 'balance_sheet'
  }

  return {
    report,
    statements,
    loading,
    error,
    edited,
    activeTab,
    reportId,
    currentItems,
    hasEdited,
    load,
    setTab,
    editValue,
    displayValue,
    resetEdits,
    reset,
  }
})

/** 千分位格式化（数值展示用，不修改精度） */
function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return String(value)
  // 后端 itemValue 是 DECIMAL(20,4)；展示时保留最多 4 位小数并去除尾随 0
  const fixed = value.toFixed(4).replace(/\.?0+$/, '')
  return Number(fixed).toLocaleString('zh-CN', { maximumFractionDigits: 4 })
}
