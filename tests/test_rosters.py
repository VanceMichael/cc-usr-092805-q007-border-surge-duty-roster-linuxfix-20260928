from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.security import AccessContext


NOW = "2026-09-30T12:00:00+08:00"
QUAL = "边检查验"


class RosterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.sqlite3"
        self.app = CivicFlow.open(self.db_path, fixed_now=NOW)
        self.planner = AccessContext.system("planner")
        self.supervisor = AccessContext.system("supervisor")
        self.neutral = AccessContext.system("neutral-chief")
        self.home_party = AccessContext.system("home-dept")
        self.receiving_party = AccessContext.system("bridge-post")
        self.r = self.app.rosters

    def tearDown(self):
        self.temp.cleanup()

    # ---- 辅助 -----------------------------------------------------------

    def _credential(self, person_id: str, qualification: str = QUAL, key: str | None = None):
        self.app.repository.create("credentials", {
            "holder_type": "person", "holder_id": person_id, "credential_type": qualification,
            "issuer": "org:customs", "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00", "state": "effective",
        }, actor=self.planner.actor_id, request_key=key or f"cred-{person_id}-{qualification}")

    def _plan(self, rest_hours: float = 11):
        return self.r.create_plan(self.planner, scenario="假期高峰", port_id="port:bridge",
                                  opening_start_at="2026-09-30T08:00:00+08:00",
                                  opening_end_at="2026-10-01T02:00:00+08:00",
                                  rest_rule={"min_rest_hours": rest_hours}, request_key="plan")

    def _requirement(self, plan_id: str, start: str, end: str, min_people: int = 1, qualification: str = QUAL, key: str = "req"):
        return self.r.add_requirement(self.planner, plan_id, post_id="post:inspection",
                                      shift_start_at=start, shift_end_at=end,
                                      qualification=qualification, min_people=min_people, request_key=key)

    def _exemption(self, plan_id: str, person_id: str, start: str, end: str, *, requester="supervisor", approver=None, settle=True, key="ex"):
        approver_ctx = approver or AccessContext.system("neutral-chief")
        created = self.r.grant_overtime_exemption(
            approver_ctx, plan_id=plan_id, person_id=person_id, start_at=start, end_at=end,
            reason="高峰窗口人手不足", requester_id=requester, request_key=key + "-create")
        if not settle:
            return created
        return self.r.settle_overtime_exemption(approver_ctx, created["entity_id"], approve=True,
                                                reason="同意并安排补休", request_key=key + "-settle")

    # ---- 统一时间轴 -----------------------------------------------------

    def test_timeline_rejects_overlap_including_rest_and_standby(self):
        plan = self._plan(); req = self._requirement(plan["plan_id"], "2026-09-30T20:00:00+08:00", "2026-10-01T00:00:00+08:00", key="r1")
        self._credential("person:li")
        self.r.assign(self.planner, plan["plan_id"], "person:li", "regular", requirement_id=req["requirement_id"], request_key="a1")
        # 同一时段的待命同样占用时间轴。
        with self.assertRaises(ConflictError):
            self.r.assign(self.planner, plan["plan_id"], "person:li", "standby", start_at="2026-09-30T22:00:00+08:00", end_at="2026-09-30T23:00:00+08:00", request_key="a2")
        # 休息段也不能与班次重叠。
        with self.assertRaises(ConflictError):
            self.r.assign(self.planner, plan["plan_id"], "person:li", "rest", start_at="2026-09-30T23:30:00+08:00", end_at="2026-10-01T01:00:00+08:00", request_key="a3")

    def test_rest_shortage_blocks_assign_without_exemption(self):
        plan = self._plan(rest_hours=11)
        self._credential("person:li")
        day = self._requirement(plan["plan_id"], "2026-09-30T18:00:00+08:00", "2026-09-30T22:00:00+08:00", key="day")
        night = self._requirement(plan["plan_id"], "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00", key="night")
        self.r.assign(self.planner, plan["plan_id"], "person:li", "regular", requirement_id=day["requirement_id"], request_key="day-a")
        with self.assertRaises(ValidationError) as caught:
            self.r.assign(self.planner, plan["plan_id"], "person:li", "relief", requirement_id=night["requirement_id"], request_key="night-a")
        self.assertIn("休息", str(caught.exception))
        exemption = self._exemption(plan["plan_id"], "person:li", "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00")
        night_assignment = self.r.assign(self.planner, plan["plan_id"], "person:li", "relief", requirement_id=night["requirement_id"], exemption_id=exemption["entity_id"], request_key="night-a2")
        self.assertEqual(night_assignment["exempted_by"], exemption["entity_id"])

    def test_exemption_window_must_cover_assignment(self):
        plan = self._plan(); self._credential("person:li")
        day = self._requirement(plan["plan_id"], "2026-09-30T18:00:00+08:00", "2026-09-30T22:00:00+08:00", key="day")
        night = self._requirement(plan["plan_id"], "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00", key="night")
        self.r.assign(self.planner, plan["plan_id"], "person:li", "regular", requirement_id=day["requirement_id"], request_key="d")
        narrow = self._exemption(plan["plan_id"], "person:li", "2026-10-01T00:00:00+08:00", "2026-10-01T01:00:00+08:00", key="narrow")
        with self.assertRaises(ValidationError):
            self.r.assign(self.planner, plan["plan_id"], "person:li", "relief", requirement_id=night["requirement_id"], exemption_id=narrow["entity_id"], request_key="n")

    def test_credential_required_for_post(self):
        plan = self._plan()
        req = self._requirement(plan["plan_id"], "2026-09-30T20:00:00+08:00", "2026-09-30T22:00:00+08:00")
        with self.assertRaises(ValidationError):
            self.r.assign(self.planner, plan["plan_id"], "person:nobody", "regular", requirement_id=req["requirement_id"], request_key="x")

    # ---- 借调双确认 -----------------------------------------------------

    def test_loan_requires_both_confirmations_and_counts_only_after(self):
        plan = self._plan(); self._credential("person:wang")
        req = self._requirement(plan["plan_id"], "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00")
        loan = self.r.propose_loan(self.planner, plan["plan_id"], req["requirement_id"], "person:wang", home_post_id="post:gongbei", request_key="loan")
        self.assertEqual(loan["status"], "proposed")
        gaps_before = self.r.evaluate_gaps(self.supervisor, plan["plan_id"])
        self.assertEqual(len(gaps_before["unconfirmed_loans"]), 1)
        self.assertEqual(gaps_before["gaps"][0]["assigned"], 0)
        self.r.confirm_loan(self.home_party, loan["assignment_id"], "home", request_key="h")
        still = self.r.get_assignment(self.supervisor, loan["assignment_id"])
        self.assertEqual(still["status"], "proposed")
        self.r.confirm_loan(self.receiving_party, loan["assignment_id"], "receiving", request_key="rc")
        confirmed = self.r.get_assignment(self.supervisor, loan["assignment_id"])
        self.assertEqual(confirmed["status"], "confirmed")
        gaps_after = self.r.evaluate_gaps(self.supervisor, plan["plan_id"])
        self.assertEqual(gaps_after["gaps"], [])
        self.assertEqual(gaps_after["unconfirmed_loans"], [])

    def test_loan_two_parties_must_be_distinct_people(self):
        plan = self._plan(); self._credential("person:wang")
        req = self._requirement(plan["plan_id"], "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00")
        loan = self.r.propose_loan(self.planner, plan["plan_id"], req["requirement_id"], "person:wang", home_post_id="post:gongbei", request_key="loan")
        self.r.confirm_loan(self.home_party, loan["assignment_id"], "home", request_key="h")
        with self.assertRaises(PermissionDenied):
            self.r.confirm_loan(self.home_party, loan["assignment_id"], "receiving", request_key="same")

    def test_loan_rejection_frees_requirement(self):
        plan = self._plan(); self._credential("person:wang")
        req = self._requirement(plan["plan_id"], "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00")
        loan = self.r.propose_loan(self.planner, plan["plan_id"], req["requirement_id"], "person:wang", home_post_id="post:gongbei", request_key="loan")
        self.r.reject_loan(self.home_party, loan["assignment_id"], reason="拱北当晚同样缺人", request_key="rj")
        rejected = self.r.get_assignment(self.supervisor, loan["assignment_id"])
        self.assertEqual(rejected["status"], "rejected")
        # 拒绝后人员时间轴释放，可被派到别的岗位。
        other = self._requirement(plan["plan_id"], "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00", key="req-other")
        self.r.assign(self.planner, plan["plan_id"], "person:wang", "regular", requirement_id=other["requirement_id"], request_key="reuse")

    # ---- 超时豁免利益回避 ----------------------------------------------

    def test_overtime_exemption_requires_disinterested_supervisor(self):
        plan = self._plan()
        with self.assertRaises(PermissionDenied):
            self.r.grant_overtime_exemption(self.supervisor, plan_id=plan["plan_id"], person_id="person:li",
                                            start_at="2026-10-01T00:00:00+08:00", end_at="2026-10-01T02:00:00+08:00",
                                            reason="x", requester_id="supervisor", request_key="self-request")
        with self.assertRaises(PermissionDenied):
            self.r.grant_overtime_exemption(AccessContext.system("person:li"), plan_id=plan["plan_id"], person_id="person:li",
                                            start_at="2026-10-01T00:00:00+08:00", end_at="2026-10-01T02:00:00+08:00",
                                            reason="x", requester_id="supervisor", request_key="self-person")

    def test_pending_or_rejected_exemption_cannot_be_used(self):
        plan = self._plan(rest_hours=11); self._credential("person:li")
        day = self._requirement(plan["plan_id"], "2026-09-30T18:00:00+08:00", "2026-09-30T22:00:00+08:00", key="day")
        night = self._requirement(plan["plan_id"], "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00", key="night")
        self.r.assign(self.planner, plan["plan_id"], "person:li", "regular", requirement_id=day["requirement_id"], request_key="d")
        pending = self._exemption(plan["plan_id"], "person:li", "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00", settle=False, key="p")
        with self.assertRaises(ValidationError):
            self.r.assign(self.planner, plan["plan_id"], "person:li", "relief", requirement_id=night["requirement_id"], exemption_id=pending["entity_id"], request_key="np")
        rejected = self.r.settle_overtime_exemption(self.neutral, pending["entity_id"], approve=False, reason="风险过高", request_key="rj")
        self.assertEqual(rejected["state"], "rejected")
        with self.assertRaises(ValidationError):
            self.r.assign(self.planner, plan["plan_id"], "person:li", "relief", requirement_id=night["requirement_id"], exemption_id=pending["entity_id"], request_key="nr")

    # ---- 缺员暴露 -------------------------------------------------------

    def test_gaps_expose_shortage_and_reasons(self):
        plan = self._plan()
        req = self._requirement(plan["plan_id"], "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00", min_people=2)
        # 小赵被待命占在同一时段，无法计入查验岗位。
        self._credential("person:zhao")
        self.r.assign(self.planner, plan["plan_id"], "person:zhao", "standby",
                      start_at="2026-10-01T00:00:00+08:00", end_at="2026-10-01T02:00:00+08:00", request_key="standby")
        result = self.r.evaluate_gaps(self.supervisor, plan["plan_id"])
        self.assertEqual(len(result["gaps"]), 1)
        gap = result["gaps"][0]
        self.assertEqual(gap["shortage"], 2)
        self.assertEqual(gap["required"], 2)
        self.assertTrue(any("person:zhao" in reason and "时间轴冲突" in reason for reason in gap["reasons"]))
        self.assertTrue(any("没有任何具备资质" in reason for reason in gap["reasons"]))
        # 缺员进入主管看板并安排了班前一小时的持久提醒。
        dashboard = self.r.dashboard(self.supervisor, plan["plan_id"])
        self.assertEqual(len(dashboard["open_gaps"]), 1)
        with self.app.database.connect() as connection:
            reminders = connection.execute("SELECT * FROM scheduled_jobs WHERE job_type='roster-gap' AND status='waiting'").fetchall()
        self.assertEqual(len(reminders), 1)

    def test_gap_reasons_include_rest_shortage_violation(self):
        plan = self._plan(rest_hours=11); self._credential("person:li")
        day = self._requirement(plan["plan_id"], "2026-09-30T18:00:00+08:00", "2026-09-30T23:30:00+08:00", key="day")
        night = self._requirement(plan["plan_id"], "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00", key="night")
        self.r.assign(self.planner, plan["plan_id"], "person:li", "regular", requirement_id=day["requirement_id"], request_key="d")
        result = self.r.evaluate_gaps(self.supervisor, plan["plan_id"])
        night_gap = next(g for g in result["gaps"] if g["requirement_id"] == night["requirement_id"])
        self.assertTrue(any("休息不足" in reason for reason in night_gap["reasons"]))
        # 候选休息不足只作为缺口原因暴露；未实际签到不产生在岗违规事件。
        dashboard = self.r.dashboard(self.supervisor, plan["plan_id"])
        self.assertEqual(dashboard["rest_violations"], [])

    # ---- 签到冻结、异常与交接 ------------------------------------------

    def test_checkin_locks_assignment_notes_only_afterwards(self):
        plan = self._plan(); self._credential("person:li")
        req = self._requirement(plan["plan_id"], "2026-09-30T20:00:00+08:00", "2026-10-01T00:00:00+08:00")
        assignment = self.r.assign(self.planner, plan["plan_id"], "person:li", "regular", requirement_id=req["requirement_id"], request_key="a")
        checked = self.r.check_in(self.supervisor, assignment["assignment_id"], source="gate-kiosk", receipt_key="rcpt-1", sequence=1, occurred_at="2026-09-30T20:05:00+08:00", request_key="ci")
        self.assertEqual(checked["status"], "checked_in")
        self.assertEqual(checked["locked"], 1)
        with self.assertRaises(ConflictError):
            self.r.cancel_assignment(self.planner, assignment["assignment_id"], reason="想改派", request_key="cancel")
        with self.assertRaises(ValidationError):
            self.r.record_handover(self.supervisor, assignment["assignment_id"], to_person_id="person:wang", summary="  ", request_key="h-empty")
        note = self.r.add_exception_note(self.supervisor, assignment["assignment_id"], "客流高峰延后，继续值守", request_key="note")
        handover = self.r.record_handover(self.supervisor, assignment["assignment_id"], to_person_id="person:wang", summary="设备正常，注意绿色通道", request_key="handover")
        self.assertEqual(handover["status"], "checked_in")
        self.assertTrue(note["locked"])
        timeline = self.r.timeline(self.supervisor, plan["plan_id"])
        types = [event["event_type"] for event in timeline]
        self.assertIn("checked_in", types)
        self.assertIn("exception_note", types)
        self.assertIn("handover", types)

    def test_checkin_idempotent_and_contradictory_receipt_to_queue(self):
        plan = self._plan(); self._credential("person:li")
        req = self._requirement(plan["plan_id"], "2026-09-30T20:00:00+08:00", "2026-10-01T00:00:00+08:00")
        assignment = self.r.assign(self.planner, plan["plan_id"], "person:li", "regular", requirement_id=req["requirement_id"], request_key="a")
        kwargs = dict(source="gate-kiosk", receipt_key="rcpt-x", sequence=1, occurred_at="2026-09-30T20:05:00+08:00")
        first = self.r.check_in(self.supervisor, assignment["assignment_id"], payload={"gate": "A1"}, request_key="ci1", **kwargs)
        # 相同签到只记一次。
        second = self.r.check_in(self.supervisor, assignment["assignment_id"], payload={"gate": "A1"}, request_key="ci2", **kwargs)
        self.assertEqual(first["checked_in_at"], second["checked_in_at"])
        # 业务标识相同但内容矛盾的回执进入核对队列。
        with self.assertRaises(ConflictError):
            self.r.check_in(self.supervisor, assignment["assignment_id"], payload={"gate": "B2"}, request_key="ci3", **kwargs)
        queue = self.r.reconciliation_queue(self.supervisor)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["source_key"], "rcpt-x")

    def test_rest_violation_at_checkin_keeps_fact_and_flags_event(self):
        plan = self._plan(rest_hours=11); self._credential("person:li")
        early = self._requirement(plan["plan_id"], "2026-09-30T20:00:00+08:00", "2026-09-30T21:00:00+08:00", key="early")
        late = self._requirement(plan["plan_id"], "2026-09-30T21:00:00+08:00", "2026-09-30T22:00:00+08:00", key="late")
        self.r.assign(self.planner, plan["plan_id"], "person:li", "regular", requirement_id=late["requirement_id"], request_key="late-a")
        # 派班后又补排了一段紧挨着的跨口岸移动，签到时休息不足被标记，但在岗事实不变。
        self.r.create_travel(self.planner, plan["plan_id"], "person:li", from_post_id="port:gongbei", to_post_id="port:bridge",
                             start_at="2026-09-30T20:00:00+08:00", end_at="2026-09-30T21:00:00+08:00", request_key="travel")
        checked = self.r.check_in(self.supervisor, self._assignment_for(plan, "person:li", "regular")["assignment_id"],
                                  source="gate", receipt_key="rc-1", sequence=1, occurred_at="2026-09-30T21:02:00+08:00", request_key="ci")
        self.assertEqual(checked["status"], "checked_in")
        dashboard = self.r.dashboard(self.supervisor, plan["plan_id"])
        self.assertTrue(any(event["event_type"] == "rest_violation" for event in dashboard["rest_violations"]))

    def _assignment_for(self, plan, person_id: str, kind: str) -> dict:
        return next(a for a in self.r.list_assignments(self.supervisor, plan["plan_id"]) if a["person_id"] == person_id and a["kind"] == kind)

    # ---- 情景调整：已开始班次不重算 ------------------------------------

    def test_revise_only_touches_future_affected_requirements(self):
        plan = self._plan()
        started = self._requirement(plan["plan_id"], "2026-09-30T10:00:00+08:00", "2026-09-30T14:00:00+08:00", key="started")
        future = self._requirement(plan["plan_id"], "2026-09-30T20:00:00+08:00", "2026-10-01T00:00:00+08:00", key="future")
        self._credential("person:li"); self._credential("person:zhao")
        self.r.assign(self.planner, plan["plan_id"], "person:li", "regular", requirement_id=started["requirement_id"], request_key="a-started")
        self.r.assign(self.planner, plan["plan_id"], "person:zhao", "regular", requirement_id=future["requirement_id"], request_key="a-future")
        # 人员级休息段不挂岗位，重排岗位时保持不变。
        self.r.assign(self.planner, plan["plan_id"], "person:li", "rest", start_at="2026-09-30T20:00:00+08:00", end_at="2026-10-01T00:00:00+08:00", request_key="a-rest")
        result = self.r.revise_requirements(self.planner, plan["plan_id"], [
            {"post_id": "post:inspection", "shift_start_at": "2026-09-30T10:00:00+08:00", "shift_end_at": "2026-09-30T14:00:00+08:00", "qualification": QUAL, "min_people": 1},
            {"post_id": "post:inspection", "shift_start_at": "2026-09-30T22:00:00+08:00", "shift_end_at": "2026-10-01T02:00:00+08:00", "qualification": QUAL, "min_people": 1},
        ], reason="客流转向，夜间窗口后移两小时", request_key="revise")
        self.assertEqual(result["kept_started_requirements"], [started["requirement_id"]])
        self.assertEqual(result["cancelled_assignments"], [self._assignment_for(plan, "person:zhao", "regular")["assignment_id"]])
        self.assertEqual(len(result["added_requirements"]), 1)
        # 已开始的白班派班与人员休息段原样保留。
        self.assertEqual(self._assignment_for(plan, "person:li", "regular")["status"], "scheduled")
        self.assertEqual(self._assignment_for(plan, "person:li", "rest")["status"], "scheduled")
        self.assertEqual(self._assignment_for(plan, "person:zhao", "regular")["status"], "cancelled")
        # 开放时间再调整本身不改动任何班次。
        self.r.adjust_opening(self.planner, plan["plan_id"], new_end_at="2026-10-01T03:00:00+08:00", reason="开放再延长一小时", request_key="adj")
        self.assertEqual(self._assignment_for(plan, "person:li", "regular")["status"], "scheduled")

    # ---- 跨口岸移动与运力预约 ------------------------------------------

    def test_travel_uses_reservation_capacity(self):
        plan = self._plan()
        self.r.create_travel(self.planner, plan["plan_id"], "person:li", from_post_id="port:gongbei", to_post_id="port:bridge",
                             start_at="2026-09-30T19:00:00+08:00", end_at="2026-09-30T20:00:00+08:00",
                             resource_id="vehicle:shuttle", seats=10, capacity=10, request_key="t1")
        with self.assertRaises(ConflictError):
            self.r.create_travel(self.planner, plan["plan_id"], "person:wang", from_post_id="port:gongbei", to_post_id="port:bridge",
                                 start_at="2026-09-30T19:30:00+08:00", end_at="2026-09-30T20:30:00+08:00",
                                 resource_id="vehicle:shuttle", seats=1, capacity=10, request_key="t2")

    # ---- 时点回放与服务恢复 --------------------------------------------

    def test_replay_shows_on_duty_movements_and_handovers_as_of(self):
        plan = self._plan(); self._credential("person:li")
        req = self._requirement(plan["plan_id"], "2026-09-30T20:00:00+08:00", "2026-10-01T00:00:00+08:00")
        assignment = self.r.assign(self.planner, plan["plan_id"], "person:li", "regular", requirement_id=req["requirement_id"], request_key="a")
        before = self.r.replay(self.supervisor, plan["plan_id"], as_of="2026-09-30T19:59:00+08:00")
        self.assertEqual(before["on_duty"], [])
        self.r.check_in(self.supervisor, assignment["assignment_id"], source="gate", receipt_key="rc", sequence=1, occurred_at="2026-09-30T20:05:00+08:00", request_key="ci")
        self.r.record_handover(self.supervisor, assignment["assignment_id"], to_person_id="person:wang", summary="交接设备", request_key="h")
        during = self.r.replay(self.supervisor, plan["plan_id"], as_of="2026-09-30T21:00:00+08:00")
        self.assertEqual([a["person_id"] for a in during["on_duty"]], ["person:li"])
        self.assertTrue(any(e["event_type"] == "handover" for e in during["records"]))
        self.assertTrue(any(e["event_type"] == "assigned" for e in during["movements"]))
        after_shift = self.r.replay(self.supervisor, plan["plan_id"], as_of="2026-10-01T01:00:00+08:00")
        self.assertEqual(after_shift["on_duty"], [])

    def test_dashboard_survives_service_restart(self):
        plan = self._plan()
        req = self._requirement(plan["plan_id"], "2026-10-01T00:00:00+08:00", "2026-10-01T02:00:00+08:00")
        self._credential("person:wang")
        self.r.propose_loan(self.planner, plan["plan_id"], req["requirement_id"], "person:wang", home_post_id="post:gongbei", request_key="loan")
        self.r.evaluate_gaps(self.supervisor, plan["plan_id"])
        reopened = CivicFlow.open(self.db_path, fixed_now=NOW)
        dashboard = reopened.rosters.dashboard(AccessContext.system("recovery-supervisor"), plan["plan_id"])
        self.assertEqual(len(dashboard["open_gaps"]), 1)
        self.assertEqual(len(dashboard["unconfirmed_loans"]), 1)
        with reopened.database.connect() as connection:
            reminders = connection.execute("SELECT * FROM scheduled_jobs WHERE job_type='roster-gap' AND status='waiting'").fetchall()
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reopened.verify()["inbox_conflicts"], 0)

    # ---- 权限与审计 -----------------------------------------------------

    def test_permissions_enforced(self):
        limited = AccessContext(actor_id="reader", permissions=frozenset({"read:rosters"}))
        with self.assertRaises(PermissionDenied):
            self.r.create_plan(limited, scenario="x", port_id="port:bridge", opening_start_at="2026-09-30T20:00:00+08:00", opening_end_at="2026-09-30T22:00:00+08:00", request_key="p")

    def test_audit_chain_intact_after_roster_ops(self):
        plan = self._plan(); self._credential("person:li")
        req = self._requirement(plan["plan_id"], "2026-09-30T20:00:00+08:00", "2026-09-30T22:00:00+08:00")
        assignment = self.r.assign(self.planner, plan["plan_id"], "person:li", "regular", requirement_id=req["requirement_id"], request_key="a")
        self.r.evaluate_gaps(self.supervisor, plan["plan_id"])
        self.r.check_in(self.supervisor, assignment["assignment_id"], source="gate", receipt_key="rc", sequence=1, occurred_at="2026-09-30T20:05:00+08:00", request_key="ci")
        self.assertGreaterEqual(self.app.verify()["audit_entries"], 4)


if __name__ == "__main__":
    unittest.main()
