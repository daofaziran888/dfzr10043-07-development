"""排程计划服务：周期性排程、时区管理与任务投递。

维护流程的周期性排程、时区设定和生效区间，触发时向任务
队列投递排队任务。支持迟到数据防护和并发上限控制。

恢复排程时不是简单把状态切回``生效中``，而是先按计划原本的
``[生效起日, 生效止日]`` 窗口列出应采集日期，再拿已确认且回执有效的
采集批次去比对，只把真正漏掉的日期派发成补采任务。入队不算完成，
待补范围始终按已确认批次计算。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app import backfill
from app.services.base import BaseModuleService


class SchedulePlanService(BaseModuleService):
    MODULE = "schedule_plan"
    ENTITY = "排程计划"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "计划编号", "关联模板", "触发周期", "时区设定", "生效起日",
        "生效止日", "并发上限", "下次触发", "待补日期", "本次已补上日期",
        "上次核对时间",
    ]
    REQUIRED_FIELDS = ["计划编号", "关联模板", "触发周期"]
    STATUS_ORDER = ["待生效", "生效中", "已暂停", "已过期"]
    # 恢复必须走 resume_and_backfill 做缺口核对，因此不开放通用「恢复」动作，
    # 避免有人只把状态切回生效中就当成补采已完成。
    ACTION_RULES = {"启效排程": "生效中", "暂停排程": "已暂停", "终止排程": "已过期"}
    NEGATIVE_ACTIONS = []

    def check_late_arrival(self, plan_id: int) -> dict[str, Any]:
        """检查排程是否发生迟到数据（生效止日已过）。"""
        entry = self.get_entry(plan_id)
        if entry is None:
            return {"ok": False, "reason": "排程计划不存在"}
        end_date = entry.get("生效止日", "")
        if not end_date:
            return {"ok": True, "late": False}
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()[:10]
        if end_date < now:
            self.run_action(plan_id, "终止排程")
            return {"ok": True, "late": True, "reason": f"生效止日{end_date}已过期，迟到数据拒绝"}
        return {"ok": True, "late": False}

    def get_active_plans(self) -> list[dict[str, Any]]:
        """获取生效中的排程计划列表。"""
        items, _ = self.list_entries(size=10000)
        return [it for it in items if it.get("status") == "生效中"]

    # ── 采集缺口核对 ──────────────────────────────────────────

    def find_plan(self, key: Any) -> dict[str, Any] | None:
        """按主键或计划编号定位计划，方便接口用编号直接核对。"""
        try:
            entry = self.get_entry(int(key))
        except (TypeError, ValueError):
            entry = None
        if entry is not None:
            return entry
        for row in self.list_entries(size=10000)[0]:
            if str(row.get("计划编号") or "") == str(key):
                return row
        return None

    def _runs(self) -> list[dict[str, Any]]:
        from app.services import service_for
        run_svc = service_for("flow_run")
        return run_svc.list_entries(size=10000)[0] if run_svc else []

    def _batches(self) -> list[dict[str, Any]]:
        from app.services import service_for
        batch_svc = service_for("capture_batch")
        return batch_svc.list_entries(size=10000)[0] if batch_svc else []

    def resolve_plan_for_batch(self, batch: dict[str, Any]) -> dict[str, Any] | None:
        """按批次归属找计划：显式计划编号优先，其次模板与上游来源。"""
        plans = self.list_entries(size=10000)[0]
        return backfill.resolve_plan_for_batch(batch, plans, self._runs())

    def audit_gaps(self, plan: dict[str, Any], as_of: Any = None,
                   persist: bool = True) -> dict[str, Any]:
        """核对计划应采日期与已确认批次，返回真实待补范围。"""
        runs = self._runs()
        batches = self._batches()
        analysis = backfill.analyze_gaps(plan, batches, runs, as_of)
        if persist:
            backfill.refresh_plan_fields(plan, batches, runs, as_of)
        analysis["backfill_tasks"] = self.backfill_task_views(plan)
        return analysis

    def backfill_task_views(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        from app.services import service_for
        task_svc = service_for("task_queue")
        tasks = task_svc.get_backfill_tasks(plan) if task_svc else []
        return [task_view(task) for task in tasks]

    def receiver_data(self, plan: dict[str, Any], as_of: Any = None) -> dict[str, Any]:
        """给下游接收方：只列有效回执对应的批次，附上尚未完成的日期。"""
        return backfill.receiver_payload(plan, self._batches(), self._runs(), as_of)

    # ── 恢复排程与补采派发 ────────────────────────────────────

    def resume_and_backfill(
        self,
        plan: dict[str, Any],
        *,
        as_of: Any = None,
        dispatch: bool = False,
        operator: str = "duty",
    ) -> dict[str, Any]:
        """恢复排程：核对缺口 → 建补采任务 →（可选）派发，并写审计。

        执行完成条件只认``已确认``且回执有效的批次，任务入队不会把日期
        标成已完成，所以返回的``待补日期``就是本次状态流转后的真实范围。
        """
        from app.services import service_for
        plan_id = int(plan["id"])
        if plan.get("status") == "已过期":
            return {
                "ok": False,
                "status": "refused",
                "reason": "排程已终止，不能恢复；请新建计划",
                "plan_id": plan_id,
            }

        was_active = plan.get("status") == "生效中"
        if not was_active:
            self.run_action(plan_id, "启效排程")

        analysis = self.audit_gaps(plan, as_of)
        task_svc = service_for("task_queue")
        created: list[dict[str, Any]] = []
        reused: list[dict[str, Any]] = []
        for capture_date in analysis["missing_dates"]:
            task, fresh = task_svc.create_backfill_task(
                plan, capture_date, as_of=as_of, priority="high",
                reason="恢复排程缺口补采",
            )
            if task is None:
                continue
            (created if fresh else reused).append(task_view(task))

        dispatched: list[dict[str, Any]] = []
        if dispatch:
            dispatched = self._dispatch_backfill(plan, created, reused)

        after = self.audit_gaps(plan, as_of)
        summary = {
            "ok": True,
            "status": "active" if was_active else "resumed",
            "plan_id": plan_id,
            "计划编号": backfill.plan_key(plan),
            "计划状态": plan.get("status"),
            "window": after["window"],
            "应采日期": after["expected_dates"],
            "已确认采集日期": after["confirmed_dates"],
            "待补日期": after["missing_dates"],
            "待补日期数": len(after["missing_dates"]),
            "本次新增补采任务": created,
            "复用补采任务": reused,
            "已派发补采任务": dispatched,
            "补采任务": after["backfill_tasks"],
            "本次已补上日期": _split_dates(plan.get("本次已补上日期")),
            "完成口径": "只有已确认且回执有效的采集批次计入完成，入队/派发不算完成",
            "operator": operator,
        }
        self._write_audit(plan, summary, operator)
        return summary

    def _dispatch_backfill(self, plan: dict[str, Any],
                           created: list[dict[str, Any]],
                           reused: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from app.services import service_for
        node_svc = service_for("robot_node")
        nodes = [
            node for node in (node_svc.list_entries(size=1000)[0] if node_svc else [])
            if node.get("status") == "在线"
        ]
        if not nodes:
            return []
        nodes.sort(key=lambda n: int(n.get("当前负载", "0") or "0"))
        node = nodes[0]
        task_svc = service_for("task_queue")
        results = []
        for view in [*created, *reused]:
            task = task_svc.find_open_backfill(plan, str(view.get("采集日期") or ""))
            if task is None:
                continue
            result = task_svc.dispatch_to_node(int(task["id"]), node.get("节点编号", ""))
            results.append({**task_view(task_svc.get_entry(int(task["id"])) or task),
                            "dispatch": result})
        return results

    def _write_audit(self, plan: dict[str, Any], summary: dict[str, Any],
                     operator: str) -> None:
        from app.services import service_for
        audit_svc = service_for("audit_trail")
        if audit_svc is None:
            return
        rows = audit_svc.list_entries(size=10000)[0]
        audit_svc.create_entry({
            "条目编号": f"AUDI-SCHE{plan['id']}-BF{len(rows) + 1}",
            "操作对象": f"排程计划.{backfill.plan_key(plan)}",
            "操作类型": "排程恢复补采",
            "操作者标识": operator,
            "操作时间": backfill.now_iso(),
            "变更摘要": (
                f"计划{backfill.plan_key(plan)}恢复，核对窗口{summary['window']['生效起日']}"
                f"~{summary['window']['核对截止日']}；应采{len(summary['应采日期'])}天，"
                f"已确认{len(summary['已确认采集日期'])}天，待补{backfill.date_text(summary['待补日期'])}；"
                f"新建补采任务{len(summary['本次新增补采任务'])}个，复用{len(summary['复用补采任务'])}个"
            ),
            "关联执行": "",
            "追溯标记": f"schedule-resume-{plan['id']}-{backfill.now_iso()}",
        })


def task_view(task: dict[str, Any]) -> dict[str, Any]:
    """补采任务的对外快照，避免把内部字段整行抛出。"""
    return {
        "task_id": task.get("id"),
        "队列编号": task.get("队列编号"),
        "采集日期": task.get("采集日期"),
        "关联模板": task.get("关联模板"),
        "status": task.get("status"),
        "占用节点": task.get("占用节点"),
        "补采状态": task.get("补采状态"),
        "关联批次": task.get("关联批次"),
    }


def _split_dates(value: Any) -> list[str]:
    return [item for item in str(value or "").split(",") if item]
