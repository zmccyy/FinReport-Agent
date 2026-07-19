/**
 * 统一 API 错误类型（RFC 9457，见 spec §6.6 / CLAUDE.md §10）。
 *
 * 后端错误体结构：
 *   { type, title, status, detail, instance, traceId, errors?[{field,message}] }
 */

export interface ApiErrorShape {
  status: number
  code: string
  message: string
  traceId?: string
  /** 字段级校验错误（422 VALIDATION_FAILED） */
  fieldErrors?: Array<{ field: string; message: string }>
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly traceId?: string
  readonly fieldErrors?: Array<{ field: string; message: string }>

  constructor(shape: ApiErrorShape) {
    super(shape.message)
    this.name = 'ApiError'
    this.status = shape.status
    this.code = shape.code
    this.traceId = shape.traceId
    this.fieldErrors = shape.fieldErrors
  }
}

/** RFC 9457 错误体的最小结构（其余字段忽略） */
interface ProblemDetails {
  type?: string
  title?: string
  status?: number
  detail?: string
  traceId?: string
  errors?: Array<{ field: string; message: string }>
}

/**
 * 从 type URL 提取错误码。
 * `https://finreport.example/errors/VALIDATION_FAILED` → `VALIDATION_FAILED`
 */
function codeFromType(type?: string): string | null {
  if (!type) return null
  const idx = type.lastIndexOf('/')
  return idx >= 0 ? type.slice(idx + 1) : null
}

/**
 * 把后端 RFC 9457 错误体解析为 ApiError。
 *
 * @param data 响应体（可能是 ProblemDetails 或空）
 * @param httpStatus HTTP 状态码
 * @param fallbackMessage 解析失败时的兜底文案
 */
export function toApiError(
  data: unknown,
  httpStatus: number,
  fallbackMessage = '请求失败，请稍后重试'
): ApiError {
  const problem = (data ?? {}) as ProblemDetails
  const message = problem.detail || problem.title || fallbackMessage
  const code = codeFromType(problem.type) || `HTTP_${httpStatus}`
  return new ApiError({
    status: problem.status ?? httpStatus,
    code,
    message,
    traceId: problem.traceId,
    fieldErrors: problem.errors,
  })
}
