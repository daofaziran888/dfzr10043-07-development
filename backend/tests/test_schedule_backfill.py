"""恢复排程补采链路测试：缺口核对、补采派发、迟到重复回执与接收方可见性。

覆盖题目给定的场景：
计划在 20、21、22 日取数，暂停后恢复时只有 20 日确认；
核对结果是 21、22 待补；原机器人在补采排队时补报 21 日；
最终 21、22 各一份可用批次；23 日不在计划窗口内，不纳入补采。
"""
from __future__ import annotations

import pytest

from app import backfill
from app.pipeline import (
    audit_plan_gaps,
    claim_backfill_task,
    collect_files_and_land_data,
    confirm_capture_batch,
    pause_schedule,
    publish_flow_and_schedule,
    receiver_capture_data,
    resume_schedule_and_backfill,
)
from app.services import service_for

AS_OF = "2026-11-22T18:00:00"


# ── 场景搭建 ──────────────────────────────────────────────────

def _make_plan(*, start: str = "2026-11-20", end: str = "2026-11-22",
               cycle: str = "daily", status: str = "已暂停") -> dict:
    plan_svc = service_for("schedule_plan")
    plan, missing = plan_svc.create_entry({
        "计划编号": "SCHE-BF-1",
        "关联模板": "FLOW-0001",
        "触发周期": cycle,
        "时区设定": "Asia/Shanghai",
        "生效起日": start,
        "生效止日": end,
        "并发上限": "2",
        "下次触发": f"{start}T09:00:00",
    })
    assert not missing
    plan["status"] = status
    return plan


def _add_batch(*, capture_date: str, plan: dict, status: str = "已确认",
               batch_code: str, receipt: str = "有效", run_code: str = "FLOW-0001") -> dict:
    batch_svc = service_for("capture_batch")
    batch, missing = batch_svc.create_entry({
        "批次编号": batch_code,
        "关联执行": run_code,
        "记录行数": "12",
        "采集窗口": f"[{capture_date}]",
        "完整性校验": f"sha-{batch_code}",
        "批次标签": "scheduled",
        "上游来源": f"plan:{backfill.plan_key(plan)}",
        "生成时间": f"{capture_date}T09:10:00",
        "计划编号": backfill.plan_key(plan),
        "采集日期": capture_date,
        "回执编号": f"RCPT-{batch_code}" if status == "已确认" else "",
        "回执状态": receipt if status == "已确认" else "",
    })
    assert not missing
    batch["status"] = status
    batch["pending"] = status != "已确认"
    return batch


@pytest.fixture()
def paused_plan():
    plan = _make_plan()
    _add_batch(capture_date="2026-11-20", plan=plan, batch_code="CAPT-D20")
    return plan


# ── 期望日期与窗口 ────────────────────────────────────────────

class TestExpectedDates:
    def test_daily_window_covers_20_21_22(self, paused_plan):
        assert backfill.expected_dates(paused_plan, AS_OF) == [
            "2026-11-20", "2026-11-21", "2026-11-22",
        ]

    def test_window_stops_at_plan_end_not_today(self, paused_plan):
        """计划止日之后（如 23 日）即使今天已过，也不算计划应采日期。"""
        dates = backfill.expected_dates(paused_plan, "2026-11-24T10:00:00")
        assert dates == ["2026-11-20", "2026-11-21", "2026-11-22"]
        assert "2026-11-23" not in dates

    def test_window_does_not_include_future_dates(self, paused_plan):
        plan = _make_plan(start="2026-11-20", end="2026-11-30", status="已暂停")
        dates = backfill.expected_dates(plan, "2026-11-22T18:00:00")
        assert dates == ["2026-11-20", "2026-11-21", "2026-11-22"]

    def test_weekly_cycle(self):
        plan = _make_plan(start="2026-11-02", end="2026-11-30", cycle="weekly", status="已暂停")
        assert backfill.expected_dates(plan, AS_OF) == [
            "2026-11-02", "2026-11-09", "2026-11-16",
        ]


# ── 恢复排程：缺口核对与补采派发 ──────────────────────────────

