<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section v-if="meta.key === 'schedule_plan'" class="backfill-panel" data-testid="backfill-panel">
      <header class="backfill-head">
        <div>
          <h3>恢复排程补采核对</h3>
          <p class="page-desc">
            按计划原本生效窗口核对已确认采集批次，只把漏采日期派发补采；入队不算完成。
          </p>
        </div>
        <div class="page-actions">
          <label class="backfill-date">
            <span>核对截止日</span>
            <input v-model="auditDate" data-testid="audit-date" type="date" />
          </label>
          <button class="btn" data-testid="audit-gaps" type="button" :disabled="!selectedPlanId" @click="auditGaps">
            核对缺口
          </button>
          <button
            class="btn primary"
            data-testid="resume-backfill"
            type="button"
            :disabled="!selectedPlanId"
            @click="resumeBackfill(false)"
          >
            恢复并派发补采
          </button>
          <button
            class="btn"
            data-testid="resume-dispatch"
            type="button"
            :disabled="!selectedPlanId"
            @click="resumeBackfill(true)"
          >
            恢复并直接派发到节点
          </button>
        </div>
      </header>
      <p v-if="!selectedPlanId" class="page-desc">先在下方计划列表里选一条计划（点击计划编号所在行）。</p>
      <div v-else class="backfill-grid">
        <article class="backfill-card">
          <span class="stat-label">计划 / 窗口</span>
          <strong>{{ auditData?.计划编号 || selectedPlan?.计划编号 }}</strong>
          <p class="page-desc">
            {{ auditData?.window?.生效起日 || selectedPlan?.生效起日 }} ~
            {{ auditData?.window?.生效止日 || selectedPlan?.生效止日 }}
            <span v-if="auditData?.window?.核对截止日">（核对到 {{ auditData.window.核对截止日 }}）</span>
          </p>
        </article>
        <article class="backfill-card">
          <span class="stat-label">应采日期</span>
          <strong data-testid="expected-dates">{{ dateList(auditData?.expected_dates) }}</strong>
        </article>
        <article class="backfill-card">
          <span class="stat-label">已确认</span>
          <strong class="ok-text" data-testid="confirmed-dates">{{ dateList(auditData?.confirmed_dates) }}</strong>
        </article>
        <article class="backfill-card">
          <span class="stat-label">真实待补</span>
          <strong class="warn-text" data-testid="missing-dates">{{ dateList(auditData?.missing_dates) }}</strong>
        </article>
      </div>
      <table v-if="auditData" class="data-table backfill-table">
        <thead>
          <tr>
            <th>补采任务</th><th>采集日期</th><th>状态</th><th>占用节点</th>
            <th>关联批次</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="task in auditData.backfill_tasks" :key="task.task_id" :data-testid="`bf-task-${task.task_id}`">
            <td>{{ task.队列编号 }}</td>
            <td>{{ task.采集日期 }}</td>
            <td>{{ task.status }}</td>
            <td>{{ task.占用节点 || '—' }}</td>
            <td>{{ task.关联批次 || '—' }}</td>
            <td>
              <button
                v-if="task.status === '待派发'"
                class="link"
                type="button"
                :data-testid="`claim-${task.task_id}`"
                @click="claimTask(task.task_id)"
              >
                领取复核
              </button>
              <span v-else>—</span>
            </td>
          </tr>
          <tr v-if="!auditData.backfill_tasks?.length">
            <td colspan="6" class="empty-state">没有补采任务，计划窗口内已有有效批次覆盖</td>
          </tr>
        </tbody>
      </table>
      <p v-if="backfillMessage" class="page-desc" data-testid="backfill-message">{{ backfillMessage }}</p>
      <p v-if="backfillError" class="error-text">{{ backfillError }}</p>
    </section>

    <section v-if="meta.key === 'capture_batch'" class="backfill-panel" data-testid="receipt-panel">
      <header class="backfill-head">
        <div>
          <h3>批次回执与接收方可见范围</h3>
          <p class="page-desc">同一计划同一采集日期只认第一份有效批次，重复提交只留重复回执。</p>
        </div>
        <div class="page-actions">
          <button class="btn" data-testid="confirm-batch" type="button" :disabled="!selectedBatchId" @click="confirmBatch">
            确认选中批次并生成回执
          </button>
        </div>
      </header>
      <p v-if="!selectedBatchId" class="page-desc">在下方列表点击一行选中批次。</p>
      <p v-else class="page-desc">已选批次：{{ selectedBatchCode }}</p>
      <p v-if="receiptMessage" class="page-desc" data-testid="receipt-message">{{ receiptMessage }}</p>
      <p v-if="receiptError" class="error-text">{{ receiptError }}</p>
      <div v-if="receiverData" class="backfill-grid">
        <article class="backfill-card">
          <span class="stat-label">接收方可见日期</span>
          <strong class="ok-text">{{ dateList(receiverData.dates) }}</strong>
        </article>
        <article class="backfill-card">
          <span class="stat-label">仍待补日期</span>
          <strong class="warn-text">{{ dateList(receiverData.pending_dates) }}</strong>
        </article>
      </div>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-if="usesSelection"
              class="link"
              type="button"
              :data-testid="`select-${row.id}`"
              @click="selectRow(row)"
            >
              选中
            </button>
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  auditScheduleGaps,
  claimBackfillTask,
  confirmCaptureBatch,
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  receiverCaptureData,
  resumeSchedule,
  runAction as applyAction,
} from '@/api/backend-service'
import type { BackfillAudit, EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const usesSelection = meta.key === 'schedule_plan' || meta.key === 'capture_batch'
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

// ── 恢复排程补采核对 ──────────────────────────────────────────
const selectedPlanId = ref<number | null>(null)
const selectedPlan = ref<EntryRow | null>(null)
const selectedBatchId = ref<number | null>(null)
const selectedBatchCode = ref('')
const auditDate = ref('')
const auditData = ref<BackfillAudit | null>(null)
const receiverData = ref<BackfillAudit | null>(null)
const backfillMessage = ref('')
const backfillError = ref('')
const receiptMessage = ref('')
const receiptError = ref('')

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function dateList(dates?: string[]) {
  return dates && dates.length ? dates.join('、') : '无'
}

function selectRow(row: EntryRow) {
  if (meta.key === 'schedule_plan') {
    selectedPlanId.value = Number(row.id)
    selectedPlan.value = row
    auditData.value = null
    receiverData.value = null
    backfillMessage.value = ''
    backfillError.value = ''
  } else if (meta.key === 'capture_batch') {
    selectedBatchId.value = Number(row.id)
    selectedBatchCode.value = String(row.批次编号 ?? row.id)
    receiptMessage.value = ''
    receiptError.value = ''
  }
}

async function auditGaps() {
  if (!selectedPlanId.value) return
  backfillError.value = ''
  backfillMessage.value = ''
  try {
    const [audit, receiver] = await Promise.all([
      auditScheduleGaps(selectedPlanId.value, auditDate.value),
      receiverCaptureData(selectedPlanId.value, auditDate.value),
    ])
    auditData.value = audit
    receiverData.value = receiver
    const awaiting = audit.awaiting_confirmation?.length ?? 0
    backfillMessage.value = `核对完成：应采 ${audit.expected_dates?.length ?? 0} 天，已确认 ${
      audit.confirmed_dates?.length ?? 0
    } 天，待补 ${dateList(audit.missing_dates)}${
      awaiting ? `（另有 ${awaiting} 份补报待确认，确认前仍算待补）` : ''
    }`
    await reload()
  } catch (error) {
    backfillError.value = error instanceof Error ? error.message : '缺口核对失败'
  }
}

async function resumeBackfill(dispatch: boolean) {
  if (!selectedPlanId.value) return
  backfillError.value = ''
  backfillMessage.value = ''
  try {
    const result = await resumeSchedule(selectedPlanId.value, {
      asOf: auditDate.value,
      dispatch,
      operator: '值班员',
    })
    if (!result.ok) throw new Error(result.reason ?? '恢复排程失败')
    auditData.value = {
      ...(auditData.value ?? {}),
      plan_id: result.plan_id,
      计划编号: result.计划编号,
      window: result.window,
      expected_dates: result.应采日期,
      confirmed_dates: result.已确认采集日期,
      missing_dates: result.待补日期,
      pending_dates: result.待补日期,
      backfill_tasks: result.补采任务,
    } as BackfillAudit
    backfillMessage.value = `计划${result.计划状态}；本次新增补采 ${
      result.本次新增补采任务?.length ?? 0
    } 个，复用 ${result.复用补采任务?.length ?? 0} 个；真实待补 ${dateList(result.待补日期)}。${
      result.完成口径 ?? ''
    }`
    await reload()
  } catch (error) {
    backfillError.value = error instanceof Error ? error.message : '恢复排程失败'
  }
}

async function confirmBatch() {
  if (!selectedBatchId.value) return
  receiptError.value = ''
  receiptMessage.value = ''
  try {
    const result = await confirmCaptureBatch(selectedBatchId.value, auditDate.value)
    if (result.status === 'duplicate') {
      receiptMessage.value = `判为重复：${result.reason}；重复回执 ${result.receipt}`
    } else {
      receiptMessage.value = `批次已确认，回执 ${result.receipt}（有效）；关闭补采任务 ${
        result.closed_backfill_tasks?.length ?? 0
      } 个；仍待补 ${dateList(result.pending_dates)}`
    }
    await reload()
  } catch (error) {
    receiptError.value = error instanceof Error ? error.message : '确认批次失败'
  }
}

async function claimTask(taskId: number) {
  try {
    const result = await claimBackfillTask(taskId, 'ROBO-0002')
    backfillMessage.value =
      result.status === 'obsolete'
        ? `任务已关闭：${result.reason}`
        : `任务已领取；在批次确认前 ${result.采集日期} 仍属待补，待补 ${dateList(result.pending_dates)}`
    if (selectedPlanId.value) await auditGaps()
  } catch (error) {
    backfillError.value = error instanceof Error ? error.message : '领取任务失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
