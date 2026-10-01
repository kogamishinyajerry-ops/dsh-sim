"""持久作业队列（定义书 §作业生命周期 / §并发、断线、取消与补算规则；WP-08）。

要点：
- enqueue / claim / post_event / heartbeat / expire_lost_leases / request_cancel。
- claim 在一个短事务内完成"锁行-校验配额-分配租约-提交"：
  SQLite 依赖 engine 的 BEGIN IMMEDIATE（db/session.py）串行化并发 claim；
  PostgreSQL 部署时改为 SELECT ... FOR UPDATE SKIP LOCKED 选行，其余逻辑不变。
- fencing_token 单调递增（job.fencing_counter）；Worker 只接受当前 token，
  控制器只接受该节点该租约的事件。
- 取消是异步语义：request_cancel 只入库请求；未出租作业现场确认后直接 CANCELLED，
  已出租作业转 CANCELLING，等 Worker 现场确认事件才记 CANCELLED；
  无法证实退出 → LOST，不释放为可重跑（FR-12）。
- 晚到完成不得覆盖已撤销状态：事件始终落审计日志，但 Job/Attempt/Run 处于
  终态时不再迁移状态。

首版工程默认（定义书 §并发断线取消与补算规则，可按站点测试调整）：
心跳 15s、租约 90s、失联 3 次心跳触发告警，租约到期转 LOST 待核实。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from dsh_sim.db.models import (
    AttemptRow,
    EventRow,
    JobRow,
    LeaseRow,
    NodeRow,
    RunRow,
    utcnow,
)
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.states import (
    EXECUTION_TERMINAL,
    ExecutionState,
    can_transition,
)

# 控制参数（软件参数，非工程阈值；可按站点测试调整）
HEARTBEAT_INTERVAL_SECONDS = 15
LEASE_SECONDS = 90
HEARTBEAT_MISS_ALERT = 3

# 允许 Worker 上报的事件 kind（OpenAPI postJobEvent 枚举）
EVENT_KINDS = frozenset(
    {"STARTING", "RUNNING", "HEARTBEAT", "COMPLETED", "FAILED", "CANCELLED"}
)

_CLAIMABLE = (ExecutionState.QUEUED.value, ExecutionState.WAITING_RESOURCE.value)

# LOST 尚不能证明进程退出，继续占用节点/任务预算，禁止并行重派造成双跑。
_LEASED_ACTIVE = (
    ExecutionState.LEASED.value,
    ExecutionState.STARTING.value,
    ExecutionState.RUNNING.value,
    ExecutionState.CANCELLING.value,
    ExecutionState.COLLECTING.value,
    ExecutionState.LOST.value,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _as_utc(dt: datetime) -> datetime:
    """SQLite 读回的 datetime 不带 tz；按 UTC 解释（PG timestamptz 不受影响）。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 入队
# ---------------------------------------------------------------------------


def enqueue(
    session: Session,
    *,
    kind: str,
    task_id: str | None = None,
    run_id: str | None = None,
    attempt_id: str | None = None,
) -> JobRow:
    """持久入队：会话退出不取消作业（FR-09）。"""
    job = JobRow(
        job_id=_new_id("job"),
        kind=kind,
        task_id=task_id,
        run_id=run_id,
        attempt_id=attempt_id,
        state=ExecutionState.QUEUED.value,
    )
    session.add(job)
    session.flush()
    return job


# ---------------------------------------------------------------------------
# 领取
# ---------------------------------------------------------------------------


