# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行离线演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```

## 高峰值守排班

假期联合指挥演练等高峰场景由 `rosters` 承载：口岸按客流情景登记岗位、时段、资质与最低人数；
正常班、待命、借调、休息、跨口岸移动与替班共用同一条人员时间轴，时间重叠直接拒绝。

- 排班结果通过 `evaluate_gaps` 逐岗位暴露缺员及原因（时间轴冲突、休息不足且无豁免、无具备资质人员）。
- 借调须经原部门与接收岗位双方（不同人）分别确认后才计入在岗；超时豁免由无利益关系主管在 `approvals` 中审批。
- 签到回执走进件层：相同签到只记一次，业务标识相同而内容矛盾的回执进入 `inbox_conflicts` 核对队列；签到后在岗事实冻结，只能追加异常说明与交接记录。
- 开放时间或客流情景调整时，已开始班次不重算，仅取消尚未开始且确实受影响的派班；班前一小时的缺员提醒落库为可恢复的持久任务。
- `duty_events` 只追加，支持按任一历史时点回放实际在岗人员、调动过程与交接依据。

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 roster-demo
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 roster-dashboard <plan_id>
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 roster-replay <plan_id> --as-of 2026-10-01T01:00:00+08:00
```
