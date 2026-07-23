import http from './http'
import type { ReportArtifact } from '@/types'

/**
 * 报告产物 API（spec §6.2.2 / M3.09）。
 *
 * 后端契约：
 * - GET /api/v1/reports/{reportId}/artifacts → ReportArtifactResponse[]
 *
 * 鉴权：JwtFilter 注入 X-User-Id 头；归属校验在后端 ArtifactQueryService 完成，
 * 不归属时返回 404 REPORT_NOT_FOUND。
 */

/**
 * 查询某份报告的所有产物（PDF / Markdown / 图表 PNG）。
 *
 * GENERATED 产物附带 MinIO 预签名下载 URL；FAILED 产物 downloadUrl 为空字符串。
 *
 * @param reportId 财报 ID
 * @returns 产物列表（按 artifactType 排序）
 */
export async function getArtifacts(reportId: number): Promise<ReportArtifact[]> {
  const resp = await http.get<ReportArtifact[]>(`/reports/${reportId}/artifacts`)
  return resp.data
}

/**
 * 通过 MinIO 预签名 URL 拉取 Markdown 原文。
 *
 * 预签名 URL 已含鉴权签名，不能使用带额外 Header（Authorization / X-Trace-Id）的
 * axios 实例，因此用裸 fetch；异常由 ReportViewer 统一处理。
 *
 * @param url 预签名下载 URL
 * @returns Markdown 文本
 */
export async function getMarkdownText(url: string): Promise<string> {
  const resp = await fetch(url)
  if (!resp.ok) {
    throw new Error(`下载失败: ${resp.status}`)
  }
  return resp.text()
}
