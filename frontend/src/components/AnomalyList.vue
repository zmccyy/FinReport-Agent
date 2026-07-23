<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getAnomalies } from '@/api/anomalies'
import { ApiError } from '@/api/errors'
import type { AnomalyRecord } from '@/types'

interface Props {
  reportId: number
}

const props = defineProps<Props>()

const anomalies = ref<AnomalyRecord[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const SEVERITY_LABELS: Record<string, string> = {
  INFO: '提示',
  WARN: '警告',
  ERROR: '错误',
  CRITICAL: '严重',
}

const ANOMALY_TYPE_LABELS: Record<string, string> = {
  yoy_change: '同比变动',
  qoq_change: '环比变动',
  logic_conflict: '逻辑冲突',
}

function severityTagType(severity: string): 'success' | 'warning' | 'danger' | 'info' {
  if (severity === 'CRITICAL') return 'danger'
  if (severity === 'ERROR') return 'danger'
  if (severity === 'WARN') return 'warning'
  return 'info'
}

function severityLabel(severity: string): string {
  return SEVERITY_LABELS[severity] ?? severity
}

function anomalyTypeLabel(type: string): string {
  return ANOMALY_TYPE_LABELS[type] ?? type
}

function severityIconClass(severity: string): string {
  const s = severity?.toLowerCase() ?? 'info'
  if (s === 'critical' || s === 'error') return 'anomaly-card__icon-box--danger'
  if (s === 'warn') return 'anomaly-card__icon-box--warning'
  return 'anomaly-card__icon-box--info'
}

function formatNumber(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
}

async function load(id: number): Promise<void> {
  loading.value = true
  error.value = null
  try {
    anomalies.value = await getAnomalies(id)
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '加载异常检测结果失败，请稍后重试'
    if (err instanceof ApiError && err.code !== 'REPORT_NOT_FOUND') {
      ElMessage.error(error.value)
    }
    anomalies.value = []
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
  <div class="anomaly-list">
    <div v-if="loading" class="anomaly-list__empty">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>正在加载异常检测结果…</span>
    </div>

    <div v-else-if="error" class="anomaly-list__empty anomaly-list__empty--error">
      <el-icon><CircleCloseFilled /></el-icon>
      <p>{{ error }}</p>
      <el-button size="small" type="primary" plain @click="retry">重试</el-button>
    </div>

    <div v-else-if="anomalies.length === 0" class="anomaly-list__empty">
      <el-icon><SuccessFilled /></el-icon>
      <span>未发现异常</span>
      <p class="anomaly-list__empty-hint">CHECK 阶段完成后会自动刷新，0 异常亦是明确结论</p>
    </div>

    <div v-else class="anomaly-list__cards">
      <div
        v-for="anomaly in anomalies"
        :key="anomaly.id"
        class="anomaly-card fin-card"
        :class="`anomaly-card--${anomaly.severity?.toLowerCase() ?? 'info'}`"
      >
        <div class="anomaly-card__head">
          <div class="anomaly-card__title">
            <span
              class="anomaly-card__icon-box"
              :class="severityIconClass(anomaly.severity)"
            >
              <el-icon v-if="anomaly.severity === 'CRITICAL' || anomaly.severity === 'ERROR'"><CircleCloseFilled /></el-icon>
              <el-icon v-else-if="anomaly.severity === 'WARN'"><WarningFilled /></el-icon>
              <el-icon v-else><InfoFilled /></el-icon>
            </span>
            <span class="anomaly-card__item">{{ anomaly.itemName }}</span>
          </div>
          <el-tag :type="severityTagType(anomaly.severity)" size="small" effect="light" round>
            {{ severityLabel(anomaly.severity) }}
          </el-tag>
        </div>

        <p class="anomaly-card__desc">{{ anomaly.description }}</p>

        <div class="anomaly-card__meta">
          <span class="anomaly-card__type">{{ anomalyTypeLabel(anomaly.anomalyType) }}</span>
        </div>

        <div class="anomaly-card__metrics">
          <div class="metric">
            <span class="metric__label">指标值</span>
            <span class="metric__value">{{ formatNumber(anomaly.metricValue) }}</span>
          </div>
          <div class="metric">
            <span class="metric__label">阈值</span>
            <span class="metric__value">{{ formatNumber(anomaly.threshold) }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.anomaly-list {
  min-height: 200px;
}

.anomaly-list__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 64px 24px;
  color: var(--fin-text-secondary);
  font-size: 14px;
}

.anomaly-list__empty .el-icon {
  font-size: 32px;
  color: var(--fin-text-secondary);
  opacity: 0.6;
}

.anomaly-list__empty--error .el-icon {
  color: var(--fin-danger);
  opacity: 1;
}

.anomaly-list__empty-hint {
  font-size: 12px;
  color: var(--fin-text-secondary);
  opacity: 0.7;
  text-align: center;
  max-width: 420px;
}

.anomaly-list__cards {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

@media (max-width: 768px) {
  .anomaly-list__cards {
    grid-template-columns: 1fr;
  }
}

.anomaly-card {
  position: relative;
  padding: 18px 20px;
  overflow: hidden;
  transition:
    box-shadow 0.2s ease,
    transform 0.2s ease;
}

.anomaly-card:hover {
  box-shadow: var(--fin-shadow-card-hover);
  transform: translateY(-2px);
}

.anomaly-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  bottom: 0;
  width: 4px;
  background: var(--fin-info);
}

.anomaly-card--critical::before,
.anomaly-card--error::before {
  background: var(--fin-danger);
}

.anomaly-card--warn::before {
  background: var(--fin-warning);
}

.anomaly-card--info::before {
  background: var(--fin-info);
}

.anomaly-card__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.anomaly-card__title {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.anomaly-card__icon-box {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border-radius: var(--fin-radius-xs);
  font-size: 18px;
  background: var(--fin-info-bg);
  color: var(--fin-info);
}

.anomaly-card__icon-box--danger {
  background: var(--fin-danger-bg);
  color: var(--fin-danger);
}

.anomaly-card__icon-box--warning {
  background: var(--fin-warning-bg);
  color: var(--fin-warning);
}

.anomaly-card__icon-box--info {
  background: var(--fin-info-bg);
  color: var(--fin-info);
}

.anomaly-card__item {
  font-weight: 600;
  font-size: 15px;
  color: var(--fin-text-regular);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.anomaly-card__meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.anomaly-card__type {
  font-size: 12px;
  color: var(--fin-text-secondary);
  background: var(--fin-bg);
  padding: 2px 8px;
  border-radius: var(--fin-radius-xs);
}

.anomaly-card__desc {
  font-size: 13px;
  color: var(--fin-text-regular);
  line-height: 1.6;
  margin: 0 0 12px;
}

.anomaly-card__metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(120px, 1fr));
  gap: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--fin-border);
}

.metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.metric__label {
  font-size: 11px;
  color: var(--fin-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.metric__value {
  font-size: 14px;
  color: var(--fin-text-regular);
  font-family: 'SFMono-Regular', Consolas, monospace;
}
</style>
