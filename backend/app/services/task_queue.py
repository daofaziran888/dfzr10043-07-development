"""任务队列服务：排队顺序管理、资源额度占用与派发回收。

维护待执行任务的排队顺序、优先级和资源额度占用情况，
派发失败时回收入队。支持优先级排序和等待秒数计算。

恢复排程时会为漏采日期生成``任务类型=补采``的排队任务，任务携带
计划编号与采集日期；任务被领取或批次确认后要重新核对缺口，已由
有效批次覆盖的补采任务直接关闭，避免同一日期重复采集。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app import backfill
from app.services.base import BaseModuleService


class TaskQueueService(BaseModuleService):
    MODULE = "task_queue"
    ENTITY = "排队任务"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "队列编号", "关联模板", "优先级", "期望开始", "重试次数",
        "占用节点", "等待秒数", "入队时间", "任务类型", "计划编号",
        "采集日期", "关联批次", "补采状态",
    ]
    REQUIRED_FIELDS = ["队列编号", "关联模板", "优先级"]
    STATUS_ORDER = ["待派发", "已派发", "已消费", "已丢弃"]
    ACTION_RULES = {"派发任务": "已派发", "回收任务": "已消费", "丢弃任务": "已丢弃"}
    NEGATIVE_ACTIONS = []

    _PRIORITY_WEIGHTS = {"high": 3, "medium": 2, "low": 1}

    def get_pending_by_priority(self) -> list[dict[str, Any]]:
        """按优先级排序返回待派发任务。"""
        items, _ = self.list_entries(size=10000)
        pending = [it for it in items if it.get("status") == "待派发"]
        pending.sort(
            key=lambda t: self._PRIORITY_WEIGHTS.get(
                str(t.get("优先级", "low")).lower(), 1
            ),
            reverse=True,
        )
        return pending

    def recycle_failed_dispatches(self) -> dict[str, Any]:
        """回收派发失败的任务（已派发但被标记为待派发）。"""
        items, _ = self.list_entries(size=10000)
        recycled = 0
        for it in items:
            if it.get("status") == "已派发" and not it.get("占用节点"):
                self.run_action(it["id"], "回收任务")
                recycled += 1
        return {"ok": True, "recycled_count": recycled}

    def dispatch_to_node(self, task_id: int, node_code: str) -> dict[str, Any]:
        """将任务派发到指定节点。"""
        entry = self.get_entry(task_id)
        if entry is None:
            return {"ok": False, "reason": "任务不存在"}
        if entry.get("status") != "待派发":
            return {"ok": False, "reason": "任务状态不是待派发"}
        entry["占用节点"] = node_code
        entry["补采状态"] = "已领取" if backfill.is_backfill_task(entry) else entry.get("补采状态", "")
        self.run_action(task_id, "派发任务")
        return {"ok": True, "task_id": task_id, "node": node_code, "entry": entry}

    # ── 补采任务 ──────────────────────────────────────────────

    def get_backfill_tasks(self, plan: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        items, _ = self.list_entries(size=10000)
        tasks = [it for it in items if backfill.is_backfill_task(it)]
        if plan is not None:
            tasks = [it for it in tasks if it.get("计划编号") == backfill.plan_key(plan)
                     or backfill.task_matches_plan(it, plan)]
        return tasks

    def find_open_backfill(self, plan: dict[str, Any], capture_date: str) -> dict[str, Any] | None:
        for task in self.get_backfill_tasks(plan):
            if str(task.get("采集日期") or "") != capture_date:
                continue
            if backfill.task_is_open(task):
                return task
        return None

    def _next_backfill_code(self, plan: dict[str, Any], capture_date: str) -> str:
        base = f"TASK-BF-{backfill.plan_key(plan)}-{capture_date.replace('-', '')}"
        base = re.sub(r"[^0-9A-Za-z一-鿿_-]", "-", base)
        items, _ = self.list_entries(size=10000)
        codes = {str(it.get("队列编号") or "") for it in items}
        code = base
        suffix = 2
        while code in codes:
            code = f"{base}-{suffix}"
            suffix += 1
        return code

    def create_backfill_task(
        self,
        plan: dict[str, Any],
        capture_date: str,
        *,
        as_of: Any = None,
        priority: str = "high",
        reason: str = "恢复排程缺口补采",
    ) -> tuple[dict[str, Any] | None, bool]:
        """为计划窗口内的漏采日期登记补采任务；已有未关闭任务则复用。

        不在计划``[生效起日, 生效止日]``窗口内的日期一律拒绝登记。
        """
        if capture_date not in backfill.expected_dates(plan, as_of):
            return None, False
        existing = self.find_open_backfill(plan, capture_date)
        if existing is not None:
            return existing, False
        stamp = backfill.now_iso(as_of)
        value = {
            "队列编号": self._next_backfill_code(plan, capture_date),
            "关联模板": plan.get("关联模板", ""),
            "优先级": priority,
            "期望开始": stamp,
            "重试次数": "0",
            "占用节点": "",
            "等待秒数": "0",
            "入队时间": stamp,
            "任务类型": "补采",
            "计划编号": backfill.plan_key(plan),
            "采集日期": capture_date,
            "关联批次": "",
            "补采状态": reason,
        }
        entry, missing = self.create_entry(value)
        return entry, entry is not None

    def close_task(self, task_id: int, batch_code: str, reason: str) -> dict[str, Any] | None:
        """关闭补采任务：有效批次已覆盖该日期，任务不必再执行。"""
        entry = self.get_entry(task_id)
        if entry is None:
            return None
        if not backfill.task_is_open(entry):
            return entry
        entry["关联批次"] = batch_code
        entry["补采状态"] = reason
        entry["完成时间"] = backfill.now_iso()
        self.run_action(task_id, "回收任务")
        return entry

    def close_tasks_for_date(
        self,
        plan: dict[str, Any] | None,
        capture_date: str,
        batch_code: str,
        reason: str = "有效批次已覆盖",
        template: str = "",
    ) -> list[int]:
        closed: list[int] = []
        for task in self.get_backfill_tasks(plan):
            if str(task.get("采集日期") or "") != capture_date:
                continue
            if not backfill.task_is_open(task):
                continue
            if plan is not None and not backfill.task_matches_plan(task, plan):
                continue
            if plan is None and template and str(task.get("关联模板") or "") != template:
                continue
            if self.close_task(int(task["id"]), batch_code, reason) is not None:
                closed.append(int(task["id"]))
        return closed
