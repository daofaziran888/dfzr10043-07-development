"""补采缺口核对：按排程计划原有生效窗口，用已确认的采集批次找漏采日期。

这里的函数只处理内存数据行，不直接依赖服务注册表，方便被多个领域服务复用。
核心规则：

* 计划应采集日期只落在``[生效起日, 生效止日]``且不晚于核对日；
* 只有``已确认``且回执有效的采集批次才算补上；入队、派发、校验中都不算完成；
* 同一计划（或同一模板）同一采集日期只认一份有效批次，迟到补报也一样。
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

TZ = timezone(timedelta(hours=8))
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# 任务队列中已经不需要继续补采的状态
CLOSED_TASK_STATUSES = {"已消费", "已丢弃"}


# ── 时间与窗口 ────────────────────────────────────────────────


def now_iso(as_of: Any = None) -> str:
    """把核对时点转成 ISO 秒级字符串；默认取当前东八区时间。"""
    if isinstance(as_of, datetime):
        value = as_of
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ)
        return value.astimezone(TZ).isoformat(timespec="seconds")
    if isinstance(as_of, date):
        return datetime.combine(as_of, datetime.min.time(), tzinfo=TZ).isoformat(timespec="seconds")
    if as_of:
        parsed = parse_date(as_of)
        if parsed:
            return datetime.combine(parsed, datetime.min.time(), tzinfo=TZ).isoformat(timespec="seconds")
    return datetime.now(TZ).isoformat(timespec="seconds")


def resolve_as_of(as_of: Any = None) -> date:
    """确定本次核对的截止日期。"""
    if isinstance(as_of, datetime):
        value = as_of
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ)
        return value.astimezone(TZ).date()
    if isinstance(as_of, date):
        return as_of
    if as_of:
        parsed = parse_date(as_of)
        if parsed:
            return parsed
    return datetime.now(TZ).date()


def parse_date(value: Any) -> date | None:
    """从字段里尽力解析出日期，兼容带时间的 ISO 字符串。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _cycle(plan: dict[str, Any]) -> tuple[str, int]:
    raw = str(plan.get("触发周期") or "").strip().lower()
    if raw in {"weekly", "week", "周", "每周"} or "week" in raw:
        return "weekly", 7
    if raw in {"monthly", "month", "月", "每月"} or "month" in raw:
        return "monthly", 1
    return "daily", 1


def expected_dates(plan: dict[str, Any], as_of: Any = None) -> list[str]:
    """计划原本应该采集的日期列表；只覆盖计划生效窗口，不包含未来日期。"""
    start = parse_date(plan.get("生效起日"))
    if start is None:
        return []
    end = parse_date(plan.get("生效止日")) or resolve_as_of(as_of)
    horizon = min(end, resolve_as_of(as_of))
    if start > horizon:
        return []

    kind, step = _cycle(plan)
    dates: list[str] = []
    cursor = start
    guard = 0
    while cursor <= horizon and guard < 10000:
        dates.append(cursor.isoformat())
        guard += 1
        if kind == "weekly":
            cursor = cursor + timedelta(days=step)
        elif kind == "monthly":
            cursor = _add_months(cursor, step)
        else:
            cursor = cursor + timedelta(days=1)
    return dates


def plan_window(plan: dict[str, Any], as_of: Any = None) -> dict[str, str]:
    """计划生效窗口与本次核对截止日。"""
    end = parse_date(plan.get("生效止日"))
    horizon = resolve_as_of(as_of)
    return {
        "生效起日": str(plan.get("生效起日") or ""),
        "生效止日": str(plan.get("生效止日") or ""),
        "核对截止日": min(end, horizon).isoformat() if end else horizon.isoformat(),
    }


# ── 批次与回执 ────────────────────────────────────────────────


def plan_key(plan: dict[str, Any]) -> str:
    return str(plan.get("计划编号") or plan.get("id") or "")


def batch_capture_date(batch: dict[str, Any]) -> str | None:
    """采集批次归属的采集日期：优先显式字段，再解析采集窗口与生成时间。"""
    for field in ("采集日期", "采集窗口", "生成时间", "完成时间"):
        parsed = parse_date(batch.get(field))
        if parsed is not None:
            return parsed.isoformat()
    return None


def batch_receipt_status(batch: dict[str, Any]) -> str:
    return str(batch.get("回执状态") or "").strip()


def batch_is_usable(batch: dict[str, Any]) -> bool:
    """批次是否可提供给接收方：必须已确认且回执状态为``有效``。

    没有回执的旧批次、回执为重复/无效的批次都不算数，避免下游把同一
    采集日期的数据重复消费或消费到被驳回的数据。
    """
    if str(batch.get("status") or "") != "已确认":
        return False
    return batch_receipt_status(batch) == "有效"


def batch_awaiting_receipt(batch: dict[str, Any]) -> bool:
    """已提交但还没出回执的批次（校验中/归集中），补报确认期间仍算待补。"""
    return str(batch.get("status") or "") not in {"已确认", "校验失败"}


def run_template(run_code: str, runs: Iterable[dict[str, Any]]) -> str:
    for run in runs:
        if str(run.get("执行编号") or "") == str(run_code or ""):
            return str(run.get("关联模板") or "")
    return ""


def batch_template(batch: dict[str, Any], runs: Iterable[dict[str, Any]]) -> str:
    explicit = str(batch.get("关联模板") or "").strip()
    if explicit:
        return explicit
    return run_template(str(batch.get("关联执行") or ""), runs)