class TestResumeGapAudit:
    def test_resume_dispatches_only_missing_dates(self, paused_plan):
        result = resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)

        assert result["ok"] is True
        assert result["计划状态"] == "生效中"
        assert result["已确认采集日期"] == ["2026-11-20"]
        assert result["待补日期"] == ["2026-11-21", "2026-11-22"]
        assert result["待补日期数"] == 2
        assert [t["采集日期"] for t in result["本次新增补采任务"]] == [
            "2026-11-21", "2026-11-22",
        ]
        assert all(t["status"] == "待派发" for t in result["本次新增补采任务"])

    def test_backfill_never_touches_out_of_window_date(self, paused_plan):
        result = resume_schedule_and_backfill(paused_plan["id"], as_of="2026-11-23T12:00:00")
        task_dates = [t["采集日期"] for t in result["本次新增补采任务"]]
        assert task_dates == ["2026-11-21", "2026-11-22"]
        assert "2026-11-23" not in task_dates

    def test_enqueue_is_not_completion(self, paused_plan):
        """补采任务已入队，但待补范围仍然显示 21、22。"""
        first = resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        assert first["待补日期"] == ["2026-11-21", "2026-11-22"]

        audit = audit_plan_gaps(paused_plan["id"], as_of=AS_OF)
        assert audit["missing_dates"] == ["2026-11-21", "2026-11-22"]
        assert audit["confirmed_dates"] == ["2026-11-20"]
        assert {t["采集日期"] for t in audit["backfill_tasks"]} == {"2026-11-21", "2026-11-22"}

    def test_resume_is_idempotent_and_reuses_open_tasks(self, paused_plan):
        resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        second = resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        assert second["本次新增补采任务"] == []
        assert len(second["复用补采任务"]) == 2
        assert second["待补日期"] == ["2026-11-21", "2026-11-22"]

    def test_resume_writes_audit_with_real_scope(self, paused_plan):
        resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF, operator="值班员甲")
        audit_svc = service_for("audit_trail")
        entries = audit_svc.list_entries(size=1000)[0]
        record = next(e for e in entries if e.get("操作类型") == "排程恢复补采")
        assert record["操作者标识"] == "值班员甲"
        assert "2026-11-21" in record["变更摘要"]
        assert "2026-11-22" in record["变更摘要"]

    def test_dispatch_marks_claimed_but_still_pending(self, paused_plan):
        result = resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF, dispatch=True)
        assert len(result["已派发补采任务"]) == 2
        assert all(t["status"] == "已派发" for t in result["已派发补采任务"])
        audit = audit_plan_gaps(paused_plan["id"], as_of=AS_OF)
        assert audit["missing_dates"] == ["2026-11-21", "2026-11-22"]

    def test_expired_plan_cannot_resume(self):
        plan = _make_plan(status="已过期")
        result = resume_schedule_and_backfill(plan["id"], as_of=AS_OF)
        assert result["ok"] is False
        assert result["status"] == "refused"
        assert plan["status"] == "已过期"

    def test_pause_then_resume_flow(self, paused_plan):
        pause_schedule(paused_plan["id"])
        resumed = resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        assert resumed["计划状态"] == "生效中"
        assert resumed["待补日期"] == ["2026-11-21", "2026-11-22"]


# ── 迟到补报、重复提交与接收方可见性 ──────────────────────────

