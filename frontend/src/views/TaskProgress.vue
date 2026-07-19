<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import AppHeader from '@/components/AppHeader.vue'
import ProgressCard from '@/components/ProgressCard.vue'
import { useTaskStore } from '@/stores/task'
import { useReportsStore } from '@/stores/reports'
import { cancelTask } from '@/api/task'
import { ApiError } from '@/api/errors'

/**
 * 解析进度页（/tasks/:taskId/progress）。
 * 挂载即订阅 SSE，卸载断开；实时渲染 4 阶段进度卡片。
 */

const route = useRoute()
const router = useRouter()
const task = useTaskStore()
const reportsStore = useReportsStore()

const taskId = computed(() => route.params.taskId as string)
const cancelling = ref(false)

onMounted(() => {
  // 从列表记录中取 reportId 作为初值（SSE done 事件会用权威值覆盖）
  const tracked = reportsStore.reports.find((r) => r.taskId === taskId.value)
  task.start(taskId.value, tracked?.reportId ?? null)
})

onBeforeUnmount(() => {
  task.stop()
})

// 任务状态变化同步回列表记录（用于列表页展示最新状态）
watch(
  () => task.taskStatus,
  (status) => {
    reportsStore.updateStatus(taskId.value, status, task.reportId)
  }
)

const isRunning = computed(() => task.taskStatus === 'RUNNING' || task.taskStatus === 'PENDING')

async function handleCancel(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定取消该解析任务吗？', '取消任务', {
      confirmButtonText: '取消任务',
      cancelButtonText: '再想想',
      type: 'warning',
    })
  } catch {
    return
  }
  cancelling.value = true
  try {
    await cancelTask(taskId.value)
    ElMessage.success('任务已取消')
  } catch (err) {
    const msg = err instanceof ApiError ? err.message : '取消失败'
    ElMessage.error(msg)
  } finally {
    cancelling.value = false
  }
}

function goReports(): void {
  router.push({ name: 'Reports' })
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
        <h2 class="page__title">财报解析</h2>
        <p class="page__sub">实时跟踪 PDF 解析、科目抽取、勾稽核对与报告生成进度</p>
      </div>

      <ProgressCard class="fin-fade-up">
        <template #done-action>
          <el-button type="primary" plain @click="goReports">返回列表</el-button>
        </template>
      </ProgressCard>

      <div class="page__actions fin-fade-up">
        <el-button v-if="isRunning" type="danger" plain :loading="cancelling" @click="handleCancel">
          取消任务
        </el-button>
        <el-button @click="goReports">返回列表</el-button>
        <el-button type="primary" plain @click="goUpload">再传一份</el-button>
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
  max-width: 860px;
}

.page__head {
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

.page__actions {
  display: flex;
  justify-content: center;
  gap: 12px;
  margin-top: 24px;
}
</style>
