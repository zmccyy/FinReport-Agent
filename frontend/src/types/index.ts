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

/**
 * 三表类型常量 — 与 L3 StatementType.value + L2 financial_statement.statement_type 锁定一致
 * （spec §5.2.2 / plan M2.11）。
 */
export const STATEMENT_TYPE = {
  BALANCE_SHEET: 'balance_sheet',
  INCOME_STATEMENT: 'income_statement',
  CASH_FLOW: 'cash_flow',
} as const

export type StatementType = typeof STATEMENT_TYPE[keyof typeof STATEMENT_TYPE]

/** GET /reports/{reportId}/statements 单条科目 — spec §5.2.2 financial_statement 表 */
export interface StatementItem {
  id: number
  statementType: StatementType | string
  itemName: string
  itemValue: number | null
  currency: string
  unit: string
  scope: string
  periodType: string
  confidence: number | null
  sourcePage: number | null
}

/** GET /reports/{reportId}/statements 响应 — 三表分组 */
export interface StatementsResponse {
  balanceSheet: StatementItem[]
  incomeStatement: StatementItem[]
  cashFlow: StatementItem[]
}

/** GET /reports/{reportId} 响应 — 详情页头部元数据 */
export interface ReportDetail {
  id: number
  taskId: string
  companyCode: string
  companyName: string
  reportType: string
  reportPeriod: string
  pageCount: number | null
  parseStatus: string
  pdfObjectKey: string
  createdAt: string
}

/** GET /reports/{reportId}/checks 响应单条 — accounting_check 表 */
export interface AccountingCheck {
  id: number
  ruleName: string
  ruleType: string
  expected: number | null
  actual: number | null
  diff: number | null
  isPass: boolean | null
  severity: string
  note: string
  createdAt: string
}

/** GET /reports/{reportId}/anomalies 响应单条 — anomaly 表 */
export interface AnomalyRecord {
  id: number
  itemName: string
  anomalyType: string
  metricValue: number | null
  threshold: number | null
  description: string
  severity: string
  createdAt: string
}

/** GET /reports/{reportId}/artifacts 响应单条 — report_artifact 表 */
export interface ReportArtifact {
  id: number
  artifactType: 'PDF' | 'MARKDOWN' | 'CHART_PIE' | 'CHART_LINE' | 'CHART_BAR' | string
  objectKey: string
  status: 'GENERATED' | 'FAILED' | string
  downloadUrl: string
  createdAt: string
}

/** 报告页需要的产物分组（由 ReportViewer 消费） */
export interface ReportArtifacts {
  pdf: ReportArtifact | null
  markdown: ReportArtifact | null
  charts: ReportArtifact[]
}
