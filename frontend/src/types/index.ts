/**
 * 认证与任务相关的 TypeScript 类型定义。
 *
 * 与后端 DTO 对齐（见 backend/.../domain/dto 与 spec §6.2）。
 */

/** POST /auth/{register,login,refresh} 响应 — TokenResponse record */
export interface TokenResponse {
  accessToken: string
  refreshToken: string
  /** access token 有效期（秒） */
  expiresIn: number
  tokenType: string
}

/** GET /users/me 响应 — UserInfoResponse record */
export interface UserInfo {
  id: number
  username: string
  email: string
  role: string
  createdAt: string
}

/** POST /reports/upload 响应 — UploadResponse record */
export interface UploadResponse {
  taskId: string
  reportId: number
  status: string
}

/** 上传财报时的元数据（multipart 各部分） */
export interface UploadMeta {
  companyCode: string
  companyName: string
  reportType: string
  reportPeriod: string
}

/**
 * 后端任务步骤名（TaskStepName）。
 * EXTRACT 阶段拆为三表并行：EXTRACT_BS（资产负债）/ EXTRACT_IS（利润）/ EXTRACT_CF（现金流）。
 */
export type TaskStepName = 'PARSE' | 'EXTRACT_BS' | 'EXTRACT_IS' | 'EXTRACT_CF' | 'CHECK' | 'REPORT'

/** SSE progress 事件 data — {taskId, step, status, progress, result?} */
export interface ProgressEvent {
  taskId: string
  step: TaskStepName
  /** 步骤状态：RUNNING / SUCCESS / FAILED / RETRY 等 */
  status: string
  /** 整体进度 0-100 */
  progress: number
  result?: Record<string, unknown>
}

/** SSE done 事件 data — {taskId, reportId?, status?} */
export interface DoneEvent {
  taskId: string
  reportId?: number
  /** COMPLETED / CANCELLED */
  status?: string
}

/** SSE error 事件 data — {taskId, step?, code, message} */
export interface ErrorEvent {
  taskId: string
  step?: string
  code: string
  message: string
}

/** GET /tasks/{id} 响应 — Task 实体（仅前端需要的字段） */
export interface TaskInfo {
  id: string
  userId: number
  taskType: string
  refReportId?: number
  status: string
  currentStep?: string
  progress?: number
  errorMsg?: string
  createdAt?: string
  startedAt?: string
  finishedAt?: string
}
