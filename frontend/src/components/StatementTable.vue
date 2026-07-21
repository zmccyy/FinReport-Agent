<script setup lang="ts">
import { computed } from 'vue'
import { useStatementsStore } from '@/stores/statements'
import type { StatementItem } from '@/types'

/**
 * 三表科目展示表格（spec §6.5.1 / plan M2.11）。
 *
 * 单个 Tab 内的科目列表；列：科目名 / 数值（可编辑）/ 单位 / 期间 / scope / 置信度 / 源页。
 * 编辑后的数值仅保留在前端 store，暂不写回后端（plan §M2.11）。
 */

const props = defineProps<{
  /** 表类型：balance_sheet / income_statement / cash_flow */
  statementType: string
  /** 表展示标题 */
  title: string
  /** 简短描述（用于无数据时的占位） */
  hint?: string
}>()

const store = useStatementsStore()

const items = computed<StatementItem[]>(() => {
  switch (props.statementType) {
    case 'balance_sheet':
      return store.statements.balanceSheet
    case 'income_statement':
      return store.statements.incomeStatement
    case 'cash_flow':
      return store.statements.cashFlow
    default:
      return []
  }
})

const hasData = computed(() => items.value.length > 0)

/** 该表内编辑的条目数 */
const editedCount = computed(() => {
  return items.value.filter((it) => isEdited(it)).length
})

function isEdited(item: StatementItem): boolean {
  return `${item.statementType}:${item.id}` in store.edited.values
}

function displayValue(item: StatementItem): string {
  return store.displayValue(item)
}

function onInput(item: StatementItem, value: string): void {
  store.editValue(item.statementType, item.id, value)
}

function confidenceLabel(item: StatementItem): string {
  if (item.confidence == null) return '—'
  return (item.confidence * 100).toFixed(0) + '%'
}

function confidenceType(item: StatementItem): 'success' | 'warning' | 'danger' | 'info' {
  if (item.confidence == null) return 'info'
  if (item.confidence >= 0.9) return 'success'
  if (item.confidence >= 0.7) return 'warning'
  return 'danger'
}
</script>

<template>
  <section class="statement-table">
    <header class="statement-table__head">
      <h3 class="head__title">{{ title }}</h3>
      <span v-if="editedCount > 0" class="head__edited">已编辑 {{ editedCount }} 项</span>
      <span class="head__count">共 {{ items.length }} 项</span>
    </header>

    <div v-if="hasData" class="statement-table__body">
      <el-table :data="items" stripe style="width: 100%" row-key="id" border>
        <el-table-column label="科目" min-width="200">
          <template #default="{ row }">
            <span class="cell-item">{{ row.itemName }}</span>
          </template>
        </el-table-column>

        <el-table-column label="数值" min-width="180">
          <template #default="{ row }">
            <el-input
              :model-value="displayValue(row)"
              size="small"
              placeholder="—"
              class="value-input"
              :class="{ 'value-input--edited': isEdited(row) }"
              @update:model-value="(v: string) => onInput(row, v)"
            />
          </template>
        </el-table-column>

        <el-table-column label="单位" width="80">
          <template #default="{ row }">
            <span class="cell-meta">{{ row.unit || '元' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="期间" width="90">
          <template #default="{ row }">
            <span class="cell-meta">{{ row.periodType || '本期' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="范围" width="90">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" type="info">{{ row.scope || '合并' }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="置信度" width="100" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="confidenceType(row)">{{ confidenceLabel(row) }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="源页" width="80" align="center">
          <template #default="{ row }">
            <span v-if="row.sourcePage != null" class="cell-page">P{{ row.sourcePage }}</span>
            <span v-else class="cell-page cell-page--none">—</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-else class="statement-table__empty">
      <el-icon class="empty__icon"><Document /></el-icon>
      <p class="empty__title">暂无{{ title }}数据</p>
      <p v-if="hint" class="empty__sub">{{ hint }}</p>
    </div>
  </section>
</template>

<style scoped>
.statement-table {
  display: flex;
  flex-direction: column;
}

.statement-table__head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 4px 0 16px;
}

.head__title {
  font-size: 16px;
  font-weight: 700;
  color: var(--fin-text-primary);
}

.head__count {
  margin-left: auto;
  font-size: 13px;
  color: var(--fin-text-secondary);
}

.head__edited {
  font-size: 12px;
  color: var(--fin-warning);
  background: rgba(230, 126, 34, 0.08);
  padding: 2px 8px;
  border-radius: var(--fin-radius-sm);
}

.statement-table__body {
  overflow-x: auto;
}

.value-input :deep(.el-input__wrapper) {
  background: transparent;
  box-shadow: none;
  font-variant-numeric: tabular-nums;
}

.value-input--edited :deep(.el-input__wrapper) {
  background: rgba(230, 126, 34, 0.06);
  box-shadow: inset 0 0 0 1px var(--fin-warning);
}

.cell-item {
  font-weight: 500;
  color: var(--fin-text-primary);
}

.cell-meta {
  font-size: 13px;
  color: var(--fin-text-regular);
}

.cell-page {
  font-size: 12px;
  font-family: 'SFMono-Regular', Consolas, monospace;
  color: var(--fin-text-secondary);
}

.cell-page--none {
  color: var(--fin-text-secondary);
}

.statement-table__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 56px 24px;
  text-align: center;
}

.empty__icon {
  font-size: 48px;
  color: var(--fin-primary-lighter);
  margin-bottom: 12px;
}

.empty__title {
  font-size: 15px;
  font-weight: 600;
  color: var(--fin-text-primary);
}

.empty__sub {
  margin-top: 6px;
  font-size: 13px;
  color: var(--fin-text-secondary);
}
</style>
