import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_BASE_URL || 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // M6.02 vendor 拆分：element-plus / vue 系 / 应用代码互为长缓存 chunk，
    // 小功能变更不失效大包；懒加载路由 chunk 从主包独立并行加载
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-vue': ['vue', 'vue-router', 'pinia'],
          'vendor-element': ['element-plus', '@element-plus/icons-vue'],
        },
      },
    },
    // Element Plus 全量引入（M1 起）：vendor-element ≥ 1MB 属预期，不视为告警
    chunkSizeWarningLimit: 1200,
  },
})
