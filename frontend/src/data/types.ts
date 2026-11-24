/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

/** 补采缺口核对结果：应采/已确认/待补日期，以及当前补采任务。 */
export type BackfillTask = {
  task_id: number
  队列编号: string
  采集日期: string
  关联模板: string
  status: string
  占用节点: string
  补采状态: string
  关联批次: string
}

export type BackfillAudit = {
  ok?: boolean
  status?: string
  plan_id: number
  计划编号?: string
  window?: { 生效起日: string; 生效止日: string; 核对截止日: string }
  expected_dates?: string[]
  confirmed_dates?: string[]
  missing_dates?: string[]
  pending_dates?: string[]
  backfill_tasks?: BackfillTask[]
  awaiting_confirmation?: { 批次编号: string; 采集日期: string; status: string }[]
  usable_batches?: { 批次编号: string; 采集日期: string; 回执状态: string; 记录行数?: string }[]
  dates?: string[]
}

/** 补采恢复结果：区分本次新增/复用任务与真实待补日期。 */
export type ResumeResult = {
  ok: boolean
  status: string
  reason?: string
  plan_id: number
  计划编号: string
  计划状态: string
  window: { 生效起日: string; 生效止日: string; 核对截止日: string }
  应采日期: string[]
  已确认采集日期: string[]
  待补日期: string[]
  待补日期数: number
  本次新增补采任务: BackfillTask[]
  复用补采任务: BackfillTask[]
  补采任务: BackfillTask[]
  完成口径?: string
}

/** 批次确认回执：重复提交返回 duplicate，不进入接收方可见范围。 */
export type ConfirmReceipt = {
  ok: boolean
  status: string
  reason?: string
  batch_id?: number
  批次编号?: string
  capture_date?: string | null
  duplicate_of?: string
  receipt?: string
  receipt_status?: string
  closed_backfill_tasks?: number[]
  pending_dates?: string[]
  plan_id?: number | null
}

export type ClaimResult = {
  ok: boolean
  status: string
  reason?: string
  task_id?: number
  采集日期?: string
  pending_dates: string[]
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
