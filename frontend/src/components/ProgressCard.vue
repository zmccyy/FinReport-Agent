<script setup lang="ts">
import { computed } from 'vue'
import { useTaskStore, type StageKey, type StageStatus } from '@/stores/task'

/**
 * 4 阶段解析进度卡片（spec §6.5.2）。
 *
 * 领域组件：直接绑定 useTaskStore（SSE 进度状态机），
 * 展示 PARSE → EXTRACT（三表）→ CHECK → REPORT 的可视化进度。
 */

const task = useTaskStore()

interface StageMeta {
  key: StageKey
  label: string
  desc: string
}

const STAGE_META: StageMeta[] = [
  { key: 'PARSE', label: '文档解析', desc: 'PDF → 结构化文本' },
  { key: 'EXTRACT', label: '科目抽取', desc: '三表数据提取' },
  { key: 'CHECK', label: '勾稽核对', desc: '平衡关系校验' },
  { key: 'REPORT', label: '报告生成', desc: 'NLG 分析报告' },
]

/** EXTRACT 三表子步骤展示名 */
const EXTRACT_LABELS: Record<string, string> = {
  EXTRACT_BS: '资产负债表',
  EXTRACT_IS: '利润表',
  EXTRACT_CF: '现金流量表',
}

const progressStatus = computed(() => {
  if (task.taskStatus === 'FAILED') return 'exception'
  if (task.taskStatus === 'COMPLETED') return 'success'
  return undefined
})

/** StageStatus → el-steps 步骤状态 */
function stepStatus(status: StageStatus): 'wait' | 'process' | 'finish' | 'error' {
  switch (status) {
    case 'active':
      return 'process'
    case 'success':
      return 'finish'
    case 'failed':
      return 'error'
    default:
      return 'wait'
  }
}

const statusText = computed(() => {
  switch (task.taskStatus) {
    case 'COMPLETED':
      return '解析完成'
    case 'FAILED':
      return '解析失败'
    case 'CANCELLED':
      return '已取消'
    case 'RUNNING':
      return '解析中…'
    default:
      return '排队中…'
  }
})

const statusTagType = computed(() => {
  switch (task.taskStatus) {
    case 'COMPLETED':
      return 'success'
    case 'FAILED':
      return 'danger'
    case 'CANCELLED':
      return 'info'
    default:
      return 'primary'
  }
})

/** EXTRACT 是否展示三表子步骤 */
const hasExtractSteps = computed(() => Object.keys(task.extractSteps).length > 0)

function extractTagType(status: StageStatus): 'success' | 'danger' | 'primary' | 'info' {
  switch (status) {
    case 'success':
      return 'success'
    case 'failed':
      return 'danger'
    case 'active':
      return 'primary'
    default:
      return 'info'
  }
}
</script>

<template>
  <div class="progress-card fin-card">
    <header class="progress-card__head">
      <div class="head__title">
        <h3>解析进度</h3>
        <span v-if="task.taskId" class="head__task-id">任务 {{ task.taskId.slice(0, 8) }}…</span>
      </div>
      <div class="head__status">
        <span class="conn" :class="{ 'conn--on': task.connected }" :title="task.connected ? 'SSE 已连接' : 'SSE 连接中'">
          <span class="conn__dot" />{{ task.connected ? '实时' : '连接中' }}
        </span>
        <el-tag :type="statusTagType" effect="light">{{ statusText }}</el-tag>
      </div>
    </header>

    <el-progress
      :percentage="task.overallProgress"
      :status="progressStatus"
      :stroke-width="14"
      striped
      :striped-flow="task.taskStatus === 'RUNNING'"
      class="progress-card__bar"
    />

    <el-steps :active="4" align-center class="progress-card__steps">
      <el-step
        v-for="meta in STAGE_META"
        :key="meta.key"
        :title="meta.label"
        :status="stepStatus(task.stages[meta.key])"
      >
        <template #description>
          <span class="step__desc">{{ meta.desc }}</span>
        </template>
      </el-step>
    </el-steps>

    <!-- EXTRACT 三表子步骤 -->
    <div v-if="hasExtractSteps" class="extract-steps">
      <span class="extract-steps__label">三表明细：</span>
      <el-tag
        v-for="(status, step) in task.extractSteps"
        :key="step"
        :type="extractTagType(status)"
        size="small"
        effect="plain"
        class="extract-steps__tag"
      >
        {{ EXTRACT_LABELS[step] ?? step }}
      </el-tag>
    </div>

    <el-alert
      v-if="task.taskStatus === 'FAILED' && task.errorMessage"
      :title="task.errorMessage"
      type="error"
      show-icon
      :closable="false"
      class="progress-card__error"
    />

    <div v-if="task.taskStatus === 'COMPLETED'" class="progress-card__done">
      <el-icon class="done__icon"><CircleCheckFilled /></el-icon>
      <div class="done__text">
        <p class="done__title">解析完成</p>
        <p class="done__sub">报告 ID：{{ task.reportId ?? '—' }}，三表抽取与勾稽核对已就绪</p>
      </div>
      <slot name="done-action" />
    </div>
  </div>
</template>

<style scoped>
.progress-card {
  padding: 28px 28px 24px;
}

.progress-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.head__title {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.head__title h3 {
  font-size: 17px;
  font-weight: 700;
}

.head__task-id {
  font-size: 12px;
  color: var(--fin-text-secondary);
  font-family: 'SFMono-Regular', Consolas, monospace;
}

.head__status {
  display: flex;
  align-items: center;
  gap: 12px;
}

.conn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--fin-text-secondary);
}

.conn__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #d0d6dd;
  transition: background 0.2s ease;
}

.conn--on .conn__dot {
  background: var(--fin-success);
  box-shadow: 0 0 0 3px rgba(39, 174, 96, 0.18);
}

.progress-card__bar {
  margin-bottom: 28px;
}

.progress-card__steps {
  margin-bottom: 8px;
}

.step__desc {
  font-size: 12px;
  color: var(--fin-text-secondary);
}

.extract-steps {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 16px;
  padding: 12px 16px;
  background: var(--fin-primary-bg);
  border-radius: var(--fin-radius-sm);
}

.extract-steps__label {
  font-size: 13px;
  color: var(--fin-text-regular);
}

.progress-card__error {
  margin-top: 16px;
}

.progress-card__done {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 20px;
  padding: 16px 18px;
  background: #f0f9f2;
  border: 1px solid #c7ecd2;
  border-radius: var(--fin-radius-sm);
}

.done__icon {
  font-size: 32px;
  color: var(--fin-success);
}

.done__title {
  font-size: 15px;
  font-weight: 700;
  color: var(--fin-text-primary);
}

.done__sub {
  font-size: 13px;
  color: var(--fin-text-regular);
  margin-top: 2px;
}
</style>