def claim(
    session: Session,
    *, node_id: str, capabilities: dict | None = None, now: datetime | None = None
) -> tuple[JobRow | None, LeaseRow | None]:
    """Worker 领取作业。

    短事务语义（调用方须在事务内调用）：锁队列行 → 校验配额 → 分配租约 → 提交。
    不跨求解周期持有数据库锁。

    SQLite：由 BEGIN IMMEDIATE 保证两个并发 claim 只有一个先进入写事务，
    第二个读到的是已提交的新状态 → 同一 Job 只有一个赢家。
    PostgreSQL：改为
        SELECT ... FROM jobs WHERE state IN ('QUEUED','WAITING_RESOURCE')
        ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
    其余逻辑（配额、租约、fencing）不变。
    """
    now = now or utcnow()

    node = session.get(NodeRow, node_id)
    if node is None:
        # 开发模式：首次 claim 自动登记节点；生产须由 NODE_ADMIN 显式注册（TBD-07/TBD-08）
        node = NodeRow(node_id=node_id, capabilities=capabilities or {})
        session.add(node)
        session.flush()

    # 配额：节点并发上限（站点配额近似，不冒充许可证服务器真实余量）
    active_on_node = (
        session.query(JobRow)
        .filter(JobRow.node_id == node_id, JobRow.state.in_(_LEASED_ACTIVE))
        .count()
    )
    if active_on_node >= node.max_concurrent:
        return None, None

    job = (
        session.query(JobRow)
        .filter(JobRow.state.in_(_CLAIMABLE))
        .order_by(JobRow.created_at)
        .first()
    )
    if job is None:
        return None, None

    # 任务级并发预算：EXECUTE 作业受 execution_budget.max_concurrent 约束（FR-10）
    if job.kind == "EXECUTE" and job.task_id is not None:
        task_budget = _task_max_concurrent(session, job.task_id)
        active_for_task = (
            session.query(JobRow)
            .filter(
                JobRow.task_id == job.task_id,
                JobRow.kind == "EXECUTE",
                JobRow.state.in_(_LEASED_ACTIVE),
                JobRow.job_id != job.job_id,
            )
            .count()
        )
        if active_for_task >= task_budget:
            job.state = ExecutionState.WAITING_RESOURCE.value
            job.row_version += 1
            session.flush()
            return None, None

    job.fencing_counter += 1
    lease = LeaseRow(
        lease_id=_new_id("lease"),
        job_id=job.job_id,
        node_id=node_id,
        fencing_token=job.fencing_counter,
        acquired_at=now,
        expires_at=now + timedelta(seconds=LEASE_SECONDS),
        last_heartbeat_at=now,
        active=True,
    )
    session.add(lease)
    job.state = ExecutionState.LEASED.value
    job.node_id = node_id
    job.row_version += 1
    session.flush()

    # 同步 attempt 租约身份（EXECUTE 作业）
    if job.attempt_id:
        attempt = session.get(AttemptRow, job.attempt_id)
        if attempt is not None:
            attempt.node_id = node_id
            attempt.lease_id = lease.lease_id
            attempt.fencing_token = lease.fencing_token
            attempt.state = ExecutionState.LEASED.value
    # Run 执行状态是当前 attempt 的投影：出租时同步 QUEUED→LEASED，
    # 后续事件（STARTING/RUNNING/...）才能沿状态机传播
    if job.run_id:
        run = session.get(RunRow, job.run_id)
        if run is not None and can_transition(
            ExecutionState(run.execution_state), ExecutionState.LEASED
        ):
            run.execution_state = ExecutionState.LEASED.value
    session.flush()
    return job, lease


def _task_max_concurrent(session: Session, task_id: str) -> int:
    """读取任务当前修订的 execution_budget.max_concurrent；缺省 1（保守）。"""
    from dsh_sim.db.models import TaskRevisionRow, TaskRow

    task = session.get(TaskRow, task_id)
    if task is None or task.current_revision == 0:
        return 1
    rev = (
        session.query(TaskRevisionRow)
        .filter_by(task_id=task_id, revision=task.current_revision)
        .first()
    )
    if rev is None:
        return 1
    budget = (rev.spec or {}).get("execution_budget") or {}
    return int(budget.get("max_concurrent", 1))


# ---------------------------------------------------------------------------
# 事件上报
# ---------------------------------------------------------------------------


def _current_lease(session: Session, job_id: str) -> LeaseRow | None:
    return (
        session.query(LeaseRow)
        .filter(LeaseRow.job_id == job_id, LeaseRow.active.is_(True))
        .order_by(LeaseRow.fencing_token.desc())
        .first()
    )


