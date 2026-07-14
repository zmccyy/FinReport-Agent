import axios from 'axios'

const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 请求拦截器：注入 traceId + token
http.interceptors.request.use((config) => {
  // traceId（前端生成）
  const traceId = crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  config.headers['X-Trace-Id'] = traceId

  // JWT token
  const token = localStorage.getItem('access_token')
  if (token) {
    config.headers['Authorization'] = `Bearer ${token}`
  }

  return config
})

// 响应拦截器：401 跳转登录
http.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('access_token')
      localStorage.removeItem('refresh_token')
      window.location.href = '/login'
    }
    return Promise.reject(error)
  }
)

export default http