def batch_matches_plan(batch: dict[str, Any], plan: dict[str, Any],
                       runs: Iterable[dict[str, Any]]) -> bool:
    """判断批次是否属于该计划：显式计划编号优先，其次模板与上游来源。"""
    explicit = str(batch.get("计划编号") or "").strip()
    if explicit:
        return explicit == plan_key(plan)
    template = str(plan.get("关联模板") or "").strip()
    if template and batch_template(batch, runs) == template:
        return True
    source = str(batch.get("上游来源") or "")
    key = plan_key(plan)
    return bool(key) and key in source


def resolve_plan_for_batch(batch: dict[str, Any], plans: Iterable[dict[str, Any]],
                           runs: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """给采集批次找到对应计划，优先匹配窗口包含采集日期的计划。"""
    capture_date = batch_capture_date(batch)
    matches = [p for p in plans if batch_matches_plan(batch, p, runs)]
    if not matches:
        return None
    if capture_date:
        for plan in matches:
            if capture_date in expected_dates(plan, capture_date):
                return plan
    return matches[0]


# ── 任务队列 ──────────────────────────────────────────────────


def is_backfill_task(task: dict[str, Any]) -> bool:
    if str(task.get("任务类型") or "") == "补采":
        return True
    return str(task.get("队列编号") or "").startswith("TASK-BF")


def task_is_open(task: dict[str, Any]) -> bool:
    return str(task.get("status") or "") not in CLOSED_TASK_STATUSES


def task_plan_code(task: dict[str, Any]) -> str:
    return str(task.get("计划编号") or "")


def task_matches_plan(task: dict[str, Any], plan: dict[str, Any]) -> bool:
    explicit = task_plan_code(task)
    if explicit:
        return explicit == plan_key(plan)
    template = str(plan.get("关联模板") or "").strip()
    return bool(template) and str(task.get("关联模板") or "") == template


# ── 缺口核对 ──────────────────────────────────────────────────


def analyze_gaps(plan: dict[str, Any], batches: Iterable[dict[str, Any]],
                 runs: Iterable[dict[str, Any]], as_of: Any = None) -> dict[str, Any]:
    """核对计划应采集日期与已确认批次，返回完整、缺口和有效批次。

    ``awaiting_confirmation``列出已提交、回执还没出的批次所在日期；
    这些日期仍留在``missing_dates``里，方便值班员看到真实的待补范围。
    """
    runs_list = list(runs)
    batch_list = list(batches)
    expected = expected_dates(plan, as_of)
    expected_set = set(expected)
    usable: list[dict[str, Any]] = []
    awaiting: list[dict[str, Any]] = []
    for batch in batch_list:
        capture_date = batch_capture_date(batch)
        if not capture_date or capture_date not in expected_set:
            continue
        if not batch_matches_plan(batch, plan, runs_list):
            continue
        if batch_is_usable(batch):
            usable.append({
                "batch_id": batch.get("id"),
                "批次编号": batch.get("批次编号"),
                "采集日期": capture_date,
                "回执编号": batch.get("回执编号"),
                "回执状态": batch_receipt_status(batch) or "有效",
                "关联执行": batch.get("关联执行"),
                "记录行数": batch.get("记录行数"),
            })
        elif batch_awaiting_receipt(batch):
            awaiting.append({
                "batch_id": batch.get("id"),
                "批次编号": batch.get("批次编号"),
                "采集日期": capture_date,
                "status": batch.get("status"),
            })

    confirmed = sorted({item["采集日期"] for item in usable})
    confirmed_set = set(confirmed)
    missing = [day for day in expected if day not in confirmed_set]
    return {
        "plan_id": plan.get("id"),
        "计划编号": plan_key(plan),
        "关联模板": plan.get("关联模板"),
        "window": plan_window(plan, as_of),
        "expected_dates": expected,
        "confirmed_dates": confirmed,
        "missing_dates": missing,
        "pending_dates": list(missing),
        "usable_batches": usable,
        "awaiting_confirmation": awaiting,
    }


def receiver_payload(plan: dict[str, Any], batches: Iterable[dict[str, Any]],
                     runs: Iterable[dict[str, Any]], as_of: Any = None) -> dict[str, Any]:
    """接收方可见数据：只返回有效回执对应的批次，同时给出真实待补日期。"""
    analysis = analyze_gaps(plan, batches, runs, as_of)
    return {
        "计划编号": analysis["计划编号"],
        "usable_batches": analysis["usable_batches"],
        "dates": analysis["confirmed_dates"],
        "pending_dates": analysis["missing_dates"],
        "receipt_required": True,
    }


def refresh_plan_fields(plan: dict[str, Any], batches: Iterable[dict[str, Any]],
                        runs: Iterable[dict[str, Any]], as_of: Any = None,
                        newly_completed: str | None = None) -> dict[str, Any]:
    """把核对结果写回计划，方便工作台直接看到真实待补范围。"""
    analysis = analyze_gaps(plan, batches, runs, as_of)
    plan["待补日期"] = ",".join(analysis["missing_dates"])
    plan["缺口日期"] = ",".join(analysis["missing_dates"])
    plan["已确认采集日期"] = ",".join(analysis["confirmed_dates"])
    plan["上次核对时间"] = now_iso(as_of)
    if newly_completed:
        completed = [d for d in str(plan.get("本次已补上日期") or "").split(",") if d]
        if newly_completed not in completed:
            completed.append(newly_completed)
        plan["本次已补上日期"] = ",".join(sorted(completed))
    return analysis


def date_text(dates: Iterable[str]) -> str:
    values = [d for d in dates if d]
    return "、".join(values) if values else "无"
