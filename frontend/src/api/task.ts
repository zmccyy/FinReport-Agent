import http from './http'
import type { TaskInfo } from '@/types'

/**
 * 任务 API（spec §6.2.3）。
 */

/** 查询任务详情。 */
export async function getTask(taskId: string): Promise<TaskInfo> {
  const resp = await http.get<TaskInfo>(`/tasks/${encodeURIComponent(taskId)}`)
  return resp.data
}

/** 取消任务。返回更新后的任务实体。 */
export async function cancelTask(taskId: string): Promise<TaskInfo> {
  const resp = await http.post<TaskInfo>(`/tasks/${encodeURIComponent(taskId)}/cancel`)
  return resp.data
}
