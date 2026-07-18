import http from './http'
import type { UploadMeta, UploadResponse } from '@/types'

/**
 * 财报上传 API（spec §6.2.2 / §6.3.1）。
 */

// 大文件上传放宽超时（默认 30s 不足以传 50MB PDF）
const UPLOAD_TIMEOUT_MS = 120_000

/**
 * 上传 PDF 财报。
 *
 * multipart/form-data，part 名与后端 ReportController 对齐：
 * file / companyCode / companyName / reportType / reportPeriod。
 *
 * @param file PDF 文件
 * @param meta 公司代码/名称、报告类型、报告期间
 * @param idempotencyKey 幂等键（重复提交返回原 taskId，见 CLAUDE.md §8.4）
 * @param onUploadProgress 上传进度回调（percent 0-100）
 */
export async function uploadReport(
  file: File,
  meta: UploadMeta,
  idempotencyKey?: string,
  onUploadProgress?: (percent: number) => void
): Promise<UploadResponse> {
  const form = new FormData()
  form.append('file', file)
  form.append('companyCode', meta.companyCode)
  form.append('companyName', meta.companyName)
  form.append('reportType', meta.reportType)
  form.append('reportPeriod', meta.reportPeriod)

  const resp = await http.post<UploadResponse>('/reports/upload', form, {
    timeout: UPLOAD_TIMEOUT_MS,
    headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
    onUploadProgress: (e) => {
      if (onUploadProgress && e.total) {
        onUploadProgress(Math.round((e.loaded / e.total) * 100))
      }
    },
  })
  return resp.data
}
