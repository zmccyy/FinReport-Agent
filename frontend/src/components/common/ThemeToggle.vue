<script setup lang="ts">
/**
 * M6.01 通用主题切换按钮：复用 useTheme（html.dark class + localStorage 持久化）。
 * AppHeader（登录后）与 Login（登录前）共用，保证暗黑模式全程可切换。
 */
import { useTheme } from '@/composables/useTheme'

const { isDark, toggle } = useTheme()
</script>

<template>
  <button
    class="theme-toggle"
    type="button"
    :aria-label="isDark ? '切换到浅色模式' : '切换到深色模式'"
    :title="isDark ? '切换到浅色模式' : '切换到深色模式'"
    @click="toggle"
  >
    <el-icon><Moon v-if="!isDark" /><Sunny v-else /></el-icon>
  </button>
</template>

<style scoped>
.theme-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: none;
  border-radius: var(--fin-radius-sm);
  background: transparent;
  color: var(--fin-text-secondary);
  cursor: pointer;
  font-size: 17px;
  transition: background 0.15s ease, color 0.15s ease;
}

.theme-toggle:hover {
  background: var(--fin-primary-bg);
  color: var(--fin-primary);
}
</style>
