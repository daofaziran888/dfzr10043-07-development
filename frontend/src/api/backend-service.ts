import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  BackfillAudit,
  ClaimResult,
  ConfirmReceipt,
  EntryRow,
  OverviewResult,
  PageResult,
  ResumeResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ── 恢复排程补采 ──────────────────────────────────────────────

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) throw new Error(fallback)
  return (await response.json()) as T
}

export async function auditScheduleGaps(planId: number, asOf = ''): Promise<BackfillAudit> {
  const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''
  const response = await request(`/api/schedule_plan/${planId}/gaps${query}`)
  return readJson<BackfillAudit>(response, '采集缺口核对失败')
}

export async function resumeSchedule(
  planId: number,
  options: { asOf?: string; dispatch?: boolean; operator?: string } = {},
): Promise<ResumeResult> {
  const response = await request(`/api/schedule_plan/${planId}/resume`, {
    method: 'POST',
    body: JSON.stringify({
      as_of: options.asOf || null,
      dispatch: options.dispatch ?? false,
      operator: options.operator ?? 'duty',
    }),
  })
  return readJson<ResumeResult>(response, '恢复排程失败')
}

export async function receiverCaptureData(planId: number, asOf = ''): Promise<BackfillAudit> {
  const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''
  const response = await request(`/api/schedule_plan/${planId}/receiver${query}`)
  return readJson<BackfillAudit>(response, '接收方数据读取失败')
}

export async function claimBackfillTask(taskId: number, nodeCode = ''): Promise<ClaimResult> {
  const response = await request(`/api/task_queue/${taskId}/claim`, {
    method: 'POST',
    body: JSON.stringify({ node_code: nodeCode }),
  })
  return readJson<ClaimResult>(response, '领取补采任务失败')
}

export async function confirmCaptureBatch(batchId: number, asOf = ''): Promise<ConfirmReceipt> {
  const response = await request(`/api/capture_batch/${batchId}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ as_of: asOf || null, operator: '值班员' }),
  })
  return readJson<ConfirmReceipt>(response, '确认采集批次失败')
}
