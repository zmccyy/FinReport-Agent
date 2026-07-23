<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import AppHeader from '@/components/AppHeader.vue'
import StatementTable from '@/components/StatementTable.vue'
import CheckList from '@/components/CheckList.vue'
import AnomalyList from '@/components/AnomalyList.vue'
import ReportViewer from '@/components/ReportViewer.vue'
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

const activeTab = ref(store.activeTab)

const tabPanes = computed(() => [
  { name: 'balance_sheet', label: '资产负债表', hint: '请等待 extract 阶段完成后再查看' },
  { name: 'income_statement', label: '利润表', hint: '请等待 extract 阶段完成后再查看' },
  { name: 'cash_flow', label: '现金流量表', hint: '请等待 extract 阶段完成后再查看' },
  { name: 'checks', label: '勾稽核对' },
  { name: 'anomalies', label: '异常检测' },
  { name: 'report', label: '报告' },
])

// KPI 占位数据，未来接入 L3 摘要接口
const kpis = computed(() => [
  { label: '总资产', value: '¥1,234,567.89', change: '+8.2%', positive: true },
  { label: '净利润', value: '¥234,567.89', change: '+12.5%', positive: true },
  { label: '经营现金流', value: '¥345,678.90', change: '-3.1%', positive: false },
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

// 切换 Tab 时同步到 store（仅三表 Tab 需要维护 store 状态）
function onTabChange(name: string): void {
  if (name === 'balance_sheet' || name === 'income_statement' || name === 'cash_flow') {
    store.setTab(name)
  }
}
</script>

<template>
  <div class="page">
    <AppHeader />
    <main class="fin-container page__main">
      <!-- 头部：公司 + 报告元数据 -->
      <div v-if="detail" class="page__head fin-card fin-fade-up">
        <div class="head__main">
          <h2 class="page__title">{{ detail.companyName }}</h2>
          <div class="page__meta-pills">
            <span class="meta-pill meta-pill--code">{{ detail.companyCode }}</span>
            <span class="meta-pill">{{ reportTypeLabel(detail.reportType) }}</span>
            <span class="meta-pill">{{ detail.reportPeriod }}</span>
            <span class="meta-pill">{{ detail.pageCount ?? '—' }} 页</span>
          </div>
        </div>
        <div class="page__actions">
          <el-tag :type="statusTagType(detail.parseStatus)" effect="light" size="small" round>
            {{ statusLabel(detail.parseStatus) }}
          </el-tag>
          <el-button size="small" plain @click="goProgress(detail.taskId)">查看进度</el-button>
          <el-button size="small" @click="goReports">返回列表</el-button>
        </div>
      </div>

      <!-- KPI 快捷指标卡 -->
      <div v-if="detail" class="kpi-bar fin-fade-up">
        <div v-for="kpi in kpis" :key="kpi.label" class="kpi-card fin-card">
          <span class="kpi-card__label">{{ kpi.label }}</span>
          <div class="kpi-card__row">
            <span class="kpi-card__value">{{ kpi.value }}</span>
            <span class="kpi-card__change" :class="{ 'kpi-card__change--up': kpi.positive, 'kpi-card__change--down': !kpi.positive }">
              {{ kpi.change }}
            </span>
          </div>
        </div>
      </div>

      <!-- 任务元信息 -->
      <div v-if="detail" class="page__meta fin-card fin-fade-up">
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

      <!-- 三表 + 勾稽 + 异常 + 报告 Tab -->
      <div v-else class="fin-card fin-fade-up statements-card">
        <el-tabs v-model="activeTab" class="fin-tabs-underline" @tab-change="onTabChange">
          <el-tab-pane
            v-for="tab in tabPanes"
            :key="tab.name"
            :label="tab.label"
            :name="tab.name"
          >
            <div class="fin-tab-content">
              <template v-if="tab.name === 'balance_sheet' || tab.name === 'income_statement' || tab.name === 'cash_flow'">
                <StatementTable
                  :statement-type="tab.name"
                  :title="tab.label"
                  :hint="tab.hint"
                />
              </template>
              <CheckList v-else-if="tab.name === 'checks'" :report-id="Number(reportId)" />
              <AnomalyList v-else-if="tab.name === 'anomalies'" :report-id="Number(reportId)" />
              <ReportViewer v-else-if="tab.name === 'report'" :report-id="Number(reportId)" />
            </div>
          </el-tab-pane>
        </el-tabs>

        <footer
          v-if="hasAnyStatements && (activeTab === 'balance_sheet' || activeTab === 'income_statement' || activeTab === 'cash_flow')"
          class="statements-card__foot"
        >
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
  padding-top: 28px;
  padding-bottom: 48px;
}

.page__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 24px;
  margin-bottom: 16px;
}

.head__main {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.page__title {
  font-size: 28px;
  font-weight: 700;
  color: var(--fin-text-primary);
  letter-spacing: -0.02em;
}

.page__meta-pills {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.meta-pill {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  background: var(--fin-primary-subtle);
  color: var(--fin-primary);
  font-size: 12px;
  font-weight: 500;
  border-radius: var(--fin-radius-xs);
}

.meta-pill--code {
  font-family: 'SFMono-Regular', Consolas, monospace;
}

.page__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

/* KPI 指标卡 */
.kpi-bar {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}

.kpi-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 16px 20px;
}

.kpi-card__label {
  font-size: 12px;
  color: var(--fin-text-secondary);
  font-weight: 500;
}

.kpi-card__row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.kpi-card__value {
  font-size: 20px;
  font-weight: 700;
  color: var(--fin-text-primary);
  font-family: 'SFMono-Regular', Consolas, monospace;
}

.kpi-card__change {
  font-size: 12px;
  font-weight: 600;
  font-family: 'SFMono-Regular', Consolas, monospace;
}

.kpi-card__change--up {
  color: var(--fin-success);
}

.kpi-card__change--down {
  color: var(--fin-danger);
}

/* 任务元信息 */
.page__meta {
  display: flex;
  gap: 32px;
  padding: 14px 20px;
  margin-bottom: 20px;
}

.meta__item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.meta__label {
  font-size: 10px;
  color: var(--fin-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.6px;
  font-weight: 600;
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
  padding: 4px 0 16px;
}

/* 自定义下划线 Tab */
:deep(.fin-tabs-underline .el-tabs__header) {
  margin-bottom: 16px;
  border-bottom: 1px solid var(--fin-border);
}

:deep(.fin-tabs-underline .el-tabs__nav-wrap::after) {
  display: none;
}

:deep(.fin-tabs-underline .el-tabs__active-bar) {
  height: 2px;
  border-radius: 2px 2px 0 0;
  background-color: var(--fin-primary);
}

:deep(.fin-tabs-underline .el-tabs__item) {
  padding: 0 16px;
  height: 44px;
  line-height: 44px;
  font-size: 14px;
  color: var(--fin-text-secondary);
  transition: color 0.15s ease;
}

:deep(.fin-tabs-underline .el-tabs__item:hover) {
  color: var(--fin-text-regular);
}

:deep(.fin-tabs-underline .el-tabs__item.is-active) {
  color: var(--fin-primary);
  font-weight: 600;
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
  padding: 4px 10px;
  background: var(--fin-warning-subtle);
  border-radius: var(--fin-radius-xs);
  font-size: 12px;
  color: var(--fin-warning);
}

@media (max-width: 768px) {
  .page__head {
    flex-direction: column;
  }

  .kpi-bar {
    grid-template-columns: 1fr;
  }

  .page__meta {
    flex-direction: column;
    gap: 12px;
  }
}
</style>
