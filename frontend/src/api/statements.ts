import http from './http'
import type { ReportDetail, StatementsResponse } from '@/types'

/**
 * 财报详情与三表数据 API（spec §6.2.2 / M2.11）。
 *
 * 后端契约：
 * - GET /api/v1/reports/{reportId}                       → ReportDetailResponse
 * - GET /api/v1/reports/{reportId}/statements            → StatementsResponse
 *
 * 鉴权：JwtFilter 注入 X-User-Id 头；归属校验在后端 StatementQueryService 完成，
 * 不归属时返回 404 REPORT_NOT_FOUND（前端按 ApiError.code 处理）。
 */

/**
 * 查询财报详情（公司、报告期间、PDF 元数据等）。
 *
 * @param reportId 财报 ID
 * @returns ReportDetail（含 taskId/companyCode/parseStatus 等）
 */
export async function getReportDetail(reportId: number): Promise<ReportDetail> {
  const resp = await http.get<ReportDetail>(`/reports/${reportId}`)
  return resp.data
}

/**
 * 查询三表数据（BS / IS / CF 一次性返回）。
 *
 * @param reportId 财报 ID
 * @returns StatementsResponse，三张表分别对应 balanceSheet/incomeStatement/cashFlow 字段
 */
export async function getStatements(reportId: number): Promise<StatementsResponse> {
  const resp = await http.get<StatementsResponse>(`/reports/${reportId}/statements`)
  return resp.data
}
