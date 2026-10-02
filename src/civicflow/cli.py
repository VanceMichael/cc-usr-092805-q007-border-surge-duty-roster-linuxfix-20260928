"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def demo(app: CivicFlow) -> dict:
    context = AccessContext.system("demo-operator")
    cases = CaseService(app.repository)
    created = cases.create(context, {"case_type": "协同事项", "subject": "示例联合处置", "owner_org": "org:demo", "priority": "high", "opened_at": app.clock.now()}, request_key="demo-case")
    accepted = app.inbox.receive(source="demo", source_key=created["entity_id"], sequence=1, payload={"kind": "opened"}, occurred_at=app.clock.now())
    reservation = app.reservations.reserve(resource_id="room:joint", subject_id=created["entity_id"], quantity=2, capacity=10, start_at="2026-09-28T10:00:00+08:00", end_at="2026-09-28T11:00:00+08:00", actor=context.actor_id)
    debit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="debit", reference="demo-debit", actor=context.actor_id)
    credit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="credit", reference="demo-credit", actor=context.actor_id)
    message = app.outbox.enqueue(topic="case.opened", aggregate_id=created["entity_id"], payload={"case_id": created["entity_id"]})
    roster = roster_demo(app)
    return {"case": created, "inbox": accepted, "reservation": reservation, "entries": [debit, credit], "balance": app.ledger.balance("demo", currency="CNY"), "message_id": message, "roster": roster, "verification": app.verify()}


def roster_demo(app: CivicFlow) -> dict:
    """假期联合指挥演练：港珠澳大桥口岸延长开放两小时的高峰值守。"""
    planner = AccessContext.system("duty-planner")
    supervisor = AccessContext.system("duty-supervisor")
    neutral = AccessContext.system("neutral-duty-chief")
    home = AccessContext.system("gongbei-home-dept")
    receiving = AccessContext.system("bridge-receiving-post")
    rosters = app.rosters
    plan = rosters.create_plan(planner, scenario="假期高峰客流，大桥口岸开放时间延长两小时", port_id="port:bridge", opening_start_at="2026-09-30T08:00:00+08:00", opening_end_at="2026-10-01T02:00:00+08:00", request_key="demo-plan")
    for holder, qualification in (("person:li", "边检查验"), ("person:wang", "边检查验"), ("person:zhao", "边检查验"), ("person:sun", "车辆引导")):
        app.repository.create("credentials", {"holder_type": "person", "holder_id": holder, "credential_type": qualification, "issuer": "org:customs", "valid_from": "2026-01-01T00:00:00+08:00", "valid_to": "2027-01-01T00:00:00+08:00", "state": "effective"}, actor=planner.actor_id, request_key=f"demo-cred-{holder}")
    req_night = rosters.add_requirement(planner, plan["plan_id"], post_id="post:bridge-inspection", shift_start_at="2026-10-01T00:00:00+08:00", shift_end_at="2026-10-01T02:00:00+08:00", qualification="边检查验", min_people=2, request_key="demo-req-night")
    req_day = rosters.add_requirement(planner, plan["plan_id"], post_id="post:bridge-inspection", shift_start_at="2026-09-30T20:00:00+08:00", shift_end_at="2026-10-01T00:00:00+08:00", qualification="边检查验", min_people=2, request_key="demo-req-day")
    # 小李刚下白班，夜间替班休息不足，先由无利益关系主管批准超时豁免。
    rosters.assign(planner, plan["plan_id"], "person:li", "regular", requirement_id=req_day["requirement_id"], request_key="demo-asg-li-day")
    rosters.assign(planner, plan["plan_id"], "person:zhao", "regular", requirement_id=req_day["requirement_id"], request_key="demo-asg-zhao-day")
    exemption = rosters.grant_overtime_exemption(neutral, plan_id=plan["plan_id"], person_id="person:li", start_at="2026-10-01T00:00:00+08:00", end_at="2026-10-01T02:00:00+08:00", reason="夜间延长窗口无其他可用查验员", requester_id=supervisor.actor_id, request_key="demo-exemption")
    settled = rosters.settle_overtime_exemption(neutral, exemption["entity_id"], approve=True, reason="同意超时两小时，落实补休", request_key="demo-exemption-settle")
    rosters.assign(planner, plan["plan_id"], "person:li", "relief", requirement_id=req_night["requirement_id"], exemption_id=settled["entity_id"], request_key="demo-asg-li-night")
    # 小王从拱北口岸借调，原部门与接收岗位分别确认。
    loan = rosters.propose_loan(planner, plan["plan_id"], req_night["requirement_id"], "person:wang", home_post_id="post:gongbei-inspection", request_key="demo-loan-wang")
    rosters.confirm_loan(home, loan["assignment_id"], "home", request_key="demo-loan-home")
    loan = rosters.confirm_loan(receiving, loan["assignment_id"], "receiving", request_key="demo-loan-recv")
    gaps = rosters.evaluate_gaps(supervisor, plan["plan_id"])
    dashboard = rosters.dashboard(supervisor, plan["plan_id"])
    return {"plan_id": plan["plan_id"], "loan_status": loan["status"], "open_gaps": len(dashboard["open_gaps"]), "unconfirmed_loans": len(dashboard["unconfirmed_loans"]), "evaluated_gaps": gaps["gaps"], "dashboard": dashboard}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    roster_demo_cmd = commands.add_parser("roster-demo")
    dashboard_cmd = commands.add_parser("roster-dashboard")
    dashboard_cmd.add_argument("plan_id")
    replay_cmd = commands.add_parser("roster-replay")
    replay_cmd.add_argument("plan_id")
    replay_cmd.add_argument("--as-of", required=True)
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    elif args.command == "roster-demo": emit(roster_demo(app))
    elif args.command == "roster-dashboard": emit(app.rosters.dashboard(AccessContext.system("cli"), args.plan_id))
    elif args.command == "roster-replay": emit(app.rosters.replay(AccessContext.system("cli"), args.plan_id, as_of=args.as_of))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
