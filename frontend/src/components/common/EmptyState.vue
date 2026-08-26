<script setup lang="ts">
/**
 * M6.01 通用空态组件：图标 + 标题 + 描述 + 可选主操作。
 */
interface Props {
  title: string
  description?: string
  icon?: string
}

const props = withDefaults(defineProps<Props>(), {
  description: '',
  icon: 'FolderOpened',
})
</script>

<template>
  <div class="fin-empty fin-card">
    <el-icon class="fin-empty__icon"><component :is="props.icon" /></el-icon>
    <p class="fin-empty__title">{{ props.title }}</p>
    <p v-if="props.description" class="fin-empty__desc">{{ props.description }}</p>
    <div v-if="$slots.action" class="fin-empty__action">
      <slot name="action" />
    </div>
  </div>
</template>

<style scoped>
.fin-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 72px 24px;
  text-align: center;
}

.fin-empty__icon {
  font-size: 56px;
  color: var(--fin-primary-lighter);
  margin-bottom: 16px;
}

.fin-empty__title {
  font-size: 17px;
  font-weight: 700;
  color: var(--fin-text-primary);
}

.fin-empty__desc {
  margin: 8px 0 0;
  font-size: 14px;
  color: var(--fin-text-secondary);
}

.fin-empty__action {
  margin-top: 24px;
}
</style>
