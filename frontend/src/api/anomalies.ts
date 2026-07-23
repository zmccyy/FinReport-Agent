import http from './http'
import type { AnomalyRecord } from '@/types'

/**
 * 异常检测结果 API（spec §6.2.2 / M3.09）。
 *
 * 后端契约：
 * - GET /api/v1/reports/{reportId}/anomalies → AnomalyResponse[]
 *
 * 鉴权：JwtFilter 注入 X-User-Id 头；归属校验在后端 AnomalyQueryService 完成，
 * 不归属时返回 404 REPORT_NOT_FOUND。
 */

/**
 * 查询某份报告的所有异常，后端已按严重度降序排列（CRITICAL > ERROR > WARN > INFO）。
 *
 * @param reportId 财报 ID
 * @returns 异常列表
 */
export async function getAnomalies(reportId: number): Promise<AnomalyRecord[]> {
  const resp = await http.get<AnomalyRecord[]>(`/reports/${reportId}/anomalies`)
  return resp.data
}
