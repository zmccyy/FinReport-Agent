<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

/**
 * 认证页面共享顶栏：品牌标识 + 主导航 + 用户菜单。
 */
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const username = computed(() => auth.user?.username ?? '用户')
const activePath = computed(() => route.path)

async function handleLogout(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定退出登录吗？', '退出登录', {
      confirmButtonText: '退出',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return // 用户取消
  }
  await auth.logout()
  router.push({ name: 'Login' })
}
</script>

<template>
  <header class="app-header">
    <div class="app-header__inner fin-container">
      <router-link to="/reports" class="brand">
        <span class="brand__logo">FR</span>
        <span class="brand__name">FinReport Agent</span>
      </router-link>

      <nav class="nav">
        <router-link
          to="/reports"
          class="nav__item"
          :class="{ 'nav__item--active': activePath === '/reports' }"
        >
          我的财报
        </router-link>
        <router-link
          to="/reports/upload"
          class="nav__item"
          :class="{ 'nav__item--active': activePath === '/reports/upload' }"
        >
          上传财报
        </router-link>
      </nav>

      <el-dropdown trigger="click">
        <button class="user" type="button">
          <span class="user__avatar">{{ username.charAt(0).toUpperCase() }}</span>
          <span class="user__name">{{ username }}</span>
          <el-icon class="user__caret"><ArrowDown /></el-icon>
        </button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item disabled>
              <el-icon><User /></el-icon>{{ auth.user?.email || username }}
            </el-dropdown-item>
            <el-dropdown-item divided @click="handleLogout">
              <el-icon><SwitchButton /></el-icon>退出登录
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
  </header>
</template>

<style scoped>
.app-header {
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(255, 255, 255, 0.82);
  border-bottom: 1px solid var(--fin-border);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
}

.app-header__inner {
  display: flex;
  align-items: center;
  height: 56px;
  gap: 32px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  text-decoration: none;
}

.brand__logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--fin-radius-xs);
  background: linear-gradient(135deg, var(--fin-primary) 0%, var(--fin-primary-light) 100%);
  color: #fff;
  font-weight: 700;
  font-size: 12px;
  letter-spacing: 0.5px;
  box-shadow: 0 2px 6px rgba(37, 99, 235, 0.25);
}

.brand__name {
  font-size: 16px;
  font-weight: 700;
  color: var(--fin-text-primary);
}

.nav {
  display: flex;
  gap: 4px;
  flex: 1;
}

.nav__item {
  padding: 8px 14px;
  border-radius: var(--fin-radius-sm);
  font-size: 14px;
  color: var(--fin-text-regular);
  text-decoration: none;
  transition: background 0.15s ease, color 0.15s ease;
}

.nav__item:hover {
  background: var(--fin-primary-bg);
  color: var(--fin-primary);
}

.nav__item--active {
  background: var(--fin-primary-bg);
  color: var(--fin-primary);
  font-weight: 600;
}

.user {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border: none;
  background: transparent;
  border-radius: var(--fin-radius-sm);
  cursor: pointer;
  transition: background 0.15s ease;
}

.user:hover {
  background: var(--fin-primary-bg);
}

.user__avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  background: var(--fin-primary);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
}

.user__name {
  font-size: 14px;
  color: var(--fin-text-primary);
}

.user__caret {
  color: var(--fin-text-secondary);
  font-size: 12px;
}
</style>