def post_event(
    session: Session,
    *,
    job_id: str,
    lease_id: str,
    fencing_token: int,
    event_seq: int,
    kind: str,
    payload: dict | None = None,
    now: datetime | None = None,
) -> EventRow:
    """接收 Worker 事件：校验节点租约、fencing_token、事件序号与状态转换。

    - 租约/token 不匹配（旧 Worker 晚到）→ 409，事件不生效；
    - event_seq 必须严格递增（恢复后按序重传）；重复序号视为幂等重发，返回已存事件；
    - 晚到完成不得覆盖已撤销：Job 已在终态时事件仍入审计日志，但不迁移任何状态。
    """
    now = now or utcnow()
    payload = payload or {}
    if kind not in EVENT_KINDS:
        raise ApiError(
            ErrorCode.VALIDATION, f"未知事件 kind: {kind}", details={"kind": kind}
        )

    job = session.get(JobRow, job_id)
    if job is None:
        raise ApiError(ErrorCode.VALIDATION, "job 不存在", details={"job_id": job_id})
    # 长运行 Worker 持有 identity map；先回读刚提交的取消/LOST，不能用旧状态覆盖它。
    session.refresh(job)

    lease = _current_lease(session, job_id)
    if (
        lease is None
        or lease.lease_id != lease_id
        or lease.fencing_token != fencing_token
    ):
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "租约或 fencing_token 已失效：Worker 只接受当前 token，控制器只接受当前租约的事件",
            details={"job_id": job_id},
        )

    last_seq = (
        session.query(EventRow.event_seq)
        .filter(EventRow.job_id == job_id)
        .order_by(EventRow.event_seq.desc())
        .first()
    )
    last_seq = last_seq[0] if last_seq else 0
    if event_seq <= last_seq:
        existing = (
            session.query(EventRow)
            .filter_by(job_id=job_id, event_seq=event_seq)
            .first()
        )
        if existing is not None and existing.kind == kind:
            return existing  # 幂等重发
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "event_seq 必须严格递增且 (job_id,event_seq) 唯一",
            details={"job_id": job_id, "expected": last_seq + 1, "got": event_seq},
        )
    if event_seq != last_seq + 1:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "事件序号出现空洞：恢复后须按序重传",
            details={"job_id": job_id, "expected": last_seq + 1, "got": event_seq},
        )

    event = EventRow(
        event_id=_new_id("evt"),
        job_id=job_id,
        event_seq=event_seq,
        kind=kind,
        payload=payload,
        occurred_at=now,
    )
    session.add(event)

    if kind == "HEARTBEAT":
        if job.state not in EXECUTION_TERMINAL and job.state != ExecutionState.LOST.value:
            lease.last_heartbeat_at = now
            lease.expires_at = now + timedelta(seconds=LEASE_SECONDS)
        session.flush()
        return event

    target = _target_state_for_event(kind)
    # 保持冻结的六种事件 kind；失败事件内明确退出未证实，投影为既有 LOST 状态。
    if kind == "FAILED" and payload.get("exit_unconfirmed") is True:
        target = ExecutionState.LOST

    # 晚到完成不得覆盖已撤销/已终态：事件已入审计日志，状态不再迁移。
    if job.state in EXECUTION_TERMINAL or job.state == ExecutionState.LOST.value:
        session.flush()
        return event

    current = ExecutionState(job.state)
    # COMPLETED 事件语义含"产物收集完成"（定义书 §作业生命周期：求解→收集→完成）。
    # Worker 事件协议无独立 COLLECTING kind，队列层沿 RUNNING→COLLECTING→SUCCEEDED
    # 逐步迁移；非法中间态（如 STARTING 直接 COMPLETED）仍按原样跳过，不冒充成功。
    if kind == "COMPLETED" and can_transition(current, ExecutionState.COLLECTING):
        _apply_execution_state(session, job, ExecutionState.COLLECTING, now, payload)
        current = ExecutionState.COLLECTING
    if target is not None and can_transition(current, target):
        _apply_execution_state(session, job, target, now, payload)
    session.flush()
    return event


def _target_state_for_event(kind: str) -> ExecutionState | None:
    return {
        "STARTING": ExecutionState.STARTING,
        "RUNNING": ExecutionState.RUNNING,
        "COMPLETED": ExecutionState.SUCCEEDED,
        "FAILED": ExecutionState.FAILED,
        "CANCELLED": ExecutionState.CANCELLED,
    }.get(kind)


def _apply_execution_state(
    session: Session, job: JobRow, target: ExecutionState, now: datetime, payload: dict
) -> None:
    """迁移 Job 及其关联 Attempt/Run 的执行状态。"""
    job.state = target.value
    job.row_version += 1

    if job.attempt_id:
        attempt = session.get(AttemptRow, job.attempt_id)
        if attempt is not None and attempt.state not in EXECUTION_TERMINAL:
            if can_transition(ExecutionState(attempt.state), target):
                attempt.state = target.value
                if "process_identity" in payload:
                    value = payload["process_identity"]
                    attempt.process_identity = value if isinstance(value, dict) else {"identity": value}
                if target == ExecutionState.STARTING:
                    attempt.started_at = now
                if target in EXECUTION_TERMINAL:
                    attempt.ended_at = now

    if job.run_id:
        run = session.get(RunRow, job.run_id)
        if run is not None and run.execution_state not in EXECUTION_TERMINAL:
            if can_transition(ExecutionState(run.execution_state), target):
                run.execution_state = target.value

    # 注意：终态后不停用租约。租约自然到期由 expire_lost_leases 回收（此时 Job 已终态
    # 不会转 LOST）；保持租约有效可让迟到的终态后事件落入审计日志但不覆盖状态（§数据库约束）。


# ---------------------------------------------------------------------------
# 心跳 / 失联处理
# ---------------------------------------------------------------------------


def heartbeat(
    session: Session,
    *, job_id: str, lease_id: str, fencing_token: int, now: datetime | None = None
) -> None:
    """轻量心跳（不携带事件序号的通道）；事件通道请用 post_event(HEARTBEAT)。"""
    now = now or utcnow()
    lease = _current_lease(session, job_id)
    if (
        lease is None
        or lease.lease_id != lease_id
        or lease.fencing_token != fencing_token
    ):
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "心跳携带的租约/fencing_token 已失效",
            details={"job_id": job_id},
        )
    lease.last_heartbeat_at = now
    job = session.get(JobRow, job_id)
    if job is not None:
        session.refresh(job)
    if job is not None and job.state not in EXECUTION_TERMINAL and job.state != ExecutionState.LOST.value:
        lease.expires_at = now + timedelta(seconds=LEASE_SECONDS)
    session.flush()


