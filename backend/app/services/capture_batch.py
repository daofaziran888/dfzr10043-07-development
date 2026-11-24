"""采集批次服务：执行产出归集、完整性校验、窗口去重和批次确认。

一次或多次流程执行的产出记录归集成批次，附带采集窗口和
完整性校验值供下游引用。去重逻辑按采集窗口+上游来源判重。

确认批次时会做两件事：一是按``计划编号/模板 + 采集日期``判重，
同一日期的有效数据只认第一份，迟到补报的重复批次只留一条重复回执；
二是确认成功后回写计划缺口、关闭同日期补采任务，让``待补日期``
反映真实剩余范围。下游接收方只允许读取``回执状态=有效``的批次。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app import backfill
from app.services.base import BaseModuleService


class CaptureBatchService(BaseModuleService):
    MODULE = "capture_batch"
    ENTITY = "采集批次"
    OPERATION_TERM = "采集"
    LIST_FIELDS = [
        "批次编号", "关联执行", "记录行数", "采集窗口", "完整性校验",
        "批次标签", "上游来源", "生成时间", "计划编号", "采集日期",
        "回执编号", "回执状态",
    ]
    REQUIRED_FIELDS = ["批次编号", "关联执行", "记录行数"]
    STATUS_ORDER = ["归集中", "校验中", "已确认", "校验失败"]
    ACTION_RULES = {"启动校验": "校验中", "确认批次": "已确认", "标记失败": "校验失败"}
    NEGATIVE_ACTIONS = []

    # ── 查询与判重 ────────────────────────────────────────────

    def find_duplicate(self, checksum: str, source: str) -> dict[str, Any] | None:
        """按完整性校验值和上游来源查找重复批次。"""
        items, _ = self.list_entries(size=10000)
        for it in items:
            if it.get("完整性校验") == checksum and it.get("上游来源") == source:
                return it
        return None

    def get_confirmed_batches(self) -> list[dict[str, Any]]:
        """获取所有已确认的采集批次，供下游交接打包。"""
        items, _ = self.list_entries(size=10000)
        return [it for it in items if it.get("status") == "已确认"]

    def get_usable_batches(self) -> list[dict[str, Any]]:
        """已确认且回执有效的批次，接收方只能看到这些。"""
        return [it for it in self.get_confirmed_batches() if backfill.batch_is_usable(it)]

    def _runs(self) -> list[dict[str, Any]]:
        from app.services import service_for
        run_svc = service_for("flow_run")
        return run_svc.list_entries(size=10000)[0] if run_svc else []

    def find_effective(self, plan: dict[str, Any], capture_date: str) -> dict[str, Any] | None:
        """同一计划（或模板）同一采集日期是否已有有效批次。"""
        runs = self._runs()
        for batch in self.get_usable_batches():
            if backfill.batch_capture_date(batch) != capture_date:
                continue
            if backfill.batch_matches_plan(batch, plan, runs):
                return batch
        return None

    def find_effective_by_template(self, template: str, capture_date: str) -> dict[str, Any] | None:
        if not template:
            return None
        runs = self._runs()
        for batch in self.get_usable_batches():
            if backfill.batch_capture_date(batch) != capture_date:
                continue
            if backfill.batch_template(batch, runs) == template:
                return batch
        return None

    # ── 确认与回执 ────────────────────────────────────────────

    def confirm_batch(self, batch_id: int, operator: str = "duty",
                      as_of: Any = None) -> dict[str, Any]:
        """确认批次并写回执；同计划同日期已有有效批次时判为重复。

        重复批次不会变成``已确认``，只留一条``重复驳回``回执，避免下游
        把同一采集日期的数据算两次。
        """
        from app.services import service_for
        batch = self.get_entry(batch_id)
        if batch is None:
            return {"ok": False, "status": "missing", "reason": "批次不存在"}

        if str(batch.get("status") or "") == "已确认" and backfill.batch_is_usable(batch):
            return {
                "ok": True,
                "status": "already_confirmed",
                "batch_id": batch_id,
                "receipt": batch.get("回执编号"),
                "capture_date": backfill.batch_capture_date(batch),
            }

        plan_svc = service_for("schedule_plan")
        plan = plan_svc.resolve_plan_for_batch(batch) if plan_svc else None
        capture_date = backfill.batch_capture_date(batch)
        runs = self._runs()
        template = backfill.batch_template(batch, runs)

        duplicate = None
        if capture_date:
            if plan is not None:
                duplicate = self.find_effective(plan, capture_date)
            else:
                duplicate = self.find_effective_by_template(template, capture_date)

        stamp = backfill.now_iso(as_of)
        if duplicate is not None and int(duplicate.get("id", 0)) != batch_id:
            batch["回执编号"] = f"RCPT-{batch.get('批次编号', batch_id)}"
            batch["回执状态"] = "重复驳回"
            batch["回执时间"] = stamp
            batch["重复来源"] = duplicate.get("批次编号")
            batch["计划编号"] = batch.get("计划编号") or (
                backfill.plan_key(plan) if plan is not None else "")
            if capture_date:
                batch["采集日期"] = capture_date
            BaseModuleService.run_action(self, batch_id, "标记失败")
            return {
                "ok": False,
                "status": "duplicate",
                "batch_id": batch_id,
                "capture_date": capture_date,
                "duplicate_of": duplicate.get("批次编号"),
                "receipt": batch["回执编号"],
                "receipt_status": "重复驳回",
                "reason": f"采集日期{capture_date}已有有效批次{duplicate.get('批次编号')}，只认一次",
            }

        batch["回执编号"] = f"RCPT-{batch.get('批次编号', batch_id)}"
        batch["回执状态"] = "有效"
        batch["回执时间"] = stamp
        if plan is not None:
            batch["计划编号"] = batch.get("计划编号") or backfill.plan_key(plan)
        if capture_date:
            batch["采集日期"] = capture_date
        BaseModuleService.run_action(self, batch_id, "确认批次")

        closed_tasks: list[int] = []
        task_svc = service_for("task_queue")
        if task_svc is not None and capture_date:
            closed_tasks = task_svc.close_tasks_for_date(
                plan, capture_date, str(batch.get("批次编号") or ""),
                template=template, reason="有效批次已覆盖",
            )

        pending_dates: list[str] = []
        if plan is not None and plan_svc is not None:
            analysis = plan_svc.audit_gaps(plan, as_of)
            pending_dates = analysis["missing_dates"]
            if capture_date:
                completed = [d for d in str(plan.get("本次已补上日期") or "").split(",") if d]
                if capture_date not in completed:
                    completed.append(capture_date)
                plan["本次已补上日期"] = ",".join(sorted(completed))

        return {
            "ok": True,
            "status": "confirmed",
            "batch_id": batch_id,
            "批次编号": batch.get("批次编号"),
            "capture_date": capture_date,
            "plan_id": plan.get("id") if plan is not None else None,
            "计划编号": backfill.plan_key(plan) if plan is not None else "",
            "receipt": batch["回执编号"],
            "receipt_status": "有效",
            "closed_backfill_tasks": closed_tasks,
            "pending_dates": pending_dates,
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """拦截``确认批次``，让页面动作与链路都走带回执与判重的确认流程。"""
        if action != "确认批次":
            return BaseModuleService.run_action(self, entry_id, action)
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        result = self.confirm_batch(entry_id)
        if result["status"] == "duplicate":
            return self.get_entry(entry_id), (
                f"{self.ENTITY}判为重复：{result['reason']}")
        if result["status"] == "missing":
            return None, result["reason"]
        return self.get_entry(entry_id), f"{self.ENTITY}已确认，回执{result.get('receipt')}"

    def verify_integrity(self, batch_id: int) -> dict[str, Any]:
        """启动校验并确认批次；行数为0时标记失败。"""
        entry = self.get_entry(batch_id)
        if entry is None:
            return {"ok": False, "reason": "批次不存在"}
        BaseModuleService.run_action(self, batch_id, "启动校验")
        row_count = int(entry.get("记录行数", "0") or "0")
        if row_count <= 0:
            BaseModuleService.run_action(self, batch_id, "标记失败")
            return {"ok": False, "reason": "批次记录行数为0，校验失败"}
        result = self.confirm_batch(batch_id)
        return {
            "ok": result["status"] in ("confirmed", "already_confirmed"),
            "batch_id": batch_id,
            "row_count": row_count,
            "receipt": result.get("receipt"),
            "receipt_status": result.get("receipt_status", "有效"),
            "duplicate_of": result.get("duplicate_of"),
            "reason": result.get("reason", ""),
        }

    def mark_failed(self, batch_id: int, reason: str = "") -> dict[str, Any]:
        entry = self.get_entry(batch_id)
        if entry is None:
            return {"ok": False, "reason": "批次不存在"}
        entry["回执编号"] = entry.get("回执编号") or f"RCPT-{entry.get('批次编号', batch_id)}"
        entry["回执状态"] = "无效"
        entry["回执时间"] = backfill.now_iso()
        BaseModuleService.run_action(self, batch_id, "标记失败")
        return {"ok": True, "batch_id": batch_id, "reason": reason}

    def receiver_view(self, plan: dict[str, Any], as_of: Any = None) -> dict[str, Any]:
        """接收方视角：只给有效回执批次和真实待补日期。"""
        runs = self._runs()
        batches = [b for b in self.list_entries(size=10000)[0]
                   if backfill.batch_matches_plan(b, plan, runs)]
        return backfill.receiver_payload(plan, batches, runs, as_of)
