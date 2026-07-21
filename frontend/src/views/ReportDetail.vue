<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import AppHeader from '@/components/AppHeader.vue'
import StatementTable from '@/components/StatementTable.vue'
import { useStatementsStore } from '@/stores/statements'
import { useReportsStore } from '@/stores/reports'
import type { ReportDetail } from '@/types'

/**
 * 财报详情页（spec §6.5.1 /reports/:id）。
 *
 * M2.11 范围：Tab 结构骨架 + 三表展示；勾稽/异常/报告/问答 Tab 在 M3 补齐。
 */

const route = useRoute()
const router = useRouter()
const store = useStatementsStore()
const reportsStore = useReportsStore()

const reportId = computed<number | null>(() => {
  const raw = Array.isArray(route.params.reportId) ? route.params.reportId[0] : route.params.reportId
  const n = Number(raw)
  return Number.isFinite(n) && n > 0 ? n : null
})

const REPORT_TYPE_LABELS: Record<string, string> = {
  ANNUAL: '年报',
  SEMI: '半年报',
  Q1: '一季报',
  Q3: '三季报',
}

const PARSE_STATUS_LABELS: Record<string, string> = {
  PENDING: '排队中',
  RUNNING: '解析中',
  COMPLETED: '已完成',
  FAILED: '失败',
}

function statusTagType(status: string): 'success' | 'danger' | 'primary' | 'info' | 'warning' {
  if (status === 'COMPLETED') return 'success'
  if (status === 'FAILED') return 'danger'
  if (status === 'RUNNING' || status === 'PENDING') return 'primary'
  return 'warning'
}

function reportTypeLabel(type: string): string {
  return REPORT_TYPE_LABELS[type] ?? type
}