class TestLateReportAndDedup:
    def test_original_robot_late_report_closes_pending_date(self, paused_plan):
        """21 日原机器人在补采排队时补报，21 日确认后待补只剩 22 日。"""
        resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        late = _add_batch(capture_date="2026-11-21", plan=paused_plan,
                          batch_code="CAPT-LATE21", status="校验中")

        result = confirm_capture_batch(late["id"], as_of=AS_OF)
        assert result["status"] == "confirmed"
        assert result["receipt_status"] == "有效"
        assert result["closed_backfill_tasks"]

        audit = audit_plan_gaps(paused_plan["id"], as_of=AS_OF)
        assert audit["confirmed_dates"] == ["2026-11-20", "2026-11-21"]
        assert audit["missing_dates"] == ["2026-11-22"]
        # 21 日补采任务被关闭，22 日任务保持待派发
        task_svc = service_for("task_queue")
        tasks = {t["采集日期"]: t for t in task_svc.get_backfill_tasks(paused_plan)}
        assert tasks["2026-11-21"]["status"] == "已消费"
        assert tasks["2026-11-22"]["status"] == "待派发"

    def test_duplicate_same_date_submission_only_counts_once(self, paused_plan):
        """同一采集日期第二份有效数据只认一次，重复提交只留重复回执。"""
        resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        first = _add_batch(capture_date="2026-11-21", plan=paused_plan,
                           batch_code="CAPT-21A", status="校验中")
        second = _add_batch(capture_date="2026-11-21", plan=paused_plan,
                            batch_code="CAPT-21B", status="校验中")

        assert confirm_capture_batch(first["id"], as_of=AS_OF)["status"] == "confirmed"
        duplicate = confirm_capture_batch(second["id"], as_of=AS_OF)

        assert duplicate["status"] == "duplicate"
        assert duplicate["duplicate_of"] == "CAPT-21A"
        assert duplicate["receipt_status"] == "重复驳回"
        assert service_for("capture_batch").get_entry(second["id"])["status"] != "已确认"

        audit = audit_plan_gaps(paused_plan["id"], as_of=AS_OF)
        usable_21 = [b for b in audit["usable_batches"] if b["采集日期"] == "2026-11-21"]
        assert len(usable_21) == 1
        assert usable_21[0]["批次编号"] == "CAPT-21A"

    def test_receiver_only_gets_valid_receipts(self, paused_plan):
        resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        good = _add_batch(capture_date="2026-11-22", plan=paused_plan,
                          batch_code="CAPT-22", status="校验中")
        confirm_capture_batch(good["id"], as_of=AS_OF)

        payload = receiver_capture_data(paused_plan["id"], as_of=AS_OF)
        assert payload["ok"] is True
        codes = [b["批次编号"] for b in payload["usable_batches"]]
        assert sorted(codes) == ["CAPT-22", "CAPT-D20"]
        assert payload["pending_dates"] == ["2026-11-21"]

    def test_unconfirmed_batch_not_visible_to_receiver(self, paused_plan):
        pending = _add_batch(capture_date="2026-11-21", plan=paused_plan,
                             batch_code="CAPT-PENDING", status="校验中")
        payload = receiver_capture_data(paused_plan["id"], as_of=AS_OF)
        codes = [b["批次编号"] for b in payload["usable_batches"]]
        assert "CAPT-PENDING" not in codes
        assert pending["id"] > 0

    def test_plan_field_shows_true_pending_scope(self, paused_plan):
        resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        assert paused_plan["待补日期"] == "2026-11-21,2026-11-22"
        late = _add_batch(capture_date="2026-11-21", plan=paused_plan,
                          batch_code="CAPT-LATE21", status="校验中")
        confirm_capture_batch(late["id"], as_of=AS_OF)
        assert paused_plan["待补日期"] == "2026-11-22"
        assert paused_plan["本次已补上日期"] == "2026-11-21"


# ── 队列领取复核 ──────────────────────────────────────────────

class TestClaimBackfillTask:
    def test_claim_reaudits_and_keeps_pending_until_confirmed(self, paused_plan):
        resumed = resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        task_id = next(t["task_id"] for t in resumed["本次新增补采任务"]
                       if t["采集日期"] == "2026-11-21")

        result = claim_backfill_task(task_id, "ROBO-0002")
        assert result["ok"] is True
        assert result["status"] == "claimed"
        assert result["采集日期"] == "2026-11-21"
        assert result["pending_dates"] == ["2026-11-21", "2026-11-22"]

    def test_claim_closes_task_already_covered(self, paused_plan):
        resumed = resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        task_id = next(t["task_id"] for t in resumed["本次新增补采任务"]
                       if t["采集日期"] == "2026-11-21")
        _add_batch(capture_date="2026-11-21", plan=paused_plan,
                   batch_code="CAPT-EARLY21", status="已确认")

        result = claim_backfill_task(task_id, "ROBO-0002")
        assert result["status"] == "obsolete"
        assert result["pending_dates"] == ["2026-11-22"]
        task_svc = service_for("task_queue")
        assert task_svc.get_entry(task_id)["status"] == "已消费"


