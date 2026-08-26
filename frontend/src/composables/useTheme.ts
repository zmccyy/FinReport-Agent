import { ref } from 'vue'

/**
 * M6.01 暗黑模式切换：localStorage 持久化 + html.dark class 驱动
 * （Element Plus dark css-vars + main.css 的 --fin-* 暗色覆盖）。
 */

const THEME_KEY = 'finreport-theme'
const isDark = ref<boolean>(false)

function applyTheme(dark: boolean): void {
  isDark.value = dark
  document.documentElement.classList.toggle('dark', dark)
  localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light')
}

/** 初始化主题（main.ts 挂载前调用一次；跟随系统偏好）。 */
export function initTheme(): void {
  const saved = localStorage.getItem(THEME_KEY)
  const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
  applyTheme(saved === 'dark' || (saved === null && prefersDark))
}

/** 主题切换 composable。 */
export function useTheme() {
  const toggle = (): void => applyTheme(!isDark.value)
  return { isDark, toggle }
}
