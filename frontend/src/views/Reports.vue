<script setup lang="ts">
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import AppHeader from '@/components/AppHeader.vue'
import { useReportsStore, type TrackedReport } from '@/stores/reports'

/**
 * 财报列表页（spec §6.5.1 /reports）。
 * M1 期间数据来自 localStorage（后端列表接口 M2+ 补齐）。
 */

const router = useRouter()
const reportsStore = useReportsStore()

const REPORT_TYPE_LABELS: Record<string, string> = {
  ANNUAL: '年报',
  SEMI: '半年报',
  Q1: '一季报',
  Q3: '三季报',
}

function reportTypeLabel(type: string): string {
  return REPORT_TYPE_LABELS[type] ?? type
}

function statusTagType(status: string): 'success' | 'danger' | 'primary' | 'info' | 'warning' {
  if (status === 'COMPLETED') return 'success'
  if (status === 'FAILED') return 'danger'
  if (status === 'CANCELLED') return 'info'
  if (status === 'RUNNING' || status === 'PENDING') return 'primary'
  return 'warning'
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    PENDING: '排队中',
    RUNNING: '解析中',
    COMPLETED: '已完成',
    FAILED: '失败',
    CANCELLED: '已取消',
  }
  return map[status] ?? status
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function viewProgress(row: TrackedReport): void {
  router.push({ name: 'TaskProgress', params: { taskId: row.taskId } })
}

async function removeReport(row: TrackedReport): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除「${row.companyName}」这条记录吗？（仅移除本地记录）`, '删除记录', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  reportsStore.remove(row.taskId)
}

function goUpload(): void {
  router.push({ name: 'ReportUpload' })
}
</script>

<template>
  <div class="page">
    <AppHeader />
    <main class="fin-container page__main">
      <div class="page__head fin-fade-up">
        <div>
          <h2 class="page__title">我的财报</h2>
          <p class="page__sub">共 {{ reportsStore.sorted.length }} 份上传记录</p>
        </div>
        <el-button type="primary" :icon="'Upload'" @click="goUpload">上传财报</el-button>
      </div>

      <!-- 空态 -->
      <div v-if="reportsStore.sorted.length === 0" class="empty fin-card fin-fade-up">
        <el-icon class="empty__icon"><FolderOpened /></el-icon>
        <p class="empty__title">还没有上传任何财报</p>
        <p class="empty__sub">上传一份 PDF 年报，体验解析 → 抽取 → 勾稽 → 报告全流程</p>
        <el-button type="primary" @click="goUpload">立即上传</el-button>
      </div>

      <!-- 列表 -->
      <div v-else class="list fin-card fin-fade-up">
        <el-table :data="reportsStore.sorted" style="width: 100%">
          <el-table-column label="公司" min-width="180">
            <template #default="{ row }">
              <div class="cell-company">
                <span class="cell-company__name">{{ row.companyName }}</span>
                <span class="cell-company__code">{{ row.companyCode }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="报告" width="140">
            <template #default="{ row }">
              {{ reportTypeLabel(row.reportType) }} · {{ row.reportPeriod }}
            </template>
          </el-table-column>
          <el-table-column label="上传时间" width="150">
            <template #default="{ row }">{{ formatTime(row.uploadedAt) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="110" align="center">
            <template #default="{ row }">
              <el-tag :type="statusTagType(row.status)" effect="light" size="small">
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="180" align="center">
            <template #default="{ row }">
              <el-button type="primary" link size="small" @click="viewProgress(row)">
                查看进度
              </el-button>
              <el-button type="danger" link size="small" @click="removeReport(row)">
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
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
}

.page__head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  margin-bottom: 24px;
}

.page__title {
  font-size: 24px;
  font-weight: 700;
}

.page__sub {
  margin-top: 6px;
  font-size: 14px;
  color: var(--fin-text-secondary);
}

.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 72px 24px;
  text-align: center;
}

.empty__icon {
  font-size: 56px;
  color: var(--fin-primary-lighter);
  margin-bottom: 16px;
}

.empty__title {
  font-size: 17px;
  font-weight: 700;
  color: var(--fin-text-primary);
}

.empty__sub {
  margin: 8px 0 24px;
  font-size: 14px;
  color: var(--fin-text-secondary);
}

.list {
  overflow: hidden;
}

.cell-company {
  display: flex;
  flex-direction: column;
}

.cell-company__name {
  font-weight: 600;
  color: var(--fin-text-primary);
}

.cell-company__code {
  font-size: 12px;
  color: var(--fin-text-secondary);
  font-family: Consolas, monospace;
}
</style>