# ── 端到端：完整场景 ──────────────────────────────────────────

class TestEndToEndScenario:
    def test_20_21_22_scenario(self, paused_plan):
        # 1. 恢复：只有 20 日确认，核对派发 21、22
        resumed = resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        assert resumed["已确认采集日期"] == ["2026-11-20"]
        assert resumed["待补日期"] == ["2026-11-21", "2026-11-22"]

        # 2. 入队不算完成
        assert resumed["待补日期"] == ["2026-11-21", "2026-11-22"]

        # 3. 21 日原机器人迟到补报，经链路2 确认批次
        run_svc = service_for("flow_run")
        run, _ = run_svc.create_entry({
            "执行编号": "FLOW-LATE21",
            "关联模板": "FLOW-0001",
            "执行节点": "ROBO-0009",
            "触发方式": "原机器人迟到上报",
            "开始时间": "2026-11-21T09:00:00",
            "结束时间": "2026-11-21T09:20:00",
            "步骤进度": "1/1",
            "产出记录数": "8",
        })
        late = collect_files_and_land_data(run["id"], [
            {"文件名称": "day21_late.csv"},
        ], [], {"计划编号": paused_plan["计划编号"], "采集日期": "2026-11-21", "as_of": AS_OF})
        assert late["status"] == "landed"
        assert late["receipt_status"] == "有效"

        # 4. 同一天再补一份重复数据，只认一次
        dup_run, _ = run_svc.create_entry({
            "执行编号": "FLOW-DUP21",
            "关联模板": "FLOW-0001",
            "执行节点": "ROBO-0010",
            "触发方式": "补采",
            "开始时间": "2026-11-21T11:00:00",
            "结束时间": "2026-11-21T11:10:00",
            "步骤进度": "1/1",
            "产出记录数": "8",
        })
        dup = collect_files_and_land_data(dup_run["id"], [
            {"文件名称": "day21_dup.csv"},
        ], [], {"计划编号": paused_plan["计划编号"], "采集日期": "2026-11-21", "as_of": AS_OF})
        assert dup["status"] == "duplicate"

        # 5. 22 日补采任务执行完成并确认
        batch_svc = service_for("capture_batch")
        b22 = _add_batch(capture_date="2026-11-22", plan=paused_plan,
                         batch_code="CAPT-22BF", status="校验中")
        assert confirm_capture_batch(b22["id"], as_of=AS_OF)["status"] == "confirmed"

        # 6. 最终 21、22 各一份可用批次，23 日不在范围内
        final = audit_plan_gaps(paused_plan["id"], as_of=AS_OF)
        assert final["confirmed_dates"] == ["2026-11-20", "2026-11-21", "2026-11-22"]
        assert final["missing_dates"] == []
        by_date = {}
        for item in final["usable_batches"]:
            by_date.setdefault(item["采集日期"], []).append(item["批次编号"])
        assert len(by_date["2026-11-21"]) == 1
        assert len(by_date["2026-11-22"]) == 1
        assert "2026-11-23" not in by_date
        assert final["expected_dates"] == ["2026-11-20", "2026-11-21", "2026-11-22"]


# ── 边界：窗口、重复与回执 ────────────────────────────────────

