"""排程补采专用接口：缺口核对、恢复派发、队列领取复核与接收方取数。

这些接口放在通用资源路由之前注册，避免``/api/{module}/{entry_id}``
把带业务后缀的固定路径抢先匹配掉。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.pipeline import (
    audit_plan_gaps,
    claim_backfill_task,
    confirm_capture_batch,
    pause_schedule,
    receiver_capture_data,
    resume_schedule_and_backfill,
)

router = APIRouter(prefix="/api", tags=["排程补采"])


class ResumePayload(BaseModel):
    as_of: str | None = None
    dispatch: bool = False
    operator: str = "duty"


class ClaimPayload(BaseModel):
    node_code: str = ""


class ConfirmPayload(BaseModel):
    as_of: str | None = None
    operator: str = "duty"


def _require(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail=result.get("reason", "对象不存在"))
    return result


@router.get("/schedule_plan/{plan_id}/gaps")
def plan_gaps(plan_id: str, as_of: str | None = None) -> dict[str, Any]:
    """核对计划应采日期：返回已确认、待补日期与补采任务。"""
    return _require(audit_plan_gaps(plan_id, as_of))


@router.post("/schedule_plan/{plan_id}/resume")
def resume_plan(plan_id: str, payload: ResumePayload) -> dict[str, Any]:
    """恢复暂停排程并派发补采任务；入队不算完成，待补日期按批次确认回写。"""
    return _require(resume_schedule_and_backfill(
        plan_id,
        as_of=payload.as_of,
        dispatch=payload.dispatch,
        operator=payload.operator,
    ))


@router.post("/schedule_plan/{plan_id}/pause")
def pause_plan(plan_id: str) -> dict[str, Any]:
    return _require(pause_schedule(plan_id))


@router.get("/schedule_plan/{plan_id}/receiver")
def plan_receiver_data(plan_id: str, as_of: str | None = None) -> dict[str, Any]:
    """接收方取数：只返回有效回执对应的批次，附真实待补日期。"""
    return _require(receiver_capture_data(plan_id, as_of))


@router.post("/task_queue/{task_id}/claim")
def claim_task(task_id: int, payload: ClaimPayload) -> dict[str, Any]:
    """队列领取补采任务：领取前先复核缺口，已覆盖的任务直接关闭。"""
    return _require(claim_backfill_task(task_id, payload.node_code))


@router.post("/capture_batch/{batch_id}/confirm")
def confirm_batch(batch_id: int, payload: ConfirmPayload) -> dict[str, Any]:
    """确认采集批次并生成回执；同一采集日期只认第一份有效数据。"""
    return _require(confirm_capture_batch(
        batch_id, operator=payload.operator, as_of=payload.as_of))
