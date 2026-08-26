<script setup lang="ts">
/**
 * M6.01 通用错误态组件：文案 + 重试按钮。
 */
interface Props {
  title?: string
  description?: string
  retryable?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  title: '加载失败',
  description: '网络异常或服务暂不可用，请稍后重试',
  retryable: true,
})

const emit = defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="fin-error fin-card">
    <el-icon class="fin-error__icon"><CircleCloseFilled /></el-icon>
    <p class="fin-error__title">{{ props.title }}</p>
    <p class="fin-error__desc">{{ props.description }}</p>
    <el-button v-if="props.retryable" type="primary" plain @click="emit('retry')">
      <el-icon><RefreshRight /></el-icon>重试
    </el-button>
  </div>
</template>

<style scoped>
.fin-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 56px 24px;
  text-align: center;
}

.fin-error__icon {
  font-size: 48px;
  color: var(--fin-danger);
  margin-bottom: 12px;
}

.fin-error__title {
  font-size: 16px;
  font-weight: 700;
  color: var(--fin-text-primary);
}

.fin-error__desc {
  margin: 8px 0 20px;
  font-size: 14px;
  color: var(--fin-text-secondary);
}
</style>