class TestBackfillBoundaries:
    def test_backfill_task_rejects_out_of_window_date(self, paused_plan):
        task_svc = service_for("task_queue")
        task, created = task_svc.create_backfill_task(paused_plan, "2026-11-23", as_of=AS_OF)
        assert task is None and created is False
        assert task_svc.get_backfill_tasks(paused_plan) == []

    def test_duplicate_batch_cannot_land_or_handoff(self, paused_plan):
        resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        first = _add_batch(capture_date="2026-11-22", plan=paused_plan,
                           batch_code="CAPT-22A", status="校验中")
        assert confirm_capture_batch(first["id"], as_of=AS_OF)["status"] == "confirmed"

        run_svc = service_for("flow_run")
        run, _ = run_svc.create_entry({
            "执行编号": "FLOW-DUP22", "关联模板": "FLOW-0001", "执行节点": "ROBO-0003",
            "触发方式": "补采", "开始时间": "2026-11-22T20:00:00",
            "结束时间": "2026-11-22T20:10:00", "步骤进度": "1/1", "产出记录数": "5",
        })
        dup = collect_files_and_land_data(run["id"], [{"文件名称": "day22_dup.csv"}], [],
                                          {"计划编号": paused_plan["计划编号"],
                                           "采集日期": "2026-11-22", "as_of": AS_OF})
        assert dup["status"] == "duplicate"
        assert dup["duplicate_of"] == "CAPT-22A"
        landing_svc = service_for("data_landing")
        assert all(l.get("关联批次") != "CAPT-R%s" % run["id"]
                   for l in landing_svc.list_entries(size=1000)[0])

    def test_collect_rejects_capture_date_outside_window(self, paused_plan):
        run_svc = service_for("flow_run")
        run, _ = run_svc.create_entry({
            "执行编号": "FLOW-OUT23", "关联模板": "FLOW-0001", "执行节点": "ROBO-0003",
            "触发方式": "补采", "开始时间": "2026-11-23T08:00:00",
            "结束时间": "2026-11-23T08:10:00", "步骤进度": "1/1", "产出记录数": "5",
        })
        result = collect_files_and_land_data(run["id"], [{"文件名称": "day23.csv"}], [],
                                             {"计划编号": paused_plan["计划编号"],
                                              "采集日期": "2026-11-23", "as_of": AS_OF})
        assert result["status"] == "rejected"
        batch_svc = service_for("capture_batch")
        assert all(b.get("采集日期") != "2026-11-23"
                   for b in batch_svc.list_entries(size=1000)[0])

    def test_unconfirmed_batch_keeps_date_pending(self, paused_plan):
        resume_schedule_and_backfill(paused_plan["id"], as_of=AS_OF)
        _add_batch(capture_date="2026-11-21", plan=paused_plan,
                   batch_code="CAPT-WAIT21", status="校验中")
        audit = audit_plan_gaps(paused_plan["id"], as_of=AS_OF)
        assert "2026-11-21" in audit["missing_dates"]
        awaiting = {item["采集日期"] for item in audit["awaiting_confirmation"]}
        assert awaiting == {"2026-11-21"}

    def test_trigger_rejects_dispatch_when_end_date_passed(self):
        tpl_svc = service_for("flow_template")
        tpl_id = tpl_svc.list_entries()[0][0]["id"]
        result = publish_flow_and_schedule(tpl_id, {
            "计划编号": "SCHE-OLD", "触发周期": "daily", "生效止日": "2020-01-01",
        })
        assert result["status"] == "rejected"


# ── 通用动作口径 ──────────────────────────────────────────────

class TestActionSemantics:
    def test_page_confirm_action_writes_receipt(self, paused_plan):
        batch = _add_batch(capture_date="2026-11-20", plan=paused_plan,
                           batch_code="CAPT-D20B", status="校验中")
        batch_svc = service_for("capture_batch")
        entry, message = batch_svc.run_action(batch["id"], "确认批次")
        assert entry["status"] == "校验中" or entry["status"] == "校验失败"
        assert "重复" in message
        assert entry["回执状态"] == "重复驳回"

    def test_page_confirm_action_normal_batch(self, paused_plan):
        batch = _add_batch(capture_date="2026-11-22", plan=paused_plan,
                           batch_code="CAPT-22OK", status="校验中")
        batch_svc = service_for("capture_batch")
        entry, message = batch_svc.run_action(batch["id"], "确认批次")
        assert entry["status"] == "已确认"
        assert entry["回执状态"] == "有效"
        assert "回执" in message

    def test_resume_action_is_not_a_generic_shortcut(self, paused_plan):
        plan_svc = service_for("schedule_plan")
        _, message = plan_svc.run_action(paused_plan["id"], "恢复排程")
        assert "不属于" in message
        assert paused_plan["status"] == "已暂停"