function statusLabel(status: string): string {
  return PARSE_STATUS_LABELS[status] ?? status
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

const detail = computed<ReportDetail | null>(() => store.report)

const hasAnyStatements = computed(() => {
  const s = store.statements
  return s.balanceSheet.length > 0 || s.incomeStatement.length > 0 || s.cashFlow.length > 0
})

const tabPanes = computed(() => [
  { name: 'balance_sheet', label: '资产负债表', hint: '请等待 extract 阶段完成后再查看' },
  { name: 'income_statement', label: '利润表', hint: '请等待 extract 阶段完成后再查看' },
  { name: 'cash_flow', label: '现金流量表', hint: '请等待 extract 阶段完成后再查看' },
])

function goReports(): void {
  router.push({ name: 'Reports' })
}

function goProgress(taskId: string): void {
  router.push({ name: 'TaskProgress', params: { taskId } })
}

function refresh(): void {
  if (reportId.value != null) {
    load(reportId.value)
  }
}

async function load(id: number): Promise<void> {
  await store.load(id)
  if (store.report == null && store.error) {
    // 报告不存在或无权限（REPORT_NOT_FOUND 已在 store 内转为 error message）
    ElMessage.error(store.error)
  }
}

function handleResetEdits(): void {
  store.resetEdits()
  ElMessage.info('已重置本地编辑')
}

onMounted(() => {
  if (reportId.value == null) {
    ElMessage.error('报告 ID 无效')
    router.push({ name: 'Reports' })
    return
  }
  load(reportId.value)
})

// 当详情包含 taskId 时同步状态到 reports store（用于列表页状态一致性）
watch(
  () => detail.value,
  (d) => {
    if (d && d.taskId) {
      reportsStore.updateStatus(d.taskId, d.parseStatus, d.id)
    }
  }
)

// 切换 Tab 时同步到 store
function onTabChange(name: string): void {
  store.setTab(name as 'balance_sheet' | 'income_statement' | 'cash_flow')
}
</script>

<template>
  <div class="page">
    <AppHeader />
    <main class="fin-container page__main">
      <!-- 头部：公司 + 报告元数据 -->
      <div v-if="detail" class="page__head fin-fade-up">
        <div>
          <h2 class="page__title">{{ detail.companyName }}</h2>
          <p class="page__sub">
            <span class="sub__code">{{ detail.companyCode }}</span>
            <span class="sub__dot">·</span>
            <span>{{ reportTypeLabel(detail.reportType) }}</span>
            <span class="sub__dot">·</span>
            <span>{{ detail.reportPeriod }}</span>
            <span class="sub__dot">·</span>
            <span>{{ detail.pageCount ?? '—' }} 页</span>
          </p>
        </div>
        <div class="page__actions">
          <el-tag :type="statusTagType(detail.parseStatus)" effect="light">
            {{ statusLabel(detail.parseStatus) }}
          </el-tag>
          <el-button size="small" plain @click="goProgress(detail.taskId)">查看进度</el-button>
          <el-button size="small" @click="goReports">返回列表</el-button>
        </div>
      </div>

      <div v-if="detail" class="page__meta fin-fade-up">
        <div class="meta__item">
          <span class="meta__label">报告 ID</span>
          <span class="meta__value">#{{ detail.id }}</span>
        </div>
        <div class="meta__item">
          <span class="meta__label">任务 ID</span>
          <span class="meta__value meta__value--mono">{{ detail.taskId }}</span>
        </div>
        <div class="meta__item">
          <span class="meta__label">上传时间</span>
          <span class="meta__value">{{ formatTime(detail.createdAt) }}</span>
        </div>
      </div>

      <!-- Loading -->
      <div v-if="store.loading" class="fin-card fin-fade-up loading-block">
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>正在加载三表数据…</span>
      </div>

      <!-- Error -->
      <div v-else-if="store.error" class="fin-card fin-fade-up error-block">
        <el-icon class="error-block__icon"><CircleCloseFilled /></el-icon>
        <p class="error-block__title">{{ store.error }}</p>
        <el-button size="small" type="primary" plain @click="refresh">重试</el-button>
      </div>

      <!-- 三表 Tab -->
      <div v-else class="fin-card fin-fade-up statements-card">
        <el-tabs v-model="store.activeTab" type="border-card" @tab-change="onTabChange">
          <el-tab-pane
            v-for="tab in tabPanes"
            :key="tab.name"
            :label="tab.label"
            :name="tab.name"
          >
            <StatementTable
              :statement-type="tab.name"
              :title="tab.label"
              :hint="tab.hint"
            />
          </el-tab-pane>
        </el-tabs>

        <footer v-if="hasAnyStatements" class="statements-card__foot">
          <span v-if="store.hasEdited" class="foot__hint">
            <el-icon><Warning /></el-icon>
            <span>当前编辑仅保存在本地，暂未写回后端</span>
          </span>
          <el-button
            v-if="store.hasEdited"
            size="small"
            plain
            type="warning"
            @click="handleResetEdits"
          >
            重置编辑
          </el-button>
        </footer>
      </div>
    </main>
  </div>
</template>

<style scoped>
.page {
  min-height: 100vh;
}

.page__main {
  padding-top: 32px;
  padding-bottom: 48px;
  max-width: 1180px;
}

.page__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.page__title {
  font-size: 24px;
  font-weight: 700;
}

.page__sub {
  margin-top: 6px;
  font-size: 14px;
  color: var(--fin-text-secondary);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.sub__code {
  font-family: 'SFMono-Regular', Consolas, monospace;
}

.sub__dot {
  color: var(--fin-text-secondary);
  opacity: 0.5;
}

.page__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.page__meta {
  display: flex;
  gap: 24px;
  padding: 16px 20px;
  background: var(--fin-card-bg);
  border: 1px solid var(--fin-border);
  border-radius: var(--fin-radius-sm);
  margin-bottom: 24px;
}

.meta__item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.meta__label {
  font-size: 11px;
  color: var(--fin-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.meta__value {
  font-size: 13px;
  color: var(--fin-text-regular);
}

.meta__value--mono {
  font-family: 'SFMono-Regular', Consolas, monospace;
}

.loading-block,
.error-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 64px 24px;
}

.loading-block .is-loading {
  font-size: 32px;
  color: var(--fin-primary);
}

.error-block__icon {
  font-size: 40px;
  color: var(--fin-danger);
}

.error-block__title {
  font-size: 14px;
  color: var(--fin-text-regular);
}

.statements-card {
  padding: 8px 0 16px;
}

.statements-card__foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  padding: 12px 20px 4px;
  border-top: 1px dashed var(--fin-border);
  margin-top: 8px;
}

.foot__hint {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--fin-warning);
}
</style>
