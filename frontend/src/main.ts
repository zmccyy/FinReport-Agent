import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import App from './App.vue'
import router from './router'
import { initTheme } from './composables/useTheme'
import './assets/main.css'

// M6.01：暗黑模式初始化须在挂载前（避免首帧闪白）。
initTheme()

const app = createApp(App)

// 顺序重要：pinia 必须先于 router（路由守卫内会访问 store）
app.use(createPinia())
app.use(router)
app.use(ElementPlus)

// 注册所有 Element Plus 图标（M1 全量；后续可按需引入以减小包体）
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.mount('#app')
