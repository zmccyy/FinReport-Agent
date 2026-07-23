import http from './http'
import type { AccountingCheck } from '@/types'

/**
 * 勾稽核对结果 API（spec §6.2.2 / M3.09）。
 *
 * 后端契约：
 * - GET /api/v1/reports/{reportId}/checks → AccountingCheckResponse[]
 *
 * 鉴权：JwtFilter 注入 X-User-Id 头；归属校验在后端 CheckQueryService 完成，
 * 不归属时返回 404 REPORT_NOT_FOUND。
 */

/**
 * 查询某份报告的所有勾稽结果。
 *
 * @param reportId 财报 ID
 * @returns 勾稽结果列表（按 ruleType 排序）
 */
export async function getChecks(reportId: number): Promise<AccountingCheck[]> {
  const resp = await http.get<AccountingCheck[]>(`/reports/${reportId}/checks`)
  return resp.data
}
