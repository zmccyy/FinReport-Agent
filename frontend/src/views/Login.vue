<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { FormInstance, FormRules } from 'element-plus'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { ApiError } from '@/api/errors'

/**
 * 登录 / 注册页（spec §6.5.1 /login）。
 * 校验规则与后端 RegisterRequest / LoginRequest 对齐。
 */

type Mode = 'login' | 'register'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const mode = ref<Mode>('login')
const loading = ref(false)
const errorMessage = ref<string | null>(null)

const formRef = ref<FormInstance>()
const form = reactive({
  username: '',
  password: '',
  confirmPassword: '',
  email: '',
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 64, message: '用户名长度须在 3-64 之间', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 128, message: '密码长度须在 6-128 之间', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_r, value: string, cb) =>
        value === form.password ? cb() : cb(new Error('两次输入的密码不一致')),
      trigger: 'blur',
    },
  ],
  email: [{ type: 'email', message: '邮箱格式不正确', trigger: 'blur' }],
}

function switchMode(next: Mode): void {
  mode.value = next
  errorMessage.value = null
  formRef.value?.clearValidate()
}

async function submit(): Promise<void> {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  errorMessage.value = null
  try {
    if (mode.value === 'login') {
      await auth.login(form.username, form.password)
      ElMessage.success('登录成功')
    } else {
      await auth.register(form.username, form.password, form.email || undefined)
      ElMessage.success('注册成功，已自动登录')
    }
    const redirect = (route.query.redirect as string) || '/reports'
    router.push(redirect)
  } catch (err) {
    errorMessage.value =
      err instanceof ApiError ? err.message : '操作失败，请稍后重试'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth">
    <!-- 品牌区 -->
    <aside class="auth__brand">
      <div class="brand__inner">
        <div class="brand__logo">FR</div>
        <h1 class="brand__title">FinReport Agent</h1>
        <p class="brand__subtitle">A 股上市公司财报深度解析 Agent</p>
        <ul class="brand__features">
          <li><el-icon><Document /></el-icon><span>PDF 财报结构化解析</span></li>
          <li><el-icon><DataAnalysis /></el-icon><span>三表勾稽核对与异常检测</span></li>
          <li><el-icon><ChatDotRound /></el-icon><span>基于 ReAct 的智能问答</span></li>
        </ul>
      </div>
    </aside>

    <!-- 表单区 -->
    <main class="auth__panel">
      <div class="panel__card fin-fade-up">
        <el-tabs :model-value="mode" class="panel__tabs" @update:model-value="switchMode">
          <el-tab-pane label="登录" name="login" />
          <el-tab-pane label="注册" name="register" />
        </el-tabs>

        <el-alert
          v-if="errorMessage"
          :title="errorMessage"
          type="error"
          show-icon
          class="panel__alert"
          @close="errorMessage = null"
        />

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          label-position="top"
          size="large"
          @submit.prevent="submit"
        >
          <el-form-item label="用户名" prop="username">
            <el-input
              v-model="form.username"
              placeholder="3-64 位用户名"
              :prefix-icon="'User'"
              autocomplete="username"
              clearable
            />
          </el-form-item>

          <el-form-item v-if="mode === 'register'" label="邮箱（可选）" prop="email">
            <el-input
              v-model="form.email"
              placeholder="you@example.com"
              :prefix-icon="'Message'"
              autocomplete="email"
              clearable
            />
          </el-form-item>

          <el-form-item label="密码" prop="password">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="6-128 位密码"
              :prefix-icon="'Lock'"
              :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
              show-password
            />
          </el-form-item>

          <el-form-item v-if="mode === 'register'" label="确认密码" prop="confirmPassword">
            <el-input
              v-model="form.confirmPassword"
              type="password"
              placeholder="再次输入密码"
              :prefix-icon="'Lock'"
              autocomplete="new-password"
              show-password
              @keyup.enter="submit"
            />
          </el-form-item>

          <el-button
            type="primary"
            class="panel__submit"
            :loading="loading"
            native-type="submit"
            @click="submit"
          >
            {{ mode === 'login' ? '登 录' : '注册并登录' }}
          </el-button>
        </el-form>

        <p class="panel__hint">
          {{ mode === 'login' ? '还没有账号？' : '已有账号？' }}
          <a class="panel__link" @click="switchMode(mode === 'login' ? 'register' : 'login')">
            {{ mode === 'login' ? '立即注册' : '返回登录' }}
          </a>
        </p>
      </div>
    </main>
  </div>
</template>

<style scoped>
.auth {
  display: flex;
  min-height: 100vh;
}

/* 品牌区（左） */
.auth__brand {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px;
  background: linear-gradient(150deg, var(--fin-primary) 0%, #14405e 55%, #0e2f46 100%);
  color: #fff;
}

.brand__logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.12);
  border: 1px solid rgba(255, 255, 255, 0.25);
  font-size: 22px;
  font-weight: 700;
  margin-bottom: 24px;
}

.brand__title {
  font-size: 30px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.brand__subtitle {
  margin-top: 10px;
  font-size: 15px;
  opacity: 0.82;
}

.brand__features {
  margin-top: 40px;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.brand__features li {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 14px;
  opacity: 0.9;
}

.brand__features .el-icon {
  font-size: 18px;
  color: var(--fin-primary-lighter);
}

/* 表单区（右） */
.auth__panel {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
  background: var(--fin-bg);
}

.panel__card {
  width: 100%;
  max-width: 400px;
  background: var(--fin-card-bg);
  border: 1px solid var(--fin-border);
  border-radius: var(--fin-radius);
  box-shadow: var(--fin-shadow-md);
  padding: 32px 32px 24px;
}

.panel__tabs {
  margin-bottom: 8px;
}

.panel__tabs :deep(.el-tabs__item) {
  font-size: 16px;
}

.panel__alert {
  margin-bottom: 18px;
}

.panel__submit {
  width: 100%;
  margin-top: 6px;
  font-size: 16px;
  letter-spacing: 2px;
}

.panel__hint {
  margin-top: 20px;
  text-align: center;
  font-size: 13px;
  color: var(--fin-text-secondary);
}

.panel__link {
  color: var(--fin-primary-light);
  cursor: pointer;
  font-weight: 600;
}

.panel__link:hover {
  text-decoration: underline;
}

/* 窄屏堆叠 */
@media (max-width: 860px) {
  .auth {
    flex-direction: column;
  }
  .auth__brand {
    padding: 40px 24px;
  }
  .brand__features {
    margin-top: 24px;
  }
}
</style>
