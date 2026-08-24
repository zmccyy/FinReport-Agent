<script setup lang="ts">
import { computed, ref } from 'vue'
import { CaretRight, CircleCheckFilled, WarningFilled } from '@element-plus/icons-vue'
import type { ReactStep } from '@/types'

/**
 * ReAct 步骤折叠面板 — spec §6.5.2「折叠面板展示 thought/tool_call/tool_result，
 * 工具调用高亮可点击展开」。
 *
 * 每步展示：思考（Thought）、工具调用（等宽 JSON）、工具结果（ok/不可用语义着色）。
 * 流式进行中的步骤自动展开，历史步骤收起。
 */

interface Props {
  steps: ReactStep[]
  /** 是否为进行中的流（最后一步自动展开 + 呼吸动画） */
  streaming: boolean
}

const props = withDefaults(defineProps<Props>(), {
  streaming: false,
})

/** 默认展开除最后一步外的所有步骤（流式时最后一步务必可见）。 */
const expanded = ref<Set<number>>(new Set())

const expandedSteps = computed(() => expanded.value)

function isExpanded(step: number): boolean {
  if (props.streaming) {
    // 流式进行中：全部展开，新步骤透明呈现场景
    return true
  }
  return expandedSteps.value.has(step)
}

function toggle(step: number): void {
  const next = new Set(expanded.value)
  if (next.has(step)) {
    next.delete(step)
  } else {
    next.add(step)
  }
  expanded.value = next
}

function toolLabel(tool: string): string {
  // query_statement → 科目查询；展示层保留工具原名，仅美化空描述
  const known: Record<string, string> = {
    query_statement: '科目查询',
    compute_yoy: '同比计算',
    compute_qoq: '环比计算',
    check_accounting: '勾稽核对',
    search_kb: '知识库检索',
    unit_convert: '单位换算',
  }
  return known[tool] ?? tool
}

/** 工具结果摘要：成功给主结论，业务性不可用时给 reason。 */
function resultSummary(step: ReactStep): string {
  const r = step.toolResult
  if (!r) return ''
  if (r.ok && r.data) {
    const data = r.data as Record<string, unknown>
    if (typeof data.value === 'number') return String(data.value)
    if (Array.isArray(data.hits)) return `${data.hits.length} 条检索结果`
    return '成功'
  }
  return r.reason ?? r.error ?? ''
}
</script>

<template>
  <div class="tool-panel" :class="{ 'tool-panel--streaming': streaming }">
    <div
      v-for="step in steps"
      :key="step.step"
      class="tool-step"
    >
      <button
        class="tool-step__head"
        type="button"
        @click="toggle(step.step)"
      >
        <el-icon class="tool-step__chevron" :class="{ 'is-open': isExpanded(step.step) }">
          <CaretRight />
        </el-icon>
        <span class="tool-step__no">{{ step.step }}</span>
        <span class="tool-step__tool">
          <span v-if="step.tool" class="tool-step__tool-name">{{ toolLabel(step.tool) }}</span>
          <el-icon v-if="step.toolResult" class="tool-step__status" :class="step.toolOk ? 'is-ok' : 'is-bad'">
            <CircleCheckFilled v-if="step.toolOk" />
            <WarningFilled v-else />
          </el-icon>
        </span>
        <span v-if="step.thought" class="tool-step__thought" :title="step.thought">{{ step.thought }}</span>
        <span v-if="resultSummary(step)" class="tool-step__summary">{{ resultSummary(step) }}</span>
      </button>

      <div v-show="isExpanded(step.step)" class="tool-step__body">
        <div v-if="step.thought" class="tool-step__block tool-step__block--thought">
          <span class="tool-step__label">Thought</span>
          <p class="tool-step__text">{{ step.thought }}</p>
        </div>
        <div v-if="step.tool || step.args" class="tool-step__block tool-step__block--call">
          <span class="tool-step__label">Tool call</span>
          <code class="tool-step__code">{{ step.tool }}({{ JSON.stringify(step.args ?? {}, null, 2) }})</code>
        </div>
        <div v-if="step.toolResult" class="tool-step__block" :class="step.toolOk ? 'tool-step__block--ok' : 'tool-step__block--warn'">
          <span class="tool-step__label">Tool result</span>
          <code class="tool-step__code tool-step__code--pre">{{ JSON.stringify(step.toolResult, null, 2) }}</code>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tool-panel {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}

.tool-step {
  border: 1px solid var(--fin-border);
  border-radius: var(--fin-radius-sm);
  background: linear-gradient(180deg, #fafafa 0%, #ffffff 100%);
  overflow: hidden;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.tool-step:hover {
  border-color: var(--fin-border-strong);
}

.tool-panel--streaming .tool-step:last-child {
  border-color: var(--fin-primary-lighter);
  box-shadow: 0 0 0 3px var(--fin-primary-subtle);
  animation: tool-step-pulse 2s ease-in-out infinite;
}

@keyframes tool-step-pulse {
  0%, 100% { box-shadow: 0 0 0 3px var(--fin-primary-subtle); }
  50% { box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.16); }
}

.tool-step__head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: none;
  background: transparent;
  cursor: pointer;
  text-align: left;
  color: var(--fin-text-regular);
}

.tool-step__head:hover {
  background: var(--fin-primary-bg);
}

.tool-step__chevron {
  font-size: 12px;
  color: var(--fin-text-tertiary);
  transition: transform 0.18s ease;
  flex: none;
}

.tool-step__chevron.is-open {
  transform: rotate(90deg);
}

.tool-step__no {
  flex: none;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--fin-primary-bg);
  color: var(--fin-primary);
  font-size: 11px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.tool-step__tool {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: none;
}

.tool-step__tool-name {
  font-weight: 600;
  font-size: 12px;
  white-space: nowrap;
}

.tool-step__status {
  font-size: 14px;
}

.tool-step__status.is-ok {
  color: var(--fin-success);
}

.tool-step__status.is-bad {
  color: var(--fin-warning);
}

.tool-step__thought {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fin-text-secondary);
  font-size: 12px;
}

.tool-step__summary {
  flex: none;
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Consolas, monospace;
  font-size: 11px;
  color: var(--fin-text-tertiary);
}

.tool-step__body {
  border-top: 1px dashed var(--fin-border);
  padding: 10px 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.tool-step__block {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tool-step__label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--fin-text-tertiary);
}

.tool-step__text {
  margin: 0;
  color: var(--fin-text-regular);
  line-height: 1.6;
}

.tool-step__code {
  display: block;
  font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--fin-text-regular);
  background: #f6f7f9;
  border: 1px solid var(--fin-border);
  border-radius: 6px;
  padding: 6px 8px;
  white-space: pre-wrap;
  word-break: break-all;
  overflow-x: auto;
}

.tool-step__block--thought .tool-step__text {
  border-left: 2px solid var(--fin-primary-lighter);
  padding-left: 10px;
  color: var(--fin-text-secondary);
}

.tool-step__block--ok .tool-step__code {
  border-left: 2px solid var(--fin-success);
}

.tool-step__block--warn .tool-step__code {
  border-left: 2px solid var(--fin-warning);
  color: var(--fin-text-secondary);
}
</style>