def expire_lost_leases(
    session: Session,
    *,
    now: datetime | None = None,
    heartbeat_seconds: int = HEARTBEAT_INTERVAL_SECONDS,
    lease_seconds: int = LEASE_SECONDS,
    miss_alert: int = HEARTBEAT_MISS_ALERT,
) -> list[str]:
    """租约到期/失联处理：失联 ≥3 次心跳触发告警，租约到期转 LOST 待核实。

    LOST 不释放为可重跑：冻结重派，核实现场后人工裁定（定义书 §作业生命周期）。
    返回转 LOST 的 job_id 列表。
    """
    now = now or utcnow()
    lost_jobs: list[str] = []
    leases = session.query(LeaseRow).filter(LeaseRow.active.is_(True)).all()
    for lease in leases:
        last_hb = _as_utc(lease.last_heartbeat_at)
        expires = _as_utc(lease.expires_at)
        missed = (now - last_hb).total_seconds() / max(heartbeat_seconds, 1)
        expired = now >= expires
        # missed >= miss_alert 触发告警（此处不单独留痕，由事件/监控层消费）；
        # 租约到期即转 LOST 待核实
        if not expired:
            continue
        lease.active = False
        job = session.get(JobRow, lease.job_id)
        if job is None or job.state in EXECUTION_TERMINAL:
            continue
        current = ExecutionState(job.state)
        if can_transition(current, ExecutionState.LOST):
            _apply_execution_state(session, job, ExecutionState.LOST, now, {})
            lost_jobs.append(job.job_id)
    session.flush()
    return lost_jobs


# ---------------------------------------------------------------------------
# 取消（异步语义）
# ---------------------------------------------------------------------------


def request_cancel(session: Session, *, job_id: str, now: datetime | None = None) -> JobRow:
    """取消请求入库。取消请求 ≠ 已停止（FR-12）：

    - 未出租（QUEUED/WAITING_RESOURCE）：无外部进程，现场确认成立 → 直接 CANCELLED；
    - 已出租/运行中：转 CANCELLING，等 Worker 报 CANCELLED 事件现场确认；
    - 终态作业：幂等返回，不报错。
    """
    now = now or utcnow()
    job = session.get(JobRow, job_id)
    if job is None:
        raise ApiError(ErrorCode.VALIDATION, "job 不存在", details={"job_id": job_id})
    if job.state in EXECUTION_TERMINAL:
        return job
    job.cancel_requested = True
    current = ExecutionState(job.state)
    if current in (ExecutionState.QUEUED, ExecutionState.WAITING_RESOURCE):
        job.state = ExecutionState.CANCELLED.value
        job.row_version += 1
        if job.attempt_id:
            attempt = session.get(AttemptRow, job.attempt_id)
            if attempt is not None and attempt.state not in EXECUTION_TERMINAL:
                attempt.state = ExecutionState.CANCELLED.value
                attempt.ended_at = now
        if job.run_id:
            run = session.get(RunRow, job.run_id)
            if run is not None and run.execution_state not in EXECUTION_TERMINAL:
                run.execution_state = ExecutionState.CANCELLED.value
    elif can_transition(current, ExecutionState.CANCELLING):
        job.state = ExecutionState.CANCELLING.value
        job.row_version += 1
        if job.attempt_id:
            attempt = session.get(AttemptRow, job.attempt_id)
            if attempt is not None and can_transition(ExecutionState(attempt.state), ExecutionState.CANCELLING):
                attempt.state = ExecutionState.CANCELLING.value
        if job.run_id:
            run = session.get(RunRow, job.run_id)
            if run is not None and can_transition(
                ExecutionState(run.execution_state), ExecutionState.CANCELLING
            ):
                run.execution_state = ExecutionState.CANCELLING.value
    session.flush()
    return job


def find_active_job_for_run(session: Session, run_id: str) -> JobRow | None:
    return (
        session.query(JobRow)
        .filter(
            JobRow.run_id == run_id,
            JobRow.state.notin_(sorted(EXECUTION_TERMINAL | {ExecutionState.LOST})),
        )
        .order_by(JobRow.created_at.desc())
        .first()
    )


__all__ = [
    "EVENT_KINDS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_MISS_ALERT",
    "LEASE_SECONDS",
    "claim",
    "enqueue",
    "expire_lost_leases",
    "find_active_job_for_run",
    "heartbeat",
    "post_event",
    "request_cancel",
]
