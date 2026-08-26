<script setup lang="ts">
/**
 * M6.01 通用页/块加载态：居中 spinner + 可选文案。
 */
interface Props {
  text?: string
  block?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  text: '加载中…',
  block: true,
})
</script>

<template>
  <div class="fin-loading" :class="{ 'fin-loading--block': props.block }">
    <el-icon class="is-loading fin-loading__icon"><Loading /></el-icon>
    <p class="fin-loading__text">{{ props.text }}</p>
  </div>
</template>

<style scoped>
.fin-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
}

.fin-loading--block {
  padding: 72px 24px;
}

.fin-loading__icon {
  font-size: 32px;
  color: var(--fin-primary);
}

.fin-loading__text {
  font-size: 14px;
  color: var(--fin-text-secondary);
}
</style>
