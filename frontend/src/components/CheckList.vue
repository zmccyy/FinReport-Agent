<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getChecks } from '@/api/checks'
import { ApiError } from '@/api/errors'
import type { AccountingCheck } from '@/types'

interface Props {
  reportId: number
}

const props = defineProps<Props>()

const checks = ref<AccountingCheck[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const SEVERITY_LABELS: Record<string, string> = {
  INFO: '提示',
  WARN: '警告',
  ERROR: '错误',
  CRITICAL: '严重',
}

const RULE_TYPE_LABELS: Record<string, string> = {
  balance_sheet_identity: '资产负债表恒等式',
  net_income_to_retained: '净利润→未分配利润变动',
  cash_flow_vs_net_income: '经营现金流 vs 净利润',
}

function severityTagType(severity: string): 'success' | 'warning' | 'danger' | 'info' {
  if (severity === 'CRITICAL' || severity === 'ERROR') return 'danger'
  if (severity === 'WARN') return 'warning'
  return 'info'
}

function ruleTypeLabel(type: string): string {
  return RULE_TYPE_LABELS[type] ?? type
}

function severityLabel(severity: string): string {
  return SEVERITY_LABELS[severity] ?? severity
}

function formatNumber(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
}

async function load(id: number): Promise<void> {
  loading.value = true
  error.value = null
  try {
    checks.value = await getChecks(id)
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '加载勾稽结果失败，请稍后重试'
    if (err instanceof ApiError && err.code !== 'REPORT_NOT_FOUND') {
      ElMessage.error(error.value)
    }
    checks.value = []
  } finally {
    loading.value = false
  }
}

function retry(): void {
  load(props.reportId)
}

watch(
  () => props.reportId,
  (id) => {
    if (id > 0) {
      load(id)
    }
  },
  { immediate: true }
)
</script>

<template>
  <div class="check-list">
    <div v-if="loading" class="check-list__empty">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>正在加载勾稽核对结果…</span>
    </div>

    <div v-else-if="error" class="check-list__empty check-list__empty--error">
      <el-icon><CircleCloseFilled /></el-icon>
      <p>{{ error }}</p>
      <el-button size="small" type="primary" plain @click="retry">重试</el-button>
    </div>

    <div v-else-if="checks.length === 0" class="check-list__empty">
      <el-icon><DocumentChecked /></el-icon>
      <span>暂无勾稽核对结果</span>
      <p class="check-list__empty-hint">请等待 CHECK 阶段完成后刷新</p>
    </div>

    <div v-else class="check-list__cards">
      <div
        v-for="check in checks"
        :key="check.id"
        class="check-card fin-card"
        :class="{
          'check-card--pass': check.isPass === true,
          'check-card--fail': check.isPass === false,
          'check-card--unknown': check.isPass == null,
        }"
      >
        <div class="check-card__head">
          <div class="check-card__title">
            <span
              class="check-card__icon-box"
              :class="{
                'check-card__icon-box--pass': check.isPass === true,
                'check-card__icon-box--fail': check.isPass === false,
                'check-card__icon-box--unknown': check.isPass == null,
              }"
            >
              <el-icon v-if="check.isPass === true"><CircleCheckFilled /></el-icon>
              <el-icon v-else-if="check.isPass === false"><CircleCloseFilled /></el-icon>
              <el-icon v-else><QuestionFilled /></el-icon>
            </span>
            <span class="check-card__title-text">{{ ruleTypeLabel(check.ruleType) }}</span>
          </div>
          <el-tag :type="severityTagType(check.severity)" size="small" effect="light" round>
            {{ severityLabel(check.severity) }}
          </el-tag>
        </div>

        <div class="check-card__name">{{ check.ruleName }}</div>

        <div class="check-card__metrics">
          <div class="metric">
            <span class="metric__label">预期值</span>
            <span class="metric__value">{{ formatNumber(check.expected) }}</span>
          </div>
          <div class="metric">
            <span class="metric__label">实际值</span>
            <span class="metric__value">{{ formatNumber(check.actual) }}</span>
          </div>
          <div class="metric">
            <span class="metric__label">差额</span>
            <span
              class="metric__value"
              :class="{ 'metric__value--warn': check.diff != null && check.diff !== 0 }"
            >
              {{ formatNumber(check.diff) }}
            </span>
          </div>
        </div>

        <p v-if="check.note" class="check-card__note">{{ check.note }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.check-list {
  min-height: 200px;
}

.check-list__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 64px 24px;
  color: var(--fin-text-secondary);
  font-size: 14px;
}

.check-list__empty .el-icon {
  font-size: 32px;
  color: var(--fin-text-secondary);
  opacity: 0.6;
}

.check-list__empty--error .el-icon {
  color: var(--fin-danger);
  opacity: 1;
}

.check-list__empty-hint {
  font-size: 12px;
  color: var(--fin-text-secondary);
  opacity: 0.7;
}

.check-list__cards {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

@media (max-width: 768px) {
  .check-list__cards {
    grid-template-columns: 1fr;
  }
}

.check-card {
  position: relative;
  padding: 18px 20px;
  overflow: hidden;
  transition:
    box-shadow 0.2s ease,
    transform 0.2s ease;
}

.check-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  bottom: 0;
  width: 4px;
}

.check-card:hover {
  box-shadow: var(--fin-shadow-card-hover);
  transform: translateY(-2px);
}

.check-card--pass::before {
  background: var(--fin-success);
}

.check-card--pass {
  background: var(--fin-success-bg);
}

.check-card--fail::before {
  background: var(--fin-danger);
}

.check-card--fail {
  background: var(--fin-danger-bg);
}

.check-card--unknown::before {
  background: var(--fin-border-strong);
}

.check-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.check-card__title {
  display: flex;
  align-items: center;
  gap: 10px;
}

.check-card__icon-box {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--fin-radius-xs);
  font-size: 18px;
}

.check-card__icon-box--pass {
  color: var(--fin-success);
  background: var(--fin-success-subtle);
}

.check-card__icon-box--fail {
  color: var(--fin-danger);
  background: var(--fin-danger-subtle);
}

.check-card__icon-box--unknown {
  color: var(--fin-text-secondary);
  background: var(--fin-bg);
}

.check-card__title-text {
  font-weight: 600;
  font-size: 15px;
  color: var(--fin-text-primary);
}

.check-card__name {
  font-size: 13px;
  color: var(--fin-text-secondary);
  margin-bottom: 14px;
}

.check-card__metrics {
  display: flex;
  gap: 16px;
  padding: 14px 0;
  border-top: 1px dashed var(--fin-border);
  border-bottom: 1px dashed var(--fin-border);
  margin-bottom: 14px;
}

.metric {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 4px;
}

.metric__label {
  font-size: 11px;
  color: var(--fin-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  font-weight: 600;
}

.metric__value {
  font-size: 14px;
  color: var(--fin-text-regular);
  font-family: 'SFMono-Regular', Consolas, monospace;
}

.metric__value--warn {
  color: var(--fin-warning);
  font-weight: 700;
}

.check-card__note {
  font-size: 13px;
  color: var(--fin-text-secondary);
  line-height: 1.5;
  margin: 0;
  padding: 10px 12px;
  background: rgba(0, 0, 0, 0.03);
  border-radius: var(--fin-radius-xs);
}
</style>
