"""高峰值守排班：情景岗位需求、统一时间轴派班、缺员暴露与在岗事实回放。

正常班、待命、借调、休息、跨口岸移动与替班共用同一条人员时间轴；
签到一旦发生，在岗事实即冻结，只能追加异常说明与交接记录。
借调须由原部门与接收岗位分别确认，超时豁免须由无利益关系的主管批准。
权限、资源预约、审批、通知、审计链、幂等、核对队列与持久任务均复用平台机制。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from .audit import AuditLog
from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .idempotency import IdempotencyStore
from .identifiers import new_id, require_safe
from .inbox import Inbox
from .jobs import JobQueue
from .jsonutil import canonical_json
from .outbox import Outbox
from .repository import EntityRepository
from .reservations import ReservationBook
from .security import AccessContext, assert_distinct
from .timeutil import Clock, canonical_instant, parse_instant

DUTY_KINDS = ("regular", "standby", "loan", "rest", "travel", "relief")
WORK_KINDS = ("regular", "loan", "relief", "standby", "travel")
LIVE_STATUSES = ("scheduled", "confirmed", "checked_in", "completed")
DEFAULT_REST_HOURS = 11


@dataclass(frozen=True)
class RosterService:
    """高峰值守排班领域服务。"""

    database: Database
    clock: Clock
    repository: EntityRepository
    inbox: Inbox
    outbox: Outbox
    jobs: JobQueue
    reservations: ReservationBook

    # ---- 预案与岗位需求 -------------------------------------------------

    def create_plan(self, context: AccessContext, *, scenario: str, port_id: str, opening_start_at: str, opening_end_at: str, rest_rule: dict | None = None, request_key: str) -> dict:
        context.require("write:rosters")
        require_safe(port_id, "口岸标识")
        start = canonical_instant(opening_start_at); end = canonical_instant(opening_end_at)
        if parse_instant(start) >= parse_instant(end):
            raise ValidationError("口岸开放结束时间必须晚于开始时间")
        if not scenario.strip():
            raise ValidationError("客流情景说明不能为空")
        rule = dict(rest_rule or {})
        rule.setdefault("min_rest_hours", DEFAULT_REST_HOURS)
        if not isinstance(rule["min_rest_hours"], (int, float)) or isinstance(rule["min_rest_hours"], bool) or rule["min_rest_hours"] < 0:
            raise ValidationError("最短休息时长不合法")
        audit = AuditLog(self.clock); idempotency = IdempotencyStore(self.clock)
        with self.database.transaction() as connection:
            def operation() -> dict:
                now = self.clock.now(); plan_id = new_id("plan")
                connection.execute(
                    "INSERT INTO duty_plans(plan_id,scenario,port_id,opening_start_at,opening_end_at,status,version,rest_rule_json,created_at,updated_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (plan_id, scenario.strip(), port_id, start, end, "active", 1, canonical_json(rule), now, now, context.actor_id))
                row = self._plan_row(connection, plan_id)
                audit.append(connection, actor_id=context.actor_id, action="roster.plan_create", entity_type="duty_plans", entity_id=plan_id, version=1, detail=row)
                return row
            return idempotency.execute(connection, scope="roster:plan_create", request_key=request_key, request={"scenario": scenario, "port_id": port_id, "start": start, "end": end}, operation=operation)

    def get_plan(self, context: AccessContext, plan_id: str) -> dict:
        context.require("read:rosters")
        with self.database.connect() as connection:
            return self._plan_row(connection, plan_id)

    def add_requirement(self, context: AccessContext, plan_id: str, *, post_id: str, shift_start_at: str, shift_end_at: str, qualification: str, min_people: int, request_key: str) -> dict:
        context.require("write:rosters")
        post_id = self._require_text(post_id, "岗位标识"); qualification = self._require_text(qualification, "资质标识")
        start = canonical_instant(shift_start_at); end = canonical_instant(shift_end_at)
        if parse_instant(start) >= parse_instant(end):
            raise ValidationError("值守时段结束时间必须晚于开始时间")
        if not isinstance(min_people, int) or isinstance(min_people, bool) or min_people < 1:
            raise ValidationError("最低在岗人数必须为正整数")
        audit = AuditLog(self.clock); idempotency = IdempotencyStore(self.clock)
        with self.database.transaction() as connection:
            def operation() -> dict:
                plan = self._plan_row(connection, plan_id)
                requirement_id = new_id("req")
                connection.execute(
                    "INSERT INTO duty_requirements(requirement_id,plan_id,post_id,shift_start_at,shift_end_at,qualification,min_people,affected_count,status,version) VALUES(?,?,?,?,?,?,?,0,'active',1)",
                    (requirement_id, plan_id, post_id, start, end, qualification, min_people))
                row = self._requirement_row(connection, requirement_id)
                audit.append(connection, actor_id=context.actor_id, action="roster.requirement_add", entity_type="duty_requirements", entity_id=requirement_id, version=plan["version"], detail=row)
                return row
            return idempotency.execute(connection, scope=f"roster:requirement_add:{plan_id}", request_key=request_key, request={"post_id": post_id, "start": start, "end": end, "qualification": qualification, "min_people": min_people}, operation=operation)

    def adjust_opening(self, context: AccessContext, plan_id: str, *, new_start_at: str | None = None, new_end_at: str | None = None, reason: str, request_key: str) -> dict:
        """客流转向或开放时间调整：只改预案窗口；已开始班次一律不重算。"""
        context.require("write:rosters")
        if not reason.strip():
            raise ValidationError("开放时间调整必须说明原因")
        audit = AuditLog(self.clock); idempotency = IdempotencyStore(self.clock)
        with self.database.transaction() as connection:
            def operation() -> dict:
                plan = self._plan_row(connection, plan_id)
                start = canonical_instant(new_start_at) if new_start_at else plan["opening_start_at"]
                end = canonical_instant(new_end_at) if new_end_at else plan["opening_end_at"]
                if parse_instant(start) >= parse_instant(end):
                    raise ValidationError("口岸开放结束时间必须晚于开始时间")
                now = self.clock.now(); version = plan["version"] + 1
                connection.execute("UPDATE duty_plans SET opening_start_at=?,opening_end_at=?,version=?,updated_at=? WHERE plan_id=?", (start, end, version, now, plan_id))
                self._event(connection, actor_id=context.actor_id, event_type="opening_adjusted", plan_id=plan_id, detail={"reason": reason.strip(), "before": {"start": plan["opening_start_at"], "end": plan["opening_end_at"]}, "after": {"start": start, "end": end}, "at": now})
                row = self._plan_row(connection, plan_id)
                audit.append(connection, actor_id=context.actor_id, action="roster.opening_adjust", entity_type="duty_plans", entity_id=plan_id, version=version, detail={"reason": reason, "start": start, "end": end})
                return row
            return idempotency.execute(connection, scope=f"roster:opening_adjust:{plan_id}", request_key=request_key, request={"new_start": new_start_at, "new_end": new_end_at, "reason": reason}, operation=operation)

    def revise_requirements(self, context: AccessContext, plan_id: str, requirements: list[dict], *, reason: str, request_key: str) -> dict:
        """按新情景重排：只处理尚未开始且确实受影响的岗位，已开始班次保持不变。"""
        context.require("write:rosters")
        if not reason.strip():
            raise ValidationError("情景重排必须说明原因")
        if not isinstance(requirements, list) or not requirements:
            raise ValidationError("新情景的岗位需求不能为空")
        normalized = [self._normalize_requirement(item) for item in requirements]
        audit = AuditLog(self.clock); idempotency = IdempotencyStore(self.clock)
        with self.database.transaction() as connection:
            def operation() -> dict:
                plan = self._plan_row(connection, plan_id); now = self.clock.now()
                wanted = {(item["post_id"], item["shift_start_at"], item["shift_end_at"], item["qualification"]): item["min_people"] for item in normalized}
                cancelled_assignments: list[str] = []
                superseded: list[str] = []
                kept_started: list[str] = []
                existing = connection.execute("SELECT * FROM duty_requirements WHERE plan_id=? AND status='active'", (plan_id,)).fetchall()
                present: set[tuple] = set()
                for row in existing:
                    key = (row["post_id"], row["shift_start_at"], row["shift_end_at"], row["qualification"])
                    started = parse_instant(row["shift_start_at"]) <= parse_instant(now)
                    if not started and key in wanted and wanted[key] == row["min_people"]:
                        present.add(key)
                        continue
                    if started:
                        # 已开始班次不重算：需求与派班原样保留。
                        present.add(key)
                        kept_started.append(row["requirement_id"])
                        continue
                    for assignment in connection.execute("SELECT assignment_id FROM duty_assignments WHERE requirement_id=? AND status IN ('proposed','scheduled','confirmed') AND locked=0", (row["requirement_id"],)).fetchall():
                        connection.execute("UPDATE duty_assignments SET status='cancelled' WHERE assignment_id=?", (assignment["assignment_id"],))
                        cancelled_assignments.append(assignment["assignment_id"])
                        self._event(connection, actor_id=context.actor_id, event_type="assignment_recancelled", plan_id=plan_id, assignment_id=assignment["assignment_id"], detail={"reason": reason.strip(), "post_id": row["post_id"], "at": now})
                        self._notify(connection, topic="roster.assignment_recancelled", aggregate_id=plan_id, payload={"assignment_id": assignment["assignment_id"], "post_id": row["post_id"], "reason": reason.strip()})
                    connection.execute("UPDATE duty_requirements SET status='superseded',affected_count=affected_count+1,version=version+1 WHERE requirement_id=?", (row["requirement_id"],))
                    superseded.append(row["requirement_id"])
                added: list[dict] = []
                for item in normalized:
                    key = (item["post_id"], item["shift_start_at"], item["shift_end_at"], item["qualification"])
                    if key in present:
                        continue
                    requirement_id = new_id("req")
                    connection.execute(
                        "INSERT INTO duty_requirements(requirement_id,plan_id,post_id,shift_start_at,shift_end_at,qualification,min_people,affected_count,status,version) VALUES(?,?,?,?,?,?,?,1,'active',1)",
                        (requirement_id, plan_id, item["post_id"], item["shift_start_at"], item["shift_end_at"], item["qualification"], item["min_people"]))
                    added.append(self._requirement_row(connection, requirement_id))
                self._event(connection, actor_id=context.actor_id, event_type="requirements_revised", plan_id=plan_id, detail={"reason": reason.strip(), "cancelled_assignments": cancelled_assignments, "superseded": superseded, "kept_started": kept_started, "added": added, "at": now})
                audit.append(connection, actor_id=context.actor_id, action="roster.requirements_revise", entity_type="duty_plans", entity_id=plan_id, version=plan["version"], detail={"reason": reason, "cancelled": len(cancelled_assignments), "superseded": len(superseded), "kept_started": len(kept_started), "added": len(added)})
                return {"cancelled_assignments": cancelled_assignments, "superseded_requirements": superseded, "kept_started_requirements": kept_started, "added_requirements": added}
            return idempotency.execute(connection, scope=f"roster:requirements_revise:{plan_id}", request_key=request_key, request={"requirements": normalized, "reason": reason}, operation=operation)

    # ---- 派班：正常班、待命、替班、休息、借调、跨口岸移动 --------------

    def assign(self, context: AccessContext, plan_id: str, person_id: str, kind: str, *, requirement_id: str | None = None, start_at: str | None = None, end_at: str | None = None, exemption_id: str = "", request_key: str) -> dict:
        if kind in ("loan", "travel") or kind not in DUTY_KINDS:
            raise ValidationError("该派班类型请使用借调或跨口岸移动专用入口")
        context.require("write:rosters")
        return self._create_assignment(context, plan_id, person_id, kind, requirement_id=requirement_id, start_at=start_at, end_at=end_at, exemption_id=exemption_id, request_key=request_key)

    def propose_loan(self, context: AccessContext, plan_id: str, requirement_id: str, person_id: str, *, home_post_id: str, start_at: str | None = None, end_at: str | None = None, request_key: str) -> dict:
        """发起借调：先过统一时间轴冲突校验；双方确认前不占用接收岗位名额。"""
        context.require("write:rosters")
        require_safe(home_post_id, "原部门岗位标识")
        return self._create_assignment(context, plan_id, person_id, "loan", requirement_id=requirement_id, start_at=start_at, end_at=end_at, home_post_id=home_post_id, request_key=request_key, initial_status="proposed")

    def confirm_loan(self, context: AccessContext, assignment_id: str, party: str, *, request_key: str) -> dict:
        """借调须由原部门(home)与接收岗位(receiving)分别确认，且两方不能是同一人。"""
        context.require("confirm:rosters")
        if party not in ("home", "receiving"):
            raise ValidationError("确认方必须是 home 或 receiving")
        audit = AuditLog(self.clock); idempotency = IdempotencyStore(self.clock)
        with self.database.transaction() as connection:
            def operation() -> dict:
                assignment = self._assignment_row(connection, assignment_id)
                if assignment["kind"] != "loan":
                    raise ValidationError("只有借调需要双方确认")
                if assignment["status"] not in ("proposed", "confirmed"):
                    raise ConflictError(f"借调当前状态为 {assignment['status']}，不能确认")
                now = self.clock.now()
                if party == "home":
                    if assignment["confirmed_home_by"]:
                        raise ConflictError("原部门已经确认过该借调")
                    if assignment["confirmed_receiving_by"]:
                        assert_distinct(assignment["confirmed_receiving_by"], context.actor_id)
                    connection.execute("UPDATE duty_assignments SET confirmed_home_at=?,confirmed_home_by=? WHERE assignment_id=?", (now, context.actor_id, assignment_id))
                else:
                    if assignment["confirmed_receiving_by"]:
                        raise ConflictError("接收岗位已经确认过该借调")
                    if assignment["confirmed_home_by"]:
                        assert_distinct(assignment["confirmed_home_by"], context.actor_id)
                    connection.execute("UPDATE duty_assignments SET confirmed_receiving_at=?,confirmed_receiving_by=? WHERE assignment_id=?", (now, context.actor_id, assignment_id))
                row = self._assignment_row(connection, assignment_id)
                if row["confirmed_home_by"] and row["confirmed_receiving_by"] and row["status"] == "proposed":
                    connection.execute("UPDATE duty_assignments SET status='confirmed' WHERE assignment_id=?", (assignment_id,))
                    row = self._assignment_row(connection, assignment_id)
                self._event(connection, actor_id=context.actor_id, event_type="loan_confirmed", plan_id=assignment["plan_id"], assignment_id=assignment_id, detail={"party": party, "status": row["status"], "at": now})
                audit.append(connection, actor_id=context.actor_id, action="roster.loan_confirm", entity_type="duty_assignments", entity_id=assignment_id, version=1, detail={"party": party, "status": row["status"]})
                self._notify(connection, topic="roster.loan_confirmed", aggregate_id=assignment["plan_id"], payload={"assignment_id": assignment_id, "party": party, "status": row["status"]})
                return row
            return idempotency.execute(connection, scope=f"roster:loan_confirm:{assignment_id}:{party}", request_key=request_key, request={"party": party}, operation=operation)

    def reject_loan(self, context: AccessContext, assignment_id: str, *, reason: str, request_key: str) -> dict:
        context.require("confirm:rosters")
        if not reason.strip():
            raise ValidationError("拒绝借调必须说明原因")
        audit = AuditLog(self.clock); idempotency = IdempotencyStore(self.clock)
        with self.database.transaction() as connection:
            def operation() -> dict:
                assignment = self._assignment_row(connection, assignment_id)
                if assignment["kind"] != "loan" or assignment["status"] not in ("proposed", "confirmed"):
                    raise ConflictError("当前借调状态不能拒绝")
                connection.execute("UPDATE duty_assignments SET status='rejected' WHERE assignment_id=?", (assignment_id,))
                row = self._assignment_row(connection, assignment_id)
                self._event(connection, actor_id=context.actor_id, event_type="loan_rejected", plan_id=assignment["plan_id"], assignment_id=assignment_id, detail={"reason": reason.strip(), "at": self.clock.now()})
                audit.append(connection, actor_id=context.actor_id, action="roster.loan_reject", entity_type="duty_assignments", entity_id=assignment_id, version=1, detail={"reason": reason})
                self._notify(connection, topic="roster.loan_rejected", aggregate_id=assignment["plan_id"], payload={"assignment_id": assignment_id, "reason": reason.strip()})
                return row
            return idempotency.execute(connection, scope=f"roster:loan_reject:{assignment_id}", request_key=request_key, request={"reason": reason}, operation=operation)

    def create_travel(self, context: AccessContext, plan_id: str, person_id: str, *, from_post_id: str, to_post_id: str, start_at: str, end_at: str, resource_id: str | None = None, seats: int | None = None, capacity: int | None = None, request_key: str) -> dict:
        """跨口岸移动同样占用时间轴；安排车辆时复用资源预约的容量控制。"""
        context.require("write:rosters")
        require_safe(from_post_id, "起点口岸"); require_safe(to_post_id, "目的口岸")
        reservation_id = ""
        if resource_id:
            if not seats or not capacity:
                raise ValidationError("预约运力必须给出座位数与容量")
            reservation_id = self.reservations.reserve(resource_id=resource_id, subject_id=plan_id, quantity=seats, capacity=capacity, start_at=start_at, end_at=end_at, actor=context.actor_id)["reservation_id"]
        try:
            return self._create_assignment(context, plan_id, person_id, "travel", start_at=start_at, end_at=end_at, home_post_id=from_post_id, request_key=request_key, detail={"from_post_id": from_post_id, "to_post_id": to_post_id, "reservation_id": reservation_id})
        except Exception:
            if reservation_id:
                self.reservations.release(reservation_id, expected_version=1)
            raise

    def grant_overtime_exemption(self, context: AccessContext, *, plan_id: str, person_id: str, start_at: str, end_at: str, reason: str, requester_id: str, request_key: str) -> dict:
        """超时豁免由无利益关系主管批准：批准人不得是申请主管或被豁免人本人。"""
        context.require("approve:rosters")
        require_safe(requester_id, "申请主管")
        assert_distinct(requester_id, context.actor_id)
        assert_distinct(person_id, context.actor_id)
        if not reason.strip():
            raise ValidationError("超时豁免必须说明原因")
        start = canonical_instant(start_at); end = canonical_instant(end_at)
        if parse_instant(start) >= parse_instant(end):
            raise ValidationError("豁免时段结束时间必须晚于开始时间")
        with self.database.connect() as connection:
            self._plan_row(connection, plan_id)
        payload = {"case_id": plan_id, "step": "超时豁免", "reviewer": context.actor_id, "decision": "pending", "reason": reason.strip(), "person_id": person_id, "start_at": start, "end_at": end, "state": "pending"}
        return self.repository.create("approvals", payload, actor=context.actor_id, request_key=request_key)

    def settle_overtime_exemption(self, context: AccessContext, approval_id: str, *, approve: bool, reason: str, request_key: str) -> dict:
        """豁免审批以平台 approvals 状态机落定；批准后派班才可引用。重复提交按幂等处理。"""
        context.require("approve:rosters")
        if not reason.strip():
            raise ValidationError("豁免审批必须说明意见")
        current = self.repository.get("approvals", approval_id)
        target = "approved" if approve else "rejected"
        if current["state"] == target:
            return current
        if current["state"] != "pending":
            raise ConflictError(f"豁免审批当前状态为 {current['state']}，不能再作 {target} 决定")
        return self.repository.update("approvals", approval_id, {"state": target, "decision": target, "decision_reason": reason.strip()}, actor=context.actor_id, expected_version=current["version"], request_key=request_key)

    # ---- 签到冻结、异常说明与交接 --------------------------------------

    def check_in(self, context: AccessContext, assignment_id: str, *, source: str, receipt_key: str, sequence: int, occurred_at: str, payload: dict | None = None, request_key: str) -> dict:
        """签到回执先进入接入层：相同签到只记一次，同业务标识矛盾回执进入核对队列。"""
        context.require("checkin:rosters")
        receipt_payload = dict(payload or {}); receipt_payload.setdefault("assignment_id", assignment_id)
        receipt = self.inbox.receive(source=source, source_key=receipt_key, sequence=sequence, payload=receipt_payload, occurred_at=occurred_at)
        if receipt["status"] == "duplicate":
            with self.database.connect() as connection:
                return self._assignment_row(connection, assignment_id)
        audit = AuditLog(self.clock); idempotency = IdempotencyStore(self.clock)
        with self.database.transaction() as connection:
            def operation() -> dict:
                assignment = self._assignment_row(connection, assignment_id)
                if assignment["status"] not in ("scheduled", "confirmed"):
                    raise ConflictError(f"派班当前状态为 {assignment['status']}，不能签到")
                at = canonical_instant(occurred_at)
                connection.execute("UPDATE duty_assignments SET status='checked_in',checked_in_at=?,locked=1 WHERE assignment_id=?", (at, assignment_id))
                row = self._assignment_row(connection, assignment_id)
                self._event(connection, actor_id=context.actor_id, event_type="checked_in", plan_id=assignment["plan_id"], assignment_id=assignment_id, detail={"at": at, "receipt_key": receipt_key, "source": source})
                rest_gap = self._rest_gap(connection, self._plan_row(connection, assignment["plan_id"]), assignment["person_id"], assignment["start_at"])
                if rest_gap is not None and not assignment["exempted_by"]:
                    # 在岗事实保持不变，休息违规仅作为异常事件暴露并通知主管。
                    self._event(connection, actor_id=context.actor_id, event_type="rest_violation", plan_id=assignment["plan_id"], assignment_id=assignment_id, detail={"at": at, "rest_hours": rest_gap, "receipt_key": receipt_key})
                    self._notify(connection, topic="roster.rest_violation", aggregate_id=assignment["plan_id"], payload={"assignment_id": assignment_id, "rest_hours": rest_gap})
                audit.append(connection, actor_id=context.actor_id, action="roster.check_in", entity_type="duty_assignments", entity_id=assignment_id, version=1, detail={"at": at, "receipt": receipt["status"]})
                return row
            return idempotency.execute(connection, scope=f"roster:check_in:{assignment_id}", request_key=request_key, request={"receipt_key": receipt_key, "sequence": sequence}, operation=operation)

    def add_exception_note(self, context: AccessContext, assignment_id: str, note: str, *, request_key: str) -> dict:
        """签到后只允许补充异常说明，不改变任何在岗事实。"""
        context.require("write:rosters")
        if not note.strip():
            raise ValidationError("异常说明不能为空")
        audit = AuditLog(self.clock); idempotency = IdempotencyStore(self.clock)
        with self.database.transaction() as connection:
            def operation() -> dict:
                assignment = self._assignment_row(connection, assignment_id)
                if not assignment["locked"]:
                    raise ConflictError("只有签到后的班次才能补充异常说明")
                self._event(connection, actor_id=context.actor_id, event_type="exception_note", plan_id=assignment["plan_id"], assignment_id=assignment_id, detail={"note": note.strip(), "at": self.clock.now()})
                audit.append(connection, actor_id=context.actor_id, action="roster.exception_note", entity_type="duty_assignments", entity_id=assignment_id, version=1, detail={"note": note})
                return self._assignment_row(connection, assignment_id)
            return idempotency.execute(connection, scope=f"roster:exception_note:{assignment_id}", request_key=request_key, request={"note": note}, operation=operation)

    def record_handover(self, context: AccessContext, assignment_id: str, *, to_person_id: str, summary: str, request_key: str) -> dict:
        """交接记录只追加，作为事后说明在岗依据。"""
        context.require("write:rosters")
        require_safe(to_person_id, "接班人")
        if not summary.strip():
            raise ValidationError("交接内容不能为空")
        audit = AuditLog(self.clock); idempotency = IdempotencyStore(self.clock)
        with self.database.transaction() as connection:
            def operation() -> dict:
                assignment = self._assignment_row(connection, assignment_id)
                if not assignment["locked"]:
                    raise ConflictError("只有签到后的班次才能记录交接")
                at = self.clock.now()
                self._event(connection, actor_id=context.actor_id, event_type="handover", plan_id=assignment["plan_id"], assignment_id=assignment_id, detail={"to_person_id": to_person_id, "summary": summary.strip(), "at": at})
                audit.append(connection, actor_id=context.actor_id, action="roster.handover", entity_type="duty_assignments", entity_id=assignment_id, version=1, detail={"to_person_id": to_person_id, "summary": summary})
                return self._assignment_row(connection, assignment_id)
            return idempotency.execute(connection, scope=f"roster:handover:{assignment_id}", request_key=request_key, request={"to_person_id": to_person_id, "summary": summary}, operation=operation)

    def cancel_assignment(self, context: AccessContext, assignment_id: str, *, reason: str, request_key: str) -> dict:
        context.require("write:rosters")
        if not reason.strip():
            raise ValidationError("取消派班必须说明原因")
        audit = AuditLog(self.clock); idempotency = IdempotencyStore(self.clock)
        with self.database.transaction() as connection:
            def operation() -> dict:
                assignment = self._assignment_row(connection, assignment_id)
                if assignment["locked"]:
                    raise ConflictError("班次已经签到，在岗事实不能撤销，只能追加异常说明或交接记录")
                if assignment["status"] not in ("proposed", "scheduled", "confirmed"):
                    raise ConflictError(f"派班状态 {assignment['status']} 不能取消")
                if parse_instant(assignment["start_at"]) <= parse_instant(self.clock.now()):
                    raise ConflictError("班次已经开始，不能重算或取消")
                connection.execute("UPDATE duty_assignments SET status='cancelled' WHERE assignment_id=?", (assignment_id,))
                row = self._assignment_row(connection, assignment_id)
                self._event(connection, actor_id=context.actor_id, event_type="assignment_cancelled", plan_id=assignment["plan_id"], assignment_id=assignment_id, detail={"reason": reason.strip(), "at": self.clock.now()})
                audit.append(connection, actor_id=context.actor_id, action="roster.assignment_cancel", entity_type="duty_assignments", entity_id=assignment_id, version=1, detail={"reason": reason})
                return row
            return idempotency.execute(connection, scope=f"roster:assignment_cancel:{assignment_id}", request_key=request_key, request={"reason": reason}, operation=operation)

    # ---- 缺员评估与主管看板 --------------------------------------------

    def evaluate_gaps(self, context: AccessContext, plan_id: str) -> dict:
        """逐岗位暴露无法满足的需求及具体原因，并为临近缺口安排持久提醒。"""
        context.require("read:rosters")
        holders = self._credential_holders()
        exemptions = self._exemptions(plan_id)
        audit = AuditLog(self.clock)
        with self.database.transaction() as connection:
            plan = self._plan_row(connection, plan_id); now = self.clock.now()
            open_gaps: list[dict] = []
            unconfirmed_loans: list[dict] = []
            requirements = connection.execute("SELECT * FROM duty_requirements WHERE plan_id=? AND status='active' ORDER BY shift_start_at,post_id", (plan_id,)).fetchall()
            for requirement in requirements:
                assignments = connection.execute(
                    "SELECT * FROM duty_assignments WHERE requirement_id=? AND status IN ('scheduled','confirmed','checked_in','completed') AND kind IN ('regular','loan','relief') AND start_at<? AND end_at>?",
                    (requirement["requirement_id"], requirement["shift_end_at"], requirement["shift_start_at"])).fetchall()
                covered = len(assignments)
                for loan in connection.execute("SELECT * FROM duty_assignments WHERE requirement_id=? AND status='proposed' AND kind='loan'", (requirement["requirement_id"],)).fetchall():
                    unconfirmed_loans.append(dict(loan))
                shortage = max(0, requirement["min_people"] - covered)
                reasons: list[str] = []
                if shortage:
                    used = {a["person_id"] for a in assignments}
                    candidates = [person_id for person_id in holders.get(requirement["qualification"], []) if person_id not in used]
                    blockers: list[tuple[str, str]] = []
                    for person_id in candidates:
                        blockers.append((person_id, self._candidate_blocker(connection, plan, person_id, requirement, exemptions)))
                    if not candidates:
                        reasons.append("无具备该资质且尚未占用的备班人员")
                    for index in range(min(shortage, len(blockers))):
                        person_id, blocker = blockers[index]
                        reasons.append(f"{person_id}: {blocker}")
                    if shortage > len(blockers):
                        reasons.append(f"另有 {shortage - len(blockers)} 个名额没有任何具备资质的候选人员")
                gap_id = new_id("gap")
                connection.execute(
                    "INSERT INTO duty_gaps(gap_id,plan_id,requirement_id,evaluated_at,shift_start_at,shift_end_at,post_id,qualification,required,assigned,shortage,reasons_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (gap_id, plan_id, requirement["requirement_id"], now, requirement["shift_start_at"], requirement["shift_end_at"], requirement["post_id"], requirement["qualification"], requirement["min_people"], covered, shortage, canonical_json(reasons), "open" if shortage else "resolved"))
                if shortage:
                    row = self._gap_row(connection, gap_id)
                    open_gaps.append(row)
                    self._event(connection, actor_id=context.actor_id, event_type="gap_exposed", plan_id=plan_id, detail={"gap_id": gap_id, "post_id": requirement["post_id"], "shortage": shortage, "reasons": reasons, "at": now})
                    self._schedule_gap_reminder(connection, row)
                    already_open = connection.execute("SELECT 1 FROM duty_gaps WHERE requirement_id=? AND status='open' AND gap_id<>? LIMIT 1", (requirement["requirement_id"], gap_id)).fetchone()
                    if not already_open:
                        self._notify(connection, topic="roster.gap", aggregate_id=plan_id, payload={"gap_id": gap_id, "post_id": requirement["post_id"], "shift_start_at": requirement["shift_start_at"], "shortage": shortage, "reasons": reasons})
            for loan in unconfirmed_loans:
                already_notified = connection.execute("SELECT 1 FROM outbox_messages WHERE topic='roster.loan_pending' AND json_extract(payload_json,'$.assignment_id')=? LIMIT 1", (loan["assignment_id"],)).fetchone()
                if not already_notified:
                    self._notify(connection, topic="roster.loan_pending", aggregate_id=plan_id, payload={"assignment_id": loan["assignment_id"], "person_id": loan["person_id"]})
            audit.append(connection, actor_id=context.actor_id, action="roster.gaps_evaluate", entity_type="duty_plans", entity_id=plan_id, version=plan["version"], detail={"open_gaps": len(open_gaps), "unconfirmed_loans": len(unconfirmed_loans)})
        return {"gaps": open_gaps, "unconfirmed_loans": unconfirmed_loans, "evaluated_at": now}

    def dashboard(self, context: AccessContext, plan_id: str) -> dict:
        """服务恢复后主管仍可看到临近班次缺员、未确认借调与休息违规。"""
        context.require("read:rosters")
        with self.database.connect() as connection:
            self._plan_row(connection, plan_id)
            gaps = [self._gap_from_row(row) for row in connection.execute(
                "SELECT * FROM duty_gaps WHERE plan_id=? AND status='open' AND evaluated_at=(SELECT MAX(evaluated_at) FROM duty_gaps WHERE plan_id=?) ORDER BY shift_start_at,post_id",
                (plan_id, plan_id)).fetchall()]
            loans = [dict(row) for row in connection.execute("SELECT * FROM duty_assignments WHERE plan_id=? AND kind='loan' AND status='proposed' ORDER BY start_at", (plan_id,)).fetchall()]
            violations = [self._event_dict(row) for row in connection.execute("SELECT * FROM duty_events WHERE plan_id=? AND event_type='rest_violation' ORDER BY occurred_at,event_id", (plan_id,)).fetchall()]
            exemptions = [dict(row) for row in connection.execute("SELECT assignment_id,person_id,start_at,end_at,exempted_by FROM duty_assignments WHERE plan_id=? AND exempted_by!='' ORDER BY start_at", (plan_id,)).fetchall()]
            return {"plan_id": plan_id, "open_gaps": gaps, "unconfirmed_loans": loans, "rest_violations": violations, "overtime_exemptions": exemptions, "as_of": self.clock.now()}

    def reconciliation_queue(self, context: AccessContext) -> list[dict]:
        """业务标识相同却内容矛盾的签到回执核对队列（复用 inbox_conflicts）。"""
        context.require("read:rosters")
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM inbox_conflicts ORDER BY conflict_id").fetchall()]

    # ---- 时间轴与历史时点回放 ------------------------------------------

    def timeline(self, context: AccessContext, plan_id: str) -> list[dict]:
        context.require("read:rosters")
        with self.database.connect() as connection:
            self._plan_row(connection, plan_id)
            return [self._event_dict(row) for row in connection.execute("SELECT * FROM duty_events WHERE plan_id=? ORDER BY occurred_at,event_id", (plan_id,)).fetchall()]

    def replay(self, context: AccessContext, plan_id: str, *, as_of: str) -> dict:
        """按任一历史时点说明实际在岗人员、调动过程和交接依据。"""
        context.require("read:rosters")
        instant = canonical_instant(as_of)
        with self.database.connect() as connection:
            self._plan_row(connection, plan_id)
            events = [self._event_dict(row) for row in connection.execute("SELECT * FROM duty_events WHERE plan_id=? AND occurred_at<=? ORDER BY occurred_at,event_id", (plan_id, instant)).fetchall()]
            on_duty = [dict(row) for row in connection.execute(
                "SELECT * FROM duty_assignments WHERE plan_id=? AND status='checked_in' AND checked_in_at<=? AND end_at>?",
                (plan_id, instant, instant)).fetchall()]
            movement_types = ("assigned", "loan_proposed", "loan_confirmed", "loan_rejected", "assignment_cancelled", "assignment_recancelled", "travel_arranged", "opening_adjusted", "requirements_revised")
            record_types = ("checked_in", "rest_violation", "exception_note", "handover", "gap_exposed")
            return {"as_of": instant, "on_duty": on_duty, "movements": [e for e in events if e["event_type"] in movement_types], "records": [e for e in events if e["event_type"] in record_types]}

    def get_assignment(self, context: AccessContext, assignment_id: str) -> dict:
        context.require("read:rosters")
        with self.database.connect() as connection:
            return self._assignment_row(connection, assignment_id)

    def list_assignments(self, context: AccessContext, plan_id: str) -> list[dict]:
        context.require("read:rosters")
        with self.database.connect() as connection:
            self._plan_row(connection, plan_id)
            return [self._assignment_from_row(row) for row in connection.execute("SELECT * FROM duty_assignments WHERE plan_id=? ORDER BY start_at,person_id,assignment_id", (plan_id,)).fetchall()]

    def list_requirements(self, context: AccessContext, plan_id: str) -> list[dict]:
        context.require("read:rosters")
        with self.database.connect() as connection:
            self._plan_row(connection, plan_id)
            return [dict(row) for row in connection.execute("SELECT * FROM duty_requirements WHERE plan_id=? ORDER BY shift_start_at,post_id", (plan_id,)).fetchall()]

    # ---- 内部规则 -------------------------------------------------------

    def _create_assignment(self, context: AccessContext, plan_id: str, person_id: str, kind: str, *, requirement_id: str | None = None, start_at: str | None = None, end_at: str | None = None, home_post_id: str | None = None, exemption_id: str = "", request_key: str, initial_status: str = "scheduled", detail: dict | None = None) -> dict:
        require_safe(person_id, "人员标识")
        audit = AuditLog(self.clock); idempotency = IdempotencyStore(self.clock)
        with self.database.transaction() as connection:
            def operation() -> dict:
                plan = self._plan_row(connection, plan_id)
                requirement = None
                if requirement_id:
                    requirement = self._requirement_row(connection, requirement_id)
                    if requirement["plan_id"] != plan_id:
                        raise ValidationError("岗位需求不属于该值守预案")
                start = canonical_instant(start_at) if start_at else requirement["shift_start_at"]
                end = canonical_instant(end_at) if end_at else requirement["shift_end_at"]
                if parse_instant(start) >= parse_instant(end):
                    raise ValidationError("派班结束时间必须晚于开始时间")
                if requirement:
                    if parse_instant(start) < parse_instant(requirement["shift_start_at"]) or parse_instant(end) > parse_instant(requirement["shift_end_at"]):
                        raise ValidationError("派班时段必须落在岗位需求时段内")
                    self._require_credential(connection, person_id, requirement["qualification"])
                # 统一时间轴：正常班、待命、借调、休息、移动、替班不得互相重叠。
                overlap = connection.execute("SELECT assignment_id,kind,status FROM duty_assignments WHERE person_id=? AND status NOT IN ('cancelled','rejected') AND start_at<? AND end_at>?", (person_id, end, start)).fetchone()
                if overlap:
                    raise ConflictError(f"{person_id} 在该时段已有 {overlap['kind']} 派班({overlap['assignment_id']})，同一时间轴不能重复占用")
                exempted_by = ""
                if kind in WORK_KINDS:
                    rest_hours = self._rest_gap(connection, plan, person_id, start)
                    if rest_hours is not None:
                        exempted_by = self._require_exemption(connection, plan=plan, exemption_id=exemption_id, person_id=person_id, start=start, end=end, actor=context.actor_id, rest_hours=rest_hours)
                assignment_id = new_id("asg")
                connection.execute(
                    "INSERT INTO duty_assignments(assignment_id,plan_id,requirement_id,person_id,kind,start_at,end_at,qualification,home_post_id,status,supersedes_assignment_id,exempted_by,created_at,created_by,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (assignment_id, plan_id, requirement_id or "", person_id, kind, start, end, requirement["qualification"] if requirement else "", home_post_id or "", initial_status, "", exempted_by, self.clock.now(), context.actor_id, canonical_json(detail or {})))
                row = self._assignment_row(connection, assignment_id)
                event_type = "loan_proposed" if kind == "loan" else ("travel_arranged" if kind == "travel" else "assigned")
                self._event(connection, actor_id=context.actor_id, event_type=event_type, plan_id=plan_id, assignment_id=assignment_id, detail={"assignment": row, **(detail or {})})
                if kind == "loan" and initial_status == "proposed":
                    self._notify(connection, topic="roster.loan_pending", aggregate_id=plan_id, payload={"assignment_id": assignment_id, "person_id": person_id, "home_post_id": home_post_id or ""})
                audit.append(connection, actor_id=context.actor_id, action="roster.assign", entity_type="duty_assignments", entity_id=assignment_id, version=plan["version"], detail={"person_id": person_id, "kind": kind, "start": start, "end": end, "exempted": bool(exempted_by)})
                return row
            return idempotency.execute(connection, scope=f"roster:assign:{plan_id}:{person_id}:{kind}", request_key=request_key, request={"requirement_id": requirement_id, "start": start_at, "end": end_at, "exemption_id": exemption_id, "detail": detail or {}}, operation=operation)

    def _rest_gap(self, connection, plan: dict, person_id: str, start: str) -> float | None:
        """距上一段工作/移动/待命的休息小时数不足规则要求时返回实际小时数，否则 None。"""
        min_rest = float(plan["rest_rule"]["min_rest_hours"])
        row = connection.execute(
            "SELECT MAX(end_at) AS last_end FROM duty_assignments WHERE person_id=? AND kind IN ('regular','loan','relief','travel','standby') AND status NOT IN ('cancelled','rejected') AND end_at<=?",
            (person_id, start)).fetchone()
        if not row["last_end"]:
            return None
        hours = (parse_instant(start) - parse_instant(row["last_end"])).total_seconds() / 3600
        return round(hours, 2) if hours < min_rest else None

    def _require_exemption(self, connection, *, plan: dict, exemption_id: str, person_id: str, start: str, end: str, actor: str, rest_hours: float) -> str:
        if not exemption_id:
            raise ValidationError(f"{person_id} 距上一班仅休息 {rest_hours} 小时，低于 {plan['rest_rule']['min_rest_hours']} 小时，须先取得无利益关系主管的超时豁免")
        row = connection.execute("SELECT payload_json,state FROM entities WHERE entity_type='approvals' AND entity_id=?", (exemption_id,)).fetchone()
        if not row:
            raise NotFoundError("超时豁免审批不存在")
        payload = json.loads(row["payload_json"])
        if row["state"] != "approved" or payload.get("step") != "超时豁免" or payload.get("case_id") != plan["plan_id"] or payload.get("person_id") != person_id:
            raise ValidationError("超时豁免审批无效或不适用于该人员/预案")
        if parse_instant(payload["start_at"]) > parse_instant(start) or parse_instant(payload["end_at"]) < parse_instant(end):
            raise ValidationError("超时豁免时段不覆盖该派班")
        reviewer = payload.get("reviewer", "")
        if reviewer == actor or reviewer == person_id:
            raise PermissionDenied("超时豁免必须由无利益关系的主管批准")
        return exemption_id

    def _require_credential(self, connection, person_id: str, qualification: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM entities WHERE entity_type='credentials' AND state='effective' AND json_extract(payload_json,'$.holder_type')='person' AND json_extract(payload_json,'$.holder_id')=? AND json_extract(payload_json,'$.credential_type')=? LIMIT 1",
            (person_id, qualification)).fetchone()
        if not row:
            raise ValidationError(f"{person_id} 不具备岗位所需资质 {qualification} 或资质未生效")

    def _candidate_blocker(self, connection, plan: dict, person_id: str, requirement, exemptions: dict) -> str:
        overlap = connection.execute(
            "SELECT kind FROM duty_assignments WHERE person_id=? AND status NOT IN ('cancelled','rejected') AND start_at<? AND end_at>?",
            (person_id, requirement["shift_end_at"], requirement["shift_start_at"])).fetchone()
        if overlap:
            return f"时间轴冲突（已有{overlap['kind']}派班）"
        rest_hours = self._rest_gap(connection, plan, person_id, requirement["shift_start_at"])
        if rest_hours is not None and not self._exemption_covers(exemptions.get(person_id, []), requirement["shift_start_at"], requirement["shift_end_at"]):
            return f"休息不足（仅休息{rest_hours}小时且无覆盖该时段的超时豁免）"
        return "尚未派班（该候选可调派）"

    @staticmethod
    def _exemption_covers(approvals: list[dict], start: str, end: str) -> bool:
        for approval in approvals:
            if parse_instant(approval["start_at"]) <= parse_instant(start) and parse_instant(approval["end_at"]) >= parse_instant(end):
                return True
        return False

    def _credential_holders(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        now = self.clock.now()
        for row in self.repository.list("credentials", state="effective", limit=500):
            if row.get("holder_type") != "person":
                continue
            if row.get("valid_from") and parse_instant(row["valid_from"]) > parse_instant(now):
                continue
            if row.get("valid_to") and parse_instant(row["valid_to"]) < parse_instant(now):
                continue
            result.setdefault(row["credential_type"], []).append(row["holder_id"])
        for holders in result.values():
            holders.sort()
        return result

    def _exemptions(self, plan_id: str) -> dict:
        result: dict[str, list[dict]] = {}
        for row in self.repository.list("approvals", state="approved", limit=500):
            if row.get("step") != "超时豁免" or row.get("case_id") != plan_id:
                continue
            if row.get("person_id"):
                result.setdefault(row["person_id"], []).append(row)
        return result

    def _schedule_gap_reminder(self, connection, gap: dict) -> None:
        run_at = (parse_instant(gap["shift_start_at"]) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        payload = {"gap_id": gap["gap_id"], "plan_id": gap["plan_id"], "post_id": gap["post_id"], "shortage": gap["shortage"]}
        existing = connection.execute("SELECT 1 FROM scheduled_jobs WHERE job_type='roster-gap' AND subject_id=? AND status IN ('waiting','retry','running') AND payload_json=?", (gap["plan_id"], canonical_json(payload))).fetchone()
        if existing:
            return
        connection.execute("INSERT INTO scheduled_jobs(job_id,job_type,subject_id,run_at,payload_json,status) VALUES(?,?,?,?,?,'waiting')", (new_id("job"), "roster-gap", gap["plan_id"], run_at, canonical_json(payload)))

    def _notify(self, connection, *, topic: str, aggregate_id: str, payload: dict) -> None:
        connection.execute("INSERT INTO outbox_messages(message_id,topic,aggregate_id,payload_json,available_at,status) VALUES(?,?,?,?,?,?)", (new_id("msg"), topic, aggregate_id, canonical_json(payload), self.clock.now(), "pending"))

    def _event(self, connection, *, actor_id: str, event_type: str, plan_id: str, assignment_id: str = "", detail: dict | None = None) -> None:
        connection.execute("INSERT INTO duty_events(occurred_at,actor_id,event_type,plan_id,assignment_id,detail_json) VALUES(?,?,?,?,?,?)", (self.clock.now(), actor_id, event_type, plan_id, assignment_id, canonical_json(detail or {})))

    @staticmethod
    def _event_dict(row) -> dict:
        item = dict(row); item["detail"] = json.loads(item.pop("detail_json")); return item

    @staticmethod
    def _plan_row(connection, plan_id: str) -> dict:
        row = connection.execute("SELECT * FROM duty_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if not row:
            raise NotFoundError(f"值守预案 {plan_id} 不存在")
        result = dict(row); result["rest_rule"] = json.loads(result.pop("rest_rule_json")); return result

    @staticmethod
    def _requirement_row(connection, requirement_id: str) -> dict:
        row = connection.execute("SELECT * FROM duty_requirements WHERE requirement_id=?", (requirement_id,)).fetchone()
        if not row:
            raise NotFoundError(f"岗位需求 {requirement_id} 不存在")
        return dict(row)

    @staticmethod
    def _assignment_row(connection, assignment_id: str) -> dict:
        row = connection.execute("SELECT * FROM duty_assignments WHERE assignment_id=?", (assignment_id,)).fetchone()
        if not row:
            raise NotFoundError(f"派班 {assignment_id} 不存在")
        return RosterService._assignment_from_row(row)

    @staticmethod
    def _assignment_from_row(row) -> dict:
        result = dict(row); result["detail"] = json.loads(result.pop("detail_json") or "{}"); return result

    @staticmethod
    def _gap_row(connection, gap_id: str) -> dict:
        row = connection.execute("SELECT * FROM duty_gaps WHERE gap_id=?", (gap_id,)).fetchone()
        return RosterService._gap_from_row(row)

    @staticmethod
    def _gap_from_row(row) -> dict:
        result = dict(row); result["reasons"] = json.loads(result.pop("reasons_json")); return result

    @staticmethod
    def _require_text(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{label}不能为空")
        text = value.strip()
        if len(text) > 128:
            raise ValidationError(f"{label}长度不能超过128")
        return text

    @staticmethod
    def _normalize_requirement(item: dict) -> dict:
        if not isinstance(item, dict):
            raise ValidationError("岗位需求必须是对象")
        required = ("post_id", "shift_start_at", "shift_end_at", "qualification", "min_people")
        missing = [key for key in required if key not in item]
        if missing:
            raise ValidationError("岗位需求缺少字段: " + ", ".join(missing))
        if not isinstance(item["min_people"], int) or isinstance(item["min_people"], bool) or item["min_people"] < 1:
            raise ValidationError("最低在岗人数必须为正整数")
        return {"post_id": RosterService._require_text(item["post_id"], "岗位标识"), "shift_start_at": canonical_instant(item["shift_start_at"]), "shift_end_at": canonical_instant(item["shift_end_at"]), "qualification": RosterService._require_text(item["qualification"], "资质标识"), "min_people": item["min_people"]}
